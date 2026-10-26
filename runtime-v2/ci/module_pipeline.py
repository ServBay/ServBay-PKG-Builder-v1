#!/usr/bin/env python3
"""Connect exact core/module artifacts to the existing public build reservations."""

import copy
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import urllib.request

import importlib.util


def _load(name):
    spec = importlib.util.spec_from_file_location('servbay_pipeline_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_core, _profiles, _dependencies = _load('core_manifest'), _load('module_profiles'), _load('module_dependencies')
_state, _hosts, _meta = _core._state, _core._hosts, _core._meta


def _public_url(value):
    url = urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError('An explicit public HTTPS catalog/archive URL is required')
    return value


def read_catalog(url, arch, packages=False):
    _public_url(url)
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(runtime_version='v2', os='macos')
    query.update({'cpu_arch': arch, 'uid': '00000000-0000-0000-0000-000000000000', 'app_version': '0.0.0'}
                 if packages else {'arch': arch})
    target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))
    last_error = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(target, headers={'Accept': 'application/json'}), timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            last_error = error
            if attempt < 4:
                import time; time.sleep(min(10 * (2 ** attempt), 60))
    raise last_error


def catalog_host_requests(payload, arch, base):
    if not isinstance(payload, dict) or payload.get('status_code') != 200:
        raise ValueError('The public package catalog did not return success')
    packages = payload.get('data', {}).get('packages')
    if not isinstance(packages, list):
        raise ValueError('The actual grouped package catalog is missing')
    result = []
    for group in packages:
        if group.get('name') not in ('php', 'postgresql'):
            continue
        if not isinstance(group.get('versions'), list):
            raise ValueError('The public package group lacks its actual versions')
        for row in group['versions']:
            if (row.get('arch'), row.get('os'), row.get('runtimeVersion')) != (arch, 'macOS', 'v2'):
                raise ValueError('The catalog host platform/runtime differs from the requested source')
            manifest = row.get('packageManifest')
            if not isinstance(manifest, dict) or manifest.get('schema_version') != 2:
                raise ValueError('The public host lacks its actual schema2 ABI/base manifest')
            host = copy.deepcopy(manifest['host'])
            host['package_sha256'] = _core._inventory._sha(row.get('hash'))
            _core._inventory._host(host)
            expected_version = _hosts.pg_release_version(row['latestVersion']) if host['type'] == 'postgresql' else row['latestVersion']
            if (host['type'] != group['name'] or host['series'] != row['majorVersion'] or host['version'] != expected_version
                    or manifest.get('arch') != arch or manifest.get('base') != base):
                raise ValueError('The public core host identity/arch/base differs')
            _profiles._os_version(manifest.get('minimum_os'))
            if row.get('fileName') != group['name'] + '-' + row['latestVersion'] + '.tar.gz':
                raise ValueError('The public host archive filename differs from its actual identity')
            result.append({'origin': 'catalog-host', 'host': host, 'arch': arch, 'base': copy.deepcopy(base),
                           'minimum_os': manifest['minimum_os'], 'filename': row['fileName'],
                           'download_url': _public_url(row['downloadUrl'])})
    return result


