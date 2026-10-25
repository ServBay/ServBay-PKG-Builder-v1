#!/usr/bin/env python3
"""Map a pinned EDB PostgreSQL ZIP into diagnostic or formal Windows v2 core output."""

import argparse
import copy
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from urllib.parse import urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_BINARIES = {
    "postgres.exe", "pg_config.exe", "initdb.exe", "pg_ctl.exe", "psql.exe",
}
IDENTITY_FIELDS = {"runtime_version", "os", "kind", "name", "version", "arch", "build_revision"}
SOURCE_FIELDS = {"path", "version", "sha256", "bytes"}
PE_MACHINES = {
    0x14C: "x86",
    0x8664: "x86_64",
    0xAA64: "arm64",
}
_NATIVE_KEYS = {
    "schema", "archive", "os_architecture", "process_architecture", "processor_name",
    "processor_architecture_code", "process_environment_architecture", "pg_config",
    "executables", "database",
}


def _load(name, relative):
    spec = importlib.util.spec_from_file_location("postgresql_repack_" + name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_meta = _load("package_meta", "runtime/ci/package_meta.py")
package_layout = _load("package_layout", "runtime/ci/package_layout.py")
windows_archive = _load("windows_archive", "runtime/ci/windows_archive.py")
core_manifest = _load("core_manifest", "runtime-v2/ci/core_manifest.py")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _sha256(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid " + field + " SHA256")
    return value


def _source(source):
    if not isinstance(source, dict) or not SOURCE_FIELDS <= set(source) or set(source) - SOURCE_FIELDS - {"url"}:
        raise ValueError("Source requires exact path/version/sha256/bytes and optional public URL")
    if not isinstance(source["version"], str) or not re.fullmatch(r"[0-9]+[.][0-9]+", source["version"]):
        raise ValueError("PostgreSQL source version must be a stable major.minor")
    _sha256(source["sha256"], "source")
    if type(source["bytes"]) is not int or source["bytes"] <= 0:
        raise ValueError("Source bytes must be a positive integer")
    if not isinstance(source["path"], (str, Path)) or not str(source["path"]):
        raise ValueError("Source path must identify a local ZIP")
    original = Path(source["path"]).absolute()
    if not stat.S_ISREG(original.lstat().st_mode):
        raise ValueError("Source must be a regular ZIP, not a link or directory")
    windows_archive.validate_path(original.name)
    if "/" in original.name or not original.name.lower().endswith(".zip"):
        raise ValueError("Source must be an explicit top-level ZIP basename")
    if original.stat().st_size != source["bytes"] or package_meta.sha256(original) != source["sha256"]:
        raise ValueError("Source SHA256/bytes mismatch")
    if "url" in source:
        value = source["url"]
        if not isinstance(value, str):
            raise ValueError("Source URL must be public HTTPS")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("Source URL must be public HTTPS without credentials/query/fragment")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if "." not in parsed.hostname or parsed.hostname.endswith((".local", ".internal", ".localhost")):
                raise ValueError("Source URL must use a public hostname")
        else:
            if not address.is_global:
                raise ValueError("Source URL must use a public address")
    return original


def _identity(identity):
    if not isinstance(identity, dict) or set(identity) != IDENTITY_FIELDS:
        raise ValueError("An explicit complete PostgreSQL package identity is required")
    row = package_meta._state.clean_record(identity)
    if (row["runtime_version"] != "v2" or row["os"] != "windows"
            or row["kind"] != "package" or row["name"] != "postgresql"
            or row["arch"] != "x86_64"):
        raise ValueError("Expected a v2 Windows x86_64 PostgreSQL package identity")
    if not isinstance(row["version"], str) or not re.fullmatch(r"[0-9]+[.][0-9]+", row["version"]):
        raise ValueError("PostgreSQL package version must be a stable major.minor")
    if int(row["version"].split(".", 1)[0]) < 10:
        raise ValueError("Windows PostgreSQL v2 requires major 10 or newer")
    return row


def _zip_inventory(archive):
    """Return path/info/file-size records after validating the complete namespace."""
    files, directories, records = [], [], {}
    try:
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            if not infos:
                raise ValueError("Source ZIP is empty")
            for info in infos:
                if info.orig_filename != info.filename or "\x00" in info.filename:
                    raise ValueError("Invalid or truncated ZIP filename")
                directory = info.is_dir()
                name = info.filename[:-1] if directory else info.filename
                windows_archive.validate_path(name)
                if not name.startswith("pgsql/") and name != "pgsql":
                    raise ValueError("Source ZIP has content outside the unique pgsql root")
                if name == "pgsql" and not directory:
                    raise ValueError("The pgsql root must be a directory")
                if info.flag_bits & 1:
                    raise ValueError("Encrypted ZIP members are unsupported")

                source.fp.seek(info.header_offset)
                local_header = source.fp.read(30)
                if len(local_header) != 30 or local_header[:4] != b"PK\x03\x04":
                    raise ValueError("Invalid ZIP local file header")
                local_flags = struct.unpack_from("<H", local_header, 6)[0]
                if local_flags & 1:
                    raise ValueError("Encrypted ZIP members are unsupported")
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode) if info.create_system == 3 else 0
                if ((kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG))
                        or (bool(info.external_attr & 0x10) and not directory)
                        or (directory and info.file_size != 0)):
                    raise ValueError("ZIP links, special files or conflicting member types are unsupported")
                (directories if directory else files).append(name)
                if not directory:
                    records[name] = info
            windows_archive.validate_paths(files, directories)
            if not files:
                raise ValueError("Source ZIP has no regular files")
            inventory = {}
            for name, info in records.items():
                value = hashlib.sha256()
                size = 0
                with source.open(info) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        value.update(block)
                        size += len(block)
                if size != info.file_size:
                    raise ValueError("ZIP member size differs from its header: " + name)
                inventory[name] = {"bytes": size, "sha256": value.hexdigest()}
            return records, inventory
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError) as error:
        raise ValueError("Cannot validate PostgreSQL source ZIP: " + str(error)) from error


