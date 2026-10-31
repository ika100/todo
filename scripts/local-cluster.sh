#!/usr/bin/env bash
# Local k3d cluster for this product: Gateway API (Traefik) and, by default, ArgoCD with access to the private GitHub
# repo and GHCR images, then the root Application. For trying the application on your machine; real clusters are
# bootstrapped by a human with `KUBE_CONTEXT=<ctx> devbox run bootstrap`.
#
#   local-cluster.sh up      create (or reuse) the cluster, install the Gateway API (+ ArgoCD, credentials, root app)
#   local-cluster.sh down    delete the cluster (and its registry)
#   local-cluster.sh ui      open k9s on the cluster, in the <app>-dev namespace (extra arguments go to k9s)
#
# Environment:
#   LOCAL_CLUSTER        cluster name (default <app>-local)
#   LOCAL_HTTP_PORT      host port of the Gateway (unset: 8088, or the next free port when 8088 is busy; `auto`: the first free port; an explicit busy port is reported
#                        with its owner before anything is created)
#   ARGOCD_VERSION       default v3.4.12 (the versions table of this platform release)
#   K3S_IMAGE            default rancher/k3s:v1.32.5-k3s1@sha256:b75eac801bbc91b4584769c840f346683c7740d72f83e630088370f8e1eb2c8a — the version the Gateway setup below is verified against
#                        (k3d's own default may be older and ship a Traefik without Gateway API support)
#   Every default of this script equals its row of the versions table (`cplat platform versions`). A variable that sets a
#   different value prints `WARNING: <VAR>=<value> overrides the versions table (<row> <table version>); this cluster does not
#   run what this platform release pins` before anything is created.
#   GATEWAY_API_VERSION  only used if k3s' Traefik chart did not install the Gateway API CRDs (default v1.2.1)
#   GH_TOKEN             default `gh auth token`; used for both credentials below unless they are set (ArgoCD mode only)
#   REPO_TOKEN           least-privilege token for ArgoCD to read this repo: a fine-grained PAT with only
#                        "Contents: read" on the GitOps repo (default GH_TOKEN)
#   PULL_TOKEN           token for the image pull secret: a classic PAT with only read:packages (default GH_TOKEN)
#   WITH_ARGO=0          Gateway + namespaces only: no ArgoCD, no GitHub credentials, no root Application
#                        (used by the platform's end-to-end test, which applies the rendered manifests directly)
#   WITH_SYSTEM=0        cluster only (a cluster repo, no applications/): needs LOCAL_CLUSTER; `up` stops after Argo CD and its
#                        credential, before the namespaces and bootstrap/ (default 1)
#   ESO_VERSION          External Secrets Operator chart version (default 2.12.0)
#   WITH_ESO=0           skip External Secrets Operator (no `secrets:` support in that cluster)
#   LGTM_VERSION         grafana/otel-lgtm image tag for `observability: {ui: lgtm}` (default 0.35.0, with its digest)
#   WITH_LGTM            1 = install the dev Grafana stack, 0 = never; default: only if system.yaml says `ui: lgtm`
#   KYVERNO_VERSION      Kyverno chart version (default 3.9.1)
#   WITH_KYVERNO         1 = install Kyverno, 0 = never; default: only if an environment's policy in system.yaml is not off
#   CNPG_VERSION         CloudNativePG operator chart version (default: the targetRevision of operators/apps/cloudnative-pg.yaml, else 0.29.1)
#   WITH_CNPG            1 = CloudNativePG, 0 = never; default: 1 exactly when operators/apps/cloudnative-pg.yaml exists.
#                        With Argo CD the operator comes from operators/root.yaml (applied before bootstrap/); only with
#                        WITH_ARGO=0 does this script install it through a k3s HelmChart.
#   SEAWEEDFS_OPERATOR_VERSION  SeaweedFS operator chart version (default: the targetRevision of operators/apps/seaweedfs.yaml, else 0.1.45)
#   WITH_SEAWEEDFS       1 = SeaweedFS operator, 0 = never; default: 1 exactly when operators/apps/seaweedfs.yaml exists.
#                        Installed like CloudNativePG: from operators/root.yaml with Argo CD, a k3s HelmChart with WITH_ARGO=0.
#   BARMAN_CLOUD_VERSION  Barman Cloud plugin chart version (default: the targetRevision of operators/apps/plugin-barman-cloud.yaml, else 0.8.1)
#   CERT_MANAGER_VERSION  cert-manager chart version (default: the targetRevision of operators/apps/cert-manager.yaml, else v1.21.3)
#   WITH_BARMAN_CLOUD    1 = Barman Cloud plugin (and cert-manager, which it needs), 0 = never; default: 1 exactly when
#                        operators/apps/plugin-barman-cloud.yaml exists.
#   VELERO_VERSION       Velero chart version (default: the targetRevision of operators/apps/velero.yaml, else 12.2.1)
#   VELERO_AWS_PLUGIN_IMAGE  Velero AWS plugin image (default velero/velero-plugin-for-aws:v1.14.4@sha256:c85a7fa803569e3128c33e4f5766f84ab156c8d171c9f1dd2715581d45ce98fa)
#   WITH_VELERO          1 = Velero, 0 = never; default: 1 exactly when operators/apps/velero.yaml exists.
#                        All three are installed like CloudNativePG: from operators/root.yaml with Argo CD, k3s HelmCharts with WITH_ARGO=0.
#   NACK_VERSION         NACK (NATS JetStream controller) chart version (default: the targetRevision of operators/apps/nack.yaml, else 0.35.0)
#   WITH_NACK            1 = NACK, 0 = never; default: 1 exactly when operators/apps/nack.yaml exists. Installed like CloudNativePG.
#   KEDA_VERSION         KEDA chart version (default: the targetRevision of operators/apps/keda.yaml, else 2.19.0)
#   WITH_KEDA            1 = KEDA, 0 = never; default: 1 exactly when operators/apps/keda.yaml exists. Installed like CloudNativePG.
#   METRICS_SERVER_VERSION  metrics-server chart version (default: the targetRevision of operators/apps/metrics-server.yaml, else 3.14.0)
#   WITH_METRICS_SERVER  1 = the pinned metrics-server, 0 = never; default: 1 exactly when operators/apps/metrics-server.yaml exists.
#                        With 1, a new cluster is created without k3s' bundled metrics-server.
#   REGISTRY_PORT        also create a local image registry reachable as localhost:<port> from the host and as
#                        k3d-<cluster>-registry:<port> from the cluster
# With Argo CD, External Secrets, Kyverno and the LGTM stack come from operators/apps/<name>.yaml (applied from operators/root.yaml) when
# that file exists; otherwise (or with WITH_ARGO=0) this script installs them itself.
# Re-running `up` is safe; it re-applies credentials and the root Application.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=ports.sh
. "$here/ports.sh"
cd "$here/.."
with_system="${WITH_SYSTEM:-1}"
if [ "$with_system" = 0 ]; then
  app=""
  if [ -z "${LOCAL_CLUSTER:-}" ]; then echo "ERROR: WITH_SYSTEM=0 needs LOCAL_CLUSTER" >&2; exit 2; fi
