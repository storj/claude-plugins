---
name: satellite-observability
description: Use before querying production Storj satellite telemetry in Grafana — which datasource holds satellite metrics, the mandatory label selectors per peer, k8s pod metrics, where prod logs live (ClickHouse otel_logs) and how to query them, what is NOT available (traces, profiles), and the traps that make queries silently return wrong data. Pair with monkit-metrics (metric shapes) and satellite-peers (per-peer signals).
---

# Observing production satellites

Facts verified 2026-10-09. Observability infra is being migrated; if a datasource below fails,
run `list_datasources` once and say what changed in your report.

## What exists (and what does not)

| Signal | Status | Where |
|---|---|---|
| App metrics (monkit) | **Yes**, all prod regions | VictoriaMetrics `ffqrvx0pyhp8gf` (Grafana default) |
| k8s pod metrics | Yes | same datasource |
| Prod satellite **logs** | **Yes** (most peers) | ClickHouse `otel.otel_logs` via `cfqvdb4b1y96oa` — see Logs below |
| Traces / profiles | No satellite data in Tempo/Pyroscope | live pprof via `satellite-debug-port` skill |
| Eventkit events | Not in Grafana | ClickHouse warehouse (if you have access) |
| Prod config, version | git | `satellite-infra` skill |
| Live process state | debug port: `/mon/stats`, `/debug/pprof`, `/metrics` | `satellite-debug-port` skill |

## Logs (ClickHouse `otel.otel_logs`)

