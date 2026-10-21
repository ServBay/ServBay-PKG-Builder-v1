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

    def test_refresh_keeps_the_local_line_next_to_the_upstream_line(self):
        """The refresh rewrites each series down to one line; both must survive.

        The upstream line stays as the provenance record (commented), the local
        build keeps whatever state the human left it in.
        """
        content = ("# php\t7.3.33\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n"
                   "php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n")
        updater = self.updater(content)
        self.update(updater, {"php": {"7.3": "7.3.33"}})
        self.assertEqual(content, self.conf.read_text())
        self.assertEqual([], updater.updated_records)

    def test_refresh_leaves_a_published_local_line_commented(self):
        content = ("# php\t7.3.33\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n"
                   "# php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n")
        updater = self.updater(content)
        self.update(updater, {"php": {"7.3": "7.3.33"}})
        self.assertEqual(content, self.conf.read_text())
        self.assertEqual([], updater.updated_records)

    def test_refresh_comments_a_still_active_upstream_line(self):
        content = ("php\t7.3.33\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n"
                   "php\t7.3.33.1\tphp-7.3.33.tar.gz\tphp-7.3.33.tar.gz\n")
        updater = self.updater(content)
        self.update(updater, {"php": {"7.3": "7.3.33"}})
        self.assertEqual(content.replace("php\t7.3.33\t", "# php\t7.3.33\t"),
                         self.conf.read_text())

    def test_source_filename_strips_the_local_revision_segment(self):
        """x.y.z.N publishes the upstream x.y.z tarball.

        The revision segment exists only so the registry can order the local
        build above the tag; the archive on php.net is still php-7.3.33.tar.gz.
        Deriving php-7.3.33.1.tar.gz would 404 at download time.
        """
        updater = UPDATE.PackageUpdater(runtime_version="v1")
        self.assertEqual("php-7.3.33.tar.gz", updater.generate_filename("php", "7.3.33.1", "x86"))
        self.assertEqual("php-5.6.40.tar.gz", updater.generate_filename("php", "5.6.40.2", "arm"))
        # Unchanged for upstream releases and for the dated dev builds.
        self.assertEqual("php-7.3.33.tar.gz", updater.generate_filename("php", "7.3.33", "x86"))
        self.assertEqual("php-8.6.0-dev-20251105.tar.gz",
                         updater.generate_filename("php", "8.6.0-dev-20251105", "x86"))

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
