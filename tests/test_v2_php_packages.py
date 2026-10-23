import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import unittest

import test_build_scripts as legacy_tests


ROOT = Path(__file__).resolve().parents[1]


class PHPCorePackageTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.helper = legacy_tests.BuildScriptTests(methodName="runTest")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_core(self, failure="", core_only=True, custom_make=False, vm_headers=None):
        root = self.root / (failure or "success")
        root.mkdir()
        source = root / "source"
        source.mkdir()
        configure = source / "configure"
        configure.write_text("#!/bin/sh\nexit 0\n")
        configure.chmod(0o755)
        prefix = root / "base/packages/php/8.4"
        if vm_headers is not None:
            host_php = root / "host-php"
            host_php.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(root / "host-called")) + "\nexit 9\n")
            host_php.chmod(0o755)
            zend = source / "Zend"
            zend.mkdir()
            for header in vm_headers:
                (zend / header).write_text("shipped VM header\n")
            makefile = ("PHP = " + str(host_php) + "\n.PHONY: all install\n"
                        ".INTERMEDIATE: generated-vm\nall: generated-vm\ngenerated-vm:\n"
                        '\t@if test ! -z "$(PHP)"; then "$(PHP)"; fi\ninstall:\n\t@true\n')
            configure.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(makefile) + " > Makefile\n")
        script = [f"SERVBAY_BUILD_DIR={shlex.quote(str(root))}",
                  f"SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(root / 'base/packages'))}",
                  f"SERVBAY_ETC_PATH={shlex.quote(str(root / 'base/packages/etc'))}",
                  f"SERVBAY_COMMON_PATH={shlex.quote(str(root / 'base/packages/common'))}",
                  f"SOURCE={shlex.quote(str(source))}", f"FAILURE={shlex.quote(failure)}",
                  "architecture=x86_64", "CPU_NUMBER=2", "typeset -A PACK_INFO PHP_MODS", "typeset -a PACK_FILES",
                  "phpCoreOnly=" + ("true" if core_only else ""),
                  "GTAR_BIN=$(command -v gtar)", "log_message() { :; }",
                  "_get_php_build_config() { PHP_PATCHES=(); PHP_PRE_BUILD_CMDS=(); PHP_POST_BUILD_CMDS=(); PHP_CONFIGURE_ARGS=(); PHP_MODS=([phpredis]=6.3.0); }",
                  "unpack_package() { builtin cd \"$SOURCE\"; }",
                  "prepare_php_source() { [[ $FAILURE != source-prepare ]]; }",
                  "make() { if [[ $FAILURE == host-generator-* ]]; then command make \"$@\" || return 1; fi; if [[ $1 == install ]]; then mkdir -p \"$SERVBAY_PACKAGE_FULL_PATH/php/8.4/bin\"; print -rl -- '#!/bin/sh' 'echo PHP 8.4.27' > \"$SERVBAY_PACKAGE_FULL_PATH/php/8.4/bin/php\"; [[ $FAILURE == cli ]] && print -r -- 'exit 9' >> \"$SERVBAY_PACKAGE_FULL_PATH/php/8.4/bin/php\"; chmod +x \"$SERVBAY_PACKAGE_FULL_PATH/php/8.4/bin/php\"; fi; return 0; }",
                  "_build_php_mod_phpredis() { print -r -- called > third-party-called; }",
                  "_pack_single_php_extension() { return 0; }", "_update_extensions_manifest() { return 0; }",
                  "_verify_php_runtime_extensions() { return 0; }",
                  "check_package_homebrew_deps() { [[ $FAILURE != external ]]; }"]
        for name in ("get_package_packing_info", "core_verify_package", "core_pack_package", "build_php"):
            script.append(name + "() {\n" + self.helper._extract_shell_function(self.text, name) + "\n}")
        if failure == "packing-info":
            script.append("valid_packing_info() {\n" + self.helper._extract_shell_function(self.text, "get_package_packing_info") + "\n}")
            script.append('get_package_packing_info() { valid_packing_info "$@"; return 1; }')
        elif failure == "verify":
            script.append("core_verify_package() { return 1; }")
        elif failure == "pack":
            script.append("core_pack_package() { return 1; }")
        if custom_make:
            script.append("_get_php_build_config() { PHP_PATCHES=(); PHP_PRE_BUILD_CMDS=(); PHP_POST_BUILD_CMDS=('false' 'true'); PHP_CONFIGURE_ARGS=(); PHP_MODS=(); }")
        script += [f"builtin cd {shlex.quote(str(root))}",
                   'if build_php 8.4.27 source.tar.gz ""; then print -r -- handoff > meta.json; exit 0; else exit 1; fi']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        return result, root, prefix

    def test_each_core_handoff_failure_returns_nonzero_without_metadata_or_archive(self):
        for failure in ("source-prepare", "packing-info", "verify", "cli", "external", "pack"):
            with self.subTest(failure=failure):
                result, root, _ = self.run_core(failure)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertFalse((root / "meta.json").exists())
                self.assertFalse((root / "dist/packages/php/php-8.4.27.tar.gz").exists())

    def test_first_custom_make_failure_is_not_hidden_by_a_later_success(self):
        result, root, _ = self.run_core("custom-make", custom_make=True)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((root / "meta.json").exists())
        self.assertFalse((root / "dist/packages/php/php-8.4.27.tar.gz").exists())

    def test_default_core_build_leaves_third_party_modules_for_independent_jobs(self):
        result, root, prefix = self.run_core(core_only=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((root / "meta.json").is_file())
        self.assertTrue((root / "dist/packages/php/php-8.4.27.tar.gz").is_file())
        self.assertTrue((prefix / "bin/php").is_file())
        self.assertFalse((root / "third-party-called").exists())
        with tarfile.open(root / "dist/packages/php/php-8.4.27.tar.gz") as archive:
            self.assertEqual(b"8.4.27\n", archive.extractfile("php/8.4/VERSION").read())

    def test_shipped_vm_headers_do_not_run_the_host_php_but_missing_headers_keep_generation(self):
        for supplied in (True, False):
            with self.subTest(shipped_headers=supplied):
                headers = ("zend_vm_execute.h", "zend_vm_opcodes.h") if supplied else ("zend_vm_execute.h",)
                result, root, _ = self.run_core("host-generator-" + str(supplied), vm_headers=headers)
                if supplied:
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertFalse((root / "host-called").exists())
                    self.assertTrue((root / "meta.json").is_file())
                else:
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertTrue((root / "host-called").is_file())
                    self.assertFalse((root / "meta.json").exists())

    def test_php_configurations_bind_only_runtime_libpq(self):
        body = self.helper._extract_shell_function(self.text, "_get_php_build_config")
        common = self.root / "runtime-common"
        for series in ("5.3", "5.4", "5.5", "5.6", "7.0", "7.1", "7.2", "7.3", "7.4", "8.0", "8.1", "8.2", "8.3", "8.4", "8.5", "8.6"):
            with self.subTest(series=series):
                script = f'''SERVBAY_COMMON_PATH={shlex.quote(str(common))}
SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(self.root / 'packages'))}
OPENSSL_VERSION=3.5
architecture=x86_64
typeset -A PHP_MODS
_get_php_build_config() {{
{body}
}}
_get_php_build_config {shlex.quote(series)} || exit 1
print -rl -- "${{PHP_CONFIGURE_ARGS[@]}}"
'''
                result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                options = result.stdout.splitlines()
                selected = [value for value in options if value.startswith(("--with-pgsql", "--with-pdo-pgsql"))]
                # PHP_SETUP_PGSQL arrives in8.4; through8.3 both real m4
                # files still search pg_config and need the explicit prefix.
                expected = ["--with-pgsql", "--with-pdo-pgsql"] if tuple(map(int, series.split('.'))) >= (8, 4) else [f"--with-pgsql={common}", f"--with-pdo-pgsql={common}"]
                self.assertEqual(expected, selected)
                self.assertFalse(any("postgresql/current" in value for value in options))

    def test_php86_mounts_the_xtoffsetof_core_patch_while_other_series_do_not(self):
        # PHP 8.6 移除了 Zend/zend_portability.h 中的 XtOffsetOf 宏，而大量 PECL
        # 扩展仍在使用；v1 (32c50ef) 通过核心补丁一次性恢复，随 make install 分发。
        body = self.helper._extract_shell_function(self.text, "_get_php_build_config")
        common = self.root / "runtime-common"
        source = self.root / "php-src"
        header = source / "Zend" / "zend_portability.h"
        header.parent.mkdir(parents=True)
        # 仿照真实 zend_portability.h 的行位（hunk 落在第 58 行）构造夹具。
        header.write_text("".join(f"/* filler {n} */\n" for n in range(1, 58))
                          + "\n#include <stdarg.h>\n#include <stddef.h>\n\n#ifdef HAVE_DLFCN_H\n# include <dlfcn.h>\n#endif\n")
        for series in ("8.5", "8.6"):
            with self.subTest(series=series):
                script = f'''SERVBAY_COMMON_PATH={shlex.quote(str(common))}
SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(self.root / 'packages'))}
OPENSSL_VERSION=3.5
architecture=x86_64
typeset -A PHP_MODS
_get_php_build_config() {{
{body}
}}
_get_php_build_config {shlex.quote(series)} || exit 1
print -rl -- "${{PHP_PATCHES[@]}}"
'''
                result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                patches = result.stdout.splitlines()
                if series == "8.6":
                    self.assertIn("php8.6/zend_portability-xtoffsetof.patch:p0", patches)
                else:
                    self.assertNotIn("php8.6/zend_portability-xtoffsetof.patch:p0", patches)
        patch_file = ROOT / "runtime-v2/patch/php8.6/zend_portability-xtoffsetof.patch"
        self.assertTrue(patch_file.is_file(), "php8.6 XtOffsetOf core patch must ship")
        applied = subprocess.run(["/usr/bin/patch", "--ignore-whitespace", "-N", "-p0", "-i", str(patch_file)],
                                 cwd=source, capture_output=True, text=True)
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        self.assertIn("# define XtOffsetOf(s_type, field) offsetof(s_type, field)", header.read_text())

    def test_each_pre_80_series_carries_its_own_gd_configure_runtime_patch(self):
        # 5.6-7.4 的 configure 里 GD 构建测试编译 char foobar() 小程序并**运行**它，
        # 当前工具链下该二进制 entryoff=0，一启动就 SIGILL，configure 误判失败。
        # 修复为 link-only（v1 09db682/6dcee8a）。各 release 的 hunk 行号不同：
        # 复用别的版本的补丁会静默改写另一处 ac_fn_c_try_run 而 GD 检查原样留下，
        # 所以六个文件必须各就各位、且内容互不相同（禁止合并成一份）。
        body = self.helper._extract_shell_function(self.text, "_get_php_build_config")
        for series in ("5.6", "7.0", "7.1", "7.2", "7.3", "7.4"):
            branch = body.split(f'"{series}")', 1)[1].split(';;', 1)[0]
            with self.subTest(series=series):
                self.assertIn(f'"php{series}/gd-configure-runtime-test.patch:p1"', branch)
                patch_file = ROOT / "runtime-v2" / "patch" / f"php{series}" / "gd-configure-runtime-test.patch"
                self.assertTrue(patch_file.is_file(), f"php {series} GD patch must ship")
                self.assertIn("if ac_fn_c_try_link \"$LINENO\"; then :", patch_file.read_text())
        digests = {
            series: (ROOT / "runtime-v2" / "patch" / f"php{series}" / "gd-configure-runtime-test.patch").read_bytes()
            for series in ("5.6", "7.0", "7.1", "7.2", "7.3", "7.4")
        }
        self.assertEqual(len(set(digests.values())), len(digests),
                         "each release needs its own hunk line numbers; do not share one file")

    def test_php56_and_70_find_the_renamed_tidy_header(self):
        # libtidy 5 把 buffio.h 改名为 tidybuffio.h（本机 ServBay common/include 只装
        # tidybuffio.h，没有兼容头）。7.1+ 由 configure 探测，5.6/7.0 无条件 include
        # 旧名而直接失败；其 configure 不探测新名，补丁用 __has_include 兜底
        # （v1 f21be82）。
        body = self.helper._extract_shell_function(self.text, "_get_php_build_config")
        for major in ("5.6", "7.0"):
            with self.subTest(major=major):
                branch = body.split(f'"{major}")', 1)[1].split(';;', 1)[0]
                self.assertIn(f'"php{major}/tidy-buffio-header.patch:p1"', branch)
                patch_file = ROOT / "runtime-v2" / "patch" / f"php{major}" / "tidy-buffio-header.patch"
                self.assertTrue(patch_file.is_file())
                content = patch_file.read_text()
                self.assertIn("__has_include(<tidybuffio.h>)", content)
                self.assertIn("#include \"buffio.h\"", content)

    def test_core_and_extension_cli_initialize_the_same_isolated_sdk_contract(self):
        for extension_only in (False, True):
            with self.subTest(extension_only=extension_only):
                root = self.root / ("extension-cli" if extension_only else "core-cli")
                profile = root / "profile"
                script = profile / "scripts/build_package.sh"
                script.parent.mkdir(parents=True)
                (profile / "ci").symlink_to(ROOT / "runtime-v2/ci", target_is_directory=True)
                (profile / "config").mkdir()
                (profile / "config/packages.conf").write_text("php\t8.4.27\tphp-8.4.27.tar.gz\tphp-8.4.27.tar.gz\tsource\n")
                base = root / "base"
                common = base / "packages/common"
                for directory in (common / "static/lib", common / "static/include", common / "lib", common / "include"):
                    directory.mkdir(parents=True, exist_ok=True)
                (base / ".bootstrap-v2.json").write_text("{}\n")
                php_bin = base / "packages/php/8.4/bin"
                php_bin.mkdir(parents=True)
                config = php_bin / "php-config"
                config.write_text("#!/bin/sh\necho 8.4.27\n")
                config.chmod(0o755)
                capture = root / "environment"
                replacement = '''
print -rl -- "$SDK_PATH" "$CFLAGS" "$LDFLAGS" "$CPPFLAGS" "$CXXFLAGS" "$PKG_CONFIG_PATH" "$PKG_CONFIG_LIBDIR" > "$ENV_CAPTURE"
exit 0
'''
                text = self.text
                for name in ("build_php", "_build_php_mod_phpredis"):
                    old = self.helper._extract_shell_function(text, name)
                    self.assertEqual(1, text.count(name + "() {\n" + old + "}"))
                    text = text.replace(name + "() {\n" + old + "}", name + "() {\n" + replacement + "\n}")
                old = self.helper._extract_shell_function(text, "download_source")
                self.assertEqual(1, text.count("download_source() {\n" + old + "}"))
                text = text.replace("download_source() {\n" + old + "}", "download_source() { return 0; }")
                script.write_text(text)
                env = dict(os.environ, HOME=str(root / "home"), SERVBAY_BASE_FULL_PATH=str(base),
                           ENV_CAPTURE=str(capture), PKG_CONFIG_PATH="/tmp/foreign/pc", PKG_CONFIG_LIBDIR="/tmp/foreign/lib")
                args = ["/bin/zsh", str(script), "-a", "x86_64"]
                args += ["--php-ext", "8.4:phpredis:6.3.0"] if extension_only else ["-p", "php-8.4.27"]
                result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                sdk, cflags, ldflags, cppflags, cxxflags, pc_path, pc_libdir = capture.read_text().splitlines()
                self.assertTrue(Path(sdk).is_dir(), sdk)
                for flags in (cflags, ldflags, cxxflags):
                    self.assertIn("-isysroot " + sdk, flags)
                self.assertIn(str(common / "include"), cppflags)
                self.assertEqual(pc_path, pc_libdir)
                self.assertIn(str(common / "lib/pkgconfig"), pc_path)
                self.assertNotIn("/tmp/foreign", pc_path)

    def test_fresh_build_environment_runs_shipped_imagemagick_config_scripts(self):
        fixtures = json.loads((ROOT / "tests/fixtures/imagemagick-config-scripts.json").read_text())["files"]
        base = self.root / "base"
        common = base / "packages/common"
        (common / "bin").mkdir(parents=True)
        pc = common / "lib/pkgconfig"
        pc.mkdir(parents=True)
        original_scripts = {}
        for name, fixture in fixtures.items():
            self.assertEqual(fixture["sha256"], hashlib.sha256(fixture["script"].encode()).hexdigest())
            path = common / "bin" / name
            path.write_text(fixture["script"].replace("/Applications/ServBay", str(base)))
            path.chmod(0o755)
            original_scripts[name] = path.read_bytes()
            library = name.removesuffix("-config")
            (pc / (library + "-7.Q16HDRI.pc")).write_text(
                f"prefix={common}\nName: {library}\nDescription: isolated test headers and library\n"
                f"Version: 7.1.2\nCflags: -I${{prefix}}/include/ImageMagick-7\n"
                f"Libs: -L${{prefix}}/lib -l{library}-7.Q16HDRI\n")
        script = "\n".join([
            "export SERVBAY_RECIPE_LIBRARY_ONLY=1",
            "source " + shlex.quote(str(ROOT / "runtime-v2/scripts/build_package.sh")) + " || exit 1",
            "unset SERVBAY_RECIPE_LIBRARY_ONLY", "architecture=arm64", 'BUILD_CPU_ARCH="-arch arm64"',
            'BUILD_MACOS_TARGET="-target arm64-apple-macos12.00"', "init_logging || exit 1",
            "setup_package_build_environment php 8.4.27 || exit 1",
            # A second consumer must not replace the tool with a self-referential link.
            "setup_package_build_environment postgresql 18.6 || exit 1",
            'failed=0',
            *['"$SERVBAY_COMMON_PATH/bin/' + name + '" --cflags --libs || failed=1' for name in fixtures],
            'exit "$failed"',
        ])
        result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, SERVBAY_BASE_FULL_PATH=str(base),
                                         SERVBAY_BUILD_ROOT=str(self.root / "build")))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for name, original in original_scripts.items():
            self.assertEqual(original, (common / "bin" / name).read_bytes())
            self.assertIn("-I" + str(common / "include/ImageMagick-7"), result.stdout, result.stderr)
            self.assertIn("-l" + name.removesuffix("-config") + "-7.Q16HDRI", result.stdout, result.stderr)
        alias = base / "packages/bin/pkg-config"
        self.assertTrue(alias.is_symlink())
        self.assertTrue(alias.resolve().is_file())

    def test_fresh_build_environment_recreates_the_base_baked_compiler_wrapper(self):
        # base 烘焙的 *-config（apr-1-config/apu-1-config/MagickCore-config…）把
        # Utility runtime 构建机的编译器 wrapper 绝对路径写死为 --cc 输出
        # （RUNTIME_TOOL_DIR=$RUNNER_TEMP/runtime-v2-tools）。包编译 runner 上该
        # 路径不存在，任何咨询 --cc 的 configure（如 httpd 的 APR-util 探测）
        # 都会报 "C compiler cannot create executables"。环境初始化必须在
        # runner 上按同一契约重建 wrapper。
        base = self.root / "base"
        common = base / "packages/common/bin"
        common.mkdir(parents=True)
        runner_temp = self.root / "runner-temp"
        (runner_temp / "runtime-v2-tools").mkdir(parents=True)
        baked_cc = runner_temp / "runtime-v2-tools" / "cc"
        apr_config = common / "apu-1-config"
        apr_config.write_text("#!/bin/sh\n" +
                              'case "$1" in\n' +
                              f'  --cc) echo "{baked_cc}" ;;\n' +
                              '  --version) echo "APR-util 1.6.3" ;;\n' +
                              '  *) echo "usage" >&2; exit 1 ;;\n' +
                              "esac\n")
        apr_config.chmod(0o755)
        script = "\n".join([
            "export SERVBAY_RECIPE_LIBRARY_ONLY=1",
            "source " + shlex.quote(str(ROOT / "runtime-v2/scripts/build_package.sh")) + " || exit 1",
            "unset SERVBAY_RECIPE_LIBRARY_ONLY", "architecture=arm64", 'BUILD_CPU_ARCH="-arch arm64"',
            'BUILD_MACOS_TARGET="-target arm64-apple-macos12.00"', "init_logging || exit 1",
            "setup_package_build_environment httpd 2.4.69 || exit 1",
            # --cc 输出必须是现在真实可执行的编译器（config 消费方会直接执行它）。
            f'cc=$("{apr_config}" --cc) || exit 1',
            '[[ -x "$cc" ]] || { echo "baked --cc is not executable: $cc"; exit 1; }',
            # 真实消费方（configure/make）会带上环境 CFLAGS（含 -isysroot）；
            # wrapper 本体与 runtime 侧契约一致，只做编译器转发不注入 SDK 标志。
            # ${=CFLAGS}：zsh 显式分词，模拟 sh 语义下的 CFLAGS 展开。
            'echo "int main(void){return 0;}" | "$cc" -x c ${=CFLAGS} -o /dev/null - || exit 1',
            "exit 0",
        ])
        result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, SERVBAY_BASE_FULL_PATH=str(base),
                                         SERVBAY_BUILD_ROOT=str(self.root / "build"),
                                         RUNNER_TEMP=str(runner_temp)))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(baked_cc.is_symlink() or baked_cc.is_file())
        self.assertIn("clang", os.path.realpath(baked_cc))


    def test_only_explicit_security_releases_allow_a_different_primary_source_version(self):
        spec = importlib.util.spec_from_file_location("php_package_meta", ROOT / "runtime/ci/package_meta.py")
        metadata = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(metadata)
        source = self.root / "php-8.1.34.tar.gz"
        source.write_bytes(b"actual upstream archive, independent from built package")
        producer = dict(commit="a" * 40, run_id="123", run_attempt=2)
        base = dict(version="2.0.1", runtime_sha256="b" * 64, devlib_sha256="c" * 64)
        for name, version, upstream, allowed in (
                ("php", "8.1.34.1", "8.1.34", True),
                ("php", "8.1.34.1", "8.1.33", False),
                ("php", "8.1.34.9", "8.1.34", False),
                ("php", "8.1.34.9", "8.1.34.9", False),
                ("php", "8.1.34.1", "8.1.34.1", False),
                ("php", "8.4.27", "8.4.26", False),
                ("nginx", "1.31.7", "1.31.6", False)):
            with self.subTest(name=name, version=version, upstream=upstream):
                package = self.root / f"{name}-{version}.tar.gz"
                package.write_bytes(b"actual built package " + version.encode())
                identity = dict(runtime_version="v2", os="macos", kind="package", name=name,
                                version=version, arch="x86_64", build_revision=0)
                if allowed:
                    meta = metadata.create_meta(package, identity, producer, base, "12.0", dict(version=upstream, path=source))
                    self.assertEqual(version, meta["version"])
                    self.assertEqual(upstream, meta["source"]["version"])
                    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), meta["source"]["sha256"])
                    self.assertNotEqual(meta["source"]["sha256"], meta["sha256"])
                else:
                    with self.assertRaises(ValueError):
                        metadata.create_meta(package, identity, producer, base, "12.0", dict(version=upstream, path=source))

    def test_security_source_map_rejects_modified_patch_bytes(self):
        helper = self.source_helper()
        actual = json.loads((ROOT / "runtime-v2/config/php-backports.json").read_text())
        for version, release in actual["releases"].items():
            with self.subTest(version=version):
                checked = helper.backport_release(version)
                self.assertEqual(release, checked)
                self.assertEqual(release["source_version"], helper.source_version(version))
        profile = self.root / "isolated-profile"
        (profile / "config").mkdir(parents=True)
        patch = profile / "patch/security.patch"
        patch.parent.mkdir()
        patch.write_bytes(b"the approved security patch\n")
        expected = hashlib.sha256(patch.read_bytes()).hexdigest()
        (profile / "config/php-backports.json").write_text(json.dumps({
            "schema_version": 1,
            "releases": {"8.1.34.1": {"source_version": "8.1.34", "patch": "security.patch",
                                        "patch_sha256": expected}},
        }))
        helper.PROFILE = profile
        self.assertEqual("8.1.34", helper.source_version("8.1.34.1"))
        patch.write_bytes(b"modified security patch with the same package label\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            helper.source_version("8.1.34.1")

    def test_security_release_rejects_an_archive_with_a_different_actual_source_header(self):
        helper = self.source_helper()
        source = self.root / "wrong-source"
        (source / "main").mkdir(parents=True)
        (source / "main/php_version.h").write_text('#define PHP_VERSION "8.1.33"\n')
        with self.assertRaisesRegex(ValueError, "Actual PHP source header"):
            helper.prepare_source(source, "8.1.34.1", self.root / "build", self.root / "common")

    def test_backport_source_cache_keeps_its_package_identity_but_reports_real_upstream(self):
        spec = importlib.util.spec_from_file_location("php_build_profile", ROOT / "runtime/ci/build_profile.py")
        profile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(profile)
        conf = self.root / "packages.conf"
        conf.write_text("php\t8.1.34.1\tphp-8.1.34.tar.gz\tphp-8.1.34.tar.gz\tsource\n")
        path = self.root / "src/php-8.1.34.1.tar.gz"
        path.parent.mkdir()
        with tarfile.open(path, "w:gz") as archive:
            contents = b'#define PHP_VERSION "8.1.34"\n'
            info = tarfile.TarInfo("php-8.1.34/main/php_version.h")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
        result = profile.find_source(self.root, "php", "8.1.34.1", "x86_64", conf)
        self.assertEqual(str(path), result["path"])
        self.assertEqual("8.1.34", result["version"])

    def source_helper(self):
        spec = importlib.util.spec_from_file_location("tested_php_source", ROOT / "runtime-v2/ci/php_source.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_local_version_reaches_generated_header_and_php_config_without_changing_api(self):
        helper = self.source_helper()
        for version, version_id, autoconf in (("5.3.29", 50329, "configure.in"), ("8.1.34", 80134, "configure.ac")):
            with self.subTest(version=version):
                source = self.root / version
                (source / "main").mkdir(parents=True)
                (source / "Zend").mkdir()
                (source / "main/php_version.h").write_text(f'#define PHP_VERSION "{version}"\n#define PHP_EXTRA_VERSION ""\n#define PHP_VERSION_ID {version_id}\n')
                api = source / "Zend/zend_modules.h"
                api.write_text("#define ZEND_MODULE_API_NO 20210902\n")
                original_api = api.read_bytes()
                major, minor, release = version.split('.')
                (source / autoconf).write_text(f'''AC_INIT([PHP],[{version}],[bugs])
PHP_MAJOR_VERSION={major}
PHP_MINOR_VERSION={minor}
PHP_RELEASE_VERSION={release}
''' + ('PHP_EXTRA_VERSION=""\n' if autoconf == "configure.in" else 'PHP_EXTRA_VERSION=[$]4\n') + '''PHP_VERSION="$PHP_MAJOR_VERSION.$PHP_MINOR_VERSION.$PHP_RELEASE_VERSION$PHP_EXTRA_VERSION"
printf '%s\\n' "$PHP_VERSION" > php-config-from-m4
AC_OUTPUT
''')
                configure = source / "configure"
                configure.write_text(f'''#!/bin/sh
PHP_MAJOR_VERSION={major}
PHP_MINOR_VERSION={minor}
PHP_RELEASE_VERSION={release}
PHP_EXTRA_VERSION=""
PHP_VERSION="$PHP_MAJOR_VERSION.$PHP_MINOR_VERSION.$PHP_RELEASE_VERSION$PHP_EXTRA_VERSION"
printf '%s\\n' "$PHP_VERSION" > php-config-version
''')
                configure.chmod(0o755)
                helper.rewrite_local_version(source, version + ".1", version)
                result = subprocess.run([str(configure)], cwd=source, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(version + ".1\n", (source / "php-config-version").read_text())
                regenerated = source / "regenerated-configure"
                subprocess.run(["autoconf", "-o", str(regenerated), str(source / autoconf)],
                               cwd=source, check=True, capture_output=True)
                subprocess.run(["/bin/sh", str(regenerated)], cwd=source, check=True, capture_output=True)
                self.assertEqual(version + ".1\n", (source / "php-config-from-m4").read_text())
                program = source / "version.c"
                program.write_text('#include "main/php_version.h"\n#include <stdio.h>\nint main(void) { printf("%s %d %s\\n", PHP_VERSION, PHP_VERSION_ID, PHP_EXTRA_VERSION); }\n')
                subprocess.run(["/usr/bin/clang", str(program), "-o", str(source / "version")], check=True, capture_output=True)
                observed = subprocess.run([str(source / "version")], check=True, capture_output=True, text=True).stdout
                self.assertEqual(f"{version}.1 {version_id} .1\n", observed)
                self.assertEqual(original_api, api.read_bytes())

    def test_each_legacy_pgsql_macro_uses_selected_dylib_without_calling_external_pg_config(self):
        helper = self.source_helper()
        source = self.root / "legacy"
        common = self.root / "selected-runtime"
        foreign = self.root / "foreign"
        (common / "include").mkdir(parents=True)
        (common / "lib").mkdir()
        (common / "include/libpq-fe.h").write_text("real selected header fixture\n")
        (common / "lib/libpq.dylib").write_bytes(b"selected Mach-O library fixture")
        foreign.mkdir()
        external = foreign / "pg_config"
        called = self.root / "external-called"
        external.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(called))}\necho foreign\n')
        external.chmod(0o755)

        def macro(option):
            return f'''unset PGSQL_INCLUDE PGSQL_LIBDIR
PG_CONFIG="$EXTERNAL/pg_config"
for i in ${option} ${option}/bin $EXTERNAL; do
  if test -x $i/pg_config; then PG_CONFIG="$i/pg_config"; break; fi
done
if test -n "$PG_CONFIG"; then
  PGSQL_INCLUDE=`$PG_CONFIG --includedir`
  PGSQL_LIBDIR=`$PG_CONFIG --libdir`
else
  i="${option}"; j=lib
  PGSQL_INCLUDE="$i/include"
  if test -f "$i/$j/libpq.so" || test -f "$i/$j/libpq.a"; then PGSQL_LIBDIR="$i/$j"; fi
fi
test -n "$PGSQL_LIBDIR" || exit 1
printf '%s|%s\\n' "$PGSQL_INCLUDE" "$PGSQL_LIBDIR"
'''

        for name, body in (("ext/pgsql/config.m4", macro("PHP_PGSQL")),
                           ("ext/pdo_pgsql/config.m4", macro("PHP_PDO_PGSQL")),
                           ("configure", macro("PHP_PDO_PGSQL") + macro("PHP_PGSQL"))):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        helper.isolate_legacy_libpq(source)
        for name in ("ext/pgsql/config.m4", "ext/pdo_pgsql/config.m4", "configure"):
            with self.subTest(name=name):
                env = dict(os.environ, PHP_PGSQL=str(common), PHP_PDO_PGSQL=str(common), EXTERNAL=str(foreign))
                result = subprocess.run(["/bin/sh", str(source / name)], env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue(result.stdout.splitlines())
                self.assertEqual({f"{common}/include|{common}/lib"}, set(result.stdout.splitlines()))
                self.assertFalse(called.exists())

    def test_upstream_refresh_cannot_replace_a_patched_v2_series_with_its_older_tag(self):
        spec = importlib.util.spec_from_file_location("php_refresh", ROOT / "runtime/update_packages.py")
        updater_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater_module)
        conf = self.root / "security-packages.conf"
        conf.write_text("php\t5.3.29.1\tphp-5.3.29.tar.gz\tphp-5.3.29.tar.gz\tsource\n"
                        "php\t7.0.33.1\tphp-7.0.33.tar.gz\tphp-7.0.33.tar.gz\tsource\n"
                        "php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\tsource\n")
        original = conf.read_bytes()
        updater = updater_module.PackageUpdater(str(conf), runtime_version="v2")
        updater.update_conf_file({"php": {"5.3": "5.3.29", "7.3": "7.3.33"}})
        self.assertEqual(original, conf.read_bytes())
        self.assertEqual([], updater.updated_records)

    def test_v2_tracked_series_include_the_current_v1_security_and_release_updates(self):
        spec = importlib.util.spec_from_file_location("aligned_package_meta", ROOT / "runtime/ci/package_meta.py")
        metadata = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(metadata)
        spec = importlib.util.spec_from_file_location("aligned_package_updater", ROOT / "runtime/update_packages.py")
        updater_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater_module)
        updater = updater_module.PackageUpdater(str(ROOT / "runtime-v2/config/packages.conf"), runtime_version="v2")
        tracked = {}
        selected = {"php", "python", "node", "mysql", "cloudflared", "ollama"}
        for row in updater.active_package_records():
            if row["name"] not in selected:
                continue
            key = row["name"], metadata.package_metadata(row["name"], row["version"])["major"]
            tracked.setdefault(key, []).append(row["version"])
        for line in (ROOT / "runtime/packages.conf").read_text().splitlines():
            fields = line.split()
            if len(fields) != 4 or fields[0] not in selected:
                continue
            name, version = fields[:2]
            key = name, metadata.package_metadata(name, version)["major"]
            if key not in tracked:
                continue  # V2 support floor remains its own profile contract.
            with self.subTest(name=name, version=version):
                actual = max(tracked[key], key=updater.version_to_tuple)
                self.assertGreaterEqual(updater.compare_versions(actual, version), 0)

    def test_four_segment_versions_keep_the_same_php_compiler_compatibility_flags(self):
        body = self.helper._extract_shell_function(self.text, "setup_package_build_environment")
        for version in ("8.0.30", "8.0.30.1"):
            with self.subTest(version=version):
                work = self.root / version
                work.mkdir()
                script = f'''SERVBAY_BUILD_DIR={shlex.quote(str(work))}
PROJECT_ROOT={shlex.quote(str(ROOT / 'runtime-v2'))}
SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(work / 'packages'))}
SERVBAY_BIN_PATH="$SERVBAY_PACKAGE_FULL_PATH/bin"
SERVBAY_COMMON_PATH="$SERVBAY_PACKAGE_FULL_PATH/common"
SERVBAY_COMMON_LIB_PATH="$SERVBAY_COMMON_PATH/lib"
SERVBAY_COMMON_INCLUDE_PATH="$SERVBAY_COMMON_PATH/include"
SERVBAY_STATIC_LIBS_PATH="$SERVBAY_COMMON_PATH/static"
SERVBAY_STATIC_LIB_PATH="$SERVBAY_STATIC_LIBS_PATH/lib"
SERVBAY_STATIC_INCLUDE_PATH="$SERVBAY_STATIC_LIBS_PATH/include"
OPENSSL_VERSION=3.5
BUILD_OS_MIN_VERSION=12.0
architecture=x86_64
log_message() {{ :; }}
setup_package_build_environment() {{
{body}
}}
setup_package_build_environment php {shlex.quote(version)} || exit 1
print -r -- "$CXXFLAGS"
'''
                result = subprocess.run(["/bin/zsh", "-c", script], cwd=work, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("-std=c++17", shlex.split(result.stdout))


if __name__ == "__main__":
    unittest.main()
