import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_updater(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MACOS = load_updater("php_macos_updates", "runtime/update_packages.py")
WINDOWS = load_updater("php_windows_updates", "runtime-windows/update_packages.py")


class PHPUpdateTests(unittest.TestCase):
    def updaters(self):
        return (MACOS.PackageUpdater(), WINDOWS.WindowsPackageUpdater())

    def detected(self, updater, versions):
        tags = [{"name": "php-" + version} for version in versions]
        with mock.patch.object(updater, "get_github_api", side_effect=[tags, []]), \
                mock.patch.object(MACOS.requests, "head", return_value=mock.Mock(status_code=200)), \
                mock.patch.object(MACOS.requests, "get", side_effect=AssertionError("unexpected network")):
            return updater.get_php_versions()

    def test_official_php86_tags_select_rc4_instead_of_beta3(self):
        versions = ["8.6.0RC1", "8.6.0RC2", "8.6.0RC3", "8.6.0RC4",
                    "8.6.0alpha1", "8.6.0alpha2", "8.6.0alpha3",
                    "8.6.0beta1", "8.6.0beta2", "8.6.0beta3"]
        for updater in self.updaters():
            with self.subTest(updater=type(updater).__name__):
                self.assertEqual("8.6.0RC4", self.detected(updater, versions)["php_8.6"])

    def test_php86_selection_uses_stage_and_numeric_order(self):
        cases = [
            (["8.6.0RC9", "8.6.0RC10"], "8.6.0RC10"),
            (["8.6.0RC4", "8.6.0"], "8.6.0"),
            (["8.6.9", "8.6.10"], "8.6.10"),
            (["8.6.0-dev-20261026", "8.6.0alpha1"], "8.6.0alpha1"),
        ]
        for updater in self.updaters():
            for versions, expected in cases:
                with self.subTest(updater=type(updater).__name__, versions=versions):
                    self.assertEqual(expected, self.detected(updater, versions)["php_8.6"])

    def test_stable_series_continue_excluding_prereleases(self):
        for updater in self.updaters():
            with self.subTest(updater=type(updater).__name__):
                found = self.detected(updater, ["8.5.9", "8.5.10", "8.5.11RC1", "8.5.11beta1"])
                self.assertEqual("8.5.10", found["php_8.5"])

    def refresh(self, runtime, current, latest, history="", dry_run=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        conf = Path(temporary.name) / "packages.conf"
        if runtime == "windows":
            content = history + f"php\t{current}\tphp-{current}-windows-x64.zip\n"
            updater = WINDOWS.WindowsPackageUpdater(conf_file=str(conf), dry_run=dry_run)
        else:
            suffix = "\tsource" if runtime == "v2" else ""
            content = history + f"php\t{current}\tphp-{current}.tar.gz\tphp-{current}.tar.gz{suffix}\n"
            updater = MACOS.PackageUpdater(conf_file=str(conf), runtime_version=runtime, dry_run=dry_run)
        conf.write_text(content)
        with mock.patch.object(MACOS.requests, "head", side_effect=AssertionError("unexpected network")), \
                mock.patch.object(MACOS.requests, "get", side_effect=AssertionError("unexpected network")), \
                contextlib.redirect_stdout(io.StringIO()):
            updater.load_packages()
            updater.update_conf_file({"php": {"8.6": latest}})
        return updater, conf, content

    def test_rc_upgrade_is_active_and_emitted_in_records_and_matrix(self):
        cases = [("8.6.0beta3", "8.6.0RC4"), ("8.6.0RC9", "8.6.0RC10"),
                 ("8.6.0RC4", "8.6.0"), ("8.6.9", "8.6.10")]
        for runtime in ("v1", "v2", "windows"):
            for current, latest in cases:
                with self.subTest(runtime=runtime, current=current, latest=latest):
                    updater, conf, _ = self.refresh(runtime, current, latest)
                    active = updater.active_package_records()
                    self.assertEqual([latest], [row["version"] for row in active])
                    self.assertEqual([latest], [row["version"] for row in updater.updated_records])
                    matrix = updater.build_matrix(updater.updated_records)["include"]
                    self.assertEqual([latest] * len(matrix), [row["version"] for row in matrix])
                    self.assertTrue(matrix)
                    if runtime == "windows" and "RC" in latest:
                        self.assertEqual("testing", matrix[0]["version_type"])

    def test_rc4_is_not_downgraded_when_upstream_input_is_beta3(self):
        for runtime in ("v1", "v2"):
            with self.subTest(runtime=runtime):
                updater, conf, content = self.refresh(runtime, "8.6.0RC4", "8.6.0beta3")
                self.assertEqual(content, conf.read_text())
                self.assertEqual([], updater.updated_records)

    def test_local_best_uses_php_order_even_with_beta_history(self):
        history = "# php\t8.6.0beta3\tphp-8.6.0beta3.tar.gz\tphp-8.6.0beta3.tar.gz\n"
        updater, _, _ = self.refresh("v1", "8.6.0RC4", "8.6.0RC3", history=history)
        self.assertEqual(["8.6.0RC4"], [row["version"] for row in updater.active_package_records()])
        self.assertEqual([], updater.updated_records)

    def test_v1_dry_run_emits_rc4_without_rewriting_beta3(self):
        updater, conf, content = self.refresh("v1", "8.6.0beta3", "8.6.0RC4", dry_run=True)
        self.assertEqual(content, conf.read_text())
        self.assertEqual([{"name": "php", "version": "8.6.0RC4"}], updater.updated_records)

    def test_v2_uses_the_rc_profile_instead_of_an_older_beta_template(self):
        history = "# php\t8.6.0beta3\tphp-8.6.0beta3.tar.gz\t-\tsource\n"
        updater, _, _ = self.refresh("v2", "8.6.0RC4", "8.6.0RC5", history=history)
        matrix = updater.build_matrix(updater.updated_records)["include"]
        self.assertEqual([("8.6.0RC5", "x86_64"), ("8.6.0RC5", "arm64")],
                         [(row["version"], row["arch"]) for row in matrix])


if __name__ == "__main__":
    unittest.main()
