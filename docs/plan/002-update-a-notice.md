---
plan_id: 002-update-a-notice
spec: 002-update-a-notice
feature: Edit a todo's title in place in the list (todo-web), persisted by todo-api through PATCH /todos/{id}
gitops_app: ika100/todo
status: in_progress
repos:
- id: todo-api
  shape: service-python
  summary: Change a todo's title through PATCH /todos/{id}, with the same validation and trimming as create
  acs:
  - AC-002.2
  - AC-002.3
  - AC-002.5
  - AC-002.6
  - AC-002.7
  - AC-002.8
  - AC-002.10
  arguments: |
    Extend the existing PATCH /todos/{id} (src/todo_api/todos/validation.py parse_patch, router.patch_todo,
    repository.set_done) instead of adding a new endpoint: the body may now carry `title`, `done`, or both.
    Reuse the title rules of parse_create (one shared helper), keep the existing rule order (body validated before
    the id lookup) and a done-only body must behave exactly as in spec 001 (no regression test may change).
    Update title and/or done in one UPDATE statement; never touch `created_at` and do not add an `updated_at` column
    (history is a non-goal). No schema migration is needed: the existing CHECK on `title` already covers 1..200.
    Acceptance tests against a real Postgres, like spec 001.
  depends_on: []
  done: false
- id: todo-web
  shape: web-nextjs
  summary: Edit a todo's title in place in the list (Edit, Save, Cancel, Enter/Escape, click outside cancels)
  acs:
  - AC-002.1
  - AC-002.2
  - AC-002.3
  - AC-002.4
  - AC-002.5
  - AC-002.6
  - AC-002.7
  - AC-002.8
  - AC-002.9
  - AC-002.10
  - AC-002.11
  arguments: |
    Same server-side call path as spec 001: the browser never calls todo-api; the save goes through a Server Action
    or app/api route handler to TODO_API_URL with the existing 5 s timeout and error mapping (reuse it, do not add a
    second client). Saving sends `{"title": ...}` only, never `done`, so a concurrent toggle in another tab is not
    overwritten. Test against a mocked todo-api that follows the contract below.
  depends_on: []
  done: false
gitops_pin: []
---

## Decomposition

Two repos take part, and they build in parallel: the contract below fixes the only interface between them (one
extended endpoint, `PATCH /todos/{id}`). The feature fits the two repos that already exist; no new repo is needed.

No shared library: the interface is one JSON field on an existing endpoint between a Python service and a
TypeScript frontend. The `todo` gitops-app repo is **not** part of this plan: the spec makes deployment, exposure
and wiring changes a non-goal, and nothing needs one (todo-web already reaches todo-api through
`TODO_API_URL=http://todo-api`, todo-api already has Postgres, and the existing `todos.title` column holds edited
titles without a migration).

**Endpoint decision:** the title change extends the existing `PATCH /todos/{id}` (partial update of the Todo
resource) rather than adding `PUT /todos/{id}/title` or a new route. PATCH already means "change part of a todo",
already returns the updated Todo and already answers `404 todo_not_found`, which AC-002.8 relies on. A body with only
`done` keeps working unchanged, so todo-web's spec 001 toggle code and its tests are untouched.

## todo-api — Change a todo's title through PATCH /todos/{id}

**Shape:** `service-python` · **Criteria:** AC-002.2, AC-002.3, AC-002.5, AC-002.6, AC-002.7, AC-002.8, AC-002.10 · **Depends on:** []

todo-api stays the single source of truth; it owns the server side of each criterion:

- AC-002.2: `PATCH /todos/{id}` with `{"title": ...}` stores the new title in Postgres and returns the updated Todo,
  so the change survives a reload (and a restart of todo-api).
- AC-002.3: the update changes only `title` (and `done` only when the body carries it); `id` and `created_at` are
  never changed, so `GET /todos` keeps the todo in the same place. Test concern: edit the middle todo of three and
  check `GET /todos` order and its `done` value are unchanged.
