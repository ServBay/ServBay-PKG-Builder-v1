"""Contract tests for the async ollama Windows pipeline.

build-windows.yml must dispatch ollama upstream builds without blocking
(upstream takes 3.5-4.5h with the CUDA toolchains), and the release-ready
callback must reach build-windows-async.yml and then the Uploader with the
same artifact contract and the dispatching round's dry_run mode.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class BuildWindowsAsyncTests(unittest.TestCase):
    def test_ollama_dispatch_passes_dry_run_through(self):
        workflow = read(".github/workflows/build-windows.yml")
        ollama_branch = workflow.split("            ollama)", 1)[1].split("            python)", 1)[0]
        self.assertIn("builder_dry_run=${UPLOAD_DRY_RUN}", ollama_branch)
        # Only ollama may carry the flag: upstream workflows 422 on unknown inputs.
        python_branch = workflow.split("            python)", 1)[1].split("            openssl)", 1)[0]
        self.assertNotIn("builder_dry_run", python_branch)

    def test_ollama_exits_pending_without_polling(self):
        workflow = read(".github/workflows/build-windows.yml")
        self.assertIn('if [ "$PKG" = "ollama" ]', workflow)
        self.assertIn('echo "pending=true" >> "$GITHUB_OUTPUT"', workflow)
        # The early exit must precede the poll loop so ollama never blocks.
        self.assertLess(
            workflow.index('if [ "$PKG" = "ollama" ]'),
            workflow.index("for i in $(seq 1 180)"),
        )

    def test_stage2_skipped_when_pending(self):
        workflow = read(".github/workflows/build-windows.yml")
        guarded = [line for line in workflow.splitlines()
                   if "steps.upstream.outputs.pending != 'true'" in line]
        # Build, meta.json and artifact upload must all be guarded.
        self.assertEqual(len(guarded), 3)

    def test_nightly_forwards_dry_run_to_build_windows(self):
        orchestrator = read(".github/workflows/check-updates.yml")
        self.assertIn(
            "upload_dry_run: ${{ needs.check.outputs.upload_dry_run }}", orchestrator)
        manual = read(".github/workflows/build-and-dispatch.yml")
        # build-and-dispatch declares a boolean input; format() stringifies it
        # for the string-typed workflow_call input.
        self.assertIn(
            "upload_dry_run: ${{ format('{0}', inputs.upload_dry_run) }}", manual)

    def test_async_workflow_contract(self):
        workflow = read(".github/workflows/build-windows-async.yml")
        self.assertIn("types: [upstream-release-ready]", workflow)
        # Untrusted payload: whitelist the package, constrain the version, and
        # default an invalid dry_run to the Uploader's safe value.
        self.assertIn("ollama) ;;", workflow)
        self.assertIn('[[ "$VERSION" =~ ^[0-9]+(\\.[0-9]+)+$ ]]', workflow)
        self.assertIn("DRY_RUN=true", workflow)
        # The reusable pipeline produces the pkg-* artifact; the notify step
        # hands this run to the Uploader with the v1 contract.
        self.assertIn("uses: ./.github/workflows/build-windows.yml", workflow)
        self.assertIn("'event_type': 'builder-build-complete'", workflow)
        self.assertIn("'run_id': os.environ['RUN_ID']", workflow)
        self.assertIn("'dry_run': os.environ['DRY_RUN']", workflow)
        # One async package at a time.
        self.assertIn("group: build-windows-async-${{ github.event.client_payload.pkg }}", workflow)

    def test_upstream_callback_waits_for_release_step(self):
        # The upstream notify step must come after the release step and only
        # fire for non-dry-run builds (dry runs create no release to package).
        upstream = read_upstream_workflow()
        if upstream is None:
            self.skipTest("Ollama-Windows-Portable checkout not found")
        self.assertIn("-f event_type=upstream-release-ready", upstream)
        notify = upstream.split("- name: Notify Builder release is ready", 1)[1]
        self.assertIn("inputs.dry_run == false", notify.split("run:", 1)[0])
        self.assertLess(
            upstream.index("Create GitHub Release and Upload Asset"),
            upstream.index("Notify Builder release is ready"),
        )
        self.assertIn("client_payload[dry_run]=$env:BUILDER_DRY_RUN", upstream)


def read_upstream_workflow():
    path = Path("/Users/samwu/hosts/Ollama-Windows-Portable/.github/workflows/build_ollama_windows.yml")
    return path.read_text(encoding="utf-8") if path.exists() else None


if __name__ == "__main__":
    unittest.main()