def plan_modules(candidates, catalog, producer, module_revision=0):
    if type(module_revision) is not int or module_revision < 0:
        raise ValueError('Module revision must be an explicit nonnegative integer')
    producer = _state.validate_producer(producer)
    include, cores, external, seen = [], [], [], set()
    for candidate in candidates:
        if candidate.get('origin') == 'catalog-host':
            context = candidate
            host = _core._inventory._host(copy.deepcopy(candidate['host']))
            _public_url(context['download_url'])
        else:
            if candidate.get('schema_version') != 1 or candidate.get('publishable') is not False:
                raise ValueError('Only a nonpublishing candidate can produce a final core')
            context, host = candidate['meta'], _core._inventory._host(copy.deepcopy(candidate['host']))
            if context['producer'] != producer or context['sha256'] != host['package_sha256']:
                raise ValueError('Candidate core is not from this exact run/attempt/source')
        rows = _profiles.module_records(host, context['arch'], revision=module_revision)
        selected = _profiles.select_modules(rows, catalog, context['base'], context['minimum_os'])
        external.extend(selected['external_candidates'])
        if candidate.get('origin') != 'catalog-host':
            cores.append({'candidate': copy.deepcopy(candidate), 'external_candidates': selected['external_candidates'],
                          'module_revisions': {row['name']: row['build_revision'] for row in rows}})
        for row in selected['build']:
            key = _state.identity(row)
            if key in seen:
                continue
            seen.add(key)
            request = {'record': copy.deepcopy(row), 'host_sha256': host['package_sha256'], 'host_version': host['version'],
                       'host_filename': context['filename'], 'base': copy.deepcopy(context['base']),
                       'minimum_os': context['minimum_os']}
            if candidate.get('origin') == 'catalog-host':
                request['download_url'] = context['download_url']
            else:
                request['candidate_artifact'] = candidate['artifact_name']
            include.append({**row, 'request': request})
    return {'include': include, 'cores': cores, 'external_candidates': external}


def selected_matrix(requested, reserved):
    by_identity = {_state.identity(row): row for row in requested}
    if len(by_identity) != len(requested):
        raise ValueError('Duplicate module requests before reservation')
    rows = []
    for selected in reserved:
        key = _state.identity(selected)
        if key not in by_identity:
            raise ValueError('Reservation returned a module that was not requested')
        row = copy.deepcopy(by_identity[key])
        row['runner'] = 'macos-26-intel' if row['arch'] == 'x86_64' else 'macos-26'
        row['unit_job'] = _state.unit_job_name(row)
        rows.append(row)
    return {'include': rows}


def current_run_records(records, state, producer):
    _state.validate_state(state)
    producer = _state.validate_producer(producer)
    result = {_state.identity(row): _state.clean_record(row) for row in records}
    for key, unit in state['units'].items():
        if unit['record']['kind'] == 'package':
            continue
        owner = unit.get('reservation', {}).get('producer')
        if owner == producer or unit.get('status') == 'build-ready' and unit.get('producer') == producer:
            result[key] = copy.deepcopy(unit['record'])
    return list(result.values())


def prepare_candidate(package, record, producer, base, source, prefix):
    digest = _meta.sha256(package)
    series = _meta.package_metadata(record['name'], record['version'])['major']
    _hosts.verify_host_archive(package, digest, prefix, record['name'], series)
    probe = (_hosts.probe_php_host if record['name'] == 'php' else _hosts.probe_postgresql_host)(
        prefix, record['version'], digest, record['arch'])
    return _core.create_candidate(package, record, producer, base, _meta.minimum_os_from_archive(package), source, probe['host'])


def prepare_module_request(unit, candidate_dir, base_root, work_root, producer, output_dir):
    request, producer = copy.deepcopy(unit['request']), _state.validate_producer(producer)
    base_root, work_root = Path(base_root).absolute(), Path(work_root).absolute()
    expected_marker = {'runtime_version': 'v2', 'arch': unit['arch'], **request['base']}
    if json.loads((base_root / '.bootstrap-v2.json').read_text()) != expected_marker:
        raise ValueError('The module host and configured Runtime/Devlib base differ')
    if 'candidate_artifact' in request:
        files = list(Path(candidate_dir).rglob('candidate.json'))
        if len(files) != 1:
            raise ValueError('Download exactly one immutable candidate core Artifact')
        candidate = json.loads(files[0].read_text())
        if candidate['meta']['producer'] != producer or candidate['artifact_name'] != request['candidate_artifact']:
            raise ValueError('Downloaded candidate source/run/attempt differs')
        archive = files[0].parent / request['host_filename']
    else:
        _public_url(request['download_url'])
        work_root.mkdir(parents=True, exist_ok=True)
        archive = work_root / ('host-' + unit['host']['type'] + '-' + request['host_version'] + '-' + request['host_sha256'] + '.tar.gz')
        if not archive.exists():
            download = urllib.request.Request(request['download_url'], headers={'User-Agent': 'ServBay-PKG-Builder'})
            with urllib.request.urlopen(download, timeout=120) as response:
                archive.write_bytes(response.read())
    if _meta.sha256(archive) != request['host_sha256']:
        raise ValueError('Exact module host download SHA256 differs')
    host_type, series = unit['host']['type'], unit['host']['series']
    prefix = base_root / 'packages' / host_type / series
    if prefix.exists():
        _hosts.verify_host_archive(archive, request['host_sha256'], prefix, host_type, series)
    else:
        _hosts.extract_host_archive(archive, request['host_sha256'], base_root / 'packages', host_type, series, request['host_version'])
    probe = (_hosts.probe_php_host if host_type == 'php' else _hosts.probe_postgresql_host)(
        prefix, request['host_version'], request['host_sha256'], unit['arch'])
    if {key: probe['host'][key] for key in ('type', 'series', 'abi')} != unit['host']:
        raise ValueError('Downloaded host actual ABI differs from the reserved module identity')
    return {'record': request['record'], 'host_archive': str(archive), 'host_sha256': request['host_sha256'],
            'host_version': request['host_version'], 'base_root': str(base_root), 'work_root': str(work_root),
            'producer': producer, 'base': request['base'], 'minimum_os': request['minimum_os'],
            'output_dir': str(output_dir), 'jobs': 2}


