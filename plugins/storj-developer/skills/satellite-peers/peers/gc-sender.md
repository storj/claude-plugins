# Peer card: gc-sender (garbage collection retain-filter sender)

Verified 2026-10-12 against storj/storj `main`, storj/infra and prod metrics/logs (all 4 regions). Line numbers drift — search by symbol.
The producer side (bloom filter scan, publish guard, CronJob, `LATEST` write) is in [gc-bf.md](gc-bf.md) — read it first; this card covers only the sender.

## Identity
- Subcommand `satellite gc-sender` → `satellite/run/gc_sender.go` (`GcSender.GetSelector` = `Observability` + `*sender.Service`).
  Package `satellite/gc/sender` (`service.go`, `iterators.go`), config prefix `garbage-collection`.
- k8s Deployment `app="satellite-gc-sender"`, namespace `satellite`, **1 replica** in us1, eu1, ap1, slc. Pod label `service: gc`.
- Region base `satellite.yaml` sets `garbage-collection.enabled: false` (so core does not send — see [core.md](core.md));
  `satellite-gc-sender.yaml` turns it back on. Bucket = region name (`us1`, `eu1`, ...), access grant from Vault.
- us1 sender was not installed while the gc-bf-once migration was being validated (`installed: false`); infra `cdaca2f0d`
  re-enabled it on 2026-09-29 after a guarded manual run.
- Memory limits: us1 **2Gi**, eu1 1Gi, ap1/slc 1.5Gi (chart default). CPU 250m/500m.

## What runs inside
`Service.Run` → `sync2.Cycle` every `interval` (1h) → `RunOnce`:
1. Download `LATEST` from the bucket. Missing → info "LATEST file does not exist in bucket", `return nil` (idle).
2. Prefix = content of `LATEST` + `/`. List zips under that prefix (`IterateZipObjectKeys`, non-recursive).
3. Per zip, **sequentially**: download the whole zip into memory (`io.ReadAll`), then for each entry (one node,
   named by node ID) `UnpackZipEntry` + `validate` and start `sendRetainRequest` under a limiter of `concurrent-sends` (100).
   The limiter is per zip and a zip holds `zip-batch-size` (40) filters, so **real concurrency is ≤ 40**. Wait for all.
4. Zip OK → `MoveObject` to `sent-<prefix>/...`. Zip download/unzip failed → move to `error-<prefix>/...` plus an
   `.error.txt` object (expires after `expire-in`, 336h).
5. `sendRetainRequest`: `overlay.Get(node)` → disqualified or gracefully exited → **return nil (counted as success, nothing sent)**;
   else dial + `piecestore.Client.Retain` with `retain-send-timeout` (1m). Filters > the message limit go over the
   streaming `RetainBig` RPC (node side `__Endpoint__RetainBig`).

How generations move:
- The sender never deletes or edits `LATEST`. After a generation is sent the prefix is empty, and each hourly `RunOnce`
  just lists nothing (~25 ms; eu1 ~0.7 s).
- gc-bf rewrites `LATEST` **after each shard** (us1 has 2 shards in one prefix), so us1 starts sending shard 0 while gc-bf
  still scans shard 1. Example 2026-10-11: shard 0 published 05:40, shard 1 14:12; sends ran ~05:40 → 2026-10-12 ~01:00.
- `RunOnce` reads `LATEST` once. A run that is in progress finishes the old prefix even if `LATEST` changes; the next run
  takes the new one. Zips of an old generation that were never listed are **abandoned** (expire after 336h).
- There is **no coordination** with gc-bf. gc-bf's "target bucket was not empty" check lists the *new* timestamp prefix
  (`freshPrefix`), which is always empty, so it does not wait for the sender. 0 such log lines in 9 days (2026-10-12).
- No retry per node: a failed node waits for the next generation (2–3 per week).

## Key signals
Selector `S` = `app="satellite-gc-sender",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1"`, scope `storj_io_storj_satellite_gc_sender`.

