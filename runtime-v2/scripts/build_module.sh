#!/bin/zsh
# Reuse the package recipes while installing only into an owned module stage.
setopt PIPE_FAIL
[[ $# == 8 ]] || { print -u2 'Expected kind recipe version host-prefix host-version host-series arch stage'; exit 2; }
kind=$1 recipe=$2 version=$3 host_prefix=$4 host_version=$5 host_series=$6 architecture=$7
export MODULE_INSTALL_ROOT=$8
[[ "$MODULE_INSTALL_ROOT" == /* && -d "$MODULE_INSTALL_ROOT" ]] || exit 2
export SERVBAY_RECIPE_LIBRARY_ONLY=1
source "${0:A:h}/build_package.sh" || exit 1
unset SERVBAY_RECIPE_LIBRARY_ONLY
CPU_NUMBER=${SERVBAY_MODULE_JOBS:-2}
[[ "$CPU_NUMBER" == <1-> ]] || exit 2
BUILD_CPU_ARCH="-arch $architecture"
BUILD_MACOS_TARGET="-target ${architecture}-apple-macos${BUILD_OS_MIN_VERSION}"
init_logging || exit 1
setup_build_env || exit 1
case "$architecture" in x86_64|arm64) ;; *) exit 2 ;; esac
case "$kind" in
    php-extension)
        [[ "$host_prefix" == "$SERVBAY_PACKAGE_FULL_PATH/php/$host_series" ]] || exit 2
        setup_package_build_environment php "$host_version" || exit 1
        SERVBAY_ETC_PATH="$MODULE_INSTALL_ROOT/etc"
        SERVBAY_LOG_PATH="$MODULE_INSTALL_ROOT/log"
        mkdir -p "$SERVBAY_ETC_PATH/php/$host_series/conf.d" || exit 1
        function_name="_build_php_mod_${recipe//-/_}"
        whence -f "$function_name" >/dev/null || exit 2
        bundle_psr="${SERVBAY_PSR_BUNDLE_VERSION:-}"
        if [[ "$recipe" == phalcon && "$version" == 4.* && "$host_series" == (7.2|7.3|7.4) ]]; then
            [[ -n "$bundle_psr" ]] || { print -u2 'Phalcon4 needs its explicit PSR source version'; exit 1; }
            _build_php_mod_psr "$bundle_psr" "$host_version" "$host_series" "$host_prefix" || exit 1
            export CPPFLAGS="$CPPFLAGS -I$MODULE_INSTALL_ROOT$host_prefix/include/php"
        elif [[ -n "$bundle_psr" ]]; then
            print -u2 'This recipe/host does not own a bundled PSR module'; exit 1
        fi
        "$function_name" "$version" "$host_version" "$host_series" "$host_prefix" || exit 1
        extension_dir=$(_php_module_extension_dir "$host_prefix/bin/php-config") || exit 1
        [[ -d "$extension_dir" ]] || { print -u2 'No standalone module was installed'; exit 1; }
        [[ -z "$bundle_psr" || ( -f "$extension_dir/psr.so" && -f "$extension_dir/phalcon.so" ) ]] || exit 1
        if [[ "$recipe" == psr ]]; then
            cp "$PROJECT_ROOT/conf/php/psr.ini" "$SERVBAY_ETC_PATH/php/$host_series/conf.d/" || exit 1
        fi
        check_homebrew_dependencies "$extension_dir" strict || exit 1
        ;;
    postgresql-extension)
        [[ "$host_prefix" == "$SERVBAY_PACKAGE_FULL_PATH/postgresql/$host_series" ]] || exit 2
        setup_package_build_environment postgresql "$host_version" || exit 1
        export DESTDIR="$MODULE_INSTALL_ROOT"
        function_name="_build_pgsql_mod_${recipe//-/_}"
        whence -f "$function_name" >/dev/null || exit 2
        "$function_name" "$version" "$host_version" "$host_series" "$host_prefix" || exit 1
        actual_bindir=$("$host_prefix/bin/pg_config" --bindir) || exit 1
        [[ "${actual_bindir:A}" == "${host_prefix:A}/bin" ]] || exit 1
        staged_prefix="$DESTDIR${actual_bindir:h}"
        [[ -d "$staged_prefix" ]] || exit 1
        check_homebrew_dependencies "$staged_prefix" strict || exit 1
        ;;
    *) exit 2 ;;
esac
