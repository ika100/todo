#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Render ApplicationSets, overlays and Kubernetes manifests from system.yaml (ADR-014, ADR-017, ADR-054).

The GitOps repo owns every manifest. applications/<system>/system.yaml is the only hand-edited file of a system; this script derives:
  applications/<system>/applicationset.yaml                    one ApplicationSet per environment (dev/staging/prod)
  applications/<system>/overlays/<env>/<component>/            deployment.yaml, service.yaml, [httproute.yaml], kustomization.yaml
  applications/<system>/addons/<env>/<resource>/               cluster.yaml, kustomization.yaml (only where a running component `uses` the resource)
  applications/<system>/policies/<env>/                        policy.yaml, kustomization.yaml (Kyverno guard rails, unless `policy: off`)
  bootstrap/<system>-root.yaml                                 root Argo Application (app-of-apps), applied once by a human

A component only runs in the environments that are keys of its `instances` (none = dev), so a new component never reaches
staging/prod before `/instance:promote` adds the key. Image pins (`newTag`) live in the overlays and are preserved on re-render:
versions never appear in system.yaml. A directory that still has app.yaml or services.yaml is not rendered: run
`cplat platform update --migrate`.

system.yaml (every value explicit and reviewable; the model is described in docs/DOMAIN-MODEL.md):
  system: todo                       # required; equals the directory name
  owner: acme                        # required: a non-empty string
  gateway: {name: traefik-gateway, namespace: kube-system}         # optional (default as shown)
  secretStore: {name: platform-secrets, kind: ClusterSecretStore}   # optional: where `remote` secrets are read from
  registries: [docker.io/library/]   # optional: extra image prefixes the guard rails accept
  environments:                      # required: exactly dev, staging, prod, in this order (the promotion order)
    dev:
      host: "{component}.{system}-dev.localhost"   # placeholders {component} (expose.host or the name), {system}, {env}; none = not exposed
      url: "http://{host}:8088"                     # read by smoke/ship/verify, not by render: the URL smoke tests use
      policy: audit                                 # audit | enforce | off (default audit in dev and staging, enforce in prod)
      observability:                                # optional: this environment gets the OpenTelemetry collector (ADR-021)
        ui: lgtm                                    # none | lgtm; or exportTo: https://otlp.example.com (+ headersSecret: <secret>)
        alerts: true                                # alert issues (spec 092): false = the poll skips this environment
        rules: [{name: HighErrorRate, query: "<PromQL>", severity: warning, summary: "optional"}]   # critical | warning | info
        queryURL: https://prom.example.com/api/prom # optional; querySecret: <name> or <namespace>/<name> (key AUTHORIZATION)
        alertmanagerURL: https://am.example.com     # optional
    staging:
      promote: after-dev-smoke       # after-dev-smoke | manual (default); dev takes none; prod is always promoted by hand
    prod: {}
  components:
    todo-api:
      type: service                  # service | website | library, required with lang
      lang: python                   # python | java | go | typescript (website) | unknown (migrated 4.x entries)
      repo: org/todo-api             # where the image is built
      image: ghcr.io/org/todo-api    # no tag: the tag is pinned in the overlay (dev: latest)
      port: 8080
      probes: {liveness: /health, readiness: /ready}
      user: 65532                    # numeric non-root UID the image runs as
      volumes: {tmp: /tmp}           # writable emptyDirs (root filesystem is read-only)
      env: {LOG_LEVEL: INFO}         # non-secret wiring, e.g. API_URL: http://todo-api
      metrics: /metrics              # optional: the collector scrapes it
      secretRefs: []                 # names of existing Secrets exposed as env vars
      secrets:                       # Secrets created by External Secrets Operator (never values in git, ADR-018)
        - {name: todo-api-auth, generate: [JWT_KEY]}              # random values generated in the cluster; optional `length` (32)
        - {name: todo-api-stripe, remote: {keys: [STRIPE_KEY]}}   # read from the secret store; optional `path`, `refreshInterval`
                                     # path placeholders {system}, {env}, {secret}, {component}; default {system}-{env}-{secret}
      replicas: 1                    # one number
      resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {memory: 512Mi}}
      expose: {host: api}            # optional: publish through the Gateway (the environment needs `host`)
      provides: [todo-api-rest]      # APIs declared under `apis:` (exactly one provider each)
      consumes: []
      uses: [postgres, todo-lib]     # resources of the system and library components (a library changes no manifest)
      instances:                     # the keys are the environments it runs in; absent = dev only
        dev: {}
        staging: {promote: manual}
        prod: {replicas: 2, env: {LOG_LEVEL: WARN}}    # replicas, resources, env (merged), promote, secrets (replace declared fields by name)
    todo-lib: {type: library, lang: python, repo: org/todo-lib}    # a library has no instances and renders nothing
  apis:
    todo-api-rest: {type: openapi}   # openapi | grpc | graphql | events; changes no manifest
  resources:
    postgres:                        # the only resource in 5.0
      type: database
      version: 17
      replicas: 1
      storage: 1Gi
      resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {memory: 512Mi}}
      instances: {prod: {replicas: 3, storage: 20Gi}}   # per-environment overrides: replicas, storage, resources

