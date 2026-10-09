# Peer card: api

Verified 2026-10-09 against storj/storj `main` and us1 metrics. Line numbers drift — search by symbol.

## Identity
- Subcommand `satellite api` → `satellite/run/api.go` (`Api.GetSelector`).
- `app="satellite-api"`, namespace `satellite`, all regions. HPA (us1 5–20 pods). us1 also has bare-metal api hosts (`cb_pa`, Ansible).
- Serves the DRPC API that uplinks, gateways and storage nodes call.

## What runs inside
Selector: `Observability` + `*satellite.EndpointRegistration` + `*orders.Chore` + `*metainfo.TrackerInfo`.
- `EndpointRegistration` (`satellite/mud.go`, search `EndpointRegistration`) registers:
  `metainfo.Endpoint` (objects/segments/buckets — the main load), `orders.Endpoint` (node order settlement),
  `snopayouts.Endpoint`, `nodestats.Endpoint`, `contact.Endpoint` (node check-ins),
  `userinfo.Endpoint` (only if `userinfo.enabled`), `gracefulexit.Endpoint` (only if `graceful-exit.enabled`).
- Pulls in `overlay.Service` with the upload node-selection cache, metabase DB, rate limiters.
- `orders.Chore` flushes the bandwidth rollups write cache to the DB.
- `metainfo.TrackerInfo` (`satellite/metainfo/mud.go`) — success-tracker debug page `/trackers`.

## Key signals
Selector `S` = `app="satellite-api",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"`.

| Question | PromQL |
|---|---|
| Request rate per endpoint | `sum by (name) (rate(function{field="total",S,scope="storj_io_storj_satellite_metainfo",name=~"__Endpoint__.*"}[5m]))` |
| Error ratio per endpoint | same with `field="failures"` ÷ `field="total"` |
| Which errors | `sum by (name, error_name) (rate(function{field="count",S,scope="storj_io_storj_satellite_metainfo",name=~"__Endpoint__.*"}[5m]))` |
| p99 latency (s) | `max by (name) (function_times{field="r99",S,scope="storj_io_storj_satellite_metainfo",name=~"__Endpoint__.*"})` then pick the endpoints you care about |
| Uploads started | `sum(rate(req_put_object{field="total",S}[5m]))` (tags `placement`, `multipart`) |
| Downloads started | `sum(rate(req_get_object{field="total",S}[5m]))` |
| Rate limiting | `sum(rate(metainfo_rate_limit_exceeded{field="total",S}[5m]))` |
| Node selection empty | `sum(rate(stream_empty{field="total",S}[5m]))` (scope nodeselection) |
| Upload node cache size | `min(refresh_cache_size_reputable{field="recent",S})`, `..._new` |
| Pieces per committed segment | `segment_commit_pieces_successful{field="ravg",S}` vs `segment_commit_pieces_invalid` |
| Geofence lookup failures | `sum(rate(geofencing_lookup_failed{field="total",S}[5m]))` |
| Lost bandwidth rollups (money) | `rollups_write_cache_flush_lost`, `rollups_write_cache_update_lost` (scope orders) |
| Pod restarts / CPU | see `satellite-observability` (pods `satellite-api-.*`) |

Normal us1 baseline (2026-10-09): GetObject ~7.5k/s, ~7% failures, p99 ~30ms. `Batch` 6k–12k/s with
~10–12% failures, all client-side: ~400/s `drpc_NotFound` (missing objects) and ~410/s
`drpc_ResourceExhausted` (per-project rate limit, mostly ListObjects). Internal errors ≈ 0.
**Known nightly pattern:** every night ~19:00–05:00 UTC rate-limited `DownloadObject` rises to
900–2,200/s (one client's batch job). Seen every day for 8+ days — not an incident.
For "is this normal?" look at 8 days at a 3h step before comparing single hours. Compare to the
same hour last week before calling something abnormal.

## Silent failures (not visible as errors)
- **Not enough nodes on upload** (`overlay.ErrNotEnoughNodes`, `metainfo/endpoint_segment.go`): returned to client as FailedPrecondition, **not logged**. Watch `stream_empty` and BeginObject/BeginSegment failures.
- **Unknown internal errors**: `ConvertKnownErrWithMessage` logs "internal error" and returns Internal; process keeps running.
- **Usage tracking failures** (bandwidth, storage, segment usage in `validation.go` / `endpoint_segment.go`): logged and swallowed → project limits drift, no client error.
- **Rollups write cache** (`orders/rollups_write_cache.go`): when flush fails or is too slow, bandwidth data is **dropped** ("MONEY LOST!"). Only the `*_lost` metrics show it.
- **Rate-limiter cache error**: returns Unavailable to the client.
- **Project with limits 0/0**: `checkRate` returns PermissionDenied "All access disabled" — looks like an auth error.
- **Optional endpoints off by flag**: userinfo / gracefulexit silently not registered.

## Distinctive logs (logger + message; search them in `otel.otel_logs`)
- `metainfo:endpoint`: "internal error", "Unable to create order limits.", "Could not track new project's storage and segment usage", warn "Monthly bandwidth limit exceeded" / "Storage limit exceeded" / "Segment limit exceeded" / "Upload limit exceeded".
- `orders:rollupswritecache`: "MONEY LOST! Bucket bandwidth rollup batch flush failed", "MONEY LOST! Flushing too slow to keep up with demand".

## Config that matters (`satellite/metainfo/config.go`)
`metainfo.rate-limiter.enabled` (true), `.rate` (100 req/s per project), `.cache-capacity`, `.cache-expiration`;
`metainfo.upload-limiter.*` (1 upload/s per object key, burst 3); `metainfo.download-limiter.*`;
`metainfo.max-segment-size` (64MiB), `metainfo.max-inline-segment-size` (4KiB), `metainfo.max-commit-interval` (48h),
`metainfo.success-tracker-kind`. Per-project limit overrides live in the DB (console/admin), not in config.
Prod values: `helm/satellites/<region>/satellite-api.yaml` (see `satellite-infra`).

## Traps
- **Batch / CompressedBatch have no errors of their own.** Uplinks send almost everything through them;
  `Batch` returns the error of the first failing sub-request. Split by the sub-endpoint
  (`DownloadObject`, `ListObjects`, `GetObject`, `BeginObject`, ...) to find the real source.
- **One failure is counted many times**: under `checkRate`, `validateBasic`, `validateAuth`/`ValidateAuthN`,
  the sub-endpoint, `Batch` and `CompressedBatch`. Never sum `function{field="count"}` across names. For
  real per-endpoint numbers use `name!~"__Endpoint__(Batch|CompressedBatch|checkRate|validate.*|ValidateAuth.*)"`.
- **No project label** on any metric (`metainfo_rate_limit_exceeded` included). To find which project
  is throttled you need eventkit (tags `project-limited`, `rate-limit-kind` set in `checkRate`) in the
  ClickHouse warehouse — say so instead of guessing.
- The API is DB-heavy: slow endpoints are often the metabase (TiDB/Spanner/Postgres), not Go code. Check `scope=~"storj_io_storj_satellite_metabase.*"` `function_times` for the same window.
- `field="count"` on `function` = failures by `error_name`. Use `total` for request rate.
- `/trackers` and some debug extensions existed only in the legacy peer; check mud wiring if a debug page is missing.
