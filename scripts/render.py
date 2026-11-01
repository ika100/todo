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
  applications/<system>/autonomy.json                         only when system.yaml declares autonomy (spec 124): what an engine reads
  operators/                                                   Argo CD applications of the operators the system needs (versions table); with the answer
                                                               property_operators: install (.copier-answers.yml, default owner) also External Secrets,
                                                               Kyverno and LGTM (operators/apps/lgtm.yaml, operators/lgtm/); never for operators: owner
  applications/<system>/addons/<env>/<db>/restore-state.yaml  written by `cplat resource restore`/`upgrade`: clusters and archives (server, major)
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
      operators: owner                              # optional, the only value: the platform installs no operator here (spec 115)
      backup: {to: backups, schedule: "0 3 * * *", retention: 30d}   # optional (spec 117): back the environment up to a bucket resource
    staging:
      promote: after-dev-smoke       # after-dev-smoke | manual (default); dev takes none; prod is always promoted by hand
    prod: {}
  autonomy: {paused: true}           # optional: every environment acts as observe (the only key; spec 124)
  # environments.<env>.autonomy (optional; prod at most act; every omitted field has its default; changes no manifest):
  #   level: act                       # observe (default) | advise | propose | act | improve
  #   engine: https://ai-ops.example.com   # http(s) URL of the engine serving this environment
  #   autoMerge: {routes: [fix], maxCriticality: low}                    # low | medium | high
  #   riskGate: {maxFiles: 5, maxLines: 200, regressionTest: true, secondReview: true}
  #   budget: {runsPerDay: 5, mergesPerDay: 2, perRun: {minutes: 60, invocations: 20, reruns: 2, cost: 3}}
  #   window: {days: [mon, tue, wed, thu, fri], from: "09:00", to: "17:00", timezone: UTC}
  #   undoWindow: 24h                  # <n>m | <n>h | <n>d
  # components.<c>.instances.<env>.autonomy: {max: propose}   # caps the instance below its environment
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
      # scale (spec 120): on the component (default for every instance) or on an instance; excludes `replicas` on the same level.
      #   scale: {min: 2, max: 10, on: {cpu: 70}}                          # HPA on CPU utilisation percent (needs resources.requests.cpu)
      #   scale: {max: 6, on: {memory: 80}}                                # HPA on memory utilisation percent; min defaults to 1
      #   scale: {min: 0, max: 8, on: {consumes: orders, lag: 50}}         # KEDA on a broker's events API queue; min 0 = scale to zero
      #   scale: {max: 5, on: {cache: sessions, list: jobs, length: 20}}   # KEDA on a Redis/Valkey list length
      #   min and max are whole numbers, 1..20 (at most 20 replicas); min <= max; min: 0 only with a queue trigger (consumes, cache).
      #   cpu/memory need a metrics server: answer resource_metrics (provided | install) in .copier-answers.yml; install renders it.
    todo-lib: {type: library, lang: python, repo: org/todo-lib}    # a library has no instances and renders nothing
  apis:
    todo-api-rest: {type: openapi}   # openapi | grpc | graphql | events; changes no manifest
    orders: {type: events, broker: bus}   # spec 119: an events API is carried by a broker (optional key while there is one)
  resources:                         # any number, each named, typed (database, bucket, cache, broker)
    orders-db:                       # name: lowercase letters, digits, hyphens; at most 30 characters; not a component name
      type: database                 # required
      version: 17                    # 15 | 16 | 17 | 18 (default 17); instances.<env>.version sets one environment's major (spec 121)
      replicas: 1
      storage: 1Gi
      resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {memory: 512Mi}}
      instances: {prod: {replicas: 3, storage: 20Gi,    # per-environment overrides: external, remote, replicas, storage, resources, backup, version (spec 121:
                                     # the major of that environment, written by /resource:upgrade; held pins live in the rendered files)
                         backup: {to: backups, schedule: "30 2 * * *", retention: 8w}}}   # spec 117: to = a bucket resource; schedule is
                                     # a five-field cron in UTC (default "0 3 * * *"), retention <n>d or <n>w (default 30d)
    sessions:                        # a cache (spec 118): one Valkey node per environment, REDIS_URL and REDIS_* for a using component
      type: cache
      version: 9                     # 8 | 9 (default 9)
      memory: 256Mi                  # default 256Mi, a quantity of at least 64Mi; the server gets 80 % as maxmemory
      maxmemoryPolicy: allkeys-lru   # default; noeviction | allkeys-lru | allkeys-lfu | allkeys-random | volatile-lru | volatile-lfu | ...
      persistence: false             # true = append-only file on a volume (default false)
      storage: 1Gi                   # only with persistence: true
      instances: {prod: {memory: 1Gi, persistence: true, storage: 5Gi}}   # per environment: external, remote, memory, maxmemoryPolicy, persistence, storage, version
    bus:                             # a broker (spec 119): NATS with JetStream; providers and consumers of its events APIs use it
      type: broker                   # without listing it in `uses`; each gets NATS_* from <system>-bus-<component>-app
      version: "2.15"                # "2.14" | "2.15" (default "2.15")
      replicas: 1                    # 1 | 3 (default 1)
      storage: 1Gi                   # JetStream volume per replica (default 1Gi)
      instances: {prod: {replicas: 3, storage: 10Gi}}   # per environment: external, remote, replicas, storage, resources, version
    billing-db:                      # external: runs nothing, its connection Secret is synced from the secretStore
      type: database
      external: true                 # true | false (default false); per environment under instances.<env>.external
      remote: {path: "{system}-{env}-{resource}", refreshInterval: 1h}    # both optional; placeholders {system}, {env}, {resource}
    files:                           # a bucket (spec 116): the bucket is <system>-files; at most 45 characters in-cluster
      type: bucket
      storage: 10Gi                  # default 10Gi
      versioning: false              # true | false (default false)
      instances: {dev: {versioning: true}, prod: {external: true}}   # per environment: external, remote, storage, versioning
    backups:                         # an external bucket that backups write to needs the S3 coordinates in git (spec 117)
      type: bucket
      external: true
      remote: {s3: {endpoint: "https://s3.eu-central-1.amazonaws.com", bucket: acme-backups, region: eu-central-1, forcePathStyle: false}}

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

import hashlib
import json
import os
import re
import shutil
import sys
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENVS = ["dev", "staging", "prod"]
NOT_MIGRATED = "not migrated to 5.0: run `cplat platform update --migrate`"
MANAGED_FILES = {"deployment.yaml", "service.yaml", "httproute.yaml", "kustomization.yaml", "password-generator.yaml", "application.yaml",
                 "hpa.yaml", "scaledobject.yaml", "triggerauthentication.yaml"}
MANAGED_PREFIXES = ("externalsecret-", "credentials-")
TOP_KEYS = ["system", "owner", "environments", "components", "apis", "resources", "gateway", "secretStore", "registries", "autonomy"]
ENV_KEYS = ["host", "url", "policy", "promote", "observability", "operators", "backup", "autonomy"]
# Valid type/lang pairs (the languages of shapes.yml); service/unknown is what the migration writes for a 4.x entry without a known shape.
VALID_PAIRS = [("service", "python"), ("service", "java"), ("service", "go"), ("website", "typescript"), ("library", "python"),
               ("service", "unknown")]
PAIR_TEXT = ", ".join(f"{t}/{lang}" for t, lang in VALID_PAIRS if lang != "unknown")
COMMON_KEYS = ["type", "lang", "repo"]
SERVICE_KEYS = [*COMMON_KEYS, "image", "port", "probes", "user", "volumes", "env", "secretRefs", "secrets", "replicas", "resources",
                "expose", "metrics", "provides", "consumes", "uses", "instances", "scale"]
# Keys an external component may carry: its package owns ports, probes, secrets and exposure (spec 069).
EXTERNAL_KEYS = [*COMMON_KEYS, "image", "external", "provides", "consumes", "instances"]
LIBRARY_KEYS = [*COMMON_KEYS, "provides", "consumes"]
INSTANCE_KEYS = ["replicas", "resources", "env", "promote", "secrets", "autonomy", "scale"]
SCALE_KEYS = ["min", "max", "on"]
SCALE_MAX = 20
ON_FORMS = "{cpu: <percent>}, {memory: <percent>}, {consumes: <api>, lag: <n>} or {cache: <resource>, list: <key>, length: <n>}"
TRIGGER_FORMS = {"cpu": ["cpu"], "memory": ["memory"], "consumes": ["consumes", "lag"], "cache": ["cache", "list", "length"]}
REQUEST_API_TYPES = ("openapi", "grpc", "graphql")
RESOURCE_TYPES = ("database", "bucket", "cache", "broker")
PLANNED_TYPES: dict[str, int] = {}     # types no release runs yet: the spec that brings each (none since spec 119)
RESOURCE_KEYS = ["type", "external", "remote", "version", "replicas", "storage", "resources", "instances"]
RESOURCE_INSTANCE_KEYS = ["external", "remote", "replicas", "storage", "resources", "backup", "version"]
SIZING_KEYS = ["version", "replicas", "storage", "resources"]      # an external resource runs nothing: none of these
BUCKET_KEYS = ["type", "external", "remote", "storage", "versioning", "instances"]
BUCKET_INSTANCE_KEYS = ["external", "remote", "storage", "versioning"]
BUCKET_SIZING_KEYS = ["storage", "versioning"]
CACHE_KEYS = ["type", "external", "remote", "version", "memory", "maxmemoryPolicy", "persistence", "storage", "instances"]
CACHE_INSTANCE_KEYS = ["external", "remote", "memory", "maxmemoryPolicy", "persistence", "storage", "version"]
CACHE_SIZING_KEYS = ["version", "memory", "maxmemoryPolicy", "persistence", "storage"]
CACHE_SUFFIXES = ("-app", "-password")     # the cluster objects a cache creates, next to `<system>-<name>` itself (design.md)
CACHE_VERSIONS = (8, 9)
BROKER_KEYS = ["type", "external", "remote", "version", "replicas", "storage", "resources", "instances"]
BROKER_INSTANCE_KEYS = ["external", "remote", "replicas", "storage", "resources", "version"]
BROKER_SIZING_KEYS = ["version", "replicas", "storage", "resources"]
BROKER_VERSIONS = ("2.14", "2.15")
BROKER_SUFFIXES = ("-headless", "-config", "-nack", "-password")    # the cluster objects a broker creates, next to `<system>-<name>` and the per-component Secrets
BROKER_DEFAULT_RESOURCES = {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}}
MAXMEMORY_POLICIES = ("noeviction", "allkeys-lru", "allkeys-lfu", "allkeys-random", "volatile-lru", "volatile-lfu", "volatile-random",
                      "volatile-ttl")
QUANTITY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$")     # a Kubernetes quantity without milli and exponent forms
QUANTITY_FACTORS = {"": 1, "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
                    "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6}
CACHE_MEMORY_MIN = 64 * 1024 ** 2
REDIS_TYPES = ("redis", "valkey")          # not resource types: the type is `cache`
# The cluster objects an in-cluster bucket's SeaweedFS creates, next to `<system>-<name>` itself (design.md).
BUCKET_SUFFIXES = ("-app", "-master", "-master-peer", "-volume", "-volume-0", "-volume-peer", "-filer", "-filer-peer", "-access-key",
                   "-secret-key")
STORAGE_SIZE = re.compile(r"^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|Ei)?$")    # a Kubernetes size; scripts/cplat/addon.py uses the same pattern
S3_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
BUCKET_NAME_MAX = 45     # the StatefulSet `<name>-volume` plus a pod revision hash must fit a 63-character label
REMOTE_KEYS = ["path", "refreshInterval"]
BUCKET_REMOTE_KEYS = [*REMOTE_KEYS, "s3"]
S3_KEYS = ["endpoint", "bucket", "region", "forcePathStyle"]
BACKUP_KEYS = ["to", "schedule", "retention"]                     # spec 117: environments.<env>.backup and database instances' backup
BACKUP_DEFAULTS = {"schedule": "0 3 * * *", "retention": "30d"}
RETENTION = re.compile(r"^[1-9][0-9]*[dw]$")
CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]        # minute, hour, day of month, month, day of week
CRON_ITEM = re.compile(r"^(\*|[0-9]+|[0-9]+-[0-9]+)(/[0-9]+)?$")
RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
SUPPORTED_VERSIONS = (15, 16, 17, 18)
CNPG_SUFFIXES = ("-app", "-ca", "-replication", "-server", "-superuser", "-rw", "-ro", "-r", "-primary")
# Contract per resource type (ADR-057): the keys of the connection Secret `<system>-<name>-app` and the variables a using component gets.
TYPES = {
    "database": {"keys": ["uri", "host", "port", "dbname", "user", "password"],  # pragma: allowlist secret
                 "env": {"DATABASE_URL": "uri", "PGHOST": "host", "PGPORT": "port", "PGDATABASE": "dbname", "PGUSER": "user",
                         "PGPASSWORD": "password"}},  # pragma: allowlist secret
    "bucket": {"keys": ["endpoint", "bucket", "region", "forcePathStyle", "accessKeyId", "secretAccessKey"],
               "env": {"S3_ENDPOINT": "endpoint", "S3_BUCKET": "bucket", "S3_REGION": "region", "S3_FORCE_PATH_STYLE": "forcePathStyle",
                       "AWS_ACCESS_KEY_ID": "accessKeyId", "AWS_SECRET_ACCESS_KEY": "secretAccessKey", "AWS_REGION": "region",  # pragma: allowlist secret
                       "AWS_ENDPOINT_URL_S3": "endpoint"}},
    "cache": {"keys": ["url", "host", "port", "password"],  # pragma: allowlist secret
              "env": {"REDIS_URL": "url", "REDIS_HOST": "host", "REDIS_PORT": "port", "REDIS_PASSWORD": "password"}},  # pragma: allowlist secret
    "broker": {"keys": ["url", "user", "password"],  # pragma: allowlist secret
               "env": {"NATS_URL": "url", "NATS_USER": "user", "NATS_PASSWORD": "password"}},  # pragma: allowlist secret
}
API_TYPES = ["openapi", "grpc", "graphql", "events"]
POLICY_VALUES = {"audit": "Audit", "enforce": "Enforce", "off": "Off"}     # system.yaml policy -> the 4.x mode
# The versions table (ADR-058, ADR-069, spec 121): every image and chart the renderer writes or the local cluster installs is a row of
# `versions_table()` with an exact version; an image reference is `<repository>:<tag>@sha256:<digest>`. Changing a pin follows spec 121
# and ADR-069 (CHANGELOG lists every changed row). A database image is CloudNativePG's `standard-trixie` build of a minor.
POSTGRES_IMAGES = {
    15: "ghcr.io/cloudnative-pg/postgresql:15.19-202610260819-standard-trixie@sha256:f617efc030d3099362e7fca616bb6d83d102599abf9b7b5e225482e4d0306aa3",
    16: "ghcr.io/cloudnative-pg/postgresql:16.15-202610260820-standard-trixie@sha256:1ec419e997a8bde5263b6951f4c5ebc4aa2e1937927001f2561025c0a816e174",
    17: "ghcr.io/cloudnative-pg/postgresql:17.11-202610260819-standard-trixie@sha256:ed6923f9ba2399cf9070379a353c710c29ced8a294be73986a2c3dae7e599ce3",
    18: "ghcr.io/cloudnative-pg/postgresql:18.6-202610260820-standard-trixie@sha256:39add80f5b1e573e2f6b641c66fd1a2f8dede67ca0330e870285bc1078551390",
}
VALKEY_IMAGES = {         # no bare major, no latest
    8: "valkey/valkey:8.1.11@sha256:8f836008e0986c36adf50da714c7060cf689dbb335f61fe077939d90fa650809",
    9: "valkey/valkey:9.1.3@sha256:3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7",
}
NATS_CHART = {"repo_url": "https://nats-io.github.io/k8s/helm/charts", "chart": "nats", "chart_version": "2.15.1"}   # per broker instance, not an operator
NATS_IMAGES = {           # no bare minor, no latest
    "2.14": "nats:2.14.9-alpine@sha256:127c5ccbc538a93f53ebb19461dc193130df386c2d6dab8199661a28a30d85bd",
    "2.15": "nats:2.15.2-alpine@sha256:823e7f1d6d8ffe3b30dbd73d4a06bbb939acf94929f7fdf224404da7a05bafb5",
}
VELERO_AWS_PLUGIN_IMAGE = "velero/velero-plugin-for-aws:v1.14.4@sha256:c85a7fa803569e3128c33e4f5766f84ab156c8d171c9f1dd2715581d45ce98fa"   # matches the Velero chart's appVersion line (1.14 <-> 1.18)
CREDENTIALS_IMAGE = "python:3.13.18-alpine3.24@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8"   # runs the credentials Job (spec 128): stdlib python only
SEAWEEDFS_IMAGE = "chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8"     # every in-cluster bucket runs its own SeaweedFS of this image
COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.162.0@sha256:39923a8e431bd1f57be82411999d389fcfe40857492e4365456d97a4c1f74be6"
LGTM_IMAGE = "grafana/otel-lgtm:0.35.0@sha256:2de1094c593c671cbfca878a28ffcd7e46ff5e32f2ce966c0cf33003f6cf4266"
CLUSTER_PINS = {          # installed by local-cluster.sh, never rendered
    "argo-cd": {"version": "v3.4.12", "manifest": "https://raw.githubusercontent.com/argoproj/argo-cd/v3.4.12/manifests/install.yaml"},
    "k3s": {"image": "rancher/k3s:v1.32.5-k3s1@sha256:b75eac801bbc91b4584769c840f346683c7740d72f83e630088370f8e1eb2c8a"},
    "gateway-api": {"version": "v1.2.1",
                    "manifest": "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml"},
}
SUPPORTED_MAJORS = {"database": (16, 17, 18), "cache": (8, 9), "broker": ("2.14", "2.15")}   # upgrade targets
DEPRECATED_MAJORS = {"database": (15,)}                                                       # render, upgrade from, never to
UPGRADE_PATHS = {"database": {15: (16, 17, 18), 16: (17, 18), 17: (18,), 18: ()},
                 "cache": {8: (9,), 9: ()}, "broker": {"2.14": ("2.15",), "2.15": ()}}
