#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Render ApplicationSets, overlays and Kubernetes manifests from services.yaml (ADR-014, ADR-017).

The GitOps repo owns every manifest. services.yaml is the human-edited registry; this script derives:
  applications/<app>/applicationset.yaml                       one ApplicationSet per env (dev/staging/prod)
  applications/<app>/overlays/<env>/<service>/                 deployment.yaml, service.yaml, [httproute.yaml], kustomization.yaml
  applications/<app>/addons/<env>/<addon>/                     cluster.yaml, kustomization.yaml (only where a service `uses` the addon)
  applications/<app>/policies/<env>/                           policy.yaml, kustomization.yaml (Kyverno guard rails, when app.yaml has `policies:`)
  bootstrap/<app>-root.yaml                                    root Argo Application (app-of-apps), applied once by a human

A service is only rendered for the environments listed in its `environments` (default: dev), so a new service never
reaches staging/prod before `/gitops:promote` adds it. Existing image pins (`newTag`) are preserved on re-render.

services.yaml entry (written by `/gitops:compose`, every value explicit and reviewable):
  - name: todo-api
    repo: org/todo-api                 # informational: where the image is built
    shape: service-java
    image: ghcr.io/org/todo-api        # the tag is pinned in the overlay (dev: latest)
    port: 8080
    probes: {liveness: /health, readiness: /ready}
    user: 65532                        # numeric non-root UID the image runs as
    volumes: {tmp: /tmp}               # writable emptyDirs (root filesystem is read-only)
    env: {LOG_LEVEL: INFO}             # non-secret wiring, e.g. API_URL: http://todo-api
    uses: [postgres]                   # addons from app.yaml whose connection settings are injected as env vars
    secretRefs: []                     # names of existing Secrets exposed as env vars
    secrets:                           # Secrets created by External Secrets Operator (never values in git, ADR-018)
      - name: todo-api-auth            # the Kubernetes Secret; exposed as env vars
        generate: [DB_PASSWORD, JWT_KEY]   # random values generated in the cluster once per environment
        # length: 32                   # optional (default 32)
      - name: todo-api-stripe
        remote: {keys: [STRIPE_KEY]}   # read from the secret store; optional `path` (default {app}-{env}-<secret name>)
    replicas: {dev: 1, staging: 1, prod: 2}   # or a single integer
    resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {cpu: 500m, memory: 512Mi}}
    expose: {host: todo-api}           # optional: publish through the Gateway (needs app.yaml `hosts`)
    environments: [dev]

applications/<app>/app.yaml (optional):
  gateway: {name: traefik-gateway, namespace: kube-system}
  hosts: {dev: "{service}.{app}-dev.localhost"}     # per env; no entry = not exposed in that env
  secretStore: {name: platform-secrets, kind: ClusterSecretStore}   # where `remote` secrets are read from
  policies: {mode: {dev: Audit, staging: Audit, prod: Enforce}, registries: []}   # Kyverno guard rails per environment (ADR-022)
  addons:                                           # backing services, one per application and environment (ADR-020)
    postgres: {version: 17, instances: {dev: 1, prod: 3}, storage: {dev: 1Gi, prod: 20Gi}}

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
MANAGED_FILES = {"deployment.yaml", "service.yaml", "httproute.yaml", "kustomization.yaml", "password-generator.yaml"}
MANAGED_PREFIXES = ("externalsecret-",)
# Addon contract (ADR-020): what a service gets when it `uses` an addon. Services never see the implementation.
ADDONS = {
    "postgres": {
        "secret": "{app}-postgres-app",    # connection Secret written by the operator (CloudNativePG)
        "env": {"DATABASE_URL": "uri", "PGHOST": "host", "PGPORT": "port", "PGDATABASE": "dbname", "PGUSER": "user", "PGPASSWORD": "password"},
        "defaults": {"version": 17, "instances": 1, "storage": "1Gi"},
    },
    # OpenTelemetry base (ADR-021): applies to every service, no `uses`. A collector per environment receives OTLP and scrapes
    # the services' Prometheus endpoints; a UI/backend is optional (`ui: lgtm`, or `exportTo`).
    "observability": {
        "scope": "all",
        "prune": True,                      # configuration only: removing it removes the collector (postgres keeps its data)
        "image": "otel/opentelemetry-collector-contrib:0.162.0",
        "defaults": {"ui": "none"},
    },
}
POLICY_API = "policies.kyverno.io/v1"
POLICY_MODES = {"Audit": "Audit", "Enforce": "Deny"}     # app.yaml mode -> Kyverno validationActions
LGTM_ENDPOINT = "http://lgtm.observability.svc:4318"    # the shared all-in-one UI stack `cluster-up` installs for `ui: lgtm`
ADDON_FILES = {"cluster.yaml", "kustomization.yaml"}
ESO_API = "external-secrets.io/v1"
GEN_API = "generators.external-secrets.io/v1alpha1"
DEFAULT_STORE = {"name": "platform-secrets", "kind": "ClusterSecretStore"}
SECRET_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LITERAL_SECRET = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL)", re.I)


