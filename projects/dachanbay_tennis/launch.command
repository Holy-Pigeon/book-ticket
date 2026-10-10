#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -m projects.dachanbay_tennis "$@"
fi
exec python3 -m projects.dachanbay_tennis "$@"
