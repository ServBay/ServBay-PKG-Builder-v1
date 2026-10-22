import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import test_v2_core_manifest
import test_v2_workflows

ROOT = Path(__file__).resolve().parents[1]


class ModuleWorkflowTests(unittest.TestCase):
    def workflow(self, name):
        return test_v2_workflows.WorkflowTests(methodName='runTest').workflow(name)

    def copy_helpers(self, root):
        files = ['runtime/ci/build_state.py', 'runtime/ci/package_meta.py', 'runtime/ci/collect_builds.sh']
        files += ['runtime-v2/ci/' + name + '.py' for name in
                  ['core_manifest', 'module_manifest', 'module_profiles', 'module_host', 'module_pipeline', 'module_dependencies']]
        files += ['runtime-v2/config/modules.json']
        for name in files:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        test_v2_workflows.WorkflowTests(methodName='runTest').copy_php_source_contract(root)

    def fixture(self):
        fixture = test_v2_core_manifest.CoreManifestTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_actual_finalizer_blocks_missing_default_and_preserves_optional_failure_independence(self):
        workflow = self.workflow('build-macos.yml')
        step = next(step for step in workflow['jobs']['finalize-core']['steps'] if step.get('name') == 'Finalize Runtime v2 core')
        fixture = self.fixture()
        root = fixture.root / 'workflow'
        root.mkdir()
        self.copy_helpers(root)
        candidate = fixture.candidate()
        candidate_dir = root / 'candidate'
        candidate_dir.mkdir()
        shutil.copy2(fixture.package, candidate_dir / fixture.package.name)
        (candidate_dir / 'candidate.json').write_text(json.dumps(candidate))
        plan = {'cores': [{'candidate': candidate, 'external_candidates': []}]}
        plan_path = root / 'module-plan.json'
        plan_path.write_text(json.dumps(plan))
        modules = fixture.modules()
        snapshots = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in modules}
        for missing_default in (True, False):
            with self.subTest(missing_default=missing_default):
                meta_list = root / 'module-metas.json'
                meta_list.write_text(json.dumps(modules[:2] if missing_default else modules))
                final = root / ('missing-default-final' if missing_default else 'optional-imagick-failed-final')
                output = root / 'github-output'
                env = dict(os.environ, CANDIDATE_DIR=str(candidate_dir), MODULE_PLAN_FILE=str(plan_path),
                           MODULE_META_LIST=str(meta_list), FINAL_CORE_DIR=str(final), GITHUB_OUTPUT=str(output),
                           PRODUCER_COMMIT=fixture.producer['commit'], GITHUB_RUN_ID=fixture.producer['run_id'],
                           GITHUB_RUN_ATTEMPT=str(fixture.producer['run_attempt']))
                result = subprocess.run(['/bin/bash', '-c', step['run']], cwd=root, env=env, capture_output=True, text=True)
                if missing_default:
                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse(final.exists(), 'An incomplete default set must not emit a final core Artifact')
                else:
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    meta = json.loads((final / 'meta.json').read_text())
                    self.assertEqual({'redis', 'memcache', 'memcached'}, {row['name'] for row in meta['package_manifest']['default_modules']})
                    self.assertNotIn('imagick', {row['name'] for row in meta['package_manifest']['default_modules']})
                    self.assertEqual(fixture.package.read_bytes(), (final / fixture.package.name).read_bytes())
        self.assertEqual(snapshots, {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in modules})

    def test_both_actual_orchestrator_collectors_keep_successful_module_artifacts_without_a_final_core(self):
        for workflow_name in ('check-updates.yml', 'build-and-dispatch.yml'):
            with self.subTest(workflow=workflow_name):
                workflow = self.workflow(workflow_name)
                step = next(s for s in workflow['jobs']['collect-macos-v2']['steps'] if s.get('id') == 'collect')
                self.assertEqual('true', step['env'].get('INCLUDE_CURRENT_RUN_MODULES'), 'The entrypoint must opt into its actual reserved module results')
                fixture = self.fixture()
                root = fixture.root / workflow_name
                root.mkdir()
                self.copy_helpers(root)
                helper = fixture.helper()
                core_record = helper._state.clean_record(fixture.identity)
                module_paths = fixture.modules()
                metas = [json.loads(Path(p).read_text()) for p in module_paths]
                rows = [core_record] + [helper._state.clean_record(meta) for meta in metas]
                state = helper._state.reserve_records({'schema_version': 1, 'units': {}}, rows, fixture.producer)['state']
                (root / 'ledger.json').write_text(json.dumps(state))
                (root / 'runtime-v2/config/build-state.json').write_text(json.dumps(state))
                artifacts, files, jobs = [], {}, []
                for index, (path, meta) in enumerate(zip(module_paths, metas), 1):
                    success = meta['name'] != 'memcache'
                    jobs.append({'name': helper._state.unit_job_name(meta), 'head_sha': fixture.producer['commit'],
                                 'status': 'completed', 'conclusion': 'success' if success else 'failure',
                                 'steps': [{'name': 'Compile Runtime v2 package', 'status': 'completed', 'conclusion': 'success' if success else 'failure'}]})
                    if not success:
                        continue
                    buffer = io.BytesIO()
                    with zipfile.ZipFile(buffer, 'w') as archive:
                        for name in ('meta.json', meta['filename'], meta['manifest_filename']):
                            archive.writestr(name, (Path(path).parent / name).read_bytes())
                    data = buffer.getvalue()
                    files[str(index)] = base64.b64encode(data).decode()
                    artifacts.append({'id': index, 'name': helper._state.artifact_name(meta), 'expired': False,
                                      'digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'expires_at': '2030-01-01T00:00:00Z',
                                      'workflow_run': {'id': int(fixture.producer['run_id']), 'head_sha': fixture.producer['commit']}})
                jobs.append({'name': helper._state.unit_job_name(core_record), 'head_sha': fixture.producer['commit'],
                             'status': 'completed', 'conclusion': 'success',
                             'steps': [{'name': 'Compile Runtime v2 package', 'status': 'completed', 'conclusion': 'success'}]})
                (root / 'api.json').write_text(json.dumps({'producer': fixture.producer, 'jobs': jobs, 'artifacts': artifacts, 'files': files}))
                gh = root / 'gh'
                gh.write_text('#!' + sys.executable + '\n' + '''import base64,json,pathlib,sys
r=pathlib.Path.cwd(); a=json.loads((r/'api.json').read_text()); endpoint=sys.argv[2]
if '--method' in sys.argv:
 p=json.load(sys.stdin);(r/'ledger.json').write_bytes(base64.b64decode(p['content']));print('{}')
elif '/contents/' in endpoint:print(json.dumps({'sha':'fixture-blob','content':base64.b64encode((r/'ledger.json').read_bytes()).decode()}))
elif endpoint.endswith('/zip'):sys.stdout.buffer.write(base64.b64decode(a['files'][endpoint.split('/')[-2]]))
elif '/jobs?' in endpoint:print(json.dumps({'jobs':a['jobs']}))
elif '/artifacts?' in endpoint:print(json.dumps({'artifacts':a['artifacts']}))
elif '/attempts/' in endpoint:print(json.dumps({'id':int(a['producer']['run_id']),'run_attempt':a['producer']['run_attempt'],'head_sha':a['producer']['commit'],'status':'in_progress'}))
else:raise SystemExit('Unexpected controlled endpoint '+endpoint)
''')
                gh.chmod(0o755)
                output, temp = root / 'github-output', root / 'runner-temp'
                env = dict(os.environ, PATH=str(root) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                           INCLUDE_CURRENT_RUN_MODULES=step['env']['INCLUDE_CURRENT_RUN_MODULES'], GITHUB_OUTPUT=str(output),
                           GITHUB_REPOSITORY='fixture/Builder', GITHUB_RUN_ID=fixture.producer['run_id'],
                           GITHUB_RUN_ATTEMPT=str(fixture.producer['run_attempt']), RUNNER_TEMP=str(temp),
                           PRODUCER_COMMIT=fixture.producer['commit'], BUILD_RECORDS_JSON=json.dumps({'include': [core_record]}))
                result = subprocess.run(['/bin/bash', '-c', step['run']], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual('2', dict(line.split('=', 1) for line in output.read_text().splitlines())['ready_count'])
                collected = json.loads((root / 'ledger.json').read_text())['units']
                self.assertNotEqual('build-ready', collected[helper._state.identity(core_record)]['status'])
                for meta in metas:
                    unit = collected[helper._state.identity(meta)]
                    self.assertEqual('build-failed' if meta['name'] == 'memcache' else 'build-ready', unit['status'])


    def test_both_host_only_entrypoints_keep_run_head_identity_without_reserving_or_compiling_a_core(self):
        head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        for name, job in [('build-macos.yml', 'plan'), ('build-and-dispatch.yml', 'plan-macos')]:
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                step = next(row for row in self.workflow(name)['jobs'][job]['steps'] if row.get('id') == 'plan')
                (root / 'runtime/ci').mkdir(parents=True)
                shutil.copy2(ROOT / 'runtime/ci/build_control.py', root / 'runtime/ci/build_control.py')
                output = root / 'github-output'
                env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                           RUNTIME_VERSION='v2', HOST_REQUEST='{"origin":"catalog-host"}', TARGETS='macos',
                           GITHUB_OUTPUT=str(output), GITHUB_SHA=head, GIT_DIR=str(ROOT / '.git'), GIT_WORK_TREE=str(root))
                result = subprocess.run(['/bin/bash', '-c', step['run']], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                self.assertEqual(head, values['commit'])
                self.assertEqual('0', values['count'])
                self.assertEqual([], json.loads(values['matrix'])['include'])
                output.write_text('')
                env['GITHUB_SHA'] = 'a' * 40
                wrong = subprocess.run(['/bin/bash', '-c', step['run']], cwd=root, env=env, capture_output=True, text=True)
                self.assertNotEqual(0, wrong.returncode)
                self.assertNotIn('count=0', output.read_text(), 'Foreign source cannot reach the host-only continuation')
