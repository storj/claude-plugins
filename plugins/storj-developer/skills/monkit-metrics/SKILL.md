---
name: monkit-metrics
description: Use when investigating latency, throughput, error rate, or concurrency of any Storj Go service (satellite, storagenode, gateway, linksharing, etc.), comparing performance across pods/nodes/regions, finding which Go functions are instrumented, or interpreting monkit `function` / `function_times` series from VictoriaMetrics/Prometheus.
---

# Monkit Prometheus Metrics

## Overview

Monkit (`defer mon.Task()(&ctx)(&err)`) emits **two** metrics for every instrumented Go method:

- **`function_times`** — latency (percentiles, min/max, recent samples). Use for "how slow?"
- **`function`** — call counters and concurrency. Use for "how often?" / "how many in flight?" / "how many fail?"

Both share the same `name`, `scope`, and environment labels, so you usually query them with the same filter set.

## `function_times` — latency

### Structure

```
function_times{name="...", field="...", kind="...", scope="...", ...}
```

| Label | Values | Notes |
|---|---|---|
| `name` | `__Receiver__Method` or bare func name | See naming below |
| `field` | see field table | Most useful: `r99`, `recent` |
| `kind` | `success`, `failure` | Failure r99 can be 100×+ success r99 — always filter |
| `scope` | package import path with `/` → `_` | e.g. `storj_io_storj_satellite_metainfo` |

Values are in **seconds** — multiply by 1000 for ms.

### `field` values

| Field | Meaning | Available where |
|---|---|---|
| `count`, `sum` | Cumulative since process start | **everywhere** |
| `recent` | Most recent observation | **everywhere** |
| `min`, `max` | Cumulative min/max | Prometheus-scraped envs only |
| `r10`, `r50`, `r90`, `r99` | Percentile latency (rolling reservoir) | Prometheus-scraped envs only |
| `rmin`, `rmax`, `ravg` | Min/max/avg over the rolling window | Prometheus-scraped envs only |

#### Percentiles are NOT available in every environment

Which `field` values exist depends on the **scrape path**, not the metric. Verified
2026-09-08 on `ffqrvx0pyhp8gf`:

| `environment_name` | Scraped by | `field` values present |
|---|---|---|
| `storj-prod-satellite-us1` / `-eu1` / `-ap1` / `-slc` | in-cluster Prometheus | full set incl. `r10`–`r99`, `rmin`/`rmax`/`ravg`, `min`, `max` |
| `storj-select`, `dp-prod-edge-*`, `storj-qa-satellite` | node-level **otelcol** | **only `count`, `recent`, `sum`** |

So `field="r99"` silently returns **no series** for the select and edge fleets — and a
no-data result looks identical to a wrong label name, which is an easy hour to lose.

**Portable fallback that works in every environment — mean latency:**

```promql
sum(rate(function_times{..., field="sum"}[10m]))
  /
sum(rate(function_times{..., field="count"}[10m]))
```

Before assuming a field exists, check:

```promql
count by (field, kind) (function_times{environment_name="...", scope="...", name="..."})
```

## `function` — call counters & concurrency

Same `name`/`scope`/env labels as `function_times`. **No `kind` label** — outcomes are encoded in `field`.

### `field` values

| Field | Kind | Meaning | How to query |
|---|---|---|---|
| `total` | counter | All invocations | `rate(...[5m])` = total QPS |
| `successes` | counter | Non-error returns | `rate(...[5m])` = success QPS |
| `failures` | counter | Error returns | `rate(...[5m])` = error rate |
| `errors` | counter | Same as `failures` in current monkit | — |
| `panics` | counter | Go panics caught by monkit | `rate(...[5m])` — should be ~0 |
| `count` | counter | Completed invocations | `rate(...[5m])` ≈ `rate(total[5m])` |
| `current` | gauge | In-flight invocations right now | Read as-is |
| `highwater` | gauge | Peak `current` ever seen | Read as-is |
| `delta` | counter-ish | Completions since last sample | Rarely useful — prefer `rate(total)` |

