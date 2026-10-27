# Project notes

- The todo system: `todo-web` (Next.js, published through the Gateway) calls `todo-api` (FastAPI) at `http://todo-api` through the `todo-api-rest` API; `todo-api` stores todos in the `postgres` resource. Overview in `README.md`.
- Local clones of `todo`, `todo-api` and `todo-web` go stale, because pin PRs and platform updates merge on GitHub without a local pull. Run `git fetch` before judging a repo's state.