else
  app=$(basename "$(find applications -mindepth 1 -maxdepth 1 -type d | sort | head -1)")
fi
name="${LOCAL_CLUSTER:-$app-local}"
ctx="k3d-$name"
port="${LOCAL_HTTP_PORT:-default}"
base_port="${LOCAL_HTTP_PORT_BASE:-8088}"   # the preferred port when LOCAL_HTTP_PORT is unset
argocd_version="${ARGOCD_VERSION:-v3.4.12}"
k3s_requested="${K3S_IMAGE:-}"; unset K3S_IMAGE
k3s_pinned="${K3S_IMAGE:-rancher/k3s:v1.32.5-k3s1@sha256:b75eac801bbc91b4584769c840f346683c7740d72f83e630088370f8e1eb2c8a}"
k3s_image="${k3s_requested:-$k3s_pinned}"
gateway_api_version="${GATEWAY_API_VERSION:-v1.2.1}"
with_argo="${WITH_ARGO:-1}"
registry_port="${REGISTRY_PORT:-}"
eso_version="${ESO_VERSION:-2.12.0}"
with_eso="${WITH_ESO:-1}"
cnpg_file=operators/apps/cloudnative-pg.yaml
cnpg_default=0.29.1
if [ -f "$cnpg_file" ]; then
  cnpg_default=$(sed -n 's/^[[:space:]]*targetRevision:[[:space:]]*["'"'"']\{0,1\}\([^"'"'"'[:space:]]*\).*/\1/p' "$cnpg_file" | head -1)
  cnpg_default="${cnpg_default:-0.29.1}"
fi
cnpg_version="${CNPG_VERSION:-$cnpg_default}"
with_cnpg="${WITH_CNPG:-auto}"
seaweedfs_file=operators/apps/seaweedfs.yaml
seaweedfs_default=0.1.45
if [ -f "$seaweedfs_file" ]; then
  seaweedfs_default=$(sed -n 's/^[[:space:]]*targetRevision:[[:space:]]*["'"'"']\{0,1\}\([^"'"'"'[:space:]]*\).*/\1/p' "$seaweedfs_file" | head -1)
  seaweedfs_default="${seaweedfs_default:-0.1.45}"
