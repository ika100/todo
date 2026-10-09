#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Pin a service's image in one environment (spec 062).

  uv run scripts/pin.py <env> <service> <tag>      e.g. dev todo-api sha-abc1234

Sets `newTag` in applications/<app>/overlays/<env>/<service>/kustomization.yaml. Only immutable tags are accepted
(`sha-<7 hex>` from a build of main, or a release `X.Y.Z`), only for a composed service, and only in an environment
the service runs in. render.py keeps the pin on every re-render. The pin-dev workflow runs this for every merge to a
service's main; promotions to staging and prod stay `/gitops:promote`.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^(sha-[0-9a-f]{7}|\d+\.\d+\.\d+)$")


def fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    env, name, tag = argv
    if not TAG.match(tag):
        return fail(f"'{tag}' is not an immutable tag (expected sha-<7 hex> or X.Y.Z)")
    apps = sorted(p for p in (ROOT / "applications").iterdir() if (p / "services.yaml").is_file())
    if len(apps) != 1:
        return fail(f"expected one application under applications/, found {len(apps)}")
    app_dir = apps[0]
    services = (yaml.safe_load((app_dir / "services.yaml").read_text()) or {}).get("services") or []
    svc = next((s for s in services if s.get("name") == name), None)
    if svc is None:
        return fail(f"unknown service '{name}' (composed: {', '.join(s['name'] for s in services) or 'none'})")
    if env not in (svc.get("environments") or ["dev"]):
        return fail(f"service '{name}' does not run in {env} (environments: {', '.join(svc.get('environments') or ['dev'])})")
    path = app_dir / "overlays" / env / name / "kustomization.yaml"
    if not path.is_file():
        return fail(f"{path.relative_to(ROOT)} does not exist: run `devbox run render` first")
    doc = yaml.safe_load(path.read_text())
    old = doc["images"][0].get("newTag")
    doc["images"][0]["newTag"] = tag
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    print(f"{name} in {env}: {old} -> {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
