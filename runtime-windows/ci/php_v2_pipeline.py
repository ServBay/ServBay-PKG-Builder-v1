#!/usr/bin/env python3
"""Run the exact PHP 8.6.0RC4 split-artifact pipeline on native Windows x64."""

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "ServBay/PHP-Windows-Portable"
RUN_ID = 40272362001
ATTEMPT = 1
VERSION = "8.6.0RC4"
PRODUCER_COMMIT = "5f5af83f797ae89a7187ddca87492cf8064d68d7"
COLLECTOR_COMMIT = "b614c80d50c417f22a92a78be2540c750c7559f8"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command, *, env=None, strict_stderr=False):
    result = subprocess.run([str(item) for item in command], capture_output=True,
                            text=True, encoding="utf-8", errors="strict", timeout=300, env=env)
    # Never include the environment or GitHub command output in error reports.
    if result.returncode or (strict_stderr and result.stderr.strip()):
        detail = (result.stdout + "\n" + result.stderr) if Path(str(command[0])).name.lower() == "php.exe" else ""
        raise RuntimeError(f"{Path(str(command[0])).name} failed ({result.returncode})\n{detail}")
    return result.stdout


def php_run(php, arguments):
    env = dict(os.environ)
    env.update(PHPRC="", PHP_INI_SCAN_DIR="", PATH=str(php.parent) + os.pathsep + env.get("PATH", ""))
    return run([php, *arguments], env=env, strict_stderr=True)


def windows_machines():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.IsWow64Process2.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort),
                                     ctypes.POINTER(ctypes.c_ushort)]
    kernel.IsWow64Process2.restype = ctypes.c_int
    process, native = ctypes.c_ushort(), ctypes.c_ushort()
    if not kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
        raise OSError(ctypes.get_last_error(), "IsWow64Process2 failed")
    return process.value, native.value


def require_native_x64():
    if platform.system() != "Windows" or struct.calcsize("P") != 8:
        raise ValueError("Native Windows x64 Python is required")
    process, native = windows_machines()
    if process != 0 or native != 0x8664:
        raise ValueError("Native AMD64 is required; emulation is not accepted")
    return {"system": "Windows", "native_machine": "AMD64", "process_machine": process,
            "platform": platform.platform()}


def parse_abi(info):
    def one(label):
        values = [line.split("=>", 1)[1].strip() for line in info.splitlines()
                  if line.startswith(label + " =>")]
        # phpinfo prints PHP Version in both the general and Core sections.
        if not values or len(set(values)) != 1:
            raise ValueError("Missing or conflicting actual PHP field: " + label)
        return values[0]
    if (one("PHP Version") != VERSION or one("Architecture") != "x64" or one("Thread Safety") != "disabled"
            or one("Debug Build") != "no"):
        raise ValueError("Actual PHP version/thread/debug differs from RC4 NTS release")
    module_api, zend_api = int(one("PHP API")), int(one("Zend Extension"))
    extension, zend = one("PHP Extension Build"), one("Zend Extension Build")
    if extension != f"API{module_api},NTS,VS18" or zend != f"API{zend_api},NTS,VS18":
        raise ValueError("Actual PHP ABI is not the requested NTS VS18 build")
    return {"module_api": module_api, "zend_extension_api": zend_api,
            "extension_build": extension, "zend_extension_build": zend, "zts": False, "debug": False}


def identities(selected, abi, module_revision):
    if type(module_revision) is not int or module_revision < 1:
        raise ValueError("An explicit positive module_revision is required; revision 0 is occupied")
    core = {"runtime_version": "v2", "os": "windows", "kind": "package", "name": "php",
            "version": VERSION, "arch": "x64", "build_revision": 0}
    modules = [{**core, "kind": "php-extension", "name": row["name"],
                "version": row["ref"][1:] if row["ref"].startswith("v") else row["ref"],
                "build_revision": module_revision, "host": {"type": "php", "series": "8.6", "abi": dict(abi)}}
               for row in selected]
    return core, modules


