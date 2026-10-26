"""Offline orchestration checks; Windows process boundaries are mocked explicitly."""

import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-windows/ci/php_v2_pipeline.py"
INFO = """PHP Version => 8.6.0RC4
Architecture => x64
PHP API => 20260924
Zend Extension => 420260924
PHP Extension Build => API20260924,NTS,VS18
Zend Extension Build => API420260924,NTS,VS18
Thread Safety => disabled
Debug Build => no
"""


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("php_pipeline_tests", SCRIPT)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_actual_abi_and_version_are_parsed_and_bad_core_is_rejected(self):
        abi = self.m.parse_abi(INFO)
        self.assertEqual((abi["module_api"], abi["extension_build"], abi["zts"]),
                         (20260924, "API20260924,NTS,VS18", False))
        self.assertEqual(self.m.parse_abi(INFO + "PHP Version => 8.6.0RC4\n"), abi)
        with self.assertRaises(ValueError):
            self.m.parse_abi(INFO + "PHP Version => 8.6.0beta3\n")
        for bad in (INFO.replace("8.6.0RC4", "8.6.0beta3"), INFO.replace("NTS,VS18", "TS,VS18"),
                    INFO.replace("disabled", "enabled"), INFO.replace("VS18", "VS17"),
                    INFO.replace("Debug Build => no", "Debug Build => yes"), INFO.replace("x64", "x86"),
                    INFO + "PHP API => 1\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.m.parse_abi(bad)

    def test_refs_and_explicit_revisions_preserve_real_values(self):
        selected = [{"name": "msgpack", "ref": "msgpack-3.0.0"},
                    {"name": "memcached", "ref": "v3.4.0"}]
        core, modules = self.m.identities(selected, self.m.parse_abi(INFO), 1)
        self.assertEqual(core["build_revision"], 0)
        self.assertEqual([(r["version"], r["build_revision"]) for r in modules],
                         [("msgpack-3.0.0", 1), ("3.4.0", 1)])
        for revision in (None, 0, -1, True, "1"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.m.identities(selected, self.m.parse_abi(INFO), revision)

    def test_exact_upstream_identity_and_complete_original_artifacts(self):
        run = {"id": self.m.RUN_ID, "run_attempt": 1, "head_sha": self.m.PRODUCER_COMMIT,
               "repository": {"full_name": self.m.REPOSITORY}, "status": "completed",
               "conclusion": "success", "path": ".github/workflows/build.yml",
               "event": "workflow_dispatch"}
        selected = [{"name": "xdebug"}]
        artifacts = [{"id": i + 1, "name": name, "expired": False, "size_in_bytes": 100,
                      "workflow_run": {"id": self.m.RUN_ID, "head_sha": self.m.PRODUCER_COMMIT}}
                     for i, name in enumerate(self.m.artifact_names(selected))]
        self.assertEqual(self.m.validate_upstream(run, artifacts, selected), self.m.artifact_names(selected))
        for key, value in (("run_attempt", 2), ("head_sha", "a" * 40), ("conclusion", "failure"),
                           ("id", 5), ("path", "other.yml")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.m.validate_upstream({**run, key: value}, artifacts, selected)
        for bad in (artifacts[:-1], artifacts + [artifacts[0]],
                    [{**artifacts[0], "expired": True}] + artifacts[1:]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.m.validate_upstream(run, bad, selected)

    def test_native_load_checks_values_stderr_and_xdebug_directive(self):
        audit = {"extension_dir": "lib/php/extensions/no-debug-non-zts-20260924",
                 "core": {"identity": {"version": self.m.VERSION}},
                 "modules": {"xdebug": {"files": [{"target_path":
                     "lib/php/extensions/no-debug-non-zts-20260924/php_xdebug.dll"}]}}}
        php = self.base / "bin/php.exe"
        ok = json.dumps({"name": "xdebug", "loaded": True, "version": "3.5.0"})
        with patch.object(self.m, "php_run", side_effect=[INFO, ok]) as command:
            result = self.m.native_test(self.base, audit)
        self.assertTrue(result["modules"][0]["loaded"])
        self.assertIn("zend_extension=php_xdebug.dll", command.call_args.args[1])
        for output in (ok.replace("true", "false"), 'prefix ' + ok, '{}'):
            with self.subTest(output=output), \
                    patch.object(self.m, "php_run", side_effect=[INFO, output]), self.assertRaises(ValueError):
                self.m.native_test(self.base, audit)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "PHP Warning")), \
                self.assertRaises(RuntimeError):
            self.m.php_run(php, ["-n"])

    def test_png_query_failure_is_not_success(self):
        ext = "lib/php/extensions/no-debug-non-zts-20260924"
        audit = {"extension_dir": ext, "core": {"identity": {"version": self.m.VERSION}},
                 "modules": {"imagick": {"files": [{"target_path": ext + "/php_imagick.dll"},
                                                      {"target_path": "bin/IM_MOD_RL_png_.dll"}]}}}
        loaded = json.dumps({"name": "imagick", "loaded": True, "version": "3.8.1"})
        with patch.object(self.m, "php_run", side_effect=[INFO, loaded, "false"]), self.assertRaises(ValueError):
            self.m.native_test(self.base, audit)
        with patch.object(self.m, "php_run", side_effect=[INFO, loaded, "true"]):
            self.assertTrue(self.m.native_test(self.base, audit)["imagick_png"])

    def test_non_native_windows_is_rejected(self):
        for system, process, native in (("Darwin", 0, 0x8664), ("Windows", 0x8664, 0xAA64),
                                        ("Windows", 0, 0xAA64)):
            with self.subTest(system=system, native=native), \
                    patch.object(self.m.platform, "system", return_value=system), \
                    patch.object(self.m, "windows_machines", return_value=(process, native)), self.assertRaises(ValueError):
                self.m.require_native_x64()
        with patch.object(self.m.platform, "system", return_value="Windows"), \
                patch.object(self.m, "windows_machines", return_value=(0, 0x8664)):
            self.assertEqual(self.m.require_native_x64()["native_machine"], "AMD64")

    def test_existing_output_is_preserved_and_failure_never_exports(self):
        target = self.base / "existing"
        target.mkdir()
        sentinel = target / "keep"
        sentinel.write_text("keep")
        with self.assertRaises(ValueError):
            self.m.export_verified(target, lambda work: None)
        self.assertEqual(sentinel.read_text(), "keep")
        target = self.base / "output"
        def fail(work):
            (work / "meta.json").write_text('{"publishable":true}')
            raise ValueError("formal archive native load failed")
        with self.assertRaises(ValueError):
            self.m.export_verified(target, fail)
        self.assertFalse(target.exists())
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["existing"])

    def split_fixture(self):
        split = self.base / "split"
        split.mkdir()
        versions = {"xdebug": "3.5.0", "redis": "6.3.0", "imagick": "3.8.1", "memcache": "8.2",
                    "memcached": "v3.4.0", "imap": "1.0.3", "apcu": "v5.1.28", "mongodb": "2.2.1",
                    "sqlsrv": "5.12.0", "pdo_sqlsrv": "5.12.0", "yaml": "2.3.0", "protobuf": "4.29.6",
                    "grpc": "1.78.0", "igbinary": "3.2.16", "msgpack": "msgpack-3.0.0", "oauth": "2.0.10"}
        selected = [{"name": name, "ref": ref} for name, ref in versions.items()]
        context = {"producer": {"repository": self.m.REPOSITORY, "commit": self.m.PRODUCER_COMMIT,
                                "run_id": self.m.RUN_ID, "run_attempt": 1},
                   "display_version": self.m.VERSION, "build_version": self.m.VERSION,
                   "source_ref": self.m.VERSION, "build_branch": "", "arch": "x64", "thread_safe": "nts",
                   "core_runtime_filename": f"php-{self.m.VERSION}-nts-Win32-vs18-x64.zip"}
        index = {"schema": "servbay.php-split-assets.v1", "publishable_v2": False,
                 "build_context": context, "selected_extensions": selected, "assets": []}
        def add(role, name, original, files):
            filename = role + "--" + (name + "--" if name else "") + original
            path = split / filename
            with zipfile.ZipFile(path, "w") as archive:
                for relative, data in files.items():
                    archive.writestr(relative, data)
            row = {"role": role, "filename": filename, "original_filename": original,
                   "bytes": path.stat().st_size, "sha256": self.m.sha(path),
                   "source_artifact": "original", "source_path": original,
                   "members": [{"path": relative, "type": "file", "bytes": len(data),
                                "sha256": hashlib.sha256(data).hexdigest()} for relative, data in files.items()]}
            if name:
                row["extension"] = name
            index["assets"].append(row)
        add("core-runtime", None, context["core_runtime_filename"],
            {name: name.encode() for name in ("php.exe", "php-cgi.exe", "php-win.exe", "php8.dll",
                                              "php.ini-development", "php.ini-production")})
        for name, ref in versions.items():
            stem = f"php_{name}-{ref}-{self.m.VERSION}-nts-vs18-x86_64"
            files = {stem + ".dll": name.encode()}
            if name == "imagick":
                files["IM_MOD_RL_png_.dll"] = b"coder"
            add("extension-runtime", name, stem + ".zip", files)
        (split / "split-assets.json").write_text(json.dumps(index))
        return split, index, selected

    def test_real_repacker_and_archives_export_all_modules_only_after_formal_native_pass(self):
        seven = shutil.which("7zz") or shutil.which("7z")
        self.assertTrue(seven, "Installed 7z is required; this test never downloads tools")
        repacker = self.m.load("php_rc4_real_repacker_test", ROOT / "runtime-windows/repack_php_v2.py")
        split, index, selected = self.split_fixture()
        calls = []
        fail_at = [None]
        def native_process(php, args):
            calls.append((php, args))
            if len(calls) == fail_at[0]:
                raise RuntimeError("injected native process failure")
            if args[-1] == "-i":
                return INFO
            if "queryFormats" in args[-1]:
                return "true"
            code = args[-1]
            name = next(row["name"] for row in selected if '"name"=>"' + row["name"] + '"' in code)
            return json.dumps({"name": name, "loaded": True, "version": "native-fixture"})
        def build(work):
            self.m.build_payloads(repacker, split, index, selected, work, 1, seven,
                                  {"native_machine": "AMD64"}, {"artifacts": []})
        output = self.base / "result"
        with patch.object(self.m, "php_run", side_effect=native_process):
            self.m.export_verified(output, build)
        self.assertEqual(len(calls), 37)
        evidence = json.loads((output / "native-evidence.json").read_text())
        self.assertEqual(evidence["formal_native"]["loaded"], 16)
        self.assertTrue(evidence["formal_native"]["imagick_png"])
        self.assertEqual(evidence["formal_zip_sha256"], self.m.sha(output / "formal.zip"))
        with zipfile.ZipFile(output / "formal.zip") as archive:
            core = json.loads(archive.read("core/meta.json"))
            msgpack = json.loads(archive.read("msgpack/meta.json"))
            self.assertEqual((core["version"], core["build_revision"]), ("8.6.0RC4", 0))
            self.assertEqual((msgpack["version"], msgpack["build_revision"]), ("msgpack-3.0.0", 1))
            self.assertEqual(sum(name.endswith("/meta.json") for name in archive.namelist()), 17)
        for position in (1, 3, 19, 21, 37):
            with self.subTest(failed_native_call=position):
                calls.clear()
                fail_at[0] = position
                failed_output = self.base / ("failed-" + str(position))
                with patch.object(self.m, "php_run", side_effect=native_process), self.assertRaises(RuntimeError):
                    self.m.export_verified(failed_output, build)
                self.assertFalse(failed_output.exists())
                self.assertFalse(any(self.base.glob("servbay-php-rc4-*")))


if __name__ == "__main__":
    unittest.main()
