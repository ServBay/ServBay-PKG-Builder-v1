#!/bin/zsh

# ==============================================================================
# ServBay Package Build Script (Optimized for Zsh on macOS)
#
# This script automates the process of downloading, compiling, and packaging
# various software for the ServBay application. It can run in two modes:
# - Batch Mode: Reads 'packages.conf' and builds all listed packages.
# - Single Package Mode: Builds a specific package provided via command line.
# ==============================================================================

# --- Initial PATH Setup ---
# Set a baseline PATH to ensure essential tools are found.
# This will be refined later for specific build environments.
export PATH="/usr/local/bin:/usr/local/sbin:/usr/local/go/bin:$HOME/go/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Library/Apple/usr/bin"

# CMake 4.x 兼容性 - 部分旧项目的 cmake_minimum_required(VERSION < 3.5) 会被 CMake 4.0+ 拒绝
export CMAKE_POLICY_VERSION_MINIMUM="3.5"

# --- Tool Discovery ---
# Locate required tools at the beginning to fail early if they are missing.
GTAR_BIN=$(which gtar)
GO_BIN=$(which go)

# --- Global Environment & Constants ---
# set -e # Deactivated for more granular error control.

# --- System & Path Configuration ---
CUR_USER=$(whoami)
CPU_NUMBER=$(sysctl -n hw.ncpu)
SERVBAY_BASE_FULL_PATH="${SERVBAY_BASE_FULL_PATH:?Set the bootstrapped Runtime v2 base explicitly}"
SERVBAY_PACKAGE_DIR_NAME="packages"
SERVBAY_PACKAGE_FULL_PATH="${SERVBAY_BASE_FULL_PATH}/${SERVBAY_PACKAGE_DIR_NAME}"
SERVBAY_BIN_PATH="${SERVBAY_PACKAGE_FULL_PATH}/bin"
SERVBAY_SBIN_PATH="${SERVBAY_PACKAGE_FULL_PATH}/sbin"
SERVBAY_ETC_PATH="${SERVBAY_PACKAGE_FULL_PATH}/etc"
SERVBAY_VAR_PATH="${SERVBAY_PACKAGE_FULL_PATH}/var"
SERVBAY_TMP_PATH="${SERVBAY_BASE_FULL_PATH}/tmp"
SERVBAY_LOG_PATH="${SERVBAY_VAR_PATH}/log"
SERVBAY_DB_PATH="${SERVBAY_BASE_FULL_PATH}/db"
SERVBAY_COMMON_PATH="${SERVBAY_PACKAGE_FULL_PATH}/common"
SERVBAY_COMMON_INCLUDE_PATH="${SERVBAY_COMMON_PATH}/include"
SERVBAY_COMMON_LIB_PATH="${SERVBAY_COMMON_PATH}/lib"
SERVBAY_COMMON_SHARE_PATH="${SERVBAY_COMMON_PATH}/share"

# --- 项目目录结构 ---
# 自动检测脚本所在目录，计算项目根目录
SCRIPT_DIR="${${(%):-%x}:A:h}"                           # 脚本所在目录 (scripts/)
PROJECT_ROOT="${SCRIPT_DIR:h}"                  # 项目根目录 (runtime-v2/)
PACKAGES_CONF="${SERVBAY_PACKAGES_CONF:-${PROJECT_ROOT}/config/packages.conf}"
PATCH_DIR="${PROJECT_ROOT}/patch"

# 构建目录（基于项目根目录，不依赖 pwd，确保从 scripts/ 或上层运行时路径一致）
SERVBAY_BUILD_DIR="${SERVBAY_BUILD_ROOT:-${PROJECT_ROOT}}"
SERVBAY_BUILD_SRC_DIR="${SERVBAY_BUILD_DIR}/src"
SERVBAY_BUILD_LOG_DIR="${SERVBAY_BUILD_DIR}/logs"

# --- 静态库路径配置 ---
# 静态链接库 (static) 安装到这里，编译时链接，不打包给用户
SERVBAY_STATIC_LIBS_PATH="${SERVBAY_COMMON_PATH}/static"
SERVBAY_STATIC_INCLUDE_PATH="${SERVBAY_STATIC_LIBS_PATH}/include"
SERVBAY_STATIC_LIB_PATH="${SERVBAY_STATIC_LIBS_PATH}/lib"

# --- 动态库路径配置 ---
# 动态链接库 (dynamic) 安装到 common，需要打包给用户
SERVBAY_DYNAMIC_LIB_PATH="${SERVBAY_COMMON_LIB_PATH}"
LOG_FILE="${SERVBAY_BUILD_LOG_DIR}/build_$(date '+%Y-%m-%d').log"
ERROR_FILE=""  # Will be set in init_logging

# --- Build Target Configuration ---
BUILD_OS_MIN_VERSION="12.00"
BUILD_MACOS_TARGET=""

# --- Default Dependencies ---
OPENSSL_VERSION="3.5"

# --- Global State Variables for Packing (Zsh compatible) ---
typeset -A PACK_INFO
typeset -a PACK_FILES

# --- Global State Variables for PHP Build Plan ---
declare -a PHP_CONFIGURE_ARGS
declare -a PHP_PATCHES
typeset -A PHP_MODS
declare -a PHP_PRE_BUILD_CMDS
declare -a PHP_POST_BUILD_CMDS

# --- Global State Variables for PostgreSQL Build Plan ---
typeset -A PGSQL_MODS

# ==============================================================================
# LOGGING & UTILITY FUNCTIONS
# ==============================================================================

###
# @description Initializes logging by creating the log directory.
###
trash_path() {
    local item resolved trash_dir
    for item in "$@"; do
        [[ -e "$item" || -L "$item" ]] || continue
        resolved="${item:A}"
        if [[ "$resolved" == "$SERVBAY_COMMON_PATH" || "$resolved" == "$SERVBAY_COMMON_PATH/"* ]]; then
            log_message "ERROR" "Refusing to move the shared Runtime base: $resolved"
            return 1
        fi
        case "$resolved" in
            "$SERVBAY_BUILD_SRC_DIR/"*|"$SERVBAY_PACKAGE_FULL_PATH/"*|"$SERVBAY_BASE_FULL_PATH/www/phpmyadmin") ;;
            *) log_message "ERROR" "Cleanup outside the build/package tree: $resolved"; return 1 ;;
        esac
        mkdir -p "$HOME/.Trash"
        trash_dir=$(mktemp -d "$HOME/.Trash/ServBay-PKG-Builder-XXXXXXXX") || return 1
        mv "$item" "$trash_dir/" || return 1
    done
}

init_logging() {
    mkdir -p "$SERVBAY_BUILD_LOG_DIR"
    # Create or clear the error file
    ERROR_FILE="${SERVBAY_BUILD_LOG_DIR}/build_errors_$(date '+%Y-%m-%d').txt"
    : > "$ERROR_FILE"  # Use : (null command) to create/clear the file in zsh
}


###
# @description Logs a message to stderr with color and to a log file.
# @param $1 string Log level (e.g., "INFO", "WARN", "ERROR").
# @param $2 string The message to log.
###
log_message() {
    local level=$1
    local message=$2
    local color_reset="\033[0m"
    local color_info="\033[0;32m"  # Green
    local color_warn="\033[0;33m"  # Yellow
    local color_error="\033[0;31m" # Red
    local color=""
    local emoji="" # 新增 emoji 变量

    case "$level" in
        INFO)
            color="$color_info"
            emoji="🟢" # 或者用 ✅
            ;;
        WARN)
            color="$color_warn"
            emoji="⚠️" # 警告 Emoji
            ;;
        ERROR)
            color="$color_error"
            emoji="❌" # 错误 Emoji
            ;;
    esac

    # 在控制台输出中加入 Emoji
    # 注意 emoji 和 [LEVEL] 之间的空格
    echo -e "${color}${emoji} [${level}] $(date '+%H:%M:%S') - ${message}${color_reset}" >&2

    # 在日志文件中也加入 Emoji，以便于事后查看
    echo "${emoji} [${level}] $(date '+%Y-%m-%d %H:%M:%S') - ${message}" >> "$LOG_FILE"

    # Also write WARN and ERROR messages to the error log file
    if [[ "$level" == "WARN" || "$level" == "ERROR" ]] && [[ -n "$ERROR_FILE" ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] ${message}" >> "$ERROR_FILE"
    fi
}

###
# @description Displays the script's usage instructions and exits.
###
usage() {
    log_message "ERROR" "Invalid usage."
    echo "Usage: $0 [-p packageNames] [-a architecture] [--install-dir path] [--check-homebrew] [--check-homebrew-only] [--php-ext]"
    echo "  -p, --package          Optional: Package name(s) to build. Multiple packages can be"
    echo "                         separated by commas (e.g., php-8.4.7,mariadb-11.4.8,redis-7.2.4)."
    echo "                         If not specified, all packages in packages.conf will be built."
    echo "  -a, --architecture     Required: The target architecture (x86_64 or arm64)."
    echo "  --install-dir          Optional: A temporary installation prefix."
    echo "  --check-homebrew       Optional: Check for Homebrew dependencies after build completes."
    echo "  --check-homebrew-only  Optional: Only check for Homebrew dependencies (skip building)."
    echo "  --strict               Optional: Treat Homebrew dependency warnings as errors (exit 1)."
    echo "  --php-ext              Optional: Build PHP extension(s) only (skip PHP core build)."
    echo "                         Format: php_version:ext_name:ext_version (comma-separated for multiple)"
    echo "                         Example: 8.6:phpredis:6.2.0 or 8.6:phpredis:6.2.0,8.6:memcache:8.0"
    echo "  --php-core-only        Optional: Build PHP core only (skip extensions). Use with -p php-x.y.z"
    echo "  --pack-php-ext         Optional: Package PHP extension(s) from extensions/ to dist/php-ext/."
    echo "                         Format: php_version:ext_name:ext_version (same as --php-ext)"
    echo "                         Example: 8.6:phpredis:6.2.0 or 8.6:phalcon:5.9.3"
    echo ""
    echo "Examples:"
    echo "  $0 -a arm64                                # Build all packages from packages.conf"
    echo "  $0 -a arm64 -p php-8.4.7                  # Build single package"
    echo "  $0 -a arm64 -p php-8.4.7,mariadb-11.4.8   # Build multiple packages"
    echo "  $0 -a arm64 --check-homebrew              # Build all and check for Homebrew deps"
    echo "  $0 -a arm64 --check-homebrew-only         # Only check existing builds for Homebrew deps"
    echo "  $0 -a arm64 -p php-8.4.7 --check-homebrew-only --strict  # Check specific package strictly"
    echo "  $0 -a x86_64 --php-ext 8.6:phpredis:6.2.0  # Build only phpredis for PHP 8.6"
    echo "  $0 -a x86_64 --php-ext 8.6:phpredis:6.2.0,8.6:memcache:8.0  # Build multiple extensions"
    echo "  $0 -a x86_64 --pack-php-ext 8.6:phpredis:6.2.0  # Package phpredis extension to dist/php-ext/8.6/"
    exit 1
}

###
# @description Extracts the file extension from a source filename.
# @param $1 string Filename.
# @stdout The extracted extension.
# @return 0 on success, 1 on failure.
###
get_extension() {
    local filename="$1"
    local packageName="$2"
    # 优先按包名判断（用于文件名不含扩展名的特殊包）
    case "$packageName" in
        ngrok)  echo "zip"; return 0 ;;     # ngrok 文件名是下载 token，实际为 zip
        pinggy) echo "bin"; return 0 ;;     # pinggy 文件名是版本号，实际为原始二进制
    esac
    case "$filename" in
        *.tar.gz|*.tgz) echo "tar.gz" ;;
        *.tar.bz2) echo "tar.bz2" ;;
        *.tar.xz)  echo "tar.xz" ;;
        *.zip)     echo "zip" ;;
        *.phar)    echo "phar" ;;  # Support for .phar files (composer)
        *.php)     echo "php" ;;   # Support for .php files (adminer)
        meilisearch-*) echo "bin" ;;  # Support for meilisearch binaries (no extension)
        *)
            log_message "ERROR" "Unknown or unsupported extension for $filename"
            return 1 ;;
    esac
}

###
# @description Constructs the download URL for a given package.
# @param $1 string Package name.
# @param $2 string Package version.
# @param $3 string Original source filename.
# @stdout The full download URL.
###
get_download_url() {
    local packageName=$1
    local version=$2
    local sourceFilename=$3
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')

    case "$packageName" in
        "mariadb")
            echo "https://archive.mariadb.org/mariadb-${version}/source/${sourceFilename}" ;;
        "mysql")
            local version_major_mysql=$(echo "$version" | awk -F. '{print $1"."$2}')
            echo "https://cdn.mysql.com/Downloads/MySQL-${version_major_mysql}/${sourceFilename}" ;;
        "redis")
            echo "https://github.com/redis/redis/archive/refs/tags/${version}.tar.gz" ;;
        "memcached")
            echo "https://memcached.org/files/${sourceFilename}" ;;
        "caddy")
            # Caddy is built using xcaddy, no source download needed.
            echo "" ;;
        "nginx")
            # Nginx download URL uses the full source filename.
            echo "https://nginx.org/download/${sourceFilename}" ;;
        "httpd")
            echo "https://dlcdn.apache.org/httpd/${sourceFilename}" ;;
        "dnsmasq")
            echo "http://www.thekelleys.org.uk/dnsmasq/${sourceFilename}" ;;
        "subversion")
            echo "https://dlcdn.apache.org/subversion/${sourceFilename}" ;;
        "git")
            echo "https://github.com/git/git/archive/refs/tags/${sourceFilename}" ;;
        "cloudflared")
            # The filename from packages.conf already contains arch info
            echo "https://github.com/cloudflare/cloudflared/releases/download/${version}/${sourceFilename}" ;;
        "frp")
            # The filename from packages.conf already contains arch info
            echo "https://github.com/fatedier/frp/releases/download/v${version}/${sourceFilename}" ;;
        "ngrok")
            local arch="arm64"
            if [[ "$architecture" == "x86_64" ]]; then
                arch="amd64"
            fi
            echo "https://bin.ngrok.com/a/${sourceFilename}/ngrok-v3-${version}-darwin-${arch}.zip" ;;
        "pinggy")
            echo "https://github.com/Pinggy-io/cli-js/releases/download/v${version}/${sourceFilename}" ;;
        "mailpit")
            echo "https://github.com/axllent/mailpit/releases/download/v${version}/${sourceFilename}" ;;
        "ai-gateway")
            # Private upstream (ServBay/ServBay-AI-Gateway). An empty URL makes the
            # generic anonymous downloader skip it; build_ai_gateway() does an
            # authenticated `gh release download` (needs GH_TOKEN with repo read).
            echo "" ;;
        "disk-cleaner")
            # Private upstream (ServBay/ServBay-DiskCleaner). An empty URL makes the
            # generic anonymous downloader skip it; build_disk_cleaner() does an
            # authenticated `gh release download` (needs GH_TOKEN with repo read).
            echo "" ;;
        "mongosh")
            echo "https://downloads.mongodb.com/compass/${sourceFilename}" ;;
        "mongotools")
            echo "https://fastdl.mongodb.org/tools/db/${sourceFilename}" ;;
        "mongodb")
            echo "https://fastdl.mongodb.org/osx/${sourceFilename}" ;;
        "ollama")
            echo "https://github.com/ollama/ollama/archive/refs/tags/v${version}.tar.gz" ;;
        "minio")
            # The filename from packages.conf is the release tag
            echo "https://github.com/minio/minio/archive/refs/tags/${sourceFilename}" ;;
        "typesense")
            echo "https://dl.typesense.org/releases/${version}/${sourceFilename}" ;;
        "meilisearch")
            # The filename from packages.conf should be the binary name
            echo "https://github.com/meilisearch/meilisearch/releases/download/v${version}/${sourceFilename}" ;;
        "composer")
            echo "https://getcomposer.org/download/${version}/composer.phar" ;;
        "adminer")
            echo "https://github.com/vrana/adminer/releases/download/v${version}/adminer-${version}.php" ;;
        "phpmyadmin")
            echo "https://files.phpmyadmin.net/phpMyAdmin/${version}/${sourceFilename}" ;;
        "go")
            # The filename in packages.conf is assumed to be correct for the target arch
            echo "https://dl.google.com/go/${sourceFilename}" ;;
        "openjdk")
            # The filename from packages.conf is assumed to be the full name from the URL
            echo "https://cdn.azul.com/zulu/bin/${sourceFilename}" ;;
        "maven")
            # Example version: 3.9.6
            local version_major=$(echo "$version" | awk -F. '{print $1}') # e.g., 3
            echo "https://dlcdn.apache.org/maven/maven-${version_major}/${version}/binaries/${sourceFilename}" ;;
        "rust")
            # The filename from packages.conf already contains arch and version info
            echo "https://static.rust-lang.org/dist/${sourceFilename}" ;;
        "dotnetsdk")
            # Microsoft CDN keys the directory by the FULL SDK version (e.g.
            # 11.0.100-rc.1.26425.128) while packages.conf carries our repacked
            # short name (e.g. 11.0.100-rc1). Recover the full version from the
            # filename; for GA rows short name == full name so this is a no-op.
            local full_ver="${sourceFilename#dotnet-sdk-}"
            full_ver="${full_ver%-osx-*}"
            echo "https://builds.dotnet.microsoft.com/dotnet/Sdk/${full_ver}/${sourceFilename}" ;;
        "mono")
            echo "https://dl.winehq.org/mono/sources/mono/${sourceFilename}" ;;
        "ruby")
            local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')
            echo "https://cache.ruby-lang.org/pub/ruby/${version_major}/${sourceFilename}" ;;
        "python")
            local python_url_version="$version"
            # Python prerelease (3.15.0b4, 3.14.0a7, 3.13.0rc1) 文件放在 base version 目录
            # e.g. https://www.python.org/ftp/python/3.15.0/Python-3.15.0b4.tgz
            if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)(a|b|rc)[0-9]+$ ]]; then
                python_url_version="${match[1]}"
            fi
            echo "https://www.python.org/ftp/python/${python_url_version}/${sourceFilename}" ;;
        "node")
            echo "https://nodejs.org/dist/v${version}/${sourceFilename}" ;;
        "deno")
            # The filename from packages.conf already contains arch info.
            # e.g., deno-aarch64-apple-darwin.zip
            echo "https://github.com/denoland/deno/releases/download/v${version}/${sourceFilename}" ;;
        "bun")
            # The filename from packages.conf already contains arch info.
            # e.g., bun-darwin-aarch64.zip
            echo "https://github.com/oven-sh/bun/releases/download/bun-v${version}/${sourceFilename}" ;;
        "postgresql")
            # packages.conf uses "19.beta2" (dotted) so upload_packages.py's
            # strip_patch can derive major=19. PG FTP is dotless in BOTH the
            # directory ("v19beta2/") and the tarball ("postgresql-19beta2.tar.bz2"),
            # so strip the dot from the filename too. The filename can arrive dotted
            # (update_packages.py's generate_filename interpolates the conf version
            # verbatim) or already dotless (hand-written rows like 19.beta2); the
            # substitution is a no-op in the latter case. Stable versions such as
            # 18.6 never match the pattern and are untouched.
            local pg_url_version="$version"
            local pg_filename="$sourceFilename"
            if [[ "$version" =~ ^([0-9]+)\.(beta|rc|alpha)([0-9]+)$ ]]; then
                pg_url_version="${match[1]}${match[2]}${match[3]}"
                pg_filename="${sourceFilename//$version/$pg_url_version}"
            fi
            echo "https://ftp.postgresql.org/pub/source/v${pg_url_version}/${pg_filename}" ;;
        "php")
            if [[ "$version" =~ ^8\.6\.0 ]]; then
                # Special case for alpha/dev versions from GitHub
                echo "https://github.com/php/php-src/archive/${sourceFilename}"
            elif [[ "$version" =~ ^8\.5\.0 ]]; then
                # Special case for alpha/dev versions from GitHub
                echo "https://github.com/php/php-src/archive/refs/tags/${sourceFilename}"
            else
                echo "https://www.php.net/distributions/${sourceFilename}"
            fi
            ;;
        *)
            echo "" ;;
    esac
}

