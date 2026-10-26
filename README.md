# todo

A shared todo list in the browser: add todos, edit their titles, mark them done or not done, and delete them. There is one list for everyone and no sign-in, and the todos are kept in Postgres, so they survive restarts and redeploys.

This repo is the GitOps source of truth for the **todo** system (platform shape `gitops-app`). It declares the components, how each one runs, and which image version each environment (`dev`, `staging`, `prod`) uses. ArgoCD deploys what is merged here. See `CLAUDE.md` for layout, pin policy and commands.

## The system

| Component | Type | Repo | Role |
|---|---|---|---|
| `todo-web` | website (Next.js) | [ika100/todo-web](https://github.com/ika100/todo-web) | The todo list page; the only component published through the Gateway |
| `todo-api` | service (Python, FastAPI) | [ika100/todo-api](https://github.com/ika100/todo-api) | Stores and changes todos; provides the `todo-api-rest` API (OpenAPI) |
| `postgres` | database resource | (CloudNativePG, declared here) | Keeps the todos; used by `todo-api` |

```
browser ──► todo-web ──todo-api-rest──► todo-api ──► postgres
```

`todo-web` reaches `todo-api` inside the cluster at `http://todo-api` (`TODO_API_URL`); `todo-api` is not published outside the cluster. In dev, the list is at http://todo-web.todo-dev.localhost:8088/ on the local cluster.

Everything above is declared in [`applications/todo/system.yaml`](applications/todo/system.yaml), the only hand-edited file of the system. Features are described as specs in [`docs/specs/`](docs/specs/): 001 (todo list) and 002 (edit a todo's title).

## Commands

```bash
devbox shell
devbox run cluster-up              # local k3d cluster + ArgoCD + credentials + root Application
# real cluster, by a human: KUBE_CONTEXT=<ctx> devbox run bootstrap
devbox run render                  # regenerate the manifests from system.yaml
devbox run quality                 # validate overlays + ApplicationSets
```

In Claude Code: `/component:add <component>` to add a component, `/instance:promote <component> --to staging` to promote, `/system:status` for an overview, and `/system:spec <description>` to start a feature.

Updates to this skeleton: `/platform:update`.
