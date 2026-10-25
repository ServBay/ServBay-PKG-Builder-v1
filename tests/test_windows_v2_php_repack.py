"""ZIP-to-real-7z PHP payload tests; these do not execute Windows binaries."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-windows/repack_php_v2.py"
SEVEN_ZIP = os.environ.get("SEVEN_ZIP") or shutil.which("7zz") or shutil.which("7z")
ABI = {"module_api": 20220829, "zend_extension_api": 420220829,
       "extension_build": "API20220829,NTS,VS16", "zend_extension_build": "API420220829,NTS,VS16",
       "zts": False, "debug": False}
EXT = "lib/php/extensions/no-debug-non-zts-20220829/"
VERSIONS = {"redis": "6.3.0", "memcache": "8.2", "memcached": "3.4.0",
            "xdebug": "3.5.0", "imagick": "3.8.1", "imap": "1.0.3",
            "apcu": "5.1.28", "mongodb": "2.2.1", "sqlsrv": "5.12.0",
            "pdo_sqlsrv": "5.12.0", "yaml": "2.3.0", "phalcon": "5.10.0",
            "protobuf": "4.29.6", "grpc": "1.78.0", "igbinary": "3.2.16",
            "msgpack": "msgpack-3.0.0", "oauth": "2.0.10"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


class WindowsV2PHPRepackTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "dedicated PHP payload repacker is missing")
        spec = importlib.util.spec_from_file_location("test_php_repack", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.assertTrue(SEVEN_ZIP, "real 7z is required")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.output = self.base / "output"
        self.core = {"runtime_version": "v2", "os": "windows", "kind": "package", "name": "php",
                     "version": "8.2.35", "arch": "x64", "build_revision": 0}
        self.identities = [{**self.core, "kind": "php-extension", "name": name, "version": version,
                            "host": {"type": "php", "series": "8.2", "abi": copy.deepcopy(ABI)}}
                           for name, version in VERSIONS.items()]
        self.core_payload = {name: ("core:" + name).encode() for name in (
            "php.exe", "php-cgi.exe", "php-win.exe", "phpdbg.exe", "deplister.exe", "php8.dll",
            "libssl.dll", "shared.dll", "phar.phar.bat", "pharcommand.phar", "dev/php8.lib",
            "php8embed.lib", "extras/ssl/legacy.dll", "extras/ssl/openssl.cnf",
            "lib/enchant/libenchant2_hunspell.dll", "php.ini-development", "php.ini-production",
            "README.md", "license.txt", "news.txt", "snapshot.txt", "readme-redist-bins.txt")}
        self.core_payload.update({"ext/php_" + name + ".dll": ("builtin:" + name).encode()
                                  for name in ["imap", "opcache", "oci8_19"] + ["fixture" + str(i) for i in range(37)]})
        self.payloads = {}
        for name, version in VERSIONS.items():
            ref = "v" + version if name == "memcached" else version
            stem = f"php_{name}-{ref}-8.2.35-nts-vs16-x86_64"
            self.payloads[name] = {stem + ".dll": (name + ":php-dll").encode(),
                                   stem + ".pdb": (name + ":php-pdb").encode(),
                                   "README.md": (name + ":readme").encode(),
                                   "LICENSE": (name + ":license").encode(),
                                   "examples/example.php": (name + ":example").encode(),
                                   "upstream.ini": (name + ":upstream-ini").encode()}
        for name in ("libhashkit", "libmemcached", "libmemcachedprotocol", "libmemcachedutil"):
            for suffix in ("dll", "pdb"):
                self.payloads["memcached"][name + "." + suffix] = (name + ":" + suffix).encode()
        for name in ("CORE_RL_magick_", "CORE_RL_wand_", "IM_MOD_RL_png_", "IM_MOD_RL_jpeg_", "FILTER_analyze_"):
            for suffix in ("dll", "pdb"):
                self.payloads["imagick"][name + "." + suffix] = (name + ":" + suffix).encode()
        self.payloads["imagick"].update({"config/config" + str(i) + ".xml": ("config:" + str(i)).encode()
                                          for i in range(10)})
        self.payloads["imagick"]["tests/OFL.txt"] = b"font license"
        self.index = {"schema": "servbay.php-split-assets.v1", "publishable_v2": False,
                      "build_context": {"producer": {"repository": "ServBay/PHP-Windows-Portable",
                                                       "commit": "a" * 40, "run_id": 1234, "run_attempt": 1},
                                        "display_version": "8.2.35", "build_version": "8.2.35",
                                        "source_ref": "8.2.35", "build_branch": "", "arch": "x64",
                                        "thread_safe": "nts", "core_runtime_filename": "php-8.2.35-nts-Win32-vs16-x64.zip"},
                      "selected_extensions": [{"name": n, "ref": "v" + v if n == "memcached" else v}
                                              for n, v in VERSIONS.items()], "assets": []}
        self.add_asset("core-runtime", None, "php-8.2.35-nts-Win32-vs16-x64.zip", self.core_payload)
        for name, payload in self.payloads.items():
            version = VERSIONS[name]
            ref = "v" + version if name == "memcached" else version
            self.add_asset("extension-runtime", name, f"php_{name}-{ref}-8.2.35-nts-vs16-x86_64.zip", payload)
        self.evidence = {"source_sha256": self.index["assets"][0]["sha256"], "version": "8.2.35", "abi": copy.deepcopy(ABI)}

    def add_asset(self, role, extension, original, payload):
        filename = role + "--" + ((extension + "--") if extension else "") + original
        path = self.assets / filename
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in payload.items():
                archive.writestr(name, data)
        row = {"role": role, "original_filename": original, "filename": filename,
               "source_path": original, "source_artifact": "original-split", "bytes": path.stat().st_size,
               "sha256": digest(path.read_bytes()), "members": self.members(path)}
        if extension:
            row["extension"] = extension
        self.index["assets"].append(row)
        return row

    def members(self, path):
        with zipfile.ZipFile(path) as archive:
            return [{"path": info.filename, "type": "directory" if info.is_dir() else "file",
                     "bytes": info.file_size, "sha256": digest(archive.read(info))}
                    for info in archive.infolist()]

    def save(self):
        path = self.assets / "split-assets.json"
        path.write_text(json.dumps(self.index), encoding="utf-8")
        return {"path": path, "sha256": digest(path.read_bytes())}

    def run_repack(self, **changes):
        args = {"split_source": self.save(), "core_identity": self.core,
                "module_identities": self.identities, "core_evidence": self.evidence,
                "output_dir": self.output, "seven_zip": SEVEN_ZIP}
        args.update(changes)
        return self.module.prepare_php_payloads(**args)

    def rejected(self, **changes):
        with self.assertRaises((ValueError, OSError, zipfile.BadZipFile)):
            self.run_repack(**changes)
        self.assertFalse((self.output / "php-repack-manifest.json").exists())
        if self.output.exists():
            self.assertFalse(list(self.output.rglob("meta.json")))
            self.assertFalse(list(self.output.rglob("module-manifest.json")))

    def test_legacy_official_layout_maps_to_the_v2_core_contract(self):
        # The legacy producer uses windows.php.net archives as its base, which
        # carry files the split producer never emits (sasl2/, install.txt,
        # php.gif, the PWS registration samples). They must land in the core
        # package instead of failing the whole build.
        target = self.module._core_target
        self.assertEqual("share/doc/php/install.txt", target("install.txt", EXT))
        self.assertEqual("share/doc/php/php.gif", target("php.gif", EXT))
        self.assertEqual("share/defaults/php/pws-php5cgi.reg", target("pws-php5cgi.reg", EXT))
        self.assertEqual("share/defaults/php/pws-php5isapi.reg", target("pws-php5isapi.reg", EXT))
        self.assertEqual("sasl2/saslLOGIN.dll", target("sasl2/saslLOGIN.dll", EXT))
        self.assertEqual("lib/php5.lib", target("dev/php5.lib", EXT))
        self.assertEqual(EXT + "/php_curl.dll", target("ext/php_curl.dll", EXT))
        with self.assertRaises(ValueError):
            target("unknown/asset.bin", EXT)

    def expected_core(self):
        result = {}
        for path, data in self.core_payload.items():
            if path.startswith("ext/"):
                target = EXT + path[4:]
            elif path.startswith(("extras/ssl/", "lib/enchant/")):
                target = path
            elif path.startswith("dev/") or path.endswith(".lib"):
                target = "lib/" + Path(path).name
            elif path.startswith("php.ini-"):
                target = "share/defaults/php/" + path
            elif path.lower().endswith((".exe", ".dll")) or path in ("phar.phar.bat", "pharcommand.phar"):
                target = "bin/" + path
            else:
                target = "share/doc/php/" + path
            result["php/8.2/" + target] = digest(data)
        return result

    def expected_module(self, name):
        result = {}
        for path, data in self.payloads[name].items():
            if "/" not in path and path.startswith("php_" + name + "-") and path.endswith((".dll", ".pdb")):
                target = EXT + path
            elif "/" not in path and path.endswith((".dll", ".pdb")):
                target = "bin/" + path
            else:
                target = "share/" + name + "/" + path
            result[target] = digest(data)
        return result

    def test_complete_core_and_five_module_archive_bytes_and_audit(self):
        result = self.run_repack()
        audit = result["audit"]
        self.assertFalse(audit["publishable_v2"])
        self.assertEqual(audit["native_validation"], "pending")
        self.assertEqual(audit["source_index"]["sha256"], self.save()["sha256"])
        self.assertEqual(audit["producer"], self.index["build_context"]["producer"])
        self.assertEqual(audit["core"]["source"]["sha256"], self.evidence["source_sha256"])
        core_archive = Path(result["core_archive"])
        self.assertEqual(core_archive.name, "php-8.2.35-windows-x86_64.7z")
        self.assertEqual(audit["host"]["package_sha256"], digest(core_archive.read_bytes()))
        self.assertNotEqual(audit["host"]["package_sha256"], self.evidence["source_sha256"])
        self.assertEqual(audit["host"]["abi"], ABI)
        self.assertEqual(audit["extension_dir"], EXT.rstrip("/"))
        self.assertEqual(self.module.windows_archive.archive_hashes(core_archive, SEVEN_ZIP), self.expected_core())
        self.assertEqual(len(audit["core"]["files"]), len(self.core_payload))
        self.assertEqual({r["source_path"] for r in audit["core"]["files"]}, set(self.core_payload))
        for row in audit["core"]["files"]:
            self.assertEqual((row["sha256"], row["bytes"]), (digest(self.core_payload[row["source_path"]]), len(self.core_payload[row["source_path"]])))
        defaults = audit["configuration_defaults"]
        self.assertEqual(len(defaults), 2)
        self.assertEqual({r["etc_relative_path"] for r in defaults}, {"php/8.2/php.ini"})
        for name, module in audit["modules"].items():
            archive = Path(result["module_archives"][name])
            self.assertEqual(archive.name, f"ext-{name}-{VERSIONS[name]}-php8.2-windows-x86_64.7z")
            self.assertEqual(self.module.windows_archive.archive_hashes(archive, SEVEN_ZIP), self.expected_module(name))
            self.assertEqual(len(module["files"]), len(self.payloads[name]))
            self.assertEqual({r["source_path"] for r in module["files"]}, set(self.payloads[name]))
            self.assertEqual({r["target_path"]: r["sha256"] for r in module["files"]}, self.expected_module(name))
            for row in module["files"]:
                self.assertEqual(row["bytes"], len(self.payloads[name][row["source_path"]]))
            config = module["configuration_candidate"]
            directive = "zend_extension" if name == "xdebug" else "extension"
            dll = next(p for p in self.payloads[name] if p.startswith("php_") and p.endswith(".dll"))
            self.assertEqual(config["content"], directive + "=" + dll + "\n")
            self.assertEqual(config["etc_relative_path"], "php/8.2/conf.d/" + name + ".ini")
            self.assertFalse(config["automatically_enabled"])
            self.assertEqual(config["sha256"], digest(config["content"].encode()))
        self.assertEqual(json.loads(Path(result["manifest"]).read_text()), audit)
        self.assertNotIn(str(self.base), json.dumps(audit))
        self.assertFalse(list(self.output.rglob("meta.json")))
        self.assertFalse(list(self.output.rglob("module-manifest.json")))

    def test_index_hash_is_pinned_before_parse(self):
        source = self.save()
        source["sha256"] = "0" * 64
        self.rejected(split_source=source)

    def test_asset_and_member_hash_and_size_tampering(self):
        original = copy.deepcopy(self.index)
        for asset in (0, 1):
            for field in ("sha256", "bytes"):
                with self.subTest(asset=asset, field=field):
                    self.index = copy.deepcopy(original)
                    self.index["assets"][asset][field] = "0" * 64 if field == "sha256" else 1
                    self.rejected()
            for field, value in (("sha256", "1" * 64), ("bytes", 1), ("type", "directory"), ("path", "wrong.dll")):
                with self.subTest(asset=asset, member=field):
                    self.index = copy.deepcopy(original)
                    self.index["assets"][asset]["members"][0][field] = value
                    self.rejected()

    def test_index_context_and_asset_roles_and_filename_identity_rejected(self):
        original = copy.deepcopy(self.index)
        for field, value in (("arch", "x86"), ("thread_safe", "ts"), ("display_version", "8.2.34"), ("build_version", "8.3.35")):
            with self.subTest(context=field):
                self.index = copy.deepcopy(original)
                self.index["build_context"][field] = value
                self.rejected()
        for asset, field, value in ((0, "role", "merged-runtime"), (1, "role", "logs"),
                                     (0, "original_filename", "php-devel-pack-8.2.35-nts-Win32-vs16-x64.zip"),
                                     (1, "original_filename", "php_redis-6.3.0-8.2.35-ts-vs16-x86_64.zip"),
                                     (1, "extension", "wrong")):
            with self.subTest(asset=asset, field=field):
                self.index = copy.deepcopy(original)
                self.index["assets"][asset][field] = value
                self.rejected()

    def test_wrong_core_evidence_and_module_host_rejected(self):
        original = copy.deepcopy(self.evidence)
        for field, value in (("source_sha256", "1" * 64), ("version", "8.2.34"), ("abi", {"module_api": 20220829})):
            evidence = {**original, field: value}
            with self.subTest(field=field):
                self.rejected(core_evidence=evidence)
        for field, value in (("module_api", 20230831), ("zend_extension_api", 420230831),
                             ("extension_build", "wrong"), ("zend_extension_build", "wrong"),
                             ("zts", True), ("debug", True)):
            identities = copy.deepcopy(self.identities)
            identities[0]["host"]["abi"][field] = value
            with self.subTest(abi=field):
                self.rejected(module_identities=identities)
        evidence = copy.deepcopy(original)
        evidence["abi"]["zts"] = True
        self.rejected(core_evidence=evidence)

    def test_duplicate_and_unsupported_module_and_identity_rejected(self):
        self.rejected(module_identities=self.identities + [self.identities[0]])
        identities = copy.deepcopy(self.identities)
        identities[0]["name"] = "grpc"
        self.rejected(module_identities=identities)
        for field, value in (("kind", "package"), ("arch", "arm64"), ("version", "6.2.0")):
            identities = copy.deepcopy(self.identities)
            identities[0][field] = value
            self.rejected(module_identities=identities)
        for field, value in (("name", "python"), ("kind", "php-extension"), ("build_revision", -1)):
            self.rejected(core_identity={**self.core, field: value})

    def rewrite_zip(self, entries, asset=0):
        row = self.index["assets"][asset]
        path = self.assets / row["filename"]
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries:
                archive.writestr(name, content)
        row.update(bytes=path.stat().st_size, sha256=digest(path.read_bytes()), members=self.members(path))
        if asset == 0:
            self.evidence["source_sha256"] = row["sha256"]
        return path

    def test_unsafe_zip_namespace_rejected(self):
        original = copy.deepcopy(self.index)
        for extra in ("../escape.dll", "/escape.dll", "C:/x.dll", "x:ads", "CON.dll", "bad./x", "bad\\x"):
            with self.subTest(path=extra):
                self.index = copy.deepcopy(original)
                self.rewrite_zip([*self.core_payload.items(), (extra, b"unsafe")])
                self.rejected()
        for extra in ("PHP.EXE", "php.exe/child", "ext/PHP_imap.dll", "ext", "ext/", "php.exe"):
            with self.subTest(collision=extra):
                self.index = copy.deepcopy(original)
                self.rewrite_zip([*self.core_payload.items(), (extra, b"conflict")])
                self.rejected()

    def test_links_special_files_and_encryption_rejected(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600):
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = mode << 16
            self.rewrite_zip([*self.core_payload.items(), (info, b"php.exe")])
            self.rejected()
        path = self.rewrite_zip(list(self.core_payload.items()))
        data = bytearray(path.read_bytes())
        for magic, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            pos = data.find(magic)
            self.assertGreaterEqual(pos, 0)
            flags = struct.unpack_from("<H", data, pos + offset)[0]
            struct.pack_into("<H", data, pos + offset, flags | 1)
        path.write_bytes(data)
        self.index["assets"][0].update(bytes=len(data), sha256=digest(data))
        self.evidence["source_sha256"] = digest(data)
        self.rejected()

    def test_corrupt_crc_rejected_even_with_recomputed_zip_sha(self):
        path = self.assets / self.index["assets"][0]["filename"]
        data = bytearray(path.read_bytes())
        marker = data.find(b"core:php.exe")
        self.assertGreaterEqual(marker, 0)
        data[marker] ^= 1
        path.write_bytes(data)
        self.index["assets"][0]["sha256"] = digest(data)
        self.evidence["source_sha256"] = digest(data)
        self.rejected()

    def test_encrypted_local_header_cannot_hide_behind_clear_central_flags(self):
        path = self.assets / self.index["assets"][0]["filename"]
        data = bytearray(path.read_bytes())
        self.assertEqual(data[:4], b"PK\x03\x04")
        struct.pack_into("<H", data, 6, struct.unpack_from("<H", data, 6)[0] | 1)
        path.write_bytes(data)
        self.index["assets"][0]["sha256"] = digest(data)
        self.evidence["source_sha256"] = digest(data)
        self.rejected()

    def test_unknown_core_structure_and_module_binary_rejected(self):
        self.rewrite_zip([*self.core_payload.items(), ("unknown/hidden.dll", b"unknown")])
        self.rejected()
        self.rewrite_zip(list(self.core_payload.items()))
        self.rewrite_zip([*self.payloads["redis"].items(), ("nested/hidden.dll", b"unknown")], 1)
        self.rejected()

    def test_existing_or_input_nested_output_rejected(self):
        self.output.mkdir()
        (self.output / "keep").write_bytes(b"existing")
        self.rejected()
        self.assertEqual((self.output / "keep").read_bytes(), b"existing")
        self.rejected(output_dir=self.assets / "new-output")

    def test_shared_identical_dependencies_keep_every_owner(self):
        for asset, name in ((1, "redis"), (2, "memcache")):
            self.payloads[name]["shared.dll"] = self.core_payload["shared.dll"]
            self.rewrite_zip(list(self.payloads[name].items()), asset)
        result = self.run_repack()
        shared = next(r for r in result["audit"]["shared_targets"] if r["target_path"] == "bin/shared.dll")
        self.assertEqual({r["owner"] for r in shared["sources"]}, {"core", "redis", "memcache"})
        self.assertEqual(shared["sha256"], digest(self.core_payload["shared.dll"]))
        for name in ("redis", "memcache"):
            self.assertEqual(self.module.windows_archive.archive_hashes(result["module_archives"][name], SEVEN_ZIP), self.expected_module(name))

    def test_core_and_cross_module_dependency_conflicts_rejected(self):
        self.rewrite_zip([*self.payloads["redis"].items(), ("shared.dll", b"different")], 1)
        self.rejected()
        self.rewrite_zip([*self.payloads["redis"].items(), ("conflict.dll", b"one")], 1)
        self.rewrite_zip([*self.payloads["memcache"].items(), ("conflict.dll", b"two")], 2)
        self.rejected()

    def test_input_changes_after_freeze_cannot_change_output(self):
        real_create = self.module.windows_archive.create_archive
        changed = []
        def create(*args, **kwargs):
            if not changed:
                (self.assets / self.index["assets"][1]["filename"]).write_bytes(b"changed after freezing")
                changed.append(True)
            return real_create(*args, **kwargs)
        with patch.object(self.module.windows_archive, "create_archive", side_effect=create):
            result = self.run_repack()
        self.assertTrue(changed)
        self.assertEqual(self.module.windows_archive.archive_hashes(result["module_archives"]["redis"], SEVEN_ZIP), self.expected_module("redis"))

    def test_archive_readback_mismatch_never_publishes_audit(self):
        with patch.object(self.module.windows_archive, "extract_verified_archive", return_value={}):
            self.rejected()

    def test_native_evidence_finalizes_core_and_every_selected_module(self):
        prepared = self.run_repack()
        audit = prepared["audit"]
        modules = []
        for name, module in sorted(audit["modules"].items()):
            dlls = [row["target_path"] for row in module["files"]
                    if row["target_path"].startswith(audit["extension_dir"] + "/")
                    and row["target_path"].casefold().endswith(".dll")]
            modules.append({"name": name, "dll": Path(dlls[0]).name, "exit_code": 0, "loaded": True})
        native = self.base / "native-results.json"
        native.write_text(json.dumps({
            "php": self.core["version"], "extension_dir": audit["extension_dir"],
            "loaded": len(modules), "modules": modules, "imagick_png": True,
        }), encoding="utf-8")
        output = self.base / "formal-output"
        result = self.module.finalize_php_payloads(
            self.output, self.save(), native, list(VERSIONS), "10.0",
            {"distribution": "windows-x64", "toolchain": "msvc-vs16", "crt": "vc14-runtime"},
            output, core_source_url="https://windows.php.net/downloads/releases/",
            module_source_url="https://github.com/ServBay/PHP-Windows-Portable",
            seven_zip=SEVEN_ZIP,
        )
        core_meta = json.loads((output / "core/meta.json").read_text())
        self.assertEqual(len(core_meta["package_manifest"]["default_modules"]), len(VERSIONS))
        self.assertEqual(set(result["modules"]), set(VERSIONS))
        for name in VERSIONS:
            meta = json.loads((output / name / "meta.json").read_text())
            self.assertEqual(meta["host"]["abi"], ABI)
            self.assertNotIn("base", meta)


if __name__ == "__main__":
    unittest.main()
