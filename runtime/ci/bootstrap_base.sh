#!/usr/bin/env bash
#
# ==============================================================================
# bootstrap_base.sh — Prepare the macOS build base for build_package.
#
# build_package compiles software against the ServBay "common" tree
# (/Applications/ServBay/package/common: include/ lib/ share/ openssl/<ver>/),
# which is shipped as two tarballs:
#   - ServBay-runtime-v<ver>.tar.gz  (runtime libs + openssl libs)
#   - ServBay-devlib-v<ver>.tar.gz   (headers + openssl include/share, etc.)
# Both archives are packed from /Applications with the leading "ServBay/" path
# preserved, so they extract straight into /Applications.
#
# This script downloads + verifies + extracts that base on a GitHub macOS
# runner (cache-miss path), and is a fast no-op when the base is already
# present (cache-hit path).
#
# PUBLIC REPO: this script contains NO real download URL / host / bucket.
# The download base is injected at runtime via the BASE_DOWNLOAD_URL env var
# (a public Actions variable); the script aborts if it is unset on a cache miss.
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# Constants — base layout (matches build_package's SERVBAY_* paths)
# ------------------------------------------------------------------------------
readonly SERVBAY_BASE_FULL_PATH="/Applications/ServBay"
# v1 marker directory: presence of "package" means an active v1 environment.
readonly SERVBAY_V1_MARKER_DIR="${SERVBAY_BASE_FULL_PATH}/package"
# v2 marker directory: build_package refuses to touch a v2 env that uses "packages".
readonly SERVBAY_V2_MARKER_DIR="${SERVBAY_BASE_FULL_PATH}/packages"
# The "common" tree build_package links against.
readonly SERVBAY_COMMON_PATH="${SERVBAY_BASE_FULL_PATH}/package/common"
# Archives extract into /Applications because they carry the "ServBay/" prefix.
readonly EXTRACT_DEST="/Applications"

# Base component name prefixes (filename convention from pack_runtime).
readonly RUNTIME_PREFIX="ServBay-runtime"
readonly DEVLIB_PREFIX="ServBay-devlib"

# ------------------------------------------------------------------------------
# Logging helpers (stderr; keeps stdout clean for machine-readable output)
# ------------------------------------------------------------------------------
log_info()  { printf '\033[0;32m[INFO]\033[0m  %s\n' "$*" >&2; }
log_warn()  { printf '\033[0;33m[WARN]\033[0m  %s\n' "$*" >&2; }
log_error() { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; }

###
# @description Print usage and exit non-zero.
###
usage() {
    cat >&2 <<'EOF'
Usage: bootstrap_base.sh <arch>

  <arch>   Required. Target architecture: x86_64 | arm64

Required environment (only consulted on a cache MISS):
  BASE_DOWNLOAD_URL   Base URL of the public bucket holding the ServBay base
                      tarballs (no trailing slash needed). Injected by the
                      workflow as a public Actions variable. NEVER hardcoded.
  BASE_VERSION        Version string of the base to fetch, e.g. "1.2.3".
                      Used to build "ServBay-runtime-v<ver>.tar.gz" etc.

Optional environment:
  BASE_REMOTE_SUBPATH Override the per-arch remote sub-path appended to
                      BASE_DOWNLOAD_URL. Defaults follow the published layout
                      (arm64 -> "packages/arm64/servbay",
                       x86_64 -> "packages/servbay").
  BASE_SHA256_RUNTIME Optional expected sha256 of the runtime tarball.
  BASE_SHA256_DEVLIB  Optional expected sha256 of the devlib tarball.

Outputs:
  - Prints "devlib_version=<ver>" to stdout.
  - If GITHUB_OUTPUT is set, also appends "devlib_version=<ver>" to it
    (for use as an actions/cache key component).

Examples:
  bootstrap_base.sh arm64
  bootstrap_base.sh x86_64
EOF
    exit 1
}

# ------------------------------------------------------------------------------
# Argument parsing & validation
# ------------------------------------------------------------------------------
if [[ "$#" -ne 1 ]]; then
    log_error "Exactly one argument (<arch>) is required."
    usage
fi

case "$1" in
    -h|--help) usage ;;
esac

readonly ARCH="$1"
if [[ "${ARCH}" != "x86_64" && "${ARCH}" != "arm64" ]]; then
    log_error "Invalid architecture: '${ARCH}'. Must be 'x86_64' or 'arm64'."
    usage
fi

# Version is needed both for the marker filename (cache key) and for downloads.
# It must be known up front so the idempotency marker is version-scoped.
BASE_VERSION="${BASE_VERSION:-}"
if [[ -z "${BASE_VERSION}" ]]; then
    log_error "BASE_VERSION is not set. Cannot determine which base to use."
    log_error "Set BASE_VERSION (e.g. '1.2.3') so the base can be version-pinned."
    exit 1
