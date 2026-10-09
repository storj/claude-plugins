---
name: satellite-infra
description: Use when you need the production deployment facts for a Storj satellite peer — which cluster/namespace/app it runs in, replica counts, the exact config flag values set in prod per region, the deployed version, or how a flag reaches the pod. Reads the storj/infra repo (helmfile + helm values).
---

# Satellite prod deployment (storj/infra)

The infra repo is the source of truth for the **intended** prod state. Default local path:
`~/git/storj/infra` (ask the user if it is not there). Read `helm/CLAUDE.md` in that repo only when
this page is not enough (e.g. chart internals, how deploys run).
It shows git state, not live state — confirm with read-only `kubectl get` when it matters.

## Layout

- Deploys use **Helmfile + Helm**. No ArgoCD, no kustomize. Run by Jenkins (`Jenkinsfile.satellite.v2`, `jenkins/satellite-deploy.groovy`).
- One helmfile per region: `helm/{us1,eu1,ap1,slc,qa1,nightly}.yaml.gotmpl`.
- Values per region and peer: `helm/satellites/<region>/satellite-<peer>.yaml`.
  Region base: `helm/satellites/<region>/satellite.yaml`. Shared: `helm/placement.yaml`, `helm/satellite-products-common.yaml`.
- Charts: `helm/charts/satellite-single` (most peers), `satellite`, `satellite-console`, `jobq`.
- Some parts are **bare metal via Ansible**, not k8s:
  us1 satellite-api on `cb_pa` hosts (`ansible/playbooks/roles/satellite-api/vars/us1.yaml`) and
  extra repairers (`ansible/playbooks/roles/repairer-docker/vars/{us1,eu1}.yaml`).

## Clusters (kube contexts)

| Region | Default context | Extra |
|---|---|---|
| us1 | `dp-lax-us1-rke2` | gc-bf + repair on `cb-ash-k3s`; repair on `rh-ewr-k3s`; repair on slc/ap1 clusters in namespace `satellite-us1` |
| eu1 | `dp-prg-eu1-rke2` | |
| ap1 | `dp-lax-ap1-rke2` | |
| slc | `dp-lax-slc-rke2` | qa1 shares this cluster (namespace `satellite-qa`) |

Namespace is `satellite` everywhere, except us1 repairers on other clusters (`satellite-us1`).

## Peers

Release name = Deployment name = `app` label, e.g. `app=satellite-repair`.
Image `ghcr.io/storj/satellite-modular:<tag>`; command `/app/satellite <sub> --config-dir=/root/.local/share/storj/satellite`.
The code for `<sub>` is `satellite/run/<sub>.go` in storj/storj (modular "mud" build — **not** `cmd/satellite` or `satellite/api.go`).

| app | `<sub>` | Notes |
|---|---|---|
| satellite-api | api | HPA (us1 5–20). us1 also bare metal. |
| satellite-console | console | HPA 3–9 |
| satellite-core | core | 1 replica, chores |
| satellite-repair | repair | us1 10, eu1 50, ap1 20, slc 4; plus extra us1 releases |
| satellite-ranged-loop | ranged-loop | 1 replica, `--components=...Observer` |
| satellite-auditor | auditor | us1 HPA 1–6 |
| satellite-gc-sender | gc-sender | 1 replica |
| satellite-gc-bf | gc-bf-once | CronJob (us1 `0 22 * * 1,3,6`) |
| satellite-admin | admin | |
| satellite-change-stream | change-stream | |

Replica numbers drift — re-check the values file before quoting them.

## Config: how a flag reaches the pod

- `config:` map in values → `config.yaml` in a ConfigMap (`helm/charts/satellite-single/templates/configmap.yaml`).
- `secrets:` map → `STORJ_*` env from a Secret (`ref+vault://`). Never print secret values.
- Files are layered in the region helmfile; later wins; maps merge. Example for us1 repair:
  `satellites/us1/satellite.yaml` then `satellites/us1/satellite-repair.yaml`.
- Not set in values = code default. Find the default in the Go `Config` struct (`help:"..." default:"..." releaseDefault:"..."` tags; prod uses `releaseDefault` when present).

Find a flag across regions:
```bash
rg -n '"repairer.max-repair"' ~/git/storj/infra/helm/satellites ~/git/storj/infra/ansible
```

## Version

- `image.tag` at the top of `helm/satellites/<region>/satellite.yaml`. Per-peer pins would be in `satellite-<peer>.yaml`.
- Map tag to code: `git -C ~/git/storj/storj log --oneline -1 <tag>`; diff two tags to see what a deploy changed.
- Read code **as deployed** without checking out: `git show <tag>:satellite/metainfo/batch.go`,
  `git grep -n <pattern> <tag> -- satellite/`, `git diff <tag> main -- <path>` (is `main` different?).
- Live check: `kubectl --context <ctx> -n satellite get deploy -o wide` (read-only).
- Recent infra changes (config changes are a common cause of incidents):
  `git -C ~/git/storj/infra log --since=7.days --oneline -- helm/satellites/<region>/`

## Observability config (where, not how)

- Prometheus per cluster: `helm/charts/prometheus-stack`, `helm/satellites/<region>/prometheus.yaml`.
- Logs: `helm/charts/otel-gateway` (namespace `otel`), `log.use-otel-only`, `eventkit.destination: otel` in region `satellite.yaml`.
- Tracing: `tracing.*` keys in region `satellite.yaml`; `helm/satellites/us1/jaeger.yaml`.
