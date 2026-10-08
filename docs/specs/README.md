# Feature specs

Every feature of this repo is described, planned, built and verified from one folder here: `docs/specs/<NNN>-<slug>/` ([ADR-026](https://github.com/ika100/sdlc-foundry/blob/main/docs/adr/026-feature-specs.md)).

| File | Holds | Written by |
|---|---|---|
| `spec.md` | **What**: problem, stories, acceptance criteria `AC-<NNN>.<n>` (Given / when / then), non-goals, open questions, changelog | `/svc:spec` (in a gitops-app repo `/app:spec`) |
| `design.md` | **How**, when needed: decisions and the contract (API, events, errors) | `/svc:plan` |
| `plan.md` | **Tasks**: files, dependencies, and the criteria each task covers | `/svc:plan` (product plans: `docs/plan/<id>.md`, `/app:plan`) |
| `verification.md` | **Evidence**: each criterion met or not, with its test and code | `/svc:build`, `/svc:verify` |

## Lifecycle

`draft` → `approved` → `building` → `done` (and `superseded`). A spec is approved only when its open questions are answered, planned only when approved, and built only as planned. Status changes through the commands, never by hand. Amending a spec (`/svc:spec --amend <NNN> <change>`) sends it back to `draft`.

## Rules that keep the spec true

- Criterion ids are never reused or renumbered; a withdrawn criterion stays, struck through, with the reason.
- Tests that name a criterion (`AC-<NNN>.<n>` in the test name or a comment) are written from the approved spec **before** the code and are never weakened to make a build pass; change the spec instead.
- `devbox run spec-check` (and the `specs` CI workflow) validates every spec and plan and, for specs being built or done, that each criterion is named by a test.
- `/svc:specs` lists the specs with their status and the next command; `docs/backlog.md` holds the generated one-line index.
