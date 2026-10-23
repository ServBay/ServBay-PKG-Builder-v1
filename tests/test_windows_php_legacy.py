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

# Every EOL branch that ships as a Windows package. 7.0/7.1 have an official
# VC14 dependency bundle; 7.2+ take the per-library series manifest instead.
EOL_VERSIONS = [
    "5.6.40.1",
    "7.0.33.1",
    "7.1.33.1",
    "7.2.34.1",
    "7.3.33.1",
    "7.4.33.1",
    "8.0.30.1",
    "8.1.34.1",
]
VC14_VERSIONS = ["7.0.33.1", "7.1.33.1"]

# The compiler branch each PHP branch was released against. The series
# manifest is keyed by it, so getting this wrong downloads the wrong CRT's
# libraries and links instead of failing outright.
CRT_BY_BRANCH = {
    "5.6": "vc11",
    "7.0": "vc14",
    "7.1": "vc14",
    "7.2": "vc15",
    "7.3": "vc15",
    "7.4": "vc15",
    "8.0": "vs16",
    "8.1": "vs16",
}


def major_minor(version):
    parts = version.split(".")
    return ".".join(parts[:2])


class WindowsPhpLegacyPatchTests(unittest.TestCase):
    def test_patch_directory_follows_php_prefix(self):
        # runtime/patch/<branch> is named php7.0, not 7.0. The workflow once
        # joined the bare version and failed on the runner.
        for version in EOL_VERSIONS:
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
        self.assertIn("deps-$MajorMinor-$Crt-$Arch.7z", text)
        self.assertNotIn("deps-7.0-vc14-x64.7z", text)

    def test_rejects_branches_outside_the_supported_series(self):
        # The script has no dependency source for anything outside the table,
        # so it must fail before configure rather than build a bare CLI.
        text = PS1.read_text()
        self.assertIn("$CrtByBranch", text)
        self.assertIn("does not cover PHP", text)

    def test_toolset_prefers_the_branch_toolset_and_falls_back(self):
        # Microsoft no longer ships the v140 toolset to hosted runners: the
        # standalone VS2015 bootstrapper is retired, and asking the runner's
        # own installer for the VC.140 component returns 0 without changing
        # anything. The build therefore has to accept the newest toolset the
        # image does have.
        text = PS1.read_text()
        self.assertNotIn("aka.ms/vs/15/release/vs_buildtools.exe", text)
        self.assertIn(r"'^14\.0\d*\.'", text)
        self.assertIn(r"'^14\.1\d*\.'", text)
        self.assertIn(r"'^14\.2\d*\.'", text)
        self.assertIn("toolset is available; building PHP", text)
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

    def test_package_layout_matches_an_official_build(self):
        # Release\ mirrors the source tree and is full of .obj/.res/.pdb. The
        # package has to be staged instead: binaries at the root, extensions
        # under ext\, or nothing loads on an installed machine.
        text = PS1.read_text()
        self.assertIn("$StageExt = Join-Path $Stage 'ext'", text)
        self.assertIn("-Filter 'php_*.dll'", text)
        self.assertIn("Push-Location $Stage", text)

    def test_runtime_dependencies_are_packed(self):
        # php -v only proves the CLI starts. Without the dependency bundle's
        # DLLs (OpenSSL, ICU, libpq, libssh2) the matching extensions fail to
        # load on a clean machine, so they have to be in the archive.
        text = PS1.read_text()
        self.assertIn("deps\\bin", text)
        self.assertIn("'_debug\\.dll$'", text)
        self.assertIn("php.ini-production", text)

    def test_staged_layout_is_smoke_tested(self):
        # The extensions that need OpenSSL, ICU, libpq and libsasl are what
        # break when the dependency DLLs are absent or misplaced, so the
        # staged copy is exercised before it is packed.
        text = PS1.read_text()
        self.assertIn("extension_dir=$StageExt", text)
        self.assertIn("Unable to (load dynamic library|initialize module)", text)

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


