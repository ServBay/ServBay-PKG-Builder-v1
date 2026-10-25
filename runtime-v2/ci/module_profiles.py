#!/usr/bin/env python3
"""Resolve module recipes and compatible public candidates without claiming publication."""

import copy
import importlib.util
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.request


_spec = importlib.util.spec_from_file_location("servbay_module_inventory", Path(__file__).with_name("module_manifest.py"))
_inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_inventory)


def module_records(host, arch, config=None, revision=0, *, os_name="macos"):
    if os_name not in ("macos", "windows") or (os_name == "windows" and config is None):
        raise ValueError("Windows modules need an explicit Windows profile")
    host = _inventory._host(copy.deepcopy(host))
    if os_name == "windows":
        arch = {"x64": "x86_64"}.get(arch, arch)
        # PHP 5.3/5.4 modules are x86; every other Windows module is x86_64.
        if arch not in ("x86_64", "x86") or (arch == "x86" and host["type"] != "php"):
            raise ValueError("Unsupported Windows module architecture")
    elif arch not in ("x86_64", "arm64"):
        raise ValueError("Modules need an explicit supported architecture")
    if type(revision) is not int or revision < 0:
        raise ValueError("Modules need an explicit supported architecture and revision")
    config = Path(config) if config else Path(__file__).parents[1] / "config/modules.json"
    profile = json.loads(config.read_text())
    if profile.get("schema_version") != 1 or profile.get("os", "macos") != os_name:
        raise ValueError("Unsupported module profile schema")
    selected = profile.get("hosts", {}).get(host["type"], {}).get(host["series"])
    if not isinstance(selected, dict):
        raise ValueError("No verified module recipe profile for the actual host series")
    bundles = profile.get("bundles", {}).get(host["type"], {}).get(host["series"], {})
    rows = []
    for recipe, version in selected.items():
        description = profile["recipes"][recipe]
        _inventory._state._text(version, "module profile version")
        if description["kind"] != host["type"] + "-extension" or type(description["default"]) is not bool:
            raise ValueError("Module profile kind/default differs from the actual host")
        bundled = bundles.get(recipe, {})
        if not isinstance(bundled, dict) or any(selected.get(name) != value for name, value in bundled.items()):
            raise ValueError("Bundled module versions must use the same actual host profile")
        row = {"runtime_version": "v2", "os": os_name, "kind": description["kind"],
               "name": description["name"], "recipe_name": recipe, "version": version,
               "build_revision": revision, "arch": arch,
               "host": {key: copy.deepcopy(host[key]) for key in ("type", "series", "abi")},
               "default": description["default"], "bundled": copy.deepcopy(bundled)}
        _inventory._state.clean_record(row)
        rows.append(row)
    by_name = {row["name"]: row for row in rows}
    for row in rows:
        required = profile["recipes"][row["recipe_name"]].get("requires", [])
        if not isinstance(required, list) or len(set(required)) != len(required):
            raise ValueError("Module requirements must name distinct profile modules")
        if required:
            if any(name == row["name"] or name not in by_name for name in required):
                raise ValueError("A required independent module lacks the actual host profile")
            row["required_modules"] = {name: _inventory._state.clean_record(by_name[name]) for name in required}
    return rows


def catalog_rows(payload):
    # Actual ExtensionController::index returns status_code/message/data.
    if not isinstance(payload, dict) or payload.get("status_code") != 200:
        raise ValueError("The public module catalog did not report a successful response")
    rows = payload.get("data", {}).get("extensions")
    if not isinstance(rows, list):
        raise ValueError("The public module catalog needs its actual extensions list")
    return rows


def read_catalog(url, arch, *, os_name="macos"):
    if os_name not in ("macos", "windows"):
        raise ValueError("Unsupported module catalog OS")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Configure the public HTTPS module catalog URL explicitly")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(runtime_version="v2", os=os_name, arch=arch)
    target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    request = urllib.request.Request(target, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return catalog_rows(json.load(response))


def _os_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("Invalid actual module minimum OS")
    fields = tuple(map(int, value.split(".")))
    return fields + (0,) * (3 - len(fields))


def select_modules(records, catalog, base, minimum_os, *, os_name="macos", windows_compatibility=None):
    if os_name not in ("macos", "windows"):
        raise ValueError("Unsupported module catalog OS")
    _os_version(minimum_os)
    if os_name == "windows":
        if base is not None:
            raise ValueError("Windows modules must not have a base")
        _inventory._compatibility(windows_compatibility)
    if not isinstance(catalog, list):
        raise ValueError("Catalog failure is not an empty successful result")
    builds, candidates, references = [], [], []
    for record in records:
        if _inventory._state.clean_record(record)["os"] != os_name:
            raise ValueError("Module record OS differs from catalog context")
        selected = []
        for row in catalog:
            if not isinstance(row, dict):
                raise ValueError("Invalid module catalog row")
            if (row.get("name"), row.get("extName"), row.get("version"), row.get("hostVersion"), row.get("arch"),
                    row.get("os"), row.get("runtimeVersion"), row.get("buildRevision")) != (
                    {"php": "php-ext", "postgresql": "pgsql-ext"}[record["host"]["type"]],
                    record["name"], record["version"], record["host"]["series"], record["arch"],
                    {"macos": "macOS", "windows": "Windows"}[os_name], "v2", record["build_revision"]):
                continue
            manifest = row.get("config", {}).get("module_manifest") if isinstance(row.get("config"), dict) else None
            if not isinstance(manifest, dict):
                continue
            try:
                _inventory._validate_inventory(manifest, expected_os=os_name)
                _inventory._sha(row.get("hash"))
            except ValueError:
                continue
            compatibility = {key: manifest["host"][key] for key in ("type", "series", "abi")}
            if (compatibility != record["host"] or manifest["arch"] != record["arch"] or manifest.get("base") != base
                    or (os_name == "windows" and manifest["windows_compatibility"] != windows_compatibility)
                    or _os_version(manifest["minimum_os"]) > _os_version(minimum_os)):
                continue
            url = urlsplit(row.get("downloadUrl", ""))
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("An external module candidate needs its actual public archive URL")
            selected.append(row)
        if len(selected) > 1:
            raise ValueError("Ambiguous public candidates for the exact module identity")
        if not selected:
            builds.append(copy.deepcopy(record))
            continue
        row = selected[0]
        reference = {key: copy.deepcopy(record[key]) for key in ("kind", "name", "version", "build_revision", "host")}
        reference["sha256"] = row["hash"]
        references.append(reference)
        candidates.append({"record": copy.deepcopy(record), "reference": reference, "download_url": row["downloadUrl"],
                           "module_manifest": copy.deepcopy(row["config"]["module_manifest"]),
                           "publication_status": "requires_uploader_confirmation"})
    # Public GET omits the protected publication key. Only the Uploader's exact
    # protected2 lookup can confirm these references before registering a core.
    return {"build": builds, "external_candidates": candidates, "references": references}
