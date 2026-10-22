from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]


class ReservationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("reservation_state", ROOT / "runtime/ci/build_state.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.assertTrue(hasattr(self.module, "reserve_records"), "Paid jobs need atomic identity reservations")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        self.record = dict(runtime_version="v2", os="macos", kind="package", name="nginx", version="1.31.7",
                           arch="x86_64", build_revision=0, build_type="source")
        self.first = dict(commit="a" * 40, run_id="12345", run_attempt=1)
        self.second = dict(commit="b" * 40, run_id="12346", run_attempt=1)
        self.empty = dict(schema_version=1, units={})

    def reserve(self, state=None, records=None, producer=None, now=None):
        return self.module.reserve_records(self.empty if state is None else state, records or [self.record],
                                           producer or self.first, now=now or self.now)

    def expired(self, records=None):
        return self.reserve(records=records, now=self.now - timedelta(hours=10))["state"]

    def snapshot(self, records=None, conclusion="failure", step="failure", status="completed", artifacts=None):
        rows = records or [self.record]
        return {"run": dict(id=12345, run_attempt=1, head_sha=self.first["commit"], status=status, conclusion=conclusion),
                "jobs": [dict(name=self.module.unit_job_name(row), status=status, conclusion=conclusion,
                              head_sha=self.first["commit"], steps=[dict(name="Compile Runtime v2 package", status="completed", conclusion=step)])
                         for row in rows], "artifacts": artifacts or [], "artifact_dir": self.root}

    def reconcile(self, state, snapshot):
        return self.module.reconcile_reservations(state, self.second, lambda producer: snapshot, now=self.now)

    def artifact(self):
        filename = "nginx-1.31.7.tar.gz"
        package = b"actual fixture package bytes"
        meta = dict(self.record, schema_version=2, filename=filename, producer=self.first, sha256=hashlib.sha256(package).hexdigest())
        path = self.root / "77.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("meta.json", json.dumps(meta))
            archive.writestr(filename, package)
        return dict(id=77, name=self.module.artifact_name(self.record), expired=False, expires_at="2026-10-28T12:00:00Z",
                    workflow_run=dict(id=12345, head_sha=self.first["commit"]), digest="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())

    def test_duplicate_producer_skips_but_same_owner_child_can_consume_its_reservation(self):
        claimed = self.reserve()
        key = self.module.identity(self.record)
        self.assertEqual([self.record], claimed["include"])
        self.assertEqual("discovered", claimed["state"]["units"][key]["status"])
        self.assertEqual(0, claimed["state"]["units"][key]["consecutive_failures"])
        self.assertEqual([], self.reserve(claimed["state"], producer=self.second)["include"])
        same = self.reserve(claimed["state"])
        self.assertEqual([self.record], same["include"])
        self.assertEqual(claimed["state"], same["state"])

    def test_cas_retry_observes_the_winning_other_owner_before_reserving(self):
        other = self.reserve(producer=self.second)["state"]
        reads = iter([("old", self.empty), ("new", other)])
        writes = []

        def write(sha, state):
            writes.append(sha)
            raise self.module.StateConflict("another request reserved the same unit")

        result = self.module.persist_reservations([self.record], self.first, lambda: next(reads), write, now=self.now)
        self.assertEqual([], result["include"])
        self.assertEqual(["old"], writes)
        self.assertEqual(other, result["state"])

    def test_arch_revision_and_actual_module_abi_have_distinct_reservations(self):
        module = dict(self.record, kind="php-extension", name="redis", host=dict(type="php", series="8.4", abi=dict(module_api=20240924)))
        rows = [self.record, dict(self.record, arch="arm64"), dict(self.record, build_revision=1), module,
                dict(module, host=dict(type="php", series="8.4", abi=dict(module_api=20250925)))]
        result = self.reserve(records=rows)
        self.assertEqual(5, len(result["include"]))
        self.assertEqual(5, len(result["state"]["units"]))
        with self.assertRaises(ValueError):
            self.reserve(records=[dict(self.record, runtime_version="v1")])

    def test_expired_reservation_is_not_reassigned_without_authoritative_recovery(self):
        old = self.expired()
        self.assertEqual([], self.reserve(old, producer=self.second)["include"])
        self.assertEqual(old, self.reserve(old, producer=self.second)["state"])

    def test_unexpired_owner_is_not_polled_or_released(self):
        old = self.reserve()["state"]
        calls = []
        result = self.module.reconcile_reservations(old, self.second, lambda owner: calls.append(owner), now=self.now)
        self.assertEqual([], calls)
        self.assertEqual(old, result["state"])

    def test_two_architectures_keep_real_success_and_retry_only_failed_compile(self):
        rows = [self.record, dict(self.record, arch="arm64")]
        old = self.expired(rows)
        snapshot = self.snapshot(rows, artifacts=[self.artifact()])
        snapshot["jobs"][0].update(conclusion="success")
        snapshot["jobs"][0]["steps"][0]["conclusion"] = "success"
        result = self.reconcile(old, snapshot)
        self.assertEqual("build-ready", result["state"]["units"][self.module.identity(rows[0])]["status"])
        self.assertEqual("build-failed", result["state"]["units"][self.module.identity(rows[1])]["status"])
        retry = self.reserve(result["state"], records=rows, producer=self.second)
        self.assertEqual([rows[1]], retry["include"])

    def test_expired_still_running_or_queued_owner_remains_held(self):
        old = self.expired()
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                snapshot = self.snapshot(status=status)
                # The API adapter deliberately stops after an active run and returns no job inventory.
                snapshot["jobs"] = []
                result = self.reconcile(old, snapshot)
                self.assertEqual(old, result["state"])
                self.assertTrue(result["diagnostics"])

    def test_lost_api_or_wrong_attempt_and_head_cannot_release_unknown_work(self):
        old = self.expired()
        for field, value in (("run_attempt", 2), ("head_sha", "b" * 40), ("id", 99999)):
            with self.subTest(field=field):
                snapshot = self.snapshot()
                snapshot["run"][field] = value
                result = self.reconcile(old, snapshot)
                self.assertEqual(old, result["state"])
                self.assertTrue(result["diagnostics"])

        def unavailable(producer):
            raise RuntimeError("GitHub API unavailable")

        result = self.module.reconcile_reservations(old, self.second, unavailable, now=self.now)
        self.assertEqual(old, result["state"])
        self.assertTrue(result["diagnostics"])

    def test_terminal_failed_compile_releases_once_and_keeps_the_three_failure_limit(self):
        old = self.expired()
        key = self.module.identity(self.record)
        old["units"][key]["consecutive_failures"] = 2
        result = self.reconcile(old, self.snapshot())
        unit = result["state"]["units"][key]
        self.assertEqual("build-failed", unit["status"])
        self.assertEqual(3, unit["consecutive_failures"])
        self.assertNotIn("reservation", unit)
        self.assertEqual([], self.reserve(result["state"], producer=self.second)["include"])
        self.assertEqual(result["state"], self.reconcile(result["state"], self.snapshot())["state"])

    def test_timed_out_real_compile_counts_but_precompile_timeout_does_not(self):
        for step, count in (("timed_out", 1), ("skipped", 0)):
            with self.subTest(step=step):
                result = self.reconcile(self.expired(), self.snapshot(conclusion="timed_out", step=step))
                unit = result["state"]["units"][self.module.identity(self.record)]
                self.assertEqual(count, unit["consecutive_failures"])
                self.assertNotIn("reservation", unit)

    def test_cancelled_or_precompile_config_failure_does_not_increment_compile_failures(self):
        for conclusion, step in (("cancelled", "cancelled"), ("failure", "skipped")):
            with self.subTest(conclusion=conclusion):
                old = self.expired()
                key = self.module.identity(self.record)
                old["units"][key]["consecutive_failures"] = 2
                result = self.reconcile(old, self.snapshot(conclusion=conclusion, step=step))
                unit = result["state"]["units"][key]
                self.assertEqual("discovered", unit["status"])
                self.assertEqual(2, unit["consecutive_failures"])
                self.assertNotIn("reservation", unit)
                self.assertEqual([self.record], self.reserve(result["state"], producer=self.second)["include"])

    def test_successful_old_artifact_is_recovered_instead_of_recompiling(self):
        old = self.expired()
        result = self.reconcile(old, self.snapshot(conclusion="success", step="success", artifacts=[self.artifact()]))
        unit = result["state"]["units"][self.module.identity(self.record)]
        self.assertEqual("build-ready", unit["status"])
        self.assertNotIn("reservation", unit)
        self.assertEqual([], self.reserve(result["state"], producer=self.second)["include"])

    def test_artifact_readback_failure_holds_the_reservation(self):
        old = self.expired()
        artifact = self.artifact()
        (self.root / "77.zip").write_bytes(b"broken readback")
        result = self.reconcile(old, self.snapshot(artifacts=[artifact]))
        self.assertEqual(old, result["state"])
        self.assertTrue(result["diagnostics"])

    def test_recovery_queries_once_per_producer_for_two_architectures(self):
        rows = [self.record, dict(self.record, arch="arm64")]
        old = self.expired(rows)
        calls = []

        def read(producer):
            calls.append(producer)
            return self.snapshot(rows)

        result = self.module.reconcile_reservations(old, self.second, read, now=self.now)
        self.assertEqual([self.first], calls)
        self.assertTrue(all(unit["status"] == "build-failed" for unit in result["state"]["units"].values()))

    def test_late_old_collector_cannot_replace_an_already_ready_other_producer(self):
        artifact = self.artifact()
        initial = self.module.plan_pending(self.empty, [self.record])["state"]
        first_ready = self.module.collect_results(initial, [self.record], [artifact], self.first, self.root)["state"]
        other_ready = __import__('copy').deepcopy(first_ready)
        other_ready["units"][self.module.identity(self.record)]["producer"] = self.second
        result = self.module.collect_results(other_ready, [self.record], [artifact], self.first, self.root)
        self.assertEqual(other_ready, result["state"])
        self.assertEqual(0, result["ready_count"])

    def test_late_old_collector_cannot_overwrite_another_owners_reservation(self):
        new = self.reserve(producer=self.second)["state"]
        result = self.module.collect_results(new, [self.record], [], self.first, self.root)
        self.assertEqual(new, result["state"])


if __name__ == "__main__":
    unittest.main()
