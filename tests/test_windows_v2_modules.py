"""Synthetic payload contract tests; these do not claim Windows DLL loading."""
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SEVEN_ZIP = os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7zz")
COMPAT = {"distribution": "synthetic-portable-family", "toolchain": "synthetic-vc-family", "crt": "synthetic-crt-family"}
ABI = {"module_api": 20240924, "zend_extension_api": 420240924,
       "extension_build": "API20240924,NTS", "zend_extension_build": "API420240924,NTS", "zts": False, "debug": False}
EXT = "lib/php/extensions/no-debug-non-zts-20240924"


def load(relative):
    spec = importlib.util.spec_from_file_location("tested_" + Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WindowsModuleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.helper = load("runtime-v2/ci/module_manifest.py")
        self.host = {"type": "php", "series": "8.4", "version": "8.4.27", "package_sha256": "c" * 64, "abi": copy.deepcopy(ABI)}
        self.file(EXT + "/php_imagick.dll", b"synthetic module")
        self.file("bin/CORE_RL_synthetic.dll", b"synthetic dependency")
        self.file("share/ImageMagick/config.xml", b"synthetic resources")

    def file(self, name, content=b"fixture"):
        path = self.stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def manifest(self, **kwargs):
        args = dict(arch="x64", minimum_os="10.0", base=None, source_name="imagick", extension_dir=EXT,
                    os_name="windows", windows_compatibility=copy.deepcopy(COMPAT))
        args.update(kwargs)
        return self.helper.create_manifest(self.host, self.stage,
            [{"scope": "config", "install_path": "conf.d/imagick.ini", "mode": "0644", "content": "extension=php_imagick.dll\n"}],
            ["imagick"], [], **args)

    def archive(self, archive_type="7z", options=()):
        path = self.root / "fixture.7z"
        subprocess.run([SEVEN_ZIP, "a", "-t" + archive_type, *options, str(path), "."], cwd=self.stage, check=True, capture_output=True)
        return path

    def validate(self, manifest, package=None, **kwargs):
        args = dict(expected_os="windows", seven_zip=SEVEN_ZIP)
        args.update(kwargs)
        self.helper.validate_manifest(manifest, package or self.archive(), **args)

    def test_complete_tree_has_exact_paths_hashes_and_platform(self):
        manifest = self.manifest()
        self.assertEqual("windows", manifest["os"])
        self.assertEqual("x86_64", manifest["arch"])
        self.assertEqual(("servbay-v2", 1), (manifest["layout"], manifest["layout_version"]))
        self.assertEqual(COMPAT, manifest["windows_compatibility"])
        self.assertNotIn("base", manifest)
        self.assertEqual(EXT, manifest["extension_dir"])
        self.assertEqual({EXT + "/php_imagick.dll", "bin/CORE_RL_synthetic.dll", "share/ImageMagick/config.xml"}, {r["archive_path"] for r in manifest["files"]})
        for row in manifest["files"]:
            self.assertEqual(row["archive_path"], row["install_path"])
            self.assertEqual(hashlib.sha256((self.stage / row["archive_path"]).read_bytes()).hexdigest(), row["sha256"])
        self.validate(manifest)

    def test_package_metadata_and_real_7z_roundtrip(self):
        manifest = self.manifest()
        source = self.root / "source.zip"
        source.write_bytes(b"synthetic upstream input")
        identity = {"runtime_version": "v2", "os": "windows", "kind": "php-extension", "name": "imagick", "version": "3.8.0", "arch": "x64", "build_revision": 1,
                    "host": {k: self.host[k] for k in ("type", "series", "abi")}}
        result = self.helper.write_package(manifest, self.stage, identity,
                    {"commit": "e" * 40, "run_id": "123", "run_attempt": 1},
                    {"version": "3.8.0", "path": str(source)}, self.root / "dist", seven_zip=SEVEN_ZIP)
        meta = result["meta"]
        self.assertEqual("ext-imagick-3.8.0-php8.4-windows-x86_64.7z", meta["filename"])
        self.assertNotIn("base", meta)
        for key in ("os", "arch", "layout", "layout_version", "windows_compatibility"):
            self.assertEqual(manifest[key], meta[key])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), meta["source"]["sha256"])
        self.assertEqual(hashlib.sha256(Path(result["package"]).read_bytes()).hexdigest(), meta["sha256"])
        self.assertEqual(hashlib.sha256(Path(result["manifest"]).read_bytes()).hexdigest(), meta["manifest_sha256"])
        self.assertEqual(manifest, json.loads(Path(result["manifest"]).read_text()))
        self.validate(manifest, result["package"])
        identity["os"] = "macos"
        identity["arch"] = "x86_64"
        with self.assertRaises(ValueError):
            self.helper.write_package(manifest, self.stage, identity, {}, {}, self.root / "bad")

    def test_archive_member_names_are_not_command_options_or_listfiles(self):
        for name in ("@missing-list", "-sdel", "share/多语言 settings.xml", "share/COM10.txt"):
            self.file(name, name.encode())
        helper = load("runtime/ci/windows_archive.py")
        names = helper.staging_files(self.stage)
        package = self.root / "literal.7z"
        helper.create_archive(self.stage, package, names, SEVEN_ZIP)
        self.assertEqual({name: hashlib.sha256((self.stage / name).read_bytes()).hexdigest() for name in names},
                         helper.archive_hashes(package, SEVEN_ZIP))
        self.assertTrue((self.stage / "-sdel").is_file())

    def test_archive_compression_level_is_explicitly_bounded(self):
        helper = load("runtime/ci/windows_archive.py")
        stage = self.root / "compression-stage"
        stage.mkdir()
        (stage / "payload").write_bytes(b"fixture")
        names = ["payload"]
        for value in (-1, 10, True, "7"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                helper.create_archive(stage, self.root / "invalid.7z", names, SEVEN_ZIP,
                                      compression_level=value)
        archive = self.root / "fast.7z"
        helper.create_archive(stage, archive, names, SEVEN_ZIP, compression_level=1)
        self.assertEqual({"payload": hashlib.sha256(b"fixture").hexdigest()},
                         helper.archive_hashes(archive, SEVEN_ZIP))

    def test_crlf_and_windows_separator_listing_is_canonicalized_consistently(self):
        helper = load("runtime/ci/windows_archive.py")
        archive = self.root / "crlf-listing.7z"
        archive.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"synthetic")
        file_block = (b"Path = bin\\nginx.exe\r\nSize = 9\r\nAttributes = A\r\n"
                      b"CRC =\r\nEncrypted = -\r\nBlock =\r\n")
        directory_block = (b"Path = conf\r\nAttributes = D\r\n"
                           b"CRC =\r\nEncrypted = -\r\nBlock =\r\n")
        full_listing = file_block + b"\r\n" + directory_block

        def fake_run(command, cwd=None):
            target = command[-1]
            if target == str(archive.absolute()):
                return full_listing
            return {"bin/nginx.exe": file_block, "conf": directory_block}[target]

        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(b"nginx-exe")
        process.wait.return_value = 0
        with mock.patch.object(helper, "_run", side_effect=fake_run), \
                mock.patch.object(helper.subprocess, "Popen", return_value=process):
            self.assertEqual({"bin/nginx.exe": hashlib.sha256(b"nginx-exe").hexdigest()},
                             helper.archive_hashes(archive, seven_zip="7z"))

    def test_windows_link_attribute_is_rejected_before_member_read(self):
        helper = load("runtime/ci/windows_archive.py")
        archive = self.root / "windows-link.7z"
        archive.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"synthetic")
        link_block = (b"Path = linked\r\nSize = 8\r\nAttributes = AL\r\n"
                      b"Encrypted = -\r\n")
        file_block = (b"Path = payload.exe\r\nSize = 7\r\nAttributes = A\r\n"
                      b"Encrypted = -\r\n")
        full_listing = link_block + b"\r\n" + file_block

        def fake_run(command, cwd=None):
            target = command[-1]
            if target == str(archive.absolute()):
                return full_listing
            return {"linked": link_block, "payload.exe": file_block}[target]

        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(b"payload")
        process.wait.return_value = 0
        with mock.patch.object(helper, "_run", side_effect=fake_run), \
                mock.patch.object(helper.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "links, special or encrypted"):
                helper.archive_hashes(archive, seven_zip="7z")

    def test_base_arch_os_and_layout_rejections(self):
        for base in ({}, {"version": "fake"}, {"version": "v2", "runtime_sha256": "a" * 64, "devlib_sha256": "b" * 64}):
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.manifest(base=base)
        for arch in ("arm64", "x86"):
            with self.subTest(arch=arch), self.assertRaises(ValueError):
                self.manifest(arch=arch)
        original = self.manifest()
        for key, value in (("base", None), ("os", "macos"), ("os", None), ("arch", "x64"), ("layout", "bootstrap"), ("layout_version", True), ("layout_version", 2)):
            bad = copy.deepcopy(original)
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.validate(bad)
        with self.assertRaises(ValueError):
            self.validate(original, expected_os="macos")
        with self.assertRaises(ValueError):
            self.validate(original, expected_os=None)

    def test_compatibility_requires_exact_three_real_strings(self):
        for compat in (None, {}, dict(COMPAT, extra="value"), dict(COMPAT, crt=""), dict(COMPAT, crt="  "), dict(COMPAT, crt=10)):
            with self.subTest(compat=compat), self.assertRaises(ValueError):
                self.manifest(windows_compatibility=compat)
        custom = {key: "another family " + key for key in COMPAT}
        self.assertEqual(custom, self.manifest(windows_compatibility=custom)["windows_compatibility"])

    def test_php_extension_location_and_no_reprefix(self):
        for extension in ("ext", "lib/php/extensions", "lib/php/extensions/wrong-abi"):
            with self.subTest(extension=extension), self.assertRaises(ValueError):
                self.manifest(extension_dir=extension)
        manifest = self.manifest()
        manifest["files"][0]["install_path"] = EXT + "/" + manifest["files"][0]["archive_path"]
        with self.assertRaises(ValueError):
            self.validate(manifest)

    def test_config_rejects_windows_absolute_extension_paths_but_keeps_settings(self):
        original = self.manifest()
        for content in ('extension="C:\\build\\php_imagick.dll"\n',
                        'zend_extension=C:/build/php_imagick.dll\n',
                        'extension=\\\\server\\share\\php_imagick.dll\n',
                        'extension=\\build\\php_imagick.dll\n'):
            manifest = copy.deepcopy(original)
            manifest["config_files"][0].update(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.validate(manifest)
        content = 'extension=php_imagick.dll\nsession.save_path="C:\\Temp"\n'
        original["config_files"][0].update(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
        self.validate(original)

    def test_php_dependency_tree_cannot_replace_the_actual_extension(self):
        (self.stage / EXT / "php_imagick.dll").rename(self.root / "retained-module.dll")
        with self.assertRaises(ValueError):
            self.manifest()

    def test_windows_path_rules_directly_reject_each_invalid_namespace(self):
        helper = load("runtime/ci/windows_archive.py")
        for name in ("../escape", "/escape", "a//b", "a/./b", "C:escape", "a:stream", "a\\b", "NUL.dll", "a/CoN.txt", "COM¹.log", "LPT³", "a./b", "a /b", "bad?file", "bad*file", "bad|file", "bad<file", 'bad"file', "bad\x01file"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                helper.validate_path(name)
        for files, directories in ((["bin/a", "bin/a"], []), (["bin/a", "BIN/b"], []), (["bin/A", "bin/a"], []),
                                   (["bin", "bin/a"], []), (["bin/a"], ["BIN"]), (["bin"], ["bin"])):
            with self.subTest(files=files, directories=directories), self.assertRaises(ValueError):
                helper.validate_paths(files, directories)
        helper.validate_paths(["bin/a", "bin/b", "share/COM10.txt", "share/你好.txt"], ["bin", "share"])

    def test_archive_reader_itself_rejects_links_and_unsafe_names(self):
        helper = load("runtime/ci/windows_archive.py")
        link = self.stage / "linked"
        link.symlink_to("bin/CORE_RL_synthetic.dll")
        package = self.archive(options=("-snl",))
        with self.assertRaises(ValueError):
            helper.archive_hashes(package, SEVEN_ZIP)
        link.rename(self.root / "retained-archive-link")
        package.rename(self.root / "linked.7z")
        package = self.root / "linked.7z"
        result = subprocess.run([SEVEN_ZIP, "a", "-ssc", str(package), "-sia:stream"],
                                input=b"raw invalid member", capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        with self.assertRaises(ValueError):
            helper.archive_hashes(package, SEVEN_ZIP)

    @unittest.skipIf(os.name == "nt", "Windows file APIs reject archived control-character names")
    def test_archive_rejects_control_characters_hidden_by_7z_listing(self):
        helper = load("runtime/ci/windows_archive.py")
        name = "empty\nInjected = value"
        bad = self.file(name, b"")
        package = self.archive()
        with self.subTest(kind="file"), self.assertRaises(ValueError):
            helper.archive_hashes(package, SEVEN_ZIP)
        bad.rename(self.root / "retained-empty")
        package.rename(self.root / "newline-file.7z")
        (self.stage / name).mkdir()
        with self.subTest(kind="directory"), self.assertRaises(ValueError):
            helper.archive_hashes(self.archive(), SEVEN_ZIP)

    def test_archive_writer_rechecks_stage_and_rejects_hardlinks(self):
        helper = load("runtime/ci/windows_archive.py")
        names = helper.staging_files(self.stage)
        root_link = self.root / "stage-link"
        root_link.symlink_to(self.stage, target_is_directory=True)
        with self.assertRaises(ValueError):
            helper.create_archive(root_link, self.root / "root-link.7z", names, SEVEN_ZIP)
        self.file("unowned")
        with self.assertRaises(ValueError):
            helper.create_archive(self.stage, self.root / "unowned.7z", names, SEVEN_ZIP)
        os.link(self.stage / "unowned", self.stage / "hardlink")
        with self.assertRaises(ValueError):
            helper.staging_files(self.stage)

    def test_php_zts_debug_directory_comes_from_actual_abi(self):
        self.host["abi"].update(zts=True, debug=True)
        directory = "lib/php/extensions/debug-zts-20240924"
        self.file(directory + "/php_synthetic.dll")
        self.validate(self.manifest(extension_dir=directory))
        with self.assertRaises(ValueError):
            self.manifest()

    def test_paths_reject_windows_reserved_and_escaping_values(self):
        original = self.manifest()
        bad_paths = ("../escape", "/escape", "a//b", "a/./b", "C:escape", "a:stream", "a\\b", "NUL.dll", "a/CoN.txt", "COM¹.log", "LPT³", "a. /b", "a./b", "a /b", "bad?file", "bad*file", "bad|file", "bad<file", 'bad"file', "bad\x01file")
        for key in ("archive_path", "install_path"):
            for name in bad_paths:
                bad = copy.deepcopy(original)
                bad["files"][0][key] = name
                with self.subTest(key=key, name=name), self.assertRaises(ValueError):
                    self.validate(bad)
        for name in bad_paths:
            bad = copy.deepcopy(original)
            bad["config_files"][0]["install_path"] = name
            with self.subTest(config=name), self.assertRaises(ValueError):
                self.validate(bad)

    def test_casefold_and_parent_collisions(self):
        original = self.manifest()
        for name in ("BIN/other.dll", "bin/core_rl_SYNTHETIC.dll", "bin", "BIN/CORE_RL_synthetic.dll/child"):
            bad = copy.deepcopy(original)
            row = copy.deepcopy(bad["files"][0])
            row.update(archive_path=name, install_path=name)
            bad["files"].append(row)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.validate(bad)
        bad = copy.deepcopy(original)
        row = copy.deepcopy(bad["config_files"][0])
        row["install_path"] = "CONF.D/other.ini"
        bad["config_files"].append(row)
        with self.assertRaises(ValueError):
            self.validate(bad)

    def test_staging_rejects_links_special_files_and_unsafe_directories(self):
        link = self.stage / "linked"
        link.symlink_to("bin")
        with self.assertRaises(ValueError):
            self.manifest()
        link.rename(self.root / "retained-link")
        if hasattr(os, "mkfifo"):
            fifo = self.stage / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                self.manifest()
            fifo.rename(self.root / "retained-pipe")
        # Win32 can report a reserved-device mkdir as successful without making
        # the directory visible to rglob; validate_path covers the name itself.
        if os.name != "nt":
            (self.stage / "NUL").mkdir()
            with self.assertRaises(ValueError):
                self.manifest()

    def test_archive_rejects_changed_missing_and_unowned_files(self):
        manifest = self.manifest()
        package = self.archive()
        self.file("share/ImageMagick/config.xml", b"changed")
        subprocess.run([SEVEN_ZIP, "u", str(package), "."], cwd=self.stage, check=True, capture_output=True)
        with self.assertRaises(ValueError):
            self.validate(manifest, package)
        manifest = self.manifest()
        self.file("extra", b"unowned")
        subprocess.run([SEVEN_ZIP, "u", str(package), "."], cwd=self.stage, check=True, capture_output=True)
        with self.assertRaises(ValueError):
            self.validate(manifest, package)
        manifest = self.manifest()
        subprocess.run([SEVEN_ZIP, "d", str(package), "extra"], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            self.validate(manifest, package)

    def test_archive_checks_real_format_not_extension(self):
        manifest = self.manifest()
        for kind in ("zip", "tar"):
            package = self.root / (kind + ".7z")
            if kind == "zip":
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr("payload", b"not 7z")
            else:
                with tarfile.open(package, "w") as archive:
                    archive.add(self.stage, arcname=".")
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.validate(manifest, package)

    def test_archive_rejects_links_and_reserved_members(self):
        manifest = self.manifest()
        self.file("NUL.dll")
        package = self.archive()
        with self.assertRaises(ValueError):
            self.validate(manifest, package)
        (self.stage / "NUL.dll").rename(self.root / "retained-nul")
        package.rename(self.root / "reserved.7z")
        (self.stage / "linked").symlink_to("bin/CORE_RL_synthetic.dll")
        with self.assertRaises(ValueError):
            self.validate(manifest, self.archive(options=("-snl",)))

    def test_windows_dependency_reference_retains_existing_host_abi_contract(self):
        manifest = self.manifest()
        dependency = {"kind": "php-extension", "name": "psr", "version": "1.2.0", "build_revision": 0, "sha256": "d" * 64,
                      "host": {k: copy.deepcopy(self.host[k]) for k in ("type", "series", "abi")}}
        manifest["dependencies"] = [dependency]
        self.validate(manifest)
        manifest["dependencies"][0]["host"]["abi"]["module_api"] = 20230831
        with self.assertRaises(ValueError):
            self.validate(manifest)

    def test_postgresql_control_sql_and_dependencies_are_owned(self):
        self.stage.rename(self.root / "retained-php-stage")
        self.stage.mkdir()
        self.host = {"type": "postgresql", "series": "18", "version": "18.6", "package_sha256": "d" * 64, "abi": {"major": 18}}
        self.file("lib/vector.dll")
        self.file("share/extension/vector.control", b"default_version='0.8.1'")
        self.file("share/extension/vector--0.8.1.sql", b"CREATE TYPE vector;")
        manifest = self.helper.create_manifest(self.host, self.stage, [], ["vector"], [], arch="x86_64", minimum_os="10.0", base=None, source_name="pgvector", os_name="windows", windows_compatibility=COMPAT)
        self.validate(manifest)
        source = self.root / "pgvector-source.zip"
        source.write_bytes(b"synthetic PostgreSQL extension source")
        identity = {"runtime_version": "v2", "os": "windows", "kind": "postgresql-extension", "name": "vector",
                    "version": "0.8.1", "arch": "x86_64", "build_revision": 0,
                    "host": {key: self.host[key] for key in ("type", "series", "abi")}}
        result = self.helper.write_package(manifest, self.stage, identity,
                    {"commit": "e" * 40, "run_id": "123", "run_attempt": 1},
                    {"version": "0.8.1", "path": str(source)}, self.root / "pgdist", seven_zip=SEVEN_ZIP)
        self.assertEqual("ext-vector-0.8.1-pgsql18-windows-x86_64.7z", result["meta"]["filename"])
        self.validate(manifest, result["package"])
        for missing in ("share/extension/vector.control", "share/extension/vector--0.8.1.sql"):
            bad = copy.deepcopy(manifest)
            bad["files"] = [row for row in bad["files"] if row["archive_path"] != missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.validate(bad)

    def test_macos_legacy_requires_macos_context_and_base(self):
        stage = self.root / "macos"
        stage.mkdir()
        (stage / "imagick.so").write_bytes(b"synthetic mach-o")
        args = dict(arch="x86_64", minimum_os="12.0", source_name="imagick", extension_dir="lib/custom")
        with self.assertRaises(ValueError):
            self.helper.create_manifest(self.host, stage, [], ["imagick"], [], base=None, **args)
        manifest = self.helper.create_manifest(self.host, stage, [], ["imagick"], [], base={"version": "2", "runtime_sha256": "a" * 64, "devlib_sha256": "b" * 64}, **args)
        package = self.root / "macos.tar.gz"
        with tarfile.open(package, "w:gz") as archive:
            archive.add(stage / "imagick.so", arcname="imagick.so")
        self.helper.validate_manifest(manifest, package)
        bad_mode = copy.deepcopy(manifest)
        bad_mode["files"][0]["mode"] = "0755"
        with self.assertRaises(ValueError):
            self.helper.validate_manifest(bad_mode, package)
        with self.assertRaises(ValueError):
            self.validate(manifest, package)
        manifest["os"] = "windows"
        with self.assertRaises(ValueError):
            self.helper.validate_manifest(manifest, package)


if __name__ == "__main__":
    unittest.main()
