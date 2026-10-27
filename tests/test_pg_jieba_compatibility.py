import hashlib
import io
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import test_build_scripts as legacy_tests

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


@unittest.skipUnless(sys.platform == "darwin", "The actual module recipe requires Darwin/zsh")
class PgJiebaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.extract = legacy_tests.BuildScriptTests(methodName="runTest")._extract_shell_function
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.contract = json.loads((FIXTURES / "pg_jieba-v1-compatibility.json").read_text())

    def test_selected_source_is_full_fixed_v1_compatible_closure(self):
        profile = json.loads((ROOT / "runtime-v2/config/modules.json").read_text())
        version = profile["hosts"]["postgresql"]["18"]["pg_jieba"]
        pin = profile["recipes"]["pg_jieba"]["source_pins"][version]
        self.assertEqual("1.1.1", pin.get("sql_version"))
        self.assertEqual({name: row["sha256"] for name, row in self.contract["files"].items()},
                         pin["compatibility_files"])
        for actual, expected in ((pin, self.contract["candidate_main"]),
                                 (pin["component"], self.contract["candidate_cppjieba"]),
                                 (pin["component"]["limonp"], self.contract["candidate_limonp"])):
            self.assertEqual(expected["sha256"], actual["sha256"])
            self.assertEqual(expected["url"].rsplit("/", 1)[-1], actual["url"].rsplit("/", 1)[-1])
        self.assertEqual(self.contract["files"]["share/extension/pg_jieba--1.1.1.sql"]["sha256"],
                         hashlib.sha256((FIXTURES / "pg_jieba-v1-1.1.1.sql").read_bytes()).hexdigest())

    def archive(self, name, files):
        target = self.root / (name + ".tar.gz")
        with tarfile.open(target, "w:gz") as archive:
            for path, data in files.items():
                member = tarfile.TarInfo(name + "/" + path)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return target

    def run_recipe(self, omitted="", corrupt="", corrupt_product="", missing_attachment="", loader_suffix=".dylib", loader_header=None, conflicting_loader=False):
        work = Path(tempfile.mkdtemp(dir=self.root))
        prefix = work / "base/packages/postgresql/18"
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        server_headers = prefix / "include/server"
        server_headers.mkdir(parents=True)
        (server_headers / "pg_config.h").write_text(loader_header if loader_header is not None else
            '/*\n#define DLSUFFIX ".wrong"\n*/\n#define DLSUFFIX "' + loader_suffix + '"\n')
        (prefix / "bin/pg_config").write_text("#!/bin/sh\ncase \"$1\" in --pkglibdir) printf '%s' " +
            shlex.quote(str(prefix / "lib")) + ";; --sharedir) printf '%s' " + shlex.quote(str(prefix / "share")) +
            ";; --includedir-server) printf '%s' " + shlex.quote(str(server_headers)) + ";; esac\n")
        (prefix / "bin/pg_config").chmod(0o755)
        main = self.archive("main", {"pg_jieba.control": b"default_version = '1.1.1'\n",
            "pg_jieba.sql": (FIXTURES / "pg_jieba-v1-1.1.1.sql").read_bytes()})
        component = self.archive("component", {"dict/" + name: name.encode() for name in
            ("jieba.dict.utf8", "hmm_model.utf8", "user.dict.utf8", "idf.utf8", "stop_words.utf8")})
        limonp = self.archive("limonp", {"include/limonp/Logging.hpp": b"real header fixture"})
        def pin(path):
            return {"url": "https://sources.invalid/" + path.name,
                    "sha256": "0" * 64 if corrupt == path.stem.split(".")[0] else hashlib.sha256(path.read_bytes()).hexdigest()}
        source_pin = pin(main)
        source_pin["sql_version"] = "1.1.1"
        source_pin["component"] = pin(component)
        source_pin["component"]["limonp"] = pin(limonp)
        source_pin["compatibility_files"] = {
            "share/extension/pg_jieba.control": hashlib.sha256(b"default_version = '1.1.1'\n").hexdigest(),
            "share/extension/pg_jieba--1.1.1.sql": hashlib.sha256((FIXTURES / "pg_jieba-v1-1.1.1.sql").read_bytes()).hexdigest(),
        }
        for installed, original in (("jieba_base.dict", "jieba.dict.utf8"), ("jieba_hmm.model", "hmm_model.utf8"),
                                    ("jieba_user.dict", "user.dict.utf8"), ("jieba.idf", "idf.utf8"), ("jieba.stop", "stop_words.utf8")):
            source_pin["compatibility_files"]["share/tsearch_data/" + installed] = hashlib.sha256(original.encode()).hexdigest()
        profile = work / "profile/config"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "modules.json").write_text(json.dumps({"recipes": {"pg_jieba": {"source_pins": {"test": source_pin}}}}))
        source_dir = work / "src"
        source_dir.mkdir(exist_ok=True)
        stage = work / "stage"
        calls = work / "calls"
        patches = work / "patch/pgsql"
        patches.mkdir(parents=True)
        for name in ("pg_jieba--1.1.0--1.1.1.sql",):
            if name != missing_attachment:
                shutil.copyfile(ROOT / "runtime-v2/patch/pgsql" / name, patches / name)
        script = ["CPU_NUMBER=2", "GTAR_BIN=/usr/bin/tar", "DESTDIR=" + shlex.quote(str(stage)),
                  "SERVBAY_BUILD_DIR=" + shlex.quote(str(self.root)),
                  "SERVBAY_BUILD_SRC_DIR=" + shlex.quote(str(source_dir)),
                  "PROJECT_ROOT=" + shlex.quote(str(profile.parent)),
                  "PATCH_DIR=" + shlex.quote(str(patches.parent)),
                  "PREFIX=" + shlex.quote(str(prefix)), "CALLS=" + shlex.quote(str(calls)),
                  "OMITTED=" + shlex.quote(omitted), "FIXTURE=" + shlex.quote(str(self.root)),
                  "CORRUPT_PRODUCT=" + shlex.quote(corrupt_product),
                  "CONFLICTING_LOADER=" + ("1" if conflicting_loader else ""),
                  "CMAKE_CALLS=" + shlex.quote(str(work / "cmake.calls")),
                  "log_message() { :; }", "cleanup_source_directory() { :; }", "git() { return 19; }",
                  'download_source() { print -r -- "$1" >> "$CALLS"; cp "$FIXTURE/${1##*/}" "$2"; }',
                  'unpack_package() { mkdir -p "$SERVBAY_BUILD_SRC_DIR/$1"; /usr/bin/tar -xzf "$SERVBAY_BUILD_SRC_DIR/$2" --strip-components=1 -C "$SERVBAY_BUILD_SRC_DIR/$1"; builtin cd "$SERVBAY_BUILD_SRC_DIR/$1"; }',
                  '''cmake() {
                    print -r -- "$*" >> "$CMAKE_CALLS"
                    [[ -f libjieba/deps/limonp/include/limonp/Logging.hpp ]] || return 7
                    if [[ $1 == --install ]]; then
                        local dest="$DESTDIR$PREFIX"
                        mkdir -p "$dest/lib" "$dest/share/extension" "$dest/share/tsearch_data"
                        print native > "$dest/lib/pg_jieba.so"
                        if [[ -n "$CONFLICTING_LOADER" ]]; then ln -s missing.so "$dest/lib/pg_jieba.dylib"; fi
                        cp pg_jieba.control "$dest/share/extension/"
                        cp pg_jieba.sql "$dest/share/extension/pg_jieba--1.1.1.sql"
                        local -a inputs=(jieba.dict.utf8 hmm_model.utf8 user.dict.utf8 idf.utf8 stop_words.utf8)
                        local -a outputs=(jieba_base.dict jieba_hmm.model jieba_user.dict jieba.idf jieba.stop)
                        local i
                        for i in {1..5}; do
                            [[ "$OMITTED" == "$outputs[$i]" ]] || cp "libjieba/dict/$inputs[$i]" "$dest/share/tsearch_data/$outputs[$i]"
                        done
                        if [[ -n "$CORRUPT_PRODUCT" ]]; then
                            print altered >> "$dest/$CORRUPT_PRODUCT"
                        fi
                    fi
                    return 0
                  }''',
                  '_install_pg_module_loader_alias() {\n' + self.extract(self.text, "_install_pg_module_loader_alias") + '\n}',
                  'build_pg_jieba() {\n' + self.extract(self.text, "_build_pgsql_mod_pg_jieba") + '\n}',
                  'build_pg_jieba test 18.6 18 "$PREFIX"']
        result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], capture_output=True, text=True)
        return result, Path(str(stage) + str(prefix)), calls

    def test_recipe_installs_real_upgrade_sql_and_v1_dictionary_layout(self):
        result, installed, calls = self.run_recipe()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(3, len(calls.read_text().splitlines()))
        self.assertFalse((installed / "share/extension/pg_jieba--1.1.0.sql").exists())
        self.assertEqual((FIXTURES / "pg_jieba-v1-1.1.1.sql").read_bytes(),
                         (installed / "share/extension/pg_jieba--1.1.1.sql").read_bytes())
        upgrade = installed / "share/extension/pg_jieba--1.1.0--1.1.1.sql"
        self.assertTrue(upgrade.is_file())
        self.assertEqual((ROOT / "runtime-v2/patch/pgsql/pg_jieba--1.1.0--1.1.1.sql").read_bytes(), upgrade.read_bytes())
        for name in ("jieba_base.dict", "jieba_hmm.model", "jieba_user.dict", "jieba.idf", "jieba.stop"):
            self.assertTrue((installed / "share/tsearch_data" / name).is_file(), name)

    def test_recipe_rejects_each_changed_source_before_build(self):
        for name in ("main", "component", "limonp"):
            with self.subTest(source=name):
                result, _, _ = self.run_recipe(corrupt=name)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_recipe_rejects_each_missing_legacy_dictionary(self):
        for name in ("jieba_base.dict", "jieba_hmm.model", "jieba_user.dict", "jieba.idf", "jieba.stop"):
            with self.subTest(dictionary=name):
                result, _, _ = self.run_recipe(omitted=name)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_recipe_rejects_each_changed_v1_control_sql_or_dictionary(self):
        for path in self.contract["files"]:
            with self.subTest(product=path):
                result, _, _ = self.run_recipe(corrupt_product=path)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_recipe_requires_real_upgrade_sql_attachment(self):
        result, _, _ = self.run_recipe(missing_attachment="pg_jieba--1.1.0--1.1.1.sql")
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_recipe_explicitly_selects_current_sdk_for_relocated_pg_config_flags(self):
        result, _, calls = self.run_recipe()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        sdk = subprocess.check_output(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        self.assertIn("-DCMAKE_OSX_SYSROOT=" + sdk, (calls.parent / "cmake.calls").read_text().splitlines()[0])

    def test_recipe_installs_relative_loader_alias_from_actual_host_suffix(self):
        for suffix in (".dylib", ".bundle"):
            with self.subTest(suffix=suffix):
                result, installed, _ = self.run_recipe(loader_suffix=suffix)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                alias = installed / ("lib/pg_jieba" + suffix)
                self.assertTrue(alias.is_symlink())
                self.assertEqual(Path("pg_jieba.so"), alias.readlink())
                self.assertEqual((installed / "lib/pg_jieba.so").read_bytes(), alias.read_bytes())
                self.assertFalse((installed / "lib/pg_jieba.wrong").exists())

    def test_matching_host_so_suffix_keeps_real_library_without_self_link(self):
        result, installed, _ = self.run_recipe(loader_suffix=".so")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((installed / "lib/pg_jieba.so").is_symlink())
        self.assertEqual(b"native\n", (installed / "lib/pg_jieba.so").read_bytes())

    def test_missing_ambiguous_or_unsafe_host_suffix_fails(self):
        for header in ('/* #define DLSUFFIX ".dylib" */', '#define DLSUFFIX "../escape"\n',
                       '#define DLSUFFIX ".dylib"\n#define DLSUFFIX ".so"\n'):
            with self.subTest(header=header):
                result, _, _ = self.run_recipe(loader_header=header)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("Expected one safe PostgreSQL DLSUFFIX", result.stderr)

    def test_existing_broken_loader_alias_is_rejected_without_overwriting_it(self):
        result, installed, _ = self.run_recipe(conflicting_loader=True)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(Path("missing.so"), (installed / "lib/pg_jieba.dylib").readlink())


if __name__ == "__main__":
    unittest.main()
