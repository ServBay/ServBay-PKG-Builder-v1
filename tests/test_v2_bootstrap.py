import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-v2/ci/bootstrap_base.sh"


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary_tmp = tempfile.TemporaryDirectory()
        cls.binary_root = Path(cls.binary_tmp.name)
        source = cls.binary_root / "fixture.c"
        source.write_text("int fixture(void) { return 17; }\n")
        cls.dylib = cls.binary_root / "libfixture.dylib"
        result = subprocess.run(["/usr/bin/clang", "-dynamiclib", "-arch", "x86_64", str(source), "-o", str(cls.dylib)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.arm_dylib = cls.binary_root / "libfixture-arm64.dylib"
        subprocess.run(["/usr/bin/clang", "-dynamiclib", "-arch", "arm64", str(source), "-o", str(cls.arm_dylib)],
                       check=True, capture_output=True)
        obj = cls.binary_root / "fixture.o"
        subprocess.run(["/usr/bin/clang", "-c", "-arch", "x86_64", str(source), "-o", str(obj)],
                       check=True, capture_output=True)
        cls.static = cls.binary_root / "libfixture.a"
        subprocess.run(["/usr/bin/libtool", "-static", "-o", str(cls.static), str(obj)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.binary_tmp.cleanup()

    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "The v2 base bootstrap entrypoint is missing")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "base"
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.runtime = self.make_archive("Runtime", {"packages/common/lib/libfixture.dylib": self.dylib.read_bytes()})
        self.devlib = self.make_archive("Devlib", {"packages/common/include/fixture.h": b"int fixture(void);\n"})

    def make_archive(self, kind, files):
        path = self.downloads / f"ServBay-{kind}-v2.0.1-x86_64.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    def run_bootstrap(self, arch="x86_64", use_default_layout=False, **changes):
        env = dict(os.environ, BASE_DOWNLOAD_URL=self.downloads.as_uri(), BASE_REMOTE_SUBPATH="",
                   BASE_VERSION="2.0.1", BASE_SHA256_RUNTIME=hashlib.sha256(self.runtime.read_bytes()).hexdigest(),
                   BASE_SHA256_DEVLIB=hashlib.sha256(self.devlib.read_bytes()).hexdigest(),
                   SERVBAY_BASE_FULL_PATH=str(self.base))
        env.update(changes)
        if use_default_layout:
            env.pop("BASE_REMOTE_SUBPATH", None)
        return subprocess.run(["/bin/bash", str(SCRIPT), arch], env=env, capture_output=True, text=True)

    def stage_published_archives(self, arch, escaping_link=False):
        self.base = self.root / ("base-" + arch)
        published = self.downloads / "macOS/runtime" / arch
        published.mkdir(parents=True)
        binary = self.arm_dylib if arch == "arm64" else self.dylib
        paths = []
        for kind, files in (("Runtime", {"packages/common/lib/libfixture.dylib": binary.read_bytes()}),
                            ("Devlib", {"packages/common/include/fixture.h": b"int fixture(void);\n"})):
            path = published / f"ServBay-{kind}-v2.0.1-{arch}.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                if kind == "Runtime":
                    info = tarfile.TarInfo("packages/common/lib/libfixture-current.dylib")
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/outside/libfixture.dylib" if escaping_link else "libfixture.dylib"
                    archive.addfile(info)
            paths.append(path)
        self.runtime, self.devlib = paths
        return binary

    def test_default_download_layout_consumes_both_actual_v2_archives_for_each_architecture(self):
        for arch in ("x86_64", "arm64"):
            with self.subTest(arch=arch):
                binary = self.stage_published_archives(arch)
                original = {path: path.read_bytes() for path in (self.runtime, self.devlib)}
                result = self.run_bootstrap(arch, use_default_layout=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(binary.read_bytes(), (self.base / "packages/common/lib/libfixture.dylib").read_bytes())
                self.assertEqual(b"int fixture(void);\n", (self.base / "packages/common/include/fixture.h").read_bytes())
                link = self.base / "packages/common/lib/libfixture-current.dylib"
                self.assertTrue(link.is_symlink())
                self.assertEqual("libfixture.dylib", os.readlink(link))
                marker = json.loads((self.base / ".bootstrap-v2.json").read_text())
                self.assertEqual(arch, marker["arch"])
                self.assertEqual(hashlib.sha256(original[self.runtime]).hexdigest(), marker["runtime_sha256"])
                self.assertEqual(hashlib.sha256(original[self.devlib]).hexdigest(), marker["devlib_sha256"])
                for path, contents in original.items():
                    self.assertEqual(contents, path.read_bytes())

    def test_default_v2_layout_still_rejects_an_escaping_archive_link(self):
        for arch in ("x86_64", "arm64"):
            with self.subTest(arch=arch):
                self.stage_published_archives(arch, escaping_link=True)
                result = self.run_bootstrap(arch, use_default_layout=True)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Escaping build-base link", result.stderr)
                self.assertFalse(self.base.exists())

    def test_actual_runtime_and_devlib_archives_make_one_v2_base(self):
        result = self.run_bootstrap()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((self.base / "packages/common/lib/libfixture.dylib").is_file())
        self.assertTrue((self.base / "packages/common/include/fixture.h").is_file())
        self.assertFalse((self.base / "package").exists())
        self.assertIn("devlib_version=2.0.1", result.stdout)

    def test_wrong_sha_fails_without_installing_partial_base(self):
        result = self.run_bootstrap(BASE_SHA256_DEVLIB="0" * 64)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.base.exists())

    def test_legacy_root_is_rejected(self):
        self.make_archive("Runtime", {"ServBay/package/common/lib/a": b"not v2"})
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.base.exists())

    def test_wrong_architecture_is_rejected_from_actual_macho(self):
        for path in (self.runtime, self.devlib):
            path.rename(path.with_name(path.name.replace("x86_64", "arm64")))
        self.runtime = self.downloads / self.runtime.name.replace("x86_64", "arm64")
        self.devlib = self.downloads / self.devlib.name.replace("x86_64", "arm64")
        result = self.run_bootstrap(arch="arm64")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.base.exists())

    def test_missing_configuration_fails_before_any_base_changes(self):
        for field in ("BASE_DOWNLOAD_URL", "BASE_VERSION", "BASE_SHA256_RUNTIME", "BASE_SHA256_DEVLIB"):
            with self.subTest(field=field):
                result = self.run_bootstrap(**{field: ""})
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(self.base.exists())

    def test_cache_identity_includes_both_hashes_and_runtime(self):
        first = self.run_bootstrap()
        self.assertEqual(0, first.returncode, first.stderr)
        before = (self.base / "packages/common/lib/libfixture.dylib").read_bytes()
        changed = self.run_bootstrap(BASE_SHA256_RUNTIME="0" * 64)
        self.assertNotEqual(0, changed.returncode)
        self.assertEqual(before, (self.base / "packages/common/lib/libfixture.dylib").read_bytes())
        hit = self.run_bootstrap()
        self.assertEqual(0, hit.returncode, hit.stderr)
        marker = json.loads((self.base / ".bootstrap-v2.json").read_text())
        self.assertEqual("v2", marker["runtime_version"])

    def test_existing_v1_tree_is_not_moved_or_overwritten(self):
        original = self.base / "package/common/original"
        original.parent.mkdir(parents=True)
        original.write_text("keep")
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("keep", original.read_text())

    def test_tar_path_traversal_is_rejected_without_writing_outside_candidate(self):
        self.make_archive("Runtime", {"../escaped": b"bad"})
        result = self.run_bootstrap()
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / "escaped").exists())
        self.assertFalse(self.base.exists())

    def test_correct_runtime_cannot_hide_a_wrong_architecture_devlib_static_archive(self):
        self.make_archive("Runtime", {"packages/common/lib/libfixture.dylib": self.arm_dylib.read_bytes()})
        self.make_archive("Devlib", {"packages/common/include/fixture.h": b"int fixture(void);\n",
                                     "packages/common/static/lib/libfixture.a": self.static.read_bytes()})
        for path in (self.runtime, self.devlib):
            path.rename(path.with_name(path.name.replace("x86_64", "arm64")))
        self.runtime = self.downloads / self.runtime.name.replace("x86_64", "arm64")
        self.devlib = self.downloads / self.devlib.name.replace("x86_64", "arm64")
        result = self.run_bootstrap(arch="arm64")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.base.exists())


if __name__ == "__main__":
    unittest.main()
