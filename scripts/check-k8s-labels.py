#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Fail if any Kubernetes label value in the manifests under <dir>/k8s and <dir>/applications is invalid.

Kubernetes label values must match ^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$ and be <= 63 chars. A rendered template
with owner_team="@org" once produced `team: "@org"`, which kubectl/Argo reject at apply time (found by the todo e2e test).

Usage: check-k8s-labels.py <rendered-project-dir>
"""
import re
import sys
from pathlib import Path

import yaml

VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")


def labels(doc, path=""):
    if isinstance(doc, dict):
        for k, v in doc.items():
            here = f"{path}.{k}" if path else k
            if k in ("labels", "matchLabels", "commonLabels") and isinstance(v, dict):
                for lk, lv in v.items():
                    yield here, str(lk), str(lv)
            else:
                yield from labels(v, here)
    elif isinstance(doc, list):
        for i, item in enumerate(doc):
            yield from labels(item, f"{path}[{i}]")


def main(root: str) -> int:
    bad = 0
    files = [f for sub in ("k8s", "applications") if Path(root, sub).is_dir() for f in sorted(Path(root, sub).rglob("*.y*ml"))]
    for f in files:
        for doc in yaml.safe_load_all(f.read_text()):
            if not isinstance(doc, (dict, list)):
                continue
            for where, k, v in labels(doc):
                if not VALUE.match(v) or len(v) > 63:
                    print(f"ERROR: {f.relative_to(root)}: {where}: {k}={v!r} is not a valid label value", file=sys.stderr)
                    bad += 1
    print("OK: k8s label values valid" if not bad else f"{bad} invalid label value(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
