#!/usr/bin/env python3
"""Resolve existing package profiles without duplicating version discovery."""

import argparse
import importlib.util
import json
import os
from pathlib import Path


_spec = importlib.util.spec_from_file_location("package_updater", Path(__file__).parents[1] / "update_packages.py")
_updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_updater)

_php_spec = importlib.util.spec_from_file_location("servbay_php_source", Path(__file__).parents[2] / "runtime-v2/ci/php_source.py")
_php_source = importlib.util.module_from_spec(_php_spec)
_php_spec.loader.exec_module(_php_source)


def profile_record(conf, package, version):
    package = "httpd" if package == "apache" else package
    updater = _updater.PackageUpdater(str(conf), runtime_version="v2")
    rows = [updater._v2_record(row) for row in updater._v2_rows()
            if row["name"] == package and row["version"] == version]
    if len(rows) != 1:
        raise ValueError(f"Expected one exact v2 profile for {package} {version}")
    return updater, rows[0]


def build_matrix(runtime_version, package, version, arch, conf, revision=0):
    if runtime_version not in ("v1", "v2") or arch not in ("all", "x86_64", "arm64"):
        raise ValueError("Invalid build runtime or architecture")
    if runtime_version == "v2" and (type(revision) is not int or revision < 0):
        raise ValueError("build_revision must be a nonnegative integer")
    if runtime_version == "v1":
        rows = [{"arch": target} for target in ("x86_64", "arm64") if arch in ("all", target)]
    else:
        updater, record = profile_record(conf, package, version)
        record["build_revision"] = revision
        rows = [row for row in updater.build_matrix([record])["include"] if arch in ("all", row["arch"])]
        if not rows:
            raise ValueError(f"The v2 profile has no {arch} input for {package} {version}")
    for row in rows:
        row["runner"] = "macos-26-intel" if row["arch"] == "x86_64" else "macos-26"
    return {"include": rows}


def workflow_plan(records, max_groups=256):
    """Bound the caller matrix while retaining exact units for result collection."""
    if type(max_groups) is not int or not 1 <= max_groups <= 256:
        raise ValueError("Actions supports between one and 256 parent matrix groups")
    groups = {}
    for record in records:
        if record.get("runtime_version") != "v2" or record.get("kind") != "package":
            raise ValueError("The ordinary v2 workflow planner only accepts package build units")
        if record.get("arch") not in ("arm64", "x86_64"):
            raise ValueError("Each pending unit needs its exact architecture")
        row = {key: value for key, value in record.items() if key not in ("arch", "runner")}
        key = json.dumps(row, sort_keys=True, separators=(",", ":"))
        group = groups.setdefault(key, {"row": row, "arches": set()})
        if record["arch"] in group["arches"]:
            raise ValueError("Duplicate pending architecture unit")
        group["arches"].add(record["arch"])
    selected = set(list(groups)[:max_groups])
    matrix = []
    for key, group in groups.items():
        if key in selected:
            row = dict(group["row"], arch="all" if len(group["arches"]) == 2 else next(iter(group["arches"])))
            matrix.append(row)
    scheduled, deferred = [], []
    for record in records:
        row = {key: value for key, value in record.items() if key not in ("arch", "runner")}
        key = json.dumps(row, sort_keys=True, separators=(",", ":"))
        (scheduled if key in selected else deferred).append(dict(record))
    return {"matrix": {"include": matrix}, "records": scheduled, "deferred": deferred}


def find_source(profile_root, package, version, arch, conf, gomodcache=None):
    _, record = profile_record(conf, package, version)
    field = "x86_file" if arch == "x86_64" else "arm_file"
    filename = record[field]
    if filename == "-":
        raise ValueError("The selected architecture has no upstream input")
    root = Path(profile_root)
    if package == "caddy":
        if not gomodcache:
            raise ValueError("Caddy needs the exact Go module cache used by xcaddy")
        path = Path(gomodcache) / f"cache/download/github.com/caddyserver/caddy/v2/@v/v{version}.zip"
    else:
        extensions = ("tar.gz", "tar.bz2", "tar.xz", "tgz", "zip", "phar", "php")
        extension = next((suffix for suffix in extensions if filename.endswith("." + suffix)), None)
        if extension == "tgz":
            extension = "tar.gz"
        if package == "ngrok":
            extension = "zip"
        elif package == "pinggy" or filename.startswith("meilisearch-"):
            extension = "bin"
        if extension is None:
            raise ValueError(f"Unknown source archive type for {package}")
        path = root / "src" / f"{record['name']}-{version}.{extension}"
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Actual primary upstream input is missing for {package} {version}")
    upstream = _php_source.source_version(version) if package == "php" else version
    return {"version": upstream, "path": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("matrix", "source", "profile"))
    parser.add_argument("--runtime-version", default="v1", choices=("v1", "v2"))
    parser.add_argument("--pkg", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", default="all")
    parser.add_argument("--conf", default="runtime-v2/config/packages.conf")
    parser.add_argument("--build-revision", type=int, default=0)
    parser.add_argument("--profile-root", default="runtime-v2")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "matrix":
        data = build_matrix(args.runtime_version, args.pkg, args.version, args.arch, args.conf, args.build_revision)
    elif args.command == "source":
        data = find_source(args.profile_root, args.pkg, args.version, args.arch, args.conf, os.environ.get("GOMODCACHE"))
    else:
        _, record = profile_record(args.conf, args.pkg, args.version)
        Path(args.output).write_text("\t".join(record[key] for key in
                                             ("name", "version", "x86_file", "arm_file", "build_type")) + "\n")
        return
    Path(args.output).write_text(json.dumps(data, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
