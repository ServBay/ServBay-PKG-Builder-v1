"""Offline orchestration checks; Windows process boundaries are mocked explicitly."""

import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile
import yaml


HANDOFF_PRODUCER = {"commit": "a" * 40, "run_id": "987654321", "run_attempt": 2}


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

    def test_workflow_hands_off_17_units_from_one_successful_native_job(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/build-windows-v2-php.yml").read_text())
        self.assertEqual(len(workflow["jobs"]), 1)
        steps = workflow["jobs"]["php-rc4"]["steps"]
        uploads = [step for step in steps if step.get("uses") == "actions/upload-artifact@v4"]
        self.assertEqual(len(uploads), 18)
        pipeline = next(step for step in steps if "php_v2_pipeline.py" in step.get("run", ""))
        self.assertEqual(pipeline["id"], "php")
        self.assertIn("--handoff-producer", pipeline["run"])
        for key in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_OUTPUT"):
            self.assertIn(key, pipeline["run"])
        self.assertLess(pipeline["run"].index("if ($LASTEXITCODE"), pipeline["run"].index("handoff-outputs.txt"))
        for slot, upload in enumerate(uploads[:17]):
            key = "core" if slot == 0 else f"module_{slot:02d}"
            name = "${{ steps.php.outputs." + key + " }}"
            self.assertEqual(upload["with"]["name"], name)
            self.assertEqual(upload["with"]["path"], "php-v2-output/" + name + "/*")
            self.assertEqual(upload["with"]["if-no-files-found"], "error")
            self.assertNotIn("if", upload)
        diagnostics = uploads[-1]["with"]
        self.assertFalse(diagnostics["name"].startswith("pkg2-windows-"))
        self.assertEqual(diagnostics["path"].splitlines(),
                         ["php-v2-output/formal.zip", "php-v2-output/native-evidence.json", "php-v2-output/split-assets.json"])

    def test_workflow_requires_reviewed_explicit_core_revision(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/build-windows-v2-php.yml").read_text())
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertIn("core_revision", inputs)
        self.assertIs(inputs["core_revision"]["required"], True)
        self.assertNotIn("default", inputs["core_revision"])
        self.assertIn("API-reviewed", inputs["core_revision"]["description"])
        steps = workflow["jobs"]["php-rc4"]["steps"]
        pipeline = next(step for step in steps if step.get("id") == "php")
        self.assertEqual(pipeline["env"]["CORE_REVISION"], "${{ inputs.core_revision }}")
        self.assertIn('--core-revision "$env:CORE_REVISION"', pipeline["run"])
        self.assertEqual(workflow["jobs"]["php-rc4"]["runs-on"], "windows-2025-vs2026")
        self.assertEqual(workflow["jobs"]["php-rc4"]["timeout-minutes"], 60)

    def test_core_revision_one_with_module_revision_two_keeps_bound_archives_and_default_refs(self):
        seven = shutil.which("7zz") or shutil.which("7z")
        repacker = self.m.load("php_revision_repacker_test", ROOT / "runtime-windows/repack_php_v2.py")
        split, index, selected = self.split_fixture()
        def native(php, args):
            if args[-1] == "-i":
                return INFO
            if "queryFormats" in args[-1]:
                return "true"
            name = next(row["name"] for row in selected if '"name"=>"' + row["name"] + '"' in args[-1])
            return json.dumps({"name": name, "loaded": True, "version": "fixture"})
        portable = self.base / "portable"
        source = portable / "scripts/collect_split_assets.py"
        source.parent.mkdir(parents=True)
        source.write_text("# synthetic collector boundary\n")
        producer = self.base / "producer.json"
        producer.write_text(json.dumps(HANDOFF_PRODUCER))
        output = self.base / "revised"
        def collect(core, extensions, requests, context, destination):
            self.assertEqual(requests, selected)
            self.assertEqual(context["producer"], index["build_context"]["producer"])
            shutil.copytree(split, destination)
            return index
        collector = SimpleNamespace(_validate_inputs=lambda context, requests: None, collect_split_assets=collect)
        real_load = self.m.load
        def load(name, path):
            return collector if name == "php_rc4_collector" else real_load(name, path)
        def git(command):
            return json.dumps(selected) if command[-1].endswith(":extensions.php8.6.json") else source.read_text()
        args = ["php_v2_pipeline.py", "--portable-source", str(portable), "--producer-source", str(self.base),
                "--handoff-producer", str(producer), "--core-revision", "1", "--module-revision", "2",
                "--seven-zip", seven, "--output-dir", str(output)]
        with patch("sys.argv", args), patch("sys.stdout", new_callable=io.StringIO), \
                patch.object(self.m, "require_native_x64", return_value={"native_machine": "AMD64"}), \
                patch.object(self.m, "run", side_effect=git), patch.object(self.m, "load", side_effect=load), \
                patch.object(self.m, "upstream_inputs", return_value={"artifacts": []}), \
                patch.object(self.m, "php_run", side_effect=native):
            self.m.main()
        units = sorted(output.glob("pkg2-windows-*/meta.json"))
        self.assertEqual(len(units), 17)
        metas = [json.loads(path.read_text()) for path in units]
        core = next(meta for meta in metas if meta["kind"] == "package")
        self.assertEqual(core["build_revision"], 1)
        self.assertIn("pkg2-windows-x86_64-package-php-8.6.0RC4-r1", {path.parent.name for path in units})
        refs = core["package_manifest"]["default_modules"]
        self.assertEqual(len(refs), 16)
        self.assertEqual({ref["build_revision"] for ref in refs}, {2})
        by_name = {meta["name"]: meta for meta in metas if meta["kind"] == "php-extension"}
        self.assertEqual(set(by_name), {ref["name"] for ref in refs})
        for ref in refs:
            meta = by_name[ref["name"]]
            self.assertEqual(meta["build_revision"], 2)
            self.assertEqual(ref["sha256"], meta["sha256"])
            self.assertEqual(ref["host"], meta["host"])
        for path in units:
            meta = json.loads(path.read_text())
            self.assertEqual(path.parent.name, repacker.package_meta.artifact_name(meta))
            self.assertEqual(self.m.sha(path.parent / meta["filename"]), meta["sha256"])
            if meta["kind"] != "package":
                manifest = json.loads((path.parent / meta["manifest_filename"]).read_text())
                self.assertEqual(manifest["host"]["package_sha256"], core["sha256"])
        self.assertEqual(json.loads((output / "split-assets.json").read_text()), index)
        self.assertEqual(core["source"]["sha256"], next(row["sha256"] for row in index["assets"]
                                                       if row["role"] == "core-runtime"))
        self.assertEqual(core["producer"], HANDOFF_PRODUCER)
        receipt = json.loads((output / "native-evidence.json").read_text())
        self.assertEqual(receipt["producer"], index["build_context"]["producer"])
        self.assertEqual(receipt["split_index_sha256"], self.m.sha(split / "split-assets.json"))
        self.assertEqual(receipt["core_identity"]["build_revision"], 1)
        self.assertEqual({row["build_revision"] for row in receipt["module_identities"]}, {2})
        with zipfile.ZipFile(output / "formal.zip") as archive:
            self.assertEqual(json.loads(archive.read("core/meta.json"))["build_revision"], 1)
            for row in selected:
                self.assertEqual(json.loads(archive.read(row["name"] + "/meta.json"))["build_revision"], 2)

    def test_revision_validation_rejects_missing_invalid_boolean_or_overflow_before_processing(self):
        abi = self.m.parse_abi(INFO)
        for core_revision, module_revision in ((0, 1), (1, 2), (2147483647, 2147483647)):
            core, modules = self.m.identities([{"name": "redis", "ref": "v6.3.0"}], abi,
                                              module_revision, core_revision=core_revision)
            self.assertEqual(core["build_revision"], core_revision)
            self.assertEqual(modules[0]["build_revision"], module_revision)
        producer = self.base / "producer.json"
        producer.write_text(json.dumps(HANDOFF_PRODUCER))
        repacker = self.m.load("php_invalid_revision_repacker", ROOT / "runtime-windows/repack_php_v2.py")
        selected = [{"name": str(index), "ref": "1"} for index in range(16)]
        output = self.base / "unused-output"
        with self.assertRaises(TypeError):
            self.m.identities([], {}, 2)
        with self.assertRaises(TypeError):
            self.m.pipeline(self.base, self.base, output, 2, "unused", handoff_producer_file=producer)
        with self.assertRaises(TypeError):
            self.m.build_payloads(repacker, self.base, {}, selected, self.base, 2, "unused", {}, {},
                                  handoff_producer=HANDOFF_PRODUCER)
        for field in ("core", "module"):
            invalid = (None, -1, True, False, "1", 1.5, 2147483648)
            if field == "module":
                invalid += (0,)
            for value in invalid:
                core_revision, module_revision = (value, 2) if field == "core" else (1, value)
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.m.identities([], {}, module_revision, core_revision=core_revision)
                    with patch.object(self.m, "require_native_x64") as machine, \
                            patch.object(self.m, "run") as upstream, self.assertRaises(ValueError):
                        self.m.pipeline(self.base, self.base, output, module_revision, "unused",
                                        handoff_producer_file=producer, core_revision=core_revision)
                    machine.assert_not_called()
                    upstream.assert_not_called()
                    with patch.object(self.m, "php_run") as native, self.assertRaises(ValueError):
                        self.m.build_payloads(repacker, self.base, {}, selected, self.base, module_revision,
                                              "unused", {}, {}, handoff_producer=HANDOFF_PRODUCER,
                                              core_revision=core_revision)
                    native.assert_not_called()
                    self.assertFalse(output.exists())

    def test_cli_requires_core_revision_and_rejects_noninteger_values(self):
        base = ["php_v2_pipeline.py", "--portable-source", "unused", "--producer-source", "unused",
                "--handoff-producer", "unused", "--module-revision", "2", "--seven-zip", "unused",
                "--output-dir", "unused"]
        for extra in ([], ["--core-revision", "true"], ["--core-revision", "1.5"]):
            with self.subTest(extra=extra), patch("sys.argv", base + extra), \
                    patch("sys.stderr", new_callable=io.StringIO), patch.object(self.m, "pipeline") as pipeline, \
                    self.assertRaises(SystemExit) as result:
                self.m.main()
            self.assertEqual(result.exception.code, 2)
            pipeline.assert_not_called()

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
        core, modules = self.m.identities(selected, self.m.parse_abi(INFO), 1, core_revision=0)
        self.assertEqual(core["build_revision"], 0)
        self.assertEqual([(r["version"], r["build_revision"]) for r in modules],
                         [("msgpack-3.0.0", 1), ("3.4.0", 1)])
        for revision in (None, 0, -1, True, "1"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.m.identities(selected, self.m.parse_abi(INFO), revision, core_revision=0)

    def test_php_subprocess_receives_only_explicit_runtime_environment(self):
        sentinel = "synthetic-secret-never-forward-to-php"
        environment = {"SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows",
                       "TEMP": "C:\\Temp", "TMP": "C:\\Temp", "PATH": "C:\\Windows\\System32",
                       "COMSPEC": "C:\\Windows\\System32\\cmd.exe", "PATHEXT": ".EXE;.BAT",
                       "GH_TOKEN": sentinel, "GITHUB_TOKEN": sentinel,
                       "ACTIONS_RUNTIME_TOKEN": sentinel, "UNRECOGNIZED_CREDENTIAL": sentinel}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as process:
            self.assertEqual(self.m.php_run(self.base / "bin/php.exe", ["-n", "-i"]), "ok")
        forwarded = process.call_args.kwargs["env"]
        self.assertFalse(any(sentinel in value for value in forwarded.values()))
        self.assertEqual({key.upper() for key in forwarded},
                         {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC", "PATHEXT",
                          "PHPRC", "PHP_INI_SCAN_DIR"})
        self.assertEqual(forwarded["PATH"], str(self.base / "bin") + os.pathsep + environment["PATH"])
        self.assertEqual(forwarded["PHPRC"], "")
        self.assertEqual(forwarded["PHP_INI_SCAN_DIR"], "")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, sentinel, sentinel)), \
                self.assertRaises(RuntimeError) as failed:
            self.m.php_run(self.base / "bin/php.exe", ["-n", "-i"])
        self.assertNotIn(sentinel, str(failed.exception))

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

    def test_diagnostic_bundle_without_complete_units_cannot_be_exposed(self):
        target = self.base / "output"
        def diagnostics_only(work):
            exported = work / "export"
            exported.mkdir()
            (exported / "formal.zip").write_bytes(b"diagnostic")
            (exported / "native-evidence.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.m.export_verified(target, diagnostics_only)
        self.assertFalse(target.exists())

    def test_pipeline_cli_requires_explicit_handoff_producer(self):
        args = ["php_v2_pipeline.py", "--portable-source", "unused", "--producer-source", "unused",
                "--output-dir", "unused", "--core-revision", "0", "--module-revision", "1", "--seven-zip", "unused"]
        with patch("sys.argv", args), patch("sys.stderr", new_callable=io.StringIO), \
                patch.object(self.m, "pipeline") as pipeline, self.assertRaises(SystemExit) as result:
            self.m.main()
        self.assertEqual(result.exception.code, 2)
        pipeline.assert_not_called()

    def test_invalid_handoff_producer_fails_before_native_or_upstream_calls(self):
        path = self.base / "producer.json"
        for value in (None, {}, {**HANDOFF_PRODUCER, "commit": "invalid"},
                      {**HANDOFF_PRODUCER, "run_id": 12}, {**HANDOFF_PRODUCER, "run_attempt": True},
                      {**HANDOFF_PRODUCER, "run_attempt": 0}, {**HANDOFF_PRODUCER, "extra": 1}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                with patch.object(self.m, "require_native_x64") as native, \
                        patch.object(self.m, "run") as upstream, self.assertRaises(ValueError):
                    self.m.pipeline(self.base, self.base, self.base / "out", 1, "unused",
                                    handoff_producer_file=path, core_revision=0)
                native.assert_not_called()
                upstream.assert_not_called()
        path.write_text('{"commit":"' + 'a' * 40 + '","run_id":"12","run_attempt":1,"run_attempt":2}')
        with patch.object(self.m, "require_native_x64") as native, self.assertRaises(ValueError):
            self.m.pipeline(self.base, self.base, self.base / "out", 1, "unused", handoff_producer_file=path, core_revision=0)
        native.assert_not_called()

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
        sentinel = "synthetic-phpinfo-secret-never-export"
        def native_process(php, args):
            calls.append((php, args))
            if len(calls) == fail_at[0]:
                raise RuntimeError("injected native process failure")
            if args[-1] == "-i":
                return INFO + "\nEnvironment\nGH_TOKEN => " + sentinel + "\n"
            if "queryFormats" in args[-1]:
                return "true"
            code = args[-1]
            name = next(row["name"] for row in selected if '"name"=>"' + row["name"] + '"' in code)
            return json.dumps({"name": name, "loaded": True, "version": "native-fixture"})
        def build(work):
            self.m.build_payloads(repacker, split, index, selected, work, 1, seven,
                                  {"native_machine": "AMD64"}, {"artifacts": []},
                                  handoff_producer=HANDOFF_PRODUCER, core_revision=0)
        output = self.base / "result"
        with patch.object(self.m, "php_run", side_effect=native_process):
            self.m.export_verified(output, build)
        self.assertEqual(len(calls), 37)
        evidence = json.loads((output / "native-evidence.json").read_text())
        for path in output.rglob("*"):
            if path.is_file():
                self.assertNotIn(sentinel.encode(), path.read_bytes())
        self.assertEqual(evidence["producer"], index["build_context"]["producer"])
        self.assertEqual(json.loads((output / "split-assets.json").read_text())["build_context"]["producer"],
                         index["build_context"]["producer"])
        self.assertEqual(evidence["core_evidence"]["abi"], self.m.parse_abi(INFO))
        self.assertNotIn("raw_core_phpinfo", evidence)
        self.assertEqual(evidence["formal_native"]["loaded"], 16)
        self.assertTrue(evidence["formal_native"]["imagick_png"])
        self.assertEqual(evidence["formal_zip_sha256"], self.m.sha(output / "formal.zip"))
        with zipfile.ZipFile(output / "formal.zip") as archive:
            for name in archive.namelist():
                self.assertNotIn(sentinel.encode(), archive.read(name))
            core = json.loads(archive.read("core/meta.json"))
            msgpack = json.loads(archive.read("msgpack/meta.json"))
            self.assertEqual((core["version"], core["build_revision"]), ("8.6.0RC4", 0))
            self.assertEqual((msgpack["version"], msgpack["build_revision"]), ("msgpack-3.0.0", 1))
            self.assertEqual(sum(name.endswith("/meta.json") for name in archive.namelist()), 17)
            for path in archive.namelist():
                if path.endswith("/meta.json"):
                    self.assertEqual(json.loads(archive.read(path))["producer"], HANDOFF_PRODUCER)
        units = sorted(path for path in output.iterdir() if path.name.startswith("pkg2-windows-"))
        self.assertEqual(len(units), 17)
        for unit in units:
            meta = json.loads((unit / "meta.json").read_text())
            self.assertEqual(unit.name, repacker.package_meta.artifact_name(meta))
            self.assertEqual(meta["producer"], HANDOFF_PRODUCER)
            self.assertEqual(self.m.sha(unit / meta["filename"]), meta["sha256"])
            if meta["kind"] != "package":
                self.assertEqual(self.m.sha(unit / meta["manifest_filename"]), meta["manifest_sha256"])
        outputs = (output / "handoff-outputs.txt").read_text().splitlines()
        self.assertEqual(len(outputs), 17)
        self.assertEqual({line.split("=", 1)[1] for line in outputs}, {unit.name for unit in units})
        core_identity, module_identities = self.m.identities(selected, self.m.parse_abi(INFO), 1, core_revision=0)
        for fault in ("missing-unit", "duplicate-identity", "missing-meta", "missing-archive",
                      "missing-manifest", "changed-manifest", "wrong-producer"):
            with self.subTest(export_fault=fault):
                formal = self.base / ("formal-" + fault)
                formal.mkdir()
                for unit in units:
                    meta = json.loads((unit / "meta.json").read_text())
                    owner = "core" if meta["kind"] == "package" else meta["name"]
                    shutil.copytree(unit, formal / owner)
                target = formal / "apcu"
                meta_path = target / "meta.json"
                meta = json.loads(meta_path.read_text())
                if fault == "missing-unit":
                    target.rename(self.base / ("removed-" + fault))
                elif fault == "duplicate-identity":
                    meta_path.write_bytes((formal / "redis/meta.json").read_bytes())
                elif fault.startswith("missing-"):
                    key = {"missing-meta": "meta.json", "missing-archive": meta["filename"],
                           "missing-manifest": meta["manifest_filename"]}[fault]
                    (target / key).rename(self.base / ("removed-" + fault))
                elif fault == "changed-manifest":
                    (target / meta["manifest_filename"]).write_text("{}")
                else:
                    meta["producer"]["run_attempt"] += 1
                    meta_path.write_text(json.dumps(meta))
                def corrupt_export(work):
                    exported = work / "export"
                    exported.mkdir()
                    self.m.export_units(repacker, formal, exported, core_identity, module_identities, HANDOFF_PRODUCER)
                rejected = self.base / ("rejected-" + fault)
                with self.assertRaises(ValueError):
                    self.m.export_verified(rejected, corrupt_export)
                self.assertFalse(rejected.exists())
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
