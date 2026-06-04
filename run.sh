#!/bin/sh
set -eu

cd "$(dirname "$0")"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/yunjia-daily-pycache}"
exec python3 generate_poster.py "$@"
