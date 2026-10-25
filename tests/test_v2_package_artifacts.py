import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackageArtifactTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime/ci/package_meta.py"
        self.assertTrue(path.is_file(), "The schema2 package metadata helper is missing")
        spec = importlib.util.spec_from_file_location("package_meta", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "nginx-1.31.7.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            info = tarfile.TarInfo("nginx/VERSION")
            info.size = 7
            archive.addfile(info, io.BytesIO(b"1.31.7\n"))
        self.source = self.root / "nginx-1.31.7-source.tar.gz"
        self.source.write_bytes(b"actual upstream archive bytes")
        self.record = {"runtime_version": "v2", "os": "macos", "kind": "package", "name": "nginx",
                       "version": "1.31.7", "arch": "x86_64", "build_revision": 0}
        self.producer = {"commit": "a" * 40, "run_id": "12345", "run_attempt": 2}
        self.base = {"version": "2.0.1", "runtime_sha256": "b" * 64, "devlib_sha256": "c" * 64}

    def create(self, **changes):
        values = dict(package_path=self.archive, identity=self.record, producer=self.producer,
                      base=self.base, minimum_os="12.0", source={"version": "1.31.7", "path": self.source})
        values.update(changes)
        return self.module.create_meta(**values)

    def test_schema2_hashes_actual_package_and_upstream_bytes_separately(self):
        meta = self.create()
        self.assertEqual(2, meta["schema_version"])
        self.assertEqual("v2", meta["runtime_version"])
        self.assertEqual("macos", meta["os"])
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), meta["sha256"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), meta["source"]["sha256"])
        self.assertNotEqual(meta["sha256"], meta["source"]["sha256"])
        self.assertEqual(self.producer, meta["producer"])
        self.assertNotIn(str(self.root), str(meta))

    def test_missing_package_never_emits_metadata(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            self.create(package_path=self.root / "missing.tar.gz")

    def test_missing_actual_source_never_reuses_package_hash(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            self.create(source={"version": "1.31.7", "path": self.root / "missing-source.tar.gz"})

    def test_legacy_record_cannot_be_upgraded_by_filename(self):
        record = dict(self.record)
        record.pop("runtime_version")
        with self.assertRaises(ValueError):
            self.create(identity=record)

    def test_artifact_name_isolated_by_architecture_kind_and_revision(self):
        first = self.module.artifact_name(self.record)
        second = self.module.artifact_name(dict(self.record, arch="arm64", build_revision=3))
        self.assertEqual("pkg2-macos-x86_64-package-nginx-1.31.7", first)
        self.assertEqual("pkg2-macos-arm64-package-nginx-1.31.7-r3", second)
        self.assertFalse(first.startswith("pkg-"))

    def test_wrong_filename_does_not_borrow_another_package(self):
        with self.assertRaises(ValueError):
            self.create(identity=dict(self.record, name="php"))

    def test_source_url_must_be_public_without_credentials(self):
        with self.assertRaises(ValueError):
            self.create(source={"version": "1.31.7", "path": self.source, "url": "https://user:secret@example.com/a"})

    def test_base_and_producer_are_validated_before_handoff(self):
        for values in ({"base": dict(self.base, devlib_sha256="")},
                       {"producer": dict(self.producer, run_id=12345)},
                       {"producer": dict(self.producer, run_attempt=True)}, {"minimum_os": "unknown"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.create(**values)

    def test_minimum_os_comes_from_the_actual_macho_load_commands(self):
        source = self.root / "fixture.c"
        source.write_text("int fixture(void) { return 17; }\n")
        library = self.root / "libfixture.dylib"
        result = subprocess.run(["/usr/bin/clang", "-dynamiclib", "-arch", "x86_64",
                                 "-mmacosx-version-min=14.0", str(source), "-o", str(library)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        with tarfile.open(self.archive, "w:gz") as archive:
            archive.add(library, arcname="nginx/libfixture.dylib")
        self.assertTrue(hasattr(self.module, "minimum_os_from_archive"), "Actual Mach-O minimum OS inspection is missing")
        self.assertEqual("14.0", self.module.minimum_os_from_archive(self.archive, "12.0"))

    def test_major_and_display_name_follow_actual_v2_install_series(self):
        self.assertTrue(hasattr(self.module, "package_metadata"), "The authoritative v2 package series mapping is missing")
        cases = [
            ("redis", "8.4.2", "8.4", "Redis"), ("php", "8.6.0-dev-20261021", "8.6", "PHP 8.6"),
            ("python", "3.15.0b4", "3.15", "Python 3.15"), ("go", "1.27.1", "1.27", "Go 1.27"),
            ("mysql", "8.4.11", "8.4", "MySQL 8.4"), ("mariadb", "11.8.9", "11.8", "MariaDB 11.8"),
            ("postgresql", "19.beta4", "19", "PostgreSQL 19"), ("openjdk", "21.0.12.1", "21", "OpenJDK 21"),
            ("node", "26.10.0", "26", "Node.js 26"), ("mono", "6.14.1", "6.14", "Mono 6.14"),
            ("dotnetsdk", "11.0.100-rc1", "11.0", ".NET SDK 11.0"),
            ("dotnetsdk", "2.1.202", "2.0", ".NET SDK 2.0"),
            ("nginx", "1.31.7", "1", "NGINX"), ("openssl", "3.6.1", "3", "OpenSSL"),
            ("rust", "1.99.1", "1", "Rust"), ("simple-acme", "2.4.1", "2", "Simple ACME"),
            ("caddy", "2.11.7", "2", "Caddy"),
            ("apache", "2.4.69", "2.4", "Apache 2.4"), ("composer", "2.2.30", "2.2", "Composer 2.2 LTS"),
            ("composer", "2.10.3", "2", "Composer"), ("mongosh", "2.12.0", "2.0", "Mongo Shell"),
            ("ai-gateway", "1.0.0", "1", "ServBay AI Gateway"),
            ("disk-cleaner", "1.0.0", "1", "ServBay DiskCleaner"),
        ]
        for name, version, major, display in cases:
            with self.subTest(name=name, version=version):
                self.assertEqual({"major": major, "display_name": display}, self.module.package_metadata(name, version))
        meta = self.create()
        self.assertEqual(("1", "NGINX"), (meta["major"], meta["display_name"]))

    def test_every_real_profile_package_has_an_explicit_metadata_mapping(self):
        self.assertTrue(hasattr(self.module, "package_metadata"), "The v2 package mapping is missing")
        conf = ROOT / "runtime-v2/config/packages.conf"
        self.assertTrue(conf.is_file())
        for line in conf.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            name, version, *_ = line.split()
            name = "apache" if name == "httpd" else name
            with self.subTest(name=name, version=version):
                result = self.module.package_metadata(name, version)
                self.assertTrue(result["display_name"])
                self.assertRegex(result["major"], r"^\d+(?:\.\d+)?$")
        with self.assertRaises(ValueError):
            self.module.package_metadata("unregistered-package", "1.2.3")

    def test_metadata_major_matches_the_real_v2_packing_function_for_versioned_packages(self):
        text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()

        def function(name):
            start = text.index(name + "() {")
            return text[start:text.index("\n}\n", start) + 3]

        functions = function("_get_dotnetsdk_major_version") + function("get_package_packing_info")
        cases = [("redis", "8.4.2"), ("php", "8.6.0-dev-20261021"), ("mysql", "8.4.11"),
                 ("mariadb", "11.8.9"), ("postgresql", "19.beta4"), ("mongodb", "8.3.11"),
                 ("python", "3.15.0b4"), ("go", "1.27.1"), ("node", "26.10.0"),
                 ("openjdk", "21.0.12.1"), ("mono", "6.14.1"), ("ruby", "4.0.7"),
                 ("dotnetsdk", "11.0.100-rc1"), ("dotnetsdk", "2.1.202"),
                 ("composer", "2.2.30"), ("composer", "2.10.3")]
        for name, version in cases:
            with self.subTest(name=name, version=version):
                script = ('typeset -A PACK_INFO\ntypeset -a PACK_FILES\n'
                          'SERVBAY_PACKAGE_FULL_PATH=/fixture/packages\nlog_message() { :; }\n' + functions +
                          f'\nget_package_packing_info {shlex.quote(name)} {shlex.quote(version)} || exit 1\n'
                          'print -r -- "${PACK_FILES[1]}"\n')
                result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(f"{name}/{self.module.package_metadata(name, version)['major']}", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
