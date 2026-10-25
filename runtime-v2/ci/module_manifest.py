#!/usr/bin/env python3
"""Produce schema 2 module inventories from exact host and staged payload bytes."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import tarfile
import tempfile


_spec = importlib.util.spec_from_file_location(
    "servbay_package_meta", Path(__file__).parents[2] / "runtime/ci/package_meta.py")
_metadata = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_metadata)
_state = _metadata._state
_spec = importlib.util.spec_from_file_location(
    "servbay_windows_archive", Path(__file__).parents[2] / "runtime/ci/windows_archive.py")
_windows = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_windows)


def _platform(manifest, expected_os):
    if expected_os not in ("macos", "windows") or manifest.get("os", "macos") != expected_os:
        raise ValueError("Module manifest and outer OS differ")
    return expected_os


def _compatibility(value):
    if (not isinstance(value, dict) or set(value) != {"distribution", "toolchain", "crt"}
            or any(not isinstance(item, str) or not item.strip() for item in value.values())):
        raise ValueError("Windows compatibility needs distribution, toolchain and crt")
    return value


def _windows_extension_dir(host, extension_dir):
    abi = host["abi"]
    expected = "lib/php/extensions/" + ("debug" if abi["debug"] else "no-debug")
    expected += "-" + ("zts" if abi["zts"] else "non-zts") + "-" + str(abi["module_api"])
    if _windows.validate_path(extension_dir) != expected:
        raise ValueError("Windows PHP extension_dir differs from the actual host ABI")
    return extension_dir


def _relative(value, label):
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or value.startswith("/") or value != posixpath.normpath(value)
            or any(part in {".", ".."} for part in value.split("/"))):
        raise ValueError("Invalid relative " + label)
    return value


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _mode(value):
    if not isinstance(value, str) or not re.fullmatch(r"0[0-7]{3}", value):
        raise ValueError("Invalid owned file mode")
    return int(value, 8)


def _sha(value):
    if not isinstance(value, str) or not _state.SHA256.fullmatch(value):
        raise ValueError("Invalid module SHA256")
    return value


def _host(host):
    if not isinstance(host, dict):
        raise ValueError("The actual module host is required")
    _state._text(host.get("version"), "host version")
    _state._text(host.get("series"), "host series")
    _sha(host.get("package_sha256"))
    abi = host.get("abi")
    if not isinstance(abi, dict):
        raise ValueError("The actual module host ABI is required")
    if host.get("type") == "php":
        fields = {"module_api", "zend_extension_api", "extension_build", "zend_extension_build", "zts", "debug"}
        if (set(abi) != fields or any(type(abi[key]) is not int or abi[key] < 1
                                      for key in ("module_api", "zend_extension_api"))
                or any(type(abi[key]) is not bool for key in ("zts", "debug"))
                or any(not isinstance(abi[key], str) or not abi[key]
                       for key in ("extension_build", "zend_extension_build"))):
            raise ValueError("PHP host needs both actual module and Zend ABI/build identities")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", host["series"]) or not host["version"].startswith(host["series"] + "."):
            raise ValueError("PHP host version and series differ")
    elif host.get("type") == "postgresql":
        if set(abi) != {"major"} or type(abi["major"]) is not int or abi["major"] < 1 or str(abi["major"]) != host["series"]:
            raise ValueError("PostgreSQL host needs its actual server major ABI")
        if not (host["version"].startswith(host["series"] + ".")
                or re.fullmatch(re.escape(host["series"]) + r"(?:beta|rc)[0-9]+", host["version"])):
            raise ValueError("PostgreSQL host version and series differ")
    else:
        raise ValueError("Unsupported actual module host")
    return host


def _references(rows, host):
    if not isinstance(rows, list):
        raise ValueError("Module dependencies must be an explicit list")
    expected = {key: host[key] for key in ("type", "series", "abi")}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != host["type"] + "-extension" or row.get("host") != expected:
            raise ValueError("Module dependency host ABI differs")
        _state._text(row.get("name"), "dependency name")
        _state._text(row.get("version"), "dependency version")
        _sha(row.get("sha256"))
        if type(row.get("build_revision")) is not int or row["build_revision"] < 0:
            raise ValueError("Dependency revision must be explicit")
        key = row["kind"], row["name"], row["version"], row["build_revision"]
        if key in seen:
            raise ValueError("Duplicate module dependency")
        seen.add(key)


def _config(row, os_name="macos"):
    if not isinstance(row, dict) or row.get("scope") != "config" or not isinstance(row.get("content"), str):
        raise ValueError("A module config needs its complete actual template")
    _relative(row.get("install_path"), "config install path")
    _mode(row.get("mode"))
    if _sha(row.get("sha256")) != _hash(row["content"].encode("utf-8")):
        raise ValueError("Module config template hash differs")
    if (re.search(r"(?m)^\s*(?:zend_)?extension\s*=\s*['\"]?/", row["content"])
            or any(path in row["content"] for path in ("/Users/runner/", "/Users/servbay/", "/private/var/"))):
        raise ValueError("Module config contains a build-machine absolute path")
    if os_name == "windows" and re.search(
            r"(?im)^\s*(?:zend_)?extension\s*=\s*['\"]?(?:[a-z]:|\\)", row["content"]):
        raise ValueError("Windows module config contains an absolute extension path")


def _link_destination(name, target):
    if not isinstance(target, str) or not target or target.startswith("/") or "\\" in target:
        raise ValueError("Escaping module payload link")
    return _relative(posixpath.normpath(posixpath.join(posixpath.dirname(name), target)), "link target")


def create_manifest(host, staging, config_files, provides, dependencies, *, arch, minimum_os,
                    base, source_name, extension_dir=None, os_name="macos", windows_compatibility=None):
    host = _host(copy.deepcopy(host))
    if os_name not in ("macos", "windows"):
        raise ValueError("Unsupported module OS")
    if os_name == "windows":
        if base is not None:
            raise ValueError("Windows modules must not have a base")
        arch = {"x64": "x86_64"}.get(arch, arch)
        _compatibility(windows_compatibility)
        _windows.staging_files(staging)
    staging = Path(staging)
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("The module needs its own real staging directory")
    if host["type"] == "php":
        extension_dir = _relative(extension_dir, "actual php-config extension-dir")
    files = []
    for path in sorted(staging.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        name = _relative(path.relative_to(staging).as_posix(), "archive path")
        mode = stat.S_IMODE(path.lstat().st_mode)
        if mode & ~0o777:
            raise ValueError("Module payload has unsupported set-id mode")
        row = {"scope": "host", "archive_path": name,
               "install_path": posixpath.join(extension_dir, name) if host["type"] == "php" and os_name == "macos" else name,
               "mode": f"0{mode:03o}"}
        if path.is_symlink():
            target = os.readlink(path)
            _link_destination(name, target)
            row.update(type="symlink", target=target, sha256=_hash(target.encode("utf-8")))
        elif path.is_file():
            row.update(type="file", sha256=_metadata.sha256(path))
        else:
            raise ValueError("Unsupported module payload entry: " + name)
        files.append(row)
    configs = copy.deepcopy(config_files)
    for row in configs:
        if not isinstance(row, dict) or not isinstance(row.get("content"), str):
            raise ValueError("The complete module config template is required")
        row["sha256"] = _hash(row["content"].encode("utf-8"))
    value = {"schema_version": 2, "host": host, "arch": arch, "minimum_os": minimum_os,
             "base": copy.deepcopy(base), "source_name": source_name, "files": files,
             "config_files": configs, "provides": list(provides), "dependencies": copy.deepcopy(dependencies)}
    if os_name == "windows":
        value.pop("base")
        value.update(os="windows", layout="servbay-v2", layout_version=1,
                     windows_compatibility=copy.deepcopy(windows_compatibility))
        if host["type"] == "php":
            value["extension_dir"] = _windows_extension_dir(host, extension_dir)
    _validate_inventory(value, expected_os=os_name)
    return value


def _validate_inventory(manifest, *, expected_os="macos"):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise ValueError("A schema 2 module inventory is required")
    os_name = _platform(manifest, expected_os)
    host = _host(manifest.get("host"))
    if manifest.get("arch") not in ("arm64", "x86_64", "x86"):
        raise ValueError("Module architecture is required")
    if not isinstance(manifest.get("minimum_os"), str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", manifest["minimum_os"]):
        raise ValueError("Module minimum OS is required")
    if os_name == "windows":
        # PHP 5.3/5.4 modules are x86; every other Windows module is x86_64.
        if ("base" in manifest or manifest["arch"] not in ("x86_64", "x86")
                or (manifest["arch"] == "x86" and host["type"] != "php")):
            raise ValueError("Windows modules require x86_64 (x86 only for PHP) and no base")
        if (manifest.get("layout") != "servbay-v2" or type(manifest.get("layout_version")) is not int
                or manifest["layout_version"] != 1):
            raise ValueError("Windows module layout differs")
        _compatibility(manifest.get("windows_compatibility"))
        if host["type"] == "php":
            _windows_extension_dir(host, manifest.get("extension_dir"))
    else:
        base = manifest.get("base")
        if not isinstance(base, dict) or set(base) != {"version", "runtime_sha256", "devlib_sha256"}:
            raise ValueError("The actual fixed module base identity is required")
        _state._text(base["version"], "base version")
        _sha(base["runtime_sha256"])
        _sha(base["devlib_sha256"])
    _state._text(manifest.get("source_name"), "module source name")
    _references(manifest.get("dependencies"), host)
    provides = manifest.get("provides")
    if not isinstance(provides, list) or not provides or len(set(provides)) != len(provides):
        raise ValueError("Module provides must name each actual distinct extension")
    for name in provides:
        _state._text(name, "provides")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or not isinstance(manifest.get("config_files"), list):
        raise ValueError("Module owned files and config templates are required")
    names, targets = {}, set()
    for row in files:
        if not isinstance(row, dict) or row.get("scope") not in ("host", "config"):
            raise ValueError("Invalid module file scope")
        name = _relative(row.get("archive_path"), "archive path")
        target = row["scope"], _relative(row.get("install_path"), "install path")
        _mode(row.get("mode"))
        _sha(row.get("sha256"))
        if name in names or target in targets:
            raise ValueError("Duplicate module archive file or installed destination")
        names[name] = row
        targets.add(target)
        if os_name == "windows":
            _windows.validate_path(name)
            _windows.validate_path(target[1])
            if row.get("type") != "file" or row["scope"] != "host" or name != target[1]:
                raise ValueError("Windows module files must retain their host-relative paths")
        if row.get("type") == "symlink":
            _link_destination(name, row.get("target"))
        elif row.get("type") != "file":
            raise ValueError("Unsupported owned module file type")
        if os_name == "macos" and host["type"] == "php" and (row["scope"] != "host" or "/" in name or not name.endswith(".so")):
            raise ValueError("PHP modules keep the actual flat SO archive format")
    for name, row in names.items():
        if row["type"] == "symlink":
            target, visited = _link_destination(name, row["target"]), {name}
            while True:
                if target not in names or target in visited:
                    raise ValueError("Unresolved or cyclic owned module link")
                visited.add(target)
                if names[target]["type"] != "symlink":
                    break
                target = _link_destination(target, names[target]["target"])
    for row in manifest["config_files"]:
        _config(row, os_name)
        target = "config", row["install_path"]
        if target in targets:
            raise ValueError("Duplicate module config destination")
        targets.add(target)
    if os_name == "windows":
        _windows.validate_paths(names)
        for scope in ("host", "config"):
            _windows.validate_paths([path for row_scope, path in targets if row_scope == scope])
        if host["type"] == "php" and not any(
                posixpath.dirname(name) == manifest["extension_dir"] and name.lower().endswith(".dll") for name in names):
            raise ValueError("Windows PHP module payload is missing its extension DLL")
    if host["type"] == "postgresql":
        for name in provides:
            if not any(PurePosixPath(path).name == name + ".control" for path in names):
                raise ValueError("PostgreSQL provides is missing its actual control file")
            if not any(re.fullmatch(re.escape(name) + r"--[^/]+\.sql", PurePosixPath(path).name)
                       and len(PurePosixPath(path).name[:-4].split("--")) == 2 for path in names):
                raise ValueError("PostgreSQL provides is missing an initial installation SQL script")
    return names


def validate_manifest(manifest, package_tar, *, expected_os="macos", seven_zip=None):
    owned = _validate_inventory(manifest, expected_os=expected_os)
    if expected_os == "windows":
        actual = _windows.archive_hashes(package_tar, seven_zip)
        if actual != {name: row["sha256"] for name, row in owned.items()}:
            raise ValueError("Windows archive file set or hash differs from module inventory")
        return
    with tarfile.open(package_tar, "r:gz") as archive:
        entries = {}
        for member in archive.getmembers():
            name = _relative(member.name.rstrip("/") if member.isdir() else member.name, "archive member")
            if name in entries:
                raise ValueError("Duplicate module archive member")
            if not member.isdir() and not member.isfile() and not member.issym():
                raise ValueError("Unsupported module archive member")
            entries[name] = member
        if {name for name, member in entries.items() if not member.isdir()} != set(owned):
            raise ValueError("Every module archive file must be owned exactly once")
        for name, row in owned.items():
            member = entries[name]
            if member.mode & 0o7777 != _mode(row["mode"]):
                raise ValueError("Module archive file mode differs")
            if row["type"] == "file" and member.isfile():
                with archive.extractfile(member) as handle:
                    digest = hashlib.sha256()
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                actual = digest.hexdigest()
            elif row["type"] == "symlink" and member.issym() and member.linkname == row["target"]:
                actual = _hash(member.linkname.encode("utf-8"))
            else:
                raise ValueError("Module archive file type or symlink target differs")
            if actual != row["sha256"]:
                raise ValueError("Module archive file hash differs")


def write_package(manifest, staging, identity, producer, source, output_dir, *, seven_zip=None):
    manifest = copy.deepcopy(manifest)
    row = _state.clean_record(identity)
    owned = _validate_inventory(manifest, expected_os=row["os"])
    host = manifest["host"]
    if row["kind"] != host["type"] + "-extension" or row.get("host") != {key: host[key] for key in ("type", "series", "abi")}:
        raise ValueError("Module inventory and requested identity differ")
    if row["arch"] != manifest["arch"]:
        raise ValueError("Module inventory and requested architecture differ")
    suffix = "php" if host["type"] == "php" else "pgsql"
    filename = f"ext-{row['name']}-{row['version']}-{suffix}{host['series']}-{row['arch']}.tar.gz"
    if row["os"] == "windows":
        filename = f"ext-{row['name']}-{row['version']}-{suffix}{host['series']}-windows-{row['arch']}.7z"
    output_dir, staging = Path(output_dir), Path(staging)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".module-package-", dir=output_dir) as directory:
        directory = Path(directory)
        package = directory / filename
        if row["os"] == "windows":
            _windows.create_archive(staging, package, owned, seven_zip)
        else:
            with tarfile.open(package, "w:gz") as archive:
                for name, entry in owned.items():
                    path = staging / name
                    info = tarfile.TarInfo(name)
                    info.mode = stat.S_IMODE(path.lstat().st_mode)
                    if path.is_symlink():
                        info.type = tarfile.SYMTYPE
                        info.linkname = os.readlink(path)
                        archive.addfile(info)
                    else:
                        info.size = path.stat().st_size
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
            manifest["minimum_os"] = _metadata.minimum_os_from_archive(package, manifest["minimum_os"])
        validate_manifest(manifest, package, expected_os=row["os"], seven_zip=seven_zip)
        metadata = _metadata.metadata_fields(row, producer, manifest.get("base"), manifest["minimum_os"], source)
        if row["os"] == "windows":
            metadata.update(layout="servbay-v2", layout_version=1,
                            windows_compatibility=copy.deepcopy(manifest["windows_compatibility"]))
        attachment = directory / "module-manifest.json"
        attachment.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        metadata.update(filename=filename, sha256=_metadata.sha256(package),
                        manifest_filename=attachment.name, manifest_sha256=_metadata.sha256(attachment))
        meta_path = directory / "meta.json"
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        for path in (package, attachment, meta_path):
            os.replace(path, output_dir / path.name)
    return {"meta": metadata, "package": str(output_dir / filename), "manifest": str(output_dir / attachment.name),
            "meta_path": str(output_dir / meta_path.name)}
