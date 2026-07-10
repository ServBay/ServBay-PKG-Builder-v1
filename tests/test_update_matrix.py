import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


macos_update = load_module("macos_update", "runtime/update_packages.py")
windows_update = load_module("windows_update", "runtime-windows/update_packages.py")


class UpdateMatrixTests(unittest.TestCase):
    def test_macos_matrix_expands_each_package_to_two_arches(self):
        updater = macos_update.PackageUpdater(dry_run=True)

        matrix = updater.build_matrix([
            {"name": "nginx", "version": "1.29.0"},
        ])

        self.assertEqual(
            matrix,
            {
                "include": [
                    {"os": "macos", "arch": "x86_64", "name": "nginx", "version": "1.29.0"},
                    {"os": "macos", "arch": "arm64", "name": "nginx", "version": "1.29.0"},
                ]
            },
        )

    def test_windows_matrix_uses_x64_for_first_automation_phase(self):
        updater = windows_update.WindowsPackageUpdater(dry_run=True)

        matrix = updater.build_matrix([
            {"name": "ollama", "version": "0.21.2"},
        ])

        self.assertEqual(
            matrix,
            {
                "include": [
                    {
                        "os": "windows",
                        "arch": "x64",
                        "name": "ollama",
                        "version": "0.21.2",
                        "version_type": "stable",
                    },
                ]
            },
        )

    def test_macos_active_package_records_ignore_commented_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "packages.conf"
            conf.write_text(
                "# nginx\t1.28.0\tnginx-1.28.0.tar.gz\tnginx-1.28.0.tar.gz\n"
                "nginx\t1.29.0\tnginx-1.29.0.tar.gz\tnginx-1.29.0.tar.gz\n",
                encoding="utf-8",
            )
            updater = macos_update.PackageUpdater(conf_file=str(conf), dry_run=True)

            records = updater.active_package_records()

            self.assertEqual(records, [{"name": "nginx", "version": "1.29.0"}])

    def test_windows_active_package_records_ignore_header_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "windows-packages.txt"
            conf.write_text(
                "Package\tVersion\tFilename\n"
                "# ollama\t0.20.0\n"
                "ollama\t0.21.2\n",
                encoding="utf-8",
            )
            updater = windows_update.WindowsPackageUpdater(conf_file=str(conf), dry_run=True)

            records = updater.active_package_records()

            self.assertEqual(records, [{"name": "ollama", "version": "0.21.2"}])


if __name__ == "__main__":
    unittest.main()
