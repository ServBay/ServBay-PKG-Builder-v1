import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_build_scripts as legacy_tests

ROOT = Path(__file__).resolve().parents[1]


class MySqlCmakeArgsTests(unittest.TestCase):
    """A4/A5（v1 bf1d8e2/16fab29/dee252e、1c73242）的 portback 守卫。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def run_cmake_args(self, version):
        body = self.extract(self.text, "_get_mysql_cmake_args")
        script = f'''SERVBAY_COMMON_PATH={shlex.quote(str(self.root / "common"))}
SERVBAY_COMMON_INCLUDE_PATH="$SERVBAY_COMMON_PATH/include"
SERVBAY_DB_PATH={shlex.quote(str(self.root / "db"))}
SERVBAY_ETC_PATH={shlex.quote(str(self.root / "etc"))}
SERVBAY_TMP_PATH={shlex.quote(str(self.root / "tmp"))}
OPENSSL_VERSION=3.5
BUILD_OS_MIN_VERSION=12.0
BUILD_CPU_ARCH="-arch x86_64"
BUILD_MACOS_TARGET="-target x86_64-apple-macos12.00"
typeset -a CMAKE_ARGS
_get_mysql_cmake_args() {{
{body}
}}
_get_mysql_cmake_args {shlex.quote(version)} /prefix || exit 1
print -rl -- "${{CMAKE_ARGS[@]}}"
'''
        result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.splitlines()

    def test_mysql_openssl_linked_series_pin_openssl_root_dir(self):
        # MySQL Router 的 harness_tls 自己跑 find_package(OpenSSL)；不 pin
        # OPENSSL_ROOT_DIR 会抓到 CI runner 的 brew openssl@3（3.x 头 + base 库
        # -> OSSL_DECODER_*/OSSL_PARAM_* 未定义）。v1 bf1d8e2。
        # 8.0-8.3 无 harness_tls、v1 未钉，但按用户裁决做防御性一致（WITH_SSL 与
        # FindOpenSSL 恒指同一 base OpenSSL）；5.6 有历史钉、5.5 bundled、5.7 无。
        for version, expected in (("8.4.6", True), ("9.4.0", True), ("9.7.0", True),
                                  ("8.0.44", True), ("8.3.0", True), ("5.6.50", True),
                                  ("5.7.44", False), ("5.5.62", False)):
            with self.subTest(version=version):
                args = self.run_cmake_args(version)
                pin = [arg for arg in args if arg.startswith("-DOPENSSL_ROOT_DIR=")]
                if expected:
                    self.assertEqual(
                        ["-DOPENSSL_ROOT_DIR=" + str(self.root / "common") + "/openssl/3.5"], pin)
                else:
                    self.assertEqual([], pin)

    def test_mysql97_disables_both_telemetry_flavors_and_other_series_keep_defaults(self):
        # 9.7 的 OpenTelemetry telemetry 经 ext::curl 链 ServBay 静态 libcurl.a，
        # 其私有依赖 (libnghttp2/libbrotli) 不被 FIND_CUSTOM_CURL 传播，链接必炸。
        # 本地环境不做 OTLP，两个开关都关闭。v1 1c73242。
        for version, expected in (("9.7.0", True), ("9.4.0", False), ("8.4.6", False)):
            with self.subTest(version=version):
                args = self.run_cmake_args(version)
                client = "-DWITH_MYSQL_CLIENT_TELEMETRY=OFF" in args
                server = "-DWITH_MYSQL_SERVER_TELEMETRY=OFF" in args
                self.assertEqual(expected, client)
                self.assertEqual(expected, server)

    def test_build_mysql_leads_cflags_with_only_the_openssl_include(self):
        # 16fab29: 前置 ServBay OpenSSL include 让 harness_tls 的每次编译都用 base
        # 头文件。dee252e 教训：严禁再带 common/include——base 在 common/include/unicode
        # 的旧 ICU 会压过 WITH_ICU=bundled 的 ICU 77，gen_keyword_list 链接错配。
        body = self.extract(self.text, "build_mysql")
        self.assertIn('local _servbay_inc="-I${SERVBAY_COMMON_PATH}/openssl/${OPENSSL_VERSION}/include"', body)
        inc_line = [line for line in body.splitlines() if "local _servbay_inc=" in line][0]
        self.assertNotIn("SERVBAY_COMMON_INCLUDE_PATH", inc_line)
        for flag in ("export CFLAGS=", "export CXXFLAGS="):
            export_line = [line.strip() for line in body.splitlines() if line.strip().startswith(flag)][0]
            self.assertIn("${_servbay_inc} ", export_line)
            self.assertLess(body.index("local _servbay_inc="), body.index(export_line))
        self.assertIn("unset CPPFLAGS", body)


class RedisLinkDriverTests(unittest.TestCase):
    def test_redis_install_routes_links_through_the_compiler_driver(self):
        # redis 的 src/modules/Makefile 在 Darwin 上把 LIBS 硬编码为
        # "-L /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/lib -lsystem"
        # 并用裸 ld 链接；Intel runner 的 CLT ld 解析不了该 SDK 的
        # libSystem.B.tbd（arm64e.x1 unknown architecture，x86_64 run 39962827584
        # 实测失败，arm64 的 ld 较新所以通过）。LD="$(CC)" 让链接走编译器驱动；
        # LDFLAGS="" 继续隔离全局 gcc 风格链接标志。
        # 断言剥掉注释行，只看真实命令（否则注释里的关键词会假绿）。
        recipe = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        body = recipe.split("build_redis() {", 1)[1].split("\n}", 1)[0]
        commands = [line for line in body.splitlines() if line.strip().startswith("make ")]
        install = [line for line in commands if "install" in line]
        self.assertEqual(1, len(install), f"expected one make install command, got {install}")
        self.assertIn('LDFLAGS=""', install[0])
        self.assertIn('LD="$(CC)"', install[0])


class HttpdAprLibtoolBackfillTests(unittest.TestCase):
    def test_httpd_backfills_the_missing_apr_build_libtool(self):
        # base 的 devlib 不带 share/apr-1/build/libtool，而 httpd 的 make 按
        # apr-config 报告的路径直接调用它（run 39965591690 两架构实测
        # "No such file or directory"）。配方必须在构建时用 brew glibtool
        # 回填该路径；断言剥掉注释，只看真实代码。
        recipe = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        body = recipe.split("build_httpd() {", 1)[1].split("\n}", 1)[0]
        code = [line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]
        joined = "\n".join(code)
        self.assertIn("share/apr-1/build/libtool", joined)
        self.assertIn("command -v glibtool", joined)
        self.assertIn("> \"$_apr_libtool\"", joined)


class PhpLegacyMakefileFlagsTests(unittest.TestCase):
    """A7（v1 6b03016）：旧分支 libjpeg 回调签名降级必须写进 configure 生成的
    Makefile 的 CFLAGS_CLEAN——PHP configure 会重建 CFLAGS，环境变量到不了编译行。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def run_build_php(self, version):
        root = self.root / version
        source = root / "source"
        source.mkdir(parents=True)
        configure = source / "configure"
        configure.write_text("#!/bin/sh\nprintf 'CFLAGS_CLEAN = -g -O2\\n' > Makefile\n")
        configure.chmod(0o755)
        buildconf = source / "buildconf"
        buildconf.write_text("#!/bin/sh\nexit 0\n")
        buildconf.chmod(0o755)
        base = root / "base"
        common = base / "common"
        capture = root / "makefile-at-make-time"
        body = self.extract(self.text, "build_php")
        config_body = self.extract(self.text, "_get_php_build_config")
        script = [f"SERVBAY_BUILD_DIR={shlex.quote(str(root))}",
                  f"SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(base / 'packages'))}",
                  f"SERVBAY_ETC_PATH={shlex.quote(str(base / 'etc'))}",
                  f"SERVBAY_VAR_PATH={shlex.quote(str(base / 'var'))}",
                  f"SERVBAY_TMP_PATH={shlex.quote(str(base / 'tmp'))}",
                  f"SERVBAY_BUILD_LOG_DIR={shlex.quote(str(base / 'logs'))}",
                  f"SERVBAY_COMMON_PATH={shlex.quote(str(common))}",
                  f"SERVBAY_COMMON_LIB_PATH={shlex.quote(str(common / 'lib'))}",
                  f"SERVBAY_COMMON_INCLUDE_PATH={shlex.quote(str(common / 'include'))}",
                  f"SERVBAY_STATIC_LIB_PATH={shlex.quote(str(common / 'static/lib'))}",
                  f"SERVBAY_STATIC_INCLUDE_PATH={shlex.quote(str(common / 'static/include'))}",
                  f"SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(base / 'packages'))}",
                  f"SOURCE={shlex.quote(str(source))}",
                  f"MAKE_CAPTURE={shlex.quote(str(capture))}",
                  "OPENSSL_VERSION=3.5", 'SDK_PATH="/nonexistent-sdk"',
                  "architecture=x86_64", "CPU_NUMBER=2", "log_message() { :; }",
                  "typeset -A PHP_MODS PACK_INFO", "typeset -a PHP_PATCHES PHP_CONFIGURE_ARGS PHP_PRE_BUILD_CMDS PHP_POST_BUILD_CMDS",
                  "unpack_package() { builtin cd \"$SOURCE\"; }",
                  "_apply_single_patch() { :; }",
                  "prepare_php_source() { :; }",
                  'make() { if [[ $1 == install ]]; then return 0; fi; cp Makefile "$MAKE_CAPTURE"; }',
                  "get_package_packing_info() { :; }", "core_verify_package() { :; }",
                  "check_package_homebrew_deps() { :; }", "core_pack_package() { :; }",
                  "_get_php_build_config() {\n" + config_body + "\n}",
                  "build_php() {\n" + body + "\n}",
                  'if build_php ' + shlex.quote(version) + ' src.tar.gz ""; then exit 0; else exit 1; fi']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return capture.read_text()

    def test_legacy_series_get_the_libjpeg_demotion_in_cflags_clean_at_make_time(self):
        for version, demoted in (("5.6.40", True), ("7.3.33", True),
                                 ("8.1.34", False), ("8.4.27", False)):
            with self.subTest(version=version):
                makefile = self.run_build_php(version)
                lines = [line for line in makefile.splitlines() if line.startswith("CFLAGS_CLEAN")]
                self.assertEqual(["CFLAGS_CLEAN = -g -O2"
                                  + (" -Wno-error=incompatible-function-pointer-types -Wno-error=implicit-int" if demoted else "")],
                                 lines)


