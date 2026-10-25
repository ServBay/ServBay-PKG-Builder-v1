#!/usr/bin/env python3
"""Freeze independent PHP split ZIPs into diagnostic Windows v2 7z payloads."""

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location("php_repack_" + name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_meta = _load("package_meta", "runtime/ci/package_meta.py")
package_layout = _load("package_layout", "runtime/ci/package_layout.py")
windows_archive = _load("windows_archive", "runtime/ci/windows_archive.py")
module_manifest = _load("module_manifest", "runtime-v2/ci/module_manifest.py")
core_manifest = _load("core_manifest", "runtime-v2/ci/core_manifest.py")
php_source = _load("php_source", "runtime-v2/ci/php_source.py")
IDENTITY_FIELDS = {"runtime_version", "os", "kind", "name", "version", "arch", "build_revision"}
ABI_FIELDS = {"module_api", "zend_extension_api", "extension_build", "zend_extension_build", "zts", "debug"}
CORE_DOCS = {"readme.md", "readme", "readme.txt", "license", "license.txt", "news", "news.txt",
             "snapshot.txt", "readme-redist-bins.txt", "install.txt", "install", "php.gif"}
CORE_CONFIG_SAMPLES = {"pws-php5cgi.reg", "pws-php5isapi.reg"}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("An explicit SHA256 is required")
    return value


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Input must be a regular file")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field: " + key)
        result[key] = value
    return result


def _identity(identity, *, module=False):
    fields = IDENTITY_FIELDS | ({"host"} if module else set())
    if not isinstance(identity, dict) or set(identity) != fields:
        raise ValueError("An explicit complete PHP package identity is required")
    row = package_meta._state.clean_record(identity)
    if (row["runtime_version"] != "v2" or row["os"] != "windows"
            or row["kind"] != ("php-extension" if module else "package")
            or (not module and row["name"] != "php")):
        raise ValueError("Unsupported Windows v2 PHP identity")
    if module and (not isinstance(identity["host"], dict) or set(identity["host"]) != {"type", "series", "abi"}):
        raise ValueError("Module identity needs exact host type/series/ABI")
    return row


def _source_record(row):
    # Keep public relative provenance only; never copy arbitrary source fields.
    result = {key: row[key] for key in ("role", "filename", "original_filename", "bytes", "sha256")}
    for key in ("extension", "source_artifact", "source_path"):
        if key in row:
            windows_archive.validate_path(row[key])
            result[key] = row[key]
    return result


def _asset(index, role, extension=None):
    assets = index.get("assets")
    if not isinstance(assets, list) or any(not isinstance(row, dict) for row in assets):
        raise ValueError("A split asset list is required")
    matches = [row for row in assets if row.get("role") == role and row.get("extension") == extension]
    if len(matches) != 1:
        raise ValueError("Exactly one original runtime asset is required: " + (extension or "core"))
    row = matches[0]
    for key in ("filename", "original_filename"):
        windows_archive.validate_path(row.get(key))
        if "/" in row[key] or not row[key].endswith(".zip"):
            raise ValueError("Runtime input must be an explicitly indexed ZIP basename")
    expected = role + "--" + ((extension + "--") if extension else "") + row["original_filename"]
    if row["filename"] != expected:
        raise ValueError("Indexed runtime filename differs from its role/extension identity")
    _sha(row.get("sha256"))
    if type(row.get("bytes")) is not int or row["bytes"] <= 0:
        raise ValueError("Runtime ZIP bytes must be explicit")
    _source_record(row)
    return row