class RenderError(Exception):
    pass


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


def load_services(app_dir: Path) -> list[dict]:
    services = (yaml.safe_load((app_dir / "services.yaml").read_text()) or {}).get("services") or []
    names = [s.get("name") for s in services]
    if len(names) != len(set(names)):
        raise RenderError(f"duplicate service names in {app_dir.name}/services.yaml")
    for s in services:
        n = s.get("name", "<no name>")
        if "port" not in s or "probes" not in s:
            raise RenderError(
                f"service '{n}' uses the v1 format (remote kustomize base). v2 needs port/probes/...: "
                "run `/gitops:compose add " + n + " --from-k8s` (see docs/ADOPTING.md → migrating from v1 to v2)")
        for k in ("image", "probes"):
            if k not in s:
                raise RenderError(f"service '{n}': missing '{k}'")
        if set(s["probes"]) != {"liveness", "readiness"}:
            raise RenderError(f"service '{n}': probes needs liveness and readiness")
        validate_secrets(s)
        bad = [e for e in s.get("environments", ["dev"]) if e not in ENVS]
        if bad:
            raise RenderError(f"service '{n}': unknown environments {bad} (valid: {ENVS})")
    return services


def validate_secrets(s: dict) -> None:
    n = s["name"]
    seen = set()
    for sec in s.get("secrets") or []:
        sn = sec.get("name", "")
        if not SECRET_NAME.match(sn) or len(sn) > 63:
            raise RenderError(f"service '{n}': secret name '{sn}' must be a lowercase DNS label")
        if sn in seen:
            raise RenderError(f"service '{n}': duplicate secret '{sn}'")
        seen.add(sn)
        gen, rem = sec.get("generate") or [], (sec.get("remote") or {}).get("keys") or []
        if bool(gen) == bool(rem):
            raise RenderError(f"service '{n}': secret '{sn}' needs exactly one of `generate: [KEY...]` or `remote: {{keys: [KEY...]}}` "
                              "(generated values must not be refreshed together with fetched ones: use two secrets)")
        for k in [*gen, *rem]:
            if not ENV_KEY.match(str(k)):
                raise RenderError(f"service '{n}': secret '{sn}': '{k}' is not a valid environment variable name")
    for k, v in (s.get("env") or {}).items():
        if LITERAL_SECRET.search(k) and str(v).strip():
            raise RenderError(f"service '{n}': env {k} looks like a secret; values in services.yaml are committed to git. "
                              f"Declare it under `secrets:` (generate: [{k}] or remote: {{keys: [{k}]}}) instead")


def per_env(value, env: str, default):
    """A scalar, or a per-environment map ({dev: 1, prod: 3}); missing environments fall back to `default`."""
    return value.get(env, default) if isinstance(value, dict) else (default if value is None else value)


