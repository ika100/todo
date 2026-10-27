#!/usr/bin/env bash
# One-time cluster bootstrap (human only): KUBE_CONTEXT=<ctx> devbox run bootstrap
# Applies operators/root.yaml first (cluster-level operators such as CloudNativePG, SeaweedFS, cert-manager, the Barman Cloud plugin and Velero, rendered when a system needs them),
# waits for each operator's CRD, then applies bootstrap/ (the system roots). Argo CD does not retry a failed automated
# sync of the same commit, so a database Cluster synced before its CRD exists would stay failed.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${KUBE_CONTEXT:-}" ]; then
  echo 'Set KUBE_CONTEXT to the cluster to bootstrap, e.g. KUBE_CONTEXT=my-cluster devbox run bootstrap. Contexts:'
  kubectl config get-contexts -o name
  exit 1
fi
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }

if [ -f operators/root.yaml ]; then
  echo "Applying operators/root.yaml to context $KUBE_CONTEXT"
  k apply -n argocd -f operators/root.yaml
  for f in operators/apps/*.yaml; do
    [ -f "$f" ] || continue
    case "$(basename "$f" .yaml)" in
      cloudnative-pg) crd=clusters.postgresql.cnpg.io ;;
      seaweedfs) crd=buckets.seaweed.seaweedfs.com ;;
      plugin-barman-cloud) crd=objectstores.barmancloud.cnpg.io ;;
      cert-manager) crd=certificates.cert-manager.io ;;
      velero) crd=schedules.velero.io ;;
      *) continue ;;
    esac
    echo "Waiting for the CRD $crd (up to 10 minutes)"
    found=0
    for _ in $(seq 1 200); do
      if k get crd "$crd" >/dev/null 2>&1; then found=1; break; fi
      sleep 3
    done
    if [ "$found" != 1 ]; then
      echo "ERROR: the CRD $crd did not appear within 10 minutes." >&2
      echo "  fix: check the Argo CD application platform-operators: kubectl --context $KUBE_CONTEXT -n argocd get applications" >&2
      exit 1
    fi
  done
fi

echo "Applying bootstrap/ to context $KUBE_CONTEXT"
k apply -n argocd -f bootstrap/