# The CRDs of every operator chart version below and the versions each serves (`served: true` at the chart's default values), read from the
# chart archives on 2026-10-28; the comment above each constant says where the chart keeps them (`crds/` is installed once by Helm and
# never upgraded, `templates/` follows every upgrade).
# cloudnative-pg: templates/crds/crds.yaml, gate crds.create (default true): installed by the chart, not from crds/
_CRDS_CLOUDNATIVE_PG = {
    "backups.postgresql.cnpg.io": ["v1"],
    "clusterimagecatalogs.postgresql.cnpg.io": ["v1"],
    "clusters.postgresql.cnpg.io": ["v1"],
    "databaseroles.postgresql.cnpg.io": ["v1"],
    "databases.postgresql.cnpg.io": ["v1"],
    "failoverquorums.postgresql.cnpg.io": ["v1"],
    "imagecatalogs.postgresql.cnpg.io": ["v1"],
    "poolers.postgresql.cnpg.io": ["v1"],
    "publications.postgresql.cnpg.io": ["v1"],
    "scheduledbackups.postgresql.cnpg.io": ["v1"],
    "subscriptions.postgresql.cnpg.io": ["v1"],
}
# seaweedfs: templates/crd-seaweed.yaml, gate crds.create (default true)
_CRDS_SEAWEEDFS = {
    "adminscripts.seaweed.seaweedfs.com": ["v1"],
    "bucketlifecyclepolicies.seaweed.seaweedfs.com": ["v1"],
    "bucketnotifications.seaweed.seaweedfs.com": ["v1"],
    "buckets.seaweed.seaweedfs.com": ["v1"],
    "resourcereferencegrants.seaweed.seaweedfs.com": ["v1"],
    "s3credentials.seaweed.seaweedfs.com": ["v1"],
    "s3identities.seaweed.seaweedfs.com": ["v1"],
    "s3oidcproviders.seaweed.seaweedfs.com": ["v1"],
    "s3policies.seaweed.seaweedfs.com": ["v1"],
    "s3policybindings.seaweed.seaweedfs.com": ["v1"],
    "seaweedbackups.seaweed.seaweedfs.com": ["v1"],
    "seaweedcsidrivers.seaweed.seaweedfs.com": ["v1"],
    "seaweedrestores.seaweed.seaweedfs.com": ["v1"],
    "seaweeds.seaweed.seaweedfs.com": ["v1"],
}
# external-secrets: templates/crds/*.yaml, gates installCRDs and crds.create* (default true); v1beta1 is served only with crds.unsafeServeV1Beta1 (default false)
_CRDS_EXTERNAL_SECRETS = {
    "acraccesstokens.generators.external-secrets.io": ["v1alpha1"],
    "beyondtrustworkloadcredentialsdynamicsecrets.generators.external-secrets.io": ["v1alpha1"],
    "cloudsmithaccesstokens.generators.external-secrets.io": ["v1alpha1"],
    "clusterexternalsecrets.external-secrets.io": ["v1"],
    "clustergenerators.generators.external-secrets.io": ["v1alpha1"],
    "clusterpushsecrets.external-secrets.io": ["v1alpha1"],
    "clustersecretstores.external-secrets.io": ["v1"],
    "ecrauthorizationtokens.generators.external-secrets.io": ["v1alpha1"],
    "externalsecrets.external-secrets.io": ["v1"],
    "fakes.generators.external-secrets.io": ["v1alpha1"],
    "gcraccesstokens.generators.external-secrets.io": ["v1alpha1"],
    "generatorstates.generators.external-secrets.io": ["v1alpha1"],
    "githubaccesstokens.generators.external-secrets.io": ["v1alpha1"],
    "gitlabdeploytokens.generators.external-secrets.io": ["v1alpha1"],
    "grafanas.generators.external-secrets.io": ["v1alpha1"],
    "mfas.generators.external-secrets.io": ["v1alpha1"],
    "passwords.generators.external-secrets.io": ["v1alpha1"],
    "pushsecrets.external-secrets.io": ["v1alpha1"],
    "quayaccesstokens.generators.external-secrets.io": ["v1alpha1"],
    "secretstores.external-secrets.io": ["v1"],
    "sshkeys.generators.external-secrets.io": ["v1alpha1"],
    "stssessiontokens.generators.external-secrets.io": ["v1alpha1"],
    "uuids.generators.external-secrets.io": ["v1alpha1"],
    "vaultdynamicsecrets.generators.external-secrets.io": ["v1alpha1"],
    "webhooks.generators.external-secrets.io": ["v1alpha1"],
}
# kyverno: subcharts crds/ and kyverno-api templates, gate crds.install (default true); openreports CRDs need openreports.installCrds (default false); updaterequests v1beta1 is not served
_CRDS_KYVERNO = {
    "cleanuppolicies.kyverno.io": ["v2", "v2beta1"],
    "clustercleanuppolicies.kyverno.io": ["v2", "v2beta1"],
    "clusterpolicies.kyverno.io": ["v1", "v2beta1"],
    "globalcontextentries.kyverno.io": ["v2", "v2alpha1", "v2beta1"],
    "policies.kyverno.io": ["v1", "v2beta1"],
    "policyexceptions.kyverno.io": ["v2", "v2beta1"],
    "updaterequests.kyverno.io": ["v2"],
    "clusterephemeralreports.reports.kyverno.io": ["v1"],
    "ephemeralreports.reports.kyverno.io": ["v1"],
    "clusterpolicyreports.wgpolicyk8s.io": ["v1alpha2"],
    "policyreports.wgpolicyk8s.io": ["v1alpha2"],
    "deletingpolicies.policies.kyverno.io": ["v1", "v1alpha1", "v1beta1"],
    "generatingpolicies.policies.kyverno.io": ["v1", "v1alpha1", "v1beta1"],
    "imagevalidatingpolicies.policies.kyverno.io": ["v1", "v1alpha1", "v1beta1"],
    "mutatingpolicies.policies.kyverno.io": ["v1", "v1alpha1", "v1beta1"],
    "namespaceddeletingpolicies.policies.kyverno.io": ["v1", "v1beta1"],
    "namespacedgeneratingpolicies.policies.kyverno.io": ["v1", "v1beta1"],
    "namespacedimagevalidatingpolicies.policies.kyverno.io": ["v1", "v1beta1"],
    "namespacedmutatingpolicies.policies.kyverno.io": ["v1", "v1beta1"],
    "namespacedvalidatingpolicies.policies.kyverno.io": ["v1", "v1beta1"],
    "policyexceptions.policies.kyverno.io": ["v1", "v1alpha1", "v1beta1"],
    "validatingpolicies.policies.kyverno.io": ["v1", "v1alpha1", "v1beta1"],
}
# plugin-barman-cloud: templates/crds/crds.yaml, gate crds.create (default true)
_CRDS_PLUGIN_BARMAN_CLOUD = {
    "objectstores.barmancloud.cnpg.io": ["v1"],
}
# cert-manager: templates/crd-*.yaml, gate crds.enabled (default false, set true in `values` below)
_CRDS_CERT_MANAGER = {
    "challenges.acme.cert-manager.io": ["v1"],
    "orders.acme.cert-manager.io": ["v1"],
    "certificaterequests.cert-manager.io": ["v1"],
    "certificates.cert-manager.io": ["v1"],
    "clusterissuers.cert-manager.io": ["v1"],
    "issuers.cert-manager.io": ["v1"],
}
# velero: crds/ (Helm installs them once and never upgrades them)
_CRDS_VELERO = {
    "backuprepositories.velero.io": ["v1"],
    "backups.velero.io": ["v1"],
    "backupstoragelocations.velero.io": ["v1"],
    "datadownloads.velero.io": ["v2alpha1"],
    "datauploads.velero.io": ["v2alpha1"],
    "deletebackuprequests.velero.io": ["v1"],
    "downloadrequests.velero.io": ["v1"],
    "podvolumebackups.velero.io": ["v1"],
    "podvolumerestores.velero.io": ["v1"],
    "restores.velero.io": ["v1"],
    "schedules.velero.io": ["v1"],
    "serverstatusrequests.velero.io": ["v1"],
    "volumesnapshotlocations.velero.io": ["v1"],
}
# nack: crds/crds.yml (Helm installs them once and never upgrades them); v1beta1 is not served
_CRDS_NACK = {
    "streams.jetstream.nats.io": ["v1beta2"],   # streamtemplates.jetstream.nats.io is installed but serves no version (v1beta1 served: false), so it has no entry
    "consumers.jetstream.nats.io": ["v1beta2"],
    "accounts.jetstream.nats.io": ["v1beta2"],
    "keyvalues.jetstream.nats.io": ["v1beta2"],
    "objectstores.jetstream.nats.io": ["v1beta2"],
}
# keda: templates/crds/*.yaml, gate crds.install (default true)
_CRDS_KEDA = {
    "cloudeventsources.eventing.keda.sh": ["v1alpha1"],
    "clustercloudeventsources.eventing.keda.sh": ["v1alpha1"],
    "clustertriggerauthentications.keda.sh": ["v1alpha1"],
    "scaledjobs.keda.sh": ["v1alpha1"],
    "scaledobjects.keda.sh": ["v1alpha1"],
    "triggerauthentications.keda.sh": ["v1alpha1"],
}

OPERATORS = {
    "cloudnative-pg": {"repo_url": "https://cloudnative-pg.github.io/charts", "chart": "cloudnative-pg", "chart_version": "0.29.1",
                       "operator_version": "1.30.1", "namespace": "cnpg-system", "release": "cnpg", "crd": "clusters.postgresql.cnpg.io", "crds": _CRDS_CLOUDNATIVE_PG},
    "seaweedfs": {"repo_url": "https://seaweedfs.github.io/seaweedfs-operator", "chart": "seaweedfs-operator", "chart_version": "0.1.45",
                  "operator_version": "1.0.42", "namespace": "seaweedfs-operator", "release": "seaweedfs-operator",
                  "crd": "buckets.seaweed.seaweedfs.com", "crds": _CRDS_SEAWEEDFS},
    # `selector` (optional, default `app.kubernetes.io/name=<chart>`): the operator Deployment whose image tag is the version; used by doctor.
    "external-secrets": {"repo_url": "https://charts.external-secrets.io", "chart": "external-secrets", "chart_version": "2.12.0",
                         "operator_version": "2.12.0", "namespace": "external-secrets", "release": "external-secrets",
                         "crd": "clustersecretstores.external-secrets.io", "crds": _CRDS_EXTERNAL_SECRETS},
    "kyverno": {"repo_url": "https://kyverno.github.io/kyverno", "chart": "kyverno", "chart_version": "3.9.1",
                "operator_version": "1.19.1", "namespace": "kyverno", "release": "kyverno",
                "crd": "namespacedvalidatingpolicies.policies.kyverno.io", "crds": _CRDS_KYVERNO,
                "selector": "app.kubernetes.io/component=admission-controller,app.kubernetes.io/instance=kyverno"},
    # Spec 117: the Barman Cloud plugin (and its hard requirement cert-manager) for database backups, Velero for environment backups.
    "plugin-barman-cloud": {"repo_url": "https://cloudnative-pg.github.io/charts", "chart": "plugin-barman-cloud", "chart_version": "0.8.1",
                            "operator_version": "0.15.1", "namespace": "cnpg-system", "release": "barman-cloud",
                            "crd": "objectstores.barmancloud.cnpg.io", "crds": _CRDS_PLUGIN_BARMAN_CLOUD, "retry": True},
    "cert-manager": {"repo_url": "https://charts.jetstack.io", "chart": "cert-manager", "chart_version": "v1.21.3",
                     "operator_version": "1.21.3", "namespace": "cert-manager", "release": "cert-manager",
                     "crd": "certificates.cert-manager.io", "crds": _CRDS_CERT_MANAGER, "values": {"crds": {"enabled": True}}},
    "velero": {"repo_url": "https://vmware-tanzu.github.io/helm-charts", "chart": "velero", "chart_version": "12.2.1",
               "operator_version": "1.18.2", "namespace": "velero", "release": "velero", "crd": "schedules.velero.io", "crds": _CRDS_VELERO, "finalizer": True,
               "values": {"initContainers": [{"name": "velero-plugin-for-aws", "image": VELERO_AWS_PLUGIN_IMAGE,
                                              "volumeMounts": [{"mountPath": "/target", "name": "plugins"}]}],
                          "deployNodeAgent": True, "snapshotsEnabled": False, "credentials": {"useSecret": False},
                          "configuration": {"backupStorageLocation": [], "volumeSnapshotLocation": [], "uploaderType": "kopia"}}},
    # Spec 119: the NATS JetStream controller (NACK) reconciles the Account, Stream and Consumer objects of in-cluster brokers.
    "nack": {"repo_url": "https://nats-io.github.io/k8s/helm/charts", "chart": "nack", "chart_version": "0.35.0",
             "operator_version": "0.24.0", "namespace": "nack", "release": "nack", "crd": "streams.jetstream.nats.io", "crds": _CRDS_NACK,
             "selector": "app=nack"},   # the chart's useLegacyNames (default true) labels the Deployment app: <release>
    # Spec 120: KEDA scales queue-triggered instances; metrics-server serves cpu/memory to the HPA when the cluster lacks one.
    "keda": {"repo_url": "https://kedacore.github.io/charts", "chart": "keda", "chart_version": "2.19.0",
             "operator_version": "2.19.0", "namespace": "keda", "release": "keda", "crd": "scaledobjects.keda.sh", "crds": _CRDS_KEDA,
             "selector": "app.kubernetes.io/name=keda-operator"},   # the chart names the Deployment keda-operator; the default label would match nothing
    "metrics-server": {"repo_url": "https://kubernetes-sigs.github.io/metrics-server", "chart": "metrics-server",
                       "chart_version": "3.14.0", "operator_version": "0.9.0", "namespace": "kube-system",
                       "release": "metrics-server", "crd": None, "crds": {}, "selector": "app.kubernetes.io/name=metrics-server"},
}
OPERATOR_RETRY = {"limit": 10, "backoff": {"duration": "30s", "factor": 2, "maxDuration": "10m"}}
OPERATOR_OF_TYPE = {"database": "cloudnative-pg", "bucket": "seaweedfs", "broker": "nack"}
DB_DEFAULT_RESOURCES = {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}}
# Resource contract (ADR-020, ADR-057): what a component gets when it `uses` a resource is in TYPES. Components never see the implementation.
ADDONS = {
    # OpenTelemetry base (ADR-021): applies to every component of an environment that has `observability`. A collector per environment
    # receives OTLP and scrapes the components' Prometheus endpoints; a UI/backend is optional (`ui: lgtm`, or `exportTo`).
    "observability": {
        "scope": "all",
        "prune": True,                      # configuration only: removing it removes the collector (a database keeps its data)
        "image": COLLECTOR_IMAGE,
        "defaults": {"ui": "none"},
    },
}
LEVELS = ["observe", "advise", "propose", "act", "improve"]      # autonomy levels (spec 124); each includes the ones before
LEVEL_CAP = {"dev": "improve", "staging": "improve", "prod": "act"}
AUTONOMY_KEYS = ["level", "autoMerge", "riskGate", "budget", "window", "undoWindow", "engine"]
AUTONOMY_DEFAULTS = {
    "autoMerge": {"routes": ["fix"], "maxCriticality": "low"},
    "riskGate": {"maxFiles": 5, "maxLines": 200, "regressionTest": True, "secondReview": True},
    "budget": {"runsPerDay": 5, "mergesPerDay": 2, "perRun": {"minutes": 60, "invocations": 20, "reruns": 2, "cost": 3}},
    "window": {"days": ["mon", "tue", "wed", "thu", "fri"], "from": "09:00", "to": "17:00", "timezone": "UTC"},
    "undoWindow": "24h",
}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CRITICALITIES = ["low", "medium", "high"]
DURATION = re.compile(r"^([1-9][0-9]{0,3})[mhd]$")
HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
POLICY_API = "policies.kyverno.io/v1"
POLICY_MODES = {"Audit": "Audit", "Enforce": "Deny"}     # mode -> Kyverno validationActions
LGTM_ENDPOINT = "http://lgtm.observability.svc:4318"    # the shared all-in-one UI stack `cluster-up` installs for `ui: lgtm`
ADDON_FILES = {"cluster.yaml", "kustomization.yaml", "objectstore.yaml", "scheduledbackup.yaml"}
DATABASE_FILES = {"cluster.yaml", "objectstore.yaml", "scheduledbackup.yaml"}
DATABASE_GENERATION = re.compile(r"^cluster-g[1-9][0-9]*\.yaml$")
ESO_API = "external-secrets.io/v1"
GEN_API = "generators.external-secrets.io/v1alpha1"
DEFAULT_STORE = {"name": "platform-secrets", "kind": "ClusterSecretStore"}
DEFAULT_GATEWAY = {"name": "traefik-gateway", "namespace": "kube-system"}
SECRET_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LITERAL_SECRET = re.compile(r"(PASSWORD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL)", re.I)
ALERT_SEVERITIES = ("critical", "warning", "info")
BROKER_ENV_NAMES = ("NATS_PASSWORD",)     # secret-looking names a broker provides: their clash text wins over the literal-secret text
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
EVENTS_API_NAME = DNS_LABEL     # a bound events API names a stream, a subject token and an object


class RenderError(Exception):
    pass


def fail(text: str):
    raise RenderError(text)


class Literal(str):
    """Platform code embedded in a ConfigMap's `data` (the credentials Job's create.py): written as a YAML literal block whose
    header line carries `# pragma: allowlist secret`. detect-secrets reports a whole YAML value at its key line, and the script's
    alphabet and names look like secrets to it; no credential value is ever rendered (ADR-072)."""