def _zip_inventory(archive, row, destination):
    """Validate the entire namespace before writing files, then verify CRC/bytes."""
    members = row.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("A complete indexed member inventory is required")
    declared = {}
    for member in members:
        if (not isinstance(member, dict) or set(member) != {"path", "type", "bytes", "sha256"}
                or member.get("type") not in ("file", "directory")
                or type(member.get("bytes")) is not int or member["bytes"] < 0):
            raise ValueError("Invalid indexed ZIP member")
        _sha(member["sha256"])
        path = member["path"]
        if not isinstance(path, str) or path in declared:
            raise ValueError("Duplicate or invalid indexed ZIP member")
        windows_archive.validate_path(path[:-1] if member["type"] == "directory" and path.endswith("/") else path)
        declared[path] = member
    inventory, files, directories = {}, [], []
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        for info in infos:
            directory = info.is_dir()
            name = info.filename
            windows_archive.validate_path(name[:-1] if directory else name)
            source.fp.seek(info.header_offset)
            local_header = source.fp.read(30)
            if len(local_header) != 30 or local_header[:4] != b"PK\x03\x04":
                raise ValueError("Invalid ZIP local file header")
            local_flags = int.from_bytes(local_header[6:8], "little")
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if ((info.flag_bits | local_flags) & 1 or info.orig_filename != name or "\x00" in name
                    or kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG)
                    or (info.external_attr & 0x10 and not directory)
                    or directory and info.file_size != 0):
                raise ValueError("ZIP links, special files, encrypted or malformed members are unsupported")
            (directories if directory else files).append(name.rstrip("/") if directory else name)
        windows_archive.validate_paths(files, directories)
        if len(infos) != len(declared) or {info.filename for info in infos} != set(declared):
            raise ValueError("ZIP member set differs from its indexed inventory")
        for info in infos:
            data = source.read(info)  # zipfile validates CRC, including empty directories.
            actual = {"path": info.filename, "type": "directory" if info.is_dir() else "file",
                      "bytes": len(data), "sha256": _hash(data)}
            if actual != declared[info.filename] or len(data) != info.file_size:
                raise ValueError("ZIP member type/bytes/SHA differs from its indexed inventory")
            if not info.is_dir():
                target = destination / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                inventory[info.filename] = actual
    if not inventory:
        raise ValueError("Runtime ZIP has no regular files")
    if set(windows_archive.staging_files(destination)) != set(inventory):
        raise ValueError("Extracted ZIP file set differs")
    return inventory


def _core_target(path, extension_dir):
    lower = path.casefold()
    if "/" not in path:
        if lower.endswith((".exe", ".dll")) or path in ("phar.phar.bat", "pharcommand.phar"):
            return "bin/" + path
        if re.fullmatch(r"php[^/]*\.lib", path, re.I):
            return "lib/" + path
        if path in ("php.ini-development", "php.ini-production"):
            return "share/defaults/php/" + path
        if lower in CORE_CONFIG_SAMPLES:
            return "share/defaults/php/" + path
        if lower in CORE_DOCS:
            return "share/doc/php/" + path
    if path.startswith("dev/") and "/" not in path[4:] and lower.endswith(".lib"):
        return "lib/" + path[4:]
    if path.startswith("ext/") and "/" not in path[4:] and lower.endswith(".dll"):
        return extension_dir + "/" + path[4:]
    if path.startswith(("extras/ssl/", "lib/enchant/", "sasl2/")):
        return path
    raise ValueError("Unknown PHP core runtime structure: " + path)


def _module_targets(files, name, original, extension_dir):
    main = original[:-4] + ".dll"
    if main not in files:
        raise ValueError("Module ZIP lacks its exactly named PHP DLL")
    dlls = {path for path in files if "/" not in path and path.casefold().endswith(".dll")}
    result = {}
    for path in files:
        lower = path.casefold()
        if path == main or path == main[:-4] + ".pdb":
            target = extension_dir + "/" + path
        elif path in dlls:
            if lower.startswith("php_"):
                raise ValueError("Unexpected additional PHP module DLL")
            target = "bin/" + path
        elif "/" not in path and lower.endswith(".pdb") and path[:-4] + ".dll" in dlls:
            target = "bin/" + path
        else:
            if lower.endswith((".dll", ".pdb", ".exe", ".lib", ".zip", ".7z")):
                raise ValueError("Unknown module binary structure: " + path)
            target = "share/" + name + "/" + path
        result[path] = target
    return result, main


def _stage(files, targets, extracted, staging):
    windows_archive.validate_paths(list(targets.values()))
    records = []
    for path in sorted(files):
        target = staging / targets[path]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(extracted / path, target)
        member = files[path]
        if target.stat().st_size != member["bytes"] or package_meta.sha256(target) != member["sha256"]:
            raise ValueError("Mapped PHP file bytes changed")
        records.append({"source_path": path, "target_path": targets[path], "bytes": member["bytes"], "sha256": member["sha256"]})
    return records


