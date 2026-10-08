#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Validate and manage multi-repo plans in docs/plan/ (ADR-011, amended by ADR-026).

A plan is a Markdown file: a YAML front-matter block followed by a prose body. A plan made from a product spec
names it (`spec: <spec_id>`, the file is docs/specs/<spec_id>/spec.md) and lists per repo the product criteria
(`acs: [AC-<NNN>.<n>]`) that repo implements; every active criterion of the spec must be assigned to a repo.
Plans without `spec:` (written before ADR-026) stay valid and carry a free-text `arguments` prompt per repo.

Usage:
  plan.py validate [<slug>...]     schema + dependency checks (all plans if none given)
  plan.py list [--all]             draft + in_progress plans (--all adds completed/abandoned)
  plan.py show <slug>              topo-ordered checklist
  plan.py ready <slug> [--json]    repos that can start now (not done, every dependency done): one parallel wave
  plan.py start <slug>             draft -> in_progress
  plan.py done <slug> <repo-id>    mark a repo done; all done -> completed
  plan.py abandon <slug>           -> abandoned
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PLAN_DIR = ROOT / "docs" / "plan"
SPEC_DIR = ROOT / "docs" / "specs"
STATUSES = {"draft", "in_progress", "completed", "abandoned"}
ACTIVE = {"draft", "in_progress"}
# Mirrors shapes.yml of the platform repo (ADR-015). Extend via PLAN_SHAPES=a,b when a new shape lands
# before this template is updated.
SHAPES = {"service-python", "library-python", "web-nextjs", "gitops-app", "service-java", "service-go"}
ENVS = {"dev", "staging", "prod"}
FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
AC_LINE = re.compile(r"^\s*[-*]\s+(~~)?\*\*(AC-\d{3,}\.\d+)\*\*")


