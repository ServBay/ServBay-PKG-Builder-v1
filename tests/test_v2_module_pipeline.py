import copy
import hashlib
import importlib.util
import json
import os
import shlex
import sys
import tarfile
import io
import zipfile
import test_v2_modules
import test_v2_core_manifest
from pathlib import Path
import tempfile
from unittest import mock
import unittest

from test_v2_modules import BASE, PHP_ABI

ROOT = Path(__file__).resolve().parents[1]


class ModulePipelineTests(unittest.TestCase):
    def helper(self):
        spec = importlib.util.spec_from_file_location('tested_module_pipeline', ROOT / 'runtime-v2/ci/module_pipeline.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper

    def producer(self):
        return {'commit': 'a' * 40, 'run_id': '123', 'run_attempt': 2}

    def candidate(self):
        host = {'type': 'php', 'series': '8.4', 'version': '8.4.27', 'package_sha256': 'c' * 64, 'abi': copy.deepcopy(PHP_ABI)}
        return {'schema_version': 1, 'publishable': False, 'host': host,
                'artifact_name': 'candidate-core-macos-x86_64-php-8.4.27',
                'meta': {'name': 'php', 'version': '8.4.27', 'arch': 'x86_64', 'producer': self.producer(),
                         'minimum_os': '12.0', 'base': copy.deepcopy(BASE), 'filename': 'php-8.4.27.tar.gz', 'sha256': 'c' * 64}}

    def test_candidate_planning_uses_actual_host_abi_and_reservation_preserves_the_request(self):
        helper = self.helper()
        planned = helper.plan_modules([self.candidate()], [], self.producer())
        rows = planned['include']
        self.assertEqual({'redis', 'memcache', 'memcached'}, {row['name'] for row in rows if row['default']})
        self.assertTrue(all(row['host']['abi'] == PHP_ABI and row['request']['host_sha256'] == 'c' * 64 for row in rows))
        reserved = helper._state.reserve_records({'schema_version': 1, 'units': {}}, rows, self.producer())
        merged = helper.selected_matrix(rows, reserved['include'])
        self.assertEqual(len(rows), len(merged['include']))
        self.assertTrue(all(row['request']['record']['recipe_name'] for row in merged['include']))
        other = helper._state.reserve_records(reserved['state'], rows, {**self.producer(), 'run_id': '124'})
        self.assertEqual([], other['include'])
        self.assertEqual([], helper.selected_matrix(rows, other['include'])['include'])
        self.assertTrue(all(row['unit_job'] == helper._state.unit_job_name(row) for row in merged['include']))

    def test_candidate_from_another_attempt_fails_before_module_reservation(self):
        helper = self.helper()
        wrong = self.candidate()
        wrong['meta']['producer']['run_attempt'] = 1
        with self.assertRaises(ValueError):
            helper.plan_modules([wrong], [], self.producer())

    def test_explicit_revision_reaches_every_arch_module_request_and_dependency(self):
        helper = self.helper()
        candidates = []
        for arch in ('x86_64', 'arm64'):
            candidate = self.candidate()
            candidate['host'].update(series='8.6', version='8.6.0RC4')
            candidate['meta'].update(arch=arch, version='8.6.0RC4')
            candidate['artifact_name'] = 'candidate-core-macos-' + arch + '-php-8.6.0RC4'
            candidates.append(candidate)
        planned = helper.plan_modules(candidates, [], self.producer(), module_revision=1)
        self.assertEqual(26, len(planned['include']))
        for row in planned['include']:
            self.assertEqual(1, row['build_revision'])
            self.assertEqual(1, row['request']['record']['build_revision'])
            self.assertEqual(candidates[0]['meta']['base'], row['request']['base'])
        for core in planned['cores']:
            self.assertEqual({row['name']: 1 for row in planned['include'] if row['arch'] == core['candidate']['meta']['arch']},
                             core['module_revisions'])
        default = helper.plan_modules(candidates, [], self.producer())
        self.assertTrue(all(row['build_revision'] == 0 for row in default['include']))
        postgres = self.candidate()
        postgres['host'] = {'type': 'postgresql', 'series': '18', 'version': '18.6',
                            'package_sha256': 'c' * 64, 'abi': {'major': 18}}
        postgres['meta'].update(name='postgresql', version='18.6', filename='postgresql-18.6.tar.gz')
        dependent = helper.plan_modules([postgres], [], self.producer(), module_revision=1)
        routing = next(row for row in dependent['include'] if row['name'] == 'pgrouting')
        self.assertEqual(1, routing['required_modules']['postgis']['build_revision'])
        self.assertEqual(1, routing['request']['record']['required_modules']['postgis']['build_revision'])
        for invalid in (None, True, -1, '1', 1.5):
            with self.subTest(revision=invalid), self.assertRaises(ValueError):
                helper.plan_modules([], [], self.producer(), module_revision=invalid)

    def test_frozen_core_plan_controls_revisions_and_legacy_plan_keeps_zero(self):
        helper = self.helper()
        fixture = test_v2_core_manifest.CoreManifestTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        candidate = fixture.candidate()
        directory = fixture.root / 'candidate'
        directory.mkdir()
        (directory / 'candidate.json').write_text(json.dumps(candidate))
        (directory / fixture.package.name).write_bytes(fixture.package.read_bytes())
        paths = fixture.modules()
        plan_path = fixture.root / 'plan.json'
        legacy = {'cores': [{'candidate': candidate, 'external_candidates': []}]}
        plan_path.write_text(json.dumps(legacy))
        result = helper.finalize_candidate(directory, plan_path, paths, fixture.producer, fixture.root / 'legacy-zero')
        self.assertTrue(all(row['build_revision'] == 0 for row in result['meta']['package_manifest']['default_modules']))
        explicit = copy.deepcopy(legacy)
        records = helper._profiles.module_records(candidate['host'], candidate['meta']['arch'])
        explicit['cores'][0]['module_revisions'] = {row['name']: 1 for row in records}
        plan_path.write_text(json.dumps(explicit))
        with self.assertRaises(ValueError):
            helper.finalize_candidate(directory, plan_path, paths, fixture.producer, fixture.root / 'wrong-zero')
        for path in paths:
            meta = json.loads(Path(path).read_text())
            meta['build_revision'] = 1
            Path(path).write_text(json.dumps(meta))
        result = helper.finalize_candidate(directory, plan_path, paths, fixture.producer, fixture.root / 'explicit-one')
        self.assertEqual(0, result['meta']['build_revision'])
        self.assertEqual({row['name']: 1 for row in records if row['default']},
                         {row['name']: row['build_revision'] for row in result['meta']['package_manifest']['default_modules']})
        plan_path.write_text(json.dumps(legacy))
        with self.assertRaises(ValueError):
            helper.finalize_candidate(directory, plan_path, paths, fixture.producer, fixture.root / 'legacy-wrong-one')

    def test_plan_cli_freezes_explicit_revision_before_reserving_modules(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = self.candidate()
            candidate['record'] = {'runtime_version': 'v2', 'os': 'macos', 'kind': 'package', 'name': 'php',
                                   'version': '8.4.27', 'arch': 'x86_64', 'build_revision': 0, 'build_type': 'source'}
            (root / 'candidate.json').write_text(json.dumps(candidate))
            output = root / 'plan.json'
            args = ['module_pipeline.py', 'plan', '--candidates', str(root), '--catalog',
                    'https://catalog.example.invalid/', '--output', str(output), '--module-revision', '1']
            def reserve(rows, producer, repository, work):
                self.assertTrue(all(row['build_revision'] == 1 for row in rows))
                return helper._state.reserve_records({'schema_version': 1, 'units': {}}, rows, producer)
            with mock.patch.object(sys, 'argv', args), mock.patch.object(helper, 'environment_producer', return_value=self.producer()), \
                    mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'fixture/Builder', 'RUNNER_TEMP': str(root),
                                                  'GITHUB_OUTPUT': str(root / 'github-output')}, clear=True), \
                    mock.patch.object(helper, 'read_catalog', return_value={'status_code': 200, 'data': {'extensions': []}}), \
                    mock.patch.object(helper._state, 'reserve_github', side_effect=reserve):
                helper.main()
            planned = json.loads(output.read_text())
            self.assertTrue(all(row['build_revision'] == 1 for row in planned['include']))
            self.assertTrue(all(revision == 1 for revision in planned['cores'][0]['module_revisions'].values()))

    def test_workflow_inputs_keep_module_revision_independent_from_core_revision(self):
        import yaml
        orchestration = yaml.safe_load((ROOT / '.github/workflows/build-and-dispatch.yml').read_text())
        workflow = yaml.safe_load((ROOT / '.github/workflows/build-macos.yml').read_text())
        entry = orchestration.get('on', orchestration.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(('number', 0), (entry['macos_module_revision']['type'], entry['macos_module_revision']['default']))
        passed = orchestration['jobs']['build-macos']['with']
        self.assertEqual("${{ fromJSON(format('{0}', inputs.macos_module_revision || 0)) }}", passed['module_revision'])
        self.assertEqual("${{ fromJSON(format('{0}', inputs.build_revision || 0)) }}", passed['build_revision'])
        triggers = workflow.get('on', workflow.get(True))
        for trigger in ('workflow_dispatch', 'workflow_call'):
            self.assertEqual('number', triggers[trigger]['inputs']['module_revision']['type'])
            self.assertEqual(0, triggers[trigger]['inputs']['module_revision']['default'])
        step = next(row for row in workflow['jobs']['module-plan']['steps'] if row.get('id') == 'modules')
        self.assertEqual('${{ inputs.module_revision }}', step['env']['MODULE_REVISION'])
        command = shlex.split(next(line for line in step['run'].replace('\\\n', '').splitlines()
                                   if line.startswith('python3 ')))
        self.assertEqual('$MODULE_REVISION', command[command.index('--module-revision') + 1])

    def test_public_package_rows_are_real_grouped_payload_and_never_invent_host_source_producer(self):
        helper = self.helper()
        host = self.candidate()['host']
        host.pop('package_sha256')
        row = {'name': 'php', 'versions': [{'arch': 'x86_64', 'os': 'macOS', 'majorVersion': '8.4',
               'latestVersion': '8.4.27', 'runtimeVersion': 'v2', 'buildRevision': 0, 'fileName': 'php-8.4.27.tar.gz',
               'hash': 'c' * 64, 'downloadUrl': 'https://downloads.example.invalid/php-8.4.27.tar.gz',
               'packageManifest': {'schema_version': 2, 'host': host, 'arch': 'x86_64', 'minimum_os': '12.0',
                                   'base': copy.deepcopy(BASE), 'default_modules': [], 'dependencies': []}}]}
        requests = helper.catalog_host_requests({'status_code': 200, 'data': {'packages': [row]}}, 'x86_64', BASE)
        self.assertEqual(1, len(requests))
        self.assertEqual('catalog-host', requests[0]['origin'])
        self.assertNotIn('producer', requests[0])
        self.assertNotIn('source', requests[0])
        self.assertEqual('c' * 64, requests[0]['host']['package_sha256'])
        planned = helper.plan_modules(requests, [], self.producer())
        self.assertEqual([], planned['cores'], 'Updating a module does not invent a new core release')
        self.assertTrue(all('download_url' in row['request'] for row in planned['include']))
        for field, value in [('os', 'windows'), ('runtimeVersion', 'v1'), ('arch', 'arm64')]:
            wrong = copy.deepcopy(row)
            wrong['versions'][0][field] = value
            with self.assertRaises(ValueError):
                helper.catalog_host_requests({'status_code': 200, 'data': {'packages': [wrong]}}, 'x86_64', BASE)

    def test_public_catalog_missing_or_failed_data_is_an_explicit_failure(self):
        for value in ({'status_code': 500}, {'status_code': 200, 'data': {}}, {'status_code': 200, 'data': {'packages': None}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.helper().catalog_host_requests(value, 'x86_64', BASE)

    def test_current_run_collection_includes_successful_modules_when_core_is_missing(self):
        helper = self.helper()
        rows = helper.plan_modules([self.candidate()], [], self.producer())['include']
        reserved = helper._state.reserve_records({'schema_version': 1, 'units': {}}, rows, self.producer())['state']
        selected = helper.current_run_records([], reserved, self.producer())
        self.assertEqual({helper._state.identity(row) for row in rows}, {helper._state.identity(row) for row in selected})
        next_owner = copy.deepcopy(reserved)
        for unit in next_owner['units'].values():
            unit['reservation']['producer']['run_attempt'] = 3
        self.assertEqual([], helper.current_run_records([], next_owner, self.producer()))
        self.assertNotIn('package', {row['kind'] for row in selected})
        ready = copy.deepcopy(reserved)
        for row in rows:
            ready = helper._state.record_result(ready, helper._state.identity(row), {
                'status': 'build-ready', 'producer': {**self.producer(), 'run_attempt': 3},
                'artifact_id': '7', 'artifact_name': helper._state.artifact_name(row),
                'artifact_zip_sha256': 'a' * 64, 'package_sha256': 'b' * 64,
                'artifact_expires_at': '2030-01-01T00:00:00Z'})
        self.assertEqual([], helper.current_run_records([], ready, self.producer()))
        for unit in ready['units'].values():unit['producer'] = self.producer()
        self.assertEqual(len(rows), len(helper.current_run_records([], ready, self.producer())))


    def public_host_payload(self, arch='x86_64'):
        host = self.candidate()['host']
        host.pop('package_sha256')
        return {'status_code': 200, 'data': {'packages': [{'name': 'php', 'versions': [
            {'arch': arch, 'os': 'macOS', 'majorVersion': '8.4', 'latestVersion': '8.4.27',
             'runtimeVersion': 'v2', 'buildRevision': 0, 'fileName': 'php-8.4.27.tar.gz', 'hash': 'c' * 64,
             'downloadUrl': 'https://downloads.example.invalid/php.tar.gz',
             'packageManifest': {'schema_version': 2, 'host': host, 'arch': arch, 'minimum_os': '12.0',
                                 'base': copy.deepcopy(BASE), 'default_modules': [], 'dependencies': []}}]}]}}

    def test_host_only_updates_skip_selected_new_core_arch_and_reject_missing_published_host(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / 'packages.conf'
            conf.write_text('php\t8.4.27\tphp-8.4.27.tar.gz\tphp-8.4.27.tar.gz\tsource\n')
            core = {'name': 'php', 'version': '8.4.27', 'arch': 'arm64'}
            def catalog(url, arch, packages=False):
                self.assertTrue(packages)
                return self.public_host_payload(arch)
            with mock.patch.object(helper, 'read_catalog', side_effect=catalog):
                matrix = helper.host_request_matrix(conf, [core], {'x86_64': BASE, 'arm64': BASE}, 'https://catalog.example.invalid/')
            self.assertEqual(1, len(matrix['include']))
            self.assertEqual('x86_64', matrix['include'][0]['arch'])
            self.assertEqual('catalog-host', matrix['include'][0]['host_request']['origin'])
            with mock.patch.object(helper, 'read_catalog', return_value={'status_code': 200, 'data': {'packages': []}}):
                with self.assertRaisesRegex(ValueError, 'host'):
                    helper.host_request_matrix(conf, [core], {'x86_64': BASE, 'arm64': BASE}, 'https://catalog.example.invalid/')

    def test_actual_module_planner_reserves_before_matrix_and_duplicate_owner_cannot_schedule(self):
        helper = self.helper()
        ledger = {'schema_version': 1, 'units': {}}
        producers = []
        def reserve(rows, producer, repository, work):
            nonlocal ledger
            producers.append(copy.deepcopy(producer))
            result = helper._state.reserve_records(ledger, rows, producer)
            ledger = result['state']
            return result
        payload = self.public_host_payload()
        host = helper.catalog_host_requests(payload, 'x86_64', BASE)[0]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(helper, 'read_catalog', return_value={'status_code': 200, 'data': {'extensions': []}}), \
                mock.patch.object(helper._state, 'reserve_github', side_effect=reserve):
            plan, matrix, cores, _ = helper.plan_reserved_modules(directory, json.dumps(host), 'https://catalog.example.invalid/',
                self.producer(), 'fixture/Builder', directory)
            self.assertTrue(matrix['include'])
            self.assertEqual([], cores['include'], 'Independent module updates never build or republish their host core')
            self.assertEqual({helper._state.identity(row) for row in plan['include']},
                             {helper._state.identity(row) for row in matrix['include']})
            other = {**self.producer(), 'run_id': '124'}
            _, repeated, _, _ = helper.plan_reserved_modules(directory, json.dumps(host), 'https://catalog.example.invalid/',
                other, 'fixture/Builder', directory)
            self.assertEqual([], repeated['include'])
            self.assertEqual([self.producer(), other], producers)


    def test_module_host_preparation_checks_exact_candidate_base_sha_and_actual_abi_before_compile(self):
        helper = self.helper()
        fixture = test_v2_modules.HostProbeTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        base = fixture.root / 'base'
        fixture.prefix = base / 'packages/php/8.4'
        (fixture.prefix / 'bin').mkdir(parents=True)
        fixture.php_fixture()
        (fixture.prefix / 'VERSION').write_text('8.4.27\n')
        (base / '.bootstrap-v2.json').write_text(json.dumps({'runtime_version': 'v2', 'arch': fixture.arch, **BASE}))
        archive = fixture.root / 'php-8.4.27.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(fixture.prefix, arcname='php/8.4')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        candidate = self.candidate()
        candidate['meta'].update(arch=fixture.arch, sha256=digest)
        candidate['host']['package_sha256'] = digest
        directory = fixture.root / 'candidate'
        directory.mkdir()
        (directory / archive.name).write_bytes(archive.read_bytes())
        (directory / 'candidate.json').write_text(json.dumps(candidate))
        unit = helper.plan_modules([candidate], [], self.producer())['include'][0]
        request = helper.prepare_module_request(unit, directory, base, fixture.root / 'work', self.producer(), fixture.root / 'out')
        self.assertEqual(digest, request['host_sha256'])
        self.assertEqual(unit['request']['record'], request['record'])
        self.assertFalse((fixture.root / 'out').exists(), 'Preparation never emits a module handoff')
        for defect in ('base', 'sha', 'abi', 'producer'):
            with self.subTest(defect=defect):
                wrong = copy.deepcopy(unit)
                producer = self.producer()
                if defect == 'base': wrong['request']['base']['runtime_sha256'] = 'd' * 64
                elif defect == 'sha': wrong['request']['host_sha256'] = 'd' * 64
                elif defect == 'abi': wrong['host']['abi']['module_api'] += 1
                else: producer['run_attempt'] += 1
                with self.assertRaises(ValueError):
                    helper.prepare_module_request(wrong, directory, base, fixture.root / 'work', producer, fixture.root / 'out')

    def test_finalizer_readback_uses_actual_zip_bytes_and_rejects_tamper_duplicate_and_older_attempt(self):
        helper = self.helper()
        fixture = test_v2_core_manifest.CoreManifestTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        meta_path = Path(fixture.modules()[0])
        original = json.loads(meta_path.read_text())
        records = [helper._state.clean_record(original)]
        name = helper._state.artifact_name(original)
        for defect in (None, 'zip_digest', 'package', 'manifest', 'duplicate', 'older_attempt'):
            with self.subTest(defect=defect):
                directory = fixture.root / ('readback-' + str(defect))
                directory.mkdir()
                meta = copy.deepcopy(original)
                if defect == 'older_attempt':meta['producer']['run_attempt'] -= 1
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w') as archive:
                    archive.writestr('meta.json', json.dumps(meta))
                    for entry in (meta['filename'], meta['manifest_filename']):
                        data = (meta_path.parent / entry).read_bytes()
                        archive.writestr(entry, b'tampered' if defect == 'package' and entry == meta['filename'] or defect == 'manifest' and entry == meta['manifest_filename'] else data)
                data = buffer.getvalue()
                (directory / '1.zip').write_bytes(data)
                artifact = {'id': 1, 'name': name, 'digest': 'sha256:' + ('f' * 64 if defect == 'zip_digest' else hashlib.sha256(data).hexdigest())}
                snapshot = {'run': {'status': 'in_progress'}, 'artifacts': [artifact] * (2 if defect == 'duplicate' else 1)}
                with mock.patch.object(helper._state, 'github_owner_snapshot', return_value=snapshot) as read:
                    if defect in ('zip_digest', 'package', 'manifest', 'duplicate'):
                        with self.assertRaises(ValueError):helper.fetch_module_artifacts('fixture/Builder', fixture.producer, records, directory)
                    else:
                        paths = helper.fetch_module_artifacts('fixture/Builder', fixture.producer, records, directory)
                        self.assertEqual(0 if defect == 'older_attempt' else 1, len(paths))
                        if paths:self.assertEqual(original, json.loads(Path(paths[0]).read_text()))
                    self.assertEqual({name}, read.call_args.kwargs['names'])
                    self.assertTrue(read.call_args.kwargs['allow_running'])
