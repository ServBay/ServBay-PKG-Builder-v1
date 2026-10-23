import hashlib
import http.server
import importlib.util
import io
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile

import test_build_scripts as legacy_tests


ROOT = Path(__file__).resolve().parents[1]


class DownloadInputTests(unittest.TestCase):
    def setUp(self):
        self.recipe = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.helper = legacy_tests.BuildScriptTests(methodName="runTest")
        spec = importlib.util.spec_from_file_location("download_profile", ROOT / "runtime/ci/build_profile.py")
        self.profile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.profile)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def download_and_unpack(self, package, version, filename, payload, web=False):
        observed = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = self.root / package
        root.mkdir()
        base = root / "base"
        source = root / "src"
        script = [f"HOME={shlex.quote(str(root / 'home'))}",
                  f"SERVBAY_BASE_FULL_PATH={shlex.quote(str(base))}",
                  f"SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(base / 'packages'))}",
                  f"SERVBAY_COMMON_PATH={shlex.quote(str(base / 'packages/common'))}",
                  f"SERVBAY_BUILD_SRC_DIR={shlex.quote(str(source))}",
                  f"SERVBAY_BUILD_DIR={shlex.quote(str(root))}",
                  f"FIXTURE_URL=http://127.0.0.1:{server.server_port}/source",
                  "architecture=x86_64", "log_message() { :; }",
                  "curl() { local -a arguments=(\"$@\"); arguments[-1]=\"$FIXTURE_URL\"; /usr/bin/curl \"${arguments[@]}\"; }"]
        for name in ("get_extension", "get_download_url", "download_source", "trash_path", "unpack_package", "build_phpmyadmin"):
            body = self.helper._extract_shell_function(self.recipe, name)
            script.append(name + "() {\n" + body + "\n}")
        script += [f"package={shlex.quote(package)}", f"version={shlex.quote(version)}", f"upstream={shlex.quote(filename)}",
                   'extension=$(get_extension "$upstream" "$package") || exit 1',
                   'standard="$package-$version.$extension"',
                   'url=$(get_download_url "$package" "$version" "$upstream") || exit 1',
                   'print -r -- "$url" > upstream-url',
                   'download_source "$url" "$SERVBAY_BUILD_SRC_DIR/$standard" || exit 1']
        if web:
            script.append('build_phpmyadmin "$version" "$standard" || exit 1')
        else:
            script.append('unpack_package "$package-$version" "$standard" || exit 1')
        try:
            result = subprocess.run(["/bin/zsh", "-c", "\n".join(script)], cwd=root, capture_output=True, text=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["/source"], observed, "The real downloader must fetch the selected upstream input")
        return root, (root / "upstream-url").read_text().strip()

    def test_phpmyadmin_real_zip_download_and_unpack_preserve_its_v2_web_layout(self):
        filename = "phpMyAdmin-5.2.3-all-languages.zip"
        data = io.BytesIO()
        contents = b"<?php const VERSION = '5.2.3';\n"
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("phpMyAdmin-5.2.3-all-languages/libraries/classes/Version.php", contents)
        root, url = self.download_and_unpack("phpmyadmin", "5.2.3", filename, data.getvalue(), web=True)
        self.assertEqual("https://files.phpmyadmin.net/phpMyAdmin/5.2.3/" + filename, url)
        self.assertEqual(data.getvalue(), (root / "src/phpmyadmin-5.2.3.zip").read_bytes())
        self.assertEqual(contents, (root / "base/www/phpmyadmin/libraries/classes/Version.php").read_bytes())
        self.assertEqual("5.2.3\n", (root / "base/www/phpmyadmin/VERSION").read_text())

    def test_real_tgz_inputs_use_the_recipe_normalized_cache_for_metadata(self):
        updater = self.profile._updater.PackageUpdater(str(ROOT / "runtime-v2/config/packages.conf"), runtime_version="v2")
        rows = updater._v2_rows()
        for package in ("python", "mongodb", "cloudflared"):
            with self.subTest(package=package):
                selected = max((row for row in rows if row["name"] == package and not row["is_commented"]
                                and row["x86_file"].endswith(".tgz")), key=lambda row: updater.version_to_tuple(row["version"]))
                version, filename = selected["version"], selected["x86_file"]
                data = io.BytesIO()
                contents = (package + " " + version).encode()
                with tarfile.open(fileobj=data, mode="w:gz") as archive:
                    member = tarfile.TarInfo("upstream/README")
                    member.size = len(contents)
                    archive.addfile(member, io.BytesIO(contents))
                root, _ = self.download_and_unpack(package, version, filename, data.getvalue())
                conf = root / "packages.conf"
                conf.write_text("\t".join([package, version, filename, filename, selected["build_type"]]) + "\n")
                found = self.profile.find_source(root, package, version, "x86_64", conf)
                self.assertEqual(root / "src" / f"{package}-{version}.tar.gz", Path(found["path"]))
                self.assertEqual(hashlib.sha256(data.getvalue()).hexdigest(), hashlib.sha256(Path(found["path"]).read_bytes()).hexdigest())
                self.assertEqual(contents, (root / "src" / f"{package}-{version}/README").read_bytes())


if __name__ == "__main__":
    unittest.main()