def artifact_names(selected):
    return ["php-build-artifacts-x64-nts"] + [f"extension-{row['name']}-{VERSION}-x64-nts" for row in selected]


def validate_upstream(upstream, artifacts, selected):
    expected = {"id": RUN_ID, "run_attempt": ATTEMPT, "head_sha": PRODUCER_COMMIT,
                "status": "completed", "conclusion": "success", "path": ".github/workflows/build.yml",
                "event": "workflow_dispatch"}
    if (any(upstream.get(key) != value for key, value in expected.items())
            or upstream.get("repository", {}).get("full_name") != REPOSITORY):
        raise ValueError("Upstream run/attempt/producer/status differs from the exact approved build")
    names = artifact_names(selected)
    for name in names:
        matches = [row for row in artifacts if row.get("name") == name]
        if len(matches) != 1:
            raise ValueError("Missing or duplicate original artifact: " + name)
        row = matches[0]
        if (row.get("expired") is not False or row.get("size_in_bytes", 0) <= 0
                or row.get("workflow_run", {}).get("id") != RUN_ID
                or row.get("workflow_run", {}).get("head_sha") != PRODUCER_COMMIT):
            raise ValueError("Original artifact has expired or differs from its producer: " + name)
    return names


def upstream_inputs(selected, destination):
    def api(suffix, paginate=False):
        command = ["gh", "api"] + (["--paginate", "--slurp"] if paginate else [])
        return json.loads(run(command + [f"repos/{REPOSITORY}/actions/runs/{RUN_ID}" + suffix]))
    latest = api("")
    attempt = api(f"/attempts/{ATTEMPT}")
    artifacts = [row for page in api("/artifacts?per_page=100", True) for row in page["artifacts"]]
    names = validate_upstream(latest, artifacts, selected)
    validate_upstream(attempt, artifacts, selected)
    for name in names:
        target = destination / ("core" if name == names[0] else "extensions/" + name)
        run(["gh", "run", "download", str(RUN_ID), "--repo", REPOSITORY,
             "--name", name, "--dir", target])
    # A rerun during download invalidates the artifact binding as well.
    validate_upstream(api(""), artifacts, selected)
    return {"run": attempt, "artifacts": [row for row in artifacts if row["name"] in names]}


def native_test(core_root, audit):
    php = core_root / "bin/php.exe"
    abi = parse_abi(php_run(php, ["-n", "-i"]))
    if "host" in audit and abi != audit["host"]["abi"]:
        raise ValueError("Native PHP ABI differs from the frozen original core")
    extension = audit["extension_dir"]
    results = []
    for name, module in sorted(audit["modules"].items()):
        dlls = [Path(row["target_path"]).name for row in module["files"]
                if row["target_path"].startswith(extension + "/") and row["target_path"].lower().endswith(".dll")]
        if len(dlls) != 1:
            raise ValueError("Exactly one PHP module DLL is required: " + name)
        directive = "zend_extension" if name == "xdebug" else "extension"
        code = ('echo json_encode(["name"=>' + json.dumps(name) + ',"loaded"=>extension_loaded('
                + json.dumps(name) + '),"version"=>phpversion(' + json.dumps(name) + ')]);')
        args = ["-n", "-d", "extension_dir=" + str(core_root / extension), "-d", directive + "=" + dlls[0]]
        try:
            result = json.loads(php_run(php, args + ["-r", code]))
        except json.JSONDecodeError as error:
            raise ValueError("Native module returned invalid JSON: " + name) from error
        if (result.get("name") != name or result.get("loaded") is not True
                or not isinstance(result.get("version"), str) or not result["version"]):
            raise ValueError("Native module load failed: " + name)
        results.append({"name": name, "dll": dlls[0], "exit_code": 0, "loaded": True,
                        "actual_version": result["version"]})
    coders = any(Path(row["target_path"]).name.upper().startswith("IM_MOD_RL_")
                 for row in audit["modules"].get("imagick", {}).get("files", []))
    if coders:
        imagick = next(row for row in results if row["name"] == "imagick")
        value = php_run(php, ["-n", "-d", "extension_dir=" + str(core_root / extension),
                             "-d", "extension=" + imagick["dll"], "-r",
                             '$i=new Imagick(); echo json_encode(count($i->queryFormats("PNG"))>0);'])
        if value.strip() != "true":
            raise ValueError("Imagick native PNG query failed")
    return {"php": VERSION, "extension_dir": extension, "loaded": len(results),
            "modules": results, "imagick_png": coders}


