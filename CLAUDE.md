# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

## Project

**todo** — GitOps repo for todo

This is a `gitops-app` repo: the GitOps source of truth for the **todo** system. **It owns every Kubernetes manifest.** Component repos only build and publish a container image; this repo declares which components make up the system, how each one runs (port, probes, env wiring, replicas, resources, exposure) and which image version each environment (`dev`, `staging`, `prod`) uses. ArgoCD reconciles it. There is no application code here.

## Layout

```
applications/todo/
├── system.yaml                   the only hand-edited file: owner, environments, components, APIs, resources (reference: docs/DOMAIN-MODEL.md)
├── applicationset.yaml           GENERATED — one ApplicationSet per env
└── overlays/<env>/<component>/     GENERATED — deployment.yaml, service.yaml, [httproute.yaml], kustomization.yaml (image pin)
bootstrap/todo-root.yaml        GENERATED — root Argo Application (app-of-apps), applied once by a human
docs/specs/                       system specs (docs/specs/<NNN>-<slug>/spec.md)
docs/plan/                        multi-repo plans written by /system:plan (one per system spec)
```

Everything marked GENERATED is derived from `system.yaml` by `devbox run render` (existing image pins are preserved). Never hand-edit generated files; change `system.yaml` (by hand or with the commands below) and render. Argo only ever reads **this** repo.

## How a component is described

`/component:add <component>` writes a complete entry under `components:` using the shape defaults (port, probes, numeric user, writable volumes, resources) — every value explicit so it can be reviewed. Then adapt it:

- **Wiring between components** is configuration of the *system*, so it lives here: `env: {API_URL: http://todo-api}` (a Service is reachable by its name inside the environment's namespace).
- `replicas` and `resources` (per environment under `instances.<env>`), `secrets` (`generate: [KEY]` = ESO creates random values in the cluster; `remote: {keys: [KEY]}` = read from the secret store; add with `/component:add <component> --generate NAME=KEY` / `--secret NAME=KEY`, set local values with `/instance:secret`) and `secretRefs` (names of Secrets managed elsewhere). Never put secret values in this repo or in `env`. `uses: [postgres]` (after `/resource:add postgres`) injects `DATABASE_URL` and `PG*` from the CloudNativePG-generated Secret. An `observability:` block under an environment in `system.yaml` adds an OpenTelemetry collector there and wires every component to it (`OTEL_*`). `policy: audit` or `enforce` on an environment turns on Kyverno guard rails (ADR-022): `devbox run validate` checks every rendered workload, so a violation fails the PR; fix `system.yaml`, never the generated files.
- `expose: {host: web}` publishes the component through the Gateway at the `host` template of the environment (dev default: `<component>.todo-dev.localhost`, which resolves to 127.0.0.1 with no DNS setup).
- **External components** (spec 069, ADR-029): when the generator cannot express a component (CronJobs, volumes, a login proxy, a Helm chart with CRDs), keep its package here and register it as `external: {kustomize: external/<svc>}` (a kustomize package under `applications/todo/`) or `external: {helm: {repoURL, path, revision, imageTag}}` with `values`/`envValues`, plus `name`, `repo`, `image`, `environments` and an optional `namespace`. Render wires it into each environment and pins its image, so pin-dev, `/instance:promote` and `/system:status` treat it like any other component. Its manifests are hand-written: review them like code.
- `instances:` — a component runs only in the environments that are keys of its `instances`. New components start in `dev`; `/instance:promote` adds `staging`, then `prod`.

## One-time cluster bootstrap (human only)

Argo must be able to read this repo. Then, once per cluster, a human runs `KUBE_CONTEXT=<context> devbox run bootstrap` (`kubectl apply -n argocd -f bootstrap/`; the context must be named explicitly so it never hits whatever cluster happens to be current). That creates the root Application `todo-root`, which watches `applications/todo/applicationset.yaml` in git; from then on every change reaches the cluster through merged PRs. Agents never run `bootstrap` or any `kubectl apply`.

**To try the system on your machine:** `devbox run cluster-up` creates a k3d cluster `todo-local` (port 8088 → the Gateway), installs ArgoCD and External Secrets Operator and enables the Gateway API, gives Argo your `gh` token for this private repo, pre-creates `todo-{dev,staging,prod}` with a GHCR pull secret, applies the root Application and prints the URLs of exposed components. `devbox run cluster-down` removes it. Needs Docker running; the `gh` token needs `repo` and `read:packages`.

## Service-ref marker (spec 098)

A gitops PR opened by `/component:ship` carries one body line `Service-ref: <owner>/<repo>@<branch>`: the component repo and its ship branch. On pull requests CI then reads that branch's CRDs (`crds/` and untemplated CRDs in `templates/` of the service's chart) through the GitHub API and validates every Component under `applications/` against them, instead of the public CRD catalog. Without a marker nothing changes. A malformed or duplicate marker, a repo that is not in `system.yaml`, a missing secret or an unreadable repo fails the check; a branch that is gone falls back to the service's default branch. Edit the PR body to correct the marker; the check runs again. The secret `SERVICE_REPOS_READ_TOKEN` (fine-grained token, Contents: read on every service repo) is only used by the `service CRDs` CI step; `cplat platform doctor` warns when it is missing: `gh secret set SERVICE_REPOS_READ_TOKEN -R <this repo>`.

## Alert issues (spec 092)

