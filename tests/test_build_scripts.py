import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BuildScriptTests(unittest.TestCase):
    def test_macos_git_build_disables_optional_rust_components(self):
        script = (ROOT / "runtime" / "build_package").read_text(encoding="utf-8")
        build_git = self._extract_shell_function(script, "build_git")

        self.assertIn("NO_RUST=1", build_git)
        self.assertRegex(build_git, r"make\s+-j\$\{CPU_NUMBER\}[^\\n]*NO_RUST=1")
        self.assertRegex(build_git, r"make\s+install[^\\n]*NO_RUST=1")

    def test_windows_git_uses_portable_git_with_shell_tools(self):
        script = (ROOT / "runtime-windows" / "build_windows").read_text(encoding="utf-8")
        build_git = self._extract_shell_function(script, "Build_Package_git")

        self.assertIn("PortableGit-${package_version}-${git_arch_bit}.7z.exe", build_git)
        self.assertNotIn("MinGit-${package_version}", build_git)
        self.assertIn('arm64) git_arch_bit="arm64"', build_git)
        self.assertIn('"git-${package_version}"', build_git)

    @staticmethod
    def _extract_shell_function(script, name):
        match = re.search(rf"^{name}\(\) \{{\n(?P<body>.*?)(?=^\}}\n)", script, re.MULTILINE | re.DOTALL)
        if not match:
            raise AssertionError(f"Function not found: {name}")
        return match.group("body")


if __name__ == "__main__":
    unittest.main()
