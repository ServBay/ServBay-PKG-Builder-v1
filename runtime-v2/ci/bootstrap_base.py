#!/usr/bin/env python3
"""Install the fixed public Runtime v2 binary build base on a disposable runner."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_base(path, destination):
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "packages":
                raise ValueError(f"Invalid v2 archive root: {member.name}")
            if len(name.parts) > 1 and name.parts[1] != "common":
                raise ValueError(f"Unexpected build-base path: {member.name}")
            if member.issym() or member.islnk():
                target = (posixpath.join(posixpath.dirname(member.name), member.linkname)
                          if member.issym() else member.linkname)
                target = PurePosixPath(posixpath.normpath(target))
                if target.is_absolute() or target.parts[:2] != ("packages", "common"):
                    raise ValueError(f"Escaping build-base link: {member.name}")
            elif not member.isdir() and not member.isfile():
                raise ValueError(f"Unsupported build-base entry: {member.name}")
            member.mode &= 0o777
        # The explicit checks above also cover the Python 3.9 shipped by macOS.
        archive.extractall(destination)


def verify_base(root, arch):
    common = root / "packages/common"
    if not (common / "include").is_dir() or not (common / "lib").is_dir():
        raise ValueError("The v2 base needs both Runtime libraries and Devlib headers")
    macho = 0
    magics = (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
              b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")
    for path in common.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            header = handle.read(8)
        static = header == b"!<arch>\n"
        if header[:4] not in magics and not static:
            continue
        # `file` distinguishes Java CAFEBABE classes from real fat Mach-O files.
        kind = subprocess.run(["/usr/bin/file", "-b", str(path)], check=True,
                              capture_output=True, text=True).stdout
        if header[:4] == b"\xca\xfe\xba\xbe" and "Java class" in kind:
            continue
        if not static and "Mach-O" not in kind:
            raise ValueError(f"Damaged Mach-O build-base file: {path.relative_to(root)}")
        architectures = subprocess.run(["/usr/bin/lipo", "-archs", str(path)], check=True,
                                       capture_output=True, text=True).stdout.split()
        if architectures != [arch]:
            raise ValueError(f"Wrong build-base architecture: {path.relative_to(root)}: {architectures}")
        macho += 1
    if not macho:
        raise ValueError("No Mach-O Runtime libraries found in the v2 base")


def bootstrap(arch, env):
    required = ("BASE_DOWNLOAD_URL", "BASE_VERSION", "BASE_SHA256_RUNTIME", "BASE_SHA256_DEVLIB")
    for field in required:
        if not env.get(field):
            raise ValueError(f"{field} is required for a pinned v2 build base")
    if not re.fullmatch(r"[0-9][A-Za-z0-9_.+-]*", env["BASE_VERSION"]):
        raise ValueError("Invalid BASE_VERSION")
    for field in required[2:]:
        if not re.fullmatch(r"[0-9a-f]{64}", env[field]):
            raise ValueError(f"Invalid {field}")
    url = urlsplit(env["BASE_DOWNLOAD_URL"])
    if url.scheme not in ("https", "file") or url.username or url.password or url.query or url.fragment:
        raise ValueError("BASE_DOWNLOAD_URL must be a public HTTPS archive location")
    identity = {"runtime_version": "v2", "arch": arch, "version": env["BASE_VERSION"],
                "runtime_sha256": env["BASE_SHA256_RUNTIME"], "devlib_sha256": env["BASE_SHA256_DEVLIB"]}
    base = Path(env.get("SERVBAY_BASE_FULL_PATH", "/Applications/ServBay")).absolute()
    marker = base / ".bootstrap-v2.json"
    if marker.is_file():
        if json.loads(marker.read_text()) != identity:
            raise ValueError("The restored v2 base identity differs from the requested archives")
        verify_base(base, arch)
    else:
        if base.exists() and (base.is_symlink() or any(base.iterdir())):
            raise ValueError(f"Refusing to replace an existing unverified build environment: {base}")
        base.parent.mkdir(parents=True, exist_ok=True)
        subpath = env.get("BASE_REMOTE_SUBPATH", f"macOS/runtime/{arch}")
        if ".." in PurePosixPath(subpath).parts or subpath.startswith("/"):
            raise ValueError("Invalid BASE_REMOTE_SUBPATH")
        # This temporary candidate is owned solely by this invocation.
        with tempfile.TemporaryDirectory(prefix=".servbay-base-", dir=base.parent) as work:
            work = Path(work)
            candidate = work / "candidate"
            candidate.mkdir()
            for kind, expected in (("Runtime", identity["runtime_sha256"]), ("Devlib", identity["devlib_sha256"])):
                filename = f"ServBay-{kind}-v{identity['version']}-{arch}.tar.gz"
                source = "/".join(part.strip("/") for part in (env["BASE_DOWNLOAD_URL"], subpath, filename) if part)
                target = work / filename
                subprocess.run(["/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
                                "--retry", "3", "--retry-all-errors", "--connect-timeout", "20",
                                "--max-time", "1200", "--output", str(target), source], check=True)
                if sha256(target) != expected:
                    raise ValueError(f"SHA256 mismatch for {kind}")
                extract_base(target, candidate)
            verify_base(candidate, arch)
            (candidate / marker.name).write_text(json.dumps(identity, sort_keys=True) + "\n")
            os.replace(candidate, base)
    print(f"devlib_version={identity['version']}")
    if env.get("GITHUB_OUTPUT"):
        with Path(env["GITHUB_OUTPUT"]).open("a") as handle:
            handle.write(f"devlib_version={identity['version']}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arch", choices=("x86_64", "arm64"))
    args = parser.parse_args()
    bootstrap(args.arch, os.environ)


if __name__ == "__main__":
    main()