###
# @description Downloads a file from a URL if it doesn't already exist.
# Contains package-specific URL patterns and fallback logic.
# @param $1 string The URL to download from.
# @param $2 string The local destination path.
###
download_source() {
    # The function now accepts an array of URLs
    local -a urls=( ${(z)1} ) # Split string into array
    local destination=$2
    
    if [[ ${#urls[@]} -eq 0 ]] || [[ -z "${urls[1]}" ]]; then
        log_message "INFO" "No download URL for $(basename "$destination"). Assuming file exists locally."
        return 0
    fi
    if [ -f "$destination" ]; then
        log_message "INFO" "✅ Source file already exists: $(basename "$destination")"
        return 0
    fi

    mkdir -p "$(dirname "$destination")"
    
    local max_retries=3
    local retry_delay=5

    for url in "${urls[@]}"; do
        local attempt=1
        while (( attempt <= max_retries )); do
            log_message "INFO" "⬇️  Attempting to download from ${url} (attempt ${attempt}/${max_retries})"
            local http_code
            http_code=$(curl -L -C - --fail --connect-timeout 15 --max-time 300 -o "$destination" -w '%{http_code}' "$url")
            local curl_exit=$?
            if [[ $curl_exit -eq 0 ]]; then
                log_message "INFO" "✅ Download successful."
                return 0
            fi
            # 4xx 错误 (404/403 等) 是永久性的，删除残留文件，跳过重试直接尝试下一个 URL
            if [[ "$http_code" =~ ^4[0-9][0-9]$ ]]; then
                rm -f "$destination"
                log_message "WARN" "‼️ HTTP ${http_code} for ${url}, skipping to next source..."
                break
            fi
            # 其他错误 (超时/网络中断等) 保留已下载部分，重试时断点续传
            if (( attempt < max_retries )); then
                log_message "WARN" "‼️ Download failed, retrying in ${retry_delay}s..."
                sleep "$retry_delay"
            fi
            ((attempt++))
        done
        if [[ ! "$http_code" =~ ^4[0-9][0-9]$ ]] && (( attempt > max_retries )); then
            log_message "WARN" "‼️ All ${max_retries} attempts failed for ${url}. Trying next source..."
        fi
        # 切换到下一个 URL 前清理残留文件（不同源的部分文件不能断点续传混用）
        rm -f "$destination"
    done

    log_message "ERROR" "🛑 All download sources failed for $(basename "$destination")."
    rm -f "$destination" # Clean up partially downloaded file
    return 1
}

###
# @description Unpacks a source archive into a specified directory.
# Handles various archive formats and intelligently handles zip structures.
# @param $1 string Full package name (e.g., "redis-7.2.4").
# @param $2 string Filename of the source archive.
# @param $3 string (Optional) The target directory to extract to.
# @param $4 boolean (Optional) If "true", enables --strip-components=1 for tarballs. Default is "true".
###
unpack_package() {
    local packageFullName=$1
    local sourceFilename=$2
    local targetDir=$3
    # Default to true if the fourth argument is not provided
    local stripComponents=${4:-true}
    
    local archivePath="${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"
    local final_target_dir

    if [[ -n "$targetDir" ]]; then
        final_target_dir="$targetDir"
        log_message "INFO" "📦 Unpacking ${sourceFilename} directly to final destination: ${final_target_dir}/"
    else
        final_target_dir="${SERVBAY_BUILD_SRC_DIR}/${packageFullName}"
        log_message "INFO" "📦 Unpacking ${sourceFilename} to temporary build directory: ${final_target_dir}/"
    fi

    trash_path "$final_target_dir"
    mkdir -p "$final_target_dir"

    # Prepare tar options
    local tar_strip_opt=""
    if [[ "$stripComponents" == "true" ]]; then
        tar_strip_opt="--strip-components=1"
        log_message "INFO" "  -> Using --strip-components=1"
    else
        log_message "INFO" "  -> Not using --strip-components (flat archive)"
    fi

    case "$sourceFilename" in
        *.tar.gz|*.tgz)
            tar -C "$final_target_dir" -zxf "$archivePath" ${tar_strip_opt}
            ;;
        *.tar.bz2)
            tar -C "$final_target_dir" -jxf "$archivePath" ${tar_strip_opt}
            ;;
        *.tar.xz)
            tar -C "$final_target_dir" -Jxf "$archivePath" ${tar_strip_opt}
            ;;
        *.zip)
            # Zip handling is already intelligent and doesn't need the strip flag.
            local temp_zip_dir="${SERVBAY_BUILD_SRC_DIR}/temp_zip_extract_$$"
            mkdir -p "$temp_zip_dir"
            unzip -q "$archivePath" -d "$temp_zip_dir"
            local items=( "$temp_zip_dir"/*(N) )
            local item_count=${#items[@]}
            if [[ $item_count -eq 1 ]]; then
                local single_item=$items[1]
                if [[ -d "$single_item" ]]; then
                    log_message "INFO" "Moving contents from nested zip folder..."
                    mv "$single_item"/* "$final_target_dir/"
                else
                    log_message "INFO" "Moving single file from zip..."
                    mv "$single_item" "$final_target_dir/"
                fi
            elif [[ $item_count -gt 1 ]]; then
                log_message "INFO" "Moving multiple items from zip root..."
                mv "$temp_zip_dir"/* "$final_target_dir/"
            else
                log_message "WARN" "The zip file appears to be empty."
            fi
            trash_path "$temp_zip_dir"
            ;;
        *)
            log_message "ERROR" "Unsupported archive format: $sourceFilename"
            return 1 ;;
    esac

    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to unpack ${sourceFilename}."
        return 1
    fi
    
    if [[ -z "$targetDir" ]]; then
        cd "$final_target_dir"
    fi
}

###
# @description Cleans up the unpacked source directory after a successful build.
# @param $1 string Full package name.
###
cleanup_source_directory() {
    local packageFullName=$1

    # Parameter validation
    if [[ -z "$packageFullName" ]]; then
        log_message "ERROR" "cleanup_source_directory: packageFullName is empty, skipping cleanup"
        return 1
    fi

    if [[ "$packageFullName" == "/" ]] || [[ "$packageFullName" == "." ]] || [[ "$packageFullName" == ".." ]]; then
        log_message "ERROR" "cleanup_source_directory: Invalid packageFullName: ${packageFullName}"
        return 1
    fi

    local sourceDir="${SERVBAY_BUILD_SRC_DIR}/${packageFullName}"

    # Check if the directory exists and is not the parent directory
    # Note: Some packages are extracted directly to the target directory (e.g., cloudflared,
    # composer, dotnetsdk, go, node) and don't create a source directory. This is expected
    # behavior, so we silently return success without a warning.
    if [[ ! -d "$sourceDir" ]]; then
        return 0
    fi

    # Safety check: ensure we're not deleting SERVBAY_BUILD_SRC_DIR itself
    if [[ "$sourceDir" == "$SERVBAY_BUILD_SRC_DIR" ]] || [[ "$sourceDir" == "${SERVBAY_BUILD_SRC_DIR}/" ]]; then
        log_message "ERROR" "cleanup_source_directory: Attempting to delete SERVBAY_BUILD_SRC_DIR itself! Aborting."
        return 1
    fi

    # Additional safety: ensure we're in SERVBAY_BUILD_DIR before deleting
    if [[ "$(pwd)" == "$sourceDir"* ]]; then
        log_message "ERROR" "cleanup_source_directory: Currently in the directory to be deleted! Please cd out first."
        return 1
    fi

    log_message "INFO" "🧽 Cleaning up source directory: ${sourceDir}"
    trash_path "${sourceDir}"
}

# ==============================================================================
# POST-BUILD PROCESSING & PACKAGING FUNCTIONS
# ==============================================================================

###
# @description Check if binaries have accidentally linked to Homebrew libraries.
# This prevents shipping binaries that won't work on machines without Homebrew.
# @param $1 string File or directory to scan for binaries.
# @param $2 string (optional) "strict" to treat warnings as errors.
# @return 0 if no Homebrew dependencies found, 1 if found (in strict mode).
###
check_homebrew_dependencies() {
    local check_path="$1"
    local strict_mode="${2:-}"
    local found_issues=0
    local files_checked=0

    # Homebrew library paths to check for
    local homebrew_patterns=(
        "/usr/local/lib/"
        "/usr/local/opt/"
        "/usr/local/Cellar/"
        "/opt/homebrew/lib/"
        "/opt/homebrew/opt/"
        "/opt/homebrew/Cellar/"
    )

    # Build list of files to check (support both file and directory inputs)
    local files_to_check=()
    local base_path=""

    if [[ -f "$check_path" ]]; then
        # Single file mode
        files_to_check+=("$check_path")
        base_path="$(dirname "$check_path")"
        log_message "INFO" "🔍 Checking Homebrew dependencies for file: $check_path"
    elif [[ -d "$check_path" ]]; then
        # Directory mode
        base_path="$check_path"
        log_message "INFO" "🔍 Scanning for Homebrew dependencies in: $check_path"
        while IFS= read -r -d '' file; do
            files_to_check+=("$file")
        done < <(find "$check_path" \( -type f \( -perm +111 -o -name "*.dylib" -o -name "*.so" -o -name "*.bundle" \) \) -print0 2>/dev/null)
    else
        log_message "ERROR" "check_homebrew_dependencies: Path not found: $check_path"
        return 1
    fi

    local deps=""
    local matches=""
    local lib_path=""

    for file in "${files_to_check[@]}"; do
        # Skip non-Mach-O files
        if ! file "$file" 2>/dev/null | grep -q "Mach-O"; then
            continue
        fi

        ((files_checked++))

        # Get dependency list using otool
        deps=$(otool -L "$file" 2>/dev/null | tail -n +2)

        for pattern in "${homebrew_patterns[@]}"; do
            matches=$(echo "$deps" | grep "$pattern" || true)
            if [[ -n "$matches" ]]; then
                if [[ $found_issues -eq 0 ]]; then
                    log_message "WARN" "⚠️  Found Homebrew dependencies!"
                    log_message "WARN" "    These binaries may not work on systems without Homebrew."
                fi
                log_message "WARN" "  File: ${file#$base_path/}"
                echo "$matches" | while read -r line; do
                    lib_path=$(echo "$line" | awk '{print $1}')
                    log_message "WARN" "    → $lib_path"
                done
                found_issues=1
            fi
        done
    done

    log_message "INFO" "   Scanned $files_checked Mach-O files"

    if [[ $found_issues -eq 0 ]]; then
        log_message "INFO" "✅ No Homebrew dependencies found"
        return 0
    else
        log_message "WARN" "⚠️  Found Homebrew dependencies (see above)"
        if [[ "$strict_mode" == "strict" ]]; then
            log_message "ERROR" "Strict mode: Treating Homebrew dependencies as error"
            return 1
        fi
        return 0  # Non-strict mode: warn but don't fail
    fi
}

###
# @description Check Homebrew dependencies for a specific package.
# @param $1 string Package name.
# @param $2 string Package version.
# @param $3 string (optional) "strict" to treat warnings as errors.
# @return 0 if no issues, 1 if issues found in strict mode.
###
check_package_homebrew_deps() {
    local packageName="$1"
    local packageVersion="$2"
    local strict_mode="${3:-}"
    local check_path=""

    # Determine package path based on type
    case "$packageName" in
        # Multi-version packages
        php|python|ruby|mysql|mariadb|redis|mongodb|go|composer)
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            check_path="${SERVBAY_PACKAGE_FULL_PATH}/${packageName}/${version_major}"
            ;;
        node|postgresql)
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1}')
            check_path="${SERVBAY_PACKAGE_FULL_PATH}/${packageName}/${version_major}"
            ;;
        java|openjdk)
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1}')
            check_path="${SERVBAY_PACKAGE_FULL_PATH}/openjdk/${version_major}"
            ;;
        dotnetsdk)
            local version_major=$(_get_dotnetsdk_major_version "$packageVersion")
            check_path="${SERVBAY_PACKAGE_FULL_PATH}/dotnetsdk/${version_major}"
            ;;
        mono)
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            check_path="${SERVBAY_PACKAGE_FULL_PATH}/mono/${version_major}"
            ;;
        adminer)
            # adminer is a PHP file, no binary dependencies to check
            log_message "INFO" "Skipping Homebrew check for adminer (PHP file, no binary dependencies)"
            return 0
            ;;
        phpmyadmin)
            log_message "INFO" "Skipping Homebrew check for phpmyadmin (PHP application, no binary dependencies)"
            return 0
            ;;
        # Single-version packages
        httpd)
            check_path="${SERVBAY_PACKAGE_FULL_PATH}/apache"
            ;;
        dnsmasq|nginx|caddy|memcached|git|mailpit|mongosh|subversion|maven|rust|deno|bun|cloudflared|ollama|meilisearch|minio|typesense|frp|ngrok|pinggy|mongotools)
            check_path="${SERVBAY_PACKAGE_FULL_PATH}/${packageName}"
            ;;
        *)
            log_message "WARN" "check_package_homebrew_deps: Unknown package type '$packageName', skipping"
            return 0
            ;;
    esac

    if [[ -d "$check_path" ]]; then
        check_homebrew_dependencies "$check_path" "$strict_mode"
        return $?
    else
        log_message "WARN" "Package directory not found: $check_path"
        return 0
    fi
}

###
# @description Check all packages in the ServBay package directory for Homebrew dependencies.
# @param $1 string (optional) "strict" to treat warnings as errors.
# @return 0 if no issues, 1 if issues found in strict mode.
###
check_all_homebrew_deps() {
    local strict_mode="${1:-}"
    local has_issues=0

    log_message "INFO" "═══════════════════════════════════════════════════════════════"
    log_message "INFO" "Checking all packages for Homebrew dependencies..."
    log_message "INFO" "═══════════════════════════════════════════════════════════════"

    # Check main packages directory
    if [[ -d "${SERVBAY_PACKAGE_FULL_PATH}" ]]; then
        check_homebrew_dependencies "${SERVBAY_PACKAGE_FULL_PATH}" "$strict_mode"
        [[ $? -ne 0 ]] && has_issues=1
    fi

    # Check common libraries
    if [[ -d "${SERVBAY_COMMON_PATH}" ]]; then
        log_message "INFO" ""
        log_message "INFO" "Checking common libraries..."
        check_homebrew_dependencies "${SERVBAY_COMMON_PATH}" "$strict_mode"
        [[ $? -ne 0 ]] && has_issues=1
    fi

    log_message "INFO" "═══════════════════════════════════════════════════════════════"

    return $has_issues
}

###
# @description "Knowledge Base" for package verification.
# @param $1 string Package name.
# @param $2 string Package version.
# @stdout The verification command string.
###
get_package_verification_info() {
    local packageName="$1"
    local packageVersion="$2"
    local verify_command=""

    case "$packageName" in
        "ai-gateway") verify_command="${SERVBAY_PACKAGE_FULL_PATH}/ai-gateway/bin/servbay-ai-gateway --version" ;;
        "disk-cleaner") verify_command="${SERVBAY_PACKAGE_FULL_PATH}/disk-cleaner/bin/servbay-diskcleaner --version" ;;
        "redis")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/redis/${version_major}/bin/redis-server -v"
            ;;
        "memcached")
            # Memcached uses -V (uppercase) for version (single-version package)
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/memcached/bin/memcached -V"
            ;;
        "caddy")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/caddy/bin/caddy version"
            ;;
        "nginx")
            # Single-version package
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/nginx/sbin/nginx -v"
            ;;
        "httpd")
            # Single-version package
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/apache/sbin/httpd -v"
            ;;
        "php")
            local package_version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/php/${package_version_major}/bin/php -v"
            ;;
        "dnsmasq")
            # Dnsmasq uses -v (lowercase) for version
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/dnsmasq/sbin/dnsmasq -v"
            ;;
        "subversion")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/subversion/bin/svn --version"
            ;;
        "git")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/git/bin/git --version"
            ;;
        "cloudflared")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/cloudflared/bin/cloudflared -v"
            ;;
        "frp")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/frp/bin/frpc -v"
            ;;
        "ngrok")
            # ngrok version output is like "ngrok version 3.x.x"
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/ngrok/bin/ngrok -v"
            ;;
        "pinggy")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/pinggy/bin/pinggy --version"
            ;;
        "mailpit")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/mailpit/bin/mailpit version | grep -i 'compiled with'"
            ;;
        "mongosh")
            # Single-version package
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/mongosh/bin/mongosh --version"
            ;;
        "mongotools")
            # Use mongostat for version check as in the original script
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/mongotools/bin/mongostat --version"
            ;;
        "mongodb")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/mongodb/${version_major}/bin/mongod --version"
            ;;
        "ollama")
            local ollama_major=${packageVersion%%.*}
            local ollama_version_remainder=${packageVersion#*.}
            local ollama_minor=${ollama_version_remainder%%.*}
            if [[ "$ollama_major" == <-> && "$ollama_minor" == <-> ]] &&
               (( ollama_major > 0 || (ollama_major == 0 && ollama_minor >= 30) )); then
                verify_command="test -x ${SERVBAY_PACKAGE_FULL_PATH}/ollama/lib/ollama/llama-server && test -x ${SERVBAY_PACKAGE_FULL_PATH}/ollama/lib/ollama/llama-quantize && ${SERVBAY_PACKAGE_FULL_PATH}/ollama/lib/ollama/llama-server --version >/dev/null && ${SERVBAY_PACKAGE_FULL_PATH}/ollama/bin/ollama -v"
            else
                verify_command="${SERVBAY_PACKAGE_FULL_PATH}/ollama/bin/ollama -v"
            fi
            ;;
        "minio")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/minio/bin/minio -v"
            ;;
        "typesense")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/typesense/bin/typesense-server --version 2>&1 | grep ${packageVersion}"
            ;;
        "meilisearch")
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/meilisearch/bin/meilisearch --version"
            ;;
        "composer")
            local version_major="${packageVersion%%.*}"
            [[ "$packageVersion" == 2.2.* ]] && version_major="2.2"
            # The fixed Runtime base has no PHP interpreter; inspect the actual PHAR.
            verify_command="grep -a ${packageVersion} ${SERVBAY_PACKAGE_FULL_PATH}/composer/${version_major}/bin/composer"
            ;;
        "adminer")
            # Adminer is a single PHP file, verify by checking the @version annotation.
            verify_command="grep @version ${SERVBAY_BASE_FULL_PATH}/www/adminer/index.php"
            ;;
        "phpmyadmin")
            verify_command="grep ${packageVersion} ${SERVBAY_BASE_FULL_PATH}/www/phpmyadmin/libraries/classes/Version.php"
            ;;
        "go")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/go/${version_major}/bin/go version"
            ;;
        "openjdk"|"java")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1}')
            local jdk_bin="${SERVBAY_PACKAGE_FULL_PATH}/openjdk/${version_major}/bin/java"
            # Guard with test -x: a missing bin/java (e.g. archive layout
            # regression) must produce empty output, not an error line whose
            # path contains the version and could falsely satisfy the outer
            # match. The -version output goes to stderr, hence 2>&1.
            # JDK 27+ prints only the feature version (openjdk version "27")
            # while 9+ lines print the full version ("17.0.20.1"), so match
            # the quoted major prefix and echo the conf version for the outer
            # substring match. Legacy JDK 7/8 (Zulu) print the version with a
            # "1." prefix (openjdk version "1.8.0_504") which can never match
            # that prefix — emit the raw output instead and let
            # core_verify_package's legacy conversion (8.0.504 -> 1.8.0_504)
            # do the matching.
            if [[ "${version_major}" == "7" || "${version_major}" == "8" ]]; then
                verify_command="test -x ${jdk_bin} && ${jdk_bin} -version 2>&1"
            else
                verify_command="test -x ${jdk_bin} && ${jdk_bin} -version 2>&1 | grep -q 'openjdk version \"${version_major}' && echo ${packageVersion}"
            fi
            ;;
        "maven")
            # Single-version package - verify by checking for version-specific jar files
            verify_command="ls ${SERVBAY_PACKAGE_FULL_PATH}/maven/lib/maven-core-${packageVersion}.jar"
            ;;
        "rust")
            # Single-version package
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/rust/bin/rustc --version"
            ;;
        "dotnetsdk")
            # Use the helper function to get the correct major version for the path
            local version_major=$(_get_dotnetsdk_major_version "$packageVersion")
            local dotnet_bin="${SERVBAY_PACKAGE_FULL_PATH}/dotnetsdk/${version_major}/dotnet"
            # Repacked short name (e.g. 11.0.100-rc1) differs from the runtime's
            # own report (e.g. 11.0.100-rc.1.26425.128). Echo the conf version
            # only when the installed SDK reports the expected major.minor, so
            # core_verify's substring match works for GA and prerelease rows.
            # Both branches are guarded by test -x so a missing binary cannot
            # fake a pass via the path in the error text, and both drop stderr:
            # a broken binary's error embeds the versioned install path, which
            # would otherwise leak into core_verify's 2>&1 capture and match.
            # Legacy rows (e.g. 2.1.202) live under a dotnetsdk/2.0 path while
            # the SDK reports "2.1.202", which never contains "2.0" — emit the
            # raw --version output and let core_verify match the conf version
            # itself.
            if [[ "${packageVersion}" == "${version_major}".* ]]; then
                verify_command="test -x ${dotnet_bin} && ${dotnet_bin} --version 2>/dev/null | grep -q \"${version_major}\" && echo \"${packageVersion}\""
            else
                verify_command="test -x ${dotnet_bin} && ${dotnet_bin} --version 2>/dev/null"
            fi
            ;;
        "mono")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            local mono_path="${SERVBAY_PACKAGE_FULL_PATH}/mono/${version_major}"
            # Output both mono and xsp4 version info; outer grep matches mono version
            verify_command="${mono_path}/bin/mono -V && ${mono_path}/bin/xsp4 --version"
            ;;
        "ruby")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/ruby/${version_major}/bin/ruby -v"
            ;;
        "python")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            # Use pythonX.Y -V to get the version
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/python/${version_major}/bin/python${version_major} -V"
            ;;
        "node")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1}')
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/node/${version_major}/bin/node -v"
            ;;
        "deno")
            # Single-version package
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/deno/bin/deno --version"
            ;;
        "bun")
            # Single-version package
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/bun/bin/bun --version"
            ;;
        "mysql")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            # Use mysql client -V for version
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/mysql/${version_major}/bin/mysql -V"
            ;;
        "mariadb")
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/mariadb/${version_major}/bin/mariadb -V"
            ;;
        "postgresql")
            local version_major=${packageVersion%%.*}
            version_major=${version_major%%rc*}
            verify_command="${SERVBAY_PACKAGE_FULL_PATH}/postgresql/${version_major}/bin/psql -V"
            ;;
        *)
            log_message "ERROR" "No verification rule for package '$packageName'."
            return 1
            ;;
    esac
    
    echo "$verify_command"
    return 0
}

###
# @description "Knowledge Base" for packaging.
# @param $1 string Package name.
# @param $2 string Package version.
# @modifies global PACK_INFO, PACK_FILES.
###
get_package_packing_info() {
    local packageName="$1"
    local packageVersion="$2"
    
    PACK_INFO=()
    PACK_FILES=()

    case "$packageName" in
        "ai-gateway"|"disk-cleaner")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "$packageName" )
            ;;
        "redis")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "redis/${version_major}" )
            ;;
        "memcached")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "memcached" )
            ;;
        "caddy")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "caddy" )
            ;;
        "nginx")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "nginx" )
            ;;
        "httpd")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "apache" )
            ;;
        "php")
            # Multi-version package
            # 注：PHP core 在扩展编译前打包，此时只包含内置扩展 (opcache 等)
            local package_version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "php/${package_version_major}" )
            ;;
        "dnsmasq")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "dnsmasq" )
            ;;
        "subversion")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "subversion" )
            ;;
        "git")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "git" )
            ;;
        "cloudflared")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "cloudflared" )
            ;;
        "frp")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "frp" )
            ;;
        "ngrok")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "ngrok" )
            ;;
        "pinggy")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "pinggy" )
            ;;
        "mailpit")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "mailpit" )
            ;;
        "mongosh")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "mongosh" )
            ;;
        "mongotools")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "mongotools" )
            ;;
        "mongodb")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "mongodb/${version_major}" )
            ;;
        "ollama")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "ollama" )
            ;;
        "minio")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "minio" )
            ;;
        "typesense")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "typesense" )
            ;;
        "meilisearch")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "meilisearch" )
            ;;
        "composer")
            # Multi-version package (2, 2.2)
            local version_major
            if [[ "$packageVersion" == 2.2.* ]]; then
                version_major="2.2"
            else
                version_major=$(echo "$packageVersion" | awk -F. '{print $1}')
            fi
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "composer/${version_major}" )
            ;;
        "adminer")
            PACK_INFO[source_dir]="${SERVBAY_BASE_FULL_PATH}/www"
            PACK_FILES=( "adminer" )
            ;;
        "phpmyadmin")
            PACK_INFO[source_dir]="${SERVBAY_BASE_FULL_PATH}/www"
            PACK_FILES=( "phpmyadmin" )
            ;;
        "go")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "go/${version_major}" )
            ;;
        "openjdk"|"java")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "openjdk/${version_major}" )
            ;;
        "maven")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "maven" )
            ;;
        "rust")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "rust" )
            ;;
        "dotnetsdk")
            # Multi-version package
            local version_major=$(_get_dotnetsdk_major_version "$packageVersion")
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "dotnetsdk/${version_major}" )
            ;;
        "mono")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "mono/${version_major}" )
            ;;
        "ruby")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "ruby/${version_major}" )
            ;;
        "python")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "python/${version_major}" )
            ;;
        "node")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "node/${version_major}" )
            ;;
        "deno")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "deno" )
            ;;
        "bun")
            # Single-version package
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "bun" )
            ;;
        "mysql")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "mysql/${version_major}" )
            ;;
        "mariadb")
            # Multi-version package
            local version_major=$(echo "$packageVersion" | awk -F. '{print $1"."$2}')
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "mariadb/${version_major}" )
            ;;
        "postgresql")
            # Multi-version package
            local version_major=${packageVersion%%.*}
            version_major=${version_major%%rc*}
            PACK_INFO[source_dir]="${SERVBAY_PACKAGE_FULL_PATH}"
            PACK_FILES=( "postgresql/${version_major}" )
            ;;
        *)
            log_message "ERROR" "No packing rule for package '$packageName'."
            return 1
            ;;
    esac
    
    return 0
}

###
# @description Core "doer" function for verification.
# Executes a command and checks for a version string.
# @param $1 string Package name.
# @param $2 string Package version to grep for.
# @param $3 string The full verification command to execute.
# @return 0 on success, 1 on failure.
###
core_verify_package() {
    # Enable pipefail to ensure that if the command fails, the whole pipeline fails.
    setopt pipefail

    local packageName="$1"
    local packageVersion="$2"
    local verifyCommand="$3"

    # For PHP dev versions with date suffix (e.g., 8.6.0-dev-20251105),
    # PHP only outputs the base version (e.g., 8.6.0-dev) without the date.
    # So we need to strip the date suffix for verification.
    local versionToMatch="$packageVersion"
    if [[ "$packageName" == "php" && "$packageVersion" =~ ^([0-9]+\.[0-9]+\.[0-9]+-dev)-[0-9]{8}$ ]]; then
        versionToMatch="${match[1]}"
        log_message "INFO" "⏳ Verifying ${packageName} installation (matching base version: ${versionToMatch})..."
    # OpenJDK 7/8 use legacy versioning: "1.7.0_352" / "1.8.0_482" instead of "7.0.352" / "8.0.482".
    # Convert our version (e.g., 7.0.352) to legacy format (1.7.0_352) for matching.
    elif [[ "$packageName" == "openjdk" && "$packageVersion" =~ ^([78])\.0\.([0-9]+)$ ]]; then
        versionToMatch="1.${match[1]}.0_${match[2]}"
        log_message "INFO" "⏳ Verifying ${packageName} installation (matching legacy version: ${versionToMatch})..."
    else
        log_message "INFO" "⏳ Verifying ${packageName} installation..."
    fi

    # Preserve the actual CLI status separately from the local declaration.
    local output
    local verifyStatus
    output=$(eval "$verifyCommand" 2>&1)
    verifyStatus=$?
    if [[ "$packageName" == "php" && $verifyStatus -ne 0 ]]; then
        log_message "ERROR" "❌ PHP verification command exited with status $verifyStatus."
        log_message "ERROR" "   Command: $verifyCommand"
        log_message "ERROR" "   Actual output: $output"
        unsetopt pipefail
        return 1
    fi
    if ! echo "$output" | grep -qFi "$versionToMatch"; then
        log_message "ERROR" "❌ Verification failed for ${packageName} ${packageVersion}."
        log_message "ERROR" "   Command: $verifyCommand"
        log_message "ERROR" "   Expected version: $versionToMatch"
        log_message "ERROR" "   Actual output: $output"
        unsetopt pipefail # Unset pipefail before returning
        return 1
    fi
    log_message "INFO" "✅ Verification successful."

    # Unset pipefail to avoid affecting other parts of the script
    unsetopt pipefail
}

###
# @description Core "doer" function for packaging.
# Creates a .tar.gz archive based on global PACK_* variables.
# @param $1 string Package name (e.g., "httpd", "php").
# @param $2 string Package version.
# @uses global PACK_INFO
# @uses global PACK_FILES
# @return 0 on success, 1 on failure.
###
core_pack_package() {
    local packageName=$1
    local packageVersion=$2

    # --- Handle name aliasing for packaging ---
    local archiveName=$packageName
    if [[ "$packageName" == "httpd" ]]; then
        archiveName="apache"
    fi
    # --- END ---

    local packSourceDir="${PACK_INFO[source_dir]}"
    if [[ -z "$packSourceDir" ]]; then
        log_message "ERROR" "Packing info incomplete for ${packageName}: PACK_INFO[source_dir] is not set. Check get_package_packing_info."
        return 1
    fi
    if [[ ${#PACK_FILES} -eq 0 ]]; then
        log_message "ERROR" "Packing info incomplete for ${packageName}: PACK_FILES array is empty. Check get_package_packing_info."
        return 1
    fi

    # Use the corrected archiveName for paths and logging
    local distDir="${SERVBAY_BUILD_DIR}/dist/packages/${archiveName}/"
    local archivePath="${distDir}/${archiveName}-${packageVersion}.tar.gz"

    log_message "INFO" "📦 Packing ${packageName} to ${archivePath}..."
    mkdir -p "$distDir"

    if command -v /usr/sbin/dot_clean &> /dev/null; then
        log_message "INFO" "🧹 Running dot_clean on ${packSourceDir}..."
        /usr/sbin/dot_clean -m "${packSourceDir}"
    fi

    log_message "INFO" "➕ Packing items: ${PACK_FILES[*]}"

    # 构建 tar 命令参数
    local tar_args=("--owner=0" "--group=0" "--no-same-owner" "-zcf" "${archivePath}")

    # 全局排除：各包中通用的不必要文件
    tar_args+=("--exclude=.DS_Store")
    tar_args+=("--exclude=*.dSYM")
    tar_args+=("--exclude=__pycache__")
    tar_args+=("--exclude=*.pyc")
    tar_args+=("--exclude=*.pyo")
    tar_args+=("--exclude=*.la")

    # 添加排除模式 (如果有)
    if [[ -n "${PACK_INFO[exclude]}" ]]; then
        log_message "INFO" "➖ Excluding: ${PACK_INFO[exclude]}"
        tar_args+=("--exclude=${PACK_INFO[exclude]}")
    fi

    tar_args+=("${PACK_FILES[@]}")

    ( cd "${packSourceDir}" && "$GTAR_BIN" "${tar_args[@]}" )
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to create package for ${packageName}."
        rm -f "${archivePath}"
        return 1
    fi
    
    log_message "INFO" "📦 Successfully packed ${packageName} as ${archiveName}-${packageVersion}.tar.gz."
}

###
# @description Main orchestrator for post-build tasks.
# @param $1 string Pkg name, $2 string Pkg version.
###
post_build_process() {
    local packageName="$1"
    local packageVersion="$2"

    log_message "INFO" "--- Starting post-build process for ${packageName}-${packageVersion} ---"

    local verify_command
    verify_command=$(get_package_verification_info "$packageName" "$packageVersion")
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    core_verify_package "$packageName" "$packageVersion" "$verify_command"
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    # Homebrew dependency check - automatically run after build, fail before packaging
    log_message "INFO" "🔍 Checking for Homebrew dependencies..."
    if ! check_package_homebrew_deps "$packageName" "$packageVersion" "strict"; then
        log_message "ERROR" "❌ Homebrew dependency check FAILED for ${packageName}-${packageVersion}"
        log_message "ERROR" "Package will NOT be packed. Please fix the Homebrew library links."
        return 1
    fi
    log_message "INFO" "✅ No Homebrew dependencies found"

    # PHP 在扩展编译前已打包，跳过重复打包
    if [[ "$packageName" == "php" ]]; then
        log_message "INFO" "📦 PHP core already packed before extensions build, skipping..."
        log_message "INFO" "--- Post-build process completed successfully ---"
        return 0
    fi

    get_package_packing_info "$packageName" "$packageVersion"
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    core_pack_package "$packageName" "$packageVersion"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "--- Post-build process FAILED for ${packageName}-${packageVersion} ---"
        return 1
    fi

    log_message "INFO" "--- Post-build process completed successfully ---"
    return 0
}


# ==============================================================================
# PACKAGE-SPECIFIC HELPER FUNCTIONS
# ==============================================================================

###
# @description Internal helper to apply a single, specific patch file.
# @param $1 string Full path to the .patch file.
# @param $2 string The patch level to use (e.g., "p0", "p1").
###
_apply_single_patch() {
    local patch_file=$1
    local patch_level=$2

    if [[ -f "$patch_file" ]]; then
        log_message "INFO" "Applying patch: $(basename "$patch_file") with level ${patch_level}..."
        patch --ignore-whitespace -N "-${patch_level}" < "$patch_file"
        if [[ $? -ne 0 ]]; then
            log_message "ERROR" "Failed to apply patch: $(basename "$patch_file")"
            # Returning a non-zero status allows the calling function to handle the error
            return 1
        fi
    else
        log_message "ERROR" "Patch file not found: ${patch_file}"
        return 1
    fi
    return 0
}

###
# @description Internal helper to apply all patch files from a given directory.
# @param $1 string Directory containing .patch files.
# @param $2 string The patch level to use (e.g., "p0", "p1").
###
_apply_patches() {
    local patch_dir=$1
    local patch_level=$2
    if [[ -d "$patch_dir" ]]; then
        log_message "INFO" "Applying patches from ${patch_dir} with level ${patch_level}..."
        for file in "${patch_dir}"/*.patch; do
            if [[ -f "$file" ]]; then
                log_message "INFO" "  -> Applying $(basename "$file")"
                patch --ignore-whitespace -N "-${patch_level}" < "$file"
            fi
        done
    fi
}

###
# @description Internal helper to generate configure arguments for Node.js.
# @param $1 string Full version (e.g., "18.17.1").
# @param $2 string Installation prefix.
# @return Fills the global array 'CONFIGURE_ARGS' with arguments.
###
_get_node_configure_args() {
    local version=$1
    local prefix=$2
    integer version_major_num=$(echo "$version" | awk -F. '{print $1}')
    
    # Start with common args
    CONFIGURE_ARGS=(
        "--prefix=${prefix}"
        "--ninja"
    )

    # Architecture-specific args
    if [[ "$architecture" == "arm64" ]]; then
        # For older versions on arm64 that require Python 2.7
        if (( version_major_num <= 13 )); then
            CONFIGURE_ARGS+=( "--dest-cpu=arm64" )
        fi
    fi
}

###
# @description Ensure cmake 3.x is available for building legacy MySQL 5.x.
# cmake 4.x removed OLD policy behaviors (CMP0018/0022/0026/0042/0045)
# that MySQL 5.x depends on, so cmake 3.x is required.
# Installs cmake 3.31.7 to ${PROJECT_ROOT}/tools/cmake3/ if not present.
# @stdout Path to cmake 3.x binary.
###
_ensure_cmake3() {
    local cmake3_dir="${PROJECT_ROOT}/tools/cmake3"
    local cmake3_bin="${cmake3_dir}/CMake.app/Contents/bin/cmake"

    if [[ -x "$cmake3_bin" ]]; then
        log_message "INFO" "Using existing cmake 3.x: $(${cmake3_bin} --version | head -1)"
        echo "$cmake3_bin"
        return 0
    fi

    log_message "INFO" "Installing cmake 3.x for MySQL 5.x compatibility..."
    local cmake3_url="https://github.com/Kitware/CMake/releases/download/v3.31.7/cmake-3.31.7-macos-universal.tar.gz"
    local cmake3_tar="${SERVBAY_BUILD_SRC_DIR}/cmake-3.31.7-macos-universal.tar.gz"

    download_source "$cmake3_url" "$cmake3_tar"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to download cmake 3.x"
        return 1
    fi

    mkdir -p "$cmake3_dir"
    tar -xzf "$cmake3_tar" --strip-components=1 -C "$cmake3_dir"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to extract cmake 3.x"
        return 1
    fi

    if [[ ! -x "$cmake3_bin" ]]; then
        log_message "ERROR" "cmake 3.x installation failed: $cmake3_bin not found"
        return 1
    fi

    log_message "INFO" "cmake 3.x installed: $(${cmake3_bin} --version | head -1)"
    echo "$cmake3_bin"
    return 0
}

###
# @description Ensure npm is available in PATH (needed by ollama go generate).
# Searches nvm, homebrew, and common system paths.
# Adds the directory containing npm to PATH if not already present.
###
_ensure_npm() {
    # Already available?
    if command -v npm &>/dev/null; then
        log_message "INFO" "npm found: $(command -v npm) ($(npm --version))"
        return 0
    fi

    local npm_bin=""

    # 1. Try nvm
    local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
    if [[ -d "$nvm_dir/versions/node" ]]; then
        # Pick the latest installed node version
        local latest_node
        latest_node=$(ls -1 "$nvm_dir/versions/node/" | sort -V | tail -1)
        if [[ -n "$latest_node" && -x "$nvm_dir/versions/node/$latest_node/bin/npm" ]]; then
            npm_bin="$nvm_dir/versions/node/$latest_node/bin"
        fi
    fi

    # 2. Try homebrew
    if [[ -z "$npm_bin" ]]; then
        for brew_prefix in /opt/homebrew /usr/local; do
            if [[ -x "$brew_prefix/bin/npm" ]]; then
                npm_bin="$brew_prefix/bin"
                break
            fi
        done
    fi

    if [[ -z "$npm_bin" ]]; then
        log_message "ERROR" "npm not found. Install Node.js (via nvm or homebrew) to build ollama."
        return 1
    fi

    export PATH="${npm_bin}:${PATH}"
    log_message "INFO" "Added npm to PATH: ${npm_bin}/npm ($(npm --version))"
    return 0
}

###
# @description Internal helper to generate cmake arguments for MySQL.
# @param $1 string Full version (e.g., "8.0.36").
# @param $2 string Installation prefix.
# @return Fills the global array 'CMAKE_ARGS' with arguments.
###
_get_mysql_cmake_args() {
    local version=$1
    local prefix=$2
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')
    local ver_major=${version%%.*}

    # --- Base args (all versions) ---
    CMAKE_ARGS=(
        "-DCMAKE_INSTALL_PREFIX=${prefix}"
        "-DMYSQL_DATADIR=${SERVBAY_DB_PATH}/mysql/${version_major}"
        "-DMYSQL_UNIX_ADDR=${SERVBAY_TMP_PATH}/mysql-${version_major}.sock"
        "-DDEFAULT_SYSCONFDIR=${SERVBAY_ETC_PATH}/mysql/${version_major}"
        "-DEXTRA_CHARSETS=all"
        "-DDEFAULT_CHARSET=utf8mb4"
        "-DDEFAULT_COLLATION=utf8mb4_general_ci"
        "-DENABLED_LOCAL_INFILE=1"
        "-DWITH_EMBEDDED_SERVER=0"
        "-DWITH_DEBUG=0"
        "-DWITH_UNIT_TESTS=OFF"
        "-DCMAKE_MACOSX_RPATH=1"
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=${BUILD_OS_MIN_VERSION}"
        "-DCMAKE_OSX_ARCHITECTURES=$(echo "$BUILD_CPU_ARCH" | awk '{print $2}')"
        "-DCOMPILATION_COMMENT=ServBay.dev"
    )

    # --- Shared args by version threshold ---
    CMAKE_ARGS+=("-DWITH_ZLIB=bundled")

    # >= 5.6: ServBay OpenSSL, editline, NUMA off
    if [[ "$version_major" != "5.5" ]]; then
        CMAKE_ARGS+=(
            "-DWITH_SSL=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}"
            "-DWITH_EDITLINE=bundled"
            "-DWITH_NUMA=OFF"
        )
    fi

    # >= 5.7: bundled compression/protocol libs + curl
    if (( ver_major >= 8 )) || [[ "$version_major" == "5.7" ]]; then
        CMAKE_ARGS+=(
            "-DWITH_LZ4=bundled"
            "-DWITH_PROTOBUF=bundled"
            "-DWITH_CURL=${SERVBAY_COMMON_PATH}"
        )
    fi

    # >= 8.0: disable system libs, bundled alternatives
    if (( ver_major >= 8 )); then
        CMAKE_ARGS+=(
            "-DWITH_SYSTEM_LIBS=OFF"
            "-DWITH_FIDO=bundled"
            "-DWITH_ICU=bundled"
            "-DWITH_ZSTD=bundled"
        )
    fi

    # --- Version-specific args (only truly unique options) ---
    case "$version_major" in
        "5.5")
            CMAKE_ARGS+=("-DWITH_SSL=bundled" "-DCMAKE_CXX_STANDARD=98")
            ;;
        "5.6")
            CMAKE_ARGS+=("-DWITH_INNODB_MEMCACHED=ON" "-DOPENSSL_ROOT_DIR=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}")
            ;;
        "5.7")
            CMAKE_ARGS+=("-DWITH_BOOST=./boost_1_59_0" "-DWITH_INNODB_MEMCACHED=ON")
            ;;
        "8.0"|"8.1"|"8.2"|"9.0"|"9.1"|"9.2"|"9.3")
            CMAKE_ARGS+=("-DWITH_BOOST=./boost_1_77_0" "-DWITH_LIBEVENT=bundled" "-DWITH_INNODB_MEMCACHED=ON")
            ;;
        "8.3")
            CMAKE_ARGS+=("-DWITH_LIBEVENT=bundled")
            ;;
        # 8.4+ / 9.4+: all needed args covered by shared sections above
    esac

    # 8.4+/9.x: MySQL Router 的 harness_tls 会自己跑一遍 find_package(OpenSSL)；
    # 不 pin OPENSSL_ROOT_DIR 时它会抓到 CI runner 的 brew openssl@3，按 3.x 头文件
    # 编译 tls_*_context.cc 却链接 base 的 OpenSSL，产生未定义的
    # OSSL_DECODER_*/OSSL_PARAM_* 符号（v1 bf1d8e2）。
    # 8.0-8.3 没有 Router/harness_tls，v1 也从未钉过；按用户裁决做防御性一致：
    # WITH_SSL 与 FindOpenSSL 永远指向同一个 base OpenSSL，即使未来 8.x 点版本
    # 内部新增 find_package(OpenSSL) 消费者也不会漂移到 brew。
    if [[ "$version_major" == 8.* || "$version_major" == 9.* ]]; then
        CMAKE_ARGS+=("-DOPENSSL_ROOT_DIR=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}")
    fi

    # MySQL 9.7 引入基于 OpenTelemetry 的 telemetry（client plugin + server component），
    # 二者都经 ext::curl 链接 opentelemetry_http_client_curl。WITH_CURL 指向 ServBay 的
    # 静态 libcurl.a 时，cmake/curl.cmake 的 FIND_CUSTOM_CURL 只把 libcurl.a 本身传播进
    # curl_interface，不传播其私有依赖（libnghttp2/libbrotli），telemetry_client.so 链接
    # 必现未定义 _nghttp2_* 符号（v1 1c73242）。ServBay 是本地开发环境，从不导出 OTLP
    # telemetry，两个开关全部关闭，顺带跳过捆绑的 opentelemetry-cpp 编译。
    if [[ "$version_major" == "9.7" ]]; then
        CMAKE_ARGS+=(
            "-DWITH_MYSQL_CLIENT_TELEMETRY=OFF"
            "-DWITH_MYSQL_SERVER_TELEMETRY=OFF"
        )
    fi
}

###
# @description Internal helper to get the correct major version for .NET SDK,
# handling special cases.
# @param $1 string Full package version.
# @stdout The calculated major version string.
###
_get_dotnetsdk_major_version() {
    local version=$1
    if [[ "$version" == "2.1.202" ]]; then
        echo "2.0"
    else
        # Default logic for other versions
        echo $(echo "$version" | awk -F. '{print $1"."$2}')
    fi
}

###
# @description Internal helper to generate configure arguments for Python.
# @param $1 string Full version (e.g., "3.9.18").
# @param $2 string Installation prefix.
# @return Fills the global array 'CONFIGURE_ARGS' with arguments.
###
_get_python_configure_args() {
    local version=$1
    local prefix=$2
    local version_major_str=$(echo "$version" | awk -F. '{print $1}')
    local version_minor_str=$(echo "$version" | awk -F. '{print $2}')
    
    # Convert to integers for numerical comparison
    integer major_version=${version_major_str}
    integer minor_version=${version_minor_str}

    # Start with common args
    CONFIGURE_ARGS=(
        "--prefix=${prefix}"
        "--enable-ipv6"
        "--enable-optimizations"
        "--with-system-expat"
        "--with-lto"
        "--enable-framework=${prefix}"
    )

    if (( major_version == 2 )); then
        # Python 2.x specific args (if any)
        # The base args are mostly sufficient
    elif (( major_version == 3 )); then
        CONFIGURE_ARGS+=( "--enable-loadable-sqlite-extensions" )
        if (( minor_version >= 9 )); then
            # Python 3.9+
            CONFIGURE_ARGS+=( "--with-openssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" "--with-readline=editline" )
        elif (( minor_version >= 7 )); then
            # Python 3.7, 3.8
            CONFIGURE_ARGS+=( "--with-openssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" )
        elif (( minor_version >= 1 )); then
            # Python 3.1 - 3.6 (no OpenSSL arg needed)
        else
            # Python 3.0 (removes --enable-ipv6 and --with-lto)
            # This is complex, for now we assume modern versions.
            # If 3.0 support is critical, we can add more logic here.
        fi
    fi
}

