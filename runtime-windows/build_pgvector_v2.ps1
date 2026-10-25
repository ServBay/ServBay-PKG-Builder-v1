[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $SourceArchive,
    [Parameter(Mandatory = $true)] [string] $CoreArchive,
    [Parameter(Mandatory = $true)] [string] $OutputDir,
    [Parameter(Mandatory = $true)] [ValidatePattern('^[a-f0-9]{64}$')] [string] $ExpectedSourceSha256,
    [Parameter(Mandatory = $true)] [ValidatePattern('^[a-f0-9]{64}$')] [string] $ExpectedCoreArchiveSha256,
    [string] $SourceVersion = '0.8.7',
    [ValidatePattern('^[1-9][0-9]*$')] [string] $PostgresqlMajor = '18',
    [string] $SevenZip = 'C:\Program Files\7-Zip\7z.exe',
    [string] $VsDevCmd = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-LowerSha256 {
    param([string] $Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-RegularFile {
    param([string] $Path, [string] $Description)
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Description must be a regular file"
    }
    return $item
}

function Assert-X64Pe {
    param([string] $Path)
    $stream = [IO.File]::OpenRead($Path)
    $reader = New-Object IO.BinaryReader($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) { throw "Expected MZ header: $(Split-Path -Leaf $Path)" }
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset
        $signature = $reader.ReadBytes(4)
        if ([BitConverter]::ToString($signature) -ne '50-45-00-00') { throw "Expected PE header: $(Split-Path -Leaf $Path)" }
        if ($reader.ReadUInt16() -ne 0x8664) { throw "Expected x86_64 PE machine: $(Split-Path -Leaf $Path)" }
    } finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

function Get-SafeDiagnosticText {
    param([string] $Text, [object[]] $Paths)
    $safe = [string] $Text
    $known = @($Paths | Where-Object { $null -ne $_ -and [string] $_ } |
        ForEach-Object { [IO.Path]::GetFullPath([string] $_) } |
        Sort-Object Length -Descending -Unique)
    foreach ($path in $known) {
        $safe = [regex]::Replace($safe, [regex]::Escape($path), '<path>',
            [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    }
    return $safe
}

try {
$source = Assert-RegularFile $SourceArchive 'pgvector source archive'
$coreArchiveFile = Assert-RegularFile $CoreArchive 'PostgreSQL core archive'
if ((Get-LowerSha256 $source.FullName) -ne $ExpectedSourceSha256) { throw 'pgvector source SHA256 mismatch' }
if ((Get-LowerSha256 $coreArchiveFile.FullName) -ne $ExpectedCoreArchiveSha256) { throw 'PostgreSQL core archive SHA256 mismatch' }
if (-not (Test-Path -LiteralPath $SevenZip -PathType Leaf)) { throw '7-Zip was not found' }
if (-not $VsDevCmd) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) { throw 'vswhere was not found' }
    $installationPath = (& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Out-String).Trim()
    if (-not $installationPath) { throw 'Visual Studio C++ Build Tools were not found' }
    $VsDevCmd = Join-Path $installationPath 'Common7\Tools\VsDevCmd.bat'
}
if (-not (Test-Path -LiteralPath $VsDevCmd -PathType Leaf)) { throw 'VsDevCmd was not found' }

$output = [IO.Path]::GetFullPath($OutputDir).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
if (-not $output -or $output -eq [IO.Path]::GetPathRoot($output).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)) {
    throw 'OutputDir must identify a new directory below a filesystem root'
}
if (Test-Path -LiteralPath $output) { throw 'OutputDir must not exist' }
$outputParent = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) { throw 'OutputDir parent must exist' }

$work = $null
$pending = $null
try {
    $work = Join-Path ([IO.Path]::GetTempPath()) ('servbay-pgvector-' + [guid]::NewGuid().ToString('N'))
    $pending = Join-Path $outputParent ('.' + (Split-Path -Leaf $output) + '.pending-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    New-Item -ItemType Directory -Path $pending | Out-Null
    $frozenSource = Join-Path $work 'source.tar.gz'
    $frozenCore = Join-Path $work 'postgresql-core.7z'
    [IO.File]::Copy($source.FullName, $frozenSource)
    [IO.File]::Copy($coreArchiveFile.FullName, $frozenCore)
    if ((Get-LowerSha256 $frozenSource) -ne $ExpectedSourceSha256) { throw 'pgvector frozen source SHA256 mismatch' }
    if ((Get-LowerSha256 $frozenCore) -ne $ExpectedCoreArchiveSha256) { throw 'PostgreSQL frozen core SHA256 mismatch' }
    $tarNames = @(& tar.exe -tzf $frozenSource)
    if ($LASTEXITCODE -ne 0 -or $tarNames.Count -eq 0) { throw 'Cannot list pgvector source tar' }
    $sourcePrefix = 'pgvector-' + $SourceVersion + '/'
    foreach ($name in $tarNames) {
        if ($name.Contains('\') -or $name.StartsWith('/') -or $name -match '^[A-Za-z]:' -or
                @($name.Split('/') | Where-Object { $_ -eq '..' -or $_ -eq '.' }).Count -gt 0 -or
                ($name -ne $sourcePrefix.TrimEnd('/') -and -not $name.StartsWith($sourcePrefix))) {
            throw "Unsafe or unexpected pgvector source tar path: $name"
        }
    }
    $extract = Join-Path $work 'source'
    New-Item -ItemType Directory -Path $extract | Out-Null
    & tar.exe -xzf $frozenSource -C $extract
    if ($LASTEXITCODE -ne 0) { throw "tar.exe failed with exit code $LASTEXITCODE" }
    $coreExtract = Join-Path $work 'core'
    New-Item -ItemType Directory -Path $coreExtract | Out-Null
    & $SevenZip x -y ("-o$coreExtract") -- $frozenCore | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "7-Zip core extraction failed with exit code $LASTEXITCODE" }
    $core = Get-Item -LiteralPath (Join-Path $coreExtract ('postgresql\' + $PostgresqlMajor)) -ErrorAction Stop
    if (-not $core.PSIsContainer) { throw 'Extracted PostgreSQL major root is not a directory' }
    foreach ($relative in 'bin\pg_config.exe', 'include\server\postgres.h', 'lib\postgres.lib') {
        if (-not (Test-Path -LiteralPath (Join-Path $core.FullName $relative) -PathType Leaf)) {
            throw "Extracted core lacks $relative"
        }
    }
    $sourceRoot = Join-Path $extract ('pgvector-' + $SourceVersion)
    $makefile = Join-Path $sourceRoot 'Makefile.win'
    if (-not (Test-Path -LiteralPath $makefile -PathType Leaf)) { throw 'pgvector source lacks Makefile.win' }
    $versionLine = Select-String -LiteralPath $makefile -Pattern '^EXTVERSION\s*=\s*(\S+)\s*$'
    if ($null -eq $versionLine -or $versionLine.Matches.Count -ne 1 -or
            $versionLine.Matches[0].Groups[1].Value -ne $SourceVersion) {
        throw 'pgvector Makefile.win EXTVERSION differs from SourceVersion'
    }

    $buildScript = Join-Path $work 'build.cmd'
    $buildLines = @(
        '@echo off',
        ('call "' + $VsDevCmd + '" -arch=amd64 -host_arch=amd64'),
        'if errorlevel 1 exit /b 10',
        ('cd /d "' + $sourceRoot + '"'),
        ('set "PGROOT=' + $core.FullName + '"'),
        'cl',
        'rem The no-argument cl line records the actual compiler banner; nmake below is authoritative.',
        'nmake /F Makefile.win',
        'exit /b %errorlevel%'
    )
    [IO.File]::WriteAllText($buildScript, ($buildLines -join [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false)))
    $quotedBuildScript = '"' + $buildScript + '"'
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $buildLinesOutput = @(& cmd.exe /d /s /c $quotedBuildScript 2>&1)
        $buildExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $buildOutput = ($buildLinesOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    $safeBuildOutput = $buildOutput.Replace($core.FullName, '<core>').Replace($sourceRoot, '<source>').Replace($work, '<work>')
    if ($buildExit -ne 0) { throw "pgvector nmake failed with exit code $($buildExit): $safeBuildOutput" }

    $vectorDll = Join-Path $sourceRoot 'vector.dll'
    $control = Join-Path $sourceRoot 'vector.control'
    $initialSql = Join-Path $sourceRoot ('sql\vector--' + $SourceVersion + '.sql')
    foreach ($path in $vectorDll, $control, $initialSql) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Build output missing $(Split-Path -Leaf $path)" }
    }
    $sqlFiles = @(Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'sql') -File -Filter 'vector--*.sql')
    if ($sqlFiles.Count -eq 0) { throw 'Build output lacks pgvector SQL files' }
    Assert-X64Pe $vectorDll

    $install = Join-Path $pending 'install'
    foreach ($relative in 'lib', 'share\extension', 'include\server\extension\vector') {
        New-Item -ItemType Directory -Path (Join-Path $install $relative) -Force | Out-Null
    }
    [IO.File]::Copy($vectorDll, (Join-Path $install 'lib\vector.dll'))
    [IO.File]::Copy($control, (Join-Path $install 'share\extension\vector.control'))
    foreach ($sqlFile in $sqlFiles) {
        [IO.File]::Copy($sqlFile.FullName, (Join-Path $install ('share\extension\' + $sqlFile.Name)))
    }
    foreach ($name in 'vector.h', 'halfvec.h', 'sparsevec.h') {
        [IO.File]::Copy((Join-Path $sourceRoot ('src\' + $name)),
            (Join-Path $install ('include\server\extension\vector\' + $name)))
    }

    $files = Get-ChildItem -LiteralPath $install -Recurse -File | ForEach-Object {
        [ordered]@{
            path = $_.FullName.Substring($install.Length + 1).Replace('\', '/')
            bytes = $_.Length
            sha256 = Get-LowerSha256 $_.FullName
        }
    }
    $compiler = [regex]::Match($buildOutput, 'Optimizing Compiler Version ([0-9.]+)')
    if (-not $compiler.Success) { throw 'Unable to identify the actual MSVC compiler version' }
    $postgresqlVersion = (& (Join-Path $core.FullName 'bin\pg_config.exe') --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $postgresqlVersion -notmatch '^PostgreSQL ([0-9]+)\.([0-9]+)$') {
        throw 'Extracted core pg_config did not report its actual version'
    }
    if ($Matches[1] -ne $PostgresqlMajor) { throw 'Extracted core major differs from PostgresqlMajor' }
    $audit = [ordered]@{
        schema = 'servbay.windows-pgvector-build.v1'
        publishable_v2 = $false
        native_validation = 'pending'
        payload_root = 'install'
        source = [ordered]@{
            filename = $source.Name
            version = $SourceVersion
            bytes = $source.Length
            sha256 = $ExpectedSourceSha256
        }
        host = [ordered]@{
            core_package_sha256 = $ExpectedCoreArchiveSha256
            postgresql_version = $postgresqlVersion
            core_root_layout = 'postgresql/<major>'
        }
        toolchain = [ordered]@{
            requested = 'MSVC amd64 via VsDevCmd'
            actual_compiler = $compiler.Groups[1].Value
            crt = 'pending'
        }
        files = @($files)
        compatibility = 'pending'
        minimum_os = 'pending'
    }
    $auditText = ($audit | ConvertTo-Json -Depth 8) + [Environment]::NewLine
    [IO.File]::WriteAllText((Join-Path $pending 'build-audit.json'), $auditText,
        (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText((Join-Path $pending 'build.log'), $safeBuildOutput,
        (New-Object Text.UTF8Encoding($false)))
    if ((Get-LowerSha256 $source.FullName) -ne $ExpectedSourceSha256 -or
            (Get-LowerSha256 $coreArchiveFile.FullName) -ne $ExpectedCoreArchiveSha256) {
        throw 'pgvector source/core archive changed during build'
    }
    [IO.Directory]::Move($pending, $output)
} finally {
    if ($null -ne $work -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force }
    if ($null -ne $pending -and (Test-Path -LiteralPath $pending)) { Remove-Item -LiteralPath $pending -Recurse -Force }
}

[ordered]@{
    output_dir = Split-Path -Leaf $output
    install_dir = 'install'
    build_audit = 'build-audit.json'
} | ConvertTo-Json
} catch {
    $paths = @($SourceArchive, $CoreArchive, $OutputDir, $SevenZip, $VsDevCmd)
    foreach ($name in 'source', 'coreArchiveFile', 'output', 'outputParent', 'work', 'pending',
            'frozenSource', 'frozenCore', 'extract', 'coreExtract', 'core', 'sourceRoot') {
        $value = Get-Variable -Name $name -ValueOnly -ErrorAction SilentlyContinue
        if ($null -ne $value) {
            if ($value -is [IO.FileSystemInfo]) { $paths += $value.FullName }
            elseif ($value -is [string]) { $paths += $value }
        }
    }
    throw (Get-SafeDiagnosticText $_.Exception.Message $paths)
}
