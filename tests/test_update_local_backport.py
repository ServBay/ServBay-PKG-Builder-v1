import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("profile_updater", ROOT / "runtime/update_packages.py")
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


class LocalBackportVersionTests(unittest.TestCase):
    """Refreshing must not undo a locally backported build on an EOL branch.

    PHP 5.x-8.1 are end-of-life, so php-src will never tag anything past the
    version those branches stopped at. The security backports in
    runtime/patch/phpX.Y are published under a version above that tag
    (e.g. 7.3.33.1 vs upstream 7.3.33). Without the guard below, the daily
    refresh rewrites the line back to 7.3.33, queues a build of the unpatched
    tarball, and the resulting upload overwrites the patched artifact.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.conf = self.root / "packages.conf"

    def updater(self, content):
        self.conf.write_text(content)
        return UPDATE.PackageUpdater(conf_file=str(self.conf), runtime_version="v1")

    def update(self, updater, versions):
        with contextlib.redirect_stdout(io.StringIO()):
            updater.load_packages()
            updater.update_conf_file(versions)

    def test_locally_patched_version_is_kept_instead_of_downgraded(self):
        content = "php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n"
        updater = self.updater(content)
        self.update(updater, {"php": {"7.3": "7.3.33"}})
        self.assertEqual(content, self.conf.read_text())
        self.assertEqual([], updater.updated_records)

    def test_an_already_published_patch_is_not_queued_for_rebuild(self):
        content = "# php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n"
        updater = self.updater(content)
        self.update(updater, {"php": {"7.3": "7.3.33"}})
        self.assertEqual(content, self.conf.read_text())
        self.assertEqual([], updater.updated_records)

    def test_upstream_still_wins_once_it_moves_past_the_local_build(self):
        content = "php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n"
        updater = self.updater(content)
        self.update(updater, {"php": {"7.3": "7.3.34"}})
        self.assertIn("7.3.34", self.conf.read_text())
        self.assertNotIn("7.3.33.1", self.conf.read_text())

    def test_a_local_build_in_another_series_does_not_mask_this_one(self):
        content = "php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n" \
                  "php\t8.0.30\tphp-8.0.30.tar.gz\tphp-8.0.30.tar.gz\n"
        updater = self.updater(content)
        self.update(updater, {"php": {"7.3": "7.3.33", "8.0": "8.0.31"}})
        text = self.conf.read_text()
        self.assertIn("7.3.33.1", text)
        self.assertIn("8.0.31", text)


if __name__ == "__main__":
    unittest.main()
