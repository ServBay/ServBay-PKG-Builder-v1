import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import test_build_scripts as legacy_tests

ROOT = Path(__file__).resolve().parents[1]
RECIPES = {"phpredis": "redis", "memcache": "memcache", "memcached": "memcached",
           "xdebug": "xdebug", "apcu": "apcu", "mongodb": "mongodb", "imap": "imap",
           "scws": "scws", "imagick": "imagick", "swoole": "swoole", "sqlsrv": "sqlsrv",
           "pdo_sqlsrv": "pdo_sqlsrv", "phalcon": "phalcon", "psr": "psr"}


class ModuleRecipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def test_recipe_library_source_does_not_initialize_or_start_a_build(self):
        base = self.root / "absent-base"
        env = dict(os.environ, SERVBAY_BASE_FULL_PATH=str(base),
                   SERVBAY_BUILD_ROOT=str(self.root / "absent-work"), SERVBAY_RECIPE_LIBRARY_ONLY="1")
        result = subprocess.run(["/bin/zsh", "-c", 'source "$1"; (( $+functions[_build_php_mod_phpredis] ))',
                                 "library-test", str(ROOT / "runtime-v2/scripts/build_package.sh")],
                                capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(base.exists())
        self.assertFalse((self.root / "absent-work").exists())

    def test_standalone_entry_does_not_require_an_unrelated_go_compiler(self):
        profile = self.root / "profile"
        scripts = profile / "scripts"
        scripts.mkdir(parents=True)
        entry = scripts / "build_module.sh"
        entry.write_bytes((ROOT / "runtime-v2/scripts/build_module.sh").read_bytes())
        source = self.text.replace('GO_BIN=$(which go)', 'GO_BIN=$(false)', 1)
        boundary = '# The standalone module producer reuses'
        self.assertEqual(1, source.count(boundary))
        fixture = '\nsetup_package_build_environment() { return 0; }\n_build_php_mod_phpredis() { local d=$(_php_module_extension_dir "$4/bin/php-config"); mkdir -p "$d"; cp "$MODULE_NATIVE_FIXTURE" "$d/redis.so"; }\n'
        source = source.replace(boundary, fixture + boundary, 1)
        (scripts / "build_package.sh").write_text(source)
        base = self.root / "base"
        (base / "packages/common").mkdir(parents=True)
        (base / ".bootstrap-v2.json").write_text("{}\n")
        prefix = base / "packages/php/8.4"
        (prefix / "bin").mkdir(parents=True)
        config = prefix / "bin/php-config"
        config.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(str(prefix / "lib/extensions")) + "\n")
        config.chmod(0o755)
        stage = self.root / "stage"
        stage.mkdir()
        csource = self.root / "native.c"
        csource.write_text("int module_fixture(void) { return 7; }\n")
        dylib = self.root / "native.dylib"
        subprocess.run(["/usr/bin/clang", "-dynamiclib", str(csource), "-o", str(dylib)], check=True, capture_output=True)
        env = dict(os.environ, SERVBAY_BASE_FULL_PATH=str(base), SERVBAY_BUILD_ROOT=str(self.root / "work"), MODULE_NATIVE_FIXTURE=str(dylib))
        result = subprocess.run(["/bin/zsh", str(entry), "php-extension", "phpredis", "6.3.0", str(prefix),
                                 "8.4.27", "8.4", "x86_64", str(stage)], capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((Path(str(stage) + str(prefix)) / "lib/extensions/redis.so").is_file())
        self.assertIn("Scanned 1 Mach-O files", result.stderr)
        self.assertFalse((prefix / "lib/extensions").exists())

    def test_phalcon_bundle_requires_both_actual_staged_modules_and_builds_psr_first(self):
        for failure in ("", "psr-fail", "psr-missing", "phalcon-fail", "phalcon-missing"):
            with self.subTest(failure=failure):
                root = self.root / (failure or "success")
                profile = root / "profile"
                scripts = profile / "scripts"
                scripts.mkdir(parents=True)
                entry = scripts / "build_module.sh"
                entry.write_bytes((ROOT / "runtime-v2/scripts/build_module.sh").read_bytes())
                boundary = '# The standalone module producer reuses'
                fixture = '\nsetup_package_build_environment() { return 0; }\n_build_php_mod_psr() { [[ "$BUNDLE_FAILURE" != psr-fail ]] || return 9; local d=$(_php_module_extension_dir "$4/bin/php-config"); mkdir -p "$d" "$MODULE_INSTALL_ROOT$4/include/php/ext/psr"; [[ "$BUNDLE_FAILURE" == psr-missing ]] || cp "$MODULE_NATIVE_FIXTURE" "$d/psr.so"; }\n_build_php_mod_phalcon() { [[ "$BUNDLE_FAILURE" != phalcon-fail ]] || return 9; local d=$(_php_module_extension_dir "$4/bin/php-config"); [[ -f "$d/psr.so" && "$CPPFLAGS" == *"$MODULE_INSTALL_ROOT$4/include/php"* ]] || return 9; [[ "$BUNDLE_FAILURE" == phalcon-missing ]] || cp "$MODULE_NATIVE_FIXTURE" "$d/phalcon.so"; return 0; }\n'
                (scripts / "build_package.sh").write_text(self.text.replace(boundary, fixture + boundary, 1))
                base = root / "base"
                (base / "packages/common").mkdir(parents=True)
                (base / ".bootstrap-v2.json").write_text("{}\n")
                prefix = base / "packages/php/7.4"
                (prefix / "bin").mkdir(parents=True)
                config = prefix / "bin/php-config"
                config.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(str(prefix / "lib/extensions")) + "\n")
                config.chmod(0o755)
                stage = root / "stage"
                stage.mkdir()
                source = root / "fixture.c"
                source.write_text("int php_bundle_fixture(void) { return 1; }\n")
                native = root / "fixture.dylib"
                subprocess.run(["/usr/bin/clang", "-dynamiclib", str(source), "-o", str(native)], check=True, capture_output=True)
                env = dict(os.environ, SERVBAY_BASE_FULL_PATH=str(base), SERVBAY_BUILD_ROOT=str(root / "work"),
                           SERVBAY_PSR_BUNDLE_VERSION="1.1.0", BUNDLE_FAILURE=failure, MODULE_NATIVE_FIXTURE=str(native))
                result = subprocess.run(["/bin/zsh", str(entry), "php-extension", "phalcon", "4.1.2", str(prefix),
                                         "7.4.33.1", "7.4", "x86_64", str(stage)], env=env, capture_output=True, text=True)
                if failure:
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                else:
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual({"psr.so", "phalcon.so"}, {p.name for p in (Path(str(stage) + str(prefix)) / "lib/extensions").iterdir()})
                self.assertFalse((prefix / "lib/extensions").exists())

    def run_recipe(self, recipe, failure="", version="6.2.0", series="8.3", source_files=None):
        root = self.root / (recipe + "-" + version + "-" + series + "-" + (failure or "success"))
        source = root / "source"
        for part in ("build/phalcon", "build/php7/64bits", "phpext"):
            (source / part).mkdir(parents=True)
        for relative, content in (source_files or {}).items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        prefix = root / "base/packages/php" / series
        stage = root / "stage"
        config = prefix / "bin/php-config"
        config.parent.mkdir(parents=True)
        extension = prefix / "lib/nonstandard/extensions"
        config.write_text("#!/bin/sh\nprintf '%s' " + shlex.quote(str(extension)) + "\n")
        config.chmod(0o755)
        phpize = prefix / "bin/phpize"
        phpize.write_text("#!/bin/sh\nexit " + ("7" if failure == "phpize" else "0") + "\n")
        phpize.chmod(0o755)
        php = prefix / "bin/php"
        # 模拟真实 php 对 -d extension=<so> --ri <name> 的响应：dlopen 成功时输出
        # 扩展信息段并退出 0；FAILURE=php-ri 时模拟 "Extension not present"（退出 1）。
        # phalcon 4.x（PHP 7.2-7.4）带 ZEND_MOD_REQUIRED("psr")：宿主实测 -n 下 psr
        # 缺席时启动报 "required module 'psr' is not loaded" 且 --ri 退出 1；调用行
        # 预载了 psr.so 才放行。必须保持 POSIX sh 语义（stub 以 #!/bin/sh 运行）。
        dep_guard = ""
        if recipe == "phalcon" and series in ("7.2", "7.3", "7.4"):
            # 冒烟传入的 psr.so 路径来自 _php_module_extension_dir，带 MODULE_INSTALL_ROOT 前缀。
            dep_guard = ('case "$*" in *"-d extension=' + str(stage) + str(extension / "psr.so") + '"*) ;;\n'
                         '  *) printf \'%s\\n\' "PHP Warning:  Cannot load module \'phalcon\' because required module \'psr\' is not loaded in Unknown on line 0" >&2\n'
                         '     printf \'%s\\n\' "Extension \'phalcon\' not present." >&2\n'
                         '     exit 1 ;;\n'
                         'esac\n')
        php.write_text("#!/bin/sh\n"
                       'printf \'%s\\n\' "$*" >> "$RI_CALLS"\n'
                       + dep_guard +
                       'for arg in "$@"; do\n'
                       '  case "$arg" in\n'
                       '    --ri) printf \'%s\\n\' "===' + RECIPES[recipe] + '-section==="\n'
                       '          [ "$FAILURE" = "php-ri" ] && exit 1\n'
                       '          exit 0 ;;\n'
                       '  esac\n'
                       'done\n'
                       "exit 0\n")
        php.chmod(0o755)
        for directory in (source, source / "build/phalcon", source / "build/php7/64bits", source / "phpext"):
            configure = directory / "configure"
            configure.write_text("#!/bin/sh\nexit " + ("8" if failure == "configure" else "0") + "\n")
            configure.chmod(0o755)
        assets = root / "assets/conf/php"
        assets.mkdir(parents=True)
        template = "; retain user options\nextension=" + RECIPES[recipe] + ".so\n"
        (assets / (RECIPES[recipe] + ".ini")).write_text(template)
        (assets / "phalcon.ini").write_text(template)
        if recipe == "phalcon":
            # _build_php_mod_phalcon 的 7.x 分支从 phalcon-7.x.ini 复制（目标名仍是 phalcon.ini）。
            (assets / "phalcon-7.x.ini").write_text(template)
        conf = stage / "etc/php" / series / "conf.d"
        conf.mkdir(parents=True)
        script = ["CPU_NUMBER=2", "FAILURE=" + shlex.quote(failure), "export FAILURE",
                  "MODULE_INSTALL_ROOT=" + shlex.quote(str(stage)),
                  "RI_CALLS=" + shlex.quote(str(root / "ri-calls")), "export RI_CALLS",
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(root)),
                  "PROJECT_ROOT=" + shlex.quote(str(root / "assets")),
                   "SERVBAY_ETC_PATH=" + shlex.quote(str(stage / "etc")),
                   "SERVBAY_LOG_PATH=" + shlex.quote(str(stage / "log")),
                   "EXTRA_SO=" + shlex.quote("psr.so" if recipe == "phalcon" else ""),
                  "SOURCE=" + shlex.quote(str(source)), "EXTENSION=" + shlex.quote(str(extension)),
                  "SO=" + shlex.quote(RECIPES[recipe] + ".so"), "log_message() { :; }",
                  "_download_pecl_source() { [[ $FAILURE != download ]]; }",
                  "unpack_package() { [[ $FAILURE != unpack ]] || return 8; builtin cd \"$SOURCE\"; }",
                  "cleanup_source_directory() { :; }", "PATCH_DIR=" + shlex.quote(str(root / "assets/patch")),
                  "PATCH_CALLS=" + shlex.quote(str(root / "patch-calls")), '_apply_single_patch() { print -r -- "$1" >> "$PATCH_CALLS"; [[ $FAILURE != patch ]]; }',
                  "aclocal() { :; }; autoconf() { :; }; autoheader() { :; }; glibtoolize() { :; }; automake() { :; }",
                  'make() { if [[ $1 == install ]]; then [[ $FAILURE == install ]] && return 9; local target="$EXTENSION"; for arg in "$@"; do [[ $arg == INSTALL_ROOT=* ]] && target="${arg#INSTALL_ROOT=}$EXTENSION"; done; mkdir -p "$target"; if [[ $FAILURE != missing-so ]]; then print -r -- "fresh $SO" > "$target/$SO"; [[ -n "$EXTRA_SO" ]] && print -r -- "fresh $EXTRA_SO" > "$target/$EXTRA_SO"; fi; return 0; fi; [[ $FAILURE != build ]]; }']
        for name in ("_install_php_module", "_php_module_extension_dir", "_verify_php_module_loads"):
            try:
                body = self.extract(self.text, name)
            except AssertionError:
                continue
            script.append(name + "() {\n" + body + "\n}")
        name = "_build_php_mod_" + recipe
        script.append(name + "() {\n" + self.extract(self.text, name) + "\n}")
        script.append('if ' + name + ' ' + shlex.quote(version) + ' ' + shlex.quote(series + '.36') + ' ' + shlex.quote(series) + ' ' + shlex.quote(str(prefix)) +
                      '; then print -r -- verified > ' + shlex.quote(str(root / "handoff")) + '; else exit 1; fi')
        # A failed unpack must stop even when the caller already has usable source files.
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], cwd=source, capture_output=True, text=True)
        return result, root, extension, stage, conf, template

    def test_every_php_recipe_installs_only_in_its_fresh_module_stage_and_preserves_templates(self):
        for recipe, name in RECIPES.items():
            with self.subTest(recipe=recipe):
                result, root, extension, stage, conf, template = self.run_recipe(recipe)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertFalse(extension.exists(), "Standalone module wrote into its exact core")
                self.assertTrue((Path(str(stage) + str(extension)) / (name + ".so")).is_file())
                if recipe != "psr":
                    self.assertEqual(template, (conf / (name + ".ini")).read_text())
                self.assertTrue((root / "handoff").is_file())

    def test_every_php_recipe_propagates_each_actual_build_stage_failure_without_handoff(self):
        for recipe in RECIPES:
            for failure in ("download", "unpack", "phpize", "configure", "build", "install", "missing-so", "php-ri"):
                with self.subTest(recipe=recipe, failure=failure):
                    result, root, extension, stage, conf, template = self.run_recipe(recipe, failure)
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertFalse((root / "handoff").exists())
                    self.assertFalse(extension.exists())

    def test_new_upstream_redis_and_imagick_do_not_receive_incompatible_old_release_patches(self):
        for recipe, old, current in (("phpredis", "6.2.0", "6.3.0"), ("imagick", "3.7.0", "3.8.1")):
            for version in (old, current):
                with self.subTest(recipe=recipe, version=version):
                    result, root, _, _, _, _ = self.run_recipe(recipe, version=version, series="8.4")
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(version == old, (root / "patch-calls").exists())


    def test_php_core_and_extension_commands_share_confirmed_legacy_optimization_without_changing_other_series(self):
        for version in ('7.3.33.1', '7.4.33.1', '8.0.30.1', '8.1.34.1', '8.4.27', '8.5.12'):
            series = '.'.join(version.split('.')[:2])
            for arch in ('x86_64', 'arm64'):
                for extension_only in (False, True):
                    with self.subTest(version=version, arch=arch, extension_only=extension_only):
                        root = self.root / ('flags-' + version + '-' + arch + '-' + str(extension_only))
                        profile = root / 'profile'
                        scripts = profile / 'scripts'
                        scripts.mkdir(parents=True)
                        (profile / 'ci').symlink_to(ROOT / 'runtime-v2/ci', target_is_directory=True)
                        (profile / 'config').mkdir()
                        (profile / 'config/packages.conf').write_text('php\t' + version + '\tphp.tar.gz\tphp.tar.gz\tsource\n')
                        base = root / 'base'
                        common = base / 'packages/common'
                        for part in ('static/lib', 'static/include', 'lib', 'include'):(common / part).mkdir(parents=True, exist_ok=True)
                        (base / '.bootstrap-v2.json').write_text('{}\n')
                        config = base / 'packages/php' / series / 'bin/php-config'
                        config.parent.mkdir(parents=True)
                        config.write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.quote(version) + '\n')
                        config.chmod(0o755)
                        source = self.text
                        for function in ('build_php', '_build_php_mod_phpredis'):
                            body = self.extract(source, function)
                            old = function + '() {\n' + body + '}'
                            self.assertEqual(1, source.count(old))
                            source = source.replace(old, function + '() {\nprint -r -- "$CFLAGS" > "$FLAGS_CAPTURE"\nexit 0\n}')
                        body = self.extract(source, 'download_source')
                        old = 'download_source() {\n' + body + '}'
                        self.assertEqual(1, source.count(old))
                        source = source.replace(old, 'download_source() { return 0; }')
                        entry = scripts / 'build_package.sh'
                        entry.write_text(source)
                        capture = root / 'flags'
                        env = dict(os.environ, HOME=str(root / 'home'), SERVBAY_BASE_FULL_PATH=str(base), FLAGS_CAPTURE=str(capture))
                        args = ['/bin/zsh', str(entry), '-a', arch]
                        args += ['--php-ext', series + ':phpredis:6.3.0'] if extension_only else ['-p', 'php-' + version]
                        result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True)
                        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                        flags = shlex.split(capture.read_text())
                        optimizations = [flag for flag in flags if flag in ('-O0', '-O1', '-O2', '-O3')]
                        # v1 0918cc7：7.3 与 7.4/8.0 同样的 -O3 误编译（x86_64 cli/php
                        # 在 make 首次运行时 SIGABRT），无条件按 -O1 构建。
                        expected = '-O1' if series in ('7.3', '7.4', '8.0') or series == '8.5' and arch == 'x86_64' else '-O3'
                        self.assertEqual(expected, optimizations[-1], capture.read_text())
                        if series in ('7.3', '7.4', '8.0'):self.assertIn('-fno-strict-aliasing', flags)
                        print('PHP_COMMAND_FLAGS ' + json.dumps({'version': version, 'arch': arch, 'extension_only': extension_only, 'cflags': capture.read_text().strip()}))


    def test_memcache86_expands_ps_mod_and_proves_the_module_dlopens(self):
        # v1 d3e4301: PHP 8.6 重写 PS_MOD(x)，要求扩展自带 ps_create_sid/ps_validate_sid；
        # memcache 8.0 不实现，.so 链接期不报错、dlopen 时才炸缺符号。除 PS_FUNCS/save_path
        # 两个 sed 外，必须把 PS_MOD(memcache) 手工展开为 8.5 语义；并在安装成功路径用
        # php --ri <name> 做 dlopen 冒烟（ini 已复制之后执行）。
        result, root, extension, stage, conf, template = self.run_recipe(
            "memcache", series="8.6",
            source_files={
                "src/php_memcache.h": "PS_FUNCS(memcache);\n",
                "src/memcache_session.c": "ps_mod = PS_MOD(memcache);\npath = save_path;\n",
            })
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        header = (root / "source/src/php_memcache.h").read_text()
        self.assertNotIn("PS_FUNCS(memcache);", header)
        self.assertIn("PS_CREATE_SID_FUNC(memcache); PS_VALIDATE_SID_FUNC(memcache);", header)
        session = (root / "source/src/memcache_session.c").read_text()
        self.assertNotIn("PS_MOD(memcache)", session)
        self.assertIn("php_session_create_id, php_session_validate_sid, php_session_update_timestamp", session)
        self.assertIn("path = ZSTR_VAL(save_path);", session)
        calls = (root / "ri-calls").read_text().splitlines()
        self.assertTrue(calls, "php --ri smoke must run")
        smoke = [line for line in calls if "--ri" in line]
        self.assertEqual(1, len(smoke), calls)
        self.assertIn("--ri memcache", smoke[0])
        self.assertIn("-d extension=", smoke[0])
        self.assertIn("memcache.so", smoke[0])
        self.assertTrue((root / "handoff").is_file())

    def test_phalcon_4x_smoke_preloads_the_required_psr_dependency_before_the_module(self):
        # P1：phalcon 4.1.2（PHP 7.2-7.4）带 ZEND_MOD_REQUIRED("psr")，宿主实测 -n 下
        # psr 缺席时 --ri 报 "required module 'psr' is not loaded" 且退出 1，导致构建
        # 中断。冒烟必须先 -d extension=<同链 psr.so> 预载再加载 phalcon 本体；psr 由
        # bundle（build_module.sh）先于 phalcon 构建到同一 extension dir。3.4.5 与
        # 5.7.0/5.9.3（宿主 5.6/7.1/8.0/8.3 实测）无此依赖，不预载。
        for series, version, needs_psr in (("7.4", "4.1.2", True), ("8.3", "5.9.3", False)):
            with self.subTest(series=series, version=version, needs_psr=needs_psr):
                result, root, extension, stage, conf, template = self.run_recipe("phalcon", version=version, series=series)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                calls = (root / "ri-calls").read_text().splitlines()
                smoke = [line for line in calls if "--ri phalcon" in line]
                self.assertEqual(1, len(smoke), calls)
                # 冒烟传入的 psr.so 路径带 MODULE_INSTALL_ROOT 前缀（_php_module_extension_dir）。
                psr_flag = "-d extension=" + str(stage) + str(extension / "psr.so")
                self.assertEqual(needs_psr, psr_flag in smoke[0], smoke[0])
                if needs_psr:
                    # 依赖必须先于被测模块出现在调用行。
                    self.assertLess(smoke[0].index(psr_flag),
                                    smoke[0].index("-d extension=" + str(stage) + str(extension / "phalcon.so")), smoke[0])
                self.assertTrue((root / "handoff").is_file())

    def test_xdebug_316_uses_the_current_v1_declaration_fix_and_propagates_patch_failure(self):
        for version, failure in [('3.1.6', ''), ('3.1.6', 'patch'), ('3.4.3', '')]:
            with self.subTest(version=version, failure=failure):
                result, root, _, _, _, _ = self.run_recipe('xdebug', version=version, series='7.4', failure=failure)
                calls = root / 'patch-calls'
                if version == '3.1.6':
                    self.assertTrue(calls.is_file())
                    self.assertEqual(str(root / 'assets/patch/xdebug/3.1.6-init-func-args.patch'), calls.read_text().strip())
                else:self.assertFalse(calls.exists())
                self.assertEqual(failure == '', result.returncode == 0, result.stdout + result.stderr)
                self.assertEqual(failure == '', (root / 'handoff').exists())

    def test_migrated_xdebug_patch_preserves_actual_upstream_hook_definitions_and_calls_under_c23(self):
        upstream = json.loads((ROOT / 'tests/fixtures/xdebug-3.1.6-hooks.json').read_text())
        self.assertEqual('554eca0b4d5b7b93cb2258fab0b0bd84cc8721e74322a2255c14e137cbcad5d2', upstream['source']['sha256'])
        patch = ROOT / 'runtime-v2/patch/xdebug/3.1.6-init-func-args.patch'
        for hook in upstream['hooks'].values():
            path = self.root / hook['header_path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(hook['header_context'])
        source = self.root / 'hook-calls.c'
        source.write_text('#define INIT_FUNC_ARGS int type, int module_number\n'
            '#define INIT_FUNC_ARGS_PASSTHRU 1, 2\n'
            'typedef int xdebug_develop_globals_t;typedef int xdebug_gc_stats_globals_t;typedef int zend_op_array;\n'
            + ''.join('#include "' + hook['header_path'] + '"\n' for hook in upstream['hooks'].values())
            + ''.join(hook['definition'] + '{}\n' for hook in upstream['hooks'].values())
            + 'int main(void){' + ''.join(hook['call'] for hook in upstream['hooks'].values()) + 'return 0;}\n')
        command = ['/usr/bin/clang', '-std=gnu23', '-fsyntax-only', str(source)]
        before = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(0, before.returncode)
        patched = subprocess.run(['/usr/bin/patch', '--batch', '--forward', '-p1', '-i', str(patch)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(0, patched.returncode, patched.stdout + patched.stderr)
        after = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, after.returncode, after.stdout + after.stderr)
        self.assertEqual((ROOT / 'runtime/patch/xdebug/3.1.6-init-func-args.patch').read_bytes(), patch.read_bytes())
        print('XDEBUG_UPSTREAM_HOOKS ' + json.dumps({name: {key: hook[key] for key in ('declaration', 'definition', 'call')}
              for name, hook in upstream['hooks'].items()}))



class PostgreSQLModuleRecipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def test_postgis_configure_uses_common_protobuf_prefix_and_common_generator(self):
        source = self.root / "postgis-source"
        source.mkdir()
        common = self.root / "base/packages/common"
        (common / "bin").mkdir(parents=True)
        generator = common / "bin/protoc-c"
        generator.write_text("#!/bin/sh\nprintf 'common-protobuf-c\\n'\n")
        generator.chmod(0o755)
        sfcgal = common / "bin/sfcgal-config"
        sfcgal.write_text("#!/bin/sh\nprintf '2.1.0\\n'\n")
        sfcgal.chmod(0o755)
        prefix = self.root / "base/packages/postgresql/18"
        (prefix / "bin").mkdir(parents=True)
        config = prefix / "bin/pg_config"
        config.write_text("#!/bin/sh\ncase \"$1\" in --bindir) printf '%s\\n' " + shlex.quote(str(prefix / "bin")) +
                          ";; --pkglibdir) printf '%s\\n' " + shlex.quote(str(prefix / "lib")) +
                          ";; --sharedir) printf '%s\\n' " + shlex.quote(str(prefix / "share")) + ";; esac\n")
        config.chmod(0o755)
        capture = self.root / "configure.args"
        configure = source / "configure"
        configure.write_text('#!/bin/sh\nset -eu\nprintf "%s\\n" "$@" > "$CAPTURE"\n'
                             'command -v protoc-c > "$CAPTURE.generator"\n'
                             'protoc-c > "$CAPTURE.result"\n')
        configure.chmod(0o755)
        script = ["set -e", "SERVBAY_COMMON_PATH=" + shlex.quote(str(common)),
                  "SERVBAY_COMMON_INCLUDE_PATH=" + shlex.quote(str(common / "include")),
                  "SERVBAY_COMMON_LIB_PATH=" + shlex.quote(str(common / "lib")),
                  "SERVBAY_BIN_PATH=" + shlex.quote(str(self.root / "base/packages/bin")),
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(self.root)),
                  "SERVBAY_BUILD_SRC_DIR=" + shlex.quote(str(self.root / "src")),
                  "SOURCE=" + shlex.quote(str(source)), "PREFIX=" + shlex.quote(str(prefix)),
                  "export CAPTURE=" + shlex.quote(str(capture)),
                  'log_message() { :; }', 'download_source() { :; }',
                  'unpack_package() { builtin cd "$SOURCE"; }',
                  '_apply_single_patch() { :; }', 'cleanup_source_directory() { :; }',
                  'make() { if [[ $1 == install ]]; then mkdir -p "$PREFIX/lib" "$PREFIX/share/extension"; touch "$PREFIX/bin/postgis" "$PREFIX/lib/postgis_sfcgal-3.dylib" "$PREFIX/share/extension/postgis_sfcgal.control" "$PREFIX/share/extension/postgis_sfcgal--3.5.7.sql"; fi; }',
                  '_build_pgsql_mod_postgis() {\n' + self.extract(self.text, "_build_pgsql_mod_postgis") + '\n}',
                  '_build_pgsql_mod_postgis 3.5.7 18.6 18 "$PREFIX"']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)],
                                capture_output=True, text=True, env=dict(os.environ, PATH="/usr/bin:/bin"))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("--with-protobufdir=" + str(common), capture.read_text().splitlines())
        self.assertIn("--with-sfcgal=" + str(sfcgal), capture.read_text().splitlines())
        self.assertEqual(str(generator), Path(str(capture) + ".generator").read_text().strip())
        self.assertEqual("common-protobuf-c\n", Path(str(capture) + ".result").read_text())

    def test_pg_core_configure_preserves_caller_flags_and_disables_icu_symbol_renaming(self):
        source = self.root / "icu-source"
        (source / "src/include").mkdir(parents=True)
        (source / "src/include/pg_config_manual.h").write_text('#define DEFAULT_PGSOCKET_DIR  "/tmp"\n')
        capture = self.root / "configure.cppflags"
        configure = source / "configure"
        configure.write_text('#!/bin/sh\nprintf "%s\\n" "$CPPFLAGS" > "$CAPTURE"\n')
        configure.chmod(0o755)
        body = self.extract(self.text, "build_postgresql")
        # Keep the recipe's fixed diagnostic probes inside this test's sandbox.
        body = body.replace("/tmp/icu2", str(self.root / "icu2"))
        script = ["architecture=arm64", "CPU_NUMBER=2",
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(self.root)),
                  "SERVBAY_PACKAGE_FULL_PATH=" + shlex.quote(str(self.root / "packages")),
                  "SOURCE=" + shlex.quote(str(source)),
                  "export CAPTURE=" + shlex.quote(str(capture)),
                  "export CPPFLAGS='-DCALLER_SENTINEL=37 -I/caller/include'",
                  "log_message() { :; }", "trash_path() { :; }", "make() { return 0; }",
                  'unpack_package() { builtin cd "$SOURCE"; }',
                  'build_postgresql() {\n' + body + '\n}',
                  'build_postgresql 18.6 source.tar.bz2 ""']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        flags = shlex.split(capture.read_text())
        self.assertIn("-DCALLER_SENTINEL=37", flags)
        self.assertIn("-I/caller/include", flags)
        self.assertIn("-DU_DISABLE_RENAMING=1", flags)

    def test_pg_core_keeps_official_contrib_without_building_third_party_or_copying_runtime_libpq(self):
        source = self.root / "source"
        (source / "src/include").mkdir(parents=True)
        (source / "src/include/pg_config_manual.h").write_text('#define DEFAULT_PGSOCKET_DIR  "/tmp"\n')
        configure = source / "configure"
        configure.write_text("#!/bin/sh\nexit 0\n")
        configure.chmod(0o755)
        packages = self.root / "base/packages"
        prefix = packages / "postgresql/18"
        common = packages / "common/lib"
        common.mkdir(parents=True)
        calls = self.root / "calls"
        script = ["architecture=x86_64", "CPU_NUMBER=2", "typeset -A PGSQL_MODS",
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(self.root)),
                  "SERVBAY_PACKAGE_FULL_PATH=" + shlex.quote(str(packages)),
                  "SERVBAY_COMMON_LIB_PATH=" + shlex.quote(str(common)),
                  "SOURCE=" + shlex.quote(str(source)), "PREFIX=" + shlex.quote(str(prefix)),
                  "CALLS=" + shlex.quote(str(calls)), "log_message() { :; }", "trash_path() { :; }",
                  'unpack_package() { builtin cd "$SOURCE"; }',
                  'make() { print -r -- "$*" >> "$CALLS"; if [[ $1 == install-world || $1 == install-world-bin ]]; then mkdir -p "$PREFIX/share/extension" "$PREFIX/lib"; print official > "$PREFIX/share/extension/hstore.control"; print client > "$PREFIX/lib/libpq.5.dylib"; fi; }']
        for name in ("postgis", "pgrouting", "pgvector", "pg_jieba", "zhparser"):
            script.append('_build_pgsql_mod_' + name + '() { print third-party >> "$CALLS"; }')
        script.append('build_postgresql() {\n' + self.extract(self.text, "build_postgresql") + '\n}')
        script.append('build_postgresql 18.6 source.tar.bz2 ""')
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((prefix / "share/extension/hstore.control").is_file())
        self.assertIn("install-world", calls.read_text())
        self.assertNotIn("third-party", calls.read_text())
        self.assertFalse((packages / "postgresql/current").is_symlink())
        self.assertFalse((common / "libpq.5.dylib").exists())

    def test_pg_core_installs_official_binaries_and_pgxs_without_homebrew_docbook(self):
        source = self.root / "no-docbook-source"
        (source / "src/include").mkdir(parents=True)
        (source / "src/include/pg_config_manual.h").write_text('#define DEFAULT_PGSOCKET_DIR  "/tmp"\n')
        configure = source / "configure"
        configure.write_text("#!/bin/sh\nexit 0\n")
        configure.chmod(0o755)
        prefix = self.root / "packages/postgresql/18"
        calls = self.root / "binary-install-calls"
        script = ["architecture=x86_64", "CPU_NUMBER=2",
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(self.root)),
                  "SERVBAY_PACKAGE_FULL_PATH=" + shlex.quote(str(self.root / "packages")),
                  "SOURCE=" + shlex.quote(str(source)), "PREFIX=" + shlex.quote(str(prefix)),
                  "CALLS=" + shlex.quote(str(calls)), "log_message() { :; }", "trash_path() { :; }",
                  'unpack_package() { builtin cd "$SOURCE"; }',
                  'make() { print -r -- "$*" >> "$CALLS"; [[ $1 == install-world ]] && return 3; if [[ $1 == install-world-bin ]]; then mkdir -p "$PREFIX/share/extension" "$PREFIX/lib/pgxs/src/makefiles"; print official > "$PREFIX/share/extension/hstore.control"; print pgxs > "$PREFIX/lib/pgxs/src/makefiles/pgxs.mk"; fi; return 0; }',
                  'build_postgresql() {\n' + self.extract(self.text, "build_postgresql") + '\n}',
                  'build_postgresql 18.6 source.tar.bz2 ""']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((prefix / "share/extension/hstore.control").is_file())
        self.assertTrue((prefix / "lib/pgxs/src/makefiles/pgxs.mk").is_file())
        self.assertEqual(["-j2", "install-world-bin"], calls.read_text().splitlines())

    def test_pg_entry_collects_the_actual_pg_config_prefix_through_an_internal_alias(self):
        profile = self.root / "profile"
        scripts = profile / "scripts"
        scripts.mkdir(parents=True)
        entry = scripts / "build_module.sh"
        entry.write_bytes((ROOT / "runtime-v2/scripts/build_module.sh").read_bytes())
        source = self.text
        boundary = '# The standalone module producer reuses'
        self.assertEqual(1, source.count(boundary))
        fixture = '\nsetup_package_build_environment() { return 0; }\n_build_pgsql_mod_pgvector() { local d="$DESTDIR$($4/bin/pg_config --pkglibdir)"; mkdir -p "$d"; cp "$MODULE_NATIVE_FIXTURE" "$d/vector.dylib"; }\n'
        (scripts / "build_package.sh").write_text(source.replace(boundary, fixture + boundary, 1))
        actual = self.root / "physical-base"
        (actual / "packages/common").mkdir(parents=True)
        (actual / ".bootstrap-v2.json").write_text("{}\n")
        alias = self.root / "b"
        alias.symlink_to(actual, target_is_directory=True)
        prefix = actual / "packages/postgresql/18"
        (prefix / "bin").mkdir(parents=True)
        config = prefix / "bin/pg_config"
        config.write_text("#!/bin/sh\ncase \"$1\" in --bindir) printf '%s' " + shlex.quote(str(prefix / "bin")) +
                          ";; --pkglibdir) printf '%s' " + shlex.quote(str(prefix / "lib")) + ";; esac\n")
        config.chmod(0o755)
        stage = self.root / "stage"
        stage.mkdir()
        csource = self.root / "native.c"
        csource.write_text("int pg_fixture(void) { return 7; }\n")
        native = self.root / "native.dylib"
        subprocess.run(["/usr/bin/clang", "-dynamiclib", str(csource), "-o", str(native)], check=True, capture_output=True)
        env = dict(os.environ, SERVBAY_BASE_FULL_PATH=str(alias), SERVBAY_BUILD_ROOT=str(self.root / "work"), MODULE_NATIVE_FIXTURE=str(native))
        result = subprocess.run(["/bin/zsh", str(entry), "postgresql-extension", "pgvector", "0.8.7",
                                 str(alias / "packages/postgresql/18"), "18.6", "18", "x86_64", str(stage)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((Path(str(stage) + str(prefix)) / "lib/vector.dylib").is_file())
        self.assertFalse((prefix / "lib/vector.dylib").exists())
        self.assertIn("Scanned 1 Mach-O files", result.stderr)

    def test_pg_jieba_uses_pinned_release_and_gitlink_archive_without_mutable_git(self):
        archives = {}
        for name, payload_name in (("release", "pg_jieba.cpp"), ("component", "include/cppjieba/Jieba.hpp")):
            path = self.root / (name + ".tar.gz")
            with tarfile.open(path, "w:gz") as archive:
                row = tarfile.TarInfo(name + "/" + payload_name)
                data = name.encode()
                row.size = len(data)
                archive.addfile(row, io.BytesIO(data))
            archives[name] = path
        assets = self.root / "profile/config"
        assets.mkdir(parents=True)
        pin = {"url": "https://github.com/jaiminpan/pg_jieba/archive/refs/tags/v2.0.1.tar.gz",
               "sha256": hashlib.sha256(archives["release"].read_bytes()).hexdigest(),
               "component": {"url": "https://github.com/yanyiwu/cppjieba/archive/" + "a" * 40 + ".tar.gz",
                             "sha256": hashlib.sha256(archives["component"].read_bytes()).hexdigest()}}
        (assets / "modules.json").write_text(json.dumps({"recipes": {"pg_jieba": {"source_pins": {"2.0.1": pin}}}}))
        prefix = self.root / "base/packages/postgresql/18"
        (prefix / "bin").mkdir(parents=True)
        config = prefix / "bin/pg_config"
        headers = prefix / "include/server"
        headers.mkdir(parents=True)
        (headers / "pg_config.h").write_text('#define DLSUFFIX ".dylib"\n')
        config.write_text("#!/bin/sh\ncase \"$1\" in --includedir-server) printf '%s' " + shlex.quote(str(headers)) +
                          ";; *) printf '%s' " + shlex.quote(str(prefix / "lib")) + ";; esac\n")
        config.chmod(0o755)
        source_dir = self.root / "src"
        source_dir.mkdir()
        stage = self.root / "stage"
        script = ["CPU_NUMBER=2", "DESTDIR=" + shlex.quote(str(stage)),
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(self.root)), "SERVBAY_BUILD_SRC_DIR=" + shlex.quote(str(source_dir)),
                  "PROJECT_ROOT=" + shlex.quote(str(assets.parent)), "GTAR_BIN=/usr/bin/tar",
                  "PREFIX=" + shlex.quote(str(prefix)), "CALLS=" + shlex.quote(str(self.root / "calls")),
                  "MAIN=" + shlex.quote(str(archives["release"])), "COMPONENT=" + shlex.quote(str(archives["component"])),
                  "log_message() { :; }", "cleanup_source_directory() { :; }", "git() { return 17; }",
                  'download_source() { print -r -- "$1" >> "$CALLS"; [[ "$1" == *cppjieba* ]] && cp "$COMPONENT" "$2" || cp "$MAIN" "$2"; }',
                  'unpack_package() { mkdir -p "$SERVBAY_BUILD_SRC_DIR/$1"; /usr/bin/tar -xzf "$SERVBAY_BUILD_SRC_DIR/$2" --strip-components=1 -C "$SERVBAY_BUILD_SRC_DIR/$1"; builtin cd "$SERVBAY_BUILD_SRC_DIR/$1"; }',
                  'cmake() { print -r -- "$*" >> "$CALLS"; [[ -f libjieba/include/cppjieba/Jieba.hpp ]] || return 9; if [[ $1 == --install ]]; then mkdir -p "$DESTDIR$PREFIX/lib"; print native > "$DESTDIR$PREFIX/lib/pg_jieba.so"; fi; return 0; }',
                  '_install_pg_jieba_loader_alias() {\n' + self.extract(self.text, "_install_pg_jieba_loader_alias") + '\n}',
                  '_build_pgsql_mod_pg_jieba() {\n' + self.extract(self.text, "_build_pgsql_mod_pg_jieba") + '\n}',
                  '_build_pgsql_mod_pg_jieba 2.0.1 18.6 18 ' + shlex.quote(str(prefix))]
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = (self.root / "calls").read_text()
        self.assertIn(pin["url"], calls)
        self.assertIn(pin["component"]["url"], calls)
        self.assertIn("-DPostgreSQL_PG_CONFIG=" + str(config), calls)
        self.assertTrue((Path(str(stage) + str(prefix)) / "lib/pg_jieba.so").is_file())
        self.assertEqual(Path("pg_jieba.so"), (Path(str(stage) + str(prefix)) / "lib/pg_jieba.dylib").readlink())
        self.assertFalse((prefix / "lib/pg_jieba.so").exists())
        for changed in ("release", "component"):
            wrong = json.loads(json.dumps(pin))
            (wrong if changed == "release" else wrong["component"])["sha256"] = "0" * 64
            (assets / "modules.json").write_text(json.dumps({"recipes": {"pg_jieba": {"source_pins": {"2.0.1": wrong}}}}))
            result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode, changed + result.stdout + result.stderr)

    def test_vector_uses_exact_pgxs_pkglibdir_destdir_and_disables_native_cpu(self):
        source = self.root / "source"
        source.mkdir()
        prefix = self.root / "base/packages/postgresql/18"
        config = prefix / "bin/pg_config"
        config.parent.mkdir(parents=True)
        config.write_text("#!/bin/sh\ncase \"$1\" in --pkglibdir) printf '%s' " +
                          shlex.quote(str(prefix / "lib/server-extensions")) + ";; --libdir) printf '%s' " +
                          shlex.quote(str(prefix / "lib")) + ";; esac\n")
        config.chmod(0o755)
        stage = self.root / "stage"
        script = ["CPU_NUMBER=2", "DESTDIR=" + shlex.quote(str(stage)),
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(self.root)),
                  "SOURCE=" + shlex.quote(str(source)), "PREFIX=" + shlex.quote(str(prefix)),
                  "CALLS=" + shlex.quote(str(self.root / "calls")), "log_message() { :; }",
                  "download_source() { :; }", 'unpack_package() { builtin cd "$SOURCE"; }', "cleanup_source_directory() { :; }",
                  'make() { print -r -- "$*" >> "$CALLS"; if [[ $1 == install ]]; then mkdir -p "$DESTDIR$PREFIX/lib/server-extensions"; print native > "$DESTDIR$PREFIX/lib/server-extensions/vector.dylib"; fi; }',
                  "_build_pgsql_mod_pgvector() {\n" + self.extract(self.text, "_build_pgsql_mod_pgvector") + "\n}",
                  '_build_pgsql_mod_pgvector 0.8.1 18.6 18 ' + shlex.quote(str(prefix))]
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = (self.root / "calls").read_text().splitlines()
        self.assertEqual(2, len(calls))
        self.assertTrue(all("OPTFLAGS=" in line for line in calls))
        self.assertIn("DESTDIR=" + str(stage), calls[1])
        self.assertTrue((Path(str(stage) + str(prefix)) / "lib/server-extensions/vector.dylib").is_file())
        self.assertFalse((prefix / "lib/server-extensions/vector.dylib").exists())
