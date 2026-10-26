"""Offline source compatibility checks; inputs are exact upstream release trees.

Set SERVBAY_PHP86_SOURCE_ROOT and SERVBAY_PHP86_INCLUDE to run the source probes.
No downloads, package builds, installs, or PHP binaries are executed by this suite.
"""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

import test_build_scripts as legacy_tests

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {"memcached": "3.4.0", "apcu": "5.1.28", "phalcon": "5.23.0",
            "imap": "1.0.3", "scws": "1.2.4-pre", "xdebug": "3.4.3",
            "sqlsrv": "5.13.3", "pdo_sqlsrv": "5.13.3", "swoole": "6.1.7-88fc55029c"}


@unittest.skipUnless(os.environ.get("SERVBAY_PHP86_SOURCE_ROOT") and os.environ.get("SERVBAY_PHP86_INCLUDE"),
                     "requires predownloaded upstream sources and candidate PHP headers")
class PHP86SourceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="php86-compat-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = Path(os.environ["SERVBAY_PHP86_SOURCE_ROOT"])
        self.include = Path(os.environ["SERVBAY_PHP86_INCLUDE"])
        version_header = (self.include / "main/php_version.h").read_text()
        self.assertRegex(version_header, r'#define PHP_VERSION\s+"8\.6\.0RC4"')
        self.script = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function

    def prepare(self, recipe):
        version = VERSIONS[recipe]
        source = self.root / recipe
        shutil.copytree(self.sources / (recipe + "-" + version), source)
        prefix = self.root / "host"
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        phpize = prefix / "bin/phpize"
        phpize.write_text('#!/bin/sh\nprintf reached > "$PHPIZE_CAPTURE"\nexit 77\n')
        phpize.chmod(0o755)
        sentinel = self.root / (recipe + "-phpize")
        if recipe == "xdebug":
            phpize.write_text("#!/bin/sh\nexit 0\n")
            configure = source / "configure"
            configure.write_text('#!/bin/sh\nprintf reached > "$PHPIZE_CAPTURE"\nexit 77\n')
            configure.chmod(0o755)
        functions = []
        for name in ("_apply_single_patch", "_build_php_mod_" + recipe):
            functions.append(name + "() {\n" + self.extract(self.script, name) + "\n}")
        prelude = ["log_message() { :; }", "_download_pecl_source() { :; }",
                   "unpack_package() { builtin cd "+shlex.quote(str(source))+"; }",
                   "aclocal() { :; }; autoconf() { :; }; autoheader() { :; }; glibtoolize() { :; }; automake() { :; }",
                   "PATCH_DIR="+shlex.quote(str(ROOT / "runtime-v2/patch"))]
        command = "\n".join(prelude + functions + ["_build_php_mod_"+recipe+" "+shlex.quote(version)+" 8.6.0RC4 8.6 "+shlex.quote(str(prefix))])
        result = subprocess.run(["/bin/zsh", "-c", command], cwd=self.root,
                                env=dict(os.environ, PHPIZE_CAPTURE=str(sentinel)), capture_output=True, text=True)
        self.assertTrue(sentinel.is_file(), recipe + " did not reach phpize after applying real patches:\n" + result.stdout + result.stderr)
        return source

    def compile_probe(self, code, extra=(), cxx=False):
        source = self.root / ("probe.cc" if cxx else "probe.c")
        source.write_text('#include "php.h"\n#include "php_ini.h"\n#include "php_streams.h"\n' + code)
        command = ["/usr/bin/clang++" if cxx else "/usr/bin/clang", "-fsyntax-only", "-Werror=implicit-function-declaration"]
        command += ["-std=c++17"] if cxx else ["-std=gnu11"]
        for part in ("", "main", "Zend", "TSRM"):
            command += ["-I", str(self.include / part)]
        result = subprocess.run(command + list(extra) + [str(source)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_imap_scws_and_xdebug_ini_calls_compile_against_exact_rc4_headers(self):
        for recipe, filename in (("imap", "php_imap.c"), ("scws", "phpext/php_scws.c"), ("xdebug", "src/develop/stack.c")):
            with self.subTest(recipe=recipe):
                source = self.prepare(recipe)
                text = (source / filename).read_text()
                defines = "\n".join(line for line in text.splitlines() if re.match(r"#\s*define INI_", line))
                # Compile every upstream INI_STR argument, including Xdebug's cast.
                args = re.findall(r'INI_STR\(((?:\(char\*\)\s*)?"[^"\n]+")\)', text)
                self.assertTrue(args)
                self.compile_probe(defines + "\nvoid probe(void) {\n" + "\n".join("(void)INI_STR("+arg+");" for arg in args) + "\n}")

    def test_sqlsrv_ini_and_both_shared_stream_calls_compile(self):
        for recipe in ("sqlsrv", "pdo_sqlsrv"):
            with self.subTest(recipe=recipe):
                source = self.prepare(recipe)
                text = (source / "shared/core_stream.cpp").read_text()
                # Keep the real signature: an invented named context hides an upstream
                # unnamed php_stream_context* parameter and lets broken patches pass.
                start = text.index("static php_stream* sqlsrv_stream_opener(")
                end = text.index("\n}", start) + 2
                opener = text[start:end]
                dependencies = """
#define _In_opt_
#define _In_
#define SQLSRV_UNUSED(value) (void)(value)
struct sqlsrv_stream {};
template<class T> struct sqlsrv_malloc_auto_ptr {
    operator T*() const;
    T* operator=(T*);
    void transferred();
};
void *sqlsrv_malloc(size_t);
extern php_stream_ops sqlsrv_stream_ops;
"""
                self.compile_probe(dependencies + opener, cxx=True)
                if recipe == "sqlsrv":
                    lines = [line.strip() for line in (source / "init.cpp").read_text().splitlines() if re.search(r'SQLSRV_G\([^)]*\)\s*=.*(?:INI_|zend_ini_)',line)]
                    self.assertEqual(5, len(lines))
                    code = "#define SQLSRV_G(x) value\nvoid probe() { long value; char warnings_as_errors[]=\"a\", severity[]=\"b\", subsystems[]=\"c\", buffered_limit[]=\"d\", set_locale_info[]=\"e\";\n"+"\n".join(lines)+"\n}"
                    self.compile_probe(code, cxx=True)

    def test_apcu_complete_persist_translation_unit_compiles_without_removed_switch_macro(self):
        source = self.prepare("apcu")
        self.compile_probe("", extra=("-I", str(source), str(source / "apc_persist.c")))

    def test_swoole_process_pool_actual_parse_call_compiles_without_removed_throw_alias(self):
        source = self.prepare("swoole")
        text = (source / "ext-src/swoole_process_pool.cc").read_text()
        call = re.search(r"zend_parse_parameters(?:_throw)?\(ZEND_NUM_ARGS\(\), [^\n]*\)", text).group()
        declarations = re.search(r"zend_long worker_num;.*?zend_bool enable_coroutine[^;]*;", text, re.S).group()
        self.compile_probe("#define SW_IPC_NONE 0\nPHP_FUNCTION(probe) {\n" + declarations + "\n(void)" + call + ";\n}", cxx=True)

    def test_apcu_print_macro_compiles_with_the_rc4_php_verror_signature(self):
        source = self.prepare("apcu")
        text = (source / "apc.c").read_text()
        macro = re.search(r"#define APC_PRINT_FUNCTION[^\n]*(?:\n[^\n]*\\)*\n[^\n]*", text).group()
        self.compile_probe(macro + "\nAPC_PRINT_FUNCTION(probe, E_WARNING)\n")

    def test_memcached_current_source_accepts_all_session_save_path_uses(self):
        source = self.prepare("memcached")
        text = (source / "php_memcached_session.c").read_text()
        uses = [line.strip() for line in text.splitlines() if re.search(r"strstr\(|memcached_servers_parse\(|spprintf\(", line) and "save_path" in line]
        self.assertEqual(3, len(uses))
        code = "void *memcached_servers_parse(const char *);\nvoid probe(zend_string *save_path) { void *servers; char *plist_key; int plist_key_len;\n"
        code += uses[0].rstrip(" {") + " {}\n" + "\n".join(uses[1:]) + "\n}"
        self.compile_probe(code)
        destructors = re.findall(r"zval_(?:dtor|ptr_dtor_nogc)\(return_value\);", (source / "php_memcached.c").read_text())
        self.assertEqual(4, len(destructors))
        self.compile_probe("void probe(zval *return_value) {\n" + "\n".join(destructors) + "\n}")

    def test_phalcon_current_amalgamation_compiles_against_rc4_headers(self):
        source = self.prepare("phalcon") / "build/phalcon"
        flags = []
        for part in ("", "main", "Zend", "TSRM"):
            flags += ["-I", str(self.include / part)]
        result = subprocess.run(["/usr/bin/clang", "-fsyntax-only", "-std=gnu11", "-Werror=implicit-function-declaration", "-I", str(source)] + flags + [str(source / "phalcon.zep.c")], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, "\n".join(line for line in (result.stdout + result.stderr).splitlines() if "error:" in line or "note:" in line))

    def test_swoole_calls_compile_with_stream_and_parameter_api_changes(self):
        source = self.prepare("swoole")
        runtime = (source / "ext-src/swoole_runtime.cc").read_text()
        # Compile the actual six registration expressions; old void comparisons fail.
        calls = re.findall(r'(?:if \()?php_stream_xport_register\("(?:tcp|udp|unix|udg|ssl|tls)", socket_create\)[^\n]*', runtime)
        self.assertEqual(6, len(calls))
        code = "void probe(php_stream_transport_factory socket_create) {\n"
        code += "\n".join(call + "}" if call.startswith("if (") else call for call in calls) + "\n}"
        self.compile_probe(code, cxx=True)
        plain = (source / "thirdparty/php/streams/plain_wrapper.c").read_text()
        definitions = "\n".join(line for line in plain.splitlines() if line.startswith("#define php_error_docref"))
        calls = [line.strip() for line in plain.splitlines() if "php_error_docref1(" in line or "php_error_docref2(" in line or "php_stream_wrapper_log_" in line]
        calls = [line for line in calls if not line.startswith("#")]
        self.assertEqual(14, len(calls))
        code = definitions + '\nvoid probe(php_stream_wrapper php_plain_files_wrapper, int options, const char *mode, const char *url, const char *url_from, const char *url_to, const char *value) {\n' + "\n".join(calls) + "\n}"
        self.compile_probe(code, cxx=True)

    def test_swoole_selected_php84_curl_ini_lookups_compile(self):
        source = self.prepare("swoole")
        text = (source / "thirdparty/php84/curl/interface.cc").read_text()
        lookups = [line.strip() for line in text.splitlines() if re.search(r"cainfo = (?:INI_STR|zend_ini_string_literal)\(", line)]
        self.assertEqual(2, len(lookups))
        declaration = re.search(r"^\s*(?:const )?char \*cainfo;", text, re.M).group().strip()
        for call in lookups:
            with self.subTest(call=call):
                self.compile_probe("void probe() { " + declaration + " " + call + " }", cxx=True)

    def test_every_swoole_removed_parse_flag_site_compiles_with_actual_parameter_macros(self):
        source = self.prepare("swoole")
        upstream = self.sources / ("swoole-" + VERSIONS["swoole"])
        sites = []
        for original in sorted((upstream / "ext-src").glob("*.cc")):
            # Preserve line numbers while excluding comments from the inventory.
            code = re.sub(r"/\*.*?\*/|//[^\n]*", lambda match: "\n" * match.group().count("\n"),
                          original.read_text(), flags=re.S)
            for index, line in enumerate(code.splitlines()):
                if re.search(r"ZEND_PARSE_PARAMETERS_START_EX\(ZEND_PARSE_PARAMS_THROW,", line):
                    sites.append((original.relative_to(upstream), index))
        self.assertEqual(22, len(sites))
        for filename, index in sites:
            with self.subTest(filename=str(filename), line=index + 1):
                line = (source / filename).read_text().splitlines()[index]
                self.compile_probe("PHP_FUNCTION(probe) {\n" + line + "\nZEND_PARSE_PARAMETERS_END();\n}", cxx=True)


if __name__ == "__main__":
    unittest.main()