def _pe_machine(data):
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset < 0x40 or offset + 6 > len(data) or data[offset:offset + 4] != b"PE\x00\x00":
        return None
    return struct.unpack_from("<H", data, offset + 4)[0]


def _postgresql_header(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("pg_config.h is not UTF-8") from error
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    versions = re.findall(r'(?m)^\s*#\s*define\s+PG_VERSION\s+"([^"\n]+)"\s*$', text)
    numbers = re.findall(r"(?m)^\s*#\s*define\s+PG_VERSION_NUM\s+([0-9]+)\s*$", text)
    if len(versions) != 1 or len(numbers) != 1:
        raise ValueError("pg_config.h requires exactly one effective PG_VERSION/PG_VERSION_NUM")
    if not re.fullmatch(r"[0-9]+[.][0-9]+", versions[0]):
        raise ValueError("pg_config.h PG_VERSION is not a stable major.minor")
    major_text, minor_text = versions[0].split(".", 1)
    major, minor = int(major_text), int(minor_text)
    expected_number = major * 10000 + minor
    if int(numbers[0]) != expected_number:
        raise ValueError("pg_config.h PG_VERSION_NUM does not match PG_VERSION")
    return {"version": versions[0], "version_num": expected_number, "major": major}


def _configuration_defaults(mapping, layout):
    result = []
    targets = set()
    for row in mapping:
        source = row["source_path"]
        if source.startswith("pgsql/share/") and source.endswith(".sample"):
            target = layout["config_root"] + "/" + Path(source).name
            if target.casefold() in targets:
                raise ValueError("Configuration sample target collision: " + target)
            targets.add(target.casefold())
            result.append({
                "archive_path": row["target_path"],
                "etc_relative_path": target,
                "kind": "upstream-default",
                "automatically_enabled": False,
                "sha256": row["sha256"],
            })
    return result


def _publish(bundle, output, filename):
    pending = Path(tempfile.mkdtemp(prefix="." + output.name + ".pending-", dir=output.parent))
    try:
        archive = pending / filename
        manifest = pending / "postgresql-repack-manifest.json"
        shutil.copyfile(bundle / filename, archive)
        shutil.copyfile(bundle / manifest.name, manifest)
        audit = json.loads(manifest.read_text(encoding="utf-8"))
        if (archive.stat().st_size != audit["archive"]["bytes"]
                or package_meta.sha256(archive) != audit["archive"]["sha256"]):
            raise ValueError("Published PostgreSQL archive differs from its audit")
        pending.rename(output)
    except Exception:
        if pending.exists():
            shutil.rmtree(pending)
        raise


def _verify_archive(archive, expected, tool, temporary):
    """Verify a large 7z in one listing and one guarded extraction.

    windows_archive.archive_hashes is intentionally literal and safe for small
    archives, but its per-member CLI reselection would launch tens of thousands
    of processes for the EDB tree. This path validates the complete listing
    before extracting into a new task-owned directory, then hashes actual bytes.
    """
    output = windows_archive._run(
        [tool, "l", "-t7z", "-slt", "-ba", "-sccUTF-8", "--", str(archive)]
    )
    files, directories = [], []
    for record in windows_archive.parse_listing(output):
        (directories if record["directory"] else files).append(record["path"])
    windows_archive.validate_paths(files, directories)
    if set(files) != set(expected):
        raise ValueError("Repacked PostgreSQL 7z file set differs")
    extracted = temporary / "verified"
    extracted.mkdir()
    windows_archive._run([tool, "x", "-t7z", "-y", "-o" + str(extracted), "--", str(archive)])
    actual = windows_archive.staging_files(extracted)
    if set(actual) != set(expected):
        raise ValueError("Extracted PostgreSQL 7z file set differs")
    for name in actual:
        if package_meta.sha256(extracted / name) != expected[name]:
            raise ValueError("Repacked PostgreSQL 7z member SHA256 differs: " + name)


def prepare_postgresql_core(source, identity, output_dir, *, seven_zip=None):
    """Create a complete verified diagnostic PostgreSQL core 7z and audit sidecar."""
    row = _identity(identity)
    original = _source(source)
    if source["version"] != row["version"]:
        raise ValueError("Source and package versions differ")
    layout = package_layout.package_layout(row["name"], row["version"], os=row["os"], arch=row["arch"])

    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output must be a new directory")
    if not output.parent.is_dir():
        raise ValueError("Output parent directory must already exist")
    if output == original:
        raise ValueError("Output cannot replace the source")
    tool = windows_archive._tool(seven_zip)

    with tempfile.TemporaryDirectory(prefix="servbay-windows-postgresql-") as temporary:
        temporary = Path(temporary)
        frozen = temporary / "source.zip"
        shutil.copyfile(original, frozen)
        if frozen.stat().st_size != source["bytes"] or package_meta.sha256(frozen) != source["sha256"]:
            raise ValueError("Source changed while freezing its input bytes")
        records, inventory = _zip_inventory(frozen)

        header_name = "pgsql/include/server/pg_config.h"
        if header_name not in inventory:
            raise ValueError("Source ZIP lacks include/server/pg_config.h")
        with zipfile.ZipFile(frozen) as archive:
            header_data = archive.read(records[header_name])
        header = _postgresql_header(header_data)
        if header["version"] != row["version"]:
            raise ValueError("pg_config.h version differs from the package identity")

        required = {"pgsql/bin/" + name for name in REQUIRED_BINARIES}
        missing = required - set(inventory)
        if missing:
            raise ValueError("Source ZIP lacks required PostgreSQL binaries: " + ", ".join(sorted(missing)))

        mapping = []
        for path in sorted(inventory):
            relative = path[len("pgsql/"):]
            target = layout["package_root"] + "/" + relative
            mapping.append({"source_path": path, "target_path": target,
                            **inventory[path]})
        windows_archive.validate_paths([row["target_path"] for row in mapping])
        targets = {row["target_path"] for row in mapping}
        if layout["entrypoint"]["path"] not in targets:
            raise ValueError("Mapped payload does not contain its declared entrypoint")

        required_binaries = []
        other_machine_counts = {}
        with zipfile.ZipFile(frozen) as archive:
            for path in sorted(inventory):
                if not path.lower().endswith(".exe"):
                    continue
                data = archive.read(records[path])
                machine = _pe_machine(data)
                machine_name = PE_MACHINES.get(machine, "unknown")
                top_level_bin = path.startswith("pgsql/bin/") and "/" not in path[len("pgsql/bin/"):]
                if top_level_bin:
                    if machine_name != "x86_64":
                        raise ValueError("Required PostgreSQL binary is not x86_64: " + path)
                    required_binaries.append({
                        "path": layout["package_root"] + "/" + path[len("pgsql/"):],
                        "machine": machine_name,
                        "sha256": inventory[path]["sha256"],
                    })
                else:
                    other_machine_counts[machine_name] = other_machine_counts.get(machine_name, 0) + 1

        stage = temporary / "stage"
        stage.mkdir()
        expected = {}
        with zipfile.ZipFile(frozen) as archive:
            for record in mapping:
                data = archive.read(records[record["source_path"]])
                if len(data) != record["bytes"] or _hash(data) != record["sha256"]:
                    raise ValueError("Source member bytes/SHA changed: " + record["source_path"])
                target = stage / record["target_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                expected[record["target_path"]] = record["sha256"]
        if set(windows_archive.staging_files(stage)) != set(expected):
            raise ValueError("Mapped staging file set differs from the source inventory")

        bundle = temporary / "bundle"
        bundle.mkdir()
        filename = f"postgresql-{row['version']}-windows-{row['arch']}.7z"
        archive = bundle / filename
        windows_archive.create_archive(stage, archive, expected, tool)
        _verify_archive(archive, expected, tool, temporary)

        defaults = _configuration_defaults(mapping, layout)
        retained_contrib = [row["target_path"] for row in mapping
                            if row["source_path"].startswith("pgsql/share/extension/")
                            and row["source_path"].endswith(".control")]
        source_record = {"filename": original.name, "version": source["version"],
                         "bytes": source["bytes"], "sha256": source["sha256"]}
        if "url" in source:
            source_record["url"] = source["url"]
        audit = {
            "schema": "servbay.windows-postgresql-repack.v1",
            "publishable_v2": False,
            "native_validation": "pending",
            "identity": row,
            "layout": layout,
            "source": source_record,
            "source_root": "pgsql",
            "postgresql_header": header,
            "required_binaries": required_binaries,
            "other_pe_machine_counts": dict(sorted(other_machine_counts.items())),
            "configuration_defaults": defaults,
            "retained_contrib": retained_contrib,
            "files": mapping,
            "archive": {"filename": filename, "bytes": archive.stat().st_size,
                        "sha256": package_meta.sha256(archive)},
        }
        if original.stat().st_size != source["bytes"] or package_meta.sha256(original) != source["sha256"]:
            raise ValueError("Source changed during PostgreSQL repacking")
        audit_path = bundle / "postgresql-repack-manifest.json"
        audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        _publish(bundle, output, filename)
    return audit


def _native_core_evidence(path, archive_sha256, archive_bytes, filename, version, layout):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or set(value) != _NATIVE_KEYS:
        raise ValueError("PostgreSQL native evidence has unexpected or missing fields")
    if value["schema"] != "servbay.windows-postgresql-core-native.v1":
        raise ValueError("Unsupported PostgreSQL native evidence schema")
    if value["archive"] != {"filename": filename, "bytes": archive_bytes, "sha256": archive_sha256}:
        raise ValueError("PostgreSQL native evidence archive differs")
    if (value["os_architecture"] != "X64" or value["process_architecture"] != "X64"
            or value["processor_architecture_code"] not in (9, 12)
            or value["process_environment_architecture"] != "AMD64"):
        raise ValueError("PostgreSQL native evidence does not prove an AMD64 process")
    pg_config = value["pg_config"]
    if not isinstance(pg_config, dict) or set(pg_config) != {
            "version", "bindir", "sharedir", "pkglibdir", "includedir_server"}:
        raise ValueError("PostgreSQL native pg_config evidence is incomplete")
    if pg_config["version"] != "PostgreSQL " + version:
        raise ValueError("PostgreSQL native pg_config version differs")
    def at_layout_path(value, relative):
        if not isinstance(value, str):
            return False
        normalized = value.replace("\\", "/").casefold()
        full = "/" + layout["package_root"] + "/" + relative
        short = "/postgr~1/" + layout["package_root"].split("/", 1)[1] + "/" + relative
        return normalized.endswith(full.casefold()) or normalized.endswith(short.casefold())
    for key, dirname in (("bindir", "bin"), ("sharedir", "share"),
                         ("pkglibdir", "lib"), ("includedir_server", "include/server")):
        if not at_layout_path(pg_config[key], dirname):
            raise ValueError("PostgreSQL native pg_config path differs: " + key)
    executables = value["executables"]
    expected = {name: f"{name} (PostgreSQL) {version}" for name in ("postgres", "initdb", "pg_ctl", "psql")}
    if executables != expected:
        raise ValueError("PostgreSQL native executable evidence differs")
    database = value["database"]
    if (not isinstance(database, dict) or set(database) != {
            "initialized_pg_version", "server_version", "server_encoding", "data_directory",
            "query_exit", "startup_exit", "stop_exit", "remaining_processes", "listeners"}
            or database["initialized_pg_version"] != version.split(".", 1)[0]
            or database["server_version"] != version or database["server_encoding"] != "UTF8"
            or not at_layout_path(database["data_directory"], "data")):
        raise ValueError("PostgreSQL native database identity differs")
    for key in ("query_exit", "startup_exit", "stop_exit", "remaining_processes", "listeners"):
        if type(database[key]) is not int or database[key] != 0:
            raise ValueError("PostgreSQL native cleanup/query evidence failed: " + key)
    return value


def finalize_postgresql_core(package, identity, source_path, source_url, native_result,
                             producer, minimum_os, windows_compatibility, output_dir,
                             *, seven_zip=None):
    """Bind passed native evidence to a schema-2 PostgreSQL core package."""
    package = Path(package).absolute()
    source_path = Path(source_path).absolute()
    output = Path(output_dir).absolute()
    if package.is_symlink() or not package.is_file() or source_path.is_symlink() or not source_path.is_file():
        raise ValueError("PostgreSQL package and source must be real regular files")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("PostgreSQL final output must be a new directory with an existing parent")
    row = _identity(identity)
    layout = package_layout.package_layout("postgresql", row["version"], os="windows", arch=row["arch"])
    filename = f"postgresql-{row['version']}-windows-{row['arch']}.7z"
    archive_sha = package_meta.sha256(package)
    archive_bytes = package.stat().st_size
    native = _native_core_evidence(native_result, archive_sha, archive_bytes, filename,
                                   row["version"], layout)
    source = {"path": str(source_path), "version": row["version"],
              "sha256": package_meta.sha256(source_path), "bytes": source_path.stat().st_size,
              "url": source_url}
    _source(source)
    host = {"type": "postgresql", "series": row["version"].split(".", 1)[0],
            "version": row["version"], "package_sha256": archive_sha,
            "abi": {"major": int(row["version"].split(".", 1)[0])}}
    with tempfile.TemporaryDirectory(prefix="servbay-postgresql-finalize-") as temporary:
        temporary = Path(temporary)
        canonical = temporary / filename
        shutil.copyfile(package, canonical)
        if package_meta.sha256(canonical) != archive_sha or canonical.stat().st_size != archive_bytes:
            raise ValueError("PostgreSQL package changed while freezing its bytes")
        candidate = core_manifest.create_candidate(
            canonical, row, producer, None, minimum_os, source, host,
            windows_compatibility=windows_compatibility,
        )
        profile = temporary / "modules.json"
        profile.write_text(json.dumps({
            "schema_version": 1, "os": "windows",
            "hosts": {"postgresql": {row["version"].split(".", 1)[0]: {}}},
            "recipes": {},
        }), encoding="utf-8")
        staged = temporary / "output"
        result = core_manifest.finalize_core(
            candidate, canonical, [], [], producer, staged, config=profile, seven_zip=seven_zip,
        )
        receipt = {
            "schema": "servbay.windows-postgresql-finalization.v1",
            "native_validation": "passed",
            "minimum_os": minimum_os,
            "windows_compatibility": copy.deepcopy(candidate["meta"]["windows_compatibility"]),
            "native_result_sha256": package_meta.sha256(native_result),
            "native": {
                "processor_name": native["processor_name"],
                "process_environment_architecture": native["process_environment_architecture"],
                "pg_config_version": native["pg_config"]["version"],
                "executables": copy.deepcopy(native["executables"]),
                "database": {key: copy.deepcopy(native["database"][key]) for key in (
                    "initialized_pg_version", "server_version", "server_encoding", "query_exit",
                    "startup_exit", "stop_exit", "remaining_processes", "listeners")},
            },
            "archive": {"filename": filename, "bytes": archive_bytes, "sha256": archive_sha},
            "meta": {"filename": "meta.json", "sha256": package_meta.sha256(staged / "meta.json")},
        }
        (staged / "postgresql-finalization.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(staged, output)
    return {"meta": output / "meta.json", "package": output / filename,
            "finalization": output / "postgresql-finalization.json"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seven-zip")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--source-path", type=Path)
    parser.add_argument("--source-url")
    parser.add_argument("--native-result", type=Path)
    parser.add_argument("--producer", type=Path)
    parser.add_argument("--minimum-os")
    parser.add_argument("--windows-compatibility", type=Path)
    args = parser.parse_args()
    try:
        identity = json.loads(args.identity.read_text(encoding="utf-8"))
        if args.native_result:
            if not all((args.package, args.source_path, args.source_url, args.producer,
                        args.minimum_os, args.windows_compatibility)):
                parser.error("finalization requires package/source/producer/minimum-os/windows-compatibility")
            result = finalize_postgresql_core(
                args.package, identity, args.source_path, args.source_url, args.native_result,
                json.loads(args.producer.read_text(encoding="utf-8-sig")), args.minimum_os,
                json.loads(args.windows_compatibility.read_text(encoding="utf-8-sig")),
                args.output_dir, seven_zip=args.seven_zip,
            )
        else:
            if args.source is None:
                parser.error("diagnostic repacking requires --source")
            source = json.loads(args.source.read_text(encoding="utf-8"))
            result = prepare_postgresql_core(source, identity, args.output_dir, seven_zip=args.seven_zip)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError,
            zipfile.BadZipFile, subprocess.SubprocessError) as error:
        parser.exit(1, f"PostgreSQL repack failed: {error}\n")
    if args.native_result:
        print(json.dumps({key: str(value) for key, value in result.items()}, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"archive": result["archive"],
                          "manifest": "postgresql-repack-manifest.json",
                          "files": len(result["files"])}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
