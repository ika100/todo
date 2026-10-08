---
spec_id: 002-update-a-notice
title: Edit a todo's title
status: draft
priority: P1
shape: gitops-app
---

# 002 — Edit a todo's title

## Problem

The todo list (spec 001) lets a user add, tick off and delete todos, but a todo's title can't be changed after it is added. To fix a typo or make a task more precise ("Buy milk" to "Buy oat milk"), the user has to delete the todo and add it again. That loses its place in the list, because the list is ordered oldest first, and it loses its done state. Spec 001 left title editing out on purpose; this spec adds it.

## Stories

As a user, I want to change the title of a todo that is already in my list, so that I can correct or refine it without losing its place or its done state.
**Repos:** todo-web, todo-api

As a user, I want to cancel a change I started, so that I don't change a todo by accident.
**Repos:** todo-web

As a user, I want a clear message when my change can't be saved, so that I never believe a title was changed when it was not.
**Repos:** todo-web, todo-api

## Acceptance criteria

- **AC-002.1** Given a todo "Buy milk" in the list, when the user chooses that todo's "Edit" control (each todo has one), then the title is shown in place in the list (no separate page or dialog) in an editable input that already holds "Buy milk", and only this one todo is in edit mode.
- **AC-002.2** Given a todo "Buy milk" in edit mode, when the user changes the title to "Buy oat milk" and saves (with the Enter key or a "Save" control), then the todo is shown in place in the list as "Buy oat milk" without a full page reload, and it is still shown as "Buy oat milk" after the page is reloaded.
- **AC-002.3** Given a todo whose title was changed, when the list is shown, then the todo keeps its done state and its position in the list (ordering by creation time is unaffected by the change).
- **AC-002.4** Given a todo in edit mode with a changed title, when the user cancels (with the Escape key or a "Cancel" control), then edit mode ends, the original title is shown and nothing is saved, including after a page reload.
- **AC-002.5** Given a todo in edit mode, when the user saves a title that is empty or only whitespace, then the title is not changed, the todo stays in edit mode and the page shows "Title is required".
- **AC-002.6** Given a todo in edit mode, when the user saves a title longer than 200 characters, then the title is not changed, the todo stays in edit mode and the page shows "Title must be at most 200 characters"; a title of exactly 200 characters is saved.
- **AC-002.7** Given a todo in edit mode, when the user saves a title with leading or trailing spaces, then the todo is stored and shown with those spaces removed.
- **AC-002.8** Given a todo that was deleted in another browser tab, when the user saves a new title for it in the current tab, then the page shows "This todo no longer exists" and the list is refreshed from todo-api.
- **AC-002.9** Given todo-api is unreachable, when the user saves a new title, then the page shows "Todos are unavailable, please try again" within 10 seconds, the change is not shown as saved and the todo keeps its previous title after a page reload.
- **AC-002.10** Given the same todo is edited in two browser tabs, when both save different titles one after the other, then the title saved last is the one shown after reloading either tab (last save wins, no conflict warning).
- **AC-002.11** Given a todo in edit mode with a changed title, when the user clicks anywhere on the page outside that todo's input, "Save" and "Cancel" controls, then edit mode ends, the original title is shown and nothing is saved, including after a page reload.

## Non-goals

- Changing anything other than the title in edit mode. The done state is still changed with the existing control from spec 001.
- Editing on a separate page or in a dialog; editing happens in place in the list.
- A history of changes, undo, or showing when a todo was last changed.
- Detecting or warning about edits made at the same time in another tab or browser (see AC-002.10).
- Editing several todos at once.
- Changes to deployment, exposure or wiring in this repo: the feature only needs todo-web and todo-api.
- Promotion to `staging` and `prod` (handled later with `/gitops:promote`).

## Open questions

none

## Changelog

- 2026-10-08 created
- 2026-10-08 amended: answers to open questions; renamed to Edit a todo's title
