#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Pin a component's image in one environment (spec 062).

  uv run scripts/pin.py <env> <component> <tag>      e.g. dev todo-api sha-<40 hex>

Sets `newTag` in applications/<app>/overlays/<env>/<component>/kustomization.yaml (and, for an external Helm component,
the image tag in its application.yaml, spec 069). Only immutable tags are accepted
(`sha-<40 hex>` from a build of main, or `sha-<7>` from one before spec 078, or a release `X.Y.Z`), only for a composed component, and only in an environment
the component runs in (an instance in system.yaml); system.yaml is never changed. render.py keeps the pin on every re-render. The pin-dev workflow runs this for every merge to a
component's main. Staging is pinned by the pin-staging workflow after a green dev smoke (spec 093); prod only by
`/instance:promote`.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^(sha-[0-9a-f]{40}|sha-[0-9a-f]{7}|\d+\.\d+\.\d+)$")   # sha-<7>: builds before spec 078


def fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    env, name, tag = argv
    if not TAG.match(tag):
        return fail(f"'{tag}' is not an immutable tag (expected sha-<40 hex> or X.Y.Z)")
    apps = sorted(p for p in (ROOT / "applications").iterdir() if (p / "system.yaml").is_file())
    if len(apps) != 1:
        return fail(f"expected one system under applications/, found {len(apps)}")
    app_dir = apps[0]
    components = (yaml.safe_load((app_dir / "system.yaml").read_text()) or {}).get("components") or {}
    svc = components.get(name)
    if not isinstance(svc, dict):
        return fail(f"unknown component '{name}' (composed: {', '.join(components) or 'none'})")
    envs = list(svc.get("instances") or {}) or ["dev"]
    if env not in envs:
        return fail(f"component '{name}' does not run in {env} (instances: {', '.join(envs)})")
    path = app_dir / "overlays" / env / name / "kustomization.yaml"
    if not path.is_file():
        return fail(f"{path.relative_to(ROOT)} does not exist: run `devbox run render` first")
    doc = yaml.safe_load(path.read_text())
    old = doc["images"][0].get("newTag")
    doc["images"][0]["newTag"] = tag
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    if svc.get("external"):   # spec 069: an external chart carries the tag in its Application too; re-render it
        import render
        for p, content in render.render_app(app_dir, render.answers()).items():
            if p.parent == path.parent and (not p.is_file() or p.read_text() != content):
                p.write_text(content)
    print(f"{name} in {env}: {old} -> {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