### When to use `function` vs `function_times`

| Question | Metric / field |
|---|---|
| What's the request rate? | `rate(function{field="successes"}[5m])` |
| What's the error rate? | `rate(function{field="failures"}[5m])` |
| Error percentage? | `rate(function{field="failures"}[5m]) / rate(function{field="total"}[5m])` |
| Are we panicking? | `rate(function{field="panics"}[5m])` |
| How many calls are in flight? | `function{field="current"}` |
| Peak concurrency seen? | `function{field="highwater"}` |
| How slow is it? | `function_times{field="r99", kind="success"}` |

### Example: compare QPS across regions

```promql
sum by (environment_name) (
    rate(function{
        name="__Endpoint__CommitObject",
        scope="storj_io_storj_satellite_metainfo",
        field="successes"
    }[5m])
)
```

## Naming Convention

| Go code | `name` label |
|---|---|
| `func (e *Endpoint) CommitObject(...)` | `__Endpoint__CommitObject` |
| `func commitObject(...)` (package-level) | `commitObject` |
| `func (s *SpannerAdapter) CommitObject(...)` | `__SpannerAdapter__CommitObject` |

## Same name, different scope

The same `name` can appear under multiple `scope` values. Example: `__Endpoint__CommitObject` lives in `storj_io_storj_satellite_metainfo` (the handler), and the RPC wrapper for the same call shows up as `_metainfo_Metainfo_CommitObject` in `storj_io_common_rpc_rpctracing`. Always include `scope` (or `scope=~"..."`) when comparing or you will mix unrelated timings.

> Note: Storj scopes double — `storj_io_storj_satellite_metainfo` — because the import path is `storj.io/storj/...`. Expected.

## Finding Instrumented Methods

Methods with `defer mon.Task()(&ctx)(&err)` emit metrics:

```bash
rg "defer mon\.Task\(\)" satellite/metainfo/
rg -n "func.*CommitObject|defer mon\.Task" satellite/metainfo/endpoint_object.go
```

To trace direct callees: find method calls in the function body, locate their definitions, check for `defer mon.Task()`.

## Querying via Grafana MCP

This is the primary path — the datasource sits behind Grafana auth, so direct `curl` won't work.

### Datasource UIDs

Use **`ffqrvx0pyhp8gf`** — VictoriaMetrics (victoria.intra.storj.tools), the default
Prometheus datasource. It carries monkit `function` / `function_times` for **all** four prod
satellites plus the select fleet, the edge/gateway fleet and QA. Confirmed 2026-09-08.

That is the only metrics datasource in use. Infrastructure metrics live here too, under
their own job labels — e.g. the Crunchbits-PA TiDB cluster is
`{job="tidb_cluster", server_name=~"cb-prod-us1-pa-[1-7]"}`.

Verify with `mcp__grafana-cloud__list_datasources(type="prometheus")` if this changes.

### Discovery workflow

```text
# 1. Confirm the metric exists in this datasource
mcp__grafana-cloud__list_prometheus_metric_names(
    datasourceUid="adoggz37zfda8f", regex="function_times")

# 2. Discover real label values (don't guess `environment_name`s)
mcp__grafana-cloud__list_prometheus_label_values(
    datasourceUid="adoggz37zfda8f", labelName="environment_name",
    matches=[{"filters":[{"name":"__name__","type":"=","value":"function_times"}]}])

# 3. Query
mcp__grafana-cloud__query_prometheus(
    datasourceUid="adoggz37zfda8f",
    expr='function_times{name="__Endpoint__CommitObject", scope=~".*satellite_metainfo", field="r99", kind="success"}',
    queryType="range", startTime="now-1h", endTime="now", stepSeconds=60)
```

- `queryType="instant"` → single point right now. Use to sanity-check a label combo exists.
- `queryType="range"` → time series. Requires `startTime`, `endTime`, `stepSeconds`.
- Times accept RFC3339 (`2026-05-19T22:00:00Z`) or relative (`now`, `now-1h`, `now-30m`).
- Use `mcp__grafana-cloud__generate_deeplink` to hand the engineer a Grafana Explore URL when reporting findings.

