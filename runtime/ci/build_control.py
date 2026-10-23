#!/usr/bin/env python3
"""Keep v2 build provenance equal to the triggering Actions source."""

import re


def run_decision(runtime_version, pending_only, config_changed, continue_build, trigger_sha, checkout_sha):
    if runtime_version not in ("v1", "v2"):
        raise ValueError("Invalid runtime generation")
    if runtime_version == "v1":
        if pending_only:
            raise ValueError("pending-only is a Runtime v2 entrypoint")
        return {"build_allowed": bool(continue_build), "dispatch_pending": False}
    if pending_only and config_changed:
        raise ValueError("A pending-only run must never refresh or commit another configuration")
    if config_changed:
        return {"build_allowed": False, "dispatch_pending": bool(continue_build)}
    if not re.fullmatch(r"[0-9a-f]{40}", trigger_sha or "") or checkout_sha != trigger_sha:
        raise ValueError("Runtime v2 build checkout must equal its own Actions trigger SHA")
    return {"build_allowed": bool(continue_build), "dispatch_pending": False}


def pending_inputs(dry_run):
    if type(dry_run) is not bool:
        raise ValueError("The upload dry-run flag must be boolean")
    return {"runtime_version": "v2", "target": "macos", "pending_only": "true", "continue_build": "true",
            "upload_dry_run": str(dry_run).lower(), "full_rebuild": "false"}


def refresh_inputs(event_name, runtime_version="", target="all", pending_only=False,
                   continue_build=False, dry_run=True, full_rebuild=False, schedule_mode="off"):
    """Expand only the update entrypoint; never emit an all-generation build."""
    if event_name == "schedule":
        mode = schedule_mode or "off"
        if mode == "off":
            return None
        if mode not in ("check", "build", "publish"):
            raise ValueError("Invalid V2_PACKAGE_SCHEDULE_MODE")
        continue_build, dry_run, full_rebuild = mode in ("build", "publish"), mode != "publish", False
    elif event_name == "workflow_dispatch" and runtime_version == "all":
        if pending_only or target not in ("all", "macos"):
            raise ValueError("all requires a non-pending macOS update check")
    else:
        return None
    if any(type(value) is not bool for value in (continue_build, dry_run, full_rebuild)):
        raise ValueError("Update flags must be boolean")
    return {"runtime_version": "v2", "target": "macos", "pending_only": "false",
            "continue_build": str(continue_build).lower(), "upload_dry_run": str(dry_run).lower(),
            "full_rebuild": str(full_rebuild).lower()}


def _dispatch(repository, inputs):
    """Use main after configuration commits, as the existing continuation does."""
    import json
    import re
    import subprocess
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    subprocess.run(["gh", "api", "--method", "POST",
                    f"repos/{repository}/actions/workflows/check-updates.yml/dispatches", "--input", "-"],
                   input=json.dumps({"ref": "main", "inputs": inputs}),
                   text=True, check=True)


def dispatch_pending(repository, dry_run):
    """Create one new workflow run whose checkout and Actions head agree."""
    _dispatch(repository, pending_inputs(dry_run))


def dispatch_refresh(repository, **options):
    inputs = refresh_inputs(**options)
    if inputs is None:
        return False
    _dispatch(repository, inputs)
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--upload-dry-run", choices=("true", "false"), required=True)
    arguments = parser.parse_args()
    dispatch_pending(arguments.repository, arguments.upload_dry_run == "true")
