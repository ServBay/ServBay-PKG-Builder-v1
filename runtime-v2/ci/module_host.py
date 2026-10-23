#!/usr/bin/env python3
"""Read real host tools and headers from an exact, independently pinned core archive."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import stat
import tarfile
import tempfile


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("The exact core archive SHA256 is required")
    return value


def pg_release_version(value):
    match = re.fullmatch(r"([0-9]+)\.(beta|rc)([0-9]+)", value)
    return "".join(match.groups()) if match else value


def extract_host_archive(archive_path, expected_sha256, destination, host_type, series, version):
    if _sha(archive_path) != _checksum(expected_sha256):
        raise ValueError("Exact host archive SHA256 differs")
    if host_type not in ("php", "postgresql") or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", series):
        raise ValueError("Invalid exact module host type/series")
    destination = Path(destination)
    parent, prefix = destination / host_type, destination / host_type / series
    if (destination.is_symlink() or parent.is_symlink() or prefix.exists() or prefix.is_symlink()
            or (destination.exists() and not destination.is_dir())
            or (parent.exists() and not parent.is_dir())):
        raise ValueError("Refusing to merge or replace an existing exact module host")
    root = host_type + "/" + series
    with tarfile.open(archive_path, "r:gz") as archive:
        names = set()
        for member in archive.getmembers():
            name = member.name.rstrip("/") if member.isdir() else member.name
            path = PurePosixPath(name)
            if (not name or name != posixpath.normpath(name) or path.is_absolute()
                    or ".." in path.parts or "\\" in name or name in names):
                raise ValueError("Invalid exact host archive entry")
            names.add(name)
            if name != root and not name.startswith(root + "/") and not (member.isdir() and name == host_type):
                raise ValueError("Host archive contains another installation identity")
            if member.issym() or member.islnk():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname)
                                           if member.issym() else member.linkname)
                if target != root and not target.startswith(root + "/"):
                    raise ValueError("Escaping exact host archive link")
                if member.linkname.startswith("/") or "\\" in member.linkname:
                    raise ValueError("Absolute exact host archive link")
            elif not member.isdir() and not member.isfile():
                raise ValueError("Unsupported exact host archive entry")
        version_member = root + ("/VERSION" if host_type == "php" else "/include/server/pg_config.h")
        if version_member not in names:
            raise ValueError("Exact host archive lacks its actual version input")
        # Own a fresh extraction candidate; never merge archived paths into an old host.
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".module-host-", dir=destination.parent) as directory:
            candidate = Path(directory)
            archive.extractall(candidate, filter="data")
            text = (candidate / version_member).read_text().strip()
            if host_type == "php":
                actual, expected_version = text, version
            else:
                text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
                values = re.findall(r'^\s*#\s*define\s+PG_VERSION\s+"([^"\n]+)"', text, re.M)
                if len(values) != 1:
                    raise ValueError("Exact PostgreSQL host lacks its actual server version")
                actual, expected_version = _postgresql_version("PostgreSQL " + values[0]), pg_release_version(version)
            if actual != expected_version:
                raise ValueError("Installed core version differs from requested host")
            parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate / root, prefix)
    return prefix


def verify_host_archive(archive_path, expected_sha256, prefix, host_type, series):
    """Allow a shared exact host only when the installed tree still equals its tar."""
    if _sha(archive_path) != _checksum(expected_sha256):
        raise ValueError("Exact host archive SHA256 differs")
    prefix = Path(prefix)
    root = host_type + "/" + series
    expected = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        seen = set()
        for member in archive:
            name = member.name.rstrip("/") if member.isdir() else member.name
            if name in seen or name != posixpath.normpath(name) or ".." in PurePosixPath(name).parts:
                raise ValueError("Invalid or duplicate exact host archive path")
            seen.add(name)
            if member.isdir() and name in (host_type, root):
                continue
            if not name.startswith(root + "/"):
                raise ValueError("Exact host archive contains another installation identity")
            relative = name[len(root) + 1:]
            path = prefix / relative
            if member.isdir():
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("Existing host directory differs from its exact archive")
                continue
            expected.add(relative)
            if not path.exists() and not path.is_symlink():
                raise ValueError("Existing host is missing an exact archive member")
            if stat.S_IMODE(path.lstat().st_mode) != member.mode & 0o777:
                raise ValueError("Existing host file mode differs from its exact archive")
            if member.issym():
                if not path.is_symlink() or os.readlink(path) != member.linkname:
                    raise ValueError("Existing host link differs from its exact archive")
                _within(path, prefix, "archive symlink")
            elif member.isfile() or member.islnk():
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Existing host file type differs from its exact archive")
                _within(path, prefix, "archive file")
                with archive.extractfile(member) as handle:
                    digest = hashlib.sha256()
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                if _sha(path) != digest.hexdigest():
                    raise ValueError("Existing host file bytes differ from its exact archive")
            else:
                raise ValueError("Unsupported exact host archive member")
    actual = {path.relative_to(prefix).as_posix() for path in prefix.rglob("*") if path.is_symlink() or path.is_file()}
    if actual != expected:
        raise ValueError("Existing host contains unowned files or old modules")


def _run(args):
    result = subprocess.run([str(value) for value in args], capture_output=True, text=True,
                            check=True, env=dict(os.environ, LC_ALL="C"), timeout=30)
    return result.stdout.strip()


def _within(path, prefix, label):
    path, prefix = Path(path), Path(prefix)
    try:
        relative = path.resolve().relative_to(prefix.resolve())
    except ValueError as error:
        raise ValueError("Host " + label + " points outside its exact prefix") from error
    if not relative.parts:
        raise ValueError("Host " + label + " needs an actual relative subdirectory")
    return relative.as_posix()


def _architecture(binary, arch):
    if arch not in ("x86_64", "arm64") or _run(["/usr/bin/lipo", "-archs", binary]).split() != [arch]:
        raise ValueError("Exact host architecture differs from requested module")


def _define(path, key):
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", Path(path).read_text(), flags=re.S)
    values = re.findall(r"^\s*#\s*define\s+" + re.escape(key) + r"\s+([0-9]+)\b", text, re.M)
    if len(values) != 1:
        raise ValueError("Missing or ambiguous host header definition: " + key)
    return int(values[0])


def _info_value(info, name):
    values = re.findall(r"^" + re.escape(name) + r"\s*=>\s*([^\n]+)$", info, re.M)
    if len(values) != 1:
        raise ValueError("Missing or ambiguous actual PHP info: " + name)
    return values[0].strip()


def probe_php_host(prefix, expected_version, package_sha256, arch):
    prefix = Path(prefix)
    config, php, phpize = (prefix / "bin" / name for name in ("php-config", "php", "phpize"))
    version = _run([config, "--version"])
    if version != expected_version or _run([php, "-n", "-r", "echo PHP_VERSION;"]) != version:
        raise ValueError("Actual PHP program/config version differs from exact host identity")
    if Path(_run([config, "--prefix"])).resolve() != prefix.resolve():
        raise ValueError("php-config belongs to another host prefix")
    _architecture(php, arch)
    extension_dir = _within(_run([config, "--extension-dir"]), prefix, "extension-dir")
    includes = Path(_run([config, "--include-dir"]))
    _within(includes, prefix, "include-dir")
    info = _run([php, "-n", "-i"])
    module_api, zend_api = (_define(includes / "Zend" / name, key) for name, key in (
        ("zend_modules.h", "ZEND_MODULE_API_NO"), ("zend_extensions.h", "ZEND_EXTENSION_API_NO")))
    if any(_info_value(info, name) != str(expected) for name, expected in (
            ("PHP API", module_api), ("PHP Extension", module_api), ("Zend Extension", zend_api))):
        raise ValueError("PHP headers and actual ordinary/Zend APIs differ")
    phpize_info = _run([phpize, "--version"])
    for name, expected in (("PHP Api Version", module_api), ("Zend Module Api No", module_api), ("Zend Extension Api No", zend_api)):
        values = re.findall(r"^" + name + r":\s*([0-9]+)\s*$", phpize_info, re.M)
        if values != [str(expected)]:
            raise ValueError("Actual phpize host ABI differs: " + name)
    thread, debug = _info_value(info, "Thread Safety"), _info_value(info, "Debug Build")
    if thread not in ("enabled", "disabled") or debug not in ("yes", "no"):
        raise ValueError("PHP must report actual ZTS/debug flags")
    abi = {"module_api": module_api, "zend_extension_api": zend_api,
           "extension_build": _info_value(info, "PHP Extension Build"),
           "zend_extension_build": _info_value(info, "Zend Extension Build"),
           "zts": thread == "enabled", "debug": debug == "yes"}
    series = re.match(r"^([0-9]+\.[0-9]+)\.", version)
    if not series:
        raise ValueError("Invalid actual PHP host version")
    return {"host": {"type": "php", "series": series.group(1), "version": version,
                     "package_sha256": _checksum(package_sha256), "abi": abi},
            "extension_dir": extension_dir}


def _postgresql_version(value):
    # The recipe passes --with-extra-version=(ServBay). PostgreSQL appends it
    # directly to PACKAGE_VERSION, and pg_config prefixes it with "PostgreSQL ".
    match = re.fullmatch(r"PostgreSQL ([0-9]+(?:\.[0-9]+)+|[0-9]+(?:beta|rc)[0-9]+)(?:\s*\(ServBay\))?", value)
    if not match:
        raise ValueError("Unrecognized actual pg_config version")
    return match.group(1)


def probe_postgresql_host(prefix, expected_version, package_sha256, arch):
    prefix = Path(prefix)
    config = prefix / "bin/pg_config"
    version = _postgresql_version(_run([config, "--version"]))
    if version != pg_release_version(expected_version):
        raise ValueError("Actual PostgreSQL version differs from exact host identity")
    bindir = Path(_run([config, "--bindir"]))
    if bindir.resolve() != (prefix / "bin").resolve():
        raise ValueError("pg_config belongs to another actual server prefix")
    _architecture(bindir / "postgres", arch)
    includes = Path(_run([config, "--includedir-server"]))
    _within(includes, prefix, "server include-dir")
    header = includes / "pg_config.h"
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", header.read_text(), flags=re.S)
    values = re.findall(r'^\s*#\s*define\s+PG_VERSION\s+"([^"\n]+)"', text, re.M)
    if len(values) != 1 or _postgresql_version("PostgreSQL " + values[0]) != version:
        raise ValueError("PostgreSQL server headers and actual tool version differ")
    major = _define(header, "PG_VERSION_NUM") // 10000
    if str(major) != re.match(r"[0-9]+", version).group(0):
        raise ValueError("PostgreSQL actual server major ABI differs from its headers")
    pgxs = Path(_run([config, "--pgxs"]))
    relative_pgxs = _within(pgxs, prefix, "PGXS")
    if not pgxs.is_file():
        raise ValueError("The exact PostgreSQL core archive does not provide PGXS")
    return {"host": {"type": "postgresql", "series": str(major), "version": version,
                     "package_sha256": _checksum(package_sha256), "abi": {"major": major}},
            "install_prefix": str(bindir.parent),
            "pgxs": relative_pgxs,
            "pkglibdir": _within(_run([config, "--pkglibdir"]), prefix, "pkglibdir"),
            "sharedir": _within(_run([config, "--sharedir"]), prefix, "sharedir")}
