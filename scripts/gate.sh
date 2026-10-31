#!/usr/bin/env bash
# Decides when a quality gate checked nothing (spec 130). Always called through `bash scripts/gate.sh`.
#
#   gate.sh nothing <gate> <what> <fix>        print the line to stderr, exit 1
#   gate.sh files <gate> <pathspec>...         exit 0 when git tracks at least one matching file
#   gate.sh tests <gate> <runner> -- <command> [args...]
#                                              run the command, count the tests that ran
#                                              runner: pytest | maven | go | vitest
#
# The line: `<gate>: checked nothing: <what> - <fix>`. There is no setting, variable or argument that skips the decision.
# Same file in every template (scripts/sync-template-copies.py keeps the copies equal); keep it free of Jinja syntax.

usage() {
  echo "usage: gate.sh nothing <gate> <what> <fix> | files <gate> <pathspec>... | tests <gate> <pytest|maven|go|vitest> -- <command> [args...]" >&2
  exit 2
}

nothing() {
  echo "$1: checked nothing: $2 - $3" >&2
  exit 1
}

# Sum of tests - skipped over the <testsuite ...> start tags of the given JUnit files; missing files count 0.
junit_sum() {
  local f tag t s total=0
  for f in "$@"; do
    [ -f "$f" ] || continue
    while IFS= read -r tag; do
      [ -n "$tag" ] || continue
      t=$(printf '%s\n' "$tag" | grep -o '[[:space:]]tests="[0-9]*"' | sed 's/[^0-9]//g' | sed -n 1p)
      s=$(printf '%s\n' "$tag" | grep -o '[[:space:]]skipped="[0-9]*"' | sed 's/[^0-9]//g' | sed -n 1p)
      total=$((total + ${t:-0} - ${s:-0}))
    done <<EOF
$(sed -e ':a' -e '$!{N;ba' -e '}' -e 's/\n/ /g' "$f" | grep -o '<testsuite[[:space:]][^>]*>')
EOF
  done
  echo "$total"
}

# Sum of a numeric JSON key (pretty or compact) in a file; a missing file counts 0.
json_sum() {
  local f="$1" key="$2" n total=0
  [ -f "$f" ] || { echo 0; return; }
  for n in $(grep -o "\"$key\"[[:space:]]*:[[:space:]]*[0-9]*" "$f" | sed 's/.*://; s/[[:space:]]//g'); do
    total=$((total + n))
  done
  echo "$total"
}

cmd_tests() {
  [ "$#" -ge 2 ] || usage
  local gate="$1" runner="$2" rc ran no_tests=0 tmp
  shift 2
  case "$runner" in pytest | maven | go | vitest) ;; *) usage ;; esac
  [ "${1:-}" = "--" ] || usage
  shift
  [ "$#" -ge 1 ] || usage

  tmp=$(mktemp -d "${TMPDIR:-/tmp}/gate.XXXXXX") || exit 1
  trap 'rm -rf "$tmp"' EXIT

  case "$runner" in
    pytest)
      "$@" "--junitxml=$tmp/junit.xml"
      rc=$?
      ran=$(junit_sum "$tmp/junit.xml")
      [ "$rc" -eq 0 ] || [ "$rc" -eq 5 ] && no_tests=1
      ;;
    maven)
      rm -rf target/surefire-reports target/failsafe-reports
      "$@"
      rc=$?
      ran=$(junit_sum target/surefire-reports/TEST-*.xml target/failsafe-reports/TEST-*.xml)
      [ "$rc" -eq 0 ] && no_tests=1
      ;;
    go)
      "$@" | tee "$tmp/out"
      rc=${PIPESTATUS[0]}
      ran=$(grep -c -E '^[[:space:]]*--- (PASS|FAIL):' "$tmp/out")
      [ "$rc" -eq 0 ] && no_tests=1
      ;;
    vitest)
      export VITEST_GATE_REPORT="$tmp/vitest.json"
      "$@" 2>&1 | tee "$tmp/out"
      rc=${PIPESTATUS[0]}
      ran=$(($(json_sum "$tmp/vitest.json" numPassedTests) + $(json_sum "$tmp/vitest.json" numFailedTests)))
      if [ "$rc" -eq 0 ] || grep -q 'No test files found' "$tmp/out"; then no_tests=1; fi
      ;;
  esac

  if [ "${ran:-0}" -gt 0 ]; then
    exit "$rc"
  fi
  if [ "$no_tests" -eq 1 ]; then
    echo "$gate: checked nothing: 0 tests ran - add at least one test" >&2
    [ "$rc" -ne 0 ] && exit "$rc"
    exit 1
  fi
  exit "$rc"
}

cmd_files() {
  [ "$#" -ge 2 ] || usage
  local gate="$1" listed rc
  shift
  listed=$(git ls-files -- "$@")
  rc=$?
  [ "$rc" -eq 0 ] || exit "$rc"
  [ -n "$listed" ] && exit 0
  nothing "$gate" "0 files to scan" 'commit at least one file that the scan does not exclude (`git add`)'
}

[ "$#" -ge 1 ] || usage
sub="$1"
shift
case "$sub" in
  nothing)
    [ "$#" -eq 3 ] || usage
    nothing "$1" "$2" "$3"
    ;;
  files) cmd_files "$@" ;;
  tests) cmd_tests "$@" ;;
  *) usage ;;
esac
