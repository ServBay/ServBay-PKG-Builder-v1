import importlib.util
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re
import shutil
import subprocess
import sys
from unittest import mock
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


macos_update = load_module("macos_update", "runtime/update_packages.py")
windows_update = load_module("windows_update", "runtime-windows/update_packages.py")


class UpdateMatrixTests(unittest.TestCase):
    def test_macos_matrix_expands_each_package_to_two_arches(self):
        updater = macos_update.PackageUpdater(dry_run=True)

        matrix = updater.build_matrix([
            {"name": "nginx", "version": "1.29.0"},
        ])

        self.assertEqual(
            matrix,
            {
                "include": [
                    {"os": "macos", "arch": "x86_64", "name": "nginx", "version": "1.29.0"},
                    {"os": "macos", "arch": "arm64", "name": "nginx", "version": "1.29.0"},
                ]
            },
        )

    def test_windows_matrix_uses_x64_for_first_automation_phase(self):
        updater = windows_update.WindowsPackageUpdater(dry_run=True)

        matrix = updater.build_matrix([
            {"name": "ollama", "version": "0.21.2"},
        ])

        self.assertEqual(
            matrix,
            {
                "include": [
                    {
                        "os": "windows",
                        "arch": "x64",
                        "name": "ollama",
                        "version": "0.21.2",
                        "version_type": "stable",
                    },
                ]
            },
        )

    def test_macos_active_package_records_ignore_commented_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "packages.conf"
            conf.write_text(
                "# nginx\t1.28.0\tnginx-1.28.0.tar.gz\tnginx-1.28.0.tar.gz\n"
                "nginx\t1.29.0\tnginx-1.29.0.tar.gz\tnginx-1.29.0.tar.gz\n",
                encoding="utf-8",
            )
            updater = macos_update.PackageUpdater(conf_file=str(conf), dry_run=True)

            records = updater.active_package_records()

            self.assertEqual(records, [{"name": "nginx", "version": "1.29.0"}])

    def test_windows_active_package_records_ignore_header_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "windows-packages.txt"
            conf.write_text(
                "Package\tVersion\tFilename\n"
                "# ollama\t0.20.0\n"
                "ollama\t0.21.2\n",
                encoding="utf-8",
            )
            updater = windows_update.WindowsPackageUpdater(conf_file=str(conf), dry_run=True)

            records = updater.active_package_records()

            self.assertEqual(records, [{"name": "ollama", "version": "0.21.2"}])


