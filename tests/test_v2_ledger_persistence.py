"""Ledger CAS persistence must survive batch-scale contention.

第 2 波（12 个并行 run）实测：乐观锁仅 3 次硬重试、无退避，且未把 GitHub
二级限流（403）/瞬态 5xx 归类为可重试，导致正常收账被打死。本文件锁定
退避重试与瞬态分类契约。
"""

import base64
import importlib.util
import os
import json
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "runtime/ci/build_state.py"
    spec = importlib.util.spec_from_file_location("build_state_persistence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LedgerPersistenceRetryTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.record = {"runtime_version": "v2", "kind": "package", "name": "nginx",
                       "version": "1.31.8", "os": "macos", "arch": "arm64",
                       "build_revision": 0}
        self.sleeps = []
        self.sleep = self.sleeps.append

    def test_conflicts_retry_with_backoff_until_the_write_lands(self):
        conflicts = {"n": 0}

        def flaky_write(sha, state):
            if conflicts["n"] < 3:
                conflicts["n"] += 1
                raise self.module.StateConflict("The Builder ledger changed concurrently")

        result = self.module._persist(lambda state: {"state": {**state, "touched": True}},
                                      lambda: ("sha-0", {}),
                                      flaky_write, sleep=self.sleep)
        self.assertTrue(result["state"]["touched"])
        self.assertEqual([1, 2, 4], self.sleeps, "backoff must grow 1/2/4s across retries")

    def test_secondary_rate_limit_is_transient_and_retried(self):
        failures = {"n": 0}

        def rate_limited_write(sha, state):
            if failures["n"] < 2:
                failures["n"] += 1
                # write_state 分类后的形态（store 层分类另有专门用例覆盖）
                raise self.module.LedgerTransient(
                    "The ledger write hit a retryable transport failure: "
                    "gh: API secondary rate limit (HTTP 403)")

        result = self.module._persist(lambda state: {"state": {**state, "touched": True}},
                                      lambda: ("sha-0", {}),
                                      rate_limited_write, sleep=self.sleep)
        self.assertTrue(result["state"]["touched"])
        self.assertEqual([1, 2], self.sleeps)

    def test_permanent_token_failure_still_fails_fast_without_sleeping(self):
        def forbidden_write(sha, state):
            raise RuntimeError("Could not persist the Builder ledger with its repository token")

        with self.assertRaises(RuntimeError):
            self.module._persist(lambda state: {"state": {**state, "touched": True}},
                                 lambda: ("sha-0", {}),
                                 forbidden_write, sleep=self.sleep)
        self.assertEqual([], self.sleeps)

    def test_exhausted_conflicts_raise_after_the_final_attempt(self):
        def always_conflict(sha, state):
            raise self.module.StateConflict("contention")

        with self.assertRaises(self.module.StateConflict):
            self.module._persist(lambda state: {"state": {**state, "touched": True}},
                                 lambda: ("sha-0", {}),
                                 always_conflict, attempts=3, sleep=self.sleep)
        self.assertEqual([1, 2], self.sleeps, "3 attempts mean exactly two backoff sleeps")

    def test_github_store_classifies_secondary_limit_as_transient(self):
        ledger = json.dumps({"schema_version": 1, "units": {}})
        contents = {"sha": "ledger-sha", "content": base64.b64encode(ledger.encode()).decode()}

        def fake_run(command, **kwargs):
            if "--method" in command:
                return mock.Mock(returncode=1, stdout="",
                                 stderr="gh: API secondary rate limit (HTTP 403)")
            return mock.Mock(returncode=0, stdout=json.dumps(contents), stderr="")

        with mock.patch.dict(os.environ, {"GH_TOKEN": "runner-token"}):
            with mock.patch.object(self.module.subprocess, "run", fake_run):
                read_state, write_state = self.module.github_state_store(
                    "ServBay/ServBay-PKG-Builder-v1", "runtime-v2/config/build-state.json")
                with self.assertRaises(self.module.LedgerTransient):
                    write_state("ledger-sha", {"schema_version": 1, "units": {}})

