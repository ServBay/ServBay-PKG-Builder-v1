"""Offline behavioral checks for the explicitly selected Windows v2 update."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_v2_workflows as workflow_tests


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-windows/ci/select_v2_update.py"
WORKFLOW = ROOT / ".github/workflows/build-windows-v2-incremental.yml"


def version(value, *, runtime="v1", major="0", **changes):
    row = dict(latestVersion=value, majorVersion=major, os="Windows",
               arch="x64" if runtime == "v1" else "x86_64", runtimeVersion=runtime,
               downloadUrl=f"https://example.invalid/source-{value}.7z",
               hash=hashlib.sha256(value.encode()).hexdigest(), buildRevision=0)
    row.update(changes)
    return row


def package(name, *versions):
    return dict(name=name, versions=list(versions))


def workflow(path):
    return workflow_tests.WorkflowTests().workflow(path.name)


def condition(expression, values):
    if not expression.startswith("${{"):
        return expression
    text = expression.removeprefix("${{").removesuffix("}}").strip()
    for name in sorted(values, key=len, reverse=True):
        text = text.replace(name, repr(values[name]))
    return eval(text.replace("&&", " and ").replace("||", " or "),
                {"__builtins__": {}, "fromJSON": json.loads}, {})


class WindowsV2SelectionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "The controlled Windows v2 selector is missing")
        spec = importlib.util.spec_from_file_location("selected_windows_v2_test", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        network = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("offline test"))
        network.start()
        self.addCleanup(network.stop)
        self.v1 = [package("cloudflared", version("2026.10.6"), version("2026.6.1", major="2026"))]
        self.v2 = [package("cloudflared", version("2026.10.3", runtime="v2"))]

    def select(self, v1=None, v2=None, **changes):
        return self.module.select_update(
            self.v1 if v1 is None else v1, self.v2 if v2 is None else v2,
            package_name="cloudflared", **changes)

    def test_selected_update_uses_real_source_identity_and_does_not_mutate_catalogs(self):
        before = copy.deepcopy((self.v1, self.v2))
        rows = self.select(build_revision=2)
        self.assertEqual(rows, [dict(pkg="cloudflared", version="2026.10.6", major="0",
                                     source_url=self.v1[0]["versions"][0]["downloadUrl"],
                                     source_sha256=self.v1[0]["versions"][0]["hash"],
                                     minimum_os="10.0", build_revision=2)])
        self.assertEqual(before, (self.v1, self.v2))

    def test_unselected_invalid_versions_and_unknown_packages_are_not_parsed(self):
        unrelated = [package("openjdk", version("not-a-version")), package("future-package", version("?"))]
        self.assertEqual(self.select(self.v1 + unrelated, self.v2 + unrelated), self.select())

    def test_absent_major_updates_but_same_or_newer_target_never_downgrades(self):
        self.assertEqual(len(self.select(v2=[])), 1)
        for target in ("2026.10.6", "2026.10.7"):
            with self.subTest(target=target):
                self.assertEqual([], self.select(v2=[package("cloudflared", version(target, runtime="v2"))]))

    def test_v1_revision_is_not_compared_to_independent_v2_revision(self):
        source = [package("cloudflared", version("2026.10.6", buildRevision=99))]
        target = [package("cloudflared", version("2026.10.6", runtime="v2", buildRevision=0))]
        self.assertEqual([], self.select(source, target, build_revision=2))

    def test_multiple_pending_series_require_an_explicit_real_v2_major(self):
        source = [package("composer", version("2.11.0", major="2"), version("2.2.31", major="2.2"))]
        target = [package("composer", version("2.10.3", runtime="v2", major="2"),
                          version("2.2.30", runtime="v2", major="2.2"))]
        with self.assertRaisesRegex(ValueError, "major"):
            self.module.select_update(source, target, package_name="composer")
        rows = self.module.select_update(source, target, package_name="composer", major="2.2")
        self.assertEqual([(r["major"], r["version"]) for r in rows], [("2.2", "2.2.31")])

    def test_selected_openjdk_representation_changes_require_source_verification(self):
        cases = [("17", "17.0.21.0.3", "17.0.21"),
                 ("21", "21.0.13.0.3", "21.0.13"),
                 ("25", "25.0.5.0.3", "25.0.5"),
                 ("27", "27.0.1.0.3", "27.0.1"),
                 ("17", "17.0.21", "17.0.21.0.3"),
                 ("17", "17.0.21.0.0", "17.0.21"),
                 ("17", "17.0.22.0.1", "17.0.21")]
        for major, source, target in cases:
            with self.subTest(source=source, target=target), \
                    self.assertRaisesRegex(ValueError, "OpenJDK.*source verification"):
                self.module.select_update(
                    [package("openjdk", version(source, major=major))],
                    [package("openjdk", version(target, major=major, runtime="v2"))],
                    package_name="openjdk", major=major)

    def test_openjdk_same_representation_still_plans_clear_version_updates(self):
        for major, source, target in [("17", "17.0.22", "17.0.21"),
                                       ("18", "18.0.2.2", "18.0.2.1"),
                                       ("21", "21.0.14.0.1", "21.0.13.0.3")]:
            with self.subTest(source=source):
                rows = self.module.select_update(
                    [package("openjdk", version(source, major=major))],
                    [package("openjdk", version(target, major=major, runtime="v2"))],
                    package_name="openjdk", major=major)
                self.assertEqual([(row["major"], row["version"]) for row in rows], [(major, source)])

    def test_openjdk_same_representation_current_or_missing_target_keeps_existing_rules(self):
        source = [package("openjdk", version("17.0.21", major="17"))]
        target = [package("openjdk", version("17.0.21", major="17", runtime="v2"))]
        self.assertEqual([], self.module.select_update(source, target, package_name="openjdk", major="17"))
        self.assertEqual(1, len(self.module.select_update(source, [], package_name="openjdk", major="17")))

    def test_unselected_openjdk_major_does_not_block_the_selected_major(self):
        source = [package("openjdk", version("17.0.21.0.3", major="17"), version("18.0.2.1", major="18"))]
        target = [package("openjdk", version("17.0.21", major="17", runtime="v2"),
                          version("18.0.2.1", major="18", runtime="v2"))]
        self.assertEqual([], self.module.select_update(source, target, package_name="openjdk", major="18"))

    def test_openjdk_ambiguous_cli_fails_without_exposing_an_executable_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, rows in [("v1", [package("openjdk", version("17.0.21.0.3", major="17"))]),
                               ("v2", [package("openjdk", version("17.0.21", major="17", runtime="v2"))])]:
                (root / (name + ".json")).write_text(json.dumps({"data": {"packages": rows}}))
            output = root / "selection.json"
            result = subprocess.run([sys.executable, str(SCRIPT), "--package", "openjdk", "--major", "17",
                                     "--v1-catalog", str(root / "v1.json"), "--v2-catalog", str(root / "v2.json"),
                                     "--output", str(output)], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("source verification", result.stderr)
            self.assertFalse(output.exists())

    def test_source_aliases_use_producer_major_and_do_not_roll_adminer_back(self):
        source = [package("adminer", version("6.2.0", major="0"), version("6.0.2", major="6"))]
        target = [package("adminer", version("6.2.0", runtime="v2", major="6"))]
        self.assertEqual([], self.module.select_update(source, target, package_name="adminer", major="6"))

    def test_missing_selected_source_or_major_is_an_error_not_an_empty_update(self):
        for source, major in (([], ""), (self.v1, "99")):
            with self.subTest(source=source, major=major), self.assertRaises(ValueError):
                self.select(v1=source, major=major)

    def test_excluded_and_unknown_names_cannot_enter_the_ordinary_producer(self):
        for name in ("", "ai-gateway", "mcp", "php", "postgresql", "simple-acme", "unknown"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.module.select_update([package(name, version("1.31.0", major="1"))], [],
                                          package_name=name)

    def test_cleaner_retains_only_its_existing_package_distribution(self):
        rows = self.module.select_update(
            [package("disk-cleaner", version("1.1.0", major="1"))], [], package_name="disk-cleaner")
        self.assertEqual([(r["pkg"], r["major"]) for r in rows], [("disk-cleaner", "1")])

    def test_invalid_selected_source_metadata_fails_before_production(self):
        for field, value in (("hash", "bad"), ("os", "macOS"), ("runtimeVersion", "v2"),
                             ("arch", "arm64"), ("downloadUrl", "http://example.invalid/source.7z"),
                             ("downloadUrl", "https://user:pass@example.invalid/source.7z"),
                             ("downloadUrl", "https://example.invalid/source.7z?token=secret"),
                             ("downloadUrl", "https://example.invalid/source.7z\npkg=other"),
                             ("latestVersion", "2026.10.6\n"),
                             ("latestVersion", "invalid")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                bad = copy.deepcopy(self.v1)
                bad[0]["versions"][0][field] = value
                self.select(v1=bad)

    def test_invalid_target_identity_and_invalid_revision_do_not_skip_the_update(self):
        for field, value in (("arch", "x64"), ("os", "macOS"), ("runtimeVersion", "v1"),
                             ("majorVersion", "2026"), ("latestVersion", "invalid")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                bad = copy.deepcopy(self.v2)
                bad[0]["versions"][0][field] = value
                self.select(v2=bad)
        for revision in (-1, True, "1"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.select(build_revision=revision)

    def test_catalog_http_and_shape_failures_are_distinct_from_valid_empty_catalog(self):
        response = mock.Mock()
        response.raise_for_status.side_effect = RuntimeError("HTTP unavailable")
        with mock.patch.object(self.module.requests, "get", return_value=response), self.assertRaises(RuntimeError):
            self.module.load_catalog(None, "v1")
        response.raise_for_status.side_effect = None
        for value in ({"status_code": 500, "data": {"packages": []}}, {"data": {}}, []):
            with self.subTest(value=value), mock.patch.object(self.module.requests, "get", return_value=response), \
                    self.assertRaises(ValueError):
                response.json.return_value = value
                self.module.load_catalog(None, "v2")
        response.json.return_value = {"status_code": 200, "data": {"packages": []}}
        with mock.patch.object(self.module.requests, "get", return_value=response) as request:
            self.assertEqual([], self.module.load_catalog(None, "v2"))
        self.assertEqual(("v2", "x86_64"), (request.call_args.kwargs["params"]["runtime_version"],
                                           request.call_args.kwargs["params"]["cpu_arch"]))

    def test_cli_writes_an_explicit_plan_from_saved_catalogs_without_rewriting_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, rows in (("v1", self.v1), ("v2", self.v2)):
                (root / (name + ".json")).write_text(json.dumps({"data": {"packages": rows}}))
            target = root / "selection.json"
            result = subprocess.run([sys.executable, str(SCRIPT), "--package", "cloudflared", "--major", "0",
                                     "--v1-catalog", str(root / "v1.json"), "--v2-catalog", str(root / "v2.json"),
                                     "--output", str(target)], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(target.read_text())["include"], self.select())
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["selection.json", "v1.json", "v2.json"])


class WindowsV2IncrementalWorkflowTests(unittest.TestCase):
    def test_plan_only_empty_and_failed_runs_do_not_produce_or_notify(self):
        self.assertTrue(WORKFLOW.is_file(), "The controlled manual workflow is missing")
        data = workflow(WORKFLOW)
        self.assertEqual(set(data["on"]), {"workflow_dispatch"})
        inputs = data["on"]["workflow_dispatch"]["inputs"]
        self.assertFalse(inputs["continue_build"]["default"])
        self.assertTrue(inputs["upload_dry_run"]["default"])
        producer = data["jobs"]["repack"]
        self.assertEqual(producer["uses"], "./.github/workflows/build-windows-v2-package.yml")
        planned = dict(pkg="cloudflared", version="2026.10.6", source_url="https://example.invalid/source.7z",
                       source_sha256="b" * 64, minimum_os="10.0", build_revision="2")
        bindings = {"needs.plan.outputs." + key: value for key, value in planned.items()}
        self.assertEqual({key: condition(value, bindings) for key, value in producer["with"].items()},
                         dict(planned, build_revision=2))
        for proceed, count, expected in ((False, "1", False), (True, "0", False), (True, "1", True)):
            self.assertEqual(bool(condition(producer["if"], {"inputs.continue_build": proceed,
                             "needs.plan.outputs.count": count})), expected)
        notify = data["jobs"]["notify"]
        for result, artifact, expected in (("failure", "pkg", False), ("skipped", "", False),
                                           ("success", "", False), ("success", "pkg", True)):
            self.assertEqual(bool(condition(notify["if"], {"needs.repack.result": result,
                             "needs.repack.outputs.artifact_name": artifact})), expected)

    def test_notification_uses_this_producer_and_explicit_windows_schema2_route(self):
        self.assertTrue(WORKFLOW.is_file(), "The controlled manual workflow is missing")
        step = workflow(WORKFLOW)["jobs"]["notify"]["steps"][0]
        for dry_run in ("true", "false"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                curl = root / "curl"
                curl.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                                "open(os.environ['CAPTURE'],'w').write(sys.argv[-1])\n")
                curl.chmod(0o755)
                bindings = {"secrets.DISPATCH_PAT": "fixture-token", "vars.TARGET_REPO": "fixture/uploader",
                            "secrets.TARGET_REPO": "", "github.run_id": "12345", "github.run_attempt": 2,
                            "github.sha": "a" * 40, "inputs.upload_dry_run": dry_run == "true"}
                resolved = {key: condition(value, bindings) for key, value in step["env"].items()}
                environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                                   CAPTURE=str(root / "captured.json"))
                environment.update({key: str(value).lower() if isinstance(value, bool) else str(value)
                                    for key, value in resolved.items()})
                result = subprocess.run(["/bin/bash", "-c", step["run"]], env=environment, cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads((root / "captured.json").read_text())
                self.assertEqual(payload, dict(event_type="builder-build-complete", client_payload=dict(
                    runtime_version="v2", os="windows", kind="package", run_id="12345", run_attempt=2,
                    producer_commit="a" * 40, dry_run=dry_run == "true")))



if __name__ == "__main__":
    unittest.main()
