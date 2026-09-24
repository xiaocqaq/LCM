#!/usr/bin/env bash
# Offline contract tests: stdlib only, external programs replaced by local stubs.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
bash -n tests/acceptance.sh
exec "${PYTHON:-python3}" tests/reliability/test_acceptance_runner.py -v
