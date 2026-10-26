#!/usr/bin/env python3
"""Build an independent module against one exact core archive and pinned base."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


def _load(name):
    spec = importlib.util.spec_from_file_location("servbay_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_hosts, _manifest, _dependencies = _load("module_host"), _load("module_manifest"), _load("module_dependencies")


def primary_source(work_root, recipe, version):
    files = [Path(work_root) / "src" / f"{recipe}-{version}.{suffix}" for suffix in ("tgz", "tar.gz", "tar.bz2", "tar.xz")]
    files = [path for path in files if path.is_file() and path.stat().st_size]
    if len(files) != 1:
        raise ValueError("Expected one actual primary module download: " + recipe + " " + version)
    return {"version": version, "path": str(files[0])}


def collect_php(stage, prefix, series, extension_dir, destination):
    installed = Path(str(stage) + str(Path(prefix) / extension_dir))
    if not installed.is_dir() or installed.is_symlink():
        raise ValueError("The fresh PHP module stage has no real extension directory")
    destination = Path(destination)
    destination.mkdir()
    files = sorted(installed.iterdir())
    if not files or any(not path.name.endswith(".so") or not path.is_file() or path.is_symlink() for path in files):
        raise ValueError("The PHP module install must consist of its actual flat SO files")
    for path in files:
        shutil.copy2(path, destination / path.name)
    configs = []
    for path in sorted((Path(stage) / "etc/php" / series / "conf.d").glob("*.ini")):
        configs.append({"scope": "config", "install_path": "conf.d/" + path.name,
                        "mode": f"0{stat.S_IMODE(path.stat().st_mode):03o}", "content": path.read_text(encoding="utf-8")})
    return destination, configs, [path.stem for path in files]


def control_provides(directory):
    return list(_dependencies.controls(directory))


def collect_postgresql(stage, prefix, destination):
    installed = Path(str(stage) + str(prefix))
    if not installed.is_dir() or installed.is_symlink():
        raise ValueError("The fresh PGXS stage has no exact host installation tree")
    provides = control_provides(installed)
    shutil.copytree(installed, destination, symlinks=True)
    return Path(destination), provides


def verify_native_payload(payload, arch):
    count = 0
    for path in Path(payload).rglob("*"):
        if path.is_symlink() or not path.is_file() or path.suffix not in (".so", ".dylib", ".bundle"):
            continue
        kind = _hosts._run(["/usr/bin/file", "-b", path])
        if "Mach-O" not in kind:
            raise ValueError("A module library is not an actual Mach-O file: " + str(path))
        _hosts._architecture(path, arch)
        count += 1
    if not count:
        raise ValueError("The module stage contains no actual native module libraries")


def verify_php_payload(prefix, payload, provides):
    # Reflection checks the actual loaded module. Correct stdout accompanied by a
    # startup warning must not turn a missing/incompatible module into success.
    args = [str(Path(prefix) / "bin/php"), "-n", "-d", "display_errors=stderr", "-d", "extension_dir=" + str(payload)]
    for name in sorted(provides, key=lambda value: value != "psr"):
        args.extend(["-d", ("zend_extension=" if name == "xdebug" else "extension=") + str(Path(payload) / (name + ".so"))])
    code = "echo json_encode(array_map(function($n){$r=new ReflectionExtension($n);return array($r->getName(),$r->getVersion());},array(" + ",".join(json.dumps(name) for name in provides) + ")));"
    result = subprocess.run(args + ["-r", code], check=True, capture_output=True, text=True, timeout=30)
    if result.stderr.strip():
        raise ValueError("PHP module loading emitted diagnostics: " + result.stderr)
    rows = json.loads(result.stdout)
    if [row[0].lower() for row in rows] != [name.lower() for name in provides] or any(not row[1] for row in rows):
        raise ValueError("Actual PHP modules did not report their names and versions")
    return rows


def build_module(record, host_archive, host_sha256, host_version, base_root, work_root,
                 producer, base, minimum_os, output_dir, jobs=2):
    if type(jobs) is not int or jobs < 1:
        raise ValueError("Module jobs must be a positive integer")
    base_root, work_root = Path(base_root).absolute(), Path(work_root).absolute()
    marker = json.loads((base_root / ".bootstrap-v2.json").read_text())
    expected = {"runtime_version": "v2", "arch": record["arch"], **base}
    if marker != expected:
        raise ValueError("The module build base marker differs from its exact archive identity")
    host_type = {"php-extension": "php", "postgresql-extension": "postgresql"}.get(record["kind"])
    if not host_type:
        raise ValueError("An independent PHP or PostgreSQL module identity is required")
    series = record["host"]["series"]
    prefix = base_root / "packages" / host_type / series
    if prefix.exists():
        _hosts.verify_host_archive(host_archive, host_sha256, prefix, host_type, series)
    else:
        prefix = _hosts.extract_host_archive(host_archive, host_sha256, base_root / "packages", host_type, series, host_version)
    probe = (_hosts.probe_php_host if host_type == "php" else _hosts.probe_postgresql_host)(prefix, host_version, host_sha256, record["arch"])
    host = probe["host"]
    if record["host"] != {key: host[key] for key in ("type", "series", "abi")}:
        raise ValueError("The requested module identity differs from the exact host's actual ABI")
    recipe = record.get("recipe_name", record["name"])
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", recipe):
        raise ValueError("Invalid module recipe name")
    if record["name"] != {"phpredis": "redis", "pgvector": "vector"}.get(recipe, recipe):
        raise ValueError("The module release name differs from its actual recipe")
    bundled = record.get("bundled", {})
    needs_psr = recipe == "phalcon" and record["version"].startswith("4.") and series in ("7.2", "7.3", "7.4")
    if not isinstance(bundled, dict) or (needs_psr and set(bundled) != {"psr"}) or (not needs_psr and bundled):
        raise ValueError("The exact module recipe needs its actual bundled PSR ownership")
    if needs_psr:
        _manifest._state._text(bundled["psr"], "bundled PSR source version")
    work_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SERVBAY_BASE_FULL_PATH=str(base_root), SERVBAY_BUILD_ROOT=str(work_root), SERVBAY_MODULE_JOBS=str(jobs),
               SERVBAY_PSR_BUNDLE_VERSION=bundled.get("psr", ""))
    script = Path(__file__).parents[1] / "scripts/build_module.sh"
    with tempfile.TemporaryDirectory(prefix=".module-stage-", dir=work_root) as directory:
        stage = Path(directory) / "install"
        stage.mkdir()
        subprocess.run(["/bin/zsh", str(script), record["kind"], recipe, record["version"], str(prefix), host_version,
                        series, record["arch"], str(stage)], env=env, check=True)
        if host_type == "php":
            payload, configs, provides = collect_php(stage, prefix, series, probe["extension_dir"], Path(directory) / "payload")
            if set(provides) != {record["name"], *bundled}:
                raise ValueError("Fresh PHP payload differs from its exact module/bundled ownership")
            loaded = verify_php_payload(prefix, payload, provides)
            if any(dict(loaded).get(name) != version for name, version in bundled.items()):
                raise ValueError("The actual bundled PHP module version differs from its explicit source")
            actual_version = next((version for name, version in loaded if name.lower() == record["name"].lower()), None)
            # Some PHP modules report their own binding version instead of the
            # library release version: scws's phpext reports "0.2.4" while the
            # library is "1.2.4-pre". An explicit mapping bridges the two.
            MODULE_VERSION_OVERRIDES = {"scws": "0.2.4"}
            expected_version = MODULE_VERSION_OVERRIDES.get(record["name"], record["version"])
            expected_version = expected_version.removesuffix("-pre")
            if actual_version != expected_version and actual_version != record["version"]:
                raise ValueError(f"The actual PHP module version {actual_version!r} differs from its expected {expected_version!r}")
        else:
            payload, provides = collect_postgresql(stage, Path(probe["install_prefix"]), Path(directory) / "payload")
            configs = []
            requirements = _dependencies.classify_requirements(payload, host_archive, host)
            if set(requirements['external']) != set(record.get('required_modules', {})):
                raise ValueError("Actual PG independent requirements differ from the selected profile")
        verify_native_payload(payload, record["arch"])
        manifest = _manifest.create_manifest(host, payload, configs, provides, record.get("dependencies", []),
                                             arch=record["arch"], minimum_os=minimum_os, base=base,
                                             source_name=recipe, extension_dir=probe.get("extension_dir"))
        if host_type == "postgresql" and requirements['external']:
            return _dependencies.write_candidate(manifest, payload, record, producer,
                primary_source(work_root, recipe, record["version"]), output_dir, requirements['requires'])
        return _manifest.write_package(manifest, payload, record, producer, primary_source(work_root, recipe, record["version"]), output_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="Exact unit, host archive/SHA, producer and pinned base JSON")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text())
    print(json.dumps(build_module(**request), sort_keys=True))


if __name__ == "__main__":
    main()