def spec_criteria(spec_id: str) -> tuple[set[str], set[str]] | None:
    """(active, withdrawn) criterion ids of docs/specs/<spec_id>/spec.md, or None when the spec is missing."""
    path = SPEC_DIR / spec_id / "spec.md"
    if not path.is_file():
        return None
    body = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S)
    m = re.search(r"^## +Acceptance criteria\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    active, withdrawn = set(), set()
    for ln in (m.group(1) if m else "").splitlines():
        hit = AC_LINE.match(ln)
        if hit:
            (withdrawn if hit.group(1) else active).add(hit.group(2))
    return active, withdrawn


def _literal(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(str, _literal, Dumper=yaml.SafeDumper)


def load(path: Path) -> tuple[dict, str]:
    m = FRONT.match(path.read_text())
    if not m:
        raise ValueError("missing YAML front-matter (--- ... ---)")
    meta = yaml.safe_load(m.group(1))
    if not isinstance(meta, dict):
        raise ValueError("front-matter is not a mapping")
    return meta, m.group(2)


def save(path: Path, meta: dict, body: str) -> None:
    front = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False, width=100).rstrip("\n")
    path.write_text(f"---\n{front}\n---\n{body}")


def topo_levels(repos: list[dict]) -> list[list[str]]:
    deps = {r["id"]: set(r.get("depends_on") or []) for r in repos}
    levels: list[list[str]] = []
    done: set[str] = set()
    while len(done) < len(deps):
        ready = sorted(i for i, d in deps.items() if i not in done and d <= done)
        if not ready:
            raise ValueError("dependency cycle among: " + ", ".join(sorted(set(deps) - done)))
        levels.append(ready)
        done |= set(ready)
    return levels


def check(path: Path) -> list[str]:
    errs: list[str] = []
    try:
        meta, _ = load(path)
    except Exception as e:  # noqa: BLE001
        return [str(e)]
    shapes = SHAPES | set(filter(None, __import__("os").environ.get("PLAN_SHAPES", "").split(",")))
    for k in ("plan_id", "feature", "gitops_app", "status", "repos"):
        if k not in meta:
            errs.append(f"missing key '{k}'")
    if errs:
        return errs
    if meta["plan_id"] != path.stem:
        errs.append(f"plan_id '{meta['plan_id']}' must equal the file name '{path.stem}'")
    if meta["status"] not in STATUSES:
        errs.append(f"status must be one of {sorted(STATUSES)}")
    repos = meta["repos"]
    if not isinstance(repos, list) or not repos:
        return errs + ["repos must be a non-empty list"]
    ids = [r.get("id") for r in repos]
    if len(ids) != len(set(ids)):
        errs.append("duplicate repo ids")
    for r in repos:
        rid = r.get("id", "<no id>")
        for k in ("id", "shape", "summary", "depends_on", "done") + (("acs",) if "spec" in meta else ("arguments",)):
            if k not in r:
                errs.append(f"repo {rid}: missing '{k}'")
        if r.get("shape") not in shapes:
            errs.append(f"repo {rid}: unknown shape '{r.get('shape')}'")
        for d in r.get("depends_on") or []:
            if d not in ids:
                errs.append(f"repo {rid}: depends_on unknown repo '{d}'")
        if not isinstance(r.get("done"), bool):
            errs.append(f"repo {rid}: done must be true/false")
    for p in meta.get("gitops_pin") or []:
        if p.get("service") not in ids:
            errs.append(f"gitops_pin: unknown service '{p.get('service')}'")
        if p.get("overlay") not in ENVS:
            errs.append(f"gitops_pin: overlay must be one of {sorted(ENVS)}")
        after = p.get("apply_after")
        if after != "merge_of_all" and after not in ids:
            errs.append(f"gitops_pin: apply_after '{after}' must be a repo id or 'merge_of_all'")
    if "spec" in meta:
        crit = spec_criteria(str(meta["spec"]))
        if crit is None:
            errs.append(f"spec '{meta['spec']}': docs/specs/{meta['spec']}/spec.md not found")
        else:
            active, withdrawn = crit
            assigned: set[str] = set()
            for r in repos:
                for a in r.get("acs") or []:
                    if a in withdrawn:
                        errs.append(f"repo {r.get('id')}: criterion '{a}' is withdrawn")
                    elif a not in active:
                        errs.append(f"repo {r.get('id')}: unknown criterion '{a}'")
                assigned |= set(r.get("acs") or [])
            for a in sorted(active - assigned):
                errs.append(f"{a} is not assigned to any repo")
    if not errs:
        try:
            topo_levels(repos)
        except ValueError as e:
            errs.append(str(e))
    if meta["status"] == "completed" and not all(r.get("done") for r in repos):
        errs.append("status is completed but not every repo is done")
    return errs


def plan_path(slug: str) -> Path:
    p = PLAN_DIR / f"{slug}.md"
    if not p.is_file():
        sys.exit(f"ERROR: no such plan: docs/plan/{slug}.md")
    return p


def progress(meta: dict) -> str:
    repos = meta["repos"]
    return f"{sum(1 for r in repos if r['done'])}/{len(repos)} repos done"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    plans = sorted(PLAN_DIR.glob("*.md")) if PLAN_DIR.is_dir() else []

    if cmd == "validate":
        targets = [plan_path(s) for s in rest] if rest else plans
        bad = 0
        for p in targets:
            for e in check(p):
                print(f"ERROR: {p.relative_to(ROOT)}: {e}", file=sys.stderr)
                bad += 1
        print(f"OK: {len(targets)} plan(s) valid" if not bad else f"{bad} error(s)")
        return 1 if bad else 0

    if cmd == "list":
        show_all = "--all" in rest
        rows = 0
        for p in plans:
            meta, _ = load(p)
            if show_all or meta.get("status") in ACTIVE:
                print(f"{p.stem:32} {meta['status']:12} {progress(meta):16} {meta['feature']}")
                rows += 1
        if not rows:
            print("(no active plans)")
        return 0

    if cmd == "show" and rest:
        meta, _ = load(plan_path(rest[0]))
        by_id = {r["id"]: r for r in meta["repos"]}
        print(f"{meta['plan_id']} [{meta['status']}] — {meta['feature']}  ({progress(meta)})")
        for n, level in enumerate(topo_levels(meta["repos"]), 1):
            print(f"\nLevel {n} (can run in parallel):")
            for rid in level:
                r = by_id[rid]
                dep = f"  after: {', '.join(r['depends_on'])}" if r["depends_on"] else ""
                print(f"  [{'x' if r['done'] else ' '}] {rid} ({r['shape']}) — {r['summary']}{dep}")
        for p in meta.get("gitops_pin") or []:
            print(f"\n  pin {p['service']} in {p['overlay']} after {p['apply_after']}: {p.get('note', '')}")
        return 0

    if cmd == "ready" and rest:
        meta, _ = load(plan_path(rest[0]))
        if meta["status"] in {"completed", "abandoned"}:
            sys.exit(f"ERROR: plan is {meta['status']}")
        finished = {r["id"] for r in meta["repos"] if r["done"]}
        wave = [r for r in meta["repos"] if not r["done"] and set(r.get("depends_on") or []) <= finished]
        wave.sort(key=lambda r: r["id"])
        if "--json" in rest:
            import json
            print(json.dumps([{"id": r["id"], "shape": r["shape"], "summary": r["summary"], "acs": r.get("acs") or [],
                               "arguments": r.get("arguments", "")} for r in wave]))
        else:
            print(f"{len(wave)} repo(s) can start now (in parallel):" if wave else "nothing can start now")
            for r in wave:
                print(f"  {r['id']} ({r['shape']}) — {r['summary']}")
        return 0

    if cmd in {"start", "done", "abandon"} and rest:
        path = plan_path(rest[0])
        meta, body = load(path)
        if cmd == "start":
            if meta["status"] == "draft":
                meta["status"] = "in_progress"
        elif cmd == "abandon":
            if meta["status"] == "completed":
                sys.exit("ERROR: plan is already completed")
            meta["status"] = "abandoned"
        else:
            if len(rest) < 2:
                sys.exit("usage: plan.py done <slug> <repo-id>")
            repo = next((r for r in meta["repos"] if r["id"] == rest[1]), None)
            if repo is None:
                sys.exit(f"ERROR: unknown repo id '{rest[1]}'")
            repo["done"] = True
            if meta["status"] == "draft":
                meta["status"] = "in_progress"
            if all(r["done"] for r in meta["repos"]):
                meta["status"] = "completed"
        save(path, meta, body)
        print(f"{path.stem}: {meta['status']} ({progress(meta)})")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