`.github/workflows/alerts.yml` polls every 15 minutes and files an issue for each firing alert (`auto-filed`, `alert`, `env:<env>`, `triage`). It runs only on the self-hosted runner (the `CI_RUNNER` variable) and is skipped, with a note in the run name, when that variable is empty. It never runs on a GitHub-hosted runner, because the cluster's query API is not reachable from there. Try it without changing anything: `gh workflow run alerts.yml -f dry_run=true`.

- **Rules** go under `environments.<env>.observability` in `applications/<system>/system.yaml` (`rules: [{name, query, severity, summary}]`, severity `critical`, `warning` or `info`; `devbox run quality` validates them). The built-in `ServiceDown` rule covers every component. `alerts: false` switches the poll off for that environment. `queryURL`, `querySecret` and `alertmanagerURL` point at another Prometheus or Alertmanager.
- **Where issues land:** in the component's `repo` from `system.yaml`, else in this repo. Filing in the component repos needs the secret `ALERTS_TOKEN` (fine-grained, Issues: read and write on every component repo): `gh secret set ALERTS_TOKEN -R <this repo>`; `cplat platform doctor` warns when it is missing. <!-- pragma: allowlist secret -->
- **Settings:** the secret `ALERTS_QUERY_AUTHORIZATION` (sent as the `Authorization` header to the query API) and the variable `ALERTS_PROMETHEUS_URL` (default: the in-cluster lgtm Prometheus).
- Every alert issue goes through `/issue:triage` like any other auto-filed issue. Run `/platform:update` to get the workflow in an existing repo.

## Pin policy

- `dev`: follows `main`. Each merge to a component's main opens a `pin/dev-<component>-<sha7>` PR here (`.github/workflows/pin-dev.yml`, ADR-027) that runs CI on the pin branch and merges itself only when it passed (no branch protection needed, ADR-028); a red CI leaves it open with a comment and dev on the old image. Until a component's first pin its dev overlay uses the `latest` tag. Pin by hand: `devbox run pin -- dev <component> sha-<7>`.
- `staging`: image tag `sha-<40>` (full commit sha, ADR-030) of a main build, set by `/instance:promote`, and after a green dev smoke by the self-merging `pin-staging.yml` PR (spec 093, ADR-041) when `environments.staging.promote` is `after-dev-smoke` in `system.yaml`. Switch it off with `promote: manual` there, or per component under `instances.staging`.
- `prod`: release image tag `X.Y.Z` (the git tag `vX.Y.Z` without the `v`), set by `/instance:promote` only, never automatically (the command shows an evidence summary first).

## Commands (devbox)

| Script | Purpose |
|---|---|
| `render` | Regenerate applicationsets, overlays and manifests from `system.yaml` |
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

Plugins enabled in `.claude/settings.json`: `system` (system specs and planning), `component` (add, set, remove, build), `instance` (promote, secrets, verify), `resource`, `api`, `issue`, `platform` (update, doctor), `svc` (product-manager, architect agents) and `shared` (quality/security agents, the `cplat` launcher).

| Task | Command |
|---|---|
| Add components to the system | `/component:add <component...> [--expose] [--env KEY=VALUE]` |
| Remove components | `/component:remove <component...>` |
| Declare the database | `/resource:add postgres` |
| Promote versions | `/instance:promote <component...> --to <env> [--version vX.Y.Z]` |
| Local secret values | `/instance:secret set\|list` |
| Write a system spec (first step) | `/system:spec <description>` |
| Split it across the repos | `/system:plan <NNN>` |
| Build every repo, unattended | `/system:ship <NNN>` |
| Specs and plan progress | `/system:spec [--all\|show\|done\|abandon]` |
| Overview of the running system | `/system:status` |
| Update this skeleton | `/platform:update` |

### Spec first

A feature starts with a spec, not code ([ADR-026](https://github.com/ika100/sdlc-foundry/blob/main/docs/adr/026-feature-specs.md)): `/system:spec <description>` writes the system spec in `docs/specs/<NNN>-<slug>/spec.md` (it asks you its open questions and your approval); `/system:plan <NNN>` assigns every criterion to a component repo with the contract between them (`docs/plan/<spec_id>.md`, checked by `devbox run plan-check`); `/system:ship <NNN>` then takes every repo through its own slice of the spec (`/component:spec --from-plan` → `/component:plan` → `/component:build` → `/component:ship`) in plan order. Each repo's PR lists the criteria it meets. `devbox run spec-check` validates the specs here (also the warn-only `specs` CI workflow).

## Where your additions go

Updates (`/platform:update`) overwrite the skeleton files below. Put your own work next to them, where an update keeps it:

| Updates overwrite | Your additions go in |
|---|---|
| the template's recipes in `devbox.json` (including `ci`) | gates: the `ci-extra` recipe (`devbox run ci` and the `ci-extra` CI check run it); other tools: recipes of your own in `devbox.json` (kept) |
| `.github/workflows/ci.yml` | a workflow file of your own in `.github/workflows/` |
| `.claude/settings.json` | your own permission entries (allow, ask, deny) in it; they are kept |
| `CLAUDE.md` | `.claude/project-notes.md` |
| other skeleton files (Dockerfile, lint config ...) | nowhere: propose the change to the platform (`/issue:report`) |

## Conventions

- Branches: `compose/<slug>`, `promote/<slug>`, `feature/<slug>`, `chore/<slug>`. Never commit to `main` directly.
- Conventional Commits for commit messages and PR titles.

## Project notes

Project-specific guidance for Claude Code goes in `.claude/project-notes.md`, which is loaded below and never changed by updates.

@.claude/project-notes.md
