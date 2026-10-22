#!/usr/bin/env python3
"""Bind real PostgreSQL control requirements after immutable module builds."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


def _load(name):
    spec = importlib.util.spec_from_file_location('servbay_dependencies_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_inventory, _hosts, _profiles = _load('module_manifest'), _load('module_host'), _load('module_profiles')
_state, _meta = _inventory._state, _inventory._metadata


def controls(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*.control')):
        text = re.sub(r'#[^\n]*', '', path.read_text())
        defaults = re.findall(r"(?m)^\s*default_version\s*=\s*'([^'\n]+)'\s*$", text)
        requires = re.findall(r"(?m)^\s*requires\s*=\s*'([^'\n]*)'\s*$", text)
        if len(defaults) != 1 or not defaults[0].strip() or len(requires) > 1 or path.stem in result:
            raise ValueError('Missing or ambiguous actual PG control: ' + str(path))
        names = [name.strip() for name in requires[0].split(',')] if requires else []
        if len(set(names)) != len(names) or any(not re.fullmatch(r'[a-z][a-z0-9_]*', name) for name in names):
            raise ValueError('Invalid actual PostgreSQL requires: ' + str(path))
        result[path.stem] = names
    if not result:
        raise ValueError('The PGXS payload lacks actual control files')
    return result


def core_provides(archive, host):
    host = _inventory._host(copy.deepcopy(host))
    if host['type'] != 'postgresql':
        raise ValueError('SQL module requirements need their exact PostgreSQL core')
    # Reuse the existing safe archive/version contract, without running a host
    # program on the Ubuntu metadata finalizer or trusting a mutable install tree.
    with tempfile.TemporaryDirectory(prefix='.module-core-controls-') as directory:
        prefix = _hosts.extract_host_archive(archive, host['package_sha256'], Path(directory) / 'packages',
                                             'postgresql', host['series'], host['version'])
        return set(controls(prefix / 'share/extension'))


def classify_requirements(payload, host_archive, host):
    declared = controls(payload)
    required = {name for names in declared.values() for name in names}
    owned, core = set(declared), core_provides(host_archive, host)
    return {'requires': declared, 'core': sorted(required & core),
            'owned': sorted((required - core) & owned), 'external': sorted(required - core - owned)}


def candidate_artifact_name(record):
    return 'candidate-module-' + _state.artifact_name(record)[len('pkg2-'):]


def write_candidate(manifest, payload, record, producer, source, output_dir, requirements):
    required = record.get('required_modules')
    if not isinstance(required, dict) or not required or not isinstance(requirements, dict):
        raise ValueError('A dependency candidate needs actual requirements and exact profile targets')
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ValueError('Refusing to replace a dependency candidate')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.module-candidate-', dir=output_dir.parent) as directory:
        staging = Path(directory) / 'artifact'
        result = _inventory.write_package(manifest, payload, record, producer, source, staging)
        candidate = {'schema_version': 1, 'publishable': False, 'record': copy.deepcopy(record),
                     'meta': result['meta'], 'requirements': copy.deepcopy(requirements),
                     'artifact_name': candidate_artifact_name(record)}
        # No schema2 meta.json is ever handed off under the candidate prefix.
        (staging / 'meta.json').rename(staging / 'candidate.json')
        (staging / 'candidate.json').write_text(json.dumps(candidate, indent=2) + '\n')
        os.replace(staging, output_dir)
    return {'candidate_path': str(output_dir / 'candidate.json'), 'package': str(output_dir / result['meta']['filename']),
            'meta': result['meta'], 'artifact_name': candidate['artifact_name']}


def _read_local(path, producer):
    path = Path(path)
    value = json.loads(path.read_text())
    pending = value.get('publishable') is False
    if pending:
        if value.get('schema_version') != 1 or not isinstance(value.get('record'), dict):
            raise ValueError('Invalid nonpublishing module candidate')
        meta = value['meta']
        if (value.get('artifact_name') != candidate_artifact_name(meta)
                or _state.identity(value['record']) != _state.identity(meta)):
            raise ValueError('Module candidate identity differs')
    else:
        meta = value
    if meta.get('schema_version') != 2 or meta.get('producer') != producer:
        raise ValueError('Module source/run/attempt producer differs')
    record = _state.clean_record(meta)
    if record['kind'] != 'postgresql-extension':
        raise ValueError('A SQL dependency needs a PostgreSQL module')
    source = meta.get('source', {})
    if source.get('version') != record['version']:
        raise ValueError('Module source version differs')
    _inventory._sha(source.get('sha256'))
    for field in ('filename', 'manifest_filename'):
        if not isinstance(meta.get(field), str) or Path(meta[field]).name != meta[field]:
            raise ValueError('Module handoff must remain flat')
    package, attachment = path.parent / meta['filename'], path.parent / meta['manifest_filename']
    if _meta.sha256(package) != meta['sha256'] or _meta.sha256(attachment) != meta['manifest_sha256']:
        raise ValueError('Module tar or manifest SHA256 differs')
    manifest = json.loads(attachment.read_text())
    _inventory.validate_manifest(manifest, package)
    if (any(meta.get(key) != manifest.get(key) for key in ('arch', 'minimum_os', 'base'))
            or meta['host'] != {key: manifest['host'][key] for key in ('type', 'series', 'abi')}):
        raise ValueError('Module metadata and actual inventory context differ')
    return {'meta': meta, 'manifest': manifest, 'candidate': value if pending else None, 'package': package}


def _reference(meta):
    return {key: copy.deepcopy(meta[key]) for key in ('kind', 'name', 'version', 'build_revision', 'sha256', 'host')}


def _archive_controls(item):
    import tarfile
    with tempfile.TemporaryDirectory(prefix='.module-control-readback-') as directory:
        with tarfile.open(item['package'], 'r:gz') as archive:
            for row in item['manifest']['files']:
                if row['type'] == 'file' and row['archive_path'].endswith('.control'):
                    path = Path(directory) / row['archive_path']
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.extractfile(row['archive_path']).read())
        declared = controls(directory)
    if set(declared) != set(item['manifest']['provides']):
        raise ValueError('Actual control provides differ from the module inventory')
    return declared


def finalize_candidate(candidate_path, module_paths, external_candidates, producer, host_archive, output_dir):
    producer = _state.validate_producer(producer)
    target = _read_local(candidate_path, producer)
    if not target['candidate']:
        raise ValueError('Only an explicit nonpublishing module can be finalized')
    target_context = target['meta']
    core = core_provides(host_archive, target['manifest']['host'])
    available, pending, active, resolved = {}, [], set(), {}

    def add(item, external=False):
        meta, manifest = item['meta'], item['manifest']
        if (meta['host'] != target_context['host'] or meta['arch'] != target_context['arch']
                or meta['base'] != target_context['base']):
            raise ValueError('Required module host ABI/architecture/base differs')
        key = _state.identity(meta)
        if key in available:
            raise ValueError('Multiple providers for one exact module identity')
        item['external'] = external
        available[key] = item

    add(target)
    for path in module_paths:
        if Path(path).resolve() != Path(candidate_path).resolve():
            add(_read_local(path, producer))
    for value in external_candidates:
        if value.get('publication_status') != 'requires_uploader_confirmation' or 'published' in value:
            raise ValueError('External compatibility does not prove protected publication')
        reference, manifest = value['reference'], copy.deepcopy(value['module_manifest'])
        _inventory._validate_inventory(manifest)
        _inventory._references([reference], manifest['host'])
        meta = {**reference, 'runtime_version': 'v2', 'os': 'macos', 'arch': manifest['arch'],
                'base': manifest['base'], 'minimum_os': manifest['minimum_os']}
        add({'meta': meta, 'manifest': manifest, 'candidate': None}, True)

    def visit(key):
        if key in active:
            raise ValueError('Cyclic independent module dependencies')
        if key in resolved:
            return resolved[key]
        if key not in available:
            raise ValueError('Missing exact independent module dependency: ' + key)
        item = available[key]
        active.add(key)
        manifest, meta = copy.deepcopy(item['manifest']), copy.deepcopy(item['meta'])
        references = []
        declared = None if item['external'] else _archive_controls(item)
        required = ({name for names in declared.values() for name in names} - set(manifest['provides']) - core
                    if declared is not None else None)
        if item['candidate']:
            candidate = item['candidate']
            # Re-read control bytes from the SHA-validated owned archive. The
            # planner/profile is only intent; it cannot erase upstream requires.
            if declared != candidate.get('requirements'):
                raise ValueError('Actual control requirements changed after native compilation')
            wanted = candidate['record'].get('required_modules', {})
            if set(wanted) != required:
                raise ValueError('Actual independent requirements differ from profile intent')
            for name in sorted(required):
                dependency_key = _state.identity(wanted[name])
                dependency = visit(dependency_key)
                providers = [other_key for other_key, other in available.items() if name in other['manifest']['provides']]
                if providers != [dependency_key] or name not in dependency['manifest']['provides']:
                    raise ValueError('Missing or ambiguous actual provider: ' + name)
                references.append(_reference(dependency['meta']))
        else:
            supplied = set()
            for reference in manifest['dependencies']:
                identity = {**reference, 'runtime_version': 'v2', 'os': 'macos', 'arch': meta['arch']}
                dependency = visit(_state.identity(identity))
                if _reference(dependency['meta']) != reference:
                    raise ValueError('Actual dependency content differs from its frozen reference')
                if required is not None:
                    provided = required & set(dependency['manifest']['provides'])
                    if not provided:
                        raise ValueError('A module dependency does not satisfy an actual control requirement')
                    supplied.update(provided)
                references.append(copy.deepcopy(reference))
            if required is not None and supplied != required:
                raise ValueError('Unbound actual independent control requirement: ' + ','.join(sorted(required - supplied)))
        floors = [meta['minimum_os']]
        for reference in references:
            identity = {**reference, 'runtime_version': 'v2', 'os': 'macos', 'arch': meta['arch']}
            floors.append(resolved[_state.identity(identity)]['meta']['minimum_os'])
        floor = max(floors, key=_profiles._os_version)
        manifest['dependencies'], manifest['minimum_os'] = references, floor
        meta['minimum_os'] = floor
        _inventory._validate_inventory(manifest)
        if item['external']:
            pending.append(_reference(meta))
        active.remove(key)
        result = {**item, 'meta': meta, 'manifest': manifest}
        resolved[key] = result
        return result

    final = visit(_state.identity(target['meta']))
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ValueError('Refusing to replace a finalized module Artifact')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.module-final-', dir=output_dir.parent) as directory:
        stage = Path(directory) / 'artifact'
        stage.mkdir()
        meta, manifest = final['meta'], final['manifest']
        package = stage / meta['filename']
        shutil.copyfile(final['package'], package)
        if _meta.sha256(package) != meta['sha256']:
            raise ValueError('Metadata finalization changed native module tar bytes')
        attachment = stage / meta['manifest_filename']
        attachment.write_text(json.dumps(manifest, indent=2) + '\n')
        meta['manifest_sha256'] = _meta.sha256(attachment)
        (stage / 'meta.json').write_text(json.dumps(meta, indent=2) + '\n')
        os.replace(stage, output_dir)
    return {'meta': meta, 'package': str(output_dir / meta['filename']),
            'manifest': str(output_dir / meta['manifest_filename']), 'meta_path': str(output_dir / 'meta.json'),
            'artifact_name': _state.artifact_name(meta), 'requires_uploader_confirmation': pending}
