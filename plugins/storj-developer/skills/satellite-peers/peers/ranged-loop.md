# Peer card: ranged-loop

Verified 2026-10-09 against storj/storj `main`, storj/infra and prod metrics/logs. Line numbers drift — search by symbol.
gc-bf runs its **own, separate** ranged loop — see [gc-bf.md](gc-bf.md). Two processes, two loops; never mix them.

## Identity
- Subcommand `satellite ranged-loop` → `satellite/run/rangedloop.go` (`RangedLoop.GetSelector` = `Observability` + `*rangedloop.Service`).
- `app="satellite-ranged-loop"`, 1 replica per region, namespace `satellite`.
  **slc also runs a QA loop** in namespace `satellite-qa` with the same app and pod names — always pin the namespace.
- Scans every segment in the metabase in parallel ranges and feeds each batch to a set of observers.
  The loop is shared infrastructure: when it is slow or an observer fails, repair, audit, node payouts and piece counts go stale.

## What runs inside
Observers are chosen in infra with `--components=` (`shared/modular/cli/cmdmud.go`, prefixes in `shared/modular/selector.go`).
All observers are `mud.Optional`, so only the named ones run. Prod (all 4 regions, `helm/satellites/<r>/satellite-ranged-loop.yaml`):

| Observer (metric label `observer`) | Package | Feeds |
|---|---|---|
| `_rangedloop_LiveCountObserver` | `satellite/metabase/rangedloop` | progress + scan coverage check |
| `_metrics_Observer` | `satellite/metrics` | segment/object totals |
| `_audit_Observer` | `satellite/audit` | audit queue |
| `_nodetally_Observer` | `satellite/accounting/nodetally` | node at-rest storage → **SNO payouts** |
| `_checker_Observer` | `satellite/repair/checker` | **repair queue** (jobq) |
| `_piecetracker_Observer` | `satellite/gc/piecetracker` | node piece counts (used by gc-bf sizing) |
| `_rangedloop_SequenceObserver` | `satellite/durability` | durability report; one class per run, shuffled; eventkit only |

Loop shape (`satellite/metabase/rangedloop/service.go`): `Run` → every `ranged-loop.interval` → `RunOnce` →
`Start` all observers → split segments into `parallelism` ranges → each range: `Fork`, `Process` batches → `Join` → `Finish`.

| | us1 | eu1 | ap1 | slc |
|---|---|---|---|---|
| parallelism / batch-size | 30 / 15000 | 4 / 5000 | 1 / 10000 | 1 / 5000 |
| interval | 2h (default) | 5h | 5h | 10h |
| run length (8d range, 2026-10) | 5.2–7.3h | 3.6–5.0h | 1.6–2.5h (longer since 10-03, unexplained) | ~3.5 min |
| "stalled" if no completed run for | > ~10h | > ~11h | > ~8h | > ~11h |
| segments | ~9.7B | ~1.3B | ~126M | ~4.2M |
| DB | TiDB | TiDB | TiDB | TiDB |

Normalize for this before comparing regions. A region that differs is a question, not a defect.

## Key signals
Selector `S` = `app="satellite-ranged-loop",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"`.
Loop scope `L` = `scope="storj_io_storj_satellite_metabase_rangedloop"`.

| Question | PromQL |
|---|---|
| Is a run in progress? | `max(function{field="current",S,L,name="__Service__RunOnce"})` (1 = running) |
| Runs completed / failed | query **R1** below |
| Last run duration (s) | `max(function_times{field="recent",kind="success",S,L,name="__Service__RunOnce"})` |
| Time since last completed run (s) | `time() - tlast_change_over_time(function{field="successes",S,L,name="__Service__RunOnce"}[7d])` (MetricsQL) — best stall signal |
| Did an observer fail (last run)? | `min by (observer) (completed_observer_duration{field="duration",S}) == -1` |
| Did an observer fail (last 7d)? | `min by (environment_name, observer) (min_over_time(completed_observer_duration{field="duration",S}[7d])) < 0` |
| Per-observer time (s, cumulative over ranges) | `max by (observer) (completed_observer_duration{field="duration",S})` |
| Live progress | `max(rangedloop_live{field="num_segments",S})` (resets to 0 each run) |
| Scan rate (seg/s) | `clamp_min(deriv(max(rangedloop_live{field="num_segments",S})[15m:]), 0)` |
| Active range workers | `max(function{field="current",S,L,name="__Service__RunOnce_createGoroutineClosure_func4"})` (plateau = parallelism) |
| Scan coverage | `segmentloop_verify_processed{field="recent",S}` ÷ `segmentloop_verify_before{field="recent",S}`; `segmentloop_verify_outside_ratio{field="recent",S}` |
| DB batch latency (s) | `max(function_times{field="r50",S,name="__tagsqlLoopSegmentIterator__doNextQuery"})`, same with `r99` (tagsql wrapper — same name on every DB backend) |
| Observer Start/Join/Finish time | query **R2** below — lives in the **observer's** scope |
| Pod replaced (deploy/OOM) | range query `max by (pod) (kube_pod_start_time{environment_name="...",namespace="satellite",pod=~"satellite-ranged-loop.*"})` — gives exact start times |
| Live version | `count by (image) (kube_pod_container_info{environment_name="...",namespace="satellite",container="satellite",pod=~"satellite-ranged-loop.*"})` |

