"""Synthetic Windows core/profile/control contracts using real 7z archives."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from test_windows_v2_modules import ABI, COMPAT, EXT, SEVEN_ZIP, load


class WindowsCoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.inventory = load('runtime-v2/ci/module_manifest.py')
        self.core = load('runtime-v2/ci/core_manifest.py')
        self.profiles = load('runtime-v2/ci/module_profiles.py')
        self.dep = load('runtime-v2/ci/module_dependencies.py')
        self.archive = load('runtime/ci/windows_archive.py')
        self.producer = {'commit': 'a' * 40, 'run_id': '123', 'run_attempt': 2}
        self.source = self.root / 'source.zip'
        self.source.write_bytes(b'synthetic source distinct from archives')
        self.record = dict(runtime_version='v2', os='windows', kind='package', name='php', version='8.4.27', arch='x64', build_revision=0)
        self.package = self.pack('php-8.4.27-windows-x86_64.7z', {'php/8.4/bin/php.exe': b'synthetic core'})
        self.host = dict(type='php', series='8.4', version='8.4.27', abi=copy.deepcopy(ABI), package_sha256=self.sha(self.package))
        self.profile = self.root / 'profile.json'
        self.profile.write_text(json.dumps({'schema_version': 1, 'os': 'windows',
            'hosts': {'php': {'8.4': {'redis': '9.1', 'memcache': '9.2', 'memcached': '9.3'}}},
            'recipes': {name: {'kind': 'php-extension', 'name': name, 'default': True} for name in ('redis', 'memcache', 'memcached')}}))

    def sha(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def pack(self, name, files):
        stage = self.root / (name + '-stage')
        stage.mkdir()
        for name_, data in files.items():
            path = stage / name_
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        path = self.root / name
        self.archive.create_archive(stage, path, list(files), SEVEN_ZIP)
        return path

    def candidate(self, **kwargs):
        return self.core.create_candidate(self.package, self.record, self.producer, None, '10.0',
            {'version': '8.4.27', 'path': str(self.source)}, self.host, windows_compatibility=kwargs.get('compat', COMPAT))

    def modules(self, revisions=None):
        results = []
        for name, version in [('redis', '9.1'), ('memcache', '9.2'), ('memcached', '9.3')]:
            stage = self.root / name
            (stage / EXT).mkdir(parents=True)
            (stage / EXT / ('php_' + name + '.dll')).write_bytes(name.encode())
            manifest = self.inventory.create_manifest(self.host, stage, [], [name], [], arch='x86_64', minimum_os='10.1', base=None,
                source_name=name, extension_dir=EXT, os_name='windows', windows_compatibility=COMPAT)
            record = {**self.record, 'kind': 'php-extension', 'name': name, 'version': version,
                'build_revision': (revisions or {}).get(name, 0),
                'host': {key: copy.deepcopy(self.host[key]) for key in ('type', 'series', 'abi')}}
            results.append(self.inventory.write_package(manifest, stage, record, self.producer,
                {'version': version, 'path': str(self.source)}, self.root / ('out-' + name), seven_zip=SEVEN_ZIP))
        return results

    def finalize(self, candidate, modules=(), external=(), name='final', revisions=None):
        return self.core.finalize_core(candidate, self.package, [m['meta_path'] for m in modules], external,
            self.producer, self.root / name, self.profile, seven_zip=SEVEN_ZIP, module_revisions=revisions)

    def test_explicit_per_module_revisions_preserve_exact_readiness(self):
        revisions = {'redis': 1, 'memcache': 0, 'memcached': 2}
        candidate, modules = self.candidate(), self.modules(revisions)
        with self.assertRaises(ValueError):
            self.finalize(candidate, modules, name='default-zero')
        result = self.finalize(candidate, modules, revisions=revisions)
        self.assertEqual({row['name']: row['build_revision'] for row in result['meta']['package_manifest']['default_modules']}, revisions)
        for wrong in ({**revisions, 'redis': 0}, {'redis': 1}, {**revisions, 'unknown': 1},
                      {**revisions, 'redis': True}, {**revisions, 'redis': -1}):
            with self.subTest(revisions=wrong), self.assertRaises(ValueError):
                self.finalize(candidate, modules, name='wrong-revision', revisions=wrong)

    def external(self, module):
        return {'reference': {k: copy.deepcopy(module['meta'][k]) for k in ('kind', 'name', 'version', 'build_revision', 'sha256', 'host')},
            'module_manifest': json.loads(Path(module['manifest']).read_text()), 'publication_status': 'requires_uploader_confirmation'}

    def test_explicit_windows_profile_never_uses_macos_defaults(self):
        rows = self.profiles.module_records(self.host, 'x64', self.profile, os_name='windows')
        self.assertEqual(['9.1', '9.2', '9.3'], [r['version'] for r in rows])
        self.assertTrue(all(r['os'] == 'windows' and r['arch'] == 'x86_64' for r in rows))
        for config in (None, self.root / 'wrong.json'):
            if config:
                value = json.loads(self.profile.read_text()); value.pop('os'); config.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                self.profiles.module_records(self.host, 'x86_64', config, os_name='windows')
        with self.assertRaises(ValueError):
            self.profiles.module_records(self.host, 'x86_64', self.profile)

    def test_core_defaults_floor_and_immutable_bytes(self):
        candidate, modules = self.candidate(), self.modules()
        self.assertEqual('candidate-core-windows-x86_64-php-8.4.27', candidate['artifact_name'])
        with self.assertRaises(ValueError): self.finalize(candidate, modules[:2])
        result = self.finalize(candidate, modules)
        self.assertEqual(self.package.read_bytes(), Path(result['package']).read_bytes())
        meta = result['meta']; manifest = meta['package_manifest']
        self.assertEqual('10.1', meta['minimum_os'])
        self.assertEqual('10.1', manifest['minimum_os'])
        for obj in (meta, manifest):
            self.assertNotIn('base', obj)
            self.assertEqual(('windows', 'servbay-v2', 1, COMPAT), (obj['os'], obj['layout'], obj['layout_version'], obj['windows_compatibility']))
        self.assertEqual({'redis', 'memcache', 'memcached'}, {r['name'] for r in manifest['default_modules']})
        self.assertTrue(all(len(r) == 6 for r in manifest['default_modules']))

    def test_candidate_compatibility_rejects_missing_empty_extra_fields(self):
        for compat in (None, {}, {**COMPAT, 'crt': ''}, {**COMPAT, 'extra': 'x'}):
            with self.subTest(compat=compat), self.assertRaises(ValueError): self.candidate(compat=compat)

    def test_core_rejects_candidate_base_compat_source_and_sha_tamper(self):
        candidate, modules = self.candidate(), self.modules()
        for field, value in [('base', None), ('windows_compatibility', {}), ('os', 'macos'), ('arch', 'arm64'), ('source', {'version': '8.4.1', 'sha256': 'a' * 64}), ('sha256', 'a' * 64)]:
            wrong = copy.deepcopy(candidate); wrong['meta'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.finalize(wrong, modules, name=field)

    def test_external_context_rejects_each_platform_abi_compat_and_floor(self):
        candidate, modules = self.candidate(), self.modules()
        external = [self.external(m) for m in modules]
        result = self.finalize(candidate, external=external)
        self.assertEqual([e['reference'] for e in external], result['requires_uploader_confirmation'])
        for field, value in [('os', 'macos'), ('arch', 'arm64'), ('base', None), ('windows_compatibility', {**COMPAT, 'crt': 'other'}), ('minimum_os', 'invalid')]:
            wrong = copy.deepcopy(external); wrong[0]['module_manifest'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.finalize(candidate, external=wrong, name='bad-' + field)
        wrong = copy.deepcopy(external); wrong[0]['module_manifest']['host']['abi']['module_api'] += 1
        with self.assertRaises(ValueError): self.finalize(candidate, external=wrong, name='bad-abi')

    def test_local_outer_context_and_attachment_are_checked(self):
        candidate, modules = self.candidate(), self.modules()
        path = Path(modules[0]['meta_path']); original = path.read_bytes()
        for field, value in [('os', 'macos'), ('arch', 'arm64'), ('minimum_os', '11.0'), ('windows_compatibility', {**COMPAT, 'crt': 'other'}), ('base', None), ('manifest_sha256', 'a' * 64), ('sha256', 'b' * 64), ('source', {'version': '9.1', 'sha256': 'bad'})]:
            wrong = json.loads(original); wrong[field] = value; path.write_text(json.dumps(wrong))
            with self.subTest(field=field), self.assertRaises(ValueError): self.finalize(candidate, modules, name='local-' + field)
            path.write_bytes(original)

    def test_closure_requires_exact_hash_and_rejects_cycles(self):
        candidate, modules = self.candidate(), self.modules()
        ext = [self.external(m) for m in modules]
        ext[0]['module_manifest']['dependencies'] = [copy.deepcopy(ext[1]['reference'])]
        ext[0]['module_manifest']['dependencies'][0]['sha256'] = 'a' * 64
        with self.assertRaises(ValueError): self.finalize(candidate, external=ext)
        ext[0]['module_manifest']['dependencies'] = [copy.deepcopy(ext[1]['reference'])]
        ext[1]['module_manifest']['dependencies'] = [copy.deepcopy(ext[0]['reference'])]
        with self.assertRaises(ValueError): self.finalize(candidate, external=ext)

    def test_catalog_matches_windows_context_and_never_claims_publication(self):
        module = self.modules()[0]; external = self.external(module)
        record = self.profiles.module_records(self.host, 'x86_64', self.profile, os_name='windows')[0]
        row = dict(name='php-ext', extName='redis', version='9.1', hostVersion='8.4', arch='x86_64', os='Windows', runtimeVersion='v2', buildRevision=0,
            hash=module['meta']['sha256'], downloadUrl='https://example.invalid/redis.7z', config={'module_manifest': external['module_manifest']})
        def plan(row_): return self.profiles.select_modules([record], [row_], None, '10.1', os_name='windows', windows_compatibility=COMPAT)
        selected = plan(row)
        self.assertEqual([], selected['build']); self.assertEqual(6, len(selected['references'][0]))
        self.assertEqual('requires_uploader_confirmation', selected['external_candidates'][0]['publication_status'])
        for field, value in [('os', 'macos'), ('arch', 'arm64'), ('minimum_os', '11.0'), ('windows_compatibility', {**COMPAT, 'toolchain': 'other'})]:
            wrong = copy.deepcopy(row); wrong['config']['module_manifest'][field] = value
            with self.subTest(field=field): self.assertEqual([record], plan(wrong)['build'])

    def test_exact_member_read_requires_archive_and_member_hashes(self):
        data = self.archive.read_members(self.package, ['php/8.4/bin/php.exe'], expected_sha256=self.sha(self.package), seven_zip=SEVEN_ZIP)
        self.assertEqual({'php/8.4/bin/php.exe': b'synthetic core'}, data)
        for names, digest in [(['missing'], self.sha(self.package)), (['php/8.4/bin/php.exe'], 'a' * 64), (['../bad'], self.sha(self.package))]:
            with self.assertRaises(ValueError): self.archive.read_members(self.package, names, expected_sha256=digest, seven_zip=SEVEN_ZIP)

    def pg(self, version='18.6', header=None):
        archive = self.pack('postgresql-' + version + '-windows-x86_64.7z', {
            'postgresql/18/include/server/pg_config.h': header if header is not None else b'#define PG_VERSION "18.6"\n',
            'postgresql/18/share/extension/plpgsql.control': b"default_version = '1.0'\n",
            'postgresql/18/share/extension/hstore.control': b"default_version = '1.8'\n"})
        return archive, dict(type='postgresql', series='18', version=version, abi={'major': 18}, package_sha256=self.sha(archive))

    def test_pg_core_reads_real_header_and_contrib_control_bytes(self):
        archive, host = self.pg()
        self.assertEqual({'plpgsql', 'hstore'}, self.dep.core_provides(archive, host, os_name='windows', seven_zip=SEVEN_ZIP))
        stage = self.root / 'sql'; stage.mkdir()
        (stage / 'routing.control').write_text("default_version = '1.0'\nrequires = 'hstore,postgis'\n")
        actual = self.dep.classify_requirements(stage, archive, host, os_name='windows', seven_zip=SEVEN_ZIP)
        self.assertEqual(['hstore'], actual['core']); self.assertEqual(['postgis'], actual['external'])
        host['package_sha256'] = 'a' * 64
        with self.assertRaises(ValueError): self.dep.core_provides(archive, host, os_name='windows', seven_zip=SEVEN_ZIP)

    def test_pg_wrong_or_ambiguous_header_version_is_rejected(self):
        for i, header in enumerate((b'#define PG_VERSION "18.5"\n', b'#define PG_VERSION "18.6"\n#define PG_VERSION "18.7"\n', b'/* #define PG_VERSION "18.6" */\n')):
            archive, host = self.pg('18.' + str(i + 7), header)
            host['version'] = '18.6'
            with self.subTest(header=header), self.assertRaises(ValueError): self.dep.core_provides(archive, host, os_name='windows', seven_zip=SEVEN_ZIP)

    def pg_module(self, host, name, requires=(), pending=False, floor='10.0'):
        stage = self.root / ('pg-' + name)
        (stage / 'share/extension').mkdir(parents=True)
        (stage / 'lib').mkdir()
        text = "default_version = '1.0'\n"
        if requires: text += "requires = '" + ','.join(requires) + "'\n"
        (stage / 'share/extension' / (name + '.control')).write_text(text)
        (stage / 'share/extension' / (name + '--1.0.sql')).write_text('SELECT 1;\n')
        (stage / 'lib' / (name + '.dll')).write_bytes(b'synthetic pg DLL')
        manifest = self.inventory.create_manifest(host, stage, [], [name], [], arch='x86_64', minimum_os=floor,
            base=None, source_name=name, os_name='windows', windows_compatibility=COMPAT)
        record = {**self.record, 'kind': 'postgresql-extension', 'name': name, 'version': '1.0',
            'host': {k: copy.deepcopy(host[k]) for k in ('type', 'series', 'abi')}}
        if pending:
            record['required_modules'] = {'postgis': {**record, 'name': 'postgis'}}
            return self.dep.write_candidate(manifest, stage, record, self.producer,
                {'version': '1.0', 'path': str(self.source)}, self.root / ('out-pg-' + name), {name: list(requires)}, seven_zip=SEVEN_ZIP)
        return self.inventory.write_package(manifest, stage, record, self.producer,
            {'version': '1.0', 'path': str(self.source)}, self.root / ('out-pg-' + name), seven_zip=SEVEN_ZIP)

    def pg_dependencies(self):
        archive, host = self.pg()
        return archive, self.pg_module(host, 'postgis', ['hstore'], floor='10.2'), self.pg_module(host, 'routing', ['hstore', 'postgis'], pending=True)

    def bind(self, archive, dependency, pending, external=(), name='bound'):
        return self.dep.finalize_candidate(pending['candidate_path'], [dependency['meta_path']] if dependency else [], external,
            self.producer, archive, self.root / name, seven_zip=SEVEN_ZIP)

    def test_pg_local_dependency_binding_preserves_archive_and_six_field_reference(self):
        archive, dependency, pending = self.pg_dependencies()
        result = self.bind(archive, dependency, pending)
        self.assertEqual(Path(pending['package']).read_bytes(), Path(result['package']).read_bytes())
        self.assertEqual('10.2', result['meta']['minimum_os'])
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertEqual(COMPAT, manifest['windows_compatibility']); self.assertNotIn('base', manifest)
        self.assertEqual(dependency['meta']['sha256'], manifest['dependencies'][0]['sha256'])
        self.assertEqual(6, len(manifest['dependencies'][0]))
        self.assertEqual(self.sha(result['manifest']), result['meta']['manifest_sha256'])
        with self.assertRaises(ValueError): self.bind(archive, None, pending, name='missing')

    def test_pg_external_dependency_checks_context_and_protected_status(self):
        archive, dependency, pending = self.pg_dependencies()
        ext = self.external(dependency)
        result = self.bind(archive, None, pending, [ext])
        self.assertEqual([ext['reference']], result['requires_uploader_confirmation'])
        for field, value in [('os', 'macos'), ('arch', 'arm64'), ('base', None), ('minimum_os', 'invalid'), ('windows_compatibility', {**COMPAT, 'distribution': 'other'})]:
            wrong = copy.deepcopy(ext); wrong['module_manifest'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.bind(archive, None, pending, [wrong], name=field)
        wrong = copy.deepcopy(ext); wrong['module_manifest']['host']['abi']['major'] = 17
        with self.assertRaises(ValueError): self.bind(archive, None, pending, [wrong], name='abi')
        wrong = copy.deepcopy(ext); wrong['published'] = True
        with self.assertRaises(ValueError): self.bind(archive, None, pending, [wrong], name='published')

    def test_pg_local_dependency_outer_context_source_and_attachment_rejected(self):
        archive, dependency, pending = self.pg_dependencies()
        path = Path(dependency['meta_path']); original = path.read_bytes()
        for field, value in [('os', 'macos'), ('arch', 'arm64'), ('base', None), ('minimum_os', '11.0'), ('windows_compatibility', {**COMPAT, 'crt': 'other'}), ('source', {'version': '1.0', 'sha256': 'bad'}), ('sha256', 'a' * 64), ('manifest_sha256', 'a' * 64)]:
            wrong = json.loads(original); wrong[field] = value; path.write_text(json.dumps(wrong))
            with self.subTest(field=field), self.assertRaises(ValueError): self.bind(archive, dependency, pending, name=field)
            path.write_bytes(original)

    def test_pg_control_cache_cannot_erase_actual_independent_requires(self):
        archive, dependency, pending = self.pg_dependencies()
        path = Path(pending['candidate_path']); candidate = json.loads(path.read_text())
        candidate['requirements'] = {'routing': ['hstore']}; path.write_text(json.dumps(candidate))
        with self.assertRaisesRegex(ValueError, 'Actual control requirements changed'): self.bind(archive, dependency, pending)

    def test_pg_cycles_rejected_and_external_selected_hash_preserved(self):
        archive, dependency, pending = self.pg_dependencies()
        ext = self.external(dependency)
        target = self.external({**pending, 'manifest': str(Path(pending['candidate_path']).parent / pending['meta']['manifest_filename'])})
        ext['module_manifest']['dependencies'] = [target['reference']]
        with self.assertRaisesRegex(ValueError, 'Cyclic'): self.bind(archive, None, pending, [ext])
        ext['module_manifest']['dependencies'] = []
        ext['reference']['sha256'] = 'e' * 64
        # The dependency's exact selected bytes must survive in the six-field ref.
        result = self.bind(archive, None, pending, [ext], name='selected-hash')
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertEqual('e' * 64, manifest['dependencies'][0]['sha256'])

    def test_member_content_sha_is_rechecked_after_inventory_verification(self):
        from unittest import mock
        name = 'php/8.4/bin/php.exe'
        with mock.patch.object(self.archive, '_run', return_value=b'changed after verification'), \
                mock.patch.object(self.archive, 'archive_hashes', return_value={name: hashlib.sha256(b'synthetic core').hexdigest()}):
            with self.assertRaisesRegex(ValueError, 'member SHA256 changed'):
                self.archive.read_members(self.package, [name], expected_sha256=self.sha(self.package), seven_zip=SEVEN_ZIP)

    def test_windows_catalog_query_uses_explicit_os_without_network(self):
        import io
        from unittest import mock
        with mock.patch.object(self.profiles.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"status_code":200,"data":{"extensions":[]}}')) as request:
            self.assertEqual([], self.profiles.read_catalog('https://example.invalid/catalog', 'x86_64', os_name='windows'))
        self.assertIn('os=windows', request.call_args.args[0].full_url)

    def test_archive_sha_is_checked_before_any_archive_tool_call(self):
        from unittest import mock
        with mock.patch.object(self.archive, 'archive_hashes') as listing:
            with self.assertRaises(ValueError):
                self.archive.read_members(self.package, ['php/8.4/bin/php.exe'], expected_sha256='a' * 64, seven_zip=SEVEN_ZIP)
        listing.assert_not_called()

    def test_archive_sha_is_rechecked_after_member_read(self):
        from unittest import mock
        name = 'php/8.4/bin/php.exe'
        digest = self.sha(self.package)
        def changed(*args, **kwargs):
            self.package.write_bytes(b'changed after member read')
            return b'synthetic core'
        with mock.patch.object(self.archive, '_run', side_effect=changed), \
                mock.patch.object(self.archive, 'archive_hashes', return_value={name: hashlib.sha256(b'synthetic core').hexdigest()}):
            with self.assertRaisesRegex(ValueError, 'changed while reading'):
                self.archive.read_members(self.package, [name], expected_sha256=digest, seven_zip=SEVEN_ZIP)
