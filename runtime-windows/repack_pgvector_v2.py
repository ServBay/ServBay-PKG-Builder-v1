#!/usr/bin/env python3
"""Validate a Windows pgvector build handoff and create a diagnostic module 7z."""

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location("pgvector_repack_" + name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_meta = _load("package_meta", "runtime/ci/package_meta.py")
module_manifest = _load("module_manifest", "runtime-v2/ci/module_manifest.py")
windows_archive = _load("windows_archive", "runtime/ci/windows_archive.py")
_BUILD_KEYS = {"schema", "publishable_v2", "native_validation", "payload_root", "source",
               "host", "toolchain", "files", "compatibility", "minimum_os"}
_IDENTITY_KEYS = {"runtime_version", "os", "kind", "name", "version", "arch",
                  "build_revision", "host"}
_CORE_KEYS = {"sha256", "version", "series"}
_NATIVE_KEYS = {
    "schema", "source_sha256", "core_archive_sha256", "built_dll_sha256",
    "install_file_count", "sql_file_count", "compiler", "processor_architecture_code",
    "process_environment_architecture", "extension_version", "vector_distance_l2_squared",
    "create_extension_exit", "drop_and_recreate_exit", "stop_exit",
    "remaining_processes", "listeners",
}


def _json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = value
        return result
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            value = json.load(stream, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        raise ValueError("Cannot read pgvector JSON: " + Path(path).name) from error
    if not isinstance(value, dict):
        raise ValueError("pgvector JSON must be an object")
    return value


def _sha(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid " + field + " SHA256")
    return value


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _contains(value, key):
    if key in value:
        raise ValueError("pgvector control has a duplicate field: " + key)
    return value


def _control(path):
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-z_]+)\s*=\s*'([^']*)'\s*", line.strip())
        if match:
            _contains(fields, match.group(1))
            fields[match.group(1)] = match.group(2)
    return fields