class MariaDbGroongaPatchTests(unittest.TestCase):
    """B1（v1 474c6f2）：groonga <netdb.h> x86_64 补丁对所有 x86_64 版本生效，
    应用失败只降级为 WARN（旧版按 major 白名单且失败即 return 1，每次发新版都过期）。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def run_groonga_block(self, version, arch, apply_result):
        body = self.extract(self.text, "build_mariadb")
        block = body[body.index("groonga"):body.index("Fix libxml2")]
        root = self.root / (version + "-" + arch + "-" + str(apply_result))
        root.mkdir(parents=True)
        script = [f"version={shlex.quote(version)}",
                  f"architecture={shlex.quote(arch)}",
                  f"APPLY_RESULT={apply_result}",
                  f"PATCH_DIR={shlex.quote(str(root / 'patch'))}",
                  f"PATCH_CALLS={shlex.quote(str(root / 'patch-calls'))}",
                  f"WARN_LOG={shlex.quote(str(root / 'warn'))}",
                  'log_message() { [[ $1 == WARN ]] && print -r -- "$2" >> "$WARN_LOG"; return 0; }',
                  '_apply_single_patch() { print -r -- "$1" >> "$PATCH_CALLS"; return $APPLY_RESULT; }',
                  "groonga_block() {\n" + block + "\n}",
                  "groonga_block",
                  "exit 0"]
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = root / "patch-calls"
        warns = root / "warn"
        return (calls.read_text() if calls.exists() else "",
                warns.read_text() if warns.exists() else "")

    def test_every_x86_64_version_attempts_the_patch_even_ones_outside_the_old_whitelist(self):
        self.assertNotIn("patch_majors_groonga_netdb", self.extract(self.text, "build_mariadb"))
        for version in ("11.4.12", "11.8.8", "12.2.0"):
            with self.subTest(version=version):
                calls, warns = self.run_groonga_block(version, "x86_64", 0)
                self.assertIn("fix-groonga-netdb-11.4-x86_64.patch", calls)

    def test_a_failing_patch_only_warns_and_never_fails_the_build(self):
        calls, warns = self.run_groonga_block("12.2.0", "x86_64", 1)
        self.assertIn("fix-groonga-netdb-11.4-x86_64.patch", calls)
        self.assertIn("groonga-netdb x86_64 patch not applied", warns)

    def test_arm64_builds_are_not_touched(self):
        calls, warns = self.run_groonga_block("12.2.0", "arm64", 0)
        self.assertEqual("", calls)


class OpenJdkFlattenTests(unittest.TestCase):
    """B2（v1 dfd05d4）：Zulu macOS 包把 JDK 打成 .jdk App bundle，
    --strip-components=1 后载荷在 Contents/Home/ 下，bin/java 不可达；必须展平。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def run_openjdk(self, bundle):
        root = self.root / ("bundle" if bundle else "flat")
        root.mkdir(parents=True)
        body = self.extract(self.text, "build_openjdk")
        if bundle:
            unpack = ('unpack_package() { local target="$3"; [[ -n "$target" ]] || target="$PWD/$1"; '
                      'mkdir -p "$target/Contents/Home/bin" "$target/Contents/Home/lib" "$target/Contents/Home/conf"; '
                      'print java > "$target/Contents/Home/bin/java"; print x > "$target/Contents/Home/release"; }')
        else:
            unpack = ('unpack_package() { local target="$3"; [[ -n "$target" ]] || target="$PWD/$1"; '
                      'mkdir -p "$target/bin"; print java > "$target/bin/java"; print x > "$target/release"; }')
        script = [f"SERVBAY_BUILD_DIR={shlex.quote(str(root))}",
                  f"SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(root / 'base/packages'))}",
                  "log_message() { :; }",
                  "trash_path() { :; }",
                  unpack,
                  "build_openjdk() {\n" + body + "\n}",
                  'if build_openjdk 21.0.12 zulu.tar.gz ""; then exit 0; else exit 1; fi']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return root / "base/packages/openjdk/21"

    def test_zulu_app_bundle_payload_is_lifted_to_the_package_root(self):
        final = self.run_openjdk(bundle=True)
        self.assertTrue((final / "bin/java").is_file(), "bin/java must be reachable at the root")
        self.assertTrue((final / "release").is_file())
        self.assertFalse((final / "Contents").exists(), "the .jdk wrapper must be dropped")

    def test_flat_archives_are_left_untouched(self):
        final = self.run_openjdk(bundle=False)
        self.assertTrue((final / "bin/java").is_file())