External component (spec 069, ADR-029): a component whose manifests this generator cannot express (CronJobs, volumes,
a login proxy, a Helm chart with CRDs). The package is owned by this repo or pinned to the component repo's chart;
render.py only wires it into the environments and pins its image, so pin-dev, /instance:promote and /system:status work.
It takes only type, lang, repo, image, external, provides, consumes and instances (an instance sets only `promote`):
  stock-manager:
    {type: service, lang: unknown, repo: org/stock-manager, image: ghcr.io/org/stock-manager,
     external: {kustomize: external/stock-manager, namespace: stock-manager},   # {env} allowed in the path
     instances: {dev: {}}}
  ai-ops (a chart):
    external: {helm: {repoURL: https://github.com/org/ai-ops, path: charts/ai-ops, revision: main, imageTag: image.tag},
               namespace: ai-ops, values: {replicas: 1}, envValues: {prod: {replicas: 2}}}

Usage:
  render.py            write files
  render.py --check    exit 1 if the tree differs from what would be rendered (CI / quality gate)
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENVS = ["dev", "staging", "prod"]
NOT_MIGRATED = "not migrated to 5.0: run `cplat platform update --migrate`"
MANAGED_FILES = {"deployment.yaml", "service.yaml", "httproute.yaml", "kustomization.yaml", "password-generator.yaml", "application.yaml"}
MANAGED_PREFIXES = ("externalsecret-",)
TOP_KEYS = ["system", "owner", "environments", "components", "apis", "resources", "gateway", "secretStore", "registries"]
ENV_KEYS = ["host", "url", "policy", "promote", "observability"]
# Valid type/lang pairs (the languages of shapes.yml); service/unknown is what the migration writes for a 4.x entry without a known shape.
VALID_PAIRS = [("service", "python"), ("service", "java"), ("service", "go"), ("website", "typescript"), ("library", "python"),
               ("service", "unknown")]
PAIR_TEXT = ", ".join(f"{t}/{lang}" for t, lang in VALID_PAIRS if lang != "unknown")
COMMON_KEYS = ["type", "lang", "repo"]
SERVICE_KEYS = [*COMMON_KEYS, "image", "port", "probes", "user", "volumes", "env", "secretRefs", "secrets", "replicas", "resources",
                "expose", "metrics", "provides", "consumes", "uses", "instances"]
# Keys an external component may carry: its package owns ports, probes, secrets and exposure (spec 069).
EXTERNAL_KEYS = [*COMMON_KEYS, "image", "external", "provides", "consumes", "instances"]
LIBRARY_KEYS = [*COMMON_KEYS, "provides", "consumes"]
INSTANCE_KEYS = ["replicas", "resources", "env", "promote", "secrets"]
RESOURCE_KEYS = ["type", "version", "replicas", "storage", "resources", "instances"]
RESOURCE_INSTANCE_KEYS = ["replicas", "storage", "resources"]
API_TYPES = ["openapi", "grpc", "graphql", "events"]
POLICY_VALUES = {"audit": "Audit", "enforce": "Enforce", "off": "Off"}     # system.yaml policy -> the 4.x mode
# Resource contract (ADR-020): what a component gets when it `uses` a resource. Components never see the implementation.
ADDONS = {
    "postgres": {
        "secret": "{app}-postgres-app",    # connection Secret written by the operator (CloudNativePG)
        "env": {"DATABASE_URL": "uri", "PGHOST": "host", "PGPORT": "port", "PGDATABASE": "dbname", "PGUSER": "user", "PGPASSWORD": "password"},  # pragma: allowlist secret
        "defaults": {"version": 17, "instances": 1, "storage": "1Gi"},
    },
    # OpenTelemetry base (ADR-021): applies to every component of an environment that has `observability`. A collector per environment
    # receives OTLP and scrapes the components' Prometheus endpoints; a UI/backend is optional (`ui: lgtm`, or `exportTo`).
    "observability": {
        "scope": "all",
        "prune": True,                      # configuration only: removing it removes the collector (postgres keeps its data)
        "image": "otel/opentelemetry-collector-contrib:0.162.0",
        "defaults": {"ui": "none"},
    },
}
POLICY_API = "policies.kyverno.io/v1"
POLICY_MODES = {"Audit": "Audit", "Enforce": "Deny"}     # mode -> Kyverno validationActions
LGTM_ENDPOINT = "http://lgtm.observability.svc:4318"    # the shared all-in-one UI stack `cluster-up` installs for `ui: lgtm`
ADDON_FILES = {"cluster.yaml", "kustomization.yaml"}
ESO_API = "external-secrets.io/v1"
GEN_API = "generators.external-secrets.io/v1alpha1"
DEFAULT_STORE = {"name": "platform-secrets", "kind": "ClusterSecretStore"}
DEFAULT_GATEWAY = {"name": "traefik-gateway", "namespace": "kube-system"}
SECRET_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LITERAL_SECRET = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL)", re.I)
ALERT_SEVERITIES = ("critical", "warning", "info")
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class RenderError(Exception):
    pass


def fail(text: str):
    raise RenderError(text)


def dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def answers() -> dict:
    f = ROOT / ".copier-answers.yml"
    return yaml.safe_load(f.read_text()) if f.is_file() else {}


def existing_tag(kfile: Path) -> str | None:
    """Image tag already pinned in an overlay kustomization, if any."""
    if not kfile.is_file():
        return None
    doc = yaml.safe_load(kfile.read_text()) or {}
    for img in doc.get("images", []):
        if img.get("newTag"):
            return img["newTag"]
    return None


# ---------------- loading and validating system.yaml ----------------

def check_placeholders(where: str, template, allowed: list[str]) -> None:
    if not isinstance(template, str):
        fail(f"{where} must be a string")
    for name in re.findall(r"\{([^{}]*)\}", template):
        if name == "service":
            fail(f"{where}: {{service}} is {{component}} in 5.0")
        if name == "app":
            fail(f"{where}: {{app}} is {{system}} in 5.0")
        if name not in allowed:
            fail(f"{where}: unknown placeholder {{{name}}} (allowed: {', '.join('{' + a + '}' for a in allowed)})")


def check_promote(where: str, env: str, value) -> None:
    """`where` ends in `.promote`. dev follows main; staging may follow the dev smoke test; prod is always manual."""
    if env == "dev":
        fail(f"{where}: dev follows main and takes no promote")
    if env == "staging" and value not in ("after-dev-smoke", "manual"):
        fail(f"{where} must be after-dev-smoke or manual, got '{value}'")
    if env == "prod" and value != "manual":
        fail(f"{where}: prod is always promoted by hand (manual), got '{value}'")


def validate_alerts(obs: dict, where: str) -> None:
    """Alert settings of an environment's observability (spec 092). Only the poll reads them; the manifests do not change."""
    if "alerts" in obs and not isinstance(obs["alerts"], bool):
        fail(f"{where}.alerts must be true or false")
    for key in ("queryURL", "alertmanagerURL"):
        if key in obs and not (isinstance(obs[key], str) and obs[key].startswith(("http://", "https://"))):
            fail(f"{where}.{key} must be an http(s) URL")
    if "querySecret" in obs:
        parts = str(obs["querySecret"]).split("/") if isinstance(obs["querySecret"], str) else []
        if not 1 <= len(parts) <= 2 or not all(DNS_LABEL.match(p) for p in parts):
            fail(f"{where}.querySecret must be <name> or <namespace>/<name>")
    if "rules" not in obs or obs["rules"] is None:
        return
    shape = f"{where}.rules must be a list of {{name, query, severity, summary}}"
    if not isinstance(obs["rules"], list):
        fail(shape)
    seen: set[str] = set()
    for i, rule in enumerate(obs["rules"]):
        if not isinstance(rule, dict):
            fail(f"{where}.rules[{i}] (no name): rules must be a list of {{name, query, severity, summary}}")
        name = rule.get("name")
        label = name if isinstance(name, str) and name.strip() else "no name"

        def bad(problem: str) -> RenderError:
            return RenderError(f"{where}.rules[{i}] ({label}): {problem}")

        if label == "no name":
            raise bad("a rule needs a name")
        if rule.get("severity") not in ALERT_SEVERITIES:
            raise bad(f"severity '{rule.get('severity')}' must be one of {', '.join(ALERT_SEVERITIES)}")
        if name in seen:
            raise bad(f"the name '{name}' is used twice")
        seen.add(name)
        if not isinstance(rule.get("query"), str) or not rule["query"].strip():
            raise bad("query must be a non-empty string (PromQL)")
        if "summary" in rule and not isinstance(rule["summary"], str):
            raise bad("summary must be a string")


