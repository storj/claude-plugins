# Peer card: gc-bf (garbage collection bloom filters)

Verified 2026-10-09 against storj/storj `main`, storj/infra and prod metrics/logs. Line numbers drift — search by symbol.
gc-bf runs its **own** ranged loop, separate from [ranged-loop.md](ranged-loop.md). Never mix their metrics.

## Identity
- Subcommand `satellite gc-bf-once` → `satellite/run/gc_bf_once.go` (`OnceRunner`). (`gc_bf.go` is the long-running
  variant; prod does not use it, and it refuses `shard-count > 1`.)
- A Kubernetes **CronJob**: the pod starts, scans all segments once, uploads bloom filters to a bucket, and exits.
  `app="satellite-gc-bf"`, namespace `satellite`. Pod name `satellite-gc-bf-<jobid>-<x>` changes every run.
- Output is consumed by **gc-sender** (`app="satellite-gc-sender"`, `satellite/gc/sender`), which reads `LATEST`
  from the bucket every hour and sends retain requests to storage nodes. Nodes then delete pieces not in their filter.
- Why it matters: if filters stop, nodes keep garbage forever (SNOs are paid for it); if a filter is **wrong** (missing
  segments), nodes delete live data. The publish guard (below) exists for the second case.

| | us1 | eu1 | ap1 | slc |
|---|---|---|---|---|
| schedule (UTC) | `0 22 * * 1,3,6` | `0 15 * * 1,3,6` | `0 15 * * 0,2,4` | `30 16 * * 0,2,5` |
| cluster | **cb-ash-k3s** | dp-prg-eu1 | dp-lax-ap1 | dp-lax-slc |
| shard-count | **2** (two full scans per job) | 1 | 1 | 1 |
| parallelism / batch | 20 / 10000 | 15 / 5000 | 5 / 5000 | 4 / 5000 |
| memory req/limit | 100Gi / 235Gi | 70Gi / 128Gi | 9Gi / 16Gi | 4Gi / – |
| activeDeadlineSeconds | 97200 (27h) | – | 14400 (4h) | – |
| TiKV safepoint hold | yes, 2 TiDB clusters, ttl 1h, max 24h | no (TiDB, `as-of` -5m + stale-interval 5m) | no (TiDB, stale 5m) | yes, max 2h |

us1 also scales `cb-ash-satellite-repair` to 0 while the job runs (`cb-ash-repair-gcbf-scaler.yaml`), so a cb-ash repair
throughput drop on Mon/Wed/Sat nights is expected.

## What runs inside
`OnceRunner.Run`:
1. Hold TiKV GC safepoints (`rangedloop.HoldSafepoint`, log "holding TiKV GC safepoints for the scan") so the snapshot
   read stays valid. Losing the hold aborts the run.
2. Read timestamp = safepoint time (or now − stale-interval).
3. Add `SegmentsCountValidation` + the **publish guard** `PinnedSegmentsCountGuard` (`readtimestamp.go`): filters are not
   published unless the scan processed exactly the snapshot's segment count.
4. `bloomfilter.Observer.RunPasses` → one `rangedloop.Service.RunOnce` per shard (us1: 2 passes).
   Observers: `bloomfilter.Observer`, `LiveCountObserver`, `SegmentsCountValidation`, and `piecetracker.Observer`
   (pulled in as a dependency; logs "piecetracker observer finished").
5. Upload (`satellite/gc/bloomfilter/upload.go`): zips of `zip-batch-size` (40) filters as `bloomfilters-<shard>-N`, then
   overwrite `LATEST` with the new prefix.
6. Observer errors → non-zero exit (`rangedloop.ObserverError`); the pod's exit is the only "run failed" signal.
   Any error from one segment-range query kills the whole run (no retry in code). The **k8s Job then starts a retry
   pod** (new upload prefix, new read timestamp), so a failed pod followed by a Completed one is a late success.

