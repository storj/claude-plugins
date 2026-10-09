# Peer card: repair

Verified 2026-10-09 against storj/storj `main` and us1 metrics. Line numbers drift — search by symbol.

## Identity
- Subcommand `satellite repair` → `satellite/run/repair.go` (`Repair.GetSelector`).
- **Many apps.** `satellite-repair` in every region, plus in us1: `rh-ewr-satellite-repair` (biggest),
  `cb-ash-satellite-repair`, `slc-satellite-repair` / `ap1-satellite-repair` (namespace `satellite-us1`),
  `cb-select-storage-wa-N-repair`, `lw-prod-storage-ams-N-repair` (bare metal). eu1: `hn-prod-storage-*-repair`.
  Use `app=~".*repair"` for the whole fleet; group `by (app)` to compare sites.
- The **queue** lives in the `jobq` service; the **checker** that fills it runs in ranged-loop; queue-size
  metrics come from `satellite-core`. Repair problems often start upstream — see "Where to look next".

## What runs inside
Selector: `Observability` + `*repairer.Service` (`satellite/repair/repairer/mud.go`).
- `Service.Run` → `sync2.Cycle` every `repairer.interval` → `processWhileQueueHasItems` → `process` pops
  `segments-select-batch-size` jobs → one goroutine per segment, bounded by `max-repair` → `worker` →
  `SegmentRepairer.Repair` (`segments.go`) → `ECRepairer` downloads pieces, reconstructs, uploads new pieces.
- Has its own connection pool (`repairer.connection-pool.*`). Queue = `jobq.RepairJobQueue` (remote).

## Key signals
Selector `S` = `app=~".*repair",environment_name="storj-prod-satellite-us1"`. Scope `storj_io_storj_satellite_repair_repairer`.

| Question | PromQL |
|---|---|
| Repair outcomes | query **R** below |
| Same per placement / RS scheme | add `by (placement)` or `by (rs_scheme)` |
| Throughput per site | `sum by (app) (rate(function{field="total",S,scope="storj_io_storj_satellite_repair_repairer",name="__SegmentRepairer__Repair"}[15m]))` |
| Queue fetch errors (kills the loop) | `sum by (app, error_name) (rate(function{field="count",S,scope="storj_io_storj_satellite_repair_repairer",name="__Service__process",error_name!="empty_queue"}[15m]))` — `empty_queue` is a normal idle poll, exclude it |
| Piece uploads | `function{...,name="__ECRepairer__putPiece"}`; `repair_segment_pieces_successful/_failed/_canceled` (distributions: use `field="sum"` rate) |
| Piece downloads | `function{...,name="__ECRepairer__downloadAndVerifyPiece"}`; `download_failed_not_enough_pieces_repair` |
| Queue size (from core!) | `sum by (placement) (repair_queue{field="count",app="satellite-core",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1",attempted="false"}) > 0` |
| Mean time in queue (s) | `sum(rate(time_since_checker_queue{field="sum",S}[1d])) / sum(rate(time_since_checker_queue{field="count",S}[1d]))` |
| Workers idle (queue empty) | `sum(rate(function{field="count",S,scope="storj_io_storj_satellite_repair_repairer",name="__Service__process",error_name="empty_queue"}[1h]))` |
| CPU / memory | OTel `container_cpu_usage` with `k8s_pod_name=~"satellite-repair-.*"` (only k8s sites; bare metal has none) |

R (outcome breakdown):
```promql
sum by (name) (rate(tagged_repair_stats{field="total",app=~".*repair",environment_name="storj-prod-satellite-us1",
  name=~"repair_attempts|repair_success|repair_partial|repair_failed|repair_unnecessary|repair_too_many_nodes_failed|repairer_segments_below_min_req"}[15m]))
```

### Is the queue keeping up?
Use these together: queue size trend (above, 8d at 6h step), mean time in queue, and the `empty_queue`
rate (non-zero = workers sometimes find nothing to do = keeping up). Compare `[1d]` now vs `[7d] offset 1d`.
Lower attempt rate after a backlog drains is normal, not degradation.

### Baselines (2026-10-09)
- **us1:** ~1260 attempts/s fleet-wide, ~95% success, ~1% partial, ~4% unnecessary, `repair_failed`≈0.
  Unattempted queue ~22M in placement `_0` — large but normal for us1; look at the trend, not the level.
