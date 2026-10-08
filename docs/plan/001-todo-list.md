---
plan_id: 001-todo-list
spec: 001-todo-list
feature: One shared todo list in the browser (todo-web), stored by todo-api in the product's Postgres
gitops_app: ika100/todo
status: in_progress
repos:
- id: todo-api
  shape: service-python
  summary: Todo REST API (list, create, mark done/not done, delete) persisted in Postgres
  acs:
  - AC-001.3
  - AC-001.5
  - AC-001.6
  - AC-001.7
  - AC-001.8
  - AC-001.9
  - AC-001.10
  - AC-001.11
  - AC-001.13
  - AC-001.14
  arguments: |
    Connection comes only from DATABASE_URL (CloudNativePG `uri`, scheme `postgresql://`), injected by the postgres
    addon; PG* variables are also present. Add a Postgres driver (psycopg 3) through the repo's devbox/uv workflow;
    if an ORM needs a dialect scheme (`postgresql+psycopg://`), rewrite it in code, never in gitops.
    Create the `todos` table idempotently on startup (or with a migration run at startup); no manual DB steps.
    `/ready` must return 503 while the database is unreachable; `/health` stays DB-independent.
    Acceptance tests run against a real Postgres (testcontainers or a devbox-provided server), not SQLite.
    Replace FastAPI's default 422 body with the error format of the contract. Document DATABASE_URL in docs/env-vars.md.
  depends_on: []
  done: false
