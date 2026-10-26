import copy
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from unittest import mock
import unittest
import zipfile

import test_v2_module_workflows

from test_v2_modules import BASE

ROOT = Path(__file__).resolve().parents[1]


class ModuleDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.producer = {'commit': 'a' * 40, 'run_id': '123', 'run_attempt': 2}
        self.archive = self.root / 'postgresql-18.6.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, data in {'include/server/pg_config.h': b'#define PG_VERSION "18.6"\n',
                               'share/extension/plpgsql.control': b"default_version = '1.0'\n"}.items():
                entry = tarfile.TarInfo('postgresql/18/' + name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        self.host = {'type': 'postgresql', 'series': '18', 'version': '18.6',
                     'package_sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest(), 'abi': {'major': 18}}
        self.source = self.root / 'source.tar.gz'
        self.source.write_bytes(b'actual controlled primary source input')

    def helper(self, name='module_dependencies'):
        spec = importlib.util.spec_from_file_location('tested_' + name, ROOT / 'runtime-v2/ci' / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def record(self, name, version):
        return {'runtime_version': 'v2', 'os': 'macos', 'kind': 'postgresql-extension',
                'name': name, 'recipe_name': name, 'version': version, 'build_revision': 0, 'arch': 'x86_64',
                'host': {key: copy.deepcopy(self.host[key]) for key in ('type', 'series', 'abi')}}

    def payload(self, name, version, requires=None):
        payload = self.root / (name + '-payload')
        payload.mkdir()
        (payload / 'share/extension').mkdir(parents=True)
        (payload / 'lib').mkdir()
        if name == 'pgrouting':
            actual = (ROOT / 'tests/fixtures/pgrouting-3.8.0.control').read_bytes()
            self.assertEqual('5a8621df17f4c3b270a11bad9cbd67487545362ab1e95e61008a23af63684a80', hashlib.sha256(actual).hexdigest())
            text = actual.decode().replace('${PROJECT_VERSION}', version).replace('${PROJECT_MODULE_PATHNAME}', '$libdir/pgrouting')
        else:
            text = "default_version = '" + version + "'\n"
            if requires:
                text += "requires = '" + ','.join(requires) + "'\n"
        (payload / 'share/extension' / (name + '.control')).write_text(text)
        (payload / 'share/extension' / (name + '--' + version + '.sql')).write_text('SELECT 1;\n')
        (payload / 'lib' / (name + '.so')).write_bytes(('controlled staged ' + name).encode())
        return payload

    def manifest(self, payload, name, floor='12.0'):
        return self.helper('module_manifest').create_manifest(self.host, payload, [], [name], [],
            arch='x86_64', minimum_os=floor, base=BASE, source_name=name)

    def postgis(self, floor='12.0'):
        payload = self.payload('postgis', '3.5.4', ['plpgsql'])
        return self.helper('module_manifest').write_package(self.manifest(payload, 'postgis', floor), payload,
            self.record('postgis', '3.5.4'), self.producer, {'version': '3.5.4', 'path': str(self.source)}, self.root / 'postgis')

    def routing(self):
        payload = self.payload('pgrouting', '3.8.0')
        record = self.record('pgrouting', '3.8.0')
        record['required_modules'] = {'postgis': self.record('postgis', '3.5.4')}
        return self.helper().write_candidate(self.manifest(payload, 'pgrouting'), payload, record, self.producer,
            {'version': '3.8.0', 'path': str(self.source)}, self.root / 'routing-candidate',
            {'pgrouting': ['plpgsql', 'postgis']})

    def test_real_profile_resolves_postgis_identity_from_the_same_host_without_inventing_a_hash(self):
        helper = self.helper('module_profiles')
        rows = {row['name']: row for row in helper.module_records(self.host, 'x86_64')}
        required = rows['pgrouting'].get('required_modules', {})
        self.assertEqual({'postgis'}, set(required))
        self.assertEqual('3.5.4', required['postgis']['version'])
        self.assertEqual(rows['postgis']['host'], required['postgis']['host'])
        self.assertNotIn('sha256', required['postgis'])

    def test_actual_producer_uses_real_control_to_withhold_formal_meta_until_binding(self):
        helper = self.helper('build_module')
        payload = self.payload('pgrouting', '3.8.0')
        base, work = self.root / 'base', self.root / 'work'
        base.mkdir()
        (base / '.bootstrap-v2.json').write_text(json.dumps({'runtime_version': 'v2', 'arch': 'x86_64', **BASE}))
        (work / 'src').mkdir(parents=True)
        (work / 'src/pgrouting-3.8.0.tar.gz').write_bytes(self.source.read_bytes())
        prefix = base / 'packages/postgresql/18'
        record = self.record('pgrouting', '3.8.0')
        record['required_modules'] = {'postgis': self.record('postgis', '3.5.4')}
        real_run = subprocess.run

        def stage(argv, **kwargs):
            if argv[0] == '/bin/zsh':
                shutil.copytree(payload, Path(str(argv[-1]) + str(prefix)))
                return subprocess.CompletedProcess(argv, 0)
            return real_run(argv, **kwargs)

        probe = {'host': self.host, 'install_prefix': str(prefix)}
        with mock.patch.object(helper._hosts, 'probe_postgresql_host', return_value=probe), \
                mock.patch.object(helper.subprocess, 'run', side_effect=stage), \
                mock.patch.object(helper, 'verify_native_payload'):
            output = self.root / 'producer-candidate'
            result = helper.build_module(record, self.archive, self.host['package_sha256'], '18.6', base, work,
                                         self.producer, BASE, '12.0', output)
            self.assertIn('candidate_path', result)
            self.assertFalse((output / 'meta.json').exists())
            self.assertEqual({'pgrouting': ['plpgsql', 'postgis']},
                             json.loads(Path(result['candidate_path']).read_text())['requirements'])
            for label, required in [('missing', {}), ('different', {'other_module': self.record('other_module', '1.0')})]:
                with self.subTest(profile=label):
                    record['required_modules'] = required
                    wrong = self.root / (label + '-profile-requirement')
                    with self.assertRaises(ValueError):
                        helper.build_module(record, self.archive, self.host['package_sha256'], '18.6', base, work,
                                             self.producer, BASE, '12.0', wrong)
                    self.assertFalse(wrong.exists())

    def test_actual_control_distinguishes_core_owned_bundle_and_independent_requirements(self):
        helper = self.helper()
        payload = self.payload('pgrouting', '3.8.0')
        classification = helper.classify_requirements(payload, self.archive, self.host)
        self.assertEqual({'pgrouting': ['plpgsql', 'postgis']}, classification['requires'])
        self.assertEqual(['plpgsql'], classification['core'])
        self.assertEqual(['postgis'], classification['external'])
        (payload / 'share/extension/postgis.control').write_text("default_version = '3.5.4'\n")
        bundled = helper.classify_requirements(payload, self.archive, self.host)
        self.assertEqual(['postgis'], bundled['owned'])
        self.assertEqual([], bundled['external'])
        self.archive.write_bytes(b'foreign core archive')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            helper.classify_requirements(payload, self.archive, self.host)

    def test_dependency_candidate_cannot_be_collected_as_pkg2_and_final_binding_keeps_tar_bytes(self):
        helper = self.helper()
        dependency, pending = self.postgis(), self.routing()
        candidate_path = Path(pending['candidate_path'])
        candidate = json.loads(candidate_path.read_text())
        self.assertFalse(candidate['publishable'])
        self.assertTrue(candidate['artifact_name'].startswith('candidate-module-'))
        self.assertFalse((candidate_path.parent / 'meta.json').exists())
        original = Path(pending['package']).read_bytes()
        result = helper.finalize_candidate(candidate_path, [dependency['meta_path']], [], self.producer,
                                            self.archive, self.root / 'routing-final')
        self.assertEqual(original, Path(result['package']).read_bytes())
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertEqual(1, len(manifest['dependencies']))
        reference = manifest['dependencies'][0]
        self.assertEqual(('postgis', '3.5.4', 0, dependency['meta']['sha256']),
                         (reference['name'], reference['version'], reference['build_revision'], reference['sha256']))
        self.assertEqual(self.record('postgis', '3.5.4')['host'], reference['host'])
        self.assertEqual(candidate['meta']['source'], result['meta']['source'])

    def test_existing_collector_does_not_mark_nonpublishing_candidates_build_ready(self):
        helper = self.helper()
        pending = self.routing()
        candidate = json.loads(Path(pending['candidate_path']).read_text())
        record = candidate['record']
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            for name in ('candidate.json', candidate['meta']['filename'], candidate['meta']['manifest_filename']):
                archive.writestr(name, (Path(pending['candidate_path']).parent / name).read_bytes())
        directory = self.root / 'collector'
        directory.mkdir()
        (directory / '7.zip').write_bytes(content.getvalue())
        artifacts = [{'id': 7, 'name': candidate['artifact_name'], 'expired': False,
                      'digest': 'sha256:' + hashlib.sha256(content.getvalue()).hexdigest()}]
        state = helper._state.reserve_records({'schema_version': 1, 'units': {}}, [record], self.producer)['state']
        result = helper._state.collect_results(state, [record], artifacts, self.producer, directory,
                                               {helper._state.identity(record): 'held'})
        self.assertEqual(0, result['ready_count'])
        self.assertNotEqual('build-ready', result['state']['units'][helper._state.identity(record)]['status'])

    def test_actual_workflow_resolver_keeps_candidate_and_formal_artifact_names_separate(self):
        harness = test_v2_module_workflows.ModuleWorkflowTests(methodName='runTest')
        workflow = harness.workflow('build-macos.yml')
        step = next(row for row in workflow['jobs']['module-build']['steps']
                    if row.get('name') == 'Resolve verified module Artifact name')
        dependency, pending = self.postgis(), self.routing()
        root = self.root / 'resolver'
        root.mkdir()
        harness.copy_helpers(root)
        for source, expected, name in [(Path(pending['candidate_path']).parent, pending['artifact_name'], 'candidate'),
                                      (Path(dependency['meta_path']).parent, self.helper()._state.artifact_name(dependency['meta']), 'formal')]:
            with self.subTest(metadata=name):
                temporary = root / name
                shutil.copytree(source, temporary / 'module-artifact')
                output = temporary / 'github-output'
                env = dict(os.environ, RUNNER_TEMP=str(temporary), GITHUB_OUTPUT=str(output))
                result = subprocess.run(['/bin/bash', '-c', step['run']], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual('name=' + expected, output.read_text().strip())
        self.assertTrue(pending['artifact_name'].startswith('candidate-module-'))
        self.assertFalse(pending['artifact_name'].startswith('pkg2-'))

    def test_missing_dependency_blocks_only_the_dependent_formal_artifact(self):
        helper = self.helper()
        dependency, pending = self.postgis(), self.routing()
        before = Path(dependency['package']).read_bytes()
        output = self.root / 'missing-final'
        with self.assertRaisesRegex(ValueError, 'postgis'):
            helper.finalize_candidate(pending['candidate_path'], [], [], self.producer, self.archive, output)
        self.assertFalse(output.exists())
        self.assertEqual(before, Path(dependency['package']).read_bytes())
        self.assertTrue(Path(dependency['meta_path']).is_file())

    def test_cycles_and_multiple_actual_providers_cannot_finalize_the_dependent(self):
        helper = self.helper()
        pending = self.routing()
        payload = self.payload('postgis', '3.5.4', ['plpgsql', 'pgrouting'])
        record = self.record('postgis', '3.5.4')
        record['required_modules'] = {'pgrouting': self.record('pgrouting', '3.8.0')}
        dependency = helper.write_candidate(self.manifest(payload, 'postgis'), payload, record, self.producer,
            {'version': '3.5.4', 'path': str(self.source)}, self.root / 'postgis-cycle',
            {'postgis': ['plpgsql', 'pgrouting']})
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            helper.finalize_candidate(pending['candidate_path'], [dependency['candidate_path']], [], self.producer,
                                       self.archive, self.root / 'cycle-final')
        self.assertFalse((self.root / 'cycle-final').exists())
        (payload / 'share/extension/postgis.control').write_text("default_version = '3.5.4'\nrequires = 'plpgsql'\n")
        manifest = self.manifest(payload, 'postgis')
        complete = []
        for name in ('postgis', 'alternate-provider'):
            item = self.helper('module_manifest').write_package(manifest, payload, self.record(name, '3.5.4'),
                self.producer, {'version': '3.5.4', 'path': str(self.source)}, self.root / ('complete-' + name))
            complete.append(item['meta_path'])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            helper.finalize_candidate(pending['candidate_path'], complete, [], self.producer,
                                       self.archive, self.root / 'ambiguous-final')
        self.assertFalse((self.root / 'ambiguous-final').exists())

    def test_normal_dependency_control_cannot_silently_hide_another_independent_requirement(self):
        helper = self.helper()
        dependency, pending = self.postgis(), self.routing()
        package = Path(dependency['package'])
        payload = self.root / 'postgis-payload'
        (payload / 'share/extension/postgis.control').write_text("default_version = '3.5.4'\nrequires = 'plpgsql,other_module'\n")
        bad = self.helper('module_manifest').write_package(self.manifest(payload, 'postgis'), payload,
            self.record('postgis', '3.5.4'), self.producer, {'version': '3.5.4', 'path': str(self.source)}, self.root / 'hidden')
        with self.assertRaisesRegex(ValueError, 'requirement'):
            helper.finalize_candidate(pending['candidate_path'], [bad['meta_path']], [], self.producer,
                                       self.archive, self.root / 'hidden-final')
        self.assertFalse((self.root / 'hidden-final').exists())
        self.assertTrue(package.exists())

    def test_actual_host_only_pipeline_downloads_the_pinned_core_without_relabelling_its_producer(self):
        helper = self.helper('module_pipeline')
        dependency, pending = self.postgis(), self.routing()
        candidate = json.loads(Path(pending['candidate_path']).read_text())
        unit = copy.deepcopy(candidate['record'])
        unit['request'] = {'record': candidate['record'], 'download_url': 'https://catalog.example.invalid/pg18.tar.gz',
                           'host_sha256': self.host['package_sha256'], 'host_version': '18.6',
                           'host_filename': self.archive.name, 'base': BASE, 'minimum_os': '12.0'}
        plan = {'include': [unit], 'external_candidates': []}
        work = self.root / 'host-only'
        work.mkdir()
        def download(request, timeout):
            if (not isinstance(request, helper.urllib.request.Request)
                    or request.get_header('User-agent') != 'ServBay-PKG-Builder'):
                raise helper.urllib.error.HTTPError(unit['request']['download_url'], 403, 'Forbidden', {}, None)
            self.assertEqual(120, timeout)
            return io.BytesIO(self.archive.read_bytes())
        with mock.patch.object(helper, 'fetch_module_artifacts', return_value=[pending['candidate_path'], dependency['meta_path']]), \
                mock.patch.object(helper.urllib.request, 'urlopen', side_effect=download) as read:
            result = helper.finalize_requested_module(unit, plan, self.producer, 'fixture/Builder', work,
                                                       self.root / 'no-new-core', self.root / 'host-only-final')
        self.assertEqual(unit['request']['download_url'], read.call_args.args[0].full_url)
        self.assertEqual(self.producer, result['meta']['producer'])
        self.assertEqual(self.host['package_sha256'],
                         json.loads(Path(result['manifest']).read_text())['host']['package_sha256'])
        self.assertFalse((self.root / 'no-new-core').exists())
        for defect in ('http', 'sha'):
            failure = helper.urllib.error.HTTPError(unit['request']['download_url'], 403, 'Forbidden', {}, None)
            with self.subTest(defect=defect), \
                    mock.patch.object(helper, 'fetch_module_artifacts', return_value=[pending['candidate_path'], dependency['meta_path']]), \
                    mock.patch.object(helper.urllib.request, 'urlopen', side_effect=failure if defect == 'http' else None,
                                      return_value=io.BytesIO(b'wrong host bytes')), \
                    self.assertRaises(helper.urllib.error.HTTPError if defect == 'http' else ValueError):
                helper.finalize_requested_module(unit, plan, self.producer, 'fixture/Builder', work,
                                                 self.root / 'no-new-core', self.root / ('failed-final-' + defect))
            self.assertFalse((self.root / ('failed-final-' + defect)).exists())

    def test_external_dependency_keeps_its_real_hash_and_requires_uploader_confirmation(self):
        helper = self.helper()
        dependency, pending = self.postgis('13.0'), self.routing()
        manifest = json.loads(Path(dependency['manifest']).read_text())
        manifest['host']['version'] = '18.5'
        external = {'reference': {key: dependency['meta'][key] for key in ('kind', 'name', 'version', 'build_revision', 'sha256', 'host')},
                    'module_manifest': manifest, 'publication_status': 'requires_uploader_confirmation'}
        result = helper.finalize_candidate(pending['candidate_path'], [], [external], self.producer,
                                            self.archive, self.root / 'external-final')
        self.assertEqual('13.0', result['meta']['minimum_os'])
        self.assertEqual([external['reference']], result['requires_uploader_confirmation'])
        self.assertNotIn('published', result['meta'])

    def test_cached_requirements_cannot_replace_the_actual_control_readback(self):
        helper = self.helper()
        dependency, pending = self.postgis(), self.routing()
        path = Path(pending['candidate_path'])
        candidate = json.loads(path.read_text())
        candidate['requirements'] = {'pgrouting': ['plpgsql']}
        path.write_text(json.dumps(candidate))
        output = self.root / 'erased-requirement-final'
        with self.assertRaisesRegex(ValueError, 'Actual control requirements changed'):
            helper.finalize_candidate(path, [dependency['meta_path']], [], self.producer, self.archive, output)
        self.assertFalse(output.exists())

    def test_external_compatibility_cannot_claim_protected_publication(self):
        helper = self.helper()
        dependency, pending = self.postgis(), self.routing()
        external = {'reference': {key: dependency['meta'][key] for key in ('kind', 'name', 'version', 'build_revision', 'sha256', 'host')},
                    'module_manifest': json.loads(Path(dependency['manifest']).read_text()),
                    'publication_status': 'requires_uploader_confirmation', 'published': True}
        output = self.root / 'false-publication-final'
        with self.assertRaisesRegex(ValueError, 'does not prove protected publication'):
            helper.finalize_candidate(pending['candidate_path'], [], [external], self.producer, self.archive, output)
        self.assertFalse(output.exists())

    def test_foreign_producer_abi_version_and_modified_tar_never_yield_formal_metadata(self):
        helper = self.helper()
        dependency, pending = self.postgis(), self.routing()
        meta_path, manifest_path = Path(dependency['meta_path']), Path(dependency['manifest'])
        original_meta, original_manifest = meta_path.read_bytes(), manifest_path.read_bytes()
        for defect in ('producer', 'abi', 'version', 'tar'):
            with self.subTest(defect=defect):
                meta, manifest = json.loads(original_meta), json.loads(original_manifest)
                package = Path(dependency['package'])
                original_package = package.read_bytes()
                if defect == 'producer':meta['producer']['run_attempt'] += 1
                elif defect == 'abi':
                    meta['host']['abi']['major'] = manifest['host']['abi']['major'] = 17
                elif defect == 'version':meta['version'] = '3.5.3'
                else:package.write_bytes(b'changed archive')
                manifest_path.write_text(json.dumps(manifest))
                meta['manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                meta_path.write_text(json.dumps(meta))
                output = self.root / ('foreign-' + defect)
                try:
                    with self.assertRaises(ValueError):
                        helper.finalize_candidate(pending['candidate_path'], [meta_path], [], self.producer, self.archive, output)
                    self.assertFalse(output.exists())
                finally:
                    meta_path.write_bytes(original_meta)
                    manifest_path.write_bytes(original_manifest)
                    package.write_bytes(original_package)

    def test_actual_workflow_finalizer_binds_exact_same_run_artifacts_and_keeps_partial_success(self):
        workflow = test_v2_module_workflows.ModuleWorkflowTests(methodName='runTest').workflow('build-macos.yml')
        job = workflow['jobs']['finalize-modules']
        self.assertEqual('ubuntu-latest', job['runs-on'])
        step = next(row for row in job['steps'] if row.get('name') == 'Bind exact module dependencies')
        dependency, pending = self.postgis(), self.routing()
        candidate = json.loads(Path(pending['candidate_path']).read_text())
        unit = copy.deepcopy(candidate['record'])
        unit['request'] = {'record': candidate['record'], 'candidate_artifact': 'candidate-core-macos-x86_64-postgresql-18.6',
                           'host_sha256': self.host['package_sha256'], 'host_version': '18.6',
                           'host_filename': self.archive.name, 'base': BASE, 'minimum_os': '12.0'}
        root = self.root / 'workflow'
        root.mkdir()
        test_v2_module_workflows.ModuleWorkflowTests(methodName='runTest').copy_helpers(root)
        host_dir = root / 'host'
        host_dir.mkdir()
        shutil.copy2(self.archive, host_dir / self.archive.name)
        (host_dir / 'candidate.json').write_text(json.dumps({'artifact_name': unit['request']['candidate_artifact'],
            'meta': {'producer': self.producer, 'sha256': self.host['package_sha256']}}))
        plan_path = root / 'module-plan.json'
        plan_path.write_text(json.dumps({'include': [self.record('postgis', '3.5.4'), unit], 'external_candidates': []}))
        artifacts, content = [], {}
        for index, (name, path, metadata_name) in enumerate([
                (self.helper('module_manifest')._state.artifact_name(dependency['meta']), Path(dependency['meta_path']), 'meta.json'),
                (candidate['artifact_name'], Path(pending['candidate_path']), 'candidate.json')], 1):
            data = io.BytesIO()
            meta = dependency['meta'] if metadata_name == 'meta.json' else candidate['meta']
            with zipfile.ZipFile(data, 'w') as archive:
                for filename in [metadata_name, meta['filename'], meta['manifest_filename']]:
                    archive.writestr(filename, (path.parent / filename).read_bytes())
            content[str(index)] = base64.b64encode(data.getvalue()).decode()
            artifacts.append({'id': index, 'name': name, 'expired': False,
                'digest': 'sha256:' + hashlib.sha256(data.getvalue()).hexdigest(),
                'workflow_run': {'id': int(self.producer['run_id']), 'head_sha': self.producer['commit']}})
        gh = root / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + '''import base64,json,pathlib,sys
r=pathlib.Path.cwd();v=json.loads((r/'api.json').read_text());endpoint=sys.argv[2]
if endpoint.endswith('/zip'):sys.stdout.buffer.write(base64.b64decode(v['content'][endpoint.split('/')[-2]]))
elif '/artifacts?' in endpoint:print(json.dumps({'artifacts':v['artifacts']}))
elif '/jobs?' in endpoint:print(json.dumps({'jobs':[]}))
elif '/attempts/' in endpoint:print(json.dumps({'id':123,'run_attempt':2,'head_sha':'a'*40,'status':'in_progress'}))
else:raise SystemExit('Unexpected controlled API '+endpoint)
''')
        gh.chmod(0o755)
        original_tar = Path(pending['package']).read_bytes()
        for missing in (True, False):
            with self.subTest(missing_postgis=missing):
                (root / 'api.json').write_text(json.dumps({'artifacts': artifacts[1:] if missing else artifacts, 'content': content}))
                output, temp = root / ('missing' if missing else 'ready'), root / ('tmp-missing' if missing else 'tmp-ready')
                env = dict(os.environ, PATH=str(root) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
                    UNIT_JSON=json.dumps(unit), MODULE_PLAN_FILE=str(plan_path), HOST_DIR=str(host_dir), FINAL_MODULE_DIR=str(output),
                    PRODUCER_COMMIT=self.producer['commit'], GITHUB_RUN_ID=self.producer['run_id'], GITHUB_RUN_ATTEMPT='2',
                    GITHUB_REPOSITORY='fixture/Builder', RUNNER_TEMP=str(temp), GITHUB_OUTPUT=str(root / 'github-output'))
                result = subprocess.run(['/bin/bash', '-c', step['run']], cwd=root, env=env, capture_output=True, text=True)
                if missing:
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn('Missing exact independent module dependency', result.stderr)
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    meta = json.loads((output / 'meta.json').read_text())
                    manifest = json.loads((output / meta['manifest_filename']).read_text())
                    self.assertEqual(dependency['meta']['sha256'], manifest['dependencies'][0]['sha256'])
                    self.assertEqual(original_tar, (output / meta['filename']).read_bytes())
                self.assertTrue(Path(dependency['meta_path']).is_file())
