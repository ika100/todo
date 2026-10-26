#!/usr/bin/env bash
# alert-poll (spec 092, ADR-038): turn firing alerts into issues with the platform's `cplat alerts poll`, at the version
# this repo was generated from (`ref:` in .platform-version). Runs from .github/workflows/alerts.yml on the self-hosted runner.
# ALERTS_DRY_RUN=1 prints what would be filed, commented, reopened or closed and changes nothing.
# ALERTS_PLATFORM=<dir> uses a local platform checkout (platform development, tests).
set -euo pipefail
ref=$( { sed -n 's/^ref: *//p' .platform-version 2>/dev/null || true; } | head -1)  # no stamp (adopted repo): main
ref=${ref:-main}
dir="${XDG_CACHE_HOME:-$HOME/.cache}/sdlc-foundry-spec-check/$ref"
fetch() {
  rm -rf "$dir" && mkdir -p "$dir"
  git -C "$dir" init -q && git -C "$dir" remote add origin https://github.com/ika100/sdlc-foundry.git
  git -C "$dir" fetch -q --depth 1 origin "$1" && git -C "$dir" checkout -q FETCH_HEAD
}
# a release tag never changes, so its checkout is reused; a branch (main) is fetched every time
if [ -n "${ALERTS_PLATFORM:-}" ]; then
  dir=$ALERTS_PLATFORM
else
  case "$ref" in v[0-9]*) [ -f "$dir/scripts/cplat/cplat.py" ] || fetch "$ref" ;; *) fetch "$ref" ;; esac
fi
if [ ! -f "$dir/scripts/cplat/alerts.py" ]; then
  msg="alert poll: platform $ref has no alert poll (spec 092); run /platform:update"
  echo "$msg"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then echo "$msg" >> "$GITHUB_STEP_SUMMARY"; fi
  exit 0
fi
exec uv run --quiet "$dir/scripts/cplat/cplat.py" alerts poll --repo-dir . ${ALERTS_DRY_RUN:+--dry-run}