fi
seaweedfs_version="${SEAWEEDFS_OPERATOR_VERSION:-$seaweedfs_default}"
with_seaweedfs="${WITH_SEAWEEDFS:-auto}"
kyverno_version="${KYVERNO_VERSION:-3.9.1}"
with_kyverno="${WITH_KYVERNO:-auto}"
if [ "$with_kyverno" = auto ] && [ "$with_system" = 0 ]; then with_kyverno=0; fi
if [ "$with_kyverno" = auto ]; then
  # Kyverno when any environment's policy is not off (an environment without `policy:` gets audit/audit/enforce)
  with_kyverno=$(awk '
    /^environments:/ {inenv = 1; next}
    /^[^ #]/ {inenv = 0}
    inenv && /^  [A-Za-z0-9_-]+:/ {envs++}
    inenv && /^    policy:[[:space:]]*["'"'"']?off["'"'"']?[[:space:]]*(#.*)?$/ {off++}
    END {print (envs > 0 && off >= envs) ? 0 : 1}' applications/*/system.yaml 2>/dev/null || echo 0)
fi
lgtm_pinned="grafana/otel-lgtm:0.35.0@sha256:2de1094c593c671cbfca878a28ffcd7e46ff5e32f2ce966c0cf33003f6cf4266"
lgtm_default="${lgtm_pinned%%@*}"; lgtm_default="${lgtm_default##*:}"
lgtm_version="${LGTM_VERSION:-$lgtm_default}"
if [ "$lgtm_version" = "$lgtm_default" ]; then lgtm_image="$lgtm_pinned"; else lgtm_image="grafana/otel-lgtm:${lgtm_version}"; fi
with_lgtm="${WITH_LGTM:-auto}"
if [ "$with_lgtm" = auto ] && [ "$with_system" = 0 ]; then with_lgtm=0; fi
if [ "$with_lgtm" = auto ]; then
  if grep -qE '^[^#]*ui:[[:space:]]*lgtm' applications/*/system.yaml 2>/dev/null; then with_lgtm=1; else with_lgtm=0; fi
fi
if [ "$with_cnpg" = auto ]; then
  if [ -f "$cnpg_file" ]; then with_cnpg=1; else with_cnpg=0; fi
fi
if [ "$with_seaweedfs" = auto ]; then
  if [ -f "$seaweedfs_file" ]; then with_seaweedfs=1; else with_seaweedfs=0; fi
fi
rev_of() {   # rev_of <file> <fallback>: the targetRevision of an operators/apps file
  local v=""
  if [ -f "$1" ]; then
    v=$(sed -n 's/^[[:space:]]*targetRevision:[[:space:]]*["'"'"']\{0,1\}\([^"'"'"'[:space:]]*\).*/\1/p' "$1" | head -1)
  fi
  echo "${v:-$2}"
}
barman_file=operators/apps/plugin-barman-cloud.yaml
certmgr_file=operators/apps/cert-manager.yaml
velero_file=operators/apps/velero.yaml
barman_version="${BARMAN_CLOUD_VERSION:-$(rev_of "$barman_file" 0.8.1)}"
certmgr_version="${CERT_MANAGER_VERSION:-$(rev_of "$certmgr_file" v1.21.3)}"
velero_version="${VELERO_VERSION:-$(rev_of "$velero_file" 12.2.1)}"
velero_plugin_requested="${VELERO_AWS_PLUGIN_IMAGE:-}"; unset VELERO_AWS_PLUGIN_IMAGE
velero_plugin_pinned="${VELERO_AWS_PLUGIN_IMAGE:-velero/velero-plugin-for-aws:v1.14.4@sha256:c85a7fa803569e3128c33e4f5766f84ab156c8d171c9f1dd2715581d45ce98fa}"
velero_aws_plugin_image="${velero_plugin_requested:-$velero_plugin_pinned}"
nack_file=operators/apps/nack.yaml
nack_version="${NACK_VERSION:-$(rev_of "$nack_file" 0.35.0)}"
with_nack="${WITH_NACK:-auto}"
keda_file=operators/apps/keda.yaml
keda_version="${KEDA_VERSION:-$(rev_of "$keda_file" 2.19.0)}"
with_keda="${WITH_KEDA:-auto}"
metrics_file=operators/apps/metrics-server.yaml
metrics_version="${METRICS_SERVER_VERSION:-$(rev_of "$metrics_file" 3.14.0)}"
with_metrics="${WITH_METRICS_SERVER:-auto}"
with_barman="${WITH_BARMAN_CLOUD:-auto}"
with_velero="${WITH_VELERO:-auto}"
if [ "$with_barman" = auto ]; then
  if [ -f "$barman_file" ]; then with_barman=1; else with_barman=0; fi
fi
if [ "$with_velero" = auto ]; then
  if [ -f "$velero_file" ]; then with_velero=1; else with_velero=0; fi
fi
if [ "$with_nack" = auto ]; then
  if [ -f "$nack_file" ]; then with_nack=1; else with_nack=0; fi
fi
if [ "$with_keda" = auto ]; then
  if [ -f "$keda_file" ]; then with_keda=1; else with_keda=0; fi
fi
if [ "$with_metrics" = auto ]; then
  if [ -f "$metrics_file" ]; then with_metrics=1; else with_metrics=0; fi
fi
# With Argo CD the property operators come from operators/apps/<name>.yaml when it exists (spec 121); a cluster-up of an older
# release left External Secrets or Kyverno as k3s HelmCharts, and those stay.
eso_file=operators/apps/external-secrets.yaml
kyverno_file=operators/apps/kyverno.yaml
lgtm_file=operators/apps/lgtm.yaml
argo_managed() {   # argo_managed <operator file>: 1 when Argo CD installs the operator from operators/root.yaml
  if [ "$with_argo" = 1 ] && [ "$with_system" != 0 ] && [ -f operators/root.yaml ] && [ -f "$1" ]; then echo 1; else echo 0; fi
}
eso_argo=0; kyverno_argo=0; lgtm_argo=0
[ "$with_eso" = 1 ] && eso_argo=$(argo_managed "$eso_file")
[ "$with_kyverno" = 1 ] && kyverno_argo=$(argo_managed "$kyverno_file")
[ "$with_lgtm" = 1 ] && lgtm_argo=$(argo_managed "$lgtm_file")
registry="${name}-registry"
org=$(sed -n 's/^github_org:[[:space:]]*//p' .copier-answers.yml | head -1)
k() { kubectl --context "$ctx" "$@"; }
wait_keda_deployments() {   # keda-operator, keda-operator-metrics-apiserver, keda-admission-webhooks (chart 2.19.0), each bounded
  local d
  for d in keda-operator keda-operator-metrics-apiserver keda-admission-webhooks; do
    for _ in $(seq 1 100); do k -n keda get "deploy/$d" >/dev/null 2>&1 && break; sleep 3; done
  done
  k -n keda rollout status deploy/keda-operator --timeout=300s >/dev/null
  k -n keda rollout status deploy/keda-operator-metrics-apiserver --timeout=300s >/dev/null
  k -n keda rollout status deploy/keda-admission-webhooks --timeout=300s >/dev/null
}
wait_resource_metrics() {   # cpu and memory scaling need metrics.k8s.io; warn, never fail
  local i
  for i in $(seq 1 40); do
    [ "$(k get apiservices.apiregistration.k8s.io/v1beta1.metrics.k8s.io -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)" = True ] && return 0
    sleep 3
  done
  echo "WARNING: the cluster serves no resource metrics yet (v1beta1.metrics.k8s.io is not Available); cpu and memory triggers cannot scale until it is"
}

overrides() {   # overrides <VAR> <requested value> <the table's default> <row> <table version>
  if [ -n "$2" ] && [ "$2" != "$3" ]; then
    echo "WARNING: $1=$2 overrides the versions table ($4 $5); this cluster does not run what this platform release pins"
  fi
}
warn_overrides() {   # one line per variable that sets something other than the table's version; before anything is created
  overrides ARGOCD_VERSION "${ARGOCD_VERSION:-}" v3.4.12 argo-cd v3.4.12
  overrides K3S_IMAGE "$k3s_requested" "$k3s_pinned" k3s "$(v="${k3s_pinned#*:}"; echo "${v%%@*}")"
  overrides GATEWAY_API_VERSION "${GATEWAY_API_VERSION:-}" v1.2.1 gateway-api v1.2.1
  overrides LGTM_VERSION "${LGTM_VERSION:-}" "$lgtm_default" lgtm "$lgtm_default"
  overrides VELERO_AWS_PLUGIN_IMAGE "$velero_plugin_requested" "$velero_plugin_pinned" velero-plugin-for-aws "$(v="${velero_plugin_pinned%%@*}"; echo "${v##*:}")"
  overrides ESO_VERSION "${ESO_VERSION:-}" 2.12.0 "operator external-secrets" 2.12.0
  overrides KYVERNO_VERSION "${KYVERNO_VERSION:-}" 3.9.1 "operator kyverno" 3.9.1
  overrides CNPG_VERSION "${CNPG_VERSION:-}" 0.29.1 "operator cloudnative-pg" 0.29.1
  overrides SEAWEEDFS_OPERATOR_VERSION "${SEAWEEDFS_OPERATOR_VERSION:-}" 0.1.45 "operator seaweedfs" 0.1.45
  overrides BARMAN_CLOUD_VERSION "${BARMAN_CLOUD_VERSION:-}" 0.8.1 "operator plugin-barman-cloud" 0.8.1
  overrides CERT_MANAGER_VERSION "${CERT_MANAGER_VERSION:-}" v1.21.3 "operator cert-manager" v1.21.3
  overrides VELERO_VERSION "${VELERO_VERSION:-}" 12.2.1 "operator velero" 12.2.1
  overrides NACK_VERSION "${NACK_VERSION:-}" 0.35.0 "operator nack" 0.35.0
  overrides KEDA_VERSION "${KEDA_VERSION:-}" 2.19.0 "operator keda" 2.19.0
  overrides METRICS_SERVER_VERSION "${METRICS_SERVER_VERSION:-}" 3.14.0 "operator metrics-server" 3.14.0
}
apply_eso_store() {   # the platform-secrets ClusterSecretStore (the cluster owner's, not an operator's) and its reader
  k apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: ServiceAccount
metadata: {name: eso-store, namespace: secrets-store}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: eso-store, namespace: secrets-store}
rules:
  - {apiGroups: [""], resources: [secrets], verbs: [get, list, watch]}
  - {apiGroups: [authorization.k8s.io], resources: [selfsubjectrulesreviews], verbs: [create]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: eso-store, namespace: secrets-store}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: Role, name: eso-store}
subjects: [{kind: ServiceAccount, name: eso-store, namespace: secrets-store}]
---
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata: {name: platform-secrets}
spec:
  provider:
    kubernetes:
      remoteNamespace: secrets-store
      server:
        caProvider: {type: ConfigMap, name: kube-root-ca.crt, namespace: secrets-store, key: ca.crt}
      auth:
        serviceAccount: {name: eso-store, namespace: secrets-store}
YAML
}

case "${1:-up}" in
  down)
    k3d cluster delete "$name"
    k3d registry delete "k3d-${registry}" >/dev/null 2>&1 || true
    ;;
  up)
    warn_overrides
    if ! k3d cluster list "$name" >/dev/null 2>&1; then    # a new cluster binds the host ports: check them first
      if [ "$port" = default ]; then   # spec 056: no LOCAL_HTTP_PORT → the preferred port, or the next free one
        port=$(free_port "$base_port") || { echo "ERROR: no free host port between $base_port and $((base_port + 50))." >&2; exit 1; }
        [ "$port" != "$base_port" ] && echo "Host port: port $base_port is busy ($(port_owner "$base_port" | grep . || echo 'another program')); using $port"
      elif [ "$port" = auto ]; then
        port=$(free_port "$base_port") || { echo "ERROR: no free host port between $base_port and $((base_port + 50))." >&2; exit 1; }
        echo "Host port: using free port $port (LOCAL_HTTP_PORT=auto)"
      fi
      if port_in_use "$port"; then
        free=$(free_port $((port + 1)) || true)
        echo "ERROR: host port $port is already in use by $(port_owner "$port" | grep . || echo 'another program')." >&2
        echo "  fix: free it, or choose another port: LOCAL_HTTP_PORT=${free:-9090} devbox run cluster-up   (or LOCAL_HTTP_PORT=auto)" >&2
        exit 1
      fi
      if [ -n "$registry_port" ] && port_in_use "$registry_port"; then
        echo "ERROR: registry port $registry_port is already in use by $(port_owner "$registry_port" | grep . || echo 'another program')." >&2
        echo "  fix: choose another REGISTRY_PORT" >&2
        exit 1
      fi
    fi

    if [ "$with_argo" = 1 ]; then
      token="${GH_TOKEN:-}"
      if [ -z "$token" ] && { [ -z "${REPO_TOKEN:-}" ] || [ -z "${PULL_TOKEN:-}" ]; }; then token=$(gh auth token); fi
      repo_token="${REPO_TOKEN:-$token}"
      pull_token="${PULL_TOKEN:-$token}"
      if [ -z "${REPO_TOKEN:-}" ] || [ -z "${PULL_TOKEN:-}" ]; then
        echo "Note: using your broad GitHub login token for ArgoCD/GHCR; REPO_TOKEN (Contents: read) and PULL_TOKEN (read:packages) limit what the cluster can do with it"
      fi
    fi
    if k3d cluster list "$name" >/dev/null 2>&1; then
      echo "Cluster $name exists - reusing it"
      if [ "$with_metrics" = 1 ]; then echo "Note: a reused cluster keeps its resource-metrics source; recreate it (cluster-down, cluster-up) to switch to the pinned metrics-server"; fi
      k3d cluster start "$name" >/dev/null 2>&1 || true
    else
      create_args=(--image "$k3s_image" -p "${port}:80@loadbalancer" --wait)
      if [ -n "$registry_port" ]; then
        k3d registry list "k3d-${registry}" >/dev/null 2>&1 || k3d registry create "$registry" --port "$registry_port" >/dev/null
        create_args+=(--registry-use "k3d-${registry}:${registry_port}")
      fi
      if [ "$with_metrics" = 1 ]; then   # WITH_METRICS_SERVER=1: one metrics source, the pinned metrics-server, not k3s' bundled one
        create_args+=(--k3s-arg "--disable=metrics-server@server:*")
      fi
      k3d cluster create "$name" "${create_args[@]}"
    fi

    echo "Enabling the Gateway API (Traefik)"
    # k3s installs Traefik with a Helm job; its CRD chart brings the Gateway API CRDs on current k3s. Wait for that job
    # first so that anything we add cannot race with it (applying the CRDs ourselves breaks the Helm install), and only
    # add the CRDs if the chart did not.
    for _ in $(seq 1 60); do k -n kube-system get job helm-install-traefik-crd >/dev/null 2>&1 && break; sleep 2; done
    k -n kube-system wait --for=condition=complete job/helm-install-traefik-crd --timeout=240s >/dev/null
    if ! k get crd gatewayclasses.gateway.networking.k8s.io >/dev/null 2>&1; then
      k apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${gateway_api_version}/standard-install.yaml" >/dev/null
    fi
    # The Gateway provider is off by default: turn it on, and let routes attach from every namespace.
    k apply -f - >/dev/null <<'YAML'
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    providers:
      kubernetesGateway:
        enabled: true
    gateway:
      listeners:
        web:
          namespacePolicy: All