###
# @description Internal helper for creating symlinks.
# @param $1 string The base directory of the Python installation.
# @param $2 string The major.minor version string (e.g., "3.9").
# @param $3 array  An array of "target:source" strings for framework bin dir.
# @param $4 array  An array of "target:source" strings for top-level bin dir.
###
_create_symlinks() {
    local base_dir=$1
    local version_major=$2
    # Use =(z) to split the string parameter back into an array
    local -a framework_symlinks=( ${(z)3} )
    local -a bin_symlinks=( ${(z)4} )

    local framework_bin_dir="${base_dir}/Python.framework/Versions/${version_major}/bin"
    local bin_dir="${base_dir}/bin"

    if [[ ! -d "$framework_bin_dir" ]] || [[ ! -d "$bin_dir" ]]; then
        log_message "ERROR" "Python symlink creation failed: bin directories not found."
        return 1
    fi

    # Create links in framework bin directory
    for link_info in "${framework_symlinks[@]}"; do
        local target_path=${link_info%%:*}
        local source_path=${link_info#*:}
        if [[ -e "${framework_bin_dir}/${source_path}" ]] && [[ ! -e "${framework_bin_dir}/${target_path}" ]]; then
            ln -sf "${source_path}" "${framework_bin_dir}/${target_path}"
        fi
    done

    # Create links in top-level bin directory
    for link_info in "${bin_symlinks[@]}"; do
        local target_path=${link_info%%:*}
        local source_path=${link_info#*:}
        if [[ -e "${source_path}" ]] && [[ ! -e "${target_path}" ]]; then
            ln -sf "${source_path}" "${target_path}"
        fi
    done
}

###
# @description Wrapper to create Python 2 style symlinks.
###
_create_python_symlinks2() {
    local version_major=$1
    local version=$2
    local base_dir="${SERVBAY_PACKAGE_FULL_PATH}/python/${version_major}"
    
    local framework_links_str="2to3:2to3-${version_major} easy_install2:easy_install-${version_major} easy_install:easy_install-${version_major} idle2:idle${version_major} idle:idle${version_major} pip2:pip${version_major} pip:pip${version_major} pydoc2:pydoc${version_major} pydoc:pydoc${version_major} python2:python${version_major} python:python${version_major} python2-config:python${version_major}-config python-config:python${version_major}-config wheel2:wheel wheel${version_major}:wheel"
    
    local bin_links_str="2to3:../Python.framework/Versions/${version_major}/bin/2to3 easy_install2:../Python.framework/Versions/${version_major}/bin/easy_install2 easy_install-${version_major}:../Python.framework/Versions/${version_major}/bin/easy_install-${version_major} easy_install:../Python.framework/Versions/${version_major}/bin/easy_install idle2:../Python.framework/Versions/${version_major}/bin/idle2 idle:../Python.framework/Versions/${version_major}/bin/idle pip2:../Python.framework/Versions/${version_major}/bin/pip2 pip${version_major}:../Python.framework/Versions/${version_major}/bin/pip${version_major} pip:../Python.framework/Versions/${version_major}/bin/pip pydoc2:../Python.framework/Versions/${version_major}/bin/pydoc2 pydoc:../Python.framework/Versions/${version_major}/bin/pydoc python2:../Python.framework/Versions/${version_major}/bin/python2 python:../Python.framework/Versions/${version_major}/bin/python python2-config:../Python.framework/Versions/${version_major}/bin/python2-config python-config:../Python.framework/Versions/${version_major}/bin/python-config wheel2:../Python.framework/Versions/${version_major}/bin/wheel2 wheel:../Python.framework/Versions/${version_major}/bin/wheel wheel${version_major}:../Python.framework/Versions/${version_major}/bin/wheel${version_major} pythonw:../Python.framework/Versions/${version_major}/bin/pythonw pythonw2:../Python.framework/Versions/${version_major}/bin/pythonw2 smtpd.py:../Python.framework/Versions/${version_major}/bin/smtpd.py smtpd2.py:../Python.framework/Versions/${version_major}/bin/smtpd2.py"
    
    _create_symlinks "$base_dir" "$version_major" "$framework_links_str" "$bin_links_str"
}

###
# @description Wrapper to create Python 3 style symlinks.
###
_create_python_symlinks3() {
    local version_major=$1
    local version=$2
    local base_dir="${SERVBAY_PACKAGE_FULL_PATH}/python/${version_major}"
    
    local framework_links_str="2to3:2to3-${version_major} easy_install3:easy_install-${version_major} easy_install:easy_install-${version_major} idle3:idle${version_major} idle:idle${version_major} pip3:pip${version_major} pip:pip${version_major} pydoc3:pydoc${version_major} pydoc:pydoc${version_major} python3:python${version_major} python:python${version_major} python3-config:python${version_major}-config python-config:python${version_major}-config pyvenv3:pyvenv-${version_major} pyvenv:pyvenv-${version_major} wheel3:wheel wheel${version_major}:wheel"
    
    local bin_links_str="2to3:../Python.framework/Versions/${version_major}/bin/2to3 easy_install3:../Python.framework/Versions/${version_major}/bin/easy_install3 easy_install-${version_major}:../Python.framework/Versions/${version_major}/bin/easy_install-${version_major} easy_install:../Python.framework/Versions/${version_major}/bin/easy_install idle3:../Python.framework/Versions/${version_major}/bin/idle3 idle:../Python.framework/Versions/${version_major}/bin/idle pip3:../Python.framework/Versions/${version_major}/bin/pip3 pip${version_major}:../Python.framework/Versions/${version_major}/bin/pip${version_major} pip:../Python.framework/Versions/${version_major}/bin/pip pydoc3:../Python.framework/Versions/${version_major}/bin/pydoc3 pydoc:../Python.framework/Versions/${version_major}/bin/pydoc python3:../Python.framework/Versions/${version_major}/bin/python3 python:../Python.framework/Versions/${version_major}/bin/python python3-config:../Python.framework/Versions/${version_major}/bin/python3-config python-config:../Python.framework/Versions/${version_major}/bin/python-config pyvenv3:../Python.framework/Versions/${version_major}/bin/pyvenv3 pyvenv:../Python.framework/Versions/${version_major}/bin/pyvenv wheel3:../Python.framework/Versions/${version_major}/bin/wheel3 wheel:../Python.framework/Versions/${version_major}/bin/wheel wheel${version_major}:../Python.framework/Versions/${version_major}/bin/wheel${version_major}"
    
    _create_symlinks "$base_dir" "$version_major" "$framework_links_str" "$bin_links_str"
}


# ==============================================================================
# PACKAGE-SPECIFIC MODULES BUILD FUNCTIONS
# ==============================================================================

###
# @description Internal build logic for the PostGIS extension.
# @param $1 string Module version (e.g., "3.4.2").
# @param $2 string PostgreSQL full version (e.g., "16.1").
# @param $3 string PostgreSQL major version (e.g., "16").
# @param $4 string Installation prefix for PostgreSQL.
###
_build_pgsql_mod_postgis() {
    local mod_version=$1
    local pg_version=$2
    local pg_version_major=$3
    local pg_prefix=$4
    local pg_config="${pg_prefix}/bin/pg_config"

    log_message "INFO" "  -> Building module: PostGIS v${mod_version}"
    local mod_source_filename="postgis-${mod_version}.tar.gz"
    local mod_package_fullname="postgis-${mod_version}"

    if ! download_source "https://download.osgeo.org/postgis/source/${mod_source_filename}" "${SERVBAY_BUILD_SRC_DIR}/${mod_source_filename}"; then
        log_message "ERROR" "Failed to download PostGIS."
        return 1
    fi

    unpack_package "$mod_package_fullname" "$mod_source_filename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # Replicating the original configure flags with proper environment variables
    CFLAGS="${CFLAGS} -I${SERVBAY_COMMON_INCLUDE_PATH}/libxml2 -I${SERVBAY_COMMON_INCLUDE_PATH}" \
    CXXFLAGS="${CXXFLAGS} -std=c++17 -I${SERVBAY_COMMON_INCLUDE_PATH}/libxml2 -I${SERVBAY_COMMON_INCLUDE_PATH}" \
    LDFLAGS="${LDFLAGS} -L${SERVBAY_COMMON_LIB_PATH} -lxml2 -lz -lpthread -liconv -licui18n -licuuc -licudata -lm" \
    ./configure --prefix="${pg_prefix}" \
                --bindir="${pg_prefix}/bin" \
                --datarootdir="${pg_prefix}/share" \
                --with-pgconfig="${pg_config}" \
                --disable-nls \
                --without-protobuf \
                --without-raster \
                --without-topology
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PostGIS configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    make -j${CPU_NUMBER} && make install DESTDIR="${DESTDIR}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PostGIS make/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    local extension_bindir="${DESTDIR}$($pg_config --bindir)"
    if [[ ! -f "${extension_bindir}/postgis" ]]; then # Check for a key binary
        log_message "ERROR" "PostGIS installation verification failed: postgis binary not found."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi
    log_message "INFO" "  -> PostGIS module verified successfully."

    cd "$SERVBAY_BUILD_DIR"
    cleanup_source_directory "$mod_package_fullname"
}

###
# @description Internal build logic for the pgRouting extension.
# @param $1 string Module version (e.g., "3.8.0").
# @param $2 string PostgreSQL full version (e.g., "16.1").
# @param $3 string PostgreSQL major version (e.g., "16").
# @param $4 string Installation prefix for PostgreSQL.
###
_build_pgsql_mod_pgrouting() {
    local mod_version=$1
    local pg_version=$2
    local pg_version_major=$3
    local pg_prefix=$4
    local pg_config="${pg_prefix}/bin/pg_config"

    log_message "INFO" "  -> Building module: pgRouting v${mod_version}"
    local mod_source_filename="pgrouting-${mod_version}.tar.gz"
    local mod_package_fullname="pgrouting-${mod_version}"
    
    if ! download_source "https://github.com/pgRouting/pgrouting/archive/refs/tags/v${mod_version}.tar.gz" "${SERVBAY_BUILD_SRC_DIR}/${mod_source_filename}"; then
        log_message "ERROR" "Failed to download pgRouting."
        return 1
    fi
    
    unpack_package "$mod_package_fullname" "$mod_source_filename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # Boost 通过 Homebrew 安装，需要指向 Homebrew 的 include 路径
    local boost_prefix
    boost_prefix="$(brew --prefix boost 2>/dev/null)"
    local boost_include="${boost_prefix}/include"
    if [[ -z "$boost_prefix" ]] || [[ ! -f "${boost_include}/boost/version.hpp" ]]; then
        log_message "ERROR" "Boost not found via Homebrew. Please install: brew install boost"
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    local cmake_args=(
        "-DBoost_INCLUDE_DIR=${boost_include}"
        "-DCMAKE_MACOSX_RPATH=1"
        "-DCMAKE_INSTALL_RPATH=${SERVBAY_COMMON_LIB_PATH}"
        "-DPOSTGRESQL_PG_CONFIG=${pg_config}"
    )
    cmake -S . -B builddir "${cmake_args[@]}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "pgRouting configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    
    cmake --build builddir -j ${CPU_NUMBER} && cmake --install builddir
    if [[ $? -ne 0 ]]; then log_message "ERROR" "pgRouting build/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    local extension_libdir="${DESTDIR}$($pg_config --pkglibdir)"
    if [[ ! -f "${extension_libdir}/libpgrouting-3.8.so" ]]; then
        log_message "ERROR" "pgRouting installation verification failed: library not found in ${extension_libdir}."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi
    log_message "INFO" "  -> pgRouting module verified successfully."

    cd "$SERVBAY_BUILD_DIR"
    cleanup_source_directory "$mod_package_fullname"
}

###
# @description Internal build logic for the pgvector extension.
# @param $1 string Module version (e.g., "0.8.0").
# @param $2 string PostgreSQL full version (e.g., "16.1").
# @param $3 string PostgreSQL major version (e.g., "16").
# @param $4 string Installation prefix for PostgreSQL.
###
_build_pgsql_mod_pgvector() {
    local mod_version=$1
    local pg_version=$2
    local pg_version_major=$3
    local pg_prefix=$4
    local pg_config="${pg_prefix}/bin/pg_config"

    log_message "INFO" "  -> Building module: pgvector v${mod_version}"
    local mod_source_filename="pgvector-${mod_version}.tar.gz"
    local mod_package_fullname="pgvector-${mod_version}"

    if ! download_source "https://github.com/pgvector/pgvector/archive/refs/tags/v${mod_version}.tar.gz" "${SERVBAY_BUILD_SRC_DIR}/${mod_source_filename}"; then
        log_message "ERROR" "Failed to download pgvector."
        return 1
    fi

    unpack_package "$mod_package_fullname" "$mod_source_filename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # Apply patch for PostgreSQL 18 compatibility
    if [[ "$pg_version_major" == "18" && "$mod_version" == "0.8.0" ]]; then
        local patch_file="${PATCH_DIR}/pgsql/pgvector-pg18-compat.patch"
        if [[ -f "$patch_file" ]]; then
            log_message "INFO" "  -> Applying PostgreSQL 18 compatibility patch"
            patch -p0 < "$patch_file"
            if [[ $? -ne 0 ]]; then
                log_message "ERROR" "Failed to apply PostgreSQL 18 compatibility patch."
                cd "$SERVBAY_BUILD_DIR"
                return 1
            fi
            log_message "INFO" "  -> Patch applied successfully"
        else
            log_message "WARNING" "PostgreSQL 18 compatibility patch not found: ${patch_file}"
        fi
    fi

    make -j${CPU_NUMBER} OPTFLAGS="" PG_CONFIG="${pg_config}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "pgvector make failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    make install OPTFLAGS="" DESTDIR="${DESTDIR}" PG_CONFIG="${pg_config}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "pgvector make install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    local extension_libdir="${DESTDIR}$($pg_config --pkglibdir)"
    if [[ ! -f "${extension_libdir}/vector.dylib" && ! -f "${extension_libdir}/vector.so" ]]; then
        log_message "ERROR" "pgvector installation verification failed: library not found in ${extension_libdir}."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi
    log_message "INFO" "  -> pgvector module verified successfully."

    cd "$SERVBAY_BUILD_DIR"
    cleanup_source_directory "$mod_package_fullname"
}

###
# @description Internal build logic for the pg_jieba extension.
# @param $1 string Module version (e.g., "1.1.1").
# @param $2 string PostgreSQL full version (e.g., "16.1").
# @param $3 string PostgreSQL major version (e.g., "16").
# @param $4 string Installation prefix for PostgreSQL.
###
_build_pgsql_mod_pg_jieba() {
    local mod_version=$1
    local pg_version=$2
    local pg_version_major=$3
    local pg_prefix=$4
    local pg_config="${pg_prefix}/bin/pg_config"

    log_message "INFO" "  -> Building module: pg_jieba v${mod_version}"
    local package_fullname="pg_jieba-${mod_version}"
    local main_archive="${SERVBAY_BUILD_SRC_DIR}/${package_fullname}.tar.gz"
    local component_archive="${SERVBAY_BUILD_SRC_DIR}/${package_fullname}-cppjieba.tar.gz"
    local pin_data
    pin_data=$(python3 -c 'import json,sys; p=json.load(open(sys.argv[1]))["recipes"]["pg_jieba"]["source_pins"][sys.argv[2]]; print(p["url"]); print(p["sha256"]); print(p["component"]["url"]); print(p["component"]["sha256"])' "$PROJECT_ROOT/config/modules.json" "$mod_version") || return 1
    local -a source_values=( "${(@f)pin_data}" )
    [[ ${#source_values} == 4 ]] || return 1
    download_source "$source_values[1]" "$main_archive" || return 1
    download_source "$source_values[3]" "$component_archive" || return 1
    local actual
    actual=$(shasum -a 256 "$main_archive") || return 1
    [[ "${actual%% *}" == "$source_values[2]" ]] || { log_message "ERROR" "pg_jieba release SHA256 differs from its pinned source"; return 1; }
    actual=$(shasum -a 256 "$component_archive") || return 1
    [[ "${actual%% *}" == "$source_values[4]" ]] || { log_message "ERROR" "pg_jieba cppjieba gitlink SHA256 differs from its pinned source"; return 1; }
    unpack_package "$package_fullname" "${package_fullname}.tar.gz" || return 1
    mkdir -p libjieba || return 1
    "$GTAR_BIN" -xzf "$component_archive" --strip-components=1 -C libjieba || return 1
    local cmake_args=(
        "-DCMAKE_PREFIX_PATH=${pg_prefix}"
        "-DPostgreSQL_PG_CONFIG=${pg_config}"
        "-DPostgreSQL_EXECUTABLE=${pg_prefix}/bin/postgres"
    )
    cmake -S . -B builddir "${cmake_args[@]}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "pg_jieba configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    cmake --build builddir -j ${CPU_NUMBER} && cmake --install builddir
    if [[ $? -ne 0 ]]; then log_message "ERROR" "pg_jieba build/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    local extension_libdir="${DESTDIR}$($pg_config --pkglibdir)"
    if [[ ! -f "${extension_libdir}/pg_jieba.dylib" && ! -f "${extension_libdir}/pg_jieba.so" ]]; then
        log_message "ERROR" "pg_jieba installation verification failed: library not found in ${extension_libdir}."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi
    log_message "INFO" "  -> pg_jieba module verified successfully."

    cd "$SERVBAY_BUILD_DIR" || return 1
    cleanup_source_directory "$package_fullname"
}

###
# @description Internal build logic for the zhparser extension.
# @param $1 string Module version (e.g., "2.3").
# @param $2 string PostgreSQL full version (e.g., "16.1").
# @param $3 string PostgreSQL major version (e.g., "16").
# @param $4 string Installation prefix for PostgreSQL.
###
_build_pgsql_mod_zhparser() {
    local mod_version=$1
    local pg_version=$2
    local pg_version_major=$3
    local pg_prefix=$4
    local pg_config="${pg_prefix}/bin/pg_config"

    log_message "INFO" "  -> Building module: zhparser v${mod_version}"
    local mod_source_filename="zhparser-${mod_version}.tar.gz"
    local mod_package_fullname="zhparser-${mod_version}"

    if ! download_source "https://github.com/amutu/zhparser/archive/refs/tags/v${mod_version}.tar.gz" "${SERVBAY_BUILD_SRC_DIR}/${mod_source_filename}"; then
        log_message "ERROR" "Failed to download zhparser."
        return 1
    fi

    unpack_package "$mod_package_fullname" "$mod_source_filename"
    if [[ $? -ne 0 ]]; then return 1; fi

    make -j${CPU_NUMBER} PG_CONFIG="${pg_config}" SCWS_HOME="${SERVBAY_COMMON_PATH}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "zhparser make failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    make install DESTDIR="${DESTDIR}" PG_CONFIG="${pg_config}" SCWS_HOME="${SERVBAY_COMMON_PATH}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "zhparser make install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    local extension_libdir="${DESTDIR}$($pg_config --pkglibdir)"
    if [[ ! -f "${extension_libdir}/zhparser.dylib" && ! -f "${extension_libdir}/zhparser.so" ]]; then
        log_message "ERROR" "zhparser installation verification failed: library not found in ${extension_libdir}."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi
    log_message "INFO" "  -> zhparser module verified successfully."

    cd "$SERVBAY_BUILD_DIR"
    cleanup_source_directory "$mod_package_fullname"
}

# ==============================================================================
# PACKAGE-SPECIFIC BUILD FUNCTIONS
# ==============================================================================

###
# @description Build logic for the Redis package.
# @param $1 string Pkg version, $2 string Src filename, $3 string Install dir.
###
build_redis() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="redis-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')

    # Determine the installation prefix (multi-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/redis/${version_major}"
    fi
    mkdir -p "$prefix"

    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    log_message "INFO" "Compiling Redis ${version}..."
    # LDFLAGS="" prevents our global gcc-style LDFLAGS (e.g. -isysroot, -Wl,-rpath,
    # -mmacosx-version-min) from leaking into Redis's tests/modules Makefile, which
    # invokes ld directly and cannot handle these compiler-driver flags.
    # Redis finds OpenSSL via OPENSSL_PREFIX and links its own deps in-tree.
    # LD="$(CC)" routes every link through the compiler driver: redis's
    # src/modules/Makefile hardcodes "-L /Library/Developer/CommandLineTools/
    # SDKs/MacOSX.sdk/usr/lib -lsystem" on Darwin, and the Intel runner's CLT ld
    # cannot parse that SDK's libSystem.B.tbd (arm64e.x1 unknown architecture,
    # x86_64 run 39962827584) while the clang driver handles it (v1 8.4.2 same).
    make install PREFIX="${prefix}" BUILD_TLS=yes CFLAGS="${CFLAGS}" LDFLAGS="" OPENSSL_PREFIX="${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" CC=gcc LD="$(CC)" MACOSX_DEPLOYMENT_TARGET="${BUILD_OS_MIN_VERSION}"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Redis 'make install' failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    log_message "INFO" "Creating directories and configuring Redis..."
    mkdir -p "${SERVBAY_ETC_PATH}/redis/${version_major}" "${SERVBAY_LOG_PATH}/redis" "${SERVBAY_DB_PATH}/redis/${version_major}"
    # cp redis.conf "${prefix}/etc/redis/"
    # cp sentinel.conf "${prefix}/etc/redis/"
    
    # local redis_conf_path="${prefix}/etc/redis/redis.conf"
    # sed -i '' 's/^bind 127.0.0.1 -::1/bind 127.0.0.1 ::1/' "$redis_conf_path"
    # sed -i '' 's/^daemonize no/daemonize yes/' "$redis_conf_path"
    # sed -i '' "s|^# pidfile /var/run/redis_6379.pid|pidfile ${prefix}/tmp/redis.pid|" "$redis_conf_path"
    # sed -i '' "s|^# logfile \"\"|logfile ${prefix}/logs/redis/redis.log|" "$redis_conf_path"
    # sed -i '' "s|^dir ./|dir ${prefix}/db/redis/|" "$redis_conf_path"

    cd "$SERVBAY_BUILD_DIR"
}

###
# @description Build logic for the Memcached server.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_memcached() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="memcached-${version}"

    # 1. Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/memcached"
    fi
    mkdir -p "$prefix"
    
    # Unpack the source code. This changes the current directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 2. Configure Memcached.
    # Note: Many paths are hardcoded to the final ServBay structure,
    # but the core installation prefix respects the 'prefix' variable.
    log_message "INFO" "Configuring Memcached v${version}..."
    ./configure \
        --prefix="${prefix}" \
        --enable-tls \
        --enable-64bit \
        --disable-coverage

    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Memcached configure failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 3. Compile and Install
    log_message "INFO" "Compiling Memcached (make -j${CPU_NUMBER})..."
    make -j${CPU_NUMBER}
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Memcached make failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    log_message "INFO" "Installing Memcached..."
    make install
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Memcached make install failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 4. Post-installation setup
    log_message "INFO" "Creating Memcached log directory..."
    mkdir -p "${SERVBAY_LOG_PATH}/memcached"

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Memcached build process completed successfully."
}

###
# @description Build logic for the Caddy web server.
# @param $1 string Package version.
# @param $2 string Source filename (ignored for Caddy).
# @param $3 string Temporary installation directory (optional).
###
use_pinned_go_1_26() {
    local go126_bin=""
    [[ -x /opt/homebrew/opt/go@1.26/bin/go ]] && go126_bin="/opt/homebrew/opt/go@1.26/bin/go"
    [[ -z "$go126_bin" && -x /usr/local/opt/go@1.26/bin/go ]] && go126_bin="/usr/local/opt/go@1.26/bin/go"
    if [[ -z "$go126_bin" ]]; then
        log_message "WARN" "go@1.26 not found; falling back to PATH go (may emit minos 13.0)."
        return 0
    fi
    export PATH="$(dirname "$go126_bin"):${GOPATH}/bin:${PATH}"
    # local = never auto-download a newer toolchain; 1.26 stays in effect.
    export GOTOOLCHAIN=local
    log_message "INFO" "Using pinned Go 1.26 (go@1.26) for this build."
}

build_ai_gateway() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="ai-gateway-${version}"
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}

    : "${GH_TOKEN:?GH_TOKEN (PAT with ServBay/ServBay-AI-Gateway read) required for ai-gateway}"

    # macOS ships a universal asset (one binary for both arches → both slots).
    local asset="servbay-ai-gateway-${version}-macos-universal.tar.gz"
    local dlDir="${SERVBAY_BUILD_SRC_DIR}/ai-gateway-dl"
    trash_path "$dlDir"; mkdir -p "$dlDir"

    log_message "INFO" "Downloading ${asset} from private release v${version} via gh..."
    if ! gh release download "v${version}" \
        --repo ServBay/ServBay-AI-Gateway \
        --pattern "${asset}" --pattern "${asset}.sha256" \
        --dir "$dlDir" --clobber; then
        log_message "ERROR" "gh release download failed for ai-gateway v${version}"
        return 1
    fi

    # Verify the checksum published alongside the asset.
    if ! ( cd "$dlDir" && shasum -a 256 -c "${asset}.sha256" ); then
        log_message "ERROR" "SHA-256 mismatch for ${asset}"
        return 1
    fi

    # Hand the archive to unpack_package under the standardized name it expects.
    mv "${dlDir}/${asset}" "${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"

    # Flat archive (bare binary at the root) → no --strip-components.
    unpack_package "$packageFullName" "$sourceFilename" "" "false"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/ai-gateway/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Installing ai-gateway binary..."
    # We are currently inside the unpacked directory.
    cp servbay-ai-gateway "$bin_dir/"
    chmod +x "${bin_dir}/servbay-ai-gateway"

    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "ai-gateway setup complete."
}

build_disk_cleaner() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="disk-cleaner-${version}"
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}

    : "${GH_TOKEN:?GH_TOKEN (PAT with ServBay/ServBay-DiskCleaner read) required for disk-cleaner}"

    # macOS ships a universal asset (one binary for both arches → both slots).
    local asset="servbay-diskcleaner-${version}-macos-universal.tar.gz"
    local dlDir="${SERVBAY_BUILD_SRC_DIR}/disk-cleaner-dl"
    trash_path "$dlDir"; mkdir -p "$dlDir"

    log_message "INFO" "Downloading ${asset} from private release v${version} via gh..."
    if ! gh release download "v${version}" \
        --repo ServBay/ServBay-DiskCleaner \
        --pattern "${asset}" --pattern "${asset}.sha256" \
        --dir "$dlDir" --clobber; then
        log_message "ERROR" "gh release download failed for disk-cleaner v${version}"
        return 1
    fi

    # Verify the checksum published alongside the asset.
    if ! ( cd "$dlDir" && shasum -a 256 -c "${asset}.sha256" ); then
        log_message "ERROR" "SHA-256 mismatch for ${asset}"
        return 1
    fi

    # Hand the archive to unpack_package under the standardized name it expects.
    mv "${dlDir}/${asset}" "${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"

    # Flat archive (bare binary at the root) → no --strip-components.
    unpack_package "$packageFullName" "$sourceFilename" "" "false"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/disk-cleaner/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Installing disk-cleaner binary..."
    # We are currently inside the unpacked directory.
    cp servbay-diskcleaner "$bin_dir/"
    chmod +x "${bin_dir}/servbay-diskcleaner"

    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "disk-cleaner setup complete."
}

build_caddy() {
    local version=$1
    # sourceFilename is ignored as Caddy is built from source using xcaddy
    local installDir=$3

    # Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/caddy"
    fi
    mkdir -p "${prefix}/bin"

    log_message "INFO" "Preparing to build Caddy v${version}..."

    # Caddy build doesn't use a pre-downloaded source, so we stay in the main build dir
    # Ensure we are in the main build directory to have access to conf files etc.
    cd "$SERVBAY_BUILD_DIR"

    local macos_min="${BUILD_OS_MIN_VERSION%%.*}.0"
    export MACOSX_DEPLOYMENT_TARGET="${macos_min}"
    export CGO_CFLAGS="-mmacosx-version-min=${macos_min} ${CGO_CFLAGS}"
    export CGO_LDFLAGS="-mmacosx-version-min=${macos_min} ${CGO_LDFLAGS}"
    use_pinned_go_1_26 || return 1
    GO_BIN="$(command -v go)"

    # 1. Install xcaddy, the Caddy builder tool
    log_message "INFO" "Installing/updating xcaddy tool..."
    # The Go environment is already set up by build_package
    if ! "$GO_BIN" install github.com/caddyserver/xcaddy/cmd/xcaddy@latest; then
        log_message "ERROR" "Failed to install xcaddy. Please check your Go environment."
        return 1
    fi

    # 2. Build Caddy with a comprehensive list of plugins
    log_message "INFO" "Building Caddy with custom plugins..."
    # The xcaddy binary is located in $GOPATH/bin, which is in our PATH
    # Using backslashes for readability
    if ! xcaddy build "v${version}" \
        --output "${prefix}/bin/caddy" \
        --with github.com/caddy-dns/cloudflare \
        --with github.com/caddy-dns/dnspod \
        --replace github.com/libdns/dnspod=github.com/naizhao/caddy-dnspod@c534e5a8a624 \
        --with github.com/caddy-dns/route53 \
        --with github.com/caddy-dns/godaddy \
        --with github.com/caddy-dns/googleclouddns \
        --with github.com/caddy-dns/azure \
        --with github.com/caddy-dns/alidns \
        --with github.com/caddy-dns/tencentcloud \
        --with github.com/caddy-dns/he \
        --with github.com/caddy-dns/duckdns \
        --with github.com/caddy-dns/digitalocean \
        --with github.com/caddy-dns/namecheap \
        --with github.com/caddy-dns/hetzner \
        --with github.com/caddy-dns/porkbun \
        --with github.com/caddy-dns/ovh \
        --with github.com/caddy-dns/gandi \
        --with github.com/caddy-dns/powerdns \
        --with github.com/caddy-dns/netlify \
        --with github.com/caddy-dns/linode \
        --with github.com/caddy-dns/vultr \
        --with github.com/caddy-dns/easydns \
        --with github.com/caddy-dns/dnsimple \
        --with github.com/caddy-dns/cloudns \
        --with github.com/ueffel/caddy-brotli \
        --with github.com/caddyserver/transform-encoder \
        --with github.com/caddyserver/replace-response \
        --with github.com/mholt/caddy-webdav \
        --with github.com/greenpau/caddy-security \
        --with github.com/mholt/caddy-dynamicdns \
        --with github.com/caddyserver/nginx-adapter; then
        log_message "ERROR" "xcaddy build failed."
        return 1
    fi

    # 3. Create necessary directories
    log_message "INFO" "Creating Caddy directories..."
    mkdir -p "${SERVBAY_ETC_PATH}/caddy/vhosts"
    mkdir -p "${SERVBAY_ETC_PATH}/caddy/ssl"
    mkdir -p "${SERVBAY_LOG_PATH}/caddy"

    # 4. Copy and configure default configuration files
    # Assuming conf/caddy exists in the root of your build project
    if [[ -d "${SERVBAY_BUILD_DIR}/conf/caddy" ]]; then
        log_message "INFO" "Copying and processing Caddy configuration files..."
        local caddy_conf_dir="${SERVBAY_ETC_PATH}/caddy"
        cp "${SERVBAY_BUILD_DIR}/conf/caddy/Caddyfile" "${caddy_conf_dir}/"
        cp "${SERVBAY_BUILD_DIR}/conf/caddy/envfile" "${caddy_conf_dir}/"
        
        local caddyfile_path="${caddy_conf_dir}/Caddyfile"
        # Replace placeholder with the actual installation prefix
        sed -i '' "s|@@SERVBAY_BASE_PREFIX@@|${prefix}|g" "${caddyfile_path}"
    else
        log_message "WARN" "Default Caddy configuration directory not found at '${SERVBAY_BUILD_DIR}/conf/caddy'."
    fi

    log_message "INFO" "Caddy build process completed successfully."
}

###
# @description Build logic for the Nginx web server.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_nginx() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="nginx-${version}"

    # 1. Determine the installation prefix, respecting installDir override.
    local prefix
    if [[ -n "$installDir" ]]; then
        # If installDir is provided, use it directly as the prefix.
        prefix="$installDir"
    else
        # Otherwise, use the standard ServBay path structure.
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/nginx"
    fi
    # Ensure the final installation directory exists.
    mkdir -p "$prefix"

    # 2. Unpack the source code. This changes the current directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 3. Configure Nginx with a comprehensive set of modules and paths.
    #    All paths are based on the final SERVBAY_BASE_FULL_PATH, not the temporary prefix.
    log_message "INFO" "Configuring Nginx v${version}..."
    ./configure \
        --prefix="${prefix}" \
        --conf-path="${SERVBAY_ETC_PATH}/nginx/nginx.conf" \
        --pid-path="${SERVBAY_TMP_PATH}/nginx.pid" \
        --lock-path="${SERVBAY_TMP_PATH}/nginx.lock" \
        --http-client-body-temp-path="${SERVBAY_TMP_PATH}/nginx/client_body_temp" \
        --http-proxy-temp-path="${SERVBAY_TMP_PATH}/nginx/proxy_temp" \
        --http-fastcgi-temp-path="${SERVBAY_TMP_PATH}/nginx/fastcgi_temp" \
        --http-uwsgi-temp-path="${SERVBAY_TMP_PATH}/nginx/uwsgi_temp" \
        --http-scgi-temp-path="${SERVBAY_TMP_PATH}/nginx/scgi_temp" \
        --http-log-path="${SERVBAY_LOG_PATH}/nginx/access.log" \
        --error-log-path="${SERVBAY_LOG_PATH}/nginx/error.log" \
        --with-compat \
        --with-http_addition_module \
        --with-http_auth_request_module \
        --with-http_dav_module \
        --with-http_degradation_module \
        --with-http_flv_module \
        --with-http_gunzip_module \
        --with-http_gzip_static_module \
        --with-http_mp4_module \
        --with-http_random_index_module \
        --with-http_realip_module \
        --with-http_secure_link_module \
        --with-http_slice_module \
        --with-http_ssl_module \
        --with-http_stub_status_module \
        --with-http_sub_module \
        --with-http_v2_module \
        --with-http_v3_module \
        --with-mail=dynamic \
        --with-mail_ssl_module \
        --with-pcre --with-pcre-jit \
        --with-stream=dynamic \
        --with-stream_realip_module \
        --with-stream_ssl_module \
        --with-stream_ssl_preread_module \
        --with-threads \
        --build="ServBay" \
        --with-cc-opt="-I${SERVBAY_COMMON_INCLUDE_PATH} -I${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/include -mmacosx-version-min=${BUILD_OS_MIN_VERSION}" \
        --with-ld-opt="-L${SERVBAY_COMMON_LIB_PATH} -L${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/lib -mmacosx-version-min=${BUILD_OS_MIN_VERSION}"

    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Nginx configure failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 4. Compile and install
    log_message "INFO" "Compiling Nginx (make -j${CPU_NUMBER})..."
    make -j${CPU_NUMBER}
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Nginx make failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    log_message "INFO" "Installing Nginx..."
    make install
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Nginx make install failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 5. Post-installation setup: Create directories and copy configuration files.
    #    These paths should point to the final destination, regardless of installDir.
    log_message "INFO" "Setting up Nginx directories and configuration..."
    mkdir -p "${SERVBAY_ETC_PATH}/nginx/vhosts"
    mkdir -p "${SERVBAY_ETC_PATH}/nginx/php-fpm-conf.d"
    mkdir -p "${SERVBAY_TMP_PATH}/nginx"
    mkdir -p "${SERVBAY_LOG_PATH}/nginx"

    # Copy our custom configuration files, overwriting the defaults.
    if [[ -d "${SERVBAY_BUILD_DIR}/conf/nginx" ]]; then
        cp "${SERVBAY_BUILD_DIR}/conf/nginx/nginx.conf" "${SERVBAY_ETC_PATH}/nginx/"
        cp "${SERVBAY_BUILD_DIR}/conf/nginx/pathinfo.conf" "${SERVBAY_ETC_PATH}/nginx/"
        # Use rsync or cp -R for directories
        cp -R "${SERVBAY_BUILD_DIR}/conf/nginx/php-fpm-conf.d/." "${SERVBAY_ETC_PATH}/nginx/php-fpm-conf.d/"
    else
        log_message "WARN" "Default Nginx configuration directory not found at '${SERVBAY_BUILD_DIR}/conf/nginx'."
    fi

    # Return to the original build directory before finishing
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Nginx build process completed successfully."
}

###
# @description Build logic for the Apache HTTPD server.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_httpd() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="httpd-${version}"

    # 1. Determine installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/apache"
    fi
    mkdir -p "$prefix"

    # 2. Unpack the source code. This changes the current directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # base 的 devlib 只发布 APR 的 bin/-config 脚本，不带 share/apr-1/build/
    # （httpd 的 make 会按 apr-config 报告的路径直接调用其中的 libtool，
    # 两架构 run 39965591690 实测 "No such file or directory"）。用 brew 的
    # glibtool（GNU libtool，语法与 APR 生成的 libtool 兼容）在原路径顶上；
    # 编译器由 httpd 的 make 显式传递，base 烘焙 wrapper 已在环境初始化时重建。
    local _apr_libtool="${SERVBAY_COMMON_PATH}/share/apr-1/build/libtool"
    if [[ ! -x "$_apr_libtool" ]]; then
        local _glibtool
        _glibtool=$(command -v glibtool) || {
            log_message "ERROR" "glibtool is required to backfill the APR build libtool."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        }
        mkdir -p "${_apr_libtool:h}" || { cd "$SERVBAY_BUILD_DIR"; return 1; }
        # httpd 的构建以 "--silent --mode=compile <cc>" 调用且不带 --tag；GNU
        # libtool 无法从自定义 wrapper 推断 tagged configuration，必须显式给
        # --tag=CC（httpd/APR/APR-util 全为 C，CC 标签对 compile/link 都正确）。
        printf '#!/bin/sh\nexec %s --tag=CC "$@"\n' "$_glibtool" > "$_apr_libtool" || { cd "$SERVBAY_BUILD_DIR"; return 1; }
        chmod +x "$_apr_libtool" || { cd "$SERVBAY_BUILD_DIR"; return 1; }
        log_message "INFO" "Backfilled the missing APR build libtool with ${_glibtool}."
    fi

    # 4. Configure Apache HTTPD.
    log_message "INFO" "Configuring Apache HTTPD v${version}..."
    # Using backslashes for readability. All paths are based on the final ServBay structure.
    ./configure \
        --prefix="${prefix}" \
        --enable-layout=Slackware-FHS \
        --sysconfdir="${SERVBAY_ETC_PATH}/apache" \
        --datarootdir="${SERVBAY_COMMON_SHARE_PATH}" \
        --datadir="${SERVBAY_BASE_FULL_PATH}/www/apache-default" \
        --localstatedir="${SERVBAY_VAR_PATH}" \
        --runstatedir="${SERVBAY_TMP_PATH}" \
        --enable-mpms-shared=all \
        --enable-mods-shared=all \
        --enable-authnz-fcgi \
        --enable-cgi \
        --enable-pie \
        --with-apr="${SERVBAY_COMMON_PATH}" \
        --with-apr-util="${SERVBAY_COMMON_PATH}" \
        --with-brotli="${SERVBAY_COMMON_PATH}" \
        --with-libxml2="${SERVBAY_COMMON_PATH}" \
        --with-mpm=prefork \
        --with-nghttp2="${SERVBAY_COMMON_PATH}" \
        --with-ssl="${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" \
        --with-pcre="${SERVBAY_COMMON_PATH}/bin/pcre2-config" \
        --with-z="${SERVBAY_COMMON_PATH}" \
        --disable-lua \
        --disable-luajit \
        --enable-deflate \
        --enable-cache \
        --enable-include \
        --enable-request \
        --enable-remoteip \
        --enable-proxy \
        --enable-proxy-connect \
        --enable-proxy-ftp \
        --enable-proxy-http \
        --enable-proxy-fcgi \
        --enable-proxy-scgi \
        --enable-proxy-uwsgi \
        --enable-proxy-fdpass \
        --enable-proxy-wstunnel \
        --enable-proxy-balancer \
        --enable-proxy-hcheck \
        --enable-session \
        --enable-ssl \
        --enable-http2 \
        --enable-proxy-http2 \
        --enable-dav \
        --enable-vhost-alias \
        --enable-rewrite \
        --enable-headers

    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Apache HTTPD configure failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 5. Compile and Install
    log_message "INFO" "Compiling Apache HTTPD (make -j${CPU_NUMBER})..."
    make -j${CPU_NUMBER}
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Apache HTTPD make failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    log_message "INFO" "Installing Apache HTTPD..."
    make install
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Apache HTTPD make install failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 6. Post-installation setup (only if not using a temporary installDir)
    if [[ -z "$installDir" ]]; then
        log_message "INFO" "Performing post-installation setup..."
        # Create directories
        mkdir -p "${SERVBAY_TMP_PATH}/httpd"
        mkdir -p "${SERVBAY_LOG_PATH}/apache"
        mkdir -p "${SERVBAY_ETC_PATH}/apache/vhosts"
        
        # Modify configuration files
        local httpd_conf_path="${SERVBAY_ETC_PATH}/apache/httpd.conf"
        sed -i '' 's/#LoadModule session_module/LoadModule session_module/' "$httpd_conf_path"
        sed -i '' 's/you@example.com/info@servbay.dev/' "$httpd_conf_path"
        sed -i '' "s|apache-default/htdocs|servbay|g" "$httpd_conf_path" # Assuming servbay is the new root

    fi

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Apache HTTPD build process completed successfully."
}

###
# @description Build logic for the Dnsmasq server.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_dnsmasq() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="dnsmasq-${version}"

    # 1. Determine the installation prefix. For Dnsmasq, this seems to be the common path.
    # The --install-dir override will apply to the 'PREFIX' in 'make install'.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/dnsmasq"
    fi
    mkdir -p "$prefix"

    # Unpack the source code. This changes the current directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 2. Pre-compilation source code patching.
    # This is a critical step to embed the correct ServBay paths into the binary.
    log_message "INFO" "Patching Dnsmasq source files with ServBay paths..."
    local files_to_modify=(
        "dnsmasq.conf.example"
        "src/config.h"
        "man/dnsmasq.8"
        "man/es/dnsmasq.8"
        "man/fr/dnsmasq.8"
    )

    for file in "${files_to_modify[@]}"; do
        # Use a temporary file for sed on systems that require it
        sed -i '' "s|/var/lib/misc/dnsmasq.leases|${SERVBAY_TMP_PATH}/dnsmasq.leases|g" "$file"
        sed -i '' "s|/etc/dnsmasq.conf|${SERVBAY_ETC_PATH}/dnsmasq/dnsmasq.conf|g" "$file"
        sed -i '' "s|/var/run/dnsmasq.pid|${SERVBAY_TMP_PATH}/dnsmasq.pid|g" "$file"
        sed -i '' "s|/etc/dnsmasq.d|${SERVBAY_ETC_PATH}/dnsmasq|g" "$file"
        sed -i '' "s|/etc/ppp/resolv.conf|${SERVBAY_ETC_PATH}/dnsmasq/ppp/resolv.conf|g" "$file"
        sed -i '' "s|/etc/dhcpc/resolv.conf|${SERVBAY_ETC_PATH}/dnsmasq/dhcpc/resolv.conf|g" "$file"
        sed -i '' "s|/usr/sbin/dnsmasq|${prefix}/sbin/dnsmasq|g" "$file"
    done

    # 3. Patch Makefile to include our custom CFLAGS and LDFLAGS.
    log_message "INFO" "Patching Makefile for compilation..."
    sed -i '' "s|^CFLAGS.*|CFLAGS        = ${CFLAGS} -D__APPLE_USE_RFC_3542|" Makefile
    sed -i '' "s|^LDFLAGS.*|LDFLAGS       = ${LDFLAGS}|" Makefile

    # 4. Compile and Install
    log_message "INFO" "Compiling and installing Dnsmasq..."
    # Note: BINDIR and MANDIR are passed to 'make install' to override defaults.
    make install PREFIX="${prefix}" BINDIR="${prefix}/sbin" MANDIR="${prefix}/share/man"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Dnsmasq make install failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 5. Post-installation setup
    log_message "INFO" "Creating Dnsmasq configuration..."
    mkdir -p "${SERVBAY_ETC_PATH}/dnsmasq/ppp"
    mkdir -p "${SERVBAY_ETC_PATH}/dnsmasq/dhcpc"
    cp dnsmasq.conf.example "${SERVBAY_ETC_PATH}/dnsmasq/dnsmasq.conf"
    
    # Add custom configurations
    echo "conf-file=${SERVBAY_ETC_PATH}/dnsmasq/domains.conf" >> "${SERVBAY_ETC_PATH}/dnsmasq/dnsmasq.conf"
    echo "address=/servbay.host/127.0.0.1" > "${SERVBAY_ETC_PATH}/dnsmasq/domains.conf"

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Dnsmasq build process completed successfully."
}