def _archive(staging, archive, records, tool):
    expected = {row["target_path"]: row["sha256"] for row in records}
    windows_archive.create_archive(staging, archive, expected, tool)
    with tempfile.TemporaryDirectory(prefix="servbay-php-readback-") as temporary:
        actual = windows_archive.extract_verified_archive(archive, Path(temporary) / "payload", tool)
    if actual != expected:
        raise ValueError("PHP 7z readback file set/SHA differs")
    return package_meta.sha256(archive)


def _shared_targets(core_files, modules, root):
    destinations = {}
    for owner, rows in [("core", core_files)] + [(name, row["files"]) for name, row in modules.items()]:
        for row in rows:
            path = row["target_path"].removeprefix(root + "/") if owner == "core" else row["target_path"]
            previous = destinations.setdefault(path, {"target_path": path, "sha256": row["sha256"], "bytes": row["bytes"], "sources": []})
            if (previous["sha256"], previous["bytes"]) != (row["sha256"], row["bytes"]):
                raise ValueError("Conflicting PHP host destination: " + path)
            previous["sources"].append({"owner": owner, "source_path": row["source_path"]})
    # Validate case and parent collisions across independent archive owners, too.
    windows_archive.validate_paths(list(destinations))
    return [destinations[path] for path in sorted(destinations) if len(destinations[path]["sources"]) > 1]


