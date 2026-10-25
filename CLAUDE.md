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
- **External services** (spec 069, ADR-029): when the generator cannot express a service (CronJobs, volumes, a login proxy, a Helm chart with CRDs), keep its package here and register it as `external: {kustomize: external/<svc>}` (a kustomize package under `applications/todo/`) or `external: {helm: {repoURL, path, revision, imageTag}}` with `values`/`envValues`, plus `name`, `repo`, `image`, `environments` and an optional `namespace`. Render wires it into each environment and pins its image, so pin-dev, `/gitops:promote` and `/shared:status` treat it like any other service. Its manifests are hand-written: review them like code.
- `environments: [dev]` — a service is rendered only for the listed environments. New services start in `dev`; `/gitops:promote` adds `staging`, then `prod`.

## One-time cluster bootstrap (human only)

Argo must be able to read this repo. Then, once per cluster, a human runs `KUBE_CONTEXT=<context> devbox run bootstrap` (`kubectl apply -n argocd -f bootstrap/`; the context must be named explicitly so it never hits whatever cluster happens to be current). That creates the root Application `todo-root`, which watches `applications/todo/applicationset.yaml` in git; from then on every change reaches the cluster through merged PRs. Agents never run `bootstrap` or any `kubectl apply`.

**To try the product on your machine:** `devbox run cluster-up` creates a k3d cluster `todo-local` (port 8088 → the Gateway), installs ArgoCD and External Secrets Operator and enables the Gateway API, gives Argo your `gh` token for this private repo, pre-creates `todo-{dev,staging,prod}` with a GHCR pull secret, applies the root Application and prints the URLs of exposed services. `devbox run cluster-down` removes it. Needs Docker running; the `gh` token needs `repo` and `read:packages`.

## Service-ref marker (spec 098)

A gitops PR opened by `/svc:ship` carries one body line `Service-ref: <owner>/<repo>@<branch>`: the service repo and its ship branch. On pull requests CI then reads that branch's CRDs (`crds/` and untemplated CRDs in `templates/` of the service's chart) through the GitHub API and validates every Component under `applications/` against them, instead of the public CRD catalog. Without a marker nothing changes. A malformed or duplicate marker, a repo that is not in `services.yaml`, a missing secret or an unreadable repo fails the check; a branch that is gone falls back to the service's default branch. Edit the PR body to correct the marker; the check runs again. The secret `SERVICE_REPOS_READ_TOKEN` (fine-grained token, Contents: read on every service repo) is only used by the `service CRDs` CI step; `cplat doctor` warns when it is missing: `gh secret set SERVICE_REPOS_READ_TOKEN -R <this repo>`.

## Alert issues (spec 092)

`.github/workflows/alerts.yml` polls every 15 minutes and files an issue for each firing alert (`auto-filed`, `alert`, `env:<env>`, `triage`). It runs only on the self-hosted runner (the `CI_RUNNER` variable) and is skipped, with a note in the run name, when that variable is empty. It never runs on a GitHub-hosted runner, because the cluster's query API is not reachable from there. Try it without changing anything: `gh workflow run alerts.yml -f dry_run=true`.

- **Rules** go under the `observability` addon in `applications/<app>/app.yaml` (`rules: [{name, query, severity, summary}]`, severity `critical`, `warning` or `info`; `devbox run quality` validates them). The built-in `ServiceDown` rule covers every service. `alerts: false` switches the poll off for the application. `queryURL`, `querySecret` and `alertmanagerURL` point at another Prometheus or Alertmanager.
- **Where issues land:** in the service's `repo` from `services.yaml`, else in this repo. Filing in the service repos needs the secret `ALERTS_TOKEN` (fine-grained, Issues: read and write on every service repo): `gh secret set ALERTS_TOKEN -R <this repo>`; `cplat doctor` warns when it is missing. <!-- pragma: allowlist secret -->
- **Settings:** the secret `ALERTS_QUERY_AUTHORIZATION` (sent as the `Authorization` header to the query API) and the variable `ALERTS_PROMETHEUS_URL` (default: the in-cluster lgtm Prometheus).
- Every alert issue goes through `/shared:triage` like any other auto-filed issue. Run `/shared:update-service` to get the workflow in an existing repo.

## Pin policy

- `dev`: follows `main`. Each merge to a service's main opens a `pin/dev-<service>-<sha7>` PR here (`.github/workflows/pin-dev.yml`, ADR-027) that runs CI on the pin branch and merges itself only when it passed (no branch protection needed, ADR-028); a red CI leaves it open with a comment and dev on the old image. Until a service's first pin its dev overlay uses the `latest` tag. Pin by hand: `devbox run pin -- dev <service> sha-<7>`.
- `staging`: image tag `sha-<40>` (full commit sha, ADR-030) of a main build, set by `/gitops:promote`, and after a green dev smoke by the self-merging `pin-staging.yml` PR (spec 093, ADR-041) when `promotion.staging` is `auto` in `app.yaml`. Switch it off with `promotion: {staging: manual}` in `app.yaml` or on a service in `services.yaml`.
- `prod`: release image tag `X.Y.Z` (the git tag `vX.Y.Z` without the `v`), set by `/gitops:promote` only, never automatically (the command shows an evidence summary first).

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
| `secrets-baseline` | refresh `.secrets.baseline.json` with fixed flags (never run `detect-secrets scan` by hand) |
| `service-crds` | spec 098: reads the PR body's `Service-ref` marker and validates the Components under `applications/` against the CRDs of that service branch (a no-op without a marker) |
| `ci` | `service-crds` + `validate` + `security`: the gates CI runs, then `ci-extra` when your repo defines it; run it before every push |

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

## Where your additions go

Updates (`/shared:update-service`) overwrite the skeleton files below. Put your own work next to them, where an update keeps it:

| Updates overwrite | Your additions go in |
|---|---|
| the template's recipes in `devbox.json` (including `ci`) | gates: the `ci-extra` recipe (`devbox run ci` and the `ci-extra` CI check run it); other tools: recipes of your own in `devbox.json` (kept) |
| `.github/workflows/ci.yml` | a workflow file of your own in `.github/workflows/` |
| `.claude/settings.json` | your own permission entries (allow, ask, deny) in it; they are kept |
| `CLAUDE.md` | `.claude/project-notes.md` |
| other skeleton files (Dockerfile, lint config ...) | nowhere: propose the change to the platform (`/shared:report-issue`) |

## Conventions

- Branches: `compose/<slug>`, `promote/<slug>`, `feature/<slug>`, `chore/<slug>`. Never commit to `main` directly.
- Conventional Commits for commit messages and PR titles.

## Project notes

Project-specific guidance for Claude Code goes in `.claude/project-notes.md`, which is loaded below and never changed by updates.

@.claude/project-notes.md
