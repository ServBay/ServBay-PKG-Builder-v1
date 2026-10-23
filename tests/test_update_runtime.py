import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("profile_updater", ROOT / "runtime/update_packages.py")
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.conf = self.root / "packages.conf"

    def updater(self, content, runtime="v2", **kwargs):
        self.conf.write_text(content)
        try:
            return UPDATE.PackageUpdater(conf_file=str(self.conf), runtime_version=runtime, **kwargs)
        except TypeError as error:
            self.fail(f"The updater lacks the runtime profile contract: {error}")

    def update(self, updater, versions):
        with contextlib.redirect_stdout(io.StringIO()):
            updater.load_packages()
            updater.update_conf_file(versions)

    def test_pending_only_emits_existing_v2_desired_inputs_without_refreshing_or_rewriting(self):
        content = "nginx\t1.31.7\tnginx-1.31.7.tar.gz\t-\tsource\n"
        updater = self.updater(content)
        output = self.root / "matrix.json"
        with mock.patch.object(updater, "fetch_latest_versions", side_effect=AssertionError("pending must not refresh")), \
                contextlib.redirect_stdout(io.StringIO()):
            updater.run(emit_json_file=str(output), all_packages=True, pending_only=True)
        self.assertEqual(content, self.conf.read_text())
        self.assertEqual(["1.31.7"], [row["version"] for row in json.loads(output.read_text())["include"]])
        with self.assertRaises(ValueError):
            self.updater(content, runtime="v1").run(pending_only=True)

    def test_v1_default_keeps_existing_matrix(self):
        updater = UPDATE.PackageUpdater(dry_run=True)
        self.assertEqual({"include": [
            {"os": "macos", "arch": "x86_64", "name": "nginx", "version": "1.29.5"},
            {"os": "macos", "arch": "arm64", "name": "nginx", "version": "1.29.5"},
        ]}, updater.build_matrix([{"name": "nginx", "version": "1.29.5"}]))

    def test_v2_preserves_build_type_after_update(self):
        updater = self.updater("nginx\t1.29.5\tnginx-1.29.5.tar.gz\tnginx-1.29.5.tar.gz\tsource\n")
        self.update(updater, {"nginx": "1.29.6"})
        active = updater.active_package_records()
        self.assertEqual(1, len(active))
        self.assertEqual("1.29.6", active[0]["version"])
        self.assertEqual("source", active[0]["build_type"])
        self.assertEqual("nginx-1.29.6.tar.gz", active[0]["arm_file"])
        self.assertEqual(["nginx", "1.29.6", "nginx-1.29.6.tar.gz", "nginx-1.29.6.tar.gz", "source"],
                         next(line for line in self.conf.read_text().splitlines()
                              if line.startswith("nginx\t")).split("\t"))

    def test_updated_prebuilt_and_mixed_profiles_preserve_their_actual_build_type(self):
        for build_type in ("prebuilt", "mixed"):
            with self.subTest(build_type=build_type):
                updater = self.updater(f"disk-cleaner\t1.0.0\tservbay-diskcleaner-1.0.0-macos-universal.tar.gz\t"
                                       f"servbay-diskcleaner-1.0.0-macos-universal.tar.gz\t{build_type}\n")
                self.update(updater, {"disk-cleaner": "1.0.1"})
                active = updater.active_package_records()
                self.assertEqual(1, len(active))
                self.assertEqual("1.0.1", active[0]["version"])
                self.assertEqual(build_type, active[0]["build_type"])
                self.assertTrue(all(row["build_type"] == build_type for row in updater.build_matrix(active)["include"]))

    def test_dash_architecture_is_not_scheduled(self):
        updater = self.updater("nginx\t1.29.5\tnginx-1.29.5.tar.gz\t-\tsource\n")
        self.update(updater, {"nginx": "1.29.6"})
        matrix = updater.build_matrix(updater.active_package_records())
        self.assertEqual([{"runtime_version": "v2", "os": "macos", "kind": "package",
                           "name": "nginx", "version": "1.29.6", "arch": "x86_64",
                           "build_type": "source", "build_revision": 0}], matrix["include"])

    def test_invalid_type_fails_without_rewriting_conf(self):
        content = "nginx\t1.29.5\tnginx-1.29.5.tar.gz\tnginx-1.29.5.tar.gz\tbinary\n"
        updater = self.updater(content)
        with self.assertRaisesRegex(ValueError, "build_type"):
            self.update(updater, {"nginx": "1.29.6"})
        self.assertEqual(content, self.conf.read_text())

    def test_mixed_whitespace_fifth_column_is_normalized_without_corrupting_filename(self):
        updater = self.updater("mysql\t8.4.8\tmysql-8.4.8.tar.gz\tmysql-8.4.8.tar.gz source\n")
        self.update(updater, {"mysql": {"8.4": "8.4.13"}})
        active = updater.active_package_records()
        self.assertEqual("mysql-8.4.13.tar.gz", active[0]["arm_file"])
        self.assertEqual("source", active[0]["build_type"])
        self.assertEqual(5, len(next(line for line in self.conf.read_text().splitlines()
                                    if line.startswith("mysql\t")).split("\t")))

    def test_universal_prebuilt_asset_produces_two_explicit_architecture_records(self):
        updater = self.updater("disk-cleaner\t1.0.0\tservbay-diskcleaner-1.0.0-macos-universal.tar.gz\t"
                               "servbay-diskcleaner-1.0.0-macos-universal.tar.gz\tprebuilt\n")
        records = updater.active_package_records()
        self.assertEqual("prebuilt", records[0]["build_type"])
        matrix = updater.build_matrix(records)["include"]
        self.assertEqual(["x86_64", "arm64"], [row["arch"] for row in matrix])
        self.assertTrue(all(row["runtime_version"] == "v2" and row["build_type"] == "prebuilt"
                            for row in matrix))

    def test_unchanged_v2_desired_version_stays_active_for_pending_builds(self):
        updater = self.updater("nginx\t1.29.5\tnginx-1.29.5.tar.gz\tnginx-1.29.5.tar.gz\tsource\n")
        self.update(updater, {"nginx": "1.29.5"})
        self.assertEqual(["1.29.5"], [row["version"] for row in updater.active_package_records()])
        self.assertEqual([], updater.updated_records)

    def test_v2_history_does_not_enable_unsupported_older_series(self):
        updater = self.updater("postgresql\t10.23\tpostgresql-10.23.tar.bz2\tpostgresql-10.23.tar.bz2\tsource\n"
                               "postgresql\t18.1\tpostgresql-18.1.tar.bz2\tpostgresql-18.1.tar.bz2\tsource\n")
        self.update(updater, {"postgresql": {"7.4": "7.4.30", "10": "10.23", "18": "18.6", "19": "19.beta4"}})
        self.assertEqual(["10.23", "18.6", "19.beta4"],
                         [row["version"] for row in updater.active_package_records()])

    def test_v2_preserves_the_support_floor_of_each_existing_major(self):
        updater = self.updater("python\t2.7.18\tPython-2.7.18.tgz\tPython-2.7.18.tgz\tsource\n"
                               "python\t3.5.10\tPython-3.5.10.tgz\tPython-3.5.10.tgz\tsource\n"
                               "python\t3.14.3\tPython-3.14.3.tgz\tPython-3.14.3.tgz\tsource\n")
        self.update(updater, {"python": {"2.7": "2.7.18", "3.0": "3.0.1", "3.4": "3.4.10",
                                         "3.5": "3.5.10", "3.14": "3.14.8", "3.15": "3.15.0"}})
        self.assertEqual(["2.7.18", "3.5.10", "3.14.8", "3.15.0"],
                         [row["version"] for row in updater.active_package_records()])

    def test_dry_run_emits_new_v2_matrix_without_rewriting_file(self):
        content = "nginx\t1.29.5\tnginx-1.29.5.tar.gz\t-\tsource\n"
        updater = self.updater(content, dry_run=True)
        output = self.root / "matrix.json"
        with mock.patch.object(updater, "fetch_latest_versions", return_value={"nginx": "1.29.6"}), \
                contextlib.redirect_stdout(io.StringIO()):
            updater.run(emit_json_file=str(output))
        rows = json.loads(output.read_text())["include"]
        self.assertEqual([("nginx", "1.29.6", "x86_64", "source")],
                         [(row["name"], row["version"], row["arch"], row["build_type"]) for row in rows])
        self.assertEqual(content, self.conf.read_text())

    def test_v1_writer_remains_four_columns(self):
        updater = self.updater("nginx\t1.29.5\tnginx-1.29.5.tar.gz\tnginx-1.29.5.tar.gz\n", runtime="v1")
        self.update(updater, {"nginx": "1.29.6"})
        line = next(line for line in self.conf.read_text().splitlines() if line.startswith("nginx\t"))
        self.assertEqual(4, len(line.split("\t")))

    def test_invalid_profile_is_rejected(self):
        try:
            UPDATE.PackageUpdater(runtime_version="all")
        except TypeError as error:
            self.fail(f"The updater lacks the runtime profile contract: {error}")
        except ValueError:
            return
        self.fail("An aggregate runtime selector must not become a concrete build profile")

    def test_v2_refreshes_redis_series_already_supported_by_its_own_profile(self):
        updater = self.updater("redis\t7.4.7\tredis-7.4.7.tar.gz\tredis-7.4.7.tar.gz\tsource\n"
                               "redis\t8.4.1\tredis-8.4.1.tar.gz\tredis-8.4.1.tar.gz\tsource\n")
        updater.load_packages()
        tags = [{"name": "7.4.11"}, {"name": "8.4.2"}, {"name": "8.6.0"}]
        with mock.patch.object(updater, "get_github_api", side_effect=[tags, []]), \
                mock.patch.object(updater, "verify_download", return_value=True):
            found = updater.get_redis_versions()
        self.assertEqual("8.4.2", found.get("redis_8.4"))
        self.assertEqual("8.6.0", found.get("redis_8.6"))

    def test_v2_dotnet_discovers_the_new_channel_without_changing_v1_policy(self):
        updater = self.updater("dotnetsdk\t10.0.103\tdotnet-sdk-10.0.103-osx-x64.tar.gz\t"
                               "dotnet-sdk-10.0.103-osx-arm64.tar.gz\tprebuilt\n")
        updater.load_packages()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"releases-index": [{"channel-version": "11.0", "latest-sdk": "11.0.100"}]}
        with mock.patch.object(UPDATE.requests, "get", return_value=response):
            self.assertEqual({"dotnetsdk_11.0": "11.0.100"}, updater.get_dotnet_versions())
            legacy = UPDATE.PackageUpdater()
            self.assertEqual({}, legacy.get_dotnet_versions())

    def test_unverified_upstream_filename_does_not_corrupt_a_five_column_profile(self):
        content = "openjdk\t25.0.2\tzulu25-old.tar.gz\tzulu25-old-arm.tar.gz\tprebuilt\n"
        updater = self.updater(content)
        with self.assertRaises(ValueError):
            self.update(updater, {"openjdk": {"25": "25.0.4"}})
        self.assertEqual(content, self.conf.read_text())

    def test_v2_missing_openjdk_architecture_is_explicitly_unavailable(self):
        updater = self.updater("openjdk\t25.0.2\tzulu25-old.tar.gz\tzulu25-old-arm.tar.gz\tprebuilt\n")
        updater.openjdk_details = {"openjdk_25": {"version": "25.0.4", "x64_filename": "zulu25-real.tar.gz"}}
        self.update(updater, {"openjdk": {"25": "25.0.4"}})
        row = updater.active_package_records()[0]
        self.assertEqual("-", row["arm_file"])
        self.assertEqual(["x86_64"], [item["arch"] for item in updater.build_matrix([row])["include"]])

    def test_subversion_uses_the_real_apache_release_index_not_lexical_git_tags(self):
        updater = self.updater("subversion\t1.14.5\tsubversion-1.14.5.tar.bz2\tsubversion-1.14.5.tar.bz2\tsource\n")
        self.assertTrue(hasattr(updater, "get_subversion_version"), "Subversion needs its actual Apache release source")
        response = mock.Mock(status_code=200, text='<a>subversion-1.14.5.tar.bz2</a><a>subversion-1.15.0.tar.bz2</a>')
        with mock.patch.object(UPDATE.requests, "get", return_value=response):
            self.assertEqual("1.15.0", updater.get_subversion_version())
        updater.load_packages()
        with contextlib.ExitStack() as stack, contextlib.redirect_stdout(io.StringIO()):
            for name in dir(updater):
                if name.startswith("get_"):
                    stack.enter_context(mock.patch.object(updater, name,
                                        return_value="1.15.0" if name == "get_subversion_version" else
                                                     None if name == "get_ngrok_version" else {}))
            self.assertEqual("1.15.0", updater.fetch_latest_versions().get("subversion"))


if __name__ == "__main__":
    unittest.main()