- id: todo-web
  shape: web-nextjs
  summary: Todo list page at / (show, add, toggle, delete todos) backed by todo-api
  acs:
  - AC-001.1
  - AC-001.2
  - AC-001.3
  - AC-001.4
  - AC-001.5
  - AC-001.6
  - AC-001.7
  - AC-001.8
  - AC-001.9
  - AC-001.10
  - AC-001.11
  - AC-001.12
  - AC-001.13
  arguments: |
    todo-api is not published: the browser never calls it. All calls go server-side (Server Components for the
    initial list, Server Actions or app/api route handlers for mutations) to TODO_API_URL (server-only env var,
    never NEXT_PUBLIC_*; deployed value http://todo-api). Every call to todo-api uses a 5 s timeout.
    The todo list replaces the template landing page at `/`; keep /api/health, /api/ready, /api/metrics.
    Document TODO_API_URL in docs/env-vars.md. Test against a mocked todo-api that follows the contract.
  depends_on: []
  done: false
- id: todo
  shape: gitops-app
  summary: Publish todo-web, wire it to todo-api, add the postgres addon for todo-api
  acs:
  - AC-001.1
  - AC-001.14
  arguments: |
    In services.yaml: todo-web gets `expose: {host: todo-web}` and `env: {TODO_API_URL: http://todo-api}` (keep
    NODE_ENV); todo-api gets `uses: [postgres]`. `/gitops:addon add postgres` with version 17, 1 instance and 1Gi
    storage in dev. Do not add staging/prod to either service (promotion is a non-goal). Then `devbox run render`
    and `devbox run validate`.
  depends_on: []
  done: false
gitops_pin: []
---

## Decomposition

Three repos take part, all buildable in parallel because the contract below fixes the only code-level interface
(todo-web -> todo-api over HTTP inside the namespace). No shared library is needed: the interface is a small JSON
API with one resource, and the two consumers are in different languages (Python, TypeScript). No new repo is needed.

## todo-api — Todo REST API persisted in Postgres

**Shape:** `service-python` · **Criteria:** AC-001.3, AC-001.5, AC-001.6, AC-001.7, AC-001.8, AC-001.9, AC-001.10, AC-001.11, AC-001.13, AC-001.14 · **Depends on:** []

todo-api is the single source of truth for todos. It owns the server side of each criterion:

- AC-001.3: `GET /todos` returns every todo oldest first (`created_at` ascending, `id` as tie-breaker).
- AC-001.5 / AC-001.6 / AC-001.7: validation and trimming are enforced here, whatever the client sends: trim
  leading/trailing whitespace, then reject empty (`title_required`) or longer than 200 characters
  (`title_too_long`); exactly 200 characters after trimming is accepted. Length is counted in Unicode code points.
- AC-001.8 / AC-001.9: `PATCH /todos/{id}` sets `done` and persists it.
- AC-001.10 / AC-001.11: `DELETE /todos/{id}` removes the row; an unknown id on PATCH or DELETE answers 404
  `todo_not_found`, which is what lets the web show "This todo no longer exists".
- AC-001.13: there is no user scoping: one list for every caller.
- AC-001.14: todos live in the addon's Postgres (`DATABASE_URL`), never in process memory or the container
  filesystem (which is read-only apart from `/tmp`). Test concern: an acceptance test restarts the app against the
  same database and checks titles, done states and order are unchanged.

`/ready` reflects database connectivity so Kubernetes does not route to a pod that cannot reach Postgres.

## todo-web — Todo list page at /

**Shape:** `web-nextjs` · **Criteria:** AC-001.1, AC-001.2, AC-001.3, AC-001.4, AC-001.5, AC-001.6, AC-001.7, AC-001.8, AC-001.9, AC-001.10, AC-001.11, AC-001.12, AC-001.13 · **Depends on:** []

todo-web owns everything the user sees. The page at `/` replaces the template landing page.

- AC-001.1: the page renders at `/` (the hostname and port come from the `todo` repo).
- AC-001.2 / AC-001.3: empty state "No todos yet" plus the add input; otherwise the list in the order todo-api
  returns it (the web does not re-sort).
- AC-001.4: add without a full page reload (client component + Server Action or route handler), clear the input
  on success, append the returned todo.
- AC-001.5 / AC-001.6: the web may validate before sending (same rules) but must also show the message when
  todo-api answers 422 with `title_required` / `title_too_long`. The user-facing texts are the spec's, not the
  API's `message` field.
- AC-001.7: show the `title` todo-api returns (already trimmed).
- AC-001.8 / AC-001.9 / AC-001.10: toggle and delete call todo-api and show the result; a reload shows the server
  state. Delete has no confirmation.
- AC-001.11: on 404 `todo_not_found` show "This todo no longer exists" and re-fetch the list.
- AC-001.12: a network error, a timeout (5 s per call, well under the 10 s of the criterion) or any 5xx from
  todo-api shows "Todos are unavailable, please try again", and the change is not shown as saved (no optimistic
  update that sticks: either update only after success, or roll back). This also applies to the initial page
  load: the page still renders with the message instead of failing with the error page.
- AC-001.13: no client-side storage of todos (no localStorage); the list always comes from todo-api.

## todo — Publish todo-web, wire it to todo-api, add Postgres

**Shape:** `gitops-app` · **Criteria:** AC-001.1, AC-001.14 · **Depends on:** []

- AC-001.1: `expose: {host: todo-web}` renders an HTTPRoute for `todo-web.todo-dev.localhost` on the Traefik
  gateway; on the local k3d cluster the gateway is on port 8088. todo-api stays unexposed (non-goal).
- Wiring: `TODO_API_URL=http://todo-api` on todo-web (the todo-api Service listens on port 80 -> 8080).
- AC-001.14: the postgres addon (CloudNativePG Cluster `todo-postgres` in `todo-dev`, never pruned by Argo) and
  `uses: [postgres]` on todo-api, which injects `DATABASE_URL` and `PG*` from the Secret `todo-postgres-app`.

## Contract

Base URL inside the environment: `http://todo-api` (env `TODO_API_URL` on todo-web). All bodies are JSON
(`Content-Type: application/json`, UTF-8). No authentication, no user scoping: one shared list.

### Todo object

| Field | Type | Notes |
|---|---|---|
| `id` | string | UUID (v4), assigned by todo-api |
| `title` | string | trimmed, 1..200 Unicode code points |
| `done` | boolean | `false` on creation |
| `created_at` | string | RFC 3339 timestamp in UTC, e.g. `2026-10-08T12:34:56.789Z`; set by todo-api |

### Endpoints

**`GET /todos`** — list all todos.
- `200` — JSON array of Todo objects, ordered by `created_at` ascending, then `id` ascending. Empty list: `[]`.

**`POST /todos`** — create a todo.
- Request: `{"title": "<string>"}`. Unknown fields are ignored.
- todo-api trims leading and trailing whitespace (Unicode whitespace, as Python `str.strip()`), then validates.
- `201` — the created Todo object (`done: false`, trimmed `title`).
- `422` `title_required` — `title` missing, `null`, or empty after trimming.
- `422` `title_too_long` — more than 200 code points after trimming (exactly 200 is accepted).
- `422` `invalid_request` — body is not a JSON object or `title` is not a string.

**`PATCH /todos/{id}`** — set the done state.
- Request: `{"done": true}` or `{"done": false}`. Setting the current value again is a no-op that still returns `200`.
- `200` — the updated Todo object.
- `404` `todo_not_found` — no todo with this id (including ids that are not valid UUIDs).
- `422` `invalid_request` — body is not an object or `done` is missing / not a boolean.

**`DELETE /todos/{id}`** — delete a todo.
- `204` — deleted, empty body.
- `404` `todo_not_found` — no todo with this id (including ids that are not valid UUIDs, and ids already deleted).

### Error format

Every 4xx/5xx response produced by todo-api has this body (FastAPI's default validation body is replaced):

```json
{"error": {"code": "title_required", "message": "Title is required"}}
```

| HTTP | `code` | `message` |
|---|---|---|
| 422 | `title_required` | `Title is required` |
| 422 | `title_too_long` | `Title must be at most 200 characters` |
| 422 | `invalid_request` | free text describing the problem |
| 404 | `todo_not_found` | `This todo no longer exists` |
| 503 | `unavailable` | `Todos are unavailable, please try again` (database unreachable) |

todo-web branches on `code` and status, never on `message`. todo-web treats any 5xx, a connection error, or no
response within 5 seconds as "unavailable".

### Probes

- `GET /health` -> `200 {"status": "ok"}` whenever the process runs (no DB check).
- `GET /ready` -> `200 {"status": "ready"}` when the database answers; `503` otherwise.

### Configuration

| Repo | Variable | Value in `dev` | Set by |
|---|---|---|---|
| todo-api | `DATABASE_URL` (+ `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`) | from Secret `todo-postgres-app` | postgres addon, `uses: [postgres]` |
| todo-web | `TODO_API_URL` | `http://todo-api` | `services.yaml` `env` |

No events: the only interaction is synchronous HTTP from todo-web to todo-api.

## gitops-app PR

No image pins: the spec keeps the feature in `dev`, which tracks `latest` and is not pinned; promotion to
`staging` and `prod` is a non-goal (later `/gitops:promote`). Hence `gitops_pin: []`.

Merge order matters for `dev` because a merge to `main` in a service repo publishes `latest`:

1. Merge the `todo` PR first (or together with the others). It is safe against the current images: the template
   todo-api ignores `DATABASE_URL`, the template todo-web ignores `TODO_API_URL`. Merging the new todo-api first
   would leave it without a database (not ready) until this lands.
2. Merge todo-api and todo-web in any order; until both are in, the page shows the "unavailable" message or the
   template page, never wrong data.

After Argo reconciles (`todo-dev`), verify:

- the CloudNativePG Cluster `todo-postgres` is healthy and the Secret `todo-postgres-app` exists;
- the todo-api pod is Ready (its `/ready` checks the database) and has `DATABASE_URL` set;
- `http://todo-web.todo-dev.localhost:8088/` loads the todo list (AC-001.1); add, toggle and delete a todo;
- restart todo-api (Argo sync with restart, or a new `latest` image) and reload the page: same todos, done states
  and order (AC-001.14).
