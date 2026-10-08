---
spec_id: 001-todo-list
title: Todo list
status: draft
priority: P1
shape: gitops-app
---

# 001 — Todo list

## Problem

The todo product is deployed (todo-api and todo-web run in `dev`), but it does nothing a user can use yet: there is no page to open and nothing to keep track of. A user who wants to note tasks and tick them off has no way to do it. This spec delivers the first usable feature: one todo list in the browser, stored by todo-api, so that the deployed product does what its name says.

## Stories

As a user, I want to open the todo app in my browser and see all my todos with whether each is done, so that I know what is left to do.
**Repos:** todo-web, todo-api, todo (exposure of todo-web and wiring of todo-web to todo-api)

As a user, I want to add a todo by typing a title, so that I can capture a task in a few seconds.
**Repos:** todo-web, todo-api

As a user, I want to mark a todo as done, or back to not done, so that the list shows my progress.
**Repos:** todo-web, todo-api

As a user, I want to delete a todo, so that the list only holds what still matters to me.
**Repos:** todo-web, todo-api

As a user, I want my todos to still be there after the product is restarted or updated, so that I can rely on the list.
**Repos:** todo-api, todo (postgres addon for the product's database and its wiring to todo-api)

## Acceptance criteria

- **AC-001.1** Given the product is running in the `dev` environment, when a user opens `http://todo-web.todo-dev.localhost:8088/` on the local cluster, then the todo list page loads.
- **AC-001.2** Given no todos exist, when the user opens the todo list page, then the page shows the message "No todos yet" and an input to add a todo.
- **AC-001.3** Given todos exist, when the user opens the todo list page, then every todo is shown with its title and its done state, ordered by creation time with the oldest first.
- **AC-001.4** Given the todo list page, when the user enters the title "Buy milk" and submits, then a todo "Buy milk" appears in the list as not done without a full page reload, and the input is cleared.
- **AC-001.5** Given the todo list page, when the user submits a title that is empty or only whitespace, then no todo is created and the page shows "Title is required".
- **AC-001.6** Given the todo list page, when the user submits a title longer than 200 characters, then no todo is created and the page shows "Title must be at most 200 characters"; a title of exactly 200 characters is accepted.
- **AC-001.7** Given a title with leading or trailing spaces, when the user adds it, then the todo is stored and shown with those spaces removed.
- **AC-001.8** Given a todo that is not done, when the user marks it as done, then it is shown as done, and it is still shown as done after the page is reloaded.
- **AC-001.9** Given a todo that is done, when the user marks it as not done, then it is shown as not done, and it is still shown as not done after the page is reloaded.
- **AC-001.10** Given a todo in the list, when the user deletes it, then it disappears from the list immediately, without a confirmation step, and does not reappear after the page is reloaded.
- **AC-001.11** Given a todo that was deleted in another browser tab, when the user marks it as done or deletes it in the current tab, then the page shows "This todo no longer exists" and the list is refreshed from todo-api.
- **AC-001.12** Given todo-api is unreachable, when the user opens the page or adds, changes or deletes a todo, then the page shows "Todos are unavailable, please try again" within 10 seconds and does not show the change as saved.
- **AC-001.13** Given todos were added in one browser, when the same page is opened in a different browser, then the same list is shown (one shared list, no sign-in).
- **AC-001.14** Given todos exist, some done and some not done, when todo-api is restarted or redeployed in the `dev` environment, then after it is available again the todo list page shows the same todos with the same titles, done states and order; the todos are kept in the product's Postgres database provided by the postgres addon.

## Non-goals

- Editing the title of an existing todo.
- Sign-in, user accounts, or a separate list per user.
- Due dates, priorities, tags, reordering, search or filtering.
- Several lists.
- Promotion to `staging` and `prod` (handled later with `/gitops:promote`).
- Offline use without todo-api.
- A limit on the number of todos.
- Publishing todo-api outside the cluster; only todo-web is published and reaches it.
- A confirmation step or "Undo" when deleting a todo.

## Open questions

- ~~Must todos survive a restart or redeploy of todo-api?~~ Answered: yes, stored in the product's Postgres via the postgres addon, see AC-001.14.
- ~~Confirm: one shared list, no sign-in.~~ Answered: yes, see AC-001.13 and Non-goals.
- ~~Confirm: the maximum title length is 200 characters.~~ Answered: 200, see AC-001.6.
- ~~Confirm: the list is ordered oldest first.~~ Answered: oldest first, see AC-001.3.
- ~~Should deleting a todo ask for confirmation first?~~ Answered: no, delete immediately, see AC-001.10 and Non-goals.
- ~~Confirm: the user-visible texts, in English.~~ Answered: yes, as written, see AC-001.2, AC-001.5, AC-001.6, AC-001.11, AC-001.12.
- ~~Confirm: todo-web published at `todo-web.todo-dev.localhost`, todo-api not published.~~ Answered: yes, see AC-001.1 and Non-goals.
- ~~Is there a maximum number of todos?~~ Answered: no limit for now, see Non-goals.

## Changelog

- 2026-10-08 created
- 2026-10-08 amended: open questions answered; added AC-001.14 (todos survive a restart or redeploy of todo-api, stored in Postgres via the postgres addon) and its story; AC-001.10 clarified as immediate deletion without confirmation; Non-goals extended (todo limit, publishing todo-api, delete confirmation/undo).
