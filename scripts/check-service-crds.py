#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml", "jsonschema"]
# ///
"""Validate the Components of this repo against the CRDs of the component ref named in the PR body (spec 098).

`/component:ship` writes one line into its gitops PR: `Service-ref: <owner>/<repo>@<branch>`. CI runs this script on pull
requests with the PR body and a read token (`SERVICE_REPOS_READ_TOKEN`). The script reads the CRDs of that component
branch through the GitHub REST API and validates every matching resource under applications/ against them.

Inputs (environment): PR_BODY, SERVICE_REPOS_READ_TOKEN, GITHUB_API_URL (default https://api.github.com),
GITHUB_ENV and GITHUB_STEP_SUMMARY (optional).
Output: stdout carries only the kubeconform skip list (`<apiVersion>/<Kind>,...`), stderr the log. Exit 0 = pass or
nothing to do, 1 = fail. Every output line has the token value replaced by `***`.
"""
from __future__ import annotations

import base64
import binascii
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 30
HINT = "Expected one line: Service-ref: <owner>/<repo>@<branch>"
CANDIDATE = re.compile(r"^\s*service-ref\s*:", re.IGNORECASE)
MARKER = re.compile(
    r"^Service-ref: (?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/(?P<repo>[A-Za-z0-9._-]{1,100})"
    r"@(?P<branch>[A-Za-z0-9._/-]{1,255})$"
)
JSON_TYPES = {str: "string", bool: "boolean", int: "integer", float: "number", list: "array", dict: "object",
              type(None): "null"}
TOKEN = os.environ.get("SERVICE_REPOS_READ_TOKEN", "")
API = os.environ.get("GITHUB_API_URL") or "https://api.github.com"

_lines: list[str] = []


def redact(text: str) -> str:
    return text.replace(TOKEN, "***") if TOKEN else text


def emit(line: str) -> None:
    """The one place that writes log lines: redacted, to stderr and the step summary."""
    line = redact(line)
    _lines.append(line)
    print(line, file=sys.stderr, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"- {line}\n")


class Fail(Exception):
    """A check failed; the message is the full ERROR line."""


# --- marker (steps 1-3) ---

def parse_marker(body: str) -> re.Match[str] | None:
    candidates = [ln.rstrip() for ln in body.split("\n") if CANDIDATE.match(ln.rstrip("\r"))]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise Fail(f"ERROR: {len(candidates)} Service-ref lines (one per PR). {HINT}")
    m = MARKER.match(candidates[0])
    branch = m["branch"] if m else ""
    if (not m or branch[0] in "-/" or branch.endswith(("/", ".lock")) or ".." in branch or "//" in branch):
        raise Fail(f"ERROR: malformed Service-ref line '{candidates[0]}'. {HINT}")
    return m