fi
readonly BASE_VERSION

# Version-scoped marker so a version bump correctly invalidates a stale cache.
readonly MARKER_FILE="${SERVBAY_BASE_FULL_PATH}/.bootstrap-${ARCH}-${BASE_VERSION}.done"

# ------------------------------------------------------------------------------
# Output helper — emit devlib version for cache keys, exactly once.
# ------------------------------------------------------------------------------
###
# @description Emit the resolved devlib/base version to stdout and, if running
#              under GitHub Actions, to $GITHUB_OUTPUT for cache-key wiring.
###
emit_devlib_version() {
    printf 'devlib_version=%s\n' "${BASE_VERSION}"
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
        printf 'devlib_version=%s\n' "${BASE_VERSION}" >> "${GITHUB_OUTPUT}"
    fi
}

# ------------------------------------------------------------------------------
# Step 1: Ensure /Applications/ServBay exists and is owned by the current user.
# ------------------------------------------------------------------------------
###
# @description Create the base directory and take ownership so subsequent
#              downloads/extracts/builds do not need sudo.
###
prepare_base_dir() {
    log_info "Ensuring ${SERVBAY_BASE_FULL_PATH} exists and is owned by $(whoami)..."
    sudo mkdir -p "${SERVBAY_BASE_FULL_PATH}"
    sudo chown -R "$(whoami)" "${SERVBAY_BASE_FULL_PATH}"
}

# ------------------------------------------------------------------------------
# Step 2: Idempotency — is the base already present for this arch+version?
# ------------------------------------------------------------------------------
###
# @description Cache-hit test. The base is considered ready when BOTH the
#              version-scoped marker exists AND the common/ tree is non-empty.
# @return 0 if the base is ready (skip download), 1 otherwise.
###
base_is_ready() {
    if [[ ! -f "${MARKER_FILE}" ]]; then
        return 1
    fi
    # Marker alone is not enough; the common tree must actually be there.
    if [[ -d "${SERVBAY_COMMON_PATH}" ]] && [[ -n "$(ls -A "${SERVBAY_COMMON_PATH}" 2>/dev/null)" ]]; then
        return 0
    fi
    log_warn "Marker present but ${SERVBAY_COMMON_PATH} is missing/empty; will re-bootstrap."
    return 1
}

# ------------------------------------------------------------------------------
# Step 3: Resolve the per-arch remote sub-path and tarball filenames.
# ------------------------------------------------------------------------------
###
# @description Echo the per-arch remote sub-path (no host, just the path tail).
#              The published layout puts arm64 under "packages/arm64/servbay"
#              and x86_64 under "packages/servbay". Overridable via
#              BASE_REMOTE_SUBPATH.
# @stdout The remote sub-path (no leading/trailing slash).
###
resolve_remote_subpath() {
    if [[ -n "${BASE_REMOTE_SUBPATH:-}" ]]; then
        # Trim any leading/trailing slashes for clean joining.
        local sp="${BASE_REMOTE_SUBPATH#/}"
        sp="${sp%/}"
        printf '%s' "${sp}"
        return 0
    fi
    if [[ "${ARCH}" == "arm64" ]]; then
        printf '%s' "packages/arm64/servbay"
    else
        printf '%s' "packages/servbay"
    fi
}

###
# @description Build a full download URL from BASE_DOWNLOAD_URL + sub-path + file.
# @param $1 string The filename to fetch.
# @stdout The joined URL.
###
build_url() {
    local filename="$1"
    local base="${BASE_DOWNLOAD_URL%/}"   # strip a single trailing slash if any
    local subpath
    subpath="$(resolve_remote_subpath)"
    printf '%s/%s/%s' "${base}" "${subpath}" "${filename}"
}

# ------------------------------------------------------------------------------
# Step 4: Download + (optional) verify + extract one tarball.
# ------------------------------------------------------------------------------
###
# @description Download a single archive into a directory.
# @param $1 string Source URL.
# @param $2 string Destination file path.
###
download_file() {
    local url="$1"
    local dest="$2"
    log_info "Downloading $(basename "${dest}")..."
    # --fail: non-2xx becomes an error; -L: follow redirects; -S: show errors.
    # The URL is intentionally not logged to avoid echoing the injected base.
    if ! curl -fSL --retry 3 --retry-delay 2 -o "${dest}" "${url}"; then
        log_error "Download failed for $(basename "${dest}")."
        rm -f "${dest}"
        return 1
    fi
    log_info "Downloaded $(basename "${dest}")."
}

