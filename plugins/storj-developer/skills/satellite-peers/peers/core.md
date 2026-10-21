# Peer card: core

Verified 2026-10-12 against storj/storj `main` (a6c71cde1), storj/infra and prod metrics/logs (all 4 regions). Line numbers drift — search by symbol.

## Identity
- Subcommand `satellite core` → `satellite/run/core.go` (`Core.GetSelector`). Providers: each package's `mud.go`, plus
  `satellite/mud.go` for `nodeevents.Chore`, `billing.Chore`, `storjscan.Service`.
- `app="satellite-core"`, **1 replica** per region, namespace `satellite`. slc also runs a QA core in `satellite-qa`
  with the same app — always pin the namespace.
- A bag of ~25 independent `sync2.Cycle` chores. No API. Each cycle fires at pod start, then every interval,
  so daily chores run at "pod start time + n×24h" (2026-10: ~20:56 UTC in us1/eu1/ap1, ~13:17 in slc).

## What runs inside
`mud.Select` starts **every** selected component; the `--components` flag (only slc uses it:
`--components=!accountfreeze.Chore`, no Stripe key) is the only way to drop one. Several `enabled` flags are **not read
by the modular code** (marked "ignored" below). Whether a chore really works = does its inner function have calls.

| Chore (scope suffix) | Interval in prod | Per-iteration function (use this, not `Run`) | Live? |
|---|---|---|---|
| tally `accounting_tally` | 7h (slc 3h) | `__Service__Tally`, `__Service__Purge` | all |
| rollup `accounting_rollup` | 24h | `__Service__Rollup` | all |
| rollup archive `accounting_rolluparchive` | 24h, age 90d | `__Chore__ArchiveRollups` (`enabled` ignored) | all — **us1 fails every day** |
| project bw cleanup `accounting_projectbwcleanup` | 24h | `__Chore__RunOnce` | all |
| node events `nodeevents` (`send-node-emails: true`) | 5m | `__Chore__process`, `__CustomerioNotifier__Notify` | all |
| offline nodes `overlay_offlinenodes` | 1h | `__Service__InsertOfflineNodeEvents` (scope `overlay`) | all |
| stray nodes `overlay_straynodes` | 168h | `__Service__DQNodesLastSeenBefore` (scope `overlay`) | all |
| containment sync `audit` | 2h | `__ContainmentSyncChore__syncContainedStatus` | all |
| repair queue stat `repair_repairer` | **15m** (code default 1h) | none — only `repair_queue` gauge | all |
| storjscan `payments_storjscan` | 1m | `__Client__AllPayments` | us1/eu1/ap1 (slc: `disable-loop` default true) |
| billing `payments_billing` | 15s | **none** (only `Run`) | us1/eu1/ap1 (slc: disabled by default) |
| billing freeze `accountfreeze` | 24h | `__Chore__attemptBillingFreezeWarn`, `__Chore__attemptBillingUnfreezeUnwarn` | us1/eu1/ap1 (`enabled` ignored; slc excluded) |
| trial / opt-out freeze `accountfreeze` | 24h | `__TrialFreezeChore__attemptTrialExpirationFreeze`, `__OptOutFreezeChore__attemptOptOutFreeze` | all |
| bot freeze | — | `Run` returns at once (`FlagBots` off) | none |
| pending delete `console_dbcleanup_pendingdelete` | 24h | `__Chore__runDeleteProjects`, `runDeletePendingDeletionUsers`, `runDeleteFrozenUsers` | us1/eu1/ap1 (slc not enabled) |
| console db cleanup `console_dbcleanup` | 24h | none (`enabled` ignored) | all |
| email reminders `console_emailreminders` | 24h | `__Chore__sendVerificationReminders`, `sendExpirationNotifications` | all |
| analytics, mailservice | services | `mailservice __Service__SendRendered` | all |
| expired deletion, zombie deletion, GC sender, project limit events | — | `Run` returns at once (disabled in config) | **none** |

GC sender runs in its own app `satellite-gc-sender` in all 4 regions (`garbage-collection.enabled: false` for core).
Expired deletion is off everywhere now (slc included) — "handled by TTLs". Node at-rest tally is **not** here: it is
the `nodetally` observer in ranged-loop; core's rollup only aggregates what it wrote.

## Key signals
Selector `C` = `app="satellite-core",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)"`.