YAML
    ready=0
    for _ in $(seq 1 60); do
      if k wait --for=condition=Accepted gatewayclass/traefik --timeout=1s >/dev/null 2>&1 &&
        k -n kube-system wait --for=condition=Programmed gateway/traefik-gateway --timeout=1s >/dev/null 2>&1; then ready=1; break; fi
      sleep 5
    done
    if [ "$ready" != 1 ]; then
      echo "ERROR: the Traefik Gateway did not become ready within 5 minutes." >&2
      k -n kube-system get pods,job 2>&1 | tail -15 >&2
      echo "  fix: check the k3s version (K3S_IMAGE) and 'kubectl --context $ctx -n kube-system logs deploy/traefik'" >&2
      exit 1
    fi

    if [ "$eso_argo" = 1 ] && k -n kube-system get helmchart external-secrets >/dev/null 2>&1; then
      echo "External Secrets was installed by an older cluster-up (HelmChart kube-system/external-secrets); keeping it. Recreate the cluster to let Argo CD manage it"
      eso_argo=0
    fi
    if [ "$kyverno_argo" = 1 ] && k -n kube-system get helmchart kyverno >/dev/null 2>&1; then
      echo "Kyverno was installed by an older cluster-up (HelmChart kube-system/kyverno); keeping it. Recreate the cluster to let Argo CD manage it"
      kyverno_argo=0
    fi

    if [ "$with_eso" = 1 ] && [ "$eso_argo" = 0 ]; then
      echo "Installing External Secrets Operator ($eso_version)"
      # k3s' Helm controller installs the chart, so no helm binary is needed. `generate` secrets need only the operator;
      # `remote` secrets read from the platform-secrets store: Secrets in the secrets-store namespace (set with `devbox run secret`).
      k create namespace external-secrets --dry-run=client -o yaml | k apply -f - >/dev/null
      k create namespace secrets-store --dry-run=client -o yaml | k apply -f - >/dev/null
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: external-secrets
  namespace: kube-system
