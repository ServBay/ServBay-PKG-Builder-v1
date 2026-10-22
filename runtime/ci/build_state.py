#!/usr/bin/env python3
"""Persistent public build state, independent of release registration."""

import argparse
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


KINDS = ("package", "php-extension", "postgresql-extension")
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _text(value, field):
    if not isinstance(value, str) or not SAFE.fullmatch(value):
        raise ValueError(f"Invalid {field}")
    return value


def clean_record(record):
    """Copy only public scheduling fields, never arbitrary input metadata."""
    if record.get("runtime_version") not in ("v1", "v2"):
        raise ValueError("Invalid runtime_version")
    if record.get("os") != "macos" or record.get("arch") not in ("x86_64", "arm64"):
        raise ValueError("Invalid macOS build platform")
    if record.get("kind") not in KINDS:
        raise ValueError("Invalid kind")
    revision = record.get("build_revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid build_revision")
    result = {key: record[key] for key in ("runtime_version", "os", "kind", "arch")}
    result.update(name=_text(record.get("name"), "name"),
                  version=_text(record.get("version"), "version"), build_revision=revision)
    if "build_type" in record:
        if record["build_type"] not in ("source", "prebuilt", "mixed"):
            raise ValueError("Invalid build_type")
        result["build_type"] = record["build_type"]
    if "recipe_name" in record:
        result["recipe_name"] = _text(record["recipe_name"], "recipe_name")
    if record["kind"] != "package":
        host = record.get("host", {})
        expected = "php" if record["kind"] == "php-extension" else "postgresql"
        if host.get("type") != expected or not isinstance(host.get("abi"), dict) or not host["abi"]:
            raise ValueError("A module needs its actual host type, series and ABI")
        abi = host["abi"]
        if any(not isinstance(key, str) or not key or type(value) not in (str, int, bool)
               for key, value in abi.items()):
            raise ValueError("Invalid host ABI")
        result["host"] = {"type": expected, "series": _text(host.get("series"), "host series"),
                          "abi": copy.deepcopy(abi)}
    return result


def identity(record):
    """Return the canonical compatibility identity, not build provenance."""
    result = clean_record(record)
    result.pop("build_type", None)
    result.pop("recipe_name", None)
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def artifact_name(record):
    row = clean_record(record)
    if row["runtime_version"] != "v2":
        raise ValueError("The pkg2 artifact protocol requires runtime v2")
    name = f"pkg2-macos-{row['arch']}-{row['kind']}-{row['name']}-{row['version']}"
    if "host" in row:
        name += f"-{row['host']['series']}"
    if row["build_revision"]:
        name += f"-r{row['build_revision']}"
    return name


def validate_producer(producer):
    if not isinstance(producer, dict) or set(producer) != {"commit", "run_id", "run_attempt"}:
        raise ValueError("A producer needs commit, run_id and run_attempt")
    if not isinstance(producer["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", producer["commit"]):
        raise ValueError("Invalid producer commit")
    if not isinstance(producer["run_id"], str) or not re.fullmatch(r"[1-9][0-9]*", producer["run_id"]):
        raise ValueError("Invalid producer run_id")
    if type(producer["run_attempt"]) is not int or producer["run_attempt"] < 1:
        raise ValueError("Invalid producer run_attempt")
    return copy.deepcopy(producer)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timezone missing")
        return parsed
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Invalid reservation timestamp") from error


def _validate_reservation(reservation):
    if not isinstance(reservation, dict) or set(reservation) != {"producer", "claimed_at", "expires_at"}:
        raise ValueError("Invalid build reservation")
    validate_producer(reservation["producer"])
    if _timestamp(reservation["expires_at"]) <= _timestamp(reservation["claimed_at"]):
        raise ValueError("A reservation needs a positive TTL")


def unit_job_name(record):
    return "Build v2 " + hashlib.sha256(identity(record).encode()).hexdigest()


def reserve_records(state, records, producer, now=None, ttl_seconds=8 * 3600, max_groups=None):
    """Atomically select unpaid units; a child may consume its own parent's lease."""
    producer = validate_producer(producer)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise ValueError("A reservation needs an explicit UTC clock and positive TTL")
    rows = [clean_record(record) for record in records]
    if any(row["runtime_version"] != "v2" for row in rows):
        raise ValueError("Build reservations cannot change the v1 workflow")
    updated = plan_pending(state, rows)["state"]
    eligible, held, seen = [], [], set()
    for row in rows:
        key = identity(row)
        if key in seen:
            continue
        seen.add(key)
        unit = updated["units"][key]
        if unit["status"] == "build-ready" or unit["consecutive_failures"] >= 3:
            continue
        reservation = unit.get("reservation")
        if reservation and reservation["producer"] != producer:
            held.append(key)
        else:
            eligible.append(row)
    deferred = []
    if max_groups is not None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("reservation_profile", Path(__file__).with_name("build_profile.py"))
        profile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(profile)
        grouped = profile.workflow_plan(eligible, max_groups)
        eligible, deferred = grouped["records"], grouped["deferred"]
    for row in eligible:
        unit = updated["units"][identity(row)]
        if "reservation" not in unit:
            unit["reservation"] = {"producer": copy.deepcopy(producer),
                                   "claimed_at": now.isoformat(timespec="seconds"),
                                   "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")}
    return {"state": updated, "include": eligible, "held": held, "deferred": deferred}