| Question | PromQL |
|---|---|
| Runs / failures per chore (7d) | query **Q1** |
| Hours since last successful run | query **Q2** (MetricsQL) — best "not running" signal |
| Last run duration (s) | query **Q4** |
| Rollup archive per day | query **Q3** |
| Repair queue size (gauge for repair) | `sum by (environment_name, attempted) (repair_queue{field="count",C})` |
| Node-event mails | query **Q5** |
| Which chores exited at start (disabled) | query **Q6** — value 1 = `Run` returned = chore off |
| Version + pod start | `max by (environment_name, pod, image) (kube_pod_container_info{environment_name=~"...",namespace="satellite",container="satellite",pod=~"satellite-core-.*"})` |
| Memory vs limit, throttling, restarts | OTel/cAdvisor/KSM recipes from `satellite-observability` with `pod=~"satellite-core-.*"` |

```promql
# Q1: successes/failures of per-iteration functions, 7d
sum by (environment_name, scope, name, field) (increase(function{field=~"successes|failures",app="satellite-core",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",name=~"__Service__Tally|__Service__Purge|__Service__Rollup|__Chore__ArchiveRollups|__Chore__RunOnce|__ContainmentSyncChore__syncContainedStatus|__Client__AllPayments|__Chore__attemptBillingFreezeWarn|__Service__InsertOfflineNodeEvents|__Service__DQNodesLastSeenBefore|__Chore__runDeleteProjects|__Chore__sendVerificationReminders"}[7d]))
# Q2: hours since the last success
(time() - max by (environment_name, name) (tlast_change_over_time(function{field="successes",app="satellite-core",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",scope=~"storj_io_storj_satellite_(accounting_tally|accounting_rollup|accounting_rolluparchive|accounting_projectbwcleanup|audit|accountfreeze|overlay|payments_storjscan|console_dbcleanup_pendingdelete|console_emailreminders|nodeevents)",name=~"__Service__Tally|__Service__Rollup|__Chore__ArchiveRollups|__Chore__RunOnce|__ContainmentSyncChore__syncContainedStatus|__Chore__attemptBillingFreezeWarn|__TrialFreezeChore__attemptTrialExpirationFreeze|__Service__InsertOfflineNodeEvents|__Service__DQNodesLastSeenBefore|__Client__AllPayments|__Chore__runDeleteProjects|__Chore__sendVerificationReminders|__Chore__process"}[7d]))) / 3600
# Q3: rollup archive outcome per day (error_name only on field="count")
sum by (environment_name, field, error_name) (increase(function{field=~"successes|failures|count",app="satellite-core",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",scope="storj_io_storj_satellite_accounting_rolluparchive",name="__Chore__ArchiveRollups"}[1d]))
# Q4: last tally / rollup duration (s)
max by (environment_name, name) (function_times{field="recent",kind="success",app="satellite-core",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",scope=~"storj_io_storj_satellite_accounting_(tally|rollup)",name=~"__Service__Tally|__Service__Rollup|__BucketTallyCollector__Run|__Service__Purge"})
# Q5: node-event processing and Customer.io sends, 7d
sum by (environment_name, name, field) (increase(function{field=~"successes|failures",app="satellite-core",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",scope="storj_io_storj_satellite_nodeevents"}[7d]))
# Q6: chores whose Run already returned (disabled at start)
sum by (environment_name, scope) (function{field="successes",app="satellite-core",kubernetes_namespace="satellite",environment_name=~"storj-prod-satellite-(us1|eu1|ap1|slc)",name=~"__Chore__Run|__Service__Run|__BotFreezeChore__Run"})
```
Q2 "healthy" ceilings: tally < interval + run time (us1 ≲ 9h, slc ≲ 3.5h); 24h chores < 25h; storjscan, node events < 0.1h; containment sync < 2h; stray nodes < 168h.

### Baselines (2026-10-12; core pods started 2026-10-01 ~20:55 UTC, slc 13:17; slc still on v1.163.6, others v1.164.1)
- **Tally**: us1 ~6,750 s (1.9 h) per run, eu1 ~340 s, ap1 ~44 s, slc < 1 s. 30d: us1 93 ok / **10 failed**, others 0 failed.
  The one us1 failure in the log window (2026-10-05 23:49) was `[mysql] packets.go:58 unexpected EOF` on stderr — a dropped TiDB connection.
- **Rollup** 80–130 s, 1/day, 0 failures. Project bw cleanup, pending delete, freeze, reminders: 1/day, 0 failures.
- **Storjscan** `AllPayments` ~1/min, ~0.3% failures (us1 36, eu1 33, ap1 25 per 7d). The 100% outage of 2026-09-25..28 is over.
- **Node events**: ~8k `process` and ~4.8k Customer.io `Notify` per week per region, 0 failures.
- `repair_queue` attempted=false: us1 ~19.3M, ap1 ~672k, eu1 ~7k, slc 0.
- Memory peak 7d 13–29% of limit (slc 60%), CPU throttling 1–6%, 0 restarts.