def validate_observability(env: str, obs) -> dict:
    where = f"environments.{env}.observability"
    obs = {} if obs is None else obs
    if not isinstance(obs, dict):
        fail(f"{where} must be a mapping, e.g. {{ui: lgtm}}")
    if obs.get("ui", "none") not in ("none", "lgtm"):
        fail(f"{where}.ui must be none or lgtm, got '{obs.get('ui')}'")
    if obs.get("ui") == "lgtm" and obs.get("exportTo"):
        fail(f"{where}: choose either `ui: lgtm` or `exportTo`, not both")
    if obs.get("exportTo") and not str(obs["exportTo"]).startswith(("http://", "https://")):
        fail(f"{where}.exportTo must be an http(s) OTLP/HTTP endpoint")
    if obs.get("headersSecret") and not obs.get("exportTo"):
        fail(f"{where}.headersSecret needs exportTo")
    validate_alerts(obs, where)
    return obs


def validate_environments(doc: dict) -> tuple[dict, dict]:
    """Returns (hosts, per-environment settings): policy mode, observability conf."""
    envs = doc.get("environments")
    names = list(envs) if isinstance(envs, dict) else []
    if names != ENVS:
        fail(f"environments must be dev, staging, prod in this order (the promotion order), got {', '.join(map(str, names)) or 'none'}")
    hosts: dict[str, str] = {}
    conf: dict = {"policy": {}, "observability": {}}
    for e in ENVS:
        c = envs[e] if envs[e] is not None else {}
        if not isinstance(c, dict):
            fail(f"environments.{e} must be a mapping")
        for k in c:
            if k not in ENV_KEYS:
                fail(f"environments.{e}: unknown key '{k}' (allowed: {', '.join(ENV_KEYS)})")
        if c.get("host") is not None:
            check_placeholders(f"environments.{e}.host", c["host"], ["component", "system", "env"])
            hosts[e] = c["host"]
        if c.get("url") is not None and not isinstance(c["url"], str):
            fail(f"environments.{e}.url must be a string")
        v = c.get("policy")
        if v is None:
            conf["policy"][e] = "Enforce" if e == "prod" else "Audit"
        elif v is False or (isinstance(v, str) and v in POLICY_VALUES):    # YAML 1.1 reads an unquoted `off` as false
            conf["policy"][e] = POLICY_VALUES["off" if v is False else v]
        else:
            fail(f"environments.{e}.policy must be audit, enforce or off, got '{v}'")
        if "promote" in c:
            check_promote(f"environments.{e}.promote", e, c["promote"])
        if "observability" in c:
            conf["observability"][e] = validate_observability(e, c["observability"])
    return hosts, conf


def validate_secrets(label: str, secrets: list) -> None:
    seen = set()
    for sec in secrets:
        if not isinstance(sec, dict):
            fail(f"{label}: secrets entries need a name and `generate` or `remote`")
        sn = sec.get("name", "")
        if not isinstance(sn, str) or not SECRET_NAME.match(sn) or len(sn) > 63:
            fail(f"{label}: secret name '{sn}' must be a lowercase DNS label")
        if sn in seen:
            fail(f"{label}: duplicate secret '{sn}'")
        seen.add(sn)
        gen, rem = sec.get("generate") or [], (sec.get("remote") or {}).get("keys") or []
        if bool(gen) == bool(rem):
            fail(f"{label}: secret '{sn}' needs exactly one of `generate: [KEY...]` or `remote: {{keys: [KEY...]}}` "
                 "(generated values must not be refreshed together with fetched ones: use two secrets)")
        for k in [*gen, *rem]:
            if not ENV_KEY.match(str(k)):
                fail(f"{label}: secret '{sn}': '{k}' is not a valid environment variable name")
        if sec.get("remote") and sec["remote"].get("path") is not None:
            check_placeholders(f"{label}.secrets.{sn}.remote.path", sec["remote"]["path"], ["system", "env", "secret", "component"])


def validate_env_vars(label: str, env) -> None:
    if env is not None and not isinstance(env, dict):
        fail(f"{label}: env must be a mapping of names to values")
    for k, v in (env or {}).items():
        if LITERAL_SECRET.search(k) and str(v).strip():
            fail(f"{label}: env {k} looks like a secret; values in system.yaml are committed to git. "
                 f"Declare it under `secrets:` (generate: [{k}] or remote: {{keys: [{k}]}}) instead")


def validate_external(app_dir: Path, s: dict) -> None:
    n = s["name"]
    ext = s["external"] or {}
    if not isinstance(ext, dict):
        fail(f"components.{n}: external needs exactly one of `kustomize: <path>` or `helm: {{repoURL, path}}`")
    kinds = [k for k in ("kustomize", "helm") if k in ext]
    if len(kinds) != 1:
        fail(f"components.{n}: external needs exactly one of `kustomize: <path>` or `helm: {{repoURL, path}}`")
    if "kustomize" in ext:
        for env in s["environments"]:
            pkg = app_dir / str(ext["kustomize"]).format(env=env)
            if not (pkg / "kustomization.yaml").is_file():
                fail(f"components.{n}: {pkg.relative_to(ROOT)}/kustomization.yaml does not exist")
    else:
        helm = ext["helm"] or {}
        for k in ("repoURL", "path"):
            if not helm.get(k):
                fail(f"components.{n}: external.helm needs `{k}`")
    ns = ext.get("namespace")
    if ns is not None and not SECRET_NAME.match(str(ns)):
        fail(f"components.{n}: external.namespace '{ns}' must be a lowercase DNS label")


def names_list(items) -> str:
    return ", ".join(items) or "none"