def fetch_module_artifacts(repository, producer, records, directory, include_candidates=False):
    import hashlib
    import zipfile
    directory = Path(directory)
    names = {_state.artifact_name(row): _state.identity(row) for row in records}
    if include_candidates:
        names.update({_dependencies.candidate_artifact_name(row): _state.identity(row)
                      for row in records if row.get('required_modules')})
    snapshot = _state.github_owner_snapshot(repository, producer, directory, allow_running=True, names=set(names))
    if snapshot['run'].get('status') not in ('in_progress', 'completed'):
        raise ValueError('Exact module producer attempt is not readable')
    if include_candidates:
        # The shared collector deliberately accepts only pkg2. Candidate reads
        # are confined to this metadata finalizer and never change that gate.
        requested = {name for name in names if name.startswith('candidate-module-')}
        page = 1
        while requested:
            endpoint = f"repos/{repository}/actions/runs/{producer['run_id']}/artifacts?per_page=100&page={page}"
            response = subprocess.run(['gh', 'api', endpoint], check=True, capture_output=True, text=True, timeout=120)
            artifacts = json.loads(response.stdout)['artifacts']
            for artifact in artifacts:
                if artifact.get('name') not in requested or artifact.get('expired') is not False:
                    continue
                source = artifact.get('workflow_run', {})
                if str(source.get('id')) != producer['run_id'] or source.get('head_sha') != producer['commit']:
                    raise ValueError('Nonpublishing candidate came from another run/head')
                artifact_id = str(artifact['id'])
                if not re.fullmatch(r'[1-9][0-9]*', artifact_id):
                    raise ValueError('Invalid nonpublishing candidate Artifact id')
                with (directory / (artifact_id + '.zip')).open('wb') as handle:
                    subprocess.run(['gh', 'api', f'repos/{repository}/actions/artifacts/{artifact_id}/zip'],
                                   stdout=handle, check=True, timeout=600)
                snapshot['artifacts'].append(artifact)
            if len(artifacts) < 100:
                break
            page += 1
    result, seen = [], set()
    for artifact in snapshot['artifacts']:
        path = directory / (str(artifact['id']) + '.zip')
        if artifact.get('digest') != 'sha256:' + _meta.sha256(path):
            raise ValueError('Module Artifact ZIP readback digest differs')
        with zipfile.ZipFile(path) as archive:
            entries = archive.namelist()
            candidate = artifact['name'].startswith('candidate-module-')
            metadata_name = 'candidate.json' if candidate else 'meta.json'
            if entries.count(metadata_name) != 1:
                raise ValueError('Module Artifact metadata is missing or ambiguous')
            value = json.loads(archive.read(metadata_name))
            if candidate:
                if (value.get('schema_version') != 1 or value.get('publishable') is not False
                        or value.get('artifact_name') != artifact['name'] or 'meta.json' in entries):
                    raise ValueError('A module dependency candidate must remain nonpublishing')
                meta = value['meta']
            else:
                meta = value
            if meta.get('producer') != producer:
                continue  # Older attempts are never adopted or relabelled.
            if meta.get('schema_version') != 2 or _state.identity(meta) != names[artifact['name']]:
                raise ValueError('Module Artifact identity differs from this exact request')
            if artifact['name'] in seen:
                raise ValueError('Multiple current-attempt module Artifacts share one identity')
            seen.add(artifact['name'])
            wanted = [metadata_name, meta['filename'], meta['manifest_filename']]
            if any(Path(name).name != name or entries.count(name) != 1 for name in wanted):
                raise ValueError('Module Artifact files are missing/ambiguous or escape their flat handoff')
            if (hashlib.sha256(archive.read(meta['filename'])).hexdigest() != meta['sha256']
                    or hashlib.sha256(archive.read(meta['manifest_filename'])).hexdigest() != meta['manifest_sha256']):
                raise ValueError('Module Artifact package/manifest readback SHA256 differs')
            target = directory / str(artifact['id'])
            target.mkdir()
            for name in wanted:
                (target / name).write_bytes(archive.read(name))
            result.append(str(target / metadata_name))
    return result