def load_yaml_file(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def services_of(slug: str) -> list[dict]:
    """Every system.yaml component whose repo is `slug` (case-insensitive)."""
    found, any_entry = [], False
    for f in sorted((ROOT / "applications").glob("*/system.yaml")):
        data = load_yaml_file(f)
        comps = data.get("components") if isinstance(data, dict) else None
        for svc in (comps.values() if isinstance(comps, dict) else []):
            if isinstance(svc, dict) and isinstance(svc.get("repo"), str):
                any_entry = True
                if svc["repo"].lower() == slug.lower():
                    found.append(svc)
    return found if any_entry else []


# --- GitHub access (steps 5-7) ---

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the token must not leave GITHUB_API_URL."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


OPENER = urllib.request.build_opener(NoRedirect)
HTTP_REASONS = {
    401: "HTTP 401 (the token is invalid or expired)",
    403: "HTTP 403 (the token may not read this repo, or the rate limit is exhausted)",
    404: "HTTP 404 (repository not found, or the token has no access to it)",
}


class ApiError(Exception):
    def __init__(self, status: int | None, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def api_get(path: str) -> Any:
    """GET `path` from the API; one retry on 5xx and connection errors. Raises ApiError."""
    req = urllib.request.Request(API.rstrip("/") + path, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "check-service-crds"})
    for attempt in (1, 2):
        try:
            with OPENER.open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt == 1:
                time.sleep(0.5)
                continue
            raise ApiError(e.code, HTTP_REASONS.get(e.code, f"HTTP {e.code}")) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            if attempt == 1 and not isinstance(e, ValueError):
                time.sleep(0.5)
                continue
            raise ApiError(None, f"network error: {getattr(e, 'reason', None) or e}") from None
    raise AssertionError("unreachable")


class Repo:
    def __init__(self, owner: str, name: str):
        self.slug = f"{owner}/{name}"
        self.base = f"/repos/{owner}/{name}"
        self.sha = ""

    def get(self, tail: str) -> Any:
        try:
            return api_get(self.base + tail)
        except ApiError as e:
            if e.status == 404 and tail.startswith("/branches/"):
                raise
            raise Fail(f"ERROR: cannot read {self.slug}: {e.reason}") from None

    def contents(self, path: str) -> Any:
        return self.get(f"/contents/{urllib.parse.quote(path, safe='/')}?ref={self.sha}")

    def listing(self, path: str) -> list[dict]:
        """Files below `path` (recursive); a missing directory is empty."""
        try:
            entries = self.contents(path)
        except Fail as e:
            if "HTTP 404" in str(e):
                return []
            raise
        files: list[dict] = []
        for entry in entries if isinstance(entries, list) else []:
            if entry.get("type") == "dir":
                files += self.listing(entry["path"])
            elif entry.get("type") == "file":
                files.append(entry)
        return files

    def read(self, path: str) -> str | None:
        """File text, or None when the contents API does not deliver it (over 1 MB)."""
        data = self.contents(path)
        if not isinstance(data, dict) or data.get("encoding") != "base64" or not data.get("content"):
            return None if isinstance(data, dict) and data.get("size") else ""
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None


def resolve_ref(repo: Repo, branch: str) -> str:
    """Pin the repo to a commit; a gone branch falls back to the default branch. Returns the branch used."""
    default = repo.get("")["default_branch"]
    try:
        info = repo.get(f"/branches/{urllib.parse.quote(branch, safe='')}")
    except ApiError:
        emit(f"WARNING: branch {branch} no longer exists in {repo.slug}; validating against its default branch {default}")
        branch = default
        try:
            info = repo.get(f"/branches/{urllib.parse.quote(branch, safe='')}")
        except ApiError as e:   # the default branch is unreadable too: fail with the formatted reason, never a traceback
            raise Fail(f"ERROR: cannot read {repo.slug}: {e.reason}") from None
    repo.sha = info["commit"]["sha"]
    return branch


# --- CRDs (steps 6, 8) ---

def chart_dirs(repo: Repo, services: list[dict]) -> list[str]:
    paths = []
    for svc in services:
        helm = (svc.get("external") or {}).get("helm")
        if isinstance(helm, dict) and helm.get("path"):
            paths.append(str(helm["path"]).strip("/"))
    if paths or any((svc.get("external") or {}).get("helm") for svc in services):
        return sorted(set(paths))
    try:
        top = repo.contents("charts")
    except Fail as e:
        if "HTTP 404" in str(e):
            return []
        raise
    return sorted(e["path"] for e in top if isinstance(top, list) and e.get("type") == "dir")


def find_crds(repo: Repo, ref: str, charts: list[str]) -> tuple[list[dict], list[str]]:
    """The CRD documents of the charts, and the ERROR lines for files that cannot be used."""
    crds: list[dict] = []
    errors: list[str] = []
    for chart in charts:
        for sub in ("crds", "templates"):
            for entry in repo.listing(f"{chart}/{sub}"):
                path = entry["path"]
                if not path.endswith((".yaml", ".yml")):
                    continue
                prefix = f"ERROR: {repo.slug}@{ref}: {path}: "
                text = repo.read(path)
                if text is None:
                    errors.append(prefix + "file too large for the contents API")
                    continue
                if sub == "templates" and "{{" in text:
                    if "CustomResourceDefinition" in text:
                        errors.append(prefix + "templated CRD (contains {{ }}): ship it in crds/")
                    continue
                try:
                    docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
                except yaml.YAMLError as e:
                    if sub == "crds" or "CustomResourceDefinition" in text:
                        errors.append(prefix + f"invalid YAML: {' '.join(str(e).split())}")
                    continue
                for doc in docs:
                    if doc.get("kind") == "CustomResourceDefinition":
                        crds.append({"path": path, "doc": doc})
    return crds, errors


def to_schema(node: Any, root: bool = False) -> Any:
    """CRD openAPIV3Schema -> JSON schema, mirroring kubeconform -strict."""
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "patternProperties") and isinstance(value, dict):
            out[key] = {k: to_schema(v) for k, v in value.items()}
        elif key in ("items", "additionalProperties", "not") and isinstance(value, dict):
            out[key] = to_schema(value)
        elif key in ("items", "allOf", "anyOf", "oneOf") and isinstance(value, list):
            out[key] = [to_schema(v) for v in value]
        elif key.startswith("x-kubernetes-") or key == "format":
            continue
        else:
            out[key] = value
    if node.get("x-kubernetes-int-or-string"):
        out.pop("type", None)
        out["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    if node.get("nullable"):
        types = out.get("type")
        if isinstance(types, str):
            out["type"] = [types, "null"]
        elif isinstance(types, list) and "null" not in types:
            out["type"] = [*types, "null"]
    if root or node.get("x-kubernetes-embedded-resource"):
        props = out.setdefault("properties", {})
        props.setdefault("apiVersion", {"type": "string"})
        props.setdefault("kind", {"type": "string"})
        props.setdefault("metadata", {"type": "object"})
    if ("properties" in out and "additionalProperties" not in out
            and not node.get("x-kubernetes-preserve-unknown-fields")):
        out["additionalProperties"] = False
    return out


def build_kinds(repo: Repo, ref: str, crds: list[dict]) -> tuple[dict[tuple[str, str], dict], list[str]]:
    """{(group, kind): {version: validator}} for every served version; plus the ERROR lines."""
    kinds: dict[tuple[str, str], dict] = {}
    errors: list[str] = []
    for crd in crds:
        spec = crd["doc"].get("spec")
        prefix = f"ERROR: {repo.slug}@{ref}: {crd['path']}: "
        try:
            group, kind, versions = spec["group"], spec["names"]["kind"], spec["versions"]
            served = [v for v in versions if v.get("served", True)]
        except (KeyError, TypeError, AttributeError):
            errors.append(prefix + "invalid CRD: needs spec.group, spec.names.kind and spec.versions")
            continue
        validators = {}
        for v in served:
            schema = (v.get("schema") or {}).get("openAPIV3Schema")
            if not isinstance(schema, dict):
                errors.append(prefix + f"no openAPIV3Schema for version {v.get('name')}")
                continue
            validators[v["name"]] = Draft7Validator(to_schema(schema, root=True))
        kinds[(group, kind)] = validators
    return kinds, errors


# --- Components (step 9) ---

def patch_files() -> set[Path]:
    """Files that a kustomization.yaml in the same directory names as patches."""
    skipped: set[Path] = set()
    for k in (ROOT / "applications").rglob("kustomization.y*ml"):
        data = load_yaml_file(k)
        if not isinstance(data, dict):
            continue
        names = [p.get("path") for p in data.get("patches") or [] if isinstance(p, dict)]
        names += [p.get("path") for p in data.get("patchesJson6902") or [] if isinstance(p, dict)]
        names += [p for p in data.get("patchesStrategicMerge") or [] if isinstance(p, str)]
        skipped |= {(k.parent / n).resolve() for n in names if isinstance(n, str)}
    return skipped


def field_path(path: Any, extra: str = "") -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}" if out else str(part)
    if extra:
        out += f".{extra}" if out else extra
    return out or "(root)"