class WindowsPhpEolBranchTests(unittest.TestCase):
    """PHP 7.2-8.1 have no VC14 bundle; they build from the series manifest."""

    def test_crt_table_covers_every_eol_branch(self):
        text = PS1.read_text()
        for branch, crt in CRT_BY_BRANCH.items():
            self.assertRegex(
                text,
                rf"'{re.escape(branch)}'\s*=\s*'{crt}'",
                f"{branch} must map to {crt}",
            )

    def test_upper_branches_read_the_series_manifest(self):
        # php.net publishes one deps/series/packages-<branch>-<crt>-<arch>-
        # stable.txt per branch. The branch is what selects the library
        # versions; guessing latest would link a newer CRT's import libraries.
        text = PS1.read_text()
        self.assertIn("deps/series/packages-$MajorMinor-$Crt-$Arch-stable.txt", text)
        self.assertIn("deps/$Crt/$Arch/$name", text)

    def test_upper_branches_use_the_modern_sdk(self):
        # SDK 2.1 (20110915) predates 7.2 and cannot build it; its buildtree
        # also lacks the vc15/vs16 layouts.
        text = PS1.read_text()
        self.assertIn("php-sdk-2.8.4", text)
        self.assertIn("phpsdk_buildtree.bat", text)

    def test_aggregate_bundle_stays_single_archive(self):
        # vc11 (5.5/5.6) and vc14 (7.0/7.1) are the branches php.net shipped
        # one aggregate bundle for; keep that path, it is the one already
        # proven on the runner.
        text = PS1.read_text()
        self.assertIn("deps-$MajorMinor-$Crt-$Arch.7z", text)

    def test_intrinsics_shim_is_vc14_only(self):
        # _mm_loadu_si64 exists from VS2017 on. Injecting the shim into a
        # vc15/vs16 build would shadow the real intrinsic.
        text = PS1.read_text()
        self.assertRegex(text, r"if\s*\(\s*\$Crt\s+-eq\s+'vc14'\s*\)")

    def test_pecl_extensions_are_staged(self):
        # --enable-snapshot-build builds the core ext/ modules only. The
        # published packages also carry imagick, redis, mongodb and friends,
        # so a source build without them is a regression.
        text = PS1.read_text()
        self.assertIn("$ExtManifest", text)
        self.assertIn("php_*.dll", text)
        self.assertIn("$StageExt", text)

    def test_extension_runtime_dependencies_travel_with_the_package(self):
        # A PECL archive bundles what the module links against: ImageMagick's
        # CORE_RL_*/IM_MOD_RL_*, libyaml's yaml.dll. Dropping those left
        # php_imagick and php_yaml unable to load ("The specified module could
        # not be found"). An official build keeps them under ext\ beside the
        # module that loads them.
        # Windows resolves an extension's imports against php.exe's
        # directory, not ext\, so the runtime goes to the package root.
        text = PS1.read_text()
        self.assertRegex(text, r"Copy-Item \$dll\.FullName -Destination \$Stage -Force")

    def test_pecl_extensions_are_smoke_tested(self):
        text = PS1.read_text()
        self.assertRegex(text, r"extension=php_\$ext\.dll")

    def test_module_build_id_matches_the_official_extensions(self):
        # configure.js stops mapping toolsets at VS2017, so a newer cl.exe
        # yields the raw version ("19.44.35229") while every php.net and PECL
        # extension was built as ",VC15"/",VS16". The loader then refuses
        # them. The build sets the ID to the branch's official one.
        text = PS1.read_text()
        self.assertIn("PHP_COMPILER_ID", text)
        self.assertIn("$Crt.ToUpper()", text)
        self.assertIn("main\\config.w32.h", text)
        # Branches that recognise the toolset write a bare identifier, the
        # ones that do not write a quoted version, so both have to match.
        self.assertIn("(?m)^#define\\s+PHP_COMPILER_ID", text)
        # 8.0/8.1 already emit the right identifier; the smoke test is the
        # guard there, so skipping the rewrite must be recorded.
        self.assertIn("keeping the detected value", text)
        self.assertIn("#define\\s+PHP_COMPILER_ID", text)

    def test_smoke_test_checks_loaded_modules_not_just_exit_code(self):
        # "Unable to initialize module" is a warning: php.exe still exits 0.
        # The first 7.2 build passed this way with none of the PECL DLLs
        # actually registered, so the test has to read back the module list.
        text = PS1.read_text()
        self.assertIn("get_loaded_extensions", text)
        self.assertIn("Unable to (load dynamic library|initialize module)", text)
        self.assertNotIn("echo 'extensions ok'", text)