spec:
  repo: https://charts.external-secrets.io
  chart: external-secrets
  version: ${eso_version}
  targetNamespace: external-secrets
  valuesContent: |-
    installCRDs: true
YAML
      for _ in $(seq 1 60); do k get crd clustersecretstores.external-secrets.io >/dev/null 2>&1 && break; sleep 3; done
      for d in external-secrets external-secrets-webhook external-secrets-cert-controller; do
        # the CRDs appear before the chart's Deployments do
        for _ in $(seq 1 100); do k -n external-secrets get "deploy/$d" >/dev/null 2>&1 && break; sleep 3; done
        k -n external-secrets rollout status "deploy/$d" --timeout=300s >/dev/null
      done
      apply_eso_store
    fi

    if [ "$with_kyverno" = 1 ] && [ "$kyverno_argo" = 0 ]; then
      echo "Installing Kyverno ($kyverno_version)"
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: kyverno
  namespace: kube-system
spec:
  repo: https://kyverno.github.io/kyverno
  chart: kyverno
  version: ${kyverno_version}
  targetNamespace: kyverno
  createNamespace: true
YAML
      for _ in $(seq 1 60); do k get crd namespacedvalidatingpolicies.policies.kyverno.io >/dev/null 2>&1 && break; sleep 3; done
      for d in kyverno-admission-controller kyverno-background-controller kyverno-reports-controller kyverno-cleanup-controller; do
        for _ in $(seq 1 100); do k -n kyverno get "deploy/$d" >/dev/null 2>&1 && break; sleep 3; done
        k -n kyverno rollout status "deploy/$d" --timeout=300s >/dev/null
      done
    fi

    if [ "$with_cnpg" = 1 ] && [ "$with_argo" = 1 ]; then
      : # Argo CD installs the operator from operators/root.yaml (applied below, before bootstrap/)
    elif [ "$with_cnpg" = 1 ]; then
      echo "Installing the CloudNativePG operator ($cnpg_version)"
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: cnpg
  namespace: kube-system
spec:
  repo: https://cloudnative-pg.github.io/charts
  chart: cloudnative-pg
  version: ${cnpg_version}
  targetNamespace: cnpg-system
  createNamespace: true
YAML
      for _ in $(seq 1 60); do k get crd clusters.postgresql.cnpg.io >/dev/null 2>&1 && break; sleep 3; done
      for _ in $(seq 1 100); do k -n cnpg-system get deploy/cnpg-cloudnative-pg >/dev/null 2>&1 && break; sleep 3; done
      k -n cnpg-system rollout status deploy/cnpg-cloudnative-pg --timeout=300s >/dev/null
    fi

    if [ "$with_seaweedfs" = 1 ] && [ "$with_argo" = 1 ]; then
      : # Argo CD installs the operator from operators/root.yaml (applied below, before bootstrap/)
    elif [ "$with_seaweedfs" = 1 ]; then
      echo "Installing the SeaweedFS operator ($seaweedfs_version)"
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: seaweedfs-operator
  namespace: kube-system
spec:
  repo: https://seaweedfs.github.io/seaweedfs-operator
  chart: seaweedfs-operator
  version: ${seaweedfs_version}
  targetNamespace: seaweedfs-operator
  createNamespace: true
YAML
      for _ in $(seq 1 60); do k get crd buckets.seaweed.seaweedfs.com >/dev/null 2>&1 && break; sleep 3; done
      for _ in $(seq 1 100); do [ -n "$(k -n seaweedfs-operator get deploy -l app.kubernetes.io/name=seaweedfs-operator -o name 2>/dev/null)" ] && break; sleep 3; done
      k -n seaweedfs-operator rollout status deploy -l app.kubernetes.io/name=seaweedfs-operator --timeout=300s >/dev/null
    fi

    if [ "$with_barman" = 1 ] && [ "$with_argo" = 1 ]; then
      : # Argo CD installs cert-manager and the plugin from operators/root.yaml (applied below, before bootstrap/)
    elif [ "$with_barman" = 1 ]; then
      echo "Installing cert-manager ($certmgr_version)"
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: cert-manager
  namespace: kube-system
spec:
  repo: https://charts.jetstack.io
  chart: cert-manager
  version: ${certmgr_version}
  targetNamespace: cert-manager
  createNamespace: true
  valuesContent: |-
    crds:
      enabled: true
YAML
      for _ in $(seq 1 60); do k get crd certificates.cert-manager.io >/dev/null 2>&1 && break; sleep 3; done
      for d in cert-manager cert-manager-webhook cert-manager-cainjector; do
        for _ in $(seq 1 100); do k -n cert-manager get "deploy/$d" >/dev/null 2>&1 && break; sleep 3; done
        k -n cert-manager rollout status "deploy/$d" --timeout=300s >/dev/null
      done
      echo "Installing the Barman Cloud plugin ($barman_version)"
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: plugin-barman-cloud
  namespace: kube-system
