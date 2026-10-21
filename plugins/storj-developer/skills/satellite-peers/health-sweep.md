# Health sweep: "are all peers healthy?"

Use this for a fleet-wide check (all peers, all regions) instead of the one-question protocol.
The goal is a short list of problems **that need a human**, not a status of everything.

## Order (cheap and broad first)

1. **Firing alerts**: Grafana alert rules in state firing/pending (`alerting_rules_read`). For each one, decide: real, known
   noise (list below), or a normal gap (e.g. "ranged loop segments processing rate" between loop runs).
2. **Pods**: restarts and OOMs in 24h, and deployments with missing replicas, for all regions
   (KSM recipes in `satellite-observability`). One burst at deploy time and nothing after = watch only.
3. **Per peer**, the key-signal table of its card, 24h vs the same hours 7 days ago:
   api (error ratio, latency), core (Q2 "hours since last success"), ranged-loop (loops finish on schedule),
   repair (outcomes R, throughput per site, `empty_queue`), jobq (queue at cap?), gc-bf (last runs Completed) →
   gc-sender (sending), auditor (audits per outcome).
4. **Repair upstream**: `remote_segments_needing_repair` by placement, last run vs the one before. A big jump in Select
   placements only (`_12`, `_20`, `_31`) = check the Select fleet (`satellite-observability`) and the core offline-node
   events from **16–24 h before** (online-window is 16h, see [peers/repair.md](peers/repair.md)).
5. **Node churn**: `checker_offline_nodes` trend; a DQ burst right after a core restart is normal (see [peers/core.md](peers/core.md)).

Do not stop at host-level metrics ("host is up") — count storagenode processes per host.

## Known noise (do not report unless it got worse)

Verified 2026-10-21. Re-check each item quickly — if the cause was fixed, the item is no longer noise and the signal is real.

| Signal | Why it is not a problem |
|---|---|
| us1 repair queue age > 5d alert | placement `_40` test segments retried forever; not a backlog |
| eu1 repair "DB connection pool is too low" | burst of goroutines on an empty queue, ~12 ms waits; no impact |
| us1 `__Chore__ArchiveRollups` fails daily (alert `centohw6vde68f`, ~4h each evening) | known duplicate key in rollup archives, tracked |
| datasource `bfx67nh13k5j4f` (VictoriaLogs) dead | shut down on purpose; logs are in ClickHouse `otel.otel_logs` |
| edge fra01 `ListObjectsV2` 501 | known, not a satellite peer |
| us1 api "Storage limit exceeded" warn ~170k/h | normal customer limits |
| "Abnormal drop in uploads" pending at night | normal overnight low; compare with the same hours on earlier days |
| "Ranged loop segments processing rate" pending | normal gap between loop runs; check loops finish on schedule |
| stray-node DQ burst (hundreds) right after a core restart | chore runs at start; see core card |

## Report

```
## Needs action
- <region/peer>: <what is wrong> — <evidence with numbers and start time> — <likely cause> — <suggested action>

## Watch only
- <one line each>

## Could not check
- <check> — <why (no data, broken datasource, needs access)>
```
If nothing needs action, say so in one line. Do not list healthy peers.