### Correlating with logs

Storj app logs are in **VictoriaLogs uid `bfx67nh13k5j4f`**, not Loki
(`grafanacloud-logs` has no `environment_name` values). Query with
`mcp__grafana-cloud__query_loki_logs` but pass **LogsQL** and `queryType: "range"` —
`instant` returns nothing. Message text is `_msg`, not `msg`; satellite Go logs use
`severity`, not `level`. Aggregate with `| stats by (...) count()`; never dump raw records.

See the repo-local **`satellite-logs`** skill for the full field map and query ladder.

### Direct HTTP fallback

Only when Prometheus is reachable without auth (local Prometheus, dev cluster):

```bash
curl -sG 'http://HOST:9090/api/v1/query' \
    --data-urlencode 'query=function_times{name="__Endpoint__CommitObject",field="r99",kind="success"}'
```

## Comparing instances / regions

Multiple series per `name` (one per pod). Aggregate before computing stats.

**PromQL — let Prometheus do the work:**

```promql
quantile by (environment_name, name) (
    0.99,
    function_times{name=~"...", field="r99", kind="success"}
) * 1000
```

**Python — reusable pattern:** see `compare-instances.py` in this directory (range query + aggregate across pods, two endpoints).

## Common Mistakes

| Mistake | Fix |
|---|---|
| Filtering `kind="error"` on `function_times` | Use `kind="failure"`. `error` returns zero series. |
| No `kind` filter on `function_times` | `failure` r99 can dwarf `success` r99 for the same name |
| Filtering `kind="..."` on `function` | `function` has no `kind` label — use `field="successes"` / `"failures"` instead |
| Using `function_times{field="count"}` for QPS | Works, but `function{field="successes"}` (or `"total"`) is the idiomatic call-rate counter |
| Forgetting `rate()` on counter fields | `total`, `successes`, `failures`, `errors`, `panics`, `count` are counters — always wrap in `rate(...[Xm])` |
| Using `rate()` on gauge fields | `current` and `highwater` are gauges — query directly, not via `rate()` |
| Same `name` in multiple scopes | Add `scope=~"..."` to disambiguate |
| Comparing raw series across pods | Aggregate (sum/avg/quantile) by the labels you care about |
| Averaging `r10`/`r50`/`r90`/`r99` across pods | Averaging percentiles is mathematically meaningless. Use `quantile by (...)` or `max by (...)` — never `avg by` on percentile fields. |
| Forgetting seconds → ms | `function_times` values are seconds — multiply by 1000 |
| Missing instrumented methods | Check both `success` and `failure` kinds (on `function_times`) or `field="failures"` (on `function`) |
| Assuming `field="r99"` exists | Percentiles are absent on otelcol-scraped envs (`storj-select`, `dp-prod-edge-*`, QA). Check `count by (field)` first; fall back to `rate(sum)/rate(count)` |
| `count by (scope) (function_times{...})` with a broad regex | can return **502** from the datasource. Narrow with `environment_name` + a `scope=~` prefix, or query one scope at a time |
| Guessing the `name` label | wrappers add prefixes: gateway `__gatewayLayer__GetObjectNInfo`, uplink `__Client__DownloadObject`, metabase `__TiDBAdapter__GetObjectLastCommitted`. A wrong `name` returns no-data that looks like a wrong label. Discover with `count by (name) (function{scope="...", field="errors"})` |
| Assuming one host = one service | the `cb-select-storage-pa-*` boxes run storagenode + gateway + a co-located US1 satellite-api, split across **two** `environment_name`s: `storj-select` (gateway/storagenode) and `storj-prod-satellite-us1` (satellite-api). Filtering only by `server_name` mixes them |
| Summing a per-store TiKV gauge | e.g. `tikv_engine_pending_compaction_bytes` has one series per store *and* column family (28 on cb-pa). `sum()` inflates it ~28x — use `max()`/`avg()` and compare against the per-CF limit |
