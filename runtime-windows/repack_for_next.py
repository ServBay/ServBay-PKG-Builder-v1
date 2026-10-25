#!/usr/bin/env python3
"""Repack five pinned local Windows archives; never download or publish."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPECS = {
    "nginx": ("1", "1.31.7", "nginx/", ["nginx/nginx.exe"]),
    "php": ("8.3", "8.3.35", "php/8.3/", ["php/8.3/php.exe", "php/8.3/php-cgi.exe", "php/8.3/php-win.exe"]),
    "redis": ("7", "7.4.11", "redis/", ["redis/redis-server.exe", "redis/redis-cli.exe"]),
    "phpmyadmin": ("5", "5.2.3", "phpmyadmin/", ["phpmyadmin/index.php"]),
    "adminer": ("0", "6.1.2", "adminer/", ["adminer/index.php"]),
}
BATCH = {"runtime_version": "v2", "os": "windows", "arch": "x64", "layout": "windows-next-bootstrap", "layout_version": 1}
CREATE_ARGS = ["a", "-t7z", "-mx=7", "-mtc=off", "-mta=off", "-mtm=off"]


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Command failed ({command[0]}): {result.stderr or result.stdout}")
    return result.stdout


def validate_member(entry):
    path = entry.get("Path", "")
    parts = path.split("/")
    if not path or "\\" in path or ":" in path or path.startswith("/") or any(p in {"", ".", ".."} for p in parts) or any(ord(c) < 32 for c in path):
        raise ValueError(f"Unsafe archive member: {path!r}")
    attributes = entry.get("Attributes", "")
    if ("Symbolic Link" in entry or "Hard Link" in entry
            or re.search(r"(?:^| )l[rwx-]{9}", attributes)
            or "L" in attributes.split(" ", 1)[0] or "Reparse" in attributes):
        raise ValueError(f"Archive links are unsupported: {path}")
    if entry.get("Encrypted", "-") != "-":
        raise ValueError(f"Encrypted archive member: {path}")
    return path


def archive_entries(archive, seven_zip):
    output = run([seven_zip, "l", "-slt", "-ba", str(archive)])
    entries = []
    seen = set()
    output = output.replace("\r\n", "\n").replace("\r", "\n")
    for block in output.strip().split("\n\n"):
        entry = {}
        for line in block.splitlines():
            if " = " not in line:
                raise ValueError("Ambiguous archive listing")
            key, value = line.split(" = ", 1)
            if key in entry:
                raise ValueError("Duplicate archive listing field")
            entry[key] = value
        if "Path" in entry:
            entry["Path"] = entry["Path"].replace("\\", "/")
        path = validate_member(entry)
        if path.casefold() in seen:
            raise ValueError(f"Duplicate Windows archive path: {path}")
        seen.add(path.casefold())
        entries.append(entry)
    if not entries:
        raise ValueError("Empty archive")
    return entries


def load_source_manifest(path, runtime_version):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if runtime_version != "v2" or any(data.get(k) != v for k, v in BATCH.items()):
        raise ValueError("Expected explicit v2/windows/x64 bootstrap manifest")
    packages = data.get("packages", [])
    if len(packages) != 5 or {p.get("name") for p in packages} != set(SPECS):
        raise ValueError("Expected exact five-package bootstrap set")
    for package in packages:
        name = package["name"]
        major, version, prefix, entries = SPECS[name]
        if (package.get("major"), package.get("version"), package.get("archive_root"), package.get("required_entries")) != (major, version, prefix, entries):
            raise ValueError(f"Incorrect package version/layout/required entries: {name}")
        source = package["source"]
        filename = f"{name}-{version}-windows-x64.7z"
        if source.get("filename") != filename or source.get("url") != f"https://dl.servbay.com/windows-packages/x64/{name}/{filename}" or source.get("runtime_version") != "v1":
            raise ValueError(f"Incorrect source identity: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")) or type(source.get("bytes")) is not int or source["bytes"] <= 0:
            raise ValueError(f"Invalid source hash/size: {name}")
    return data


def file_tree(directory):
    files, directories = {}, set()
    for path in Path(directory).rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Unexpected extracted link or special file: {path}")
        relative = path.relative_to(directory).as_posix()
        validate_member({"Path": relative})
        if path.is_file():
            files[relative] = {"sha256": digest(path), "bytes": path.stat().st_size}
        else:
            directories.add(relative)
    return files, directories


def verify_payload(source_dir, target_dir, package):
    source, source_dirs = file_tree(source_dir)
    target, target_dirs = file_tree(target_dir)
    prefix = package["archive_root"]
    if package["name"] == "adminer":
        if set(source) != {"adminer.php"} or source_dirs:
            raise ValueError("Adminer source must contain only adminer.php")
        mapping = {"adminer/index.php": "adminer.php"}
    else:
        mapping = {prefix + relative: relative for relative in source}
    expected = {path: source[relative] for path, relative in mapping.items()}
    if target != expected:
        raise ValueError(f"Mapped file set or bytes changed: {package['name']}")
    expected_dirs = {prefix.rstrip("/")}
    expected_dirs.update(prefix + relative for relative in source_dirs)
    for path in [*expected, *expected_dirs.copy()]:
        expected_dirs.update(str(p) for p in PurePosixPath(path).parents if str(p) != ".")
    if target_dirs != expected_dirs or any(path not in target for path in package["required_entries"]):
        raise ValueError(f"Mapped directory layout/entry changed: {package['name']}")
    return [{"source_path": mapping[path], "path": path, **expected[path]} for path in sorted(expected)]


def freeze_producer(manifest_path, seven_zip):
    paths = [("runtime-windows/repack_for_next.py", Path(__file__).resolve()), ("runtime-windows/next-bootstrap-packages.json", Path(manifest_path)), (".github/workflows/build-windows-next.yml", ROOT / ".github/workflows/build-windows-next.yml")]
    frozen = {name: path.read_bytes() for name, path in paths}
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], env=env, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=all"], env=env))
    dirty = dirty or Path(manifest_path).read_bytes() != (ROOT / "runtime-windows/next-bootstrap-packages.json").read_bytes()
    files = [{"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(frozen.items())]
    producer = {"head": head, "dirty": dirty, "script_sha256": digest(__file__), "source_manifest_sha256": digest(manifest_path), "files": files, "files_sha256": canonical_digest(files), "seven_zip": {"version": "\n".join(run([seven_zip, "i"]).strip().splitlines()[:2]), "create_args": CREATE_ARGS}}
    return producer, frozen, paths


def repack(source_manifest, source_dir, output_dir, runtime_version):
    source_manifest, source_dir, output_dir = map(lambda p: Path(p).resolve(), (source_manifest, source_dir, output_dir))
    data = load_source_manifest(source_manifest, runtime_version)
    if output_dir.exists() or output_dir == source_dir or output_dir in source_dir.parents:
        raise ValueError("Output must be a new directory separate from sources")
    seven_zip = os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7zz")
    if not seven_zip:
        raise ValueError("7z is required")
    producer, frozen, frozen_paths = freeze_producer(source_manifest, seven_zip)
    # Verify every source before creating any destination or extracting a byte.
    for package in data["packages"]:
        source = package["source"]
        archive = source_dir / source["filename"]
        if not archive.is_file() or archive.stat().st_size != source["bytes"] or digest(archive) != source["sha256"]:
            raise ValueError(f"Source hash/size mismatch: {package['name']}")
        entries = archive_entries(archive, seven_zip)
        names = {entry["Path"] for entry in entries if not entry.get("Attributes", "").startswith("D")}
        required = {"adminer.php"} if package["name"] == "adminer" else {p.removeprefix(package["archive_root"]) for p in package["required_entries"]}
        if not required <= names:
            raise ValueError(f"Missing source entry: {package['name']}")
    output_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source_manifest, output_dir / "next-bootstrap-packages.json")
    for name, content in frozen.items():
        path = output_dir / "producer" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    records = []
    for package in data["packages"]:
        with tempfile.TemporaryDirectory(prefix="servbay-next-repack-") as temporary:
            temporary = Path(temporary)
            source, stage, unpacked = temporary / "source", temporary / "stage", temporary / "check"
            source.mkdir()
            stage.mkdir()
            archive = source_dir / package["source"]["filename"]
            run([seven_zip, "x", "-y", "-o" + str(source), str(archive)])
            file_tree(source)
            destination = stage / package["archive_root"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if package["name"] == "adminer":
                destination.mkdir()
                shutil.copyfile(source / "adminer.php", destination / "index.php")
            else:
                shutil.copytree(source, destination)
            verify_payload(source, stage, package)
            filename = package["source"]["filename"]
            output = output_dir / filename
            run([seven_zip, *CREATE_ARGS, str(output), "."], cwd=stage)
            archive_entries(output, seven_zip)
            run([seven_zip, "x", "-y", "-o" + str(unpacked), str(output)])
            mapping = verify_payload(source, unpacked, package)
            mapping_name = package["name"] + "-files.json"
            mapping_path = output_dir / mapping_name
            mapping_path.write_text(json.dumps(mapping, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            records.append({**package, **BATCH, "filename": filename, "sha256": digest(output), "output_bytes": output.stat().st_size, "file_manifest": {"filename": mapping_name, "sha256": digest(mapping_path)}, "producer": producer, "compatibility": {"status": "partial", "next_head": None, "next_dirty": None, "source_fingerprint": None, "binary_sha256": None, "windows_version": None, "cpu": None, "evidence_path": None, "evidence_sha256": None}})
    # Source archives and executed source files must still be the frozen bytes.
    for name, path in frozen_paths:
        if path.read_bytes() != frozen[name]:
            raise ValueError(f"Producer source changed during repack: {name}")
    for record in records:
        if digest(source_dir / record["source"]["filename"]) != record["source"]["sha256"]:
            raise ValueError("Source archive changed during repack")
    batch = {**BATCH, "packages": records, "validation_complete": False, "published": False, "unknown_fields": {"source.original_build_revision": "Original upstream build revision/run were not supplied by the v1 catalog", "compatibility": "Partial evidence only; final Next binary/source binding and native installation/HTTP acceptance pending", "target_mapping": "Public v2 CDN mapping and final release date/notes remain unconfirmed"}}
    (output_dir / "meta.json").write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--runtime-version", required=True, choices=["v2"])
    args = parser.parse_args()
    try:
        batch = repack(args.source_manifest, args.source_dir, args.output_dir, args.runtime_version)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Repack failed: {error}\n")
    print(json.dumps(batch, indent=2))


if __name__ == "__main__":
    main()