###
# @description Build logic for the Subversion (SVN) client.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_subversion() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="subversion-${version}"

    # 1. Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/subversion"
    fi
    mkdir -p "$prefix"

    # Unpack the source code. This changes the current directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 2. Configure Subversion with its dependencies.
    log_message "INFO" "Configuring Subversion v${version}..."
    ./configure \
        --prefix="${prefix}" \
        --disable-debug \
        --enable-optimize \
        --disable-mod-activation \
        --disable-plaintext-password-storage \
        --with-apr="${SERVBAY_COMMON_PATH}" \
        --with-apr-util="${SERVBAY_COMMON_PATH}" \
        --with-apxs=no \
        --with-sqlite="${SERVBAY_COMMON_PATH}" \
        --with-zlib="${SERVBAY_COMMON_PATH}" \
        --without-apache-libexecdir \
        --without-berkeley-db \
        --without-gpg-agent \
        --without-jikes \
        --with-serf="${SERVBAY_COMMON_PATH}" \
        --without-swig-python \
        --without-swig-ruby \
        --without-swig-perl \
        --without-jdk \
        --with-utf8proc=internal

    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Subversion configure failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # 3. Compile and Install
    log_message "INFO" "Compiling Subversion (make -j${CPU_NUMBER})..."
    make -j${CPU_NUMBER}
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Subversion make failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    log_message "INFO" "Installing Subversion..."
    make install
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Subversion make install failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # Clean up development and documentation files
    log_message "INFO" "Cleaning up development and documentation files..."
    trash_path "${prefix}/include"
    rm -f "${prefix}/lib/"*.a
    rm -f "${prefix}/lib/"*.la
    trash_path "${prefix}/share/man"

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Subversion build process completed successfully."
}

###
# @description "Build" logic for the cloudflared binary.
# This involves downloading and extracting the pre-compiled binary.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_cloudflared() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}
    
    local bin_dir="${prefix}/cloudflared/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Extracting cloudflared binary..."
    # Directly extract the binary from the archive into the target directory
    tar -C "$bin_dir" -zxf "${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to extract cloudflared."
        return 1
    fi
    
    chmod +x "${bin_dir}/cloudflared"
    log_message "INFO" "cloudflared binary setup complete."
}

###
# @description "Build" logic for the frp binaries (frpc, frps).
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_frp() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="frp-${version}"
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}

    # Unpack to a temporary directory first
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/frp/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Copying frpc and frps binaries..."
    # We are currently inside the unpacked directory
    chmod +x frpc frps
    cp frpc frps "$bin_dir/"
    
    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "frp binary setup complete."
}

###
# @description "Build" logic for the ngrok binary.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_ngrok() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="ngrok-${version}"
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}

    # Unpack to a temporary directory first
    unpack_package "$packageFullName" "$sourceFilename" "" "false"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/ngrok/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Copying ngrok binary..."
    # We are currently inside the unpacked directory
    chmod +x ngrok
    cp ngrok "$bin_dir/"
    
    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "ngrok binary setup complete."
}

###
# @description "Build" logic for the pinggy binary.
# Note: Pinggy is downloaded as a raw binary, not an archive.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_pinggy() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}
    
    local bin_dir="${prefix}/pinggy/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Copying pinggy binary..."
    local source_binary_path="${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"
    
    # Optional: If you need to strip architectures with lipo
    # log_message "INFO" "Stripping unused architectures from pinggy binary..."
    # lipo -remove x86_64 "$source_binary_path" -output "${source_binary_path}_arm64"
    # chmod +x "${source_binary_path}_arm64"
    # cp "${source_binary_path}_arm64" "${bin_dir}/pinggy"

    # Simple copy as per the original script
    chmod +x "$source_binary_path"
    cp "$source_binary_path" "${bin_dir}/pinggy"

    log_message "INFO" "pinggy binary setup complete."
}

###
# @description "Build" logic for Mailpit.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_mailpit() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="mailpit-${version}"
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}

    # Unpack to a temporary directory first
    unpack_package "$packageFullName" "$sourceFilename" "" "false"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/mailpit/bin"
    mkdir -p "$bin_dir"
    mkdir -p "${SERVBAY_ETC_PATH}/mailpit"
    mkdir -p "${SERVBAY_DB_PATH}/mailpit"
    mkdir -p "${SERVBAY_LOG_PATH}/mailpit"

    log_message "INFO" "Copying mailpit binary..."
    # We are currently inside the unpacked directory
    cp mailpit "$bin_dir/"
    
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Mailpit binary setup complete."
}

###
# @description "Build" logic for mongosh.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_mongosh() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="mongosh-${version}"

    # Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/mongosh"
    fi

    # Unpack to a temporary directory first
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Copying mongosh binaries..."
    # We are currently inside the unpacked directory
    cp bin/* "$bin_dir/"
    
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "mongosh binary setup complete."
}

###
# @description "Build" logic for MongoDB Database Tools.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_mongotools() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="mongotools-${version}"
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}

    # Unpack to a temporary directory first
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/mongotools/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Copying MongoDB tools binaries..."
    # We are currently inside the unpacked directory
    cp bin/* "$bin_dir/"
    
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "MongoDB tools binary setup complete."
}

###
# @description "Build" logic for the MongoDB server (mongod).
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_mongodb() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="mongodb-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')
    local prefix=${installDir:-$SERVBAY_PACKAGE_FULL_PATH}

    # Unpack to a temporary directory first
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/mongodb/${version_major}/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Copying mongod binary..."
    # We are currently inside the unpacked directory
    cp bin/mongod "$bin_dir/"
    
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "MongoDB server binary setup complete."
}

###
# @description 校验 Mach-O 产物的最低 macOS 版本（LC_BUILD_VERSION.minos）不高于期望基线。
#              Go/cgo 与 cmake 在未显式钉住 deployment target 时会跟随编译机 SDK，
#              产出的二进制会在旧系统上被 dyld 拒绝加载。
# @param $1 string 二进制文件路径。
# @param $2 string 期望的最低系统版本，如 12.0。
# @return 0 符合基线；1 超出基线或无法校验。
###
verify_macos_min_version() {
    local binary=$1
    local expected=$2

    if [[ ! -f "$binary" ]]; then
        log_message "ERROR" "verify_macos_min_version: binary not found: ${binary}"
        return 1
    fi

    local actual
    actual=$(otool -l "$binary" 2>/dev/null | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')

    if [[ -z "$actual" ]]; then
        log_message "ERROR" "verify_macos_min_version: no LC_BUILD_VERSION found in ${binary}"
        return 1
    fi

    # 用主版本号比较即可（基线均为整数主版本，如 12.0）。
    if (( ${actual%%.*} > ${expected%%.*} )); then
        log_message "ERROR" "Deployment target drift: ${binary} requires macOS ${actual}, expected <= ${expected}."
        return 1
    fi

    log_message "INFO" "Deployment target OK: ${binary} minos=${actual} (baseline ${expected})."
    return 0
}

###
# @description Build logic for the Ollama server.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_ollama() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="ollama-${version}"
    local prefix=${installDir:-${SERVBAY_PACKAGE_FULL_PATH}/ollama}

    # Ollama 的最低系统版本不能取全局 BUILD_OS_MIN_VERSION(12.0)：上游官方二进制
    # 自 0.6 起为 minos 13.0、自 0.17 起为 14.0（llama.cpp / MLX 的 Metal 后端所需）。
    # 下面两条构建路径共用该基线，出口处统一校验。
    local macos_min="14.0"

    # Unpack the source code. This changes the current directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 1. Apply patch to the source code.
    local patch_file="${PATCH_DIR}/ollama/ollama.patch"
    if [[ -f "$patch_file" ]]; then
        log_message "INFO" "Applying patch: ${patch_file}"
        # The original Make_Patch used -p0
        patch --ignore-whitespace -N -p0 < "$patch_file"
        if [[ $? -ne 0 ]]; then
            log_message "ERROR" "Failed to apply patch for Ollama."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi
    else
        log_message "WARN" "Ollama patch file not found, skipping: ${patch_file}"
    fi

    # Create the ServBay runtime directories shared by both build layouts.
    mkdir -p "${prefix}"
    mkdir -p "${SERVBAY_ETC_PATH}/ollama"
    mkdir -p "${SERVBAY_DB_PATH}/ollama/models"
    mkdir -p "${SERVBAY_LOG_PATH}/ollama"

    # Ollama 0.30 introduced a CMake superbuild and separate native helpers.
    # Detect the upstream layout instead of hard-coding a version boundary so
    # pre-releases and future releases follow their own complete build pipeline.
    if [[ -f "CMakeLists.txt" && -f "llama/server/CMakeLists.txt" ]]; then
        local ollama_arch
        local cmake_arch
        local mlx_cgo_cflags="-O3 -mmacosx-version-min=14.0"
        local mlx_cgo_cxxflags="-O3 -mmacosx-version-min=14.0"
        local mlx_cgo_ldflags
        local mlx_extra_args=()
        case "$architecture" in
            x86_64)
                ollama_arch="amd64"
                cmake_arch="x86_64"
                mlx_cgo_ldflags="-ldl -lc++ -framework Accelerate -mmacosx-version-min=14.0"
                mlx_extra_args=( "-DMLX_ENABLE_X64_MAC=ON" )
                ;;
            arm64)
                ollama_arch="arm64"
                cmake_arch="arm64"
                # Upstream also ships the newer v4 Metal backend for Apple
                # Silicon, but its tensor-op kernels need the paid
                # macos-*-xlarge runners to compile in sane time: adding it on
                # the free macos-26 runner blew past the 300 minute job
                # timeout, while metal_v3 alone finishes. Ship only metal_v3
                # until a bigger runner (or a prebuilt metallib cache) is
                # available.
                mlx_cgo_ldflags="-lc++ -framework Metal -framework Foundation -framework Accelerate -mmacosx-version-min=14.0"
                ;;
            *)
                log_message "ERROR" "Unsupported Ollama architecture: ${architecture}"
                cd "$SERVBAY_BUILD_DIR"
                return 1
                ;;
        esac

        local source_build_dir="build/darwin-sources"
        local build_dir="build/darwin-${ollama_arch}"
        local payload_dir="dist/darwin-${ollama_arch}"

        # dispatcher（build_package 主入口）为所有包导出的 CFLAGS/CXXFLAGS/LDFLAGS
        # 以全局 BUILD_OS_MIN_VERSION(12.00) 为基线，x86_64 还追加了
        # `-target x86_64-apple-macos12.00`。CMake 首次 configure 会用这些环境变量
        # 初始化 CMAKE_<LANG>_FLAGS 与链接器缓存，子构建（llama.cpp/MLX/mlx-c）的
        # 每条编译命令因此同时含有 -target(12.0) 和 CMake 平台段的
        # -mmacosx-version-min=14.0；clang 里 -target 优先于 -mmacosx-version-min，
        # 实际部署目标退回 12.0，x86_64 上 libc++ 浮点 std::to_chars（13.3+ 才可用）
        # 直接编译失败——arm64 的 BUILD_MACOS_TARGET 为空，所以只有 x86_64 爆。
        # 这里按 Ollama 自己的 14.0 基线重钉这三个变量；禁止引入 BUILD_MACOS_TARGET。
        export CFLAGS="-isysroot ${SDK_PATH} -Qunused-arguments -mmacosx-version-min=${macos_min} ${BUILD_CPU_ARCH}"
        export CXXFLAGS="${CFLAGS} -Wno-enum-constexpr-conversion"
        export LDFLAGS="-isysroot ${SDK_PATH} -mmacosx-version-min=${macos_min} ${BUILD_CPU_ARCH}"

        # Prepare the pinned native sources once, mirroring the upstream Darwin
        # superbuild with the backends we actually build below.
        log_message "INFO" "Preparing Ollama native sources..."
        if ! cmake -S . -B "$source_build_dir" \
            -DOLLAMA_MLX_BACKENDS=metal_v3 \
            -DOLLAMA_LLAMA_BACKENDS=; then
            log_message "ERROR" "Failed to configure Ollama native sources."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        if ! cmake --build "$source_build_dir" \
            --target ollama-llama-cpp-source \
            --target ollama-mlx-sources; then
            log_message "ERROR" "Failed to prepare Ollama native sources."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        local llama_cpp_shared_src="${PWD}/${source_build_dir}/_deps/llama_cpp-src"
        local mlx_shared_src="${PWD}/${source_build_dir}/_deps/mlx-src"
        local mlx_c_shared_src="${PWD}/${source_build_dir}/_deps/mlx-c-src"
        local cmake_args=(
            "-DCMAKE_BUILD_TYPE=Release"
            "-DCMAKE_OSX_ARCHITECTURES=${cmake_arch}"
            "-DCMAKE_OSX_DEPLOYMENT_TARGET=${macos_min}"
            "-DCMAKE_INSTALL_PREFIX=${payload_dir}/"
            "-DOLLAMA_PAYLOAD_INSTALL_PREFIX=${payload_dir}/"
            "-DOLLAMA_GO_OUTPUT=${payload_dir}/ollama"
            # Ollama 0.30 passes this value through Go's -ldflags parser. Keep
            # it whitespace-free so the linker receives one complete -X value.
            "-DOLLAMA_VERSION=${version}-ServBay"
            "-DOLLAMA_MLX_BACKENDS=metal_v3"
            "-DOLLAMA_LLAMA_BACKENDS="
            "-DFETCHCONTENT_SOURCE_DIR_LLAMA_CPP=${llama_cpp_shared_src}"
            "-DFETCHCONTENT_SOURCE_DIR_MLX=${mlx_shared_src}"
            "-DFETCHCONTENT_SOURCE_DIR_MLX-C=${mlx_c_shared_src}"
            "${mlx_extra_args[@]}"
        )

        log_message "INFO" "Configuring the Ollama Darwin payload for ${ollama_arch}..."
        if ! cmake -S . -B "$build_dir" "${cmake_args[@]}"; then
            log_message "ERROR" "Ollama Darwin payload configuration failed."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        log_message "INFO" "Building the complete Ollama Darwin payload for ${ollama_arch}..."
        if ! GOOS=darwin GOARCH="$ollama_arch" CGO_ENABLED=1 \
            CGO_CFLAGS="$mlx_cgo_cflags" \
            CGO_CXXFLAGS="$mlx_cgo_cxxflags" \
            CGO_LDFLAGS="$mlx_cgo_ldflags" \
            cmake --build "$build_dir" \
                --target ollama-local \
                --target ollama-mlx-backends \
                --parallel; then
            log_message "ERROR" "Ollama Darwin payload build failed."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        local required_payloads=(
            "${payload_dir}/ollama"
            "${payload_dir}/lib/ollama/llama-server"
            "${payload_dir}/lib/ollama/llama-quantize"
        )
        local payload
        for payload in "${required_payloads[@]}"; do
            if [[ ! -x "$payload" ]]; then
                log_message "ERROR" "Required Ollama payload is missing or not executable: ${payload}"
                cd "$SERVBAY_BUILD_DIR"
                return 1
            fi
            if ! lipo "$payload" -verify_arch "$cmake_arch"; then
                log_message "ERROR" "Ollama payload has the wrong architecture (${cmake_arch} required): ${payload}"
                cd "$SERVBAY_BUILD_DIR"
                return 1
            fi
        done

        # native 产物（llama-server/llama-quantize）同样校验 deployment target，
        # 防止子构建 flags 漂移（Go 二进制之外的部分此前无人把关）。
        if ! verify_macos_min_version "${payload_dir}/lib/ollama/llama-server" "${macos_min}"; then
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi
        if ! verify_macos_min_version "${payload_dir}/lib/ollama/llama-quantize" "${macos_min}"; then
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        if ! "${payload_dir}/lib/ollama/llama-server" --version >/dev/null; then
            log_message "ERROR" "Ollama llama-server failed to start; check its bundled runtime dependencies."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        log_message "INFO" "Copying the complete Ollama Darwin payload from ${payload_dir}..."
        if ! cp -a "${payload_dir}/." "${prefix}/"; then
            log_message "ERROR" "Failed to install the Ollama Darwin payload."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi
    else
        # Legacy releases generated native code and produced a single Go binary.
        log_message "INFO" "Using the legacy Ollama build pipeline..."
        log_message "INFO" "Installing/updating tscriptify tool..."
        if ! "$GO_BIN" install github.com/tkrajina/typescriptify-golang-structs/tscriptify@latest; then
            log_message "ERROR" "Failed to install tscriptify. Please check your Go environment."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        log_message "INFO" "Patching llama/llama.go for go generate..."
        sed -i '' "s|//go:generate make -j|//go:generate make -C ../ -j|" "llama/llama.go"

        # ServBay does not need Ollama's web UI in legacy packages.
        log_message "INFO" "Removing UI build from go generate (npm run build)..."
        sed -i '' 's|^//go:generate npm|// go:generate npm|' "app/ui/ui.go"

        # legacy 分支此前完全没有钉住 deployment target，Go/cgo 因此跟随编译机 SDK：
        # 线上 0.17.4 的产物实测 minos 26.0（官方同版本为 14.0），导致 macOS 26
        # 以外的用户启动 Ollama 即秒退。这里对齐上面 CMake 分支使用的 14.0 基线。
        # generate 阶段就会编译 llama.cpp 的 Metal 代码，必须在其之前生效。
        log_message "INFO" "Pinning macOS deployment target to ${macos_min} for the legacy Ollama build..."
        export MACOSX_DEPLOYMENT_TARGET="${macos_min}"
        export CMAKE_OSX_DEPLOYMENT_TARGET="${macos_min}"
        export CGO_CFLAGS="-mmacosx-version-min=${macos_min} ${CGO_CFLAGS}"
        export CGO_CXXFLAGS="-mmacosx-version-min=${macos_min} ${CGO_CXXFLAGS}"
        export CGO_LDFLAGS="-mmacosx-version-min=${macos_min} ${CGO_LDFLAGS}"

        log_message "INFO" "Running 'go generate' for Ollama..."
        if ! go generate ./...; then
            log_message "ERROR" "Ollama 'go generate' failed."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi

        log_message "INFO" "Building the legacy Ollama binary..."
        if ! go build -ldflags="-s -w -X 'github.com/ollama/ollama/version.Version=${version} (ServBay)' -X 'github.com/ollama/ollama/server.mode=release'" -o "${prefix}/ollama"; then
            log_message "ERROR" "Ollama 'go build' failed."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        fi
    fi

    # Gate: 两条构建路径共用的出口校验，防止 deployment target 再次跟随编译机漂移。
    if ! verify_macos_min_version "${prefix}/ollama" "${macos_min}"; then
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    mkdir -p "${prefix}/bin"
    mv "${prefix}/ollama" "${prefix}/bin/ollama" || return 1

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Ollama build process completed successfully."
}

###
# @description Build logic for the MinIO object storage server.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_minio() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="minio-${version}"

    # Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/minio"
    fi

    # Unpack the source code. This changes the current directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    local bin_dir="${prefix}/bin"
    mkdir -p "$bin_dir"

    # 钉住 deployment target：当 Go 走外部链接（cgo）时，产物的 minos 会跟随
    # 编译机 SDK —— 线上 minio 实测 14.0 即为此。纯内部链接的 Go 二进制 minos
    # 固定为 11.0、不受影响，多设这些变量也无副作用。
    local macos_min="${BUILD_OS_MIN_VERSION%%.*}.0"
    log_message "INFO" "Pinning macOS deployment target to ${macos_min} for MinIO..."
    export MACOSX_DEPLOYMENT_TARGET="${macos_min}"
    export CGO_CFLAGS="-mmacosx-version-min=${macos_min} ${CGO_CFLAGS}"
    export CGO_LDFLAGS="-mmacosx-version-min=${macos_min} ${CGO_LDFLAGS}"

    use_pinned_go_1_26 || return 1

    # Build the MinIO binary with version information.
    log_message "INFO" "Building MinIO binary..."
    go build -ldflags="-s -w -X github.com/minio/minio/cmd.Version=${version} -X github.com/minio/minio/cmd.ReleaseTag=${version} -X github.com/minio/minio/cmd.CommitID=SERVBAY-${version} -X github.com/minio/minio/cmd.CopyrightYear=$(date +%Y)" -o "${bin_dir}/minio"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "MinIO 'go build' failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "MinIO build process completed successfully."
}

###
# @description "Build" logic for the Typesense search engine.
# This involves downloading and extracting the pre-compiled binary.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_typesense() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3

    # Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/typesense"
    fi

    local bin_dir="${prefix}/bin"
    mkdir -p "$bin_dir"

    log_message "INFO" "Extracting Typesense binary..."
    # Directly extract the binary from the archive into the target directory
    tar -C "$bin_dir" -zxf "${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to extract Typesense."
        return 1
    fi
    
    # Remove unnecessary text files
    rm -f "${bin_dir}"/*.txt
    
    log_message "INFO" "Typesense binary setup complete."
}

###
# @description "Build" logic for the Meilisearch engine.
# This involves downloading a raw pre-compiled binary.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_meilisearch() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3

    # Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/meilisearch"
    fi

    local bin_dir="${prefix}/bin"
    mkdir -p "$bin_dir"

    # The source file is the binary itself, so we just copy it.
    local source_binary_path="${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"
    local target_binary_path="${bin_dir}/meilisearch"

    log_message "INFO" "Copying Meilisearch binary..."
    cp "$source_binary_path" "$target_binary_path"
    
    # Remove quarantine attribute and make executable
    xattr -d com.apple.quarantine "$target_binary_path"
    chmod +x "$target_binary_path"
    
    log_message "INFO" "Meilisearch binary setup complete."
}

###
# @description "Build" logic for PHP Composer.
# This involves downloading the .phar file and handling version-specific naming.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_composer() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3

    # Composer uses X.Y version series (e.g., 2, 2.2)
    # - 2 = latest Composer 2.x (supports PHP 7.2+)
    # - 2.2 = Composer 2.2 LTS (supports PHP 5.3-7.1)
    local version_major
    if [[ "$version" == 2.2.* ]]; then
        version_major="2.2"
    else
        version_major=$(echo "$version" | awk -F. '{print $1}')
    fi

    # Determine the installation prefix (multi-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/composer/${version_major}"
    fi

    local source_phar_path="${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"
    local bin_dir="${prefix}/bin"
    mkdir -p "$bin_dir"

    # The source file is the binary itself, so we just need to make it executable
    chmod +x "$source_phar_path"

    local target_path="${bin_dir}/composer"

    log_message "INFO" "Installing Composer ${version} to: ${target_path}"
    # Remove old file if it exists and copy the new one
    rm -f "$target_path"
    cp "$source_phar_path" "$target_path"

    # Write VERSION file
    echo "$version" > "${prefix}/VERSION"

    log_message "INFO" "Composer setup complete."
}

###
# @description "Build" logic for Adminer.
# This involves downloading the .php file and placing it in the web root.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_adminer() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3

    local web_root="${SERVBAY_BASE_FULL_PATH}/www/adminer"
    local target_path="${web_root}/index.php"
    local source_php_path="${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"

    log_message "INFO" "Installing Adminer to: ${target_path}"
    mkdir -p "$web_root"
    rm -f "$target_path"
    cp "$source_php_path" "$target_path"

    log_message "INFO" "Adminer setup complete."
}

###
# @description Build logic for the phpMyAdmin package.
# phpMyAdmin is distributed as a zip archive. This function extracts it to the web root.
# @param $1 string Pkg version, $2 string Src filename, $3 string Install dir (ignored).
###
build_phpmyadmin() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3

    local web_root="${SERVBAY_BASE_FULL_PATH}/www/phpmyadmin"

    log_message "INFO" "Installing phpMyAdmin to: ${web_root}"
    trash_path "$web_root" || return 1

    unpack_package "phpmyadmin-${version}" "$sourceFilename" "$web_root" || return 1

    echo "$version" > "${web_root}/VERSION"
    log_message "INFO" "phpMyAdmin setup complete."
}

###
# @description Build logic for the Git package.
# @param $1 string Pkg version, $2 string Src filename, $3 string Install dir.
###
build_git() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="git-${version}"

    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/git"
    fi

    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    log_message "INFO" "Configuring Git ${version}..."
    make configure
    ./configure \
        --prefix="${prefix}" \
        --with-openssl="${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" \
        --with-libpcre2 \
        --with-iconv="${SERVBAY_COMMON_PATH}" \
        CFLAGS="${CFLAGS}" \
        LDFLAGS="${LDFLAGS}"
    
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Git 'configure' failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    log_message "INFO" "Compiling Git ${version}..."
    make -j${CPU_NUMBER} NO_RUST=1 CFLAGS="${CFLAGS}" LDFLAGS="${LDFLAGS}"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Git 'make' failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    log_message "INFO" "Installing Git ${version}..."
    make install NO_RUST=1
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Git 'make install NO_RUST=1' failed."
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi

    # Clean up documentation
    log_message "INFO" "Cleaning up documentation..."
    trash_path "${prefix}/share/man"

    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Git build process completed successfully."
}

###
# @description "Build" logic for the Go programming language.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_go() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="go-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')

    # Determine the final installation path.
    local final_install_path
    if [[ -n "$installDir" ]]; then
        final_install_path="$installDir"
    else
        final_install_path="${SERVBAY_PACKAGE_FULL_PATH}/go/${version_major}"
    fi

    # Use the enhanced unpack_package to extract directly.
    unpack_package "$packageFullName" "$sourceFilename" "$final_install_path"
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    # Clean up unnecessary files
    log_message "INFO" "Cleaning up unnecessary files..."
    trash_path "${final_install_path}/test"
    trash_path "${final_install_path}/doc"

    log_message "INFO" "Go toolchain setup complete."
}

###
# @description "Build" logic for OpenJDK.
# This now uses the enhanced unpack_package function to extract directly.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_openjdk() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="openjdk-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1}')

    # 1. Determine the final installation path.
    local final_install_path
    if [[ -n "$installDir" ]]; then
        final_install_path="$installDir"
    else
        final_install_path="${SERVBAY_PACKAGE_FULL_PATH}/openjdk/${version_major}"
    fi
    
    # 2. Use the enhanced unpack_package to extract directly to the final destination.
    unpack_package "$packageFullName" "$sourceFilename" "$final_install_path"
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    # 3. Flatten macOS App bundle layout. Azul Zulu 的 macOS tarball 把 JDK 打成
    # .jdk App bundle：--strip-components=1 之后载荷位于 Contents/Home/ 下，
    # bin/java 不在包根，下游工具链全部找不到（v1 dfd05d4）。把 Contents/Home/*
    # 上提一层并丢弃壳目录。
    if [[ -d "${final_install_path}/Contents/Home" ]]; then
        log_message "INFO" "🧯 Flattening macOS App bundle layout (Contents/Home/*) for OpenJDK..."
        mv "${final_install_path}/Contents/Home"/*(DN) "${final_install_path}/"
        if [[ $? -ne 0 ]]; then
            log_message "ERROR" "Failed to flatten Contents/Home for ${packageFullName}"
            return 1
        fi
        rm -rf "${final_install_path}/Contents"
    fi

    # Clean up unnecessary files
    log_message "INFO" "Cleaning up unnecessary files..."
    rm -f "${final_install_path}/lib/src.zip"
    trash_path "${final_install_path}/demo"
    trash_path "${final_install_path}/sample"
    trash_path "${final_install_path}/man"

    log_message "INFO" "OpenJDK setup complete."
}

###
# @description "Build" logic for Apache Maven.
# This involves downloading, extracting the pre-compiled binaries,
# and cleaning up Windows-specific script files.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_maven() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="maven-${version}"

    # 1. Determine the final installation path (single-version package).
    local final_install_path
    if [[ -n "$installDir" ]]; then
        final_install_path="$installDir"
    else
        final_install_path="${SERVBAY_PACKAGE_FULL_PATH}/maven"
    fi
    
    # 2. Use the enhanced unpack_package to extract directly to the final destination.
    unpack_package "$packageFullName" "$sourceFilename" "$final_install_path"
    if [[ $? -ne 0 ]]; then
        return 1
    fi
    
    # 3. Post-extraction cleanup: Remove Windows batch files.
    log_message "INFO" "Cleaning up Windows-specific files for Maven..."
    rm -f "${final_install_path}/bin/"*.cmd
    
    log_message "INFO" "Maven setup complete."
}

###
# @description "Build" logic for the Rust programming language.
# This involves downloading a self-contained installer and running its install script.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_rust() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="rust-${version}"

    # 1. Determine the installation prefix (single-version package).
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/rust"
    fi
    
    # 2. Unpack to a temporary source directory (default unpack_package behavior).
    # This will also cd into the unpacked directory.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 3. Run the installer script with the correct prefix.
    log_message "INFO" "Running Rust installer script..."
    ./install.sh --prefix="${prefix}" --disable-ldconfig
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Rust install.sh script failed."
        cd "$SERVBAY_BUILD_DIR" # Ensure we cd back even on failure
        return 1
    fi

    # Clean up documentation
    log_message "INFO" "Cleaning up documentation..."
    trash_path "${prefix}/share/doc"
    trash_path "${prefix}/share/man"

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Rust installation complete."
}

###
# @description "Build" logic for the .NET SDK.
# This involves downloading and extracting the pre-compiled SDK.
# It uses a custom extraction method because the archive does not have a top-level directory.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_dotnetsdk() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    
    # 1. Get the major version using our special helper function.
    local version_major=$(_get_dotnetsdk_major_version "$version")

    # 2. Determine the final installation path.
    local final_install_path
    if [[ -n "$installDir" ]]; then
        final_install_path="$installDir"
    else
        final_install_path="${SERVBAY_PACKAGE_FULL_PATH}/dotnetsdk/${version_major}"
    fi
    
    # 3. Manually handle extraction because this archive has no top-level directory.
    # The generic unpack_package with --strip-components=1 would fail here.
    log_message "INFO" "Extracting .NET SDK directly to ${final_install_path}"
    trash_path "$final_install_path"
    mkdir -p "$final_install_path"
    
    tar -C "$final_install_path" -zxf "${SERVBAY_BUILD_SRC_DIR}/${sourceFilename}"
    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to extract .NET SDK."
        return 1
    fi
    
    log_message "INFO" ".NET SDK setup complete."
}

###
# @description Build logic for the Mono runtime and XSP web server.
# This is a complex multi-stage build.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_mono() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="mono-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')

    # 1. Determine the installation prefix for Mono.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/mono/${version_major}"
    fi
    mkdir -p "$prefix"

    # --- Stage 1: Build Mono Runtime ---
    log_message "INFO" "--- Stage 1: Building Mono Runtime v${version} ---"
    
    # Unpack Mono source and cd into it
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # Pre-compilation source code patching for paths
    log_message "INFO" "Patching Mono source files with ServBay paths..."
    local pkgshare="${prefix}/share/mono"
    local inreplace_files=(
        "external/corefx/src/System.Runtime.Extensions/src/System/Environment.Unix.cs"
        "mcs/class/corlib/System/Environment.cs"
        "mcs/class/corlib/System/Environment.iOS.cs"
        "man/mono-configuration-crypto.1" "man/mono.1" "man/mozroots.1"
    )
    for file in "${inreplace_files[@]}"; do
        if [[ -f "$file" ]]; then
            sed -i '' "s|/usr/share/|${pkgshare}/|g" "$file"
        else
            log_message "WARN" "File not found for patching, skipping: $file"
        fi
    done

    # 钉住 deployment target：configure 此前完全裸跑，未带任何编译参数，
    # 产物（含 AOT 生成的 *.dll.dylib）的 minos 因此跟随编译机 SDK，线上实测 14.0。
    # 同时写入 MACOSX_DEPLOYMENT_TARGET，覆盖 mono 构建过程中自行调用 clang 的环节。
    local macos_min="${BUILD_OS_MIN_VERSION%%.*}.0"
    log_message "INFO" "Pinning macOS deployment target to ${macos_min} for Mono..."
    export MACOSX_DEPLOYMENT_TARGET="${macos_min}"
    export CFLAGS="-mmacosx-version-min=${macos_min} ${CFLAGS}"
    export CXXFLAGS="-mmacosx-version-min=${macos_min} ${CXXFLAGS}"
    export LDFLAGS="-mmacosx-version-min=${macos_min} ${LDFLAGS}"

    # Configure Mono
    log_message "INFO" "Configuring Mono..."
    if [[ -f "autogen.sh" ]]; then
        ./autogen.sh --prefix="${prefix}" --disable-nls --disable-silent-rules
    else
        ./configure --prefix="${prefix}" --disable-nls --disable-silent-rules
    fi
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Mono configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # mono 6.14.0 在 Xcode 26 工具链下编不过 icall.c：
    #   error: use of undeclared identifier 'ICALL_SIG_TYPE__Bool';
    #          did you mean 'ICALL_SIG_TYPE_bool'?
    # C99 的 <stdbool.h> 有 `#define bool _Bool`，icall 的宏做 token paste 时便
    # 拼出了不存在的 ICALL_SIG_TYPE__Bool；C23 起 bool 是内建关键字、stdbool.h
    # 不再做该替换，拼接结果才正确。
    # mono 在 CFLAGS 之后硬加 -std=gnu99，把 CC 自带的 -std=gnu23 覆盖掉了
    # （编译命令中 gnu23 在位置 13、gnu99 在位置 87，clang 取最后一个），
    # 因此只能在 configure 生成 Makefile 之后改写，从 CFLAGS 侧追加无效。
    # 只改 icall.c 所在目录：全局改写会波及 minizip 等 K&R 风格的老代码，
    # 而 C23 移除了 K&R 函数定义，会报
    #   minizip/ioapi.c: unknown type name 'pzlib_filefunc_def'
    #   minizip/ioapi.c: expected identifier or '('
    # 需要 C23 的只有 icall 的 token paste，范围限定到那一个 Makefile 即可。
    local _icall_makefile
    _icall_makefile=$(find . -name icall.c -path "*/metadata/*" -print -quit 2>/dev/null)
    if [[ -n "$_icall_makefile" ]]; then
        _icall_makefile="$(dirname "$_icall_makefile")/Makefile"
    fi
    if [[ -f "$_icall_makefile" ]]; then
        log_message "INFO" "Rewriting -std=gnu99 to -std=gnu23 in ${_icall_makefile} (icall.c C23 requirement)..."
        sed -i '' 's/-std=gnu99/-std=gnu23/g' "$_icall_makefile"
    else
        log_message "WARN" "icall.c Makefile not found; skipping the C23 rewrite. The build may fail in icall.c."
    fi

    # Compile and install Mono
    log_message "INFO" "Compiling Mono (make -j${CPU_NUMBER})..."
    make -j${CPU_NUMBER}
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Mono make failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    log_message "INFO" "Installing Mono..."
    make install
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Mono make install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # Clean up documentation and static libraries
    log_message "INFO" "Cleaning up documentation and static libraries..."
    trash_path "${prefix}/share/man"
    trash_path "${prefix}/share/doc"
    rm -f "${prefix}/lib/"*.a
    rm -f "${prefix}/lib/"*.la

    cd "$SERVBAY_BUILD_DIR" # Return to base build dir before next stage

    # --- Stage 2: Build Mono XSP ---
    log_message "INFO" "--- Stage 2: Building Mono XSP ---"
    local xsp_version="4.7.1"
    local xsp_source_filename="mono-xsp-${xsp_version}.tar.gz"
    local xsp_package_fullname="mono-xsp-${xsp_version}"
    
    # Download XSP source
    if ! download_source "https://github.com/mono/xsp/archive/refs/tags/${xsp_version}.tar.gz" "${SERVBAY_BUILD_SRC_DIR}/${xsp_source_filename}"; then
        log_message "ERROR" "Failed to download Mono XSP."; return 1;
    fi

    # Unpack XSP source and cd into it
    unpack_package "$xsp_package_fullname" "$xsp_source_filename"
    if [[ $? -ne 0 ]]; then return 1; fi
    
    # Temporarily modify environment to find the newly installed Mono
    export PKG_CONFIG_PATH="${prefix}/lib/pkgconfig:${PKG_CONFIG_PATH}"
    export PATH="${prefix}/bin:${PATH}"
    
    # Configure, compile, and install XSP
    log_message "INFO" "Configuring Mono XSP..."
    ./autogen.sh
    ./configure --prefix="${prefix}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Mono XSP configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    log_message "INFO" "Compiling and installing Mono XSP..."
    make -j${CPU_NUMBER} && make install
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Mono XSP make/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Mono and XSP build process completed successfully."
}

###
# @description Build logic for the Ruby programming language.
# This is a complex build that includes a dependency (libyaml) and
# architecture-specific patching for older Ruby versions.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_ruby() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="ruby-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')

    # 1. Determine the installation prefix.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/ruby/${version_major}"
    fi
    mkdir -p "$prefix"

    # --- Stage 1: Build dependency: libyaml ---
    log_message "INFO" "--- Stage 1: Building dependency: libyaml ---"
    local libyaml_version="0.2.5"
    local libyaml_source_filename="libyaml-${libyaml_version}.tar.gz"
    local libyaml_package_fullname="libyaml-${libyaml_version}"
    
    # Download libyaml
    if ! download_source "https://github.com/yaml/libyaml/archive/refs/tags/${libyaml_version}.tar.gz" "${SERVBAY_BUILD_SRC_DIR}/${libyaml_source_filename}"; then
        log_message "ERROR" "Failed to download libyaml."; return 1;
    fi
    
    # Unpack libyaml and cd into it
    unpack_package "$libyaml_package_fullname" "$libyaml_source_filename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # Configure, compile, and install libyaml.
    # libyaml 0.2.5 的 configure.ac 使用过时的 AC_PROG_LIBTOOL 宏，autoconf >= 2.70
    # （arm64 runner）会直接拒绝（"undefined or overquoted macro"），./bootstrap 失败；
    # x86_64 runner 的旧 autoconf 仍接受。bootstrap 前 sed 成 LT_INIT，
    # 两种 runner 都能过（v1 e47f814）。
    sed -i '' 's/AC_PROG_LIBTOOL/LT_INIT/g' configure.ac
    ./bootstrap
    ./configure --prefix="/tmp/libyaml" \
                --libdir="${prefix}/lib" \
                --includedir="${prefix}/include" \
                --disable-dependency-tracking --disable-static
    if [[ $? -ne 0 ]]; then log_message "ERROR" "libyaml configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    
    make -j${CPU_NUMBER} && make install
    if [[ $? -ne 0 ]]; then log_message "ERROR" "libyaml make/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    
    cd "$SERVBAY_BUILD_DIR" # Return to base build dir

    # --- Stage 2: Build Ruby ---
    log_message "INFO" "--- Stage 2: Building Ruby v${version} ---"
    
    # Unpack Ruby source and cd into it
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # --- OpenSSL/Digest Extension Replacement (all architectures) ---
    # Old Ruby versions bundle an openssl extension that cannot compile against
    # OpenSSL 3.x (opaque structs like EVP_PKEY). Replace with newer versions
    # from the ruby/openssl and ruby/digest GitHub repos.
    if [[ "$version_major" =~ ^(2\.[3-6])$ ]]; then
        log_message "INFO" "Replacing ext/openssl (v3.0.3) and ext/digest (v3.0.0) for Ruby ${version_major}..."
        download_source "https://github.com/ruby/openssl/archive/refs/tags/v3.0.3.tar.gz" "openssl.tar.gz" && \
        download_source "https://github.com/ruby/digest/archive/refs/tags/v3.0.0.tar.gz" "digest.tar.gz" && \
        trash_path ext/openssl test/openssl lib/openssl && tar zxf openssl.tar.gz --strip-components=1 && \
        trash_path ext/digest test/digest && tar zxf digest.tar.gz --strip-components=1
    elif [[ "$version_major" =~ ^(2\.7|3\.0)$ ]]; then
        log_message "INFO" "Replacing ext/openssl (v3.3.0) and ext/digest (v3.2.0) for Ruby ${version_major}..."
        download_source "https://github.com/ruby/openssl/archive/refs/tags/v3.3.0.tar.gz" "openssl.tar.gz" && \
        download_source "https://github.com/ruby/digest/archive/refs/tags/v3.2.0.tar.gz" "digest.tar.gz" && \
        trash_path ext/openssl test/openssl lib/openssl && tar zxf openssl.tar.gz --strip-components=1 && \
        trash_path ext/digest test/digest && tar zxf digest.tar.gz --strip-components=1
    fi

    # --- Architecture-Specific Patching ---
    if [[ "$architecture" == "x86_64" ]]; then
        if [[ "$version_major" == "3.0" ]]; then
            local patch_file="${PATCH_DIR}/ruby/ruby-3.0-intel.patch"
            log_message "INFO" "Applying patch: ${patch_file}"
            patch -p1 < "$patch_file"
        fi
    fi

    # --- General Patching (all architectures) ---
    # Ruby <= 2.6's ext/fiddle/closure.c uses the deprecated ffi_prep_closure()
    # in its non-USE_FFI_CLOSURE_ALLOC fallback path. Modern libffi has removed
    # this function entirely. Replace with ffi_prep_closure_loc().
    if [[ "$version_major" =~ ^(2\.[3-6])$ ]] && [[ -f ext/fiddle/closure.c ]]; then
        log_message "INFO" "Patching ext/fiddle/closure.c: ffi_prep_closure -> ffi_prep_closure_loc"
        sed -i '' 's/ffi_prep_closure(pcl, cif, callback, (void \*)self)/ffi_prep_closure_loc(pcl, cif, callback, (void *)self, pcl)/' ext/fiddle/closure.c
    fi

    # Ruby 3.0's RBIMPL_ATTR_MAYBE_UNUSED() expands to [[maybe_unused]] on modern
    # Clang which detects C2x attribute support, but the ENTER macro in bigdecimal.c
    # places it in a position where the attribute cannot apply to types. Force the
    # fallback to __attribute__((unused)) which works in all positions.
    local maybe_unused_h="include/ruby/internal/attr/maybe_unused.h"
    if [[ "$version_major" == "3.0" ]] && [[ -f "$maybe_unused_h" ]]; then
        log_message "INFO" "Patching ${maybe_unused_h}: disable [[maybe_unused]] for C mode"
        sed -i '' 's/RBIMPL_HAS_C_ATTRIBUTE(maybe_unused)/0 \/* patched: force __attribute__((unused)) *\//' "$maybe_unused_h"
    fi

    # Ruby's configure validates LDFLAGS by linking a test program. The global
    # LDFLAGS contains -llzma/-lzstd/-lresolv which are library flags (belong in
    # LIBS, not LDFLAGS). If these libraries aren't yet available on the search
    # path, configure's link test fails. Strip all -l flags for the Ruby build;
    # Ruby discovers and links its own dependencies via configure probes.
    local saved_ldflags="$LDFLAGS"
    export LDFLAGS="$(echo "$LDFLAGS" | sed 's/ -l[a-zA-Z0-9_]*//g')"

    # Configure Ruby
    log_message "INFO" "Configuring Ruby..."
    # Add the just-built libyaml to PKG_CONFIG_PATH
    export PKG_CONFIG_PATH="${prefix}/lib/pkgconfig:${PKG_CONFIG_PATH}"
    ./configure --prefix="${prefix}" --enable-shared --disable-silent-rules --disable-dtrace --without-gmp
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Ruby configure failed."; export LDFLAGS="$saved_ldflags"; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # Compile and install Ruby
    log_message "INFO" "Compiling and installing Ruby..."
    make -j${CPU_NUMBER} && make install
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Ruby make/install failed."; export LDFLAGS="$saved_ldflags"; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # Restore global LDFLAGS for subsequent packages
    export LDFLAGS="$saved_ldflags"

    # Post-installation cleanup
    log_message "INFO" "Cleaning up temporary libyaml files..."
    rm -f "${prefix}/lib/libyaml.la"
    rm -f "${prefix}/include/yaml.h"
    rm -f "${prefix}/lib/pkgconfig/yaml-0.1.pc"

    # Clean up Ruby documentation index
    log_message "INFO" "Cleaning up Ruby documentation index..."
    trash_path "${prefix}/share/ri"
    trash_path "${prefix}/share/man"

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Ruby build process completed successfully."
}

