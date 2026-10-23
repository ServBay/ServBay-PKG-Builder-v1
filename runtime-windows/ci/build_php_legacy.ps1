<#
.SYNOPSIS
    Compile an EOL PHP release for Windows with the toolchain that release targets.

.DESCRIPTION
    The stock Windows pipeline repackages an already-built upstream zip, so it
    cannot apply a source patch. Every EOL branch that has to carry a security
    backport is therefore compiled here instead:

      7.0, 7.1   VC14 (MSVC 14.0)  - aggregate deps-<branch>-vc14-<arch>.7z
      7.2 - 7.4  VC15 (MSVC 19.1)  - per-library series manifest
      8.0, 8.1   VS16 (MSVC 19.2)  - per-library series manifest

    Nothing is built by hand: source and dependency libraries come from
    php.net, and the SDK comes from php-sdk-binary-tools. The only non-upstream
    input is the security patch this repository carries.

    VC14 is special. No hosted runner image carries that toolset and Microsoft
    no longer ships it: aka.ms/vs/14 is dead, aka.ms/vs/15 is the VS2017
    installer, and asking the runner's own VS installer for the VC.140
    component returns 0 while changing nothing ("Status changed to NoUpdate").
    So v140 is used when the image happens to have it, and otherwise the newest
    installed toolset. That is safe: MSVC has kept the vcruntime140 ABI since
    2015, and configure.js only warns about an untested compiler version.

    Two compatibility shims are required for VC14 only, both discovered by
    building 7.0 on a clean Windows 11 machine:

      * MC must be set explicitly. PHP's win32 Makefile expands $(MC) but its
        configure never probes for mc.exe, so nmake runs an empty command.
      * The UCRT shipped with the runner's Windows SDK uses _mm_loadu_si64,
        an intrinsic introduced in VS2017. VC14 cannot compile that header, so
        vc14-intrinsics.h maps it to the SSE2 load VC14 does provide. Newer
        Windows 10 SDKs no longer include the 10.0.10240 UCRT that VC14
        originally shipped against, which is why the header has to be patched
        rather than pinned to an older SDK.

.PARAMETER Version
    Publication version, e.g. 7.0.33.1. A fourth segment marks a local security
    backport; the upstream source tag is always the first three segments.

.PARAMETER Arch
    x64 or x86.

.PARAMETER OutputDir
    Directory that receives php-<version>-windows-<arch>.7z.

.PARAMETER PatchDir
    Directory holding this repository's patches for the branch. Only the
    cve-*.patch files are applied: the rest of that directory compensates for
    macOS-only build behaviour and does not belong in a Windows build.

.PARAMETER ExtManifest
    JSON object mapping a PECL extension name to its download URL for this
    branch and architecture, as flattened from runtime-windows/php-exts.json.
    --enable-snapshot-build only builds the core ext/ modules; the published
    packages also carry imagick, redis, mongodb and friends, so without this
    the source build is a regression against the package it replaces. Omitted
    means no PECL extensions, which is only appropriate for a local smoke run.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Version,
    [Parameter(Mandatory = $true)][ValidateSet('x64', 'x86')][string] $Arch,
    [Parameter(Mandatory = $true)][string] $OutputDir,
    [Parameter(Mandatory = $true)][string] $PatchDir,
    [string] $ExtManifest = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# x.y.z.N is a local backport of upstream x.y.z.
$BaseVersion = $Version
if ($Version -match '^(\d+\.\d+\.\d+)\.\d+$') {
    $BaseVersion = $Matches[1]
}
$MajorMinor = ($BaseVersion -split '\.')[0] + '.' + ($BaseVersion -split '\.')[1]

# The compiler each branch was released against. It selects both the starter
# toolset and the dependency series, and the two must agree: the deps/series
# manifests are keyed by CRT, and the import libraries in them are the ones
# the official binaries linked against.
$CrtByBranch = @{
    '5.5' = 'vc11'
    '5.6' = 'vc11'
    '7.0' = 'vc14'
    '7.1' = 'vc14'
    '7.2' = 'vc15'
    '7.3' = 'vc15'
    '7.4' = 'vc15'
    '8.0' = 'vs16'
    '8.1' = 'vs16'
}
$Crt = $CrtByBranch[$MajorMinor]
if (-not $Crt) {
    throw "build_php_legacy.ps1 does not cover PHP $MajorMinor; known branches: $($CrtByBranch.Keys -join ', ')"
}

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

$Root = 'C:\phplegacy'
$SdkDir = Join-Path $Root 'sdk'
$PhpDevRoot = Join-Path $Root 'phpdev'
$BuildRoot = Join-Path $PhpDevRoot $Crt
$BuildDir = Join-Path $BuildRoot $Arch
$ToolsDir = Join-Path $Root 'tools'