class SelectedV2UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / 'packages.conf'
        self.other = 'caddy  2.10.0  caddy-old.tar.gz  caddy-old.tar.gz  source\n'
        self.conf.write_text('nginx\t1.31.8\tnginx-1.31.8.tar.gz\tnginx-1.31.8.tar.gz\tsource\n' + self.other)
        self.state = load_module('selected_state_test', 'runtime/ci/build_state.py')

    def updater(self, package='nginx'):
        try:
            return macos_update.PackageUpdater(str(self.conf), runtime_version='v2', package=package)
        except TypeError:
            self.fail('The existing updater does not yet accept a controlled package selection')

    def refresh(self, updater, newest='1.32.0'):
        def github(url, headers=None):
            if url == 'https://api.github.com/repos/nginx/nginx/releases/latest':
                return {'tag_name': 'release-' + newest}
            raise AssertionError('Unselected upstream was contacted: ' + url)
        output = Path(self.tmp.name) / 'matrix.json'
        with mock.patch.object(updater, 'get_github_api', side_effect=github), mock.patch.object(macos_update.requests, 'get', side_effect=AssertionError('Unexpected upstream request')), mock.patch.object(macos_update.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            updater.run(emit_json_file=output, all_packages=True)
        return json.loads(output.read_text())['include']

    def test_selected_nginx_discovers_dynamic_version_and_preserves_other_profile_bytes(self):
        rows = self.refresh(self.updater())
        self.assertEqual([('nginx', '1.32.0', 'x86_64'), ('nginx', '1.32.0', 'arm64')], [(x['name'], x['version'], x['arch']) for x in rows])
        self.assertIn('nginx\t1.32.0\tnginx-1.32.0.tar.gz\tnginx-1.32.0.tar.gz\tsource\n', self.conf.read_text())
        self.assertEqual([self.other], [line for line in self.conf.read_text().splitlines(keepends=True) if line.startswith('caddy')])

    def test_unchanged_selected_ready_version_does_not_rebuild_or_schedule_other_pending_units(self):
        rows = self.refresh(self.updater(), newest='1.31.8')
        state = self.state.plan_pending({'schema_version': 1, 'units': {}}, rows)['state']
        for row in rows:
            state = self.state.record_result(state, self.state.identity(row), {'status': 'build-ready', 'producer': {'commit': 'a' * 40, 'run_id': '123', 'run_attempt': 1}, 'artifact_id': '321', 'artifact_name': self.state.artifact_name(row), 'artifact_zip_sha256': 'b' * 64, 'package_sha256': 'c' * 64, 'artifact_expires_at': '2026-11-09T00:00:00Z'})
        pending = self.state.plan_pending(state, rows)
        self.assertEqual([], pending['include'])
        self.assertEqual({'nginx'}, {x['name'] for x in rows})

    def test_explicit_other_package_and_empty_selection_preserve_existing_profiles(self):
        updater = self.updater('caddy')
        with contextlib.redirect_stdout(io.StringIO()):
            updater.load_packages()
            updater.update_conf_file({'nginx': '9.9.9', 'caddy': '2.11.0'})
        self.assertIn('nginx\t1.31.8\t', self.conf.read_text())
        self.assertNotIn('9.9.9', self.conf.read_text())
        self.assertEqual(['caddy'], [x['name'] for x in updater.active_package_records()])
        empty = self.updater('')
        self.assertEqual({'nginx', 'caddy'}, {x['name'] for x in empty.active_package_records()})

    def test_v1_default_retains_matrix_and_rejects_v2_only_selector(self):
        self.assertEqual('v1', macos_update.PackageUpdater().runtime_version)
        with self.assertRaises(ValueError):
            macos_update.PackageUpdater(str(self.conf), package='nginx')

    def test_selected_reservation_scope_preserves_other_expired_owner_and_active_target_gate(self):
        target = dict(runtime_version='v2', os='macos', kind='package', name='nginx', version='1.32.0', arch='arm64', build_revision=0)
        other = dict(target, name='caddy', version='2.11.0')
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        owner = dict(commit='a' * 40, run_id='123', run_attempt=1)
        new = dict(commit='b' * 40, run_id='456', run_attempt=1)
        initial = self.state.reserve_records({'schema_version': 1, 'units': {}}, [target, other], owner, now=now - timedelta(hours=10))['state']
        initial['units'][self.state.identity(target)]['reservation']['expires_at'] = (now + timedelta(hours=1)).isoformat()
        before = copy.deepcopy(initial['units'][self.state.identity(other)])
        writes = []
        def read(): return 'fixture', copy.deepcopy(initial)
        def write(sha, state): writes.append(state)
        try:
            with mock.patch.object(self.state, 'github_owner_snapshot', side_effect=AssertionError('Non-target expired owner must not be read')):
                result = self.state.reserve_github([target], new, 'fixture/repo', Path(self.tmp.name), read_state=read, write_state=write, now=now, reconcile_records=[target])
        except TypeError:
            self.fail('Scoped reservation reconciliation is not yet supported')
        self.assertEqual([], result['include'])
        self.assertEqual([self.state.identity(target)], result['held'])
        self.assertEqual(before, result['state']['units'][self.state.identity(other)])
        self.assertEqual(owner, result['state']['units'][self.state.identity(target)]['reservation']['producer'])

    def test_selected_expired_target_uses_existing_terminal_owner_rules(self):
        target = dict(runtime_version='v2', os='macos', kind='package', name='nginx', version='1.32.0', arch='arm64', build_revision=0)
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        old = dict(commit='a' * 40, run_id='123', run_attempt=1)
        new = dict(commit='b' * 40, run_id='456', run_attempt=1)
        state = self.state.reserve_records({'schema_version': 1, 'units': {}}, [target], old, now=now - timedelta(hours=10))['state']
        snapshot = {'run': {'id': 123, 'run_attempt': 1, 'head_sha': 'a' * 40, 'status': 'completed'}, 'jobs': [], 'artifacts': [], 'artifact_dir': str(Path(self.tmp.name))}
        read_owner = lambda producer: snapshot
        default = self.state.reconcile_reservations(state, new, read_owner, now)
        try:
            selected = self.state.reconcile_reservations(state, new, read_owner, now, records=[target])
        except TypeError:
            self.fail('Scoped reconciliation cannot yet preserve the original expiry rules')
        self.assertEqual(default, selected)
        self.assertNotIn('reservation', selected['state']['units'][self.state.identity(target)])


    def test_selected_scope_keeps_three_failure_and_invalid_producer_gates(self):
        target = dict(runtime_version='v2', os='macos', kind='package', name='nginx', version='1.32.0', arch='arm64', build_revision=0)
        producer = dict(commit='a' * 40, run_id='123', run_attempt=1)
        state = self.state.plan_pending({'schema_version': 1, 'units': {}}, [target])['state']
        state['units'][self.state.identity(target)]['consecutive_failures'] = 3
        result = self.state.reserve_github([target], producer, 'fixture/repo', Path(self.tmp.name), read_state=lambda: ('fixture', state), write_state=lambda sha, updated: None, reconcile_records=[target])
        self.assertEqual([], result['include'])
        self.assertEqual(3, result['state']['units'][self.state.identity(target)]['consecutive_failures'])
        with self.assertRaises(ValueError):
            self.state.reserve_github([target], dict(producer, commit='invalid'), 'fixture/repo', Path(self.tmp.name), read_state=lambda: ('fixture', state), write_state=lambda sha, updated: None, reconcile_records=[target])


class SelectedV2WorkflowTests(unittest.TestCase):
    def job_text(self, name, workflow="check-updates.yml"):
        text = (ROOT / '.github/workflows' / workflow).read_text()
        matches = re.findall(r'^  ' + re.escape(name) + r':\n(.*?)(?=^  [A-Za-z][A-Za-z0-9_-]*:|\Z)', text, re.M | re.S)
        self.assertEqual(1, len(matches))
        return matches[0]

    def step_run(self, job, field=None, value=None, workflow="check-updates.yml"):
        runs = []
        for block in re.split(r'^      - ', self.job_text(job, workflow), flags=re.M)[1:]:
            if field is not None and not re.search(r'^(?:        )?' + re.escape(field + ': ' + value) + r'$', block, re.M):
                continue
            lines = block.splitlines()
            for index, line in enumerate(lines):
                if not line.startswith('        run: '):
                    continue
                script = line[len('        run: '):]
                if script == '|':
                    body = []
                    for following in lines[index + 1:]:
                        if following and not following.startswith('          '):
                            break
                        body.append(following[10:] if following else '')
                    script = '\n'.join(body)
                runs.append(script)
        self.assertEqual(1, len(runs))
        return runs[0]

    def setup_plan_fixture(self, root, rows):
        for relative in ('runtime/update_packages.py', 'runtime/ci/build_profile.py', 'runtime/ci/build_state.py', 'runtime/ci/build_control.py', 'runtime/ci/reserve_builds.sh', 'runtime-v2/ci/php_source.py'):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        (root / 'runtime-v2/config').mkdir(parents=True)
        packages = {(row['name'], row['version']) for row in rows}
        (root / 'runtime-v2/config/packages.conf').write_text(''.join(f'{name}\t{version}\t{name}-{version}.tar.gz\t{name}-{version}.tar.gz\tsource\n' for name, version in packages))
        (root / 'ledger.json').write_text(json.dumps({'schema_version': 1, 'units': {}}))
        shutil.copy2(root / 'ledger.json', root / 'runtime-v2/config/build-state.json')
        (root / 'gh').write_text('#!' + sys.executable + '\n' + """import base64,json,pathlib,sys
p=pathlib.Path('ledger.json')
if '--method' in sys.argv:
    payload=json.load(sys.stdin)
    p.write_bytes(base64.b64decode(payload['content']))
    print('{}')
else:
    print(json.dumps({'sha':'fixture-blob','content':base64.b64encode(p.read_bytes()).decode()}))
""")
        (root / 'gh').chmod(0o755)
        head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        return dict(PENDING_ONLY='true', CONFIG_CHANGED='false', GITHUB_SHA=head, GITHUB_RUN_ID='12345', GITHUB_RUN_ATTEMPT='1', GITHUB_REPOSITORY='fixture/repo', RUNNER_TEMP=str(root / 'tmp'))

    def test_selected_nginx_refresh_executes_real_updater_without_other_discovery_or_modules(self):
        script = self.step_run('check', 'name', 'Check package updates')
        rows = [dict(name='nginx', version='1.31.8'), dict(name='caddy', version='2.10.0')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_plan_fixture(root, rows)
            before = (root / 'runtime-v2/config/packages.conf').read_text()
            (root / 'sitecustomize.py').write_text("""import requests,time
from types import SimpleNamespace
def get(url, **kwargs):
    if url != 'https://api.github.com/repos/nginx/nginx/releases/latest':
        raise AssertionError('Unselected discovery: ' + url)
    return SimpleNamespace(status_code=200,json=lambda:{'tag_name':'release-1.32.0'})
requests.get=get
time.sleep=lambda seconds:None
""")
            env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'], PYTHONPATH=str(root), PKG='nginx', TARGET='macos', RUNTIME_VERSION='v2', FULL_REBUILD='false', PENDING_ONLY='false', GITHUB_TOKEN='fixture')
            bash = '/opt/homebrew/bin/bash' if Path('/opt/homebrew/bin/bash').exists() else '/bin/bash'
            result = subprocess.run([bash, '-c', script], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            desired = json.loads((root / 'macos-desired.json').read_text())['include']
            self.assertEqual([('nginx', '1.32.0', 'x86_64'), ('nginx', '1.32.0', 'arm64')], [(x['name'], x['version'], x['arch']) for x in desired])
            self.assertEqual([x for x in before.splitlines() if x.startswith('caddy')], [x for x in (root / 'runtime-v2/config/packages.conf').read_text().splitlines() if x.startswith('caddy')])
            self.assertFalse((root / 'module-updates.json').exists())

    def test_selected_plan_filters_old_pending_other_packages_and_keeps_collector_without_modules(self):
        script = self.step_run('check', 'id', 'plan')
        rows = [dict(runtime_version='v2', os='macos', kind='package', name=name, version=version, arch=arch, build_revision=0, build_type='source') for name, version in [('nginx','1.32.0'), ('caddy','2.11.0')] for arch in ('x86_64','arm64')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_nginx = dict(rows[0], version='1.29.5')
            fixture = self.setup_plan_fixture(root, rows + [old_nginx])
            (root / 'macos-matrix.json').write_text(json.dumps({'include': rows + [old_nginx]}))
            (root / 'macos-desired.json').write_text(json.dumps({'include': [x for x in rows if x['name'] == 'nginx']}))
            (root / 'windows-matrix.json').write_text('{"include": []}')
            output = root / 'github-output'
            env = dict(os.environ, **fixture, PKG='nginx', RUNTIME_VERSION='v2', CONTINUE_BUILD='true', UPLOAD_DRY_RUN='true', GITHUB_OUTPUT=str(output), PATH=str(root)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'], GIT_DIR=os.environ.get('GIT_DIR', str(ROOT/'.git')), GIT_WORK_TREE=str(root))
            result = subprocess.run(['/bin/bash','-c',script],cwd=root,env=env,capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr)
            values = dict(line.split('=',1) for line in output.read_text().splitlines())
            self.assertEqual('false',values['allow_modules'])
            self.assertEqual([('nginx','1.32.0','all')],[(x['name'],x['version'],x['arch']) for x in json.loads(values['macos_matrix'])['include']])
            units = json.loads((root/'ledger.json').read_text())['units']
            self.assertEqual({'nginx'},{x['record']['name'] for x in units.values() if 'reservation' in x})
            collector_if = next(line[8:] for line in self.job_text('collect-macos-v2').splitlines() if line.startswith('    if: '))
            expression = collector_if.removeprefix('${{').removesuffix('}}').replace('always()', 'True')
            for key,value in {'needs.check.outputs.runtime_version':'v2','needs.check.outputs.allow_modules':'false','needs.check.outputs.macos_count':'1'}.items():
                expression = expression.replace(key,repr(value))
            self.assertTrue(eval(expression.replace('&&',' and ').replace('||',' or '),{'__builtins__':{}},{}))

    def test_selected_continuation_carries_package_and_does_not_enable_full_rebuild(self):
        script = self.step_run('continue-pending-v2')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'runtime/ci').mkdir(parents=True)
            shutil.copy2(ROOT/'runtime/ci/build_control.py',root/'runtime/ci/build_control.py')
            (root/'gh').write_text('#!'+sys.executable+'\nimport pathlib,sys\npathlib.Path("request.json").write_text(sys.stdin.read())\n')
            (root/'gh').chmod(0o755)
            env=dict(os.environ,PKG='nginx',DRY_RUN='true',GITHUB_REPOSITORY='fixture/repo',PATH=str(root)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'])
            result=subprocess.run(['/bin/bash','-c',script],cwd=root,env=env,capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr)
            body=json.loads((root/'request.json').read_text())
            self.assertEqual('main',body['ref'])
            self.assertEqual('nginx',body['inputs'].get('pkg'))
            self.assertEqual('false',body['inputs']['full_rebuild'])
            self.assertEqual('true',body['inputs']['pending_only'])

    def test_selected_mode_is_v2_manual_only_and_preserves_default_build_switches(self):
        script = self.step_run('check', 'id', 'mode')
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'output'
            for runtime,selected,expected_code in [('v2','nginx',0),('v1','nginx',1),('v1','',0)]:
                output.write_text('')
                env=dict(os.environ,GITHUB_OUTPUT=str(output),EVENT_NAME='workflow_dispatch',INPUT_RUNTIME_VERSION=runtime,INPUT_TARGET='macos',INPUT_PKG=selected,INPUT_CONTINUE_BUILD='false',INPUT_UPLOAD_DRY_RUN='true',INPUT_FULL_REBUILD='false',INPUT_PENDING_ONLY='false')
                result=subprocess.run(['/bin/bash','-c',script],env=env,capture_output=True,text=True)
                if expected_code:
                    self.assertNotEqual(0,result.returncode)
                else:
                    self.assertEqual(0,result.returncode,result.stderr)
                    values=dict(line.split('=',1) for line in output.read_text().splitlines())
                    self.assertEqual(selected,values.get('pkg',''))
                    self.assertEqual('false',values['continue_build'])
                    self.assertEqual('false',values['full_rebuild'])


    def reservation_chain(self, selected=True, runtime='v2'):
        state_module = load_module('reservation_chain_state', 'runtime/ci/build_state.py')
        rows = [dict(runtime_version='v2', os='macos', kind='package', name='nginx', version='1.32.0', arch=arch, build_revision=0, build_type='source') for arch in ('x86_64', 'arm64')]
        unrelated = [dict(rows[0], name='caddy', version='2.11.0'), dict(rows[0], version='1.29.5')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = self.setup_plan_fixture(root, rows + unrelated)
            old = dict(commit='c' * 40, run_id='999', run_attempt=1)
            initial = state_module.reserve_records({'schema_version': 1, 'units': {}}, unrelated, old, now=datetime(2020, 1, 1, tzinfo=timezone.utc))['state']
            (root / 'ledger.json').write_text(json.dumps(initial))
            (root / 'runtime-v2/config/build-state.json').write_text(json.dumps(initial))
            (root / 'gh').write_text('#!' + sys.executable + '\n' + """import base64,json,pathlib,sys
p=pathlib.Path('ledger.json')
endpoint=next(x for x in sys.argv if x.startswith('repos/'))
with pathlib.Path('requests.jsonl').open('a') as out:out.write(json.dumps(endpoint)+'\\n')
if '/actions/runs/999/attempts/1/jobs' in endpoint:
    print(json.dumps({'jobs':[]}))
elif '/actions/runs/999/attempts/1' in endpoint:
    print(json.dumps({'id':999,'run_attempt':1,'head_sha':'c'*40,'status':'completed'}))
elif '/actions/runs/999/artifacts' in endpoint:
    print(json.dumps({'artifacts':[]}))
elif '--method' in sys.argv:
    payload=json.load(sys.stdin);p.write_bytes(base64.b64decode(payload['content']));print('{}')
else:
    print(json.dumps({'sha':'fixture-blob','content':base64.b64encode(p.read_bytes()).decode()}))
""")
            (root / 'gh').chmod(0o755)
            output = root / 'github-output'
            env = dict(os.environ, **fixture, PKG='nginx', RUNTIME_VERSION=runtime, CONTINUE_BUILD='true', UPLOAD_DRY_RUN='true', GITHUB_OUTPUT=str(output), PATH=str(root)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'], GIT_DIR=os.environ.get('GIT_DIR', str(ROOT/'.git')), GIT_WORK_TREE=str(root))
            if selected:
                (root / 'macos-matrix.json').write_text(json.dumps({'include': rows + unrelated}))
                (root / 'macos-desired.json').write_text(json.dumps({'include': rows}))
                (root / 'windows-matrix.json').write_text('{"include":[]}')
                parent = subprocess.run(['/bin/bash', '-c', self.step_run('check', 'id', 'plan')], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(0, parent.returncode, parent.stderr)
                parent_records = json.loads((root / 'macos-matrix.json').read_text())['include']
                self.assertEqual([('nginx', '1.32.0', 'x86_64'), ('nginx', '1.32.0', 'arm64')], [(x['name'], x['version'], x['arch']) for x in parent_records])
                parent_state = json.loads((root / 'ledger.json').read_text())
                for row in unrelated:
                    self.assertEqual(initial['units'][state_module.identity(row)], parent_state['units'][state_module.identity(row)])
            # Resolve the real parent with/input and real producer env binding, not a test-only scope flag.
            def evaluate(value, bindings):
                if value in ('true','false'):return value == 'true'
                expression = value.removeprefix('${{').removesuffix('}}').strip()
                for key, replacement in bindings.items():expression = expression.replace(key, repr(replacement))
                return eval(expression, {'__builtins__':{}}, {})
            producer_text = (ROOT / '.github/workflows/build-macos.yml').read_text().split('  workflow_call:', 1)[1]
            scope_input = re.search(r'^      scope_reservations:\n((?:        .*\n)+)', producer_text, re.M)
            default = re.search(r'^        default: (.+)$', scope_input.group(1), re.M).group(1) if scope_input else 'false'
            scope = evaluate(default, {})
            if selected:
                caller = re.search(r'^      scope_reservations: (.+)$', self.job_text('build-macos'), re.M)
                if caller:scope = evaluate(caller.group(1), {'needs.check.outputs.pkg':'nginx'})
            child_job = self.job_text('plan', 'build-macos.yml')
            binding = re.search(r'^          SCOPE_RESERVATIONS: (.+)$', child_job, re.M)
            if binding:env['SCOPE_RESERVATIONS'] = str(evaluate(binding.group(1), {'inputs.scope_reservations':scope})).lower()
            output.write_text('')
            env.update(VERSION='1.32.0', ARCH='all', REVISION='0', HOST_REQUEST='')
            child = subprocess.run(['/bin/bash', '-c', self.step_run('plan', 'id', 'plan', workflow='build-macos.yml')], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(0, child.returncode, child.stdout + child.stderr)
            after = json.loads((root / 'ledger.json').read_text())
            queries = (root / 'requests.jsonl').read_text().splitlines() if (root / 'requests.jsonl').exists() else []
            owner_reads = sum('/actions/runs/999/attempts/1' in q and '/jobs' not in q for q in queries)
            matrix = json.loads((root / 'matrix.json').read_text())['include']
            return initial, after, unrelated, owner_reads, matrix

    def test_selected_parent_to_producer_reservation_chain_never_reads_or_changes_other_expired_owners(self):
        before, after, unrelated, reads, matrix = self.reservation_chain()
        state = load_module('chain_identity', 'runtime/ci/build_state.py')
        self.assertEqual(0, reads)
        for row in unrelated:self.assertEqual(before['units'][state.identity(row)], after['units'][state.identity(row)])
        self.assertEqual([('nginx','1.32.0','x86_64'), ('nginx','1.32.0','arm64')], [(x['name'],x['version'],x['arch']) for x in matrix])

    def test_unscoped_producer_keeps_original_full_ledger_reconciliation(self):
        before, after, unrelated, reads, matrix = self.reservation_chain(selected=False)
        state = load_module('default_chain_identity', 'runtime/ci/build_state.py')
        self.assertEqual(1, reads)
        for row in unrelated:self.assertNotIn('reservation', after['units'][state.identity(row)])
        self.assertEqual(2, len(matrix))

    def test_v1_producer_keeps_its_path_without_any_ledger_reservation(self):
        before, after, unrelated, reads, matrix = self.reservation_chain(selected=False, runtime='v1')
        self.assertEqual(before, after)
        self.assertEqual(0, reads)
        self.assertEqual(2, len(matrix))


if __name__ == "__main__":
    unittest.main()
