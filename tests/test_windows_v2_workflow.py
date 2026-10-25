from pathlib import Path
import unittest


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
        self.assertIn("pkg2-windows-x86_64-package-", text)
        self.assertNotIn("action-gh-release", text)
        self.assertNotIn("upload_packages", text)
        self.assertNotIn("repository_dispatch", text)

    def test_default_source_is_a_pinned_public_v1_adminer_release(self):
        text = WORKFLOW.read_text()
        self.assertIn("default: 6.2.0", text)
        self.assertIn("adminer-6.2.0-windows-x64.7z", text)
        self.assertIn("302f4c55095607e71bd3f7baef5fc1130ed26b9d6b0baea06231aacd571543b8", text)
        self.assertNotIn("base:", text.lower())


if __name__ == "__main__":
    unittest.main()