Write-Step "PHP $Version ($Arch) from upstream $BaseVersion on $Crt"
New-Item -ItemType Directory -Force -Path $Root, $SdkDir, $ToolsDir | Out-Null

# --- Toolchain -------------------------------------------------------------
# Each branch prefers the toolset it was released against, and falls back to
# the newest installed one. The fallback matters most for VC14: no hosted
# runner image carries 14.0 any more and Microsoft no longer ships it, so 7.0
# and 7.1 routinely build with a newer linker against the VC14 import
# libraries. MSVC has kept the vcruntime140 ABI since 2015, so that links.
$ToolsetPattern = switch ($Crt) {
    'vc14' { '^14\.0\d*\.' }
    # 5.5/5.6 shipped against VS2012 (v110), which no longer exists. VS2019's
    # 14.2x is the closest surviving toolset and the least likely to reject
    # code from that era; it falls back to the newest one when absent.
    'vc11' { '^14\.2\d*\.' }
    'vc15' { '^14\.1\d*\.' }
    'vs16' { '^14\.2\d*\.' }
}
$InstallerDir = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer'
$VsWhere = Join-Path $InstallerDir 'vswhere.exe'
if (-not (Test-Path $VsWhere)) { throw "vswhere not found at $VsWhere" }

$VsInstances = & $VsWhere -products '*' -format json | ConvertFrom-Json
$VsInstance = $VsInstances |
    Where-Object { Test-Path (Join-Path $_.installationPath 'VC\Tools\MSVC') } |
    Select-Object -First 1
if (-not $VsInstance) { throw "No Visual Studio instance with a C++ toolset found" }
$VsPath = $VsInstance.installationPath
$MsvcRoot = Join-Path $VsPath 'VC\Tools\MSVC'

$Toolset = Get-ChildItem $MsvcRoot -Directory |
    Where-Object { $_.Name -match $ToolsetPattern } |
    Sort-Object Name -Descending |
    Select-Object -First 1
if ($Toolset) {
    Write-Step "Using the $Crt toolset $($Toolset.Name)"
} else {
    $Toolset = Get-ChildItem $MsvcRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $Toolset) { throw "No MSVC toolset under $MsvcRoot" }
    Write-Warning "No $Crt toolset is available; building PHP $BaseVersion with $($Toolset.Name)"
}
$ToolsetVer = $Toolset.Name
$VcVars = Join-Path $VsPath 'VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path $VcVars)) { throw "vcvarsall.bat is missing: $VcVars" }
Write-Step "Using MSVC $ToolsetVer from $VsPath"

# --- Upstream inputs -------------------------------------------------------
# SDK 2.1 (20110915) is the last one that can build 5.5/5.6, 7.0 and 7.1; its
# buildtree also lacks the vc15/vs16 layouts. Everything newer uses the current
# SDK, whose buildtree knows every supported compiler.
$SdkZip = Join-Path $ToolsDir "php-sdk-$Crt.zip"
if ($Crt -in @('vc11', 'vc14')) {
    $SdkUrl = 'https://downloads.php.net/~windows/php-sdk/php-sdk-binary-tools-20110915.zip'
} else {
    $SdkUrl = 'https://github.com/php/php-sdk-binary-tools/archive/refs/tags/php-sdk-2.8.4.zip'
}
if (-not (Test-Path $SdkZip)) {
    Write-Step "Downloading $SdkUrl"
    Invoke-WebRequest -Uri $SdkUrl -OutFile $SdkZip
}
Expand-Archive -Path $SdkZip -DestinationPath $SdkDir -Force
# The GitHub archive carries the tag name as its only top-level directory; the
# php.net bundle puts bin/ and lib/ at the root.
$SdkRoot = $SdkDir
if (-not (Test-Path (Join-Path $SdkRoot 'bin'))) {
    $inner = Get-ChildItem $SdkDir -Directory | Select-Object -First 1
    if ($inner) { $SdkRoot = $inner.FullName }
}
if (-not (Test-Path (Join-Path $SdkRoot 'bin'))) {
    throw "SDK layout not recognised under $SdkDir"
}
# configure.js treats bison as a hard requirement. The 2.x SDK keeps it in
# msys2\usr\bin rather than bin; the 2.1 bundle has it under bin.
$env:PHP_SDK_ROOT_PATH = $SdkRoot
$env:PATH = "$(Join-Path $SdkRoot 'bin');$(Join-Path $SdkRoot 'msys2\usr\bin');" + $env:PATH