def finalize_requested_module(unit, plan, producer, repository, directory, host_dir, output_dir):
    expected = [row for row in plan['include'] if _state.identity(row) == _state.identity(unit)]
    if len(expected) != 1 or expected[0]['request'] != unit['request']:
        raise ValueError('Module finalization differs from its immutable requested unit')
    paths = fetch_module_artifacts(repository, producer, plan['include'], Path(directory) / 'readback', True)
    target, compatible = [], []
    for path in paths:
        value = json.loads(Path(path).read_text())
        meta = value['meta'] if value.get('publishable') is False else value
        if _state.identity(meta) == _state.identity(unit):
            target.append(path)
            if value.get('record') != unit['request']['record']:
                raise ValueError('Native candidate profile requirements changed')
            if meta['base'] != unit['request']['base'] or meta['host'] != unit['host']:
                raise ValueError('Native dependency candidate base/host differs')
            manifest = json.loads((Path(path).parent / meta['manifest_filename']).read_text())
            if (manifest['host']['package_sha256'] != unit['request']['host_sha256']
                    or manifest['host']['version'] != _hosts.pg_release_version(unit['request']['host_version'])):
                raise ValueError('Native dependency candidate exact compilation host differs')
        if (meta['arch'] == unit['arch'] and meta['host']['type'] == unit['host']['type']
                and meta['host']['series'] == unit['host']['series']):
            compatible.append(path)
    if len(target) != 1:
        raise ValueError('Exactly one current-run nonpublishing module candidate is required')
    request = unit['request']
    if 'candidate_artifact' in request:
        files = list(Path(host_dir).rglob('candidate.json'))
        if len(files) != 1:
            raise ValueError('Exactly one immutable core candidate is required for dependency binding')
        candidate = json.loads(files[0].read_text())
        if (candidate['artifact_name'] != request['candidate_artifact'] or candidate['meta']['producer'] != producer
                or candidate['meta']['sha256'] != request['host_sha256']):
            raise ValueError('Dependency binding core source/run/attempt differs')
        host_archive = files[0].parent / request['host_filename']
    else:
        _public_url(request['download_url'])
        host_archive = Path(directory) / ('host-' + request['host_sha256'] + '.tar.gz')
        download = urllib.request.Request(request['download_url'], headers={'User-Agent': 'ServBay-PKG-Builder'})
        with urllib.request.urlopen(download, timeout=120) as response:
            host_archive.write_bytes(response.read())
    external = [row for row in plan.get('external_candidates', [])
                if row['record']['arch'] == unit['arch'] and row['record']['host'] == unit['host']]
    return _dependencies.finalize_candidate(target[0], compatible, external, producer, host_archive, output_dir)


