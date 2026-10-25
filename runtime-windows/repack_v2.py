#!/usr/bin/env python3
"""Offline ordinary Windows v1-to-v2 archive mapping; no download or publish."""

import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location("ordinary_windows_" + name,
                                                ROOT / "runtime/ci" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_meta = _load("package_meta")
package_layout = _load("package_layout")
windows_archive = _load("windows_archive")
SUPPORTED = {
    "nginx", "redis", "python", "node", "composer",
    "go", "ruby", "openjdk", "maven", "mysql", "mariadb", "mongodb",
    "apache", "git", "openssl", "phpmyadmin", "mongosh", "mongotools",
    "ai-gateway", "disk-cleaner", "adminer",
    "mailpit", "ollama", "minio", "meilisearch", "memcached",
    "deno", "bun", "caddy", "cloudflared", "frp", "ngrok", "pinggy",
}
TREE_PRESERVING = {
    "go", "ruby", "openjdk", "maven", "mysql", "mariadb", "mongodb",
    "apache", "git", "openssl", "phpmyadmin", "mongosh", "mongotools",
    "ai-gateway", "disk-cleaner", "memcached",
}
ROOT_BINARY = {
    "mailpit", "ollama", "minio", "meilisearch",
    "deno", "bun", "caddy", "cloudflared", "frp", "ngrok", "pinggy",
}


def _mapping(files, layout, version):
    name, root = layout["name"], layout["package_root"]
    required = {"nginx": {"nginx.exe", "conf/nginx.conf", "conf/mime.types"},
                "redis": {"redis-server.exe", "redis-cli.exe"}, "python": {"python.exe"},
                "node": {"node.exe"},
                "composer": {"composer-2.2" if version.startswith("2.2.") else "composer"}}.get(name, set())
    source_required = set(required)
    if name in TREE_PRESERVING:
        source_required = {layout["entrypoint"]["path"].removeprefix(root + "/")}
    elif name in ROOT_BINARY:
        source_required = {Path(layout["entrypoint"]["path"]).name}
    elif name == "adminer":
        source_required = {"adminer.php"}
    if not source_required <= set(files):
        raise ValueError("Missing source entries: " + ", ".join(sorted(source_required - set(files))))
    foreign = {"php.exe", "php-cgi.exe", "php-win.exe", "postgres.exe", "pg_ctl.exe"}
    if any(Path(path).name.casefold() in foreign for path in files):
        raise ValueError("Mixed PHP/PostgreSQL payloads require their dedicated producer")
    mapping, defaults = [], []
    for path in sorted(files):
        default = False
        if name == "nginx":
            default = path.startswith("conf/")
            relative = ("share/defaults/" + path if default else
                        "sbin/" + path if "/" not in path else path)
        elif name == "redis":
            default = "/" not in path and path.casefold().endswith(".conf")
            relative = ("share/defaults/" if default else "bin/") + path
        elif name == "python" or name in TREE_PRESERVING:
            relative = path
        elif name in ROOT_BINARY:
            relative = ("bin/" + path) if "/" not in path else path
        elif name == "adminer":
            relative = "index.php"
        elif name == "composer" and path in required:
            relative = "bin/composer"
        else:
            relative = "bin/" + path
        target = root + "/" + relative
        mapping.append({"source_path": path, "target_path": target})
        if default:
            defaults.append({"archive_path": target,
                             "etc_relative_path": layout["config_root"] + "/" +
                             (path.removeprefix("conf/") if name == "nginx" else path),
                             "kind": "upstream-default", "automatically_enabled": False})
    windows_archive.validate_paths([row["target_path"] for row in mapping])
    if layout["entrypoint"]["path"] not in {row["target_path"] for row in mapping}:
        raise ValueError("Mapped payload does not contain its declared entrypoint")
    return mapping, defaults


def repack(source, identity, producer, minimum_os, output_dir, *, seven_zip=None):
    """Produce a new verified bundle from an explicitly pinned local v1 archive.

    producer identifies the actual caller's build/run and is validated unchanged
    by package_meta. The sidecar is a build audit, not an installation API.
    All archive operations use a frozen temporary copy; user etc is never used.
    """
    identity_fields = {"runtime_version", "os", "kind", "name", "version", "arch", "build_revision"}
    if not isinstance(identity, dict) or set(identity) != identity_fields:
        raise ValueError("An explicit ordinary package identity is required without extra fields")
    row = package_meta._state.clean_record(identity)
    if row["runtime_version"] != "v2" or row["os"] != "windows" or row["kind"] != "package":
        raise ValueError("Expected an ordinary v2/windows package identity")
    if row["name"] not in SUPPORTED:
        raise ValueError("No ordinary Windows v2 mapping for this package")
    layout = package_layout.package_layout(row["name"], row["version"], os=row["os"], arch=row["arch"])
    required_source = {"path", "version", "sha256", "bytes"}
    if (not isinstance(source, dict) or not required_source <= set(source)
            or set(source) - required_source - {"url"}):
        raise ValueError("Source requires exact path/version/sha256/bytes and optional public url")
    if (not isinstance(source["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
            or type(source["bytes"]) is not int or source["bytes"] <= 0):
        raise ValueError("Invalid source SHA256/bytes")
    if not isinstance(source["path"], (str, Path)) or not source["path"]:
        raise ValueError("Source path must identify a real local archive")
    original = Path(source["path"]).absolute()
    if not stat.S_ISREG(original.lstat().st_mode):
        raise ValueError("Source must be a regular archive, not a link or directory")
    windows_archive.validate_path(original.name)
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output must be a new directory")
    if not output.parent.is_dir():
        raise ValueError("Output parent directory must already exist")
    if original.stat().st_size != source["bytes"] or package_meta.sha256(original) != source["sha256"]:
        raise ValueError("Source SHA256/bytes mismatch")
    tool = windows_archive._tool(seven_zip)
    with tempfile.TemporaryDirectory(prefix="servbay-windows-repack-") as temporary:
        temporary = Path(temporary)
        frozen = temporary / "source.7z"
        shutil.copyfile(original, frozen)
        if frozen.stat().st_size != source["bytes"] or package_meta.sha256(frozen) != source["sha256"]:
            raise ValueError("Source changed while freezing its input bytes")
        frozen_source = {**source, "path": frozen}
        # Validate provenance/minimum OS/version before unpacking any payload.
        package_meta.metadata_fields(row, producer, None, minimum_os, frozen_source)
        extracted = temporary / "extracted"
        inventory = windows_archive.extract_verified_archive(frozen, extracted, tool)
        mapping, defaults = _mapping(inventory, layout, row["version"])
        stage = temporary / "stage"
        stage.mkdir()
        expected = {}
        for record in mapping:
            path = extracted / record["source_path"]
            target = stage / record["target_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            record.update(bytes=path.stat().st_size, sha256=inventory[record["source_path"]])
            if target.stat().st_size != record["bytes"] or package_meta.sha256(target) != record["sha256"]:
                raise ValueError("Mapped Windows file bytes changed")
            expected[record["target_path"]] = record["sha256"]
        for default in defaults:
            default["sha256"] = expected[default["archive_path"]]
        bundle = temporary / "bundle"
        bundle.mkdir()
        filename = f"{row['name']}-{row['version']}-windows-{row['arch']}.7z"
        archive = bundle / filename
        windows_archive.create_archive(stage, archive, expected, tool)
        verified = windows_archive.extract_verified_archive(
            archive, temporary / "verification", tool
        )
        if verified != expected:
            raise ValueError("Repacked archive file set or SHA256 differs")
        meta = package_meta.create_meta(archive, row, producer, None, minimum_os, frozen_source)
        audit = {"source": {"filename": original.name, "bytes": source["bytes"], "sha256": source["sha256"]},
                 "files": mapping, "configuration_defaults": defaults, "native_validation": "pending"}
        if row["name"] == "nginx":
            audit["startup_handoff"] = {"prefix": "package/nginx", "config": "etc/nginx/nginx.conf"}
        for name, data in (("repack-manifest.json", audit), ("meta.json", meta)):
            (bundle / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        # mkdir is exclusive even if another process creates the output meanwhile.
        # Publish metadata last; failures never leave a successful handoff behind.
        output.mkdir(exist_ok=False)
        for name in (filename, "repack-manifest.json", "meta.json"):
            shutil.copyfile(bundle / name, output / name)
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "identity", "producer"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--minimum-os", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seven-zip")
    args = parser.parse_args()
    try:
        inputs = {name: json.loads(getattr(args, name).read_text(encoding="utf-8"))
                  for name in ("source", "identity", "producer")}
        result = repack(**inputs, minimum_os=args.minimum_os, output_dir=args.output_dir, seven_zip=args.seven_zip)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Repack failed: {error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
