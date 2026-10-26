"""Behavior tests for the parameterized Windows PostgreSQL producer."""

import ctypes
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "postgresql_pipeline", ROOT / "runtime-windows/ci/postgresql_v2_pipeline.py")
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)
REAL_REPACKER = pipeline.load_repacker()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="servbay-pg-pipeline-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.producer = {"commit": "a" * 40, "run_id": "123", "run_attempt": 2}

    def test_official_url_and_streamed_source_hash_are_exact(self):
        payload = b"actual source bytes" * 100
        stream = io.BytesIO(payload)
        stream.geturl = lambda: pipeline.source_url("11.22", 1)
        with mock.patch.object(pipeline, "urlopen", return_value=stream) as request:
            result = pipeline.download_source("11.22", 1, self.root / "source.zip")
        self.assertEqual(request.call_args.args[0],
                         "https://get.enterprisedb.com/postgresql/postgresql-11.22-1-windows-x64-binaries.zip")
        self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["bytes"], len(payload))
        self.assertEqual(result["version"], "11.22")
        for version, revision in [("19.beta2", 1), ("9.6", 1), ("11.22", 0), ("11.22/evil", 1)]:
            with self.subTest(version=version, revision=revision), self.assertRaises(ValueError):
                pipeline.source_url(version, revision)

    def test_output_exists_refuses_before_network_and_preserves_bytes(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "keep").write_bytes(b"untouched")
        with mock.patch.object(pipeline, "download_source") as download:
            with self.assertRaises(ValueError):
                pipeline.pipeline("17.11", 5, self.producer, output, "7z")
        download.assert_not_called()
        self.assertEqual((output / "keep").read_bytes(), b"untouched")

    def test_subprocess_whitelist_and_failure_never_disclose_output(self):
        raw = subprocess.CompletedProcess([], 9, "SECRET-from-stdout", "SECRET-from-stderr")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "SECRET", "GITHUB_TOKEN": "SECRET",
                                         "AWS_SECRET_ACCESS_KEY": "SECRET"}), \
                mock.patch.object(pipeline.subprocess, "run", return_value=raw) as run:
            result = pipeline.run(["postgres.exe", "--version"], cwd=self.root)
        self.assertEqual(result.returncode, 9)
        env = run.call_args.kwargs["env"]
        self.assertTrue(run.call_args.kwargs["cwd"] == self.root, "Explicit subprocess cwd must be forwarded")
        self.assertFalse(set(env) & {"GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"})
        with self.assertRaises(RuntimeError) as caught:
            pipeline.require_ok(result, "startup")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_failure_summary_preserves_safe_stage_probe_and_exit(self):
        error = pipeline.ProbeFailure("startup", 7)
        error.pipeline_stage = "native"
        summary = pipeline.failure_summary(error)
        self.assertIn("stage=native", summary)
        self.assertIn("probe=startup", summary)
        self.assertIn("exit=7", summary)
        error = ValueError("SECRET external output")
        error.pipeline_stage = "download"
        summary = pipeline.failure_summary(error)
        self.assertIn("stage=download", summary)
        self.assertIn("ValueError", summary)
        self.assertNotIn("SECRET", summary)

    def error_in_frame(self, filename, error, depth=0):
        source = "def controlled(n):\n    if n: return controlled(n-1)\n    raise failure\ncontrolled(depth)\n"
        namespace = {"failure": error, "depth": depth}
        try:
            exec(compile(source, str(filename), "exec"), namespace)
        except Exception as captured:
            captured.pipeline_stage = "finalize"
            return captured
        self.fail("Controlled exception must actually be raised")

    def test_non_oserror_summary_retains_bounded_safe_project_frames(self):
        for error_type in (ValueError, RuntimeError):
            with self.subTest(error_type=error_type.__name__):
                error = self.error_in_frame(ROOT / "runtime-windows/ci/postgresql_v2_pipeline.py",
                                            error_type("SECRET arbitrary output"), depth=5)
                summary = pipeline.failure_summary(error)
                self.assertIn("stage=finalize error_type=" + error_type.__name__, summary)
                self.assertEqual(summary.count("frame="), 3)
                self.assertRegex(summary, r"frame=runtime-windows/ci/postgresql_v2_pipeline\.py:[1-9][0-9]*")
                self.assertNotIn("SECRET", summary)
                self.assertNotIn(str(ROOT), summary)

    def test_non_oserror_summary_rejects_untrusted_frames_and_numeric_attributes(self):
        for error_type in (ValueError, RuntimeError):
            for filename in (self.root / "SECRET-external.py", ROOT / "tests/SECRET-not-allowed.py",
                             ROOT.parent / (ROOT.name + "-SECRET") / "runtime-windows/repack_postgresql_v2.py"):
                with self.subTest(error_type=error_type.__name__, kind="untrusted_frame"):
                    error = error_type("SECRET arbitrary output")
                    error.errno, error.winerror = True, 17
                    summary = pipeline.failure_summary(self.error_in_frame(filename, error))
                    for forbidden in ("frame=", "errno=", "winerror=", "SECRET", str(ROOT)):
                        self.assertNotIn(forbidden, summary)

    def test_probe_failure_summary_keeps_fixed_probe_exit_without_frames(self):
        error = self.error_in_frame(ROOT / "runtime-windows/ci/postgresql_v2_pipeline.py",
                                    pipeline.ProbeFailure("query", 9), depth=5)
        summary = pipeline.failure_summary(error)
        self.assertIn("stage=finalize probe=query exit=9", summary)
        self.assertNotIn("frame=", summary)

    def test_oserror_summary_retains_numeric_codes_and_safe_project_frame(self):
        error = OSError(errno.EXDEV, "SECRET message", "SECRET source", 17, "SECRET destination")
        error = self.error_in_frame(ROOT / "runtime-windows/repack_postgresql_v2.py", error)
        summary = pipeline.failure_summary(error)
        self.assertIn("errno=" + str(error.errno), summary)
        self.assertIn("winerror=17", summary)
        self.assertRegex(summary, r"frame=runtime-windows/repack_postgresql_v2\.py:[1-9][0-9]*")
        self.assertNotIn("SECRET", summary)
        self.assertNotIn(str(ROOT), summary)

    def test_oserror_summary_rejects_noninteger_codes_and_untrusted_frames(self):
        for filename in (self.root / "SECRET-external.py", ROOT / "tests/SECRET-not-allowed.py",
                         ROOT.parent / (ROOT.name + "-SECRET") / "runtime-windows/repack_postgresql_v2.py"):
            with self.subTest(kind="untrusted_frame"):
                error = OSError("SECRET message")
                error.errno, error.winerror = True, "SECRET code"
                summary = pipeline.failure_summary(self.error_in_frame(filename, error))
                self.assertNotIn("errno=", summary)
                self.assertNotIn("winerror=", summary)
                self.assertNotIn("frame=", summary)
                self.assertNotIn("SECRET", summary)
                self.assertNotIn(str(self.root), summary)

    def test_oserror_summary_bounds_project_frames(self):
        error = self.error_in_frame(ROOT / "runtime-windows/repack_postgresql_v2.py", OSError("SECRET"), depth=5)
        summary = pipeline.failure_summary(error)
        self.assertEqual(summary.count("frame="), 3)
        self.assertNotIn("SECRET", summary)

    def fixture(self):
        root = self.root / "postgresql/17"
        (root / "bin").mkdir(parents=True)
        return root

    def runner(self, root, failures=None, bad=None, cwd_callback=None):
        failures, bad = failures or {}, bad or {}
        calls = []
        def invoke(command, **kwargs):
            command = [str(item) for item in command]
            if cwd_callback:
                cwd_callback(command, kwargs.get("cwd"))
            calls.append(command)
            name, args = Path(command[0]).stem, command[1:]
            key = name
            stdout = ""
            if name == "pg_config":
                flag = args[0][2:]
                values = {"version": "PostgreSQL 17.11", "bindir": str(root / "bin"),
                          "sharedir": str(root / "share"), "pkglibdir": str(root / "lib"),
                          "includedir-server": str(root / "include/server"),
                          "cc": "cl", "configure": "EDB options", "cflags": "/MD", "ldflags": "/DLL"}
                stdout = bad.get(flag, values[flag])
            elif args == ["--version"]:
                key = name + "_version"
                stdout = bad.get(name, f"{name} (PostgreSQL) 17.11")
            elif name == "initdb":
                (root / "data").mkdir()
                (root / "data/PG_VERSION").write_text("17\n")
            elif name == "pg_ctl":
                key = "start" if "start" in args else "stop"
            elif name == "psql":
                key = "query"
                stdout = json.dumps(bad.get("sql", {"server_version": "17.11", "server_encoding": "UTF8",
                                    "data_directory": str(root / "data"),
                                    "compiler": "PostgreSQL 17.11, compiled by Visual C++ build 1937, 64-bit"}))
            return subprocess.CompletedProcess(command, failures.get(key, 0), stdout, "sensitive diagnostics")
        return invoke, calls

    def observe(self, root, **kwargs):
        runner, calls = self.runner(root, kwargs.pop("failures", None), kwargs.pop("bad", None),
                                    kwargs.pop("cwd_callback", None))
        runner = kwargs.pop("runner_override", runner)
        system = {"os_architecture": "X64", "process_architecture": "X64", "processor_name": "actual cpu",
                  "processor_architecture_code": 9, "process_environment_architecture": "AMD64",
                  "os_version": "10.0.26100", "native_machine": 34404, "process_machine": 0}
        system.update(kwargs.pop("machine", {}))
        with mock.patch.object(pipeline, "machine_evidence", return_value=system), \
                mock.patch.object(pipeline, "free_port", return_value=54329), \
                mock.patch.object(pipeline, "cleanup_observation", return_value=kwargs.pop("cleanup", (0, 0))), \
                mock.patch.object(pipeline, "crt_evidence", return_value={"crt": "imports: msvcr120.dll", "files": []}):
            result = pipeline.collect_native(root, {"filename": "core.7z", "sha256": "b" * 64, "bytes": 99},
                                             "17.11", runner=runner)
        return result, calls

    def test_observed_values_are_preserved_and_stop_is_always_attempted(self):
        root = self.fixture()
        (native, sidecar, compatibility), calls = self.observe(root)
        self.assertEqual(native["processor_name"], "actual cpu")
        self.assertEqual(native["pg_config"]["bindir"], str(root / "bin"))
        self.assertEqual(native["database"]["data_directory"], str(root / "data"))
        self.assertEqual(native["database"]["stop_exit"], 0)
        self.assertEqual(compatibility, {"distribution": "windows-x64", "toolchain": "Visual C++ build 1937",
                                         "crt": "imports: msvcr120.dll"})
        self.assertEqual(sidecar["machine"]["os_version"], "10.0.26100")
        self.assertTrue(any("stop" in command for command in calls))

    def test_each_database_failure_and_residual_blocks_native_success(self):
        for kind in ("initdb", "start", "query", "stop", "processes", "listeners"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "postgresql/17"
                (root / "bin").mkdir(parents=True)
                kwargs = {"failures": {kind: 7}} if kind in ("initdb", "start", "query", "stop") else {
                    "cleanup": (1, 0) if kind == "processes" else (0, 1)}
                with self.assertRaises((RuntimeError, ValueError)):
                    self.observe(root, **kwargs)

    def test_version_paths_and_sql_identity_must_match(self):
        cases = [{"version": "PostgreSQL 18.6"}, {"bindir": str(self.root / "wrong")},
                 {"postgres": "postgres (PostgreSQL) 18.6"},
                 {"sql": {"server_version": "18.6", "server_encoding": "UTF8",
                          "data_directory": "<data>", "compiler": "Visual C++ build 1937"}},
                 {"sql": {"server_version": "17.11", "server_encoding": "UTF8",
                          "data_directory": str(self.root / "wrong"), "compiler": "Visual C++ build 1937"}}]
        cases.extend({key: str(self.root / "wrong")} for key in ("sharedir", "pkglibdir", "includedir-server"))
        cases.extend({name: name + " (PostgreSQL) 18.6"} for name in ("initdb", "pg_ctl", "psql"))
        for bad in cases:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "postgresql/17"
                (root / "bin").mkdir(parents=True)
                if bad.get("sql", {}).get("data_directory") == "<data>":
                    bad["sql"]["data_directory"] = str(root / "data")
                with self.assertRaises((ValueError, RuntimeError)):
                    self.observe(root, bad=bad)

    def test_arm64_emulation_is_rejected_before_database_work(self):
        root = self.fixture()
        with self.assertRaisesRegex(ValueError, "emulation"):
            self.observe(root, machine={"os_architecture": "ARM64", "native_machine": 0xAA64,
                                        "processor_architecture_code": 12, "process_machine": 0x8664})

    def test_windows_short_paths_identify_the_same_pgconfig_and_data_directories(self):
        root = self.fixture()
        for relative in ("share", "lib", "include/server"):
            (root / relative).mkdir(parents=True)
        buffer = ctypes.create_unicode_buffer(32768)
        count = ctypes.windll.kernel32.GetShortPathNameW(str(root), buffer, len(buffer))
        self.assertGreater(count, 0, "GetShortPathNameW must observe the actual fixture")
        short = Path(buffer.value)
        print("short_path_alias_observed=" + str(str(short).casefold() != str(root).casefold()))
        bad = {"bindir": str(short / "bin"), "sharedir": str(short / "share"),
               "pkglibdir": str(short / "lib"), "includedir-server": str(short / "include/server"),
               "sql": {"server_version": "17.11", "server_encoding": "UTF8",
                       "data_directory": str(short / "data"), "compiler": "Visual C++ build 1937"}}
        (native, sidecar, compatibility), calls = self.observe(root, bad=bad)
        self.assertEqual(native["pg_config"]["bindir"], str(short / "bin"))
        self.assertEqual(native["database"]["data_directory"], str(short / "data"))

    def test_missing_compiler_and_wrong_encoding_are_rejected(self):
        root = self.fixture()
        for encoding, compiler in (("UTF8", "unsupported compiler"), ("WIN1252", "Visual C++ build 1937")):
            with self.subTest(encoding=encoding, compiler=compiler), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "postgresql/17"
                (root / "bin").mkdir(parents=True)
                with self.assertRaises(ValueError):
                    self.observe(root, bad={"sql": {"server_version": "17.11", "server_encoding": encoding,
                                                    "data_directory": str(root / "data"), "compiler": compiler}})

    def test_cleanup_observation_failure_is_never_converted_to_zero(self):
        root = self.fixture()
        (root / "bin/postgres.exe").write_bytes(b"payload")
        failure = subprocess.CompletedProcess([], 1, "SECRET", "SECRET")
        with mock.patch.object(pipeline, "powershell", side_effect=RuntimeError("CIM failed")):
            with self.assertRaisesRegex(RuntimeError, "CIM failed"):
                pipeline.cleanup_observation(root, 54329)
        with self.assertRaises(RuntimeError):
            pipeline.crt_evidence(root, runner=lambda command: failure, dumpbin="dumpbin.exe")

    def test_crt_rejects_incomplete_inspection_and_no_payload(self):
        root = self.fixture()
        with self.assertRaises(ValueError):
            pipeline.crt_evidence(root, dumpbin="dumpbin.exe")
        (root / "bin/postgres.exe").write_bytes(b"payload")
        with self.assertRaises(ValueError):
            pipeline.crt_evidence(root, dumpbin="dumpbin.exe", runner=lambda command:
                                 subprocess.CompletedProcess(command, 0, "not a dumpbin result", ""))

    def test_crt_includes_all_payload_exe_dll_and_delay_imports(self):
        root = self.fixture()
        (root / "bin/postgres.exe").write_bytes(b"a")
        (root / "bin/helper.dll").write_bytes(b"b")
        seen = []
        def runner(command):
            seen.append(Path(command[-1]).name)
            value = "MSVCR120.dll" if command[-1].endswith("helper.dll") else "KERNEL32.dll"
            return subprocess.CompletedProcess(command, 0,
                "File Type: DLL\n  Section contains the following delay load imports:\n    " + value + "\n  Summary\n", "")
        result = pipeline.crt_evidence(root, runner=runner, dumpbin="dumpbin.exe")
        self.assertEqual(set(seen), {"postgres.exe", "helper.dll"})
        self.assertEqual(result["crt"], "imports: msvcr120.dll")
        self.assertEqual(result["dependencies"], ["msvcr120.dll"])

    def fake_repacker(self):
        def prepare(source, identity, output, **kwargs):
            output.mkdir()
            (output / "core.7z").write_bytes(b"archive bytes")
            audit = {"archive": {"filename": "core.7z", "bytes": 13, "sha256": "b" * 64},
                     "files": [{"target_path": "postgresql/17/bin/postgres.exe", "sha256": "c" * 64}]}
            (output / "postgresql-repack-manifest.json").write_text(json.dumps(audit))
            return audit
        def extract(package, expected, seven, output):
            (output / "verified/postgresql/17/bin").mkdir(parents=True)
        def finalize(package, identity, source, url, native, producer, minimum, compatibility, output, **kwargs):
            self.assertNotIn("GH_TOKEN", os.environ)
            self.assertEqual(json.loads(native.read_text()), {"observed": "native"})
            output.mkdir()
            (output / "meta.json").write_text(json.dumps({"producer": producer, "source_url": url,
                                                        "minimum_os": minimum, "version": identity["version"]}))
            (output / "core.7z").write_bytes(package.read_bytes())
        return SimpleNamespace(package_meta=REAL_REPACKER.package_meta,
                               _identity=REAL_REPACKER._identity, prepare_postgresql_core=mock.Mock(side_effect=prepare),
                               _verify_archive=mock.Mock(side_effect=extract),
                               finalize_postgresql_core=mock.Mock(side_effect=finalize))

    def security_snapshot(self, path):
        script = ("$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                  "$acl=Get-Acl -LiteralPath " + pipeline.ps_literal(path) + "; "
                  "$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])); "
                  "$ready=@($rules | Where-Object {$_.IdentityReference.Value -eq $sid -and "
                  "$_.AccessControlType -eq 'Allow' -and "
                  "($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq "
                  "[Security.AccessControl.FileSystemRights]::FullControl -and "
                  "($_.InheritanceFlags -band 3) -eq 3 -and $_.PropagationFlags -eq 0}).Count -gt 0; "
                  "$others=@($rules | Where-Object {$_.IdentityReference.Value -ne $sid} | ForEach-Object { "
                  "'{0}|{1}|{2}|{3}|{4}|{5}' -f $_.IdentityReference.Value,[int]$_.FileSystemRights,"
                  "[int]$_.InheritanceFlags,[int]$_.PropagationFlags,$_.IsInherited,$_.AccessControlType} | Sort-Object); "
                  "@{owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value; "
                  "sddl=$acl.GetSecurityDescriptorSddlForm('All'); others=$others; ready=$ready; "
                  "protected=$acl.AreAccessRulesProtected} | ConvertTo-Json -Compress -Depth 4")
        value = json.loads(pipeline.powershell(script))
        return {"owner_hash": hashlib.sha256(value["owner"].encode()).hexdigest(),
                "acl_hash": hashlib.sha256(value["sddl"].encode()).hexdigest(),
                "other_rules_hash": hashlib.sha256(json.dumps(value["others"]).encode()).hexdigest(),
                "protected": value["protected"], "ready": value["ready"]}

    def test_runtime_work_tree_has_user_inheritable_access_before_download(self):
        repacker = self.fake_repacker()
        archive = self.root / "source.zip"
        archive.write_bytes(b"source")
        output = self.root / "formal"
        parent_before = self.security_snapshot(self.root)
        existing_before = self.security_snapshot(archive)
        observations = []
        def download(version, revision, destination):
            work = Path(destination).parent
            snapshot = self.security_snapshot(work)
            observations.append(snapshot)
            self.assertTrue(snapshot["ready"], "New work tree must grant the actual TokenUser inheritable F")
            self.assertFalse(list(work.iterdir()), "Access must be prepared before payload/download creation")
            self.assertTrue(snapshot["owner_hash"] == parent_before["owner_hash"], "Existing default owner must be preserved")
            self.assertTrue(self.security_snapshot(self.root) == parent_before, "Parent ACL must remain unchanged")
            self.assertTrue(self.security_snapshot(archive) == existing_before, "Existing input ACL must remain unchanged")
            return {"path": str(archive), "version": version, "url": pipeline.source_url(version, revision),
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "bytes": archive.stat().st_size}
        with mock.patch.object(pipeline, "load_repacker", return_value=repacker), \
                mock.patch.object(pipeline, "download_source", side_effect=download), \
                mock.patch.object(pipeline, "collect_native", return_value=({"observed": "native"}, {}, {})):
            try:
                pipeline.pipeline("17.11", 5, self.producer, output, "7z")
            except ValueError:
                self.fail("New work tree access must be successfully prepared before download")
        self.assertEqual(len(observations), 1)
        self.assertTrue(self.security_snapshot(self.root) == parent_before, "Pipeline must not grant parent directory access")

    def test_runtime_native_commands_receive_package_bin_cwd(self):
        root = self.fixture()
        observed = []
        initial = Path.cwd()
        def observe_cwd(command, cwd):
            self.assertTrue(cwd is not None and Path(cwd).resolve() == (root / "bin").resolve(),
                            "Every native PostgreSQL command must use its accessible package bin cwd")
            observed.append(Path(command[0]).stem)
        self.observe(root, cwd_callback=observe_cwd)
        self.assertTrue({"pg_config", "postgres", "initdb", "pg_ctl", "psql"} <= set(observed))
        self.assertTrue(Path.cwd() == initial, "Native cwd must not change the Python process cwd")

    def test_runtime_user_grant_preserves_owner_other_aces_and_protection(self):
        before = self.security_snapshot(self.root)
        metadata = pipeline.prepare_work_directory(self.root)
        after = self.security_snapshot(self.root)
        self.assertTrue(after["ready"], "Actual user inheritable FullControl ACE must be observable")
        for key in ("owner_hash", "other_rules_hash", "protected"):
            self.assertTrue(before[key] == after[key], "Existing owner/other ACEs/protection must be preserved")
        self.assertTrue(metadata["work_access_ready"])
        self.assertTrue(metadata["owner_preserved"])
        self.assertEqual(metadata["python_version"], sys.version.split()[0])
        self.assertEqual(metadata["system_temp_drive"], Path(tempfile.gettempdir()).drive.upper())
        self.assertEqual(metadata["work_drive"], self.root.drive.upper())
        serialized = json.dumps(metadata)
        self.assertNotIn("S-1-", serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_runtime_drive_diagnostics_are_drive_letters_only(self):
        for value, expected in (("C:/Temp/folder", "C:"), ("d:/work/folder", "D:"),
                                ("//server/SECRET-share/folder", "unavailable"),
                                ("relative-SECRET", "unavailable")):
            with self.subTest(kind=expected):
                self.assertEqual(pipeline.safe_drive(value), expected)

    def test_runtime_invalid_sid_and_failed_grant_stop_before_download(self):
        for failure in ("invalid_sid", "grant_exit"):
            with self.subTest(failure=failure):
                output = self.root / failure
                failed = subprocess.CompletedProcess([], 5, "SECRET stdout", "SECRET stderr")
                original = pipeline.prepare_work_directory
                failure_probe = mock.Mock(return_value=failed)
                with mock.patch.object(pipeline, "download_source") as download, \
                        mock.patch.object(pipeline, "collect_native") as native:
                    if failure == "invalid_sid":
                        with mock.patch.object(pipeline, "token_user_sid", return_value="invalid-SECRET"):
                            with self.assertRaises(ValueError) as caught:
                                pipeline.pipeline("17.11", 5, self.producer, output, "7z")
                    else:
                        with mock.patch.object(pipeline, "prepare_work_directory",
                                               side_effect=lambda path: original(path, runner=failure_probe)):
                            with self.assertRaises(pipeline.ProbeFailure) as caught:
                                pipeline.pipeline("17.11", 5, self.producer, output, "7z")
                        command = failure_probe.call_args.args[0]
                        self.assertEqual(Path(command[0]).name.lower(), "icacls.exe")
                        self.assertNotIn("/T", command)
                        self.assertNotIn("/grant:r", command)
                        self.assertTrue(Path(command[1]).parent == self.root and Path(command[1]) != self.root,
                                        "Only the newly-created work directory may be granted")
                    download.assert_not_called()
                    native.assert_not_called()
                self.assertNotIn("SECRET", pipeline.failure_summary(caught.exception))
                self.assertFalse(output.exists())

    def test_runtime_claimed_grant_success_requires_actual_acl_readback(self):
        successful = subprocess.CompletedProcess([], 0, "claims success", "")
        with self.assertRaises(ValueError):
            pipeline.prepare_work_directory(self.root, runner=lambda command: successful)

    def test_start_returns_while_background_descendant_still_holds_standard_handles(self):
        root = self.fixture()
        ready, stop = self.root / "background.ready", self.root / "background.stop"
        query_entered, release_query = threading.Event(), threading.Event()
        baseline, calls = self.runner(root)
        errors = []
        child = ("import os,sys,time; from pathlib import Path; "
                 "ready=Path(sys.argv[1]); stop=Path(sys.argv[2]); "
                 "ready.write_text(str(os.getpid())); deadline=time.monotonic()+15\n"
                 "while not stop.exists() and time.monotonic()<deadline: time.sleep(0.05)\n")
        launcher = "import subprocess,sys; subprocess.Popen([sys.executable,'-B','-c'," + repr(child) + ",sys.argv[1],sys.argv[2]],close_fds=False)"
        def controlled_runner(command, **kwargs):
            name = Path(command[0]).stem
            if name == "pg_ctl" and "start" in command:
                return pipeline.run([sys.executable, "-B", "-c", launcher, str(ready), str(stop)], **kwargs)
            if name == "psql" and "--version" not in command:
                query_entered.set()
                release_query.wait(8)
            if name == "pg_ctl" and "stop" in command:
                stop.write_text("normal stop")
            return baseline(command, **kwargs)
        def worker():
            try:
                self.observe(root, runner_override=controlled_runner)
            except Exception as error:
                errors.append(type(error).__name__)
        thread = threading.Thread(target=worker)
        thread.start()
        handle = None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "Supervised background fixture must actually start")
            handle = kernel.OpenProcess(0x1000, False, int(ready.read_text()))
            self.assertTrue(bool(handle), "Background child must be independently observable")
            self.assertTrue(query_entered.wait(3), "pg_ctl start must return before its background descendant exits")
            exit_code = ctypes.c_ulong()
            self.assertTrue(bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))))
            self.assertEqual(exit_code.value, 259, "Background descendant must remain running when start returns")
        finally:
            release_query.set()
            stop.write_text("normal supervised shutdown")
            thread.join(12)
            if handle:
                deadline = time.monotonic() + 5
                exit_code = ctypes.c_ulong(259)
                while time.monotonic() < deadline:
                    kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    if exit_code.value != 259:
                        break
                    time.sleep(0.05)
                kernel.CloseHandle(handle)
                self.assertNotEqual(exit_code.value, 259, "Background fixture must exit without a force kill")
        self.assertFalse(thread.is_alive(), "Native call must be completely joined")
        self.assertFalse(errors, "Supervised native fixture must preserve query/stop behavior")

    def test_foreground_values_remain_captured_and_uncaptured_nonzero_still_fails(self):
        result = pipeline.run([sys.executable, "-B", "-c",
                               "import sys;print('actual foreground');sys.stderr.write('actual stderr')"], cwd=self.root)
        self.assertEqual(result.stdout.strip(), "actual foreground")
        self.assertEqual(result.stderr, "actual stderr")
        failed = pipeline.run([sys.executable, "-B", "-c",
                               "import sys;sys.stderr.write('SECRET-output');sys.exit(7)"],
                              cwd=self.root, capture_output=False)
        self.assertEqual(failed.returncode, 7)
        self.assertIsNone(failed.stdout)
        self.assertIsNone(failed.stderr)
        with self.assertRaises(pipeline.ProbeFailure) as caught:
            pipeline.require_ok(failed, "startup")
        self.assertIn("exit=7", pipeline.failure_summary(caught.exception))
        self.assertNotIn("SECRET", pipeline.failure_summary(caught.exception))

    def test_pipeline_binds_actual_source_producer_and_native_before_export(self):
        repacker = self.fake_repacker()
        archive = self.root / "source.zip"
        archive.write_bytes(b"real frozen source")
        source = {"path": str(archive), "version": "17.11", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "bytes": archive.stat().st_size,
                  "url": pipeline.source_url("17.11", 5)}
        output = self.root / "formal"
        with mock.patch.object(pipeline, "load_repacker", return_value=repacker), \
                mock.patch.object(pipeline, "download_source", return_value=source), \
                mock.patch.object(pipeline, "collect_native", return_value=({"observed": "native"}, {"compiler": "actual"}, {"crt": "actual"})), \
                mock.patch.dict(os.environ, {"GH_TOKEN": "SECRET"}):
            pipeline.pipeline("17.11", 5, self.producer, output, "7z", minimum_os="10.0")
        self.assertEqual(repacker.prepare_postgresql_core.call_args.args[0], source)
        self.assertEqual(repacker.prepare_postgresql_core.call_args.args[1]["arch"], "x86_64")
        self.assertEqual(repacker.finalize_postgresql_core.call_args.args[5], self.producer)
        self.assertEqual(json.loads((output / "source-evidence.json").read_text())["source"]["sha256"], source["sha256"])
        self.assertEqual(json.loads((output / "meta.json").read_text())["minimum_os"], "10.0")
        self.assertEqual((output / "core.7z").read_bytes(), b"archive bytes")

    def test_cli_workflow_producer_passes_real_contract_and_exports(self):
        repacker = self.fake_repacker()
        archive = self.root / "source.zip"
        archive.write_bytes(b"real frozen source")
        source = {"path": str(archive), "version": "17.11", "url": pipeline.source_url("17.11", 5),
                  "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "bytes": archive.stat().st_size}
        output = self.root / "cli-formal"
        arguments = ["postgresql_v2_pipeline.py", "--version", "17.11", "--edb-revision", "5",
                     "--minimum-os", "10.0", "--commit", "a" * 40, "--run-id", "123", "--run-attempt", "2",
                     "--output", str(output), "--seven-zip", "7z"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", arguments), \
                mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(pipeline, "load_repacker", return_value=repacker), \
                mock.patch.object(pipeline, "download_source", return_value=source), \
                mock.patch.object(pipeline, "collect_native", return_value=({"observed": "native"}, {}, {})):
            status = 0
            try:
                pipeline.main()
            except SystemExit as error:
                status = error.code
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(repacker.finalize_postgresql_core.call_args.args[5], self.producer)
        metadata = json.loads((output / "meta.json").read_text())
        self.assertEqual(metadata["producer"], self.producer)
        self.assertEqual(REAL_REPACKER.package_meta._state.validate_producer(metadata["producer"]), self.producer)
        self.assertEqual(repacker.prepare_postgresql_core.call_args.args[1]["arch"], "x86_64")

    def test_cli_invalid_producer_json_is_rejected_without_silent_normalization(self):
        cases = [{**self.producer, "repository": "SECRET-invalid-extra-field"},
                 {**self.producer, "run_id": 123}]
        for producer in cases:
            with self.subTest(producer_type=type(producer["run_id"]).__name__):
                path = self.root / "producer.json"
                path.write_text(json.dumps(producer), encoding="utf-8")
                output = self.root / "must-not-export"
                arguments = ["postgresql_v2_pipeline.py", "--version", "17.11", "--edb-revision", "5",
                             "--minimum-os", "10.0", "--producer", str(path), "--output", str(output),
                             "--seven-zip", "7z"]
                stderr = io.StringIO()
                with mock.patch.object(sys, "argv", arguments), mock.patch.object(sys, "stderr", stderr), \
                        mock.patch.object(pipeline, "download_source") as download:
                    with self.assertRaises(SystemExit) as caught:
                        pipeline.main()
                self.assertEqual(caught.exception.code, 1)
                self.assertNotIn("SECRET", stderr.getvalue())
                download.assert_not_called()
                self.assertFalse(output.exists())

    def test_changed_download_source_never_finalizes(self):
        repacker = self.fake_repacker()
        archive = self.root / "source.zip"
        archive.write_bytes(b"changed source")
        source = {"path": str(archive), "url": pipeline.source_url("17.11", 5),
                  "sha256": "0" * 64, "bytes": archive.stat().st_size}
        output = self.root / "formal"
        with mock.patch.object(pipeline, "load_repacker", return_value=repacker), \
                mock.patch.object(pipeline, "download_source", return_value=source), \
                mock.patch.object(pipeline, "collect_native", return_value=({"observed": "native"}, {}, {})):
            with self.assertRaisesRegex(ValueError, "changed before finalization"):
                pipeline.pipeline("17.11", 5, self.producer, output, "7z")
        repacker.finalize_postgresql_core.assert_not_called()
        self.assertFalse(output.exists())

    def test_native_or_extraction_failure_never_calls_finalize_or_exports(self):
        for kind in ("native", "extract"):
            with self.subTest(kind=kind):
                repacker = self.fake_repacker()
                if kind == "extract":
                    repacker._verify_archive.side_effect = RuntimeError("probe failed")
                output = self.root / kind
                archive = self.root / (kind + ".zip")
                archive.write_bytes(b"source")
                source = {"path": str(archive), "url": "official", "bytes": archive.stat().st_size,
                          "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
                with mock.patch.object(pipeline, "load_repacker", return_value=repacker), \
                        mock.patch.object(pipeline, "download_source", return_value=source), \
                        mock.patch.object(pipeline, "collect_native", side_effect=RuntimeError("probe failed")):
                    with self.assertRaises(RuntimeError):
                        pipeline.pipeline("17.11", 5, self.producer, output, "7z")
                repacker.finalize_postgresql_core.assert_not_called()
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
