# Peer card: auditor

Verified 2026-10-21 against storj/storj `v1.165.3` (deployed in all 4 regions), storj/infra and prod metrics/logs/DB.
Line numbers drift — search by symbol.

## Identity
- Subcommand `satellite auditor` → `satellite/run/auditor.go`. Selector: `Observability` + `*audit.Worker` +
  `*audit.ReverifyWorker` (providers in `satellite/audit/mud.go`, `satellite/reputation/mud.go`, queues/containment in `satellite/mud.go`).
- `app="satellite-auditor"`, namespace `satellite`, chart `satellite-single`, values `helm/satellites/<r>/satellite.yaml`
  then `satellite-auditor.yaml` (the auditor file wins). HPA on CPU 40%: us1 1–**8** (max since 2026-10-19), eu1/ap1 1–6, slc 1 fixed.
  2026-10-21: us1 8 pods, eu1 6, ap1 1–6 (scales up and down), slc 1.
- **Not in this pod:** the audit **observer** that fills the verify queue runs in `satellite-ranged-loop`; the
  **containment sync chore** runs in `satellite-core`; core's stray-nodes chore makes the offline DQs (reason 3).

## What runs inside
1. **Verify queue** (`verification_audits` table). Ranged-loop `audit.Observer` keeps `audit.slots` random segments per node
   (reservoir) each loop and pushes them. us1 slots 18, eu1 6, ap1 3, slc 1.
2. **Worker** (`audit/worker.go`): `sync2.Cycle(queue-interval=1h)` → `process` pops one segment at a time
   (`verifyQueue.Next` = DELETE … SKIP LOCKED) and runs up to `worker-concurrency` (prod **24**) `work` goroutines.
   `process` only returns when the queue is empty; then the worker **sleeps 1h**.