## Silent failures
- **Every chore's `Run` swallows errors** (logs, `return nil`). `__X__Run` only ends at shutdown — never alert on it.
- **Tally**: `Tally` errors are counted, but the `Run` wrapper only logs "tally failed" + `mon.Event("bucket_tally_error")`
  (Events never reach Prometheus). Failed live-accounting (Redis) updates inside a successful tally are only logged.
- **Rollup archive (us1)**: `__Chore__ArchiveRollups` has failed **every day since at least 2026-09-02** (start of metric
  history) in ~0.1 s, `rollups_archived=0`, at `StoragenodeAccounting.ArchiveRollupsBefore` (node rollups, so bucket rollups
  are never reached either). The error text is not in the log (only the stack). Its alert `centohw6vde68f` uses a 4h window,
  so it fires ~4h each evening and resolves — easy to mistake for noise. Old `storagenode_bandwidth_rollups` keep growing in us1.
- **Storjscan**: only `Client.AllPayments` errors are counted; DB errors ("error storing payments to db", "error retrieving last
  payment") are log-only.
- **Billing chore**: no metric at all for its work. Only logs `billing:chore` "unable to get new billing transactions",
  "error storing transaction to db", "error upgrading user", "error paying invoices".
- **Freeze chores**: `attempt*` functions return nothing, so their `failures` is **always 0** — the "billing freeze chore
  failures" alert (`dfzpa57r9mmf4f`) can never fire. Use logs (`accountfreeze:chore` errors) and the "not running" rule.
- **Node events**: a batch with an invalid email is marked sent and dropped ("invalid email", ~1.2k/week per region — normal).
  A `Notify` failure ends the cycle and retries after 5m; the backlog has no metric.
- **Repair queue stat** reports **0 for every placement** when its stat query fails ("couldn't get repair queue statistic").
- **Stray nodes DQ bursts after every core restart are normal.** The chore runs at pod start and then every 168h, and
  DQs nodes with no successful contact for `stray-nodes.max-duration-without-contact` (720h); reason
  `disqualification_reason = 3`. It loops while a batch equals `limit` (1000), so one run can DQ more than 1000. A deploy
  moves the weekly schedule, so a run can catch several extra days of old offline nodes. Baseline us1 2026-08..10:
  25–527 per run; 2026-10-20 (v1.165.3 restart) us1 1048, eu1 752, ap1 684, slc 294 — operator fleets that went silent on
  2026-09-20. Errors are logged ("error disqualifying stray nodes") and return nil. Log: `Disqualified dq_type="stray node"`.
  Before calling a burst a bug, check `last_contact_success` of the DQed nodes in the satellite DB.
- **Offline node events** (`InsertOfflineNodeEvents`, hourly at :56) log `node event inserted name=offline` with node ID and
  operator email — the fastest list of "which nodes went offline when". There is **no log for a node coming back**
  (successful check-ins are not logged); use `nodes.last_contact_success` in the satellite DB.
- **`enabled` flags that do nothing in the modular build**: `account-freeze.enabled`, `console-db-cleanup.enabled`,
  `rollup-archive.enabled`. To stop such a chore you must use `--components=!pkg.Type`.

## Logs (ClickHouse `otel.otel_logs`, pod `satellite-core-%`)
Core ships **info + warn + error**. The error text is often **missing**: lines have `exception.stacktrace=` but no
`exception.message`; a DB driver cause may be a separate stderr row (`[mysql] ...`) at the same timestamp — fetch all rows
±2 s without a level filter. Info lines contain node operator emails and user ids — do not copy them.
Normal error noise per week: `nodeevents:chore` "invalid email" ~1.2k/region; `accountfreeze:chore` "Ignoring invoice; account
already deleted" (us1 ~1.5k), "Could not attempt payment" (~20–90), `stripe:client` "Invoice is already paid"; `storjscan:chore`
"error retrieving payments" (25–36).
Key lines: `tally:service` "tally failed" / "tally purge failed" / info "Purged old bucket storage tallies"; `rollup` "rollup failed";
`rolluparchive:chore` "archiving node bandwidth rollups"; `nodeevents:chore` "process"; `audit:containmentsyncchore` info
"updated containment status of all nodes as necessary" (us1 ~5.9k contained nodes).

```sql
-- top core errors/warnings, 7d (all regions)
WITH splitByChar('\t', Body) AS p
SELECT concat(toString(count()), ' | ', ResourceAttributes['environment_name'], ' | ', p[2], ' | ', p[3], ' | ', substring(p[4], 1, 110))
FROM otel.otel_logs
WHERE Timestamp > now() - INTERVAL 7 DAY
  AND `__otel_materialized_k8s.namespace.name` = 'satellite' AND `__otel_materialized_k8s.container.name` = 'satellite'
  AND `__otel_materialized_k8s.pod.name` LIKE 'satellite-core-%' AND p[2] IN ('error','warn','fatal')
GROUP BY ResourceAttributes['environment_name'], p[2], p[3], p[4] ORDER BY count() DESC LIMIT 60
```

## Did the accounting output land? (satellite Postgres via ClickHouse federation, Grafana datasource `cfqvdb4b1y96oa`)
```sql
SELECT concat(toString(toDate(start_time)), ' | rows=', toString(count()), ' | at_rest_rows=', toString(countIf(at_rest_total > 0)))
FROM postgresql(us1_satellitedb, table='accounting_rollups')
WHERE start_time >= now() - INTERVAL 14 DAY GROUP BY toDate(start_time) ORDER BY toDate(start_time)
```
(Works with `eu1_`/`ap1_`/`slc_satellitedb`.) A day with rows but `at_rest_rows=0` = node tally (ranged-loop) wrote nothing —
past us1 cases: 2026-07-09/10/22/23, 08-12; none in 2026-09-29..10-10. **The row-count tell changed on 2026-10-02**: v1.164
(`nodetally: skip nodes which left the network`) cut rows from ~79k → ~33.7k (us1), ~70k → ~30.3k (eu1), ~63.5k → ~30k (ap1);
slc ~35.2k will drop when it gets v1.164. Compare against these, not the old ~79k.

Eventkit `storage_tally`, `storage_rollup`, `bandwidth_rollup` (Body = event name in `eventkit.otel_logs`) arrive from **all 4**
regions as of 2026-10-12 — the old "eu1 core emits no eventkit" note is no longer true.

## Per-region differences
| | us1 | eu1 | ap1 | slc |
|---|---|---|---|---|
| version (live) | v1.164.1 | v1.164.1 | v1.164.1 | v1.163.6 (git says v1.164.1) |
| tally interval / list-limit | 7h / **500** | 7h / 2500 | 7h / 2500 | **3h** / 2500 |
| tally run | ~1.9 h | ~6 min | ~45 s | < 1 s |
| DB conns (`db.max_open_conns`) | 30 | default | default | default |
| storjscan + billing loops | on | on | on | off (defaults) |
| billing freeze chore | on | on | on | **excluded** (`--components`) |
| pending delete cleanup | on | on | on | off |
| rollup archive | **failing daily** | ok | ok | ok |
| memory limit | 512 Mi | 768 Mi | 1 Gi | 256 Mi |

## Config that matters
`tally.interval`, `tally.list-limit` (buckets per `CollectBucketTallies` chunk), `tally.as-of-system-interval` (-5m),
`rollup-archive.archive-age` (2160h), `repair-queue-check.interval`, `node-events.send-node-emails`,
`payments.storjscan.disable-loop`, `payments.billing-config.disable-loop`, `pending-delete-cleanup.*`. Values: `helm/satellites/<r>/satellite-core.yaml` over `satellite.yaml`
(slc's `satellite.yaml` also has an unused `core:` block with `replicas: 0` — ignore it).

## Traps
- us1 tally is the usual victim of DB trouble: `CollectBucketTallies` chunks are bounded by bucket count, not object count, so one
  chunk can scan tens of millions of rows; long queries die when a TiDB backend behind **tiproxy** cycles (error 8146 in tiproxy,
  `invalid connection` / `unexpected EOF` in core). Slow tally with tiny TiKV process time = read-pool queueing, not the chunk bug.
- Phantom `db_stats` WaitCount/WaitDuration spikes are colliding series when several pools share a `db_name`; if
  `MaxOpenConnections` is the only flat field, the spikes are fake.
- Series are sparse (24h chores) — use `increase(...[7d])` or `tlast_change_over_time`, and zero-fill alert rules.
- Core alert rules (folder `fcd6h7`, group `Satellite`): core down `bfzpa1o9v6nlse`, tally not running `ffzpa54jkbxtsc`,
  tally failures `dfuejrdfzsg74f`, rollup not running `bfzpa54zso6psd`, archive `centohw6vde68f`, storjscan `cfzpa56sv0yyof` /
  `bfzpa579fupkwa`, freeze `dfzpa57r9mmf4f` (cannot fire) / `afzpa5884fyf4f`. The GC sender rules watch `satellite-gc-sender`, not core.

## Where to look next
- Repair queue numbers come from here but the queue is filled by ranged-loop and drained by repair: [repair.md](repair.md).
- Missing node at-rest / payouts → ranged-loop `_nodetally_Observer`: [ranged-loop.md](ranged-loop.md).
- GC bloom filter sending → `satellite-gc-sender` app, [gc-bf.md](gc-bf.md).
