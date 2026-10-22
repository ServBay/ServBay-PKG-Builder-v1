import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BuildStateTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime/ci/build_state.py"
        self.assertTrue(path.is_file(), "The persistent build-state implementation is missing")
        spec = importlib.util.spec_from_file_location("build_state", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.record = {"runtime_version": "v2", "os": "macos", "kind": "package",
                       "name": "nginx", "version": "1.31.7", "arch": "x86_64",
                       "build_type": "source", "build_revision": 0}
        self.empty = {"schema_version": 1, "units": {}}

    def test_new_desired_version_is_discovered_without_mutating_input(self):
        plan = self.module.plan_pending(self.empty, [self.record])
        self.assertEqual([self.record], plan["include"])
        self.assertEqual({}, self.empty["units"])
        unit = plan["state"]["units"][self.module.identity(self.record)]
        self.assertEqual("discovered", unit["status"])
        self.assertEqual(0, unit["consecutive_failures"])

    def test_existing_failed_version_is_pending_even_without_a_new_version(self):
        planned = self.module.plan_pending(self.empty, [self.record])["state"]
        failed = self.module.record_result(planned, self.module.identity(self.record),
                                           {"status": "build-failed", "error": "compiler failed"})
        self.assertEqual([self.record], self.module.plan_pending(failed, [])['include'])
        self.assertEqual("discovered", planned["units"][self.module.identity(self.record)]["status"])

    def test_three_consecutive_failures_stop_auto_retry(self):
        state = self.module.plan_pending(self.empty, [self.record])["state"]
        key = self.module.identity(self.record)
        for attempt in range(1, 4):
            state = self.module.record_result(state, key, {"status": "build-failed", "error": "compiler failed"})
            self.assertEqual(attempt, state["units"][key]["consecutive_failures"])
            plan = self.module.plan_pending(state, [self.record])
            self.assertEqual([self.record] if attempt < 3 else [], plan["include"])
        self.assertEqual([key], plan["blocked"])

    def test_planning_again_does_not_increment_actual_failure_count(self):
        state = self.module.plan_pending(self.empty, [self.record])["state"]
        first = copy.deepcopy(state)
        for _ in range(3):
            state = self.module.plan_pending(state, [self.record])["state"]
        self.assertEqual(first, state)

    def test_build_ready_keeps_artifact_zip_and_package_hashes_distinct(self):
        state = self.module.plan_pending(self.empty, [self.record])["state"]
        key = self.module.identity(self.record)
        result = {"status": "build-ready", "producer": {"commit": "a" * 40, "run_id": "12345", "run_attempt": 2},
                  "artifact_id": "9876", "artifact_name": "pkg2-macos-x86_64-package-nginx-1.31.7",
                  "artifact_zip_sha256": "b" * 64, "package_sha256": "c" * 64,
                  "artifact_expires_at": "2026-10-28T05:45:43Z"}
        ready = self.module.record_result(state, key, result)
        unit = ready["units"][key]
        self.assertEqual("b" * 64, unit["artifact_zip_sha256"])
        self.assertEqual("c" * 64, unit["package_sha256"])
        self.assertEqual(result["producer"], unit["producer"])
        self.assertEqual("2026-10-28T05:45:43Z", unit["artifact_expires_at"])
        self.assertEqual([], self.module.plan_pending(ready, [self.record])["include"])

    def test_build_ready_without_pinned_artifact_is_rejected(self):
        state = self.module.plan_pending(self.empty, [self.record])["state"]
        with self.assertRaises(ValueError):
            self.module.record_result(state, self.module.identity(self.record), {"status": "build-ready"})

    def test_full_identity_separates_runtime_architecture_revision_and_host(self):
        changed = []
        for field, value in (("runtime_version", "v1"), ("arch", "arm64"), ("build_revision", 1)):
            row = dict(self.record, **{field: value})
            changed.append(self.module.identity(row))
        self.assertEqual(4, len(set([self.module.identity(self.record), *changed])))
        first = dict(self.record, kind="php-extension", name="redis",
                     host={"type": "php", "series": "8.4", "abi": {"module_api": "20240924"}})
        second = copy.deepcopy(first)
        second["host"]["series"] = "8.5"
        self.assertNotEqual(self.module.identity(first), self.module.identity(second))

    def test_unknown_result_status_is_rejected_without_mutating_state(self):
        state = self.module.plan_pending(self.empty, [self.record])["state"]
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            self.module.record_result(state, self.module.identity(self.record), {"status": "published"})
        self.assertEqual(before, state)

    def test_module_identity_uses_compatibility_not_exact_host_build(self):
        first = dict(self.record, kind="php-extension", name="redis",
                     host={"type": "php", "series": "8.4", "version": "8.4.19",
                           "package_sha256": "a" * 64, "abi": {"module_api": "20240924"}})
        second = copy.deepcopy(first)
        second["host"].update(version="8.4.20", package_sha256="b" * 64)
        self.assertEqual(self.module.identity(first), self.module.identity(second))

    def test_ledger_contains_no_upload_address_from_an_input_record(self):
        row = dict(self.record, upload_url="private-publication-target")
        state = self.module.plan_pending(self.empty, [row])["state"]
        self.assertNotIn("private-publication-target", json.dumps(state))

    def test_discovery_persists_with_the_repository_compare_and_swap_interface(self):
        self.assertTrue(hasattr(self.module, "persist_plan"), "Discovery must be durable before matrix execution")
        saved = []
        result = self.module.persist_plan([self.record], lambda: ("current-sha", self.empty),
                                           lambda sha, state: saved.append((sha, state)))
        self.assertEqual([self.record], result["include"])
        self.assertEqual("current-sha", saved[0][0])
        self.assertEqual("discovered", saved[0][1]["units"][self.module.identity(self.record)]["status"])


if __name__ == "__main__":
    unittest.main()