def describe(err: Any) -> list[tuple[str, str]]:
    """(field, problem) pairs for one jsonschema error."""
    instance, schema = err.instance, err.schema
    if err.validator == "additionalProperties" and isinstance(instance, dict):
        known = set(schema.get("properties", {}))
        return [(field_path(err.absolute_path, k), "unknown field") for k in sorted(set(instance) - known)]
    if err.validator == "required" and isinstance(instance, dict):
        return [(field_path(err.absolute_path, k), "missing required field")
                for k in err.validator_value if k not in instance]
    if err.validator == "type":
        want = err.validator_value if isinstance(err.validator_value, str) else " or ".join(err.validator_value)
        return [(field_path(err.absolute_path), f"wrong type (want {want}, got {JSON_TYPES.get(type(instance), 'object')})")]
    return [(field_path(err.absolute_path), err.message)]


def check_components(kinds: dict, source: str) -> tuple[list[str], dict[tuple[str, str], int]]:
    errors: list[str] = []
    counts: dict[tuple[str, str], int] = {}
    skipped = patch_files()
    for f in sorted((ROOT / "applications").rglob("*.y*ml")):
        if f.suffix not in (".yaml", ".yml") or f.resolve() in skipped:
            continue
        try:
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        rel = f.relative_to(ROOT).as_posix()
        for doc in docs:
            if not isinstance(doc, dict) or not isinstance(doc.get("apiVersion"), str):
                continue
            group, _, version = doc["apiVersion"].rpartition("/")
            key = (group, doc.get("kind"))
            if key not in kinds:
                continue
            counts[key] = counts.get(key, 0) + 1
            meta = doc.get("metadata")
            name = meta.get("name", "?") if isinstance(meta, dict) else "?"
            head = f"ERROR: {rel}: {key[1]} {name} ({doc['apiVersion']}) does not match the CRD from {source}: "
            validator = kinds[key].get(version)
            if validator is None:
                served = ",".join(kinds[key])
                errors.append(head + f"(root): version {version} is not served by the CRD (served: {served})")
                continue
            pairs = [p for e in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
                     for p in describe(e)]
            errors += [head + f"{field}: {problem}" for field, problem in pairs]
            counts[key] -= 1 if pairs else 0
    return errors, counts