###
# @description Build logic for the Python programming language.
# This is a highly complex, multi-version, multi-architecture build.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_python() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="python-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')
    integer major_version_num=$(echo "$version" | awk -F. '{print $1}')

    # 1. Determine the installation prefix.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/python/${version_major}"
    fi
    mkdir -p "$prefix"

    # 2. Unpack source and cd into it.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 3. Apply version and architecture specific patches.
    local patch_dir="${PATCH_DIR}/python/${version_major}"
    if [[ "$architecture" == "arm64" ]] && (( major_version_num == 3 )); then
        sed -i '' "s/libmpdec_machine=universal/libmpdec_machine=uint128/" "configure"
    fi
    # Python 3.14+ uses plain diff (p0); all others use git format (p1).
    local patch_level="p1"
    if [[ "${version_major}" == "3.14" ]]; then
        patch_level="p0"
    fi
    _apply_patches "$patch_dir" "$patch_level"

    # 3b. Fix include search order for Python's setup.py SQLite detection.
    # The global CPPFLAGS has SDK path first, causing setup.py to find the
    # system sqlite3.h and reject it for loadable extensions on macOS.
    # Put our custom include paths before the SDK path.
    export CPPFLAGS="-I${SERVBAY_STATIC_INCLUDE_PATH} -I${SERVBAY_COMMON_INCLUDE_PATH} -I${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/include -I${SDK_PATH}/usr/include"

    # 4. Generate and run the configure command.
    log_message "INFO" "Configuring Python v${version}..."
    declare -a CONFIGURE_ARGS
    _get_python_configure_args "$version" "$prefix"
    
    # For Python 3.8, disable tkinter
    if [[ "$version_major" == "3.8" ]]; then
        echo -n "\n*disabled*\n\n_tkinter" >> Modules/Setup
    fi
    
    PYTHONHOME="" PYTHONPATH="" MACOSX_DEPLOYMENT_TARGET=12.0 ./configure "${CONFIGURE_ARGS[@]}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Python configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # 5. Compile and install.
    log_message "INFO" "Compiling and installing Python..."
    CPATH=${SERVBAY_COMMON_INCLUDE_PATH} make -j${CPU_NUMBER}
    CPATH=${SERVBAY_COMMON_INCLUDE_PATH} make altinstall PYTHONAPPSDIR="${prefix}"
    CPATH=${SERVBAY_COMMON_INCLUDE_PATH} make frameworkinstallextras PYTHONAPPSDIR="${prefix}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "Python make/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # 6. Post-installation: pip, setuptools, symlinks, etc.
    log_message "INFO" "Performing post-installation setup..."
    local framework_bin_dir="${prefix}/Python.framework/Versions/${version_major}/bin"
    local capem_path="${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/cacert.pem"

    if (( major_version_num == 2 )); then
        ${framework_bin_dir}/python${version_major} -m ensurepip --default-pip
    fi
    
    ${framework_bin_dir}/python${version_major} -m pip install --upgrade pip
    ${framework_bin_dir}/pip${version_major} --cert "${capem_path}" install setuptools wheel flit-core

    # Create symlinks
    if (( major_version_num == 2 )); then
        _create_python_symlinks2 "$version_major" "$version"
    else
        _create_python_symlinks3 "$version_major" "$version"
    fi

    # Rename .app files
    mv "${prefix}/IDLE.app" "${prefix}/IDLE ${version_major}.app"
    mv "${prefix}/Python Launcher.app" "${prefix}/Python Launcher ${version_major}.app"

    # Clean up test suite and caches
    log_message "INFO" "Cleaning up test suite and caches..."
    local py_lib="${prefix}/Python.framework/Versions/${version_major}/lib/python${version_major}"
    trash_path "${py_lib}/test"
    trash_path "${py_lib}/*/test"
    trash_path "${py_lib}/*/tests"
    for cache_dir in "${prefix}"/**/__pycache__(N/); do trash_path "$cache_dir" || return 1; done
    find "${prefix}" -name "*.pyc" -delete 2>/dev/null || true
    find "${prefix}" -name "*.pyo" -delete 2>/dev/null || true

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "Python build process completed successfully."
}

###
# @description Build logic for Node.js.
# This is a complex build with version and architecture specific logic.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_node() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="node-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1}')

    # 1. Determine the final installation path.
    local final_install_path
    if [[ -n "$installDir" ]]; then
        final_install_path="$installDir"
    else
        final_install_path="${SERVBAY_PACKAGE_FULL_PATH}/node/${version_major}"
    fi

    # 2. Node.js downloads are pre-compiled binaries, use unpack_package to extract them
    # The tar.gz contains a directory like node-v{version}-darwin-{arch}/ with bin/, lib/, include/, share/
    log_message "INFO" "Extracting pre-compiled Node.js v${version} binary..."
    unpack_package "$packageFullName" "$sourceFilename" "$final_install_path"
    if [[ $? -ne 0 ]]; then
        return 1
    fi

    # Clean up documentation files
    log_message "INFO" "Cleaning up documentation files..."
    rm -f "${final_install_path}/CHANGELOG.md"
    rm -f "${final_install_path}/README.md"
    trash_path "${final_install_path}/man"

    log_message "INFO" "Node.js setup complete."
}

###
# @description "Build" logic for the Deno runtime.
# This involves downloading and extracting the pre-compiled binary.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_deno() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="deno-${version}"

    # 1. Determine the final installation path (single-version package).
    local final_install_path
    if [[ -n "$installDir" ]]; then
        final_install_path="$installDir"
    else
        final_install_path="${SERVBAY_PACKAGE_FULL_PATH}/deno"
    fi
    
    # 2. Use the enhanced unpack_package to extract directly to the final destination.
    # The zip file contains a single 'deno' binary.
    unpack_package "$packageFullName" "$sourceFilename" "$final_install_path"
    if [[ $? -ne 0 ]]; then
        return 1
    fi
    
    # 3. Move binary into bin/ subdirectory and ensure it is executable.
    mkdir -p "${final_install_path}/bin"
    mv "${final_install_path}/deno" "${final_install_path}/bin/deno"
    chmod +x "${final_install_path}/bin/deno"

    log_message "INFO" "Deno setup complete."
}

###
# @description "Build" logic for the Bun runtime.
# This involves downloading and extracting the pre-compiled binary.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_bun() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="bun-${version}"

    # 1. Determine the final installation path (single-version package).
    local final_install_path
    if [[ -n "$installDir" ]]; then
        final_install_path="$installDir"
    else
        final_install_path="${SERVBAY_PACKAGE_FULL_PATH}/bun"
    fi
    
    # 2. Use the enhanced unpack_package to extract directly to the final destination.
    # The zip file contains a single 'bun' binary inside a versioned folder.
    unpack_package "$packageFullName" "$sourceFilename" "$final_install_path"
    if [[ $? -ne 0 ]]; then
        return 1
    fi
    
    # 3. Move binary into bin/ subdirectory and ensure it is executable.
    mkdir -p "${final_install_path}/bin"
    mv "${final_install_path}/bun" "${final_install_path}/bin/bun"
    chmod +x "${final_install_path}/bin/bun"

    log_message "INFO" "Bun setup complete."
}

###
# @description Build logic for MySQL Server.
# This is an extremely complex build with many version and architecture branches.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_mysql() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="mysql-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')
    local ver_major=${version%%.*}

    # 1. Determine the installation prefix.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/mysql/${version_major}"
    fi
    mkdir -p "$prefix"

    # 2. Unpack source and cd into it.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 3. Prepare environment and apply patches based on version/arch.
    log_message "INFO" "Preparing build environment for MySQL v${version}..."
    unset CPPFLAGS
    # 只前置 ServBay OpenSSL include：MySQL Router 的 harness_tls 自己跑
    # find_package(OpenSSL)（OPENSSL_ROOT_DIR 只影响顶层那一次），前置 -I 让每次
    # 编译（含 harness）都优先命中 base 的 OpenSSL 头（v1 16fab29）。
    # 严禁再带 common/include：base 在 common/include/unicode 带的旧 ICU 会压过
    # WITH_ICU=bundled 的 ICU 77，gen_keyword_list 按旧头编译、链新库，两架构皆
    # 报 "Undefined symbols: icu_74::UnicodeString..."（v1 dee252e 教训）。
    local _servbay_inc="-I${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/include"
    export CFLAGS="${_servbay_inc} -Qunused-arguments -O3 -DNDEBUG -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET}"
    export CXXFLAGS="${_servbay_inc} -Qunused-arguments -O3 -DNDEBUG -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET} -Wno-enum-constexpr-conversion -Wno-missing-template-arg-list-after-template-kw"

    # Legacy MySQL (< 8.0): suppress implicit-function-declaration and int-conversion
    # errors promoted to errors in modern Clang.
    if (( ver_major == 5 )); then
        export CFLAGS="${CFLAGS} -Wno-implicit-function-declaration -Wno-int-conversion"
        export CXXFLAGS="${CXXFLAGS} -Wno-implicit-function-declaration -Wno-int-conversion"
    fi

    # Pre-configure steps (patches, boost, etc.)
    case "$version_major" in
        "5.1")
            export CXXFLAGS="${CXXFLAGS} -std=c++98"
            export LDFLAGS="${LDFLAGS} -lstdc++"
            export LC_ALL=C  # macOS sed fails on non-UTF8 bytes without this
            _apply_single_patch "${PATCH_DIR}/mysql/5.1/make.patch" "p0"
            if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
            ;;
        "5.5"|"5.6")
            mv VERSION MYSQL_VERSION
            _apply_single_patch "${PATCH_DIR}/mysql/${version_major}/cmake.patch" "p0"
            if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
            ;;
        "5.7")
            download_source "https://archives.boost.io/release/1.59.0/source/boost_1_59_0.tar.gz" "../boost_1_59_0.tar.gz" && tar zxf ../boost_1_59_0.tar.gz
            _apply_single_patch "${PATCH_DIR}/mysql/5.7/zlib.patch" "p0"
            if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
            ;;
        "8.0"|"8.2")
            download_source "https://archives.boost.io/release/1.77.0/source/boost_1_77_0.tar.gz" "../boost_1_77_0.tar.gz" && tar zxf ../boost_1_77_0.tar.gz
            ;;
        "8.1")
            download_source "https://archives.boost.io/release/1.77.0/source/boost_1_77_0.tar.gz" "../boost_1_77_0.tar.gz" && tar zxf ../boost_1_77_0.tar.gz
            _apply_single_patch "${PATCH_DIR}/mysql/8.1/auth.patch" "p0"
            if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
            ;;
        "8.4"|"9.0"|"9.1"|"9.2"|"9.3"|"9.4")
            if [[ "$architecture" == "x86_64" ]]; then
                # 9.1+ 复用 9.0 的补丁，8.4 使用自己的补丁
                local mysql_std_patch_ver="${version_major}"
                [[ "$version_major" =~ ^9\.[1-9] ]] && mysql_std_patch_ver="9.0"
                _apply_single_patch "${PATCH_DIR}/mysql/${mysql_std_patch_ver}/x86_64-std.patch" "p0"
                if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
            fi
            ;;
    esac

    # Fix bundled zlib on macOS 15+: TARGET_OS_MAC is always defined on modern macOS,
    # causing zutil.h to enter the classic Mac OS path and #define fdopen to NULL,
    # which breaks system headers. Fixed upstream in zlib 1.3.
    # Path varies by MySQL version: zlib/zutil.h (5.1-5.6), extra/zlib/zlib-1.2.13/zutil.h (5.7+).
    local zutil_h=""
    for candidate in "zlib/zutil.h" "extra/zlib/zlib-1.2.13/zutil.h"; do
        if [[ -f "$candidate" ]] && grep -q 'defined(TARGET_OS_MAC)' "$candidate"; then
            zutil_h="$candidate"
            break
        fi
    done
    if [[ -n "$zutil_h" ]]; then
        log_message "INFO" "Patching bundled zlib zutil.h for macOS 15+ compatibility..."
        sed -i '' 's/defined(MACOS) || defined(TARGET_OS_MAC)/defined(MACOS)/' "$zutil_h"
    fi

    # 4. Configure and Compile.
    if [[ "$version_major" == "5.1" ]]; then
        # --- Handle ancient ./configure based build ---
        log_message "INFO" "Configuring with ./configure for MySQL 5.1..."
        ./configure \
            --prefix="${prefix}" \
            --with-unix-socket-path="${SERVBAY_TMP_PATH}/mysql-${version_major}.sock" \
            --sysconfdir="${SERVBAY_ETC_PATH}/mysql/${version_major}" \
            --with-extra-charsets=all --with-zlib-dir=bundled --with-pthread \
            --with-charset=utf8 --with-collation=utf8_general_ci --enable-local-infile \
            --with-ssl=bundled --without-embedded-server --with-comment='ServBay.dev' --without-debug
        if [[ $? -ne 0 ]]; then log_message "ERROR" "MySQL 5.1 configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
        
        log_message "INFO" "Compiling and installing MySQL 5.1..."
        make -j${CPU_NUMBER} && make install
        if [[ $? -ne 0 ]]; then log_message "ERROR" "MySQL 5.1 make/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    else
        # --- Handle modern CMake based builds ---
        # MySQL 5.x requires cmake 3.x (cmake 4.x removed OLD policy behaviors).
        local cmake_cmd="cmake"
        if (( ver_major == 5 )); then
            cmake_cmd=$(_ensure_cmake3)
            if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
        fi

        declare -a CMAKE_ARGS
        _get_mysql_cmake_args "$version" "$prefix"
        log_message "INFO" "Configuring with CMake for MySQL ${version_major}..."
        "$cmake_cmd" -B builddir "${CMAKE_ARGS[@]}"
        if [[ $? -ne 0 ]]; then log_message "ERROR" "MySQL CMake configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

        log_message "INFO" "Compiling and installing MySQL ${version_major}..."
        "$cmake_cmd" --build builddir -j ${CPU_NUMBER} && "$cmake_cmd" --install builddir
        if [[ $? -ne 0 ]]; then log_message "ERROR" "MySQL CMake build/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    fi

    # 5. Post-installation cleanup.
    log_message "INFO" "Cleaning up unnecessary files..."
    trash_path "${prefix}/mysql-test"
    rm -f "${prefix}/lib/"*.a
    trash_path "${prefix}/docs"
    trash_path "${prefix}/man"

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "MySQL build process completed successfully."
}

###
# @description Build logic for MariaDB Server.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_mariadb() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="mariadb-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')

    # 1. Determine the installation prefix.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/mariadb/${version_major}"
    fi
    mkdir -p "$prefix"

    # 2. Unpack source and cd into it.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 3. Isolate build environment (same as build_mysql).
    #    build_package() sets CFLAGS/CXXFLAGS with -isysroot and CPPFLAGS with SDK includes,
    #    which can conflict with MariaDB's own CMake configuration.
    unset CPPFLAGS
    export CFLAGS="-Qunused-arguments -O3 -DNDEBUG -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET}"
    # -D_LIBCPP_ABI_NO_COMPRESSED_PAIR_PADDING: work around LLVM 20.x libc++ regression
    # (llvm/llvm-project#142118) where __compressed_pair_padding applies sizeof to
    # a function type, triggered by InnoDB's <ostream> include in older MariaDB.
    export CXXFLAGS="-Qunused-arguments -O3 -DNDEBUG -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET} -Wno-enum-constexpr-conversion -D_LIBCPP_ABI_NO_COMPRESSED_PAIR_PADDING"

    # 4. Apply version-specific patches using the new, precise helper.
    local patch_versions_no_o_direct=("11.4.0" "11.4.1" "11.5.0" "11.3.2" "11.2.3" "11.1.4" "11.0.5" "10.11.7")
    if [[ " ${patch_versions_no_o_direct[@]} " =~ " ${version} " ]]; then
        _apply_single_patch "${PATCH_DIR}/mariadb/os_data_file_no_o_direct.patch" "p1"
        if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
    fi

    local patch_versions_innodb_crash=("10.11.8" "11.0.6" "11.1.5" "11.2.4" "11.4.2" "11.5.1" "11.6.0")
    if [[ " ${patch_versions_innodb_crash[@]} " =~ " ${version} " ]]; then
        _apply_single_patch "${PATCH_DIR}/mariadb/innodb_crash.patch" "p1"
        if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
        _apply_single_patch "${PATCH_DIR}/mariadb/mariadb-install-db_hang.patch" "p1"
        if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
    fi

    # groonga 的 cmake 在 x86_64 上误检 <netdb.h>（所有 feature 探测失败），留下不完整
    # 的 struct addrinfo，mroonga 编不过。对全部 x86_64 构建强制 include netdb.h：
    # 旧的按版本白名单每次 MariaDB 发新版都会过期（如 11.8.8、11.4.12 就漏了）。
    # 非致命：patch -N 幂等跳过已打过的树，vendored groonga 上下文不同导致失配时
    # 只告警不阻断构建（v1 474c6f2）。
    if [[ "$architecture" == "x86_64" ]]; then
        _apply_single_patch "${PATCH_DIR}/mariadb/fix-groonga-netdb-11.4-x86_64.patch" "p0" \
            || log_message "WARN" "groonga-netdb x86_64 patch not applied for ${version} (already patched or vendored groonga differs)"
    fi

    # Fix libxml2 2.13+ const compatibility: xmlGetLastError() now returns
    # const xmlError* but older MariaDB CONNECT engine uses non-const xmlErrorPtr.
    # Upstream fix: xmlErrorPtr → const xmlError *, xmlResetError(ptr) → xmlResetLastError().
    if [[ -f storage/connect/libdoc.cpp ]] && grep -q 'xmlErrorPtr' storage/connect/libdoc.cpp; then
        log_message "INFO" "Patching CONNECT engine for libxml2 2.13+ const compatibility..."
        sed -i '' -e 's/xmlErrorPtr/const xmlError */g' \
                  -e 's/xmlResetError([^)]*)/xmlResetLastError()/g' \
                  storage/connect/libdoc.cpp
    fi

    # 5. Configure with CMake.
    log_message "INFO" "Configuring MariaDB v${version} with CMake..."
    local cmake_args=(
        "-DCMAKE_INSTALL_PREFIX=${prefix}"
        "-DMYSQL_DATADIR=${SERVBAY_DB_PATH}/mariadb/${version_major}"
        "-DMYSQL_UNIX_ADDR=${SERVBAY_TMP_PATH}/mariadb-${version_major}.sock"
        "-DDEFAULT_SYSCONFDIR=${SERVBAY_ETC_PATH}/mariadb/${version_major}"
        "-DEXTRA_CHARSETS=all"
        "-DDEFAULT_CHARSET=utf8mb4"
        "-DDEFAULT_COLLATION=utf8mb4_general_ci"
        "-DENABLED_LOCAL_INFILE=1"
        "-DPLUGIN_TOKUDB=NO"
        "-DPLUGIN_ROCKSDB=NO"
        "-DWITH_READLINE=NO"
        "-DWITH_UNIT_TESTS=OFF"
        "-DWITH_SSL=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}"
        "-DWITH_LZ4=${SERVBAY_COMMON_PATH}"
        "-DWITH_ZSTD=bundled"
        "-DWITH_ZLIB=${SERVBAY_COMMON_PATH}"
        "-DWITH_PCRE=bundled"
        "-DWITH_BISON=${SERVBAY_COMMON_PATH}"
        "-DWITH_JEMALLOC=${SERVBAY_COMMON_PATH}"
        "-DWITH_EMBEDDED_SERVER=1"
        "-DWITH_DEBUG=0"
        "-DWITH_BIG_TABLES=1"
        "-DBoost_NO_BOOST_CMAKE=ON"
        "-DCMAKE_MACOSX_RPATH=1"
        "-DCMAKE_PREFIX_PATH=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/include;${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/lib;${SERVBAY_COMMON_LIB_PATH};${SERVBAY_COMMON_INCLUDE_PATH};${SERVBAY_BIN_PATH}"
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=${BUILD_OS_MIN_VERSION}"
        "-DCMAKE_OSX_ARCHITECTURES=$(echo "$BUILD_CPU_ARCH" | awk '{print $2}')"
        "-DCOMPILATION_COMMENT='ServBay.dev'"
    )

    # ODBC: 禁用 CONNECT 引擎的 ODBC 支持，避免链接到 Homebrew 的 unixodbc
    cmake_args+=("-DCONNECT_WITH_ODBC=OFF")

    # LZO: 使用 ServBay 自编译的静态版本；如未编译则禁用插件，避免链接到 Homebrew
    if [[ -f "${SERVBAY_COMMON_LIB_PATH}/liblzo2.a" ]]; then
        log_message "INFO" "Found ServBay lzo, enabling PROVIDER_LZO plugin..."
        cmake_args+=("-DLZO_LIBRARIES=${SERVBAY_COMMON_LIB_PATH}/liblzo2.a" "-DLZO_INCLUDE_DIRS=${SERVBAY_COMMON_INCLUDE_PATH}")
    else
        log_message "INFO" "ServBay lzo not found, disabling PROVIDER_LZO plugin."
        cmake_args+=("-DPLUGIN_PROVIDER_LZO=NO")
    fi

    cmake -B builddir "${cmake_args[@]}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "MariaDB CMake configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # 6. Compile and Install.
    log_message "INFO" "Compiling and installing MariaDB..."
    cmake --build builddir -j ${CPU_NUMBER} && cmake --install builddir
    if [[ $? -ne 0 ]]; then log_message "ERROR" "MariaDB CMake build/install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # 7. Post-installation cleanup.
    log_message "INFO" "Cleaning up test suites, static libraries and documentation..."
    trash_path "${prefix}/mysql-test"
    trash_path "${prefix}/mariadb-test"
    trash_path "${prefix}/sql-bench"
    rm -f "${prefix}/lib/"*.a
    trash_path "${prefix}/man"

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "MariaDB build process completed successfully."
}

###
# @description Build logic for PostgreSQL and its extensions.
# @param $1 string Package version.
# @param $2 string Standardized source filename.
# @param $3 string Temporary installation directory (optional).
###
build_postgresql() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="postgresql-${version}"
    
    # Handle versions like "16rc1" to get major "16"
    local version_major=${version%%.*}
    version_major=${version_major%%rc*}

    # Third-party modules are independently built against the exact core/PGXS.

    # 2. Determine the installation prefix.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/postgresql/${version_major}"
    fi
    mkdir -p "$prefix"

    # 3. Unpack PostgreSQL source and cd into it.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 4. Apply version and architecture specific patches.
    integer version_major_num=$version_major
    if [[ "$architecture" == "arm64" ]]; then
        if (( version_major_num <= 10 )); then
            # icu-pg10.patch: 添加 #undef bool 保护，PG 10.23+ 上游已合入，需检测
            if grep -q '#undef bool' src/include/utils/pg_locale.h 2>/dev/null; then
                log_message "INFO" "Skipping icu-pg10.patch (already applied upstream)"
            else
                _apply_single_patch "${PATCH_DIR}/pgsql/icu-pg10.patch" "p0"
                if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
            fi
            _apply_single_patch "${PATCH_DIR}/pgsql/icu68.patch" "p1"
            if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
        fi
    fi
    # Common patch: 修改默认 socket 目录 (使用 sed 替代 patch 文件，避免行号偏移问题)
    log_message "INFO" "Applying tmpdir patch via sed..."
    sed -i '' 's|#define DEFAULT_PGSOCKET_DIR  "/tmp"|#define DEFAULT_PGSOCKET_DIR  "/Applications/ServBay/tmp"|' src/include/pg_config_manual.h
    if ! grep -q '/Applications/ServBay/tmp' src/include/pg_config_manual.h; then
        log_message "ERROR" "Failed to apply tmpdir patch"
        cd "$SERVBAY_BUILD_DIR"; return 1
    fi

    # 5. Configure PostgreSQL.
    log_message "INFO" "Configuring PostgreSQL v${version}..."
    local extra_cflags=""
    if (( version_major_num <= 9 )); then
        extra_cflags="CFLAGS=-Wno-error=implicit-function-declaration -Wno-incompatible-function-pointer-types"
    elif (( version_major_num <= 11 )); then
        # libxml2 2.13+ 给 xmlStructuredErrorFunc 回调参数加了 const，旧版 PG 的 xml_errorHandler 签名不兼容
        extra_cflags="CFLAGS=-Wno-incompatible-function-pointer-types"
    fi
    
    # PG19 起 configure 默认探测并要求 GSSAPI（gssapi_ext.h），新 macOS SDK 已
    # 移除该头：19+ 用 brew MIT krb5 提供真实 GSSAPI 支持而非砍特性。bison 同理
    # 由工作流工具链提供 3.x（php buildconf/mysql cmake 同样需要）。
    # PG19 起 configure 默认探测并要求 GSSAPI（gssapi_ext.h），新 macOS SDK 已
    # 移除该头：19+ 用 brew MIT krb5 提供真实 GSSAPI 支持而非砍特性。krb5 路径
    # 不能拼进 --with-includes/--with-libraries（含空格的值经 eval 二次解析会被
    # 当成 host triplet，run 40060165404 实测），改走 CFLAGS/LDFLAGS 的 -I/-L。
    # ICU 静态归档是 C++ 构建物：-licudata（ICU 内部）与 libc++（umutex 等）
    # 只在 Libs.private 里，configure 的非 static pkg-config 拿不到，链接期
    # ICU 符号全缺（run 40062810115 双架构实测）。v2 静态优先策略下显式给全。
    export ICU_LIBS="-L${SERVBAY_COMMON_LIB_PATH} -L${SERVBAY_STATIC_LIB_PATH} -licui18n -licuuc -licudata -lc++"
    # 诊断探针（PG19 ICU 链接问题取证，run 39997404749/40062810115/40069565406）：
    # configure 后立即用真实 ICU_LIBS 试编/试链一个最小 ICU 程序，并打印 runner
    # 上 ICU 库的架构与导出符号事实；探针完成后 fail-fast 保持 run 廉价。
    log_message "INFO" "[icu-probe] ICU files on disk:"
    ls -la "${SERVBAY_COMMON_LIB_PATH}"/libicu* 2>&1 | while read -r line; do log_message "INFO" "[icu-probe] $line"; done
    for _icu_file in "${SERVBAY_COMMON_LIB_PATH}/libicuuc.a" "${SERVBAY_COMMON_LIB_PATH}/libicui18n.a" "${SERVBAY_COMMON_LIB_PATH}/libicuuc.76.dylib" "${SERVBAY_COMMON_LIB_PATH}/libicui18n.76.dylib"; do
        [[ -e "$_icu_file" ]] || continue
        log_message "INFO" "[icu-probe] $(lipo -archs "$_icu_file" 2>&1 | tr '\n' ' ') <- $_icu_file"
        log_message "INFO" "[icu-probe] nm -gU symbol hits (u_errorName|ucol_open): $(nm -gU "$_icu_file" 2>/dev/null | grep -cE '_u_errorName|_ucol_open')"
    done
    local _icu_probe="${SERVBAY_BUILD_DIR}/icu-probe.c"
    printf '#include <unicode/ucol.h>\n#include <unicode/uchar.h>\n#include <stdio.h>\nint main(void){UErrorCode s=U_ZERO_ERROR;UCollator*c=ucol_open("en_US",&s);printf("%%s",u_errorName(s));ucol_close(c);return 0;}\n' > "$_icu_probe"
    log_message "INFO" "[icu-probe] test link with ICU_LIBS: ${ICU_LIBS}"
    # 本脚本是 zsh：${VAR} 不分词，必须用 ${=VAR} 模拟 sh 语义（上一轮探针
    # 的 -isysroot 吞参就是裸 ${LDFLAGS} 未分词所致）。
    if "${CC:-cc}" ${=CFLAGS} ${=CPPFLAGS} -I"${SERVBAY_COMMON_INCLUDE_PATH}" "$_icu_probe" -o "${_icu_probe%.c}" ${=ICU_LIBS} ${=LDFLAGS} 2>&1 | while read -r line; do log_message "INFO" "[icu-probe] $line"; done; [[ -x "${_icu_probe%.c}" ]]; then
        log_message "INFO" "[icu-probe] test link SUCCEEDED"
        log_message "INFO" "[icu-probe] dyld check: $(("${_icu_probe%.c}" 2>&1 | head -2 | tr '\n' ' '))"
    else
        log_message "ERROR" "[icu-probe] test link FAILED — evidence above"
        cd "$SERVBAY_BUILD_DIR"
        return 1
    fi
    local krb5_include="/opt/homebrew/opt/krb5/include /usr/local/opt/krb5/include"
    local krb5_lib="/opt/homebrew/opt/krb5/lib /usr/local/opt/krb5/lib"
    if (( version_major_num >= 19 )); then
        export CFLAGS="${CFLAGS} -I${krb5_include%% *} -I${krb5_include##* }"
        export LDFLAGS="${LDFLAGS} -L${krb5_lib%% *} -L${krb5_lib##* }"
    fi

    # Use eval to correctly handle the extra CFLAGS string if it exists
    eval ./configure \
        --prefix="${prefix}" \
        --with-includes="${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" \
        --with-libraries="${SERVBAY_COMMON_LIB_PATH}" \
        --enable-thread-safety --with-gssapi --with-icu --with-ldap \
        --with-libxml --with-libxslt --with-lz4 --with-openssl --with-pam \
        --with-bonjour --with-zstd --with-libedit-preferred --with-uuid=e2fs \
        "--with-extra-version=\(ServBay\)" \
        PG_SYSROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/ \
        $extra_cflags
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PostgreSQL configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # 6. Compile and install PostgreSQL.
    log_message "INFO" "Compiling PostgreSQL..."
    make -j${CPU_NUMBER}
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PostgreSQL make failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # The official binary target includes server development files and contrib.
    # Documentation is not shipped and must not require a host XML catalog.
    log_message "INFO" "Installing PostgreSQL (make install-world-bin)..."
    make install-world-bin
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PostgreSQL make install-world-bin failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # 8. Clean up unnecessary documentation files.
    log_message "INFO" "Cleaning up unnecessary files..."
    trash_path "${prefix}/doc"
    trash_path "${prefix}/share/doc"
    trash_path "${prefix}/man"

    # Runtime owns libpq; a server package must not mutate that shared base.

    # Return to the original build directory
    cd "$SERVBAY_BUILD_DIR"
    log_message "INFO" "PostgreSQL core and official contrib build process completed successfully."
}

# ==============================================================================
# PHP AND PECL MODULE INTERNAL BUILD FUNCTIONS
# ==============================================================================

