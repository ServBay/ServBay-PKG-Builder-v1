import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_v2_workflows as workflow_tests
import test_windows_v2_incremental as incremental_tests


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/build-windows-v2-package.yml"


class WindowsV2WorkflowTests(unittest.TestCase):
    def test_manual_workflow_only_produces_v2_package_artifact(self):
        self.assertTrue(WORKFLOW.is_file())
        text = WORKFLOW.read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("runtime-windows/repack_v2.py", text)
        self.assertIn("--minimum-os", text)
        self.assertIn("actions/upload-artifact@v4", text)
        self.assertNotIn("action-gh-release", text)
        self.assertNotIn("upload_packages", text)
        self.assertNotIn("repository_dispatch", text)

    def test_default_source_is_a_pinned_public_v1_adminer_release(self):
        text = WORKFLOW.read_text()
        self.assertIn("default: 6.2.0", text)
        self.assertIn("adminer-6.2.0-windows-x64.7z", text)
        self.assertIn("302f4c55095607e71bd3f7baef5fc1130ed26b9d6b0baea06231aacd571543b8", text)
        self.assertNotIn("base:", text.lower())


    def test_reusable_producer_keeps_manual_inputs_and_passes_revision_and_identity(self):
        data = workflow_tests.WorkflowTests().workflow(WORKFLOW.name)
        self.assertIn("workflow_call", data["on"])
        for event in ("workflow_dispatch", "workflow_call"):
            self.assertEqual(data["on"][event]["inputs"]["build_revision"]["default"], 0)
        self.assertEqual(data["on"]["workflow_dispatch"]["inputs"]["version"]["default"], "6.2.0")
        self.assertEqual(data["jobs"]["repack"]["timeout-minutes"], 30)
        steps = data["jobs"]["repack"]["steps"]
        prepare = next(step for step in steps if step.get("name") == "Prepare exact Windows producer inputs")
        describe = next(step for step in steps if step.get("id") == "artifact")
        upload = next(step for step in steps if step.get("uses") == "actions/upload-artifact@v4")
        self.assertEqual(data["jobs"]["repack"]["outputs"]["artifact_name"],
                         "${{ steps.artifact.outputs.artifact_name }}")
        for revision in (0, 2):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "input").mkdir()
                (root / "input/source.7z").write_bytes(b"source")
                output = root / "output"
                bindings = {"inputs.pkg": "nginx", "inputs.version": "1.31.8",
                            "inputs.build_revision": revision, "inputs.source_url": "https://example.invalid/source.7z",
                            "inputs.source_sha256": "b" * 64, "github.sha": "a" * 40,
                            "github.run_id": "12345", "github.run_attempt": 2}
                environment = dict(os.environ, GITHUB_OUTPUT=str(output))
                environment.update({key: str(incremental_tests.condition(value, bindings))
                                    for key, value in prepare["env"].items()})
                result = subprocess.run(["/bin/bash", "-c", prepare["run"]], env=environment, cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                identity = json.loads((root / "identity.json").read_text())
                self.assertEqual(identity["build_revision"], revision)
                self.assertEqual(json.loads((root / "producer.json").read_text()),
                                 dict(commit="a" * 40, run_id="12345", run_attempt=2))
                for name in ("runtime/ci/package_meta.py", "runtime/ci/build_state.py", "runtime-v2/ci/php_source.py"):
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / name, target)
                (root / "v2-output").mkdir()
                (root / "v2-output/meta.json").write_text(json.dumps(dict(identity, arch="x86_64")))
                result = subprocess.run(["/bin/bash", "-c", describe["run"]], env=environment, cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = "pkg2-windows-x86_64-package-nginx-1.31.8" + ("-r2" if revision else "")
                self.assertEqual(output.read_text().strip(), "artifact_name=" + expected)
                self.assertEqual(incremental_tests.condition(upload["with"]["name"],
                                 {"steps.artifact.outputs.artifact_name": output.read_text().strip().split("=", 1)[1]}),
                                 expected)


if __name__ == "__main__":
    unittest.main()