###
# @description Verify a file's sha256 against an expected value, if provided.
# @param $1 string File path.
# @param $2 string Expected sha256 (may be empty -> verification skipped).
###
verify_sha256() {
    local file="$1"
    local expected="$2"
    if [[ -z "${expected}" ]]; then
        log_warn "No sha256 provided for $(basename "${file}"); skipping checksum verification."
        return 0
    fi
    local actual
    actual="$(shasum -a 256 "${file}" | awk '{print $1}')"
    if [[ "${actual}" != "${expected}" ]]; then
        log_error "Checksum mismatch for $(basename "${file}")."
        log_error "  expected: ${expected}"
        log_error "  actual:   ${actual}"
        return 1
    fi
    log_info "Checksum OK for $(basename "${file}")."
}

###
# @description Extract a ServBay base tarball into /Applications.
#              Archives carry the leading "ServBay/" path, so no
#              --strip-components is used.
# @param $1 string Archive path.
###
extract_archive() {
    local archive="$1"
    log_info "Extracting $(basename "${archive}") into ${EXTRACT_DEST}/..."
    # gtar is preferred (matches the packer), but fall back to system tar.
    local tar_bin
    if command -v gtar >/dev/null 2>&1; then
        tar_bin="gtar"
    else
        tar_bin="tar"
    fi
    "${tar_bin}" -xzf "${archive}" -C "${EXTRACT_DEST}"
}

# ------------------------------------------------------------------------------
# Step 5: Full cache-miss bootstrap.
# ------------------------------------------------------------------------------
###
# @description Download + verify + extract both base tarballs, then drop the
#              idempotency marker. Refuses to clobber a v2 environment.
###
do_bootstrap() {
    # Safety: never bootstrap a v1 base on top of a v2 environment.
    if [[ -d "${SERVBAY_V2_MARKER_DIR}" ]] && [[ ! -d "${SERVBAY_V1_MARKER_DIR}" ]]; then
        log_error "Detected a v2 environment ('packages') at ${SERVBAY_BASE_FULL_PATH}."
        log_error "Refusing to extract the v1 base over it. Manual intervention required."
        exit 1
    fi

    if [[ -z "${BASE_DOWNLOAD_URL:-}" ]]; then
        log_error "Cache miss but BASE_DOWNLOAD_URL is not set."
        log_error "Set BASE_DOWNLOAD_URL (public Actions variable) to the base bucket URL."
        exit 1
    fi

    local runtime_file="${RUNTIME_PREFIX}-v${BASE_VERSION}.tar.gz"
    local devlib_file="${DEVLIB_PREFIX}-v${BASE_VERSION}.tar.gz"

    local workdir
    workdir="$(mktemp -d)"

    local runtime_url devlib_url
    runtime_url="$(build_url "${runtime_file}")"
    devlib_url="$(build_url "${devlib_file}")"

    download_file "${runtime_url}" "${workdir}/${runtime_file}"
    download_file "${devlib_url}" "${workdir}/${devlib_file}"

    verify_sha256 "${workdir}/${runtime_file}" "${BASE_SHA256_RUNTIME:-}"
    verify_sha256 "${workdir}/${devlib_file}" "${BASE_SHA256_DEVLIB:-}"

    # Extract runtime first (libs), then devlib (headers) layered on top.
    extract_archive "${workdir}/${runtime_file}"
    extract_archive "${workdir}/${devlib_file}"

    # Sanity: the common tree must now exist.
    if [[ ! -d "${SERVBAY_COMMON_PATH}" ]]; then
        log_error "Extraction completed but ${SERVBAY_COMMON_PATH} is absent."
        log_error "Check the base tarball contents / naming convention."
        exit 1
    fi

    # Drop the version-scoped marker so subsequent runs hit the fast path.
    printf 'arch=%s\nversion=%s\nbootstrapped_at=%s\n' \
        "${ARCH}" "${BASE_VERSION}" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        > "${MARKER_FILE}"

    # Clean up the temp dir here (NOT via a RETURN trap: bash RETURN traps are
    # global and re-fire on main()'s return where workdir is unbound -> set -u
    # abort; that was the line-326 "workdir: unbound variable" failure).
    rm -rf "${workdir}"
    log_info "Base bootstrap complete for ${ARCH} (v${BASE_VERSION})."
}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
main() {
    prepare_base_dir

    if base_is_ready; then
        log_info "Cache hit: base already present for ${ARCH} (v${BASE_VERSION}); skipping download."
        emit_devlib_version
        return 0
    fi

    log_info "Cache miss: bootstrapping base for ${ARCH} (v${BASE_VERSION})..."
    do_bootstrap
    emit_devlib_version
}

main "$@"
