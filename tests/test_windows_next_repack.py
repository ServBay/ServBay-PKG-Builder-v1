import copy
import fnmatch
import re
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-windows/repack_for_next.py"
SEVEN_ZIP = os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7zz")


def load_repacker():
    if not SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("next_repacker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WindowsNextRepackTests(unittest.TestCase):
    def setUp(self):
        self.module = load_repacker()
        self.assertIsNotNone(self.module, "The offline Next repacker is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "sources"
        self.source.mkdir()
        self.manifest = json.loads((ROOT / "runtime-windows/next-bootstrap-packages.json").read_text())
        self.payloads = {
            "nginx": {"nginx.exe": b"nginx", "conf/nginx.conf": b"config"},
            "php": {"php.exe": b"php", "php-cgi.exe": b"cgi", "php-win.exe": b"win", "core.dll": b"core", "ext/php_test.dll": b"extension"},
            "redis": {"redis-server.exe": b"server", "redis-cli.exe": b"cli", "msys-2.0.dll": b"msys"},
            "phpmyadmin": {"index.php": b"index", "vendor/lib.php": b"vendor", "themes/style.css": b"style"},
            "adminer": {"adminer.php": b"original adminer bytes"},
        }
        for package in self.manifest["packages"]:
            tree = self.base / ("input-" + package["name"])
            tree.mkdir()
            for relative, data in self.payloads[package["name"]].items():
                path = tree / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            archive = self.source / package["source"]["filename"]
            self.run_7z(["a", "-t7z", str(archive), "."], cwd=tree)
            package["source"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            package["source"]["bytes"] = archive.stat().st_size
        self.manifest_path = self.base / "next-bootstrap-packages.json"
        self.manifest_path.write_text(json.dumps(self.manifest))

    def run_7z(self, args, cwd=None):
        result = subprocess.run([SEVEN_ZIP, *args], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def repack(self, manifest=None, output=None):
        if manifest is not None:
            self.manifest_path.write_text(json.dumps(manifest))
        return self.module.repack(self.manifest_path, self.source, output or self.base / "output", "v2")

    def test_real_five_archive_repack_preserves_every_mapped_byte(self):
        batch = self.repack()
        self.assertEqual(len(batch["packages"]), 5)
        self.assertEqual(batch["runtime_version"], "v2")
        self.assertFalse(batch["validation_complete"])
        self.assertFalse(batch["published"])
        for record in batch["packages"]:
            with self.subTest(name=record["name"]):
                output = self.base / "output" / record["filename"]
                self.assertEqual(record["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
                self.assertEqual(record["output_bytes"], output.stat().st_size)
                target = self.base / ("check-" + record["name"])
                self.run_7z(["x", "-y", "-o" + str(target), str(output)])
                actual = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
                source = self.payloads[record["name"]]
                expected = {record["archive_root"] + ("index.php" if record["name"] == "adminer" else name): value for name, value in source.items()}
                self.assertEqual(actual, expected)
                self.assertEqual(record["compatibility"]["status"], "partial")
                self.assertIsNone(record["source"]["original_build_revision"])
                producer = record["producer"]
                self.assertTrue(producer["dirty"])
                for frozen in producer["files"]:
                    snapshot = self.base / "output/producer" / frozen["path"]
                    self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), frozen["sha256"])
                self.assertEqual(producer["script_sha256"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())

    def test_each_source_hash_failure_prevents_any_output(self):
        for index in range(5):
            with self.subTest(index=index):
                bad = copy.deepcopy(self.manifest)
                bad["packages"][index]["source"]["sha256"] = "0" * 64
                output = self.base / ("bad-" + str(index))
                with self.assertRaisesRegex(ValueError, "[Ss]ource.*(hash|SHA)"):
                    self.repack(bad, output)
                self.assertFalse(output.exists())

    def test_exact_set_and_runtime_are_required(self):
        cases = []
        for index in range(5):
            bad = copy.deepcopy(self.manifest)
            bad["packages"].pop(index)
            cases.append(bad)
        cases.append({**self.manifest, "packages": self.manifest["packages"] + [self.manifest["packages"][0]]})
        extra = copy.deepcopy(self.manifest)
        extra["packages"].append({**extra["packages"][0], "name": "other"})
        cases.append(extra)
        duplicate = copy.deepcopy(self.manifest)
        duplicate["packages"][1] = copy.deepcopy(duplicate["packages"][0])
        cases.append(duplicate)
        for field, value in [("runtime_version", "v1"), ("os", "Windows"), ("arch", "arm64")]:
            cases.append({**self.manifest, field: value})
        for bad in cases:
            with self.subTest(bad=bad.keys()):
                with self.assertRaises(ValueError):
                    self.repack(bad)

    def test_each_wrong_prefix_and_missing_entry_is_rejected(self):
        for package in self.manifest["packages"]:
            for field, value in [("archive_root", "wrong/"), ("required_entries", ["missing.exe"])]:
                bad = copy.deepcopy(self.manifest)
                next(p for p in bad["packages"] if p["name"] == package["name"])[field] = value
                with self.subTest(name=package["name"], field=field):
                    with self.assertRaises(ValueError):
                        self.repack(bad)

    def test_mapping_detects_removed_moved_and_changed_files_independently(self):
        for package in self.manifest["packages"]:
            source = self.base / ("input-" + package["name"])
            target = self.base / ("mapped-" + package["name"])
            for relative, data in self.payloads[package["name"]].items():
                mapped = package["archive_root"] + ("index.php" if package["name"] == "adminer" else relative)
                path = target / mapped
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            self.module.verify_payload(source, target, package)
            extra = target / "unexpected-empty-directory"
            extra.mkdir()
            with self.assertRaises(ValueError):
                self.module.verify_payload(source, target, package)
            extra.rename(self.base / ("extra-" + package["name"]))
            for original in sorted(p for p in target.rglob("*") if p.is_file()):
                with self.subTest(name=package["name"], file=original.name):
                    data = original.read_bytes()
                    original.write_bytes(data + b"changed")
                    with self.assertRaises(ValueError):
                        self.module.verify_payload(source, target, package)
                    original.write_bytes(data)
                    moved = target / ("wrong-" + original.name)
                    original.rename(moved)
                    with self.assertRaises(ValueError):
                        self.module.verify_payload(source, target, package)
                    moved.rename(original)

    def test_each_missing_real_archive_entry_fails_before_output_creation(self):
        for index, package in enumerate(self.manifest["packages"]):
            entries = ["adminer.php"] if package["name"] == "adminer" else [path.removeprefix(package["archive_root"]) for path in package["required_entries"]]
            for entry_index, entry in enumerate(entries):
                suffix = package["name"] + "-" + str(entry_index)
                tree = self.base / ("incomplete-" + suffix)
                shutil.copytree(self.base / ("input-" + package["name"]), tree)
                (tree / entry).rename(tree / ("missing-" + Path(entry).name))
                altered_source = self.base / ("source-missing-" + suffix)
                altered_source.mkdir()
                for existing in self.source.iterdir():
                    if existing.name != package["source"]["filename"]:
                        shutil.copyfile(existing, altered_source / existing.name)
                archive = altered_source / package["source"]["filename"]
                self.run_7z(["a", "-t7z", str(archive), "."], cwd=tree)
                bad = copy.deepcopy(self.manifest)
                bad["packages"][index]["source"].update({"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "bytes": archive.stat().st_size})
                self.manifest_path.write_text(json.dumps(bad))
                output = self.base / ("missing-output-" + suffix)
                with self.subTest(name=package["name"], entry=entry), self.assertRaisesRegex(ValueError, "Missing source entry"):
                    self.module.repack(self.manifest_path, altered_source, output, "v2")
                self.assertFalse(output.exists())

    def test_member_paths_and_links_fail_before_extraction(self):
        for name in ["../escape", "/absolute", "C:/drive", "C:relative", "a\\..\\evil", "a:stream", "a/../evil"]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.module.validate_member({"Path": name, "Attributes": "A"})
        with self.assertRaises(ValueError):
            self.module.validate_member({"Path": "link", "Attributes": "A_ lrwxrwxrwx"})
        with self.assertRaises(ValueError):
            self.module.validate_member({"Path": "link", "Attributes": "AL"})

    def test_new_candidate_subdirectory_preserves_original_source_archives(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.glob("*.7z")}
        result = self.repack(output=self.source / "v2-candidate")
        self.assertEqual(len(result["packages"]), 5)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.glob("*.7z")})

    def test_actual_workflow_artifact_is_excluded_by_legacy_wildcard(self):
        workflow = (ROOT / ".github/workflows/build-windows-next.yml").read_text()
        step = workflow.split("      - name: Retain offline candidate bundle", 1)[1]
        names = re.findall(r"^          name: (.+)$", step, re.MULTILINE)
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0], "pkg2-windows-x64-bootstrap")
        self.assertFalse(fnmatch.fnmatch(names[0], "pkg-*"))
        self.assertTrue(fnmatch.fnmatch("pkg-windows-v2-bootstrap", "pkg-*"))

    def test_existing_output_and_source_destination_overlap_are_rejected(self):
        with self.assertRaises(ValueError):
            self.module.repack(self.manifest_path, self.source, self.source, "v2")
        output = self.base / "existing"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("user bytes")
        with self.assertRaises(ValueError):
            self.repack(output=output)
        self.assertEqual(sentinel.read_text(), "user bytes")


if __name__ == "__main__":
    unittest.main()
