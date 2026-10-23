import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProfileTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime/ci/build_profile.py"
        self.assertTrue(path.is_file(), "The parameterized build profile is missing")
        spec = importlib.util.spec_from_file_location("build_profile", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.conf = self.root / "packages.conf"
        self.conf.write_text("nginx\t1.31.7\tnginx-1.31.7.tar.gz\t-\tsource\n"
                             "httpd\t2.4.67\thttpd-2.4.67.tar.gz\thttpd-2.4.67.tar.gz\tsource\n")

    def test_v1_default_keeps_both_runner_architectures(self):
        matrix = self.module.build_matrix("v1", "nginx", "1.31.7", "all", self.conf)
        self.assertEqual(["x86_64", "arm64"], [row["arch"] for row in matrix["include"]])
        self.assertTrue(all("runtime_version" not in row for row in matrix["include"]))

    def test_v2_manual_matrix_obeys_the_actual_architecture_availability(self):
        matrix = self.module.build_matrix("v2", "nginx", "1.31.7", "all", self.conf)
        self.assertEqual(["x86_64"], [row["arch"] for row in matrix["include"]])
        with self.assertRaises(ValueError):
            self.module.build_matrix("v2", "nginx", "1.31.7", "arm64", self.conf)

    def test_httpd_recipe_keeps_the_published_apache_identity(self):
        matrix = self.module.build_matrix("v2", "httpd", "2.4.67", "x86_64", self.conf)
        self.assertEqual("apache", matrix["include"][0]["name"])
        self.assertEqual("httpd", matrix["include"][0]["recipe_name"])

    def test_manual_inputs_keep_hyphenated_names_and_development_versions(self):
        self.conf.write_text("ai-gateway\t1.0.0\tasset.tar.gz\tasset.tar.gz\tprebuilt\n"
                             "php\t8.6.0-dev-20261021\t012345.tar.gz\t012345.tar.gz\tsource\n")
        for package, version in (("ai-gateway", "1.0.0"), ("php", "8.6.0-dev-20261021")):
            row = self.module.build_matrix("v2", package, version, "arm64", self.conf)["include"][0]
            self.assertEqual((package, version), (row["name"], row["version"]))

    def test_an_exact_pending_historical_version_is_still_rebuildable(self):
        self.conf.write_text("# nginx\t1.31.6\tnginx-1.31.6.tar.gz\tnginx-1.31.6.tar.gz\tsource\n"
                             "nginx\t1.31.7\tnginx-1.31.7.tar.gz\tnginx-1.31.7.tar.gz\tsource\n")
        row = self.module.build_matrix("v2", "nginx", "1.31.6", "arm64", self.conf)["include"][0]
        self.assertEqual("1.31.6", row["version"])

    def test_source_lookup_hashes_the_standardized_download_archive(self):
        source = self.root / "src/nginx-1.31.7.tar.gz"
        source.parent.mkdir()
        source.write_bytes(b"actual downloaded bytes")
        found = self.module.find_source(self.root, "nginx", "1.31.7", "x86_64", self.conf)
        self.assertEqual(source, Path(found["path"]))
        self.assertEqual("1.31.7", found["version"])

    def test_caddy_source_is_the_real_module_zip_not_go_sum_or_final_tar(self):
        cache = self.root / "gomod"
        source = cache / "cache/download/github.com/caddyserver/caddy/v2/@v/v2.11.2.zip"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"real module zip bytes")
        self.conf.write_text("caddy\t2.11.2\tcaddy-2.11.2.tar.gz\tcaddy-2.11.2.tar.gz\tsource\n")
        found = self.module.find_source(self.root, "caddy", "2.11.2", "x86_64", self.conf, cache)
        self.assertEqual(source, Path(found["path"]))
        self.assertNotIn("url", found)

    def test_real_v2_initial_backlog_fits_the_actions_parent_matrix(self):
        self.assertTrue(hasattr(self.module, "workflow_plan"), "Actions needs a bounded parent matrix, separate from exact build units")
        updater = self.module._updater.PackageUpdater(str(ROOT / "runtime-v2/config/packages.conf"), runtime_version="v2")
        updater.load_packages()
        records = updater.build_matrix(updater.active_package_records())["include"]
        self.assertGreater(len(records), 256)
        plan = self.module.workflow_plan(records)
        self.assertLessEqual(len(plan["matrix"]["include"]), 256)
        self.assertEqual(records, plan["records"])
        self.assertEqual([], plan["deferred"])
        self.assertTrue(all(row["arch"] == "all" for row in plan["matrix"]["include"]))

    def test_workflow_grouping_keeps_a_missing_arch_and_rebuild_revisions_explicit(self):
        self.assertTrue(hasattr(self.module, "workflow_plan"))
        rows = self.module.build_matrix("v2", "httpd", "2.4.67", "all", self.conf)["include"]
        records = [rows[0], dict(rows[0], build_revision=1), dict(rows[1], build_revision=1)]
        plan = self.module.workflow_plan(records)
        self.assertEqual([("x86_64", 0), ("all", 1)],
                         [(row["arch"], row["build_revision"]) for row in plan["matrix"]["include"]])
        self.assertTrue(all(row["recipe_name"] == "httpd" for row in plan["matrix"]["include"]))
        self.assertEqual(records, plan["records"])

    def test_excess_workflow_groups_are_deferred_without_counting_build_failure(self):
        self.assertTrue(hasattr(self.module, "workflow_plan"))
        row = self.module.build_matrix("v2", "httpd", "2.4.67", "x86_64", self.conf)["include"][0]
        records = [dict(row, name=f"package-{number}") for number in range(257)]
        plan = self.module.workflow_plan(records)
        self.assertEqual(256, len(plan["matrix"]["include"]))
        self.assertEqual(records[:256], plan["records"])
        self.assertEqual(records[256:], plan["deferred"])
        self.assertTrue(all("status" not in item for item in plan["deferred"]))

    def test_public_recipe_never_renames_an_application_environment(self):
        script = ROOT / "runtime-v2/scripts/build_package.sh"
        self.assertTrue(script.is_file(), "The public v2 software recipe is missing")
        base = self.root / "base"
        (base / "packages/common").mkdir(parents=True)
        (base / ".bootstrap-v2.json").write_text(json.dumps({"runtime_version": "v2"}))
        env = dict(os.environ, SERVBAY_BASE_FULL_PATH=str(base), SERVBAY_BUILD_ROOT=str(self.root / "work"))
        result = subprocess.run(["/bin/zsh", str(script), "--unknown"], env=env, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue((base / "packages/common").is_dir())
        self.assertFalse((self.root / "base-1.0").exists())
        self.assertFalse((self.root / "base-2.0").exists())


if __name__ == "__main__":
    unittest.main()
