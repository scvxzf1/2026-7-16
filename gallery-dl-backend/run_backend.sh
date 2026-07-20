#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

if [[ -n "${PYTHON:-}" ]]; then
    python_bin="$PYTHON"
elif [[ -x "$script_dir/.venv/bin/python" ]]; then
    python_bin="$script_dir/.venv/bin/python"
else
    python_bin="python3"
fi

exec "$python_bin" -m gdl_backend "$@"