```promql
# R1: runs completed / failed in 24h
sum by (field) (increase(function{field=~"successes|failures",app="satellite-ranged-loop",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1",scope="storj_io_storj_satellite_metabase_rangedloop",name="__Service__RunOnce"}[24h]))
# R2: per-observer Start/Join/Finish (s)
max by (scope, name) (function_times{name=~"__Observer__(Start|Join|Finish)",field="recent",kind="success",app="satellite-ranged-loop",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"})
```

Baseline 2026-10-09: 0 `failures` in all regions; all 7 observers present with no `-1`; checker is the slowest
observer (us1 ~211k s cumulative, run ~5.2h). us1 batch latency r50 ~15ms (it was ~0.5s in 2026-09 during TiKV
read-pool saturation — a jump here points at the DB). us1 workers plateau at 30 = parallelism. All regions run TiDB — the `ranged-loop.testing-spanner-query-type` flag in eu1/ap1/slc values is a leftover, not the DB in use.

## Silent failures (the main job of this card)
The loop's error contract is forgiving on purpose, so a broken observer can be skipped for weeks while runs "succeed":
- **`Start` fails** → that observer is excluded from the run (`startObserver`); run still counts as success. Shows as duration `-1`.
- **`Process` fails in one range** → that observer is **not joined or finished for the whole run** (`processBatch` + `finishObserver`);
  its results for the cycle are thrown away. Other observers are unaffected. Shows as `-1`.
- **`Join` / `Finish` fails** → logged, `-1`, run still succeeds.
- **`Run` swallows** every non-cancel error and waits for the next interval. Only `RunOnce` `failures` shows it
  (infra errors: `CreateRanges`, `Iterate`).
- `mon.Event("rangedloop_error")` and `ranged_loop_suspicious_segments_count` **never reach Prometheus** (Events don't). Use `function{field="failures"}`.
- Scan coverage below `suspicious-processed-ratio` (0.03) never fails the run — ap1 drifted to ~0.02 silently in 2026-09.
- Inside observers:
  - checker: "error adding injured segment to queue" → **`return nil`** — those segments never reach repair.
  - nodetally: "unrecognized node alias in ranged-loop tally" → `continue` — that node's at-rest bytes (payout) dropped.
  - piecetracker: "error updating nodes piece counts" swallowed; Finish always returns nil.

## Logs
Ranged-loop ships **info** level (few lines: ~3 per run per region), so a missing message usually really means it did not happen —
but first confirm the run's "ranged loop started"/"finished" rows exist for that region.
- `rangedloop:service`: info "ranged loop started" (fields `parallelism`, `batch_size`, `observers`, `asofsystem_interval`, `stale_interval`),
  "ranged loop finished"; error "ranged loop failure".
- "Starting observer failed. This observer will be excluded from this run of the ranged segment loop."
- "Observer failed during Process(), it will not be finalized in this run of the ranged segment loop" (also `Join()`); "Observer failed during Finish()".
- `checker:observer` (set to info in prod): error "error adding injured segment to queue"; warn "checker found irreparable segment" (field `unavailable_node_ids`) —
  data-loss adjacent, the most serious line here.
- "ranged loop failure" has no `error=` field, only a stack trace (each frame a separate row). **The cause is in
  stderr rows starting with `---`** right next to it (recipe in `satellite-observability`).
- **Deploys produce a benign "ranged loop failure"**: the old pod is stopped mid-run, `RunOnce` returns
  `context canceled`, and the new pod starts a fresh run seconds later. The `RunOnce` `failures` counter stays 0
  because the process exits before the next scrape. Rule: failure line 1–5 s before a new pod's
  "debug server is started listening" (or a new `kube_pod_start_time`) = shutdown, not a defect.

Seen 2026-10-01 20:44 (eu1) and 20:55 (us1) UTC: "ranged loop failure" + 42× "error adding injured segment to queue"
in eu1 — all `context canceled` from the v1.164.1 rollout (new pods started 1–2 s later). Benign. The checker
insert errors were from the same cancel; those segments are found again next run.

## Config that matters (`rangedloop.Config`, prefix `ranged-loop`)
`parallelism` (default 2), `batch-size` (2500, max 50000), `interval` (release 2h), `as-of-system-interval` (release -5m; us1 0),
`stale-interval`, `allow-live-reads`, `suspicious-processed-ratio` (0.03). Observer settings live under their own prefixes
(`checker.*`, `audit.*`, `node-tally.*`, `durability.*`). In prod all regions set `checker.health-score: normalized`.

## Traps
- Never filter on `service=` — dead label for this peer (broke two alerts and three dashboard panels before).
- `completed_observer_duration` is a **last-run snapshot**, not a history: it changes once per run and is empty after a pod restart until the first run ends.
  It is cumulative over ranges, so it can be larger than the run duration.
- `rangedloop_live` resets to 0 at run start — `rate()` sees a counter reset; a low value is not a stall by itself.
- `segmentsProcessed` `count`/`sum` fields are meaningless (running total observed per batch).
- The `__Service__RunOnce_createGoroutineClosure_func4` name comes from Go closure naming; refactoring `service.go` can silently rename it.
- `satellite/metabase/rangedloop/doc.go` "Monitoring" section lists metric names that do not exist.
- monkit `cpu`/`memory` series are host-level, not container — use the OTel/cAdvisor ones from `satellite-observability`.

## Where to look next
- Checker slow or failing → repair queue drains wrong: [repair.md](repair.md).
- DB batch latency high (us1) → TiKV / TiDB health, not loop code.
- Grafana dashboard: `satellite-ranged-loop-v2` ("Satellite - Ranged Loop").