def str_list(label: str, key: str, value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        fail(f"{label}: {key} must be a list of names")
    return value


def validate_instances(c: str, spec: dict, external: bool) -> dict:
    """The effective settings of every instance: {env: {replicas, resources, env, secrets}}; keys = where the component runs."""
    inst = spec.get("instances")
    if inst is None:
        inst = {}
    if not isinstance(inst, dict):
        fail(f"components.{c}.instances must be a mapping of environments to settings")
    for e in inst:
        if e not in ENVS:
            fail(f"components.{c}.instances: '{e}' is not an environment (environments: {', '.join(ENVS)})")
    declared = [x.get("name") for x in spec.get("secrets") or [] if isinstance(x, dict)]
    out: dict[str, dict] = {}
    for e in (inst or {"dev": {}}):
        i = inst.get(e) if inst else {}
        i = {} if i is None else i
        label = f"components.{c}.instances.{e}"
        if not isinstance(i, dict):
            fail(f"{label} must be a mapping")
        allowed = ["promote"] if external else INSTANCE_KEYS
        for k in i:
            if k not in allowed:
                fail(f"{label}: unknown key '{k}' (allowed: {', '.join(allowed)})")
        if "promote" in i:
            check_promote(f"{label}.promote", e, i["promote"])
        eff: dict = {}
        if external:
            out[e] = eff
            continue
        replicas = i.get("replicas", spec.get("replicas"))
        if replicas is not None:
            if isinstance(replicas, bool) or not isinstance(replicas, int):
                fail(f"{label}.replicas must be a number")
            eff["replicas"] = replicas
        resources = i.get("resources", spec.get("resources"))
        if resources is not None:
            eff["resources"] = resources
        env = {**(spec.get("env") or {}), **(i.get("env") or {})}
        validate_env_vars(label, i.get("env"))
        eff["env"] = env
        secrets = [dict(x) for x in spec.get("secrets") or []]
        override = i.get("secrets") or []
        if not isinstance(override, list):
            fail(f"{label}.secrets must be a list of secrets by name")
        for entry in override:
            name = entry.get("name") if isinstance(entry, dict) else None
            if name not in declared:
                fail(f"{label}.secrets: '{name}' is not a secret of {c} (declared: {names_list(map(str, declared))})")
            at = declared.index(name)
            merged = {**secrets[at], **entry}
            if "generate" in entry and "remote" not in entry:
                merged.pop("remote", None)
            if "remote" in entry and "generate" not in entry:
                merged.pop("generate", None)
            secrets[at] = merged
        validate_secrets(label, secrets)
        eff["secrets"] = secrets
        out[e] = eff
    return out


def validate_component(app_dir: Path, c: str, spec) -> dict:
    """One component as the 4.x service shape plus `environments` (where it runs) and `_eff` (per-environment settings).
    A library comes back with type `library` and none of that."""
    if not isinstance(c, str) or not SECRET_NAME.match(c):
        fail(f"components.{c}: the name must be a lowercase DNS label")
    if not isinstance(spec, dict):
        fail(f"components.{c} must be a mapping")
    if "shape" in spec:
        fail(f"components.{c}: shape is replaced by type and lang in 5.0 (e.g. type: service, lang: python)")
    if "environments" in spec:
        fail(f"components.{c}: environments is replaced by instances in 5.0 (its keys are the environments it runs in)")
    if "promotion" in spec:
        fail(f"components.{c}: promotion is replaced by instances.<env>.promote in 5.0")
    if "replicas" in spec and (isinstance(spec["replicas"], bool) or not isinstance(spec["replicas"], int)):
        fail(f"components.{c}: replicas is one number; per-environment replicas go under instances.<env>.replicas")
    image = spec.get("image")
    last = image.rsplit("/", 1)[-1] if isinstance(image, str) else ""
    if "tag" in spec or "version" in spec or ":" in last or "@" in last:
        what = "tag" if "tag" in spec else "version" if "version" in spec else "the tag of image"
        fail(f"components.{c}: versions are pinned in the overlays, not in system.yaml (remove {what})")
    t, lang = spec.get("type"), spec.get("lang")
    if t is None or lang is None:
        fail(f"components.{c}: type and lang are required (valid: {PAIR_TEXT})")
    if (t, lang) not in VALID_PAIRS:
        fail(f"components.{c}: {t}/{lang} is not a valid type/lang pair (valid: {PAIR_TEXT})")
    provides, consumes = str_list(f"components.{c}", "provides", spec.get("provides")), str_list(f"components.{c}", "consumes", spec.get("consumes"))
    base = {"name": c, "type": t, "lang": lang, "repo": spec.get("repo"), "provides": provides, "consumes": consumes}
    if t == "library":
        bad = [k for k in ("instances", "image", "port", "expose") if k in spec]
        if bad:
            fail(f"components.{c}: a library has no instances (remove {', '.join(bad)})")
        for k in spec:
            if k not in LIBRARY_KEYS:
                fail(f"components.{c}: unknown key '{k}' (allowed: {', '.join(LIBRARY_KEYS)})")
        return base
    external = "external" in spec
    allowed = EXTERNAL_KEYS if external else SERVICE_KEYS
    if external:
        extra = sorted(k for k in spec if k not in allowed)
        if extra:
            fail(f"components.{c} is external: {', '.join(extra)} belongs in its package, not in system.yaml")
        if "image" not in spec:
            fail(f"components.{c}: missing 'image' (the pin applies to it)")
    else:
        for k in spec:
            if k not in allowed:
                fail(f"components.{c}: unknown key '{k}' (allowed: {', '.join(allowed)})")
        for k in ("image", "port", "probes"):
            if k not in spec:
                fail(f"components.{c}: missing '{k}'")
        if not isinstance(spec["probes"], dict) or set(spec["probes"]) != {"liveness", "readiness"}:
            fail(f"components.{c}: probes needs liveness and readiness")
        validate_secrets(f"components.{c}", spec.get("secrets") or [])
        validate_env_vars(f"components.{c}", spec.get("env"))
    svc = {**base, "image": image, "uses": str_list(f"components.{c}", "uses", spec.get("uses"))}
    for k in ("port", "probes", "user", "volumes", "secretRefs", "expose", "metrics", "external"):
        if k in spec:
            svc[k] = spec[k]
    eff = validate_instances(c, spec, external)
    svc["environments"] = list(eff)
    svc["_eff"] = eff
    if external:
        validate_external(app_dir, svc)
    return svc


def validate_apis(doc: dict, comps: list[dict]) -> None:
    apis = doc.get("apis") or {}
    if not isinstance(apis, dict):
        fail("apis must be a mapping of names to {type}")
    for a, conf in apis.items():
        if not isinstance(conf, dict) or conf.get("type") not in API_TYPES:
            fail(f"apis.{a}: type must be openapi, grpc, graphql or events")
        for k in conf:
            if k != "type":
                fail(f"apis.{a}: unknown key '{k}' (allowed: type)")
    for c in comps:
        for key in ("provides", "consumes"):
            for a in c[key]:
                if a not in apis:
                    fail(f"components.{c['name']}: {key} '{a}', which apis: does not declare")
    for a in apis:
        providers = [c["name"] for c in comps if a in c["provides"]]
        if len(providers) != 1:
            fail(f"apis.{a}: provided by {len(providers)} components ({names_list(providers)}); an API has exactly one provider")


def validate_resources(doc: dict) -> dict:
    """Returns {env: conf} of the postgres resource in the shape postgres_cluster reads, or {} without the resource."""
    res = doc.get("resources") or {}
    if not isinstance(res, dict):
        fail("resources must be a mapping of names to resources")
    for r, rc in res.items():
        if r == "observability":
            fail("resources.observability: observability is set per environment under environments.<env>.observability")
        if r != "postgres" or not isinstance(rc, dict) or rc.get("type") != "database":
            fail(f"resources.{r}: 5.0 supports one database resource named postgres (type: database)")
        for k in rc:
            if k not in RESOURCE_KEYS:
                fail(f"resources.postgres: unknown key '{k}' (allowed: {', '.join(RESOURCE_KEYS)})")
        inst = rc.get("instances") or {}
        if not isinstance(inst, dict):
            fail("resources.postgres.instances must be a mapping of environments to {replicas, storage, resources}")
        for e, i in inst.items():
            if e not in ENVS:
                fail(f"resources.postgres.instances: '{e}' is not an environment (environments: {', '.join(ENVS)})")
            for k in i or {}:
                if k not in RESOURCE_INSTANCE_KEYS:
                    fail(f"resources.postgres.instances.{e}: unknown key '{k}' (allowed: {', '.join(RESOURCE_INSTANCE_KEYS)})")
        for where in [rc, *[i or {} for i in inst.values()]]:
            if "replicas" in where and (isinstance(where["replicas"], bool) or not isinstance(where["replicas"], int) or where["replicas"] < 1):
                fail("resources.postgres: replicas must be a number of at least 1")
    pg = res.get("postgres")
    if not pg:
        return {}
    out = {}
    for e in ENVS:
        i = (pg.get("instances") or {}).get(e) or {}
        out[e] = {"version": pg.get("version"), "instances": i.get("replicas", pg.get("replicas")),
                  "storage": i.get("storage", pg.get("storage")), "resources": i.get("resources", pg.get("resources"))}
    return out


def validate_uses(comps: list[dict], resources: dict) -> None:
    libs = [c["name"] for c in comps if c["type"] == "library"]
    for s in comps:
        for u in s.get("uses") or []:
            if u not in resources and u not in libs:
                fail(f"components.{s['name']}: uses '{u}', which is neither a resource nor a library of the system "
                     f"(resources: {names_list(resources)}; libraries: {names_list(libs)})")
        for u in s.get("uses") or []:
            if u not in resources:
                continue
            for env, eff in (s.get("_eff") or {}).items():
                clash = [k for k in ADDONS[u]["env"] if k in (eff.get("env") or {})]
                if clash:
                    fail(f"components.{s['name']}: env {', '.join(clash)} is provided by the '{u}' resource; remove it from `env`")


def load_system(app_dir: Path) -> dict:
    """Parse and validate applications/<system>/system.yaml; returns the normalized system:
    `services` (components in the 4.x service shape, libraries left out), `cfg` (gateway, secretStore, hosts, policy and observability per
    environment, postgres per environment, registries), `libraries`, `system` and `owner`."""
    try:
        return _load_system(app_dir)
    except RenderError as e:
        raise RenderError(f"applications/{app_dir.name}/system.yaml: {e}") from None


def _load_system(app_dir: Path) -> dict:
    d = app_dir.name
    try:
        doc = yaml.safe_load((app_dir / "system.yaml").read_text())
    except yaml.YAMLError as e:
        fail(f"is not valid YAML ({str(e).splitlines()[0] if str(e) else e})")
    if not isinstance(doc, dict):
        fail(f"must be a mapping with {', '.join(TOP_KEYS[:4])}")
    for k in doc:
        if k not in TOP_KEYS:
            fail(f"unknown key '{k}' (allowed: {', '.join(TOP_KEYS)})")
    if doc.get("system") is None:
        fail(f"system is required (the directory is {d})")
    if str(doc["system"]) != d:
        fail(f"system '{doc['system']}' does not match the directory {d}")
    if not isinstance(doc.get("owner"), str) or not doc["owner"].strip():
        fail("owner is required: a non-empty string")
    hosts, env_conf = validate_environments(doc)
    comps_doc = doc.get("components") or {}
    if not isinstance(comps_doc, dict):
        fail("components must be a mapping of names to components")
    comps = [validate_component(app_dir, str(c), spec) for c, spec in comps_doc.items()]
    validate_apis(doc, comps)
    postgres = validate_resources(doc)
    resources = {"postgres": postgres} if postgres else {}
    validate_uses(comps, resources)
    registries = doc.get("registries") or []
    if not isinstance(registries, list):
        fail("registries must be a list of image prefixes, e.g. [ghcr.io/acme/]")
    services = [{**c, "uses": [u for u in c.get("uses") or [] if u in resources]} for c in comps if c["type"] != "library"]
    cfg = {"gateway": doc.get("gateway"), "secretStore": doc.get("secretStore"), "hosts": hosts, "registries": [str(r) for r in registries],
           "policy": env_conf["policy"], "observability": env_conf["observability"], "postgres": postgres}
    return {"system": d, "owner": doc["owner"], "services": services, "libraries": [c["name"] for c in comps if c["type"] == "library"], "cfg": cfg}


# ---------------- manifests ----------------

def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def set_path(doc: dict, dotted: str, value) -> dict:
    """`image.tag` -> {"image": {"tag": value}} merged into doc."""
    keys = dotted.split(".")
    nested: dict = {keys[-1]: value}
    for k in reversed(keys[:-1]):
        nested = {k: nested}
    return deep_merge(doc, nested)


def external_files(app: str, svc: dict, env: str, tag: str, server: str) -> dict[str, dict]:
    """The overlay of an external component: its package (kustomize) or an Argo Application for its chart (helm)."""
    ext = svc["external"]
    image = {"name": svc["image"], "newTag": tag}
    kust: dict = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization"}
    if "kustomize" in ext:
        kust["resources"] = ["../../../" + str(ext["kustomize"]).format(env=env).strip("/")]
        if ext.get("namespace"):
            kust["namespace"] = ext["namespace"]
        kust["images"] = [image]
        return {"kustomization.yaml": kust}
    helm = ext["helm"]
    values = deep_merge(ext.get("values") or {}, (ext.get("envValues") or {}).get(env) or {})
    values = set_path(values, str(helm.get("imageTag") or "image.tag"), tag)
    application = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": f"{svc['name']}-{env}-chart", "namespace": "argocd",
                     "labels": {"app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}},
        "spec": {
            "project": "default",
            "source": {"repoURL": helm["repoURL"], "path": helm["path"], "targetRevision": str(helm.get("revision") or "main"),
                       "helm": {"releaseName": str(helm.get("releaseName") or svc["name"]), "valuesObject": values}},
            "destination": {"server": server, "namespace": ext.get("namespace") or f"{app}-{env}"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True},
                           "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"]},
        },
    }
    kust["resources"] = ["application.yaml"]
    kust["images"] = [image]        # not used by kustomize here; it is where pin.py, promote and status read the pin
    return {"application.yaml": application, "kustomization.yaml": kust}


