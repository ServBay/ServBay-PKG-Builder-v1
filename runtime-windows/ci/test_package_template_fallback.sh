#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/build_windows_package_records.sh"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT

cat > "$tmp_file" <<'EOF'
# Package	Version	Filename
php	8.4.20	php-8.4.20-windows-x64.7z
# php	8.5.5	php-8.5.5-windows-x64.7z
php	8.6.0-dev-20260427	php-8.6.0-dev-20260427-windows-x64.zip
ollama	0.21.2
EOF

actual="$(resolve_windows_package_records "$tmp_file" "php-8.5.7")"
expected=$'php\t8.5.7\tphp-8.5.7-windows-x64.7z'

if [[ "$actual" != "$expected" ]]; then
    echo "expected: $expected" >&2
    echo "actual:   $actual" >&2
    exit 1
fi

exact="$(resolve_windows_package_records "$tmp_file" "ollama-0.21.2")"
if [[ "$exact" != $'ollama\t0.21.2\t' ]]; then
    echo "exact match failed: $exact" >&2
    exit 1
fi

if resolve_windows_package_records "$tmp_file" "missing-1.0.0" >/dev/null 2>&1; then
    echo "missing package unexpectedly resolved" >&2
    exit 1
fi
