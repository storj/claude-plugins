---
name: satellite-peers
description: Use when investigating, debugging or monitoring a specific production Storj satellite peer (api, repair, core, ranged-loop, auditor, gc-bf, gc-sender, admin, console, change-stream, jobq). Gives the peer card - what runs inside it, the verified PromQL health signals, silent-failure paths in the code, key config flags, and known traps.
---

# Satellite peer cards

Each prod satellite process is one `satellite <sub>` subcommand of the modular ("mud") build:
code entry `satellite/run/<sub>.go` in storj/storj, k8s `app="satellite-<sub>"`.

Read **only** the card(s) you need:

| Peer | app label | Card |
|---|---|---|
| api | `satellite-api` | [peers/api.md](peers/api.md) |
| repair | `satellite-repair`, `*-repair` | [peers/repair.md](peers/repair.md) |
| ranged-loop | `satellite-ranged-loop` | [peers/ranged-loop.md](peers/ranged-loop.md) |
| gc-bf (+ gc-sender handoff) | `satellite-gc-bf` | [peers/gc-bf.md](peers/gc-bf.md) |
| core | `satellite-core` | [peers/core.md](peers/core.md) |
| gc-sender | `satellite-gc-sender` | [peers/gc-sender.md](peers/gc-sender.md) |
| jobq (repair queue server) | `jobq` | [peers/jobq.md](peers/jobq.md) |
| auditor | `satellite-auditor` | [peers/auditor.md](peers/auditor.md) |
| admin, console, change-stream | `satellite-<name>` | not written yet — build one (below) |

Fleet-wide "are all peers healthy?" check: follow [health-sweep.md](health-sweep.md) (checklist + known noise).

Always also load `satellite-observability` (datasource, selectors, traps) and, for config, `satellite-infra`.

## When there is no card

Build the facts yourself, in this order, and keep notes in the card format below so the user can
turn them into a card:
1. `satellite/run/<sub>.go` → `GetSelector` → resolve each `mud.Select[*T]` to its package (`*/mud.go`).
2. `rg -n 'mon\.(Meter|Counter|IntVal|FloatVal|Event|Task)' <packages>` for metric names; check which exist:
   `count by (__name__) ({app="satellite-<sub>",kubernetes_namespace="satellite",environment_name="storj-prod-satellite-us1",scope=~"storj_io_storj_satellite_<pkg>.*"})`.
3. Look for `return nil` after `log.Error`/`log.Warn` in the main loops — those are silent failures.
4. Config: the `Config` struct `help:` tags + `rg` in infra values.

## Card format
1. Identity — subcommand, app labels, regions, replicas.
2. What runs inside — components, loop shape.
3. Key signals — table of question → verified PromQL; a dated baseline.
4. Silent failures — where errors are swallowed or not errors at all.
5. Distinctive logs — logger name + message (from code).
6. Config that matters — flags, defaults, where prod values live.
7. Traps / where to look next.

Every query in a card must have returned data in prod at least once. Put the verification date at the top.