def per_env(value, env: str, default):
    """A scalar, or a per-environment map ({dev: 1, prod: 3}); missing environments fall back to `default`."""
    return value.get(env, default) if isinstance(value, dict) else (default if value is None else value)


def addons_in(services: list[dict], env: str, cfg: dict) -> list[str]:
    names = {a for s in services if env in envs_of(s) for a in s.get("uses") or []}
    if env in cfg["observability"] and any(env in envs_of(s) for s in services):
        names.add("observability")
    return sorted(names)


def allowed_registries(services: list[dict], cfg: dict, env: str) -> list[str]:
    """Image prefixes the guard rails accept: where our components' images live, the collector's (if the environment has one), and extras."""
    prefixes = {s["image"].rsplit("/", 1)[0] + "/" for s in services}
    if env in cfg["observability"]:
        prefixes.add(ADDONS["observability"]["image"].split("/", 1)[0] + "/")
    prefixes |= set(cfg["registries"])
    return sorted(prefixes)


def guardrail_policy(app: str, env: str, mode: str, registries: list[str]) -> dict:
    """Namespaced Kyverno (CEL) policy every rendered Deployment satisfies; a violation means someone bypassed render.py."""
    import json
    pod = "variables.pod"
    sc = "c.?securityContext"
    rules = [
        ("containers must run as a numeric non-root user (runAsNonRoot: true, runAsUser > 0)",
         f"{pod}.containers.all(c, {sc}.?runAsNonRoot.orValue({pod}.?securityContext.?runAsNonRoot.orValue(false)) && {sc}.?runAsUser.orValue(0) > 0)"),
        ("containers need readOnlyRootFilesystem: true", f"{pod}.containers.all(c, {sc}.?readOnlyRootFilesystem.orValue(false))"),
        ("containers need allowPrivilegeEscalation: false and must drop ALL capabilities",
         f'{pod}.containers.all(c, !{sc}.?allowPrivilegeEscalation.orValue(true) && {sc}.?capabilities.?drop.orValue([]).exists(d, d == "ALL"))'),
        ("privileged containers, host namespaces and hostPath volumes are not allowed",
         f"!{pod}.?hostNetwork.orValue(false) && !{pod}.?hostPID.orValue(false) && {pod}.containers.all(c, !{sc}.?privileged.orValue(false)) && "
         f"{pod}.?volumes.orValue([]).all(v, !has(v.hostPath))"),
        ("containers need CPU and memory requests and a memory limit",
         f"{pod}.containers.all(c, c.?resources.?requests.?cpu.hasValue() && c.?resources.?requests.?memory.hasValue() && c.?resources.?limits.?memory.hasValue())"),
        ("workloads need the label app.kubernetes.io/part-of", 'object.metadata.?labels["app.kubernetes.io/part-of"].hasValue()'),
        ("images must come from an allowed registry: " + ", ".join(registries),
         f"{pod}.containers.all(c, variables.registries.exists(r, c.image.startsWith(r)))"),
    ]
    if env != "dev":
        rules.append(("images must be pinned to a tag other than latest", f'{pod}.containers.all(c, c.image.contains(":") && !c.image.endsWith(":latest"))'))
    return {
        "apiVersion": POLICY_API, "kind": "NamespacedValidatingPolicy",
        "metadata": {"name": "platform-guardrails", "namespace": f"{app}-{env}",
                     "labels": {"app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}},
        "spec": {
            "validationActions": [POLICY_MODES[mode]],
            "matchConstraints": {"resourceRules": [{"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"], "resources": ["deployments"]}]},
            "variables": [{"name": "pod", "expression": "object.spec.template.spec"}, {"name": "registries", "expression": json.dumps(registries)}],
            "validations": [{"expression": e, "message": m} for m, e in rules],
        },
    }


def policy_envs(services: list[dict], cfg: dict) -> list[str]:
    return [env for env in ENVS if cfg["policy"][env] != "Off" and any(env in envs_of(s) for s in services)]


def envs_of(svc: dict) -> list[str]:
    return svc["environments"]


def at(svc: dict, env: str) -> dict:
    """The component as it runs in `env`: instance settings over the component's."""
    return {**svc, **svc["_eff"][env]}


def labels_for(app: str, svc: dict) -> dict:
    # `app` is also the (immutable) selector: it matches what v1 manifests used, so migrating a running Deployment is an in-place update.
    return {"app": svc["name"], "app.kubernetes.io/name": svc["name"], "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}


def replicas_for(svc: dict, env: str) -> int:
    return int(svc.get("replicas", 1))


def addon_env(app: str, svc: dict) -> list[dict]:
    """Connection settings of the resources a component uses, read from the operator's Secret (never copied into git)."""
    out = []
    for a in svc.get("uses") or []:
        secret = ADDONS[a]["secret"].format(app=app)
        out += [{"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}} for var, key in ADDONS[a]["env"].items()]
    return out


def obs_env(app: str, svc: dict, env: str) -> dict[str, str]:
    """Standard OTel variables pointing at the environment's collector (the component's `env` still wins)."""
    return {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_SERVICE_NAME": svc["name"], "OTEL_RESOURCE_ATTRIBUTES": f"service.namespace={app},deployment.environment={env}",
            "OTEL_SDK_DISABLED": "false"}


def deployment(app: str, svc: dict, env: str, obs: bool = False) -> dict:
    labels = labels_for(app, svc)
    selector = {"app": svc["name"]}
    env_vars = {"PORT": str(svc["port"]), **(obs_env(app, svc, env) if obs else {}), **{k: str(v) for k, v in (svc.get("env") or {}).items()}}
    container: dict = {
        "name": "app",
        "image": svc["image"],
        "ports": [{"name": "http", "containerPort": int(svc["port"])}],
        "env": [{"name": k, "value": v} for k, v in sorted(env_vars.items())] + addon_env(app, svc),
    }
    secret_names = [*(svc.get("secretRefs") or []), *[x["name"] for x in svc.get("secrets") or []]]
    if secret_names:
        container["envFrom"] = [{"secretRef": {"name": n}} for n in secret_names]
    if svc.get("resources"):
        container["resources"] = svc["resources"]
    http = lambda path: {"httpGet": {"path": path, "port": "http"}}  # noqa: E731
    container["startupProbe"] = {**http(svc["probes"]["liveness"]), "periodSeconds": 3, "failureThreshold": 40}  # up to 2 min (JVM)
    container["livenessProbe"] = {**http(svc["probes"]["liveness"]), "periodSeconds": 10}
    container["readinessProbe"] = {**http(svc["probes"]["readiness"]), "periodSeconds": 5}
    sc: dict = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    if svc.get("user"):
        sc["runAsUser"] = int(svc["user"])
    container["securityContext"] = sc
    volumes = svc.get("volumes") or {}
    pod: dict = {"securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}, "containers": [container]}
    if volumes:
        container["volumeMounts"] = [{"name": n, "mountPath": p} for n, p in sorted(volumes.items())]
        pod["volumes"] = [{"name": n, "emptyDir": {}} for n in sorted(volumes)]
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": svc["name"], "labels": labels},
        "spec": {"replicas": replicas_for(svc, env), "selector": {"matchLabels": selector},
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    }


def service(app: str, svc: dict) -> dict:
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": svc["name"], "labels": labels_for(app, svc)},
        "spec": {"type": "ClusterIP", "selector": {"app": svc["name"]},
                 "ports": [{"name": "http", "port": 80, "targetPort": "http"}]},
    }


def httproute(app: str, svc: dict, env: str, cfg: dict) -> dict | None:
    expose = svc.get("expose")
    template = cfg["hosts"].get(env)
    if not expose or not template:
        return None
    host = template.format(component=(expose.get("host") if isinstance(expose, dict) else None) or svc["name"], system=app, env=env)
    gw = cfg.get("gateway") or DEFAULT_GATEWAY
    return {
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
        "metadata": {"name": svc["name"], "labels": labels_for(app, svc)},
        "spec": {"parentRefs": [{"name": gw["name"], "namespace": gw.get("namespace", "kube-system")}], "hostnames": [host],
                 "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": (expose.get("path", "/") if isinstance(expose, dict) else "/")}}],
                            "backendRefs": [{"name": svc["name"], "port": 80}]}]},
    }


