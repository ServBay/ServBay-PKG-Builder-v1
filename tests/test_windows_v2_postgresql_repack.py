"""Behavior tests for the diagnostic Windows PostgreSQL v2 core mapping."""

import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-windows/repack_postgresql_v2.py"
SEVEN_ZIP = os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7zz")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pe(machine=0x8664, size=512):
    data = bytearray(size)
    data[:2] = b"MZ"
    offset = 0x80
    struct.pack_into("<I", data, 0x3C, offset)
    data[offset:offset + 4] = b"PE\0\0"
    struct.pack_into("<H", data, offset + 4, machine)
    data[offset + 0x18:offset + 0x1A] = struct.pack("<H", 0x20B)
    return bytes(data)


def zip_bytes(entries):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in entries:
                if isinstance(name, str):
                    info = zipfile.ZipInfo()
                    info.filename = name
                    info.orig_filename = name
                    info.compress_type = zipfile.ZIP_STORED
                    name = info
                archive.writestr(name, content)
    return stream.getvalue()


class WindowsPostgresqlRepackTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SEVEN_ZIP, "7z or 7zz is required")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.counter = 0
        header = (
            '#define PG_VERSION "18.6"\n'
            '#define PG_VERSION_NUM 180006\n'
            '#define PG_VERSION_STR "PostgreSQL 18.6 on x86_64-windows"\n'
        ).encode()
        self.payload = {
            "pgsql/bin/postgres.exe": pe(),
            "pgsql/bin/pg_config.exe": pe(),
            "pgsql/bin/initdb.exe": pe(),
            "pgsql/bin/pg_ctl.exe": pe(),
            "pgsql/bin/psql.exe": pe(),
            "pgsql/bin/clusterdb.exe": pe(),
            "pgsql/include/server/pg_config.h": header,
            "pgsql/include/server/postgres.h": b"#define POSTGRES_HEADER 1\n",
            "pgsql/include/pg_config.h": b"#define PG_VERSION \"18.6\"\n",
            "pgsql/lib/postgres.lib": b"import-library",
            "pgsql/share/extension/amcheck.control": b"default_version = '1.4'\n",
            "pgsql/share/extension/amcheck--1.4.sql": b"CREATE FUNCTION amcheck();\n",
            "pgsql/share/extension/amcheck--1.3--1.4.sql": b"-- upgrade\n",
            "pgsql/share/postgresql.conf.sample": b"# postgres default\n",
            "pgsql/share/pg_hba.conf.sample": b"# hba default\n",
            "pgsql/share/pg_ident.conf.sample": b"# ident default\n",
            "pgsql/share/pg_service.conf.sample": b"# service default\n",
            "pgsql/share/psqlrc.sample": b"# psql default\n",
            "pgsql/share/timezone/zoneinfo/UTC": b"TZif2",
            "pgsql/pg_env.bat": b"set PATH=%~dp0bin\r\n",
            "pgsql/pgAdmin 4/python/python.exe": pe(),
            "pgsql/pgAdmin 4/python/Lib/site-packages/distlib/t32.exe": pe(0x14C),
            "pgsql/pgAdmin 4/python/Lib/site-packages/distlib/t64-arm.exe": pe(0xAA64),
            "pgsql/doc/README": b"PostgreSQL 18.6 Windows binaries\n",
        }
        self.identity = {
            "runtime_version": "v2", "os": "windows", "kind": "package",
            "name": "postgresql", "version": "18.6", "arch": "x64", "build_revision": 0,
        }
        self.producer = {
            "repository": "ServBay/ServBay-PKG-Builder-v1",
            "commit": "a" * 40, "run_id": 123, "run_attempt": 1,
        }

    def module(self):
        self.assertTrue(SCRIPT.is_file(), "PostgreSQL v2 repacker is missing")
        spec = importlib.util.spec_from_file_location("postgresql_v2_repack", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def arguments(self, payload=None, *, identity=None, source=None, label=None):
        self.counter += 1
        payload = copy.deepcopy(self.payload if payload is None else payload)
        entries = list(payload.items()) if isinstance(payload, dict) else list(payload)
        entries.insert(0, ("pgsql/", b""))
        raw = zip_bytes(entries)
        name = label or ("postgresql-" + str(self.counter) + "-binaries.zip")
        path = self.base / name
        path.write_bytes(raw)
        identity = copy.deepcopy(self.identity if identity is None else identity)
        source_value = {
            "path": path, "version": "18.6", "bytes": len(raw), "sha256": digest(raw),
            "url": "https://get.enterprisedb.com/postgresql/" + path.name,
        }
        if source:
            source_value.update(source)
        return {
            "source": source_value, "identity": identity,
            "output_dir": self.base / ("output-" + str(self.counter)), "seven_zip": SEVEN_ZIP,
        }

    def reject(self, arguments, pattern=None):
        output = Path(arguments["output_dir"])
        with self.assertRaises(ValueError) as caught:
            self.module().prepare_postgresql_core(**arguments)
        if pattern:
            self.assertRegex(str(caught.exception), pattern)
        self.assertFalse((output / "postgresql-repack-manifest.json").exists())
        self.assertFalse(list(output.glob("*.7z")) if output.is_dir() else [])

    def test_maps_every_regular_file_once_and_writes_a_verified_diagnostic_archive(self):
        arguments = self.arguments()
        result = self.module().prepare_postgresql_core(**arguments)
        self.assertEqual(result["schema"], "servbay.windows-postgresql-repack.v1")
        self.assertFalse(result["publishable_v2"])
        self.assertEqual(result["native_validation"], "pending")
        self.assertNotIn("meta.json", {path.name for path in arguments["output_dir"].iterdir()})
        audit = json.loads((arguments["output_dir"] / "postgresql-repack-manifest.json").read_text())
        self.assertEqual(result, audit)
        self.assertEqual(audit["layout"]["package_root"], "postgresql/18")
        self.assertEqual(audit["identity"]["arch"], "x86_64")
        mapping = {row["source_path"]: row for row in audit["files"]}
        self.assertEqual(set(mapping), set(self.payload))
        for source_path, content in self.payload.items():
            target = "postgresql/18/" + source_path.removeprefix("pgsql/")
            self.assertEqual(mapping[source_path]["target_path"], target)
            self.assertEqual(mapping[source_path]["bytes"], len(content))
            self.assertEqual(mapping[source_path]["sha256"], digest(content))
        archive = arguments["output_dir"] / (
            "postgresql-18.6-windows-x86_64.7z")
        self.assertTrue(archive.is_file())
        self.assertEqual(
            set(self.module().windows_archive.archive_hashes(archive, SEVEN_ZIP)),
            {row["target_path"] for row in audit["files"]})
        self.assertEqual(audit["archive"]["filename"], archive.name)
        self.assertEqual(audit["archive"]["sha256"], digest(archive.read_bytes()))
        self.assertNotIn(str(self.base), json.dumps(audit))

    def test_header_version_and_required_binaries_prove_the_requested_x64_host(self):
        arguments = self.arguments()
        result = self.module().prepare_postgresql_core(**arguments)
        self.assertEqual(result["postgresql_header"],
                         {"version": "18.6", "version_num": 180006, "major": 18})
        self.assertEqual({row["path"] for row in result["required_binaries"]},
                         {"postgresql/18/" + path.removeprefix("pgsql/") for path in self.payload
                          if path.startswith("pgsql/bin/") and path.endswith(".exe")})
        self.assertEqual({row["machine"] for row in result["required_binaries"]}, {"x86_64"})
        self.assertEqual(result["other_pe_machine_counts"], {"arm64": 1, "x86": 1, "x86_64": 1})

        bad_header = dict(self.payload)
        bad_header["pgsql/include/server/pg_config.h"] = (
            '#define PG_VERSION "18.6"\n#define PG_VERSION_NUM 180005\n').encode()
        self.reject(self.arguments(bad_header, label="bad-header.zip"), pattern="PG_VERSION_NUM")

        commented_decoy = dict(self.payload)
        commented_decoy["pgsql/include/server/pg_config.h"] = (
            '/* #define PG_VERSION "17.0" */\n'
            '// #define PG_VERSION_NUM 170000\n'
            '#define PG_VERSION "18.6"\n#define PG_VERSION_NUM 180006\n').encode()
        commented_result = self.module().prepare_postgresql_core(
            **self.arguments(commented_decoy, label="commented-header.zip"))
        self.assertEqual(commented_result["postgresql_header"]["version"], "18.6")

        ambiguous = dict(self.payload)
        ambiguous["pgsql/include/server/pg_config.h"] = (
            '#if 0\n#define PG_VERSION "18.6"\n#define PG_VERSION_NUM 180006\n#endif\n'
            '#define PG_VERSION "17.0"\n#define PG_VERSION_NUM 170000\n').encode()
        self.reject(self.arguments(ambiguous, label="ambiguous-header.zip"),
                    pattern="exactly one")

        for relative in ("postgres.exe", "pg_config.exe", "initdb.exe", "pg_ctl.exe", "psql.exe"):
            with self.subTest(relative=relative):
                bad = dict(self.payload)
                bad["pgsql/bin/" + relative] = pe(0x14C)
                self.reject(self.arguments(bad, label=relative + ".zip"), pattern="x86_64")

    def test_source_bytes_sha_version_and_fields_are_exact(self):
        arguments = self.arguments()
        for field, value in (("sha256", "0" * 64), ("bytes", arguments["source"]["bytes"] + 1),
                             ("version", "18.5"), ("sha256", None), ("bytes", True)):
            with self.subTest(field=field):
                bad = copy.deepcopy(arguments)
                bad["source"][field] = value
                bad["output_dir"] = self.base / ("bad-source-" + field + "-" + str(self.counter))
                self.reject(bad)
        for field in ("sha256", "bytes", "version"):
            bad = copy.deepcopy(arguments)
            del bad["source"][field]
            self.reject(bad)
        bad = copy.deepcopy(arguments)
        bad["source"]["token"] = "secret"
        self.reject(bad)
        bad = copy.deepcopy(arguments)
        bad["source"]["url"] = "https://user:secret@example.org/input.zip"
        self.reject(bad)

    def test_identity_is_exactly_windows_v2_postgresql_x64(self):
        for key, value in (("runtime_version", "v1"), ("os", "macos"),
                           ("kind", "postgresql-extension"), ("name", "php"),
                           ("arch", "arm64"), ("version", "18.5"),
                           ("version", "19.beta2"), ("build_revision", -1)):
            with self.subTest(key=key, value=value):
                identity = copy.deepcopy(self.identity)
                identity[key] = value
                self.reject(self.arguments(identity=identity, label="identity-" + key + ".zip"))
        identity = copy.deepcopy(self.identity)
        identity["extra"] = "unsafe"
        self.reject(self.arguments(identity=identity, label="identity-extra.zip"))

    def test_required_entries_and_single_pgsql_root_are_not_guessed(self):
        for relative in ("bin/postgres.exe", "bin/pg_config.exe", "bin/initdb.exe",
                         "bin/pg_ctl.exe", "bin/psql.exe", "include/server/pg_config.h"):
            with self.subTest(relative=relative):
                payload = dict(self.payload)
                del payload["pgsql/" + relative]
                self.reject(self.arguments(payload, label="missing-" + relative.replace("/", "-") + ".zip"))
        for payload in (
                {"wrapper/" + key: value for key, value in self.payload.items()},
                dict(self.payload, **{"readme.txt": b"outside"}),
        ):
            self.reject(self.arguments(payload, label="wrong-root.zip"))

    def test_windows_path_rules_and_case_or_parent_collisions_are_rejected(self):
        bad_names = ("pgsql/../evil", "pgsql/bin/evil.exe:stream", "pgsql/bin/trailing. ",
                     "pgsql/bin/back\\slash.exe", "pgsql/bin/NUL.txt")
        for index, name in enumerate(bad_names):
            with self.subTest(name=name):
                payload = dict(self.payload)
                payload[name] = b"unsafe"
                self.reject(self.arguments(payload, label="unsafe-" + str(index) + ".zip"))
        for name in ("pgsql/BIN/postgres.exe", "pgsql/bin"):
            with self.subTest(name=name):
                payload = dict(self.payload)
                payload[name] = b"collision"
                self.reject(self.arguments(payload, label="collision-" + str(len(payload)) + ".zip"))

    def test_links_special_files_and_local_or_central_encryption_are_rejected(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600):
            with self.subTest(mode=mode):
                info = zipfile.ZipInfo("pgsql/bin/postgres.exe")
                info.create_system = 3
                info.external_attr = mode << 16
                self.reject(self.arguments([(info, pe())], label="special-" + str(mode) + ".zip"))

        for label, signature, flag_offset in (
                ("central-only", b"PK\x01\x02", 8), ("local-only", b"PK\x03\x04", 6)):
            with self.subTest(encryption=label):
                arguments = self.arguments(label="encryption-" + label + ".zip")
                raw = bytearray(arguments["source"]["path"].read_bytes())
                offset = 0
                changed = 0
                while (offset := raw.find(signature, offset)) >= 0:
                    raw[offset + flag_offset] |= 1
                    changed += 1
                    offset += 4
                self.assertGreater(changed, 0)
                arguments["source"]["path"].write_bytes(raw)
                arguments["source"].update(bytes=len(raw), sha256=digest(raw))
                self.reject(arguments, pattern="ncrypted")

    def test_crc_damage_is_rejected_before_success_output(self):
        arguments = self.arguments()
        raw = bytearray(arguments["source"]["path"].read_bytes())
        with zipfile.ZipFile(arguments["source"]["path"]) as source:
            info = next(item for item in source.infolist() if not item.is_dir())
            offset = info.header_offset
        name_length = struct.unpack_from("<H", raw, offset + 26)[0]
        extra_length = struct.unpack_from("<H", raw, offset + 28)[0]
        payload_offset = offset + 30 + name_length + extra_length
        raw[payload_offset] ^= 1
        arguments["source"]["path"].write_bytes(raw)
        arguments["source"].update(bytes=len(raw), sha256=digest(raw))
        self.reject(arguments, pattern="CRC")

    def test_configuration_samples_and_contrib_are_retained_from_the_actual_tree(self):
        result = self.module().prepare_postgresql_core(**self.arguments())
        self.assertEqual(
            {row["archive_path"] for row in result["configuration_defaults"]},
            {"postgresql/18/" + path.removeprefix("pgsql/")
             for path in self.payload if path.startswith("pgsql/share/") and path.endswith(".sample")})
        for row in result["configuration_defaults"]:
            self.assertEqual(row["etc_relative_path"],
                             "postgresql/18/" + row["archive_path"].rsplit("/", 1)[1])
            self.assertEqual(row["kind"], "upstream-default")
            self.assertFalse(row["automatically_enabled"])
        self.assertEqual(result["retained_contrib"],
                         ["postgresql/18/share/extension/amcheck.control"])

    def test_configuration_sample_target_collisions_are_rejected(self):
        payload = dict(self.payload)
        payload["pgsql/share/other/postgresql.conf.sample"] = b"different nested sample\n"
        self.reject(self.arguments(payload, label="sample-collision.zip"), pattern="collision")

    def test_existing_output_and_source_are_never_changed(self):
        arguments = self.arguments()
        arguments["output_dir"].mkdir()
        sentinel = arguments["output_dir"] / "keep"
        sentinel.write_bytes(b"user")
        self.reject(arguments)
        self.assertEqual(sentinel.read_bytes(), b"user")
        before = arguments["source"]["path"].read_bytes()
        self.module().prepare_postgresql_core(**dict(arguments, output_dir=self.base / "new-out"))
        self.assertEqual(arguments["source"]["path"].read_bytes(), before)

    def test_corrupt_published_copy_never_creates_success_output(self):
        arguments = self.arguments()
        original = shutil.copyfile
        def corrupt(source, target, *args, **kwargs):
            result = original(source, target, *args, **kwargs)
            if str(target).endswith(".7z"):
                Path(target).write_bytes(b"corrupt")
            return result
        with mock.patch.object(self.module().shutil, "copyfile", side_effect=corrupt):
            self.reject(arguments, pattern="differs")

    def test_cli_runs_the_same_public_producer(self):
        arguments = self.arguments()
        source_file = self.base / "source.json"
        identity_file = self.base / "identity.json"
        source_file.write_text(json.dumps(dict(arguments["source"], path=str(arguments["source"]["path"]))))
        identity_file.write_text(json.dumps(arguments["identity"]))
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--source", str(source_file),
             "--identity", str(identity_file), "--output-dir", str(arguments["output_dir"]),
             "--seven-zip", SEVEN_ZIP],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["manifest"], "postgresql-repack-manifest.json")
        self.assertEqual(result["files"], len(self.payload))

    def test_input_change_during_validation_cannot_leave_success_receipt(self):
        arguments = self.arguments()
        original = zipfile.ZipFile
        changed = False
        def mutate_then_open(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                arguments["source"]["path"].write_bytes(b"changed")
            return original(*args, **kwargs)
        with mock.patch.object(zipfile, "ZipFile", side_effect=mutate_then_open):
            self.reject(arguments)

    def finalization_fixture(self):
        arguments = self.arguments(label="postgresql-18.6-windows-x64-binaries.zip")
        diagnostic = self.module().prepare_postgresql_core(**arguments)
        package = arguments["output_dir"] / diagnostic["archive"]["filename"]
        native = self.base / "native-core-result.json"
        native.write_text(json.dumps({
            "schema": "servbay.windows-postgresql-core-native.v1",
            "archive": diagnostic["archive"],
            "os_architecture": "X64",
            "process_architecture": "X64",
            "processor_name": "fixture",
            "processor_architecture_code": 9,
            "process_environment_architecture": "AMD64",
            "pg_config": {
                "version": "PostgreSQL 18.6",
                "bindir": "C:/tmp/POSTGR~1/18/bin",
                "sharedir": "C:/tmp/postgresql/18/share",
                "pkglibdir": "C:/tmp/postgresql/18/lib",
                "includedir_server": "C:/tmp/postgresql/18/include/server",
            },
            "executables": {name: f"{name} (PostgreSQL) 18.6"
                            for name in ("postgres", "initdb", "pg_ctl", "psql")},
            "database": {
                "initialized_pg_version": "18", "server_version": "18.6",
                "server_encoding": "UTF8", "data_directory": "C:/tmp/postgresql/18/data",
                "query_exit": 0, "startup_exit": 0, "stop_exit": 0,
                "remaining_processes": 0, "listeners": 0,
            },
        }), encoding="utf-8")
        producer = {"commit": "b" * 40, "run_id": "4567", "run_attempt": 1}
        compatibility = {"distribution": "windows-x64", "toolchain": "msvc-fixture", "crt": "msvc-static"}
        return (package, arguments["source"]["path"], native, producer, compatibility,
                self.base / "formal-postgresql")

    def test_native_core_evidence_finalizes_schema2_core_without_default_modules(self):
        package, source, native, producer, compatibility, output = self.finalization_fixture()
        result = self.module().finalize_postgresql_core(
            package, self.identity, source,
            "https://get.enterprisedb.com/postgresql/postgresql-18.6-windows-x64-binaries.zip",
            native, producer, "10.0", compatibility, output, seven_zip=SEVEN_ZIP,
        )
        meta = json.loads((output / "meta.json").read_text())
        receipt = json.loads((output / "postgresql-finalization.json").read_text())
        self.assertEqual(result["meta"], output / "meta.json")
        self.assertEqual(meta["sha256"], digest((output / meta["filename"]).read_bytes()))
        self.assertEqual(meta["minimum_os"], "10.0")
        self.assertEqual(meta["windows_compatibility"], compatibility)
        self.assertNotIn("base", meta)
        self.assertEqual(meta["package_manifest"]["default_modules"], [])
        self.assertEqual(meta["package_manifest"]["host"]["type"], "postgresql")
        self.assertEqual(receipt["native_validation"], "passed")
        self.assertNotIn("C:/tmp", json.dumps(receipt))

    def test_finalization_publishes_real_output_under_simulated_cross_volume_boundary(self):
        package, source, native, producer, compatibility, unused = self.finalization_fixture()
        module = self.module()
        system_temp = self.base / "simulated-system-volume"
        destination = self.base / "simulated-output-volume"
        system_temp.mkdir()
        destination.mkdir()
        output = destination / "formal-postgresql"
        original = os.replace
        publishes = []
        def simulated_replace(source_path, target_path):
            source_path, target_path = Path(source_path), Path(target_path)
            if source_path.is_dir() and target_path == output:
                if not source_path.resolve().is_relative_to(destination.resolve()):
                    raise OSError(errno.EXDEV, "Simulated cross-volume directory move")
                publishes.append(target_path)
            return original(source_path, target_path)
        failed = False
        with mock.patch.object(tempfile, "tempdir", str(system_temp)), \
                mock.patch.object(os, "replace", side_effect=simulated_replace):
            try:
                result = module.finalize_postgresql_core(
                    package, self.identity, source,
                    "https://get.enterprisedb.com/postgresql/postgresql-18.6-windows-x64-binaries.zip",
                    native, producer, "10.0", compatibility, output, seven_zip=SEVEN_ZIP)
            except OSError:
                failed = True
        self.assertFalse(failed, "Simulated cross-volume boundary must use destination-side staging")
        self.assertEqual(publishes, [output])
        meta = json.loads((output / "meta.json").read_text())
        receipt = json.loads((output / "postgresql-finalization.json").read_text())
        self.assertEqual(result["meta"], output / "meta.json")
        self.assertEqual(meta["version"], self.identity["version"])
        self.assertEqual(meta["arch"], "x86_64")
        self.assertEqual(meta["producer"], producer)
        self.assertEqual((output / meta["filename"]).read_bytes(), package.read_bytes())
        self.assertEqual(meta["sha256"], digest(package.read_bytes()))
        self.assertEqual(receipt["archive"]["sha256"], meta["sha256"])
        self.assertEqual(receipt["meta"]["sha256"], digest((output / "meta.json").read_bytes()))
        self.assertEqual(receipt["native_validation"], "passed")

    def test_native_core_terminal_failure_and_wrong_archive_prevent_output(self):
        package, source, native, producer, compatibility, output = self.finalization_fixture()
        original = json.loads(native.read_text())
        for field, value in (("query_exit", 1), ("startup_exit", 1), ("stop_exit", 1),
                             ("remaining_processes", 1), ("listeners", 1)):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["database"][field] = value
                native.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.module().finalize_postgresql_core(
                        package, self.identity, source,
                        "https://get.enterprisedb.com/postgresql/postgresql-18.6-windows-x64-binaries.zip",
                        native, producer, "10.0", compatibility, output, seven_zip=SEVEN_ZIP,
                    )
                self.assertFalse(output.exists())
        changed = copy.deepcopy(original)
        changed["archive"]["sha256"] = "0" * 64
        native.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.module().finalize_postgresql_core(
                package, self.identity, source,
                "https://get.enterprisedb.com/postgresql/postgresql-18.6-windows-x64-binaries.zip",
                native, producer, "10.0", compatibility, output, seven_zip=SEVEN_ZIP,
            )
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
