import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
UNSET = object()


class WindowsMetadataTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "windows_package_meta", ROOT / "runtime/ci/package_meta.py")
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Synthetic bytes test metadata only, not 7z validity or native loading.
        self.archive = self.root / "nginx-1.31.7-windows-x86_64.7z"
        self.archive.write_bytes(b"synthetic package bytes")
        self.source = self.root / "upstream.zip"
        self.source.write_bytes(b"distinct actual upstream bytes")
        self.record = {"runtime_version": "v2", "os": "windows", "kind": "package",
                       "name": "nginx", "version": "1.31.7", "arch": "x64", "build_revision": 0}
        self.producer = {"commit": "a" * 40, "run_id": "12345", "run_attempt": 2}
        self.base = {"version": "2.0.1", "runtime_sha256": "b" * 64, "devlib_sha256": "c" * 64}

    def create(self, **changes):
        values = dict(package_path=self.archive, identity=self.record, producer=self.producer,
                      base=None, minimum_os="10.0",
                      source={"version": "1.31.7", "path": str(self.source)})
        values.update(changes)
        return self.helper.create_meta(**values)

    def test_windows_metadata_has_canonical_identity_layout_and_no_base_key(self):
        meta = self.create()
        self.assertEqual({key: meta[key] for key in self.record}, dict(self.record, arch="x86_64"))
        self.assertEqual(meta["schema_version"], 2)
        self.assertEqual(meta["minimum_os"], "10.0")
        self.assertEqual((meta["layout"], meta["layout_version"]), ("servbay-v2", 1))
        self.assertEqual((meta["package_root"], meta["config_root"]), ("nginx", "nginx"))
        self.assertEqual(meta["entrypoint"], {"path": "nginx/sbin/nginx.exe", "type": "binary"})
        self.assertNotIn("base", meta)
        self.assertEqual(meta["producer"], self.producer)
        self.assertEqual((meta["major"], meta["display_name"]), ("1", "NGINX"))
        self.assertNotIn(str(self.root), json.dumps(meta))

    def test_windows_metadata_uses_shared_series_and_script_entrypoint(self):
        for name, version, root, entrypoint in [
            ("redis", "7.4.11", "redis/7.4", {"path": "redis/7.4/bin/redis-server.exe", "type": "binary"}),
            ("composer", "2.2.30", "composer/2.2",
             {"path": "composer/2.2/bin/composer", "type": "php-script", "interpreter": "php"}),
        ]:
            with self.subTest(name=name):
                archive = self.root / f"{name}-{version}-windows-x86_64.7z"
                archive.write_bytes(b"synthetic package bytes")
                meta = self.create(package_path=archive, identity=dict(self.record, name=name, version=version),
                                   source={"version": version, "path": self.source})
                self.assertEqual((meta["package_root"], meta["config_root"]), (root, root))
                self.assertEqual(meta["entrypoint"], entrypoint)

    def test_arch_alias_has_same_identity_and_artifact_but_macos_is_distinct(self):
        canonical = dict(self.record, arch="x86_64")
        state = self.helper._state
        self.assertEqual(state.identity(self.record), state.identity(canonical))
        self.assertNotEqual(state.identity(canonical), state.identity(dict(canonical, os="macos")))
        self.assertEqual(self.helper.artifact_name(self.record), "pkg2-windows-x86_64-package-nginx-1.31.7")
        self.assertEqual(self.helper.artifact_name(dict(canonical, build_revision=2)),
                         "pkg2-windows-x86_64-package-nginx-1.31.7-r2")
        self.assertEqual(self.create(), self.create(identity=canonical))
        self.assertEqual(self.record["arch"], "x64")

    def test_archive_and_source_hashes_are_computed_from_separate_real_bytes(self):
        meta = self.create(source={"version": "1.31.7", "path": self.source,
                                   "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest()})
        self.assertEqual(meta["filename"], self.archive.name)
        self.assertEqual(meta["sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(meta["source"]["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertNotEqual(meta["sha256"], meta["source"]["sha256"])

    def test_windows_rejects_every_supplied_base_even_falsey_objects(self):
        for base in ({}, self.base, [], "", False, 0):
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "base"):
                self.create(base=base)

    def test_windows_rejects_unsupported_architectures_and_v1(self):
        for changes in ({"arch": "arm64"}, {"arch": "x86"}, {"arch": "amd64"},
                        {"arch": "X64"}, {"runtime_version": "v1"}):
            with self.subTest(changes=changes):
                record = dict(self.record, **changes)
                with self.assertRaises(ValueError):
                    self.helper._state.clean_record(record)
                with self.assertRaises(ValueError):
                    self.create(identity=record)

    def test_wrong_platform_architecture_version_and_format_filenames_are_rejected(self):
        for filename in ("nginx-1.31.7.tar.gz", "nginx-1.31.7-windows-x64.7z",
                         "nginx-1.31.7-macos-x86_64.7z", "nginx-1.31.8-windows-x86_64.7z",
                         "nginx-1.31.7-windows-x86_64.zip"):
            with self.subTest(filename=filename):
                archive = self.root / filename
                archive.write_bytes(self.archive.read_bytes())
                with self.assertRaisesRegex(ValueError, "filename"):
                    self.create(package_path=archive)

    def test_source_version_must_match_identity(self):
        with self.assertRaisesRegex(ValueError, "Source version"):
            self.create(source={"version": "1.31.8", "path": self.source})

    def test_missing_source_cannot_borrow_package_bytes(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.create(source={"version": "1.31.7", "path": self.root / "missing.zip"})

    def test_missing_or_empty_archive_is_rejected(self):
        for directory, content in (("missing", None), ("empty", b"")):
            with self.subTest(directory=directory):
                path = self.root / directory / self.archive.name
                path.parent.mkdir()
                if content is not None:
                    path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "missing or empty"):
                    self.create(package_path=path)

    def test_windows_retains_strict_producer_and_minimum_os_validation(self):
        for producer in (dict(self.producer, run_id=12345), dict(self.producer, run_attempt=True),
                         dict(self.producer, commit="bad"), dict(self.producer, extra="value")):
            with self.subTest(producer=producer), self.assertRaisesRegex(ValueError, "producer"):
                self.create(producer=producer)
        with self.assertRaisesRegex(ValueError, "minimum_os"):
            self.create(minimum_os="unknown")

    def test_windows_rejects_unverified_layout_instead_of_emitting_flat_metadata(self):
        archive = self.root / "dotnetsdk-8.0.408-windows-x86_64.7z"
        archive.write_bytes(b"synthetic package bytes")
        with self.assertRaisesRegex(ValueError, "windows entrypoint"):
            self.create(package_path=archive,
                        identity=dict(self.record, name="dotnetsdk", version="8.0.408"),
                        source={"version": "8.0.408", "path": self.source})

    def test_macos_requires_valid_base_and_keeps_original_platform_rules(self):
        record = dict(self.record, os="macos", arch="x86_64")
        archive = self.root / "nginx-1.31.7.tar.gz"
        archive.write_bytes(b"synthetic macos package bytes")
        for base in (None, {}, dict(self.base, runtime_sha256=""), dict(self.base, devlib_sha256="")):
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.create(package_path=archive, identity=record, base=base)
        meta = self.create(package_path=archive, identity=record, base=self.base)
        self.assertEqual(meta["base"], self.base)
        self.assertNotIn("layout", meta)
        with self.assertRaises(ValueError):
            self.helper._state.clean_record(dict(record, arch="x64"))
        self.assertEqual(self.helper._state.clean_record(dict(record, runtime_version="v1"))["runtime_version"], "v1")
        with self.assertRaises(ValueError):
            self.helper.artifact_name(dict(record, runtime_version="v1"))

    def cli(self, record, archive, base_marker=UNSET):
        paths = {}
        for name, value in {"identity": record, "producer": self.producer,
                            "source": {"version": "1.31.7", "path": str(self.source)}}.items():
            paths[name] = self.root / (name + ".json")
            paths[name].write_text(json.dumps(value))
        output = self.root / "meta.json"
        args = [sys.executable, str(ROOT / "runtime/ci/package_meta.py"), "--package", str(archive),
                "--minimum-os", "10.0", "--output", str(output)]
        for key, path in paths.items():
            args.extend(["--" + key, str(path)])
        if base_marker is not UNSET:
            base_path = self.root / "base.json"
            base_path.write_text(json.dumps(base_marker))
            args.extend(["--base", str(base_path)])
        return subprocess.run(args, cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                              capture_output=True, text=True), output

    def test_cli_windows_omits_base_and_emits_canonical_artifact(self):
        result, output = self.cli(self.record, self.archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "pkg2-windows-x86_64-package-nginx-1.31.7")
        meta = json.loads(output.read_text())
        self.assertNotIn("base", meta)
        self.assertEqual(meta["arch"], "x86_64")

    def test_cli_macos_without_base_still_fails(self):
        archive = self.root / "nginx-1.31.7.tar.gz"
        archive.write_bytes(b"synthetic macos package bytes")
        result, output = self.cli(dict(self.record, os="macos", arch="x86_64"), archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("base", result.stderr.lower())
        self.assertFalse(output.exists())

    def test_cli_windows_fake_base_fails_without_writing_metadata(self):
        for base in ({}, None):
            with self.subTest(base=base):
                result, output = self.cli(self.record, self.archive, base)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("base", result.stderr.lower())
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