- **eu1:** apps `satellite-repair` (50 pods, max-repair 90) + `hn-prod-storage-ein-0-repair`, `-ein-1-repair`
  (bare metal, max-repair 100). 140–620 attempts/s. Queue near 0 since a 31M backlog drained Oct 1–3;
  mean time in queue ~30 s. Big placements: `_0`, `_1`, `_30`, `_32`. `_40` attempted=true stays at 638 (unexplained).
  `repair_unnecessary` jumps to ~70% in short bursts (Oct 7–8) — cause unknown.

## Silent failures (not visible as errors)
- **`__SegmentRepairer__Repair` failures ≈ 0 does not mean healthy.** Irreparable segments return `(false, nil)` — not an error. They show only in `repairer_segments_below_min_req`, `repair_too_many_nodes_failed`, eventkit `irreparable_segment` / `irretrievable_segment`, and warn logs. The job goes back to the queue and is retried forever.
- **Partial repair counts as success** in the function metric; only `repair_partial` shows the shortfall.
- **Worker errors only log** "repair worker failed"; process keeps running.
- **Audit/reputation updates** from repair are fire-and-forget and off by default (`reputation-update-enabled=false`).
- **Empty queue is counted as a monkit failure** of `__Service__process` (`error_name="empty_queue"`) but returns nil. Not an error.
- **Opposite case:** any other **queue (jobq) error ends the loop** — `process` error → "process" log → `Service.Run` returns → pod exits. Look for pod restarts + `__Service__process` failures together.
- Shutdown waits for in-flight repairs up to `total-timeout` (45m) — slow rollouts are expected.

## Traps
- `repair_queue` is sampled **once per hour** by `QueueStat` in core (`satellite/repair/repairer/queue_stat.go` `RunOnce`).
  If the stat query fails it logs "couldn't get repair queue statistic" and **reports 0 for every placement**.
  A sudden all-zero queue = check a placement that is never zero (e.g. eu1 `_40` attempted=true) before calling it "empty".
- `function_times` percentiles (`r50`, `max`, ...) of `time_since_checker_queue` across pods are misleading — idle pods
  keep stale values for days. Use `rate(sum)/rate(count)`.
- `repairer_segments_below_min_req` may have no series in a region (it only appears once it fires). No series ≠ broken.
- `repair_unnecessary` also counts segments deleted/expired before repair: see `segment_deleted_before_repair`,
  `segment_expired_before_repair` (plain meters, scope repairer). The rest = segment was already healthy when re-checked.
- `name` is a label only on `function` / `tagged_*` series. For plain metrics group `by (__name__)`.

## Distinctive logs (logger + message; search them in `otel.otel_logs`)
No logs for us1 `satellite-repair` (dp-lax). `rh-ewr`/`cb-ash` repairers log GCP JSON (no logger name).
Normal us1 noise: "Repair to a storage node failed" ~570k/h (node-side dial/hash errors).

- `repairer:service`: error "process" (queue fetch, fatal), "repair worker failed", "unexpected error repairing segment!".
- `repairer:segmentrepairer`: warn "irreparable segment", "irreparable segment: too many nodes offline", "irreparable segment: could not acquire enough shares"; error "GetParticipatingNodes returned an invalid result".
- `repairer:ecrepairer`: warn "Repair to a storage node failed"; info "Failed to download piece for repair: download timeout (contained)".

## Config that matters (`satellite/repair/repairer/repairer.go` `Config`)
`repairer.max-repair` (release default 5; us1 k8s 70, us1 bare metal 100), `segments-select-batch-size` (1),
`interval` (5m), `timeout` / `download-timeout` (5m), `total-timeout` (45m), `dial-timeout` (5s),
`in-memory-repair` (false → temp files on disk), `included-placements` / `excluded-placements`,
`do-declumping`, `do-placement-check`, `connection-pool.capacity` (100). Checker overrides
(`checker.repair-threshold-overrides`, `repair-target-overrides`) also change repair behavior.
Sites differ a lot: compare `rg -n 'repairer\.' ~/git/storj/infra/helm/satellites/us1/*repair*.yaml ~/git/storj/infra/ansible/playbooks/roles/repairer-docker/vars/`.

## Where to look next
- Queue growing but workers idle → checker / ranged-loop (`app="satellite-ranged-loop"`, `remote_segments_needing_repair{field="recent"}`, `remote_segments_lost`) or jobq health (`app="jobq"`).
- Node waves (offline/online) → `max by (__name__) ({__name__=~"checker_offline_nodes|checker_online_nodes",field="recent",app="satellite-ranged-loop",kubernetes_namespace="satellite",environment_name="..."})`.
- Many download failures → storage node side (offline nodes, overlay) or network at one site — compare `by (app)`.
- High CPU → known 2026-10 profile: TLS handshakes ~25%, little connection reuse in `rpcpool`.