def persist_reservations(records, producer, read_state, write_state, now=None, **options):
    return _persist(lambda state: reserve_records(state, records, producer, now=now, **options), read_state, write_state)


def _named_jobs(jobs, name):
    return [job for job in jobs if isinstance(job.get("name"), str)
            and (job["name"] == name or job["name"].endswith(" / " + name))]


def _job_producer_matches(job, producer):
    # The verified attempt-specific API endpoint anchors omitted per-job fields.
    return (job.get("head_sha") == producer["commit"]
            and ("run_id" not in job or str(job["run_id"]) == producer["run_id"])
            and ("run_attempt" not in job or (type(job["run_attempt"]) is int
                                             and job["run_attempt"] == producer["run_attempt"])))


def _phase_outcome(job, step_name):
    if job.get("status") != "completed":
        return "held"
    conclusion = job.get("conclusion")
    if conclusion in ("cancelled", "skipped", "neutral"):
        return "finished"
    if conclusion not in ("success", "failure", "timed_out"):
        return "held"
    steps = [step for step in job.get("steps", []) if step.get("name") == step_name]
    if not steps:
        return "finished"  # Checkout, download or configuration failed before this phase.
    if len(steps) != 1 or steps[0].get("status") != "completed":
        return "held"
    step = steps[0].get("conclusion")
    if step in ("cancelled", "skipped"):
        return "finished"
    if step not in ("success", "failure", "timed_out"):
        return "held"
    if conclusion in ("failure", "timed_out"):
        return "failed"
    return "succeeded" if step == "success" else "held"


def job_outcomes(records, producer, jobs):
    producer = validate_producer(producer)
    outcomes = {}
    for record in records:
        key, name = identity(record), unit_job_name(record)
        named = _named_jobs(jobs, name)
        # A complete attempt job inventory can prove that this unit never started.
        outcome = "finished" if not named else "held"
        if len(named) == 1 and _job_producer_matches(named[0], producer):
            outcome = _phase_outcome(named[0], "Compile Runtime v2 package")
            if outcome == "succeeded":
                prefix, step = (("Finalize ", "Finalize Runtime v2 core") if record["kind"] == "package"
                                else ("Bind ", "Bind exact module dependencies"))
                final = _named_jobs(jobs, prefix + name)
                outcome = "finished"
                if final:
                    outcome = "held"
                    if len(final) == 1 and _job_producer_matches(final[0], producer):
                        outcome = _phase_outcome(final[0], step)
                        if outcome == "succeeded":
                            outcome = "finished"
        outcomes[key] = outcome
    return outcomes


