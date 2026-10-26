from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import test_build_scripts as legacy_tests

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "The actual module recipe requires Darwin/zsh")
class PostgisSfcgalRecipeTests(unittest.TestCase):
    def run_recipe(self, config=True, omitted=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        common, source, prefix = root / "common", root / "source", root / "postgresql"
        (common / "bin").mkdir(parents=True)
        (prefix / "bin").mkdir(parents=True)
        source.mkdir()
        if config:
            tool = common / "bin/sfcgal-config"
            tool.write_text("#!/bin/sh\nprintf '2.1.0\\n'\n")
            tool.chmod(0o755)
        pg_config = prefix / "bin/pg_config"
        pg_config.write_text("#!/bin/sh\ncase \"$1\" in --bindir) printf '%s' " + shlex.quote(str(prefix / "bin")) +
            ";; --pkglibdir) printf '%s' " + shlex.quote(str(prefix / "lib")) +
            ";; --sharedir) printf '%s' " + shlex.quote(str(prefix / "share")) + ";; esac\n")
        pg_config.chmod(0o755)
        configure = source / "configure"
        configure.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
        configure.chmod(0o755)
        extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function
        body = extract((ROOT / "runtime-v2/scripts/build_package.sh").read_text(), "_build_pgsql_mod_postgis")
        script = ["SOURCE=" + shlex.quote(str(source)), "PREFIX=" + shlex.quote(str(prefix)),
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(root)), "SERVBAY_COMMON_PATH=" + shlex.quote(str(common)),
                  "export CAPTURE=" + shlex.quote(str(root / "configure.args")), "OMITTED=" + shlex.quote(omitted),
                  "log_message() { :; }", "download_source() { :; }", "_apply_single_patch() { :; }",
                  "cleanup_source_directory() { :; }", 'unpack_package() { builtin cd "$SOURCE"; }',
                  '''make() {
                    if [[ $1 == install ]]; then
                        mkdir -p "$PREFIX/lib" "$PREFIX/share/extension"
                        touch "$PREFIX/bin/postgis"
                        local name
                        for name in lib/postgis_sfcgal-3.dylib share/extension/postgis_sfcgal.control share/extension/postgis_sfcgal--3.5.7.sql; do
                            [[ "$OMITTED" == "$name" ]] || touch "$PREFIX/$name"
                        done
                    fi
                    return 0
                  }''',
                  'postgis() {\n' + body + '\n}', 'postgis 3.5.7 18.6 18 "$PREFIX"']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        return result, root / "configure.args", common

    def test_recipe_requires_final_common_sfcgal_config_before_configure(self):
        result, capture, _ = self.run_recipe(config=False)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(capture.exists())

    def test_recipe_explicitly_selects_common_sfcgal(self):
        result, capture, common = self.run_recipe()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("--with-sfcgal=" + str(common / "bin/sfcgal-config"), capture.read_text().splitlines())

    def test_recipe_rejects_each_missing_sfcgal_product(self):
        for name in ("lib/postgis_sfcgal-3.dylib", "share/extension/postgis_sfcgal.control", "share/extension/postgis_sfcgal--3.5.7.sql"):
            with self.subTest(product=name):
                result, _, _ = self.run_recipe(omitted=name)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