###
# @description Package a single PHP extension directly from PHP extension directory to dist/php-ext/
# Creates a tar.gz package containing only the .so file(s).
# For phalcon (versions < 5.0), also includes psr.so.
# @param $1 string PHP major version (e.g., "8.6").
# @param $2 string Extension name (e.g., "redis", "phalcon").
# @param $3 string Extension version (e.g., "6.2.0").
# @param $4 string Target architecture (e.g., "x86_64", "arm64").
###
# @description Verify PHP runtime extension loading.
# Runs `php -v` and checks stderr for "Failed loading" errors.
# @param $1 string PHP prefix path (e.g., "/Applications/ServBay/packages/php/5.4").
# @return 0 if all extensions load successfully, 1 if any failed.
###
_verify_php_runtime_extensions() {
    local php_prefix=$1
    local php_bin="${php_prefix}/bin/php"

    if [[ ! -x "$php_bin" ]]; then
        log_message "WARN" "PHP binary not found at ${php_bin}, skipping runtime verification"
        return 0
    fi

    log_message "INFO" "🔍 Verifying PHP runtime extension loading: ${php_bin} -v"
    local php_output
    php_output=$("$php_bin" -v 2>&1)
    local php_exit_code=$?

    # Check for "Failed loading" in output
    local failed_lines
    failed_lines=$(echo "$php_output" | grep -i "Failed loading" || true)

    if [[ -n "$failed_lines" ]]; then
        log_message "ERROR" "PHP runtime extension loading verification FAILED:"
        echo "$failed_lines" | while IFS= read -r line; do
            # Extract the extension name from "Failed loading xxx.so:"
            local ext_file=$(echo "$line" | sed -n 's/.*Failed loading \([^:]*\):.*/\1/p')
            log_message "ERROR" "  -> Failed to load: ${ext_file}"
        done
        return 1
    fi

    if [[ $php_exit_code -ne 0 ]]; then
        log_message "WARN" "PHP exited with code ${php_exit_code}, but no extension loading errors detected"
    fi

    log_message "INFO" "✅ PHP runtime extension loading verification passed"
    return 0
}

###
_pack_single_php_extension() {
    local php_major_version=$1
    local ext_name=$2
    local ext_version=$3
    local arch=$4

    # 直接从 PHP 扩展目录读取（无需中间目录）
    local php_prefix="${SERVBAY_PACKAGE_FULL_PATH}/php/${php_major_version}"
    local php_config="${php_prefix}/bin/php-config"
    local dist_dir="${SERVBAY_BUILD_DIR}/dist/php-ext/${php_major_version}"

    if [[ ! -x "$php_config" ]]; then
        log_message "ERROR" "php-config not found: ${php_config}"
        return 1
    fi

    local extension_dir=$("$php_config" --extension-dir 2>/dev/null)
    if [[ -z "$extension_dir" ]] || [[ ! -d "$extension_dir" ]]; then
        log_message "ERROR" "PHP extension directory not found"
        return 1
    fi

    local so_file="${extension_dir}/${ext_name}.so"
    if [[ ! -f "$so_file" ]]; then
        log_message "ERROR" "Extension file not found: ${so_file}"
        return 1
    fi

    # Create output directory
    mkdir -p "$dist_dir"

    # Create package name
    local pkg_name="ext-${ext_name}-${ext_version}-php${php_major_version}-${arch}.tar.gz"
    local pkg_path="${dist_dir}/${pkg_name}"

    log_message "INFO" "Packaging: ${ext_name} v${ext_version} for PHP ${php_major_version}"

    # Collect files to package
    local files_to_pack=("${ext_name}.so")

    # For phalcon versions < 5.0, also include psr.so
    if [[ "$ext_name" == "phalcon" ]]; then
        local phalcon_major_version="${ext_version%%.*}"
        if [[ "$phalcon_major_version" -lt 5 ]]; then
            if [[ -f "${extension_dir}/psr.so" ]]; then
                files_to_pack+=("psr.so")
                log_message "INFO" "  -> Including psr.so (required for phalcon < 5.0)"
            else
                log_message "WARN" "  -> psr.so not found, but may be required for phalcon ${ext_version}"
            fi
        fi
    fi

    # Create tar.gz (package only the .so files, no directory structure)
    (cd "$extension_dir" && tar -czf "$pkg_path" "${files_to_pack[@]}")

    if [[ $? -ne 0 ]]; then
        log_message "ERROR" "Failed to create package: ${pkg_path}"
        return 1
    fi

    # Generate SHA256 checksum
    local checksum=$(shasum -a 256 "$pkg_path" | awk '{print $1}')
    echo "$checksum  $(basename "$pkg_path")" > "${pkg_path}.sha256"

    log_message "INFO" "  -> Created: ${pkg_name}"
    log_message "INFO" "  -> SHA256: ${checksum}"
    log_message "INFO" "  -> Contents: ${files_to_pack[*]}"

    return 0
}

###
# @description Update the extensions manifest JSON file after packaging.
# Adds or updates an extension entry in the manifest.
# @param $1 string PHP major version (e.g., "8.6").
# @param $2 string Extension name.
# @param $3 string Extension version.
# @param $4 string Target architecture.
###
_update_extensions_manifest() {
    local php_major_version=$1
    local ext_name=$2
    local ext_version=$3
    local arch=$4

    local dist_dir="${SERVBAY_BUILD_DIR}/dist/php-ext/${php_major_version}"
    local manifest_file="${dist_dir}/extensions-${arch}.json"
    local pkg_name="ext-${ext_name}-${ext_version}-php${php_major_version}-${arch}.tar.gz"
    local pkg_path="${dist_dir}/${pkg_name}"

    if [[ ! -f "$pkg_path" ]]; then
        log_message "ERROR" "Package not found: ${pkg_path}"
        return 1
    fi

    local checksum=$(shasum -a 256 "$pkg_path" | awk '{print $1}')
    local created_date=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Get zend_api from PHP installation
    local php_prefix="${SERVBAY_PACKAGE_FULL_PATH}/php/${php_major_version}"
    local zend_api=""
    if [[ -x "${php_prefix}/bin/php-config" ]]; then
        local ext_dir=$("${php_prefix}/bin/php-config" --extension-dir 2>/dev/null)
        zend_api=$(basename "$ext_dir" | sed 's/no-debug-non-zts-//')
    fi

    # If manifest doesn't exist, create a new one
    if [[ ! -f "$manifest_file" ]]; then
        cat > "$manifest_file" <<EOF
{
  "php_version": "${php_major_version}",
  "zend_api": "${zend_api}",
  "arch": "${arch}",
  "created": "${created_date}",
  "extensions": [
    {
      "name": "${ext_name}",
      "version": "${ext_version}",
      "package": "${pkg_name}",
      "checksum": "sha256:${checksum}"
    }
  ]
}
EOF
        log_message "INFO" "  -> Created manifest: ${manifest_file}"
    else
        # Update existing manifest using a temporary file
        local tmp_manifest="${manifest_file}.tmp"

        # Check if extension already exists in manifest
        if grep -q "\"name\": \"${ext_name}\"" "$manifest_file"; then
            # Update existing entry - use awk to replace the entry
            awk -v name="$ext_name" -v version="$ext_version" -v pkg="$pkg_name" -v checksum="sha256:${checksum}" '
            BEGIN { in_ext = 0; found = 0 }
            /"name": "'"$ext_name"'"/ { in_ext = 1; found = 1 }
            in_ext && /"version":/ { gsub(/"version": "[^"]*"/, "\"version\": \"" version "\""); }
            in_ext && /"package":/ { gsub(/"package": "[^"]*"/, "\"package\": \"" pkg "\""); }
            in_ext && /"checksum":/ { gsub(/"checksum": "[^"]*"/, "\"checksum\": \"" checksum "\""); in_ext = 0; }
            { print }
            ' "$manifest_file" > "$tmp_manifest"
            mv "$tmp_manifest" "$manifest_file"
            log_message "INFO" "  -> Updated manifest entry for ${ext_name}"
        else
            # Add new entry before the closing bracket (use awk, BSD sed 不支持替换中的换行)
            awk -v name="$ext_name" -v version="$ext_version" -v pkg="$pkg_name" -v checksum="sha256:${checksum}" '
            /^  \]/ {
                printf "    ,{\n      \"name\": \"%s\",\n      \"version\": \"%s\",\n      \"package\": \"%s\",\n      \"checksum\": \"%s\"\n    }\n", name, version, pkg, checksum
            }
            { print }
            ' "$manifest_file" > "$tmp_manifest"
            mv "$tmp_manifest" "$manifest_file"
            log_message "INFO" "  -> Added ${ext_name} to manifest"
        fi

        # Update the created timestamp
        sed -i '' "s/\"created\": \"[^\"]*\"/\"created\": \"${created_date}\"/" "$manifest_file"
    fi

    return 0
}

###
# @description Downloads a PECL extension source file.
# This is a helper to centralize PECL download logic.
# @param $1 string Module name (e.g., "redis").
# @param $2 string Module version (e.g., "6.2.0").
###
_install_php_module() {
    make -j${CPU_NUMBER} || return 1
    if [[ -n "$MODULE_INSTALL_ROOT" ]]; then
        make install INSTALL_ROOT="$MODULE_INSTALL_ROOT" || return 1
    else
        make install || return 1
    fi
}