def host_request_matrix(conf, selected_core_records, bases, package_catalog_url):
    spec = importlib.util.spec_from_file_location('pipeline_updater', Path(__file__).parents[2] / 'runtime/update_packages.py')
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    wanted = updater.PackageUpdater(str(conf), runtime_version='v2').build_matrix(
        updater.PackageUpdater(str(conf), runtime_version='v2').active_package_records())['include']
    profile = json.loads((Path(__file__).parents[1] / 'config/modules.json').read_text())
    def key(row):
        return row['name'], _meta.package_metadata(row['name'], row['version'])['major'], row['arch']
    excluded = {key(row) for row in selected_core_records if row['name'] in ('php', 'postgresql')}
    desired = {key(row) for row in wanted if row['name'] in ('php', 'postgresql')
               and _meta.package_metadata(row['name'], row['version'])['major'] in profile['hosts'][row['name']]}
    remaining = desired - excluded
    requests = []
    for arch in ('x86_64', 'arm64'):
        keys = {row for row in remaining if row[2] == arch}
        if not keys:
            continue
        payload = read_catalog(package_catalog_url, arch, packages=True)
        if payload.get('status_code') != 200 or not isinstance(payload.get('data', {}).get('packages'), list):
            raise ValueError('The independent-module host catalog failed; no empty fallback')
        groups = []
        for group in payload['data']['packages']:
            if group.get('name') not in ('php', 'postgresql'):
                continue
            versions = [row for row in group['versions'] if (group['name'], row['majorVersion'], arch) in keys]
            if versions:
                groups.append({'name': group['name'], 'versions': versions})
        found = set()
        for request in catalog_host_requests({'status_code': 200, 'data': {'packages': groups}}, arch, bases[arch]):
            identity = request['host']['type'], request['host']['series'], arch
            if identity in found:
                raise ValueError('The public host catalog has duplicate installation identities')
            found.add(identity)
            requests.append({'pkg': request['host']['type'], 'version': request['host']['version'], 'arch': arch,
                             'host_request': request})
        if found != keys:
            raise ValueError('Independent module updates lack exact published host identities: ' + repr(sorted(keys - found)))
    return {'include': requests}


def finalize_candidate(candidate_dir, plan_path, module_meta_paths, producer, output_dir):
    files = list(Path(candidate_dir).rglob('candidate.json'))
    if len(files) != 1:
        raise ValueError('Finalization requires exactly one frozen candidate')
    candidate = json.loads(files[0].read_text())
    cores = [row for row in json.loads(Path(plan_path).read_text())['cores']
             if row['candidate']['artifact_name'] == candidate['artifact_name']]
    if len(cores) != 1 or cores[0]['candidate'] != candidate:
        raise ValueError('The final core differs from its immutable module plan')
    paths = []
    for value in module_meta_paths:
        meta = json.loads(Path(value).read_text())
        if meta['arch'] == candidate['meta']['arch'] and meta['host']['type'] == candidate['host']['type'] and meta['host']['series'] == candidate['host']['series']:
            paths.append(value)
    package = files[0].parent / candidate['meta']['filename']
    return _core.finalize_core(candidate, package, paths, cores[0]['external_candidates'], producer, output_dir,
                               module_revisions=cores[0].get('module_revisions'))


def environment_producer():
    import os
    return _state.validate_producer({'commit': os.environ['PRODUCER_COMMIT'], 'run_id': os.environ['GITHUB_RUN_ID'],
                                    'run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT'])})