def reconcile_reservations(state, producer, read_owner, now=None):
    validate_state(state)
    producer = validate_producer(producer)
    now = now or datetime.now(timezone.utc)
    updated, owners, diagnostics = copy.deepcopy(state), {}, []
    for key, unit in state["units"].items():
        reservation = unit.get("reservation")
        if not reservation or reservation["producer"] == producer or _timestamp(reservation["expires_at"]) > now:
            continue
        owner = reservation["producer"]
        owners.setdefault(json.dumps(owner, sort_keys=True), {"producer": owner, "records": []})["records"].append(unit["record"])
    for group in owners.values():
        owner, records = group["producer"], group["records"]
        try:
            snapshot = read_owner(owner)
            run = snapshot["run"]
            if (str(run.get("id")) != owner["run_id"] or type(run.get("run_attempt")) is not int
                    or run["run_attempt"] != owner["run_attempt"] or run.get("head_sha") != owner["commit"]
                    or run.get("status") != "completed"):
                raise ValueError("Reservation owner run/attempt is not conclusively completed")
            outcomes = job_outcomes(records, owner, snapshot["jobs"])
            result = collect_results(updated, records, snapshot["artifacts"], owner, snapshot["artifact_dir"], outcomes)
            updated = result["state"]
            diagnostics.extend(result["diagnostics"])
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            diagnostics.append({"producer": owner, "reason": str(error)})
    return {"state": updated, "diagnostics": diagnostics}


def _ready(record, result):
    ready = {"producer": validate_producer(result.get("producer"))}
    if not isinstance(result.get("artifact_id"), str) or not re.fullmatch(r"[1-9][0-9]*", result["artifact_id"]):
        raise ValueError("Invalid artifact_id")
    if result.get("artifact_name") != artifact_name(record):
        raise ValueError("Artifact name does not match the build identity")
    for field in ("artifact_zip_sha256", "package_sha256"):
        if not isinstance(result.get(field), str) or not SHA256.fullmatch(result[field]):
            raise ValueError(f"Invalid {field}")
        ready[field] = result[field]
    expires = result.get("artifact_expires_at")
    try:
        timestamp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Timezone missing")
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Invalid artifact_expires_at from GitHub") from error
    ready.update(artifact_id=result["artifact_id"], artifact_name=result["artifact_name"],
                 artifact_expires_at=expires)
    return ready


def validate_state(state):
    if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("units"), dict):
        raise ValueError("Unsupported build state schema")
    for key, unit in state["units"].items():
        if not isinstance(unit, dict) or key != identity(unit.get("record", {})):
            raise ValueError("Build state key does not match its record")
        if unit.get("status") not in ("discovered", "build-failed", "build-ready"):
            raise ValueError("Invalid build state status")
        if type(unit.get("consecutive_failures")) is not int or unit["consecutive_failures"] < 0:
            raise ValueError("Invalid consecutive_failures")
        if unit["status"] == "build-ready":
            _ready(unit["record"], unit)
        if "reservation" in unit:
            _validate_reservation(unit["reservation"])


def plan_pending(state, records):
    validate_state(state)
    planned = copy.deepcopy(state)
    for record in records:
        public = clean_record(record)
        key = identity(public)
        if key not in planned["units"]:
            planned["units"][key] = {"record": public, "status": "discovered", "consecutive_failures": 0}
        else:
            planned["units"][key]["record"] = public
    include, blocked = [], []
    for key, unit in planned["units"].items():
        if unit["status"] == "build-ready":
            continue
        if unit["consecutive_failures"] >= 3:
            blocked.append(key)
        else:
            include.append(copy.deepcopy(unit["record"]))
    return {"state": planned, "include": include, "blocked": blocked}


def record_result(state, unit_identity, result):
    validate_state(state)
    if unit_identity not in state["units"]:
        raise ValueError("Cannot record a result for an undiscovered build identity")
    if result.get("status") not in ("build-failed", "build-ready"):
        raise ValueError("Invalid build result status")
    old = state["units"][unit_identity]
    unit = {"record": copy.deepcopy(old["record"]), "status": result["status"]}
    if result["status"] == "build-ready":
        unit.update(_ready(old["record"], result), consecutive_failures=0)
    else:
        # Public ledgers retain the failure category, not raw logs or credentials.
        unit["consecutive_failures"] = old["consecutive_failures"] + 1
    updated = copy.deepcopy(state)
    updated["units"][unit_identity] = unit
    return updated


