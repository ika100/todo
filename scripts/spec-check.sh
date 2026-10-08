#!/usr/bin/env bash
# spec-check (ADR-026): validate docs/specs/ and trace the acceptance criteria of built specs to tests, with the
# platform's `cplat spec ci` at the version this repo was generated from (`ref:` in .platform-version).
# Warnings only; SPEC_CHECK_STRICT=1 turns them into errors. Needs git and uv (CI: astral-sh/setup-uv).
set -euo pipefail
[ -d docs/specs ] || { echo "spec-check: no docs/specs/ yet, nothing to check"; exit 0; }
ref=$( { sed -n 's/^ref: *//p' .platform-version 2>/dev/null || true; } | head -1)  # no stamp (adopted repo): main
ref=${ref:-main}
dir="${XDG_CACHE_HOME:-$HOME/.cache}/sdlc-foundry-spec-check/$ref"
fetch() {
  rm -rf "$dir" && mkdir -p "$dir"
  git -C "$dir" init -q && git -C "$dir" remote add origin https://github.com/ika100/sdlc-foundry.git
  git -C "$dir" fetch -q --depth 1 origin "$1" && git -C "$dir" checkout -q FETCH_HEAD
}
# a release tag never changes, so its checkout is reused; a branch (main) is fetched every time
if [ -n "${SPEC_CHECK_PLATFORM:-}" ]; then  # a local platform checkout (platform development, tests)
  dir=$SPEC_CHECK_PLATFORM
else
  case "$ref" in v[0-9]*) [ -f "$dir/scripts/cplat/cplat.py" ] || fetch "$ref" ;; *) fetch "$ref" ;; esac
fi
if [ ! -f "$dir/scripts/cplat/spec.py" ]; then
  echo "spec-check: platform $ref predates spec checks (ADR-026); using main"
  ref=main; dir="${XDG_CACHE_HOME:-$HOME/.cache}/sdlc-foundry-spec-check/main"; fetch main
  [ -f "$dir/scripts/cplat/spec.py" ] || { echo "spec-check: the platform has no spec checks yet; skipped"; exit 0; }
fi
if [ "${SPEC_CHECK_STRICT:-}" = "1" ]; then
  exec uv run --quiet "$dir/scripts/cplat/cplat.py" spec ci --strict
fi
exec uv run --quiet "$dir/scripts/cplat/cplat.py" spec ci