Prod satellite logs are in **ClickHouse**, table `otel.otel_logs`, through the Grafana datasource
`cfqvdb4b1y96oa` (grafana-clickhouse-datasource). They are **not** in VictoriaLogs. Human view:
dashboard `otel-logs-explorer` (https://grafana.storj.tools/d/otel-logs-explorer). Verified 2026-10-09:
all four regions, near real time, **about 9 days of retention** (oldest rows on 2026-10-09 were from 2026-09-30).

Run SQL through the Grafana API (read-only SELECT):
```
grafana_api_request POST /api/ds/query
{"from":"now-1h","to":"now","queries":[{"refId":"A","format":1,
  "datasource":{"uid":"cfqvdb4b1y96oa","type":"grafana-clickhouse-datasource"},
  "rawSql":"SELECT ..."}]}
jq: .results.A.frames[0].data.values
```
Tip: build one string column with `concat(...)` and read `.values[0]` — easier than many columns.

Columns that matter:
- `Timestamp` — always filter on it first (huge table: us1 alone is millions of lines per hour).
- `ResourceAttributes['environment_name']` — `storj-prod-satellite-us1` etc.
- `` `__otel_materialized_k8s.namespace.name` ``, `` `__otel_materialized_k8s.pod.name` ``,
  `` `__otel_materialized_k8s.container.name` `` (= `satellite` for the app container). Use these
  materialized columns, not the map, for speed. Pod name = `<app>-<hash>-<id>`, so filter a peer with
  `pod.name LIKE 'satellite-api-%'`.
- `ResourceAttributes['server_name']` — the host (bare-metal repairers have no k8s fields).
- `Body` — the line. `SeverityText`, `ServiceName`, `ScopeName` are **empty** for satellite logs.

**Two line formats** — your query must handle both:
1. rke2 clusters (most peers): zap console text with tabs:
   `08:25:00.032<TAB>warn<TAB>metainfo:endpoint<TAB>Storage limit exceeded<TAB>limit=2000000000000<TAB>public_id=...`
   → `splitByChar('\t', Body)`: `[2]` level, `[3]` logger, `[4]` message, `[5..]` `key=value` fields.
   An error with a stack trace has it inside the message part.
2. legacy k3s clusters (`rh-ewr-satellite-repair`, `cb-ash-satellite-repair`): GCP JSON. Parsed into
   `LogAttributes['severity']` (`WARNING`, `ERROR`), `LogAttributes['message']`, and
   `LogAttributes['logging.googleapis.com/labels']` (JSON with `error`, `node_id`, ...). No logger name.

Top errors and warnings per peer (verified, handles both formats):
```sql
WITH splitByChar('\t', Body) AS p,
     LogAttributes['message'] != '' AS is_json,
     replaceOne(lower(if(is_json, LogAttributes['severity'], p[2])), 'warning', 'warn') AS level,
     if(is_json, '', p[3]) AS logger,
     if(is_json, LogAttributes['message'], p[4]) AS msg,
     extract(`__otel_materialized_k8s.pod.name`, '^(.*?)-[a-z0-9]+(-[a-z0-9]+)?$') AS deploy
SELECT concat(toString(count()), ' | ', deploy, ' | ', level, ' | ', logger, ' | ', substring(msg, 1, 120))
FROM otel.otel_logs
WHERE Timestamp > now() - INTERVAL 1 HOUR
  AND ResourceAttributes['environment_name'] = 'storj-prod-satellite-us1'
  AND `__otel_materialized_k8s.namespace.name` = 'satellite'
  AND `__otel_materialized_k8s.container.name` = 'satellite'
  AND level IN ('error', 'warn', 'fatal')
GROUP BY deploy, level, logger, msg ORDER BY count() DESC LIMIT 20
```
(In the JSON body for `/api/ds/query` write the tab as `'\\t'`.)

Rules: count with `GROUP BY`, never pull raw lines without `LIMIT`; keep windows short (≤ 1h) for raw
lines; find one example line only after you know which message matters. Log fields can hold
customer data (bucket names, project ids) — don't copy them into reports unless needed.

**Coverage gaps (2026-10-09):** us1 `satellite-repair` (the dp-lax k8s repairers) has **no logs**;
the other us1 repair sites do. No logs for `satellite-change-stream`. gc-bf and gc-sender log in all four regions (gc-bf only while a job runs).
`satellite-ranged-loop` logs ~12 lines/day (low verbosity, not missing). `satellite-repair` replicas
outside k8s (bare metal) ship nothing. Also: us1 `satellite-api` warns "Storage limit exceeded" ~170k/h — normal.
**Shipped log levels differ per peer** (us1, 2026-10-09): repair (`rh-ewr`, `cb-ash`) **warn + error only**;
gc-sender **warn only**; api, core, auditor, admin, console info + warn + error; ranged-loop info; gc-bf debug + info.
So an info message from repair (e.g. "audit failed" with the node ID) is not in ClickHouse — say the data is not
available instead of searching. Check first: `groupUniqArray(level)` per deployment over 24h.

**Stack traces are split into rows**: each frame of a Go stack trace is its own log row (Body like
`\t/go/satellite/...go:164`). They pollute `GROUP BY` on the message column — filter on `p[2] IN ('error','warn',...)`
(frames have no level). zap error fields appear in Body as `exception.message=...` and `exception.stacktrace=...`,
**not** `error=` — e.g. `arrayFirst(x -> startsWith(x, 'exception.message='), p)`.
The cause can also be the **last row after the stack frames** (same timestamp +~10 ms, no tab, no level, e.g.
`Error 8175 (HY000): Your query has been cancelled due to exceeding the allowed memory limit...`).
**The cause is in separate stderr rows starting with `---`** (`--- context canceled`, `--- metabase: failed to scan
segments: ...`), with `LogAttributes['log.iostream'] = 'stderr'` and no level — so a level filter hides them. Recipe:
fetch all raw rows of that pod for ±2 s around the error (`ORDER BY Timestamp`), or
`WHERE Body LIKE '---%' GROUP BY Body` for a few minutes around it.

**Any SQL error comes back as a bare HTTP 400** with no message (bad syntax, positional `GROUP BY 1` / `GROUP BY 2,3` — group by the real expressions —, too much raw
data). Simplify the query to find the problem. Aggregate over long windows; fetch raw lines only for a window of
minutes around a known timestamp. The satellite-api pods (us1) export no Go runtime metrics (GC, heap, goroutines);
for those you need pprof via the debug port.

Dead or empty datasources (do not use): `afqbg5eubyqyob` (502), `cfpjntp1kao00d`, `de9dc57qvnegwc`,
`feghujtjgimtcd` (gone), `bfx67nh13k5j4f` (502), `efsa9dkqirrwgd` (VictoriaLogs, empty since 2026-09-22).
There is no Thanos datasource any more.

## Mandatory selectors

Every satellite query must pin **all three**:

```
environment_name="storj-prod-satellite-us1"   # or -eu1, -ap1, -slc
app="satellite-api"                           # the peer
kubernetes_namespace="satellite"              # slc also runs QA in "satellite-qa" with the SAME app names
```

Regions: `us1` (largest), `eu1`, `ap1`, `slc`. All four: `environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)"`.

`app` values: `satellite-api`, `satellite-console`, `satellite-core`, `satellite-auditor`,
`satellite-repair`, `satellite-ranged-loop`, `satellite-gc-bf`, `satellite-gc-sender`,
`satellite-admin`, `satellite-change-stream` (us1, eu1), `jobq`.

**Repair has many apps.** us1 runs `rh-ewr-satellite-repair`, `cb-ash-satellite-repair`,
`slc-satellite-repair`, `ap1-satellite-repair` (namespace `satellite-us1`), and more. eu1 has
`hn-prod-storage-*-repair`. For "all repair workers" use `app=~".*repair"` and drop the namespace pin.

Never filter on the `service` label — it is not reliable across peers.

## Metric shape (monkit)

Load the `monkit-metrics` skill for full detail. The parts that break queries most often:

- Method names carry a receiver prefix: `name="__Endpoint__GetObject"`, `__SegmentRepairer__Repair`.
  Plain `name="GetObject"` returns nothing. Discover: `count by (name) (function{scope="...", field="total", environment_name="...", app="..."})`.
- Scope = Go package path with `.` and `/` → `_`: `storj_io_storj_satellite_metainfo`.
- `function` fields: `total` = all calls, `successes`, `failures` (= `errors`). Error ratio = `failures/total`.
  **`count` is NOT calls**: it is the failure counter split by `error_name` (`drpc_ResourceExhausted`, `drpc_NotFound`, ...).
  `sum by (error_name) (rate(function{field="count",...}[5m]))` tells you *which* errors.
- `function_times` is a gauge of a recent window: `r50`, `r90`, `r99`, `max`, `ravg` in **seconds**. No `histogram_quantile`, no `rate()`.
- Per-run gauges (ranged loop observers) use `field="recent"`; never `rate()` them.
- `placement` label values have an underscore: `_0`, `_12`.
- Some failures are not errors in code (logged and `return nil`). A `failures=0` line does not prove health — check the peer card's "silent failures" list.

Example (us1 api GetObject):
```promql
# call rate
sum(rate(function{field="total",app="satellite-api",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1",scope="storj_io_storj_satellite_metainfo",name="__Endpoint__GetObject"}[5m]))
# error ratio
sum(rate(function{field="failures",...same...}[5m])) / sum(rate(function{field="total",...same...}[5m]))
# p99 latency (seconds), worst pod
max(function_times{field="r99",...same...})
```

## Pod / k8s metrics

Two families with **different label names** — they do not join:

- kube-state-metrics (since 2026-08-24): `kube_pod_container_status_restarts_total`, `kube_pod_info`,
  `kube_deployment_status_replicas`. Labels `namespace`, `pod`, `container`
  (here `kubernetes_namespace` is the KSM pod's own namespace — ignore it).
  ```promql
  sum by (pod) (increase(kube_pod_container_status_restarts_total{environment_name="storj-prod-satellite-us1",namespace="satellite",pod=~"satellite-api-.*"}[1h]))
  ```
- OTel: `container_cpu_time_seconds_total` (counter, CPU-seconds → `rate(...[1h])` = cores), `container_memory_working_set_bytes`,
  `k8s_container_memory_limit_bytes`. Labels `k8s_pod_name`, `k8s_namespace_name`, `k8s_container_name`. **No `app` label** — use `k8s_pod_name=~"satellite-api-.*"`.
  **Do not use `container_cpu_usage` for CPU trends or "near the limit" claims.** It is a gauge in cores sampled
  once per scrape: `max_over_time` of it shows short spikes (e.g. 15.8 of 16 cores) that are not sustained load,
  and sums of it jump between steps (46 / 29 / 46 / 29). Use the counter above.
  ```promql
  sum(rate(container_cpu_time_seconds_total{environment_name="storj-prod-satellite-us1",k8s_namespace_name="satellite",k8s_pod_name=~"satellite-api-.*",k8s_container_name="satellite"}[1h]))
  ```
- cAdvisor (CPU throttling, labels `namespace`, `pod`, `container` like KSM): before saying pods are CPU-starved, check
  `sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{environment_name="...",namespace="satellite",pod=~"satellite-api-.*",container="satellite"}[1h])) / sum by (pod) (rate(container_cpu_cfs_periods_total{...same...}[1h]))`.
  A few % or less = not throttled.

Monkit app metrics use a third name for the pod: `kubernetes_pod_name`.

## Working rules

1. Start with a narrow window (1h, `step` 60s). Widen only when needed.
2. Aggregate before reading. `sum by (...)` / `max by (...)` — raw series lists are often 50+ series.
3. Compare regions, but normalize for config first (replicas and flags differ a lot — see `satellite-infra`).
4. Before you trust a metric, find where it is emitted in code and read what it really counts.
   For a monkit `failures` counter, list every `return err` path of that function — the counter
   mixes all of them (e.g. repair `verifyPieceHash` fails on invalid args, piece id change, hash
   mismatch *and* bad signature; it is not "corrupt pieces").
5. Change points: compare against deploys (`image.tag` history in infra repo) and config commits.
6. Write raw data to `~/tmp/<topic>/`, not into your answer.