# vc11 and vc14 shipped as one aggregate dependency bundle per branch.
# Everything above them ships one zip per library plus a series manifest that
# pins the exact versions that branch was released against.
$DepsArchive = $null
$DepsPackages = @()
$DepsCache = Join-Path $ToolsDir "deps-$MajorMinor-$Crt-$Arch"
if ($Crt -in @('vc11', 'vc14')) {
    $DepsArchive = Join-Path $ToolsDir "deps-$MajorMinor-$Crt-$Arch.7z"
    if (-not (Test-Path $DepsArchive)) {
        Write-Step "Downloading PHP $MajorMinor $Crt dependency libraries ($Arch)"
        Invoke-WebRequest -Uri "https://downloads.php.net/~windows/php-sdk/archives/deps-$MajorMinor-$Crt-$Arch.7z" -OutFile $DepsArchive
    }
} else {
    $SeriesUrl = "https://downloads.php.net/~windows/php-sdk/deps/series/packages-$MajorMinor-$Crt-$Arch-stable.txt"
    $SeriesFile = Join-Path $ToolsDir "packages-$MajorMinor-$Crt-$Arch-stable.txt"
    if (-not (Test-Path $SeriesFile)) {
        Write-Step "Downloading PHP $MajorMinor $Crt dependency manifest ($Arch)"
        Invoke-WebRequest -Uri $SeriesUrl -OutFile $SeriesFile
    }
    $DepsPackages = @(Get-Content $SeriesFile | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (-not $DepsPackages) { throw "Empty dependency manifest: $SeriesFile" }

    New-Item -ItemType Directory -Force -Path $DepsCache | Out-Null
    Write-Step "Downloading $($DepsPackages.Count) PHP $MajorMinor $Crt dependency libraries"
    foreach ($name in $DepsPackages) {
        $local = Join-Path $DepsCache $name
        if (-not (Test-Path $local)) {
            Invoke-WebRequest -Uri "https://downloads.php.net/~windows/php-sdk/deps/$Crt/$Arch/$name" -OutFile $local
        }
    }
}

$SourceArchive = Join-Path $ToolsDir "php-$BaseVersion.tar.gz"
if (-not (Test-Path $SourceArchive)) {
    Write-Step "Downloading php-$BaseVersion source"
    Invoke-WebRequest -Uri "https://www.php.net/distributions/php-$BaseVersion.tar.gz" -OutFile $SourceArchive
}

# --- Build tree ------------------------------------------------------------
Write-Step 'Preparing build tree'
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }
# buildtree writes phpdev\ relative to the current directory, so it has to run
# from $Root rather than from wherever the caller happened to be.
Push-Location $Root
try {
    & (Join-Path $SdkRoot 'bin\phpsdk_buildtree.bat') phpdev | Out-Null
}
finally { Pop-Location }
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
if (-not (Test-Path $BuildDir)) {
    if ($Crt -in @('vc11', 'vc14')) {
        # SDK 2.1 predates both vc11 and vc14 and only creates vc6..vc9, all as
        # siblings under phpdev; copy vc9 to get the requested layout. The tree
        # is just deps\{bin,include,lib}, which the dependency archive fills.
        $Vc9Dir = Join-Path $PhpDevRoot 'vc9'
        if (-not (Test-Path $Vc9Dir)) { throw "phpsdk_buildtree did not create $Vc9Dir" }
        Copy-Item -Recurse -Force $Vc9Dir $BuildDir
    } else {
        throw "phpsdk_buildtree did not create $BuildDir"
    }
}

$SevenZip = 'C:\Program Files\7-Zip\7z.exe'
if (-not (Test-Path $SevenZip)) { $SevenZip = Join-Path $ToolsDir '7zr.exe' }
if (-not (Test-Path $SevenZip)) {
    Invoke-WebRequest -Uri 'https://www.7-zip.org/a/7zr.exe' -OutFile (Join-Path $ToolsDir '7zr.exe')
    $SevenZip = Join-Path $ToolsDir '7zr.exe'
}
Write-Step 'Extracting dependency libraries'
if ($Crt -in @('vc11', 'vc14')) {
    & $SevenZip x $DepsArchive "-o$BuildDir" -y | Out-Null
} else {
    # The per-library zips are already laid out as deps\{bin,include,lib}.
    $DepsDir = Join-Path $BuildDir 'deps'
    New-Item -ItemType Directory -Force -Path $DepsDir | Out-Null
    foreach ($name in $DepsPackages) {
        Expand-Archive -Path (Join-Path $DepsCache $name) -DestinationPath $DepsDir -Force
    }
}

$SourceDir = Join-Path $BuildDir "php-$BaseVersion-src"
if (Test-Path $SourceDir) { Remove-Item -Recurse -Force $SourceDir }
Write-Step 'Extracting PHP source'
# The 2.x SDK puts an MSYS2 GNU tar on PATH, and that tar reads "C:\..." as a
# remote host ("Cannot connect to C: resolve failed"). The system tar is
# bsdtar and handles drive letters, so call it by absolute path.
$SystemTar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path $SystemTar)) { throw "System tar not found at $SystemTar" }
& $SystemTar -xzf $SourceArchive -C $BuildDir
if ($LASTEXITCODE -ne 0) { throw "tar failed with $LASTEXITCODE" }
Rename-Item (Join-Path $BuildDir "php-$BaseVersion") $SourceDir