def collect_results(state, records, artifacts, producer, artifact_dir, outcomes=None):
    """Collect exact bytes; unknown reads and other owners remain held."""
    producer = validate_producer(producer)
    updated = plan_pending(state, records)["state"]
    ready_count, failures, held, diagnostics = 0, [], [], []
    for record in records:
        key, name = identity(record), artifact_name(record)
        unit = updated["units"][key]
        reservation = unit.get("reservation")
        if ((reservation and reservation["producer"] != producer)
                or (unit["status"] == "build-ready" and unit["producer"] != producer)):
            held.append(key)
            diagnostics.append({"identity": key, "reason": "Another producer owns this reservation or ready result"})
            continue
        matches, invalid = [], []
        for artifact in artifacts:
            if artifact.get("name") != name or artifact.get("expired") is not False:
                continue
            try:
                path = Path(artifact_dir) / f"{artifact['id']}.zip"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if artifact.get("digest") != "sha256:" + digest:
                    raise ValueError("Artifact ZIP readback digest mismatch")
                with zipfile.ZipFile(path) as archive:
                    entries = archive.namelist()
                    if entries.count("meta.json") != 1:
                        raise ValueError("Artifact metadata is missing or ambiguous")
                    meta = json.loads(archive.read("meta.json"))
                    if meta.get("producer") != producer:
                        continue
                    if meta.get("schema_version") != 2 or identity(meta) != key:
                        raise ValueError("Artifact identity differs from its producer record")
                    filename = meta.get("filename")
                    if not isinstance(filename, str) or Path(filename).name != filename or entries.count(filename) != 1:
                        raise ValueError("Artifact package is missing or ambiguous")
                    package_digest = hashlib.sha256(archive.read(filename)).hexdigest()
                    if meta.get("sha256") != package_digest:
                        raise ValueError("Artifact package readback SHA mismatch")
                result = {"status": "build-ready", "producer": producer,
                          "artifact_id": str(artifact["id"]), "artifact_name": name,
                          "artifact_zip_sha256": digest, "package_sha256": package_digest,
                          "artifact_expires_at": artifact["expires_at"]}
                _ready(record, result)
                matches.append(result)
            except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
                invalid.append(str(error))
        if len(matches) == 1 and not invalid:
            updated = record_result(updated, key, matches[0])
            updated["units"][key]["last_collected_producer"] = copy.deepcopy(producer)
            ready_count += 1
        elif invalid or len(matches) > 1:
            held.append(key)
            diagnostics.append({"identity": key, "reason": "; ".join(invalid) or "Ambiguous Artifact set"})
        elif unit["status"] != "build-ready":
            outcome = "failed" if outcomes is None else outcomes.get(key, "held")
            if outcome == "held":
                held.append(key)
                diagnostics.append({"identity": key, "reason": "No conclusive unit job/compile-step result"})
            elif outcome == "failed":
                if unit.get("last_collected_producer") != producer:
                    updated = record_result(updated, key, {"status": "build-failed"})
                    updated["units"][key]["last_collected_producer"] = copy.deepcopy(producer)
                failures.append(key)
            elif outcome == "finished":
                updated["units"][key] = {"record": copy.deepcopy(unit["record"]), "status": "discovered",
                                          "consecutive_failures": unit["consecutive_failures"],
                                          "last_collected_producer": copy.deepcopy(producer)}
            else:
                raise ValueError("Invalid unit job outcome")
    return {"state": updated, "ready_count": ready_count, "failed": failures, "held": held, "diagnostics": diagnostics}


class StateConflict(Exception):
    """The public ledger changed after it was read."""


def persist_results(records, artifacts, producer, artifact_dir, read_state, write_state, outcomes=None):
    return _persist(lambda state: collect_results(state, records, artifacts, producer, artifact_dir, outcomes),
                    read_state, write_state)


def persist_plan(records, read_state, write_state):
    return _persist(lambda state: plan_pending(state, records), read_state, write_state)


def _persist(operation, read_state, write_state):
    for attempt in range(3):
        sha, state = read_state()
        collected = operation(state)
        if collected["state"] == state:
            return collected
        try:
            write_state(sha, collected["state"])
            return collected
        except StateConflict:
            if attempt == 2:
                raise