def secret_manifests(app: str, svc: dict, env: str, cfg: dict) -> dict[str, dict]:
    """ExternalSecrets (+ one Password generator per component) for `secrets:`; values never appear in git."""
    out: dict[str, dict] = {}
    labels = labels_for(app, svc)
    store = cfg.get("secretStore") or DEFAULT_STORE
    for sec in svc.get("secrets") or []:
        name = sec["name"]
        meta = {"name": name, "labels": labels}
        target = {"name": name, "creationPolicy": "Owner", "deletionPolicy": "Retain"}
        if sec.get("generate"):
            gen_ref = {"apiVersion": GEN_API, "kind": "Password", "name": f"{svc['name']}-password"}
            spec = {"refreshInterval": "0", "target": target, "dataFrom": [
                {"sourceRef": {"generatorRef": dict(gen_ref)}, "rewrite": [{"regexp": {"source": "^password$", "target": k}}]}
                for k in sec["generate"]]}
            out[f"externalsecret-{name}.yaml"] = {"apiVersion": ESO_API, "kind": "ExternalSecret", "metadata": meta, "spec": spec}
            out["password-generator.yaml"] = {
                "apiVersion": GEN_API, "kind": "Password", "metadata": {"name": f"{svc['name']}-password", "labels": labels},
                "spec": {"length": int(sec.get("length", 32)), "digits": 5, "symbols": 0, "noUpper": False, "allowRepeat": True}}
        else:
            remote = sec["remote"]
            key = str(remote.get("path") or "{system}-{env}-{secret}").format(system=app, env=env, secret=name, component=svc["name"])
            spec = {"refreshInterval": str(remote.get("refreshInterval", "1h")),
                    "secretStoreRef": {"name": store["name"], "kind": store.get("kind", "ClusterSecretStore")},
                    "target": target,
                    "data": [{"secretKey": k, "remoteRef": {"key": key, "property": k}} for k in remote["keys"]]}
            out[f"externalsecret-{name}.yaml"] = {"apiVersion": ESO_API, "kind": "ExternalSecret", "metadata": meta, "spec": spec}
    return out