spec:
  repo: https://cloudnative-pg.github.io/charts
  chart: plugin-barman-cloud
  version: ${barman_version}
  targetNamespace: cnpg-system
  createNamespace: true
YAML
      for _ in $(seq 1 60); do k get crd objectstores.barmancloud.cnpg.io >/dev/null 2>&1 && break; sleep 3; done
      for _ in $(seq 1 100); do [ -n "$(k -n cnpg-system get deploy -l app.kubernetes.io/name=plugin-barman-cloud -o name 2>/dev/null)" ] && break; sleep 3; done
      k -n cnpg-system rollout status deploy -l app.kubernetes.io/name=plugin-barman-cloud --timeout=300s >/dev/null
    fi

    if [ "$with_velero" = 1 ] && [ "$with_argo" = 1 ]; then
      : # Argo CD installs Velero from operators/root.yaml (applied below, before bootstrap/)
    elif [ "$with_velero" = 1 ]; then
      echo "Installing Velero ($velero_version)"
      # valuesContent must match the velero values of OPERATORS in templates/gitops-app/scripts/render.py (the versions table)
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: velero
  namespace: kube-system
spec:
  repo: https://vmware-tanzu.github.io/helm-charts
  chart: velero
  version: ${velero_version}
  targetNamespace: velero
  createNamespace: true
  valuesContent: |-
    initContainers:
    - name: velero-plugin-for-aws
      image: ${velero_aws_plugin_image}
      volumeMounts:
      - mountPath: /target
        name: plugins
    deployNodeAgent: true
    snapshotsEnabled: false
    credentials:
      useSecret: false
    configuration:
      backupStorageLocation: []
      volumeSnapshotLocation: []
      uploaderType: kopia
YAML
      for _ in $(seq 1 60); do k get crd schedules.velero.io >/dev/null 2>&1 && break; sleep 3; done
      for _ in $(seq 1 100); do k -n velero get deploy/velero >/dev/null 2>&1 && break; sleep 3; done
      k -n velero rollout status deploy/velero --timeout=300s >/dev/null
      for _ in $(seq 1 100); do k -n velero get daemonset/node-agent >/dev/null 2>&1 && break; sleep 3; done
      k -n velero rollout status daemonset/node-agent --timeout=300s >/dev/null
    fi

    if [ "$with_nack" = 1 ] && [ "$with_argo" = 1 ] && [ ! -f "$nack_file" ]; then
      echo "WARNING: WITH_NACK=1 with Argo CD, but NACK comes from $nack_file and it does not exist; render a NATS broker first, or use WITH_ARGO=0. Skipping the NACK wait." >&2
      with_nack=0
    elif [ "$with_nack" = 1 ] && [ "$with_argo" = 1 ]; then
      : # Argo CD installs NACK from operators/root.yaml (applied below, before bootstrap/)
    elif [ "$with_nack" = 1 ]; then
      echo "Installing NACK ($nack_version)"
      # no values: the chart then runs the controller with -crd-connect (the versions table in render.py)
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: nack
  namespace: kube-system
spec:
  repo: https://nats-io.github.io/k8s/helm/charts
  chart: nack
  version: ${nack_version}
  targetNamespace: nack
  createNamespace: true
YAML
      for _ in $(seq 1 60); do k get crd streams.jetstream.nats.io >/dev/null 2>&1 && break; sleep 3; done
      for _ in $(seq 1 100); do [ -n "$(k -n nack get deploy -l app=nack -o name 2>/dev/null)" ] && break; sleep 3; done
      k -n nack rollout status deploy -l app=nack --timeout=300s >/dev/null
    fi

    if [ "$with_keda" = 1 ] && [ "$with_argo" = 1 ] && [ ! -f "$keda_file" ]; then
      echo "WARNING: WITH_KEDA=1 with Argo CD, but KEDA comes from $keda_file and it does not exist; render a queue-triggered instance first, or use WITH_ARGO=0. Skipping the KEDA wait." >&2
      with_keda=0
    elif [ "$with_keda" = 1 ] && [ "$with_argo" = 1 ]; then
      : # Argo CD installs KEDA from operators/root.yaml (applied below, before bootstrap/)
    elif [ "$with_keda" = 1 ]; then
      echo "Installing KEDA ($keda_version)"
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: keda
  namespace: kube-system
spec:
  repo: https://kedacore.github.io/charts
  chart: keda
  version: ${keda_version}
  targetNamespace: keda
  createNamespace: true
YAML
      for _ in $(seq 1 60); do k get crd scaledobjects.keda.sh >/dev/null 2>&1 && break; sleep 3; done
      wait_keda_deployments
    fi

    if [ "$with_metrics" = 1 ] && [ "$with_argo" = 1 ]; then
      : # Argo CD installs metrics-server from operators/root.yaml (applied below, before bootstrap/)
    elif [ "$with_metrics" = 1 ]; then
      echo "Installing metrics-server ($metrics_version)"
      k apply -f - >/dev/null <<YAML
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: metrics-server
  namespace: kube-system
spec:
  repo: https://kubernetes-sigs.github.io/metrics-server
  chart: metrics-server
  version: ${metrics_version}
  targetNamespace: kube-system
YAML
    fi

    if [ "$with_lgtm" = 1 ] && [ "$lgtm_argo" = 0 ]; then
      echo "Installing the Grafana dev stack (${lgtm_image%%@*}): Grafana, Tempo, Loki, Prometheus in one pod (dev only)"
      # One shared stack in its own namespace: the all-in-one image runs as root, so it stays outside the application namespaces.
      k create namespace observability --dry-run=client -o yaml | k apply -f - >/dev/null
      # Spec 101: the Claude Code usage dashboard. The image's own provider file lists three fixed files, so this adds a second
      # provider (any *.yaml in its provisioning/dashboards dir is read) that points at the mounted dashboards folder.
      # Source: grafana/docker-otel-lgtm docker/Dockerfile (provisioning dir /otel-lgtm/grafana/conf/provisioning/dashboards/).
      dash=observability/dashboards/claude-code-usage.json
      if [ -f "$dash" ]; then
        k -n observability create configmap lgtm-cplat-dashboards --from-file=claude-code-usage.json="$dash" \
          --from-literal=cplat-dashboards.yaml='{apiVersion: 1, providers: [{name: cplat, type: file, options: {path: /otel-lgtm/cplat-dashboards, foldersFromFilesStructure: false}}]}' \
          --dry-run=client -o yaml | k apply -f - >/dev/null
        dash_mounts=1
      else
        dash_mounts=0
      fi
      k apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: lgtm, namespace: observability, labels: {app: lgtm}}
