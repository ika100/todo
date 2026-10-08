#!/usr/bin/env bash
# Evaluate the rendered manifests against the platform's Kyverno guard rails, offline (shift-left, ADR-022).
# For every applications/<app>/policies/<env>/policy.yaml, each service overlay and addon of that environment is built with
# kustomize and checked with the Kyverno CLI; any violation fails (also for Audit environments: a PR should not introduce one).
# Without `policies:` in app.yaml there is nothing to check.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v kyverno >/dev/null 2>&1 || { echo "ERROR: the kyverno CLI is not installed (devbox provides it: run this through \`devbox run policy-check\`)" >&2; exit 1; }
build() { if command -v kustomize >/dev/null 2>&1; then kustomize build "$1"; else kubectl kustomize "$1"; fi; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
failed=0
checked=0
for policy in applications/*/policies/*/policy.yaml; do
  [ -f "$policy" ] || continue
  env=$(basename "$(dirname "$policy")")
  app=$(basename "$(dirname "$(dirname "$(dirname "$policy")")")")
  for dir in "applications/$app/overlays/$env"/*/ "applications/$app/addons/$env"/*/; do
    [ -f "${dir}kustomization.yaml" ] || continue
    build "$dir" > "$tmp/resources.yaml"
    out=$(kyverno apply "$policy" --resource "$tmp/resources.yaml" 2>&1) || true
    checked=$((checked + 1))
    if grep -Eq 'fail: [1-9]|error: [1-9]' <<< "$out"; then
      echo "POLICY VIOLATION in ${dir} (${env}):"
      sed 's/^/  /' <<< "$out"
      failed=1
    fi
  done
done
if [ "$failed" = 1 ]; then
  echo "ERROR: rendered manifests violate the guard rails (see ADR-022). Fix services.yaml/app.yaml and run \`devbox run render\`." >&2
  exit 1
fi
echo "OK: $checked rendered workload(s) satisfy the Kyverno guard rails"