class WindowsPhp5BuildTests(unittest.TestCase):
    """PHP 5.6 lost its VS2012 toolset; option B rebuilds it on VS2019."""

    def test_php5_uses_the_aggregate_vc11_bundle(self):
        # 5.6 predates the per-library series manifests, so its dependencies
        # come from one archive keyed by the branch's own CRT.
        text = PS1.read_text()
        self.assertIn("deps-$MajorMinor-$Crt-$Arch.7z", text)
        self.assertIn("$Crt -in @('vc11', 'vc14')", text)

    def test_php5_prefers_the_vs2019_toolset(self):
        # v110 is gone. VS2019's 14.2x rejects less of the 2019-era source
        # than v143 does, so it is tried first.
        text = PS1.read_text()
        self.assertIn("'vc11' { '^14\.2\d*\.' }", text)
        self.assertIn("falls back to the newest one when absent", text)

    def test_php5_omits_the_flags_only_7_has(self):
        # PHP 5 always builds the CLI and CGI SAPIs; those configure flags
        # only exist from 7.0.
        text = PS1.read_text()
        self.assertIn("if ($MajorMinor -notin @('5.5', '5.6'))", text)
        self.assertIn("--enable-cli", text)

    def test_compiler_id_rewrite_covers_an_empty_value(self):
        # PHP 5's table stops at VS2013, so configure.js leaves the value
        # empty; a value-only regex would then skip the line entirely.
        text = PS1.read_text()
        self.assertIn(r"(?m)^#define\s+PHP_COMPILER_ID[^\r\n]*", text)

    def test_php5_pins_the_toolset_name_before_configure(self):
        # Without the clamp configure prints "Detected compiler undefined" and
        # dies in write_summary ('length' is null or not an object) before it
        # writes a Makefile, so nmake then finds nothing to build.
        text = PS1.read_text()
        self.assertIn("if (!VC_VERSIONS[VCVERS]) { VCVERS = $CrtVersion; }", text)
        self.assertIn("$CrtVersion = 1700", text)
        self.assertIn("cannot pin the toolset name", text)

    def test_php5_scopes_the_snprintf_macro(self):
        # UCRT provides a real snprintf and aborts compilation while the macro
        # is defined ("Macro definition of snprintf conflicts with Standard
        # Library function declaration"). PHP 5 defines it unconditionally.
        text = PS1.read_text()
        self.assertIn("#define snprintf _snprintf", text)
        self.assertIn("'#if _MSC_VER < 1900'", text)
        self.assertIn("does not define the snprintf macro as expected", text)

    def test_php5_official_archive_is_under_archives(self):
        text = PS1.read_text()
        self.assertIn("'vc11' { 'VC11' }", text)
        self.assertIn("@('5.5', '5.6', '7.0', '7.1', '7.2', '7.3')", text)


