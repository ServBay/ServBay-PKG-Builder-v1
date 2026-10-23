import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class CollectionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("state_collection", ROOT / "runtime/ci/build_state.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.assertTrue(hasattr(self.module, "collect_results"), "The pinned artifact collector is missing")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.record = {"runtime_version": "v2", "os": "macos", "kind": "package", "name": "nginx",
                       "version": "1.31.7", "arch": "x86_64", "build_revision": 0, "build_type": "source"}
        self.producer = {"commit": "a" * 40, "run_id": "12345", "run_attempt": 2}
        self.state = self.module.plan_pending({"schema_version": 1, "units": {}}, [self.record])["state"]

    def artifact(self, artifact_id=1, producer=None, package=b"real archive bytes", recorded_hash=None):
        meta = dict(self.record, schema_version=2, filename="nginx-1.31.7.tar.gz",
                    producer=producer or self.producer, sha256=recorded_hash or hashlib.sha256(package).hexdigest())
        path = self.root / f"{artifact_id}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("meta.json", json.dumps(meta))
            archive.writestr(meta["filename"], package)
        return {"id": artifact_id, "name": self.module.artifact_name(meta), "expired": False,
                "expires_at": "2026-10-28T05:45:43Z", "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}

    def collect(self, artifacts, records=None):
        return self.module.collect_results(self.state, records or [self.record], artifacts, self.producer, self.root)

    def test_successful_artifact_survives_another_matrix_failure(self):
        other = dict(self.record, arch="arm64")
        self.state = self.module.plan_pending(self.state, [other])["state"]
        result = self.collect([self.artifact()], [self.record, other])
        units = result["state"]["units"]
        self.assertEqual("build-ready", units[self.module.identity(self.record)]["status"])
        self.assertEqual("build-failed", units[self.module.identity(other)]["status"])
        ready = units[self.module.identity(self.record)]
        self.assertEqual("1", ready["artifact_id"])
        self.assertEqual("2026-10-28T05:45:43Z", ready["artifact_expires_at"])
        self.assertEqual(1, result["ready_count"])

    def test_previous_run_attempt_is_not_borrowed(self):
        result = self.collect([self.artifact(producer=dict(self.producer, run_attempt=1))])
        self.assertEqual(0, result["ready_count"])
        self.assertEqual("build-failed", result["state"]["units"][self.module.identity(self.record)]["status"])

    def test_corrupt_package_and_corrupt_artifact_zip_are_rejected(self):
        first = self.artifact(recorded_hash="0" * 64)
        self.assertEqual(0, self.collect([first])["ready_count"])
        second = self.artifact()
        second["digest"] = "sha256:" + "0" * 64
        self.assertEqual(0, self.collect([second])["ready_count"])

    def test_duplicate_current_attempt_and_expired_artifacts_do_not_mark_ready(self):
        self.assertEqual(0, self.collect([self.artifact(1), self.artifact(2)])["ready_count"])
        artifact = self.artifact()
        artifact["expired"] = True
        self.assertEqual(0, self.collect([artifact])["ready_count"])

    def test_collecting_the_same_failed_attempt_twice_counts_only_one_failure(self):
        first = self.collect([])
        second = self.module.collect_results(first["state"], [self.record], [], self.producer, self.root)
        self.assertEqual(1, second["state"]["units"][self.module.identity(self.record)]["consecutive_failures"])

    def planner(self):
        spec = importlib.util.spec_from_file_location("collection_workflow_profile", ROOT / "runtime/ci/build_profile.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_deferred_workflow_units_remain_discovered_without_failure_increments(self):
        records = [dict(self.record, name=f"package-{number}") for number in range(257)]
        initial = self.module.plan_pending({"schema_version": 1, "units": {}}, records)["state"]
        scheduled = self.planner().workflow_plan(records)
        result = self.module.collect_results(initial, scheduled["records"], [], self.producer, self.root)
        deferred = result["state"]["units"][self.module.identity(records[-1])]
        self.assertEqual("discovered", deferred["status"])
        self.assertEqual(0, deferred["consecutive_failures"])
        self.assertEqual(256, len(result["failed"]))

    def test_grouped_partial_success_retries_only_the_failed_architecture(self):
        records = [self.record, dict(self.record, arch="arm64")]
        initial = self.module.plan_pending(self.state, records)["state"]
        scheduled = self.planner().workflow_plan(records)
        self.assertEqual(["all"], [row["arch"] for row in scheduled["matrix"]["include"]])
        result = self.module.collect_results(initial, scheduled["records"], [self.artifact()], self.producer, self.root)
        self.assertEqual("build-ready", result["state"]["units"][self.module.identity(self.record)]["status"])
        pending = self.module.plan_pending(result["state"], records)["include"]
        retry = self.planner().workflow_plan(pending)
        self.assertEqual(["arm64"], [row["arch"] for row in retry["matrix"]["include"]])
        self.assertEqual([records[1]], retry["records"])

    def test_state_write_conflict_reloads_the_other_producer_changes(self):
        self.assertTrue(hasattr(self.module, "persist_results"), "The same-repository compare-and-swap writer is missing")
        other = dict(self.record, arch="arm64")
        newer = self.module.plan_pending(self.state, [other])["state"]
        reads = iter([("first-sha", self.state), ("second-sha", newer)])
        writes = []

        def write_state(sha, state):
            writes.append((sha, state))
            if len(writes) == 1:
                raise self.module.StateConflict("another build finished")

        result = self.module.persist_results([self.record], [], self.producer, self.root,
                                              lambda: next(reads), write_state)
        self.assertEqual(["first-sha", "second-sha"], [entry[0] for entry in writes])
        self.assertIn(self.module.identity(other), result["state"]["units"])
        self.assertEqual(1, result["state"]["units"][self.module.identity(self.record)]["consecutive_failures"])


if __name__ == "__main__":
    unittest.main()
