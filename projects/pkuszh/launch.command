#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
if [ "$#" -eq 0 ]; then set -- watch; fi
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -m projects.pkuszh "$@"
fi
exec python3 -m projects.pkuszh "$@"