yaml.SafeDumper.add_representer(Literal, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", str(s), style="|"))


def dump(doc: dict) -> str:
    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    data = doc.get("data") if isinstance(doc, dict) else None
    for key, value in (data or {}).items():
        if isinstance(value, Literal):
            header = f"\n  {key}: |\n"
            if header not in text:
                fail(f"internal: the literal block of data.{key} was not written as `{key}: |`")
            text = text.replace(header, f"\n  {key}: |  # pragma: allowlist secret\n", 1)
    return text


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


def check_level(where: str, value) -> None:
    if not isinstance(value, str) or value not in LEVELS:
        fail(f"{where} must be one of {', '.join(LEVELS)}, got '{value}'")


def check_keys(where: str, block, allowed: list[str]) -> dict:
    if not isinstance(block, dict):
        fail(f"{where} must be a mapping")
    for k in block:
        if k not in allowed:
            fail(f"{where}: unknown key '{k}' (allowed: {', '.join(allowed)})")
    return block


def check_int(where: str, value, low: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        fail(f"{where} must be a whole number of at least {low}, got '{value}'")


def check_flag(where: str, value) -> None:
    if not isinstance(value, bool):
        fail(f"{where} must be true or false, got '{value}'")


def clock(where: str, value) -> str:
    """`"HH:MM"`; YAML 1.1 reads an unquoted `17:00` as the number 1020 (minutes), which is accepted as that time."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1440:
        return f"{value // 60:02d}:{value % 60:02d}"
    if not isinstance(value, str) or not HHMM.match(value):
        fail(f'{where} must be "HH:MM", got \'{value}\'')
    return value


def validate_window(where: str, block) -> dict:
    block = check_keys(where, block, list(AUTONOMY_DEFAULTS["window"]))
    win = {**AUTONOMY_DEFAULTS["window"], **block}
    days = win["days"]
    if (not isinstance(days, list) or not days or len(set(map(str, days))) != len(days)
            or any(not isinstance(d, str) or d not in WEEKDAYS for d in days)):
        fail(f"{where}.days must be a non-empty list of {', '.join(WEEKDAYS)} without duplicates, got '{days}'")
    win["from"], win["to"] = clock(f"{where}.from", win["from"]), clock(f"{where}.to", win["to"])
    if win["from"] == win["to"]:
        fail(f"{where}: from and to must differ, got '{win['from']}' twice")
    tz = win["timezone"]
    try:
        if not isinstance(tz, str) or not tz or tz.startswith("/") or ".." in tz:
            raise ValueError(tz)
        ZoneInfo(tz)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        fail(f"{where}.timezone must be an IANA time zone like Europe/Berlin, got '{tz}'")
    return win


def validate_autonomy(env: str, block) -> dict:
    """`environments.<env>.autonomy` (spec 124): the declared level, engine and guardrails, every omitted field at its default."""
    where = f"environments.{env}.autonomy"
    block = check_keys(where, {} if block is None else block, AUTONOMY_KEYS)
    level = block.get("level", "observe")
    check_level(f"{where}.level", level)
    if level == "improve" and LEVEL_CAP[env] == "act":
        fail(f"{where}.level: act is the highest level allowed in {env}, got '{level}'")
    engine = block.get("engine")
    if "engine" in block and (not isinstance(engine, str) or not re.match(r"^https?://[^\s/]+\S*$", engine)):
        fail(f"{where}.engine must be an http(s) URL, got '{engine}'")
    merge = check_keys(f"{where}.autoMerge", block.get("autoMerge", {}), list(AUTONOMY_DEFAULTS["autoMerge"]))
    routes = merge.get("routes", ["fix"])
    if not isinstance(routes, list):
        fail(f"{where}.autoMerge.routes must be a list (allowed: fix)")
    for r in routes:
        if r == "spec":
            fail(f"{where}.autoMerge.routes: spec is refused: a spec always needs a person's approval (allowed: fix)")
        if r != "fix":
            fail(f"{where}.autoMerge.routes: '{r}' is not a route (allowed: fix)")
    crit = merge.get("maxCriticality", "low")
    if crit not in CRITICALITIES:
        fail(f"{where}.autoMerge.maxCriticality must be one of {', '.join(CRITICALITIES)}, got '{crit}'")
    risk = check_keys(f"{where}.riskGate", block.get("riskGate", {}), list(AUTONOMY_DEFAULTS["riskGate"]))
    for k in ("maxFiles", "maxLines"):
        if k in risk:
            check_int(f"{where}.riskGate.{k}", risk[k], 1)
    for k in ("regressionTest", "secondReview"):
        if k in risk:
            check_flag(f"{where}.riskGate.{k}", risk[k])
    budget = check_keys(f"{where}.budget", block.get("budget", {}), list(AUTONOMY_DEFAULTS["budget"]))
    for k in ("runsPerDay", "mergesPerDay"):
        if k in budget:
            check_int(f"{where}.budget.{k}", budget[k], 0)
    per_run = check_keys(f"{where}.budget.perRun", budget.get("perRun", {}), list(AUTONOMY_DEFAULTS["budget"]["perRun"]))
    for k, v in per_run.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            fail(f"{where}.budget.perRun.{k} must be a number above 0, got '{v}'")
    undo = block.get("undoWindow", "24h")
    if not isinstance(undo, str) or not DURATION.match(undo):
        fail(f"{where}.undoWindow must be a duration like 24h, 90m or 2d, got '{undo}'")
    d = AUTONOMY_DEFAULTS
    return {"declared": level, "engine": engine,
            "autoMerge": {**d["autoMerge"], **merge, "routes": list(routes), "maxCriticality": crit},
            "riskGate": {**d["riskGate"], **risk},
            "budget": {**d["budget"], **budget, "perRun": {**d["budget"]["perRun"], **per_run}},
            "window": validate_window(f"{where}.window", block.get("window", {})), "undoWindow": undo}


def validate_pause(doc: dict) -> bool:
    """The top-level `autonomy: {paused: true}`."""
    block = check_keys("autonomy", {} if doc.get("autonomy") is None else doc["autonomy"], ["paused"])
    if "paused" in block and not isinstance(block["paused"], bool):
        fail("autonomy.paused must be true or false")
    return block.get("paused", False)


def lower_level(a: str, b: str) -> str:
    return a if LEVELS.index(a) <= LEVELS.index(b) else b


def autonomy_json(system: str, paused: bool, envs: dict, promote: dict, services: list[dict]) -> str:
    """The rendered `autonomy.json` an engine reads (equal to the autonomy part of `cplat model show --json`)."""
    out_envs: dict[str, dict] = {}
    for e in ENVS:
        declared = envs[e]["declared"]
        effective = "observe" if paused else "act" if declared == "improve" else declared
        out_envs[e] = {**envs[e], "effective": effective, "cap": LEVEL_CAP[e], "paused": paused,
                       "promotion": "main" if e == "dev" else promote.get(e, "manual")}
    instances = {}
    for s in services:
        for e in s["environments"]:
            top = s["_eff"][e].get("autonomy_max")
            eff = out_envs[e]["effective"]
            instances[f"{s['name']}/{e}"] = {"max": top, "effective": eff if top is None else lower_level(eff, top)}
    doc = {"schema": 1, "system": system, "paused": paused, "levels": LEVELS, "environments": out_envs, "instances": instances}
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


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
    conf: dict = {"policy": {}, "observability": {}, "operators": {}, "backup": {}, "autonomy": {}, "promote": {}, "declared": False}
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
        if "operators" in c:
            if c["operators"] != "owner":
                fail(f"environments.{e}.operators must be owner, got '{c['operators']}'")
            conf["operators"][e] = "owner"
        if "backup" in c:
            conf["backup"][e] = c["backup"]       # checked by validate_backups, once the resources are known
        conf["autonomy"][e] = validate_autonomy(e, c.get("autonomy"))
        conf["declared"] = conf["declared"] or "autonomy" in c
        if "promote" in c:
            conf["promote"][e] = c["promote"]
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


def validate_env_vars(label: str, env, defer: bool = True) -> None:
    if env is not None and not isinstance(env, dict):
        fail(f"{label}: env must be a mapping of names to values")
    for k, v in (env or {}).items():
        if k in BROKER_ENV_NAMES and defer:      # validate_uses reports a clash with a broker first, else checks it then
            continue
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
                shown = pkg.relative_to(ROOT) if pkg.is_relative_to(ROOT) else pkg.relative_to(app_dir)
                fail(f"components.{n}: {shown}/kustomization.yaml does not exist")
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


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def yaml_text(v) -> str:
    return yaml.safe_dump(v, default_flow_style=True).strip().removesuffix("\n...").removesuffix("...").strip()


def scale_on_keys(doc: dict) -> None:
    """YAML 1.1 reads an unquoted `on:` as the boolean true; inside a `scale` mapping that key is `on` (as `off` is for a policy)."""
    comps = doc.get("components")
    for c in (comps.values() if isinstance(comps, dict) else []):      # a malformed tree fails later with 114's own message
        if not isinstance(c, dict):
            continue
        insts = c.get("instances")
        for holder in [c, *[i for i in (insts.values() if isinstance(insts, dict) else []) if isinstance(i, dict)]]:
            s = holder.get("scale")
            if isinstance(s, dict) and True in s and "on" not in s:
                holder["scale"] = {("on" if k is True else k): v for k, v in s.items()}


def check_scale(at: str, scale) -> dict:
    """One `scale` mapping (design.md, Render errors); returns {min, max, trigger, on}."""
    where = f"{at}.scale"
    if not isinstance(scale, dict):
        fail(f"{where} must be a mapping with min, max and on, e.g. {{min: 1, max: 5, on: {{cpu: 70}}}}")
    for k in scale:
        if k not in SCALE_KEYS:
            fail(f"{where}: unknown key '{k}' (allowed: {', '.join(SCALE_KEYS)})")
    if "max" not in scale:
        fail(f"{where}: max is required: an integer from 1 to {SCALE_MAX}")
    mx = scale["max"]
    if not is_int(mx) or not 1 <= mx <= SCALE_MAX:
        fail(f"{where}.max must be an integer from 1 to {SCALE_MAX}, got {mx}")
    mn = scale.get("min", 1)
    if not is_int(mn) or not 0 <= mn <= mx:
        fail(f"{where}.min must be an integer from 0 to max ({mx}), got {mn}")
    if "on" not in scale:
        fail(f"{where}.on is required: exactly one of {ON_FORMS}")
    on = scale["on"]
    keys = list(on) if isinstance(on, dict) else None
    trigger = next((t for t, form in TRIGGER_FORMS.items() if keys is not None and set(keys) == set(form)), None)
    if trigger is None:
        got = "{" + ", ".join(str(k) for k in keys) + "}" if keys is not None else yaml_text(on)
        fail(f"{where}.on must be exactly one of {ON_FORMS}, got {got}")
    if trigger in ("cpu", "memory"):
        v = on[trigger]
        if not is_int(v) or not 1 <= v <= 100:
            fail(f"{where}.on.{trigger} must be an integer from 1 to 100 (percent of the {'CPU' if trigger == 'cpu' else 'memory'} request), got {v}")
    if trigger == "consumes" and (not is_int(on["lag"]) or on["lag"] < 1):
        fail(f"{where}.on.lag must be an integer of at least 1, got {on['lag']}")
    if trigger == "cache":
        if not isinstance(on["list"], str) or not on["list"]:
            fail(f"{where}.on.list must be a non-empty string: the key of the list in the cache, got {on['list']}")
        if not is_int(on["length"]) or on["length"] < 1:
            fail(f"{where}.on.length must be an integer of at least 1, got {on['length']}")
    if mn == 0 and trigger in ("cpu", "memory"):
        fail(f"{where}: scale to zero (min: 0) needs a queue trigger (consumes or cache); a {trigger} trigger has nothing to measure at zero replicas")
    return {"min": mn, "max": mx, "trigger": trigger, "on": dict(on)}


EXCLUSIVE = "scale and replicas exclude each other; remove one (replicas is a fixed number, scale lets the autoscaler choose between min and max)"


def validate_instances(c: str, spec: dict, external: bool, comp_scale: dict | None = None) -> dict:
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
        allowed = ["promote", "autonomy"] if external else INSTANCE_KEYS
        for k in i:
            if k not in allowed:
                fail(f"{label}: unknown key '{k}' (allowed: {', '.join(allowed)})")
        if "promote" in i:
            check_promote(f"{label}.promote", e, i["promote"])
        eff: dict = {}
        if "autonomy" in i:
            block = check_keys(f"{label}.autonomy", i["autonomy"] if i["autonomy"] is not None else {}, ["max"])
            if "max" in block:
                check_level(f"{label}.autonomy.max", block["max"])
            eff["autonomy_max"] = block.get("max")
        if external:
            out[e] = eff
            continue
        scale = None
        if "scale" in i:
            if "replicas" in i:
                fail(f"{label}: {EXCLUSIVE}")
            scale = {**check_scale(label, i["scale"]), "source": "instance"}
        elif "replicas" not in i and comp_scale is not None:
            scale = {**comp_scale, "source": "component"}
        eff["scale"] = scale
        replicas = None if scale is not None else i.get("replicas", spec.get("replicas"))
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
    comp_scale = None
    if not external and "scale" in spec:
        if "replicas" in spec:
            fail(f"components.{c}: {EXCLUSIVE}")
        comp_scale = check_scale(f"components.{c}", spec["scale"])
    eff = validate_instances(c, spec, external, comp_scale)
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
        events_api = conf["type"] == "events"
        if "broker" in conf and not events_api:
            fail(f"apis.{a}: only events APIs are carried by a broker (remove broker from this {conf['type']} API)")
        allowed = ["type", "broker"] if events_api else ["type"]
        for k in conf:
            if k not in allowed:
                fail(f"apis.{a}: unknown key '{k}' (allowed: {', '.join(allowed)})")
    for c in comps:
        for key in ("provides", "consumes"):
            for a in c[key]:
                if a not in apis:
                    fail(f"components.{c['name']}: {key} '{a}', which apis: does not declare")
    for a in apis:
        providers = [c["name"] for c in comps if a in c["provides"]]
        if len(providers) != 1:
            fail(f"apis.{a}: provided by {len(providers)} components ({names_list(providers)}); an API has exactly one provider")


def check_s3(where: str, s3) -> None:
    """`remote.s3` of a bucket: the coordinates of an external store that backups read from git (spec 117)."""
    if not isinstance(s3, dict):
        fail(f"{where} must be a mapping with endpoint, bucket, region and forcePathStyle")
    for k in s3:
        if k not in S3_KEYS:
            fail(f"{where}: unknown key '{k}' (allowed: {', '.join(S3_KEYS)})")
    if any(k not in s3 for k in S3_KEYS):
        fail(f"{where} needs endpoint, bucket, region and forcePathStyle")
    if not (isinstance(s3["endpoint"], str) and re.match(r"^https?://[^\s/]+", s3["endpoint"])):
        fail(f"{where}.endpoint must be an http(s) URL")
    if not (isinstance(s3["bucket"], str) and S3_BUCKET_NAME.match(s3["bucket"])):
        fail(f"{where}.bucket must be a valid S3 bucket name")
    if not (isinstance(s3["region"], str) and s3["region"].strip()):
        fail(f"{where}.region must be a non-empty string")
    if not isinstance(s3["forcePathStyle"], bool):
        fail(f"{where}.forcePathStyle must be true or false")


def check_remote(where: str, remote, keys: list[str] = REMOTE_KEYS, placeholders: tuple[str, ...] = ("system", "env", "resource")) -> dict:
    """`remote` of a resource or of one of its instances: {path, refreshInterval}, both optional (a bucket also takes s3)."""
    if remote is None:
        return {}
    if not isinstance(remote, dict):
        fail(f"{where} must be a mapping, e.g. {{path: prod/{{resource}}}}")
    for k in remote:
        if k not in keys:
            fail(f"{where}: unknown key '{k}' (allowed: {', '.join(keys)})")
    if remote.get("path") is not None:
        check_placeholders(f"{where}.path", remote["path"], list(placeholders))
    if remote.get("refreshInterval") is not None and not isinstance(remote["refreshInterval"], str):
        fail(f"{where}.refreshInterval must be a string, e.g. 1h")
    if "s3" in remote:
        check_s3(f"{where}.s3", remote["s3"])
    return remote


def check_replicas(where: str, replicas) -> None:
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
        fail(f"{where}: replicas must be a number of at least 1")


def database_version(r: str, value, where: str | None = None) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or value not in SUPPORTED_VERSIONS:
        fail(f"{where or f'resources.{r}'}: version '{value}' is not a supported database version ({', '.join(map(str, SUPPORTED_VERSIONS))})")
    return value


def cache_version(r: str, value, where: str | None = None) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or value not in CACHE_VERSIONS:
        fail(f"{where or f'resources.{r}'}: version '{value}' is not a supported cache version ({', '.join(map(str, CACHE_VERSIONS))})")
    return value


def broker_version(r: str, value, where: str | None = None) -> str:
    """A NATS line; a YAML float is read as its text, and an instance's float that is no line gets the hint to write a string."""
    raw = value
    value = str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    if not isinstance(value, str) or value not in BROKER_VERSIONS:
        hint = '; write it as a string, e.g. "2.15"' if where and isinstance(raw, float) else ""
        fail(f"{where or f'resources.{r}'}: version '{value}' is not a supported broker version ({', '.join(BROKER_VERSIONS)}){hint}")
    return value


def instance_versions(r: str, typ: str, inst: dict, external: dict[str, bool], default) -> dict[str, int | str]:
    """The effective major of every in-cluster environment: the instance's `version`, else the resource's (`default`). The
    instance values are checked here, in ENVS order, with the texts of the resource-level check under resources.<r>.instances.<env>."""
    check = {"cache": cache_version, "broker": broker_version}.get(typ, database_version)
    out: dict[str, int | str] = {}
    for e in ENVS:
        if external[e]:
            continue
        i = inst.get(e, {})
        out[e] = check(r, i["version"], f"resources.{r}.instances.{e}") if "version" in i else default
    return out


def quantity_bytes(text) -> int:
    """The byte value of a quantity (binary suffixes are powers of 1024, decimal ones of 1000), fractions floored; -1 if it is none."""
    match = QUANTITY.match(str(text)) if isinstance(text, (str, int)) and not isinstance(text, bool) else None
    if not match:
        return -1
    return int(Fraction(match.group(1)) * QUANTITY_FACTORS[match.group(2) or ""])


def check_cache(r: str, rc: dict, inst: dict, external: dict[str, bool]) -> dict[str, int]:
    """Value checks of a cache in the order of design.md (Render errors); returns the Valkey major of every in-cluster environment."""
    versions: dict = {}
    if not all(external.values()):
        versions = instance_versions(r, "cache", inst, external, cache_version(r, rc.get("version", 9)))
    for where, conf in [(f"resources.{r}", rc), *[(f"resources.{r}.instances.{e}", i) for e, i in inst.items()]]:
        if "memory" in conf and quantity_bytes(conf["memory"]) < CACHE_MEMORY_MIN:
            fail(f"{where}: memory '{conf['memory']}' must be a Kubernetes quantity of at least 64Mi, e.g. 256Mi")
        if "maxmemoryPolicy" in conf and conf["maxmemoryPolicy"] not in MAXMEMORY_POLICIES:
            fail(f"{where}: maxmemoryPolicy '{conf['maxmemoryPolicy']}' is not allowed ({', '.join(MAXMEMORY_POLICIES)})")
        if "persistence" in conf and not isinstance(conf["persistence"], bool):
            fail(f"{where}: persistence must be true or false")
        if "storage" in conf and not (isinstance(conf["storage"], str) and STORAGE_SIZE.match(conf["storage"])):
            fail(f"{where}: storage must be a size such as 1Gi")

    def persistent(e: str) -> bool:
        return inst.get(e, {}).get("persistence", rc.get("persistence", False))

    for e in ENVS:
        if "storage" in inst.get(e, {}) and not external[e] and not persistent(e):
            fail(f"resources.{r}.instances.{e}: storage needs persistence: true (persistence is false in {e})")
    if "storage" in rc and not any(persistent(e) for e in ENVS if not external[e]):
        fail(f"resources.{r}: storage needs persistence: true in at least one environment")
    return versions


def check_broker(r: str, rc: dict, inst: dict, external: dict[str, bool]) -> dict[str, str]:
    """Value checks of a broker in the order of design.md (Render errors); returns the NATS line of every in-cluster environment."""
    versions: dict = {}
    if not all(external.values()):
        versions = instance_versions(r, "broker", inst, external, broker_version(r, rc.get("version", "2.15")))
    for where, conf in [(f"resources.{r}", rc), *[(f"resources.{r}.instances.{e}", i) for e, i in inst.items()]]:
        if "replicas" in conf and (isinstance(conf["replicas"], bool) or not isinstance(conf["replicas"], int) or conf["replicas"] not in (1, 3)):
            fail(f"{where}: a broker runs with 1 or 3 replicas (replicas: {conf['replicas']})")
        if "storage" in conf and not (isinstance(conf["storage"], str) and STORAGE_SIZE.match(conf["storage"])):
            fail(f"{where}: storage must be a size such as 1Gi")
        if "resources" in conf and not isinstance(conf["resources"], dict):
            fail(f"{where}: resources must be a mapping with requests and limits, e.g. {{requests: {{cpu: 100m, memory: 256Mi}}, limits: {{memory: 512Mi}}}}")
    return versions


def check_bucket(system: str, r: str, rc: dict, inst: dict, in_cluster: bool) -> None:
    """Value checks of a bucket (design.md, Render errors): versioning, storage, then the S3 name and length rule."""
    for where, conf in [(f"resources.{r}", rc), *[(f"resources.{r}.instances.{e}", i) for e, i in inst.items()]]:
        if "versioning" in conf and not isinstance(conf["versioning"], bool):
            fail(f"{where}: versioning must be true or false")
        if "storage" in conf and not (isinstance(conf["storage"], str) and STORAGE_SIZE.match(conf["storage"])):
            fail(f"{where}: storage must be a size such as 10Gi")
    if in_cluster:
        name = f"{system}-{r}"
        if not S3_BUCKET_NAME.match(name):
            fail(f"resources.{r}: '{name}' is not a valid S3 bucket name (3 to 63 characters, lowercase letters, digits and hyphens, "
                 "starting and ending with a letter or digit)")
        if len(name) > BUCKET_NAME_MAX:
            fail(f"resources.{r}: '{name}' is too long for the in-cluster store (at most {BUCKET_NAME_MAX} characters)")


IMAGE_MAJOR = {"database": re.compile(r"postgresql:(\d+)"), "cache": re.compile(r"valkey:(\d+)"), "broker": re.compile(r"nats:(\d+\.\d+)")}


def table_pin(typ: str, major) -> dict:
    """The versions table's pin of a major: {pin} (an image reference), and for a broker the chart version too."""
    if typ == "database":
        return {"pin": POSTGRES_IMAGES[major]}
    if typ == "cache":
        return {"pin": VALKEY_IMAGES[major]}
    if typ == "broker":
        return {"pin": NATS_IMAGES[major], "chart_version": NATS_CHART["chart_version"]}
    return {"pin": SEAWEEDFS_IMAGE}


def rendered_pin(path: Path, typ: str) -> dict | None:
    """The engine image (and a broker's chart version) of a rendered instance file; None when it is absent or does not read as one."""
    try:
        doc = yaml.safe_load(path.read_text())
        if typ == "database":
            image = doc["spec"]["imageName"]
            extra: dict = {}
        elif typ == "cache":
            image = next(c["image"] for c in doc["spec"]["template"]["spec"]["containers"] if c["name"] == "valkey")
            extra = {}
        elif typ == "broker":
            source = doc["spec"]["source"]
            ref = source["helm"]["valuesObject"]["container"]["image"]
            image = f"{ref['repository']}:{ref['tag']}"
            extra = {"chart_version": source["targetRevision"]}
            if not isinstance(extra["chart_version"], str):
                return None
        else:
            image = doc["spec"]["image"]
            extra = {}
    except (OSError, yaml.YAMLError, KeyError, IndexError, TypeError, StopIteration, AttributeError):
        return None
    return {"pin": image, **extra} if isinstance(image, str) and image else None


def held_pin(app_dir: Path | None, system: str, r: str, typ: str, env: str, major) -> dict:
    """The pin of an in-cluster instance (design.md, Pin of an in-cluster instance): the table's pin of its major in dev, with no rendered
    file, or when the rendered image is of another major; otherwise (staging, prod) the image the existing rendered file already pins."""
    table = table_pin(typ, major if typ != "bucket" else None)
    if app_dir is None or env == "dev":
        return table
    folder = app_dir / "addons" / env / r
    if typ == "database":
        base = f"{system}-{r}"
        try:
            active = next(c["name"] for c in read_restore_state(app_dir, env, r)[0] if c["role"] == "active")
        except (RenderError, StopIteration):
            return table
        file = "cluster.yaml" if active == base else f"cluster-g{active[len(base) + 2:]}.yaml"
    else:
        file = {"cache": "statefulset.yaml", "broker": "server.yaml", "bucket": "seaweed.yaml"}[typ]
    held = rendered_pin(folder / file, typ)
    if held is None:
        return table
    if typ != "bucket":
        found = IMAGE_MAJOR[typ].search(held["pin"])
        if not found or (found.group(1) if typ == "broker" else int(found.group(1))) != major:
            return table
    return held


def validate_resource(doc: dict, r: str, rc, app_dir: Path | None = None) -> dict:
    """One resource, in the check order of design.md; returns {type, external, envs: {env: effective values}}."""
    types = f"({', '.join(RESOURCE_TYPES[:-1])} or {RESOURCE_TYPES[-1]})"
    if r == "observability":
        fail("resources.observability: observability is set per environment under environments.<env>.observability")
    if not isinstance(r, str) or not RESOURCE_NAME.match(r) or len(r) > 30:
        fail(f"resources.{r}: the name must start with a lowercase letter and contain only lowercase letters, digits and hyphens "
             "(at most 30 characters)")
    if not isinstance(rc, dict) or rc.get("type") is None:
        fail(f"resources.{r}: type is required {types}")
    typ = rc["type"]
    if typ in REDIS_TYPES:
        fail(f"resources.{r}: type '{typ}' is not a resource type: the type is `cache` (Redis-compatible, runs Valkey)")
    if typ not in RESOURCE_TYPES:
        fail(f"resources.{r}: type '{typ}' is not a resource type {types}")
    if typ in PLANNED_TYPES:
        fail(f"resources.{r}: type {typ} is not available yet: spec {PLANNED_TYPES[typ]} brings it")
    bucket = typ == "bucket"
    cache = typ == "cache"
    brk = typ == "broker"
    keys = BUCKET_KEYS if bucket else CACHE_KEYS if cache else BROKER_KEYS if brk else RESOURCE_KEYS
    instance_keys = BUCKET_INSTANCE_KEYS if bucket else CACHE_INSTANCE_KEYS if cache else BROKER_INSTANCE_KEYS if brk else RESOURCE_INSTANCE_KEYS
    sizing_keys = BUCKET_SIZING_KEYS if bucket else CACHE_SIZING_KEYS if cache else BROKER_SIZING_KEYS if brk else SIZING_KEYS
    for k in rc:
        if k not in keys:
            fail(f"resources.{r}: unknown key '{k}' (allowed: {', '.join(keys)})")
    if "external" in rc and not isinstance(rc["external"], bool):
        fail(f"resources.{r}: external must be true or false")
    inst = rc.get("instances")
    inst = {} if inst is None else inst
    if not isinstance(inst, dict):
        fail(f"resources.{r}.instances must be a mapping of environments to {{{', '.join(instance_keys)}}}")
    base_external = rc.get("external", False)
    external: dict[str, bool] = {}
    for e, i in inst.items():
        if e not in ENVS:
            fail(f"resources.{r}.instances: '{e}' is not an environment (environments: {', '.join(ENVS)})")
        where = f"resources.{r}.instances.{e}"
        if i is not None and not isinstance(i, dict):
            fail(f"{where} must be a mapping")
        i = i or {}
        if "external" in i and not isinstance(i["external"], bool):
            fail(f"{where}: external must be true or false")
        if typ == "database" and "backup" in i and i.get("external", base_external) is True:
            fail(f"{where}: an external database is backed up by its provider (remove backup)")
        if i.get("external", base_external):
            for k in sizing_keys:
                if k in i:
                    fail(f"{where}: an external {typ} is sized and configured at its provider (remove {k})" if bucket or cache
                         else f"{where}: an external resource runs nothing (remove {k})")
        for k in i:
            if k not in instance_keys:
                fail(f"{where}: unknown key '{k}' (allowed: {', '.join(instance_keys)})")
    inst = {e: i or {} for e, i in inst.items()}
    for e in ENVS:
        external[e] = inst.get(e, {}).get("external", base_external)
    if all(external.values()):
        for k in sizing_keys:
            if k in rc:
                fail(f"resources.{r}: external in every environment: an external {typ} is sized and configured at its provider "
                     f"(remove {k})" if bucket or cache else
                     f"resources.{r}: external in every environment: an external resource runs nothing (remove {k})")
    if "remote" in rc and not any(external.values()):
        fail(f"resources.{r}: remote needs external: true")
    for e, i in inst.items():
        if "remote" in i and not external[e]:
            fail(f"resources.{r}.instances.{e}: remote needs external: true in {e}")
    remote_keys = BUCKET_REMOTE_KEYS if bucket else REMOTE_KEYS
    holders = ("system", "env", "resource", "component") if brk else ("system", "env", "resource")
    check_remote(f"resources.{r}.remote", rc.get("remote"), remote_keys, holders)
    for e, i in inst.items():
        check_remote(f"resources.{r}.instances.{e}.remote", i.get("remote"), remote_keys, holders)
    first = next((e for e in ENVS if external[e]), None)
    if first and not doc.get("secretStore"):
        fail(f"resources.{r}: external in {first} needs the system's secretStore (declare secretStore: {{name, kind}})")
    versions: dict = {}
    if bucket:
        check_bucket(system=str(doc.get("system")), r=r, rc=rc, inst=inst, in_cluster=not all(external.values()))
    elif cache:
        versions = check_cache(r, rc, inst, external)
    elif brk:
        versions = check_broker(r, rc, inst, external)
    elif not all(external.values()):
        versions = instance_versions(r, typ, inst, external, database_version(r, rc.get("version", 17)))
    for where, conf in [(f"resources.{r}", rc), *[(f"resources.{r}.instances.{e}", i) for e, i in inst.items()]]:
        if "replicas" in conf and not brk:
            check_replicas(f"resources.{r}", conf["replicas"])
    envs: dict[str, dict] = {}
    for e in ENVS:
        i = inst.get(e, {})
        if external[e]:
            remote = {**(check_remote("", rc.get("remote"), remote_keys, holders)), **check_remote("", i.get("remote"), remote_keys, holders)}
            key = str(remote.get("path") or ("{system}-{env}-{resource}-{component}" if brk else "{system}-{env}-{resource}")).format(
                system=str(doc.get("system")), env=e, resource=r, component="{component}")
            envs[e] = {"external": True, "version": None, "pin": None, "replicas": None, "storage": None, "resources": None,
                       "store_key": key, "refreshInterval": str(remote.get("refreshInterval") or "1h")}
            if bucket:
                s3 = (i.get("remote") or {}).get("s3") or (rc.get("remote") or {}).get("s3")
                envs[e]["versioning"] = None
                envs[e]["s3"] = dict(s3) if s3 else None
            elif cache:
                envs[e].update({"memory": None, "maxmemory": None, "maxmemoryPolicy": None, "persistence": None})
            elif typ == "database":
                envs[e]["backup"] = None
            continue
        if cache:
            memory = str(i.get("memory", rc.get("memory", "256Mi")))
            persistence = i.get("persistence", rc.get("persistence", False))
            envs[e] = {"external": False, "version": versions[e], "replicas": None,
                       "storage": str(i.get("storage", rc.get("storage", "1Gi"))) if persistence else None, "resources": None,
                       "memory": memory, "maxmemory": quantity_bytes(memory) * 4 // 5,
                       "maxmemoryPolicy": i.get("maxmemoryPolicy", rc.get("maxmemoryPolicy", "allkeys-lru")),
                       "persistence": persistence, "store_key": None, "refreshInterval": None}
            continue
        if bucket:
            envs[e] = {"external": False, "version": None, "pin": None, "replicas": None, "storage": str(i.get("storage", rc.get("storage", "10Gi"))),
                       "resources": None, "versioning": i.get("versioning", rc.get("versioning", False)),
                       "store_key": None, "refreshInterval": None, "s3": None}
            continue
        if brk:
            storage = i.get("storage", rc.get("storage", "1Gi"))
            envs[e] = {"external": False, "version": versions[e], "replicas": i.get("replicas", rc.get("replicas", 1)), "storage": str(storage),
                       "resources": i.get("resources", rc.get("resources")), "store_key": None, "refreshInterval": None}
            continue
        storage = i.get("storage", rc.get("storage"))
        envs[e] = {"external": False, "version": versions[e], "replicas": int(i.get("replicas", rc.get("replicas", 1))),
                   "storage": "1Gi" if storage is None else str(storage), "resources": i.get("resources", rc.get("resources")),
                   "store_key": None, "refreshInterval": None, "backup": None}
    for e, env in envs.items():
        if not env["external"]:
            env.update(held_pin(app_dir, str(doc.get("system")), r, typ, e, env["version"]))
    return {"type": typ, "external": bool(base_external), "envs": envs}


def validate_resources(doc: dict, app_dir: Path | None = None) -> dict:
    """The normalised resources: {name: {type, external, envs: {env: {external, version, replicas, storage, resources, store_key,
    refreshInterval}}}}, with the effective values of all three environments (design.md, Renderer API)."""
    res = doc.get("resources") or {}
    if not isinstance(res, dict):
        fail("resources must be a mapping of names to resources")
    return {r: validate_resource(doc, r, rc, app_dir) for r, rc in res.items()}


def object_names(system: str, r: str, typ: str, users: tuple[str, ...] | list[str] = ()) -> list[str]:
    """The cluster objects a resource creates (design.md): a database the CloudNativePG family, a broker its chart objects and one Secret
    per participating component (`users`), any other type the object and its Secret."""
    base = f"{system}-{r}"
    if typ == "broker":
        return [base, *(base + s for s in BROKER_SUFFIXES), *(f"{base}-{c}-app" for c in users)]
    return [base, *(base + s for s in (CNPG_SUFFIXES if typ == "database" else BUCKET_SUFFIXES if typ == "bucket" else CACHE_SUFFIXES if typ == "cache" else ("-app",)))]


def broker_users(events: dict, comps: list[dict], broker_name: str) -> list[str]:
    """The components (not libraries) that provide or consume an events API the broker carries, in `components:` order."""
    return [c["name"] for c in comps if c["type"] != "library"
            and any(events.get(a, {}).get("broker") == broker_name for a in [*c["provides"], *c["consumes"]])]


def bind_events(doc: dict, comps: list[dict], resources: dict) -> dict:
    """Binds every events API to a broker (design.md, Binding); returns {api: {broker, provider, consumers}} in `apis:` order.
    An API with no `broker:` in a system without any broker stays unbound (`broker` None); the render prints a note."""
    brokers = [r for r, rc in resources.items() if rc["type"] == "broker"]
    out: dict[str, dict] = {}
    for a, conf in (doc.get("apis") or {}).items():
        if conf["type"] != "events":
            continue
        bound = None
        if "broker" in conf:
            bound = conf["broker"]
            if not isinstance(bound, str) or bound not in brokers:
                fail(f"apis.{a}: broker '{bound}' is not a broker resource of the system (brokers: {names_list(brokers)})")
        elif len(brokers) > 1:
            fail(f"apis.{a}: the system has several brokers ({', '.join(brokers)}): choose one with apis.{a}.broker")
        elif brokers:
            bound = brokers[0]
        if bound is not None and not EVENTS_API_NAME.match(str(a)):
            fail(f"apis.{a}: an events API carried by a broker needs a lowercase DNS label as its name "
                 f"(it names the stream {doc.get('system')}-{a} and the subjects {doc.get('system')}.{a}.>)")
        run = [c for c in comps if c["type"] != "library"]
        out[a] = {"broker": bound, "provider": next((c["name"] for c in run if a in c["provides"]), None),
                  "consumers": [c["name"] for c in run if a in c["consumes"]]}
    return out


def effective_uses(c: dict, resources: dict, events: dict) -> list[str]:
    """The declared non-broker resources in `uses` order, then the brokers of the component's events APIs (provides, then consumes), once each."""
    out = [u for u in c.get("uses") or [] if u in resources and resources[u]["type"] != "broker"]
    for a in [*c["provides"], *c["consumes"]]:
        b = events.get(a, {}).get("broker")
        if b and b not in out:
            out.append(b)
    return out


def check_resource_clashes(system: str, resources: dict, comps_doc: dict, comps: list[dict], events: dict | None = None) -> None:
    for r in resources:
        if r in comps_doc:
            fail(f"resources.{r}: components.{r} has the same name; a resource and a component need different names")
    names = list(resources)
    for n, a in enumerate(names):
        for b in names[n + 1:]:
            names_a = object_names(system, a, resources[a]["type"], broker_users(events or {}, comps, a))
            names_b = object_names(system, b, resources[b]["type"], broker_users(events or {}, comps, b))
            both = [o for o in names_a if o in names_b]
            if both:
                fail(f"resources.{a} and resources.{b} would both create the object {both[0]}; rename one")
    connection = {f"{system}-{r}-app": r for r in resources if resources[r]["type"] != "broker"}
    for r in resources:
        if resources[r]["type"] == "broker":
            connection.update({f"{system}-{r}-{c}-app": r for c in broker_users(events or {}, comps, r)})
    for c in comps:
        for sec in (comps_doc.get(c["name"]) or {}).get("secrets") or []:
            if isinstance(sec, dict) and sec.get("name") in connection:
                fail(f"components.{c['name']}: secret '{sec['name']}' is the connection Secret of resources.{connection[sec['name']]}; rename it")


def variable_names(uses: list[str], resources: dict) -> list[tuple[str, list[str]]]:
    """(resource, variable names) per used resource in `uses` order: the bare names when the component uses one resource of that type,
    otherwise `<NAME>_<VAR>` (AC-115.8)."""
    used = [u for u in uses if u in resources]
    out = []
    for u in used:
        typ = resources[u]["type"]
        many = sum(1 for x in used if resources[x]["type"] == typ) > 1
        prefix = u.upper().replace("-", "_") + "_" if many else ""
        out.append((u, [prefix + v for v in TYPES[typ]["env"]]))
    return out


def validate_uses(comps: list[dict], resources: dict, events: dict | None = None) -> None:
    events = events or {}
    libs = [c["name"] for c in comps if c["type"] == "library"]
    for s in comps:
        for u in s.get("uses") or []:
            if u not in resources and u not in libs:
                fail(f"components.{s['name']}: uses '{u}', which is neither a resource nor a library of the system "
                     f"(resources: {names_list(resources)}; libraries: {names_list(libs)})")
        if s["type"] == "library":
            continue
        effective = effective_uses(s, resources, events)
        for u in s.get("uses") or []:
            if u in resources and resources[u]["type"] == "broker" and u not in effective:
                fail(f"components.{s['name']}: uses '{u}', a broker; a component reaches a broker through an events API it provides or "
                     f"consumes, and none of {s['name']}'s is carried by {u}")
        names_by = dict(variable_names(effective, resources))
        first_broker = next((u for u in effective if resources[u]["type"] == "broker"), None)
        for u in effective:
            names = list(names_by[u])
            if resources[u]["type"] == "broker":
                for a in dict.fromkeys([*s["provides"], *s["consumes"]]):
                    if events.get(a, {}).get("broker") == u:
                        prefix = "EVENTS_" + a.upper().replace("-", "_") + "_"
                        names += [prefix + "STREAM", prefix + "SUBJECT_PREFIX", *([prefix + "CONSUMER"] if a in s["consumes"] else [])]
                if u == first_broker:
                    names.append("NATS_INBOX_PREFIX")
            for env, eff in (s.get("_eff") or {}).items():
                clash = [k for k in names if k in (eff.get("env") or {})]
                if clash:
                    fail(f"components.{s['name']}: env {', '.join(clash)} is provided by the '{u}' resource; remove it from `env`")
        for eff in (s.get("_eff") or {}).values():
            validate_env_vars(f"components.{s['name']}", {k: v for k, v in (eff.get("env") or {}).items() if k in BROKER_ENV_NAMES}, defer=False)


def check_scale_refs(where: str, c: dict, mn: int, on: dict, apis: dict, resources: dict, events: dict) -> None:
    """The reference checks of one declared `scale` (design.md, Reference and effective checks)."""
    name = c["name"]
    if "consumes" in on:
        api = on["consumes"]
        if api not in c["consumes"]:
            fail(f"{where}.on.consumes: {name} does not consume '{api}' (consumes: {names_list(c['consumes'])})")
        typ = (apis.get(api) or {}).get("type")
        if typ != "events":
            fail(f"{where}.on.consumes: '{api}' is a {typ} API; only an events API carried by a broker has a queue to scale on")
        if events.get(api, {}).get("broker") is None:
            fail(f"{where}.on.consumes: the events API '{api}' is carried by no broker; add one with /resource:add <name> --type broker")
    if "cache" in on:
        r = on["cache"]
        if r not in resources or resources[r]["type"] != "cache":
            caches = [x for x, rc in resources.items() if rc["type"] == "cache"]
            fail(f"{where}.on.cache: '{r}' is not a cache resource of the system (caches: {names_list(caches)})")
        if r not in (c.get("uses") or []):
            fail(f"{where}.on.cache: {name} does not use the cache '{r}'; add it to components.{name}.uses")
    if mn == 0:
        what = []
        if c.get("expose"):
            expose = c["expose"]
            host = (expose.get("host") if isinstance(expose, dict) else None) or name
            what.append(f"exposed at host {host}")
        requests = [f"{a} ({apis[a]['type']})" for a in c["provides"] if apis.get(a, {}).get("type") in REQUEST_API_TYPES]
        if requests:
            what.append("provides " + ", ".join(requests))
        if what:
            fail(f"{where}: min 0 is not possible for {name}: an instance at zero replicas cannot answer requests ({'; '.join(what)})")


def validate_scaling(comps: list[dict], comps_doc: dict, apis: dict, resources: dict, events: dict) -> None:
    for c in comps:
        if c["type"] == "library" or "external" in c:
            continue
        name = c["name"]
        spec = {str(k): v for k, v in comps_doc.items()}[name]
        declared = [(f"components.{name}.scale", spec["scale"])] if "scale" in spec else []
        for e, i in (spec.get("instances") or {}).items():
            if isinstance(i, dict) and "scale" in i:
                declared.append((f"components.{name}.instances.{e}.scale", i["scale"]))
        for where, raw in declared:
            check_scale_refs(where, c, raw.get("min", 1), raw["on"], apis, resources, events)
        for e in ENVS:
            scale = c["_eff"].get(e, {}).get("scale")
            if scale is None:
                continue
            label = f"components.{name}.instances.{e}"
            if scale["trigger"] in ("cpu", "memory"):
                if not ((c["_eff"][e].get("resources") or {}).get("requests") or {}).get(scale["trigger"]):
                    what = "a CPU request" if scale["trigger"] == "cpu" else "a memory request"
                    fail(f"{label}: scale on {scale['trigger']} needs {what}; set resources.requests.{scale['trigger']} on components.{name} or on this instance")
            elif scale["trigger"] == "consumes":
                api = scale["on"]["consumes"]
                broker_name = events[api]["broker"]
                if resources[broker_name]["envs"][e]["external"]:
                    fail(f"{label}: scale on consumes '{api}' needs the broker {broker_name} in the cluster, but it is external in {e}; "
                         f"the platform cannot read the queue depth of an external broker")


def scaled_instances(services: list[dict], env: str) -> list[str]:
    """The non-external components running in `env` with an effective scale, in `components:` order."""
    return [s["name"] for s in services if "external" not in s and env in s["environments"] and s["_eff"][env].get("scale")]


def check_cron(where: str, value) -> None:
    fields = value.split() if isinstance(value, str) else []
    ok = len(fields) == 5
    for field, (lo, hi) in zip(fields, CRON_RANGES):
        for item in field.split(","):
            m = CRON_ITEM.match(item)
            if not m:
                ok = False
                continue
            if m.group(2) is not None and int(m.group(2)[1:]) < 1:
                ok = False
            nums = [int(x) for x in m.group(1).split("-")] if m.group(1) != "*" else []
            if any(not lo <= x <= hi for x in nums) or (len(nums) == 2 and nums[0] > nums[1]):
                ok = False
    if not ok:
        fail(f"{where}.schedule '{value}' is not a five-field cron expression (minute hour day-of-month month day-of-week, in UTC)")


def check_backup(where: str, env: str, backup, resources: dict, comps: list[dict]) -> dict:
    """One `backup` (design.md, Render errors): returns {to, schedule, retention} with the defaults filled."""
    if not isinstance(backup, dict):
        fail(f"{where} must be a mapping with to, schedule and retention")
    for k in backup:
        if k not in BACKUP_KEYS:
            fail(f"{where}: unknown key '{k}' (allowed: {', '.join(BACKUP_KEYS)})")
    if backup.get("to") is None:
        fail(f"{where}.to is required: the name of a bucket resource of the system")
    to = backup["to"]
    buckets = [n for n, rc in resources.items() if rc["type"] == "bucket"]
    if not isinstance(to, str) or to not in buckets:
        fail(f"{where}.to: '{to}' is not a bucket resource of the system (buckets: {names_list(buckets)})")
    if not any(env in s.get("environments", []) and to in (s.get("uses") or []) for s in comps):
        fail(f"{where}.to: the bucket {to} has no resource instance in {env} (a component that runs in {env} must use it)")
    target = resources[to]["envs"][env]
    if target["external"] and not target.get("s3"):
        fail(f"{where}.to: the external bucket {to} needs remote.s3 (endpoint, bucket, region, forcePathStyle) in {env}: "
             "backups cannot read them from the secret store")
    schedule = backup.get("schedule", BACKUP_DEFAULTS["schedule"])
    check_cron(where, schedule)
    retention = backup.get("retention", BACKUP_DEFAULTS["retention"])
    if not (isinstance(retention, str) and RETENTION.match(retention) and not retention.endswith("\n")):
        fail(f"{where}.retention '{retention}' must be <n>d or <n>w with n of at least 1")
    return {"to": to, "schedule": schedule, "retention": retention}


def validate_backups(doc: dict, env_conf: dict, resources: dict, comps: list[dict]) -> dict:
    """Checks every backup, environments first, then each database with its instances in ENVS order; returns {env: backup} of the
    environments and sets `backup` in the database instances' effective values."""
    out = {}
    for e in ENVS:
        if e in env_conf["backup"]:
            out[e] = check_backup(f"environments.{e}.backup", e, env_conf["backup"][e], resources, comps)
    for r, rc in resources.items():
        if rc["type"] != "database":
            continue
        inst = ((doc.get("resources") or {}).get(r) or {}).get("instances") or {}
        for e in ENVS:
            if "backup" in (inst.get(e) or {}):
                rc["envs"][e]["backup"] = check_backup(f"resources.{r}.instances.{e}.backup", e, inst[e]["backup"], resources, comps)
    return out


def load_system(app_dir: Path) -> dict:
    """Parse and validate applications/<system>/system.yaml; returns the normalized system:
    `services` (components in the 4.x service shape, libraries left out), `cfg` (gateway, secretStore, hosts, policy and observability per
    environment, resources per environment, registries), `libraries`, `system` and `owner`."""
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
    scale_on_keys(doc)
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
    paused = validate_pause(doc)
    comps_doc = doc.get("components") or {}
    if not isinstance(comps_doc, dict):
        fail("components must be a mapping of names to components")
    comps = [validate_component(app_dir, str(c), spec) for c, spec in comps_doc.items()]
    validate_apis(doc, comps)
    resources = validate_resources(doc, app_dir)
    events = bind_events(doc, comps, resources)
    check_resource_clashes(d, resources, comps_doc, comps, events)
    validate_uses(comps, resources, events)
    validate_scaling(comps, comps_doc, doc.get("apis") or {}, resources, events)
    backups = validate_backups(doc, env_conf, resources, comps)
    registries = doc.get("registries") or []
    if not isinstance(registries, list):
        fail("registries must be a list of image prefixes, e.g. [ghcr.io/acme/]")
    services = [{**c, "uses": effective_uses(c, resources, events)} for c in comps if c["type"] != "library"]
    cfg = {"gateway": doc.get("gateway"), "secretStore": doc.get("secretStore"), "hosts": hosts, "registries": [str(r) for r in registries],
           "policy": env_conf["policy"], "observability": env_conf["observability"], "operators": env_conf["operators"],
           "backup": backups, "resources": resources, "events": events}
    cfg["autoscaling"] = {e: "keda" if any(s["_eff"][e]["scale"]["trigger"] in ("consumes", "cache") for s in services if "external" not in s
                                          and e in s["environments"] and s["_eff"][e].get("scale")) else None for e in ENVS}
    declared = "autonomy" in doc or env_conf["declared"] or any("autonomy_max" in s["_eff"][e] for s in services for e in s["environments"])
    autonomy = autonomy_json(d, paused, env_conf["autonomy"], env_conf["promote"], services) if declared else None
    return {"system": d, "owner": doc["owner"], "autonomy": autonomy, "services": services, "libraries": [c["name"] for c in comps if c["type"] == "library"], "cfg": cfg}


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
    names = {a for s in services if env in envs_of(s) for a in s.get("uses") or [] if not cfg["resources"][a]["envs"][env]["external"]}
    if env in cfg["observability"] and any(env in envs_of(s) for s in services):
        names.add("observability")
    return sorted(names)


def resource_instances(system: dict) -> list[tuple[str, str, str, bool]]:
    """(name, env, type, external) for each used resource instance, in resource order then ENVS order."""
    services, resources = system["services"], system["cfg"]["resources"]
    return [(r, e, rc["type"], rc["envs"][e]["external"]) for r, rc in resources.items() for e in ENVS
            if any(e in envs_of(s) and r in (s.get("uses") or []) for s in services)]


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


KEDA_POLLING = 15
KEDA_COOLDOWN = 315
SCALE_DOWN_WINDOW = 345


def replicas_for(svc: dict, env: str) -> int:
    if svc.get("scale"):
        return int(svc["scale"]["min"])
    return int(svc.get("replicas", 1))


def autoscaler_files(app: str, svc: dict, env: str, cfg: dict) -> dict[str, dict]:
    """The autoscaler documents of a scaled instance (spec 120): an HPA for cpu and memory, a KEDA ScaledObject for queue triggers."""
    scale = svc.get("scale")
    if not scale:
        return {}
    name, mn, mx, trigger, on = svc["name"], scale["min"], scale["max"], scale["trigger"], scale["on"]
    target = {"apiVersion": "apps/v1", "kind": "Deployment", "name": name}
    if trigger in ("cpu", "memory"):
        return {"hpa.yaml": {
            "apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": name, "labels": labels_for(app, svc)},
            "spec": {"scaleTargetRef": target, "minReplicas": mn, "maxReplicas": mx,
                     "metrics": [{"type": "Resource", "resource": {"name": trigger, "target": {"type": "Utilization", "averageUtilization": on[trigger]}}}]},
        }}
    files: dict[str, dict] = {}
    if trigger == "consumes":
        api = on["consumes"]
        broker = cfg["events"][api]["broker"]
        entry: dict = {"type": "nats-jetstream", "metadata": {
            "natsServerMonitoringEndpoint": f"{app}-{broker}.{app}-{env}.svc:8222", "account": "$G", "stream": f"{app}-{api}",
            "consumer": name, "lagThreshold": str(on["lag"]), "activationLagThreshold": "0", "useHttps": "false"}}
    else:
        entry = {"type": "redis", "metadata": {"listName": on["list"], "listLength": str(on["length"]), "activationListLength": "0",
                                               "databaseIndex": "0"},
                 "authenticationRef": {"name": name, "kind": "TriggerAuthentication"}}
        secret = f"{app}-{on['cache']}-app"
        files["triggerauthentication.yaml"] = {
            "apiVersion": "keda.sh/v1alpha1", "kind": "TriggerAuthentication",
            "metadata": {"name": name, "labels": labels_for(app, svc),
                         "annotations": {"argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true"}},
            "spec": {"secretTargetRef": [{"parameter": p, "name": secret, "key": p} for p in ("host", "port", "password")]},
        }
    files["scaledobject.yaml"] = {
        "apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject",
        "metadata": {"name": name, "labels": labels_for(app, svc),
                     "annotations": {"argocd.argoproj.io/sync-wave": "1", "argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true"}},
        "spec": {"scaleTargetRef": target, "pollingInterval": KEDA_POLLING, "cooldownPeriod": KEDA_COOLDOWN,
                 "minReplicaCount": mn, "maxReplicaCount": mx,
                 "advanced": {"horizontalPodAutoscalerConfig": {"behavior": {"scaleDown": {
                     "stabilizationWindowSeconds": SCALE_DOWN_WINDOW,
                     "policies": [{"type": "Percent", "value": 100, "periodSeconds": 15}]}}}},
                 "triggers": [entry]},
    }
    return files


def addon_env(app: str, svc: dict, resources: dict) -> list[dict]:
    """Connection settings of the resources a component uses, read from the resource's Secret `<system>-<name>-app` (never copied into git)."""
    out = []
    for u, names in variable_names(svc.get("uses") or [], resources):
        env = TYPES[resources[u]["type"]]["env"]
        secret = f"{app}-{u}-{svc['name']}-app" if resources[u]["type"] == "broker" else f"{app}-{u}-app"      # a broker: one Secret per component
        out += [{"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}} for name, key in zip(names, env.values())]
    return out


def events_env(app: str, svc: dict, events: dict) -> dict[str, str]:
    """Literal variables of the events APIs a component provides or consumes on a broker (spec 119): stream, subject prefix, consumer, inbox prefix."""
    out: dict[str, str] = {}
    for a in dict.fromkeys([*svc.get("provides", []), *svc.get("consumes", [])]):
        if events.get(a, {}).get("broker") is None:
            continue
        prefix = "EVENTS_" + a.upper().replace("-", "_") + "_"
        out[prefix + "STREAM"] = f"{app}-{a}"
        out[prefix + "SUBJECT_PREFIX"] = f"{app}.{a}"
        if a in svc.get("consumes", []):
            out[prefix + "CONSUMER"] = svc["name"]
        out["NATS_INBOX_PREFIX"] = f"_INBOX.{svc['name']}"
    return out


def obs_env(app: str, svc: dict, env: str) -> dict[str, str]:
    """Standard OTel variables pointing at the environment's collector (the component's `env` still wins)."""
    return {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_SERVICE_NAME": svc["name"], "OTEL_RESOURCE_ATTRIBUTES": f"service.namespace={app},deployment.environment={env}",
            "OTEL_SDK_DISABLED": "false"}


def deployment(app: str, svc: dict, env: str, obs: bool = False, resources: dict | None = None, events: dict | None = None) -> dict:
    labels = labels_for(app, svc)
    selector = {"app": svc["name"]}
    env_vars = {"PORT": str(svc["port"]), **(obs_env(app, svc, env) if obs else {}), **events_env(app, svc, events or {}), **{k: str(v) for k, v in (svc.get("env") or {}).items()}}
    container: dict = {
        "name": "app",
        "image": svc["image"],
        "ports": [{"name": "http", "containerPort": int(svc["port"])}],
        "env": [{"name": k, "value": v} for k, v in sorted(env_vars.items())] + addon_env(app, svc, resources or {}),
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
    """ExternalSecrets of the `remote:` secrets and one credentials Job for all `generate:` secrets; values never appear in git."""
    out: dict[str, dict] = {}
    labels = labels_for(app, svc)
    store = cfg.get("secretStore") or DEFAULT_STORE
    for sec in svc.get("secrets") or []:
        name = sec["name"]
        meta = {"name": name, "labels": labels}
        target = {"name": name, "creationPolicy": "Owner", "deletionPolicy": "Retain"}
        if sec.get("generate"):
            continue        # written once by the component's credentials Job (spec 128)
        else:
            remote = sec["remote"]
            key = str(remote.get("path") or "{system}-{env}-{secret}").format(system=app, env=env, secret=name, component=svc["name"])
            spec = {"refreshInterval": str(remote.get("refreshInterval", "1h")),
                    "secretStoreRef": {"name": store["name"], "kind": store.get("kind", "ClusterSecretStore")},
                    "target": target,
                    "data": [{"secretKey": k, "remoteRef": {"key": key, "property": k}} for k in remote["keys"]]}
            out[f"externalsecret-{name}.yaml"] = {"apiVersion": ESO_API, "kind": "ExternalSecret", "metadata": meta, "spec": spec}
    generated = [sec for sec in svc.get("secrets") or [] if sec.get("generate")]
    if generated:
        plan = {"namespace": f"{app}-{env}",
                "secrets": [{"name": sec["name"], "labels": dict(labels),
                             "generate": {k: int(sec.get("length", 32)) for k in sec["generate"]}, "data": {}} for sec in generated],
                "retire": {"externalSecrets": sorted(sec["name"] for sec in generated), "generators": [f"{svc['name']}-password"]}}
        out.update(credentials_files(app, env, svc["name"], labels, plan))
    return out


def is_backup_target(cfg: dict, env: str, name: str) -> bool:
    """True when bucket `name` is the backup target of `env`."""
    return (cfg.get("backup") or {}).get(env, {}).get("to") == name


def backup_target_labels(app: str, name: str) -> dict:
    return {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}


def external_secrets(app: str, env: str, services: list[dict], cfg: dict) -> dict[str, dict[str, dict]]:
    """{component: {file name: ExternalSecret}} for the external resource instances of `env`.

    One ExternalSecret `<system>-<name>-app` per instance, in the overlay of the alphabetically first running component that uses it."""
    store = cfg.get("secretStore") or DEFAULT_STORE
    out: dict[str, dict[str, dict]] = {}
    for name, rc in cfg["resources"].items():
        conf = rc["envs"][env]
        if not conf["external"] or rc["type"] == "broker":      # a broker's ExternalSecrets are per component (spec 119)
            continue
        users = sorted(s["name"] for s in services if env in envs_of(s) and "external" not in s and name in (s.get("uses") or []))
        if not users:
            continue
        secret = f"{app}-{name}-app"
        doc = {"apiVersion": ESO_API, "kind": "ExternalSecret",
               "metadata": {"name": secret, "labels": {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app,
                                                       "app.kubernetes.io/managed-by": "gitops-app"}},
               "spec": {"refreshInterval": conf["refreshInterval"],
                        "secretStoreRef": {"name": store["name"], "kind": store.get("kind", "ClusterSecretStore")},
                        "target": {"name": secret, "creationPolicy": "Orphan", "deletionPolicy": "Retain"},
                        "data": [{"secretKey": k, "remoteRef": {"key": conf["store_key"], "property": k}}
                                 for k in TYPES[rc["type"]]["keys"]]}}
        if is_backup_target(cfg, env, name):       # Velero's store reads the Secret by these labels (AC-117.17)
            doc["spec"]["target"]["template"] = {"engineVersion": "v2", "mergePolicy": "Merge",
                                                 "metadata": {"labels": backup_target_labels(app, name)}}
        out.setdefault(users[0], {})[f"externalsecret-{secret}.yaml"] = doc
    return out


def broker_secrets(app: str, env: str, services: list[dict], cfg: dict) -> dict[str, dict[str, dict]]:
    """{component: {file name: ExternalSecret}} for the external broker instances of `env` (spec 119): one `<system>-<broker>-<component>-app`
    per running, non-external component that provides or consumes an API the broker carries, in that component's overlay."""
    store = cfg.get("secretStore") or DEFAULT_STORE
    out: dict[str, dict[str, dict]] = {}
    for name, rc in cfg["resources"].items():
        if rc["type"] != "broker" or not rc["envs"][env]["external"]:
            continue
        conf = rc["envs"][env]
        apis = {a for a, e in cfg["events"].items() if e["broker"] == name}
        for s in services:
            if env not in envs_of(s) or "external" in s or not any(a in apis for a in [*s["provides"], *s["consumes"]]):
                continue
            secret = f"{app}-{name}-{s['name']}-app"
            key = conf["store_key"].replace("{component}", s["name"])
            out.setdefault(s["name"], {})[f"externalsecret-{secret}.yaml"] = {
                "apiVersion": ESO_API, "kind": "ExternalSecret",
                "metadata": {"name": secret, "labels": {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app,
                                                        "app.kubernetes.io/managed-by": "gitops-app"}},
                "spec": {"refreshInterval": conf["refreshInterval"],
                         "secretStoreRef": {"name": store["name"], "kind": store.get("kind", "ClusterSecretStore")},
                         "target": {"name": secret, "creationPolicy": "Orphan", "deletionPolicy": "Retain"},
                         "data": [{"secretKey": k, "remoteRef": {"key": key, "property": k}} for k in TYPES["broker"]["keys"]]}}
    return out


BARMAN_PLUGIN = "barman-cloud.cloudnative-pg.io"
VELERO_EXCLUDED = ["clusters.postgresql.cnpg.io", "backups.postgresql.cnpg.io", "scheduledbackups.postgresql.cnpg.io", "objectstores.barmancloud.cnpg.io"]
PRESERVED_FIELDS = {"annotations": ["argocd.argoproj.io/skip-reconcile"]}
SERVER_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
RETRY = {"limit": 10, "backoff": {"duration": "30s", "factor": 2, "maxDuration": "10m"}}


def utc_time(value) -> bool:
    import datetime
    if not isinstance(value, str) or not SERVER_TIME.match(value):
        return False
    try:
        datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def read_restore_state(app_dir: Path, env: str, db: str) -> tuple[list[dict], list[dict]]:
    """(clusters, archives) of addons/<env>/<db>/restore-state.yaml (spec 117, written by `cplat resource restore`; `archives` by
    `cplat resource upgrade`, spec 121): the clusters are a list of {name, role, from?, until?}, the archives a list of {server, major}
    whose last entry is the current Barman archive. Absent file = one active cluster `<system>-<db>`, no archives."""
    app = app_dir.name
    base = f"{app}-{db}"
    path = app_dir / "addons" / env / db / "restore-state.yaml"
    if not path.is_file():
        return [{"name": base, "role": "active"}], []

    def bad(reason: str):
        raise RenderError(f"applications/{app}/addons/{env}/{db}/restore-state.yaml is not valid ({reason}); take it from git history")

    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        bad(f"does not parse: {str(e).splitlines()[0] if str(e) else e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("clusters"), list) or not doc["clusters"]:
        bad("must be a mapping with next and clusters")
    clusters = doc["clusters"]
    numbers = []
    for c in clusters:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("role") not in ("active", "restored", "previous"):
            bad("every cluster needs a name and a role of active, restored or previous")
        m = re.fullmatch(re.escape(base) + r"(-g([1-9][0-9]*))?", c["name"])
        if not m:
            bad(f"cluster '{c['name']}' is not {base} or {base}-g<n>")
        if m.group(2):
            numbers.append(int(m.group(2)))
            frm = c.get("from")
            if not isinstance(frm, dict) or not isinstance(frm.get("server"), str) or not frm["server"]:
                bad(f"cluster '{c['name']}' needs from.server")
            if frm.get("to") != "latest" and not utc_time(frm.get("to")):
                bad(f"cluster '{c['name']}': from.to must be an RFC 3339 UTC time ending in Z, or latest")
        if c["role"] == "previous" and not utc_time(c.get("until")):
            bad(f"cluster '{c['name']}' is previous and needs until, an RFC 3339 UTC time ending in Z")
        if "archive" in c and (not isinstance(c["archive"], str) or not c["archive"]):
            bad(f"cluster '{c['name']}': archive must be a server name")
    if sum(c["role"] == "active" for c in clusters) != 1:
        bad("exactly one cluster must be active")
    if sum(c["role"] != "active" for c in clusters) > 1:
        bad("at most one cluster may be restored or previous, not both")
    nxt = doc.get("next")
    if isinstance(nxt, bool) or not isinstance(nxt, int) or any(nxt <= n for n in numbers):
        bad("next must be an integer greater than every generation number")
    if "archives" not in doc:
        return clusters, []
    archives = doc["archives"]
    if not isinstance(archives, list) or not archives:
        bad("archives: must be a list with at least one archive")
    for a in archives:
        if (not isinstance(a, dict) or not isinstance(a.get("server"), str) or not a["server"]
                or isinstance(a.get("major"), bool) or not isinstance(a.get("major"), int)):
            bad("archives: every archive needs a server and a major")
        if a["major"] not in POSTGRES_IMAGES:
            bad(f"archives: major {a['major']} is not in the versions table")
    if len({a["server"] for a in archives}) != len(archives):
        bad("archives: servers must be unique")
    return clusters, [{"server": a["server"], "major": a["major"]} for a in archives]


def backup_target(app: str, env: str, cfg: dict, to: str) -> tuple[str, str, str]:
    """(bucket, endpoint, secret) of the backup target bucket `to` in `env`."""
    target = cfg["resources"][to]["envs"][env]
    secret = f"{app}-{to}-app"
    if target["external"]:
        return target["s3"]["bucket"], target["s3"]["endpoint"], secret
    return f"{app}-{to}", f"http://{app}-{to}-filer.{app}-{env}.svc:8333", secret


def stepped_since(archives: list[dict], cluster: str, version: int) -> bool:
    """True when a major step appended an archive of the effective major after the generation's own archive (spec 121, AC-121.16)."""
    own = next((i for i, a in enumerate(archives) if a["server"] == cluster), None)
    return own is not None and own < len(archives) - 1 and archives[-1]["major"] == version


def database_cluster(app: str, name: str, env: str, conf: dict, cluster: str | None = None, role: str = "active",
                     frm: dict | None = None, backup: bool = False, archives: list[dict] | None = None, archive: str | None = None) -> dict:
    db = app.replace("-", "_")
    base = f"{app}-{name}"
    image = conf["pin"]
    # a physical backup only starts on the major of its archive (spec 121, changed after review): a previous cluster runs the major of
    # the archive it wrote to (its `archive`, recorded at the switch); a restored generation runs the major of its source archive; the
    # active generation keeps its source archive's major only until a major step appended a later archive with the effective major
    # a server missing from a non-empty `archives` predates the first major step: it ran archives[0]'s major (AC-121.16, changed after review)
    major_of = lambda server: next((a["major"] for a in archives or [] if a["server"] == server), archives[0]["major"] if archives else None)  # noqa: E731
    previous_major = major_of(archive) if role == "previous" and archive else None
    if previous_major is not None:
        image = conf["pin"] if previous_major == conf["version"] else POSTGRES_IMAGES[previous_major]
    elif frm is not None and (role == "restored" or not stepped_since(archives or [], cluster or base, conf["version"])):
        source_major = major_of(frm["server"])
        if source_major is not None and source_major != conf["version"]:
            image = POSTGRES_IMAGES[source_major]
    spec: dict = {
        "instances": conf["replicas"],
        "imageName": image,
        "storage": {"size": conf["storage"]},
        "enableSuperuserAccess": False,
    }
    if frm is None:
        spec["bootstrap"] = {"initdb": {"database": db, "owner": db}}
    else:
        recovery: dict = {"source": "origin", "database": db, "owner": db, "secret": {"name": f"{base}-app"}}
        if frm["to"] != "latest":
            recovery["recoveryTarget"] = {"targetTime": frm["to"]}
        spec["bootstrap"] = {"recovery": recovery}
        spec["externalClusters"] = [{"name": "origin", "plugin": {"name": BARMAN_PLUGIN, "parameters": {"barmanObjectName": base, "serverName": frm["server"]}}}]
    spec["resources"] = conf["resources"] or DB_DEFAULT_RESOURCES
    if backup and role in ("active", "previous"):
        params = {"barmanObjectName": base}
        server = archive or (archives[-1]["server"] if archives else None)      # one Barman server per major (change it with imageName)
        if server and server != (cluster or base):      # spec 121 (changed after review): a switched generation has its own `archive`
            params["serverName"] = server
        spec["plugins"] = [{"name": BARMAN_PLUGIN, "isWALArchiver": True, "parameters": params}]
    return {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": cluster or base, "labels": {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}},
        "spec": spec,
    }


def bucket_files(app: str, name: str, env: str, conf: dict, target: bool = False) -> dict[str, dict | str]:
    """The in-cluster bucket instance (ADR-059): its own SeaweedFS, a Bucket, an identity limited to it and a credentials Job.

    Argo CD never deletes any of it (`Delete=false`, `reclaimPolicy: Retain`). No credential value is rendered: the PreSync Job writes
    the keys once to `<system>-<name>-app` (create if absent, otherwise keep); S3Credentials then reads that Secret (spec 128)."""
    n = f"{app}-{name}"
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}
    keep = {"argocd.argoproj.io/sync-options": "Delete=false"}

    def meta(obj: str, wave: str | None = None) -> dict:
        ann = {**keep, **({"argocd.argoproj.io/sync-wave": wave} if wave else {})}
        return {"name": obj, "labels": dict(labels), "annotations": ann}

    seaweed_api = "seaweed.seaweedfs.com/v1"
    volume = lambda: {"enabled": True, "resources": {"requests": {"storage": "1Gi"}}}  # noqa: E731  (fresh dicts: no YAML anchors)

    files: dict[str, dict | str] = {
        "seaweed.yaml": {
            "apiVersion": seaweed_api, "kind": "Seaweed", "metadata": meta(n),
            "spec": {"image": conf["pin"], "volumeServerDiskCount": 1,
                     "master": {"replicas": 1, "volumeSizeLimitMB": 1024, "persistence": volume()},
                     "volume": {"replicas": 1, "requests": {"storage": conf["storage"]}},
                     "filer": {"replicas": 1, "persistence": volume(), "s3": {"enabled": True},
                               "config": '[leveldb2]\nenabled = true\ndir = "/data/filerldb2"\n'}}},
        "bucket.yaml": {
            "apiVersion": seaweed_api, "kind": "Bucket", "metadata": meta(n),
            "spec": {"name": n, "clusterRef": {"name": n}, "reclaimPolicy": "Retain",
                     "versioning": "Enabled" if conf["versioning"] else "Suspended", "owner": n,
                     "access": [{"user": n, "actions": ["Read", "Write", "List", "Tagging"]}]}},
        "identity.yaml": {"apiVersion": seaweed_api, "kind": "S3Identity", "metadata": meta(n), "spec": {"seaweedRef": {"name": n}}},
        "credentials.yaml": {
            "apiVersion": seaweed_api, "kind": "S3Credentials", "metadata": meta(n, "1"),
            "spec": {"seaweedRef": {"name": n}, "identityRef": {"name": n},
                     "secretRef": {"name": f"{n}-app", "accessKeyField": "accessKeyId", "secretKeyField": "secretAccessKey"},  # pragma: allowlist secret
                     "reclaimPolicy": "Retain"}},
    }
    ns = f"{app}-{env}"
    plan = {"namespace": ns,
            "secrets": [{"name": f"{n}-app", "labels": backup_target_labels(app, name) if target else {},   # the environment's backup target: Velero's store finds the Secret by its labels (AC-117.17)
                         "generate": {"accessKeyId": 20, "secretAccessKey": 40},  # pragma: allowlist secret
                         "data": {"endpoint": f"http://{n}-filer.{ns}.svc:8333", "bucket": n, "region": "us-east-1", "forcePathStyle": "true"}}],
            "retire": {"externalSecrets": [f"{n}-app"], "generators": [f"{n}-access-key", f"{n}-secret-key"]}}
    files.update(credentials_files(app, env, n, labels, plan))
    files["kustomization.yaml"] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": sorted(files)}
    return files


CREDENTIALS_SCRIPT = r'''# The credentials Job (spec 128): writes each generated credential once (create if absent, otherwise keep what exists),
# retires the 5.4 ExternalSecrets and Password generators, and restarts a server that refuses the password of its Secret.
# Standard library only. No value is ever printed.
import base64
import json
import os
import re
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
PLACEHOLDER = re.compile(r"\{\{ \.([A-Za-z0-9_]+) \}\}")
SA_DIR = os.environ.get("CREDENTIALS_SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
API = os.environ.get("CREDENTIALS_API_URL") or "https://%s:%s" % (
    os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"), os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
RETRY_SECONDS = 60


class Fail(Exception):
    def __init__(self, message, code=1):
        Exception.__init__(self, message)
        self.code = code


def log(message):
    print(message, flush=True)


with open(os.path.join(SA_DIR, "token")) as handle:
    TOKEN = handle.read().strip()
CONTEXT = None
if API.startswith("https://"):
    CONTEXT = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))


def call(method, path, body=None, content_type="application/json"):
    """One API request; connection errors, 403, 429 and 5xx are retried with backoff for up to 60 s. Returns (status, object)."""
    data = None if body is None else json.dumps(body).encode()
    deadline = time.time() + RETRY_SECONDS
    delay = 0.5
    while True:
        request = urllib.request.Request(API + path, data=data, method=method)
        request.add_header("Authorization", "Bearer " + TOKEN)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", content_type)
        status, raw = None, b""
        try:
            with urllib.request.urlopen(request, timeout=30, context=CONTEXT) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        except (urllib.error.URLError, OSError):
            status = None
        if status is not None and status != 403 and status != 429 and status < 500:
            try:
                return status, (json.loads(raw) if raw else None)
            except ValueError:
                return status, None
        if time.time() + delay > deadline:
            raise Fail("%s %s: %s" % (method, path, "no answer" if status is None else "status %d" % status))
        time.sleep(delay)
        delay = min(delay * 2, 5)


def expect(status, wanted, method, path):
    if status not in wanted:
        raise Fail("%s %s: unexpected status %d" % (method, path, status))


def retire(plan):
    ns = plan["namespace"]
    for name in plan.get("retire", {}).get("externalSecrets", []):
        path = "/apis/external-secrets.io/v1/namespaces/%s/externalsecrets/%s" % (ns, name)
        status, obj = call("GET", path)
        if status == 404:
            continue
        expect(status, (200,), "GET", path)
        sources = (obj.get("spec") or {}).get("dataFrom") or []
        if not any((source.get("sourceRef") or {}).get("generatorRef") for source in sources):
            raise Fail("ExternalSecret %s is not generator-backed; it writes this Secret, so the credentials Job does not" % name)
        status, _ = call("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Orphan"})
        if status != 404:
            expect(status, (200, 202), "DELETE", path)
        deadline = time.time() + 60
        while True:
            status, _ = call("GET", path)
            if status == 404:
                break
            if time.time() > deadline:
                raise Fail("ExternalSecret %s is still there 60 s after its deletion" % name)
            time.sleep(0.5)
        log("retired ExternalSecret " + name)
    for name in plan.get("retire", {}).get("generators", []):
        path = "/apis/generators.external-secrets.io/v1alpha1/namespaces/%s/passwords/%s" % (ns, name)
        status, _ = call("DELETE", path)
        if status != 404:
            expect(status, (200, 202), "DELETE", path)
            log("retired Password generator " + name)


def generate(length):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def render(value, values):
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], value)


def encode(value):
    return base64.b64encode(value.encode()).decode()


def pointer(key):
    return key.replace("~", "~0").replace("/", "~1")


def build_patch(entry, current):
    """The JSON patch that keeps every existing value and fixes only metadata and missing keys; returns (ops, added, adopted)."""
    meta = current["metadata"]
    ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]}]
    adopted = False
    declared = entry.get("labels") or {}
    labels = meta.get("labels")
    if declared and labels is None:
        ops.append({"op": "add", "path": "/metadata/labels", "value": dict(declared)})
        adopted = True
    else:
        for key, value in sorted(declared.items()):
            if (labels or {}).get(key) != value:
                ops.append({"op": "add", "path": "/metadata/labels/" + pointer(key), "value": value})
                adopted = True
    if "app.kubernetes.io/instance" in (labels or {}):
        ops.append({"op": "remove", "path": "/metadata/labels/app.kubernetes.io~1instance"})
        adopted = True
    if "argocd.argoproj.io/tracking-id" in (meta.get("annotations") or {}):
        ops.append({"op": "remove", "path": "/metadata/annotations/argocd.argoproj.io~1tracking-id"})
        adopted = True
    refs = meta.get("ownerReferences") or []
    kept = [ref for ref in refs if ref.get("kind") != "ExternalSecret"]
    if len(kept) != len(refs):
        if kept:
            ops.append({"op": "replace", "path": "/metadata/ownerReferences", "value": kept})
        else:
            ops.append({"op": "remove", "path": "/metadata/ownerReferences"})
        adopted = True
    existing = current.get("data")
    have = dict(existing or {})
    values = {}
    for key in entry.get("generate", {}):
        if key in have:
            values[key] = base64.b64decode(have[key]).decode()
    added = []
    for key, length in sorted(entry.get("generate", {}).items()):
        if key not in have:
            values[key] = generate(length)
            added.append(key)
    for key, literal in sorted(entry.get("data", {}).items()):
        if key not in have:
            added.append(key)
    if added:
        new = {}
        for key in added:
            if key in entry.get("generate", {}):
                new[key] = encode(values[key])
            else:
                new[key] = encode(render(entry["data"][key], values))
        if existing is None:
            ops.append({"op": "add", "path": "/data", "value": new})
        else:
            for key in sorted(new):
                ops.append({"op": "add", "path": "/data/" + pointer(key), "value": new[key]})
    return ops, sorted(added), adopted


def keep(ns, entry):
    name = entry["name"]
    path = "/api/v1/namespaces/%s/secrets/%s" % (ns, name)
    for _ in range(5):
        status, current = call("GET", path)
        expect(status, (200,), "GET", path)
        ops, added, adopted = build_patch(entry, current)
        if len(ops) == 1:
            log("kept " + name)
            return
        status, _ = call("PATCH", path, ops, "application/json-patch+json")
        if status in (409, 422):
            continue
        expect(status, (200,), "PATCH", path)
        note = "kept " + name
        if added:
            note += "; added keys " + ",".join(added)
        if adopted:
            note += "; adopted (metadata)"
        log(note)
        return
    raise Fail("PATCH %s: still conflicting after 5 attempts" % path)


def write_secrets(plan):
    ns = plan["namespace"]
    for entry in plan["secrets"]:
        values = {key: generate(length) for key, length in entry.get("generate", {}).items()}
        data = dict(values)
        for key, literal in entry.get("data", {}).items():
            data[key] = render(literal, values)
        body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": entry["name"], "namespace": ns, "labels": entry.get("labels") or {}},
                "data": {key: encode(value) for key, value in sorted(data.items())}}
        path = "/api/v1/namespaces/%s/secrets" % ns
        status, _ = call("POST", path, body)
        if status == 201:
            log("created " + entry["name"])
        elif status == 409:
            keep(ns, entry)
        else:
            expect(status, (201,), "POST", path)


def read_line(sock):
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def probe_valkey(ip, port, user, password):
    try:
        with socket.create_connection((ip, port), timeout=5) as sock:
            sock.settimeout(5)
            parts = [b"AUTH", user.encode(), password.encode()]
            sock.sendall(b"*3\r\n" + b"".join(b"$%d\r\n%s\r\n" % (len(part), part) for part in parts))
            reply = read_line(sock)
    except OSError:
        return "unknown"
    if reply.startswith(b"+OK"):
        return "accepted"
    if reply.startswith(b"-WRONGPASS"):
        return "refused"
    return "unknown"


def probe_nats(ip, port, user, password):
    try:
        with socket.create_connection((ip, port), timeout=5) as sock:
            sock.settimeout(5)
            info = read_line(sock)
            if b'"tls_required":true' in info.replace(b" ", b""):
                return "unknown"
            options = {"verbose": False, "pedantic": False, "user": user, "pass": password, "name": "credentials-check",
                       "lang": "python", "version": "1", "protocol": 1}
            sock.sendall(b"CONNECT " + json.dumps(options).encode() + b"\r\nPING\r\n")
            buf = b""
            while b"PONG" not in buf and b"-ERR" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return "unknown"
    if b"PONG" in buf:
        return "accepted"
    if b"Authorization Violation" in buf:
        return "refused"
    return "unknown"


def ready(pod):
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def get_pod(ns, name):
    path = "/api/v1/namespaces/%s/pods/%s" % (ns, name)
    status, pod = call("GET", path)
    if status == 404:
        return None
    expect(status, (200,), "GET", path)
    return pod


def secret_value(ns, name, key):
    path = "/api/v1/namespaces/%s/secrets/%s" % (ns, name)
    status, obj = call("GET", path)
    expect(status, (200,), "GET", path)
    return base64.b64decode((obj.get("data") or {})[key]).decode()


def declared_users(pod, users):
    """The users whose password variable this pod declares from the planned Secret."""
    found = []
    for user in users:
        for container in (pod.get("spec") or {}).get("containers") or []:
            for var in container.get("env") or []:
                ref = (var.get("valueFrom") or {}).get("secretKeyRef") or {}
                if var.get("name") == user["env"] and ref.get("name") == user["secret"]:
                    found.append(user)
    return found


def probe_pod(ns, verify, pod, users):
    probe = probe_valkey if verify["engine"] == "valkey" else probe_nats
    states = []
    for user in users:
        password = secret_value(ns, user["secret"], user["key"])
        states.append((user, probe(pod["status"]["podIP"], verify["port"], user["user"], password)))
    return states


def verify_servers(plan):
    verify = plan.get("verify")
    if not verify:
        return
    ns = plan["namespace"]
    checked = {}
    refused = {}
    for name in verify["pods"]:
        pod = get_pod(ns, name)
        if pod is None:
            log("skipped %s: no such pod" % name)
        elif not ready(pod):
            log("skipped %s: not ready" % name)
        elif not (pod.get("status") or {}).get("podIP"):
            log("skipped %s: no pod IP" % name)
        else:
            users = declared_users(pod, verify["users"])
            states = probe_pod(ns, verify, pod, users)
            checked[name] = (pod, users)
            bad = [user for user, state in states if state == "refused"]
            if bad:
                refused[name] = bad
            elif states and all(state == "accepted" for _, state in states):
                log("verified " + name)
            elif states:
                log("skipped %s: could not check every password" % name)
    for name, bad in sorted(refused.items()):
        path = "/api/v1/namespaces/%s/pods/%s" % (ns, name)
        status, _ = call("DELETE", path)
        expect(status, (200, 202, 404), "DELETE", path)
        for user in bad:
            log("restarted %s: it refused the password of %s" % (name, user["secret"]))
    for name, bad in sorted(refused.items()):
        old_uid = checked[name][0]["metadata"]["uid"]
        deadline = time.time() + 120
        fresh = None
        while time.time() < deadline:
            pod = get_pod(ns, name)
            if pod and pod["metadata"]["uid"] != old_uid and ready(pod) and (pod.get("status") or {}).get("podIP"):
                fresh = pod
                break
            time.sleep(2)
        if fresh is None:
            log("skipped %s: not ready 120 s after the restart" % name)
            continue
        states = probe_pod(ns, verify, fresh, checked[name][1])
        again = [user for user, state in states if state == "refused"]
        if again:
            for user in again:
                log("%s still refuses the password of %s after a restart" % (name, user["secret"]))
            raise Fail("a server still refuses a password after its restart", 2)
        log("verified " + name)


def main():
    with open(sys.argv[1]) as handle:
        plan = json.load(handle)
    retire(plan)
    write_secrets(plan)
    verify_servers(plan)


try:
    main()
except Fail as failure:
    log(str(failure))
    sys.exit(failure.code)
'''


def credentials_pod_labels(owner: str, labels: dict) -> dict:
    """The labels of a credentials Job's pod: part-of and managed-by of the owner, and its own name and component. None of the owner's
    selector labels (app, app.kubernetes.io/name), so no Service, Deployment or StatefulSet of the owner selects the Job's pod."""
    kept = {k: v for k, v in labels.items() if k in ("app.kubernetes.io/part-of", "app.kubernetes.io/managed-by")}
    return {"app.kubernetes.io/name": f"{owner}-credentials", **kept, "app.kubernetes.io/component": "credentials"}


def credentials_files(app: str, env: str, owner: str, labels: dict, plan: dict) -> dict[str, dict]:
    """The five objects of an owner's credentials Job (spec 128): they write each generated credential once, in the PreSync hook.

    `owner` is `<system>-<name>` of a resource instance or a component's name. The Job's name carries a hash of the plan, the program and
    the image, so a changed plan runs again and an unchanged one never does."""
    ns = f"{app}-{env}"
    plan_text = json.dumps(plan, indent=2, sort_keys=True)
    digest = hashlib.sha256((plan_text + CREDENTIALS_SCRIPT + CREDENTIALS_IMAGE).encode()).hexdigest()[:8]
    name = f"{owner}-credentials"
    planned = sorted(s["name"] for s in plan["secrets"])
    retire = plan.get("retire", {})
    pods = sorted(plan.get("verify", {}).get("pods", []))
    rules: list[dict] = [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]}]
    for group, resource, names, verbs in (("", "secrets", planned, ["get", "patch"]),
                                          ("external-secrets.io", "externalsecrets", sorted(retire.get("externalSecrets", [])), ["get", "delete"]),
                                          ("generators.external-secrets.io", "passwords", sorted(retire.get("generators", [])), ["delete"]),
                                          ("", "pods", pods, ["get", "delete"])):
        if names:
            rules.append({"apiGroups": [group], "resources": [resource], "resourceNames": names, "verbs": verbs})

    def meta(object_name: str, wave: str, policy: str) -> dict:
        return {"name": object_name, "labels": dict(labels),
                "annotations": {"argocd.argoproj.io/hook": "PreSync", "argocd.argoproj.io/sync-wave": wave,
                                "argocd.argoproj.io/hook-delete-policy": policy}}

    job_security = {"runAsNonRoot": True, "runAsUser": 65534, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]}}
    return {
        "credentials-serviceaccount.yaml": {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta(name, "0", "BeforeHookCreation")},
        "credentials-role.yaml": {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta(name, "0", "BeforeHookCreation"),
                                  "rules": rules},
        "credentials-rolebinding.yaml": {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta(name, "0", "BeforeHookCreation"),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
            "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": ns}]},
        "credentials-configmap.yaml": {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(name, "0", "BeforeHookCreation"),
                                       "data": {"create.py": Literal(CREDENTIALS_SCRIPT), "plan.json": plan_text}},
        "credentials-job.yaml": {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": meta(f"{owner[:42].rstrip('-')}-credentials-{digest}", "1", "BeforeHookCreation,HookSucceeded"),
            "spec": {"backoffLimit": 2, "activeDeadlineSeconds": 300, "ttlSecondsAfterFinished": 600,
                     # the pod must not match any workload's selector (a Service would send it traffic): only these labels
                     "template": {"metadata": {"labels": credentials_pod_labels(owner, labels)},
                                  "spec": {"serviceAccountName": name, "automountServiceAccountToken": True, "restartPolicy": "Never",
                                           "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                                               "seccompProfile": {"type": "RuntimeDefault"}},
                                           "containers": [{"name": "credentials", "image": CREDENTIALS_IMAGE,
                                                           "command": ["python3", "/credentials/create.py", "/credentials/plan.json"],
                                                           "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                                                           "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "64Mi"}},
                                                           "securityContext": job_security,
                                                           "volumeMounts": [{"name": "credentials", "mountPath": "/credentials", "readOnly": True}]}],
                                           "volumes": [{"name": "credentials", "configMap": {"name": name}}]}}}},
    }


