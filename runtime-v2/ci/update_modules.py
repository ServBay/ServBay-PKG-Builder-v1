#!/usr/bin/env python3
"""Refresh independent module profiles using the existing updater's version rules."""

import argparse
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tarfile
import xml.etree.ElementTree as ET
from urllib.parse import quote

_spec = importlib.util.spec_from_file_location('servbay_existing_updater', Path(__file__).parents[2] / 'runtime/update_packages.py')
_existing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_existing)


def _php_release(data, version):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = [row for row in archive if row.isfile() and Path(row.name).name == 'package.xml']
        if len(members) != 1:
            raise ValueError('PECL release needs its actual unique package.xml')
        package = ET.fromstring(archive.extractfile(members[0]).read())
    actual = package.findtext('./{*}version/{*}release')
    php = package.find('./{*}dependencies/{*}required/{*}php')
    if actual != version or php is None or not php.findtext('./{*}min'):
        raise ValueError('PECL package version/PHP support metadata differs from the selected release')
    return {'min': php.findtext('./{*}min'), 'max': php.findtext('./{*}max'),
            'exclude': [row.text for row in php.findall('./{*}exclude')]}


def refresh_modules(profile, host_versions, fetch_bytes):
    if profile.get('schema_version') != 1:
        raise ValueError('Unsupported actual module profile schema')
    updated, changes, sources, fetched, metadata = copy.deepcopy(profile), [], {}, {}, {}
    updater = _existing.PackageUpdater(runtime_version='v2')
    def fetch(url):
        if url not in fetched:
            data = fetch_bytes(url)
            if not isinstance(data, bytes) or not data:
                raise ValueError('Module discovery returned no actual response bytes: ' + url)
            fetched[url] = data
            sources[url] = hashlib.sha256(data).hexdigest()
        return fetched[url]
    for recipe, description in profile['recipes'].items():
        discovery = description['discovery']
        versions, tags = None, {}
        for host_type, series_rows in profile['hosts'].items():
            for series, modules in series_rows.items():
                if recipe not in modules:
                    continue
                bundled = profile.get('bundles', {}).get(host_type, {}).get(series, {})
                if any(recipe in dependencies for dependencies in bundled.values()):
                    continue
                current = modules[recipe]
                if versions is None:
                    if discovery['type'] == 'pecl':
                        xml = ET.fromstring(fetch('https://pecl.php.net/rest/r/' + discovery['package'] + '/allreleases.xml'))
                        versions = [row.findtext('./{*}v') for row in xml.findall('./{*}r') if row.findtext('./{*}s') == 'stable']
                    elif discovery['type'] == 'github':
                        payload = json.loads(fetch('https://api.github.com/repos/' + discovery['repository'] + '/tags?per_page=100'))
                        if not isinstance(payload, list):
                            raise ValueError('GitHub module discovery needs an actual successful tag list')
                        tags = {updater.clean_version_tag(row['name']): row['name'] for row in payload}
                        versions = list(tags)
                    else:
                        raise ValueError('Unsupported actual module discovery source')
                    versions = sorted({v for v in versions if v and re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', v)},
                                      key=updater.version_to_tuple, reverse=True)
                # Preserve the existing supported release line; PHP major bumps and
                # PG minor-line changes require an explicit profile change.
                line_parts = 1 if host_type == 'php' else 2
                line = current.split('.')[:line_parts]
                for version in versions:
                    if version.split('.')[:line_parts] != line or updater.compare_versions(version, current) <= 0:
                        continue
                    if discovery['type'] == 'pecl':
                        if series not in host_versions:
                            raise ValueError('The actual selected PHP host version is missing: ' + series)
                        key = recipe, version
                        if key not in metadata:
                            url = 'https://pecl.php.net/get/' + discovery['package'] + '-' + version + '.tgz'
                            metadata[key] = _php_release(fetch(url), version)
                        support, host = metadata[key], host_versions[series]
                        if (updater.compare_versions(host, support['min']) < 0
                                or support['max'] and updater.compare_versions(host, support['max']) > 0
                                or host in support['exclude']):
                            continue
                    if recipe == 'pg_jieba':
                        pins = updated['recipes'][recipe].setdefault('source_pins', {})
                        if version not in pins:
                            tag = tags[version]
                            tree = json.loads(fetch('https://api.github.com/repos/' + discovery['repository'] + '/git/trees/' + quote(tag, safe='')))
                            links = [row for row in tree.get('tree', []) if row.get('path') == 'libjieba'
                                     and row.get('mode') == '160000' and row.get('type') == 'commit']
                            if len(links) != 1 or not re.fullmatch(r'[0-9a-f]{40}', links[0].get('sha', '')):
                                raise ValueError('The selected pg_jieba release lacks its actual immutable cppjieba gitlink')
                            url = 'https://github.com/' + discovery['repository'] + '/archive/refs/tags/' + quote(tag, safe='') + '.tar.gz'
                            component = 'https://github.com/yanyiwu/cppjieba/archive/' + links[0]['sha'] + '.tar.gz'
                            pins[version] = {'url': url, 'sha256': hashlib.sha256(fetch(url)).hexdigest(),
                                             'component': {'url': component, 'sha256': hashlib.sha256(fetch(component)).hexdigest()}}
                    updated['hosts'][host_type][series][recipe] = version
                    changes.append({'host_type': host_type, 'host_series': series, 'recipe_name': recipe,
                                    'previous_version': current, 'version': version})
                    break
    return updated, changes, sources


def refresh_file(config, host_versions, fetch_bytes, dry_run=False):
    config = Path(config)
    profile = json.loads(config.read_text())
    updated, changes, sources = refresh_modules(profile, host_versions, fetch_bytes)
    if changes and not dry_run:
        config.write_text(json.dumps(updated, indent=2) + '\n')
    return {'updates': changes, 'discovery_sha256': sources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--packages-conf', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    updater = _existing.PackageUpdater(conf_file=args.packages_conf, runtime_version='v2')
    hosts = {'.'.join(row['version'].split('.')[:2]): row['version'] for row in updater.active_package_records() if row['name'] == 'php'}
    def fetch(url):
        response = _existing.requests.get(url, timeout=60)
        response.raise_for_status()
        return response.content
    Path(args.output).write_text(json.dumps(refresh_file(args.config, hosts, fetch, args.dry_run), indent=2) + '\n')


if __name__ == '__main__':
    main()