Kubernetes shape: pod name `satellite-gc-bf-<scheduled time in minutes since epoch>-<x>` (×60 → Unix time; compare
with the pod's real start to see how late it ran). `concurrencyPolicy: Forbid` — a long or catch-up run delays the
next one. us1 also has manual jobs (`...-manualN-...`).

## Key signals
gc-bf pods exit as soon as they finish, so **terminal counters are often never scraped**. Every gc-bf read needs
`increase()` over days or `max_over_time()`. Selector `G` = `app="satellite-gc-bf",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"`.

| Question | PromQL |
|---|---|
| Packs uploaded **per run** | query **P** below |
| Run outcome per pod | `max by (pod, reason) (last_over_time(kube_pod_container_status_terminated_reason{environment_name="...",namespace="satellite",pod=~"satellite-gc-bf-.*"}[10d])) == 1` (Completed / Error) |
| Pod phase per pod | `max by (pod, phase) (last_over_time(kube_pod_status_phase{...same...}[10d])) == 1` |
| CronJob suspended? | `max by (cronjob) (kube_cronjob_spec_suspend{environment_name="...",namespace="satellite",cronjob=~".*gc-bf.*"})` (1 = suspended) |
| Scan progress per pass | `max(max_over_time(rangedloop_live{field="num_segments",G}[2h]))` over a 10d range, step 2h — one sawtooth per pass |
| Did gc-sender deliver? | `sum(increase(function{field="successes",app="satellite-gc-sender",kubernetes_namespace="satellite",environment_name="...",name="__Service__sendRetainRequest"}[12h]))` (range query, 12h step shows each delivery burst) and `field="failures"` |
| TiKV safepoint lag (h) | query **S** below |

```promql
# S: TiKV GC safepoint lag in hours, per PD instance. Read the MIN per TiDB cluster, never the max (see Traps).
(time() - pd_gc_gc_safepoint{type="gc_safepoint",job="tidb_cluster",environment_name="storj-prod-satellite-us1"} / 262144 / 1000) / 3600
```

```promql
# P: packs uploaded per run (one pod = one run attempt)
sum by (kubernetes_pod_name, field) (max_over_time(function{field=~"successes|failures",app="satellite-gc-bf",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1",name="__Upload__uploadPack"}[10d]))
```

**Proof that a shard was published:** log line "collecting bloom filters finished" (fields `inline_segments`,
`remote_segments`, `upload_prefix`). It is written only after the publish guard, all packs and the `LATEST` commit
succeeded. One line per shard (us1: 2 per run).

Baselines (2026-10-09):
- Packs per run (×40 filters): us1 ~830 (2 shards), eu1 ~720–750. `uploadPack` failures 0. Total in 14d: us1 ~5000, eu1 ~4400, ap1 ~2860, slc ~1460.
- us1 run ~18h (two ~8h passes), eu1 ~1.5h, both Completed.
- gc-sender `sendRetainRequest` in 7d: us1 ~85k ok / ~15k failed (~15%), eu1 ~79k / 11k, ap1 ~27k / 3k, slc ~71k / 9k.
  Failures are mostly node connectivity (`i/o timeout`, `no route to host`, `connection refused`); a sudden ratio jump
  is the signal, not the level. One delivery ≈ one send per node (eu1 ~30k nodes).
- us1 pass length ~6–8h, 2 passes per job. eu1 single pass ~1.3B segments, ap1 ~123M, slc ~4.2M.
- Safepoint lag on a healthy PD leader: sawtooth between ~1h and ~17h (us1).

## Silent failures
- **"target bucket was not empty, stop operation and wait for next execution"** (warn) → `return nil`, nothing published,
  run looks successful. Usually means gc-sender has not finished moving the previous generation.
- **Publish guard**: "the scan processed %d segments, the snapshot holds %d" / "the segments count of the snapshot is unknown" —
  filters correctly **not** published. Repeated guard failures = nodes get no new filters (garbage grows) — investigate.
- **"segment created after loop started"** (bloomfilter observer `Process`) → observer fails for the whole run (snapshot
  read not consistent: safepoint / stale-interval / as-of misconfigured).
- `OnceRunner.Run` has **no `mon.Task`** — the job as a whole is unmeasured. `__Upload__UploadBloomFilters` (the wrapper)
  reads 0 for weeks even when uploads happen (pod exits before the scrape). Use `uploadPack`.
- No metric says which shard a pass is on or what the publish guard decided — use logs.
- gc-sender: per-node send errors only warn ("Error sending retain filter", cause in `exception.message=`).
  "LATEST file does not exist in bucket" is **info** level and gc-sender ships warn only — you cannot find it in logs.
- **Suspended CronJob**: nothing fails, nothing runs. us1 was suspended ~2026-09-28 → 2026-10-05 (gc-sender sent
  nothing Oct 2–6). Check `kube_cronjob_spec_suspend` whenever runs are missing.

## Logs
gc-bf ships **debug + info** (unlike repair). gc-sender ships **warn only**.
- `root:oncerunner`: error "ranged loop failure" — no error text in the line itself; the stack frames follow as separate
  rows and the **cause is the last row** (fetch the pod's raw rows ±2 s). Example: eu1 2026-10-07 16:27:56, cause
  `Error 8175 (HY000): ... exceeding the allowed memory limit for a single SQL query` (TiDB `tidb_mem_quota_query`);
  the Job's retry pod published at 18:01. eu1 2026-09-28 shows the same fail-then-retry pattern (cause unknown).
- "holding TiKV GC safepoints for the scan", "processing node shard", "collecting bloom filters finished"
  (fields `inline_segments`, `remote_segments`, `upload_prefix`).
- warn "target bucket was not empty, stop operation and wait for next execution".

## Config that matters (`satellite/gc/bloomfilter/config.go`, prefix `garbage-collection-bf`)
`initial-pieces` (release 400000), `false-positive-rate` (0.1), `max-bloom-filter-size` (2m default; prod 25–50 MB),
`bucket`, `access-grant` (secret), `zip-batch-size` (40), `expire-in` (336h), `upload-pack-concurrency` (4), `shard-count`, `shard`.
`run-once` is only read by the legacy peer — a no-op for `gc-bf-once`. Loop settings come from `ranged-loop.*` (incl.
`ranged-loop.safepoint.*`). gc-sender: prefix `garbage-collection` (`interval` 1h, `concurrent-sends` 100, `retain-send-timeout` 1m).

## Traps
- **`num_segments` per pass can exceed the table size** (us1 passes peak at 10.4–12.9B while the main loop sees ~9.7B
  segments). Not explained yet — do not use it as a coverage ratio; use the publish-guard logs instead.
- **Safepoint metric: only the PD leader updates it.** Followers keep the last value, so their "lag" grows by 1h per hour
  forever (us1 `pa-2` since a leader change ~2026-09-24: 340h+; eu1 `prg-1`: 2000h+). That looks like stuck TiKV GC but is
  not. Take the min per cluster (`server_name` prefix: `dp-prod-us1-lax-*` vs `cb-prod-us1-pa-*`).
- `service="gc-bf"` exists on gc-bf series but not on ranged-loop; group by `environment_name` when comparing the two loops.
- slc also has a QA gc-bf in `satellite-qa` — pin the namespace.
- Storage-node side (`retain_*`, `__Endpoint__Retain`) is only reported by the `storj-select` fleet.

## Where to look next
- Filters produced but garbage not dropping → gc-sender (`app="satellite-gc-sender"`) and node-side retain.
- Run aborted around safepoint → TiDB/PD health; check query **S** per cluster.
- TiDB error 8175 (per-query memory limit) → TiDB slow/expensive-query logs for that time; fixes are `tidb_mem_quota_query`
  for the gc-bf user or a smaller `ranged-loop.batch-size`.
- us1 KSM data on cb-ash is spotty (some pods never show a terminal phase) — confirm with the publish log line.
- Piece counts used for filter sizing come from the ranged-loop `piecetracker` observer ([ranged-loop.md](ranged-loop.md)).
- Grafana dashboard: `sat-gc-v3` ("Garbage Collection v3").
