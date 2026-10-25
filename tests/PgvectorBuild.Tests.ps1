param(
    [Parameter(Mandatory = $true)] [string] $SourceArchive,
    [Parameter(Mandatory = $true)] [string] $CoreArchive,
    [Parameter(Mandatory = $true)] [string] $SevenZip,
    [Parameter(Mandatory = $true)] [string] $ProducerPath,
    [string] $PythonExe = 'python',
    [string] $UnsafeTarArchive = '',
    [string] $SourceVersion = '0.8.7',
    [string] $SourceSha256 = 'cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85',
    [string] $CoreSha256 = 'f500d3df630bb450a83699f1ad1a46e2220f50360a44c36db5d1d434078ee263'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$producerBlock = [ScriptBlock]::Create([IO.File]::ReadAllText($ProducerPath))
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('ServBay pgvector tests ' + [guid]::NewGuid().ToString('N'))
$output = Join-Path $testRoot 'success output'
$temporary = Join-Path $testRoot 'temporary files'

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { throw $Message }
}

function Assert-PathFree {
    param([string] $Text, [string[]] $Paths)
    foreach ($path in $Paths) {
        if ($path -and $Text.IndexOf($path, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Diagnostic output contains an absolute path"
        }
    }
    if ($Text -match '(?i)(?:^|[^A-Za-z0-9])[A-Za-z]:\\') { throw "Diagnostic output contains a drive path" }
}

$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    New-Item -ItemType Directory -Path $temporary -Force | Out-Null
    $env:TEMP = $temporary
    $env:TMP = $temporary
    $result = ((& $producerBlock -SourceArchive $SourceArchive -CoreArchive $CoreArchive -OutputDir $output -ExpectedSourceSha256 $SourceSha256 -ExpectedCoreArchiveSha256 $CoreSha256 -SourceVersion $SourceVersion -SevenZip $SevenZip) | Out-String) | ConvertFrom-Json
    Assert-True ($result.output_dir -eq 'success output') 'Producer returned an absolute output path'
    Assert-True ($result.install_dir -eq 'install') 'Producer install path is not relative'
    Assert-True ($result.build_audit -eq 'build-audit.json') 'Producer audit path is not relative'

    $auditPath = Join-Path $output 'build-audit.json'
    $logPath = Join-Path $output 'build.log'
    $audit = Get-Content -LiteralPath $auditPath -Raw | ConvertFrom-Json
    $installFiles = @(Get-ChildItem -LiteralPath (Join-Path $output 'install') -Recurse -File)
    Assert-True ($audit.publishable_v2 -eq $false) 'Producer unexpectedly marked its build publishable'
    Assert-True ($audit.native_validation -eq 'pending') 'Producer unexpectedly claimed native validation'
    Assert-True ($audit.payload_root -eq 'install') 'Producer payload root is missing'
    Assert-True ($audit.host.core_package_sha256 -eq $CoreSha256) 'Producer did not bind the core SHA'
    Assert-True ($audit.toolchain.actual_compiler -match '^[0-9]+\.[0-9]+\.[0-9]+$') 'Producer did not record the actual compiler'
    Assert-True ($audit.files.Count -eq $installFiles.Count) 'Audit file count differs from install'
    Assert-True (@($audit.files | Where-Object { $_.path -like 'share/extension/*.sql' }).Count -eq 42) 'Producer did not retain all pgvector SQL files'
    Assert-True (@($audit.files | Where-Object { $_.path -eq 'lib/vector.dll' }).Count -eq 1) 'Producer did not retain vector.dll'

    $diagnostics = (Get-Content -LiteralPath $logPath -Raw) + ($result | ConvertTo-Json -Depth 4)
    Assert-PathFree $diagnostics @($SourceArchive, $CoreArchive, $SevenZip, $temporary, $testRoot)
    Assert-True (-not ((Get-Content -LiteralPath $logPath -Raw).Contains('<work>\core\'))) 'Log kept work-relative core path instead of the core marker'
    Assert-True (-not ((Get-Content -LiteralPath $logPath -Raw).Contains('<work>\source\'))) 'Log kept work-relative source path instead of the source marker'
    Assert-True (@(Get-ChildItem -LiteralPath $temporary -Force -ErrorAction SilentlyContinue).Count -eq 0) 'Producer left temporary work directories'

    $badOutput = Join-Path $testRoot 'wrong sha output'
    $wrongDigestRejected = $false
    try {
        & $producerBlock -SourceArchive $SourceArchive -CoreArchive $CoreArchive -OutputDir $badOutput -ExpectedSourceSha256 ('0' * 64) -ExpectedCoreArchiveSha256 $CoreSha256 -SourceVersion $SourceVersion -SevenZip $SevenZip | Out-Null
    } catch {
        $wrongDigestRejected = $true
    }
    Assert-True $wrongDigestRejected 'Producer accepted a wrong source SHA'
    Assert-True (-not (Test-Path -LiteralPath $badOutput)) 'Wrong SHA left an output directory'

    if ($UnsafeTarArchive) {
        $badTar = $UnsafeTarArchive
    } else {
        $badTar = Join-Path $testRoot 'unsafe-source.tar.gz'
        $pythonCode = @'
import io, tarfile
with tarfile.open(r'UNSAFE', 'w:gz') as archive:
    content = b'bad'
    info = tarfile.TarInfo('pgvector-0.8.7/../../evil.txt')
    info.size = len(content)
    archive.addfile(info, io.BytesIO(content))
'@.Replace('UNSAFE', $badTar)
        & $PythonExe -c $pythonCode
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create traversal tar fixture' }
    }
    $badTarSha = (Get-FileHash -LiteralPath $badTar -Algorithm SHA256).Hash.ToLowerInvariant()
    $badTarOutput = Join-Path $testRoot 'unsafe tar output'
    $traversalRejected = $false
    try {
        & $producerBlock -SourceArchive $badTar -CoreArchive $CoreArchive -OutputDir $badTarOutput -ExpectedSourceSha256 $badTarSha -ExpectedCoreArchiveSha256 $CoreSha256 -SourceVersion $SourceVersion -SevenZip $SevenZip | Out-Null
    } catch {
        $traversalRejected = $_.Exception.Message -match 'Unsafe or unexpected'
    }
    Assert-True $traversalRejected 'Producer did not reject tar traversal'
    Assert-True (-not (Test-Path -LiteralPath $badTarOutput)) 'Tar traversal left an output directory'
    Assert-True (@(Get-ChildItem -LiteralPath $temporary -Force -ErrorAction SilentlyContinue).Count -eq 0) 'Failure paths left temporary work directories'

    $failingVsDev = Join-Path $testRoot 'failing VsDevCmd.bat'
    $failingLines = @('@echo off', 'exit /b 1') -join [Environment]::NewLine
    [IO.File]::WriteAllText($failingVsDev, $failingLines + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false)))
    $failedBuildOutput = Join-Path $testRoot 'failed build output'
    $failedMessage = ''
    try {
        & $producerBlock -SourceArchive $SourceArchive -CoreArchive $CoreArchive -OutputDir $failedBuildOutput -ExpectedSourceSha256 $SourceSha256 -ExpectedCoreArchiveSha256 $CoreSha256 -SourceVersion $SourceVersion -SevenZip $SevenZip -VsDevCmd $failingVsDev | Out-Null
    } catch {
        $failedMessage = $_.Exception.Message
    }
    Assert-True ($failedMessage -ne '') 'Forced build failure unexpectedly succeeded'
    Assert-PathFree $failedMessage @($SourceArchive, $CoreArchive, $SevenZip, $temporary, $testRoot)
    Assert-True (-not (Test-Path -LiteralPath $failedBuildOutput)) 'Failed build left an output directory'

    $global:LASTEXITCODE = 0
    Write-Output 'PASS: pgvector build handoff behavior'
} finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