3. `work` → `IdentifyContainedNodes` (1 containment DB read **per piece**) → `Verifier.Verify`: create audit order limits
   (one serial per segment), download one random stripe share from every non-contained node (`GetShare`, timeout
   = max(15s, share/150kB/s)), Berlekamp-Welch check (`auditShares`). Each piece gets one outcome:
   - **success**; **fail** = node said NotFound, or share data altered;
   - **offline** = no order limit (node offline in overlay, **or DQ'd/exited**) or dial failed;
   - **contained** = dial ok, download DeadlineExceeded → put in reverify queue + `nodes.contained` set;
   - **unknown** = any other error from the node.
4. `DBReporter.RecordAudits` → `reputation.Service.ApplyAudit` per node (success/fail/unknown/offline), removes failed
   pieces from the segment (max 6 at once), queues pending audits.
5. **ReverifyWorker** (`reverifyworker.go`): same 1h cycle, `reverify-worker-concurrency` 2. Takes a job from
   `reverification_audits` (each pick does `reverify_count+1`), downloads the **whole piece**, checks hash + signatures.
   Outcomes: success / fail → remove from queue; timed out → back to `recordPendingAudits` (fail after
   `max-reverify-count` 3); **offline → stays in queue, no limit**; unknown → removed, counts as unknown.
6. **Reputation** (`reputation/writecache.go`): `reputation.flush-interval: 1h` on the auditor → `CachingDB`. Updates are
   merged in memory and written per node about once an hour (`__reputations__ApplyUpdates`). The DB write
   (`satellitedb/reputations.go` `populateUpdateNodeStats`) decides:
   - audit score α/(α+β) ≤ `audit-dq` 0.96 → **DQ reason 1** (`bad_audit_dqs`);
   - unknown score ≤ `unknown-audit-dq` 0.6 → **unknown-audit suspension** (no metric, info log "Suspended");
     DQ after 168h grace only if `suspension-dq-enabled` (**false** in prod → never);
   - online score < 0.6 over 30d → offline suspension; offline DQ only if `audit-history.offline-dq-enabled` (**false**).
   Status changes are copied to `nodes` by `overlaycache.UpdateReputation` (us1 ~22 calls/day).

## Metric shapes — read this before writing a query
The earlier "rate() returns nothing" was **not** an IntVal problem. The facts (checked 2026-10-21):
- `audit_<outcome>_nodes_global` and `reverify_<outcome>_global` are **Meters**: fields `total` (counter → `increase`/`rate`),
  `rate` (monkit's own per-second gauge, read as-is), `delta`. `rate(x{field="total"}[1h])*86400` and
  `increase(x{field="total"}[1d])` give the same number. Without a `field=` filter you mix three shapes.
- `audit_<outcome>_nodes` (no `_global`) are **IntVals** = one sample per audited segment: `count` = segments,
  `sum` = pieces (counters, `rate`/`increase` work), `r50`/`recent`/... are gauges. `audit_*_percentage` are FloatVals — per-segment ratios, skip them.
- `not_enough_shares_for_audit`, `audit_suspected_network_problem`, `audit_not_enough_nodes_online`, `could_not_verify_audit_shares`,
  `audit_reverify_queue_piece_inserted`: **Counters**, fields `value`/`high`/`low` → `increase(x{field="value"}[1d])`.
- `audit_verify_queue_segments_deleted` and `audit_reverify_queue_piece_deleted` are counters that only go **down**
  (`Dec(1)`). `increase()` sees every step as a reset → 0 or nonsense. Use `__verifyQueue__Next` `successes` instead.
- `increase()`/`rate()` **drop `__name__`**. `sum by (__name__) (increase({__name__=~"a|b"}[1d]))` merges all metrics into one line.
  Use MetricsQL `increase(...[1d]) keep_metric_names`, or one query per metric. Arithmetic on such a vector fails with "duplicate output timeseries".
- `audit_total_nodes_global` = success + fail + offline + contained. **Unknown is not in total**: unknown % = unknown / (total + unknown).
- Every metric has a `placement` label (`_0`, `_12`, ...). Series **without** placement = audits that ended in an error before the
  segment was known (e.g. not enough nodes online).
- `bad_audit_dqs` over-counts (marked on each flush while the score stays low; new pod series). Use the `nodes` table for the real count.

## Key signals
Selector `A` = `app="satellite-auditor",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)"`.

| Question | PromQL |
|---|---|
| Audits per day by outcome | **Q1** |
| Unknown % of audits | **Q2** |
| Segments audited per day | `sum by (environment_name) (increase(function{field="successes",A,scope="storj_io_storj_satellite_satellitedb",name="__verifyQueue__Next"}[1d]))` |
| Segments added to the queue per day (ranged-loop!) | `sum by (environment_name) (increase(audit_verify_queue_segments_inserted{field="value",app="satellite-ranged-loop",kubernetes_namespace="satellite",environment_name=~"..."}[1d]))` |
| Is the verify queue ever empty? | `sum by (environment_name) (increase(function{field="count",A,scope="storj_io_storj_satellite_satellitedb",name="__verifyQueue__Next",error_name="empty_audit_queue"}[1d]))` — 0 = never empty = backlog |
| Are workers busy right now? | `sum by (environment_name) (function{field="current",A,scope="storj_io_storj_satellite_audit",name="__Worker__work"})` — = 24 × pods when saturated; `__Worker__process` current = 1 per pod while draining |
| Reverify outcomes per day | **Q3** |
| Function failures (which errors) | **Q4** |
| Not enough shares / network problem | `sum by (environment_name, __name__) (increase({__name__=~"not_enough_shares_for_audit\|audit_not_enough_nodes_online\|audit_suspected_network_problem\|could_not_verify_audit_shares",field="value",A}[1d]) keep_metric_names)` |
| Audit DQs (meter, approximate) | `sum by (environment_name) (increase(bad_audit_dqs{field="total",A}[7d]))` |
| Contained / suspended / DQ (exact) | SQL **S1** below — no gauge exists in the auditor |
| Version | `count by (environment_name, image) (last_over_time(kube_pod_container_info{environment_name=~"...",namespace="satellite",pod=~"satellite-auditor-.*"}[2h]))` |

```promql
# Q1: pieces audited per day by outcome
sum by (environment_name, __name__) (increase({__name__=~"audit_(success|fail|offline|unknown|contained|total)_nodes_global",field="total",app="satellite-auditor",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)"}[1d]) keep_metric_names)
# Q2: unknown % (unknown is outside total)
100 * sum by (environment_name) (increase(audit_unknown_nodes_global{field="total",A}[1d]))
  / (sum by (environment_name) (increase(audit_total_nodes_global{field="total",A}[1d])) + sum by (environment_name) (increase(audit_unknown_nodes_global{field="total",A}[1d])))
# Q3: reverify outcomes + jobs queued
sum by (environment_name, __name__) (increase({__name__=~"reverify_.*_global|audit_reverify_queue_piece_inserted",field=~"total|value",A}[1d]) keep_metric_names)
# Q4: failures by error (field="count" is split by error_name)
sum by (environment_name, name, error_name) (increase(function{field="count",A,scope=~"storj_io_storj_satellite_(audit|reputation|satellitedb)",name!~"__Verifier__GetShare|GetShare"}[1d])) > 0
```
Q4 noise to ignore: `pending_audit_not_found` on `__containment__Get` / `__reverifyQueue__GetByNodeID` (= node not contained, the normal case,
~170M/day us1); `empty_audit_queue`; `__Reverifier__GetPiece` error `audit` (node errors, a few k/day). `GetShare` failures = every offline/unknown piece.

```sql
-- S1: containment, suspension, DQs (ClickHouse cfqvdb4b1y96oa; repeat with eu1_/ap1_/slc_satellitedb). nodes.contained is a timestamp.
SELECT concat('contained=', toString(countIf(isNotNull(contained) AND isNull(disqualified))),
  ' unk_susp=', toString(countIf(isNotNull(unknown_audit_suspended) AND isNull(disqualified))),
  ' unk_susp_new7d=', toString(countIf(unknown_audit_suspended > now() - INTERVAL 7 DAY)),
  ' auditDQ7d=', toString(countIf(disqualified > now() - INTERVAL 7 DAY AND disqualification_reason = 1)),
  ' offlineDQ7d=', toString(countIf(disqualified > now() - INTERVAL 7 DAY AND disqualification_reason = 3)))
FROM postgresql(us1_satellitedb, table='nodes')
-- reverify queue: SELECT count(), max(reverify_count), min(inserted_at) FROM postgresql(us1_satellitedb, table='reverification_audits')
```
DQ reasons: 1 audit failure, 2 unknown-suspension (off), 3 node offline (from core, not the auditor).
`verification_audits` is too big to count through the federation (times out).

### Baselines (24h to 2026-10-21 09:00 UTC, pieces/day)
| | us1 | eu1 | ap1 | slc |
|---|---|---|---|---|
| segments audited | 3.6M | 1.6M | 0.82M | 0.10M |
| total (no unknown) | 172M | 88M | 48M | 5.3M |
| success | 143M (83%) | 73M (83%) | 37M (77%) | 5.0M (94%) |
| offline | 28.7M (17%) | 15.4M (17%) | 11.1M (23%) | 0.34M (6%) |
| fail | 4.1k | 1.3k | 1.2k | 180 |
| contained | 11.6k | 7.4k | 6.4k | 1.8k |
| unknown | 65k (0.038%) | 8.6k (0.010%) | 6.5k (0.014%) | 270 (0.005%) |
| reverify success/offline/contained/unknown/fail | 12.4k/1.6k/4.4k/23/0 | 6.6k/1.0k/2.5k/21/1 | 5.8k/0.9k/2.9k/15/1 | 2.0k/0.4k/1.6k/8/0 |
| verify queue empty/day | **never** | ~70 | ~75 | ~20 |
| contained nodes (DB) | 3,376 | 2,628 | 1,594 | 621 |
| unknown-suspended now / new 7d | 27 / 5 | 24 / 5 | 21 / 5 | 21 / 6 |
| audit DQs 7d / 30d (DB) | 5 / 32 | 3 / 26 | 8 / 37 | 6 / 30 |
- Offline ~17% is normal: it includes DQ'd and exited nodes that still hold pieces. It follows node outages
  (us1 33.7M on 2026-10-20, Select outage). Check volume, not only the %.
- Unknown spiked 2–9× in **all regions** on 2026-10-15..17 (us1 ~138k/day, eu1 ~79k) and went back. Cause not found
  (the us1 "unknown error" log rate was not higher then).
- us1 `not_enough_shares_for_audit` 25–32k/day on 2026-10-13/14, ~1–5k/day after; mostly `audit_not_enough_nodes_online`.
- **us1 verify queue is a backlog**: since ~2026-10-01 the ranged loop inserts ~4.9–5.3M segments/day, the auditor pops ~2.6–3.2M.
  infra `ae50e16c4` (2026-10-19: ranged-loop interval 7h, auditor maxReplicas 6→8) is the fix; recheck inserted vs popped.
  All 8 us1 pods sit at 192 in-flight `work` (24×8) all day. (Before Oct, inserted ~0.9M/day < popped ~1.3M.)

## Silent failures
- **Worker/ReverifyWorker `Run` never fails**: `process` errors only log "process" / "failure processing reverify queue" and
  the cycle waits **1h** (`queue-interval`). A DB hiccup = one hour of no audits per pod, nothing else.
- **"error(s) during audit"** (us1 ~50/h) is the only trace of a failed segment; the segment is already deleted from the
  queue, so it is not retried. Same for not-enough-shares: no node is punished, the segment is just dropped.
- **Reputation write failures**: `ApplyAudit` errors are retried 3× in `RecordAudits`, then log
  "failed to update reputation information with audit results" and are **thrown away**. `audit_apply_to_db_failure` has
  no series in prod (never fired).
- **CachingDB flush errors are not logged and drop the pending updates** (`syncEntry` resets `mutations` even on error).
  It also has no `Close`/flush on shutdown → every pod stop (HPA scale-down!) loses up to 1h of that pod's reputation updates.
  Logged at shutdown: "context canceled" + "finished with audit, but failed to remove entry from queue" — normal.
- **Containment can last forever**: `OutcomeNodeOffline` keeps the job without counting toward `max-reverify-count`, so a node that
  stays offline stays contained (not audited for other pieces). 2026-10-21 us1: 4,042 jobs, 302 with `reverify_count` > 3
  (max 202), oldest from 2026-08-28. Same in other regions (eu1 202, ap1 187, slc 89 with count > 3).
- DB errors in reverification (`OutcomeNotPerformed`) record nothing and leave the job for the next try after 6h.
- "cowardly refusing to remove large number of pieces for failed audit" (>6 fails in one segment): reputation is still
  applied, but the pieces stay in the segment.
- `ApplyAudit` skips DQ'd/exited nodes and calls `overlay.Get` for every piece without a cached reputation (offline ones):
  `__overlaycache__Get` ~31M/day in us1 is this, not a bug.
- Unknown-audit **suspensions have no metric** (log "Suspended" category "Unknown Audits", logger in satellitedb) — use S1.

## Distinctive logs (ClickHouse `otel.otel_logs`, pod `satellite-auditor-%`; info + warn + error shipped)
Info per piece outcome (logger `audit:verifier`): "Verify: unknown error (skipped)", "Verify: download timeout (contained)",
"Verify: piece not found (audit failed)", "Verify: share data altered (audit failed)". `audit:dbreporter`: "reverification queued",
"max reverify count reached (audit failed)". Dial failures (offline) are **debug** → not shipped. us1 per hour (2026-10-21):
unknown ~1.8k, reverification queued ~500, contained ~430, not found ~80, altered ~40, max reverify ~20.
Errors: `audit:worker` "error(s) during audit" (us1 ~50/h), `audit:verifier` "could not verify shares", `audit:dbreporter`
"failed to update reputation …", `audit:reverifyworker` "failure processing reverify queue". Error text is often only in
`exception.stacktrace`; the cause is in a separate stderr row (`context canceled`, …) — see `satellite-observability`.

**Is ~16.5k "Verify: unknown error (skipped)" per 8h in us1 normal? Yes — steady background.** 6h sample on 2026-10-21:
~81% `piecestore close: used serial already exists in store` from **~400 nodes**, ~19% `hashstore: read … hashtbl … .tmp`
I/O errors from **5 nodes** (broken disks), the rest context canceled / connection reset / "requested more data than available".
The same mix and level was seen on 2026-10-13 and 10-16 (~1.5–2.3k/h). Likely cause of "used serial" (not proven): audit order
limits share **one serial per segment**, so a node holding two pieces of one segment rejects the second. It still dings those
nodes' unknown score. Breakdown query:
```sql
WITH splitByChar('\t', Body) AS p, arrayFirst(x -> startsWith(x, 'exception.message='), p) AS e,
     multiIf(e LIKE '%hashstore%', 'hashstore IO', e LIKE '%used serial already exists%', 'used serial', substring(e, 1, 90)) AS k
SELECT concat(toString(count()), ' | ', toString(uniqExact(arrayFirst(x -> startsWith(x, 'node_id='), p))), ' nodes | ', k)
FROM otel.otel_logs WHERE Timestamp > now() - INTERVAL 6 HOUR
  AND ResourceAttributes['environment_name'] = 'storj-prod-satellite-us1' AND `__otel_materialized_k8s.namespace.name` = 'satellite'
  AND `__otel_materialized_k8s.pod.name` LIKE 'satellite-auditor-%' AND p[4] = 'Verify: unknown error (skipped)'
GROUP BY k ORDER BY count() DESC LIMIT 15
```
(Keep the window ≤ 6h; `Body LIKE` over 10 days times out.)

## Config that matters (code `audit.Config`, `reputation.Config`; prod from infra)
| flag | default | prod |
|---|---|---|
| `audit.worker-concurrency` | 2 | **24** all regions (`satellite.yaml`) |
| `audit.reverify-worker-concurrency` | 2 | default |
| `audit.queue-interval` | 1h | default — sleep after an empty queue |
| `audit.max-reverify-count` / `reverification-retry-interval` | 3 / 6h | default |
| `audit.min-bytes-per-second` / `min-download-timeout` | 150kB / 15s | default |
| `audit.slots` (ranged-loop) | 3 | us1 18, eu1 6, ap1 3, slc 1 |
| `reputation.flush-interval` | 2h | **1h** in auditor yaml (0s in `satellite.yaml` for other peers) |
| `reputation.audit-count` (vetting) | 100 | us1 1000, eu1 350, ap1 200, slc 200 |
| `reputation.audit-dq` / `unknown-audit-dq` | 0.96 / 0.6 | default |
| `reputation.audit-lambda` / `unknown-audit-lambda` | 0.999 / 0.95 | default |
| `reputation.suspension-dq-enabled` | false | default → no unknown-suspension DQ |
| `reputation.audit-history.offline-dq-enabled` / `offline-suspension-enabled` | false / true | default |
| DB pool `db.max_open_conns` | — | 20 all regions; idle 2 in us1, 20 elsewhere |
| memory limit | — | us1 1536Mi (GOMEMLIMIT 1300MiB), eu1/ap1 1Gi, slc 1.5Gi |

## Traps
- **2026-10-21 from ~09:31 UTC all satellite monkit and KSM series stopped arriving** in `ffqrvx0pyhp8gf` (`up` stayed fresh).
  Instant queries at `now` then return **no data** (looks like a wrong selector), and range `rate(...[1h])` keeps fake values for an hour.
  Check freshness first: `(time() - max by (environment_name) (tlast_over_time(audit_total_nodes_global{field="total",A}[3h])))/60` (minutes).
  The baselines above end at 09:00 for this reason.
- us1 runs 8 pods via HPA: series per pod come and go; always `sum`, and use `increase`, not raw counter values.
- `__Worker__process` `total` stays 0 for days in us1 — a call is counted only when it returns (queue empty). Not a hang.
- `function_times` of `__Worker__work` mixes 24 goroutines × pods; use `rate(sum)/rate(count)` for mean time.
- slc also has QA in `satellite-qa`: pin `kubernetes_namespace="satellite"`.

## Where to look next
- Queue filling / slots → ranged-loop card ([ranged-loop.md](ranged-loop.md)), `audit_verify_queue_segments_inserted` on `satellite-ranged-loop`.
- Containment sync and offline/stray DQs → [core.md](core.md) (`__ContainmentSyncChore__syncContainedStatus`, `__Service__DQNodesLastSeenBefore`).
- Offline spikes → node or site outages (Select fleet), compare with repair (`repair_queue`) and checker offline-node gauges.