def github_state_store(repository, path):
    endpoint = f"repos/{repository}/contents/{path}"

    def read_state():
        result = subprocess.run(["gh", "api", endpoint + "?ref=main"], check=True, capture_output=True, text=True)
        remote = json.loads(result.stdout)
        return remote["sha"], json.loads(base64.b64decode(remote["content"]))

    def write_state(sha, state):
        payload = {"message": "chore: record macOS v2 build results", "branch": "main", "sha": sha,
                   "content": base64.b64encode((json.dumps(state, indent=2, sort_keys=True) + "\n").encode()).decode()}
        result = subprocess.run(["gh", "api", "--method", "PUT", endpoint, "--input", "-"],
                                input=json.dumps(payload), capture_output=True, text=True)
        if result.returncode:
            if "HTTP 409" in result.stderr:
                raise StateConflict("The Builder ledger changed concurrently")
            raise RuntimeError("Could not persist the Builder ledger with its repository token")

    return read_state, write_state


def github_owner_snapshot(repository, producer, artifact_dir, allow_running=False, names=None):
    """Read one exact attempt. Any unavailable evidence keeps its lease held."""
    producer = validate_producer(producer)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    root = f"repos/{repository}/actions"

    def get(endpoint):
        result = subprocess.run(["gh", "api", endpoint], check=True, capture_output=True, text=True, timeout=120)
        return json.loads(result.stdout)

    run = get(f"{root}/runs/{producer['run_id']}/attempts/{producer['run_attempt']}")
    if (str(run.get("id")) != producer["run_id"] or run.get("run_attempt") != producer["run_attempt"]
            or run.get("head_sha") != producer["commit"]):
        raise ValueError("GitHub owner run/attempt/head does not match its reservation")
    if run.get("status") != "completed" and not (allow_running and run.get("status") == "in_progress"):
        return {"run": run, "jobs": [], "artifacts": [], "artifact_dir": artifact_dir}

    def pages(endpoint, field):
        rows, page = [], 1
        while True:
            values = get(endpoint + f"?per_page=100&page={page}")[field]
            rows.extend(values)
            if len(values) < 100:
                return rows
            page += 1

    jobs = pages(f"{root}/runs/{producer['run_id']}/attempts/{producer['run_attempt']}/jobs", "jobs")
    artifacts = pages(f"{root}/runs/{producer['run_id']}/artifacts", "artifacts")
    selected = []
    directory = Path(artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        if not artifact.get("name", "").startswith("pkg2-macos-") or artifact.get("expired") is not False:
            continue
        if names is not None and artifact["name"] not in names:
            continue
        source = artifact.get("workflow_run", {})
        if str(source.get("id")) != producer["run_id"] or source.get("head_sha") != producer["commit"]:
            raise ValueError("GitHub Artifact has an unexpected producing run/head")
        artifact_id = str(artifact["id"])
        if not re.fullmatch(r"[1-9][0-9]*", artifact_id):
            raise ValueError("Invalid GitHub Artifact id")
        with (directory / f"{artifact_id}.zip").open("wb") as handle:
            subprocess.run(["gh", "api", f"{root}/artifacts/{artifact_id}/zip"], stdout=handle, check=True, timeout=600)
        selected.append(artifact)
    return {"run": run, "jobs": jobs, "artifacts": selected, "artifact_dir": directory}


def reserve_github(records, producer, repository, artifact_dir, read_state=None, write_state=None, now=None, max_groups=None):
    """CAS retry re-reads ownership; one invocation queries each old producer once."""
    if read_state is None or write_state is None:
        read_state, write_state = github_state_store(repository, "runtime-v2/config/build-state.json")
    snapshots = {}

    def read_owner(owner):
        key = json.dumps(owner, sort_keys=True)
        if key not in snapshots:
            try:
                snapshots[key] = github_owner_snapshot(repository, owner, Path(artifact_dir) / owner["run_id"] / str(owner["run_attempt"]))
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
                snapshots[key] = error
        if isinstance(snapshots[key], Exception):
            raise snapshots[key]
        return snapshots[key]

    def operation(state):
        reconciled = reconcile_reservations(state, producer, read_owner, now)
        claimed = reserve_records(reconciled["state"], records, producer, now=now, max_groups=max_groups)
        claimed["diagnostics"] = reconciled["diagnostics"]
        return claimed

    return _persist(operation, read_state, write_state)


def _producer_argument(path):
    return validate_producer(json.loads(Path(path).read_text()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--state", required=True)
    plan.add_argument("--records", required=True)
    plan.add_argument("--matrix", required=True)
    plan.add_argument("--repository")
    plan.add_argument("--persist", action="store_true")
    reserve = sub.add_parser("reserve")
    reserve.add_argument("--state", required=True)
    reserve.add_argument("--records", required=True)
    reserve.add_argument("--matrix", required=True)
    reserve.add_argument("--producer", required=True)
    reserve.add_argument("--repository", required=True)
    reserve.add_argument("--artifact-dir", required=True)
    reserve.add_argument("--max-groups", type=int)
    reserve.add_argument("--runners", action="store_true")
    result = sub.add_parser("result")
    result.add_argument("--state", required=True)
    result.add_argument("--record", required=True)
    result.add_argument("--result", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--state", required=True)
    collect.add_argument("--records", required=True)
    collect.add_argument("--artifacts")
    collect.add_argument("--jobs")
    collect.add_argument("--producer", required=True)
    collect.add_argument("--artifact-dir", required=True)
    collect.add_argument("--repository")
    collect.add_argument("--report", required=True)
    collect.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    state_path = Path(args.state)
    state = json.loads(state_path.read_text())
    if args.command == "plan":
        records = json.loads(Path(args.records).read_text())
        if args.persist:
            if not args.repository or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
                raise ValueError("Persisting build discovery requires its own GitHub repository")
            planned = persist_plan(records["include"], *github_state_store(args.repository, "runtime-v2/config/build-state.json"))
        else:
            planned = plan_pending(state, records["include"])
        state = planned["state"]
        Path(args.matrix).write_text(json.dumps({"include": planned["include"]}, indent=2) + "\n")
        print(json.dumps({"pending": len(planned["include"]), "blocked": planned["blocked"]}))
    elif args.command == "reserve":
        rows = json.loads(Path(args.records).read_text())["include"]
        reserved = reserve_github(rows, _producer_argument(args.producer), args.repository, args.artifact_dir, max_groups=args.max_groups)
        state = reserved.pop("state")
        if args.runners:
            for row in reserved["include"]:
                row["runner"] = "macos-26-intel" if row["arch"] == "x86_64" else "macos-26"
                row["unit_job"] = unit_job_name(row)
        Path(args.matrix).write_text(json.dumps({"include": reserved["include"]}, separators=(",", ":")) + "\n")
        print(json.dumps({"reserved": len(reserved["include"]), "held": reserved["held"], "deferred": len(reserved["deferred"]), "diagnostics": reserved["diagnostics"]}))
    elif args.command == "result":
        record = json.loads(Path(args.record).read_text())
        state = record_result(state, identity(record), json.loads(Path(args.result).read_text()))
    else:
        records = json.loads(Path(args.records).read_text())["include"]
        producer = _producer_argument(args.producer)
        directory = Path(args.artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        if args.repository:
            snapshot = github_owner_snapshot(args.repository, producer, directory, allow_running=True,
                                             names={artifact_name(record) for record in records})
            if snapshot["run"].get("status") not in ("completed", "in_progress"):
                raise ValueError("Current build owner is not collectible")
            artifacts = snapshot["artifacts"]
            outcomes = job_outcomes(records, producer, snapshot["jobs"])
        else:
            if not args.artifacts or not args.jobs:
                raise ValueError("Offline collection needs explicit Artifact and job evidence")
            inventory = json.loads(Path(args.artifacts).read_text())
            artifacts = ([artifact for page in inventory for artifact in page["artifacts"]]
                         if isinstance(inventory, list) else inventory["artifacts"])
            outcomes = job_outcomes(records, producer, json.loads(Path(args.jobs).read_text())["jobs"])
        if args.persist:
            if not args.repository:
                raise ValueError("Persisting build state requires its own GitHub repository")
            collected = persist_results(records, artifacts, producer, directory,
                                        *github_state_store(args.repository, "runtime-v2/config/build-state.json"), outcomes=outcomes)
        else:
            collected = collect_results(state, records, artifacts, producer, directory, outcomes)
        state = collected.pop("state")
        Path(args.report).write_text(json.dumps(collected, indent=2) + "\n")
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