class DispatchPermissionsTests(unittest.TestCase):
    """A reusable workflow can never be granted more than its caller."""

    def test_caller_grants_what_build_macos_requests(self):
        # build-macos.yml asks for contents: write + actions: read. When the
        # orchestrator only granted contents: read, every manual dispatch died
        # with startup_failure before any job ran - GitHub refuses to start a
        # run whose called workflow requests more than the caller allows.
        dispatch = yaml.safe_load(DISPATCH.read_text())
        caller = dispatch["permissions"]
        callee = yaml.safe_load((ROOT / ".github/workflows/build-macos.yml").read_text())["permissions"]
        for scope, level in callee.items():
            self.assertIn(scope, caller, f"caller does not grant {scope} at all")
            self.assertEqual(
                caller[scope], level, f"caller grants {scope}: {caller[scope]}, callee wants {level}"
            )


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

    def test_official_archive_is_the_package_base(self):
        # A source build cannot produce dev\*.lib, sasl2\, lib\enchant,
        # extras\ssl, deplister.exe, the phar launcher, the docs, or the
        # extensions that need the .NET/Oracle SDKs. The published packages
        # carried all of them, so the official archive is unpacked first and
        # the compiled binaries replace the unpatched ones on top.
        text = PS1.read_text()
        self.assertIn("php-$BaseVersion-nts-Win32-$CrtTag-$ArchZip.zip", text)
        self.assertIn("windows.php.net/downloads/releases/", text)
        self.assertIn("Expand-Archive -Path $OfficialZip", text)

    def test_archive_provided_layout_is_verified(self):
        # Dropping the base layer is silent: the package still loads, it just
        # cannot build extensions or load enchant/SASL any more.
        text = PS1.read_text()
        self.assertIn("deplister.exe", text)
        self.assertIn("extras\\ssl\\openssl.cnf", text)
        self.assertIn("phar.phar.bat", text)
        self.assertIn("did not provide", text)
        self.assertIn("phpize could not build an extension", text)

    def test_dependency_dlls_do_not_override_the_archive(self):
        # The archive carries the runtime its own extensions link against.
        # Overwriting those with the build tree's copies can break com_dotnet
        # and oci8, so only missing names are added.
        text = PS1.read_text()
        self.assertRegex(text, r"Where-Object \{ -not \(Test-Path \(Join-Path \$Stage \$_\.Name\)\) \}")

    def test_imagick_round_trip_is_not_skipped(self):
        # The coder set differs by branch (7.0 ships none), so keying the test
        # off one module name silently skipped it there. A branch without
        # coders must be reported, not passed over quietly.
        text = PS1.read_text()
        self.assertIn("Join-Path $StageExt 'php_imagick.dll'", text)
        self.assertNotIn("IM_MOD_RL_png_.dll'", text)
        self.assertIn("carries no IM_MOD_RL_* coders", text)
        self.assertIn("Write-Warning", text)

    def test_imagick_round_trip_is_exercised(self):
        # Loading the DLL proves nothing about the coders; only an actual
        # encode/decode does.
        text = PS1.read_text()
        self.assertIn("newImage(8, 8, 'red')", text)
        self.assertIn("readImageBlob", text)
        self.assertIn("not where it looks", text)

    def test_only_core_import_libraries_go_to_dev(self):
        # Release\ holds one .lib per extension plus object-layout mirrors;
        # the official package ships only php<major>.lib. Copying them all
        # grew the package by ~40 files per branch.
        text = PS1.read_text()
        self.assertIn("Get-ChildItem $ReleaseDir -File -Filter '*.lib'", text)
        self.assertIn("$_.Name -match '^php\d+(embed)?\.lib$'", text)

    def test_extension_archives_contribute_their_config(self):
        # imagick ships ImageMagick's policy/delegates XML and memcached a
        # sample ini. policy.xml drives ImageMagick's security limits, so it
        # belongs in the package rather than being dropped with the pdb files.
        text = PS1.read_text()
        self.assertIn("Join-Path $unpack 'config'", text)
        self.assertIn("-Filter '*.ini'", text)

    def test_dispatch_routes_legacy_php_away_from_the_ubuntu_pipeline(self):
        dispatch = yaml.safe_load(DISPATCH.read_text())
        jobs = dispatch["jobs"]
        self.assertIn("build-windows-legacy", jobs)
        self.assertEqual(
            jobs["build-windows-legacy"]["uses"],
            "./.github/workflows/build-windows-php-legacy.yml",
        )
        # Every EOL branch has to be diverted: the stock pipeline repackages
        # the upstream zip and cannot apply a source patch, so a version that
        # slips through ships unfixed.
        normal = jobs["build-windows"]["if"]
        legacy = jobs["build-windows-legacy"]["if"]
        for version in EOL_VERSIONS:
            prefix = major_minor(version) + "."
            self.assertIn(f"startsWith(inputs.windows_version, '{prefix}')", normal)
            self.assertIn(f"startsWith(inputs.windows_version, '{prefix}')", legacy)
        self.assertIn("build-windows-legacy", jobs["dispatch-downstream"]["needs"])

    def test_extension_manifest_is_generated_for_the_legacy_build(self):
        # PECL extension URLs live in php-exts.json; the workflow flattens
        # them for the requested branch/arch so the script does not have to
        # parse that file on Windows.
        steps = self.workflow["jobs"]["build"]["steps"]
        names = [s.get("name", "") for s in steps]
        self.assertTrue(
            any("extension manifest" in n.lower() for n in names),
            f"no extension manifest step in {names}",
        )
        build = next(s for s in steps if s.get("name") == "Build PHP")
        self.assertIn("-ExtManifest", build["run"])
        # Only the set the stock pipeline installs: php-exts.json also lists
        # extras the published packages never carried.
        manifest_step = next(s for s in steps if "extension manifest" in s.get("name", "").lower())
        for ext in ("imagick", "redis", "mongodb", "igbinary"):
            self.assertIn(f"'{ext}'", manifest_step["run"])
        self.assertIn("$required", manifest_step["run"])
        # apcu and grpc were in the EOL packages but are not in php-exts.json.
        self.assertIn("$pinned", manifest_step["run"])
        for branch in ("7.0", "7.1", "7.2", "7.3", "7.4", "8.0"):
            self.assertIn(f"'{branch}'", manifest_step["run"])
        # 8.1 never shipped either, so pinning one would invent a dependency.
        self.assertNotIn("'8.1' = @{", manifest_step["run"])
        for step in steps:
            run = step.get("run")
            if not run:
                continue
            self.assertNotIn("${{ inputs.", run, f"step {step.get('name')} inlines an input")


if __name__ == "__main__":
    unittest.main()
