#!/usr/bin/env python3
"""Freeze a nonpublishing core candidate, then bind its verified first-install modules."""

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import os


def _load(name):
    spec = importlib.util.spec_from_file_location('servbay_core_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_inventory, _profiles, _hosts = _load('module_manifest'), _load('module_profiles'), _load('module_host')
_meta, _state = _inventory._metadata, _inventory._state


def create_candidate(package, record, producer, base, minimum_os, source, host):
    meta = _meta.create_meta(package, record, producer, base, minimum_os, source)
    host = _inventory._host(copy.deepcopy(host))
    version = _hosts.pg_release_version(meta['version']) if meta['name'] == 'postgresql' else meta['version']
    if host['type'] != meta['name'] or host['version'] != version or host['package_sha256'] != meta['sha256']:
        raise ValueError('The actual core host differs from its candidate archive identity')
    suffix = '-r' + str(meta['build_revision']) if meta['build_revision'] else ''
    return {'schema_version': 1, 'publishable': False, 'record': _state.clean_record(record), 'meta': meta,
            'host': host, 'artifact_name': 'candidate-core-macos-' + meta['arch'] + '-' + meta['name'] + '-' + meta['version'] + suffix}


def _reference(meta):
    return {key: copy.deepcopy(meta[key]) for key in ('kind', 'name', 'version', 'build_revision', 'sha256', 'host')}


def _ref_key(row):
    return row['kind'], row['name'], row['version'], row['build_revision']


def finalize_core(candidate, package, module_meta_paths, external_candidates, producer, output_dir, config=None):
    if candidate.get('schema_version') != 1 or candidate.get('publishable') is not False:
        raise ValueError('A frozen nonpublishing core candidate is required')
    meta, host = copy.deepcopy(candidate['meta']), _inventory._host(copy.deepcopy(candidate['host']))
    record = _state.clean_record(candidate['record'])
    if (_state.validate_producer(producer) != meta['producer'] or _state.identity(meta) != _state.identity(record)
            or _meta.sha256(package) != meta['sha256'] or host['package_sha256'] != meta['sha256']):
        raise ValueError('Core candidate bytes, producer or identity changed before finalization')
    expected_version = _hosts.pg_release_version(meta['version']) if meta['name'] == 'postgresql' else meta['version']
    if host['type'] != meta['name'] or host['version'] != expected_version or meta['major'] != host['series']:
        raise ValueError('Final core host/version/installation series differs')
    source_version = _meta._php_source.source_version(meta['version']) if meta['name'] == 'php' else meta['version']
    if meta.get('source', {}).get('version') != source_version:
        raise ValueError('Final core source version differs from its actual supported source mapping')
    _inventory._sha(meta['source']['sha256'])
    rows = _profiles.module_records(host, meta['arch'], config)
    defaults = [row for row in rows if row['default']]
    available, pending = {}, []
    def add(reference, manifest, external=False):
        _inventory._validate_inventory(manifest)
        _inventory._references([reference], host)
        context = {key: manifest['host'][key] for key in ('type', 'series', 'abi')}
        if (context != reference['host'] or manifest['arch'] != meta['arch'] or manifest['base'] != meta['base']):
            raise ValueError('Module architecture, base or actual host ABI differs from the core')
        key = _ref_key(reference)
        if key in available:
            raise ValueError('A core module identity has multiple candidate sources')
        available[key] = reference, manifest
        if external:
            pending.append(copy.deepcopy(reference))
    for value in module_meta_paths:
        path = Path(value)
        item = json.loads(path.read_text())
        if item.get('producer') != producer or item.get('schema_version') != 2:
            raise ValueError('A local module must retain this exact run/attempt/source producer')
        manifest_path = path.parent / item['manifest_filename']
        if _meta.sha256(manifest_path) != item['manifest_sha256']:
            raise ValueError('Local module manifest attachment changed')
        manifest = json.loads(manifest_path.read_text())
        package_path = path.parent / item['filename']
        if _meta.sha256(package_path) != item['sha256']:
            raise ValueError('Local module archive changed')
        if any(item.get(key) != manifest.get(key) for key in ('arch', 'minimum_os', 'base')):
            raise ValueError('Local module metadata/context differs from its actual manifest')
        _inventory.validate_manifest(manifest, package_path)
        add(_reference(item), manifest)
    for item in external_candidates:
        if item.get('publication_status') != 'requires_uploader_confirmation' or 'published' in item:
            raise ValueError('Public catalog compatibility never proves protected publication')
        add(copy.deepcopy(item['reference']), item['module_manifest'], True)
    roots = []
    for row in defaults:
        key = _ref_key(row)
        if key not in available:
            raise ValueError('Required first-install module is not ready: ' + row['name'])
        roots.append(available[key][0])
    visited, active, floors = set(), set(), [meta['minimum_os']]
    def visit(reference):
        key = _ref_key(reference)
        if key in active:
            raise ValueError('Cyclic module dependencies cannot finalize a core')
        if key in visited:
            return
        if key not in available or available[key][0] != reference:
            raise ValueError('A required module dependency is unavailable or has different content')
        active.add(key)
        manifest = available[key][1]
        floors.append(manifest['minimum_os'])
        for dependency in manifest['dependencies']:
            visit(dependency)
        active.remove(key)
        visited.add(key)
    for reference in roots:
        visit(reference)
    minimum_os = max(floors, key=_profiles._os_version)
    core_host = {key: copy.deepcopy(host[key]) for key in ('type', 'series', 'version', 'abi')}
    meta['minimum_os'] = minimum_os
    meta['package_manifest'] = {'schema_version': 2, 'arch': meta['arch'], 'minimum_os': minimum_os,
                                'base': copy.deepcopy(meta['base']), 'host': core_host,
                                'default_modules': roots, 'dependencies': []}
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ValueError('Refusing to overwrite an existing final core Artifact')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.final-core-', dir=output_dir.parent) as temp:
        staging = Path(temp) / 'artifact'
        staging.mkdir()
        shutil.copyfile(package, staging / meta['filename'])
        if _meta.sha256(staging / meta['filename']) != meta['sha256']:
            raise ValueError('Final core tar differs from its immutable candidate bytes')
        (staging / 'meta.json').write_text(json.dumps(meta, indent=2) + '\n')
        os.replace(staging, output_dir)
    return {'meta': meta, 'package': str(output_dir / meta['filename']), 'meta_path': str(output_dir / 'meta.json'),
            'artifact_name': _state.artifact_name(meta), 'requires_uploader_confirmation': pending}