def repack_pgvector(build_dir, identity, core, output_dir, *, seven_zip=None):
    """Package an exact Windows pgvector build install tree without release metadata."""
    build_dir = Path(build_dir).absolute()
    if build_dir.is_symlink() or not build_dir.is_dir():
        raise ValueError("pgvector build directory must be real")
    audit_path = build_dir / "build-audit.json"
    install = build_dir / "install"
    if install.is_symlink() or not install.is_dir():
        raise ValueError("pgvector install directory must be real")
    audit = _json(audit_path)
    if set(audit) != _BUILD_KEYS:
        raise ValueError("pgvector build audit has unexpected or missing fields")
    if (audit["schema"] != "servbay.windows-pgvector-build.v1"
            or audit["publishable_v2"] is not False
            or audit["native_validation"] != "pending"
            or audit["payload_root"] != "install"):
        raise ValueError("pgvector build audit is not an exact pending handoff")

    if not isinstance(identity, dict) or set(identity) != _IDENTITY_KEYS:
        raise ValueError("An explicit pgvector module identity is required")
    row = package_meta._state.clean_record(identity)
    if (row["runtime_version"] != "v2" or row["os"] != "windows"
            or row["kind"] != "postgresql-extension" or row["name"] != "vector"
            or row["arch"] != "x86_64"):
        raise ValueError("Expected Windows v2 x86_64 pgvector identity")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", row["version"]):
        raise ValueError("pgvector version must be source major.minor.patch")
    host = row["host"]
    if host["type"] != "postgresql" or not re.fullmatch(r"[0-9]+", host["series"]):
        raise ValueError("pgvector needs a PostgreSQL host")
    if not isinstance(host["abi"], dict) or host["abi"] != {"major": int(host["series"])}:
        raise ValueError("pgvector PostgreSQL ABI must be exactly its major")

    if not isinstance(core, dict) or set(core) != _CORE_KEYS:
        raise ValueError("pgvector core binding is incomplete")
    _sha(core["sha256"], "core")
    if not isinstance(core["version"], str) or core["version"].split(".", 1)[0] != host["series"]:
        raise ValueError("pgvector core version differs from its host series")
    if core["series"] != host["series"]:
        raise ValueError("pgvector core binding series mismatch")

    source = audit["source"]
    if (not isinstance(source, dict)
            or set(source) != {"filename", "version", "bytes", "sha256"}
            or source["version"] != row["version"]
            or type(source["bytes"]) is not int or source["bytes"] <= 0):
        raise ValueError("pgvector source audit differs from its module identity")
    _sha(source["sha256"], "source")
    audit_host = audit["host"]
    if (not isinstance(audit_host, dict)
            or set(audit_host) != {"core_package_sha256", "postgresql_version", "core_root_layout"}
            or audit_host["core_package_sha256"] != core["sha256"]
            or audit_host["postgresql_version"] != "PostgreSQL " + core["version"]
            or audit_host["core_root_layout"] != "postgresql/<major>"):
        raise ValueError("pgvector build audit core binding differs")
    toolchain = audit["toolchain"]
    if (not isinstance(toolchain, dict)
            or set(toolchain) != {"requested", "actual_compiler", "crt"}
            or not isinstance(toolchain["actual_compiler"], str)
            or not toolchain["actual_compiler"]
            or toolchain["crt"] != "pending"):
        raise ValueError("pgvector toolchain evidence is incomplete")
    if audit["compatibility"] != "pending" or audit["minimum_os"] != "pending":
        raise ValueError("pgvector publish gates must remain pending")

    actual_files = windows_archive.staging_files(install)
    required = {
        "lib/vector.dll",
        "share/extension/vector.control",
        "share/extension/vector--" + row["version"] + ".sql",
        "include/server/extension/vector/vector.h",
        "include/server/extension/vector/halfvec.h",
        "include/server/extension/vector/sparsevec.h",
    }
    if not required <= set(actual_files):
        raise ValueError("pgvector install lacks required files")
    for path in actual_files:
        if not (path == "lib/vector.dll" or path == "share/extension/vector.control"
                or re.fullmatch(r"share/extension/vector--[0-9A-Za-z._-]+\.sql", path)
                or re.fullmatch(r"include/server/extension/vector/(?:vector|halfvec|sparsevec)\.h", path)):
            raise ValueError("Unexpected pgvector install path: " + path)

    control = _control(install / "share/extension/vector.control")
    if (control.get("default_version") != row["version"]
            or control.get("module_pathname") != "$libdir/vector"):
        raise ValueError("pgvector control identity differs")

    declared = audit["files"]
    if not isinstance(declared, list) or any(not isinstance(item, dict) for item in declared):
        raise ValueError("pgvector audit file list is invalid")
    by_path = {}
    for item in declared:
        if set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("pgvector audit file fields are invalid")
        path = item["path"]
        if path in by_path or path not in actual_files or type(item["bytes"]) is not int:
            raise ValueError("pgvector audit file identity is invalid")
        _sha(item["sha256"], "pgvector file")
        by_path[path] = item
    if set(by_path) != set(actual_files):
        raise ValueError("pgvector audit and install file sets differ")
    for path, item in by_path.items():
        actual = install / path
        if actual.stat().st_size != item["bytes"] or _hash(actual) != item["sha256"]:
            raise ValueError("pgvector installed file bytes/SHA differ: " + path)

    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("pgvector output must be a new directory")
    if not output.parent.is_dir():
        raise ValueError("pgvector output parent must exist")
    tool = windows_archive._tool(seven_zip)
    filename = f"ext-vector-{row['version']}-pgsql{host['series']}-windows-x86_64.7z"
    with tempfile.TemporaryDirectory(prefix="servbay-pgvector-package-") as temporary:
        temporary = Path(temporary)
        archive = temporary / filename
        windows_archive.create_archive(install, archive, actual_files, tool)
        hashes = windows_archive.archive_hashes(archive, tool)
        expected = {path: by_path[path]["sha256"] for path in actual_files}
        if hashes != expected:
            raise ValueError("pgvector 7z file set or SHA differs")
        receipt = {
            "schema": "servbay.windows-pgvector-repack.v1",
            "publishable_v2": False,
            "identity": row,
            "core": core,
            "source": source,
            "toolchain": toolchain,
            "build_audit_sha256": _hash(audit_path),
            "files": [by_path[path] for path in sorted(by_path)],
            "archive": {"filename": filename, "bytes": archive.stat().st_size,
                        "sha256": _hash(archive)},
            "compatibility": "pending",
            "minimum_os": "pending",
        }
        bundle = temporary / "bundle"
        bundle.mkdir()
        shutil.copyfile(archive, bundle / filename)
        (bundle / "pgvector-repack-manifest.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        pending = Path(tempfile.mkdtemp(prefix="." + output.name + ".pending-", dir=output.parent))
        try:
            for name in (filename, "pgvector-repack-manifest.json"):
                shutil.copyfile(bundle / name, pending / name)
            if (_hash(pending / filename) != receipt["archive"]["sha256"]
                    or (pending / filename).stat().st_size != receipt["archive"]["bytes"]):
                raise ValueError("Published pgvector archive differs from its audit")
            pending.rename(output)
        except Exception:
            if pending.exists():
                shutil.rmtree(pending)
            raise
    return receipt


def _native_evidence(path, receipt):
    value = _json(path)
    if not isinstance(value, dict) or set(value) != _NATIVE_KEYS:
        raise ValueError("pgvector native evidence has unexpected or missing fields")
    if value["schema"] != "servbay.windows-pgvector-native.v1":
        raise ValueError("Unsupported pgvector native evidence schema")
    expected = {
        "source_sha256": receipt["source"]["sha256"],
        "core_archive_sha256": receipt["core"]["sha256"],
        "built_dll_sha256": next(row["sha256"] for row in receipt["files"] if row["path"] == "lib/vector.dll"),
        "install_file_count": len(receipt["files"]),
        "sql_file_count": sum(path.startswith("share/extension/vector--") and path.endswith(".sql")
                              for path in (row["path"] for row in receipt["files"])),
        "compiler": receipt["toolchain"]["actual_compiler"],
        "extension_version": receipt["identity"]["version"],
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError("pgvector native evidence differs: " + key)
    if value["processor_architecture_code"] not in (9, 12) or value["process_environment_architecture"] != "AMD64":
        raise ValueError("pgvector native evidence does not prove an AMD64 process")
    if (type(value["vector_distance_l2_squared"]) not in (int, float)
            or value["vector_distance_l2_squared"] <= 0):
        raise ValueError("pgvector native query evidence is incomplete")
    for key in ("create_extension_exit", "drop_and_recreate_exit", "stop_exit",
                "remaining_processes", "listeners"):
        if type(value[key]) is not int or value[key] != 0:
            raise ValueError("pgvector native cleanup/query evidence failed: " + key)
    return value


def finalize_pgvector(build_dir, identity, core, native_result, source_path, source_url,
                      producer, minimum_os, windows_compatibility, output_dir, *, seven_zip=None):
    """Bind passed native evidence to a schema-2 module package and metadata."""
    build_dir = Path(build_dir).absolute()
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("pgvector final output must be a new directory with an existing parent")
    if (output.resolve().is_relative_to(build_dir.resolve())
            or build_dir.resolve().is_relative_to(output.resolve())):
        raise ValueError("pgvector final output must be separate from the build directory")
    source_path = Path(source_path).absolute()
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("pgvector source must be a real regular archive")
    if not isinstance(windows_compatibility, dict) or set(windows_compatibility) != {
            "distribution", "toolchain", "crt"} or any(
                not isinstance(value, str) or not value.strip() for value in windows_compatibility.values()):
        raise ValueError("Windows compatibility needs distribution, toolchain and crt")
    if not isinstance(minimum_os, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", minimum_os):
        raise ValueError("pgvector minimum_os must be explicit")

    with tempfile.TemporaryDirectory(prefix="servbay-pgvector-finalize-") as temporary:
        temporary = Path(temporary)
        diagnostic = repack_pgvector(build_dir, identity, core, temporary / "diagnostic",
                                     seven_zip=seven_zip)
        native = _native_evidence(native_result, diagnostic)
        source_bytes = source_path.stat().st_size
        source_sha = _hash(source_path)
        if source_bytes != diagnostic["source"]["bytes"] or source_sha != diagnostic["source"]["sha256"]:
            raise ValueError("pgvector source file differs from the build audit")
        source = {"path": str(source_path), "version": diagnostic["identity"]["version"],
                  "sha256": source_sha, "bytes": source_bytes, "url": source_url}
        host = {"type": "postgresql", "series": diagnostic["core"]["series"],
                "version": diagnostic["core"]["version"],
                "package_sha256": diagnostic["core"]["sha256"],
                "abi": {"major": int(diagnostic["core"]["series"])}}
        manifest = module_manifest.create_manifest(
            host, build_dir / "install", [], ["vector"], [], arch="x86_64",
            minimum_os=minimum_os, base=None, source_name="pgvector", os_name="windows",
            windows_compatibility=copy.deepcopy(windows_compatibility),
        )
        staged_output = temporary / "output"
        written = module_manifest.write_package(
            manifest, build_dir / "install", identity, producer, source, staged_output,
            seven_zip=seven_zip,
        )
        archive = Path(written["package"])
        manifest_path = Path(written["manifest"])
        meta_path = Path(written["meta_path"])
        shutil.copyfile(native_result, staged_output / "native-result.json")
        shutil.copyfile(build_dir / "build-audit.json", staged_output / "build-audit.json")
        receipt = {
            "schema": "servbay.windows-pgvector-finalization.v1",
            "native_validation": "passed",
            "minimum_os": minimum_os,
            "windows_compatibility": copy.deepcopy(windows_compatibility),
            "native_result": {"filename": "native-result.json", "sha256": _hash(
                staged_output / "native-result.json")},
            "build_audit": {"filename": "build-audit.json", "sha256": _hash(
                staged_output / "build-audit.json")},
            "archive": {"filename": archive.name, "bytes": archive.stat().st_size,
                        "sha256": _hash(archive)},
            "meta": {"filename": meta_path.name, "sha256": _hash(meta_path)},
            "module_manifest": {"filename": manifest_path.name,
                                "sha256": _hash(manifest_path)},
            "native": native,
        }
        (staged_output / "pgvector-finalization.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(staged_output, output)
    return {key: (output / value["filename"] if isinstance(value, dict) and "filename" in value else value)
            for key, value in receipt.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seven-zip")
    parser.add_argument("--native-result", type=Path)
    parser.add_argument("--source-path", type=Path)
    parser.add_argument("--source-url")
    parser.add_argument("--producer", type=Path)
    parser.add_argument("--minimum-os")
    parser.add_argument("--windows-compatibility", type=Path)
    args = parser.parse_args()
    try:
        if args.native_result:
            if not all((args.source_path, args.source_url, args.producer,
                        args.minimum_os, args.windows_compatibility)):
                parser.error("finalization requires source/producer/minimum-os/windows-compatibility")
            result = finalize_pgvector(
                args.build_dir, _json(args.identity), _json(args.core), args.native_result,
                args.source_path, args.source_url, _json(args.producer), args.minimum_os,
                _json(args.windows_compatibility), args.output_dir, seven_zip=args.seven_zip,
            )
        else:
            result = repack_pgvector(args.build_dir, _json(args.identity), _json(args.core),
                                     args.output_dir, seven_zip=args.seven_zip)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"pgvector repack failed: {error}\n")
    if args.native_result:
        rendered = {key: str(value) if isinstance(value, Path) else value for key, value in result.items()}
        print(json.dumps(rendered, indent=2, ensure_ascii=False, default=str))
    else:
        print(json.dumps({"archive": result["archive"],
                          "manifest": "pgvector-repack-manifest.json",
                          "files": len(result["files"])}, indent=2))


if __name__ == "__main__":
    main()