def plan_reserved_modules(candidate_dir, host_request, catalog_url, producer, repository, work_dir, module_revision=0):
    candidates = [json.loads(path.read_text()) for path in Path(candidate_dir).rglob('candidate.json')]
    if host_request:
        value = json.loads(host_request)
        if value.get('origin') != 'catalog-host':
            raise ValueError('An independent host request must retain its public catalog provenance')
        candidates.append(value)
    catalog = []
    for arch in sorted({(row if row.get('origin') == 'catalog-host' else row['meta'])['arch'] for row in candidates}):
        payload = read_catalog(catalog_url, arch)
        if payload.get('status_code') != 200 or not isinstance(payload.get('data', {}).get('extensions'), list):
            raise ValueError('The public module catalog failed; no empty fallback')
        catalog.extend(payload['data']['extensions'])
    plan = plan_modules(candidates, catalog, producer, module_revision=module_revision)
    reserved = _state.reserve_github(plan['include'], producer, repository, Path(work_dir) / 'reservation') if plan['include'] else {'include': [], 'diagnostics': []}
    matrix = selected_matrix(plan['include'], reserved['include'])
    plan['dependency_matrix'] = {'include': [row for row in matrix['include'] if row.get('required_modules')]}
    core_matrix = {'include': [{'artifact_name': row['candidate']['artifact_name'],
                               'unit_job': _state.unit_job_name(row['candidate']['record']),
                               'arch': row['candidate']['meta']['arch']} for row in plan['cores']]}
    return plan, matrix, core_matrix, reserved.get('diagnostics', [])


def main():
    import argparse
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    plan = sub.add_parser('plan')
    plan.add_argument('--candidates', required=True)
    plan.add_argument('--host-request', default='')
    plan.add_argument('--catalog', required=True)
    plan.add_argument('--output', required=True)
    plan.add_argument('--module-revision', type=int, default=0)
    prepare = sub.add_parser('prepare-module')
    prepare.add_argument('--unit', required=True)
    prepare.add_argument('--candidate-dir', required=True)
    prepare.add_argument('--base-root', required=True)
    prepare.add_argument('--work-root', required=True)
    prepare.add_argument('--output-dir', required=True)
    prepare.add_argument('--output', required=True)
    fetch = sub.add_parser('fetch-modules')
    fetch.add_argument('--plan', required=True)
    fetch.add_argument('--output', required=True)
    sub.add_parser('finalize-module')
    finalize = sub.add_parser('finalize')
    args = parser.parse_args()
    producer = environment_producer()
    if args.command == 'plan':
        result, matrix, cores, diagnostics = plan_reserved_modules(args.candidates, args.host_request, args.catalog,
            producer, os.environ['GITHUB_REPOSITORY'], os.environ['RUNNER_TEMP'], module_revision=args.module_revision)
        Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            for key, value in [('matrix', json.dumps(matrix, separators=(',', ':'))), ('count', len(matrix['include'])),
                               ('cores', json.dumps(cores, separators=(',', ':'))), ('core_count', len(cores['include'])),
                               ('dependency_matrix', json.dumps(result['dependency_matrix'], separators=(',', ':'))),
                               ('dependency_count', len(result['dependency_matrix']['include']))]:
                output.write(f'{key}={value}\n')
        print(json.dumps({'reservation_diagnostics': diagnostics}))
    elif args.command == 'prepare-module':
        value = prepare_module_request(json.loads(args.unit), args.candidate_dir, args.base_root, args.work_root, producer, args.output_dir)
        Path(args.output).write_text(json.dumps(value, indent=2) + '\n')
    elif args.command == 'fetch-modules':
        value = fetch_module_artifacts(os.environ['GITHUB_REPOSITORY'], producer, json.loads(Path(args.plan).read_text())['include'], Path(os.environ['RUNNER_TEMP']) / 'module-readback')
        Path(args.output).write_text(json.dumps(value))
    elif args.command == 'finalize-module':
        result = finalize_requested_module(json.loads(os.environ['UNIT_JSON']),
            json.loads(Path(os.environ['MODULE_PLAN_FILE']).read_text()), producer, os.environ['GITHUB_REPOSITORY'],
            Path(os.environ['RUNNER_TEMP']) / 'module-finalization', os.environ['HOST_DIR'], os.environ['FINAL_MODULE_DIR'])
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write('artifact_name=' + result['artifact_name'] + '\n')
        print(json.dumps(result))
    else:
        result = finalize_candidate(os.environ['CANDIDATE_DIR'], os.environ['MODULE_PLAN_FILE'],
            json.loads(Path(os.environ['MODULE_META_LIST']).read_text()), producer, os.environ['FINAL_CORE_DIR'])
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write('artifact_name=' + _meta.artifact_name(result['meta']) + '\n')
        print(json.dumps(result, default=str))


if __name__ == '__main__':
    main()
