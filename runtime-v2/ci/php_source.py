"""Bind explicit PHP security rebuilds to their actual upstream source."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


PROFILE = Path(__file__).resolve().parents[1]


def backport_release(version):
    data = json.loads((PROFILE / "config/php-backports.json").read_text())
    if data.get("schema_version") != 1 or not isinstance(data.get("releases"), dict):
        raise ValueError("Invalid explicit PHP security-release map")
    release = data["releases"].get(version)
    if release is None:
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("PHP local release has no explicit source/security-patch mapping")
        return None
    if set(release) != {"source_version", "patch", "patch_sha256"}:
        raise ValueError("Incomplete PHP security-release provenance")
    if not re.fullmatch(re.escape(release["source_version"]) + r"\.[1-9][0-9]*", version):
        raise ValueError("PHP security release does not extend its exact upstream version")
    patch = PROFILE / "patch" / release["patch"]
    if not patch.resolve().is_relative_to((PROFILE / "patch").resolve()):
        raise ValueError("PHP security patch is outside the software profile")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != release["patch_sha256"]:
        raise ValueError("PHP security patch checksum differs from the explicit release")
    return dict(release)


def source_version(version):
    release = backport_release(version)
    return release["source_version"] if release else version


def _replace_once(text, pattern, replacement, label):
    text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected one PHP {label}, found {count}")
    return text


def rewrite_local_version(source, version, upstream):
    """Keep numeric/API identifiers unchanged while exposing the local release."""
    release = backport_release(version)
    if release is None or release["source_version"] != upstream:
        raise ValueError("PHP program version requires an explicit security release")
    source = Path(source)
    extra = version[len(upstream):]
    header = source / "main/php_version.h"
    text = header.read_text()
    text = _replace_once(text, r'^#define PHP_VERSION "' + re.escape(upstream) + r'"$',
                         '#define PHP_VERSION "' + version + '"', "upstream header version")
    text = _replace_once(text, r'^#define PHP_EXTRA_VERSION ""$',
                         '#define PHP_EXTRA_VERSION "' + extra + '"', "header extra version")
    # Configure regenerates this header and php-config. Update both its m4 input
    # and the distributed configure script; changing only the header is lost.
    inputs = [path for path in (source / "configure.ac", source / "configure.in") if path.is_file()]
    if len(inputs) != 1 or not (source / "configure").is_file():
        raise ValueError("PHP version generation inputs are incomplete")
    updates = {header: text}
    for path in [*inputs, source / "configure"]:
        value = path.read_text()
        value = _replace_once(value, r'^PHP_EXTRA_VERSION=(?:""|\[\$\]4|\$4)$',
                              'PHP_EXTRA_VERSION="' + extra + '"', f"extra version in {path.name}")
        value = value.replace("AC_INIT([PHP],[" + upstream + "]", "AC_INIT([PHP],[" + version + "]")
        value = value.replace("PACKAGE_VERSION='" + upstream + "'", "PACKAGE_VERSION='" + version + "'")
        value = value.replace("PACKAGE_STRING='PHP " + upstream + "'", "PACKAGE_STRING='PHP " + version + "'")
        updates[path] = value
    for path, value in updates.items():
        path.write_text(value)


def isolate_legacy_libpq(source):
    """Retain old probes but remove external helpers and recognize Darwin libs."""
    source = Path(source)
    for name, variables in (("ext/pgsql/config.m4", ("PHP_PGSQL",)),
                            ("ext/pdo_pgsql/config.m4", ("PHP_PDO_PGSQL",)),
                            ("configure", ("PHP_PGSQL", "PHP_PDO_PGSQL"))):
        path = source / name
        text = path.read_text()
        for variable in variables:
            pattern = r'^(\s*)for i in \$' + variable + r' \$' + variable + r'/bin [^;\n]+; do'
            text = _replace_once(text, pattern,
                                 lambda match: match[1] + 'PG_CONFIG=""\n' + match[1]
                                 + 'for i in "$' + variable + '" "$' + variable + '/bin"; do',
                                 f"explicit {variable} helper search in {name}")
        old = 'test -f "$i/$j/libpq.so" || test -f "$i/$j/libpq.a"'
        if text.count(old) != len(variables):
            raise ValueError(f"Unexpected libpq library probes in {name}")
        text = text.replace(old, old + ' || test -f "$i/$j/libpq.dylib"')
        path.write_text(text)


def prepare_source(source, version, build_dir, common):
    source, build_dir, common = Path(source), Path(build_dir), Path(common)
    release = backport_release(version)
    upstream = source_version(version)
    expected = re.sub(r'(-dev)-[0-9]{8}$', r'\1', upstream)
    header = (source / "main/php_version.h").read_text()
    if not re.search(r'^#define PHP_VERSION "' + re.escape(expected) + '"$', header, re.MULTILINE):
        raise ValueError("Actual PHP source header differs from its declared upstream input")
    if release:
        patch = PROFILE / "patch" / release["patch"]
        result = subprocess.run(["/usr/bin/patch", "--batch", "--forward", "-p1", "-i", str(patch)],
                                cwd=source, capture_output=True, text=True)
        if result.returncode:
            raise ValueError("PHP security patch failed: " + result.stdout + result.stderr)
        rewrite_local_version(source, version, upstream)
    if tuple(map(int, version.split('.')[:2])) < (8, 4):
        isolate_legacy_libpq(source)
        # Compatibility tools are build inputs, never Runtime/Devlib contents.
        tools = build_dir / "tmp/php-tools"
        tools.mkdir(parents=True, exist_ok=True)
        wrapper = tools / "freetype-config"
        pkgconfig = os.environ.get("PKG_CONFIG")
        if not pkgconfig:
            raise ValueError("The isolated pkg-config executable is required")
        wrapper.write_text('''#!/bin/sh
case "$1" in
  --prefix) printf '%s\\n' ''' + shlex.quote(str(common)) + ''' ;;
  --version) exec ''' + shlex.quote(pkgconfig) + ''' --modversion freetype2 ;;
  --cflags) exec ''' + shlex.quote(pkgconfig) + ''' --cflags freetype2 ;;
  --libs) exec ''' + shlex.quote(pkgconfig) + ''' --libs freetype2 ;;
  *) exit 1 ;;
esac
''')
        wrapper.chmod(0o755)
        for path in (source / "ext/gd/config.m4", source / "configure"):
            if path.is_file():
                text = path.read_text()
                if "$i/bin/freetype-config" in text:
                    path.write_text(text.replace("$i/bin/freetype-config", "$FREETYPE2_CONFIG"))
    return dict(version=version, source_version=upstream, security_patch=release)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--common", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_source(args.source, args.version, args.build_dir, args.common), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"PHP source preparation failed: {error}\n")


if __name__ == "__main__":
    main()
