# Peer card: jobq

Verified 2026-10-21 against storj/storj `main` (3d10c998d), the live image, storj/infra, live k8s (read-only) and prod
metrics. Line numbers drift — search by symbol.

## Identity
- The repair queue server. Not a `satellite <sub>` subcommand in prod: **all prod regions run the old standalone
  binary** `cmd/jobq` (`satellite/jobq.go` `NewJobq`), images `storjlabs/jobq:73f66fa54-v1.137.4` (us1, eu1, ap1; built
  2025-09-11) and `5ddd76599-v1.141.4` (slc). `main` has a modular build instead (`satellite/jobq/run`, `jobq run`,
  since 2026-02-19) with **different flag names** (`server.max-mem-per-placement`, not `max-mem-per-placement`).
- k8s: deployment `jobq`, **1 replica**, ns `satellite`, `app="jobq"`, service `jobq-service:15781` (ClusterIP),
  debug/metrics on `:5999`. slc also runs a QA jobq in `satellite-qa` (same app label) — pin the namespace.
- us1 pod `jobq-*` in context `dp-lax-us1-rke2`, started 2026-09-25 17:22 UTC. eu1 `dp-prg-eu1-rke2` (started 09-04),
  ap1 `dp-lax-ap1-rke2` (06-27), slc `dp-lax-slc-rke2` (09-30). Each region has its **own** jobq; nothing is shared across regions.
- Clients: ranged-loop checker (push), repair workers (pop + re-push on failure), core `QueueStat` (stat every 15m).
  Auth = TLS with peer-CA whitelist; clients use the **satellite identity** (`.../identity/satellite/ca.cert`) and dial
  `<jobq node ID>@jobq-service:15781` (`jobq.server-node-url` in `helm/satellites/<r>/satellite-*.yaml`).
- Off-cluster repairers reach us1 jobq over Tailscale: `jobq-proxy` (socat DaemonSet on the lax nodes, `tailscale0:15781`)
  ← `jobq-dp-lax-proxy` (HAProxy, `helm/satellites/us1/shared-jobq-dp-lax-proxy.yaml`) running in cb-ash, rh-ewr, slc and
  ap1 clusters (ns `satellite` or `satellite-us1`); bare-metal repairers use a socat tunnel (`ansible/.../repairer-docker/vars/us1.yaml`).
  eu1 also has a `jobq-proxy` for its bare-metal repairers.

## What runs inside
- `server.JobqEndpoint` (DRPC) → `QueueMap`: one `jobqueue.Queue` per placement, created on first use.
- Each `Queue` = a **repair** min-max heap (order: health, then never-attempted first, then oldest attempt, then oldest insert)
  + a **retry** heap (jobs whose `LastAttemptedAt` is newer than `retry-after`, 1h) + one `indexByID` map
  (stream ID + position → slot). Heaps live in mmap'd memory; the map is normal Go heap.
- **In memory only. No persistence.** On restart every queue is empty. It fills again only as the next ranged-loop
  checker pass pushes segments (us1: hours). Until then repairers get `empty_queue`, and `repair_queue` shows ~0.
- Insert of a segment already queued = update (new health, `UpdatedAt=now`, `InsertedAt` kept, attempts summed).
- Checker `Observer.Finish` calls `Clean(loop start)`: jobs not re-pushed by this loop are removed
  (`healthy_segments_removed_from_queue`). If a loop dies before `Finish`, stale jobs stay.
- `Stat` is a full scan of a placement **under that queue's lock** (blocks push/pop for it). Core calls it every 15m.

### The per-placement cap (what happens when full)
`maxItems = max-mem-per-placement / RecordSize`. **`RecordSize` = 72 bytes** (`unsafe.Sizeof(RepairJob{})`, checked
2026-10-21; not 192). The cap counts **repair + retry together**.

| Region | live `max-mem-per-placement` | cap (jobs) | git value | pod mem limit |
|---|---|---|---|---|
| us1 | **1.5GiB** (helm-applied 2026-09-25, not in git) | **22,369,621** | 2.5GiB (→ 37,282,702) | 26.5Gi |
| eu1 | 1GiB | 14,913,080 | 1GiB | **16Gi live**, 12Gi in git |
| ap1 | 1GiB | 14,913,080 | 1GiB | 4Gi |
| slc | default 4GiB | 59,652,323 | default | **128Mi** |

When a new job comes and the placement is full (`Queue.Insert`): it pops the **max** of whichever heap is bigger —
the highest-health job from repair, or the latest-retry job from retry — and then **always inserts the new job**.
So the worst segments mostly stay, but not always: a new job with higher health than the evicted one still gets in.
Evictions have **no metric and no log**. The evicted segment comes back on the next checker pass (counted as "new" again).

## Key signals
Selector `J` = `app="jobq",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"`.
Meters are scope `storj_io_storj_satellite_jobq_jobqueue`, fields `total`/`rate`/`delta`; `_p` variants have `placement`.