Push-Location $SourceDir
try {
    # --- Security patch ----------------------------------------------------
    # Applied with git rather than the SDK's patch 2.5.9, which aborts on
    # multi-hunk diffs ("Assertion failed: hunk").
    Write-Step 'Applying security patch'
    $patch = Get-ChildItem -Path $PatchDir -Filter 'cve-*.patch' -File
    if (-not $patch) { throw "No cve-*.patch in $PatchDir" }
    foreach ($p in $patch) {
        Write-Host "    $($p.Name)"
        & git apply --ignore-whitespace $p.FullName
        if ($LASTEXITCODE -ne 0) { throw "Failed to apply $($p.Name)" }
    }

    # --- Teach configure this toolset's name -------------------------------
    # PHP 5's compiler table stops at VS2013 (1800). A newer cl.exe reports
    # "19.29", which is not a key in VC_VERSIONS/VC_VERSIONS_SHORT: configure
    # prints "Detected compiler undefined" and then dies in write_summary with
    # "'length' is null or not an object" before it ever writes the Makefile.
    # Clamp an unknown value to the CRT the branch shipped against, so the
    # flags, the summary and the module build ID all behave like the official
    # binaries. 7.0+ understand newer toolsets well enough not to need this.
    if ($MajorMinor -in @('5.5', '5.6')) {
        $CrtVersion = 1700   # VC11, the CRT 5.5/5.6 were released against
        $ConfigW32 = Join-Path $SourceDir 'win32\build\config.w32'
        $lines = Get-Content $ConfigW32
        if (-not ($lines -match '^\s*VCVERS\s*=\s*probe_binary')) {
            throw "win32\build\config.w32 does not probe VCVERS; cannot pin the toolset name"
        }
        $patched = foreach ($l in $lines) {
            $l
            if ($l -match '^\s*VCVERS\s*=\s*probe_binary') {
                "if (!VC_VERSIONS[VCVERS]) { VCVERS = $CrtVersion; }"
            }
        }
        $patched | Set-Content -Path $ConfigW32 -Encoding ASCII
        Write-Step "Pinned an unrecognised toolset to VC_VERSIONS $CrtVersion"
    }

    # --- Configure ---------------------------------------------------------
    # VC14 regenerates CFLAGS inside configure, so the shim is forced in twice:
    # through CL for configure's own probe compiles, and through the generated
    # Makefile for the real build. Newer toolsets ship _mm_loadu_si64 and must
    # not see the shim at all.
    $Shim = $null
    if ($Crt -eq 'vc14') {
        $Shim = Join-Path $Root 'vc14-intrinsics.h'
        if (-not (Test-Path $Shim)) { throw "Missing intrinsics shim: $Shim" }
    }

    Write-Step 'Running buildconf'
    & cmd /c 'buildconf.bat' | Out-Null

    Write-Step 'Configuring'
    $ArchArg = if ($Arch -eq 'x64') { 'amd64' } else { 'x86' }
    $Mc = (Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\mc.exe' -ErrorAction SilentlyContinue |
           Sort-Object FullName -Descending | Select-Object -First 1).FullName
    if (-not $Mc) {
        $Mc = (Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin\*\x86\mc.exe' -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1).FullName
    }
    if (-not $Mc) { throw 'mc.exe not found in the Windows SDK' }

    $env:MC = $Mc
    $env:PATH = "$(Split-Path $Mc);" + $env:PATH
    if ($Shim) {
        # cl.exe honours the CL variable on every invocation, including the
        # probe compiles configure.js runs. Setting it here keeps the extra
        # probes that --enable-snapshot-build performs from tripping over the
        # UCRT header before we get to touch the Makefile.
        $env:CL = "/FI`"$Shim`""
    }

    # --enable-snapshot-build is what php.net's own Windows releases use: it
    # turns on every optional module and builds each one shared, which is what
    # puts the 37 php_*.dll files in ext/. Without it the package would be a
    # bare CLI that cannot serve a ServBay site. PHP 5 always builds both the
    # CLI and CGI SAPIs, so those flags only exist from 7.0 on.
    $configureArgs = @('--enable-snapshot-build', '--disable-zts')
    if ($MajorMinor -notin @('5.5', '5.6')) {
        $configureArgs += @('--enable-cli', '--enable-cgi')
    }
    & cmd /c "`"$VcVars`" $ArchArg -vcvars_ver=$ToolsetVer && cscript //nologo configure.js $($configureArgs -join ' ')"

    # --- Align the module build ID with the official extensions -------------
    # configure.js derives PHP_COMPILER_ID from the detected cl.exe. Its
    # version table stops at VS2017, so any newer toolset produces the raw
    # version number (e.g. "19.44.35229"). The module build ID then reads
    # "API...,NTS,19.44.35229" while every php.net and PECL extension for the
    # branch was built as "...NTS,VC15"/"VS16", and the loader rejects them:
    #
    #   Module compiled with build ID=API20170718,NTS,VC15
    #   PHP    compiled with build ID=API20170718,NTS,19.44.35229
    #
    # The hosted image no longer carries the branch's original toolset, so the
    # ID is set to the one the official binaries use. This is safe across
    # MSVC 2015-2022: they share the vcruntime140 ABI, which is what the check
    # is standing in for. Nothing else in the build ID changes - the API
    # number, ZTS and debug flags are still compared for real.
    $ConfigHeader = $null
    foreach ($candidate in @('main\config.w32.h', 'main\php_config.h')) {
        $path = Join-Path $SourceDir $candidate
        if ((Test-Path $path) -and (Select-String -Path $path -Pattern '#define\s+PHP_COMPILER_ID' -Quiet)) {
            $ConfigHeader = $path
            break
        }
    }
    # "vc14" -> "VC14", "vs16" -> "VS16", matching the PECL archive names.
    $CompatId = $Crt.ToUpper()
    if (-not $ConfigHeader) {
        # 8.0/8.1 recognise the modern toolset and do not emit the macro, so
        # there is nothing to align. The staging smoke test still proves the
        # IDs match, so this is not a silent pass.
        Write-Warning "PHP_COMPILER_ID is not defined by configure for $MajorMinor; keeping the detected value"
    } else {
        $headerText = Get-Content $ConfigHeader -Raw
        # Branches that recognise the toolset emit a bare identifier, the ones
        # that do not emit a quoted version string, and PHP 5 leaves the value
        # empty because its table stops at VS2013. Replace the whole line so
        # all three forms end up with the official ID.
        $updated = [regex]::Replace($headerText, '(?m)^#define\s+PHP_COMPILER_ID[^\r\n]*', "#define PHP_COMPILER_ID `"$CompatId`"")
        Set-Content -Path $ConfigHeader -Value $updated -Encoding ASCII
        Write-Step "Module build ID set to $CompatId in $ConfigHeader"
    }

    # --- Inject the shim ---------------------------------------------------
    if ($Shim) {
        $Makefile = Join-Path $SourceDir 'Makefile'
        $lines = Get-Content $Makefile
        $patched = foreach ($l in $lines) {
            if ($l -match '^CFLAGS=' -and $l -notmatch 'vc14-intrinsics') { $l + " /FI`"$Shim`"" } else { $l }
        }
        $patched | Set-Content -Path $Makefile -Encoding ASCII
    }

    # --- Compile -----------------------------------------------------------
    Write-Step 'Compiling (nmake)'
    & cmd /c "`"$VcVars`" $ArchArg -vcvars_ver=$ToolsetVer && nmake"
    if ($LASTEXITCODE -ne 0) { throw "nmake failed with $LASTEXITCODE" }

    # --- Package -----------------------------------------------------------
    $ReleaseDir = Join-Path $SourceDir "$Arch\Release"
    if (-not (Test-Path (Join-Path $ReleaseDir 'php.exe'))) {
        throw "Build produced no php.exe in $ReleaseDir"
    }
    $Verify = & (Join-Path $ReleaseDir 'php.exe') -v 2>&1
    # -notmatch on an array filters it, so it is true whenever any single line
    # fails to match. Flatten it first.
    $VerifyText = ($Verify | Out-String)
    Write-Host $VerifyText
    if ($VerifyText -notmatch [regex]::Escape($BaseVersion)) {
        throw "php.exe does not report $BaseVersion"
    }

    # Gate on the security backport itself. A vanilla 7.0 build reports an
    # exhausted memory limit as an ordinary fatal error and exits 255; the
    # patched allocator prints the message and calls _exit(1) instead of
    # longjmp'ing through half-initialised state. A 255 here means the patch
    # never made it into the binary, so the package must not ship.
    Write-Step 'Verifying CVE-2026-109618/109619 mitigation'
    $OomOutput = & (Join-Path $ReleaseDir 'php.exe') -n -d memory_limit=2M `
                    -r "str_repeat('x', 8388608);" 2>&1
    $OomExit = $LASTEXITCODE
    Write-Host "  exit code: $OomExit"
    Write-Host "  output: $($OomOutput -join ' ')"
    if ($OomExit -ne 1) {
        throw "OOM handling did not take the _exit(1) path (exit $OomExit); the CVE patch is not active"
    }
    if (($OomOutput -join "`n") -notmatch 'Allowed memory size') {
        throw 'OOM run did not report the memory limit exhausting'
    }

    # --- Stage -------------------------------------------------------------
    # Release\ also holds every object file, resource script and import
    # library the build produced, laid out to mirror the source tree. The
    # package must look like an official Windows build instead.
    #
    # The official release archive is the base layer: it carries what a source
    # build cannot produce on a hosted runner - dev\*.lib for phpize, sasl2\,
    # lib\enchant, extras\ssl, deplister.exe, phar.phar.bat, the docs, and the
    # extensions that need an external SDK (com_dotnet, oci8, pdo_oci). The
    # compiled binaries then replace the unpatched ones on top of it.
    Write-Step 'Staging the package layout'
    $Stage = Join-Path $Root 'stage'
    if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
    $StageExt = Join-Path $Stage 'ext'
    New-Item -ItemType Directory -Force -Path $StageExt | Out-Null

    $ArchZip = if ($Arch -eq 'x64') { 'x64' } else { 'x86' }
    # windows.php.net spells the 7.4 CRT tag in lower case and puts the EOL
    # branches under archives\.
    $CrtTag = switch ($Crt) { 'vc11' { 'VC11' } 'vc14' { 'VC14' } 'vc15' { 'VC15' } 'vs16' { 'vs16' } }
    if ($MajorMinor -eq '7.4') { $CrtTag = 'vc15' }
    $OfficialName = "php-$BaseVersion-nts-Win32-$CrtTag-$ArchZip.zip"
    # EOL branches live under archives\; 8.0/8.1 remain in the current tree.
    $OfficialBase = if ($MajorMinor -in @('5.5', '5.6', '7.0', '7.1', '7.2', '7.3')) {
        'https://windows.php.net/downloads/releases/archives/'
    } else {
        'https://windows.php.net/downloads/releases/'
    }
    $OfficialZip = Join-Path $ToolsDir $OfficialName
    if (-not (Test-Path $OfficialZip)) {
        Write-Step "Downloading the official $BaseVersion archive as the package base"
        Invoke-WebRequest -Uri "$OfficialBase$OfficialName" -OutFile $OfficialZip
    }
    Expand-Archive -Path $OfficialZip -DestinationPath $Stage -Force

    # --- Compiled binaries replace the unpatched ones ----------------------
    Get-ChildItem $ReleaseDir -File -Filter '*.exe' | Copy-Item -Destination $Stage -Force
    Get-ChildItem $ReleaseDir -File -Filter 'php*.dll' |
        Where-Object { $_.Name -notmatch '^php_' } |
        Copy-Item -Destination $Stage -Force
    Get-ChildItem $ReleaseDir -File -Filter 'php_*.dll' | Copy-Item -Destination $StageExt -Force
    # phpize links extensions against this import library; the archive's copy
    # predates the security backport. Release\ also holds one .lib per
    # extension and an object-layout mirror, none of which the official
    # package ships, so only the core libraries are taken.
    $StageDev = Join-Path $Stage 'dev'
    if (Test-Path $StageDev) {
        Get-ChildItem $ReleaseDir -File -Filter '*.lib' |
            Where-Object { $_.Name -match '^php\d+(embed)?\.lib$' } |
            Copy-Item -Destination $StageDev -Force
    }

    # PHP statically links most of the dependency bundle, but the libraries it
    # does not (OpenSSL, ICU, libpq, libssh2, ...) have to travel with the
    # binary. The archive already carries the ones its extensions need, so
    # only fill in what is missing rather than overriding a matched set.
    $DepsBin = Join-Path $BuildDir 'deps\bin'
    if (Test-Path $DepsBin) {
        Get-ChildItem $DepsBin -Filter '*.dll' -File |
            Where-Object { $_.Name -notmatch '_debug\.dll$' } |
            Where-Object { -not (Test-Path (Join-Path $Stage $_.Name)) } |
            Copy-Item -Destination $Stage
    }
    foreach ($ini in @('php.ini-development', 'php.ini-production')) {
        $iniSrc = Join-Path $SourceDir $ini
        if (Test-Path $iniSrc) { Copy-Item $iniSrc -Destination $Stage -Force }
    }

    # --- PECL extensions ---------------------------------------------------
    # --enable-snapshot-build only compiles the modules under ext/. The
    # package this replaces also shipped imagick, redis, mongodb and the rest,
    # so they are fetched from the same table the stock pipeline uses and
    # dropped into ext\.
    $PeclExts = @{}
    if ($ExtManifest) {
        if (-not (Test-Path $ExtManifest)) { throw "Missing extension manifest: $ExtManifest" }
        $Manifest = Get-Content -Path $ExtManifest -Raw | ConvertFrom-Json
        $ExtCache = Join-Path $ToolsDir "pecl-$MajorMinor-$Arch"
        New-Item -ItemType Directory -Force -Path $ExtCache | Out-Null

        Write-Step "Staging PECL extensions from $ExtManifest"
        foreach ($prop in $Manifest.PSObject.Properties) {
            $name = $prop.Name
            $url = $prop.Value
            if (-not $url) { continue }

            $zip = Join-Path $ExtCache (Split-Path $url -Leaf)
            if (-not (Test-Path $zip)) {
                Write-Host "    downloading $name"
                Invoke-WebRequest -Uri $url -OutFile $zip
            }
            $unpack = Join-Path $ExtCache "$name-unpacked"
            if (Test-Path $unpack) { Remove-Item -Recurse -Force $unpack }
            Expand-Archive -Path $zip -DestinationPath $unpack -Force

            # The archive also carries the third-party runtime the extension
            # links against (ImageMagick's CORE_RL_*/IM_MOD_RL_*, libyaml's
            # yaml.dll). Windows resolves an extension's imports against
            # php.exe's directory, not ext\, so these go to the package root -
            # beside the module rather than inside ext\. A PECL-only runtime
            # kept under ext\ makes php_imagick and php_yaml fail with "The
            # specified module could not be found".
            $dlls = @(Get-ChildItem $unpack -Recurse -Filter '*.dll' -File)
            if (-not @($dlls | Where-Object { $_.Name -match '^php_' })) {
                throw "Extension package for $name carries no php_*.dll"
            }
            foreach ($dll in $dlls) {
                if ($dll.Name -match '^php_') {
                    Copy-Item $dll.FullName -Destination $StageExt -Force
                    $PeclExts[$dll.BaseName -replace '^php_', ''] = $true
                } else {
                    Copy-Item $dll.FullName -Destination $Stage -Force
                }
            }
            Write-Host "    $name -> $(($dlls | Where-Object { $_.Name -match '^php_' }).BaseName -join ', ')"

            # Some archives also carry runtime configuration: ImageMagick's
            # policy/delegates XML (imagick) and a sample ini (memcached).
            # They belong in the package - policy.xml drives ImageMagick's
            # own security limits.
            $extraIni = @(Get-ChildItem $unpack -Filter '*.ini' -File)
            foreach ($ini in $extraIni) { Copy-Item $ini.FullName -Destination $Stage -Force }
            $configDir = Join-Path $unpack 'config'
            if (Test-Path $configDir) {
                Copy-Item $configDir -Destination $Stage -Recurse -Force
            }
        }
        Write-Step "Staged $($PeclExts.Count) PECL extensions"
    }

    if (-not (Get-ChildItem $StageExt -Filter 'php_*.dll' -File)) {
        throw "Staged package has no extensions in $StageExt"
    }

    # The official archive is what supplies the parts a source build cannot
    # make. Losing them is silent until someone tries to use the package, so
    # check the ones that matter before anything is packed.
    Write-Step 'Checking the archive-provided layout'
    $RequiredPaths = @('deplister.exe', 'dev', 'extras\ssl\openssl.cnf', 'phar.phar.bat', 'pharcommand.phar', 'lib\enchant')
    if ($MajorMinor -in @('7.0', '7.1', '7.2', '7.3', '7.4')) { $RequiredPaths += 'sasl2' }
    $MissingPaths = @($RequiredPaths | Where-Object { -not (Test-Path (Join-Path $Stage $_)) })
    if ($MissingPaths) {
        throw "The official archive did not provide: $($MissingPaths -join ', ')"
    }
    # The enchant backend is named per branch (libenchant_ispell/myspell on
    # 7.x, libenchant2_hunspell on 8.x), so just require the directory to be
    # populated.
    if (-not (Get-ChildItem (Join-Path $Stage 'lib\enchant') -Filter '*.dll' -File -ErrorAction SilentlyContinue)) {
        throw 'lib\enchant\ carries no engines; the enchant extension cannot spell-check'
    }
    $DevLib = Get-ChildItem (Join-Path $Stage 'dev') -Filter 'php*.lib' -File -ErrorAction SilentlyContinue
    if (-not $DevLib) { throw 'dev\ carries no php*.lib; phpize could not build an extension' }

    # Load the extensions that depend on the bundled DLLs. php -v only proves
    # the CLI starts; this proves the layout actually resolves OpenSSL, ICU,
    # libpq and libsasl the way an installed copy will. The same applies to
    # the PECL DLLs: they link the dependency bundle too.
    Write-Step 'Smoke-testing extension loading from the staged layout'
    $SmokeArgs = @('-n', '-d', "extension_dir=$StageExt")
    # 7.x ships php_gd2.dll and registers "gd"; 8.x ships php_gd.dll.
    $GdFile = @('gd2', 'gd') | Where-Object { Test-Path (Join-Path $StageExt "php_$_.dll") } | Select-Object -First 1
    $SmokeExts = @('openssl', 'curl', 'intl', 'pgsql', 'ldap')
    if ($GdFile) { $SmokeExts += $GdFile }
    # igbinary first: redis and msgpack pick it as their serializer if present.
    # sqlsrv/pdo_sqlsrv and oci8 need an external driver, and xdebug only loads
    # as a zend_extension, so none of those are exercised here.
    foreach ($ext in @('igbinary', 'redis', 'mongodb', 'imagick', 'yaml', 'msgpack', 'oauth', 'apcu', 'grpc', 'protobuf', 'memcache', 'memcached')) {
        if ($PeclExts.ContainsKey($ext)) { $SmokeExts += $ext }
    }
    $Loaded = @()
    foreach ($ext in $SmokeExts) {
        if (Test-Path (Join-Path $StageExt "php_$ext.dll")) {
            $SmokeArgs += @('-d', "extension=php_$ext.dll")
            # php_gd2.dll registers itself as "gd".
            $Loaded += ($ext -replace '2$', '')
        }
    }
    # Ask PHP which modules it actually registered. A startup failure is a
    # warning, not a non-zero exit, so the exit code alone proves nothing:
    # an earlier build printed "igbinary: Unable to initialize module" and
    # still exited 0.
    $Smoke = & (Join-Path $Stage 'php.exe') @SmokeArgs -r "echo implode(',', get_loaded_extensions());" 2>&1
    $SmokeText = ($Smoke | Out-String)
    Write-Host "  $($SmokeText.Trim())"
    if ($LASTEXITCODE -ne 0) { throw "Extension smoke test failed with $LASTEXITCODE" }
    if ($SmokeText -match 'Unable to (load dynamic library|initialize module)') {
        throw "An extension could not start: $SmokeText"
    }
    # Split the module list instead of regex-matching it: module names are not
    # all lower case (OAuth) and Windows line endings put a \r where an end
    # anchor would look.
    $ModuleLine = ($Smoke | Out-String) -split "`r?`n" | Where-Object { $_ -match '^Core,' } | Select-Object -Last 1
    if (-not $ModuleLine) { throw "Smoke test printed no module list: $SmokeText" }
    $LoadedModules = $ModuleLine -split ',' | ForEach-Object { $_.Trim() }
    foreach ($ext in $Loaded) {
        if ($LoadedModules -notcontains $ext) {
            throw "Extension $ext is in the package but not loaded: $ModuleLine"
        }
    }

    # Loading imagick only proves the DLL resolves. Its coder modules are
    # looked up at runtime, so a package that puts them somewhere ImageMagick
    # does not search looks fine until someone converts an image. Encode and
    # decode for real.
    #
    # 7.0 is the exception: the newest imagick PECL archive that supports it
    # (3.4.3) ships no coders at all - they arrived in 3.4.4, which is 7.1
    # only - so its conversion needs an ImageMagick installed on the machine.
    # The package has always been like that, so say so instead of failing.
    if (Test-Path (Join-Path $StageExt 'php_imagick.dll')) {
        $ImagickCoders = @(Get-ChildItem $Stage -Filter 'IM_MOD_RL_*.dll' -File)
        if (-not $ImagickCoders) {
            Write-Warning "imagick for $MajorMinor carries no IM_MOD_RL_* coders; image conversion needs an external ImageMagick"
        } else {
            Write-Step 'Smoke-testing an imagick encode/decode round trip'
            $ImagickArgs = @('-n', '-d', "extension_dir=$StageExt", '-d', 'extension=php_imagick.dll')
            $ImagickCode = @'
$im = new Imagick();
$im->newImage(8, 8, 'red');
$im->setImageFormat('png');
$blob = $im->getImageBlob();
$back = new Imagick();
$back->readImageBlob($blob);
echo $back->getImageWidth() . 'x' . $back->getImageHeight();
'@
            $ImagickOut = & (Join-Path $Stage 'php.exe') @ImagickArgs -r $ImagickCode 2>&1
            $ImagickText = ($ImagickOut | Out-String)
            Write-Host "  $($ImagickText.Trim())"
            if ($LASTEXITCODE -ne 0) { throw "imagick round trip failed with $LASTEXITCODE" }
            if ($ImagickText -notmatch '8x8') {
                throw "imagick could not encode/decode PNG; its coders are not where it looks: $ImagickText"
            }
        }
    }

    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $Archive = Join-Path $OutputDir "php-$Version-windows-$Arch.7z"
    if (Test-Path $Archive) { Remove-Item -Force $Archive }
    Write-Step "Packing $Archive"
    Push-Location $Stage
    try {
        & $SevenZip a -t7z -m0=lzma2 -mx=9 -mmt=on $Archive . | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "7z failed with $LASTEXITCODE" }
    }
    finally { Pop-Location }

    Write-Host "BUILT: $Archive"
}
finally {
    Pop-Location
}
