from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import test_build_scripts as legacy_tests


ROOT = Path(__file__).resolve().parents[1]


class RecipeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "runtime-v2/scripts/build_package.sh").read_text()
        self.helper = legacy_tests.BuildScriptTests(methodName="runTest")

    def test_openjdk_legacy_and_feature_only_banners_use_v2_paths(self):
        # The existing v1 harness executes the real case, with the v2 install path.
        text = self.text.replace('"openjdk"|"java")', '"openjdk")')
        for version, banner in (("8.0.504", "1.8.0_504"), ("27.0.0", "27")):
            major = version.split('.')[0]
            output = self.helper._run_verify_case(text, "openjdk", version, f"openjdk/{major}/bin/java",
                                                  f'echo \'openjdk version "{banner}"\' >&2')
            self.assertTrue(self.helper._core_verify_passes("openjdk", version, output), output)
        bad = self.helper._run_verify_case(text, "openjdk", "27.0.0", "openjdk/27/bin/java",
                                           'echo "exec format error: $0" >&2')
        self.assertFalse(self.helper._core_verify_passes("openjdk", "27.0.0", bad))

    def test_dotnet_prerelease_reports_the_public_version_only_for_a_working_sdk(self):
        output = self.helper._run_verify_case(self.text, "dotnetsdk", "11.0.100-rc1", "dotnetsdk/11.0/dotnet",
                                              'echo "11.0.100-rc.1.26425.128"')
        self.assertTrue(self.helper._core_verify_passes("dotnetsdk", "11.0.100-rc1", output), output)
        bad = self.helper._run_verify_case(self.text, "dotnetsdk", "11.0.100-rc1", "dotnetsdk/11.0/dotnet",
                                           'echo "exec format error: $0" >&2')
        self.assertFalse(self.helper._core_verify_passes("dotnetsdk", "11.0.100-rc1", bad))

    def test_composer_can_be_verified_on_a_base_without_an_installed_php_package(self):
        output = self.helper._run_verify_case(self.text, "composer", "2.10.3", "composer/2/bin/composer",
                                              "# const VERSION = '2.10.3';")
        self.assertTrue(self.helper._core_verify_passes("composer", "2.10.3", output), output)

    def test_modern_ollama_requires_its_native_helpers_before_handoff(self):
        output = self.helper._run_verify_case(self.text, "ollama", "0.40.6", "ollama/bin/ollama",
                                              'echo "ollama version 0.40.6"')
        self.assertFalse(self.helper._core_verify_passes("ollama", "0.40.6", output), output)

    def test_caddy_and_minio_keep_the_v1_go_toolchain_and_deployment_floor(self):
        for recipe in ("caddy", "minio"):
            with self.subTest(recipe=recipe), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls = root / "calls"
                executable = root / "go"
                executable.write_text('#!/bin/zsh\nprint -r -- "$GOTOOLCHAIN|$MACOSX_DEPLOYMENT_TARGET|$CGO_CFLAGS|$CGO_LDFLAGS" >> "$CALLS"\n')
                executable.chmod(0o755)
                body = self.helper._extract_shell_function(self.text, "build_" + recipe)
                script = f'''SERVBAY_BUILD_DIR={shlex.quote(str(root))}
SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(root / 'base/packages'))}
SERVBAY_ETC_PATH={shlex.quote(str(root / 'base/packages/etc'))}
SERVBAY_LOG_PATH={shlex.quote(str(root / 'base/packages/var/log'))}
BUILD_OS_MIN_VERSION=12.00
GO_BIN={shlex.quote(str(executable))}
PATH={shlex.quote(str(root))}:$PATH
export CALLS={shlex.quote(str(calls))}
log_message() {{ :; }}
unpack_package() {{ :; }}
use_pinned_go_1_26() {{ export GOTOOLCHAIN=go1.26.0; }}
xcaddy() {{ go "$@"; }}
build_{recipe}() {{
{body}
}}
build_{recipe} 2.11.7 source.tar.gz || exit 1
'''
                result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                observed = calls.read_text().splitlines()
                self.assertTrue(observed)
                for call in observed:
                    self.assertEqual("go1.26.0|12.0|-mmacosx-version-min=12.0 |-mmacosx-version-min=12.0 ", call)

    def test_phpmyadmin_rebuild_moves_only_its_managed_web_root_to_trash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed = root / "base/www/phpmyadmin"
            managed.mkdir(parents=True)
            (managed / "old").write_text("recoverable")
            other = root / "base/www/customer"
            other.mkdir()
            (other / "keep").write_text("untouched")
            cleanup = self.helper._extract_shell_function(self.text, "trash_path")
            body = self.helper._extract_shell_function(self.text, "build_phpmyadmin")
            script = f'''HOME={shlex.quote(str(root / 'home'))}
SERVBAY_BASE_FULL_PATH={shlex.quote(str(root / 'base'))}
SERVBAY_BUILD_SRC_DIR={shlex.quote(str(root / 'src'))}
SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(root / 'base/packages'))}
SERVBAY_COMMON_PATH=$SERVBAY_PACKAGE_FULL_PATH/common
log_message() {{ :; }}
trash_path() {{
{cleanup}
}}
unpack_package() {{ mkdir -p "$3"; print -r -- new > "$3/index.php"; }}
build_phpmyadmin() {{
{body}
}}
build_phpmyadmin 5.2.3 source.zip || exit 1
trash_path {shlex.quote(str(other))} && exit 9
exit 0
'''
            result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("new\n", (managed / "index.php").read_text())
            self.assertFalse((managed / "old").exists())
            self.assertEqual("recoverable", next((root / "home/.Trash").rglob("old")).read_text())
            self.assertEqual("untouched", (other / "keep").read_text())

    def test_migrated_ollama_keeps_the_existing_v2_bin_install_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "llama").mkdir(parents=True)
            (source / "app/ui").mkdir(parents=True)
            (source / "llama/llama.go").write_text("// fixture\n")
            (source / "app/ui/ui.go").write_text("// fixture\n")
            c = root / "probe.c"
            c.write_text('int main(void) { return 0; }\n')
            binary = root / "fixture"
            subprocess.run(["/usr/bin/clang", "-mmacosx-version-min=14.0", str(c), "-o", str(binary)],
                           check=True, capture_output=True)
            body = self.helper._extract_shell_function(self.text, "build_ollama")
            verify = self.helper._extract_shell_function(self.text, "verify_macos_min_version")
            prefix = root / "base/packages/ollama"
            script = f'''SERVBAY_BUILD_DIR={shlex.quote(str(root))}
SERVBAY_PACKAGE_FULL_PATH={shlex.quote(str(root / 'base/packages'))}
SERVBAY_ETC_PATH={shlex.quote(str(root / 'base/packages/etc'))}
SERVBAY_DB_PATH={shlex.quote(str(root / 'base/db'))}
SERVBAY_LOG_PATH={shlex.quote(str(root / 'base/packages/var/log'))}
PATCH_DIR={shlex.quote(str(root / 'no-patches'))}
GO_BIN=go
log_message() {{ :; }}
unpack_package() {{ builtin cd {shlex.quote(str(source))}; }}
go() {{
    if [[ "$1" == build ]]; then
        while [[ $# -gt 0 ]]; do
            if [[ "$1" == -o ]]; then
                shift
                mkdir -p "${{1:h}}"
                cp {shlex.quote(str(binary))} "$1"
                return $?
            fi
            shift
        done
        return 1
    fi
    return 0
}}
verify_macos_min_version() {{
{verify}
}}
build_ollama() {{
{body}
}}
build_ollama 0.40.6 source.tar.gz || exit 1
'''
            result = subprocess.run(["/bin/zsh", "-c", script], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((prefix / "bin/ollama").is_file())
            self.assertFalse((prefix / "ollama").exists())


if __name__ == "__main__":
    unittest.main()
