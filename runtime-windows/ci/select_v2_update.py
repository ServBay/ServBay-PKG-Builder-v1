#!/usr/bin/env python3
"""Select one explicit ordinary Windows v2 update from the public catalogs."""

import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = _load("selected_windows_batch", Path(__file__).with_name("build_v2_batch.py"))
repacker = _load("selected_windows_repacker", ROOT / "runtime-windows/repack_v2.py")


def load_catalog(path, runtime_version):
    if path is not None:
        payload = json.loads(Path(path).read_text())
    else:
        params = dict(batch.PARAMS, runtime_version=runtime_version,
                      cpu_arch="x64" if runtime_version == "v1" else "x86_64")
        response = requests.get(batch.API, params=params, timeout=(10, 30))
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("status_code", 200) != 200:
        raise ValueError("Package catalog did not return a successful object")
    data = payload.get("data", payload)
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        raise ValueError("Package catalog requires an explicit packages list")
    return data["packages"]


def _versions(packages, name):
    if not isinstance(packages, list):
        raise ValueError("Package catalog requires a list")
    result = []
    # Filter first: unrelated packages and their version representations are not inputs.
    for package in packages:
        if not isinstance(package, dict) or package.get("name") != name:
            continue
        if not isinstance(package.get("versions"), list):
            raise ValueError("Selected package requires an explicit versions list")
        result.extend(package["versions"])
    return result


def _version(row, name, runtime_version):
    arch = "x64" if runtime_version == "v1" else "x86_64"
    if (not isinstance(row, dict) or row.get("os") != "Windows"
            or row.get("runtimeVersion") != runtime_version or row.get("arch") != arch):
        raise ValueError("Selected package has a different OS, runtime or architecture")
    value = row.get("latestVersion")
    repacker.package_meta._state._text(value, "selected package version")
    parsed = Version(value)
    major = repacker.package_meta.package_metadata(name, value)["major"]
    if runtime_version == "v2" and row.get("majorVersion") != major:
        raise ValueError("Selected v2 catalog major differs from its producer mapping")
    return parsed, major


def _source(row):
    if not isinstance(row.get("hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["hash"]):
        raise ValueError("Selected source requires its real SHA256")
    if not isinstance(row.get("downloadUrl"), str) or any(c.isspace() for c in row["downloadUrl"]):
        raise ValueError("Selected source requires its real public HTTPS URL")
    url = urlsplit(row["downloadUrl"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or "." not in url.hostname
            or url.hostname.endswith((".local", ".internal", ".localhost"))):
        raise ValueError("Selected source URL must be public HTTPS without credentials")
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Selected source URL must be public")


def select_update(v1_packages, v2_packages, *, package_name, major="", build_revision=0,
                  minimum_os="10.0"):
    if package_name not in repacker.SUPPORTED or package_name in {"ai-gateway", "simple-acme"}:
        raise ValueError("An explicit supported ordinary Windows package is required")
    if type(build_revision) is not int or build_revision < 0:
        raise ValueError("build_revision must be a nonnegative integer")
    if not isinstance(minimum_os, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", minimum_os):
        raise ValueError("An explicit numeric minimum_os is required")
    sources = _versions(v1_packages, package_name)
    if not sources:
        raise ValueError("Selected package has no published v1 source")
    for row in sources:
        _version(row, package_name, "v1")
        _source(row)
    selected, _ = batch.build([dict(name=package_name, versions=sources)],
                              repacker.package_meta.package_metadata)
    if major:
        selected = [row for row in selected if
                    repacker.package_meta.package_metadata(package_name, row["version"])["major"] == major]
        if not selected:
            raise ValueError("Selected major has no published v1 source")
    targets = {}
    for row in _versions(v2_packages, package_name):
        parsed, series = _version(row, package_name, "v2")
        if series not in targets or parsed > targets[series]:
            targets[series] = parsed
    pending = []
    for row in selected:
        series = repacker.package_meta.package_metadata(package_name, row["version"])["major"]
        source_version = Version(row["version"])
        target_version = targets.get(series)
        if target_version is not None:
            if package_name == "openjdk" and len(source_version.release) != len(target_version.release):
                raise ValueError(
                    "OpenJDK version representations require source verification "
                    f"({row['version']} vs {target_version}); no update plan was generated")
            if source_version <= target_version:
                continue
        pending.append(dict(row, major=series, minimum_os=minimum_os, build_revision=build_revision))
    if len(pending) > 1:
        raise ValueError("Multiple pending series require an explicit major")
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--major", default="")
    parser.add_argument("--build-revision", type=int, default=0)
    parser.add_argument("--minimum-os", default="10.0")
    parser.add_argument("--v1-catalog", type=Path)
    parser.add_argument("--v2-catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        rows = select_update(load_catalog(args.v1_catalog, "v1"), load_catalog(args.v2_catalog, "v2"),
                             package_name=args.package, major=args.major,
                             build_revision=args.build_revision, minimum_os=args.minimum_os)
        args.output.write_text(json.dumps({"include": rows}, indent=2) + "\n")
    except (ValueError, OSError, requests.RequestException) as error:
        parser.exit(1, f"Windows v2 selection failed: {error}\n")


if __name__ == "__main__":
    main()
