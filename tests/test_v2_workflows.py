import json
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def workflow(self, name):
        data = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        # PyYAML's YAML 1.1 reads the unquoted Actions `on` key as boolean True.
        data["on"] = data.get("on", data.get(True))
        return data

    def copy_php_source_contract(self, root):
        profile = ROOT / "runtime-v2"
        releases = json.loads((profile / "config/php-backports.json").read_text())["releases"]
        paths = ["ci/php_source.py", "config/php-backports.json"]
        paths += ["patch/" + item["patch"] for item in releases.values()]
        for relative in paths:
            target = root / "runtime-v2" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(profile / relative, target)

    def setup_plan_fixture(self, root, rows):
        (root / "runtime/ci").mkdir(parents=True)
        for source in ("runtime/update_packages.py", "runtime/ci/build_profile.py", "runtime/ci/build_state.py", "runtime/ci/build_control.py", "runtime/ci/reserve_builds.sh"):
            shutil.copy2(ROOT / source, root / source)
        self.copy_php_source_contract(root)
        packages = {(row["name"], row["version"]) for row in rows}
        (root / "runtime-v2/config/packages.conf").write_text("".join(f"{name}\t{version}\t{name}-{version}.tar.gz\t{name}-{version}.tar.gz\tsource\n" for name, version in packages))
        (root / "ledger.json").write_text(json.dumps({"schema_version": 1, "units": {}}))
        shutil.copy2(root / "ledger.json", root / "runtime-v2/config/build-state.json")
        (root / "gh").write_text("#!" + __import__('sys').executable + "\n" + """import base64,json,pathlib,sys
p=pathlib.Path('ledger.json')
if '--method' in sys.argv:
    payload=json.load(sys.stdin)
    p.write_bytes(base64.b64decode(payload['content']))
    print('{}')
else:
    print(json.dumps({'sha':'fixture-blob', 'content':base64.b64encode(p.read_bytes()).decode()}))
""")
        (root / "gh").chmod(0o755)
        head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        return dict(PENDING_ONLY="true", CONFIG_CHANGED="false", GITHUB_SHA=head,
                    GITHUB_RUN_ID="12345", GITHUB_RUN_ATTEMPT="1", GITHUB_REPOSITORY="ServBay/Builder",
                    RUNNER_TEMP=str(root / "tmp"))

    def dispatch_payload(self, workflow_name, runtime_version, dry_run):
        workflow = self.workflow(workflow_name)
        signal = next(step for step in workflow["jobs"]["dispatch-downstream"]["steps"]
                      if step.get("name", "").startswith("Signal downstream"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "dispatch.json"
            curl = root / "curl"
            curl.write_text("#!" + sys.executable + "\n" + """import json, os, sys
from pathlib import Path
Path(os.environ['DISPATCH_CAPTURE']).write_text(json.dumps({'arguments': sys.argv[1:], 'body': json.loads(sys.argv[-1])}))
""")
            curl.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                       DISPATCH_CAPTURE=str(capture), DISPATCH_PAT="fixture-token", TARGET_REPO="fixture/uploader",
                       RUN_ID="12345", RUN_ATTEMPT="2", RUNTIME_VERSION=runtime_version,
                       PRODUCER_COMMIT="a" * 40, DRY_RUN=dry_run)
            result = subprocess.run(["/bin/bash", "-c", signal["run"]], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            recorded = json.loads(capture.read_text())
            self.assertIn("https://api.github.com/repos/fixture/uploader/dispatches", recorded["arguments"])
            self.assertEqual("builder-build-complete", recorded["body"]["event_type"])
            return recorded["body"]["client_payload"]

    def assert_dispatch_routes_macos_v2_and_preserves_v1(self, workflow_name):
        for dry_run in ("true", "false"):
            with self.subTest(workflow=workflow_name, runtime="v2", dry_run=dry_run):
                payload = self.dispatch_payload(workflow_name, "v2", dry_run)
                self.assertEqual({"run_id": "12345", "dry_run": dry_run, "runtime_version": "v2", "os": "macos",
                                  "run_attempt": 2, "producer_commit": "a" * 40}, payload)
            with self.subTest(workflow=workflow_name, runtime="v1", dry_run=dry_run):
                self.assertEqual({"run_id": "12345", "dry_run": dry_run}, self.dispatch_payload(workflow_name, "v1", dry_run))

    def test_check_updates_actual_dispatch_routes_macos_v2_and_preserves_v1(self):
        self.assert_dispatch_routes_macos_v2_and_preserves_v1("check-updates.yml")

    def test_build_and_dispatch_actual_dispatch_routes_macos_v2_and_preserves_v1(self):
        self.assert_dispatch_routes_macos_v2_and_preserves_v1("build-and-dispatch.yml")

    def test_existing_macos_entrypoints_default_to_v1(self):
        for name in ("build-macos.yml", "build-and-dispatch.yml", "check-updates.yml"):
            with self.subTest(name=name):
                data = self.workflow(name)
                self.assertIn("runtime_version", data["on"]["workflow_dispatch"]["inputs"])
                self.assertEqual("v1", data["on"]["workflow_dispatch"]["inputs"]["runtime_version"]["default"])
        self.assertEqual("v1", self.workflow("build-macos.yml")["on"]["workflow_call"]["inputs"]["runtime_version"]["default"])

    def test_check_run_mode_preserves_v1_schedule_and_keeps_v2_opt_in(self):
        workflow = self.workflow("check-updates.yml")
        step = next(step for step in workflow["jobs"]["check"]["steps"] if step.get("id") == "mode")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            for event, runtime, should_build in (("schedule", "", "true"), ("workflow_dispatch", "v2", "false")):
                output.write_text("")
                env = dict(os.environ, GITHUB_OUTPUT=str(output), EVENT_NAME=event, INPUT_RUNTIME_VERSION=runtime,
                           INPUT_TARGET="macos", INPUT_CONTINUE_BUILD="false", INPUT_UPLOAD_DRY_RUN="true",
                           INPUT_FULL_REBUILD="false")
                result = subprocess.run(["/bin/bash", "-c", step["run"]], env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(runtime or "v1", values.get("runtime_version"))
                self.assertEqual(should_build, values["continue_build"])

    def test_v2_uses_its_own_cache_bootstrap_metadata_and_failure_collection(self):
        workflow = self.workflow("build-macos.yml")
        jobs = workflow["jobs"]
        self.assertIn("plan", jobs)
        steps = jobs["build"]["steps"]
        bootstrap = [step for step in steps if step.get("name") == "Bootstrap Runtime v2 build base"]
        self.assertEqual(1, len(bootstrap))
        self.assertIn("v2", bootstrap[0].get("if", ""))
        meta = next((step for step in steps if step.get("id") == "meta_v2"), None)
        self.assertIsNotNone(meta, "Schema2 handoff must have its own successful metadata step")
        for name in ("build-and-dispatch.yml", "check-updates.yml"):
            current = self.workflow(name)
            collector = current["jobs"].get("collect-macos-v2")
            self.assertIsNotNone(collector, "Each v2 orchestrator must preserve partial build results")
            self.assertIn("always()", collector["if"])
            self.assertEqual("write", collector["permissions"]["contents"])

    def test_update_workflow_separates_grouped_builds_from_exact_collection_records(self):
        workflow = self.workflow("check-updates.yml")
        step = next(item for item in workflow["jobs"]["check"]["steps"] if item.get("id") == "plan")
        rows = [{"runtime_version": "v2", "os": "macos", "kind": "package", "name": "nginx",
                 "version": "1.31.7", "arch": arch, "build_revision": 0, "build_type": "source"}
                for arch in ("x86_64", "arm64")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.setup_plan_fixture(root, rows)
            (root / "macos-matrix.json").write_text(json.dumps({"include": rows}))
            (root / "windows-matrix.json").write_text('{"include": []}')
            output = root / "github-output"
            env = dict(os.environ, PATH=str(root) + os.pathsep + str(Path(__import__('sys').executable).parent) + os.pathsep + os.environ['PATH'],
                       **fixture, RUNTIME_VERSION="v2", GITHUB_OUTPUT=str(output), CONTINUE_BUILD="true", UPLOAD_DRY_RUN="true",
                       GIT_DIR=str(ROOT / ".git"), GIT_WORK_TREE=str(root))
            result = subprocess.run(["/bin/bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            grouped = json.loads(values["macos_matrix"])["include"]
            self.assertEqual(["all"], [row["arch"] for row in grouped])
            self.assertEqual(rows, json.loads(values["macos_records"])["include"])
            self.assertEqual("1", values["macos_count"])
            self.assertEqual("true", values["should_build"])
        collector = workflow["jobs"]["collect-macos-v2"]["steps"][-1]
        self.assertEqual("${{ needs.check.outputs.macos_records }}", collector["env"]["BUILD_RECORDS_JSON"])

    def test_update_workflow_does_not_collect_units_beyond_its_256_scheduled_groups(self):
        workflow = self.workflow("check-updates.yml")
        step = next(item for item in workflow["jobs"]["check"]["steps"] if item.get("id") == "plan")
        rows = [{"runtime_version": "v2", "os": "macos", "kind": "package", "name": f"package-{number}",
                 "version": "1.0.0", "arch": arch, "build_revision": 0, "build_type": "source"}
                for number in range(257) for arch in ("x86_64", "arm64")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.setup_plan_fixture(root, rows)
            (root / "macos-matrix.json").write_text(json.dumps({"include": rows}))
            (root / "windows-matrix.json").write_text('{"include": []}')
            output = root / "github-output"
            env = dict(os.environ, PATH=str(root) + os.pathsep + str(Path(__import__('sys').executable).parent) + os.pathsep + os.environ['PATH'],
                       **fixture, RUNTIME_VERSION="v2", GITHUB_OUTPUT=str(output), CONTINUE_BUILD="true", UPLOAD_DRY_RUN="true",
                       GIT_DIR=str(ROOT / ".git"), GIT_WORK_TREE=str(root))
            result = subprocess.run(["/bin/bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(256, len(json.loads(values["macos_matrix"])["include"]))
            self.assertEqual(rows[:512], json.loads(values["macos_records"])["include"])
            self.assertEqual(rows[:512], json.loads((root / "macos-matrix.json").read_text())["include"])
            self.assertEqual("2", values["macos_deferred"])
            units = json.loads((root / 'ledger.json').read_text())['units']
            self.assertEqual(514, len(units))
            self.assertEqual(512, sum('reservation' in unit for unit in units.values()))
            self.assertTrue(all(unit['consecutive_failures'] == 0 for unit in units.values()))

    def test_new_config_run_dispatches_once_without_reserving_or_building(self):
        workflow = self.workflow("check-updates.yml")
        step = next(item for item in workflow["jobs"]["check"]["steps"] if item.get("id") == "plan")
        rows = [dict(runtime_version="v2", os="macos", kind="package", name="nginx", version="1.31.7", arch="arm64", build_revision=0)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.setup_plan_fixture(root, rows)
            (root / "macos-matrix.json").write_text(json.dumps({"include": rows}))
            (root / "windows-matrix.json").write_text('{"include": []}')
            output = root / "github-output"
            fixture.update(PENDING_ONLY="false", CONFIG_CHANGED="true", GITHUB_SHA="a" * 40)
            env = dict(os.environ, **fixture, PATH=str(root) + os.pathsep + str(Path(__import__('sys').executable).parent) + os.pathsep + os.environ['PATH'],
                       RUNTIME_VERSION="v2", GITHUB_OUTPUT=str(output), CONTINUE_BUILD="true", UPLOAD_DRY_RUN="true",
                       GIT_DIR=str(ROOT / ".git"), GIT_WORK_TREE=str(root))
            result = subprocess.run(["/bin/bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual("false", values["should_build"])
            self.assertEqual("true", values["dispatch_pending"])
            self.assertEqual([], json.loads(values["macos_records"])["include"])
            self.assertEqual({}, json.loads((root / "ledger.json").read_text())["units"])
            # A pending run cannot repeat version refresh or dispatch another continuation.
            output.write_text("")
            env.update(PENDING_ONLY="true", CONFIG_CHANGED="false", GITHUB_SHA="a" * 40)
            invalid = subprocess.run(["/bin/bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(0, invalid.returncode)
            self.assertIn("trigger SHA", invalid.stderr)

    def test_both_manual_entrypoints_reserve_before_any_paid_job_and_skip_duplicate_owner(self):
        rows = [dict(runtime_version="v2", os="macos", kind="package", name="nginx", version="1.31.7", arch="arm64", build_revision=0)]
        for workflow_name, job_name in (("build-macos.yml", "plan"), ("build-and-dispatch.yml", "plan-macos")):
            with self.subTest(workflow=workflow_name), tempfile.TemporaryDirectory() as directory:
                workflow = self.workflow(workflow_name)
                step = next(item for item in workflow["jobs"][job_name]["steps"] if item.get("id") == "plan")
                root = Path(directory)
                fixture = self.setup_plan_fixture(root, rows)
                output = root / "github-output"
                env = dict(os.environ, **fixture, PATH=str(root) + os.pathsep + str(Path(__import__('sys').executable).parent) + os.pathsep + os.environ['PATH'],
                           RUNTIME_VERSION="v2", TARGETS="macos", PKG="nginx", VERSION="1.31.7", ARCH="arm64", REVISION="0", GITHUB_OUTPUT=str(output),
                           GIT_DIR=str(ROOT / ".git"), GIT_WORK_TREE=str(root))
                result = subprocess.run(["/bin/bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                selected = json.loads(values["matrix"])["include"]
                self.assertEqual("1", values["count"])
                self.assertTrue(selected[0]["unit_job"].startswith("Build v2 "))
                self.assertEqual(fixture["GITHUB_SHA"], values["commit"])
                units = json.loads((root / "ledger.json").read_text())["units"]
                self.assertEqual("12345", next(iter(units.values()))["reservation"]["producer"]["run_id"])
                output.write_text("")
                env['GITHUB_RUN_ID'] = '12346'
                duplicate = subprocess.run(["/bin/bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(0, duplicate.returncode, duplicate.stderr)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual("0", values["count"])
                self.assertEqual([], json.loads(values["matrix"])["include"])

    def test_job_api_labels_and_continuation_outputs_match_the_runtime_contract(self):
        workflow = self.workflow("build-macos.yml")
        self.assertEqual("${{ matrix.unit_job || 'Build macOS package' }}", workflow["jobs"]["build"]["name"])
        steps = workflow["jobs"]["build"]["steps"]
        select = next(index for index, step in enumerate(steps) if step.get("name") == "Select Runtime v2 package profile")
        compile_step = next(index for index, step in enumerate(steps) if step.get("name") == "Compile Runtime v2 package")
        self.assertLess(select, compile_step)
        self.assertIn("build_package.sh", steps[compile_step]["run"])
        self.assertNotIn("build_profile.py", steps[compile_step]["run"])
        continuation = self.workflow("check-updates.yml")["jobs"]["continue-pending-v2"]
        self.assertEqual("${{ needs.check.outputs.dispatch_pending == 'true' }}", continuation["if"])
        self.assertEqual("ubuntu-latest", continuation["runs-on"])
        self.assertEqual("write", continuation["permissions"]["actions"])

    def test_real_handoff_step_emits_schema2_from_actual_artifact_and_source(self):
        workflow = self.workflow("build-macos.yml")
        step = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("id") == "meta_v2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime/ci").mkdir(parents=True)
            for path in ("runtime/update_packages.py", "runtime/ci/build_profile.py", "runtime/ci/build_state.py", "runtime/ci/package_meta.py"):
                shutil.copy2(ROOT / path, root / path)
            self.copy_php_source_contract(root)
            profile = root / "runtime-v2"
            (profile / "config").mkdir(parents=True, exist_ok=True)
            (profile / "config/packages.conf").write_text("nginx\t1.31.7\tnginx-1.31.7.tar.gz\tnginx-1.31.7.tar.gz\tsource\n")
            (profile / "src").mkdir()
            source = profile / "src/nginx-1.31.7.tar.gz"
            source.write_bytes(b"primary upstream bytes")
            package = profile / "dist/packages/nginx/nginx-1.31.7.tar.gz"
            package.parent.mkdir(parents=True)
            with tarfile.open(package, "w:gz") as archive:
                info = tarfile.TarInfo("nginx/VERSION")
                info.size = 7
                archive.addfile(info, io.BytesIO(b"1.31.7\n"))
            output = root / "github-output"
            record = {"runtime_version": "v2", "os": "macos", "kind": "package", "name": "nginx",
                      "version": "1.31.7", "arch": "arm64", "build_revision": 0, "build_type": "source"}
            env = dict(os.environ, PATH=str(Path(__import__('sys').executable).parent) + os.pathsep + os.environ['PATH'],
                       RECORD_JSON=json.dumps(record), PRODUCER_COMMIT="a" * 40, GITHUB_RUN_ID="12345",
                       GITHUB_RUN_ATTEMPT="2", BASE_VERSION="2.0.1", BASE_SHA256_RUNTIME="b" * 64,
                       BASE_SHA256_DEVLIB="c" * 64, GITHUB_OUTPUT=str(output))
            result = subprocess.run(["/bin/bash", "-c", step["run"]], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            meta = json.loads((package.parent / "meta.json").read_text())
            self.assertEqual(2, meta["schema_version"])
            self.assertEqual("arm64", meta["arch"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), meta["source"]["sha256"])
            self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(), meta["sha256"])
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual("pkg2-macos-arm64-package-nginx-1.31.7", values["artifact_name"])

    def test_v2_missing_base_variables_cannot_fall_back_to_a_v1_base(self):
        steps = self.workflow("build-macos.yml")["jobs"]["build"]["steps"]
        step = next(step for step in steps if step.get("name") == "Resolve pinned Runtime v2 base")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-env"
            env = dict(os.environ, V2_URL="", V2_VERSION="", BASE_DOWNLOAD_URL="https://example.com/v1",
                       BASE_VERSION="1.0.20", BASE_SHA256_RUNTIME="b" * 64, BASE_SHA256_DEVLIB="c" * 64,
                       GITHUB_ENV=str(output))
            result = subprocess.run(["/bin/bash", "-c", step["run"]], env=env, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(output.exists())


    def test_update_only_all_resolves_to_v1_without_leaking_all_to_builds(self):
        workflow = self.workflow('check-updates.yml')
        for other in ('build-macos.yml', 'build-and-dispatch.yml'):
            self.assertNotIn('all', self.workflow(other)['on']['workflow_dispatch']['inputs']['runtime_version']['options'])
        mode = next(step for step in workflow['jobs']['check']['steps'] if step.get('id') == 'mode')
        for target in ('all', 'macos', 'windows'):
            for pending in ('false', 'true'):
                with self.subTest(target=target, pending=pending), tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / 'output'
                    env = dict(os.environ, GITHUB_OUTPUT=str(output), EVENT_NAME='workflow_dispatch',
                               INPUT_RUNTIME_VERSION='all', INPUT_TARGET=target, INPUT_CONTINUE_BUILD='false',
                               INPUT_UPLOAD_DRY_RUN='true', INPUT_FULL_REBUILD='false', INPUT_PENDING_ONLY=pending)
                    result = subprocess.run(['/bin/bash', '-c', mode['run']], env=env, capture_output=True, text=True)
                    if target == 'windows' or pending == 'true':
                        self.assertNotEqual(0, result.returncode)
                    else:
                        self.assertEqual(0, result.returncode, result.stderr)
                        values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                        self.assertEqual('v1', values['runtime_version'])
                        self.assertEqual(target, values['target'])
                        self.assertEqual('false', values['continue_build'])
                        self.assertEqual('true', values['upload_dry_run'])

    def refresh_dispatch(self, event='schedule', runtime='', mode='off', build='false', dry='true',
                         pending='false', target='all', full='false', api_exit=0):
        workflow = self.workflow('check-updates.yml')
        job = workflow['jobs'].get('refresh-v2')
        self.assertIsNotNone(job, 'Scheduled or all-version checks need one explicit same-repository v2 refresh')
        signal = next(step for step in job['steps'] if step.get('id') == 'dispatch')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runtime/ci').mkdir(parents=True)
            shutil.copy2(ROOT / 'runtime/ci/build_control.py', root / 'runtime/ci/build_control.py')
            capture = root / 'requests.jsonl'
            gh = root / 'gh'
            gh.write_text('#!' + sys.executable + '\n' + """import json,os,pathlib,sys
with pathlib.Path(os.environ['GH_CAPTURE']).open('a') as output:
    output.write(json.dumps({'arguments':sys.argv[1:], 'payload':json.load(sys.stdin)})+'\\n')
sys.exit(int(os.environ['GH_FIXTURE_EXIT']))
""")
            gh.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                       GH_TOKEN='controlled-fixture', GH_CAPTURE=str(capture), GH_FIXTURE_EXIT=str(api_exit),
                       GITHUB_REPOSITORY='ServBay/Builder', EVENT_NAME=event, INPUT_RUNTIME_VERSION=runtime,
                       INPUT_TARGET=target, INPUT_PENDING_ONLY=pending, INPUT_CONTINUE_BUILD=build,
                       INPUT_UPLOAD_DRY_RUN=dry, INPUT_FULL_REBUILD=full, V2_PACKAGE_SCHEDULE_MODE=mode)
            result = subprocess.run(['/bin/bash', '-c', signal['run']], cwd=root, env=env, capture_output=True, text=True)
            requests = [json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []
            return result, requests

    def test_actual_refresh_step_dispatches_exact_modes_and_never_recurses(self):
        for mode, build, dry in [('check', 'false', 'true'), ('build', 'true', 'true'), ('publish', 'true', 'false')]:
            with self.subTest(mode=mode):
                result, requests = self.refresh_dispatch(mode=mode, build='true', dry='false', full='true')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(1, len(requests))
                self.assertEqual(['api', '--method', 'POST', 'repos/ServBay/Builder/actions/workflows/check-updates.yml/dispatches', '--input', '-'], requests[0]['arguments'])
                self.assertEqual({'ref':'main', 'inputs':dict(runtime_version='v2', target='macos', pending_only='false',
                                 continue_build=build, upload_dry_run=dry, full_rebuild='false')}, requests[0]['payload'])
        for mode in ('', 'off'):
            result, requests = self.refresh_dispatch(mode=mode)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], requests)
        for runtime in ('v1', 'v2'):
            for pending in ('false', 'true'):
                result, requests = self.refresh_dispatch(event='workflow_dispatch', runtime=runtime,
                                                        pending=pending, mode='publish', build='true', dry='false')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual([], requests)
        for build, dry in [('false', 'true'), ('true', 'false')]:
            result, requests = self.refresh_dispatch(event='workflow_dispatch', runtime='all', build=build, dry=dry, full='true')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(1, len(requests))
            self.assertEqual('v2', requests[0]['payload']['inputs']['runtime_version'])
            self.assertEqual(build, requests[0]['payload']['inputs']['continue_build'])
            self.assertEqual(dry, requests[0]['payload']['inputs']['upload_dry_run'])
            self.assertEqual('true', requests[0]['payload']['inputs']['full_rebuild'])
        for changes in (dict(mode='typo'), dict(event='workflow_dispatch', runtime='all', target='windows'),
                        dict(event='workflow_dispatch', runtime='all', pending='true')):
            result, requests = self.refresh_dispatch(**changes)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual([], requests)
        result, requests = self.refresh_dispatch(mode='build', api_exit=23)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(requests))

    def test_v2_check_payload_reaches_real_planner_without_reservation_or_macos_jobs(self):
        dispatched, requests = self.refresh_dispatch(mode='check')
        self.assertEqual(0, dispatched.returncode, dispatched.stderr)
        inputs = requests[0]['payload']['inputs']
        workflow = self.workflow('check-updates.yml')
        step = next(item for item in workflow['jobs']['check']['steps'] if item.get('id') == 'plan')
        rows = [dict(runtime_version='v2', os='macos', kind='package', name='nginx', version='1.31.7', arch='arm64', build_revision=0)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.setup_plan_fixture(root, rows)
            (root / 'macos-matrix.json').write_text(json.dumps({'include': rows}))
            (root / 'windows-matrix.json').write_text('{"include":[]}')
            output = root / 'output'
            fixture.update(PENDING_ONLY=inputs['pending_only'], CONFIG_CHANGED='false')
            env = dict(os.environ, **fixture, PATH=str(root) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                       RUNTIME_VERSION=inputs['runtime_version'],
                       CONTINUE_BUILD=inputs['continue_build'], UPLOAD_DRY_RUN=inputs['upload_dry_run'], GITHUB_OUTPUT=str(output),
                       GIT_DIR=str(ROOT / '.git'), GIT_WORK_TREE=str(root))
            result = subprocess.run(['/bin/bash', '-c', step['run']], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual('false', values['should_build'])
            self.assertEqual('false', values['allow_modules'])
            self.assertEqual('false', values['dispatch_pending'])
            self.assertEqual([], json.loads(values['macos_records'])['include'])
            self.assertEqual({}, json.loads((root / 'ledger.json').read_text())['units'])

    def test_refresh_job_serializes_config_updates_and_has_no_cross_repository_token(self):
        job = self.workflow('check-updates.yml')['jobs'].get('refresh-v2')
        self.assertIsNotNone(job, 'The fanout must await the v1 configuration stage')
        self.assertIn('all', self.workflow('check-updates.yml')['on']['workflow_dispatch']['inputs']['runtime_version']['options'])
        self.assertEqual('check', job['needs'])
        self.assertEqual({'contents':'read', 'actions':'write'}, job['permissions'])
        self.assertEqual("${{ !cancelled() && ((github.event_name == 'schedule' && vars.V2_PACKAGE_SCHEDULE_MODE != '' && vars.V2_PACKAGE_SCHEDULE_MODE != 'off') || (github.event_name == 'workflow_dispatch' && inputs.runtime_version == 'all')) }}", job['if'])
        signal = next(step for step in job['steps'] if step.get('id') == 'dispatch')
        self.assertEqual('${{ github.token }}', signal['env']['GH_TOKEN'])


if __name__ == "__main__":
    unittest.main()