def prepare_php_payloads(split_source, core_identity, module_identities, core_evidence, output_dir, *, seven_zip=None):
    """Return frozen payload paths and a non-publishable audit; never write metadata."""
    if not isinstance(split_source, dict) or set(split_source) != {"path", "sha256"}:
        raise ValueError("Split source needs exact path/SHA256")
    _sha(split_source["sha256"])
    original = Path(split_source["path"]).absolute()
    _regular(original)
    data = original.read_bytes()
    if _hash(data) != split_source["sha256"]:
        raise ValueError("Split index SHA256 differs")
    index = json.loads(data, object_pairs_hook=_unique_object)
    if (not isinstance(index, dict) or index.get("schema") != "servbay.php-split-assets.v1"
            or index.get("publishable_v2") is not False):
        raise ValueError("Expected a non-publishable split-assets v1 index")
    core = _identity(core_identity)
    layout = package_layout.package_layout("php", core["version"], os="windows", arch=core["arch"])
    root = layout["package_root"]
    series = root.removeprefix("php/")
    context = index.get("build_context")
    if (not isinstance(context, dict) or context.get("arch") != "x64"
            or context.get("thread_safe") not in ("nts", "ts")
            or context.get("display_version") != core["version"] or context.get("build_version") != core["version"]):
        raise ValueError("PHP core identity and original split context differ")
    producer = context.get("producer")
    if (not isinstance(producer, dict) or set(producer) != {"repository", "commit", "run_id", "run_attempt"}
            or not isinstance(producer["repository"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", producer["repository"])
            or type(producer["run_id"]) is not int):
        raise ValueError("Original split producer identity is required")
    package_meta._state.validate_producer({**{k: producer[k] for k in ("commit", "run_attempt")}, "run_id": str(producer["run_id"])})
    if (not isinstance(core_evidence, dict) or set(core_evidence) != {"source_sha256", "version", "abi"}
            or core_evidence["version"] != core["version"] or not isinstance(core_evidence["abi"], dict)
            or set(core_evidence["abi"]) != ABI_FIELDS):
        raise ValueError("Explicit actual core source/version/six-field ABI evidence is required")
    abi = core_evidence["abi"]
    if type(abi["zts"]) is not bool or abi["zts"] != (context["thread_safe"] == "ts"):
        raise ValueError("Core ABI thread safety differs from the source ZIP")
    extension_dir = "lib/php/extensions/" + ("debug" if abi["debug"] else "no-debug")
    extension_dir += "-" + ("zts" if abi["zts"] else "non-zts") + "-" + str(abi["module_api"])
    windows_archive.validate_path(extension_dir)
    core_asset = _asset(index, "core-runtime")
    if _sha(core_evidence["source_sha256"]) != core_asset["sha256"]:
        raise ValueError("Actual ABI evidence is not bound to this original core runtime ZIP")
    thread = "nts-" if context["thread_safe"] == "nts" else ""
    pattern = r"php-" + re.escape(core["version"]) + "-" + thread + r"Win32-((?:vs|vc)[0-9]+)-x64\.zip"
    match = re.fullmatch(pattern, core_asset["original_filename"], re.I)
    if not match or context.get("core_runtime_filename") != core_asset["original_filename"]:
        raise ValueError("Original PHP runtime filename identity differs")
    toolchain = match.group(1)
    if not isinstance(module_identities, list):
        raise ValueError("Module identities must be an explicit list")
    expected_host = {"type": "php", "series": series, "abi": abi}
    modules, assets = {}, {"core": core_asset}
    selected = index.get("selected_extensions")
    if not isinstance(selected, list) or any(not isinstance(row, dict) for row in selected):
        raise ValueError("Original selected extension identities are required")
    for identity in module_identities:
        module = _identity(identity, module=True)
        name = module["name"]
        if name in modules or module["host"] != expected_host:
            raise ValueError("Duplicate module or different actual core host ABI")
        requests = [row for row in selected if row.get("name") == name]
        if len(requests) != 1 or requests[0].get("ref") not in (module["version"], "v" + module["version"]):
            raise ValueError("Module version differs from the producer request")
        asset = _asset(index, "extension-runtime", name)
        filename = f"php_{name}-{requests[0]['ref']}-{core['version']}-{context['thread_safe']}-{toolchain}-x86_64.zip"
        if asset["original_filename"] != filename:
            raise ValueError("Original module runtime ZIP name/version/arch/thread differs")
        modules[name] = {"identity": module, "source": _source_record(asset)}
        assets[name] = asset
    output = Path(output_dir).absolute()
    if (output.exists() or output.is_symlink() or not output.parent.is_dir()
            or output.resolve().is_relative_to(original.parent.resolve())):
        raise ValueError("Output must be a new directory outside the input assets")
    tool = windows_archive._tool(seven_zip)
    with tempfile.TemporaryDirectory(prefix="servbay-php-repack-") as temporary:
        temporary = Path(temporary)
        # Freeze all selected archives before inspecting or staging any payload.
        for owner, asset in assets.items():
            source = original.parent / asset["filename"]
            _regular(source)
            frozen = temporary / (owner + ".zip")
            shutil.copyfile(source, frozen)
            if frozen.stat().st_size != asset["bytes"] or package_meta.sha256(frozen) != asset["sha256"]:
                raise ValueError("Selected runtime ZIP SHA256/bytes differs")
        bundle = temporary / "bundle"
        bundle.mkdir()
        inventories = {}
        for owner, asset in assets.items():
            extracted = temporary / (owner + "-extracted")
            extracted.mkdir()
            inventories[owner] = _zip_inventory(temporary / (owner + ".zip"), asset, extracted)
        core_targets = {path: root + "/" + _core_target(path, extension_dir) for path in inventories["core"]}
        if not {"php.exe", "php-cgi.exe", "php-win.exe", "php.ini-development", "php.ini-production"} <= set(core_targets):
            raise ValueError("Core runtime lacks required PHP entrypoints/defaults")
        stage = temporary / "core-stage"
        stage.mkdir()
        core_files = _stage(inventories["core"], core_targets, temporary / "core-extracted", stage)
        core_name = f"php-{core['version']}-windows-x86_64.7z"
        core_sha = _archive(stage, bundle / core_name, core_files, tool)
        host = {**expected_host, "version": core["version"], "package_sha256": core_sha}
        module_manifest._host(host)
        module_manifest._windows_extension_dir(host, extension_dir)
        defaults = [{"archive_path": row["target_path"], "etc_relative_path": root + "/php.ini",
                     "sha256": row["sha256"], "bytes": row["bytes"], "automatically_enabled": False,
                     "kind": "upstream-default", "variant": row["source_path"].removeprefix("php.ini-")}
                    for row in core_files if row["source_path"] in ("php.ini-development", "php.ini-production")]
        module_paths = {}
        for name, module in modules.items():
            targets, main = _module_targets(inventories[name], name, assets[name]["original_filename"], extension_dir)
            stage = temporary / (name + "-stage")
            stage.mkdir()
            module["files"] = _stage(inventories[name], targets, temporary / (name + "-extracted"), stage)
            filename = f"ext-{name}-{module['identity']['version']}-php{series}-windows-x86_64.7z"
            relative = "modules/" + name + "/" + filename
            archive = bundle / relative
            archive.parent.mkdir(parents=True)
            module["archive"] = {"filename": relative, "sha256": _archive(stage, archive, module["files"], tool)}
            content = ("zend_extension" if name == "xdebug" else "extension") + "=" + main + "\n"
            module["configuration_candidate"] = {"etc_relative_path": root + "/conf.d/" + name + ".ini",
                                                 "content": content, "sha256": _hash(content.encode("utf-8")),
                                                 "automatically_enabled": False}
            module_paths[name] = output / relative
        shared = _shared_targets(core_files, modules, root)
        audit = {"publishable_v2": False, "native_validation": "pending",
                 "source_index": {"filename": original.name, "sha256": split_source["sha256"]},
                 "producer": copy.deepcopy(producer), "host": host, "extension_dir": extension_dir,
                 "core_evidence": copy.deepcopy(core_evidence),
                 "core": {"identity": core, "source": _source_record(core_asset), "files": core_files,
                          "archive": {"filename": core_name, "sha256": core_sha}},
                 "modules": modules, "configuration_defaults": defaults, "shared_targets": shared}
        # No metadata handoff exists until native evidence and existing finalizers pass.
        output.mkdir(exist_ok=False)
        for archive in sorted(bundle.rglob("*.7z")):
            target = output / archive.relative_to(bundle)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, target)
            if package_meta.sha256(target) != package_meta.sha256(archive):
                raise ValueError("Published diagnostic archive bytes changed")
        manifest = output / "php-repack-manifest.json"
        pending = output / ".php-repack-manifest.json.pending"
        pending.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(pending, manifest)
    return {"core_archive": output / core_name, "module_archives": module_paths, "manifest": manifest, "audit": audit}


def finalize_php_payloads(payload_dir, split_source, native_result, default_modules,
                          minimum_os, windows_compatibility, output_dir,
                          *, core_source_url, module_source_url, seven_zip=None):
    """Attach native evidence and produce final core/module schema-2 packages."""
    payload_dir = Path(payload_dir).absolute()
    split_path = Path(split_source["path"]).absolute()
    if split_path.is_symlink() or not split_path.is_file() or _hash(split_path.read_bytes()) != _sha(split_source["sha256"]):
        raise ValueError("PHP split source changed before finalization")
    split = json.loads(split_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    audit = json.loads((payload_dir / "php-repack-manifest.json").read_text(encoding="utf-8"))
    if (audit.get("publishable_v2") is not False or audit.get("native_validation") != "pending"
            or audit.get("source_index", {}).get("sha256") != split_source["sha256"]):
        raise ValueError("PHP source does not match the pending payload audit")
    if not isinstance(default_modules, list) or len(default_modules) != len(set(default_modules)) or any(
            name not in audit["modules"] for name in default_modules):
        raise ValueError("PHP default_modules must be distinct selected module names")
    native = json.loads(Path(native_result).read_text(encoding="utf-8-sig"))
    # Legacy branches pin imagick 3.4.3, which ships no IM_MOD_RL_* coders, so
    # PNG conversion needs an external ImageMagick. Require the round trip only
    # when the payload actually carries coders, and require it to be reported
    # as untested (false) otherwise instead of silently claiming success.
    imagick_files = audit["modules"].get("imagick", {}).get("files", [])
    imagick_coders = any(Path(row["target_path"]).name.upper().startswith("IM_MOD_RL_")
                         for row in imagick_files)
    if (not isinstance(native, dict) or set(native) != {"php", "extension_dir", "loaded", "modules", "imagick_png"}
            or native["php"] != audit["core"]["identity"]["version"]
            or native["extension_dir"] != audit["extension_dir"] or native["loaded"] != len(audit["modules"])
            or type(native["loaded"]) is not int or native["imagick_png"] is not imagick_coders
            or not isinstance(native["modules"], list)):
        raise ValueError("Incomplete PHP native evidence")
    loaded = {row.get("name"): row for row in native["modules"] if isinstance(row, dict)}
    if set(loaded) != set(audit["modules"]):
        raise ValueError("PHP native module set differs from the payload")
    original_producer = audit["producer"]
    producer = {"commit": original_producer["commit"], "run_id": str(original_producer["run_id"]),
                "run_attempt": original_producer["run_attempt"]}
    host = copy.deepcopy(audit["host"])
    assets = {row["filename"]: row for row in split["assets"]}
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("PHP final output must be a new directory")

    with tempfile.TemporaryDirectory(prefix="servbay-php-final-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        staged_output = temporary / "output"
        staged_output.mkdir()
        module_metas = []
        versions = {}
        recipes = {}
        for name, module in sorted(audit["modules"].items()):
            evidence = loaded[name]
            dlls = [row["target_path"] for row in module["files"]
                    if row["target_path"].startswith(audit["extension_dir"] + "/")
                    and row["target_path"].casefold().endswith(".dll")]
            if (len(dlls) != 1 or evidence.get("dll") != Path(dlls[0]).name
                    or evidence.get("exit_code") != 0 or evidence.get("loaded") is not True):
                raise ValueError("PHP native evidence differs for " + name)
            archive = payload_dir / module["archive"]["filename"]
            with tempfile.TemporaryDirectory(prefix="php-module-", dir=temporary) as module_temp:
                module_stage = Path(module_temp) / "payload"
                actual = windows_archive.extract_verified_archive(archive, module_stage, seven_zip)
                if actual != {row["target_path"]: row["sha256"] for row in module["files"]}:
                    raise ValueError("PHP module archive differs from its audit: " + name)
                candidate = module["configuration_candidate"]
                configs = [{"scope": "config", "install_path": candidate["etc_relative_path"].removeprefix(
                    audit["core"]["identity"]["name"] + "/" + audit["host"]["series"] + "/"),
                    "mode": "0644", "content": candidate["content"]}]
                manifest = module_manifest.create_manifest(
                    host, module_stage, configs, [name], [], arch=module["identity"]["arch"],
                    minimum_os=minimum_os, base=None, source_name=name,
                    extension_dir=audit["extension_dir"], os_name="windows",
                    windows_compatibility=windows_compatibility,
                )
                source_row = module["source"]
                source_asset = assets[source_row["filename"]]
                source = {"path": str(split_path.parent / source_asset["filename"]),
                          "version": module["identity"]["version"], "sha256": source_asset["sha256"],
                          "bytes": source_asset["bytes"], "url": module_source_url}
                result = module_manifest.write_package(
                    manifest, module_stage, module["identity"], producer, source,
                    staged_output / name, seven_zip=seven_zip,
                )
                module_metas.append(result["meta_path"])
            versions[name] = module["identity"]["version"]
            recipes[name] = {"kind": "php-extension", "name": name,
                             "default": name in default_modules}
        core_asset = next(row for row in split["assets"] if row["role"] == "core-runtime")
        core_source = {"path": str(split_path.parent / core_asset["filename"]),
                       "version": php_source.source_version(audit["core"]["identity"]["version"]),
                       "sha256": core_asset["sha256"], "bytes": core_asset["bytes"],
                       "url": core_source_url}
        profile = temporary / "modules.json"
        profile.write_text(json.dumps({
            "schema_version": 1, "os": "windows",
            "hosts": {"php": {audit["host"]["series"]: versions}},
            "recipes": recipes,
        }), encoding="utf-8")
        core_archive = payload_dir / audit["core"]["archive"]["filename"]
        candidate = core_manifest.create_candidate(
            core_archive, audit["core"]["identity"], producer, None, minimum_os,
            core_source, host, windows_compatibility=windows_compatibility,
        )
        core = core_manifest.finalize_core(
            candidate, core_archive, module_metas, [], producer, staged_output / "core",
            config=profile, seven_zip=seven_zip,
        )
        receipt = {
            "schema": "servbay.php-v2-finalization.v1",
            "php_version": audit["core"]["identity"]["version"],
            "modules": sorted(loaded), "module_count": len(loaded),
            "native_evidence_sha256": _hash(Path(native_result).read_bytes()),
            "windows_compatibility": copy.deepcopy(windows_compatibility),
            "minimum_os": minimum_os, "core": core["meta"],
        }
        (staged_output / "php-finalization.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(staged_output, output)
    return {"core": output / "core", "modules": {name: output / name for name in loaded},
            "finalization": output / "php-finalization.json"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("split-source", "core-identity", "module-identities", "core-evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seven-zip")
    args = parser.parse_args()
    try:
        inputs = {key: json.loads(getattr(args, key).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
                  for key in ("split_source", "core_identity", "module_identities", "core_evidence")}
        result = prepare_php_payloads(**inputs, output_dir=args.output_dir, seven_zip=args.seven_zip)
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as error:
        parser.exit(1, f"PHP payload preparation failed: {error}\n")
    print(json.dumps({"manifest": str(result["manifest"]), "publishable_v2": False, "native_validation": "pending"}))


if __name__ == "__main__":
    main()