- AC-002.5 / AC-002.6 / AC-002.7: the same rules as `POST /todos`, enforced whatever the client sends: trim, then
  `title_required` when empty, `title_too_long` above 200 code points; exactly 200 after trimming is accepted. On a
  422 nothing is written (test: the stored title is unchanged afterwards).
- AC-002.8: an unknown or deleted id answers `404 todo_not_found` (existing behaviour, now also for title bodies).
- AC-002.10: no version check, no `If-Match`, no conflict response: each PATCH simply overwrites; the last one to
  commit wins. Test concern: two sequential PATCHes with different titles, `GET /todos` shows the second.

## todo-web — Edit a todo's title in place

**Shape:** `web-nextjs` · **Criteria:** AC-002.1 to AC-002.11 · **Depends on:** []

todo-web owns everything the user sees and the edit-mode state, which is purely client-side:

- AC-002.1: each todo row gets an "Edit" control (accessible name "Edit"). Choosing it swaps the title for an
  input in the same row, pre-filled with the current title and focused. Only one todo is in edit mode at a time:
  choosing "Edit" on another todo ends the current edit without saving (same as cancel).
- AC-002.2: "Save" control or Enter sends `PATCH /todos/{id}` with `{"title": <input value>}`; on `200` the row
  leaves edit mode and shows the `title` from the response (no full page reload). No optimistic update that sticks.
- AC-002.3: the row keeps its position in the rendered list and its done state; after a refresh the web shows the
  order todo-api returns (no re-sort).
- AC-002.4 / AC-002.11: Escape, a "Cancel" control, or a pointer-down anywhere outside the row's input, Save and
  Cancel controls ends edit mode with the original title and sends nothing. Test concern: the Save control must not
  count as "outside" (a click on Save saves; pointer-down on Save must not cancel first).