def cache_files(app: str, name: str, env: str, conf: dict) -> dict[str, dict | str]:
    """The in-cluster cache instance (ADR-064): one Valkey StatefulSet, a ClusterIP Service and a credentials Job (spec 128).

    Every object carries `app.kubernetes.io/name: <name>`, so spec 117's Velero Schedule leaves all of it out. No secret value is
    rendered: the Job writes the password once to `<system>-<name>-app` (create if absent, otherwise keep) and restarts a server that
    refuses it."""
    n, ns = f"{app}-{name}", f"{app}-{env}"
    labels = lambda: {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}  # noqa: E731
    selector = lambda: {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app}  # noqa: E731
    persistent = conf["persistence"]
    args = ["--port", "6379", "--requirepass", "$(VALKEY_PASSWORD)", "--maxmemory", str(conf["maxmemory"]),
            "--maxmemory-policy", conf["maxmemoryPolicy"], "--save", "", "--dir", "/data",
            "--appendonly", "yes" if persistent else "no"]
    if persistent:
        args += ["--appendfsync", "everysec"]
    memory = str(conf["memory"])
    container: dict = {
        "name": "valkey", "image": conf["pin"], "command": ["valkey-server"], "args": args,
        "env": [{"name": "VALKEY_PASSWORD", "valueFrom": {"secretKeyRef": {"name": f"{n}-app", "key": "password"}}}],  # pragma: allowlist secret
        "ports": [{"name": "redis", "containerPort": 6379}],
        "resources": {"requests": {"cpu": "50m", "memory": memory}, "limits": {"memory": memory}},
        "readinessProbe": {"exec": {"command": ["sh", "-c", 'valkey-cli --no-auth-warning -a "$VALKEY_PASSWORD" ping | grep -q PONG']},
                           "periodSeconds": 5},
        "livenessProbe": {"tcpSocket": {"port": "redis"}, "initialDelaySeconds": 10, "periodSeconds": 10},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 999, "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
    }
    spec: dict = {
        "replicas": 1, "serviceName": n, "selector": {"matchLabels": selector()},
        "template": {"metadata": {"labels": labels()},
                     "spec": {"automountServiceAccountToken": False,
                              "securityContext": {"runAsNonRoot": True, "runAsUser": 999, "runAsGroup": 999, "fsGroup": 999,
                                                  "seccompProfile": {"type": "RuntimeDefault"}},
                              "containers": [container]}},
    }
    if persistent:
        container["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
        spec["volumeClaimTemplates"] = [{"metadata": {"name": "data", "labels": labels()},
                                         "spec": {"accessModes": ["ReadWriteOnce"],
                                                  "resources": {"requests": {"storage": conf["storage"]}}}}]
    files: dict[str, dict | str] = {
        "service.yaml": {
            "apiVersion": "v1", "kind": "Service", "metadata": {"name": n, "labels": labels()},
            "spec": {"type": "ClusterIP", "selector": selector(),
                     "ports": [{"name": "redis", "port": 6379, "targetPort": "redis"}]}},
        "statefulset.yaml": {
            "apiVersion": "apps/v1", "kind": "StatefulSet",
            "metadata": {"name": n, "labels": labels()},
            "spec": spec},
    }
    plan = {"namespace": ns,
            "secrets": [{"name": f"{n}-app", "labels": labels(), "generate": {"password": 32},
                         "data": {"url": "redis://default:{{ .password }}@" + f"{n}.{ns}.svc:6379/0", "host": f"{n}.{ns}.svc", "port": "6379"}}],
            "retire": {"externalSecrets": [f"{n}-app"], "generators": [f"{n}-password"]},
            "verify": {"engine": "valkey", "port": 6379, "pods": [f"{n}-0"],
                       "users": [{"secret": f"{n}-app", "key": "password", "user": "default", "env": "VALKEY_PASSWORD"}]}}
    files.update(credentials_files(app, env, n, labels(), plan))
    files["kustomization.yaml"] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": sorted(files)}
    return files


def backup_files(app: str, env: str, cfg: dict) -> dict[str, dict]:
    """backup/<env>/ (AC-117.15, AC-117.17): Velero's storage location, credentials from the target bucket's Secret, and the Schedule."""
    backup = cfg["backup"][env]
    to = backup["to"]
    bucket, endpoint, secret = backup_target(app, env, cfg, to)
    target = cfg["resources"][to]["envs"][env]
    n, ns = f"{app}-{env}", f"{app}-{env}"
    labels = {"app.kubernetes.io/name": "backup", "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}

    def meta(name: str, namespace: str) -> dict:
        return {"name": name, "namespace": namespace, "labels": dict(labels)}

    if target["external"]:
        region, path_style = target["s3"]["region"], str(bool(target["s3"]["forcePathStyle"])).lower()
    else:
        region, path_style = "us-east-1", "true"
    number, unit = int(backup["retention"][:-1]), backup["retention"][-1]
    hours = number * (24 if unit == "d" else 168)
    caches = sorted(r for r, rc in cfg["resources"].items() if rc["type"] == "cache")
    policy = "version: v1\nvolumePolicies:\n- conditions:\n    volumeTypes:\n    - emptyDir\n  action:\n    type: skip\n"
    cloud = "[default]\naws_access_key_id={{ .accessKeyId }}\naws_secret_access_key={{ .secretAccessKey }}\n"
    files: dict[str, dict] = {
        "serviceaccount.yaml": {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta(f"{n}-backup", "velero")},
        "role.yaml": {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": meta(f"{app}-backup-credentials", ns),
                      "rules": [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": [secret], "verbs": ["get"]},
                                {"apiGroups": ["authorization.k8s.io"], "resources": ["selfsubjectrulesreviews"], "verbs": ["create"]}]},
        "rolebinding.yaml": {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta(f"{app}-backup-credentials", ns),
                             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": f"{app}-backup-credentials"},
                             "subjects": [{"kind": "ServiceAccount", "name": f"{n}-backup", "namespace": "velero"}]},
        "secretstore.yaml": {"apiVersion": ESO_API, "kind": "SecretStore", "metadata": meta(f"{n}-backup", "velero"),
                             "spec": {"provider": {"kubernetes": {
                                 "remoteNamespace": ns,
                                 "server": {"caProvider": {"type": "ConfigMap", "name": "kube-root-ca.crt", "key": "ca.crt"}},
                                 "auth": {"serviceAccount": {"name": f"{n}-backup"}}}}}},
        "externalsecret.yaml": {"apiVersion": ESO_API, "kind": "ExternalSecret", "metadata": meta(f"{n}-backup", "velero"),
                                "spec": {"refreshInterval": "1h", "secretStoreRef": {"name": f"{n}-backup", "kind": "SecretStore"},
                                         "target": {"name": f"{n}-backup", "creationPolicy": "Owner",
                                                    "template": {"engineVersion": "v2", "data": {"cloud": cloud}}},
                                         "data": [{"secretKey": k, "remoteRef": {"key": secret, "property": k}}
                                                  for k in ("accessKeyId", "secretAccessKey")]}},  # pragma: allowlist secret
        "storage-location.yaml": {"apiVersion": "velero.io/v1", "kind": "BackupStorageLocation", "metadata": meta(n, "velero"),
                                  "spec": {"provider": "aws", "objectStorage": {"bucket": bucket, "prefix": f"environments/{n}"},
                                           "credential": {"name": f"{n}-backup", "key": "cloud"},
                                           "config": {"region": region, "s3ForcePathStyle": path_style, "s3Url": endpoint},
                                           "accessMode": "ReadWrite"}},
        "resource-policy.yaml": {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(f"{n}-backup-policy", "velero"),
                                 "data": {"policy.yaml": policy}},
        "schedule.yaml": {"apiVersion": "velero.io/v1", "kind": "Schedule", "metadata": meta(n, "velero"),
                          "spec": {"schedule": backup["schedule"], "useOwnerReferencesInBackup": False,
                                   "template": {"includedNamespaces": [ns], "excludedResources": list(VELERO_EXCLUDED),
                                                "labelSelector": {"matchExpressions": [
                                                    {"key": "cnpg.io/cluster", "operator": "DoesNotExist"},
                                                    {"key": "app.kubernetes.io/name", "operator": "NotIn", "values": [to, *caches]},
                                                    {"key": "app.kubernetes.io/instance", "operator": "NotIn", "values": [f"{app}-{to}"]}]},
                                                "storageLocation": n, "ttl": f"{hours}h0m0s", "defaultVolumesToFsBackup": True,
                                                "snapshotVolumes": False, "resourcePolicy": {"kind": "configmap", "name": f"{n}-backup-policy"}}}},
    }
    files["kustomization.yaml"] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": sorted(files)}
    return files


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


def broker_files(app: str, name: str, env: str, cfg: dict, services: list[dict], server: str) -> dict[str, dict | str]:
    """The in-cluster broker instance (ADR-065): the NATS server as a nested Argo CD Application of the official chart, a credentials
    Job (spec 128) that writes the user passwords once, and the NACK Account, Streams and Consumers. Nothing secret is rendered; streams and consumers are never deleted by a sync."""
    conf = cfg["resources"][name]["envs"][env]
    n, ns = f"{app}-{name}", f"{app}-{env}"
    url = f"nats://{n}.{ns}.svc:4222"
    labels = lambda: {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}  # noqa: E731
    wave = lambda w: {"argocd.argoproj.io/sync-wave": w}  # noqa: E731
    events = {a: e for a, e in cfg["events"].items() if e["broker"] == name}
    users = [s for s in services if env in envs_of(s) and any(a in events for a in [*s["provides"], *s["consumes"]])]
    secret_env = lambda c: c.upper().replace("-", "_")  # noqa: E731

    files: dict[str, dict | str] = {}
    secrets_plan = [{"name": f"{n}-nack", "labels": labels(), "generate": {"password": 32}, "data": {"user": "_nack"}}]  # pragma: allowlist secret
    verify_users = [{"secret": f"{n}-nack", "key": "password", "user": "_nack", "env": "ADMIN_PASSWORD"}]  # pragma: allowlist secret
    auth = [{"user": "_nack", "password": "<< $ADMIN_PASSWORD >>"}]  # pragma: allowlist secret
    cenv: dict = {"ADMIN_PASSWORD": {"valueFrom": {"secretKeyRef": {"name": f"{n}-nack", "key": "password"}}}}  # pragma: allowlist secret
    for s in users:
        c = s["name"]
        secrets_plan.append({"name": f"{n}-{c}-app", "labels": labels(), "generate": {"password": 32},  # pragma: allowlist secret
                             "data": {"url": url, "user": c}})
        verify_users.append({"secret": f"{n}-{c}-app", "key": "password", "user": c, "env": f"USER_{secret_env(c)}_PASSWORD"})  # pragma: allowlist secret
        publish = [f"{app}.{a}.>" for a in events if a in s["provides"]]
        for a in events:
            if a in s["consumes"]:
                st = f"{app}-{a}"
                publish += [f"$JS.API.CONSUMER.INFO.{st}.{c}", f"$JS.API.CONSUMER.MSG.NEXT.{st}.{c}", f"$JS.ACK.{st}.{c}.>", f"$JS.ACK.*.*.{st}.{c}.>"]
        auth.append({"user": c, "password": f"<< $USER_{secret_env(c)}_PASSWORD >>",  # pragma: allowlist secret
                     "permissions": {"publish": {"allow": publish}, "subscribe": {"allow": [f"_INBOX.{c}.>"]}}})
        cenv[f"USER_{secret_env(c)}_PASSWORD"] = {"valueFrom": {"secretKeyRef": {"name": f"{n}-{c}-app", "key": "password"}}}  # pragma: allowlist secret
    sec = lambda: {"runAsNonRoot": True, "runAsUser": 1000, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,  # noqa: E731
                   "capabilities": {"drop": ["ALL"]}}
    image, _, digest = conf["pin"].partition("@")
    repository, tag = image.rsplit(":", 1)
    tag = f"{tag}@{digest}" if digest else tag
    replicas = conf["replicas"]
    files["server.yaml"] = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": f"{app}-{name}-{env}-nats", "namespace": "argocd", "labels": labels()},
        "spec": {"project": "default",
                 "source": {"repoURL": NATS_CHART["repo_url"], "chart": NATS_CHART["chart"], "targetRevision": conf["chart_version"],
                            "helm": {"releaseName": n, "valuesObject": {
                                "nameOverride": name, "fullnameOverride": n,
                                "global": {"labels": {"app.kubernetes.io/part-of": app}},
                                "config": {"cluster": {"enabled": True, "replicas": 3} if replicas == 3 else {"enabled": False},
                                           "jetstream": {"enabled": True, "fileStore": {"enabled": True, "pvc": {"enabled": True, "size": conf["storage"]}},
                                                         "memoryStore": {"enabled": False}},
                                           "monitor": {"enabled": True, "port": 8222},
                                           "merge": {"authorization": {"users": auth}}},
                                "container": {"image": {"repository": repository, "tag": tag}, "env": cenv,
                                              "merge": {"resources": conf["resources"] or BROKER_DEFAULT_RESOURCES, "securityContext": sec()}},
                                "reloader": {"enabled": True, "merge": {"securityContext": sec()}},
                                "podTemplate": {"merge": {"spec": {"automountServiceAccountToken": False,
                                                                   "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                                                                       "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}}}}},
                                "service": {"ports": {"monitor": {"enabled": True}}},
                                "natsBox": {"enabled": False}}}},
                 "destination": {"server": server, "namespace": ns},
                 "syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}}
    files["account.yaml"] = {
        "apiVersion": "jetstream.nats.io/v1beta2", "kind": "Account",
        "metadata": {"name": n, "labels": labels(),
                     "annotations": {**wave("1"), "argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true"}},
        "spec": {"name": n, "servers": [url], "user": {"secret": {"name": f"{n}-nack"}, "user": "user", "password": "password"}}}  # pragma: allowlist secret
    keep = {"argocd.argoproj.io/sync-options": "Delete=false,SkipDryRunOnMissingResource=true",
            "argocd.argoproj.io/compare-options": "IgnoreExtraneous"}
    for a, e in events.items():
        st = f"{app}-{a}"
        files[f"stream-{a}.yaml"] = {
            "apiVersion": "jetstream.nats.io/v1beta2", "kind": "Stream",
            "metadata": {"name": st, "labels": labels(), "annotations": {**wave("2"), **keep}},
            "spec": {"name": st, "subjects": [f"{app}.{a}.>"], "storage": "file", "replicas": replicas, "retention": "limits",
                     "discard": "old", "maxAge": "168h", "account": n, "preventDelete": True}}
        for s in users:
            if a in s["consumes"]:
                c = s["name"]
                files[f"consumer-{a}.{c}.yaml"] = {
                    "apiVersion": "jetstream.nats.io/v1beta2", "kind": "Consumer",
                    "metadata": {"name": f"{st}.{c}", "labels": labels(), "annotations": {**wave("3"), **keep}},
                    "spec": {"streamName": st, "durableName": c, "deliverPolicy": "all", "ackPolicy": "explicit", "ackWait": "30s",
                             "maxDeliver": -1, "account": n, "preventDelete": True}}
    plan = {"namespace": ns, "secrets": secrets_plan, "retire": {"externalSecrets": [x["name"] for x in secrets_plan], "generators": [f"{n}-password"]},
            "verify": {"engine": "nats", "port": 4222, "pods": [f"{n}-{i}" for i in range(replicas)], "users": verify_users}}
    files.update(credentials_files(app, env, n, labels(), plan))
    files["kustomization.yaml"] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": sorted(files)}
    return files


def addon_files(app: str, name: str, env: str, cfg: dict, services: list[dict], app_dir: Path | None = None,
                server: str = "https://kubernetes.default.svc") -> dict[str, dict | str]:
    if name == "observability":
        return observability_files(app, env, cfg["observability"][env], services)
    res = cfg["resources"][name]
    if res["type"] == "bucket":
        return bucket_files(app, name, env, res["envs"][env], is_backup_target(cfg, env, name))
    if res["type"] == "cache":
        return cache_files(app, name, env, res["envs"][env])
    if res["type"] == "broker":
        return broker_files(app, name, env, cfg, services, server)
    conf = res["envs"][env]            # database
    backup = conf.get("backup")
    base = f"{app}-{name}"
    clusters, archives = read_restore_state(app_dir, env, name) if app_dir is not None else ([{"name": base, "role": "active"}], [])
    files: dict[str, dict | str] = {}
    for c in clusters:
        fname = "cluster.yaml" if c["name"] == base else f"cluster-g{c['name'][len(base) + 2:]}.yaml"
        files[fname] = database_cluster(app, name, env, conf, c["name"], c["role"], c.get("from"), bool(backup), archives, c.get("archive") if c["role"] == "previous" else None)
    if backup:
        labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": app, "app.kubernetes.io/managed-by": "gitops-app"}
        bucket, endpoint, secret = backup_target(app, env, cfg, backup["to"])
        active = next(c["name"] for c in clusters if c["role"] == "active")
        files["objectstore.yaml"] = {
            "apiVersion": "barmancloud.cnpg.io/v1", "kind": "ObjectStore", "metadata": {"name": base, "labels": dict(labels)},
            "spec": {"retentionPolicy": backup["retention"],
                     "configuration": {"destinationPath": f"s3://{bucket}/databases", "endpointURL": endpoint,
                                       "s3Credentials": {k: {"name": secret, "key": k} for k in ("accessKeyId", "secretAccessKey", "region")},  # pragma: allowlist secret
                                       "wal": {"compression": "gzip"}, "data": {"compression": "gzip"}}}}
        files["scheduledbackup.yaml"] = {
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "ScheduledBackup", "metadata": {"name": base, "labels": dict(labels)},
            "spec": {"schedule": f"0 {backup['schedule']}", "immediate": True, "backupOwnerReference": "none", "cluster": {"name": active},
                     "method": "plugin", "pluginConfiguration": {"name": BARMAN_PLUGIN}}}
    files["kustomization.yaml"] = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": sorted(files)}
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
        scaled = scaled_instances(services, env)
        comp_sync: dict = {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]}
        if scaled:
            comp_sync["syncOptions"].append("RespectIgnoreDifferences=true")
            if cfg["autoscaling"][env] == "keda":
                comp_sync["retry"] = RETRY      # a ScaledObject may sync before KEDA's CRD exists
        if any(env in envs_of(s) and "external" not in s and any(x.get("generate") for x in s.get("secrets") or []) for s in services):
            comp_sync["retry"] = RETRY      # a PreSync credentials Job may fail once
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
                        "syncPolicy": comp_sync,
                        **({"ignoreDifferences": [{"group": "apps", "kind": "Deployment", "name": n, "jsonPointers": ["/spec/replicas"]}
                                                  for n in scaled]} if scaled else {}),
                    },
                },
                **({"preservedFields": PRESERVED_FIELDS} if env in cfg["backup"] else {}),
            },
        })
    for env in ENVS:
        names = addons_in(services, env, cfg)
        if not names:
            continue
        # one ApplicationSet per prune policy: stateful resources (databases) are never pruned, config-only ones are
        for prune, suffix in ((False, "addons"), (True, "addons-config")):
            group = [n for n in names if (n == "observability") is prune]
            if not group:
                continue
            sync = {"automated": {"prune": prune, "selfHeal": True}, "syncOptions": ["CreateNamespace=true"]}
            if any(cfg["resources"].get(n, {}).get("envs", {}).get(env, {}).get("backup") for n in group):
                sync["retry"] = RETRY       # an ObjectStore may sync before its CRD exists
            elif any(cfg["resources"].get(n, {}).get("type") in ("bucket", "cache", "broker") for n in group):
                sync["retry"] = RETRY       # the Account, streams and consumers wait for NACK and the server; a PreSync credentials Job may fail once
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
                            "syncPolicy": sync,
                        },
                    },
                    **({"preservedFields": PRESERVED_FIELDS} if env in cfg["backup"] else {}),
                },
            })
        for n in names:
            for fname, doc in addon_files(app, n, env, cfg, services, app_dir, server).items():
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
    for env in ENVS:
        if env not in cfg["backup"]:
            continue
        docs.append({
            "apiVersion": "argoproj.io/v1alpha1", "kind": "ApplicationSet",
            "metadata": {"name": f"{app}-backup-{env}", "namespace": "argocd"},
            "spec": {
                "generators": [{"list": {"elements": [{"name": "environment"}]}}],
                "template": {
                    "metadata": {"name": f"{app}-backup-{env}"},
                    "spec": {
                        "project": "default",
                        "source": {"repoURL": gitops_repo, "targetRevision": "main", "path": f"applications/{app}/backup/{env}"},
                        "destination": {"server": server, "namespace": "velero"},
                        "syncPolicy": {"automated": {"prune": True, "selfHeal": True}, "retry": RETRY},
                    },
                },
            },
        })
        for fname, doc in backup_files(app, env, cfg).items():
            out[app_dir / "backup" / env / fname] = dump(doc)
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
        ext_secrets = external_secrets(app, env, services, cfg)
        for comp_name, extra in broker_secrets(app, env, services, cfg).items():
            ext_secrets.setdefault(comp_name, {}).update(extra)
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
            files = {"deployment.yaml": deployment(app, s, env, env in cfg["observability"], cfg["resources"], cfg["events"]), "service.yaml": service(app, s)}
            route = httproute(app, s, env, cfg)
            if route:
                files["httproute.yaml"] = route
            files.update(autoscaler_files(app, s, env, cfg))
            files.update(secret_manifests(app, s, env, cfg))
            files.update(ext_secrets.get(s["name"], {}))
            for fname, doc in files.items():
                out[base / fname] = dump(doc)
            tag = existing_tag(base / "kustomization.yaml") or "latest"
            out[base / "kustomization.yaml"] = dump({
                "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                "resources": sorted(files), "images": [{"name": s["image"], "newTag": tag}],
            })
    if system["autonomy"] is not None:
        out[app_dir / "autonomy.json"] = system["autonomy"]
    return out


