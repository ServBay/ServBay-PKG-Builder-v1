#!/usr/bin/env bash
# Consume public, immutable Runtime v2 and Devlib archives only.
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
exec python3 "${script_dir}/bootstrap_base.py" "$@"
