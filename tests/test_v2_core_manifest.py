import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from test_v2_modules import BASE, PHP_ABI

ROOT = Path(__file__).resolve().parents[1]


class CoreManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.producer = {'commit': 'a' * 40, 'run_id': '123', 'run_attempt': 2}
        self.identity = {'runtime_version': 'v2', 'os': 'macos', 'kind': 'package', 'name': 'php',
                         'version': '8.4.27', 'arch': 'x86_64', 'build_revision': 0, 'build_type': 'source'}
        self.package = self.root / 'php-8.4.27.tar.gz'
        with tarfile.open(self.package, 'w:gz') as archive:
            row = tarfile.TarInfo('php/8.4/VERSION')
            row.size = 7
            archive.addfile(row, io.BytesIO(b'8.4.27\n'))
        self.source = self.root / 'source.tar.gz'
        self.source.write_bytes(b'real upstream input')
        self.host = {'type': 'php', 'series': '8.4', 'version': '8.4.27',
                     'package_sha256': hashlib.sha256(self.package.read_bytes()).hexdigest(), 'abi': copy.deepcopy(PHP_ABI)}

    def helper(self, name='core_manifest'):
        spec = importlib.util.spec_from_file_location('tested_' + name, ROOT / 'runtime-v2/ci' / (name + '.py'))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper

    def candidate(self):
        return self.helper().create_candidate(self.package, self.identity, self.producer, BASE, '12.0',
                                              {'version': '8.4.27', 'path': str(self.source)}, self.host)

    def modules(self, floor='12.0'):
        helper = self.helper('module_manifest')
        results = []
        for name, version in [('redis', '6.3.0'), ('memcache', '8.2'), ('memcached', '3.4.0')]:
            payload = self.root / (name + '-payload')
            payload.mkdir()
            (payload / (name + '.so')).write_bytes(name.encode())
            manifest = helper.create_manifest(self.host, payload,
                [{'scope': 'config', 'install_path': 'conf.d/' + name + '.ini', 'mode': '0644', 'content': 'extension=' + name + '.so\n'}],
                [name], [], arch='x86_64', minimum_os=floor, base=BASE, source_name='phpredis' if name == 'redis' else name,
                extension_dir='lib/extensions')
            identity = {**self.identity, 'kind': 'php-extension', 'name': name, 'version': version,
                        'host': {k: self.host[k] for k in ('type', 'series', 'abi')}}
            result = helper.write_package(manifest, payload, identity, self.producer,
                                          {'version': version, 'path': str(self.source)}, self.root / ('artifact-' + name))
            results.append(result['meta_path'])
        return results

    def test_final_core_preserves_candidate_tar_bytes_and_waits_for_every_real_default(self):
        helper = self.helper()
        candidate = self.candidate()
        self.assertFalse(candidate['publishable'])
        self.assertTrue(candidate['artifact_name'].startswith('candidate-core-'))
        modules = self.modules()
        output = self.root / 'final'
        with self.assertRaises(ValueError):
            helper.finalize_core(candidate, self.package, modules[:2], [], self.producer, output)
        self.assertFalse(output.exists())
        result = helper.finalize_core(candidate, self.package, modules, [], self.producer, output)
        meta = result['meta']
        self.assertEqual(hashlib.sha256(self.package.read_bytes()).hexdigest(), meta['sha256'])
        self.assertEqual(self.package.read_bytes(), Path(result['package']).read_bytes())
        self.assertNotIn('package_sha256', meta['package_manifest']['host'])
        self.assertEqual({'redis', 'memcache', 'memcached'}, {row['name'] for row in meta['package_manifest']['default_modules']})
        self.assertEqual([], meta['package_manifest']['dependencies'])
        self.assertEqual(BASE, meta['package_manifest']['base'])
        self.assertEqual('8.4', meta['major'])

    def test_effective_core_floor_includes_required_default_modules(self):
        result = self.helper().finalize_core(self.candidate(), self.package, self.modules('13.0'), [], self.producer, self.root / 'final')
        self.assertEqual('13.0', result['meta']['minimum_os'])
        self.assertEqual('13.0', result['meta']['package_manifest']['minimum_os'])

    def test_changed_candidate_producer_core_bytes_arch_base_or_abi_never_yields_meta(self):
        helper = self.helper()
        candidate, modules = self.candidate(), self.modules()
        for field, value in [('producer', {**self.producer, 'run_attempt': 3}), ('arch', 'arm64'),
                             ('base', {**BASE, 'devlib_sha256': 'f' * 64})]:
            with self.subTest(field=field):
                wrong = copy.deepcopy(candidate)
                wrong['meta'][field] = value
                with self.assertRaises(ValueError):
                    helper.finalize_core(wrong, self.package, modules, [], self.producer, self.root / ('bad-' + field))
        wrong = copy.deepcopy(candidate)
        wrong['host']['abi']['module_api'] += 1
        with self.assertRaises(ValueError):
            helper.finalize_core(wrong, self.package, modules, [], self.producer, self.root / 'bad-actual-abi')
        self.assertFalse((self.root / 'bad-actual-abi').exists())
        self.package.write_bytes(b'changed candidate archive')
        with self.assertRaises(ValueError):
            helper.finalize_core(candidate, self.package, modules, [], self.producer, self.root / 'bad-core')
        self.assertFalse((self.root / 'bad-core').exists())

    def test_external_compatible_candidate_remains_for_exact_uploader_confirmation(self):
        helper = self.helper()
        candidate, modules = self.candidate(), self.modules()
        external = []
        for path in modules:
            meta = json.loads(Path(path).read_text())
            manifest = json.loads((Path(path).parent / meta['manifest_filename']).read_text())
            reference = {key: meta[key] for key in ('kind', 'name', 'version', 'build_revision', 'sha256', 'host')}
            external.append({'reference': reference, 'module_manifest': manifest,
                             'publication_status': 'requires_uploader_confirmation'})
        result = helper.finalize_core(candidate, self.package, [], external, self.producer, self.root / 'final')
        self.assertEqual(3, len(result['requires_uploader_confirmation']))
        self.assertNotIn('published', result['meta'])
        self.assertEqual(3, len(result['meta']['package_manifest']['default_modules']))


    def test_well_formed_foreign_module_arch_base_or_abi_is_rejected_before_final_core(self):
        helper = self.helper()
        candidate, modules = self.candidate(), self.modules()
        meta_path = Path(modules[0])
        original_meta = meta_path.read_bytes()
        original = json.loads(original_meta)
        manifest_path = meta_path.parent / original['manifest_filename']
        original_manifest = manifest_path.read_bytes()
        for field in ('arch', 'base', 'abi'):
            with self.subTest(field=field):
                meta, manifest = json.loads(original_meta), json.loads(original_manifest)
                if field == 'arch':meta['arch'] = manifest['arch'] = 'arm64'
                elif field == 'base':
                    meta['base']['devlib_sha256'] = manifest['base']['devlib_sha256'] = 'd' * 64
                else:
                    meta['host']['abi']['module_api'] += 1
                    manifest['host']['abi']['module_api'] += 1
                manifest_path.write_text(json.dumps(manifest))
                meta['manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                meta_path.write_text(json.dumps(meta))
                try:
                    with self.assertRaises(ValueError):
                        helper.finalize_core(candidate, self.package, modules, [], self.producer, self.root / ('foreign-' + field))
                    self.assertFalse((self.root / ('foreign-' + field)).exists())
                finally:
                    meta_path.write_bytes(original_meta)
                    manifest_path.write_bytes(original_manifest)
