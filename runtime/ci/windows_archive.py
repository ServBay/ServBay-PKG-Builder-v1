"""Strict Windows-relative inventories and real 7z payload verification."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


# Microsoft Win32 naming rules include the superscript device-number aliases.
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def validate_path(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(char) < 32 or char in '<>:"|?*' for char in value)):
        raise ValueError(f"Invalid Windows relative path: {value!r}")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) or _DEVICE.match(part)
           for part in value.split("/")):
        raise ValueError(f"Invalid Windows relative path: {value!r}")
    return value


def validate_paths(files, directories=()):
    """Check every explicit and implied parent, without imposing an ASCII whitelist."""
    nodes = {}
    explicit = set()
    for name, directory in [(name, False) for name in files] + [(name, True) for name in directories]:
        validate_path(name)
        if name.casefold() in explicit:
            raise ValueError("Duplicate Windows archive path: " + name)
        explicit.add(name.casefold())
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            key = path.casefold()
            node = (path, directory if index == len(parts) else True)
            if key in nodes and nodes[key] != node:
                raise ValueError("Windows path or parent collision: " + path)
            nodes[key] = node


def _tool(seven_zip):
    tool = seven_zip or os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7zz")
    if not tool:
        raise ValueError("7z or 7zz is required for Windows archives")
    return str(tool)


def _run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True)
    if result.returncode:
        raise ValueError(f"7z failed ({result.returncode}): " + (result.stderr or result.stdout).decode("utf-8", errors="replace"))
    return result.stdout


def staging_files(staging):
    staging = Path(staging)
    if staging.is_symlink() or not staging.is_dir():
        raise ValueError("Windows staging must be a real directory")
    files, directories = [], []
    for path in sorted(staging.rglob("*")):
        name = validate_path(path.relative_to(staging).as_posix())
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            directories.append(name)
        elif stat.S_ISREG(mode) and path.stat().st_nlink == 1:
            files.append(name)
        else:
            raise ValueError("Windows payload links and special files are unsupported: " + name)
    validate_paths(files, directories)
    return files


def create_archive(staging, archive, names, seven_zip=None, compression_level=7):
    if type(compression_level) is not int or not 0 <= compression_level <= 9:
        raise ValueError("7z compression level must be an integer from 0 to 9")
    actual_files = staging_files(staging)
    staging, archive = Path(staging).resolve(), Path(archive).absolute()
    names = list(names)
    validate_paths(names)
    if set(actual_files) != set(names):
        raise ValueError("Every staged Windows file must be owned exactly once")
    if archive.exists():
        raise ValueError("Refusing to update an existing Windows archive")
    # An explicit UTF-8 list avoids option/@list interpretation for payload names.
    # -spd prevents wildcard expansion; list contents are paths, not CLI switches.
    with tempfile.TemporaryDirectory(prefix="windows-archive-") as temporary:
        listing = Path(temporary) / "files.txt"
        listing.write_text("".join(name + "\n" for name in names), encoding="utf-8")
        _run([_tool(seven_zip), "a", "-t7z", f"-mx={compression_level}", "-mtc=off", "-mta=off", "-mtm=off",
              "-spd", "-scsUTF-8", str(archive), "@" + str(listing)], cwd=staging)


def parse_listing(output):
    """Parse one complete 7z listing into canonical, type-checked members."""
    if isinstance(output, bytes):
        output = output.decode("utf-8")
    output = output.replace("\r\n", "\n").replace("\r", "\n")
    records = []
    for block in output.strip().split("\n\n"):
        entry = {}
        for line in block.splitlines():
            if " = " in line:
                key, value = line.split(" = ", 1)
            elif line.endswith(" ="):
                key, value = line[:-2], ""
            else:
                raise ValueError("Ambiguous 7z archive listing line: " + repr(line))
            if key in entry:
                raise ValueError("Duplicate 7z archive listing field")
            entry[key] = value
        if "Path" in entry:
            # 7-Zip uses the host separator in listings even for archive-relative
            # member names. Canonical paths are independent of the reading host.
            entry["Path"] = entry["Path"].replace("\\", "/")
        name = validate_path(entry.get("Path"))
        attrs = entry.get("Attributes", "")
        attribute_flags = attrs.split(" ", 1)[0]
        unix = re.search(r"(?:^| )([a-z-])[rwxStTs-]{9}(?: |$)", attrs)
        if ("Symbolic Link" in entry or "Hard Link" in entry or "Reparse" in attrs
                or "L" in attribute_flags
                or (unix and unix.group(1) not in ("-", "d")) or entry.get("Encrypted") != "-"):
            raise ValueError("Windows archive links, special or encrypted files are unsupported: " + name)
        if not attrs:
            raise ValueError("Missing 7z archive member attributes")
        directory = "D" in attribute_flags
        if "Folder" in entry and entry["Folder"] != ("+" if directory else "-"):
            raise ValueError("Conflicting 7z archive folder flag")
        if unix and (unix.group(1) == "d") != directory:
            raise ValueError("Conflicting 7z archive member type")
        records.append({"entry": entry, "path": name, "directory": directory,
                        "block": block.strip()})
    return records


def extract_verified_archive(archive, destination, seven_zip=None):
    """Validate one complete listing, extract once, then hash actual files."""
    archive = Path(archive).absolute()
    with archive.open("rb") as handle:
        if handle.read(6) != b"7z\xbc\xaf\x27\x1c":
            raise ValueError("Expected an actual 7z archive")
    tool = _tool(seven_zip)
    records = parse_listing(_run([
        tool, "l", "-t7z", "-slt", "-ba", "-sccUTF-8", "--", str(archive)
    ]))
    files = [record["path"] for record in records if not record["directory"]]
    directories = [record["path"] for record in records if record["directory"]]
    validate_paths(files, directories)
    if not files:
        raise ValueError("Empty Windows archive")

    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Verified extraction destination must be new")
    destination.mkdir()
    _run([tool, "x", "-t7z", "-y", "-o" + str(destination), "--", str(archive)])
    actual = staging_files(destination)
    if set(actual) != set(files):
        raise ValueError("Extracted Windows file set differs from its validated listing")
    def hash_member(name):
        value = hashlib.sha256()
        with (destination / name).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        return name, value.hexdigest()

    workers = min(8, max(1, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return dict(executor.map(hash_member, actual))


def archive_hashes(archive, seven_zip=None):
    archive = Path(archive).absolute()
    with archive.open("rb") as handle:
        if handle.read(6) != b"7z\xbc\xaf\x27\x1c":
            raise ValueError("Expected an actual 7z archive")
    tool = _tool(seven_zip)
    output = _run([tool, "l", "-t7z", "-slt", "-ba", "-sccUTF-8", "--", str(archive)])
    records = parse_listing(output)
    files = [record["path"] for record in records if not record["directory"]]
    directories = [record["path"] for record in records if record["directory"]]
    listed_blocks = {record["path"]: record["block"] for record in records}
    validate_paths(files, directories)
    if not files:
        raise ValueError("Empty Windows archive")
    # 7z's text listing substitutes some control characters with underscores.
    # Re-select each literal name: a substituted/truncated display name must not
    # be mistaken for a real empty file (7z returns success for no matches).
    for name in files + directories:
        selected = parse_listing(_run([tool, "l", "-t7z", "-slt", "-ba", "-spd", "-r-",
                                       "-sccUTF-8", "--", str(archive), name]))
        selected = [record["block"] for record in selected]
        if listed_blocks[name] not in [block.strip() for block in selected] or (name in files and len(selected) != 1):
            raise ValueError("Ambiguous or missing literal 7z member: " + name)
    # Read to stdout only after checking the entire namespace; never extract paths.
    hashes = {}
    for name in files:
        digest = hashlib.sha256()
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen([tool, "x", "-t7z", "-so", "-spd", "--", str(archive), name],
                                  stdout=subprocess.PIPE, stderr=errors) as process:
                for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                    digest.update(block)
                if process.wait():
                    errors.seek(0)
                    raise ValueError("7z payload read failed: " + errors.read().decode("utf-8", errors="replace"))
        hashes[name] = digest.hexdigest()
    return hashes


def read_members(archive, names, *, expected_sha256, seven_zip=None):
    """Read exact members after full namespace, archive and content verification."""
    archive = Path(archive)
    names = list(names)
    validate_paths(names)
    def digest():
        with archive.open("rb") as handle:
            value = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                value.update(block)
            return value.hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256 or "") or digest() != expected_sha256:
        raise ValueError("Windows archive SHA256 differs")
    hashes = archive_hashes(archive, seven_zip)
    if any(name not in hashes for name in names):
        raise ValueError("Missing exact Windows archive member")
    result = {}
    for name in names:
        content = _run([_tool(seven_zip), "x", "-t7z", "-so", "-spd", "--", str(archive.absolute()), name])
        if hashlib.sha256(content).hexdigest() != hashes[name]:
            raise ValueError("Windows archive member SHA256 changed: " + name)
        result[name] = content
    if digest() != expected_sha256:
        raise ValueError("Windows archive changed while reading members")
    return result