# --- main ---

def run() -> int:
    m = parse_marker(os.environ.get("PR_BODY", ""))
    if m is None:
        emit("service CRDs: no Service-ref marker; manifests are validated as before")
        return 0
    owner, name, branch = m["owner"], m["repo"], m["branch"]
    slug = f"{owner}/{name}"
    services = services_of(slug)
    if not services:
        raise Fail(f"ERROR: Service-ref names {slug}, which is not a component repo in applications/*/system.yaml; "
                   "nothing was fetched")
    if not TOKEN:
        raise Fail(f"ERROR: Service-ref {slug}@{branch} needs the secret SERVICE_REPOS_READ_TOKEN to read {slug}; "
                   "add it (run cplat doctor in this repo for how)")
    repo = Repo(owner, name)
    branch = resolve_ref(repo, branch)
    source = f"{slug}@{branch}"
    crds, errors = find_crds(repo, branch, chart_dirs(repo, services))
    if not crds and not errors:
        emit(f"service CRDs: {source} defines no CRDs; manifests are validated as before")
        return 0
    kinds, kind_errors = build_kinds(repo, branch, crds)
    errors += kind_errors
    if errors:
        for line in errors:
            emit(line)
        return 1
    errors, counts = check_components(kinds, source)
    for line in errors:
        emit(line)
    if errors:
        return 1
    for (group, kind), n in counts.items():
        if n:
            emit(f"OK: {kind} ({group}) from {source}: {n} resource(s) valid")
    emit(f"service CRDs from {source} (commit {repo.sha[:7]}): {', '.join(kind for _, kind in kinds)}")
    skip = ",".join(f"{group}/{version}/{kind}" for (group, kind), vs in kinds.items() for version in vs)
    if skip:
        env_file = os.environ.get("GITHUB_ENV")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write(redact(f"SERVICE_CRD_SKIP={skip}") + "\n")
        print(redact(skip))
    return 0


def main() -> int:
    try:
        return run()
    except Fail as e:
        emit(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
