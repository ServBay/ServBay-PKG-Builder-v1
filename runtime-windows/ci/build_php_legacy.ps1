<#
.SYNOPSIS
    Compile an EOL PHP release for Windows with the toolchain that release targets.

.DESCRIPTION
    PHP 7.0 is the newest branch that can still be built on a GitHub-hosted
    runner. Its official toolchain is Visual C++ 14.0, which no runner image
    carries; the standalone VS2015 bootstrapper is retired (aka.ms/vs/14 is
    dead and aka.ms/vs/15 is the VS2017 installer). The toolset is instead
    added to the installed Visual Studio as the VC.140 component, exactly as
    php-windows-builder does for the branches above it. Everything else -
    source, dependency libraries, build tools - comes from php.net, so the
    only non-upstream input is the security patch this repository carries.

    Two compatibility shims are required, both discovered by building 7.0 on a
    clean Windows 11 machine:

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
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Version,
    [Parameter(Mandatory = $true)][ValidateSet('x64', 'x86')][string] $Arch,
    [Parameter(Mandatory = $true)][string] $OutputDir,
    [Parameter(Mandatory = $true)][string] $PatchDir
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# x.y.z.N is a local backport of upstream x.y.z.
$BaseVersion = $Version
if ($Version -match '^(\d+\.\d+\.\d+)\.\d+$') {
    $BaseVersion = $Matches[1]
}
$MajorMinor = ($BaseVersion -split '\.')[0] + '.' + ($BaseVersion -split '\.')[1]

# This branch of PHP shipped prebuilt dependencies for VC14 only on 7.0 and 7.1.
# 7.2 and later moved to VC15 and build through the main Windows pipeline.
if ($MajorMinor -notin @('7.0', '7.1')) {
    throw "build_php_legacy.ps1 covers the VC14 branches (7.0, 7.1); got $MajorMinor"
}

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

$Root = 'C:\phplegacy'
$SdkDir = Join-Path $Root 'sdk'
$BuildRoot = Join-Path $Root 'phpdev\vc14'
$BuildDir = Join-Path $BuildRoot $Arch
$ToolsDir = Join-Path $Root 'tools'

Write-Step "PHP $Version ($Arch) from upstream $BaseVersion"
New-Item -ItemType Directory -Force -Path $Root, $SdkDir, $ToolsDir | Out-Null

# --- Toolchain -------------------------------------------------------------
# PHP 7.0 shipped against MSVC 14.0, and php.net only published a VC14
# dependency bundle for it. No hosted runner image carries that toolset and
# Microsoft no longer ships it: aka.ms/vs/14 is dead, aka.ms/vs/15 is the
# VS2017 installer, and asking the runner's own VS installer for the VC.140
# component returns 0 while changing nothing ("Status changed to NoUpdate").
#
# So v140 is used when the image happens to have it, and otherwise the newest
# installed toolset. That is safe for this branch: MSVC has kept the
# vcruntime140 ABI since 2015, configure.js only warns about an untested
# compiler version, and the VC14 import libraries link fine under a newer
# linker.
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
    Where-Object { $_.Name -match '^14\.0\d*\.' } |
    Sort-Object Name -Descending |
    Select-Object -First 1
if ($Toolset) {
    Write-Step "Using the MSVC v140 toolset $($Toolset.Name)"
} else {
    $Toolset = Get-ChildItem $MsvcRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $Toolset) { throw "No MSVC toolset under $MsvcRoot" }
    Write-Warning "MSVC v140 is not available; building PHP $BaseVersion with $($Toolset.Name)"
}
$ToolsetVer = $Toolset.Name
$VcVars = Join-Path $VsPath 'VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path $VcVars)) { throw "vcvarsall.bat is missing: $VcVars" }
Write-Step "Using MSVC $ToolsetVer from $VsPath"