def extract_host(repacker, audit, archives, destination, seven_zip):
    """Extract each archive independently and merge only identical shared files."""
    destination.mkdir()
    core_root = destination / "host/php/8.6"
    for name, archive in archives.items():
        owner = audit["core"] if name == "core" else audit["modules"][name]
        actual = repacker.windows_archive.extract_verified_archive(archive, destination / name, seven_zip)
        expected = {row["target_path"]: row["sha256"] for row in owner["files"]}
        if actual != expected:
            raise ValueError("Archive file inventory differs from prepared payload: " + name)
        for relative, digest in actual.items():
            target = destination / "host" / relative if name == "core" else core_root / relative
            if target.exists():
                if sha(target) != digest:
                    raise ValueError("Conflicting extracted host file: " + relative)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(destination / name / relative, target)
    return core_root


def export_verified(output, build):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("Output must be a new directory with an existing parent")
    with tempfile.TemporaryDirectory(prefix="servbay-php-rc4-", dir=output.parent) as temporary:
        work = Path(temporary)
        build(work)
        # build() writes this only after both native runs pass. Failure exposes no metadata.
        exported = work / "export"
        if not (exported / "formal.zip").is_file() or not (exported / "native-evidence.json").is_file():
            raise ValueError("Verified formal export is incomplete")
        if output.exists() or output.is_symlink():
            raise ValueError("Output appeared during the pipeline")
        os.rename(exported, output)


def pipeline(portable, producer, output, module_revision, seven_zip):
    machine = require_native_x64()
    # Read the producer configuration from its immutable Git object, not a working copy.
    selected = json.loads(run(["git", "-C", producer, "show", PRODUCER_COMMIT + ":extensions.php8.6.json"]))
    if len(selected) != 16 or len({row["name"] for row in selected}) != 16:
        raise ValueError("The exact producer must select all 16 modules")
    source = Path(portable) / "scripts/collect_split_assets.py"
    expected = run(["git", "-C", portable, "show", COLLECTOR_COMMIT + ":scripts/collect_split_assets.py"])
    if source.read_text(encoding="utf-8") != expected:
        raise ValueError("Collector working file differs from its pinned reviewed commit")
    collector = load("php_rc4_collector", source)
    repacker = load("php_rc4_repacker", ROOT / "runtime-windows/repack_php_v2.py")
    context = {"producer": {"repository": REPOSITORY, "commit": PRODUCER_COMMIT,
                            "run_id": RUN_ID, "run_attempt": ATTEMPT},
               "display_version": VERSION, "source_ref": VERSION, "build_version": VERSION,
               "build_branch": "", "arch": "x64", "thread_safe": "nts",
               "core_runtime_filename": f"php-{VERSION}-nts-Win32-vs18-x64.zip",
               "core_devel_filename": f"php-devel-pack-{VERSION}-nts-Win32-vs18-x64.zip"}
    collector._validate_inputs(context, selected)
    identities(selected, {}, module_revision)

    def build(work):
        upstream = upstream_inputs(selected, work / "inputs")
        split = work / "split"
        index = collector.collect_split_assets(work / "inputs/core", work / "inputs/extensions", selected, context, split)
        build_payloads(repacker, split, index, selected, work, module_revision, seven_zip, machine, upstream)
    export_verified(output, build)