def operator_application(name: str, server: str) -> dict:
    """The Argo CD application of one operator of the versions table (ADR-058); system and cluster repos render the same one."""
    op = OPERATORS[name]
    meta = {"name": f"operator-{name}", "namespace": "argocd",
            "labels": {"app.kubernetes.io/managed-by": "gitops-app", "app.kubernetes.io/component": "operator"},
            # the API server drops the empty annotations/labels maps some charts render on their CRDs (kyverno); the default
            # client-side diff then reports them OutOfSync forever (#261). The server-side diff compares what was applied.
            "annotations": {"argocd.argoproj.io/compare-options": "ServerSideDiff=true"}}
    if op.get("finalizer"):  # spec 117: velero leaves the cluster with its resources
        meta["finalizers"] = ["resources-finalizer.argocd.argoproj.io"]
    helm = {"releaseName": op["release"], **({"valuesObject": op["values"]} if "values" in op else {})}
    sync = {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"],
            **({"retry": OPERATOR_RETRY} if op.get("retry") else {})}
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": meta,
        "spec": {"project": "default",
                 "source": {"repoURL": op["repo_url"], "chart": op["chart"], "targetRevision": op["chart_version"], "helm": helm},
                 "destination": {"server": server, "namespace": op["namespace"]}, "syncPolicy": sync}}