- AC-002.5 / AC-002.6: the web may validate before sending (same rules as the API) but must also handle `422
  title_required` / `title_too_long` from todo-api: stay in edit mode, keep the typed value, show "Title is required"
  / "Title must be at most 200 characters" (the spec's texts, never the API `message`).
- AC-002.7: show the `title` todo-api returns (already trimmed).
- AC-002.8: on `404 todo_not_found` show "This todo no longer exists", leave edit mode and re-fetch the list.
- AC-002.9: a connection error, no answer within 5 s, or any 5xx shows "Todos are unavailable, please try again";
  the old title stays (the row may stay in edit mode with the typed value so the user can retry, but the list never
  shows the new title as saved).
- AC-002.10: saves send only `title`; whatever todo-api returns last is what a reload shows. No conflict UI.

## Contract

Base URL inside the environment: `http://todo-api` (env `TODO_API_URL` on todo-web, already set). All bodies are
JSON (`Content-Type: application/json`, UTF-8). No authentication, no user scoping: one shared list. Everything in
spec 001's contract stays valid; this plan changes only `PATCH /todos/{id}`. The full contract is restated here so
each repo's spec is self-contained.

### Todo object (unchanged)

| Field | Type | Notes |
|---|---|---|
| `id` | string | UUID (v4), assigned by todo-api; never changes |
| `title` | string | trimmed, 1..200 Unicode code points; changeable with PATCH |
| `done` | boolean | `false` on creation; changeable with PATCH |
| `created_at` | string | RFC 3339 timestamp in UTC; set on creation, **never changed by PATCH** |

No new field (no `updated_at`).

### Endpoints

**`GET /todos`** (unchanged) — `200`, JSON array of Todo objects ordered by `created_at` ascending, then `id`
ascending. An edited todo keeps its position.

**`POST /todos`** (unchanged) — `{"title": "<string>"}` -> `201` Todo; `422` `title_required` / `title_too_long` /
`invalid_request` as in spec 001.

**`PATCH /todos/{id}`** (extended) — change the title, the done state, or both.

- Request: a JSON object with at least one of
  - `title`: string — the new title;
  - `done`: boolean — the new done state (spec 001 behaviour).
  Unknown fields are ignored. todo-web sends `{"title": "..."}` for an edit and `{"done": true|false}` for a toggle.
- Rules, applied in this order (the first failing rule answers):
  1. body is not a JSON object -> `422 invalid_request`;
  2. neither `title` nor `done` is present -> `422 invalid_request`;
  3. `done` is present and not a boolean -> `422 invalid_request`;
  4. `title` is present and `null` -> `422 title_required`;
  5. `title` is present and not a string -> `422 invalid_request`;
  6. `title` trimmed (leading and trailing Unicode whitespace, as Python `str.strip()`) is empty -> `422 title_required`;
  7. trimmed `title` longer than 200 code points -> `422 title_too_long` (exactly 200 is accepted);
  8. no todo with this id (including ids that are not valid UUIDs and deleted todos) -> `404 todo_not_found`.
- `200` — the updated Todo object, with the trimmed `title`. Fields not in the request keep their values;
  `created_at` and `id` never change. Setting a field to its current value is a no-op that still returns `200`.
- A failed request (422, 404, 503) writes nothing.
- Concurrency: no version or precondition check. Concurrent PATCHes are applied in commit order; the last one wins
  and no conflict status is ever returned.

**`DELETE /todos/{id}`** (unchanged) — `204`; `404 todo_not_found` for unknown, invalid or already deleted ids.

### Errors

Every 4xx/5xx response produced by todo-api has this body (unchanged from spec 001):

```json
{"error": {"code": "title_required", "message": "Title is required"}}
```

| HTTP | `code` | `message` | todo-web shows (edit save) |
|---|---|---|---|
| 422 | `title_required` | `Title is required` | "Title is required", stays in edit mode |
| 422 | `title_too_long` | `Title must be at most 200 characters` | "Title must be at most 200 characters", stays in edit mode |
| 422 | `invalid_request` | free text | "Todos are unavailable, please try again" (todo-web never sends such a body; treated as unexpected) |
| 404 | `todo_not_found` | `This todo no longer exists` | "This todo no longer exists", leaves edit mode, re-fetches the list |
| 503 | `unavailable` | `Todos are unavailable, please try again` | "Todos are unavailable, please try again" |

todo-web branches on HTTP status and `code`, never on `message`. Any response the contract does not list (another
status, a 4xx without the error body, a 2xx without a valid Todo) is treated like a 5xx: "Todos are unavailable,
please try again", and the title is not shown as saved.

### Timeouts

- todo-web -> todo-api, every call (including the new title PATCH and the list re-fetch after a 404): 5 s. No
  answer within 5 s, a connection error or any 5xx is "unavailable" (well under the 10 s of AC-002.9). No retries:
  the user retries by saving again.
- todo-api -> Postgres: the existing per-operation deadline; when the database is unreachable or slow, todo-api
  answers `503 unavailable` and writes nothing.
- If the 5 s timeout fires on todo-web while todo-api still commits the update, a later reload may show the new
  title; the web never claims it was saved. This is accepted (AC-002.9 only covers todo-api being unreachable).

### Configuration

No change: todo-web keeps `TODO_API_URL=http://todo-api`; todo-api keeps `DATABASE_URL` from the postgres addon. No
events: the only interaction is synchronous HTTP from todo-web to todo-api.

## gitops-app PR

None. No services.yaml change (non-goal), and no image pins: `dev` tracks `latest` and is not pinned; promotion to
`staging` and `prod` is a non-goal (later `/gitops:promote`). Hence `gitops_pin: []`.

Merge order in `dev`: todo-api first, or both together. If todo-web merges first, saving an edit against the old
todo-api answers `422 invalid_request` (the old PATCH requires `done`), which todo-web shows as "Todos are
unavailable, please try again"; no wrong data, and the toggle keeps working.

After Argo reconciles the new `latest` images in `todo-dev`, verify on `http://todo-web.todo-dev.localhost:8088/`:

- edit a todo in the middle of the list, save with Enter: new title in place, same position and done state, still
  there after a reload (AC-002.1 to AC-002.3);
- Escape, Cancel and a click outside discard the change (AC-002.4, AC-002.11);
- empty and 201-character titles show the spec's messages; a 200-character title saves; surrounding spaces are
  removed (AC-002.5 to AC-002.7);
- delete a todo in a second tab, then save an edit of it in the first: "This todo no longer exists" (AC-002.8).