class RubyLibyamlAutoreconfTests(unittest.TestCase):
    """B3（v1 e47f814）：libyaml 0.2.5 的 GitHub archive 无预生成 configure，
    ./bootstrap 跑 autoreconf；其 configure.ac 用过时的 AC_PROG_LIBTOOL，
    autoconf >= 2.70（arm64 runner）直接拒绝。bootstrap 前必须 sed 成 LT_INIT。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def test_bootstrap_runs_against_a_modernized_libyaml_configure_ac(self):
        root = self.root / "ruby"
        root.mkdir()
        body = self.extract(self.text, "build_ruby")
        unpack = (
            'unpack_package() {\n'
            '  local name="$1" dir\n'
            '  dir="$SERVBAY_BUILD_DIR/$name"\n'
            '  mkdir -p "$dir"\n'
            '  if [[ "$name" == libyaml-0.2.5 ]]; then\n'
            '    print -r -- "AC_PROG_LIBTOOL" > "$dir/configure.ac"\n'
            '    print -r -- "#!/bin/sh" > "$dir/bootstrap"\n'
            '    print -r -- "cp configure.ac \\"$BOOTSTRAP_CAPTURE\\"" >> "$dir/bootstrap"\n'
            '    print -r -- "#!/bin/sh" > "$dir/configure"\n'
            '  else\n'
            '    print -r -- "#!/bin/sh" > "$dir/configure"\n'
            '    print -r -- "touch \\"$RUBY_CONFIGURE_SENTINEL\\"" >> "$dir/configure"\n'
            '  fi\n'
            '  chmod +x "$dir/bootstrap" "$dir/configure" 2>/dev/null\n'
            '  builtin cd "$dir"\n'
            '}')
        script = [f"SERVBAY_BUILD_DIR={shlex.quote(str(root))}",
                  f"SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(root / 'base/packages'))}",
                  f"BOOTSTRAP_CAPTURE={shlex.quote(str(root / 'configure-ac-at-bootstrap-time'))}",
                  f"RUBY_CONFIGURE_SENTINEL={shlex.quote(str(root / 'ruby-configure-ran'))}",
                  "architecture=x86_64", "CPU_NUMBER=2",
                  "log_message() { :; }",
                  "download_source() { return 0; }",
                  "trash_path() { :; }",
                  "make() { return 0; }",
                  unpack,
                  "build_ruby() {\n" + body + "\n}",
                  'if build_ruby 3.4.1 ruby.tar.gz ""; then exit 0; else exit 1; fi']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((root / "ruby-configure-ran").exists(),
                        "the harness must actually reach the Ruby configure stage")
        snapshot = (root / "configure-ac-at-bootstrap-time").read_text()
        self.assertNotIn("AC_PROG_LIBTOOL", snapshot,
                         "./bootstrap must never see the obsolete macro")
        self.assertIn("LT_INIT", snapshot)


class MonoIcallC23Tests(unittest.TestCase):
    """B4（v1 7509d07+1b1758b）：mono 在 CFLAGS 之后硬加 -std=gnu99，压掉 CC 的
    gnu23，icall.c 的 bool token-paste 拼出 ICALL_SIG_TYPE__Bool。只能在 configure
    生成 Makefile 之后改写，且必须收窄到 icall.c 所在目录——全局改写会打破
    minizip 等 K&R 老代码（C23 移除 K&R 函数定义）。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def test_only_the_icall_makefile_is_rewritten_to_c23(self):
        root = self.root / "mono"
        root.mkdir()
        body = self.extract(self.text, "build_mono")
        unpack = (
            'unpack_package() {\n'
            '  local name="$1" dir\n'
            '  dir="$SERVBAY_BUILD_DIR/$name"\n'
            '  mkdir -p "$dir"\n'
            '  if [[ "$name" == mono-6.14.0 ]]; then\n'
            '    mkdir -p "$dir/metadata" "$dir/minizip"\n'
            '    : > "$dir/metadata/icall.c"\n'
            '    print -r -- "CFLAGS = -O2 -std=gnu99" > "$dir/metadata/Makefile"\n'
            '    print -r -- "CFLAGS = -O2 -std=gnu99" > "$dir/minizip/Makefile"\n'
            '    print -r -- "#!/bin/sh" > "$dir/autogen.sh"\n'
            '  else\n'
            '    print -r -- "#!/bin/sh" > "$dir/autogen.sh"\n'
            '    print -r -- "#!/bin/sh" > "$dir/configure"\n'
            '    print -r -- "#!/bin/sh" > "$dir/make-fixture"\n'
            '  fi\n'
            '  chmod +x "$dir"/autogen.sh "$dir"/configure 2>/dev/null\n'
            '  builtin cd "$dir"\n'
            '}')
        script = [f"SERVBAY_BUILD_DIR={shlex.quote(str(root))}",
                  f"SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(root / 'base/packages'))}",
                  f"XSP_SENTINEL={shlex.quote(str(root / 'xsp-stage-reached'))}",
                  "BUILD_OS_MIN_VERSION=12.0",
                  "CPU_NUMBER=2",
                  "log_message() { :; }",
                  "download_source() { return 0; }",
                  "trash_path() { :; }",
                  'make() { if [[ -n "$XSP_SENTINEL" && -f make-fixture ]]; then touch "$XSP_SENTINEL"; fi; return 0; }',
                  unpack,
                  "build_mono() {\n" + body + "\n}",
                  'if build_mono 6.14.0 mono.tar.gz ""; then exit 0; else exit 1; fi']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((root / "xsp-stage-reached").is_file(),
                        "the harness must actually reach the XSP stage")
        icall_makefile = (root / "mono-6.14.0/metadata/Makefile").read_text()
        self.assertIn("-std=gnu23", icall_makefile)
        self.assertNotIn("-std=gnu99", icall_makefile)
        minizip_makefile = (root / "mono-6.14.0/minizip/Makefile").read_text()
        self.assertIn("-std=gnu99", minizip_makefile,
                      "global rewrites break minizip's K&R sources; only icall.c's directory may change")


if __name__ == "__main__":
    unittest.main()