def operator_files(apps: list[Path], ans: dict) -> dict[Path, str]:
    """operators/ (AC-115.18): cluster-level, outside every system's tree; only when an in-cluster database runs in an environment
    whose operators are not owned by the cluster's owner."""
    needed: set[str] = set()
    for app_dir in apps:
        system = load_system(app_dir)
        owned = system["cfg"]["operators"]
        for _, env, typ, ext in resource_instances(system):
            if not ext and env not in owned and typ in OPERATOR_OF_TYPE:
                needed.add(OPERATOR_OF_TYPE[typ])
        for r, rc in system["cfg"]["resources"].items():
            for env, inst in rc["envs"].items():
                if rc["type"] == "database" and not inst["external"] and env not in owned and inst.get("backup"):
                    needed |= {"plugin-barman-cloud", "cert-manager"}
        if any(env not in owned for env in system["cfg"]["backup"]):
            needed.add("velero")
        for env, kind in system["cfg"]["autoscaling"].items():
            if env not in owned and kind == "keda":
                needed.add("keda")
        if ans.get("resource_metrics", "provided") == "install":
            for s in system["services"]:
                if "external" not in s and any(e not in owned and s["_eff"][e].get("scale") and s["_eff"][e]["scale"]["trigger"] in ("cpu", "memory")
                                               for e in s["environments"]):
                    needed.add("metrics-server")
        if ans.get("property_operators", "owner") == "install":      # AC-121.17: a missing answer reads as owner
            needed |= property_operators(system, owned)
    if not needed:
        return {}
    server = ans.get("cluster_server", "https://kubernetes.default.svc")
    org = ans.get("github_org", "ika100")
    repo = f"https://github.com/{org}/{ans.get('project_name', apps[0].name + '-gitops')}"
    base = ROOT / "operators"
    files = {
        base / "root.yaml": dump({
            "apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "platform-operators", "namespace": "argocd"},
            "spec": {"project": "default", "source": {"repoURL": repo, "targetRevision": "main", "path": "operators/apps"},
                     "destination": {"server": server, "namespace": "argocd"},
                     "syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}}),
    }
    for name in OPERATORS:
        if name not in needed:
            continue
        files[base / "apps" / f"{name}.yaml"] = dump(operator_application(name, server))
    if "lgtm" in needed:
        files[base / "apps" / "lgtm.yaml"] = dump(lgtm_application(repo, server))
        for fname, doc in lgtm_manifests().items():
            files[base / "lgtm" / fname] = dump(doc)
    return files


def property_operators(system: dict, owned: dict) -> set[str]:
    """External Secrets, Kyverno and LGTM for the environments that are not owned (AC-121.17); only with `property_operators: install`."""
    needed: set[str] = set()
    cfg = system["cfg"]
    for env in ENVS:
        if env in owned:
            continue
        running = [s for s in system["services"] if env in envs_of(s)]
        if not running:
            continue
        comps = [s for s in running if "external" not in s]
        if (any(at(s, env).get("secrets") or at(s, env).get("secretRefs") for s in comps)
                or any(e == env and (ext or typ in ("bucket", "cache", "broker")) for _, e, typ, ext in resource_instances(system))
                or cfg["backup"].get(env)):
            needed.add("external-secrets")
        if cfg["policy"][env] != "Off":
            needed.add("kyverno")
        if (cfg["observability"].get(env) or {}).get("ui") == "lgtm":
            needed.add("lgtm")
    return needed


def lgtm_application(repo: str, server: str) -> dict:
    """The dev Grafana stack as an Argo CD application over plain manifests in operators/lgtm/ (not a chart; no finalizer)."""
    return {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "operator-lgtm", "namespace": "argocd",
                     "labels": {"app.kubernetes.io/managed-by": "gitops-app", "app.kubernetes.io/component": "operator"}},
        "spec": {"project": "default", "source": {"repoURL": repo, "targetRevision": "main", "path": "operators/lgtm"},
                 "destination": {"server": server, "namespace": "observability"},
                 "syncPolicy": {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"]}}}


def lgtm_manifests() -> dict[str, dict]:
    """deployment.yaml, service.yaml and kustomization.yaml of operators/lgtm/, as local-cluster.sh applies them (no dashboards, no route)."""
    ports = [("grafana", 3000), ("otlp-grpc", 4317), ("otlp-http", 4318), ("prometheus", 9090)]
    return {
        "deployment.yaml": {
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "lgtm", "namespace": "observability", "labels": {"app": "lgtm"}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "lgtm"}},
                     "template": {"metadata": {"labels": {"app": "lgtm"}}, "spec": {"containers": [{
                         "name": "lgtm", "image": LGTM_IMAGE, "ports": [{"name": n, "containerPort": p} for n, p in ports],
                         "readinessProbe": {"httpGet": {"path": "/api/health", "port": "grafana"}, "initialDelaySeconds": 20, "periodSeconds": 5},
                         "resources": {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}}}]}}}},
        "service.yaml": {
            "apiVersion": "v1", "kind": "Service", "metadata": {"name": "lgtm", "namespace": "observability"},
            "spec": {"selector": {"app": "lgtm"}, "ports": [{"name": n, "port": p, "targetPort": n} for n, p in ports]}},
        "kustomization.yaml": {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["deployment.yaml", "service.yaml"]},
    }


BROKER_STALE = re.compile(r"^(stream-.+|consumer-.+|externalsecret-.+)\.yaml$")     # broker files that are no longer rendered (spec 119)
CREDENTIALS_STALE = re.compile(r"^(externalsecret|password-generator|access-key-generator|secret-key-generator|nack-secret)\.yaml$|^credentials-.+\.yaml$")   # spec 128: files of the 5.4 generators

UNBOUND_NOTE = "note: applications/{app}: events API {api} is carried by no broker yet: add one with /resource:add <name> --type broker"


def render_notes(app_dir: Path) -> tuple[list[str], list[str]]:
    """(unbound events API notes, external broker notes) of one system (design.md, render notes)."""
    system = load_system(app_dir)
    app, services, cfg = app_dir.name, system["services"], system["cfg"]
    unbound = [UNBOUND_NOTE.format(app=app, api=a) for a, e in cfg["events"].items() if e["broker"] is None]
    external: list[str] = []
    for name, rc in cfg["resources"].items():
        if rc["type"] != "broker":
            continue
        for env in ENVS:
            if not rc["envs"][env]["external"]:
                continue
            apis = {a: e for a, e in cfg["events"].items() if e["broker"] == name}
            if not any(env in envs_of(s) and any(a in apis for a in [*s["provides"], *s["consumes"]]) for s in services):
                continue
            running = {s["name"] for s in services if env in envs_of(s)}
            for a, e in apis.items():
                consumers = [c for c in e["consumers"] if c in running]
                extra = f" with the durable consumers {', '.join(consumers)} (pull, explicit ack, ack wait 30s)" if consumers else ""
                external.append(f"note: applications/{app}: broker {name} is external in {env}: create stream {app}-{a} "
                                f"(subjects {app}.{a}.>, file storage, max age 7d){extra}; the platform creates none on an external broker")
    return unbound, external


def archive_notes(app_dir: Path) -> list[str]:
    """A database whose active generation runs on another major than the instance's (restored from before an upgrade, spec 121)."""
    system = load_system(app_dir)
    app, notes = app_dir.name, []
    for name, rc in system["cfg"]["resources"].items():
        if rc["type"] != "database":
            continue
        for env in ENVS:
            conf = rc["envs"][env]
            if conf["external"]:
                continue
            clusters, archives = read_restore_state(app_dir, env, name)
            active = next(c for c in clusters if c["role"] == "active")
            frm = active.get("from") or {}
            major = next((a["major"] for a in archives if a["server"] == frm.get("server")), archives[0]["major"] if archives and frm else conf["version"])
            if stepped_since(archives, active["name"], conf["version"]):
                major = conf["version"]      # a major step since the switch: the active generation runs the effective major
            if major != conf["version"]:
                notes.append(f"note: applications/{app}: {name} in {env} runs {active['name']} on {major} (restored from before the upgrade to "
                             f"{conf['version']}); "
                             + (f"discard the previous instance (/resource:restore {name} --env {env} --discard), "
                                if any(c["role"] in ("previous", "restored") for c in clusters) else "")
                             + f"set resources.{name}.instances.{env}.version: {major}, "
                             f"then /resource:upgrade {name} --to {conf['version']} --env {env}")
    return notes


def retired_notes(app_dir: Path, stale: list[Path]) -> list[str]:
    """Notes for the streams and consumers whose files a render removes (read before they are deleted)."""
    notes: list[tuple[str, str]] = []
    for st in stale:
        for f in sorted(st.rglob("*.yaml")) if st.is_dir() else [st]:
            rel = f.relative_to(app_dir).parts
            if len(rel) != 4 or rel[0] != "addons" or not (rel[3].startswith("stream-") or rel[3].startswith("consumer-")) or not f.is_file():
                continue
            try:
                doc = yaml.safe_load(f.read_text())
                kind, spec = doc["kind"], doc["spec"]
                where = f"applications/{app_dir.name}/addons/{rel[1]}/{rel[2]}"
                if kind == "Stream":
                    text = f"note: {where}: stream {spec['name']} is no longer declared; it and its messages stay on the broker until you delete them"
                elif kind == "Consumer":
                    text = (f"note: {where}: durable consumer {spec['durableName']} of stream {spec['streamName']} is no longer declared; "
                            "it stays on the broker until you delete it")
                else:
                    continue
            except (yaml.YAMLError, KeyError, TypeError):
                continue
            notes.append((str(f), text))
    return [t for _, t in sorted(notes)]


def stale_paths(app_dir: Path, wanted: set[Path]) -> list[Path]:
    """Overlay component dirs for components/envs no longer declared, and generated files no longer produced."""
    stale: list[Path] = []
    if (app_dir / "autonomy.json").is_file() and app_dir / "autonomy.json" not in wanted:
        stale.append(app_dir / "autonomy.json")
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
    for kind in ("addons", "policies", "backup"):
        top = app_dir / kind
        if top.is_dir() and not any(top in p.parents for p in wanted):
            stale.append(top)          # nothing of this kind is wanted any more: remove the whole directory
            continue
        for env in ENVS:
            base = app_dir / kind / env
            if not base.is_dir():
                continue
            if kind in ("policies", "backup"):
                if not any(p.parent == base for p in wanted):
                    stale.append(base)
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                if not any(p.parent == d for p in wanted):
                    stale.append(d)
                    continue
                # database files that are no longer rendered; restore-state.yaml is input and never touched
                stale += [f for f in d.iterdir() if f not in wanted and (f.name in DATABASE_FILES or DATABASE_GENERATION.match(f.name)
                                                                         or BROKER_STALE.match(f.name) or CREDENTIALS_STALE.match(f.name))]
    return stale


def unmigrated_dirs() -> list[Path]:
    top = ROOT / "applications"
    if not top.is_dir():
        return []
    return sorted(p for p in top.iterdir() if p.is_dir() and ((p / "app.yaml").is_file() or (p / "services.yaml").is_file()))


def versions_table() -> list[dict]:
    """The versions table (spec 121, ADR-069): one row per image, chart and manifest the renderer writes or the local cluster installs."""
    def image_row(name, engine, major, status, ref_digest, part=None):
        ref, digest = ref_digest.split("@", 1)
        tag = ref.rsplit(":", 1)[1]
        return {"name": name, "kind": "image", "engine": engine, "major": major, "status": status,
                "version": tag.split("-")[0] if part else tag, "ref": ref, "digest": digest, "app_version": None, "crds": None}

    def status_of(engine, major):
        return "deprecated" if major in DEPRECATED_MAJORS.get(engine, ()) else "supported"

    rows = [image_row(f"postgres {m}", "database", m, status_of("database", m), ref, True) for m, ref in POSTGRES_IMAGES.items()]
    rows += [image_row(f"valkey {m}", "cache", m, status_of("cache", m), ref) for m, ref in VALKEY_IMAGES.items()]
    rows += [image_row(f"nats {m}", "broker", m, status_of("broker", m), ref, True) for m, ref in NATS_IMAGES.items()]
    rows.append({"name": "nats", "kind": "chart", "engine": "broker", "major": None, "status": None, "version": NATS_CHART["chart_version"],
                 "ref": f"{NATS_CHART['repo_url']} {NATS_CHART['chart']}", "digest": None, "app_version": None, "crds": None})
    rows += [image_row("seaweedfs", "bucket", None, None, SEAWEEDFS_IMAGE),
             image_row("opentelemetry-collector", None, None, None, COLLECTOR_IMAGE),
             image_row("lgtm", None, None, None, LGTM_IMAGE),
             image_row("velero-plugin-for-aws", None, None, None, VELERO_AWS_PLUGIN_IMAGE),
             image_row("credentials-job", None, None, None, CREDENTIALS_IMAGE)]

    def manifest_row(name):
        pin = CLUSTER_PINS[name]
        return {"name": name, "kind": "manifest", "engine": None, "major": None, "status": None, "version": pin["version"],
                "ref": pin["manifest"], "digest": None, "app_version": None, "crds": None}

    rows.append(manifest_row("argo-cd"))
    rows.append(image_row("k3s", None, None, None, CLUSTER_PINS["k3s"]["image"]))
    rows.append(manifest_row("gateway-api"))
    rows += [{"name": f"operator {n}", "kind": "chart", "engine": None, "major": None, "status": None, "version": op["chart_version"],
              "ref": f"{op['repo_url']} {op['chart']}", "digest": None, "app_version": op["operator_version"], "crds": op["crds"]}
             for n, op in OPERATORS.items()]
    return rows


def nothing_compared() -> str:
    """The line of a --check that compared no rendered file (spec 130); the gate name comes from the recipe that runs the check."""
    gate = os.environ.get("GATE_NAME") or "render-check"
    return (f"{gate}: checked nothing: 0 files compared - "
            "run `devbox run render` and check that applications/<system>/system.yaml exists")


def main() -> int:
    if "--versions" in sys.argv[1:]:
        print(json.dumps(versions_table(), indent=2))
        return 0
    check = "--check" in sys.argv[1:]
    old = unmigrated_dirs()
    if old:
        for d in old:
            print(f"ERROR: applications/{d.name}: {NOT_MIGRATED}", file=sys.stderr)
        return 1
    ans = answers()
    apps = sorted(p.parent for p in (ROOT / "applications").glob("*/system.yaml"))
    if not apps:
        if check:
            print(nothing_compared(), file=sys.stderr)
        else:
            print("ERROR: no applications/*/system.yaml found", file=sys.stderr)
        return 1
    drift: list[str] = []
    retired: list[str] = []
    try:
        plans = [(app_dir, render_app(app_dir, ans)) for app_dir in apps]     # every system is validated before anything is written
        operators = operator_files(apps, ans)
        unbound_notes: list[str] = []
        external_notes: list[str] = []
        for app_dir in apps:
            u, x = render_notes(app_dir)
            unbound_notes += u
            external_notes += x + archive_notes(app_dir)
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
        stale = stale_paths(app_dir, set(rendered))
        retired += retired_notes(app_dir, stale)
        for st in stale:
            drift.append(f"{st.relative_to(ROOT)} (stale)")
            if not check:
                shutil.rmtree(st) if st.is_dir() else st.unlink()
    for path, content in operators.items():
        if not path.is_file() or path.read_text() != content:
            drift.append(str(path.relative_to(ROOT)))
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
    op_dir = ROOT / "operators"
    if op_dir.is_dir():
        for f in sorted(p for p in op_dir.rglob("*") if p.is_file() and p not in operators):
            drift.append(f"{f.relative_to(ROOT)} (stale)")
            if not check:
                f.unlink()
        if not check:
            for d in sorted((p for p in op_dir.rglob("*") if p.is_dir()), reverse=True) + [op_dir]:
                if not any(d.iterdir()):
                    d.rmdir()
    for note in [*unbound_notes, *external_notes, *retired]:
        print(note)
    if check and drift:
        print("ERROR: rendered files are out of date — run `devbox run render`:", file=sys.stderr)
        for d in drift:
            print(f"  {d}", file=sys.stderr)
        return 1
    if check and sum(len(rendered) for _, rendered in plans) + len(operators) == 0:
        print(nothing_compared(), file=sys.stderr)
        return 1
    print("OK: up to date" if check else f"rendered ({len(drift)} file(s) changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
