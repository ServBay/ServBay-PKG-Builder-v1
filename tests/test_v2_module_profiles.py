import copy
import importlib.util
import json
from pathlib import Path
import unittest

from test_v2_modules import BASE, PHP_ABI

ROOT = Path(__file__).resolve().parents[1]


class ModuleProfileTests(unittest.TestCase):
    def helper(self):
        spec = importlib.util.spec_from_file_location("tested_module_profiles", ROOT / "runtime-v2/ci/module_profiles.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def php(self, series="8.4"):
        return {"type": "php", "series": series, "version": series + ".27", "package_sha256": "c" * 64,
                "abi": copy.deepcopy(PHP_ABI)}

    def test_actual_php_profile_uses_canonical_names_and_only_existing_third_party_defaults(self):
        rows = self.helper().module_records(self.php(), "x86_64")
        by_name = {row["name"]: row for row in rows}
        self.assertEqual({"redis", "memcache", "memcached"}, {row["name"] for row in rows if row["default"]})
        self.assertNotIn("opcache", by_name)
        self.assertFalse(by_name["imagick"]["default"])
        self.assertEqual("phpredis", by_name["redis"]["recipe_name"])
        self.assertEqual("6.3.0", by_name["redis"]["version"])
        self.assertEqual("3.8.1", by_name["imagick"]["version"])
        self.assertEqual("8.2", by_name["memcache"]["version"])
        self.assertEqual("3.4.0", by_name["memcached"]["version"])
        self.assertTrue(all(row["host"]["abi"] == PHP_ABI for row in rows))

    def test_old_host_support_floor_and_phalcon_psr_ownership_follow_the_real_recipe_matrix(self):
        helper = self.helper()
        php80 = {row["name"]: row for row in helper.module_records(self.php("8.0"), "x86_64")}
        self.assertEqual("3.4.3", php80["xdebug"]["version"])
        old = {row["name"]: row for row in helper.module_records(self.php("5.3"), "arm64")}
        self.assertEqual("4.3.0", old["redis"]["version"])
        self.assertNotIn("phalcon", old)
        php56 = {row["name"]: row for row in helper.module_records(self.php("5.6"), "arm64")}
        self.assertEqual({}, php56["phalcon"]["bundled"])
        php74 = {row["name"]: row for row in helper.module_records(self.php("7.4"), "arm64")}
        self.assertEqual({"psr": "1.1.0"}, php74["phalcon"]["bundled"])
        self.assertEqual({}, php74["redis"]["bundled"])

    def test_postgresql_profiles_keep_supported_hosts_and_distinguish_vector_source_from_release_name(self):
        helper = self.helper()
        def host(major):
            return {"type": "postgresql", "series": str(major), "version": str(major) + ".6",
                    "package_sha256": "c" * 64, "abi": {"major": major}}
        self.assertNotIn("vector", {r["name"] for r in helper.module_records(host(12), "x86_64")})
        current = {r["name"]: r for r in helper.module_records(host(18), "x86_64")}
        self.assertEqual("pgvector", current["vector"]["recipe_name"])
        self.assertEqual("0.8.7", current["vector"]["version"])
        self.assertTrue(all(not r["default"] for r in current.values()))

    def test_public_catalog_reuse_stays_an_explicit_candidate_for_uploader_confirmation(self):
        helper = self.helper()
        record = next(r for r in helper.module_records(self.php(), "x86_64") if r["name"] == "redis")
        manifest = {"schema_version": 2, "arch": "x86_64", "minimum_os": "12.0.0", "base": copy.deepcopy(BASE),
                    "host": self.php(), "source_name": "phpredis", "files": [{"scope":"host","archive_path":"redis.so","install_path":"lib/extensions/redis.so","mode":"0755","type":"file","sha256":"e"*64}], "config_files": [],
                    "provides": ["redis"], "dependencies": []}
        row = {"name": "php-ext", "extName": "redis", "hostVersion": "8.4", "version": "6.3.0", "arch": "x86_64",
               "os": "macOS", "runtimeVersion": "v2", "buildRevision": 0, "hash": "d" * 64,
               "downloadUrl": "https://downloads.example.invalid/redis.tar.gz", "config": {"module_manifest": manifest}}
        plan = helper.select_modules([record], [row], BASE, "12.0")
        self.assertEqual([], plan["build"])
        self.assertEqual("requires_uploader_confirmation", plan["external_candidates"][0]["publication_status"])
        self.assertNotIn("published", plan["external_candidates"][0])
        self.assertEqual("d" * 64, plan["references"][0]["sha256"])
        self.assertEqual(record["host"], plan["references"][0]["host"])
        for field, value in (("arch", "arm64"), ("minimum_os", "13.0"), ("base", {**BASE, "runtime_sha256": "f" * 64})):
            with self.subTest(field=field):
                wrong = copy.deepcopy(row)
                wrong["config"]["module_manifest"][field] = value
                self.assertEqual([record], helper.select_modules([record], [wrong], BASE, "12.0")["build"])

    def actual_catalog_reuse(self, host_type):
        fixture = json.loads((ROOT / "tests/fixtures/module-catalog-api.json").read_text())
        self.assertEqual("53cec64d23355ee243a000cdd82a9b86f8ab13ec3cb82c531c41f58c37649f99", fixture["source_sha256"])
        response = fixture[host_type]
        self.assertEqual(200, response["status"])
        helper = self.helper()
        catalog = helper.catalog_rows(response["response"])
        first = catalog[0]["config"]["module_manifest"]
        host = copy.deepcopy(first["host"])
        # A new core patch release reuses compatible modules built from an older
        # exact core, while retaining the actual module source provenance.
        host["version"] = "8.4.28" if host_type == "php" else "18.7"
        host["package_sha256"] = "1" * 64
        rows = helper.module_records(host, first["arch"])
        expected = {row["extName"] for row in catalog}
        planned = helper.select_modules(rows, catalog, first["base"], first["minimum_os"])
        self.assertEqual(expected, {row["record"]["name"] for row in planned["external_candidates"]})
        self.assertTrue(expected.isdisjoint({row["name"] for row in planned["build"]}))
        self.assertEqual({row["hash"] for row in catalog}, {row["sha256"] for row in planned["references"]})
        for item in planned["external_candidates"]:
            self.assertEqual("requires_uploader_confirmation", item["publication_status"])
            self.assertNotIn("published", item)
            self.assertNotEqual(host["package_sha256"], item["module_manifest"]["host"]["package_sha256"])
        wrong = copy.deepcopy(catalog)
        for row in wrong:
            row["name"] = host_type
        self.assertEqual([], helper.select_modules(rows, wrong, first["base"], first["minimum_os"])["external_candidates"])

    def test_actual_php_api_namespace_reuses_existing_modules_for_a_new_compatible_core(self):
        self.actual_catalog_reuse("php")

    def test_actual_pg_api_namespace_reuses_existing_modules_for_a_new_compatible_core(self):
        self.actual_catalog_reuse("postgresql")

    def test_catalog_failure_is_not_treated_as_an_empty_successful_list(self):
        helper = self.helper()
        for payload in ({"status_code": 500}, {"status_code": 200, "data": {"extensions": None}}, {}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                helper.catalog_rows(payload)
        self.assertEqual([], helper.catalog_rows({"status_code": 200, "message": "Success", "data": {"extensions": []}}))
