#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""CI check: a pull request moves a resource's major in one environment at a time (spec 121, AC-121.10).

  uv run scripts/upgrade-check.py --base origin/main

For every applications/*/system.yaml present on both sides, loads the base (the app directory at the base ref, extracted to a
temporary directory with its external packages) and the working-tree file
with render.load_system and compares each in-cluster resource instance's effective major per environment. A resource whose major changes
in two or more environments fails. A base that does not render is ignored with a WARNING. Exit 0 = pass, 1 = fail.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import render  # noqa: E402


def base_text(base: str, path: str) -> str | None:
    p = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def base_tree(base: str, app: str, dest: Path) -> Path:
    """Extract the app directory `app` (applications/<name>) at `base` into `dest`; external packages come along (#340)."""
    p = subprocess.run(["git", "archive", "--format=tar", base, app], cwd=ROOT, capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return dest / app


def majors(system: dict) -> dict[str, dict[str, object]]:
    """{resource: {env: effective major}} for every in-cluster instance that has one."""
    out: dict[str, dict[str, object]] = {}
    for r, rc in system["cfg"]["resources"].items():
        out[r] = {e: v["version"] for e, v in rc["envs"].items() if not v["external"] and v.get("version") is not None}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="the git ref to compare with, e.g. origin/main")
    ns = ap.parse_args(argv)
    errors: list[str] = []
    if subprocess.run(["git", "rev-parse", "--verify", "-q", f"{ns.base}^{{commit}}"], cwd=ROOT, capture_output=True).returncode != 0:
        print(f"WARNING: {ns.base} does not resolve here (a local run without that branch): nothing to compare")
        print("OK: no resource changes its major in more than one environment")
        return 0
    for path in sorted((ROOT / "applications").glob("*/system.yaml")):
        rel = path.relative_to(ROOT).as_posix()
        text = base_text(ns.base, rel)
        if text is None:
            continue
        try:
            new = majors(render.load_system(path.parent))
        except render.RenderError as e:
            print(f"ERROR: {e}")
            errors.append(str(e))
            continue
        with tempfile.TemporaryDirectory() as tmp:
            try:
                old = majors(render.load_system(base_tree(ns.base, path.parent.relative_to(ROOT).as_posix(), Path(tmp))))
            except (render.RenderError, OSError, subprocess.CalledProcessError, tarfile.TarError) as e:
                print(f"WARNING: {rel}: the base does not render, skipped ({str(e).splitlines()[0] if str(e) else e})")
                continue
        for r, envs in new.items():
            changed = [e for e in render.ENVS if e in envs and e in old.get(r, {}) and envs[e] != old[r][e]]
            if len(changed) < 2:
                continue
            olds = {str(old[r][e]) for e in changed}
            before = next(iter(olds)) if len(olds) == 1 else ", ".join(f"{e} {old[r][e]}" for e in changed)
            targets = {str(envs[e]) for e in changed}
            after = next(iter(targets)) if len(targets) == 1 else ", ".join(f"{e} {envs[e]}" for e in changed)
            target = next(iter(targets)) if len(targets) == 1 else "<new>"
            msg = (f"ERROR: {rel}: resources.{r}: the major changes in {', '.join(changed)} at once ({before} → {after}); "
                   f"upgrade one environment per pull request with /resource:upgrade {r} --to {target} [--env <env>]")
            print(msg)
            errors.append(msg)
    if errors:
        return 1
    print("OK: no resource changes its major in more than one environment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
