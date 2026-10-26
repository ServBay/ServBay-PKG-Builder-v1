"""Download, exercise and finalize a pinned EDB Windows PostgreSQL core."""

import argparse
import base64
import ctypes
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ENV = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH", "COMSPEC", "PATHEXT"}
STAGES = {"arguments", "work_access", "prepare", "extract", "download", "native", "finalize", "export"}


class ProbeFailure(RuntimeError):
    def __init__(self, probe, exit_code):
        self.probe = probe if re.fullmatch(r"[a-z_ -]{1,40}", probe) else "subprocess"
        self.exit_code = exit_code if type(exit_code) is int else "unavailable"
        super().__init__(f"probe={self.probe} exit={self.exit_code}")


def failure_summary(error):
    stage = getattr(error, "pipeline_stage", "arguments")
    stage = stage if stage in STAGES else "arguments"
    detail = str(error) if isinstance(error, ProbeFailure) else "error_type=" + type(error).__name__
    return f"PostgreSQL producer failed stage={stage} {detail}; formal output was not exported."


def load_repacker():
    spec = importlib.util.spec_from_file_location("postgresql_pipeline_repacker",
                                               ROOT / "runtime-windows/repack_postgresql_v2.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command, *, cwd=None, capture_output=True):
    environment = {key.upper(): value for key, value in os.environ.items() if key.upper() in RUNTIME_ENV}
    try:
        return subprocess.run([str(item) for item in command], env=environment,
                              stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                              stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                              text=True, encoding="utf-8", errors="replace", timeout=300, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Subprocess execution failed") from None


@contextmanager
def runtime_environment():
    previous = dict(os.environ)
    environment = {key.upper(): value for key, value in previous.items() if key.upper() in RUNTIME_ENV}
    try:
        os.environ.clear()
        os.environ.update(environment)
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def require_ok(result, label):
    if result.returncode != 0:
        raise ProbeFailure(label, result.returncode)
    return result.stdout.strip() if result.stdout is not None else ""


def powershell(script, runner=run):
    executable = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(("$ErrorActionPreference='Stop'; " + script).encode("utf-16-le")).decode("ascii")
    return require_ok(runner([executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]),
                      "Windows observation")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def token_user_sid():
    return powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value")


def _work_access(path, sid):
    script = ("$sid=([Security.Principal.SecurityIdentifier]" + ps_literal(sid) + ").Value; "
              "$acl=Get-Acl -LiteralPath " + ps_literal(path) + "; "
              "$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])); "
              "$ready=@($rules | Where-Object {$_.IdentityReference.Value -eq $sid -and "
              "$_.AccessControlType -eq 'Allow' -and "
              "($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq "
              "[Security.AccessControl.FileSystemRights]::FullControl -and "
              "($_.InheritanceFlags -band 3) -eq 3 -and $_.PropagationFlags -eq 0}).Count -gt 0; "
              "$others=@($rules | Where-Object {$_.IdentityReference.Value -ne $sid} | ForEach-Object { "
              "'{0}|{1}|{2}|{3}|{4}|{5}' -f $_.IdentityReference.Value,[int]$_.FileSystemRights,"
              "[int]$_.InheritanceFlags,[int]$_.PropagationFlags,$_.IsInherited,$_.AccessControlType} | Sort-Object); "
              "@{owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value; "
              "others=$others; ready=$ready; protected=$acl.AreAccessRulesProtected} "
              "| ConvertTo-Json -Compress -Depth 4")
    return json.loads(powershell(script))


def prepare_work_directory(work, *, runner=run):
    """Grant only this new empty tree to TokenUser before any payload exists."""
    work = Path(work)
    if work.is_symlink() or not work.is_dir() or any(work.iterdir()):
        raise ValueError("Work access requires the newly created empty directory")
    sid = token_user_sid()
    if not isinstance(sid, str) or not re.fullmatch(r"S-1-(?:[0-9]+-){1,14}[0-9]+", sid):
        raise ValueError("Actual TokenUser SID is invalid")
    before = _work_access(work, sid)
    icacls = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/icacls.exe"
    require_ok(runner([icacls, str(work), "/grant", "*" + sid + ":(OI)(CI)F"]), "work access grant")
    after = _work_access(work, sid)
    if (after["ready"] is not True or before["owner"] != after["owner"]
            or before["others"] != after["others"] or before["protected"] != after["protected"]):
        raise ValueError("Work access was not verified or existing security changed")
    return {"python_version": sys.version.split()[0], "work_access_ready": after["ready"],
            "owner_matches_user": after["owner"] == sid, "owner_preserved": before["owner"] == after["owner"]}


def machine_evidence(runner=run):
    if os.name != "nt":
        raise ValueError("Windows is required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.IsWow64Process2.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort),
                                     ctypes.POINTER(ctypes.c_ushort)]
    kernel.IsWow64Process2.restype = ctypes.c_int
    process, native = ctypes.c_ushort(), ctypes.c_ushort()
    if not kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
        raise RuntimeError("IsWow64Process2 observation failed")
    observed = json.loads(powershell(
        "$cpu=@(Get-CimInstance Win32_Processor); $os=Get-CimInstance Win32_OperatingSystem; "
        "$architectures=@($cpu.Architecture | Sort-Object -Unique); "
        "if($cpu.Count -lt 1 -or $architectures.Count -ne 1){throw 'CPU inventory ambiguous'}; "
        "@{processor_name=($cpu.Name -join '; '); processor_architecture_code=[int]$architectures[0]; "
        "os_version=$os.Version} | ConvertTo-Json -Compress", runner))
    machines = {0x8664: "X64", 0xAA64: "ARM64", 0x14C: "X86"}
    observed.update(os_architecture=machines.get(native.value, "unsupported"),
                    process_architecture=machines.get(process.value or native.value, "unsupported"),
                    process_environment_architecture=os.environ.get("PROCESSOR_ARCHITECTURE"),
                    process_machine=process.value, native_machine=native.value)
    return observed


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def cleanup_observation(root, port, runner=run):
    # A failed CIM/Get-NetTCPConnection observation must never become a zero.
    script = ("$root=" + ps_literal(str(root.resolve()) + os.sep) + "; "
              "$processes=@(Get-CimInstance Win32_Process | Where-Object { "
              "$_.ExecutablePath -and $_.ExecutablePath.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) }); "
              "$listeners=@(Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -eq " + str(port) + "}); "
              "@{processes=$processes.Count; listeners=$listeners.Count} | ConvertTo-Json -Compress")
    observed = json.loads(powershell(script, runner))
    counts = observed["processes"], observed["listeners"]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("Invalid cleanup observation")
    return counts


def locate_dumpbin(runner=run):
    drive = os.environ.get("SYSTEMDRIVE", "C:")
    vswhere = Path(drive + "/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe")
    directory = require_ok(runner([vswhere, "-latest", "-products", "*", "-requires",
                                  "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                                  "installationPath"]), "Visual Studio discovery")
    if not directory or "\n" in directory:
        raise ValueError("Visual Studio discovery is ambiguous")
    tools = list((Path(directory) / "VC/Tools/MSVC").glob("*/bin/Hostx64/x64/dumpbin.exe"))
    if not tools:
        raise ValueError("Installed dumpbin is required")
    return sorted(tools, key=lambda path: tuple(int(item) for item in path.parts[-5].split(".")))[-1]


def crt_evidence(root, *, runner=run, dumpbin=None):
    dumpbin = dumpbin or locate_dumpbin(runner)
    files, dependencies = [], set()
    payload = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in (".exe", ".dll"))
    if not payload:
        raise ValueError("No PE payload for CRT observation")
    for path in payload:
        text = require_ok(runner([dumpbin, "/NOLOGO", "/IMPORTS", str(path)]), "CRT import observation")
        if "File Type:" not in text or "Summary" not in text:
            raise ValueError("Incomplete dumpbin import observation")
        imports = sorted(set(re.findall(r"(?im)^\s*([a-z0-9_.+-]+\.dll)\s*$", text.lower())))
        crt = [name for name in imports if re.match(r"(?:msvcr|msvcp|vcruntime|ucrtbase|concrt|api-ms-win-crt-)", name)]
        dependencies.update(crt)
        files.append({"path": path.relative_to(root).as_posix(), "imports": imports,
                      "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    actual = sorted(dependencies)
    return {"crt": "imports: " + ", ".join(actual) if actual else "no CRT DLL imports (all EXE/DLL dumpbin /IMPORTS)",
            "dependencies": actual, "files": files, "method": "dumpbin /IMPORTS (normal and delay-load)"}


def source_url(version, revision):
    if (not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", version)
            or int(version.split(".")[0]) < 10 or type(revision) is not int or revision < 1):
        raise ValueError("Stable PostgreSQL >=10 and positive EDB revision are required")
    return f"https://get.enterprisedb.com/postgresql/postgresql-{version}-{revision}-windows-x64-binaries.zip"


def download_source(version, revision, destination):
    url = source_url(version, revision)
    digest, size = hashlib.sha256(), 0
    with urlopen(url, timeout=120) as response, Path(destination).open("xb") as output:
        # Do not accept a redirect to a different endpoint or credential-bearing URL.
        if response.geturl() != url:
            raise ValueError("Unexpected upstream download redirect")
        for block in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
            output.write(block)
    if size == 0:
        raise ValueError("Empty PostgreSQL source")
    return {"path": str(destination), "version": version, "url": url,
            "sha256": digest.hexdigest(), "bytes": size}


def collect_native(root, archive, version, *, runner=run):
    def native_run(command, *, capture_output=True):
        return runner(command, cwd=root / "bin", capture_output=capture_output)

    machine = machine_evidence(runner)
    if (machine["os_architecture"] != "X64" or machine["process_architecture"] != "X64"
            or machine["process_environment_architecture"] != "AMD64" or machine["process_machine"] != 0
            or machine["native_machine"] != 0x8664 or machine["processor_architecture_code"] != 9):
        raise ValueError("Native Windows AMD64 is required; emulation is not accepted")
    pgconfig, flags = {}, {}
    for key in ("version", "bindir", "sharedir", "pkglibdir", "includedir-server", "cc", "configure", "cflags", "ldflags"):
        actual = require_ok(native_run([root / "bin/pg_config.exe", "--" + key]), "pg_config " + key)
        (pgconfig if key in ("version", "bindir", "sharedir", "pkglibdir", "includedir-server") else flags)[key.replace("-", "_")] = actual
    if pgconfig["version"] != "PostgreSQL " + version:
        raise ValueError("pg_config version differs")
    for key, relative in (("bindir", "bin"), ("sharedir", "share"), ("pkglibdir", "lib"), ("includedir_server", "include/server")):
        if os.path.normcase(str(Path(pgconfig[key]).resolve())) != os.path.normcase(str((root / relative).resolve())):
            raise ValueError("pg_config path differs: " + key)
    executables = {}
    for name in ("postgres", "initdb", "pg_ctl", "psql"):
        actual = require_ok(native_run([root / ("bin/" + name + ".exe"), "--version"]), name + " version")
        if actual != f"{name} (PostgreSQL) {version}":
            raise ValueError("Executable version differs: " + name)
        executables[name] = actual
    crt = crt_evidence(root, runner=runner)
    data, port = root / "data", free_port()
    if data.exists():
        raise ValueError("Native database directory already exists")
    init = native_run([root / "bin/initdb.exe", "-D", data, "-U", "servbay_probe", "--encoding=UTF8", "--locale=C", "--auth=trust"])
    require_ok(init, "initdb")
    initialized = (data / "PG_VERSION").read_text(encoding="utf-8").strip()
    if initialized != version.split(".")[0]:
        raise ValueError("Initialized major differs")
    start = query = stop = None
    sql = None
    try:
        start = native_run([root / "bin/pg_ctl.exe", "-D", data, "-l", root.parent.parent / "postgresql-startup.log",
                        "-o", f"-h 127.0.0.1 -p {port}", "-w", "-t", "60", "start"], capture_output=False)
        require_ok(start, "startup")
        statement = ("SELECT json_build_object('server_version',current_setting('server_version'),"
                     "'server_encoding',current_setting('server_encoding'),'data_directory',"
                     "current_setting('data_directory'),'compiler',version())::text;")
        query = native_run([root / "bin/psql.exe", "-X", "-A", "-t", "-w", "-h", "127.0.0.1", "-p", str(port),
                        "-U", "servbay_probe", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", statement])
        sql = json.loads(require_ok(query, "query"))
    finally:
        stop = native_run([root / "bin/pg_ctl.exe", "-D", data, "-w", "-t", "60", "-m", "fast", "stop"])
        remaining, listeners = cleanup_observation(root, port, runner)
        require_ok(stop, "stop")
        if remaining != 0 or listeners != 0:
            raise ValueError("PostgreSQL cleanup left processes or listeners")
    if (sql["server_version"] != version or sql["server_encoding"] != "UTF8"
            or os.path.normcase(str(Path(sql["data_directory"]).resolve())) != os.path.normcase(str(data.resolve()))):
        raise ValueError("Database version/encoding/path differs")
    compilers = re.findall(r"Visual C\+\+ build [0-9]+", sql["compiler"])
    if len(compilers) != 1:
        raise ValueError("Actual SQL compiler identity is required")
    native = {"schema": "servbay.windows-postgresql-core-native.v1", "archive": archive,
              **{key: machine[key] for key in ("os_architecture", "process_architecture", "processor_name",
                                               "processor_architecture_code", "process_environment_architecture")},
              "pg_config": pgconfig, "executables": executables,
              "database": {"initialized_pg_version": initialized, "server_version": sql["server_version"],
                           "server_encoding": sql["server_encoding"], "data_directory": sql["data_directory"],
                           "startup_exit": start.returncode, "query_exit": query.returncode,
                           "stop_exit": stop.returncode, "remaining_processes": remaining, "listeners": listeners}}
    sidecar = {"machine": machine, "sql_version": sql["compiler"], "pg_config_build": flags, "crt": crt,
               "initdb_exit": init.returncode, "loopback_port": port}
    compatibility = {"distribution": "windows-x64", "toolchain": compilers[0], "crt": crt["crt"]}
    return native, sidecar, compatibility


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def pipeline(version, edb_revision, producer, output, seven_zip, *, build_revision=0, minimum_os="10.0"):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("Output must be a new directory with an existing parent")
    source_url(version, edb_revision)
    repacker = load_repacker()
    producer = repacker.package_meta._state.validate_producer(producer)
    identity = repacker._identity({"runtime_version": "v2", "os": "windows", "kind": "package",
                                   "name": "postgresql", "version": version, "arch": "x64", "build_revision": build_revision})
    current_stage = "arguments"

    def progress(name):
        nonlocal current_stage
        current_stage = name
        print("PostgreSQL producer stage=" + name, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="servbay-pg-producer-", dir=output.parent) as temporary:
            work = Path(temporary)
            progress("work_access")
            runtime = prepare_work_directory(work)
            print("PostgreSQL runtime python_version=" + runtime["python_version"]
                  + " work_access_ready=" + str(runtime["work_access_ready"]).lower()
                  + " owner_matches_user=" + str(runtime["owner_matches_user"]).lower()
                  + " owner_preserved=" + str(runtime["owner_preserved"]).lower(), flush=True)
            progress("download")
            filename = f"postgresql-{version}-{edb_revision}-windows-x64-binaries.zip"
            source = download_source(version, edb_revision, work / filename)
            # The repacker launches 7-Zip internally; apply the same allowlist to it too.
            with runtime_environment():
                progress("prepare")
                audit = repacker.prepare_postgresql_core(source, identity, work / "prepared", seven_zip=seven_zip)
                package = work / "prepared" / audit["archive"]["filename"]
                extracted = work / "native"
                extracted.mkdir()
                expected = {row["target_path"]: row["sha256"] for row in audit["files"]}
                # This guarded helper lists, extracts, and hashes the identical prepared 7z.
                progress("extract")
                repacker._verify_archive(package, expected, seven_zip, extracted)
            root = extracted / "verified/postgresql" / version.split(".")[0]
            progress("native")
            native, sidecar, compatibility = collect_native(root, audit["archive"], version)
            native_path = work / "native-evidence.json"
            write_json(native_path, native)
            with runtime_environment():
                progress("finalize")
                if (Path(source["path"]).stat().st_size != source["bytes"]
                        or repacker.package_meta.sha256(source["path"]) != source["sha256"]):
                    raise ValueError("Downloaded source changed before finalization")
                repacker.finalize_postgresql_core(package, identity, source["path"], source["url"], native_path,
                                                  producer, minimum_os, compatibility, work / "export", seven_zip=seven_zip)
            export = work / "export"
            progress("export")
            write_json(export / "native-evidence.json", native)
            write_json(export / "source-evidence.json", {
                "source": {key: value for key, value in source.items() if key != "path"},
                "edb_revision": edb_revision, "producer": producer,
                "minimum_os_policy": minimum_os, "runtime": runtime, **sidecar})
            (export / "postgresql-repack-manifest.json").write_bytes(
                (work / "prepared/postgresql-repack-manifest.json").read_bytes())
            if output.exists() or output.is_symlink():
                raise ValueError("Output appeared during pipeline")
            export.rename(output)
        return output
    except Exception as error:
        error.pipeline_stage = current_stage
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--edb-revision", type=int, required=True)
    parser.add_argument("--build-revision", type=int, default=0)
    parser.add_argument("--minimum-os", required=True)
    parser.add_argument("--producer", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--run-id")
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seven-zip", required=True)
    args = parser.parse_args()
    try:
        if args.producer and any((args.commit, args.run_id, args.run_attempt)):
            raise ValueError("Producer JSON and explicit producer fields are mutually exclusive")
        producer = json.loads(args.producer.read_text(encoding="utf-8-sig")) if args.producer else {
            "commit": args.commit, "run_id": args.run_id, "run_attempt": args.run_attempt}
        pipeline(args.version, args.edb_revision, producer, args.output, args.seven_zip,
                 build_revision=args.build_revision, minimum_os=args.minimum_os)
    except Exception as error:
        # Never expose third-party output, traceback locals, environment or arbitrary input.
        parser.exit(1, failure_summary(error) + "\n")
    print("PostgreSQL formal output exported")


if __name__ == "__main__":
    main()