| Question | PromQL |
|---|---|
| Queue size per placement (from **core**) | `sum by (placement, attempted) (repair_queue{field="count",app="satellite-core",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"}) > 0` — `attempted="false"` = repair heap, `"true"` = retry heap |
| Is a placement at the cap? | `sum by (placement)` of the above (both heaps) == cap in the table. A flat line at exactly that number = full |
| Real backlog (last checker pass) | `max by (placement) (remote_segments_needing_repair{field="recent",app="satellite-ranged-loop",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"})` — count found by the last full loop, not live |
| Adds / updates / pops per s | query **Q1** |
| Same per placement | query **Q2** — at the cap, push − pop ≈ evictions + cleans |
| Removed by checker Clean | `max(healthy_segments_removed_from_queue{field="recent",app="satellite-ranged-loop",kubernetes_namespace="satellite",environment_name="..."})` |
| Who truncated / cleaned? | query **Q3** — cumulative since pod start; a step in Truncate = someone ran jobqtool |
| RPC call rate | `sum by (name) (rate(function{field="total",J,scope="storj_io_storj_satellite_jobq_server"}[1d]))` (us1: Pop ~83/s, PushBatch ~18/s) |
| Memory peak | `max(max_over_time(container_memory_working_set_bytes{environment_name="...",k8s_namespace_name="satellite",k8s_pod_name=~"jobq-[a-z0-9]+-[a-z0-9]+"}[7d])) / 2^30` (regex skips `jobq-proxy-*`) |
| Restarts (= queue wiped) | `sum by (pod) (increase(kube_pod_container_status_restarts_total{environment_name="...",namespace="satellite",pod=~"jobq-[a-z0-9]+-[a-z0-9]+"}[30d]))` |

```promql
# Q1: adds / updates / pops per second, 1d
round(sum by (__name__) (rate({__name__=~"jobq_(push|update|pop)",field="total",app="jobq",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"}[1d]) keep_metric_names), 0.1)
# Q2: adds and pops per placement, 6h
round(sum by (__name__, placement) (rate({__name__=~"jobq_(push|pop)_p",field="total",app="jobq",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"}[6h]) keep_metric_names))
# Q3: manual / rare RPCs, cumulative counts (range query, 1h step)
max by (name) (function{field="total",app="jobq",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1",scope="storj_io_storj_satellite_jobq_server",name=~"__JobqEndpoint__(Truncate|Clean|Stat|Peek)"})
```
Use `keep_metric_names` (MetricsQL) when you group by `__name__` after `rate()`, else the names are dropped.

### Baselines (2026-10-21, ~09:30 UTC)
- **us1:** push ~3,055/s, update ~504/s, pop ~827/s (1d). Queue: `_0` 19–22.4M (at the cap on and off since at least
  2026-10-17; saw-tooth = checker Clean), `_12` 22,369,621 (from ~11 to full between 04:00 and 07:40 UTC),
  `_20` full at 08:39 (was ~0 before 01:00). **`_0`, `_12` and `_20` all hit the cap on 2026-10-21** during the Select cb_pa
  storage node outage (reported: 33 nodes down since 2026-10-20). Real backlog from the checker: `_20` ~138M, `_0` ~43M,
  `_12` ~36M — the queue holds only the worst ~22M of each. Truncate calls: 2026-10-19 ~20:00 (×3), 10-20 ~14:00 (×2).
  Memory peak 16.3 GiB / 26.5Gi. 0 restarts in 30d.
- **eu1:** push = pop ≈ 143/s (repair keeps up). Queue max 41.6k in 1d; checker finds ~2.7M (mostly `_0`, `_1`).
  Memory peak 7.1 GiB / 16Gi.
- **ap1:** push = pop ≈ 14/s; queue max 190k in 1d, now ~5k (`_32`). Memory ~1 GiB / 4Gi.
- **slc:** push ~0.5/s; queue ~18k (`_0`). **Memory peak 118 MiB of a 128Mi limit** — one bigger checker pass can OOM it.

## Silent failures
- **Endpoint `function` metrics are useless in every live image.** All 13 endpoints called `mon.Task()(&ctx)(&err)`
  without `defer` (fixed on main in f4b419ccc, 2026-09-08, first in v1.164): `function_times` are ~µs and
  `failures` is always 0. Only `total` (call counts) is real.
- **Evictions at the cap** — no metric, no log (see above). `repair_queue` then shows the cap, not the backlog.
- **`PushBatch` partial failure**: jobs before the bad one are inserted, but the whole call returns an error. Checker
  `Join` fails on a `Flush` error; a single-segment `Insert` error only logs "error adding injured segment to queue"
  in ranged-loop and skips the segment.
- **Pop never returns an error** (empty or closed queue → 0 jobs). Repair turns 0 jobs into `empty_queue`.
  A jobq restart or network cut shows on the client side instead: repair `__Service__process` errors (that ends the repair
  process), core "couldn't get repair queue statistic" (reports 0 for all placements).