def validate_addons(services: list[dict], cfg: dict) -> None:
    declared = cfg.get("addons") or {}
    for name in declared:
        if name not in ADDONS:
            raise RenderError(f"app.yaml: unknown addon '{name}' (available: {', '.join(ADDONS)})")
    for s in services:
        for a in s.get("uses") or []:
            if a not in ADDONS:
                raise RenderError(f"service '{s['name']}': unknown addon '{a}' in `uses` (available: {', '.join(ADDONS)})")
            if ADDONS[a].get("scope") == "all":
                raise RenderError(f"service '{s['name']}': '{a}' applies to every service automatically; remove it from `uses`")
            if a not in declared:
                raise RenderError(f"service '{s['name']}' uses '{a}', which app.yaml does not declare "
                                  f"(add it with `/gitops:addon add {a}`, or under `addons:` in app.yaml)")
            clash = [k for k in ADDONS[a]["env"] if k in (s.get("env") or {})]
            if clash:
                raise RenderError(f"service '{s['name']}': env {', '.join(clash)} is provided by the '{a}' addon; remove it from `env`")


    obs = declared.get("observability")
    if "observability" in declared:
        obs = obs or {}
        if obs.get("ui", "none") not in ("none", "lgtm"):
            raise RenderError(f"app.yaml: observability.ui must be none or lgtm, got '{obs.get('ui')}'")
        if obs.get("ui") == "lgtm" and obs.get("exportTo"):
            raise RenderError("app.yaml: observability: choose either `ui: lgtm` or `exportTo`, not both")
        if obs.get("exportTo") and not str(obs["exportTo"]).startswith(("http://", "https://")):
            raise RenderError("app.yaml: observability.exportTo must be an http(s) OTLP/HTTP endpoint")
        if obs.get("headersSecret") and not obs.get("exportTo"):
            raise RenderError("app.yaml: observability.headersSecret needs exportTo")


def addons_in(services: list[dict], env: str, cfg: dict) -> list[str]:
    names = {a for s in services if env in envs_of(s) for a in s.get("uses") or []}
    if "observability" in (cfg.get("addons") or {}) and any(env in envs_of(s) for s in services):
        names.add("observability")
    return sorted(names)


def policies_conf(cfg: dict) -> dict | None:
    """None = policies off (key absent or false); {} = on with defaults."""
    v = cfg.get("policies")
    return None if v in (None, False) else (v if isinstance(v, dict) else {})


def policy_mode(conf: dict, env: str) -> str:
    return str(per_env(conf.get("mode"), env, "Enforce" if env == "prod" else "Audit"))


def validate_policies(cfg: dict) -> None:
    conf = policies_conf(cfg)
    if conf is None:
        return
    for env in ENVS:
        mode = policy_mode(conf, env)
        if mode not in (*POLICY_MODES, "Off"):
            raise RenderError(f"app.yaml: policies.mode for {env} must be Audit, Enforce or Off, got '{mode}'")
    if not isinstance(conf.get("registries") or [], list):
        raise RenderError("app.yaml: policies.registries must be a list of image prefixes, e.g. [ghcr.io/acme/]")


def allowed_registries(services: list[dict], cfg: dict, conf: dict) -> list[str]:
    """Image prefixes the guard rails accept: where our services' images live, the addons' images, and app.yaml extras."""
    prefixes = {s["image"].rsplit("/", 1)[0] + "/" for s in services}
    if obs_enabled(cfg):
        prefixes.add(ADDONS["observability"]["image"].split("/", 1)[0] + "/")
    prefixes |= {str(r) for r in conf.get("registries") or []}
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


def policy_envs(services: list[dict], conf: dict) -> list[str]:
    return [env for env in ENVS if policy_mode(conf, env) != "Off" and any(env in envs_of(s) for s in services)]


def obs_enabled(cfg: dict) -> bool:
    return "observability" in (cfg.get("addons") or {})


def envs_of(svc: dict) -> list[str]:
    return svc.get("environments") or ["dev"]


