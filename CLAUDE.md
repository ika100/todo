# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

## Project

**todo** — GitOps repo for todo

This is a `gitops-app` repo: the GitOps source of truth for the **todo** application. **It owns every Kubernetes manifest.** Service repos only build and publish a container image; this repo declares which services make up the product, how each one runs (port, probes, env wiring, replicas, resources, exposure) and which image version each environment (`dev`, `staging`, `prod`) uses. ArgoCD reconciles it. There is no application code here.

## Layout

```
applications/todo/
├── services.yaml                 human-edited registry: one entry per service (schema in the file header)
├── app.yaml                      gateway + hostname templates per environment (exposure)
├── applicationset.yaml           GENERATED — one ApplicationSet per env
└── overlays/<env>/<service>/     GENERATED — deployment.yaml, service.yaml, [httproute.yaml], kustomization.yaml (image pin)
bootstrap/todo-root.yaml        GENERATED — root Argo Application (app-of-apps), applied once by a human
docs/specs/                       product specs (docs/specs/<NNN>-<slug>/spec.md)
docs/plan/                        multi-repo plans written by /app:plan (one per product spec)
```

Everything marked GENERATED is derived from `services.yaml` + `app.yaml` by `devbox run render` (existing image pins are preserved). Never hand-edit generated files; change `services.yaml` (by hand or with the commands below) and render. Argo only ever reads **this** repo.

## How a service is described

`/gitops:compose add <service>` writes a complete entry using the shape defaults (port, probes, numeric user, writable volumes, resources) — every value explicit so it can be reviewed. Then adapt it:

- **Wiring between services** is configuration of the *product*, so it lives here: `env: {API_URL: http://todo-api}` (a Service is reachable by its name inside the environment's namespace).
- `replicas` (a number or per environment), `resources`, `secrets` (`generate: [KEY]` = ESO creates random values in the cluster; `remote: {keys: [KEY]}` = read from the secret store; add with `/gitops:compose add <svc> --generate NAME=KEY` / `--secret NAME=KEY`, set local values with `/gitops:secret`) and `secretRefs` (names of Secrets managed elsewhere). Never put secret values in this repo or in `env`. `uses: [postgres]` (after `/gitops:addon add postgres`) injects `DATABASE_URL` and `PG*` from the CloudNativePG-generated Secret. `/gitops:addon add observability [--ui lgtm | --export-to URL]` adds an OpenTelemetry collector per environment and wires every service to it (`OTEL_*`). `policies: {}` in app.yaml turns on Kyverno guard rails (ADR-022): `devbox run validate` checks every rendered workload, so a violation fails the PR; fix `services.yaml`, never the generated files.
- `expose: {host: web}` publishes the service through the Gateway at the hostname template in `app.yaml` (dev default: `<service>.todo-dev.localhost`, which resolves to 127.0.0.1 with no DNS setup).
- `environments: [dev]` — a service is rendered only for the listed environments. New services start in `dev`; `/gitops:promote` adds `staging`, then `prod`.

## One-time cluster bootstrap (human only)

Argo must be able to read this repo. Then, once per cluster, a human runs `KUBE_CONTEXT=<context> devbox run bootstrap` (`kubectl apply -n argocd -f bootstrap/`; the context must be named explicitly so it never hits whatever cluster happens to be current). That creates the root Application `todo-root`, which watches `applications/todo/applicationset.yaml` in git; from then on every change reaches the cluster through merged PRs. Agents never run `bootstrap` or any `kubectl apply`.

**To try the product on your machine:** `devbox run cluster-up` creates a k3d cluster `todo-local` (port 8088 → the Gateway), installs ArgoCD and External Secrets Operator and enables the Gateway API, gives Argo your `gh` token for this private repo, pre-creates `todo-{dev,staging,prod}` with a GHCR pull secret, applies the root Application and prints the URLs of exposed services. `devbox run cluster-down` removes it. Needs Docker running; the `gh` token needs `repo` and `read:packages`.

## Pin policy

- `dev`: follows `main`. Each merge to a service's main opens a `pin/dev-<service>-<sha7>` PR here (`.github/workflows/pin-dev.yml`, ADR-027) that merges itself after CI; a red CI leaves it open and dev on the old image. Until a service's first pin its dev overlay uses the `latest` tag. Pin by hand: `devbox run pin -- dev <service> sha-<7>`.
- `staging`: image tag `sha-<7>` of a main build, set by `/gitops:promote`.
- `prod`: release image tag `X.Y.Z` (the git tag `vX.Y.Z` without the `v`), set by `/gitops:promote`.

## Commands (devbox)

| Script | Purpose |
|---|---|
| `render` | Regenerate applicationsets, overlays and manifests from `services.yaml` / `app.yaml` |
| `render-check` | Fail if generated files are stale |
| `plan-check` | Validate multi-repo plans in `docs/plan/` (ADR-011) |
| `validate` | render-check + plan-check + label check + `kustomize build` every overlay + `kubeconform` (Argo/Gateway CRD schemas) — fully offline |
| `lint` / `quality` / `test` / `deploy-check` | aliases of `validate` |
| `lint-fix` | alias of `render` |
| `bootstrap` | **human, once per cluster**: `KUBE_CONTEXT=<ctx> devbox run bootstrap` applies the root Application |
| `cluster-up` / `cluster-down` | local k3d cluster with ArgoCD, Gateway API and credentials |
| `security` | detect-secrets |

**Rule for every agent: never call `kustomize`, `kubeconform`, `kubectl`, or `detect-secrets` directly. Always `devbox run <script>`. Never `kubectl apply` — Argo owns reconciliation.**

## Claude Code agents

Plugins enabled in `.claude/settings.json`: `gitops` (compose, promote), `app` (multi-repo planning), `svc` (product-manager, architect), `shared` (quality/security).

| Task | Command |
|---|---|
| Add services to the app | `/gitops:compose add <service...> [--expose] [--env KEY=VALUE]` |
| Remove services | `/gitops:compose remove <service...>` |
| Promote versions | `/gitops:promote <service...> <from> <to> [version]` |
| Write a product spec (first step) | `/app:spec <description>` |
| Split it across the repos | `/app:plan <NNN>` |
| Build every ready repo in parallel | `/app:build <NNN>` |
| Specs and plan progress | `/app:specs [show\|done\|abandon]` |

### Spec first

A feature starts with a spec, not code ([ADR-026](https://github.com/ika100/sdlc-foundry/blob/main/docs/adr/026-feature-specs.md)): `/app:spec <description>` writes the product spec in `docs/specs/<NNN>-<slug>/spec.md` (it asks you its open questions and your approval); `/app:plan <NNN>` assigns every criterion to a component repo with the contract between them (`docs/plan/<spec_id>.md`, checked by `devbox run plan-check`); `/app:build <NNN>` builds every ready repo in parallel, each from its own slice of the spec (`/svc:spec --from-plan` → `/svc:plan` → `/svc:build`), and stops at open PRs. Each repo's PR lists the criteria it meets. `devbox run spec-check` validates the specs here (also the warn-only `specs` CI workflow).

## Conventions

- Branches: `compose/<slug>`, `promote/<slug>`, `feature/<slug>`, `chore/<slug>`. Never commit to `main` directly.
- Conventional Commits for commit messages and PR titles.
