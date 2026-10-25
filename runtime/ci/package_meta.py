#!/usr/bin/env python3
"""Create the v2 handoff from real package and upstream input bytes."""

import argparse
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit


_spec = importlib.util.spec_from_file_location("servbay_build_state", Path(__file__).with_name("build_state.py"))
_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_state)
artifact_name = _state.artifact_name

_php_spec = importlib.util.spec_from_file_location("servbay_php_source", Path(__file__).parents[2] / "runtime-v2/ci/php_source.py")
_php_source = importlib.util.module_from_spec(_php_spec)
_php_spec.loader.exec_module(_php_source)


def package_metadata(name, version):
    """Keep v2 installation series and public labels in one producer-owned mapping."""
    labels = {"php": "PHP", "postgresql": "PostgreSQL", "mariadb": "MariaDB", "mysql": "MySQL",
              "node": "Node.js", "deno": "Deno", "bun": "Bun", "git": "Git", "dnsmasq": "dnsmasq",
              "caddy": "Caddy", "nginx": "NGINX", "apache": "Apache", "redis": "Redis", "openssl": "OpenSSL",
              "memcached": "Memcached", "mailpit": "Mailpit", "mongosh": "Mongo Shell",
              "mongotools": "MongoDB Database Tools", "mongodb": "MongoDB", "ollama": "Ollama",
              "python": "Python", "go": "Go", "openjdk": "OpenJDK", "maven": "Apache Maven",
              "rust": "Rust", "ruby": "Ruby", "dotnetsdk": ".NET SDK", "mono": "Mono",
              "simple-acme": "Simple ACME",
              "adminer": "Adminer", "phpmyadmin": "phpMyAdmin", "subversion": "Subversion",
              "frp": "frp", "cloudflared": "Cloudflared", "ngrok": "ngrok", "pinggy": "Pinggy",
              "meilisearch": "Meilisearch", "typesense": "Typesense", "minio": "MinIO",
              "ai-gateway": "ServBay AI Gateway", "disk-cleaner": "ServBay DiskCleaner", "composer": "Composer"}
    if name not in labels:
        raise ValueError(f"No verified v2 package metadata mapping: {name}")
    match = re.match(r"^(\d+)(?:\.(\d+))?", version)
    if not match:
        raise ValueError("Package metadata needs the real version")
    major = match.group(1)
    series_names = {"php", "mariadb", "mysql", "dnsmasq", "apache", "redis", "mongodb", "python", "go",
                    "ruby", "dotnetsdk", "mono", "phpmyadmin", "subversion"}
    if name in series_names:
        if match.group(2) is None:
            raise ValueError(f"Missing v2 minor installation series for {name}")
        major += "." + match.group(2)
    if name == "postgresql":
        major = match.group(1)
    elif name == "dotnetsdk" and version == "2.1.202":
        major = "2.0"
    elif name == "mongosh":
        major += ".0"
    elif name in {"frp", "cloudflared", "pinggy", "typesense", "minio"}:
        major = "0"
    elif name in {"ai-gateway", "disk-cleaner"}:
        major = "1"
    elif name == "composer":
        major = "2.2" if version.startswith("2.2.") else "2"
    display = labels[name]
    if name in {"php", "postgresql", "mariadb", "mysql", "node", "apache", "mongodb", "python", "go",
                "openjdk", "ruby", "dotnetsdk", "mono", "phpmyadmin", "subversion"}:
        display += " " + major
    elif name == "composer" and major == "2.2":
        display += " 2.2 LTS"
    return {"major": major, "display_name": display}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def minimum_os_from_archive(package_path, baseline="12.0"):
    """Inspect Mach-O load commands without unpacking arbitrary archive paths."""
    versions = [baseline]
    magics = (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
              b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")
    with tempfile.TemporaryDirectory(prefix="servbay-minos-") as directory, tarfile.open(package_path, "r:gz") as archive:
        target = Path(directory) / "payload"
        for member in archive:
            if not member.isfile():
                continue
            # go/node 等发行树在 src/**/testdata/ 里带 Mach-O 测试夹具（无
            # LC_BUILD_VERSION）：夹具不是运行时二进制，minos 门禁不适用
            # （go 1.27.3 双架构首跑实测误报，run 39997404749）。
            if any(part == "testdata" for part in PurePosixPath(member.name).parts):
                continue
            with archive.extractfile(member) as handle:
                magic = handle.read(4)
                if magic not in magics:
                    continue
                with target.open("wb") as output:
                    output.write(magic)
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        output.write(block)
            kind = subprocess.run(["/usr/bin/file", "-b", str(target)], check=True,
                                  capture_output=True, text=True).stdout
            if "Mach-O" not in kind:
                continue
            commands = subprocess.run(["/usr/bin/otool", "-l", str(target)], check=True,
                                      capture_output=True, text=True).stdout
            found = re.findall(r"\bminos\s+([0-9.]+)", commands)
            if not found:
                found = re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize\s+\d+\s+version\s+([0-9.]+)", commands)
            if not found:
                raise ValueError(f"Mach-O minimum OS is missing: {member.name}")
            versions.extend(found)
    return max(versions, key=lambda value: tuple(int(part) for part in value.split(".")))


def metadata_fields(identity, producer, base, minimum_os, source):
    """Share pinned source/base/provenance validation across ordinary and module producers."""
    row = _state.clean_record(identity)
    if row["runtime_version"] != "v2":
        raise ValueError("Schema2 requires an explicit runtime v2 identity")
    producer = _state.validate_producer(producer)
    if row["os"] == "windows":
        if base is not None:
            raise ValueError("Windows v2 metadata must not have a base")
    else:
        if not isinstance(base, dict) or set(base) != {"version", "runtime_sha256", "devlib_sha256"}:
            raise ValueError("The fixed Runtime/Devlib base identity is incomplete")
        _state._text(base["version"], "base version")
        for field in ("runtime_sha256", "devlib_sha256"):
            if not isinstance(base[field], str) or not _state.SHA256.fullmatch(base[field]):
                raise ValueError(f"Invalid base {field}")
    if not isinstance(minimum_os, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", minimum_os):
        raise ValueError("An actual minimum_os is required")
    if not isinstance(source, dict) or not source.get("path"):
        raise ValueError("The actual primary upstream input is required")
    source_version = _state._text(source.get("version"), "source version")
    expected_source = (_php_source.source_version(row["version"])
                       if row["kind"] == "package" and row["name"] == "php" else row["version"])
    if source_version != expected_source:
        raise ValueError("Source version does not match the package identity")
    origin = {"version": source_version, "sha256": sha256(source["path"])}
    if source.get("url"):
        url = urlsplit(source["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Source URLs must be public HTTPS URLs without credentials")
        try:
            if not ipaddress.ip_address(url.hostname).is_global:
                raise ValueError("Source URL is not public")
        except ValueError as error:
            if "not public" in str(error):
                raise
        if "." not in url.hostname or url.hostname.endswith((".local", ".internal", ".localhost")):
            raise ValueError("Source URL is not public")
        origin["url"] = source["url"]
    meta = {key: row[key] for key in ("runtime_version", "os", "kind", "name", "version", "arch", "build_revision")}
    meta.update(schema_version=2, producer=producer, minimum_os=minimum_os, source=origin)
    if row["os"] == "macos":
        meta["base"] = dict(base)
    if "host" in row:
        meta["host"] = row["host"]
    return meta


def create_meta(package_path, identity, producer, base, minimum_os, source=None):
    row = _state.clean_record(identity)
    if row["runtime_version"] != "v2":
        raise ValueError("Schema2 requires an explicit runtime v2 identity")
    package_path = Path(package_path)
    expected = f"{row['name']}-{row['version']}.tar.gz"
    if row["os"] == "windows":
        expected = f"{row['name']}-{row['version']}-windows-{row['arch']}.7z"
    if row["kind"] != "package":
        raise ValueError("Module metadata requires its manifest-aware producer")
    if package_path.name != expected:
        raise ValueError("Package filename does not match the build identity")
    if not package_path.is_file() or package_path.stat().st_size == 0:
        raise ValueError("The successful package archive is missing or empty")
    meta = metadata_fields(row, producer, base, minimum_os, source)
    meta.update(filename=package_path.name, sha256=sha256(package_path))
    meta.update(package_metadata(row["name"], row["version"]))
    if row["os"] == "windows":
        # Layout consumes package_metadata; load it only after this module is initialized.
        spec = importlib.util.spec_from_file_location("servbay_package_layout", Path(__file__).with_name("package_layout.py"))
        layout_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(layout_module)
        layout = layout_module.package_layout(row["name"], row["version"], os=row["os"], arch=row["arch"])
        meta.update(layout="servbay-v2", layout_version=1)
        meta.update({key: layout[key] for key in ("package_root", "config_root", "entrypoint")})
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--producer", required=True)
    parser.add_argument("--base")
    parser.add_argument("--minimum-os", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    identity = json.loads(Path(args.identity).read_text())
    if identity.get("os") == "windows" and args.base is not None:
        parser.error("Windows v2 metadata must omit --base")
    meta = create_meta(args.package, identity,
                       json.loads(Path(args.producer).read_text()),
                       json.loads(Path(args.base).read_text()) if args.base else None,
                       args.minimum_os, json.loads(Path(args.source).read_text()))
    Path(args.output).write_text(json.dumps(meta, indent=2) + "\n")
    print(artifact_name(meta))


if __name__ == "__main__":
    main()
