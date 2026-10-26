import copy
import hashlib
import importlib.util
import io
import json
import os
import platform
import shlex
import subprocess
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASE = {"version": "2.0.1", "runtime_sha256": "a" * 64, "devlib_sha256": "b" * 64}
PHP_ABI = {"module_api": 20240924, "zend_extension_api": 420240924,
           "extension_build": "API20240924,NTS", "zend_extension_build": "API420240924,NTS",
           "zts": False, "debug": False}


class ModuleManifestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()

    def helper(self):
        spec = importlib.util.spec_from_file_location("tested_module_manifest", ROOT / "runtime-v2/ci/module_manifest.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def host(self, kind="php"):
        if kind == "php":
            return {"type": "php", "series": "8.4", "version": "8.4.27",
                    "package_sha256": "c" * 64, "abi": copy.deepcopy(PHP_ABI)}
        return {"type": "postgresql", "series": "18", "version": "18.6",
                "package_sha256": "d" * 64, "abi": {"major": 18}}

    def file(self, name, contents=b"module payload", mode=0o644):
        path = self.staging / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.chmod(mode)
        return path

    def php_manifest(self):
        self.file("redis.so", b"redis native module bytes", 0o755)
        config = {"scope": "config", "install_path": "conf.d/redis.ini", "mode": "0644",
                  "content": "; redis settings\nextension = redis.so\n"}
        return self.helper().create_manifest(self.host(), self.staging, [config], ["redis"], [],
                                            arch="x86_64", minimum_os="12.0", base=BASE,
                                            source_name="phpredis", extension_dir="lib/custom-extension-location")

    def pg_manifest(self):
        self.file("lib/postgresql/vector.dylib")
        self.file("share/postgresql/extension/vector.control", b"default_version = '0.8.1'\n")
        self.file("share/postgresql/extension/vector--0.7.4.sql", b"CREATE TYPE vector;\n")
        self.file("share/postgresql/extension/vector--0.7.4--0.8.1.sql", b"-- real upgrade member\n")
        return self.helper().create_manifest(self.host("postgresql"), self.staging, [], ["vector"], [],
                                            arch="x86_64", minimum_os="12.0", base=BASE, source_name="pgvector")

    def archive(self, manifest):
        path = self.root / "module.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for row in manifest["files"]:
                archive.add(self.staging / row["archive_path"], arcname=row["archive_path"], recursive=False)
        return path

    def test_php_manifest_uses_exact_host_apis_and_actual_extension_relative_path(self):
        manifest = self.php_manifest()
        self.assertEqual(self.host(), manifest["host"])
        self.assertNotEqual(manifest["host"]["abi"]["module_api"], manifest["host"]["abi"]["zend_extension_api"])
        self.assertEqual("phpredis", manifest["source_name"])
        self.assertEqual(["redis"], manifest["provides"])
        self.assertEqual("redis.so", manifest["files"][0]["archive_path"])
        self.assertEqual("lib/custom-extension-location/redis.so", manifest["files"][0]["install_path"])
        self.assertEqual("0755", manifest["files"][0]["mode"])
        self.assertEqual(hashlib.sha256(b"redis native module bytes").hexdigest(), manifest["files"][0]["sha256"])
        self.assertEqual(hashlib.sha256(manifest["config_files"][0]["content"].encode()).hexdigest(), manifest["config_files"][0]["sha256"])
        self.helper().validate_manifest(manifest, self.archive(manifest))

    def test_postgresql_preserves_initial_old_version_and_complete_upgrade_payload(self):
        manifest = self.pg_manifest()
        self.assertEqual({"major": 18}, manifest["host"]["abi"])
        self.assertEqual("pgvector", manifest["source_name"])
        self.assertEqual(["vector"], manifest["provides"])
        self.assertEqual({row["archive_path"] for row in manifest["files"]},
                         {str(path.relative_to(self.staging)) for path in self.staging.rglob("*") if path.is_file()})
        self.helper().validate_manifest(manifest, self.archive(manifest))

    def test_postgresql_rejects_missing_control_and_upgrade_only_payload_separately(self):
        for missing in ("vector.control", "vector--0.7.4.sql"):
            with self.subTest(missing=missing):
                manifest = self.pg_manifest()
                manifest["files"] = [row for row in manifest["files"] if Path(row["archive_path"]).name != missing]
                with self.assertRaises(ValueError):
                    self.helper().validate_manifest(manifest, self.archive(manifest))

    def test_postgresql_rejects_changed_upgrade_sql_and_unowned_archive_files(self):
        manifest = self.pg_manifest()
        package = self.archive(manifest)
        self.file("share/postgresql/extension/vector--0.7.4--0.8.1.sql", b"changed upgrade SQL")
        with self.assertRaises(ValueError):
            self.helper().validate_manifest(manifest, self.archive(manifest))
        # Restore exactly the manifest bytes before adding a genuinely unowned file.
        self.file("share/postgresql/extension/vector--0.7.4--0.8.1.sql", b"-- real upgrade member\n")
        with tarfile.open(package, "w:gz") as archive:
            for row in manifest["files"]:
                archive.add(self.staging / row["archive_path"], arcname=row["archive_path"], recursive=False)
            extra = tarfile.TarInfo("share/postgresql/extension/unowned.sql")
            extra.size = 5
            archive.addfile(extra, io.BytesIO(b"extra"))
        with self.assertRaises(ValueError):
            self.helper().validate_manifest(manifest, package)

    def test_manifest_rejects_escaping_paths_links_and_build_machine_ini(self):
        manifest = self.php_manifest()
        for path in ("../redis.so", "/outside/redis.so", "lib/../../redis.so"):
            with self.subTest(path=path):
                bad = copy.deepcopy(manifest)
                bad["files"][0]["install_path"] = path
                with self.assertRaises(ValueError):
                    self.helper().validate_manifest(bad, self.archive(manifest))
        for content in ("extension=/Users/servbay/build/redis.so\n", "zend_extension=/tmp/redis.so\n",
                        "extension=/opt/foreign/redis.so\n", "cache_dir=/Users/runner/work/tmp\nextension=redis.so\n"):
            with self.subTest(content=content):
                bad = copy.deepcopy(manifest)
                bad["config_files"][0].update(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
                with self.assertRaises(ValueError):
                    self.helper().validate_manifest(bad, self.archive(manifest))
        target = self.staging / "escape.so"
        target.symlink_to("../../outside.so")
        with self.assertRaises(ValueError):
            self.helper().create_manifest(self.host(), self.staging, manifest["config_files"], ["redis"], [],
                                          arch="x86_64", minimum_os="12.0", base=BASE,
                                          source_name="phpredis", extension_dir="lib/extensions")

    def test_complete_ini_keeps_normal_user_settings_with_a_system_temporary_directory(self):
        manifest = self.php_manifest()
        config = manifest["config_files"][0]
        config["content"] += "session.save_path=/tmp/servbay-sessions\n"
        config["sha256"] = hashlib.sha256(config["content"].encode()).hexdigest()
        self.helper().validate_manifest(manifest, self.archive(manifest))

    def test_module_package_emits_real_source_tar_and_hashed_manifest_for_exact_identity(self):
        helper = self.helper()
        manifest = self.php_manifest()
        source = self.root / "redis-6.3.0.tgz"
        source.write_bytes(b"actual independent upstream input archive")
        identity = {"runtime_version": "v2", "os": "macos", "kind": "php-extension", "name": "redis",
                    "version": "6.3.0", "arch": "x86_64", "build_revision": 1,
                    "host": {key: manifest["host"][key] for key in ("type", "series", "abi")}}
        producer = {"commit": "e" * 40, "run_id": "12345", "run_attempt": 2}
        result = helper.write_package(manifest, self.staging, identity, producer,
                                      {"version": "6.3.0", "path": str(source)}, self.root / "dist")
        metadata = result["meta"]
        self.assertEqual("ext-redis-6.3.0-php8.4-x86_64.tar.gz", metadata["filename"])
        self.assertEqual(1, metadata["build_revision"])
        self.assertIn("host", metadata, "The existing state collector needs the exact compatibility identity")
        self.assertEqual(identity["host"], metadata["host"])
        self.assertEqual(producer, metadata["producer"])
        self.assertEqual(BASE, metadata["base"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), metadata["source"]["sha256"])
        package = self.root / "dist" / metadata["filename"]
        self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(), metadata["sha256"])
        attachment = self.root / "dist" / metadata["manifest_filename"]
        self.assertEqual(hashlib.sha256(attachment.read_bytes()).hexdigest(), metadata["manifest_sha256"])
        self.assertEqual(manifest, json.loads(attachment.read_text()))
        helper.validate_manifest(manifest, package)


class HostProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prefix = self.root / "exact host"
        (self.prefix / "bin").mkdir(parents=True)
        self.arch = platform.machine()

    def helper(self):
        spec = importlib.util.spec_from_file_location("tested_module_host", ROOT / "runtime-v2/ci/module_host.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper

    def tool(self, name, values):
        script = "#!/bin/sh\ncase \"$1\" in\n" + "\n".join(
            "  " + flag + ") printf '%s\\n' " + shlex.quote(value) + ";;" for flag, value in values.items())
        path = self.prefix / "bin" / name
        path.write_text(script + "\n  *) exit 9;;\nesac\n")
        path.chmod(0o755)

    def php_fixture(self, *, zts=False, debug=False, header_api=20240924):
        include = self.prefix / "include/php"
        (include / "Zend").mkdir(parents=True, exist_ok=True)
        (include / "Zend/zend_modules.h").write_text(f"#define ZEND_MODULE_API_NO {header_api}\n")
        (include / "Zend/zend_extensions.h").write_text("#define ZEND_EXTENSION_API_NO 420240924\n")
        self.tool("php-config", {"--version": "8.4.27", "--prefix": str(self.prefix),
                                "--extension-dir": str(self.prefix / "lib/custom-location"),
                                "--include-dir": str(include)})
        self.tool("phpize", {"--version": "Configuring for:\nPHP Api Version: 20240924\nZend Module Api No: 20240924\nZend Extension Api No: 420240924"})
        flags = ("TS" if zts else "NTS") + (",debug" if debug else "")
        info = (f"PHP API => 20240924\nPHP Extension => 20240924\nZend Extension => 420240924\n"
                f"PHP Extension Build => API20240924,{flags}\nZend Extension Build => API420240924,{flags}\n"
                f"Thread Safety => {'enabled' if zts else 'disabled'}\nDebug Build => {'yes' if debug else 'no'}\n")
        source = self.root / "php-fixture.c"
        source.write_text('#include <stdio.h>\n#include <string.h>\nint main(int argc,char**argv){'
                          'if(argc==3&&!strcmp(argv[1],"-n")&&!strcmp(argv[2],"-i")){fputs('
                          + json.dumps(info) + ',stdout);return 0;}'
                          'if(argc==4&&!strcmp(argv[1],"-n")&&!strcmp(argv[2],"-r")'
                          '&&!strcmp(argv[3],"echo PHP_VERSION;")){fputs("8.4.27",stdout);return 0;}return 9;}')
        subprocess.run(["/usr/bin/clang", "-arch", self.arch, str(source), "-o", str(self.prefix / "bin/php")],
                       capture_output=True, check=True)

    def host_archive(self, *, foreign=False, escaping_link=False):
        source = self.root / "host.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            entry = tarfile.TarInfo("php/8.4/VERSION" if not foreign else "php/8.3/VERSION")
            data = b"8.4.27\n"
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
            if escaping_link:
                link = tarfile.TarInfo("php/8.4/escape")
                link.type, link.linkname = tarfile.SYMTYPE, "../../outside"
                archive.addfile(link)
        return source

    def test_actual_pg_archive_uses_server_header_identity_without_a_synthetic_version_file(self):
        helper = self.helper()
        for actual in ("18.6(ServBay)", "18.5(ServBay)", None):
            with self.subTest(actual=actual):
                archive_path = self.root / ("pg-" + str(actual) + ".tar.gz")
                with tarfile.open(archive_path, "w:gz") as archive:
                    row = tarfile.TarInfo("postgresql/18/include/server/pg_config.h")
                    text = ('#define PG_VERSION "' + actual + '"\n#define PG_VERSION_NUM 180006\n') if actual else "/* no actual version */\n"
                    data = text.encode()
                    row.size = len(data)
                    archive.addfile(row, io.BytesIO(data))
                destination = self.root / ("pg-extracted-" + str(actual))
                if actual == "18.6(ServBay)":
                    prefix = helper.extract_host_archive(archive_path, hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                                                         destination, "postgresql", "18", "18.6")
                    self.assertTrue((prefix / "include/server/pg_config.h").is_file())
                    self.assertFalse((prefix / "VERSION").exists())
                else:
                    with self.assertRaises(ValueError):
                        helper.extract_host_archive(archive_path, hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                                                    destination, "postgresql", "18", "18.6")
                    self.assertFalse((destination / "postgresql/18").exists())

    def test_exact_host_extract_verifies_tar_bytes_and_never_merges_a_preexisting_host(self):
        source = self.host_archive()
        helper = self.helper()
        destination = self.root / "extracted"
        with self.assertRaises(ValueError):
            helper.extract_host_archive(source, "d" * 64, destination, "php", "8.4", "8.4.27")
        self.assertFalse(destination.exists())
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        prefix = helper.extract_host_archive(source, expected, destination, "php", "8.4", "8.4.27")
        self.assertEqual(destination / "php/8.4", prefix)
        self.assertEqual("8.4.27\n", (prefix / "VERSION").read_text())
        sentinel = prefix / "user.txt"
        sentinel.write_text("keep")
        with self.assertRaises(ValueError):
            helper.extract_host_archive(source, expected, destination, "php", "8.4", "8.4.27")
        self.assertEqual("keep", sentinel.read_text())

    def test_exact_host_extract_rejects_other_series_and_escaping_tar_links(self):
        for kind in ("foreign", "escaping"):
            with self.subTest(kind=kind):
                source = self.host_archive(foreign=kind == "foreign", escaping_link=kind == "escaping")
                destination = self.root / kind
                with self.assertRaises(ValueError):
                    self.helper().extract_host_archive(source, hashlib.sha256(source.read_bytes()).hexdigest(),
                                                       destination, "php", "8.4", "8.4.27")
                self.assertFalse(destination.exists())

    def pg_fixture(self, version="18.6", major=18, header_major=None):
        includes = self.prefix / "include/postgresql/server"
        includes.mkdir(parents=True, exist_ok=True)
        (includes / "pg_config.h").write_text(f'#define PG_VERSION "{version}(ServBay)"\n'
                                            f'#define PG_VERSION_NUM {(major if header_major is None else header_major) * 10000}\n')
        pgxs = self.prefix / "lib/pgxs/src/makefiles/pgxs.mk"
        pgxs.parent.mkdir(parents=True, exist_ok=True)
        pgxs.write_text("# Actual host PGXS input\n")
        self.tool("pg_config", {"--version": f"PostgreSQL {version}(ServBay)", "--bindir": str(self.prefix / "bin"),
                               "--includedir-server": str(includes), "--pgxs": str(pgxs),
                               "--pkglibdir": str(self.prefix / "lib/server-modules"),
                               "--sharedir": str(self.prefix / "share/server-resources")})
        source = self.root / "postgres-fixture.c"
        source.write_text("int main(void){return 0;}\n")
        subprocess.run(["/usr/bin/clang", "-arch", self.arch, str(source), "-o", str(self.prefix / "bin/postgres")],
                       capture_output=True, check=True)
        return includes, pgxs

    def test_postgresql_probe_reads_actual_pg_config_headers_pgxs_and_preview_alias(self):
        for actual, requested, major in (("18.6", "18.6", 18), ("19beta4", "19.beta4", 19)):
            with self.subTest(actual=actual):
                self.pg_fixture(actual, major)
                result = self.helper().probe_postgresql_host(self.prefix, requested, "d" * 64, self.arch)
                self.assertEqual({"type": "postgresql", "series": str(major), "version": actual,
                                  "package_sha256": "d" * 64, "abi": {"major": major}}, result["host"])
                self.assertEqual("lib/server-modules", result["pkglibdir"])
                self.assertEqual("share/server-resources", result["sharedir"])
                self.assertEqual("lib/pgxs/src/makefiles/pgxs.mk", result["pgxs"])
                self.assertEqual(str(self.prefix), result["install_prefix"])

    def test_postgresql_probe_rejects_other_major_headers_and_missing_pgxs(self):
        includes, pgxs = self.pg_fixture(header_major=17)
        with self.assertRaises(ValueError):
            self.helper().probe_postgresql_host(self.prefix, "18.6", "d" * 64, self.arch)
        (includes / "pg_config.h").write_text('#define PG_VERSION "18.6(ServBay)"\n#define PG_VERSION_NUM 180000\n')
        pgxs.rename(pgxs.with_name("retained-original.mk"))
        with self.assertRaises(ValueError):
            self.helper().probe_postgresql_host(self.prefix, "18.6", "d" * 64, self.arch)

    def test_php_and_postgresql_tools_cannot_report_another_host_prefix(self):
        self.php_fixture()
        self.tool("php-config", {"--version": "8.4.27", "--prefix": str(self.root / "foreign"),
                  "--extension-dir": str(self.prefix / "lib/custom-location"), "--include-dir": str(self.prefix / "include/php")})
        with self.assertRaisesRegex(ValueError, "another host prefix"):
            self.helper().probe_php_host(self.prefix, "8.4.27", "c" * 64, self.arch)
        self.pg_fixture()
        self.tool("pg_config", {"--version": "PostgreSQL 18.6(ServBay)", "--bindir": str(self.root / "foreign/bin")})
        with self.assertRaisesRegex(ValueError, "another actual server prefix"):
            self.helper().probe_postgresql_host(self.prefix, "18.6", "c" * 64, self.arch)

    def test_php_probe_reads_actual_distinct_apis_and_uses_php_config_extension_path(self):
        self.php_fixture()
        result = self.helper().probe_php_host(self.prefix, "8.4.27", "c" * 64, self.arch)
        self.assertEqual(PHP_ABI, result["host"]["abi"])
        self.assertEqual("lib/custom-location", result["extension_dir"])
        self.assertEqual("8.4.27", result["host"]["version"])
        self.assertEqual("c" * 64, result["host"]["package_sha256"])

    def test_php_probe_reads_zts_and_debug_flags_instead_of_assuming_release_defaults(self):
        self.php_fixture(zts=True, debug=True)
        result = self.helper().probe_php_host(self.prefix, "8.4.27", "c" * 64, self.arch)
        self.assertTrue(result["host"]["abi"]["zts"])
        self.assertTrue(result["host"]["abi"]["debug"])
        self.assertEqual("API20240924,TS,debug", result["host"]["abi"]["extension_build"])

    def test_php_probe_rejects_header_api_mismatch_and_wrong_requested_version(self):
        self.php_fixture(header_api=20190902)
        with self.assertRaises(ValueError):
            self.helper().probe_php_host(self.prefix, "8.4.27", "c" * 64, self.arch)
        (self.prefix / "include/php/Zend/zend_modules.h").write_text("#define ZEND_MODULE_API_NO 20240924\n")
        with self.assertRaises(ValueError):
            self.helper().probe_php_host(self.prefix, "8.4.28", "c" * 64, self.arch)

    def test_php_probe_rejects_extension_location_outside_the_exact_host_prefix(self):
        self.php_fixture()
        self.tool("php-config", {"--version": "8.4.27", "--prefix": str(self.prefix),
                                "--extension-dir": str(self.root / "foreign-extension-location"),
                                "--include-dir": str(self.prefix / "include/php")})
        with self.assertRaises(ValueError):
            self.helper().probe_php_host(self.prefix, "8.4.27", "c" * 64, self.arch)


if __name__ == "__main__":
    unittest.main()


class ModuleProducerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def helper(self):
        spec = importlib.util.spec_from_file_location("tested_module_producer", ROOT / "runtime-v2/ci/build_module.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_php_collection_copies_only_fresh_stage_and_all_complete_ini_templates(self):
        stage, prefix = self.root / "stage", self.root / "base/packages/php/8.4"
        extension = prefix / "lib/actual-extensions"
        (Path(str(stage) + str(extension))).mkdir(parents=True)
        (Path(str(stage) + str(extension)) / "redis.so").write_bytes(b"fresh")
        (prefix / "lib/actual-extensions").mkdir(parents=True)
        (extension / "unrelated.so").write_bytes(b"old core module")
        ini = stage / "etc/php/8.4/conf.d/redis.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text("extension=redis.so\nredis.arrays.names = users\n")
        payload, configs, provides = self.helper().collect_php(stage, prefix, "8.4", "lib/actual-extensions", self.root / "payload")
        self.assertEqual(["redis.so"], [path.name for path in payload.iterdir()])
        self.assertEqual(b"fresh", (payload / "redis.so").read_bytes())
        self.assertEqual(["redis"], provides)
        self.assertEqual("conf.d/redis.ini", configs[0]["install_path"])
        self.assertEqual(ini.read_text(), configs[0]["content"])
        self.assertEqual(b"old core module", (extension / "unrelated.so").read_bytes())

    def test_postgresql_collection_keeps_all_pgxs_destdir_resources_and_real_control_provides(self):
        stage, prefix = self.root / "stage", self.root / "base/packages/postgresql/18"
        installed = Path(str(stage) + str(prefix))
        for name in ("lib/postgresql/vector.dylib", "share/postgresql/extension/vector.control",
                     "share/postgresql/extension/vector--0.7.0.sql", "share/postgresql/extension/vector--0.7.0--0.8.1.sql",
                     "share/postgresql/extension/vector/extra.dict"):
            path = installed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("default_version = '0.8.1'\n" if path.suffix == ".control" else name)
        payload, provides = self.helper().collect_postgresql(stage, prefix, self.root / "payload")
        self.assertEqual(["vector"], provides)
        self.assertEqual({str(p.relative_to(installed)) for p in installed.rglob("*") if p.is_file()},
                         {str(p.relative_to(payload)) for p in payload.rglob("*") if p.is_file()})

    def test_primary_source_requires_one_real_recipe_download_not_the_packaged_artifact(self):
        helper = self.helper()
        source = self.root / "src"
        source.mkdir()
        (self.root / "ext-redis-6.3.0-php8.4-x86_64.tar.gz").write_bytes(b"final archive")
        with self.assertRaises(ValueError):
            helper.primary_source(self.root, "phpredis", "6.3.0")
        archive = source / "phpredis-6.3.0.tgz"
        archive.write_bytes(b"actual upstream bytes")
        self.assertEqual({"version": "6.3.0", "path": str(archive)}, helper.primary_source(self.root, "phpredis", "6.3.0"))
        (source / "phpredis-6.3.0.tar.gz").write_bytes(b"different source")
        with self.assertRaises(ValueError):
            helper.primary_source(self.root, "phpredis", "6.3.0")

    def test_phalcon_payload_loads_its_actual_bundled_psr_before_phalcon(self):
        prefix, payload = self.root / "host", self.root / "payload"
        (prefix / "bin").mkdir(parents=True)
        payload.mkdir()
        php = prefix / "bin/php"
        php.write_text("#!/bin/sh\ncase \"$*\" in *psr.so*phalcon.so*) printf '%s' '[[\"phalcon\",\"4.1.2\"],[\"psr\",\"1.1.0\"]]' ;; *) exit 9 ;; esac\n")
        php.chmod(0o755)
        self.assertEqual([["phalcon", "4.1.2"], ["psr", "1.1.0"]],
                         self.helper().verify_php_payload(prefix, payload, ["phalcon", "psr"]))

    def test_php_startup_diagnostics_report_the_reason_and_still_block_publication(self):
        prefix, payload = self.root / "host", self.root / "payload"
        (prefix / "bin").mkdir(parents=True)
        payload.mkdir()
        php = prefix / "bin/php"
        php.write_text("#!/bin/sh\ncase \"$*\" in *display_errors=stderr*) "
                       "printf '%s' 'Deprecated: fixture startup reason' >&2; "
                       "printf '%s' '[[\"phalcon\",\"5.23.0\"]]' ;; *) "
                       "printf '\\nDeprecated: fixture startup reason\\n[[\"phalcon\",\"5.23.0\"]]' ;; esac\n")
        php.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "PHP module loading emitted diagnostics: Deprecated: fixture startup reason"):
            self.helper().verify_php_payload(prefix, payload, ["phalcon"])

    def test_phalcon_session_behavior_failure_blocks_an_otherwise_loaded_module(self):
        prefix, payload = self.root / "host", self.root / "payload"
        (prefix / "bin").mkdir(parents=True)
        payload.mkdir()
        php = prefix / "bin/php"
        for status, diagnostic in ((0, "fixture SID behavior failed"), (1, "fixture SID behavior failed"), (1, "")):
            with self.subTest(status=status, diagnostic=diagnostic):
                php.write_text("#!/bin/sh\ncase \"$*\" in *.php) "
                               "printf '%s' " + shlex.quote(diagnostic) + " >&2; exit " + str(status) + ";; *) "
                               "printf '%s' '[[\"phalcon\",\"5.23.0\"]]' ;; esac\n")
                php.chmod(0o755)
                reason = diagnostic or "PHP exited " + str(status)
                with self.assertRaisesRegex(ValueError, "Phalcon session verification failed: " + reason):
                    self.helper().verify_php_payload(prefix, payload, ["phalcon"], host_version="8.6.0RC4")

    def test_older_phalcon_hosts_do_not_execute_the_php86_session_check(self):
        prefix, payload = self.root / "host", self.root / "payload"
        (prefix / "bin").mkdir(parents=True)
        payload.mkdir()
        php = prefix / "bin/php"
        php.write_text("#!/bin/sh\ncase \"$*\" in *.php) exit 9 ;; *) "
                       "printf '%s' '[[\"phalcon\",\"3.4.5\"]]' ;; esac\n")
        php.chmod(0o755)
        for version in ("5.6.40", "7.2.34", "8.5.12"):
            with self.subTest(version=version):
                self.assertEqual([["phalcon", "3.4.5"]],
                                 self.helper().verify_php_payload(prefix, payload, ["phalcon"], host_version=version))

    def test_pg_provides_rejects_missing_or_invalid_actual_control_default_version(self):
        for text in ("# default_version = '0.8.1'\n", "default_version='0.8.1'\ndefault_version='0.8.2'\n"):
            with self.subTest(control=text):
                path = self.root / "control/vector.control"
                path.parent.mkdir(exist_ok=True)
                path.write_text(text)
                with self.assertRaises(ValueError):
                    self.helper().control_provides(path.parent)


    def test_existing_exact_core_is_reused_only_when_every_owned_byte_link_and_mode_matches(self):
        host = self.helper()._hosts
        prefix = self.root / "base/packages/php/8.4"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "VERSION").write_text("8.4.27\n")
        binary = prefix / "bin/php"
        binary.write_bytes(b"exact host bytes")
        binary.chmod(0o755)
        (prefix / "bin/php-link").symlink_to("php")
        archive = self.root / "php.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(prefix, arcname="php/8.4")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        host.verify_host_archive(archive, digest, prefix, "php", "8.4")
        with self.assertRaises(ValueError):
            host.verify_host_archive(archive, "f" * 64, prefix, "php", "8.4")
        link = prefix / "bin/php-link"
        link.unlink()
        link.symlink_to("../VERSION")
        with self.assertRaises(ValueError):
            host.verify_host_archive(archive, digest, prefix, "php", "8.4")
        link.unlink()
        link.symlink_to("php")
        binary.write_bytes(b"different host bytes")
        with self.assertRaises(ValueError):
            host.verify_host_archive(archive, digest, prefix, "php", "8.4")
        binary.write_bytes(b"exact host bytes")
        binary.chmod(0o644)
        with self.assertRaises(ValueError):
            host.verify_host_archive(archive, digest, prefix, "php", "8.4")
        binary.chmod(0o755)
        (prefix / "old-module.so").write_bytes(b"unowned old module")
        with self.assertRaises(ValueError):
            host.verify_host_archive(archive, digest, prefix, "php", "8.4")


    def test_native_payload_checks_real_module_file_type_and_architecture(self):
        helper = self.helper()
        payload = self.root / "payload"
        payload.mkdir()
        csource = self.root / "module.c"
        csource.write_text("int module_fixture(void) { return 17; }\n")
        module = payload / "fixture.so"
        subprocess.run(["/usr/bin/clang", "-dynamiclib", str(csource), "-o", str(module)], check=True, capture_output=True)
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        helper.verify_native_payload(payload, arch)
        with self.assertRaises(ValueError):
            helper.verify_native_payload(payload, "x86_64" if arch == "arm64" else "arm64")
        module.write_bytes(b"not an actual dynamic module")
        with self.assertRaises(ValueError):
            helper.verify_native_payload(payload, arch)