def labels_for(app: str, svc: dict) -> dict:
    # `app` is also the (immutable) selector: it matches what v1 manifests used, so migrating a running Deployment is an in-place update.
    return {"app": svc["name"], "app.kubernetes.io/name": svc["name"], "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}


def replicas_for(svc: dict, env: str) -> int:
    r = svc.get("replicas", 1)
    return int(r.get(env, 1)) if isinstance(r, dict) else int(r)


def addon_env(app: str, svc: dict) -> list[dict]:
    """Connection settings of the addons a service uses, read from the operator's Secret (never copied into git)."""
    out = []
    for a in svc.get("uses") or []:
        secret = ADDONS[a]["secret"].format(app=app)
        out += [{"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}} for var, key in ADDONS[a]["env"].items()]
    return out


def obs_env(app: str, svc: dict, env: str) -> dict[str, str]:
    """Standard OTel variables pointing at the environment's collector (services.yaml `env` still wins)."""
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
    template = (cfg.get("hosts") or {}).get(env)
    if not expose or not template:
        return None
    host = template.format(service=(expose.get("host") if isinstance(expose, dict) else None) or svc["name"], app=app, env=env)
    gw = cfg.get("gateway") or {"name": "traefik-gateway", "namespace": "kube-system"}
    return {
        "apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
        "metadata": {"name": svc["name"], "labels": labels_for(app, svc)},
        "spec": {"parentRefs": [{"name": gw["name"], "namespace": gw.get("namespace", "kube-system")}], "hostnames": [host],
                 "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": (expose.get("path", "/") if isinstance(expose, dict) else "/")}}],
                            "backendRefs": [{"name": svc["name"], "port": 80}]}]},
    }


def secret_manifests(app: str, svc: dict, env: str, cfg: dict) -> dict[str, dict]:
    """ExternalSecrets (+ one Password generator per service) for `secrets:`; values never appear in git."""
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
            key = str(remote.get("path") or "{app}-{env}-{secret}").format(app=app, env=env, secret=name, service=svc["name"])
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
    conf = (cfg.get("addons") or {}).get(name) or {}
    if name == "observability":
        return observability_files(app, env, conf, services)
    files = {"cluster.yaml": postgres_cluster(app, env, conf)}   # one addon today; dispatch on `name` when more exist
    files["kustomization.yaml"] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["cluster.yaml"]}
    return files


def render_app(app_dir: Path, ans: dict) -> dict[Path, str]:
    app = app_dir.name
    services = load_services(app_dir)
    cfg_file = app_dir / "app.yaml"
    cfg = (yaml.safe_load(cfg_file.read_text()) or {}) if cfg_file.is_file() else {}
    validate_addons(services, cfg)
    validate_policies(cfg)
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
        # one ApplicationSet per prune policy: stateful addons (postgres) are never pruned, config-only ones are
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
    pconf = policies_conf(cfg)
    if pconf is not None:
        regs = allowed_registries(services, cfg, pconf)
        for env in policy_envs(services, pconf):
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
            out[base / "policy.yaml"] = dump(guardrail_policy(app, env, policy_mode(pconf, env), regs))
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
        for s in services:
            if env not in envs_of(s):
                continue
            base = app_dir / "overlays" / env / s["name"]
            files = {"deployment.yaml": deployment(app, s, env, obs_enabled(cfg)), "service.yaml": service(app, s)}
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
    """Overlay service dirs for services/envs no longer declared, and generated files no longer produced."""
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


def main() -> int:
    check = "--check" in sys.argv[1:]
    ans = answers()
    apps = sorted(p.parent for p in (ROOT / "applications").glob("*/services.yaml"))
    if not apps:
        print("ERROR: no applications/*/services.yaml found", file=sys.stderr)
        return 1
    drift: list[str] = []
    try:
        for app_dir in apps:
            rendered = render_app(app_dir, ans)
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
    except RenderError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if check and drift:
        print("ERROR: rendered files are out of date — run `devbox run render`:", file=sys.stderr)
        for d in drift:
            print(f"  {d}", file=sys.stderr)
        return 1
    print("OK: up to date" if check else f"rendered ({len(drift)} file(s) changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