# --- Upstream inputs -------------------------------------------------------
$SdkZip = Join-Path $ToolsDir 'php-sdk-binary-tools.zip'
if (-not (Test-Path $SdkZip)) {
    Write-Step 'Downloading php-sdk-binary-tools'
    Invoke-WebRequest -Uri 'https://downloads.php.net/~windows/php-sdk/php-sdk-binary-tools-20110915.zip' -OutFile $SdkZip
}
Expand-Archive -Path $SdkZip -DestinationPath $SdkDir -Force
# configure.js treats bison as a hard requirement (the SDK bundle carries it);
# the runner has no bison of its own.
$env:PATH = "$(Join-Path $SdkDir 'bin');" + $env:PATH

$DepsArchive = Join-Path $ToolsDir "deps-$MajorMinor-vc14-$Arch.7z"
if (-not (Test-Path $DepsArchive)) {
    Write-Step "Downloading PHP $MajorMinor VC14 dependency libraries ($Arch)"
    Invoke-WebRequest -Uri "https://downloads.php.net/~windows/php-sdk/archives/deps-$MajorMinor-vc14-$Arch.7z" -OutFile $DepsArchive
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
    & (Join-Path $SdkDir 'bin\phpsdk_buildtree.bat') phpdev | Out-Null
}
finally { Pop-Location }
# buildtree predates vc14 and only creates vc6..vc9; copy vc9 to get the layout.
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
if (-not (Test-Path $BuildDir)) {
    Copy-Item -Recurse -Force (Join-Path $BuildRoot 'vc9') $BuildDir
}

$SevenZip = 'C:\Program Files\7-Zip\7z.exe'
if (-not (Test-Path $SevenZip)) { $SevenZip = Join-Path $ToolsDir '7zr.exe' }
if (-not (Test-Path $SevenZip)) {
    Invoke-WebRequest -Uri 'https://www.7-zip.org/a/7zr.exe' -OutFile (Join-Path $ToolsDir '7zr.exe')
    $SevenZip = Join-Path $ToolsDir '7zr.exe'
}
Write-Step 'Extracting dependency libraries'
& $SevenZip x $DepsArchive "-o$BuildDir" -y | Out-Null

$SourceDir = Join-Path $BuildDir "php-$BaseVersion-src"
if (Test-Path $SourceDir) { Remove-Item -Recurse -Force $SourceDir }
Write-Step 'Extracting PHP source'
& tar -xzf $SourceArchive -C $BuildDir
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

    # --- Configure ---------------------------------------------------------
    # PHP regenerates CFLAGS inside configure, so the shim is forced in twice:
    # through CL for configure's own probe compiles, and through the generated
    # Makefile for the real build.
    $Shim = Join-Path $Root 'vc14-intrinsics.h'
    if (-not (Test-Path $Shim)) { throw "Missing intrinsics shim: $Shim" }

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
    # cl.exe honours the CL variable on every invocation, including the probe
    # compiles configure.js runs. Setting it here keeps the extra probes that
    # --enable-snapshot-build performs from tripping over the UCRT header
    # before we get to touch the Makefile.
    $env:CL = "/FI`"$Shim`""

    # --enable-snapshot-build is what php.net's own Windows releases use: it
    # turns on every optional module and builds each one shared, which is what
    # puts the 37 php_*.dll files in ext/. Without it the package would be a
    # bare CLI that cannot serve a ServBay site.
    $configureArgs = @('--enable-snapshot-build', '--disable-zts', '--enable-cli', '--enable-cgi')
    & cmd /c "`"$VcVars`" $ArchArg -vcvars_ver=$ToolsetVer && cscript //nologo configure.js $($configureArgs -join ' ')"

    # --- Inject the shim ---------------------------------------------------
    $Makefile = Join-Path $SourceDir 'Makefile'
    $lines = Get-Content $Makefile
    $patched = foreach ($l in $lines) {
        if ($l -match '^CFLAGS=' -and $l -notmatch 'vc14-intrinsics') { $l + " /FI`"$Shim`"" } else { $l }
    }
    $patched | Set-Content -Path $Makefile -Encoding ASCII

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
    Write-Host ($Verify | Out-String)
    if ($Verify -notmatch [regex]::Escape($BaseVersion)) {
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

    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $Archive = Join-Path $OutputDir "php-$Version-windows-$Arch.7z"
    if (Test-Path $Archive) { Remove-Item -Force $Archive }
    Write-Step "Packing $Archive"
    Push-Location $ReleaseDir
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
