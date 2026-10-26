"""Behavior tests for the validated Windows pgvector build handoff."""

import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-windows/repack_pgvector_v2.py"
BUILD_SCRIPT = ROOT / "runtime-windows/build_pgvector_v2.ps1"
WORKFLOW = ROOT / ".github/workflows/build-windows-pgvector.yml"
SEVEN_ZIP = os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7zz")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pe64():
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, 0x8664)
    return bytes(data)


class WindowsV2PgvectorRepackTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SEVEN_ZIP)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.build = self.root / "build"
        self.install = self.build / "install"
        self.output = self.root / "output"
        self.files = {
            "lib/vector.dll": pe64(),
            "share/extension/vector.control":
                b"default_version = '0.8.7'\nmodule_pathname = '$libdir/vector'\n",
            "share/extension/vector--0.8.6--0.8.7.sql": b"-- upgrade\n",
            "share/extension/vector--0.8.7.sql": b"CREATE FUNCTION vector_in(cstring);\n",
            "include/server/extension/vector/vector.h": b"#define VECTOR_H 1\n",
            "include/server/extension/vector/halfvec.h": b"#define HALFVEC_H 1\n",
            "include/server/extension/vector/sparsevec.h": b"#define SPARSEVEC_H 1\n",
        }
        for name, data in self.files.items():
            path = self.install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.identity = {
            "runtime_version": "v2", "os": "windows", "kind": "postgresql-extension",
            "name": "vector", "version": "0.8.7", "arch": "x64", "build_revision": 0,
            "host": {"type": "postgresql", "series": "18", "abi": {"major": 18}},
        }
        self.core = {
            "sha256": "f" * 64, "version": "18.6", "series": "18",
        }
        self.audit = {
            "schema": "servbay.windows-pgvector-build.v1",
            "publishable_v2": False,
            "native_validation": "pending",
            "payload_root": "install",
            "source": {"filename": "pgvector-v0.8.7.tar.gz", "version": "0.8.7",
                       "bytes": 133025, "sha256": "a" * 64},
            "host": {"core_package_sha256": "f" * 64,
                     "postgresql_version": "PostgreSQL 18.6",
                     "core_root_layout": "postgresql/<major>"},
            "toolchain": {"requested": "MSVC amd64 via VsDevCmd",
                          "actual_compiler": "19.44.35229", "crt": "pending"},
            "files": [{"path": name, "bytes": len(data), "sha256": digest(data)}
                      for name, data in sorted(self.files.items())],
            "compatibility": "pending",
            "minimum_os": "pending",
        }
        self.write_audit()

    def write_audit(self, audit=None):
        (self.build / "build-audit.json").write_text(
            json.dumps(self.audit if audit is None else audit), encoding="utf-8")

    def sync_audit_files(self):
        self.audit["files"] = [
            {"path": path.relative_to(self.install).as_posix(), "bytes": path.stat().st_size,
             "sha256": digest(path.read_bytes())}
            for path in sorted(self.install.rglob("*")) if path.is_file()]
        self.write_audit()

    def module(self):
        self.assertTrue(SCRIPT.is_file())
        spec = importlib.util.spec_from_file_location("tested_pgvector_repack", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def arguments(self):
        return {"build_dir": self.build, "identity": copy.deepcopy(self.identity),
                "core": copy.deepcopy(self.core), "output_dir": self.output,
                "seven_zip": SEVEN_ZIP}

    def reject(self, arguments, pattern=None):
        with self.assertRaises(ValueError) as caught:
            self.module().repack_pgvector(**arguments)
        if pattern:
            self.assertRegex(str(caught.exception), pattern)
        self.assertFalse(self.output.exists())

    def test_valid_handoff_creates_exact_verified_module_archive(self):
        result = self.module().repack_pgvector(**self.arguments())
        self.assertEqual(result["schema"], "servbay.windows-pgvector-repack.v1")
        self.assertFalse(result["publishable_v2"])
        self.assertEqual(result["identity"]["arch"], "x86_64")
        archive = self.output / "ext-vector-0.8.7-pgsql18-windows-x86_64.7z"
        self.assertEqual(result["archive"]["sha256"], digest(archive.read_bytes()))
        self.assertEqual(set(self.module().windows_archive.archive_hashes(archive, SEVEN_ZIP)),
                         set(self.files))
        manifest = json.loads((self.output / "pgvector-repack-manifest.json").read_text())
        self.assertEqual(manifest, result)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(set(path.name for path in self.output.iterdir()),
                         {archive.name, "pgvector-repack-manifest.json"})

    def finalization_arguments(self):
        source_bytes = b"synthetic pgvector source archive"
        source = self.root / "pgvector-v0.8.7.tar.gz"
        source.write_bytes(source_bytes)
        self.audit["source"].update(bytes=len(source_bytes), sha256=digest(source_bytes))
        self.write_audit()
        native = self.root / "native-result.json"
        native.write_text(json.dumps({
            "schema": "servbay.windows-pgvector-native.v1",
            "source_sha256": digest(source_bytes),
            "core_archive_sha256": "f" * 64,
            "built_dll_sha256": digest(self.files["lib/vector.dll"]),
            "install_file_count": len(self.files),
            "sql_file_count": 2,
            "compiler": "19.44.35229",
            "processor_architecture_code": 9,
            "process_environment_architecture": "AMD64",
            "extension_version": "0.8.7",
            "vector_distance_l2_squared": 5.196152422706632,
            "create_extension_exit": 0,
            "drop_and_recreate_exit": 0,
            "stop_exit": 0,
            "remaining_processes": 0,
            "listeners": 0,
        }), encoding="utf-8")
        compatibility = {"distribution": "windows-x64", "toolchain": "msvc-19.44.35229", "crt": "static"}
        producer = {"commit": "a" * 40, "run_id": "3456", "run_attempt": 2}
        return (
            native, source,
            "https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.tar.gz",
            producer, "10.0", compatibility, self.root / "final",
        )

    def test_native_evidence_finalizes_schema2_module_and_metadata(self):
        native, source, url, producer, minimum, compatibility, output = self.finalization_arguments()
        result = self.module().finalize_pgvector(
            self.build, self.identity, self.core, native, source, url, producer,
            minimum, compatibility, output, seven_zip=SEVEN_ZIP,
        )
        archive = output / "ext-vector-0.8.7-pgsql18-windows-x86_64.7z"
        meta = json.loads((output / "meta.json").read_text())
        manifest = json.loads((output / "module-manifest.json").read_text())
        receipt = json.loads((output / "pgvector-finalization.json").read_text())
        self.assertEqual(result["archive"], archive)
        self.assertEqual(meta["sha256"], digest(archive.read_bytes()))
        self.assertEqual(meta["minimum_os"], "10.0")
        self.assertEqual(meta["windows_compatibility"], compatibility)
        self.assertEqual(meta["arch"], "x86_64")
        self.assertNotIn("base", meta)
        self.assertEqual(meta["provider"] if "provider" in meta else meta["producer"], producer)
        self.assertEqual(manifest["provides"], ["vector"])
        self.assertEqual(manifest["host"]["package_sha256"], "f" * 64)
        self.assertEqual(receipt["native_validation"], "passed")
        self.assertEqual(receipt["native_result"]["sha256"], digest(native.read_bytes()))
        self.assertEqual(set(self.module().windows_archive.archive_hashes(archive, SEVEN_ZIP)), set(self.files))
        self.assertEqual({path.name for path in output.iterdir()},
                         {archive.name, "meta.json", "module-manifest.json", "pgvector-finalization.json",
                          "native-result.json", "build-audit.json"})

    def test_finalization_publishes_real_output_under_simulated_cross_volume_boundary(self):
        native, source, url, producer, minimum, compatibility, unused = self.finalization_arguments()
        module = self.module()
        system_temp = self.root / "simulated-system-volume"
        destination = self.root / "simulated-output-volume"
        system_temp.mkdir()
        destination.mkdir()
        output = destination / "formal-pgvector"
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
                result = module.finalize_pgvector(
                    self.build, self.identity, self.core, native, source, url, producer,
                    minimum, compatibility, output, seven_zip=SEVEN_ZIP)
            except OSError:
                failed = True
        self.assertFalse(failed, "Simulated cross-volume boundary must use destination-side staging")
        self.assertEqual(publishes, [output])
        meta = json.loads((output / "meta.json").read_text())
        receipt = json.loads((output / "pgvector-finalization.json").read_text())
        archive = output / meta["filename"]
        self.assertEqual(result["archive"], archive)
        self.assertEqual(meta["version"], self.identity["version"])
        self.assertEqual(meta["arch"], "x86_64")
        self.assertEqual(meta["producer"], producer)
        self.assertEqual(meta["sha256"], digest(archive.read_bytes()))
        self.assertEqual(receipt["archive"]["sha256"], meta["sha256"])
        self.assertEqual(receipt["meta"]["sha256"], digest((output / "meta.json").read_bytes()))
        self.assertEqual(receipt["native_validation"], "passed")
        self.assertEqual(set(module.windows_archive.archive_hashes(archive, SEVEN_ZIP)), set(self.files))

    def test_native_evidence_tamper_and_source_mismatch_prevent_final_output(self):
        native, source, url, producer, minimum, compatibility, output = self.finalization_arguments()
        original = json.loads(native.read_text())
        for field, value in (("source_sha256", "0" * 64), ("built_dll_sha256", "0" * 64),
                             ("sql_file_count", 1), ("compiler", "other"),
                             ("remaining_processes", 1), ("listeners", 1)):
            with self.subTest(field=field):
                changed = dict(original, **{field: value})
                native.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.module().finalize_pgvector(
                        self.build, self.identity, self.core, native, source, url, producer,
                        minimum, compatibility, output, seven_zip=SEVEN_ZIP,
                    )
                self.assertFalse(output.exists())
        native.write_text(json.dumps(original), encoding="utf-8")
        nested = self.install / "nested-output"
        with self.assertRaisesRegex(ValueError, "separate from the build"):
            self.module().finalize_pgvector(
                self.build, self.identity, self.core, native, source, url, producer,
                minimum, compatibility, nested, seven_zip=SEVEN_ZIP,
            )
        self.assertFalse(nested.exists())
        source.write_bytes(b"changed source")
        with self.assertRaises(ValueError):
            self.module().finalize_pgvector(
                self.build, self.identity, self.core, native, source, url, producer,
                minimum, compatibility, output, seven_zip=SEVEN_ZIP,
            )
        self.assertFalse(output.exists())

    def test_audit_file_tamper_and_extra_install_file_fail(self):
        bad = copy.deepcopy(self.audit)
        bad["files"][0]["sha256"] = "0" * 64
        self.write_audit(bad)
        self.reject(self.arguments(), "bytes/SHA")
        self.write_audit()
        (self.install / "lib/extra.dll").write_bytes(b"extra")
        self.reject(self.arguments(), "Unexpected|file sets")

    def test_missing_initial_sql_and_control_identity_fail(self):
        (self.install / "share/extension/vector--0.8.7.sql").unlink()
        self.reject(self.arguments(), "required files")
        self.setUp_files()
        (self.install / "share/extension/vector.control").write_text(
            "default_version = '0.8.6'\nmodule_pathname = '$libdir/vector'\n")
        self.sync_audit_files()
        self.reject(self.arguments(), "control")

    def setUp_files(self):
        for name, data in self.files.items():
            path = self.install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.write_audit()

    def test_publish_gates_and_native_validation_must_stay_pending(self):
        for field, value in (("schema", "other"), ("publishable_v2", True),
                             ("native_validation", "passed"), ("payload_root", "."),
                             ("compatibility", {"distribution": "x", "toolchain": "x", "crt": "x"}),
                             ("minimum_os", "10.0")):
            with self.subTest(field=field):
                bad = copy.deepcopy(self.audit)
                bad[field] = value
                self.write_audit(bad)
                self.reject(self.arguments())

    def test_identity_core_and_source_versions_are_exactly_related(self):
        for name, field, value in (
                ("identity", "name", "other"), ("identity", "version", "0.8.6"),
                ("identity", "arch", "x86"), ("identity", "runtime_version", "v1")):
            with self.subTest(field=field):
                arguments = self.arguments()
                arguments[name][field] = value
                self.reject(arguments)
        for field, value in (("sha256", "0" * 64), ("version", "17.9"), ("series", "17")):
            with self.subTest(core=field):
                arguments = self.arguments()
                arguments["core"][field] = value
                self.reject(arguments)
        bad = copy.deepcopy(self.audit)
        bad["source"]["version"] = "0.8.6"
        self.write_audit(bad)
        self.reject(self.arguments(), "source")

    def test_duplicate_json_and_existing_output_are_rejected(self):
        encoded = json.dumps(self.audit)
        field = '"toolchain": ' + json.dumps(self.audit["toolchain"])
        self.assertEqual(encoded.count(field), 1)
        (self.build / "build-audit.json").write_text(
            encoded.replace(field, field + ',' + field), encoding="utf-8")
        self.reject(self.arguments())
        self.write_audit()
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_text("user")
        with self.assertRaises(ValueError):
            self.module().repack_pgvector(**self.arguments())
        self.assertEqual(sentinel.read_text(), "user")

    def test_powershell_utf8_bom_audit_is_accepted(self):
        audit_path = self.build / "build-audit.json"
        audit_path.write_text("\ufeff" + json.dumps(self.audit), encoding="utf-8")
        result = self.module().repack_pgvector(**self.arguments())
        self.assertEqual(result["source"]["sha256"], self.audit["source"]["sha256"])

    def test_archive_name_and_file_count_are_explicit(self):
        result = self.module().repack_pgvector(**self.arguments())
        self.assertEqual(result["archive"]["filename"],
                         "ext-vector-0.8.7-pgsql18-windows-x86_64.7z")
        self.assertEqual(len(result["files"]), len(self.files))

    def test_corrupt_published_copy_never_creates_success_output(self):
        original = shutil.copyfile
        def corrupt(source, target, *args, **kwargs):
            value = original(source, target, *args, **kwargs)
            if Path(target).parent.name.startswith(".output.pending-") and str(target).endswith(".7z"):
                Path(target).write_bytes(b"corrupt")
            return value
        with mock.patch.object(self.module().shutil, "copyfile", side_effect=corrupt):
            self.reject(self.arguments(), "differs")

    def test_workflow_consumes_producer_and_packages_only_artifacts(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML unavailable")
        self.assertTrue(BUILD_SCRIPT.is_file())
        workflow = yaml.safe_load(WORKFLOW.read_text())
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        serialized = json.dumps(workflow)
        self.assertIn("build_pgvector_v2.ps1", serialized)
        self.assertIn("repack_pgvector_v2.py", serialized)
        self.assertIn("actions/upload-artifact@v4", serialized)
        self.assertNotIn("action-gh-release", serialized)
        self.assertNotIn("upload_packages", serialized)


if __name__ == "__main__":
    unittest.main()
