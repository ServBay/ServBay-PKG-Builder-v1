"""Explicit v2 package layouts, independent of the producer staging directory.

These are target layout contracts, not claims that a package has passed native
runtime validation. Windows repacking must retain dependent DLLs and resources.
Unmapped packages fail until their actual entrypoints have been established.
"""

import importlib.util
from pathlib import Path
import re


_spec = importlib.util.spec_from_file_location(
    "servbay_layout_metadata", Path(__file__).with_name("package_meta.py")
)
_metadata = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_metadata)

# Recipe aliases are distinct from display labels and public package IDs.
_ALIASES = {"java": "openjdk", "httpd": "apache"}
_WINDOWS_UNSUPPORTED = {"typesense"}
_PLATFORMS = {"windows": "windows", "Windows": "windows",
              "macos": "macos", "macOS": "macos"}
_VERSION = re.compile(r"[0-9][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)*")

# Each row defines: versioned directory, Windows entrypoint, macOS entrypoint.
# Series come from package_metadata, but metadata labels do not decide whether
# the package uses a versioned directory (for example, Apache and Rust do not).
# Paths follow the v2 packing recipes and the verified native producer names.
_PACKAGES = {
    "php": (True, "bin/php.exe", "bin/php"),
    "python": (True, "python.exe", "bin/python{series}"),
    "go": (True, "bin/go.exe", "bin/go"),
    "ruby": (True, "bin/ruby.exe", "bin/ruby"),
    "mysql": (True, "bin/mysqld.exe", "bin/mysqld"),
    "mariadb": (True, "bin/mysqld.exe", "bin/mysqld"),
    "redis": (True, "bin/redis-server.exe", "bin/redis-server"),
    "mongodb": (True, "bin/mongod.exe", "bin/mongod"),
    "node": (True, "bin/node.exe", "bin/node"),
    "openjdk": (True, "bin/java.exe", "bin/java"),
    "postgresql": (True, "bin/postgres.exe", "bin/postgres"),
    "mailpit": (False, "bin/mailpit.exe", "bin/mailpit"),
    "ollama": (False, "bin/ollama.exe", "bin/ollama"),
    "minio": (False, "bin/minio.exe", "bin/minio"),
    "meilisearch": (False, "bin/meilisearch.exe", "bin/meilisearch"),
    "typesense": (False, "bin/typesense-server.exe", "bin/typesense-server"),
    "mongosh": (False, "bin/mongosh.exe", "bin/mongosh"),
    "mongotools": (False, "bin/bsondump.exe", "bin/bsondump"),
    "git": (False, "cmd/git.exe", "bin/git"),
    "openssl": (False, "bin/openssl.exe", "bin/openssl"),
    "memcached": (False, "bin/memcached.exe", "bin/memcached"),
    "dotnetsdk": (True, None, "dotnet"),
    "mono": (True, None, "bin/mono"),
    "composer": (True, "bin/composer", "bin/composer"),
    "maven": (False, "bin/mvn.cmd", "bin/mvn"),
    "deno": (False, "bin/deno.exe", "bin/deno"),
    "bun": (False, "bin/bun.exe", "bin/bun"),
    "adminer": (False, "index.php", "index.php"),
    "phpmyadmin": (False, "index.php", "index.php"),
    "mcp": (False, "bin/servbay-mcp.exe", "bin/servbay-mcp"),
    "ai-gateway": (False, "bin/servbay-ai-gateway.exe", "bin/servbay-ai-gateway"),
    "disk-cleaner": (False, "bin/servbay-diskcleaner.exe", "bin/servbay-diskcleaner"),
    "nginx": (False, "sbin/nginx.exe", "sbin/nginx"),
    "apache": (False, "bin/httpd.exe", "sbin/httpd"),
    "caddy": (False, "bin/caddy.exe", "bin/caddy"),
    "rust": (False, "bin/rustc.exe", "bin/rustc"),
    "simple-acme": (False, "wacs.exe", None),
    "cloudflared": (False, "bin/cloudflared.exe", "bin/cloudflared"),
    "frp": (False, "bin/frpc.exe", "bin/frpc"),
    "ngrok": (False, "bin/ngrok.exe", "bin/ngrok"),
    "pinggy": (False, "bin/pinggy.exe", "bin/pinggy"),
}


def normalize_arch(arch):
    """Accept canonical architectures and only the existing x64 input alias."""
    if not isinstance(arch, str) or arch not in ("x64", "x86_64", "arm64"):
        raise ValueError(f"Unsupported package architecture: {arch!r}")
    return "x86_64" if arch == "x64" else arch


def package_layout(name, version, *, os, arch):
    """Return canonical identity and forward-slash relative layout paths.

    package_root and entrypoint.path are relative to the installation's package
    directory (the ordinary package archive root). config_root is relative to
    its etc directory. No path includes a build-machine or installation prefix.
    Entrypoints use type=binary or type=php-script; PHP scripts require the named
    interpreter and must not be renamed to an executable. Windows Python keeps
    its complete native distribution subtree beside python.exe.
    """
    if not isinstance(os, str) or os not in _PLATFORMS:
        raise ValueError(f"Unsupported package OS: {os!r}")
    platform = _PLATFORMS[os]
    architecture = normalize_arch(arch)
    if platform == "windows" and architecture != "x86_64":
        raise ValueError("Windows v2 layouts currently support only x86_64")
    if not isinstance(name, str):
        raise ValueError(f"Unsupported package name: {name!r}")
    name = _ALIASES.get(name, name)
    if platform == "windows" and name in _WINDOWS_UNSUPPORTED:
        raise ValueError(f"No verified Windows producer for {name}")
    if name not in _PACKAGES:
        raise ValueError(f"No verified v2 package layout: {name!r}")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError(f"Invalid package version: {version!r}")

    versioned, windows_entry, macos_entry = _PACKAGES[name]
    relative_entry = windows_entry if platform == "windows" else macos_entry
    if relative_entry is None:
        raise ValueError(f"No verified {platform} entrypoint for {name}")
    series = _metadata.package_metadata(name, version)["major"]
    if name == "postgresql" and int(series) < 10:
        raise ValueError("PostgreSQL v2 layouts require version 10 or newer")
    if name == "composer" and version.split(".", 1)[0] != "2":
        raise ValueError("Composer v2 layouts support only 2 and 2.2 series")

    root = name + "/" + series if versioned else name
    entrypoint = {"path": root + "/" + relative_entry.format(series=series),
                  "type": "binary"}
    if name == "composer":
        entrypoint.update(type="php-script", interpreter="php")
    elif name in {"adminer", "phpmyadmin"}:
        entrypoint.update(type="php-script", interpreter="php")
    return {"name": name, "os": platform, "arch": architecture,
            "package_root": root, "config_root": root, "entrypoint": entrypoint}
