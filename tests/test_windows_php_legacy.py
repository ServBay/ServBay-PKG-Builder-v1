"""Contract tests for the PHP 7.0/7.1 Windows build.

The legacy build cannot run on macOS, so these tests lock down the wiring that
has already broken once and would otherwise only surface 40 minutes into a
runner job:

  * the patch directory and file naming used by the workflow,
  * the dependency archive URL following the requested architecture,
  * the configure flag set producing a shippable, extension-complete PHP,
  * the artifact name the downstream Uploader discovers,
  * untrusted workflow inputs staying out of shell text.
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/build-windows-php-legacy.yml"
DISPATCH = ROOT / ".github/workflows/build-and-dispatch.yml"
STOCK_WINDOWS = ROOT / ".github/workflows/build-windows.yml"
PS1 = ROOT / "runtime-windows/ci/build_php_legacy.ps1"
SHIM = ROOT / "runtime-windows/ci/vc14-intrinsics.h"
PATCH_ROOT = ROOT / "runtime/patch"

# The branches whose official dependency bundles exist for VC14.
VC14_VERSIONS = ["7.0.33.1", "7.1.33.1"]


def major_minor(version):
    parts = version.split(".")
    return ".".join(parts[:2])


class WindowsPhpLegacyPatchTests(unittest.TestCase):
    def test_patch_directory_follows_php_prefix(self):
        # runtime/patch/<branch> is named php7.0, not 7.0. The workflow once
        # joined the bare version and failed on the runner.
        for version in VC14_VERSIONS:
            branch_dir = PATCH_ROOT / f"php{major_minor(version)}"
            self.assertTrue(
                branch_dir.is_dir(),
                f"{branch_dir} is missing; the workflow derives php<major.minor>",
            )
            self.assertTrue(
                list(branch_dir.glob("cve-*.patch")),
                f"{branch_dir} carries no cve-*.patch",
            )

    def test_workflow_prefixes_patch_directory(self):
        text = WORKFLOW.read_text()
        self.assertRegex(text, r'Join-Path\s+\$patchRoot\s+"php\$major"')

    def test_only_cve_patches_are_applied(self):
        # runtime/patch/php7.x also holds macOS-only workarounds (gd configure
        # run test, readdir_r, tidy buffio). Applying those on Windows fails.
        text = PS1.read_text()
        self.assertIn("-Filter 'cve-*.patch'", text)
        self.assertNotRegex(text, r"-Filter\s*\"\$BaseVersion-\*\.patch\"")


class WindowsPhpLegacyBuildTests(unittest.TestCase):
    def test_dependency_archive_follows_requested_arch(self):
        # A hardcoded -x64 archive silently produces a broken x86 build.
        text = PS1.read_text()
        self.assertIn("deps-$MajorMinor-vc14-$Arch.7z", text)
        self.assertNotIn("deps-7.0-vc14-x64.7z", text)

    def test_rejects_branches_without_vc14_deps(self):
        text = PS1.read_text()
        self.assertIn("$MajorMinor -notin @('7.0', '7.1')", text)

    def test_toolset_prefers_v140_and_falls_back(self):
        # Microsoft no longer ships the v140 toolset to hosted runners: the
        # standalone VS2015 bootstrapper is retired, and asking the runner's
        # own installer for the VC.140 component returns 0 without changing
        # anything. The build therefore has to accept the newest toolset the
        # image does have.
        text = PS1.read_text()
        self.assertNotIn("aka.ms/vs/15/release/vs_buildtools.exe", text)
        self.assertIn(r"^14\.0\d*\.", text)
        self.assertIn("MSVC v140 is not available", text)
        # configure.js and the dependency bundle key off the compiler, and the
        # toolset is only selected explicitly. Both the configure and the nmake
        # invocation need it.
        self.assertEqual(text.count("-vcvars_ver=$ToolsetVer"), 2)

    def test_build_tree_copies_vc9_from_its_phpdev_sibling(self):
        # buildtree creates phpdev\vc9, not phpdev\vc14\vc9. Looking in the
        # wrong place failed the first hosted build outright.
        text = PS1.read_text()
        self.assertNotIn("Join-Path $BuildRoot 'vc9'", text)
        self.assertIn("Join-Path $PhpDevRoot 'vc9'", text)

    def test_configure_enables_the_release_module_set(self):
        # --disable-all produced a bare CLI with no ext/ at all, while every
        # other Windows PHP package ships the 37 php_*.dll of the official
        # release. --enable-snapshot-build is what php.net itself uses.
        text = PS1.read_text()
        self.assertIn("--enable-snapshot-build", text)
        self.assertNotIn("--disable-all", text)

    def test_runtime_dependencies_are_packed(self):
        # php -v only proves the CLI starts. Without the dependency bundle's
        # DLLs (OpenSSL, ICU, libpq, libssh2) the matching extensions fail to
        # load on a clean machine, so they have to be in the archive.
        text = PS1.read_text()
        self.assertIn("deps\\bin", text)
        self.assertIn("'_debug\\.dll$'", text)
        self.assertIn("php.ini-production", text)

    def test_version_check_flattens_captured_output(self):
        # `php -v` is multi-line, and -notmatch against an array filters it,
        # so every line but the first makes the condition true. The first
        # hosted build failed its own sanity check this way.
        text = PS1.read_text()
        self.assertNotIn("$Verify -notmatch", text)
        self.assertIn("$VerifyText -notmatch", text)

    def test_ooms_are_gated_on_the_exit_path(self):
        # The whole point of the backport: an allocated-out process must leave
        # through _exit(1), not longjmp. Vanilla PHP exits 255, so asserting on
        # 1 is what actually proves the patch reached the binary.
        text = PS1.read_text()
        self.assertIn("-d memory_limit=2M", text)
        self.assertRegex(text, r"\$OomExit\s+-ne\s+1")

    def test_intrinsics_shim_is_scoped_to_old_compilers(self):
        # _mm_loadu_si64 exists from VS2017 on; overriding it unconditionally
        # would shadow the real implementation on modern toolchains.
        text = SHIM.read_text()
        self.assertIn("_MSC_VER < 1910", text)
        self.assertIn("_mm_loadu_si64", text)


class WindowsPhpLegacyWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_artifact_name_matches_downstream_contract(self):
        stock = STOCK_WINDOWS.read_text()
        legacy = WORKFLOW.read_text()
        pattern = r"name:\s*pkg-windows-\$\{\{ env\.ARCH \}\}-\$\{\{ inputs\.pkg \}\}-\$\{\{ inputs\.version \}\}"
        self.assertRegex(stock, pattern)
        self.assertIn(
            "name: pkg-windows-${{ env.ARCH }}-php-${{ env.VERSION }}", legacy
        )
        self.assertIn(
            "name: log-windows-${{ env.ARCH }}-php-${{ env.VERSION }}", legacy
        )

    def test_meta_json_matches_downstream_contract(self):
        job = self.workflow["jobs"]["build"]
        step = next(s for s in job["steps"] if s.get("id") == "meta")
        for key in ("name", "version", "arch", "os", "sha256", "filename"):
            self.assertIn(key, step["run"])
        self.assertIn("'windows'", step["run"])

    def test_inputs_are_never_interpolated_into_shell_text(self):
        # Everything the dispatcher supplies goes through env: so a crafted
        # version string cannot become shell syntax.
        for step in self.workflow["jobs"]["build"]["steps"]:
            run = step.get("run")
            if not run:
                continue
            self.assertNotIn("${{ inputs.", run, f"step {step.get('name')} inlines an input")
            self.assertNotIn("${{ github.event", run)

    def test_dispatch_routes_legacy_php_away_from_the_ubuntu_pipeline(self):
        dispatch = yaml.safe_load(DISPATCH.read_text())
        jobs = dispatch["jobs"]
        self.assertIn("build-windows-legacy", jobs)
        self.assertEqual(
            jobs["build-windows-legacy"]["uses"],
            "./.github/workflows/build-windows-php-legacy.yml",
        )
        # The normal pipeline must stand down for the same versions, otherwise
        # both jobs build php and the second one overwrites the artifact.
        normal = jobs["build-windows"]["if"]
        self.assertIn("startsWith(inputs.windows_version, '7.0.')", normal)
        self.assertIn("startsWith(inputs.windows_version, '7.1.')", normal)
        self.assertIn("build-windows-legacy", jobs["dispatch-downstream"]["needs"])


if __name__ == "__main__":
    unittest.main()