_php_module_extension_dir() {
    local directory
    directory=$("$1" --extension-dir) || return 1
    [[ "$directory" == /* ]] || return 1
    print -r -- "${MODULE_INSTALL_ROOT}${directory}"
}

_verify_php_module_loads() {
    # dlopen 冒烟：扩展以 -undefined dynamic_lookup 链接时，链接期不检查缺符号，
    # .so 照常产出，运行 dlopen 时才炸 "symbol not found in flat namespace"。
    # 用 -n 跳过宿主 ini，-d extension=<绝对路径> 直接加载刚安装的 .so，
    # --ri <模块名> 触发真正的 dlopen + MINIT；非零退出（含 "not present"）
    # 或无任何输出段都视为失败。不解析输出内容。
    # 第 4 个参数起为可选依赖扩展 .so：带 ZEND_MOD_REQUIRED 的模块（phalcon 4.1.2
    # 依赖 psr）在 -n 下依赖缺席时 --ri 直接报 "required module ... is not loaded"，
    # 必须先逐个 -d extension=<dep.so> 预载；依赖 .so 不存在时指明路径并失败。
    local php_bin=$1 module_so=$2 ext_name=$3
    shift 3
    local -a preload_args=()
    local dependency
    for dependency in "$@"; do
        if [[ ! -f "$dependency" ]]; then
            log_message "ERROR" "PHP module ${ext_name} smoke dependency missing: ${dependency}. Build ${dependency:t} for this PHP series first."
            return 1
        fi
        preload_args+=(-d "extension=${dependency}")
    done
    local smoke_output
    smoke_output=$("${php_bin}" -n "${preload_args[@]}" -d "extension=${module_so}" --ri "${ext_name}" 2>&1)
    if [[ $? -ne 0 || -z "${smoke_output//[[:space:]]/}" ]]; then
        log_message "ERROR" "PHP module ${ext_name} failed the dlopen smoke test (php --ri ${ext_name} exited nonzero or printed nothing)."
        return 1
    fi
    log_message "INFO" "PHP module ${ext_name} passed the dlopen smoke test."
    return 0
}

_download_pecl_source() {
    local mod_name=$1
    local mod_version=$2
    local mod_source_filename="${mod_name}-${mod_version}.tgz"
    local url=""

    case "$mod_name" in
        "phpredis") url="https://pecl.php.net/get/redis-${mod_version}.tgz" ;;
        "memcache") url="https://pecl.php.net/get/memcache-${mod_version}.tgz" ;;
        "memcached") url="https://pecl.php.net/get/memcached-${mod_version}.tgz" ;;
        "xdebug") url="https://pecl.php.net/get/xdebug-${mod_version}.tgz" ;;
        "apcu") url="https://pecl.php.net/get/apcu-${mod_version}.tgz" ;;
        "mongodb") url="https://pecl.php.net/get/mongodb-${mod_version}.tgz" ;;
        "imap") url="https://pecl.php.net/get/imap-${mod_version}.tgz" ;;
        "imagick") url="https://pecl.php.net/get/imagick-${mod_version}.tgz" ;;
        "swoole")
            if [[ "$mod_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{7,40}$ ]]; then
                # 版本格式为 X.Y.Z-commitid，从 GitHub 下载指定 commit
                local commit_id="${mod_version##*-}"
                url="https://github.com/swoole/swoole-src/archive/${commit_id}.tar.gz"
                mod_source_filename="swoole-${mod_version}.tar.gz"
            elif [[ "$mod_version" == *"-dev"* ]]; then
                 url="https://github.com/swoole/swoole-src/archive/refs/tags/v${mod_version%-*}.tar.gz"
            else
                 url="https://pecl.php.net/get/swoole-${mod_version}.tgz"
            fi
            ;;
        "phalcon")
            url="https://github.com/phalcon/cphalcon/archive/refs/tags/v${mod_version}.tar.gz"
            mod_source_filename="phalcon-${mod_version}.tar.gz"
            ;;
        "psr") url="https://pecl.php.net/get/psr-${mod_version}.tgz" ;;
        "scws")
            url="https://github.com/naizhao/scws/archive/refs/tags/v${mod_version}.tar.gz"
            mod_source_filename="scws-${mod_version}.tar.gz"
            ;;
        "sqlsrv") url="https://pecl.php.net/get/sqlsrv-${mod_version}.tgz" ;;
        "pdo_sqlsrv") url="https://pecl.php.net/get/pdo_sqlsrv-${mod_version}.tgz" ;;
    esac

    if [[ -n "$url" ]]; then
        download_source "$url" "${SERVBAY_BUILD_SRC_DIR}/${mod_source_filename}"
        return $?
    else
        log_message "WARN" "No download URL defined for PECL module ${mod_name}."
        return 0
    fi
}

_build_php_mod_phpredis() {
    local mod_name="phpredis"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "phpredis" "$mod_version" || return 1
    unpack_package "phpredis-${mod_version}" "phpredis-${mod_version}.tgz" || return 1
    {
        if [[ "$php_version_major" == "8.4" && "$mod_version" == "6.2.0" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/phpredis-6.2.0-php8.5.patch" "p1" || return 1
        fi
        if [[ "$php_version_major" == "8.5" && "$mod_version" == "6.2.0" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/phpredis-6.2.0-php8.5.patch" "p0" || return 1
        fi
        if [[ "$php_version_major" == "8.6" ]]; then
            if [[ "$mod_version" == "6.2.0" ]]; then
                _apply_single_patch "${PATCH_DIR}/php8.5/phpredis-6.2.0-php8.5.patch" "p0" || return 1
            fi
            _apply_single_patch "${PATCH_DIR}/php8.6/phpredis.patch" "p0" || return 1
            # PHP 8.6: PS_OPEN_FUNC changed save_path from const char* to zend_string*
            # The patch handles PS_OPEN_FUNC(redis); fix PS_OPEN_FUNC(rediscluster) via sed
            log_message "INFO" "  -> Applying PHP 8.6 save_path fix for phpredis session (rediscluster)..."
            sed -i '' 's/sapi_module.treat_data(PARSE_STRING, estrdup(save_path), &z_conf);/sapi_module.treat_data(PARSE_STRING, estrdup(ZSTR_VAL(save_path)), \&z_conf);/' redis_session.c
        fi

        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/redis.so" ]]; then log_message "ERROR" "redis.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/redis.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/redis.so" "redis" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "phpredis-${mod_version}"
    }
}

_build_php_mod_memcache() {
    local mod_name="memcache"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        if [[ "$php_version_major" == "8.5" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/memcache.diff" "p0" || return 1
        fi
        if [[ "$php_version_major" == "8.6" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/memcache.diff" "p0" || return 1
            _apply_single_patch "${PATCH_DIR}/php8.6/memcache.patch" "p0" || return 1
            # PHP 8.6 PS_FUNCS macro has a missing semicolon between
            # PS_CREATE_SID_FUNC and PS_VALIDATE_SID_FUNC. Expand manually.
            sed -i '' 's/PS_FUNCS(memcache);/PS_OPEN_FUNC(memcache); PS_CLOSE_FUNC(memcache); PS_READ_FUNC(memcache); PS_WRITE_FUNC(memcache); PS_DESTROY_FUNC(memcache); PS_GC_FUNC(memcache); PS_CREATE_SID_FUNC(memcache); PS_VALIDATE_SID_FUNC(memcache);/' src/php_memcache.h
            # PHP 8.6 changed save_path from const char* to zend_string*
            sed -i '' 's|path = save_path;|path = ZSTR_VAL(save_path);|' src/memcache_session.c
            # PHP 8.6 rewrote PS_MOD(x) to reference the extension-provided
            # ps_create_sid_##x / ps_validate_sid_##x (which were the core
            # php_session_create_id / php_session_validate_sid /
            # php_session_update_timestamp defaults in <=8.5). memcache 8.0 never
            # implements the per-extension variants, so ps_mod_memcache ends up
            # referencing undefined symbols; the .so still links under macOS
            # -undefined dynamic_lookup but dlopen fails at runtime with
            # "symbol not found in flat namespace '_ps_create_sid_memcache'".
            # Expand PS_MOD manually with the core defaults to keep 8.5 semantics.
            sed -i '' 's/PS_MOD(memcache)/"memcache", ps_open_memcache, ps_close_memcache, ps_read_memcache, ps_write_memcache, ps_delete_memcache, ps_gc_memcache, php_session_create_id, php_session_validate_sid, php_session_update_timestamp/' src/memcache_session.c
        fi

        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" --with-zlib-dir="${SERVBAY_COMMON_PATH}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/memcache.so" ]]; then log_message "ERROR" "memcache.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/memcache.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/memcache.so" "memcache" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_memcached() {
    local mod_name="memcached"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        if [[ "$php_version_major" == "8.5" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/memcached.diff" "p0" || return 1
        fi
        if [[ "$php_version_major" == "8.6" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/memcached.diff" "p0" || return 1
            _apply_single_patch "${PATCH_DIR}/php8.6/memcached.patch" "p0" || return 1
        fi

        "${php_bin_dir}/phpize" || return 1
        # --disable-memcached-sasl: libmemcached 没有启用 SASL 支持
        ./configure --with-php-config="${php_config}" --enable-memcached --with-libmemcached-dir="${SERVBAY_COMMON_PATH}" --with-zlib-dir="${SERVBAY_COMMON_PATH}" --disable-memcached-sasl || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/memcached.so" ]]; then log_message "ERROR" "memcached.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/memcached.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/memcached.so" "memcached" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_xdebug() {
    local mod_name="xdebug"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        if [[ "$mod_version" == "3.1.6" ]]; then
            _apply_single_patch "${PATCH_DIR}/xdebug/3.1.6-init-func-args.patch" "p1" || return 1
        fi
        "${php_bin_dir}/phpize" || return 1

        if [[ "$php_version_major" == "8.5" ]]; then
            sed -i '' 's/80500/80600/' configure
            _apply_single_patch "${PATCH_DIR}/php8.5/xdebug-3.4.3-php8.5.patch" "p0" || return 1
        fi
        if [[ "$php_version_major" == "8.6" ]]; then
            # xdebug 3.4.3 原始版本检查是 < 80500，需要改成 < 80700
            sed -i '' 's/80500/80700/' configure
            _apply_single_patch "${PATCH_DIR}/php8.5/xdebug-3.4.3-php8.5.patch" "p0" || return 1
            # PHP 8.6: zval_dtor 已移除，需要替换为 zval_ptr_dtor_nogc
            log_message "INFO" "  -> Applying PHP 8.6 zval_dtor fix for xdebug..."
            find . -name "*.c" -exec sed -i '' 's/zval_dtor(/zval_ptr_dtor_nogc(/g' {} \;
            # PHP 8.6: WRONG_PARAM_COUNT 已移除，直接替换为等效代码
            log_message "INFO" "  -> Applying PHP 8.6 WRONG_PARAM_COUNT fix for xdebug..."
            sed -i '' 's/WRONG_PARAM_COUNT;/{ zend_wrong_param_count(); return; }/g' src/develop/php_functions.c
            # PHP 8.6: ZSTR_INIT_LITERAL 只接受字符串字面量，修复 profiler.c 中的错误用法
            log_message "INFO" "  -> Applying PHP 8.6 ZSTR_INIT_LITERAL fix for xdebug..."
            sed -i '' 's/ZSTR_INIT_LITERAL(tmp_name, false)/zend_string_init(tmp_name, strlen(tmp_name), false)/g' src/profiler/profiler.c
        fi

        ./configure --with-php-config="${php_config}" --enable-xdebug || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/xdebug.so" ]]; then log_message "ERROR" "xdebug.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/xdebug.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        # PHP 5.3/5.4: zend_extension 不支持从 extension_dir 解析相对路径，需要使用绝对路径
        if [[ -z "$MODULE_INSTALL_ROOT" && ( "$php_version_major" == "5.3" || "$php_version_major" == "5.4" ) ]]; then
            sed -i '' "s|zend_extension=xdebug.so|zend_extension=${extension_dir}/xdebug.so|" \
                "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/xdebug.ini"
        fi
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/xdebug.so" "xdebug" || return 1
        mkdir -p "${SERVBAY_LOG_PATH}/xdebug/${php_version_major}"
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_apcu() {
    local mod_name="apcu"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        if [[ "$php_version_major" == "8.6" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.6/apcu.patch" "p0" || return 1
        fi

        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" --enable-apcu || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/apcu.so" ]]; then log_message "ERROR" "apcu.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/apcu.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/apcu.so" "apcu" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_mongodb() {
    local mod_name="mongodb"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {

        if [[ "$php_version_major" == "8.5" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/mongodb-1.19.1-php8.5.patch" "p0" || return 1
        fi
        if [[ "$php_version_major" == "8.6" ]]; then
            # PHP 8.6: IS_INTERNED 已重命名为 ZSTR_IS_INTERNED
            log_message "INFO" "  -> Applying PHP 8.6 IS_INTERNED fix for mongodb..."
            sed -i '' 's/IS_INTERNED(/ZSTR_IS_INTERNED(/g' src/contrib/php_array_api.h
            # PHP 8.6: zval_dtor, ZVAL_IS_NULL, zval_is_true 已移除
            log_message "INFO" "  -> Applying PHP 8.6 zval API fixes for mongodb..."
            find . -name "*.c" -exec sed -i '' 's/zval_dtor(/zval_ptr_dtor_nogc(/g' {} \;
            find . -name "*.c" -exec sed -i '' 's/ZVAL_IS_NULL(/Z_ISNULL_P(/g' {} \;
            find . -name "*.c" -exec sed -i '' 's/zval_is_true(/zend_is_true(/g' {} \;
        fi

        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/mongodb.so" ]]; then log_message "ERROR" "mongodb.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/mongodb.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/mongodb.so" "mongodb" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_imap() {
    local mod_name="imap"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    # 某些 PHP 版本已内置 IMAP 扩展，无需编译 PECL 模块
    if "${php_bin_dir}/php" -m 2>/dev/null | grep -qi '^imap$'; then
        log_message "INFO" "  -> IMAP already built into PHP ${php_version}, skipping PECL module."
        return 0
    fi

    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        "${php_bin_dir}/phpize" || return 1
        # macOS Kerberos is available but pkg-config can't find it, set environment variables
        local SDK_PATH=$(xcrun --show-sdk-path)
        export KERBEROS_CFLAGS="-I${SDK_PATH}/usr/include"
        export KERBEROS_LIBS="-L${SDK_PATH}/usr/lib -lkrb5 -lgssapi_krb5"
        ./configure --with-php-config="${php_config}" --with-kerberos --with-imap=${SERVBAY_COMMON_PATH}/imap-uw --with-imap-ssl="${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        # 验证: 检查 .so 文件或通过 php -m 确认模块已加载
        if [[ ! -f "${extension_dir}/imap.so" ]]; then
            if ! "${php_bin_dir}/php" -m 2>/dev/null | grep -qi '^imap$'; then
                log_message "ERROR" "imap module not found (neither imap.so nor built-in)."; return 1
            fi
            log_message "INFO" "  -> IMAP is available as built-in module."
        fi
        cp "${PROJECT_ROOT}/conf/php/imap.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        if [[ -f "${extension_dir}/imap.so" ]]; then
            _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/imap.so" "imap" || return 1
        fi
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_scws() {
    local mod_name="scws"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "scws-${mod_version}.tar.gz" || return 1
    {
        touch README && aclocal && autoconf -ivf && autoheader && glibtoolize --automake --copy --debug --force && automake --add-missing || return 1
        cd phpext || return 1
        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" --with-scws="${SERVBAY_COMMON_PATH}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/scws.so" ]]; then log_message "ERROR" "scws.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/scws.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/scws.so" "scws" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_imagick() {
    local mod_name="imagick"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        if [[ "$php_version_major" =~ ^(8.4|8.5|8.6)$ && "$mod_version" == "3.7.0" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.4/imagick.patch" "p0" || return 1
        fi
        if [[ "$php_version_major" =~ ^(8.5|8.6)$ && "$mod_version" == "3.7.0" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/imagick.c.diff" "p0" || return 1
        fi
        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" --with-imagick="${SERVBAY_COMMON_PATH}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/imagick.so" ]]; then log_message "ERROR" "imagick.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/imagick.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/imagick.so" "imagick" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_swoole() {
    local mod_name="swoole"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    # Snapshot 版本 (X.Y.Z-commitid) 使用 .tar.gz，正式版本使用 .tgz
    local source_ext="tgz"
    if [[ "$mod_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{7,40}$ ]] || [[ "$mod_version" == *"-dev"* ]]; then
        source_ext="tar.gz"
    fi
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.${source_ext}" || return 1
    {
        if [[ "$mod_version" =~ ^(4.3.6|4.5.11)$ ]]; then
            _apply_single_patch "${PATCH_DIR}/swoole/swoole-4.3.6.patch" "p0" || return 1
        fi
        if [[ "$php_version_major" == "8.5" ]]; then
            _apply_single_patch "${PATCH_DIR}/php8.5/swoole.diff" "p0" || return 1
        fi
        if [[ "$php_version_major" == "8.6" ]]; then
            # snapshot 版本 (6.1.7-commitid) 需要 PHP 8.6 API 修复
            log_message "INFO" "  -> Applying PHP 8.6 fixes for swoole..."
            # 修改版本检查 (源码可能是 > 80500 或 >= 80600，两种都匹配)
            sed -i '' 's/PHP_VERSION_ID >= 80600/PHP_VERSION_ID >= 80700/g' ext-src/php_swoole_private.h
            sed -i '' 's/PHP_VERSION_ID > 80500/PHP_VERSION_ID > 80700/g' ext-src/php_swoole_private.h
            # 修复 ZVAL_IS_NULL -> Z_ISNULL_P (ext-src 和 thirdparty)
            find ext-src thirdparty -name "*.h" -exec sed -i '' 's/ZVAL_IS_NULL(/Z_ISNULL_P(/g' {} \;
            find ext-src thirdparty -name "*.cc" -exec sed -i '' 's/ZVAL_IS_NULL(/Z_ISNULL_P(/g' {} \;
            find ext-src thirdparty -name "*.c" -exec sed -i '' 's/ZVAL_IS_NULL(/Z_ISNULL_P(/g' {} \;
            # 修复 zval_is_true -> zend_is_true
            find ext-src thirdparty -name "*.cc" -exec sed -i '' 's/zval_is_true(/zend_is_true(/g' {} \;
            find ext-src thirdparty -name "*.c" -exec sed -i '' 's/zval_is_true(/zend_is_true(/g' {} \;
            # 修复 zval_dtor -> zval_ptr_dtor_nogc
            find ext-src thirdparty -name "*.cc" -exec sed -i '' 's/zval_dtor(/zval_ptr_dtor_nogc(/g' {} \;
            find ext-src thirdparty -name "*.c" -exec sed -i '' 's/zval_dtor(/zval_ptr_dtor_nogc(/g' {} \;
            # 修复 arg_info 类型不兼容 (PHP 8.6 将 zend_arg_info 与 zend_internal_arg_info 严格区分)
            # 读取 internal_function.arg_info 时强转为 zend_internal_arg_info *
            sed -i '' 's/return zf->internal_function\.arg_info;/return (zend_internal_arg_info *)zf->internal_function.arg_info;/g' ext-src/swoole_runtime.cc
            sed -i '' 's/= zf->internal_function\.arg_info;/= (zend_internal_arg_info *)zf->internal_function.arg_info;/g' ext-src/swoole_runtime.cc
            sed -i '' 's/, zf->internal_function\.arg_info)/, (zend_internal_arg_info *)zf->internal_function.arg_info)/g' ext-src/swoole_runtime.cc
            # 写入 internal_function.arg_info 时强转为 zend_arg_info *
            sed -i '' 's/internal_function\.arg_info = rf->ori_arg_info;/internal_function.arg_info = (zend_arg_info *)rf->ori_arg_info;/g' ext-src/swoole_runtime.cc
            sed -i '' 's/internal_function\.arg_info = arg_info;/internal_function.arg_info = (zend_arg_info *)arg_info;/g' ext-src/swoole_runtime.cc
            sed -i '' 's/internal_function\.arg_info = copy_arginfo/internal_function.arg_info = (zend_arg_info *)copy_arginfo/g' ext-src/swoole_runtime.cc
        fi
        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" \
            --enable-openssl \
            --enable-brotli \
            --enable-swoole-sqlite \
            --enable-swoole-curl \
            --enable-http2 \
            --enable-swoole-json || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/swoole.so" ]]; then log_message "ERROR" "swoole.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/swoole.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/swoole.so" "swoole" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_sqlsrv() {
    local mod_name="sqlsrv"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/sqlsrv.so" ]]; then log_message "ERROR" "sqlsrv.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/sqlsrv.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/sqlsrv.so" "sqlsrv" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_pdo_sqlsrv() {
    local mod_name="pdo_sqlsrv"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        # PHP 8.5+ changed PDO internals: query_stmt_zval -> query_stmt_obj,
        # error_mode enum -> uint8_t. Patch source for compatibility.
        if [[ "$php_version_major" =~ ^(8\.[5-9])$ ]]; then
            log_message "INFO" "  -> Applying PHP 8.5+ PDO compatibility fixes for pdo_sqlsrv"
            # Fix 1: query_stmt_zval replaced by query_stmt_obj (zend_object*)
            sed -i '' 's|zval_ptr_dtor( &dbh->query_stmt_zval );|if (dbh->query_stmt_obj) { OBJ_RELEASE(dbh->query_stmt_obj); dbh->query_stmt_obj = NULL; }|' php_pdo_sqlsrv_int.h
            # Fix 2: error_mode changed from enum pdo_error_mode to uint8_t
            sed -i '' 's|pdo_error_mode prev_err_mode = dbh->error_mode;|pdo_error_mode prev_err_mode = static_cast<pdo_error_mode>(dbh->error_mode);|' pdo_dbh.cpp
        fi
        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/pdo_sqlsrv.so" ]]; then log_message "ERROR" "pdo_sqlsrv.so not found."; return 1; fi
        cp "${PROJECT_ROOT}/conf/php/pdo_sqlsrv.ini" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/" || return 1
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/pdo_sqlsrv.so" "pdo_sqlsrv" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_phalcon() {
    local mod_name="phalcon"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "phalcon-${mod_version}.tar.gz" || return 1
    {
        # Phalcon 3.x/4.x (PHP 5.6-7.4) 使用 build/phpN/64bits
        # Phalcon 5.x (PHP 8.0+) 使用 build/phalcon
        if [[ "$php_version_major" == "5.6" ]]; then
            cd build/php5/64bits || return 1
        elif [[ "$php_version_major" =~ ^(7\.[0-4])$ ]]; then
            cd build/php7/64bits || return 1
        else
            # Phalcon 5.x (PHP 8.0+)
            if [[ "$php_version_major" =~ "8.4" ]]; then
                _apply_single_patch "${PATCH_DIR}/php8.5/phalcon.diff" "p0" || return 1
            fi
            if [[ "$php_version_major" == "8.5" ]]; then
                _apply_single_patch "${PATCH_DIR}/php8.5/phalcon.diff" "p0" || return 1
            fi
            if [[ "$php_version_major" == "8.6" ]]; then
                _apply_single_patch "${PATCH_DIR}/php8.5/phalcon.diff" "p0" || return 1
                # PHP 8.6: zval_dtor 已移除，需要替换为 zval_ptr_dtor_nogc
                log_message "INFO" "  -> Applying PHP 8.6 zval API fixes for phalcon..."
                sed -i '' 's/zval_dtor(/zval_ptr_dtor_nogc(/g' build/phalcon/phalcon.zep.c
                # PHP 8.6: _zval_get_string_func 已移除前缀下划线
                sed -i '' 's/_zval_get_string_func(/zval_get_string_func(/g' build/phalcon/phalcon.zep.c
                sed -i '' 's/_zval_get_long_func(/zval_get_long_func(/g' build/phalcon/phalcon.zep.c
                sed -i '' 's/_zval_get_double_func(/zval_get_double_func(/g' build/phalcon/phalcon.zep.c
            fi
            cd build/phalcon || return 1
        fi
        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" --enable-phalcon || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/phalcon.so" ]]; then log_message "ERROR" "phalcon.so not found."; return 1; fi
        # psr 是 phalcon 的依赖，打包在一起
        local ini_file_name="phalcon.ini"
        local ini_source_path="${PROJECT_ROOT}/conf/php/${ini_file_name}"
        if [[ "$php_version_major" =~ ^(7.2|7.3|7.4)$ ]]; then
            ini_source_path="${PROJECT_ROOT}/conf/php/phalcon-7.x.ini"
        fi
        cp "$ini_source_path" "${SERVBAY_ETC_PATH}/php/${php_version_major}/conf.d/${ini_file_name}" || return 1
        # phalcon 4.1.2（7.2-7.4）带 ZEND_MOD_REQUIRED("psr")，冒烟必须预载同链 psr.so；
        # 宿主实测 3.4.5 与 5.7.0/5.9.3 均无此依赖，8.x 无 psr 模块、不传依赖。
        # 顺序：bundle 场景 build_module.sh 先构建 psr 再进本函数，psr.so 必已就位。
        local -a phalcon_smoke_deps=()
        if [[ "$php_version_major" =~ ^(7\.[2-4])$ ]]; then
            phalcon_smoke_deps+=("${extension_dir}/psr.so")
        fi
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/phalcon.so" "phalcon" "${phalcon_smoke_deps[@]}" || return 1
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}

_build_php_mod_psr() {
    local mod_name="psr"; local mod_version=$1; local php_version=$2; local php_version_major=$3; local php_prefix=$4
    log_message "INFO" "  -> Building PECL module: ${mod_name} v${mod_version}"
    local php_config="${php_prefix}/bin/php-config"; local php_bin_dir="${php_prefix}/bin"
    _download_pecl_source "$mod_name" "$mod_version" || return 1
    unpack_package "${mod_name}-${mod_version}" "${mod_name}-${mod_version}.tgz" || return 1
    {
        "${php_bin_dir}/phpize" || return 1
        ./configure --with-php-config="${php_config}" || return 1
        _install_php_module || return 1
        local extension_dir
        extension_dir=$(_php_module_extension_dir "$php_config") || return 1
        if [[ ! -f "${extension_dir}/psr.so" ]]; then log_message "ERROR" "psr.so not found."; return 1; fi
        _verify_php_module_loads "${php_bin_dir}/php" "${extension_dir}/psr.so" "psr" || return 1
        # psr 不单独打包，与 phalcon 一起打包
        cd "$SERVBAY_BUILD_DIR" || return 1
        cleanup_source_directory "${mod_name}-${mod_version}"
    }
}


###
# @description PHP Build Configuration Center.
# @param $1 string PHP major version (e.g., "8.2").
# @modifies global PHP_* arrays.
###
_get_php_build_config() {
    local version_major=$1

    # 获取 SDK 路径用于系统库
    local sdk_path=$(xcrun --sdk macosx --show-sdk-path)

    PHP_CONFIGURE_ARGS=()
    PHP_PATCHES=()
    PHP_MODS=()
    PHP_PRE_BUILD_CMDS=()
    PHP_POST_BUILD_CMDS=()

    # PHP 通用配置参数
    # 所有自编译库统一安装在 SERVBAY_COMMON_PATH
    local common_args=(
        "--sysconfdir=${SERVBAY_ETC_PATH}/php/${version_major}" "--with-config-file-path=${SERVBAY_ETC_PATH}/php/${version_major}"
        "--with-config-file-scan-dir=${SERVBAY_ETC_PATH}/php/${version_major}/conf.d" "--localstatedir=${SERVBAY_VAR_PATH}"
        "--enable-fpm" "--enable-mysqlnd" "--with-mysqli=mysqlnd" "--with-pdo-mysql=mysqlnd"
        "--with-mysql-sock=${SERVBAY_TMP_PATH}/mysql.sock"
        
        "--with-gdbm=${SERVBAY_COMMON_PATH}"
        "--with-gettext=${SERVBAY_COMMON_PATH}"
        "--with-tidy=${SERVBAY_COMMON_PATH}"
        "--with-imap=${SERVBAY_COMMON_PATH}/imap-uw"
        # 使用自编译的 iconv, zlib, bz2 库
        # 原因：macOS SDK 仅提供 .tbd 存根文件，旧版 PHP configure 脚本无法处理
        # 且系统 iconv 是 BSD 版本（无 GNU libiconv 前缀符号），会导致链接错误
        "--with-iconv=${SERVBAY_COMMON_PATH}"
        "--with-zlib=${SERVBAY_COMMON_PATH}"
        "--with-bz2=${SERVBAY_COMMON_PATH}"
        # 动态库依赖
        "--with-curl=${SERVBAY_COMMON_PATH}"
        "--with-xsl=${SERVBAY_COMMON_PATH}"
        "--with-openssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}"
        "--with-readline=${SERVBAY_COMMON_PATH}"
        "--with-ldap=${SERVBAY_COMMON_PATH}"
        "--with-imap-ssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}"
        # 其他配置
        "--enable-xml" "--enable-bcmath"
        "--enable-shmop" "--enable-sysvsem" "--enable-sysvshm" "--enable-ftp"
        "--enable-mbstring=all" "--enable-sockets" "--enable-soap"
        "--enable-calendar" "--enable-exif" "--enable-opcache"
        "--with-mhash" "--enable-mbregex" "--enable-intl"
        "--enable-pcntl" "--with-zip" "--with-gmp=${SERVBAY_COMMON_PATH}"
        "--with-sqlite3" "--with-pdo-sqlite"
        "--with-kerberos" "--with-pear"
    )

    case "$version_major" in
        "5.3")
            # PHP 5.3 需要特殊配置以兼容旧版本的依赖
            local php53_common_args=(
                "--sysconfdir=${SERVBAY_ETC_PATH}/php/${version_major}" "--with-config-file-path=${SERVBAY_ETC_PATH}/php/${version_major}"
                "--with-config-file-scan-dir=${SERVBAY_ETC_PATH}/php/${version_major}/conf.d" "--localstatedir=${SERVBAY_VAR_PATH}"
                "--enable-fpm" "--enable-mysqlnd" "--with-mysqli=mysqlnd" "--with-pdo-mysql=mysqlnd"
                "--with-mysql-sock=${SERVBAY_TMP_PATH}/mysql.sock"
                 "--with-gdbm=${SERVBAY_COMMON_PATH}"
                "--with-iconv=${SERVBAY_COMMON_PATH}" "--with-zlib-dir=${SERVBAY_COMMON_PATH}" "--enable-bcmath"
                "--enable-shmop" "--enable-sysvsem" "--enable-sysvshm" "--enable-ftp" "--with-bz2=${SERVBAY_COMMON_PATH}"
                "--enable-mbstring=all" "--with-curl=${SERVBAY_COMMON_PATH}" "--enable-sockets" "--enable-soap"
                "--enable-calendar" "--enable-exif" "--with-xsl=${SERVBAY_COMMON_PATH}"
                "--with-openssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}"
                "--with-mhash" "--with-readline=${SERVBAY_COMMON_PATH}" "--enable-mbregex" "--disable-intl"
                "--enable-pcntl" "--enable-zip" "--with-gettext=${SERVBAY_COMMON_PATH}" "--with-gmp=${SERVBAY_COMMON_PATH}"
                "--with-sqlite3" "--with-pdo-sqlite" "--with-tidy=${SERVBAY_COMMON_PATH}"
                "--with-kerberos=${SERVBAY_COMMON_PATH}" "--with-pear" "--with-imap=${SERVBAY_COMMON_PATH}/imap-uw" "--with-imap-ssl"
            )
            PHP_CONFIGURE_ARGS=(
                "--with-mysql=mysqlnd" "--with-libxml-dir=${SERVBAY_PACKAGE_FULL_PATH}"
                "--with-gd=${SERVBAY_COMMON_PATH}" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}"
                "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}" "--without-xpm-dir"
            )
            PHP_CONFIGURE_ARGS+=( "${php53_common_args[@]}" )
            PHP_PATCHES=(
                "php5.3/php5.3-servbay-complete.patch:p1"
            )
            # -lresolv 已包含在全局 LDFLAGS 中，无需覆盖
            PHP_POST_BUILD_CMDS=()

            # PHP 5.3 不支持 MongoDB 和 Phalcon
            PHP_MODS=( [phpredis]="4.3.0" [memcache]="2.2.7" [memcached]="2.2.0" [xdebug]="2.2.7" [apcu]="4.0.11" [scws]="1.2.4-pre" [imagick]="3.7.0" )
            ;;
        "5.4")
            # PHP 5.4 需要特殊配置以兼容旧版本的依赖
            local php54_common_args=(
                "--sysconfdir=${SERVBAY_ETC_PATH}/php/${version_major}" "--with-config-file-path=${SERVBAY_ETC_PATH}/php/${version_major}"
                "--with-config-file-scan-dir=${SERVBAY_ETC_PATH}/php/${version_major}/conf.d" "--localstatedir=${SERVBAY_VAR_PATH}"
                "--enable-fpm" "--enable-mysqlnd" "--with-mysqli=mysqlnd" "--with-pdo-mysql=mysqlnd"
                "--with-mysql-sock=${SERVBAY_TMP_PATH}/mysql.sock"
                 "--with-gdbm=${SERVBAY_COMMON_PATH}"
                "--with-iconv=${SERVBAY_COMMON_PATH}" "--with-zlib-dir=${SERVBAY_COMMON_PATH}" "--enable-bcmath"
                "--enable-shmop" "--enable-sysvsem" "--enable-sysvshm" "--enable-ftp" "--with-bz2=${SERVBAY_COMMON_PATH}"
                "--enable-mbstring=all" "--with-curl=${SERVBAY_COMMON_PATH}" "--enable-sockets" "--enable-soap"
                "--enable-calendar" "--enable-exif" "--with-xsl=${SERVBAY_COMMON_PATH}"
                "--with-openssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}"
                "--with-mhash" "--with-readline=${SERVBAY_COMMON_PATH}" "--enable-mbregex" "--disable-intl"
                "--enable-pcntl" "--enable-zip" "--with-gettext=${SERVBAY_COMMON_PATH}" "--with-gmp=${SERVBAY_COMMON_PATH}"
                "--with-sqlite3" "--with-pdo-sqlite" "--with-tidy=${SERVBAY_COMMON_PATH}"
                "--with-kerberos=${SERVBAY_COMMON_PATH}" "--with-pear" "--with-imap=${SERVBAY_COMMON_PATH}/imap-uw" "--with-imap-ssl"
            )
            PHP_CONFIGURE_ARGS=(
                "--with-mysql=mysqlnd" "--with-libxml-dir=${SERVBAY_PACKAGE_FULL_PATH}"
                "--with-icu-dir=${SERVBAY_PACKAGE_FULL_PATH}" "--with-gd=${SERVBAY_COMMON_PATH}" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}"
                "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}" "--without-xpm-dir"
            )
            PHP_CONFIGURE_ARGS+=( "${php54_common_args[@]}" )
            PHP_PATCHES=(
                "php5.4/php5.4-servbay-complete.patch:p1"
            )
            # -lresolv 已包含在全局 LDFLAGS 中，无需覆盖
            PHP_POST_BUILD_CMDS=()

            # PHP 5.4 不支持 MongoDB 和 Phalcon
            PHP_MODS=( [phpredis]="4.3.0" [memcache]="2.2.7" [memcached]="2.2.0" [xdebug]="2.4.1" [apcu]="4.0.11" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="1.10.5" )
            ;;
        "5.5")
            # PHP 5.5 需要特殊配置以兼容旧版本的依赖
            local php55_common_args=(
                "--sysconfdir=${SERVBAY_ETC_PATH}/php/${version_major}" "--with-config-file-path=${SERVBAY_ETC_PATH}/php/${version_major}"
                "--with-config-file-scan-dir=${SERVBAY_ETC_PATH}/php/${version_major}/conf.d" "--localstatedir=${SERVBAY_VAR_PATH}"
                "--enable-fpm" "--enable-mysqlnd" "--with-mysqli=mysqlnd" "--with-pdo-mysql=mysqlnd"
                "--with-mysql-sock=${SERVBAY_TMP_PATH}/mysql.sock"
                 "--with-gdbm=${SERVBAY_COMMON_PATH}"
                "--with-iconv=${SERVBAY_COMMON_PATH}" "--with-zlib-dir=${SERVBAY_COMMON_PATH}" "--enable-bcmath"
                "--enable-shmop" "--enable-sysvsem" "--enable-sysvshm" "--enable-ftp" "--with-bz2=${SERVBAY_COMMON_PATH}"
                "--enable-mbstring=all" "--with-curl=${SERVBAY_COMMON_PATH}" "--enable-sockets" "--enable-soap"
                "--enable-calendar" "--enable-exif" "--with-xsl=${SERVBAY_COMMON_PATH}"
                "--with-openssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" "--enable-opcache"
                "--with-mhash" "--with-readline=${SERVBAY_COMMON_PATH}" "--enable-mbregex" "--disable-intl"
                "--enable-pcntl" "--enable-zip" "--with-gettext=${SERVBAY_COMMON_PATH}" "--with-gmp=${SERVBAY_COMMON_PATH}"
                "--with-sqlite3" "--with-pdo-sqlite" "--with-tidy=${SERVBAY_COMMON_PATH}"
                "--with-kerberos=${SERVBAY_COMMON_PATH}" "--with-pear" "--with-imap=${SERVBAY_COMMON_PATH}/imap-uw" "--with-imap-ssl"
            )
            PHP_CONFIGURE_ARGS=(
                "--with-mysql=mysqlnd" "--with-libxml-dir=${SERVBAY_PACKAGE_FULL_PATH}"
                "--with-icu-dir=${SERVBAY_PACKAGE_FULL_PATH}" "--with-gd=${SERVBAY_COMMON_PATH}" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}"
                "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}" "--without-xpm-dir"
            )
            PHP_CONFIGURE_ARGS+=( "${php55_common_args[@]}" )
            PHP_PATCHES=(
                "php5.5/php5.5-servbay-complete.patch:p1"
            )
            # -lresolv 已包含在全局 LDFLAGS 中，无需覆盖
            PHP_POST_BUILD_CMDS=()

            # PHP 5.5 不支持 Phalcon
            PHP_MODS=( [phpredis]="4.3.0" [memcache]="2.2.7" [memcached]="2.2.0" [xdebug]="2.5.5" [apcu]="4.0.11" [mongodb]="1.5.5" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="1.10.5" )
            ;;
        "5.6")
            # PHP 5.6 需要自编译的 libiconv/zlib/bzip2，使用独立的配置参数（类似 5.3/5.4/5.5）
            local php56_common_args=(
                "--sysconfdir=${SERVBAY_ETC_PATH}/php/${version_major}" "--with-config-file-path=${SERVBAY_ETC_PATH}/php/${version_major}"
                "--with-config-file-scan-dir=${SERVBAY_ETC_PATH}/php/${version_major}/conf.d" "--localstatedir=${SERVBAY_VAR_PATH}"
                "--enable-fpm" "--enable-mysqlnd" "--with-mysqli=mysqlnd" "--with-pdo-mysql=mysqlnd"
                "--with-mysql-sock=${SERVBAY_TMP_PATH}/mysql.sock"
                 "--with-gdbm=${SERVBAY_COMMON_PATH}"
                "--with-iconv=${SERVBAY_COMMON_PATH}" "--with-zlib-dir=${SERVBAY_COMMON_PATH}" "--enable-bcmath"
                "--enable-shmop" "--enable-sysvsem" "--enable-sysvshm" "--enable-ftp" "--with-bz2=${SERVBAY_COMMON_PATH}"
                "--enable-mbstring=all" "--with-curl=${SERVBAY_COMMON_PATH}" "--enable-sockets" "--enable-soap"
                "--enable-calendar" "--enable-exif" "--with-xsl=${SERVBAY_COMMON_PATH}"
                "--with-openssl=${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}" "--enable-opcache"
                "--with-mhash" "--with-readline=${SERVBAY_COMMON_PATH}" "--enable-mbregex" "--enable-intl"
                "--enable-pcntl" "--enable-zip" "--with-gettext=${SERVBAY_COMMON_PATH}" "--with-gmp=${SERVBAY_COMMON_PATH}"
                "--with-sqlite3" "--with-pdo-sqlite" "--with-tidy=${SERVBAY_COMMON_PATH}"
                "--with-kerberos=${SERVBAY_COMMON_PATH}" "--with-pear" "--with-imap=${SERVBAY_COMMON_PATH}/imap-uw" "--with-imap-ssl"
                "--with-ldap=${SERVBAY_COMMON_PATH}"
            )
            PHP_CONFIGURE_ARGS=(
                "--with-mysql=mysqlnd" "--with-libzip=${SERVBAY_COMMON_PATH}" "--with-libxml-dir=${SERVBAY_COMMON_PATH}"
                "--with-icu-dir=${SERVBAY_COMMON_PATH}" "--with-gd" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}"
                "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}"
            )
            PHP_CONFIGURE_ARGS+=( "${php56_common_args[@]}" )
            PHP_PATCHES=(
                "php5.6/php-5.6.x-intl_ICU_NAMESPACE.patch:p1" "php5.6/php-5.6.x-mail-header.patch:p1"
                "php5.6/php-5.6.x-OpenSSL-1.1.1.patch:p1" "php5.6/php-mbstring-onig.6.9.patch:p0"
                "php5.6/mbstring.patch:p0" "php5.6/opcache.patch:p1"
                "php-openssl3/openssl.patch:p1" "php7.1/icu.patch:p1" "php5.6/php5.6-macos-compile-fixes.patch:p0"
                "php5.6/libxml.patch:p1"
                # GD 构建测试的 char foobar() conftest 在当前工具链下一运行就 SIGILL，
                # 改为 link-only（各 release hunk 行号不同，不可跨版本复用）。
                "php5.6/gd-configure-runtime-test.patch:p1"
                # libtidy 5+ 把 buffio.h 改名为 tidybuffio.h（base 无兼容头），
                # 5.6 无探测地 include 旧名；用 __has_include 优先新名兜底旧名。
                "php5.6/tidy-buffio-header.patch:p1"
            )
            # -lresolv 已包含在全局 LDFLAGS 中，无需覆盖
            PHP_POST_BUILD_CMDS=()
            PHP_MODS=( [phpredis]="4.3.0" [memcache]="2.2.7" [memcached]="2.2.0" [xdebug]="2.5.5" [apcu]="4.0.11" [mongodb]="1.7.5" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="1.10.5" [phalcon]="3.4.5" )
            ;;
        "7.0")
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--with-gd" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}" "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}" "--with-webp-dir=${SERVBAY_COMMON_PATH}" "--with-icu-dir=${SERVBAY_COMMON_PATH}" "--with-onig=${SERVBAY_COMMON_PATH}" )
            PHP_PATCHES=( "php-openssl3/openssl.patch:p1" "php7.0/icu.patch:p0" "php7.0/icu-2.patch:p1" "php7.0/onig.patch:p0" "php7.0/zend-sprintf.patch:p1" "php7.0/gd-configure-runtime-test.patch:p1" "php7.0/tidy-buffio-header.patch:p1" )
            PHP_MODS=( [phpredis]="5.3.7" [memcache]="4.0.5.2" [memcached]="3.1.5" [xdebug]="2.5.5" [apcu]="5.1.22" [mongodb]="1.7.5" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="4.3.6" [phalcon]="3.4.5" )
            ;;
        "7.1")
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--with-gd" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}" "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}" "--with-webp-dir=${SERVBAY_COMMON_PATH}" "--with-icu-dir=${SERVBAY_COMMON_PATH}" )
            PHP_PATCHES=( "php-openssl3/openssl.patch:p1" "php7.0/zend-sprintf.patch:p1" "php7.1/icu.patch:p1" "php7.1/gd-configure-runtime-test.patch:p1" )
            PHP_MODS=( [phpredis]="5.3.7" [memcache]="4.0.5.2" [memcached]="3.1.5" [xdebug]="2.5.5" [apcu]="5.1.22" [mongodb]="1.11.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="4.5.11" [phalcon]="3.4.5" )
            ;;
        "7.2")
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--with-gd" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}" "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}" "--with-webp-dir=${SERVBAY_COMMON_PATH}" "--with-icu-dir=${SERVBAY_COMMON_PATH}" "--with-sodium=${SERVBAY_COMMON_PATH}" )
            PHP_PATCHES=( "php-openssl3/openssl.patch:p1" "php7.0/zend-sprintf.patch:p1" "php7.2/icu.patch:p1" "php7.2/gd-configure-runtime-test.patch:p1" )
            PHP_MODS=( [phpredis]="5.3.7" [memcache]="4.0.5.2" [memcached]="3.1.5" [xdebug]="3.1.6" [apcu]="5.1.22" [mongodb]="1.11.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="4.8.13" [psr]="1.1.0" [phalcon]="4.1.2" )
            ;;
        "7.3")
            # --without-pcre-jit: PHP 7.3 bundled PCRE2 JIT 在 Xcode 16 clang 下存在 UB，
            # 导致 php 二进制在模块初始化阶段崩溃 (https://github.com/php/php-src/issues/16168)
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--with-gd" "--with-jpeg-dir=${SERVBAY_COMMON_PATH}" "--with-png-dir=${SERVBAY_COMMON_PATH}" "--with-freetype-dir=${SERVBAY_COMMON_PATH}" "--with-webp-dir=${SERVBAY_COMMON_PATH}" "--with-icu-dir=${SERVBAY_COMMON_PATH}" "--with-sodium=${SERVBAY_COMMON_PATH}" "--without-pcre-jit" )
            PHP_PATCHES=( "php-openssl3/openssl.patch:p1" "php7.0/zend-sprintf.patch:p1" "php7.3/icu.patch:p1" "php7.3/gd-configure-runtime-test.patch:p1" )
            PHP_MODS=( [phpredis]="5.3.7" [memcache]="4.0.5.2" [memcached]="3.1.5" [xdebug]="3.1.6" [apcu]="5.1.22" [mongodb]="1.11.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="4.8.13" [psr]="1.1.0" [phalcon]="4.1.2" )
            ;;
        "7.4")
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--enable-gd" "--with-jpeg" "--with-webp" "--with-freetype" "--with-sodium=${SERVBAY_COMMON_PATH}" "--with-external-pcre=${SERVBAY_COMMON_PATH}" )
            PHP_PATCHES=( "php-openssl3/openssl.patch:p1" "php7.4/gd-configure-runtime-test.patch:p1" )
            PHP_MODS=( [phpredis]="5.3.7" [memcache]="4.0.5.2" [memcached]="3.1.5" [xdebug]="3.1.6" [apcu]="5.1.22" [mongodb]="1.11.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="4.8.13" [psr]="1.1.0" [phalcon]="4.1.2" )
            ;;
        "8.0")
            # PHP 8.0: 使用 pkg-config 自动查找图像库，sodium 使用静态库路径
            # 注意：PHP 8.0 仍然有内置 imap 支持（已弃用但可用），PHP 8.1+ 才需要 PECL imap
            # Swoole 6.x 需要 PHP 8.1+ (Fibers)，PHP 8.0 使用 Swoole 5.1.x
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--enable-gd" "--with-jpeg" "--with-webp" "--with-freetype" "--with-sodium=${SERVBAY_COMMON_PATH}" )
            # arm64: PHP 8.0 bundled PCRE2 JIT 无法在 arm64 macOS 上分配 MAP_JIT 内存，
            # 导致 make 生成 phar.phar 时 Fatal Error（PHP 8.1+ 已将此错误降级为 warning）
            if [[ "$architecture" == "arm64" ]]; then
                PHP_CONFIGURE_ARGS+=( "--with-external-pcre=${SERVBAY_COMMON_PATH}" )
            fi
            # PHP 8.0 需要 OpenSSL 3.x 和 libxml2 2.12+ 兼容性补丁
            PHP_PATCHES=( "php-openssl3/openssl.patch:p1" "php-openssl3/openssl-2.patch:p1" "php8.0/libxml.patch:p1" )
            PHP_MODS=( [phpredis]="5.3.7" [memcache]="8.0" [memcached]="3.2.0" [xdebug]="3.3.1" [apcu]="5.1.22" [mongodb]="1.15.0" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="5.1.6" [phalcon]="5.7.0" )
            ;;
        "8.1")
            # PHP 8.1: 使用 pkg-config 自动查找图像库，sodium 使用静态库路径
            # 注意：imap 仍然作为内置扩展编译（通过 common_args 中的 --with-imap）
            # 注意：zend_string 内联汇编问题通过 configure 后的 sed 修复解决（见 6.3 节）
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--enable-gd" "--with-jpeg" "--with-webp" "--with-freetype" "--with-sodium=${SERVBAY_COMMON_PATH}" )
            PHP_MODS=( [phpredis]="6.2.0" [memcache]="8.0" [memcached]="3.2.0" [xdebug]="3.4.3" [apcu]="5.1.22" [mongodb]="1.19.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="6.1.6" [phalcon]="5.9.3" [sqlsrv]="5.12.0" [pdo_sqlsrv]="5.12.0" )
            ;;
        "8.2"|"8.3")
            # PHP 8.2-8.3: 使用 pkg-config 自动查找图像库，sodium 使用静态库路径
            # 注意：imap 仍然作为内置扩展编译（通过 common_args 中的 --with-imap）
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--enable-gd" "--with-jpeg" "--with-webp" "--with-freetype" "--with-sodium=${SERVBAY_COMMON_PATH}" )
            PHP_MODS=( [phpredis]="6.2.0" [memcache]="8.0" [memcached]="3.2.0" [xdebug]="3.4.3" [apcu]="5.1.22" [mongodb]="1.19.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="6.1.6" [phalcon]="5.9.3" [sqlsrv]="5.12.0" [pdo_sqlsrv]="5.12.0" )
            ;;
        "8.4")
            # PHP 8.4: 使用 pkg-config 自动查找图像库，sodium 使用静态库路径
            # PHP 8.0+ 移除了内置 imap 支持，需要使用 PECL 扩展
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--enable-gd" "--with-jpeg" "--with-webp" "--with-freetype" "--with-sodium=${SERVBAY_COMMON_PATH}" )
            PHP_CONFIGURE_ARGS=( ${PHP_CONFIGURE_ARGS[@]/--with-imap=*/} )
            PHP_CONFIGURE_ARGS=( ${PHP_CONFIGURE_ARGS[@]/--with-imap-ssl=*/} )
            PHP_PRE_BUILD_CMDS=( "./buildconf --force" )
            PHP_MODS=( [phpredis]="6.2.0" [memcache]="8.0" [memcached]="3.2.0" [xdebug]="3.4.3" [apcu]="5.1.24" [mongodb]="1.19.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="6.1.6" [phalcon]="5.9.3" [imap]="1.0.3" [sqlsrv]="5.12.0" [pdo_sqlsrv]="5.12.0" )
            ;;
        "8.5")
            # PHP 8.5: 使用 pkg-config 自动查找图像库，sodium 使用静态库路径
            # PHP 8.0+ 移除了内置 imap 支持，需要使用 PECL 扩展
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--enable-gd" "--with-jpeg" "--with-webp" "--with-freetype" "--with-sodium=${SERVBAY_COMMON_PATH}" )
            PHP_CONFIGURE_ARGS=( ${PHP_CONFIGURE_ARGS[@]/--with-imap=*/} )
            PHP_CONFIGURE_ARGS=( ${PHP_CONFIGURE_ARGS[@]/--with-imap-ssl=*/} )
            PHP_PRE_BUILD_CMDS=( "./buildconf --force" )
            # swoole 6.1.6 与 PHP 8.5 有 segfault，使用固定 commit (88fc550) 修复了 PHP 8.5 兼容性
            PHP_MODS=( [phpredis]="6.2.0" [memcache]="8.0" [memcached]="3.2.0" [xdebug]="3.4.3" [apcu]="5.1.24" [mongodb]="1.19.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="6.1.7-88fc55029c" [phalcon]="5.9.3" [imap]="1.0.3" [sqlsrv]="5.12.0" [pdo_sqlsrv]="5.12.0" )
            ;;
        "8.6")
            # PHP 8.6: 使用 pkg-config 自动查找图像库，sodium 使用静态库路径
            # PHP 8.0+ 移除了内置 imap 支持，需要使用 PECL 扩展
            PHP_CONFIGURE_ARGS=( "${common_args[@]}" "--enable-gd" "--with-jpeg" "--with-webp" "--with-freetype" "--with-sodium=${SERVBAY_COMMON_PATH}" )
            PHP_CONFIGURE_ARGS=( ${PHP_CONFIGURE_ARGS[@]/--with-imap=*/} )
            PHP_CONFIGURE_ARGS=( ${PHP_CONFIGURE_ARGS[@]/--with-imap-ssl=*/} )
            # PHP 8.6 JIT 在 x86_64 上与 Apple clang 17 不兼容 (musttail 错误)
            if [[ "$architecture" == "x86_64" ]]; then
                PHP_CONFIGURE_ARGS+=( "--disable-opcache-jit" )
            fi
            PHP_PRE_BUILD_CMDS=( "./buildconf --force" )
            # PHP 8.6 (dev) 从 Zend/zend_portability.h 移除了历史宏 XtOffsetOf，
            # 而 phpredis/memcached/apcu/imap/imagick 等大量 PECL 扩展仍在使用。
            # 通过核心补丁一次性恢复（对齐 v1 32c50ef），make install 后随核心分发，
            # 所有针对该 PHP 编译的扩展都能拿到该宏。
            PHP_PATCHES=( "php8.6/zend_portability-xtoffsetof.patch:p0" )
            # PHP 8.6-dev: 大部分扩展已通过补丁支持 (patch/php8.6/)
            # swoole 使用与 PHP 8.5 相同的 snapshot 版本 (88fc55029c commit)
            PHP_MODS=( [phpredis]="6.2.0" [memcache]="8.0" [memcached]="3.2.0" [xdebug]="3.4.3" [apcu]="5.1.24" [mongodb]="1.19.1" [scws]="1.2.4-pre" [imagick]="3.7.0" [swoole]="6.1.7-88fc55029c" [phalcon]="5.9.3" [imap]="1.0.3" [sqlsrv]="5.12.0" [pdo_sqlsrv]="5.12.0" )
            ;;
    esac
    
    # PHP8.4+ obtains libpq from isolated pkg-config. Earlier configure macros
    # receive only the Runtime prefix; their external helper search is patched
    # before configure, so no installed PostgreSQL server is required.
    if [[ "$version_major" =~ ^(5\.[3-6]|7\.[0-4]|8\.[0-3])$ ]]; then
        PHP_CONFIGURE_ARGS+=( "--with-pgsql=${SERVBAY_COMMON_PATH}" "--with-pdo-pgsql=${SERVBAY_COMMON_PATH}" )
    else
        PHP_CONFIGURE_ARGS+=( "--with-pgsql" "--with-pdo-pgsql" )
    fi

    if [[ "$version_major" =~ ^(5.3|5.4|5.5|5.6|7.0|7.1|7.2|7.3)$ ]]; then
        CFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1 -Wno-implicit-function-declaration"
        export CFLAGS
        # ICU 76/77 需要 C++17 (std::enable_if_t, std::is_same_v 等)
        # -Wno-register: PHP 5.x 使用 register 关键字，在 C++17 中已废弃
        CXXFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1 -std=c++17 -stdlib=libc++ -Wno-register"
        export CXXFLAGS
        CPPFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1"
        export CPPFLAGS
        # PHP 5.x/7.x 使用 IMAP with Kerberos，需要链接我们自编译的 gssapi_krb5 库（macOS GSS.framework 没有 gss_nt_service_name 符号）
        # -llzma: libzip 静态库需要 lzma
        export LIBS="-lgssapi_krb5 -llzma -lzstd"
    fi

    # PHP 7.4 和 8.0 需要 C++17 来支持 ICU 76+ (std::is_same_v, std::enable_if_t 等)
    if [[ "$version_major" =~ ^(7\.4|8\.0)$ ]]; then
        CFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1"
        export CFLAGS
        CXXFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1 -std=c++17 -stdlib=libc++"
        export CXXFLAGS
        CPPFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1"
        export CPPFLAGS
    fi

    # PHP 5.x/7.x 需要额外的编译参数以抑制特定警告
    # 现代 clang (Xcode 15+) 将 -Wincompatible-function-pointer-types 提升为错误
    # PHP 5.x/7.x 的 libxml/openssl 扩展存在函数指针类型不匹配（如 const 修饰符差异）
    if [[ "$version_major" =~ ^(5\.[3-6]|7\.[0-4])$ ]]; then
        CFLAGS+=" -Wno-error=incompatible-function-pointer-types"
        export CFLAGS
    fi
    # PHP 5.x 额外需要 -Wno-error=implicit-int（旧 C 代码使用隐式 int）
    if [[ "$version_major" =~ ^(5\.[3-6])$ ]]; then
        CFLAGS+=" -Wno-error=implicit-int"
        export CFLAGS
    fi

    # PHP 5.x: 移除 LDFLAGS 中的 -isysroot 和 CPPFLAGS 中的 SDK include 路径
    # 原因：PHP 5.x 的旧 configure 脚本会从 -isysroot 和 -I${SDK}/usr/include 推导出
    # -L${SDK}/usr/lib 并添加到 EXTRA_LDFLAGS 中，且排在自编译库路径之前。
    # 这导致链接器优先找到系统 libiconv（BSD 符号 _iconv）而非自编译的 GNU libiconv
    # （GNU 符号 _libiconv），从而产生 undefined symbol 错误。
    # 编译器仍然通过 CFLAGS 中的 -isysroot 找到系统头文件，无需额外 -I 路径。
    if [[ "$version_major" =~ ^(5\.[3-6])$ ]]; then
        export LDFLAGS="-Wl,-rpath -Wl,${SERVBAY_COMMON_LIB_PATH} -L${SERVBAY_STATIC_LIB_PATH} -L${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/lib -L${SERVBAY_COMMON_LIB_PATH} -L${SERVBAY_PACKAGE_FULL_PATH} -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET} -llzma -lzstd -lresolv"
        export CPPFLAGS="-I${SERVBAY_STATIC_INCLUDE_PATH} -I${SERVBAY_COMMON_INCLUDE_PATH} -I${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/include -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1"
    fi
}

###
# @description Build logic for PHP and its PECL extensions.
###
prepare_php_source() {
    local version=$1
    python3 "${PROJECT_ROOT}/ci/php_source.py" --source "$PWD" --version "$version" \
        --build-dir "$SERVBAY_BUILD_DIR" --common "$SERVBAY_COMMON_PATH" \
        > "${SERVBAY_BUILD_LOG_DIR}/php-source-${version}.json" || return 1
    local series=$(echo "$version" | awk -F. '{print $1"."$2}')
    if [[ "$series" =~ ^(5\.[3-6]|7\.[0-4]|8\.[0-3])$ ]]; then
        export FREETYPE2_CONFIG="${SERVBAY_BUILD_DIR}/tmp/php-tools/freetype-config"
    fi
}