def build_payloads(repacker, split, index, selected, work, module_revision, seven_zip, machine, upstream):
    split_source = {"path": str(split / "split-assets.json"), "sha256": sha(split / "split-assets.json")}
    core_asset = next(row for row in index["assets"] if row["role"] == "core-runtime")
    raw = work / "raw-core"
    raw.mkdir()
    repacker._zip_inventory(split / core_asset["filename"], core_asset, raw)
    abi_info = php_run(raw / "php.exe", ["-n", "-i"])
    abi = parse_abi(abi_info)
    core, modules = identities(selected, abi, module_revision)
    evidence = {"source_sha256": core_asset["sha256"], "version": VERSION, "abi": abi}
    prepared = repacker.prepare_php_payloads(split_source, core, modules, evidence,
                                            work / "prepared", seven_zip=seven_zip)
    audit = prepared["audit"]
    archives = {"core": prepared["core_archive"], **prepared["module_archives"]}
    host = extract_host(repacker, audit, archives, work / "prepared-native", seven_zip)
    native = native_test(host, audit)
    native_path = work / "native.json"
    native_path.write_text(json.dumps(native), encoding="utf-8")
    formal = work / "formal"
    repacker.finalize_php_payloads(work / "prepared", split_source, native_path,
                                  [row["name"] for row in selected], "10.0",
                                  {"distribution": "windows-x64", "toolchain": "msvc-vs18", "crt": "vc14-runtime"},
                                  formal, core_source_url="https://windows.php.net/downloads/releases/",
                                  module_source_url="https://github.com/ServBay/PHP-Windows-Portable",
                                  seven_zip=seven_zip)
    formal_archives = {}
    for name in ["core"] + [row["name"] for row in selected]:
        meta = json.loads((formal / name / "meta.json").read_text(encoding="utf-8"))
        archive = formal / name / meta["filename"]
        if sha(archive) != meta["sha256"]:
            raise ValueError("Formal archive SHA differs from metadata: " + name)
        formal_archives[name] = archive
    host = extract_host(repacker, audit, formal_archives, work / "formal-native", seven_zip)
    formal_native = native_test(host, audit)
    exported = work / "export"
    exported.mkdir()
    receipt = {"schema": "servbay.php-rc4-native-pipeline.v1", "producer": index["build_context"]["producer"],
               "collector_commit": COLLECTOR_COMMIT, "machine": machine, "core_identity": core,
               "module_identities": modules, "split_index_sha256": split_source["sha256"],
               "raw_core_phpinfo": abi_info, "abi": abi, "prepared_native": native,
               "formal_native": formal_native, "upstream_artifacts": upstream["artifacts"],
               "formal_archive_sha256": {name: sha(path) for name, path in formal_archives.items()}}
    (exported / "native-evidence.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(split / "split-assets.json", exported / "split-assets.json")
    with zipfile.ZipFile(exported / "formal.zip", "x", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(formal.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(formal).as_posix())
    # Verify every exported file and CRC before exposing the formal ZIP.
    with zipfile.ZipFile(exported / "formal.zip") as archive:
        if archive.testzip() is not None:
            raise ValueError("Formal ZIP CRC verification failed")
    receipt["formal_zip_sha256"] = sha(exported / "formal.zip")
    (exported / "native-evidence.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portable-source", type=Path, required=True)
    parser.add_argument("--producer-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--module-revision", type=int, required=True)
    parser.add_argument("--seven-zip", required=True)
    args = parser.parse_args()
    pipeline(args.portable_source.absolute(), args.producer_source.absolute(), args.output_dir,
             args.module_revision, args.seven_zip)
    print(f"Verified {VERSION} native x64 core and all 16 modules: {args.output_dir}")


if __name__ == "__main__":
    main()