def postgres_cluster(app: str, env: str, conf: dict) -> dict:
    d = ADDONS["postgres"]["defaults"]
    db = app.replace("-", "_")
    return {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": f"{app}-postgres", "labels": {"app.kubernetes.io/name": "postgres", "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}},
        "spec": {
            "instances": int(per_env(conf.get("instances"), env, d["instances"])),
            "imageName": f"ghcr.io/cloudnative-pg/postgresql:{per_env(conf.get('version'), env, d['version'])}",
            "storage": {"size": str(per_env(conf.get("storage"), env, d["storage"]))},
            "enableSuperuserAccess": False,
            "bootstrap": {"initdb": {"database": db, "owner": db}},
            "resources": conf.get("resources") or {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
        },
    }


def collector_config(app: str, env: str, conf: dict, services: list[dict]) -> dict:
    scrape = [{"job_name": s["name"], "scrape_interval": "30s", "metrics_path": s["metrics"],
               "static_configs": [{"targets": [f"{s['name']}.{app}-{env}.svc:80"]}]}
              for s in services if env in envs_of(s) and s.get("metrics")]
    receivers: dict = {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}, "http": {"endpoint": "0.0.0.0:4318"}}}}
    if scrape:
        receivers["prometheus"] = {"config": {"scrape_configs": scrape}}
    endpoint = LGTM_ENDPOINT if conf.get("ui") == "lgtm" else conf.get("exportTo")
    exporters: dict = {"debug": {"verbosity": "basic"}}
    names = ["debug"]
    if endpoint:
        backend: dict = {"endpoint": endpoint}
        if conf.get("headersSecret"):
            backend["headers"] = {"Authorization": "${env:AUTHORIZATION}"}
        exporters["otlphttp/backend"] = backend
        names.append("otlphttp/backend")
    processors = {"memory_limiter": {"check_interval": "1s", "limit_percentage": 80, "spike_limit_percentage": 25}, "batch": {},
                  "resource": {"attributes": [{"key": "deployment.environment", "value": env, "action": "upsert"},
                                              {"key": "service.namespace", "value": app, "action": "upsert"}]}}
    chain = lambda: {"processors": ["memory_limiter", "resource", "batch"], "exporters": list(names)}  # noqa: E731  (fresh lists: no YAML anchors)
    return {"extensions": {"health_check": {"endpoint": "0.0.0.0:13133"}}, "receivers": receivers, "processors": processors, "exporters": exporters,
            "service": {"extensions": ["health_check"], "pipelines": {
                "traces": {"receivers": ["otlp"], **chain()},
                "metrics": {"receivers": ["otlp", *(["prometheus"] if scrape else [])], **chain()},
                "logs": {"receivers": ["otlp"], **chain()}}}}


def observability_files(app: str, env: str, conf: dict, services: list[dict]) -> dict[str, dict | str]:
    import hashlib
    labels = {"app.kubernetes.io/name": "otel-collector", "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}
    config = dump(collector_config(app, env, conf, services))
    container: dict = {
        "name": "collector", "image": ADDONS["observability"]["image"], "args": ["--config=/conf/config.yaml"],
        "ports": [{"name": "otlp-grpc", "containerPort": 4317}, {"name": "otlp-http", "containerPort": 4318}, {"name": "health", "containerPort": 13133}],
        "readinessProbe": {"httpGet": {"path": "/", "port": "health"}, "periodSeconds": 5},
        "livenessProbe": {"httpGet": {"path": "/", "port": "health"}, "periodSeconds": 10},
        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "conf", "mountPath": "/conf"}],
    }
    if conf.get("headersSecret"):
        container["envFrom"] = [{"secretRef": {"name": conf["headersSecret"]}}]
    deploy = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "otel-collector", "labels": labels},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "otel-collector"}},
                 "template": {"metadata": {"labels": {**labels, "app": "otel-collector"},
                                           "annotations": {"checksum/config": hashlib.sha256(config.encode()).hexdigest()[:16]}},
                              "spec": {"securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                                       "containers": [container], "volumes": [{"name": "conf", "configMap": {"name": "otel-collector"}}]}}},
    }
    return {
        "configmap.yaml": {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "otel-collector", "labels": labels}, "data": {"config.yaml": config}},
        "collector.yaml": deploy,
        "service.yaml": {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "otel-collector", "labels": labels},
                         "spec": {"type": "ClusterIP", "selector": {"app": "otel-collector"},
                                  "ports": [{"name": "otlp-grpc", "port": 4317, "targetPort": "otlp-grpc"}, {"name": "otlp-http", "port": 4318, "targetPort": "otlp-http"}]}},
        "kustomization.yaml": {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["configmap.yaml", "collector.yaml", "service.yaml"]},
    }


def addon_files(app: str, name: str, env: str, cfg: dict, services: list[dict]) -> dict[str, dict | str]:
    if name == "observability":
        return observability_files(app, env, cfg["observability"][env], services)
    files = {"cluster.yaml": postgres_cluster(app, env, cfg["postgres"][env])}   # one resource today; dispatch on `name` when more exist
    files["kustomization.yaml"] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["cluster.yaml"]}
    return files


