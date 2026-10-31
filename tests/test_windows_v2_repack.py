"""Real 7z fixtures prove ordinary v2 mapping, not Windows process execution."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime-windows/repack_v2.py"
SEVEN_ZIP = os.environ.get("SEVEN_ZIP") or shutil.which("7z") or shutil.which("7zz")


class WindowsV2RepackTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "ordinary Windows v2 repacker is missing")
        spec = importlib.util.spec_from_file_location("ordinary_windows_repack", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.assertTrue(SEVEN_ZIP, "real 7z is required")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.producer = {"commit": "a" * 40, "run_id": "23456", "run_attempt": 3}
        self.payloads = {
            "nginx": {"nginx.exe": b"nginx-exe", "dependency.dll": b"nginx-dll",
                      "LICENSE": b"license", "conf/nginx.conf": b"upstream nginx default",
                      "conf/mime.types": b"distinct mime types", "conf/nested/extra": b"nested config",
                      "html/index.html": b"html", "docs/readme.txt": b"docs"},
            "redis": {"redis-server.exe": b"redis server", "redis-cli.exe": b"redis cli",
                      "msys-2.0.dll": b"msys runtime", "lib/related.dat": b"redis dependency",
                      "redis.conf": b"redis default", "redis-service.conf": b"service default"},
            "python": {"python.exe": b"python exe", "python313.dll": b"python dll",
                       "python313._pth": b"Lib", "Lib/os.py": b"stdlib", "DLLs/_ssl.pyd": b"ssl",
                       "Scripts/pip.exe": b"pip", "include/Python.h": b"header"},
            "node": {"node.exe": b"node exe", "npm": b"shell npm", "npm.cmd": b"npm cmd",
                     "npx.cmd": b"npx cmd", "node_modules/npm/bin/npm-cli.js": b"npm js",
                     "node_modules/npm/bin/npx-cli.js": b"npx js", "README.md": b"node docs"},
            "composer": {"composer": b"#!/usr/bin/env php\n<?php distinct phar;",
                         "LICENSE": b"composer license", "assets/notice": b"notice"},
        }
        self.versions = {"nginx": "1.31.8", "redis": "7.4.11", "python": "3.13.17",
                         "node": "26.12.1", "composer": "2.10.3"}

    def archive(self, payload, label="input"):
        tree = self.base / (label + "-tree")
        tree.mkdir()
        for name, content in payload.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        archive = self.base / (label + ".7z")
        result = subprocess.run([SEVEN_ZIP, "a", "-t7z", str(archive), "."], cwd=tree,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return archive

    def arguments(self, name="nginx", payload=None, label="input", version=None):
        version = version or self.versions[name]
        archive = self.archive(self.payloads[name] if payload is None else payload, label)
        return dict(source={"path": str(archive), "version": version,
                            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                            "bytes": archive.stat().st_size,
                            "url": "https://example.org/ordinary-input.7z"},
                    identity={"runtime_version": "v2", "os": "windows", "kind": "package",
                              "name": name, "version": version, "arch": "x64", "build_revision": 2},
                    producer=copy.deepcopy(self.producer), minimum_os="10.0",
                    output_dir=self.base / (label + "-output"), seven_zip=SEVEN_ZIP)

    def reject(self, arguments):
        with self.assertRaises((ValueError, OSError)):
            self.module.repack(**arguments)
        self.assertFalse((arguments["output_dir"] / "meta.json").exists())

    def test_five_complete_trees_metadata_defaults_and_every_byte(self):
        roots = {"nginx": "nginx", "redis": "redis/7.4", "python": "python/3.13",
                 "node": "node/26", "composer": "composer/2"}
        for name in self.payloads:
            with self.subTest(name=name):
                args = self.arguments(name, label=name)
                original = copy.deepcopy(args)
                meta = self.module.repack(**args)
                self.assertEqual(args, original)
                output = args["output_dir"]
                self.assertEqual(json.loads((output / "meta.json").read_text()), meta)
                self.assertEqual(meta["producer"], self.producer)
                self.assertEqual(meta["minimum_os"], "10.0")
                self.assertEqual(meta["arch"], "x86_64")
                self.assertEqual(meta["package_root"], roots[name])
                self.assertEqual(meta["config_root"], roots[name])
                self.assertNotIn("base", meta)
                self.assertNotIn("package_manifest", meta)
                self.assertEqual(meta["filename"], f"{name}-{self.versions[name]}-windows-x86_64.7z")
                target = output / meta["filename"]
                self.assertEqual(meta["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
                audit = json.loads((output / "repack-manifest.json").read_text())
                self.assertEqual(audit["native_validation"], "pending")
                self.assertEqual(audit["source"], {"filename": Path(args["source"]["path"]).name,
                                                     "sha256": args["source"]["sha256"], "bytes": args["source"]["bytes"]})
                mapping = {row["source_path"]: row for row in audit["files"]}
                self.assertEqual(set(mapping), set(self.payloads[name]))
                expected = {}
                for source_path, content in self.payloads[name].items():
                    if name == "nginx":
                        relative = ("share/defaults/" + source_path if source_path.startswith("conf/")
                                    else "sbin/" + source_path if "/" not in source_path else source_path)
                    elif name == "redis":
                        relative = ("share/defaults/" + source_path if "/" not in source_path and source_path.endswith(".conf")
                                    else "bin/" + source_path)
                    else:
                        relative = source_path if name == "python" else "bin/" + source_path
                    path = roots[name] + "/" + relative
                    digest = hashlib.sha256(content).hexdigest()
                    self.assertEqual(mapping[source_path], {"source_path": source_path, "target_path": path,
                                                            "bytes": len(content), "sha256": digest})
                    expected[path] = digest
                self.assertEqual(self.module.windows_archive.archive_hashes(target, SEVEN_ZIP), expected)
                defaults = audit["configuration_defaults"]
                expected_defaults = {row["target_path"] for row in mapping.values()
                                     if "/share/defaults/" in row["target_path"]}
                self.assertEqual({row["archive_path"] for row in defaults}, expected_defaults)
                for row in defaults:
                    suffix = row["archive_path"].split("/share/defaults/", 1)[1]
                    relative = suffix.removeprefix("conf/") if name == "nginx" else suffix
                    self.assertEqual(row["etc_relative_path"], roots[name] + "/" + relative)
                    self.assertEqual(row["sha256"], expected[row["archive_path"]])
                    self.assertEqual(row["kind"], "upstream-default")
                    self.assertFalse(row["automatically_enabled"])
                self.assertNotIn(str(self.base), json.dumps(audit))
                self.assertEqual(set(p.name for p in output.iterdir()),
                                 {meta["filename"], "meta.json", "repack-manifest.json"})
                if name == "composer":
                    self.assertEqual(meta["entrypoint"], {"path": "composer/2/bin/composer",
                                                         "type": "php-script", "interpreter": "php"})
                if name == "nginx":
                    self.assertEqual(audit["startup_handoff"], {"prefix": "package/nginx",
                                                               "config": "etc/nginx/nginx.conf"})

    def test_composer_22_uses_exact_producer_filename(self):
        payload = {"composer-2.2": b"<?php 2.2 phar", "notes": b"LTS notice"}
        args = self.arguments("composer", payload, version="2.2.30")
        meta = self.module.repack(**args)
        self.assertEqual(meta["entrypoint"]["path"], "composer/2.2/bin/composer")
        audit = json.loads((args["output_dir"] / "repack-manifest.json").read_text())
        self.assertEqual({r["source_path"]: r["target_path"] for r in audit["files"]},
                         {"composer-2.2": "composer/2.2/bin/composer", "notes": "composer/2.2/bin/notes"})

    def test_extended_tree_and_root_binary_packages_use_exact_v2_layouts(self):
        tree_payloads = {
            "go": {"bin/go.exe": b"go", "src/README": b"source"},
            "ruby": {"bin/ruby.exe": b"ruby", "lib/ruby.rb": b"library"},
            "openjdk": {"bin/java.exe": b"java", "conf/security": b"security"},
            "maven": {"bin/mvn.cmd": b"maven", "lib/maven-core.jar": b"jar"},
            "mysql": {"bin/mysqld.exe": b"mysqld", "share/error.txt": b"error"},
            "mariadb": {"bin/mysqld.exe": b"mysqld", "share/error.txt": b"error"},
            "mongodb": {"bin/mongod.exe": b"mongod", "bin/db": b"db"},
            "apache": {"bin/httpd.exe": b"httpd", "conf/httpd.conf": b"config"},
            "git": {"cmd/git.exe": b"git", "mingw64/bin/git.exe": b"git2"},
            "openssl": {"bin/openssl.exe": b"openssl", "lib/libssl.dll": b"ssl"},
            "phpmyadmin": {"index.php": b"<?php", "themes/default.css": b"css"},
            "mongosh": {"bin/mongosh.exe": b"mongosh", "snapshot.js": b"blob"},
            "mongotools": {"bin/bsondump.exe": b"tools", "deps.dll": b"dep"},
            "ai-gateway": {"bin/servbay-ai-gateway.exe": b"gateway", "share/readme": b"readme"},
            "disk-cleaner": {"bin/servbay-diskcleaner.exe": b"cleaner", "share/readme": b"readme"},
            "memcached": {"bin/memcached.exe": b"server", "include/config.h": b"header"},
            "rust": {"rustc/bin/rustc.exe": b"rustc", "cargo/bin/cargo.exe": b"cargo",
                     "rustc/manifest.in": b"rustc manifest", "cargo/manifest.in": b"cargo manifest",
                     "README.md": b"rust readme",
                     "rust-std-x86_64-pc-windows-msvc/lib/rustlib/x86_64-pc-windows-msvc/lib/libstd.rlib": b"std"},
        }
        versions = {
            "go": "1.27.3", "ruby": "3.4.12", "openjdk": "21.0.13",
            "maven": "3.9.15", "mysql": "8.4.13", "mariadb": "11.4.13",
            "mongodb": "8.2.12", "apache": "2.4.69", "git": "2.56.0",
            "openssl": "3.6.1", "phpmyadmin": "5.2.3", "mongosh": "2.13.0",
            "mongotools": "100.19.1", "ai-gateway": "1.0.0", "disk-cleaner": "1.0.0",
            "memcached": "1.6.45",
            "rust": "1.99.1",
        }
        binary_payloads = {
            "mailpit": ("mailpit.exe", "1.32.0"),
            "ollama": ("ollama.exe", "0.40.10"),
            "minio": ("minio.exe", "2025.10.15"),
            "meilisearch": ("meilisearch.exe", "1.55.0"),
            "deno": ("deno.exe", "2.9.8"),
            "bun": ("bun.exe", "1.4.4"),
            "caddy": ("caddy.exe", "2.11.7"),
            "cloudflared": ("cloudflared.exe", "2026.10.3"),
            "frp": ("frpc.exe", "0.71.1"),
            "ngrok": ("ngrok.exe", "3.39.13"),
            "pinggy": ("pinggy.exe", "0.6.1"),
        }
        cases = []
        for name, payload in tree_payloads.items():
            cases.append((name, versions[name], payload))
        for name, (executable, version) in binary_payloads.items():
            cases.append((name, version, {executable: executable.encode(),
                                           "share/notice.txt": b"notice"}))
        cases.append(("adminer", "6.2.0", {"adminer.php": b"<?php"}))

        for index, (name, version, payload) in enumerate(cases):
            with self.subTest(name=name):
                args = self.arguments(name, payload, label=f"extended-{index}", version=version)
                meta = self.module.repack(**args)
                audit = json.loads((args["output_dir"] / "repack-manifest.json").read_text())
                root = meta["package_root"]
                mapping = {row["source_path"]: row["target_path"] for row in audit["files"]}
                if name in self.module.TREE_PRESERVING:
                    expected = {path: root + "/" + path for path in payload}
                elif name in self.module.ROOT_BINARY:
                    expected = {path: root + "/" + ("bin/" + path if "/" not in path else path)
                                for path in payload}
                elif name == "rust":
                    expected = {}
                    for path in payload:
                        if "/" in path:
                            component, relative = path.split("/", 1)
                            if relative == "manifest.in":
                                relative = "share/doc/rust/components/" + component + "/manifest.in"
                        else:
                            relative = path
                        expected[path] = root + "/" + relative
                else:
                    expected = {"adminer.php": root + "/index.php"}
                self.assertEqual(mapping, expected)
                self.assertEqual(meta["entrypoint"], self.module.package_layout.package_layout(
                    name, version, os="windows", arch="x86_64")["entrypoint"])
                self.assertEqual(set(self.module.windows_archive.archive_hashes(
                    args["output_dir"] / meta["filename"], SEVEN_ZIP)), set(expected.values()))

    def test_source_sha_size_and_version_are_explicit_and_exact(self):
        args = self.arguments()
        for field, value in [("sha256", "0" * 64), ("bytes", args["source"]["bytes"] + 1),
                             ("version", "1.31.9"), ("sha256", None), ("bytes", True)]:
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(args)
                bad["source"][field] = value
                self.reject(bad)

        for field in ("sha256", "bytes", "version"):
            bad = copy.deepcopy(args)
            del bad["source"][field]
            self.reject(bad)

    def test_excluded_simple_acme_cannot_use_the_ordinary_v2_producer(self):
        self.reject(self.arguments("simple-acme", {"wacs.exe": b"acme"}, version="2.4.1"))

    def test_every_required_entry_or_nginx_companion_is_required(self):
        required = {"nginx": ["nginx.exe", "conf/nginx.conf", "conf/mime.types"],
                    "redis": ["redis-server.exe", "redis-cli.exe"], "python": ["python.exe"],
                    "node": ["node.exe"], "composer": ["composer"]}
        for name, entries in required.items():
            for index, entry in enumerate(entries):
                with self.subTest(name=name, entry=entry):
                    payload = dict(self.payloads[name])
                    del payload[entry]
                    self.reject(self.arguments(name, payload, label=f"{name}-{index}"))

    def test_wrapped_v1_input_is_not_guessed_or_stripped(self):
        for name, payload in self.payloads.items():
            with self.subTest(name=name):
                self.reject(self.arguments(name, {"wrapper/" + p: data for p, data in payload.items()}, label=name))

    def test_identity_producer_minimum_os_and_public_url_use_shared_validation(self):
        args = self.arguments()
        for key, value in [("name", "unknown"), ("name", "php"), ("name", "postgresql"),
                           ("name", "typesense"),
                           ("arch", "arm64"), ("arch", "x86"), ("os", "macos"),
                           ("runtime_version", "v1"), ("kind", "php-extension"),
                           ("version", "bad/version"), ("build_revision", -1),
                           ("entrypoint", {"path": "invented"}), ("series", "fake")]:
            with self.subTest(key=key, value=value):
                bad = copy.deepcopy(args)
                bad["identity"][key] = value
                self.reject(bad)
        for key, value in [("producer", {"commit": "bad", "run_id": "23456", "run_attempt": 3}),
                           ("producer", dict(self.producer, extra="ignored")), ("minimum_os", None)]:
            bad = copy.deepcopy(args)
            bad[key] = value
            self.reject(bad)
        bad = copy.deepcopy(args)
        bad["source"]["url"] = "https://user:secret@example.org/input.7z"
        self.reject(bad)

    def test_unknown_source_fields_cannot_smuggle_credentials(self):
        args = self.arguments()
        args["source"]["token"] = "secret"
        self.reject(args)

    def test_php_and_postgresql_mixed_payloads_cannot_impersonate_ordinary_packages(self):
        for index, foreign in enumerate(("php.exe", "ext/php-cgi.exe", "bin/postgres.exe", "pg_ctl.exe")):
            with self.subTest(foreign=foreign):
                self.reject(self.arguments(payload=dict(self.payloads["nginx"], **{foreign: b"foreign"}), label=str(index)))

    def test_target_mapping_collisions_are_rejected(self):
        for index, extra in enumerate(({"sbin/nginx.exe": b"collision"},
                                       {"share/defaults/conf/nginx.conf": b"collision"})):
            with self.subTest(extra=extra):
                self.reject(self.arguments(payload=dict(self.payloads["nginx"], **extra), label=str(index)))
        args = self.arguments("composer", {"composer-2.2": b"phar", "composer": b"collision"},
                              version="2.2.30", label="composer")
        self.reject(args)

    def test_unsafe_real_archive_paths_case_conflicts_and_links_are_rejected(self):
        for index, name in enumerate(("NUL.txt", "a:stream", "dir/trailing.")):
            with self.subTest(name=name):
                args = self.arguments(label="unsafe-" + str(index))
                archive = Path(args["source"]["path"])
                result = subprocess.run([SEVEN_ZIP, "a", "-ssc", str(archive), "-si" + name],
                                        input=b"raw invalid member", capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)
                args["source"].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                                      bytes=archive.stat().st_size)
                with self.assertRaisesRegex(ValueError, "Invalid Windows relative path"):
                    self.module.windows_archive.archive_hashes(archive, SEVEN_ZIP)
                self.reject(args)
        # APFS staging is case-insensitive: write colliding members straight to
        # 7z via stdin so the fixture actually contains both Windows names.
        for index, name in enumerate(("NGINX.exe", "CONF/other")):
            args = self.arguments(label="case-" + str(index))
            archive = Path(args["source"]["path"])
            result = subprocess.run([SEVEN_ZIP, "a", "-ssc", str(archive), "-si" + name],
                                    input=b"distinct colliding bytes", capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            listed = subprocess.run([SEVEN_ZIP, "l", "-slt", "-ba", str(archive)],
                                    capture_output=True, text=True, check=True).stdout.replace("\\", "/")
            self.assertIn("Path = " + name + "\n", listed)
            self.assertIn("Path = nginx.exe\n", listed)
            self.assertIn("Path = conf/nginx.conf\n", listed)
            args["source"].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), bytes=archive.stat().st_size)
            self.reject(args)
        args = self.arguments(label="symlink")
        tree = self.base / "symlink-tree"
        (tree / "link").symlink_to("nginx.exe")
        result = subprocess.run([SEVEN_ZIP, "a", "-snl", str(args["source"]["path"]), "link"],
                                cwd=tree, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = Path(args["source"]["path"])
        args["source"].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), bytes=archive.stat().st_size)
        self.reject(args)

    def test_existing_output_is_preserved_and_source_is_never_overwritten(self):
        args = self.arguments()
        args["output_dir"].mkdir()
        sentinel = args["output_dir"] / "keep"
        sentinel.write_bytes(b"user bytes")
        self.reject(args)
        self.assertEqual(sentinel.read_bytes(), b"user bytes")
        args["output_dir"] = Path(args["source"]["path"])
        before = args["output_dir"].read_bytes()
        self.reject(args)
        self.assertEqual(args["output_dir"].read_bytes(), before)

    def test_input_changes_during_copy_cannot_produce_success(self):
        args = self.arguments()
        alternate = self.archive(dict(self.payloads["nginx"], **{"nginx.exe": b"different valid exe"}), "alternate")
        original = self.module.shutil.copyfile
        source = Path(args["source"]["path"])

        def changed_copy(src, dst, *a, **kw):
            result = original(src, dst, *a, **kw)
            if Path(src) == source:
                Path(dst).write_bytes(alternate.read_bytes())
            return result

        with patch.object(self.module.shutil, "copyfile", side_effect=changed_copy):
            self.reject(args)

    def test_original_changes_after_freeze_cannot_change_payload_or_provenance(self):
        args = self.arguments()
        hashes = self.module.windows_archive.extract_verified_archive
        original = Path(args["source"]["path"])

        def replace_original(archive, destination, seven_zip=None):
            original.write_bytes(b"original was changed after the input froze")
            return hashes(archive, destination, seven_zip)

        with patch.object(self.module.windows_archive, "extract_verified_archive", side_effect=replace_original):
            meta = self.module.repack(**args)
        self.assertEqual(meta["source"]["sha256"], args["source"]["sha256"])
        audit = json.loads((args["output_dir"] / "repack-manifest.json").read_text())
        self.assertEqual(audit["source"]["sha256"], args["source"]["sha256"])
        self.assertEqual({r["source_path"]: r["sha256"] for r in audit["files"]},
                         {name: hashlib.sha256(data).hexdigest() for name, data in self.payloads["nginx"].items()})

    def test_archive_re_read_detects_corrupt_packaging_before_metadata(self):
        args = self.arguments()
        create = self.module.windows_archive.create_archive

        def corrupt(staging, archive, names, seven_zip=None, compression_level=7):
            create(staging, archive, names, seven_zip, compression_level=compression_level)
            Path(archive).write_bytes(b"not a 7z archive")

        with patch.object(self.module.windows_archive, "create_archive", side_effect=corrupt):
            self.reject(args)

    def test_cli_consumes_three_json_files_and_explicit_minimum_os(self):
        args = self.arguments()
        command = [sys.executable, str(SCRIPT), "--minimum-os", args["minimum_os"],
                   "--output-dir", str(args["output_dir"]), "--seven-zip", SEVEN_ZIP]
        for field in ("source", "identity", "producer"):
            path = self.base / (field + ".json")
            path.write_text(json.dumps(args[field]))
            command.extend(["--" + field, str(path)])
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), json.loads((args["output_dir"] / "meta.json").read_text()))


if __name__ == "__main__":
    unittest.main()