spec:
  replicas: 1
  selector: {matchLabels: {app: lgtm}}
  template:
    metadata: {labels: {app: lgtm}}
    spec:
      containers:
        - name: lgtm
          image: ${lgtm_image}
          ports: [{name: grafana, containerPort: 3000}, {name: otlp-grpc, containerPort: 4317}, {name: otlp-http, containerPort: 4318}, {name: prometheus, containerPort: 9090}]
          readinessProbe: {httpGet: {path: /api/health, port: grafana}, initialDelaySeconds: 20, periodSeconds: 5}
          resources: {requests: {cpu: 200m, memory: 512Mi}, limits: {memory: 2Gi}}
$([ "$dash_mounts" = 1 ] && cat <<'MOUNTS'
          volumeMounts:
            - {name: cplat-dashboards, mountPath: /otel-lgtm/cplat-dashboards/claude-code-usage.json, subPath: claude-code-usage.json}
            - {name: cplat-dashboards, mountPath: /otel-lgtm/grafana/conf/provisioning/dashboards/cplat-dashboards.yaml, subPath: cplat-dashboards.yaml}
      volumes:
        - {name: cplat-dashboards, configMap: {name: lgtm-cplat-dashboards}}
MOUNTS
)
---
apiVersion: v1
kind: Service
metadata: {name: lgtm, namespace: observability}
spec:
  selector: {app: lgtm}
  ports:
    - {name: grafana, port: 3000, targetPort: grafana}
    - {name: otlp-grpc, port: 4317, targetPort: otlp-grpc}
    - {name: otlp-http, port: 4318, targetPort: otlp-http}
    - {name: prometheus, port: 9090, targetPort: prometheus}
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: grafana, namespace: observability}
spec:
  parentRefs: [{name: traefik-gateway, namespace: kube-system}]
  hostnames: [grafana.localhost]
  rules: [{backendRefs: [{name: lgtm, port: 3000}]}]
YAML
      k -n observability rollout status deploy/lgtm --timeout=300s >/dev/null
    fi

    if [ "$with_argo" = 1 ]; then
      echo "Installing ArgoCD ($argocd_version)"
      k create namespace argocd --dry-run=client -o yaml | k apply -f - >/dev/null
      k apply -n argocd --server-side -f "https://raw.githubusercontent.com/argoproj/argo-cd/${argocd_version}/manifests/install.yaml" >/dev/null
      for d in argocd-repo-server argocd-applicationset-controller argocd-server; do
        k -n argocd rollout status "deploy/$d" --timeout=300s
      done

      echo "Wiring credentials (GitHub org ${org})"
      # Argo reads this (private) GitOps repo with the token; services no longer need any repo access (ADR-017).
      k -n argocd create secret generic github-creds --from-literal=url="https://github.com/${org}" \
        --from-literal=username="${org}" --from-literal=password="$repo_token" --dry-run=client -o yaml | k apply -f - >/dev/null
      k -n argocd label secret github-creds argocd.argoproj.io/secret-type=repo-creds --overwrite >/dev/null
    fi

    if [ "$with_system" = 0 ]; then   # a cluster repo: no system, no namespaces, no bootstrap/ (its cluster-up does those)
      case "$port" in default | auto) port=$(docker port "k3d-${name}-serverlb" 80/tcp 2>/dev/null | head -1 | sed 's/.*://' || true) ;; esac
      echo "Cluster ${ctx} is ready (no system: WITH_SYSTEM=0) on host port ${port}"
      exit 0
    fi

    # Namespaces are created up front so the GHCR pull secret exists before the first pod; Argo reuses them.
    for env in dev staging prod; do
      ns="${app}-${env}"
      k create namespace "$ns" --dry-run=client -o yaml | k apply -f - >/dev/null
      if [ "$with_argo" = 1 ]; then
        k -n "$ns" create secret docker-registry ghcr-pull --docker-server=ghcr.io --docker-username="${org}" \
          --docker-password="$pull_token" --dry-run=client -o yaml | k apply -f - >/dev/null
        k -n "$ns" patch serviceaccount default -p '{"imagePullSecrets":[{"name":"ghcr-pull"}]}' >/dev/null
      fi
    done

    urls=""
    if [ "$with_lgtm" = 1 ]; then urls="Grafana:        http://grafana.localhost:${port}/   (admin / admin; dev stack, data is lost on restart)\n"; fi
    for host in $(grep -rhA1 'hostnames:' applications/*/overlays/*/*/httproute.yaml 2>/dev/null | grep -- '- ' | sed 's/.*- //' | sort -u); do
      urls="${urls}Exposed:        http://${host}:${port}/   (once the workloads are running)\n"
    done
    urls=$(printf '%b' "$urls")
    if [ -n "$urls" ]; then urls="${urls}"$'\n'; fi   # command substitution strips the trailing newline

    if [ "$with_argo" = 1 ]; then
      if { [ "$with_cnpg" = 1 ] || [ "$with_seaweedfs" = 1 ] || [ "$with_barman" = 1 ] || [ "$with_velero" = 1 ] || [ "$with_nack" = 1 ] || [ "$with_keda" = 1 ] || [ "$with_metrics" = 1 ] || [ "$eso_argo" = 1 ] || [ "$kyverno_argo" = 1 ] || [ "$lgtm_argo" = 1 ]; } && [ -f operators/root.yaml ]; then
        legacy=0
        if [ "$with_cnpg" = 1 ] && k -n kube-system get helmchart cnpg >/dev/null 2>&1; then
          echo "CloudNativePG was installed by an older cluster-up (HelmChart kube-system/cnpg); keeping it. Recreate the cluster to let Argo CD manage it"
          legacy=1
        fi
        if [ "$with_seaweedfs" = 1 ] && [ "$legacy" = 1 ]; then
          echo "SeaweedFS (operators/apps/seaweedfs.yaml) is not installed either; recreate the cluster"
        fi
        if [ "$legacy" = 0 ]; then
          echo "Applying the operators (operators/root.yaml)"
          k apply -n argocd -f operators/root.yaml
          if [ "$eso_argo" = 1 ]; then
            for _ in $(seq 1 200); do k get crd clustersecretstores.external-secrets.io >/dev/null 2>&1 && break; sleep 3; done
            for d in external-secrets external-secrets-webhook external-secrets-cert-controller; do
              for _ in $(seq 1 100); do k -n external-secrets get "deploy/$d" >/dev/null 2>&1 && break; sleep 3; done
              k -n external-secrets rollout status "deploy/$d" --timeout=300s >/dev/null
            done
            k create namespace secrets-store --dry-run=client -o yaml | k apply -f - >/dev/null
            apply_eso_store
          fi
          if [ "$kyverno_argo" = 1 ]; then
            for _ in $(seq 1 200); do k get crd namespacedvalidatingpolicies.policies.kyverno.io >/dev/null 2>&1 && break; sleep 3; done
            for d in kyverno-admission-controller kyverno-background-controller kyverno-reports-controller kyverno-cleanup-controller; do
              for _ in $(seq 1 100); do k -n kyverno get "deploy/$d" >/dev/null 2>&1 && break; sleep 3; done
              k -n kyverno rollout status "deploy/$d" --timeout=300s >/dev/null
            done
          fi
          if [ "$lgtm_argo" = 1 ]; then
            for _ in $(seq 1 100); do k -n observability get deploy/lgtm >/dev/null 2>&1 && break; sleep 3; done
            k -n observability rollout status deploy/lgtm --timeout=300s >/dev/null
            k apply -f - >/dev/null <<'YAML'
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: grafana, namespace: observability}
spec:
  parentRefs: [{name: traefik-gateway, namespace: kube-system}]
  hostnames: [grafana.localhost]
  rules: [{backendRefs: [{name: lgtm, port: 3000}]}]