| Question | PromQL |
|---|---|
| Is the loop alive? | `function{field="current",S,name="__Service__Run"}` = 1. A `RunOnce` error ends `Run` (see silent failures) |
| Deliveries over time | query **D** below (range, step 6h) |
| Failure ratio (7d) | query **F** below |
| Why sends fail | only logs (all failures have `error_name="gc"`) — SQL **L** below |
| Zips finished | `sum(increase(function{field="successes",S,name="__Project__MoveObject"}[7d]))` (×40 ≈ nodes) |
| Nodes skipped (DQ / exited) | `sendRetainRequest` successes − `__Client__Retain` successes (us1 83 / 7d, others 1) |
| Longest send run (s) | `max(function_times{field="max",kind="success",S,name="__Service__RunOnce"})` |
| Restarts / OOM | `sum(increase(kube_pod_container_status_restarts_total{environment_name="...",namespace="satellite",pod=~"satellite-gc-sender-.*"}[14d]))`, then `max by (pod, reason) (last_over_time(kube_pod_container_status_last_terminated_reason{...same...}[14d]))` |
| Memory vs limit | OTel `max(max_over_time(container_memory_working_set_bytes{environment_name="...",k8s_namespace_name="satellite",k8s_pod_name=~"satellite-gc-sender-.*",k8s_container_name="satellite"}[7d]))` vs `k8s_container_memory_limit_bytes{...same...}` |

```promql
# D: successful sends per 6h, all regions. Bursts = generations; flat 0 for > 3 days = nothing published or sender dead.
round(sum by (environment_name) (increase(function{field="successes",app="satellite-gc-sender",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",name="__Service__sendRetainRequest"}[6h])))
```

```promql
# F: failure ratio of retain sends over 7d
sum by (environment_name) (increase(function{field="failures",app="satellite-gc-sender",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",name="__Service__sendRetainRequest"}[7d]))
/ sum by (environment_name) (increase(function{field="total",app="satellite-gc-sender",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",name="__Service__sendRetainRequest"}[7d]))
```

### Baselines (7d to 2026-10-12)
| | us1 | eu1 | ap1 | slc |
|---|---|---|---|---|
| sends ok / failed | 114k / 19.6k | 79k / 11.4k | 27k / 3.1k | 70k / 9.6k |
| failure ratio | 14.7% | 12.6% | 10.3% | 12.0% |
| ok per generation | ~28k | ~26k | ~26k | ~23k |
| one generation takes | ~20h (2 shards, spans 2 UTC days) | ~18h | ~18h | ~18h |
| max working set | **1.87 GiB of 2 GiB** | 0.6 of 1 GiB | 0.16 of 1.5 | 0.1 of 1.5 |

- Speed ~1.5k ok sends/h (us1): zips are sequential and each waits for its slowest node (up to 1m timeout).
- Failure level 10–15% is normal (node connectivity). A **jump** in the ratio, or zero deliveries for a region, is the signal.
- Generation count follows the gc-bf schedule. A 7d baseline taken while gc-bf did not run is wrong: ap1 got **one**
  generation (2026-10-06) between 10-02 and 10-12 — its 10-04, 10-08 and 10-11 CronJob pods stayed Pending.
- us1 2026-10-06 had a 42h burst of ~58k ok: two generations back-to-back (catch-up after the gc-bf suspension).

## Silent failures
- **Per-node errors only warn** ("Error sending retain filter"); the zip is still moved to `sent-`, the node is not retried.
- **Disqualified / exited nodes count as success** without any send. Use `__Client__Retain` for real RPCs.
- **Bad zip entries** (wrong node ID, zero pieces, empty/zero filter, creation date < 2020) → warn "Skipping retain filter entry",
  `return nil`. 0 lines in 7d.
- **LATEST missing** → info log only (not shipped) → looks exactly like "nothing to send". Check gc-bf publish logs instead.
- **Any `RunOnce` error ends the service**: `sync2.Cycle.Run` returns on the first error (LATEST read error other than
  not-found, list error, move error). `RunOnce` / `Run` failures were 0 in 7d — watch `__Service__Run` `current`.
- **OOM restarts**: us1 was OOMKilled 3× in 14d (2026-10-01, 10-02, 10-06), all during sends. Whole zips (40 filters,
  us1 `max-bloom-filter-size` 50MiB) are held in memory. After restart the current zip is sent again (harmless) and
  sending resumes; in-flight sends end with "gc: context canceled".

## Logs
gc-sender ships **warn only** (plus a few startup info lines). Logger `sender:service`.
Warns: "Error sending retain filter" (`node_id`, cause in `exception.message=`), "Skipping retain filter entry".
Line count matches the `failures` metric exactly. Not searchable (info): "LATEST file does not exist in bucket".