def render_app(app_dir: Path, ans: dict) -> dict[Path, str]:
    app = app_dir.name
    system = load_system(app_dir)
    services, cfg = system["services"], system["cfg"]
    org = ans.get("github_org", "ika100")
    server = ans.get("cluster_server", "https://kubernetes.default.svc")
    gitops_repo = f"https://github.com/{org}/{ans.get('project_name', app + '-gitops')}"
    out: dict[Path, str] = {}

    docs = []
    for env in ENVS:
        docs.append({
            "apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
            "metadata": {"name": f"{app}-{env}", "namespace": "argocd"},
            "spec": {
                "generators": [{"list": {"elements": [{"name": s["name"]} for s in services if env in envs_of(s)]}}],
                "template": {
                    "metadata": {"name": "{{name}}-" + env},
                    "spec": {
                        "project": "default",
                        "source": {"repoURL": gitops_repo, "targetRevision": "main", "path": f"applications/{app}/overlays/{env}/" + "{{name}}"},
                        "destination": {"server": server, "namespace": f"{app}-{env}"},
                        "syncPolicy": {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]},
                    },
                },
            },
        })
    for env in ENVS:
        names = addons_in(services, env, cfg)
        if not names:
            continue
        # one ApplicationSet per prune policy: stateful resources (postgres) are never pruned, config-only ones are
        for prune, suffix in ((False, "addons"), (True, "addons-config")):
            group = [n for n in names if bool(ADDONS[n].get("prune", False)) is prune]
            if not group:
                continue
            docs.append({
                "apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
                "metadata": {"name": f"{app}-{suffix}-{env}", "namespace": "argocd"},
                "spec": {
                    "generators": [{"list": {"elements": [{"name": n} for n in group]}}],
                    "template": {
                        "metadata": {"name": "addon-{{name}}-" + env},
                        "spec": {
                            "project": "default",
                            "source": {"repoURL": gitops_repo, "targetRevision": "main", "path": f"applications/{app}/addons/{env}/" + "{{name}}"},
                            "destination": {"server": server, "namespace": f"{app}-{env}"},
                            "syncPolicy": {"automated": {"prune": prune, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]},
                        },
                    },
                },
            })
        for n in names:
            for fname, doc in addon_files(app, n, env, cfg, services).items():
                out[app_dir / "addons" / env / n / fname] = dump(doc) if not isinstance(doc, str) else doc
    for env in policy_envs(services, cfg):
        docs.append({
            "apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
            "metadata": {"name": f"{app}-policies-{env}", "namespace": "argocd"},
            "spec": {
                "generators": [{"list": {"elements": [{"name": "guardrails"}]}}],
                "template": {
                    "metadata": {"name": "policy-{{name}}-" + env},
                    "spec": {
                        "project": "default",
                        "source": {"repoURL": gitops_repo, "targetRevision": "main", "path": f"applications/{app}/policies/{env}"},
                        "destination": {"server": server, "namespace": f"{app}-{env}"},
                        "syncPolicy": {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]},
                    },
                },
            },
        })
        base = app_dir / "policies" / env
        out[base / "policy.yaml"] = dump(guardrail_policy(app, env, cfg["policy"][env], allowed_registries(services, cfg, env)))
        out[base / "kustomization.yaml"] = dump({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["policy.yaml"]})
    out[app_dir / "applicationset.yaml"] = "---\n".join(dump(d) for d in docs)

    out[ROOT / "bootstrap" / f"{app}-root.yaml"] = dump({
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": f"{app}-root", "namespace": "argocd"},
        "spec": {
            "project": "default",
            "source": {"repoURL": gitops_repo, "targetRevision": "main", "path": f"applications/{app}", "directory": {"include": "applicationset.yaml"}},
            "destination": {"server": server, "namespace": "argocd"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True}},
        },
    })

    for env in ENVS:
        for comp in services:
            if env not in envs_of(comp):
                continue
            s = at(comp, env)
            base = app_dir / "overlays" / env / s["name"]
            if "external" in s:
                tag = existing_tag(base / "kustomization.yaml") or "latest"
                for fname, doc in external_files(app, s, env, tag, server).items():
                    out[base / fname] = dump(doc)
                continue
            files = {"deployment.yaml": deployment(app, s, env, env in cfg["observability"]), "service.yaml": service(app, s)}
            route = httproute(app, s, env, cfg)
            if route:
                files["httproute.yaml"] = route
            files.update(secret_manifests(app, s, env, cfg))
            for fname, doc in files.items():
                out[base / fname] = dump(doc)
            tag = existing_tag(base / "kustomization.yaml") or "latest"
            out[base / "kustomization.yaml"] = dump({
                "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                "resources": sorted(files), "images": [{"name": s["image"], "newTag": tag}],
            })
    return out


def stale_paths(app_dir: Path, wanted: set[Path]) -> list[Path]:
    """Overlay component dirs for components/envs no longer declared, and generated files no longer produced."""
    stale: list[Path] = []
    for env in ENVS:
        base = app_dir / "overlays" / env
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            if not any(p.parent == d for p in wanted):
                stale.append(d)
                continue
            stale += [f for f in d.iterdir() if (f.name in MANAGED_FILES or f.name.startswith(MANAGED_PREFIXES)) and f not in wanted]
    for kind in ("addons", "policies"):
        top = app_dir / kind
        if top.is_dir() and not any(top in p.parents for p in wanted):
            stale.append(top)          # nothing of this kind is wanted any more: remove the whole directory
            continue
        for env in ENVS:
            base = app_dir / kind / env
            if not base.is_dir():
                continue
            if kind == "policies":
                if not any(p.parent == base for p in wanted):
                    stale.append(base)
                continue
            for d in base.iterdir():
                if d.is_dir() and not any(p.parent == d for p in wanted):
                    stale.append(d)
    return stale


def unmigrated_dirs() -> list[Path]:
    top = ROOT / "applications"
    if not top.is_dir():
        return []
    return sorted(p for p in top.iterdir() if p.is_dir() and ((p / "app.yaml").is_file() or (p / "services.yaml").is_file()))


def main() -> int:
    check = "--check" in sys.argv[1:]
    old = unmigrated_dirs()
    if old:
        for d in old:
            print(f"ERROR: applications/{d.name}: {NOT_MIGRATED}", file=sys.stderr)
        return 1
    ans = answers()
    apps = sorted(p.parent for p in (ROOT / "applications").glob("*/system.yaml"))
    if not apps:
        print("ERROR: no applications/*/system.yaml found", file=sys.stderr)
        return 1
    drift: list[str] = []
    try:
        plans = [(app_dir, render_app(app_dir, ans)) for app_dir in apps]     # every system is validated before anything is written
    except RenderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for app_dir, rendered in plans:
        for path, content in rendered.items():
            if not path.is_file() or path.read_text() != content:
                drift.append(str(path.relative_to(ROOT)))
                if not check:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
        for st in stale_paths(app_dir, set(rendered)):
            drift.append(f"{st.relative_to(ROOT)} (stale)")
            if not check:
                shutil.rmtree(st) if st.is_dir() else st.unlink()
    if check and drift:
        print("ERROR: rendered files are out of date — run `devbox run render`:", file=sys.stderr)
        for d in drift:
            print(f"  {d}", file=sys.stderr)
        return 1
    print("OK: up to date" if check else f"rendered ({len(drift)} file(s) changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
