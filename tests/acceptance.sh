#!/usr/bin/env bash
# Safe by default. See --help for unit / isolated integration / public layers.

run() {
  local name="$1"; shift
  local out status=0
  out=$("$@" 2>&1) || status=$?
  printf '%s\n' "$out"
  printf '%-22s exit=%s\n' "$name" "$status"
  return "$status"
}

main() (
  set -euo pipefail
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
  local mode="${1:---unit}" python="${PYTHON:-python3}"
  case "$mode" in
    --help|-h)
      printf '%s\n' \
        'bash tests/acceptance.sh [--unit|--integration|--public-readonly|--legacy-write]' \
        'Unit (default): reliability tests excluding integration; frontend node tests.' \
        'Integration: same suites including integration; disposable local PG + pgvector required.' \
        '  Set MEM_TEST_DATABASE_URL=postgresql+asyncpg://USER:PASS@127.0.0.1:PORT/memorys_test' \
        '  Never point this at an existing database; integration fixtures may create/drop tables.' \
        'Both local modes replace inherited application settings and use temporary data/env files.' \
        'Public read-only: explicitly set MEM_PUBLIC_BASE=https://YOUR_HOST; GET health/docs only.' \
        'Legacy write: MEM_ALLOW_LEGACY_WRITE=1 plus MEM_PUBLIC_BASE and MEM_TEST_API_KEY.' \
        '  Runs the historical public_mcp_test.py (writes documents!); use a disposable account.' \
        '  No keys are minted or read from disk. Other historical scripts remain manual-only:' \
        '  they may use production settings/credentials and are NOT safe pytest inputs.' \
        'Offline runner contracts: bash scripts/check_acceptance_runner.sh'
      return 0 ;;
    --public-readonly|--legacy-write)
      : "${MEM_PUBLIC_BASE:?explicit MEM_PUBLIC_BASE required}"
      [[ "$MEM_PUBLIC_BASE" == https://* ]] || { printf 'HTTPS URL required\n' >&2; return 2; }
      if [[ "$mode" == --public-readonly ]]; then
        for path in /api/health / /api/docs; do
          run "GET $path" curl --disable --fail --silent --show-error --max-time 20 \
            --proto '=https' --output /dev/null "${MEM_PUBLIC_BASE%/}$path" || return $?
        done
      else
        [[ "${MEM_ALLOW_LEGACY_WRITE:-}" == 1 ]] || { printf 'Legacy writes require MEM_ALLOW_LEGACY_WRITE=1\n' >&2; return 2; }
        : "${MEM_TEST_API_KEY:?explicit disposable account API key required}"
        export MEM_MCP_URL="${MEM_PUBLIC_BASE%/}/mcp/"
        run legacy-public-mcp "$python" tests/public_mcp_test.py "$MEM_TEST_API_KEY" || return $?
      fi
      return 0 ;;
    --unit|--integration) ;;
    *) printf 'Unknown mode: %s (see --help)\n' "$mode" >&2; return 2 ;;
  esac

  local database='postgresql+asyncpg://fixture:fixture@127.0.0.1:1/memorys_test'
  if [[ "$mode" == --integration ]]; then
    database="${MEM_TEST_DATABASE_URL:?disposable local MEM_TEST_DATABASE_URL required}"
    [[ "$database" =~ ^postgresql\+asyncpg://[^/@]+@(127\.0\.0\.1|localhost):[0-9]+/memorys_test(_[a-zA-Z0-9_]+)?$ ]] || {
      printf 'Integration requires loopback PG and database memorys_test\n' >&2; return 2;
    }
  fi
  local sandbox
  sandbox=$(mktemp -d "${TMPDIR:-/tmp}/memorys-acceptance.XXXXXXXX")
  trap 'rm -rf -- "$sandbox"' EXIT
  # Discard inherited MEM_* configuration, including production endpoints and keys.
  local variable
  for variable in ${!MEM_@}; do unset "$variable"; done
  : > "$sandbox/empty.env"
  export MEM_ENV_FILE="$sandbox/empty.env" MEM_DATA_DIR="$sandbox/data"
  export MEM_DATABASE_URL="$database" MEM_JWT_SECRET='isolated-acceptance-fixture-not-a-production-secret'
  export MEM_UPSTREAM_BASE='' MEM_GITHUB_REMOTE='' MEM_EMBED_API_BASE='' MEM_EMBED_API_KEY=''
  export PYTHONPATH="$PWD" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
  if [[ "$mode" == --integration ]]; then
    export MEM_TEST_DATABASE_URL="$database"
    run reliability "$python" -m pytest -p pytest_asyncio.plugin tests/reliability -q || return $?
  else
    run reliability "$python" -m pytest -p pytest_asyncio.plugin tests/reliability -q -m 'not integration' || return $?
  fi
  shopt -s nullglob
  local frontend_tests=(tests/frontend/*.test.*)
  ((${#frontend_tests[@]} > 0)) || { printf 'No frontend tests found\n' >&2; return 2; }
  run frontend node --test "${frontend_tests[@]}" || return $?
)

# Sourcing exposes run() without any setup, network access or test execution.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