YAML
          fi
          if [ "$with_cnpg" = 1 ]; then
            for _ in $(seq 1 200); do k get crd clusters.postgresql.cnpg.io >/dev/null 2>&1 && break; sleep 3; done
            for _ in $(seq 1 100); do k -n cnpg-system get deploy/cnpg-cloudnative-pg >/dev/null 2>&1 && break; sleep 3; done
            k -n cnpg-system rollout status deploy/cnpg-cloudnative-pg --timeout=300s >/dev/null
          fi
          if [ "$with_seaweedfs" = 1 ]; then
            for _ in $(seq 1 200); do k get crd buckets.seaweed.seaweedfs.com >/dev/null 2>&1 && break; sleep 3; done
            for _ in $(seq 1 100); do [ -n "$(k -n seaweedfs-operator get deploy -l app.kubernetes.io/name=seaweedfs-operator -o name 2>/dev/null)" ] && break; sleep 3; done
            k -n seaweedfs-operator rollout status deploy -l app.kubernetes.io/name=seaweedfs-operator --timeout=300s >/dev/null
          fi
          if [ "$with_barman" = 1 ]; then
            for _ in $(seq 1 200); do k get crd certificates.cert-manager.io >/dev/null 2>&1 && break; sleep 3; done
            for d in cert-manager cert-manager-webhook cert-manager-cainjector; do
              for _ in $(seq 1 100); do k -n cert-manager get "deploy/$d" >/dev/null 2>&1 && break; sleep 3; done
              k -n cert-manager rollout status "deploy/$d" --timeout=300s >/dev/null
            done
            for _ in $(seq 1 200); do k get crd objectstores.barmancloud.cnpg.io >/dev/null 2>&1 && break; sleep 3; done
            for _ in $(seq 1 100); do [ -n "$(k -n cnpg-system get deploy -l app.kubernetes.io/name=plugin-barman-cloud -o name 2>/dev/null)" ] && break; sleep 3; done
            k -n cnpg-system rollout status deploy -l app.kubernetes.io/name=plugin-barman-cloud --timeout=300s >/dev/null
          fi
          if [ "$with_velero" = 1 ]; then
            for _ in $(seq 1 200); do k get crd schedules.velero.io >/dev/null 2>&1 && break; sleep 3; done
            for _ in $(seq 1 100); do k -n velero get deploy/velero >/dev/null 2>&1 && break; sleep 3; done
            k -n velero rollout status deploy/velero --timeout=300s >/dev/null
            for _ in $(seq 1 100); do k -n velero get daemonset/node-agent >/dev/null 2>&1 && break; sleep 3; done
            k -n velero rollout status daemonset/node-agent --timeout=300s >/dev/null
          fi
          if [ "$with_nack" = 1 ]; then
            for _ in $(seq 1 200); do k get crd streams.jetstream.nats.io >/dev/null 2>&1 && break; sleep 3; done
            for _ in $(seq 1 100); do k -n nack get deploy/nack >/dev/null 2>&1 && break; sleep 3; done
            k -n nack rollout status deploy/nack --timeout=300s >/dev/null
          fi
          if [ "$with_keda" = 1 ]; then
            for _ in $(seq 1 200); do k get crd scaledobjects.keda.sh >/dev/null 2>&1 && break; sleep 3; done
            wait_keda_deployments
          fi
        fi
      fi
      if [ "$with_system" != 0 ]; then
        echo "Waiting for resource metrics (v1beta1.metrics.k8s.io, up to 120 s)"
        wait_resource_metrics
      fi
      echo "Applying the root Application"
      k apply -n argocd -f bootstrap/
      cat <<MSG

Cluster ${ctx} is ready. Watch it converge:
  kubectl --context ${ctx} -n argocd get applications
  kubectl --context ${ctx} -n ${app}-dev get pods
${urls}Or port-forward: kubectl --context ${ctx} -n ${app}-dev port-forward svc/<service> 8080:80
Inspect it:     devbox run cluster-ui   (k9s)
Remove it:      devbox run cluster-down
MSG
    else
      if [ "$with_system" != 0 ]; then
        echo "Waiting for resource metrics (v1beta1.metrics.k8s.io, up to 120 s)"
        wait_resource_metrics
      fi
      echo "Cluster ${ctx} is ready (Gateway API only, no ArgoCD)."
      if [ -n "$registry_port" ]; then echo "Registry: push to localhost:${registry_port}/<image>; pods pull k3d-${registry}:${registry_port}/<image>"; fi
    fi
    ;;
  ui)
    if ! k get ns >/dev/null 2>&1; then
      echo "ERROR: cluster ${ctx} is not running" >&2
      echo "  fix: devbox run cluster-up" >&2
      exit 1
    fi
    shift
    exec k9s --context "$ctx" -n "${app}-dev" "$@"
    ;;
  *)
    echo "usage: $0 up|down|ui" >&2
    exit 2
    ;;
esac
