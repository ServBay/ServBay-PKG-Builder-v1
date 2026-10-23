import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ModuleUpdateTests(unittest.TestCase):
    def helper(self):
        spec = importlib.util.spec_from_file_location('tested_module_updates', ROOT / 'runtime-v2/ci/update_modules.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def profile(self):
        return {'schema_version': 1, 'recipes': {
            'memcache': {'kind': 'php-extension', 'discovery': {'type': 'pecl', 'package': 'memcache'}},
            'pgvector': {'kind': 'postgresql-extension', 'discovery': {'type': 'github', 'repository': 'pgvector/pgvector'}}},
            'hosts': {'php': {'5.6': {'memcache': '2.2.7'}, '8.4': {'memcache': '8.0'}},
                      'postgresql': {'12': {}, '18': {'pgvector': '0.8.1'}}}, 'bundles': {}}

    def pecl_package(self, version, minimum):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            data = ('<package xmlns="http://pear.php.net/dtd/package-2.0"><version><release>' + version +
                    '</release></version><dependencies><required><php><min>' + minimum +
                    '</min></php></required></dependencies></package>').encode()
            row = tarfile.TarInfo('package.xml')
            row.size = len(data)
            archive.addfile(row, io.BytesIO(data))
        return output.getvalue()

    def test_refresh_reuses_shared_version_selection_and_respects_actual_host_and_release_lines(self):
        helper = self.helper()
        profile = self.profile()
        original = copy.deepcopy(profile)
        packages = {'8.2': self.pecl_package('8.2', '8.0.0'), '2.2.8': self.pecl_package('2.2.8', '7.0.0')}
        fetched = []
        def fetch(url):
            fetched.append(url)
            if url.endswith('/allreleases.xml'):
                return b'<a><r><v>9.0</v><s>beta</s></r><r><v>8.2</v><s>stable</s></r><r><v>2.2.8</v><s>stable</s></r></a>'
            if '/get/memcache-' in url:
                return packages[url.split('memcache-')[1][:-4]]
            return json.dumps([{'name': 'v0.9.0'}, {'name': 'v0.8.7'}, {'name': 'v0.8.6rc1'}]).encode()
        updated, changes, sources, _ = helper.refresh_modules(profile, {'5.6': '5.6.40.1', '8.4': '8.4.27'}, fetch)
        self.assertEqual(original, profile)
        self.assertEqual('2.2.7', updated['hosts']['php']['5.6']['memcache'])
        self.assertEqual('8.2', updated['hosts']['php']['8.4']['memcache'])
        self.assertEqual('0.8.7', updated['hosts']['postgresql']['18']['pgvector'])
        self.assertEqual({}, updated['hosts']['postgresql']['12'])
        self.assertEqual({('php', '8.4', 'memcache', '8.2'), ('postgresql', '18', 'pgvector', '0.8.7')},
                         {(r['host_type'], r['host_series'], r['recipe_name'], r['version']) for r in changes})
        self.assertEqual(hashlib.sha256(packages['8.2']).hexdigest(), sources['https://pecl.php.net/get/memcache-8.2.tgz'])
        self.assertTrue(all('abi' not in row for row in changes), 'Discovery must not invent an ABI before exact host probing')
        self.assertEqual(1, fetched.count('https://pecl.php.net/rest/r/memcache/allreleases.xml'))

    def test_unreachable_pecl_candidates_are_skipped_without_aborting_the_refresh(self):
        # PECL 对部分老版本 tarball 返回 500（xdebug-2.3.0 实测）；候选拉取失败
        # 只能跳过该候选继续找下一个可用版本，不能中止整个刷新。
        helper = self.helper()
        profile = self.profile()
        packages = {'8.1': self.pecl_package('8.1', '8.0.0'), '2.2.8': self.pecl_package('2.2.8', '7.0.0')}
        failures = []

        def fetch(url):
            if url.endswith('/get/memcache-8.2.tgz'):
                failures.append(url)
                raise helper._existing.requests.HTTPError('500 Server Error for url: ' + url)
            return self.pecl_stub_fetch(url, packages)

        updated, changes, sources, diagnostics = helper.refresh_modules(
            profile, {'5.6': '5.6.40.1', '8.4': '8.4.27'}, fetch)
        self.assertEqual(['https://pecl.php.net/get/memcache-8.2.tgz'], failures)
        self.assertEqual('8.1', updated['hosts']['php']['8.4']['memcache'])
        self.assertEqual([{'recipe': 'memcache', 'host_series': '8.4', 'version': '8.2',
                           'url': 'https://pecl.php.net/get/memcache-8.2.tgz',
                           'error': '500 Server Error for url: https://pecl.php.net/get/memcache-8.2.tgz'}],
                         diagnostics)
        self.assertEqual(('php', '8.4', 'memcache', '8.1'),
                         (changes[0]['host_type'], changes[0]['host_series'], changes[0]['recipe_name'], changes[0]['version']))

    def pecl_stub_fetch(self, url, packages):
        if url.endswith('/allreleases.xml'):
            return b'<a><r><v>8.2</v><s>stable</s></r><r><v>8.1</v><s>stable</s></r><r><v>2.2.8</v><s>stable</s></r></a>'
        if '/get/memcache-' in url:
            return packages[url.split('memcache-')[1][:-4]]
        return json.dumps([{'name': 'v0.9.0'}, {'name': 'v0.8.7'}, {'name': 'v0.8.6rc1'}]).encode()

    def test_failed_discovery_is_not_an_empty_update_and_never_rewrites_profile(self):
        helper = self.helper()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'modules.json'
            config.write_text(json.dumps(self.profile()))
            before = config.read_bytes()
            def fail(url):
                raise OSError('controlled upstream offline')
            with self.assertRaises(OSError):
                helper.refresh_file(config, {'5.6': '5.6.40.1', '8.4': '8.4.27'}, fail)
            self.assertEqual(before, config.read_bytes())

    def test_bundled_psr_version_is_preserved_and_bad_release_metadata_fails(self):
        helper = self.helper()
        profile = self.profile()
        profile['recipes'] = {'psr': {'kind': 'php-extension', 'discovery': {'type': 'pecl', 'package': 'psr'}}}
        profile['hosts'] = {'php': {'7.4': {'psr': '1.1.0', 'phalcon': '4.1.2'}}, 'postgresql': {}}
        profile['bundles'] = {'php': {'7.4': {'phalcon': {'psr': '1.1.0'}}}}
        updated, changes, _, _ = helper.refresh_modules(profile, {'7.4': '7.4.33.1'}, lambda url: b'<a/>')
        self.assertEqual(profile, updated)
        self.assertEqual([], changes)
        profile = self.profile()
        def malformed(url):
            if url.endswith('.xml'):
                return b'<a><r><v>8.2</v><s>stable</s></r></a>'
            return self.pecl_package('8.1', '8.0.0')
        with self.assertRaises(ValueError):
            helper.refresh_modules(profile, {'5.6': '5.6.40.1', '8.4': '8.4.27'}, malformed)


    def test_pg_jieba_update_pins_the_selected_tag_and_its_actual_cppjieba_gitlink(self):
        helper = self.helper()
        profile = {'schema_version': 1, 'recipes': {'pg_jieba': {'kind': 'postgresql-extension',
            'discovery': {'type': 'github', 'repository': 'jaiminpan/pg_jieba'}, 'source_pins': {}}},
            'hosts': {'php': {}, 'postgresql': {'18': {'pg_jieba': '2.0.1'}}}, 'bundles': {}}
        main, component, commit = b'real selected main archive', b'real selected cppjieba archive', 'b' * 40
        urls = []
        def fetch(url):
            urls.append(url)
            if '/tags?' in url:
                return json.dumps([{'name': 'v2.0.2'}]).encode()
            if '/git/trees/' in url:
                return json.dumps({'tree': [{'path': 'libjieba', 'type': 'commit', 'mode': '160000', 'sha': commit}]}).encode()
            return component if '/yanyiwu/' in url else main
        updated, changes, sources, _ = helper.refresh_modules(profile, {}, fetch)
        self.assertEqual('2.0.2', updated['hosts']['postgresql']['18']['pg_jieba'])
        pins = updated['recipes']['pg_jieba']['source_pins']['2.0.2']
        self.assertEqual('https://github.com/jaiminpan/pg_jieba/archive/refs/tags/v2.0.2.tar.gz', pins['url'])
        self.assertEqual(hashlib.sha256(main).hexdigest(), pins['sha256'])
        self.assertEqual('https://github.com/yanyiwu/cppjieba/archive/' + commit + '.tar.gz', pins['component']['url'])
        self.assertEqual(hashlib.sha256(component).hexdigest(), pins['component']['sha256'])
        self.assertEqual(hashlib.sha256(component).hexdigest(), sources[pins['component']['url']])
        self.assertEqual(1, len(changes))
        self.assertTrue(any(url.endswith('/git/trees/v2.0.2') for url in urls))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'modules.json'
            config.write_text(json.dumps(profile))
            before = config.read_bytes()
            def missing_gitlink(url):
                return b'{"tree":[]}' if '/git/trees/' in url else fetch(url)
            with self.assertRaises(ValueError):
                helper.refresh_file(config, {}, missing_gitlink)
            self.assertEqual(before, config.read_bytes(), 'A version cannot advance without immutable source pins')


    def test_refresh_honors_maximum_and_excluded_actual_php_versions(self):
        helper = self.helper()
        profile = self.profile()
        profile['recipes'] = {'memcache': profile['recipes']['memcache']}
        profile['hosts'] = {'php': {'8.4': {'memcache': '8.0'}}, 'postgresql': {}}
        for requirement in ('<max>8.4.26</max>', '<exclude>8.4.27</exclude>'):
            with self.subTest(requirement=requirement):
                data = self.pecl_package('8.2', '8.0.0')
                # Rebuild the real archive member rather than injecting parsed support data.
                output = io.BytesIO()
                with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as original:
                    package = original.extractfile('package.xml').read().replace(b'</php>', requirement.encode() + b'</php>')
                with tarfile.open(fileobj=output, mode='w:gz') as archive:
                    member = tarfile.TarInfo('package.xml');member.size = len(package)
                    archive.addfile(member, io.BytesIO(package))
                def fetch(url):
                    return b'<a><r><v>8.2</v><s>stable</s></r></a>' if url.endswith('.xml') else output.getvalue()
                updated, changes, _, _ = helper.refresh_modules(profile, {'8.4': '8.4.27'}, fetch)
                self.assertEqual(profile, updated)
                self.assertEqual([], changes)
