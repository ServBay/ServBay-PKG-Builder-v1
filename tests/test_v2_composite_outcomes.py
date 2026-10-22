import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'tests/fixtures/v2-composite-jobs.json').read_text())['cases']


class CompositeOutcomeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('composite_state', ROOT / 'runtime/ci/build_state.py')
        self.state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.state)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def inputs(self, case):
        record, producer, jobs = (copy.deepcopy(case[key]) for key in ('record', 'producer', 'jobs'))
        self.assertEqual(self.state.unit_job_name(record), jobs[0]['name'])
        prefix = 'Finalize ' if record['kind'] == 'package' else 'Bind '
        self.assertEqual(prefix + jobs[0]['name'], jobs[1]['name'])
        return record, producer, jobs

    def collect(self, record, producer, jobs, ledger=None, artifacts=None):
        if ledger is None:
            ledger = self.state.reserve_records({'schema_version': 1, 'units': {}}, [record], producer)['state']
        outcomes = self.state.job_outcomes([record], producer, jobs)
        result = self.state.collect_results(ledger, [record], artifacts or [], producer, self.root, outcomes)
        return result, result['state']['units'][self.state.identity(record)]

    def artifact(self, record, producer, candidate=False, corrupt=False):
        filename = 'php-8.4.27.tar.gz' if record['kind'] == 'package' else 'ext-pgrouting-3.8.0-pgsql18-x86_64.tar.gz'
        package = b'controlled native package byte fixture'
        meta = dict(record, schema_version=2, producer=producer, filename=filename,
                    sha256=hashlib.sha256(package).hexdigest())
        path = self.root / '7.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('candidate.json' if candidate else 'meta.json', json.dumps(meta))
            archive.writestr(filename, package)
        name = self.state.artifact_name(record)
        if candidate:
            if record['kind'] == 'package':
                name = 'candidate-core-macos-' + record['arch'] + '-' + record['name'] + '-' + record['version']
            else:
                name = 'candidate-module-' + name[len('pkg2-'):]
        return dict(id=7, name=name,
                    expired=False, expires_at='2030-01-01T00:00:00Z',
                    digest='sha256:' + ('0' * 64 if corrupt else hashlib.sha256(path.read_bytes()).hexdigest()))

    def test_real_reproductions_count_a_final_failure_after_successful_compilation(self):
        for case in CASES:
            with self.subTest(case=case['id']):
                record, producer, jobs = self.inputs(case)
                result, unit = self.collect(record, producer, jobs)
                self.assertEqual('build-failed', unit['status'])
                self.assertEqual(1, unit['consecutive_failures'])
                self.assertEqual([self.state.identity(record)], result['failed'])
                self.assertNotIn('reservation', unit)

    def test_actual_offline_collector_entry_records_both_composite_failures(self):
        for case in CASES:
            with self.subTest(case=case['id']):
                record, producer, jobs = self.inputs(case)
                paths = {name: self.root / (case['id'] + '-' + name + '.json')
                         for name in ('state', 'records', 'producer', 'jobs', 'artifacts', 'report')}
                paths['state'].write_text(json.dumps(self.state.reserve_records(
                    {'schema_version': 1, 'units': {}}, [record], producer)['state']))
                for name, value in [('records', {'include': [record]}), ('producer', producer),
                                    ('jobs', {'jobs': jobs}), ('artifacts', {'artifacts': []})]:
                    paths[name].write_text(json.dumps(value))
                command = [sys.executable, str(ROOT / 'runtime/ci/build_state.py'), 'collect',
                           '--artifact-dir', str(self.root)]
                for name, path in paths.items():
                    command += ['--' + name, str(path)]
                run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(0, run.returncode, run.stdout + run.stderr)
                unit = json.loads(paths['state'].read_text())['units'][self.state.identity(record)]
                self.assertEqual(('build-failed', 1), (unit['status'], unit['consecutive_failures']))

    def test_same_attempt_counts_once_and_three_distinct_failures_stop_retry(self):
        for case in CASES:
            with self.subTest(case=case['id']):
                record, producer, jobs = self.inputs(case)
                ledger = None
                for number in range(1, 4):
                    producer['run_attempt'] = number
                    for job in jobs:
                        job['run_attempt'] = number
                    result, unit = self.collect(record, producer, jobs, ledger)
                    self.assertEqual(number, unit['consecutive_failures'])
                    again, _ = self.collect(record, producer, jobs, result['state'])
                    self.assertEqual(result['state'], again['state'])
                    pending = self.state.plan_pending(result['state'], [record])
                    self.assertEqual([record] if number < 3 else [], pending['include'])
                    ledger = result['state']
                self.assertEqual([self.state.identity(record)], pending['blocked'])

    def test_nested_workflow_names_and_attempt_scoped_api_without_job_attempt_are_supported(self):
        for case in CASES:
            with self.subTest(case=case['id']):
                record, producer, jobs = self.inputs(case)
                for job in jobs:
                    job['name'] = 'parent / child / ' + job['name']
                    job.pop('run_attempt')
                _, unit = self.collect(record, producer, jobs)
                self.assertEqual(('build-failed', 1), (unit['status'], unit['consecutive_failures']))

    def test_cancelled_skipped_and_prephase_failures_do_not_count(self):
        for case in CASES:
            for stage in (0, 1):
                for conclusion, step in [('cancelled', 'cancelled'), ('skipped', 'skipped'),
                                         ('failure', 'skipped'), ('timed_out', 'skipped'),
                                         ('failure', None)]:
                    with self.subTest(case=case['id'], stage=stage, conclusion=conclusion, step=step):
                        record, producer, jobs = self.inputs(case)
                        jobs[stage]['conclusion'] = conclusion
                        if step is None:
                            jobs[stage]['steps'] = []
                        else:
                            jobs[stage]['steps'][0]['conclusion'] = step
                        result, unit = self.collect(record, producer, jobs)
                        self.assertEqual(('discovered', 0), (unit['status'], unit['consecutive_failures']))
                        self.assertEqual([], result['failed'])
                        self.assertNotIn('reservation', unit)

    def test_a_real_finalization_timeout_and_postphase_failure_count(self):
        for case in CASES:
            for conclusion, step in [('timed_out', 'timed_out'), ('failure', 'success')]:
                with self.subTest(case=case['id'], conclusion=conclusion, step=step):
                    record, producer, jobs = self.inputs(case)
                    jobs[1]['conclusion'] = conclusion
                    jobs[1]['steps'][0]['conclusion'] = step
                    _, unit = self.collect(record, producer, jobs)
                    self.assertEqual(('build-failed', 1), (unit['status'], unit['consecutive_failures']))

    def test_contradictory_provenance_in_either_job_keeps_the_unit_held(self):
        for case in CASES:
            for stage in (0, 1):
                for field, value in [('head_sha', 'b' * 40), ('run_id', 999), ('run_attempt', 99)]:
                    with self.subTest(case=case['id'], stage=stage, field=field):
                        record, producer, jobs = self.inputs(case)
                        jobs[stage][field] = value
                        result, unit = self.collect(record, producer, jobs)
                        self.assertEqual([self.state.identity(record)], result['held'])
                        self.assertEqual(0, unit['consecutive_failures'])
                        self.assertIn('reservation', unit)

    def test_duplicate_jobs_unknown_status_and_ambiguous_phase_keep_the_unit_held(self):
        for case in CASES:
            for stage in (0, 1):
                for defect in ('duplicate-job', 'queued', 'unknown-conclusion', 'duplicate-step',
                               'unknown-step-status', 'unknown-step-conclusion'):
                    with self.subTest(case=case['id'], stage=stage, defect=defect):
                        record, producer, jobs = self.inputs(case)
                        if defect == 'duplicate-job':
                            jobs.append(copy.deepcopy(jobs[stage]))
                        elif defect == 'queued':
                            jobs[stage]['status'] = 'queued'
                        elif defect == 'unknown-conclusion':
                            jobs[stage]['conclusion'] = None
                        elif defect == 'duplicate-step':
                            jobs[stage]['steps'].append(copy.deepcopy(jobs[stage]['steps'][0]))
                        elif defect == 'unknown-step-status':
                            jobs[stage]['steps'][0]['status'] = 'in_progress'
                        else:
                            jobs[stage]['steps'][0]['conclusion'] = None
                        result, unit = self.collect(record, producer, jobs)
                        self.assertEqual([self.state.identity(record)], result['held'])
                        self.assertEqual(0, unit['consecutive_failures'])
                        self.assertIn('reservation', unit)

    def test_another_identity_is_not_borrowed_and_plain_job_behavior_stays_unchanged(self):
        for case in CASES:
            record, producer, jobs = self.inputs(case)
            other = dict(record, arch='arm64')
            jobs[1]['name'] = ('Finalize ' if record['kind'] == 'package' else 'Bind ') + self.state.unit_job_name(other)
            result, unit = self.collect(record, producer, jobs)
            self.assertEqual(0, unit['consecutive_failures'])
            self.assertEqual([], result['failed'])
        record, producer, jobs = self.inputs(CASES[0])
        for conclusion, step, expected in [('failure', 'failure', 1), ('timed_out', 'timed_out', 1),
                                           ('failure', 'skipped', 0), ('cancelled', 'cancelled', 0),
                                           ('success', 'success', 0)]:
            with self.subTest(conclusion=conclusion, step=step):
                jobs[0]['conclusion'] = conclusion
                jobs[0]['steps'][0]['conclusion'] = step
                _, unit = self.collect(record, producer, jobs[:1])
                self.assertEqual(expected, unit['consecutive_failures'])

    def test_valid_formal_artifact_wins_and_candidates_cannot_be_published(self):
        for case in CASES:
            with self.subTest(case=case['id']):
                record, producer, jobs = self.inputs(case)
                result, unit = self.collect(record, producer, jobs, artifacts=[self.artifact(record, producer)])
                self.assertEqual(('build-ready', 0), (unit['status'], unit['consecutive_failures']))
                self.assertEqual(1, result['ready_count'])
                result, unit = self.collect(record, producer, jobs, artifacts=[self.artifact(record, producer, candidate=True)])
                self.assertEqual(('build-failed', 1), (unit['status'], unit['consecutive_failures']))
                self.assertEqual(0, result['ready_count'])
                result, unit = self.collect(record, producer, jobs, artifacts=[self.artifact(record, producer, corrupt=True)])
                self.assertEqual(0, result['ready_count'])
                self.assertEqual(0, unit['consecutive_failures'])
                self.assertIn('reservation', unit)


if __name__ == '__main__':
    unittest.main()