SQL **L** — send failures by cause (7d):
```sql
WITH splitByChar('\t', Body) AS p, arrayFirst(x -> startsWith(x, 'exception.message='), p) AS em,
  multiIf(em LIKE '%i/o timeout%', 'i/o timeout', em LIKE '%no route to host%', 'no route', em LIKE '%connection refused%', 'refused',
          em LIKE '%context deadline exceeded%', 'deadline', em LIKE '%connection reset%', 'reset', em LIKE '%EOF%', 'EOF',
          em LIKE '%tls%' OR em LIKE '%certificate%', 'tls', em LIKE '%dial%', 'other dial', substring(em, 1, 90)) AS cls
SELECT concat(toString(count()), ' | ', ResourceAttributes['environment_name'], ' | ', cls)
FROM otel.otel_logs
WHERE Timestamp > now() - INTERVAL 7 DAY
  AND `__otel_materialized_k8s.namespace.name` = 'satellite'
  AND `__otel_materialized_k8s.pod.name` LIKE 'satellite-gc-sender-%'
  AND `__otel_materialized_k8s.container.name` = 'satellite' AND p[2] = 'warn'
GROUP BY ResourceAttributes['environment_name'], cls ORDER BY count() DESC LIMIT 40
```
2026-10-12: i/o timeout ~35–45%, then connection refused, no route to host, deadline exceeded, reset, EOF, tls — all node side.
A few are node bugs returned over RPC (us1: "gc: open /app/config/storage/hashstore/meta/..."). "context canceled" = pod restart.

When did a generation become available? Use the gc-bf "collecting bloom filters finished" log (one line per shard,
`upload_prefix` = the new `LATEST`) — see [gc-bf.md](gc-bf.md).

## Node-side signals
Only the Select fleets report node metrics (`environment_name` `storj-select`, `select-eu`); no `satellite` label, so you
cannot tell which satellite sent a filter. Public SNO nodes report nothing.
- `function{name=~"__Endpoint__Retain.*",field=~"successes|failures",environment_name=~"storj-select|select-eu"}` —
  storj-select uses mostly `RetainBig` (~12k ok / 7d), select-eu `Retain` (357).
- Age of the oldest last-received filter (h): `(time() - min by (environment_name) (retain_creation_date{field="recent",environment_name=~"storj-select|select-eu"})) / 3600`
  (2026-10-12: 34h / 41h). Also `retain_filter_size`, `retain_filter_hash_count`.
- The node queues the filter (`processRetainReq`); piece deletion happens later in the node's retain worker — a successful send
  is not proof that garbage was removed.

## Config that matters (`satellite/gc/sender/service.go` `Config`, prefix `garbage-collection`)
`enabled` (true; region base false, sender file true), `interval` (1h), `concurrent-sends` (100, but capped by 40 per zip),
`retain-send-timeout` (1m), `bucket`, `access-grant` (secret, needs read+write: it moves objects), `expire-in` (336h, error objects only).
None of these are overridden in prod values except `enabled`/`bucket`. `zip-batch-size` and `max-bloom-filter-size` are set on
gc-bf but decide sender memory and concurrency.

## Traps
- `function_times` `max` of `RunOnce` is a lifetime max (35–48k s), not the last run; `r50` is the idle run.
- Hourly `RunOnce` count < 168/7d during sends (us1 104): a long run swallows ticks. Not an error.
- `IterateZipObjectKeys` success ≠ something sent; it also succeeds on an empty prefix.
- Do not compare regions by ok count without the gc-bf schedule (us1 Mon/Wed/Sat, eu1 Mon/Wed/Sat, ap1 Sun/Tue/Thu, slc Sun/Tue/Fri).
- Burst missing in **D** → first check gc-bf pod phase / CronJob suspend ([gc-bf.md](gc-bf.md)), then sender `Run` current and restarts.

## Where to look next
- No deliveries but sender alive → gc-bf did not publish ([gc-bf.md](gc-bf.md) publish log, pod phase, suspend).
- Failure ratio jump → SQL **L** by cause; one cause dominating (e.g. tls, deadline) = satellite network/egress or a node release.
- us1 memory near limit / OOM → raise the 2Gi limit or lower gc-bf `zip-batch-size`; code fix would stream the zip instead of `io.ReadAll`.
- Garbage not dropping on nodes although sends succeed → node retain worker (storagenode `retain` package), not the satellite.
- Grafana dashboard: `sat-gc-v3` ("Garbage Collection v3").