- `markUnused` failure logs "failed to mark unused memory" once, then memory is never given back.
- Heap growth `memRealloc` failure **panics** ("failed to realloc memory") — the pod dies and the queue is lost.
- The pod memory limit must cover more than the heaps: the `indexByID` map and the 2× copy during heap growth are not
  part of `max-mem-per-placement`. us1: 3 full placements ≈ 67M jobs ≈ 16 GiB.

## Distinctive logs
jobq logs almost nothing at runtime: startup lines (`Configuration loaded`, `Telemetry enabled`, debug "initializing job
queue"), `jobqueue` placement-N error "failed to mark unused memory", panic "failed to realloc memory". Because the prod
pods started more than ~9 days ago (ClickHouse retention), **there are 0 prod jobq rows in `otel.otel_logs`** (checked
2026-10-21, 10 days, ns `satellite`). Only the slc QA jobq (ns `satellite-qa`, restarted 2026-10-20) shows startup lines
with levels INFO/WARN/ERROR (ERROR "Eventkit BQ destination couldn't be initialized" is normal). For jobq problems read
the **client** logs: ranged-loop, repair, core (see their cards).

## Config that matters
Live (old binary, top-level keys in configmap `jobq-config`): `max-mem-per-placement` (4GiB default), `init-alloc` (2GiB),
`mem-release-threshold` (100MiB), `retry-after` (1h), `listen-address` (:15781), `tls.use-peer-ca-whitelist`,
`tls.peer-ca-whitelist-path`, `log.level` (debug everywhere). On the modular build the same knobs are `server.*`.
Values: `helm/satellites/<r>/jobq.yaml` (release `jobq`, chart `charts/jobq`).
Client side: `jobq.server-node-url`, `jobq.tls.*`; client `DialTimeout` is 1h (`jobq.NewDialer`).

**Git ≠ live (2026-10-21):** us1 `max-mem-per-placement` 1.5GiB live vs 2.5GiB in git; eu1 memory limit 16Gi live vs 12Gi.
The next `helmfile apply` of `jobq` changes the us1 cap to 37,282,702 jobs **and restarts the pod, which empties all queues**.

## jobqtool (manual queue operations)
`cmd/jobqtool`: `len [p]`, `stat [p]` (full scan + histogram by default — heavy under lock), `peek [p]`,
`truncate <p>` (repair + retry), `clean <timestamp> [p]` (drop jobs not updated since), `trim <health> [p]`
(drop jobs with health above), `import <csv>`. Not in the jobq image (only `/app/jobq`) nor in satellite-modular.
Proven way: build static (`CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o ~/tmp/jobqtool/jobqtool ./cmd/jobqtool`),
`kubectl cp` it into the `satellite-core` pod (it has the satellite identity jobq trusts) and run with
`--server <jobq.server-node-url> --identity.cert-path/--identity.key-path /root/.local/share/storj/identity/satellite/identity.{cert,key}
--tls.use-peer-ca-whitelist --tls.peer-ca-whitelist-path /root/.local/share/storj/identity/satellite/ca.cert
--tls.revocation-dburl bolt:///tmp/jobqtool-rev.db --config-dir /tmp`. Ready script (us1, asks "yes" before truncating):
`~/tmp/jobqtool/jobq-truncate.sh [placement]`.
- **Mutating — needs explicit user approval.** `kubectl cp`/`exec` change prod even for `len`/`stat`; `truncate`/`clean`/`trim` drop jobs.
- **Truncating while the cause is still active is useless**: the next checker pass pushes the same segments and the
  placement is full again in hours (us1 2026-10-21: `_12` went 11 → 22.4M in ~3h). Fix the cause (nodes back) or wait.

## Traps
- `repair_queue` at exactly the cap is not "22M to repair". Compare with `remote_segments_needing_repair`.
- `repair_queue` comes from core's `Stat`, sampled every 15m; when the stat call fails it reports 0 everywhere.
- `remote_segments_needing_repair` is the last loop's total; in regions where repair keeps up (eu1) it is much higher than the queue. Not a live backlog.
- `{app="jobq"}` also has many `checker_*`, `choiceofn_*`, `stream_*` series — they are registered by imported
  packages and mean nothing in jobq. Use only `jobq_*` and scope `..._jobq_server`.
- Metric ingestion into VictoriaMetrics was ~1h behind for all apps on 2026-10-21 ~10:40 UTC; if the last point is old,
  check `up{app="satellite-core"}` freshness before calling jobq dead.
- A jobq restart (rollout, OOM, config change) silently wipes all queues; repair idles until the next checker pass.

## Where to look next
- Queue full / growing → checker in ranged-loop (`remote_segments_needing_repair`, offline nodes): [ranged-loop.md](ranged-loop.md).
- Queue drains slowly → repair throughput: [repair.md](repair.md).
- `repair_queue` all zero → core `QueueStat` errors or a jobq restart: [core.md](core.md).
