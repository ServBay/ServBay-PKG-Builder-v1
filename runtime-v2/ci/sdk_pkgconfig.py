#!/usr/bin/env python3
"""Describe Apple's actual SDK libffi in a build-only pkg-config directory."""

import argparse
from pathlib import Path
import re


def create_sdk_pkgconfig(sdk, destination):
    sdk, destination = Path(sdk), Path(destination)
    header = sdk / "usr/include/ffi/ffi.h"
    if not header.is_file() or not (sdk / "usr/lib/libffi.tbd").is_file():
        raise ValueError("The Apple SDK libffi header and linker stub are required")
    version = re.search(r"^\s*libffi\s+([0-9][A-Za-z0-9.+-]*)\s*$", header.read_text(), re.MULTILINE)
    if not version:
        raise ValueError("Cannot determine libffi version from the actual Apple SDK header")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "libffi.pc").write_text(
        f"prefix={sdk}/usr\nincludedir=${{prefix}}/include/ffi\n\n"
        "Name: libffi\nDescription: Apple SDK Foreign Function Interface library\n"
        f"Version: {version.group(1)}\nLibs: -lffi\nCflags: -I${{includedir}}\n")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    create_sdk_pkgconfig(args.sdk, args.output)


if __name__ == "__main__":
    main()