build_php() {
    local version=$1
    local sourceFilename=$2
    local installDir=$3
    local packageFullName="php-${version}"
    local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')

    # 1. Get the build plan by populating global arrays.
    _get_php_build_config "$version_major"
    
    # 2. Determine the installation prefix.
    local prefix
    if [[ -n "$installDir" ]]; then
        prefix="$installDir"
    else
        prefix="${SERVBAY_PACKAGE_FULL_PATH}/php/${version_major}"
    fi
    mkdir -p "$prefix"

    # 3. Unpack source and cd into it.
    unpack_package "$packageFullName" "$sourceFilename"
    if [[ $? -ne 0 ]]; then return 1; fi

    # 4. Apply patches for PHP core.
    for patch_info in "${PHP_PATCHES[@]}"; do
        local patch_file=${patch_info%%:*}
        local patch_level=${patch_info#*:}
        _apply_single_patch "${PATCH_DIR}/${patch_file}" "$patch_level"
        if [[ $? -ne 0 ]]; then cd "$SERVBAY_BUILD_DIR"; return 1; fi
    done

    prepare_php_source "$version" || { cd "$SERVBAY_BUILD_DIR"; return 1; }

    # 5. Run pre-build commands (e.g., ./buildconf).
    for cmd in "${PHP_PRE_BUILD_CMDS[@]}"; do
        log_message "INFO" "Running pre-build command: $cmd"
        eval "$cmd"
        if [[ $? -ne 0 ]]; then log_message "ERROR" "PHP pre-build command failed: $cmd"; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    done
    
    # 6. Configure PHP.
    log_message "INFO" "Configuring PHP v${version}..."
    export PEAR_CONFIGFILE="${SERVBAY_BUILD_DIR}/.pearrc"

    # PHP 7.4 和 8.0 需要 C++17 来支持 ICU 76+ (std::is_same_v, std::enable_if_t 等)
    # 必须在 configure 时设置 CXXFLAGS，因为 PHP 的 Makefile 会使用 configure 时的值
    if [[ "$version_major" =~ ^(7\.4|8\.0)$ ]]; then
        log_message "INFO" "Setting C++17 flags for ICU 76+ compatibility..."
        export CXXFLAGS="${CXXFLAGS} -std=c++17 -stdlib=libc++"
    fi

    # arm64 + Apple clang -O2/-O3 + -fvisibility=hidden（由 PHP configure 自动添加）：
    # configure 中多处 link 测试（GD、IMAP 等）使用 char foobar(){} 作为 stub 函数，
    # 非 void 函数无 return 属于 UB，在此编译器组合下 clang 会生成 BRK 陷阱指令，
    # conftest 运行时触发 SIGILL (Illegal instruction: 4)，导致 configure 误判测试失败。
    # 修复：将 char foobar 改为 void foobar，消除 UB，不影响 link 测试语义。
    # 注意：此修复仅适用于 PHP < 8.0，PHP 8.0+ 的 configure 已使用正确的 void foobar。
    if [[ "$architecture" == "arm64" ]] && [[ "$version_major" =~ ^(5\.[3-6]|7\.[0-4])$ ]]; then
        sed -i '' -e 's/char foobar () { return 0; }/void foobar () {}/g' \
                  -e 's/char foobar/void foobar/g' configure
    fi

    ./configure --prefix="${prefix}" "${PHP_CONFIGURE_ARGS[@]}"
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PHP configure failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # Stable archives ship VM headers. Their .INTERMEDIATE make target must
    # not regenerate them with an unrelated PHP executable from the host.
    # Sources without both generated headers retain their normal generator.
    if [[ -f Zend/zend_vm_execute.h && -f Zend/zend_vm_opcodes.h && -f Makefile ]]; then
        sed -E 's/^PHP = .*/PHP =/' Makefile > Makefile.servbay-tmp && mv Makefile.servbay-tmp Makefile || {
            log_message "ERROR" "Cannot pin PHP to its shipped VM headers."
            cd "$SERVBAY_BUILD_DIR"
            return 1
        }
    fi

    # 6.1 PHP 5.x/7.x Makefile 修复：移除 SDK 库路径，防止系统库被优先链接
    # 原因：旧 configure 脚本通过 xml2-config 等工具获取到 SDK 路径
    # (如 -L/path/to/SDKs/MacOSX.sdk/usr/lib)，并将其添加到 EXTRA_LDFLAGS 中
    # 且排在自编译库路径之前。这导致链接器找到系统库（如 BSD iconv、editline）
    # 而非自编译的库（如 GNU libiconv、GNU readline），产生 undefined symbol 错误。
    # 编译器已通过 CFLAGS 中的 -isysroot 找到系统库，无需额外 -L 路径。
    if [[ "$version_major" =~ ^(5\.[3-6]|7\.[0-4])$ ]]; then
        local sdk_usr_lib="${SDK_PATH}/usr/lib"
        if grep -q "${sdk_usr_lib}" Makefile 2>/dev/null; then
            log_message "INFO" "Removing SDK library path from Makefile to fix library search order..."
            sed -i '' "s|-L${sdk_usr_lib} ||g; s|-L${sdk_usr_lib}$||g" Makefile
            sed -i '' "s|-Wl,-rpath,${sdk_usr_lib} ||g; s|-Wl,-rpath,${sdk_usr_lib}$||g" Makefile
        fi
    fi

    # 6.2 PHP 7.x php_config.h 修复：修正 readdir_r 检测
    # 原因：configure 脚本错误检测为 HAVE_OLD_READDIR_R（2 参数版本），
    # 但 macOS 使用 POSIX 3 参数版本的 readdir_r。
    if [[ "$version_major" =~ ^(7\.[0-4])$ ]]; then
        if grep -q "HAVE_OLD_READDIR_R" main/php_config.h 2>/dev/null; then
            log_message "INFO" "Fixing readdir_r detection in php_config.h..."
            sed -i '' 's|#define HAVE_OLD_READDIR_R 1|/* #undef HAVE_OLD_READDIR_R */|g' main/php_config.h
            sed -i '' 's|/\* #undef HAVE_POSIX_READDIR_R \*/|#define HAVE_POSIX_READDIR_R 1|g' main/php_config.h
        fi
    fi

    # 6.3 PHP 7.3+/8.0/8.1: 禁用 zend_string_equal_val 内联汇编
    # 原因：Apple clang 17+ (Xcode 16+) 编译 PHP 7.3-8.1 的内联汇编
    # zend_string_equal_val 时，会产生栈缓冲区溢出（__stack_chk_fail），
    # 导致 php 二进制在 zend_startup_modules 阶段崩溃。
    # 修复：强制使用 memcmp 回退实现。
    # 参考：https://github.com/php/php-src/issues/16168
    if [[ "$version_major" =~ ^(7\.[34]|8\.[01])$ ]]; then
        if [ -f Zend/zend_string.h ]; then
            log_message "INFO" "Disabling inline asm zend_string_equal_val for Apple clang compatibility..."
            # 在 zend_string.h 中：强制使用 memcmp 回退（将 __GNUC__ 条件改为 0）
            sed -i '' 's/#if defined(__GNUC__) && (defined(__i386__) || (defined(__x86_64__) \&\& !defined(__ILP32__)))/#if 0 \/* disabled for Apple clang 17+ compat *\//' Zend/zend_string.h
            # 在 zend_string.c 中：禁用 i386 和 x86_64 的内联汇编实现
            sed -i '' 's/#if defined(__GNUC__) && defined(__i386__)/#if 0 \/* disabled for Apple clang 17+ compat *\//' Zend/zend_string.c
            sed -i '' 's/#elif defined(__GNUC__) && defined(__x86_64__) && !defined(__ILP32__)/#elif 0 \/* disabled for Apple clang 17+ compat *\//' Zend/zend_string.c
        fi
    fi

    # 6.4 PHP 5.6-7.3 tidy 扩展 buffio.h 兼容性说明
    # libtidy v5+ 将 buffio.h 重命名为 tidybuffio.h，
    # PHP 5.6-7.3 的 ext/tidy/tidy.c 仍然 #include "buffio.h"。
    # 已在 build_deps.sh 中通过 -DTIDY_COMPAT_HEADERS=ON 安装 buffio.h 兼容头文件，
    # 该头文件仅包含 #include "tidybuffio.h" 的转发，无需在此修改 PHP 源码。

    # 6.5 PHP 7.4/8.0: 修复 intl 扩展的 C++ 标准
    # 原因：PHP Makefile 对 intl 扩展的 .cpp 文件硬编码了 -std=c++11，
    # 这会覆盖 CXXFLAGS 中的 -std=c++17 设置。ICU 76+ 需要 C++17 特性
    # (如 std::enable_if_t, std::is_same_v)，否则编译会失败。
    # 修复：将 Makefile 中的 -std=c++11 替换为 -std=c++17。
    if [[ "$version_major" =~ ^(7\.4|8\.0)$ ]]; then
        if grep -q '\-std=c++11' Makefile 2>/dev/null; then
            log_message "INFO" "Fixing intl extension C++ standard in Makefile (c++11 -> c++17)..."
            sed -i '' 's/-std=c++11/-std=c++17/g' Makefile
        fi
    fi

    # 6.6 PHP 5.3-7.3: 旧 ext/gd/libgd 把 historic `boolean` 类型的回调赋给 jpeglib 的
    # safeboolean 槽位，现代 clang 将该不匹配提升为 error，gd_jpeg.c 编不过。
    # 该降级必须写进 configure 生成的 Makefile 的 CFLAGS_CLEAN（libtool 的编译行
    # 实际展开的是它，PHP configure 会重建 CFLAGS，追加到环境变量无效）。v1 6b03016。
    if [[ "$version_major" =~ ^(5\.[3-6]|7\.[0-3])$ && -f Makefile ]]; then
        log_message "INFO" "Allowing the legacy libjpeg callback signature mismatch in gd_jpeg.c..."
        sed -E 's/^CFLAGS_CLEAN = (.*)$/CFLAGS_CLEAN = \1 -Wno-error=incompatible-function-pointer-types -Wno-error=implicit-int/' \
            Makefile > Makefile.servbay-cflags && mv Makefile.servbay-cflags Makefile
    fi

    # 7. Compile and install PHP.
    log_message "INFO" "Compiling and installing PHP..."
    if [[ ${#PHP_POST_BUILD_CMDS[@]} -gt 0 ]]; then
        for cmd in "${PHP_POST_BUILD_CMDS[@]}"; do
            log_message "INFO" "Running custom make command: $cmd"
            if ! eval "$cmd"; then
                log_message "ERROR" "PHP custom make command failed: $cmd"
                cd "$SERVBAY_BUILD_DIR"
                return 1
            fi
        done
    else
        make -j${CPU_NUMBER}
    fi
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PHP make failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi
    
    make install
    if [[ $? -ne 0 ]]; then log_message "ERROR" "PHP make install failed."; cd "$SERVBAY_BUILD_DIR"; return 1; fi

    # 7.1 创建 PHP conf.d 目录（make install 不会自动创建）
    mkdir -p "${SERVBAY_ETC_PATH}/php/${version_major}/conf.d"

    cd "$SERVBAY_BUILD_DIR"

    # 7.2 打包 PHP core（在扩展编译前打包，此时只包含内置扩展如 opcache）
    log_message "INFO" "📦 Packaging PHP core (before PECL extensions)..."
    get_package_packing_info "php" "$version" || return 1
    core_verify_package "php" "$version" "${prefix}/bin/php -n -v" || return 1
    check_package_homebrew_deps "php" "$version" "strict" || return 1
    print -r -- "$version" > "${prefix}/VERSION" || return 1
    core_pack_package "php" "$version" || return 1

    # Third-party extensions have independent build jobs and archives.
    log_message "INFO" "PHP core build completed; use --php-ext for third-party modules."
    return 0

}

# ==============================================================================
# BUILD DISPATCHER & MAIN LOGIC
# ==============================================================================

###
# @description Main dispatcher for all package builds.
# @param $1 - $4 All necessary build parameters.
###
setup_package_build_environment() {
    local packageName=$1
    local version=$2

    # 步骤 1: 获取一次 SDK 路径并存为变量
    export SDK_PATH=$(xcrun --sdk macosx --show-sdk-path)

    # 步骤 2: 将 SDK 路径设置给 CMAKE_OSX_SYSROOT (用于 CMake 项目)
    export CMAKE_OSX_SYSROOT="${SDK_PATH}"
    local sdk_pkgconfig_dir="${SERVBAY_BUILD_DIR}/tmp/system-pkgconfig"
    python3 "${PROJECT_ROOT}/ci/sdk_pkgconfig.py" --sdk "$SDK_PATH" --output "$sdk_pkgconfig_dir" || return 1

    # 步骤 3: 将 -isysroot 标志添加到 CFLAGS, LDFLAGS, 和 CXXFLAGS (用于非 CMake 项目)
    # 注意：我们在原有内容的【前面】加入了 -isysroot 标志
    #
    # 静态库优先链接策略：
    # - SERVBAY_STATIC_LIB_PATH 放在前面，优先使用静态库 (.a)
    # - SERVBAY_COMMON_LIB_PATH 放在后面，用于动态库 (.dylib)
    export CFLAGS="-isysroot ${SDK_PATH} -Qunused-arguments -O3 -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET}"
    # LDFLAGS 需要包含静态库依赖的额外库：lzma, zstd (用于 libzip), resolv (用于 DNS)
    export LDFLAGS="-isysroot ${SDK_PATH} -Wl,-rpath -Wl,${SERVBAY_COMMON_LIB_PATH} -L${SERVBAY_STATIC_LIB_PATH} -L${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/lib -L${SERVBAY_COMMON_LIB_PATH} -L${SERVBAY_PACKAGE_FULL_PATH} -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET} -llzma -lzstd -lresolv"
    export CPPFLAGS="-I${SDK_PATH}/usr/include -I${SERVBAY_STATIC_INCLUDE_PATH} -I${SERVBAY_COMMON_INCLUDE_PATH} -I${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/include"
    # brew gdbm 的 ndbm 头（python _dbmmodule 等消费方）：-isysroot 会抑制
    # /usr/local/include 的默认搜索，而两个 runner 的 brew 前缀不同（arm64=
    # /opt/homebrew，x86_64=/usr/local），显式把 gdbm 的 opt include 加进检索
    # 路径；不存在的 -I 会被静默忽略，单侧安装不受影响（x86_64 run
    # 40008026750 实测 configure 能找到、make 带上 CPPFLAGS 后找不到）。
    export CPPFLAGS="${CPPFLAGS} -I/opt/homebrew/opt/gdbm/include -I/usr/local/opt/gdbm/include"
    # CPython 的 make 用 PY_CPPFLAGS（configure 烘焙的基础 -I），不继承运行时
    # 追加的 CPPFLAGS，上面的 -I 到不了模块编译行（x86_64 run 40018066972 实测
    # 仍报 gdbm-ndbm.h not found）。把 brew gdbm 的头复制进已在检索路径上的
    # ServBay include 目录，让 configure 与 make 两侧都能直接命中。
    local _gdbm_include
    for _gdbm_include in /opt/homebrew/opt/gdbm/include /usr/local/opt/gdbm/include; do
        if [[ -f "$_gdbm_include/gdbm-ndbm.h" ]]; then
            cp "$_gdbm_include/gdbm.h" "$_gdbm_include/gdbm-ndbm.h" "${SERVBAY_COMMON_INCLUDE_PATH}/" || return 1
            if [[ -d "$_gdbm_include/gdbm" ]]; then
                cp -R "$_gdbm_include/gdbm" "${SERVBAY_COMMON_INCLUDE_PATH}/" || return 1
            fi
            log_message "INFO" "Copied brew gdbm headers into ${SERVBAY_COMMON_INCLUDE_PATH} for the dbm module."
            break
        fi
    done
    export CXXFLAGS="-isysroot ${SDK_PATH} -Qunused-arguments -O3 -mmacosx-version-min=${BUILD_OS_MIN_VERSION} ${BUILD_CPU_ARCH} ${BUILD_MACOS_TARGET} -Wno-enum-constexpr-conversion"

    # PHP 版本特定的 C++ 标志（必须在基础 CXXFLAGS 设置之后）
    # PHP 5.x-7.2 和 7.4、8.0 需要 C++17 来支持 ICU 76+ (std::is_same_v, std::enable_if_t 等)
    if [[ "$packageName" == "php" ]]; then
        local version_major=$(echo "$version" | awk -F. '{print $1"."$2}')
        if [[ "$version_major" =~ ^(5\.[3-6]|7\.[0-2])$ ]]; then
            CFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1 -Wno-implicit-function-declaration"
            CXXFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1 -std=c++17 -stdlib=libc++ -Wno-register"
            CPPFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1"
            export CFLAGS CXXFLAGS CPPFLAGS
            export LIBS="-lgssapi_krb5 -llzma -lzstd"
        elif [[ "$version_major" =~ ^(7\.[34]|8\.0)$ ]]; then
            # Match the confirmed v1 Clang workaround for these source lines.
            # 7.3 与 7.4/8.0 是同一类 -O3 误编译：x86_64 上 cli/php 在 make
            # 首次运行（生成 phar.php）时 SIGABRT (v1 0918cc7)，arm64 亦统一 -O1。
            CFLAGS="${CFLAGS//-O3/-O1}"
            CFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1"
            CXXFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1 -std=c++17 -stdlib=libc++"
            CPPFLAGS+=" -fstack-protector-strong -fno-strict-aliasing -DUSING_ICU_NAMESPACE=1 -DU_DEFINE_FALSE_AND_TRUE=1"
            export CFLAGS CXXFLAGS CPPFLAGS
        fi
        # PHP 8.5+ 在 x86_64 上使用 Apple clang 17 编译时会出现 musttail 错误
        # 需要使用 -O1 优化级别来避免此编译器 bug (Apple clang 的 musttail 实现有问题)
        if [[ "$version_major" =~ ^(8\.[5-9])$ && "$architecture" == "x86_64" ]]; then
            CFLAGS="${CFLAGS//-O3/-O1}"
            CXXFLAGS="${CXXFLAGS//-O3/-O1}"
            export CFLAGS CXXFLAGS
            log_message "INFO" "Using -O1 optimization for PHP $version_major on x86_64 (Apple clang musttail workaround)"
        fi
        # PHP 5.x/7.x 需要额外的编译参数以抑制特定警告
        if [[ "$version_major" =~ ^(5\.[3-6]|7\.[0-4])$ ]]; then
            CFLAGS+=" -Wno-error=incompatible-function-pointer-types"
            export CFLAGS
        fi
    fi

    # 其他环境变量设置
    # PKG_CONFIG_PATH 也需要包含静态库的 pkgconfig
    export PKG_CONFIG_PATH="${SERVBAY_STATIC_LIB_PATH}/pkgconfig:${SERVBAY_COMMON_LIB_PATH}/pkgconfig:${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/lib/pkgconfig:${sdk_pkgconfig_dir}"
    export PKG_CONFIG="$(command -v pkg-config)"
    [[ -n "$PKG_CONFIG" ]] || { log_message "ERROR" "pkg-config is required"; return 1; }
    export PKG_CONFIG="${PKG_CONFIG:A}"
    # Devlib config scripts use this build-only tool path; package archives own other directories.
    mkdir -p "$SERVBAY_BIN_PATH" || return 1
    if [[ ! -e "$SERVBAY_BIN_PATH/pkg-config" ]]; then
        ln -s "$PKG_CONFIG" "$SERVBAY_BIN_PATH/pkg-config" || return 1
    fi
    export PKG_CONFIG_LIBDIR="${PKG_CONFIG_PATH}"

    # base 烘焙的 *-config（apr-1-config/apu-1-config/MagickCore-config 等）把
    # Utility runtime 构建机的编译器 wrapper 绝对路径写死为 --cc 输出（段工作流
    # 的 RUNTIME_TOOL_DIR=$RUNNER_TEMP/runtime-v2-tools）。包编译 runner 上该路径
    # 不存在，任何咨询 --cc 的 configure（如 httpd 的 APR-util 探测）都会报
    # "C compiler cannot create executables"（两架构首测 39962819981 的真实失败）。
    # 在 runner 上按同一契约重建 wrapper，指向本机真实编译器；环境 CFLAGS 已带
    # isysroot/-arch，wrapper 无需重复注入。非 runner 环境（无 /Users/runner/work）
    # 不创建，避免污染开发机。
    local _runtime_tools_root="${RUNNER_TEMP:-/Users/runner/work/_temp}"
    if [[ -d "$_runtime_tools_root" && ! -x "$_runtime_tools_root/runtime-v2-tools/cc" ]]; then
        mkdir -p "$_runtime_tools_root/runtime-v2-tools" || return 1
        ln -sf "$(xcrun --find clang)" "$_runtime_tools_root/runtime-v2-tools/cc" || return 1
        ln -sf "$(xcrun --find clang++)" "$_runtime_tools_root/runtime-v2-tools/cxx" || return 1
        log_message "INFO" "Recreated the base-baked compiler wrapper at ${_runtime_tools_root}/runtime-v2-tools/cc (base *-config scripts reference it)."
    fi

    # 确保静态库目录存在
    mkdir -p "${SERVBAY_STATIC_LIBS_PATH}" "${SERVBAY_STATIC_INCLUDE_PATH}" "${SERVBAY_STATIC_LIB_PATH}"

    # 添加 Homebrew bison 路径 (PHP dev 版本需要 bison 3.0+ 来执行 buildconf)
    # ${SERVBAY_COMMON_PATH}/bin: geos-config, icu-config 等依赖库的工具
    export PATH="${SERVBAY_BIN_PATH}:${SERVBAY_SBIN_PATH}:${SERVBAY_COMMON_PATH}/bin:/usr/local/opt/bison/bin:/opt/homebrew/opt/bison/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Library/Apple/usr/bin:/usr/local/bin:/usr/local/sbin:/opt/homebrew/bin"

    if ! command -v go &> /dev/null; then
        log_message "WARN" "Warning: 'go' command not found. Go-based packages will fail to build."
    else
        if [[ -z "$GOPATH" ]]; then
            export GOPATH="$HOME/go"
        fi
        # Add GOPATH/bin to PATH for Go tools like tscriptify
        export PATH="${GOPATH}/bin:${PATH}"
        export GO111MODULE=on
        export GOENV=off
        export GOPROXY="${GOPROXY:-https://proxy.golang.org,direct}"
    fi

    return 0
}

build_package() {
    local packageName=$1
    local version=$2
    local sourceFilename=$3
    local installDir=$4

    setup_package_build_environment "$packageName" "$version" || return 1

    local build_function_name="build_${packageName//-/_}"
    if whence -f "$build_function_name" > /dev/null; then
        "$build_function_name" "$version" "$sourceFilename" "$installDir"
    else
        log_message "ERROR" "❓ Build function '$build_function_name' not implemented."
        return 1
    fi
}


###
# @description Set up the build environment by ensuring /Applications/ServBay is the v2 environment.
# Safety: Strictly relies on the presence of 'package' (v1) and 'packages' (v2) markers.
###
setup_build_env() {
    if [[ ! -d "${SERVBAY_COMMON_PATH}" || ! -f "${SERVBAY_BASE_FULL_PATH}/.bootstrap-v2.json" || -e "${SERVBAY_BASE_FULL_PATH}/package" ]]; then
        log_message "ERROR" "A verified public Runtime v2/Devlib base is required. Run bootstrap_base.sh first."
        return 1
    fi
}

# The standalone module producer reuses the same recipes without running the CLI.
[[ "$SERVBAY_RECIPE_LIBRARY_ONLY" == "1" ]] && return 0

init_logging

setup_build_env || exit 1

log_message "INFO" "==================== Build Script Started ===================="
log_message "INFO" "Using Zsh shell for execution."

# Initialize Homebrew check flags
checkHomebrew=""
checkHomebrewOnly=""
strictMode=""
phpExtOnly=""
phpCoreOnly=""
packPhpExt=""

while [[ "$#" -gt 0 ]]; do
    case $1 in
        -p|--package)
            # 支持逗号分隔和空格分隔: -p pkg1,pkg2 或 -p pkg1 pkg2
            packageNames="$2"; shift
            while [[ "$#" -gt 1 && ! "$2" =~ ^- ]]; do
                packageNames="${packageNames},${2}"; shift
            done
            ;;
        -a|--architecture) architecture="$2"; shift ;;
        --install-dir) installDir="$2"; shift ;;
        --check-homebrew) checkHomebrew="true" ;;
        --check-homebrew-only) checkHomebrewOnly="true" ;;
        --strict) strictMode="strict" ;;
        --php-ext) phpExtOnly="$2"; shift ;;
        --php-core-only) phpCoreOnly="true" ;;
        --pack-php-ext) packPhpExt="$2"; shift ;;
        -h|--help) usage ;;
        *) log_message "ERROR" "Unknown parameter passed: $1"; usage ;;
    esac
    shift
done

if [[ -z "$architecture" ]]; then
    log_message "ERROR" "Architecture is required."
    usage
fi
if [[ "$architecture" != "x86_64" ]] && [[ "$architecture" != "arm64" ]]; then
    log_message "ERROR" "Invalid architecture."
    usage
fi

if [[ "$architecture" == "x86_64" ]]; then
    BUILD_CPU_ARCH="-arch x86_64"
    BUILD_MACOS_TARGET="-target x86_64-apple-macos${BUILD_OS_MIN_VERSION}"
else
    BUILD_CPU_ARCH="-arch arm64"
fi

log_message "INFO" "Executing in: $(pwd)"
log_message "INFO" "Target Architecture: $architecture"
log_message "INFO" "OpenSSL Version: $OPENSSL_VERSION"

# ==============================================================================
# PHP-EXT-ONLY MODE (build PHP extensions without rebuilding PHP core)
# ==============================================================================
if [[ -n "$phpExtOnly" ]]; then
    log_message "INFO" "Running in PHP Extension Only Mode"

    # Split by comma for multiple extensions
    IFS=',' read -A extList <<< "$phpExtOnly"

    local failedExts=()
    local successCount=0
    typeset -A builtPhpVersions

    for extSpec in "${extList[@]}"; do
        # Parse format: php_version:ext_name:ext_version
        IFS=':' read -r php_version_major ext_name ext_version <<< "$extSpec"

        if [[ -z "$php_version_major" ]] || [[ -z "$ext_name" ]] || [[ -z "$ext_version" ]]; then
            log_message "ERROR" "Invalid extension format: $extSpec (expected php_version:ext_name:ext_version)"
            failedExts+=("$extSpec")
            continue
        fi

        # Determine PHP prefix
        local php_prefix="${SERVBAY_PACKAGE_FULL_PATH}/php/${php_version_major}"

        if [[ ! -d "$php_prefix" ]]; then
            log_message "ERROR" "PHP ${php_version_major} not installed at ${php_prefix}"
            failedExts+=("$extSpec")
            continue
        fi

        # Get full PHP version from php-config
        local php_version=$("${php_prefix}/bin/php-config" --version 2>/dev/null)
        if [[ -z "$php_version" ]]; then
            log_message "ERROR" "Cannot determine PHP version from ${php_prefix}/bin/php-config"
            failedExts+=("$extSpec")
            continue
        fi

        log_message "INFO" "Building extension: ${ext_name} v${ext_version} for PHP ${php_version_major} (${php_version})"

        # Core and standalone modules use the same SDK and isolated libraries.
        if ! setup_package_build_environment php "$php_version"; then
            failedExts+=("$extSpec")
            continue
        fi
        _get_php_build_config "$php_version_major"

        # Find and call the build function
        local mod_build_func="_build_php_mod_${ext_name//-/_}"
        if whence -f "$mod_build_func" > /dev/null; then
            "$mod_build_func" "$ext_version" "$php_version" "$php_version_major" "$php_prefix"
            if [[ $? -ne 0 ]]; then
                log_message "ERROR" "Failed to build: ${ext_name} v${ext_version}"
                failedExts+=("$extSpec")
            else
                log_message "INFO" "Successfully built: ${ext_name} v${ext_version}"
                ((successCount++))
                builtPhpVersions[$php_version_major]=1
                # 自动打包扩展
                local ext_name_for_pack="$ext_name"
                [[ "$ext_name" == "phpredis" ]] && ext_name_for_pack="redis"
                if [[ "$ext_name" != "psr" ]]; then
                    log_message "INFO" "📦 Auto-packaging extension: ${ext_name_for_pack} v${ext_version}"
                    if _pack_single_php_extension "$php_version_major" "$ext_name_for_pack" "$ext_version" "$architecture"; then
                        _update_extensions_manifest "$php_version_major" "$ext_name_for_pack" "$ext_version" "$architecture"
                    else
                        log_message "WARN" "Failed to package ${ext_name_for_pack}, but continuing..."
                    fi
                fi
            fi
        else
            # Try generic PECL build
            _build_php_mod_pecl "$ext_name" "$ext_version" "$php_version" "$php_version_major" "$php_prefix"
            if [[ $? -ne 0 ]]; then
                log_message "ERROR" "Failed to build: ${ext_name} v${ext_version}"
                failedExts+=("$extSpec")
            else
                log_message "INFO" "Successfully built: ${ext_name} v${ext_version}"
                ((successCount++))
                builtPhpVersions[$php_version_major]=1
                # 自动打包扩展
                local ext_name_for_pack="$ext_name"
                [[ "$ext_name" == "phpredis" ]] && ext_name_for_pack="redis"
                if [[ "$ext_name" != "psr" ]]; then
                    log_message "INFO" "📦 Auto-packaging extension: ${ext_name_for_pack} v${ext_version}"
                    if _pack_single_php_extension "$php_version_major" "$ext_name_for_pack" "$ext_version" "$architecture"; then
                        _update_extensions_manifest "$php_version_major" "$ext_name_for_pack" "$ext_version" "$architecture"
                    else
                        log_message "WARN" "Failed to package ${ext_name_for_pack}, but continuing..."
                    fi
                fi
            fi
        fi
    done

    # Runtime verification: check extension loading for each PHP version that had extensions built
    local runtime_failed=false
    for ver in ${(k)builtPhpVersions}; do
        local php_prefix_verify="${SERVBAY_PACKAGE_FULL_PATH}/php/${ver}"
        if ! _verify_php_runtime_extensions "$php_prefix_verify"; then
            runtime_failed=true
        fi
    done

    log_message "INFO" "==================== PHP Extension Build Finished ===================="
    log_message "INFO" "Success: ${successCount}, Failed: ${#failedExts[@]}"

    if [[ ${#failedExts[@]} -gt 0 ]]; then
        log_message "ERROR" "Failed extensions: ${(j:, :)failedExts}"
        exit 1
    fi

    if [[ "$runtime_failed" == "true" ]]; then
        log_message "ERROR" "PHP runtime extension loading verification failed. Please check extension .ini files and .so compatibility."
        exit 1
    fi

    exit 0
fi

# ==============================================================================
# PACK-PHP-EXT MODE (package extensions to dist/php-ext/)
# ==============================================================================
if [[ -n "$packPhpExt" ]]; then
    log_message "INFO" "Running in PHP Extension Packaging Mode"

    # packPhpExt format: php_version:ext_name:ext_version
    # Can be comma-separated for multiple extensions: 8.6:redis:6.2.0,8.6:apcu:5.1.24
    IFS=',' read -A extSpecList <<< "$packPhpExt"

    local packFailedCount=0
    local packSuccessCount=0

    for extSpec in "${extSpecList[@]}"; do
        extSpec="${extSpec// /}"  # Trim whitespace
        if [[ -z "$extSpec" ]]; then
            continue
        fi

        # Parse php_version:ext_name:ext_version format
        IFS=':' read -r php_major ext_name ext_version <<< "$extSpec"

        if [[ -z "$php_major" ]] || [[ -z "$ext_name" ]] || [[ -z "$ext_version" ]]; then
            log_message "ERROR" "Invalid extension spec: ${extSpec}"
            log_message "ERROR" "Expected format: php_version:ext_name:ext_version (e.g., 8.6:redis:6.2.0)"
            ((packFailedCount++))
            continue
        fi

        log_message "INFO" "Packaging: PHP ${php_major} - ${ext_name} v${ext_version} (${architecture})"

        # Package the extension
        if _pack_single_php_extension "$php_major" "$ext_name" "$ext_version" "$architecture"; then
            # Update the manifest
            if _update_extensions_manifest "$php_major" "$ext_name" "$ext_version" "$architecture"; then
                ((packSuccessCount++))
            else
                log_message "ERROR" "Failed to update manifest for ${ext_name}"
                ((packFailedCount++))
            fi
        else
            log_message "ERROR" "Failed to package ${ext_name}"
            ((packFailedCount++))
        fi
    done

    log_message "INFO" "==================== Packaging Summary ===================="
    log_message "INFO" "Successful: ${packSuccessCount}"
    if [[ $packFailedCount -gt 0 ]]; then
        log_message "ERROR" "Failed: ${packFailedCount}"
        exit 1
    fi

    exit 0
fi

# ==============================================================================
# CHECK-HOMEBREW-ONLY MODE (skip building, only check)
# ==============================================================================
if [[ "$checkHomebrewOnly" == "true" ]]; then
    log_message "INFO" "Running in Homebrew Check Only Mode"

    check_result=0

    if [[ -z "$packageNames" ]]; then
        # Check all packages
        log_message "INFO" "Checking all packages for Homebrew dependencies..."
        if ! check_all_homebrew_deps "$strictMode"; then
            check_result=1
        fi
    else
        # Check specific packages
        IFS=',' read -A packageList <<< "$packageNames"
        for packageName in "${packageList[@]}"; do
            packageName="${packageName// /}"
            if [[ -z "$packageName" ]]; then
                continue
            fi

            # Parse package name and version
            local parts=("${(@s:-:)packageName}")
            local numParts=${#parts[@]}
            local PackageName=""
            local Version=""

            for ((i=1; i<numParts; i++)); do
                local testPackageName="${(j:-:)parts[1,$i]}"
                local testVersion="${(j:-:)parts[$((i+1)),$numParts]}"
                local testLine=$(grep -E "^${testPackageName}\s+${testVersion}\s" "$PACKAGES_CONF" 2>/dev/null)
                if [[ -n "$testLine" ]]; then
                    PackageName="$testPackageName"
                    Version="$testVersion"
                    break
                fi
            done

            if [[ -z "$PackageName" ]]; then
                log_message "ERROR" "Package $packageName not found in $PACKAGES_CONF"
                check_result=1
                continue
            fi

            log_message "INFO" "Checking $PackageName-$Version for Homebrew dependencies..."
            if ! check_package_homebrew_deps "$PackageName" "$Version" "$strictMode"; then
                check_result=1
            fi
        done
    fi

    log_message "INFO" "==================== Homebrew Check Finished ===================="
    exit $check_result
fi

# ==============================================================================
# NORMAL BUILD MODE
# ==============================================================================
if [[ -z "$packageNames" ]]; then
    # --- Batch Mode ---
    log_message "INFO" "Running in Batch Mode from ${PACKAGES_CONF}"
    if [[ ! -f "$PACKAGES_CONF" ]]; then
        log_message "ERROR" "packages.conf not found at: ${PACKAGES_CONF}"
        exit 1
    fi

    while IFS= read -r packageLine || [[ -n "$packageLine" ]]; do
        if [[ $packageLine == \#* ]] || [[ -z "$packageLine" ]]; then
            continue
        fi

        # 读取5列格式：NAME VERSION x86_64_FILENAME arm64_FILENAME BUILD_TYPE
        # BUILD_TYPE 目前仅作为文档字段，构建逻辑已在代码中硬编码
        read -r PackageName Version x86_64_Filename arm64_Filename BuildType <<< "$packageLine"
        
        sourceFilename=$([[ "$architecture" == "x86_64" ]] && echo "$x86_64_Filename" || echo "$arm64_Filename")

        if [[ "$sourceFilename" == "-" ]]; then
            log_message "WARN" "Skipping $PackageName-$Version for $architecture (not available)."
            continue
        fi
        
        ext=$(get_extension "$sourceFilename" "$PackageName")
        if [[ $? -ne 0 ]]; then
            continue
        fi
        standardFilename="$PackageName-$Version.$ext"
        packageFullName="$PackageName-$Version"

        log_message "INFO" "----------------- Processing ${packageFullName} -----------------"
        
        downloadUrl=$(get_download_url "$PackageName" "$Version" "$sourceFilename")
        if ! download_source "$downloadUrl" "${SERVBAY_BUILD_SRC_DIR}/${standardFilename}"; then
            log_message "ERROR" "Download failed for ${PackageName}. Skipping."
            continue
        fi

        if ! build_package "$PackageName" "$Version" "$standardFilename" "$installDir"; then
             log_message "ERROR" "❌❌❌ Build failed for ${packageFullName}. Skipping post-build process. ❌❌❌"
             continue
        fi

        if ! post_build_process "$PackageName" "$Version"; then
            log_message "ERROR" "‼️ Post-build process failed for ${packageFullName}."
        else
            cleanup_source_directory "$packageFullName"
        fi
        log_message "INFO" "----------------- Finished ${packageFullName} -----------------\n"

    done < "$PACKAGES_CONF"

    # Show error summary if there were failures
    if [[ -f "$ERROR_FILE" ]] && [[ -s "$ERROR_FILE" ]]; then
        log_message "WARN" "⚠️ Some packages failed to build. See errors in: $ERROR_FILE"
        echo "=== Failed Packages ===" >&2
        grep -oE 'Build failed for [^.]+' "$ERROR_FILE" | sort -u | while read -r line; do
            echo "  ❌ ${line#Build failed for }" >&2
        done
        echo "======================" >&2
    fi

else
    # --- Multiple/Single Package Mode ---
    # Split packageNames by comma
    IFS=',' read -A packageList <<< "$packageNames"

    # Count packages
    local packageCount=${#packageList[@]}
    if [[ $packageCount -eq 1 ]]; then
        log_message "INFO" "Running in Single Package Mode for ${packageList[1]}"
    else
        log_message "INFO" "Running in Multiple Package Mode for $packageCount packages"
    fi

    # Track failed packages
    local failedPackages=()
    local successCount=0

    # Process each package
    for packageName in "${packageList[@]}"; do
        # Trim whitespace
        packageName="${packageName// /}"

        if [[ -z "$packageName" ]]; then
            continue
        fi

        if [[ $packageName != *-* ]]; then
            log_message "ERROR" "Invalid package format: $packageName (expected format: name-version)"
            failedPackages+=("$packageName")
            continue
        fi

        # Try to find the correct split point for package name and version
        # by testing all possible splits against packages.conf
        packageLine=""
        PackageName=""
        Version=""

        # Split the packageName by '-' and try different combinations
        local parts=("${(@s:-:)packageName}")  # Split by '-' into array
        local numParts=${#parts[@]}

        # Try splitting at each possible position (from left to right)
        for ((i=1; i<numParts; i++)); do
            local testPackageName="${(j:-:)parts[1,$i]}"  # Join first i parts with '-'
            local testVersion="${(j:-:)parts[$((i+1)),$numParts]}"  # Join remaining parts with '-'

            local testLine=$(grep -E "^${testPackageName}\s+${testVersion}\s" "$PACKAGES_CONF")
            if [[ -n "$testLine" ]]; then
                PackageName="$testPackageName"
                Version="$testVersion"
                packageLine="$testLine"
                break
            fi
        done

        packageFullName="$packageName"

        if [[ -z "$packageLine" ]]; then
            log_message "ERROR" "❓ Package $packageName not found in $PACKAGES_CONF"
            failedPackages+=("$packageName")
            continue
        fi
        read -r _ _ x86_64_Filename arm64_Filename _ <<< "$packageLine"

        sourceFilename=$([[ "$architecture" == "x86_64" ]] && echo "$x86_64_Filename" || echo "$arm64_Filename")

        if [[ "$sourceFilename" == "-" ]]; then
            log_message "ERROR" "❓ Package $packageName not available for $architecture"
            failedPackages+=("$packageName")
            continue
        fi

        ext=$(get_extension "$sourceFilename" "$PackageName")
        if [[ $? -ne 0 ]]; then
            failedPackages+=("$packageName")
            continue
        fi
        standardFilename="$PackageName-$Version.$ext"

        log_message "INFO" "----------------- Processing ${packageFullName} -----------------"

        downloadUrl=$(get_download_url "$PackageName" "$Version" "$sourceFilename")
        if ! download_source "$downloadUrl" "${SERVBAY_BUILD_SRC_DIR}/${standardFilename}"; then
            log_message "ERROR" "Download failed for ${packageName}. Skipping."
            failedPackages+=("$packageName")
            continue
        fi

        if build_package "$PackageName" "$Version" "$standardFilename" "$installDir"; then
            if post_build_process "$PackageName" "$Version"; then
                cleanup_source_directory "$packageFullName"
                successCount=$((successCount + 1))
                log_message "INFO" "✅ Successfully built ${packageFullName}"
            else
                log_message "ERROR" "‼️ Post-build process failed for ${packageName}."
                failedPackages+=("$packageName")
            fi
        else
            log_message "ERROR" "❌❌❌ Build failed for ${packageName}. ❌❌❌"
            failedPackages+=("$packageName")
        fi
        log_message "INFO" "----------------- Finished ${packageFullName} -----------------\n"
    done

    # Summary
    log_message "INFO" "============== Build Summary =============="
    log_message "INFO" "Total packages: $packageCount"
    log_message "INFO" "Successful: $successCount"
    log_message "INFO" "Failed: ${#failedPackages[@]}"

    if [[ ${#failedPackages[@]} -gt 0 ]]; then
        log_message "ERROR" "Failed packages: ${(j:, :)failedPackages}"
        exit 1
    fi
fi

# ==============================================================================
# POST-BUILD HOMEBREW DEPENDENCY CHECK (if --check-homebrew is enabled)
# ==============================================================================
if [[ "$checkHomebrew" == "true" ]]; then
    log_message "INFO" "============== Post-Build Homebrew Check =============="

    homebrew_check_result=0

    if [[ -z "$packageNames" ]]; then
        # Check all packages
        log_message "INFO" "Checking all built packages for Homebrew dependencies..."
        if ! check_all_homebrew_deps "$strictMode"; then
            homebrew_check_result=1
        fi
    else
        # Check specific packages that were just built
        IFS=',' read -A packageList <<< "$packageNames"
        for packageName in "${packageList[@]}"; do
            packageName="${packageName// /}"
            if [[ -z "$packageName" ]]; then
                continue
            fi

            # Parse package name and version
            local parts=("${(@s:-:)packageName}")
            local numParts=${#parts[@]}
            local PackageName=""
            local Version=""

            for ((i=1; i<numParts; i++)); do
                local testPackageName="${(j:-:)parts[1,$i]}"
                local testVersion="${(j:-:)parts[$((i+1)),$numParts]}"
                local testLine=$(grep -E "^${testPackageName}\s+${testVersion}\s" "$PACKAGES_CONF" 2>/dev/null)
                if [[ -n "$testLine" ]]; then
                    PackageName="$testPackageName"
                    Version="$testVersion"
                    break
                fi
            done

            if [[ -n "$PackageName" ]]; then
                log_message "INFO" "Checking $PackageName-$Version for Homebrew dependencies..."
                if ! check_package_homebrew_deps "$PackageName" "$Version" "$strictMode"; then
                    homebrew_check_result=1
                fi
            fi
        done
    fi

    if [[ $homebrew_check_result -ne 0 ]]; then
        log_message "ERROR" "❌ Homebrew dependency check failed!"
        if [[ "$strictMode" == "strict" ]]; then
            exit 1
        fi
    else
        log_message "INFO" "✅ All packages passed Homebrew dependency check"
    fi
fi

log_message "INFO" "==================== Build Script Finished ===================="
