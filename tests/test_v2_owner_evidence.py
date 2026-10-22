from datetime import datetime, timedelta, timezone
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class OwnerEvidenceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("owner_state", ROOT / "runtime/ci/build_state.py")
        self.state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.state)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = dict(commit="a" * 40, run_id="12345", run_attempt=2)
        self.row = dict(runtime_version="v2", os="macos", kind="package", name="nginx", version="1.31.7", arch="arm64", build_revision=0)
        self.run = dict(id=12345, run_attempt=2, head_sha="a" * 40, status="completed")
        self.calls = []

    def api(self, command, **kwargs):
        self.calls.append(command)
        endpoint = command[2]
        if endpoint.endswith('/zip'):
            kwargs['stdout'].write(b'fixture zip bytes')
            return SimpleNamespace(returncode=0)
        if '/jobs?' in endpoint:
            body = dict(jobs=[])
        elif '/artifacts?' in endpoint:
            body = dict(artifacts=[dict(id=77, name=self.state.artifact_name(self.row), expired=False,
                                      workflow_run=dict(id=12345, head_sha='a' * 40))])
        else:
            body = self.run
        return SimpleNamespace(stdout=json.dumps(body), returncode=0)

    def test_exact_attempt_endpoints_and_artifact_readback_use_checked_calls(self):
        with mock.patch.object(self.state.subprocess, 'run', side_effect=self.api) as api:
            snapshot = self.state.github_owner_snapshot('ServBay/Builder', self.owner, self.root)
        self.assertEqual(4, len(self.calls))
        self.assertEqual('repos/ServBay/Builder/actions/runs/12345/attempts/2', self.calls[0][2])
        self.assertIn('/attempts/2/jobs?', self.calls[1][2])
        self.assertEqual(b'fixture zip bytes', (self.root / '77.zip').read_bytes())
        self.assertTrue(all(call.kwargs.get('check') is True for call in api.call_args_list))
        self.assertEqual(1, len(snapshot['artifacts']))

    def test_unknown_run_source_or_attempt_never_reads_jobs_or_artifacts(self):
        for field, value in (('id', 999), ('run_attempt', 1), ('head_sha', 'b' * 40)):
            with self.subTest(field=field):
                self.calls = []
                original = self.run
                self.run = dict(original, **{field: value})
                with mock.patch.object(self.state.subprocess, 'run', side_effect=self.api), self.assertRaises(ValueError):
                    self.state.github_owner_snapshot('ServBay/Builder', self.owner, self.root)
                self.assertEqual(1, len(self.calls))
                self.run = original

    def test_active_expired_run_is_held_and_artifact_source_is_checked(self):
        self.run['status'] = 'in_progress'
        with mock.patch.object(self.state.subprocess, 'run', side_effect=self.api):
            snapshot = self.state.github_owner_snapshot('ServBay/Builder', self.owner, self.root)
        self.assertEqual([], snapshot['jobs'])
        self.assertEqual(1, len(self.calls))
        self.run['status'] = 'completed'
        real_api = self.api

        def wrong_artifact(command, **kwargs):
            result = real_api(command, **kwargs)
            if '/artifacts?' in command[2]:
                body = json.loads(result.stdout)
                body['artifacts'][0]['workflow_run']['head_sha'] = 'b' * 40
                result.stdout = json.dumps(body)
            return result

        with mock.patch.object(self.state.subprocess, 'run', side_effect=wrong_artifact), self.assertRaises(ValueError):
            self.state.github_owner_snapshot('ServBay/Builder', self.owner, self.root)
        self.assertFalse((self.root / '77.zip').exists())

    def test_download_failure_keeps_expired_owner_and_memoizes_across_cas_retry(self):
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        old = self.state.reserve_records(dict(schema_version=1, units={}), [self.row], self.owner,
                                        now=now - timedelta(hours=10))['state']
        new_owner = dict(commit='b' * 40, run_id='12346', run_attempt=1)
        writes = []
        # An unrelated new unit forces a state write and one CAS retry.
        other = dict(self.row, name='redis')

        def write(sha, state):
            writes.append(state)
            if len(writes) == 1:
                raise self.state.StateConflict('race')

        with mock.patch.object(self.state, 'github_owner_snapshot', side_effect=subprocess.CalledProcessError(1, ['gh'])) as api:
            result = self.state.reserve_github([self.row, other], new_owner, 'ServBay/Builder', self.root,
                                               lambda: ('sha', copy.deepcopy(old)), write, now=now)
        self.assertEqual(1, api.call_count)
        self.assertEqual([other], result['include'])
        key = self.state.identity(self.row)
        self.assertEqual(old['units'][key], result['state']['units'][key])
        self.assertTrue(result['diagnostics'])

    def test_complete_inventory_without_a_unit_job_proves_no_compile_attempt(self):
        self.assertEqual('finished', self.state.job_outcomes([self.row], self.owner, [])[self.state.identity(self.row)])

    def test_terminal_job_without_compile_never_spends_a_failure(self):
        for conclusion in ('failure', 'cancelled', 'skipped', 'timed_out'):
            jobs = [dict(name='caller / ' + self.state.unit_job_name(self.row), head_sha='a' * 40,
                         status='completed', conclusion=conclusion, steps=[])]
            self.assertEqual('finished', self.state.job_outcomes([self.row], self.owner, jobs)[self.state.identity(self.row)])
        jobs[0]['head_sha'] = 'b' * 40
        self.assertEqual('held', self.state.job_outcomes([self.row], self.owner, jobs)[self.state.identity(self.row)])


if __name__ == '__main__':
    unittest.main()
