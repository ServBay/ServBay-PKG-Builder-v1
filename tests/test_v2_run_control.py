import importlib.util
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class RunControlTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime/ci/build_control.py"
        self.assertTrue(path.is_file(), "A changed version table needs one new source-correct pending run")
        spec = importlib.util.spec_from_file_location("build_control", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def decide(self, **changes):
        values = dict(runtime_version="v2", pending_only=False, config_changed=False, continue_build=True,
                      trigger_sha="a" * 40, checkout_sha="a" * 40)
        values.update(changes)
        return self.module.run_decision(**values)

    def test_discovery_with_a_new_commit_never_builds_under_the_old_trigger_sha(self):
        decision = self.decide(config_changed=True, checkout_sha="b" * 40)
        self.assertFalse(decision["build_allowed"])
        self.assertTrue(decision["dispatch_pending"])
        inputs = self.module.pending_inputs(True)
        self.assertEqual("v2", inputs["runtime_version"])
        self.assertEqual("true", inputs["pending_only"])
        self.assertEqual("true", inputs["continue_build"])
        self.assertEqual("true", inputs["upload_dry_run"])
        self.assertEqual("macos", inputs["target"])
        self.assertFalse(self.decide(config_changed=True, continue_build=False)["dispatch_pending"])

    def test_pending_run_uses_its_own_source_and_cannot_recurse(self):
        decision = self.decide(pending_only=True, trigger_sha="c" * 40, checkout_sha="c" * 40)
        self.assertTrue(decision["build_allowed"])
        self.assertFalse(decision["dispatch_pending"])
        with self.assertRaises(ValueError):
            self.decide(pending_only=True, config_changed=True)
        with self.assertRaises(ValueError):
            self.decide(pending_only=True, checkout_sha="b" * 40)

    def test_unchanged_config_can_build_only_the_exact_trigger_source(self):
        self.assertTrue(self.decide()["build_allowed"])
        with self.assertRaises(ValueError):
            self.decide(checkout_sha="b" * 40)

    def test_v1_keeps_its_original_behavior_without_a_new_dispatch_path(self):
        decision = self.decide(runtime_version="v1", config_changed=True, checkout_sha="b" * 40)
        self.assertTrue(decision["build_allowed"])
        self.assertFalse(decision["dispatch_pending"])
        with self.assertRaises(ValueError):
            self.decide(runtime_version="v1", pending_only=True)

    def test_continuation_uses_one_same_repository_dispatch_and_no_new_token(self):
        module = self.module
        import subprocess, json
        with mock.patch.object(subprocess, 'run') as run:
            module.dispatch_pending('ServBay/Builder', True)
        self.assertEqual(1, run.call_count)
        args, kwargs = run.call_args
        self.assertIn('repos/ServBay/Builder/actions/workflows/check-updates.yml/dispatches', args[0])
        payload = json.loads(kwargs['input'])
        self.assertEqual('main', payload['ref'])
        self.assertEqual('true', payload['inputs']['pending_only'])
        self.assertEqual('v2', payload['inputs']['runtime_version'])
        self.assertTrue(kwargs['check'])


    def refresh(self, **changes):
        function = getattr(self.module, 'refresh_inputs', None)
        self.assertTrue(callable(function), 'The update-only fanout must plan explicit v2 inputs')
        arguments = dict(event_name='schedule', runtime_version='', target='all', pending_only=False,
                         continue_build=False, dry_run=True, full_rebuild=False, schedule_mode='off')
        arguments.update(changes)
        return function(**arguments)

    def test_schedule_defaults_off_and_check_never_requests_compilation(self):
        for mode in ('', 'off'):
            with self.subTest(mode=mode):
                self.assertIsNone(self.refresh(schedule_mode=mode))
        for mode, build, dry in [('check', 'false', 'true'), ('build', 'true', 'true'),
                                 ('publish', 'true', 'false')]:
            with self.subTest(mode=mode):
                value = self.refresh(schedule_mode=mode, continue_build=True, dry_run=False, full_rebuild=True)
                self.assertEqual(dict(runtime_version='v2', target='macos', pending_only='false',
                                      continue_build=build, upload_dry_run=dry, full_rebuild='false'), value)
        with self.assertRaises(ValueError):
            self.refresh(schedule_mode='unexpected')

    def test_only_manual_all_fans_out_and_preserves_its_explicit_flags(self):
        for build in (False, True):
            for dry in (False, True):
                with self.subTest(build=build, dry=dry):
                    value = self.refresh(event_name='workflow_dispatch', runtime_version='all',
                                         continue_build=build, dry_run=dry, full_rebuild=True)
                    self.assertEqual('v2', value['runtime_version'])
                    self.assertEqual('macos', value['target'])
                    self.assertEqual('false', value['pending_only'])
                    self.assertEqual(str(build).lower(), value['continue_build'])
                    self.assertEqual(str(dry).lower(), value['upload_dry_run'])
                    self.assertEqual('true', value['full_rebuild'])
        for runtime in ('v1', 'v2'):
            for pending in (False, True):
                self.assertIsNone(self.refresh(event_name='workflow_dispatch', runtime_version=runtime,
                                               pending_only=pending, schedule_mode='publish'))
        self.assertIsNone(self.refresh(event_name='push', runtime_version='all', schedule_mode='publish'))
        for changes in (dict(pending_only=True), dict(target='windows')):
            with self.assertRaises(ValueError):
                self.refresh(event_name='workflow_dispatch', runtime_version='all', **changes)
        with self.assertRaises(ValueError):
            self.decide(runtime_version='all')

    def test_refresh_dispatch_uses_same_main_contract_and_propagates_api_failure_without_retry(self):
        function = getattr(self.module, 'dispatch_refresh', None)
        self.assertTrue(callable(function), 'An explicit update-only dispatcher must reuse the current API contract')
        import subprocess, json
        with mock.patch.object(subprocess, 'run') as run:
            self.assertFalse(function('ServBay/Builder', event_name='schedule', schedule_mode='off'))
            run.assert_not_called()
            self.assertTrue(function('ServBay/Builder', event_name='schedule', schedule_mode='check'))
            self.assertEqual(1, run.call_count)
            args, kwargs = run.call_args
            self.assertIn('repos/ServBay/Builder/actions/workflows/check-updates.yml/dispatches', args[0])
            payload = json.loads(kwargs['input'])
            self.assertEqual('main', payload['ref'])
            self.assertEqual('false', payload['inputs']['pending_only'])
            self.assertEqual('false', payload['inputs']['continue_build'])
            self.assertTrue(kwargs['check'])
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(23, ['gh'])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                function('ServBay/Builder', event_name='schedule', schedule_mode='build')
            self.assertEqual(1, run.call_count)


if __name__ == "__main__":
    unittest.main()
