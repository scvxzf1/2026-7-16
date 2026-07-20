[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Version = "v1.19.28"
$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not $InstallDir) {
    $InstallDir = Join-Path $ProjectDir "bin"
}

if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
    [Runtime.InteropServices.OSPlatform]::Windows
)) {
    throw "This installer requires Windows."
}

$Architecture = if ($env:PROCESSOR_ARCHITEW6432) {
    $env:PROCESSOR_ARCHITEW6432
} else {
    $env:PROCESSOR_ARCHITECTURE
}
switch ($Architecture.ToUpperInvariant()) {
    "AMD64" {
        $Asset = "mihomo-windows-amd64-compatible-$Version.zip"
        $ArchiveSha256 = "6d8a079d01b3631e73e56b7b42a067afc14f9e3ad99f2880d38bb141cf8fcbe7"
    }
    "ARM64" {
        $Asset = "mihomo-windows-arm64-$Version.zip"
        $ArchiveSha256 = "25cedfb999864e834a3d8424cb8ea61b9145b3cb3aea0180b9fdc009623abeda"
    }
    "X86" {
        $Asset = "mihomo-windows-386-$Version.zip"
        $ArchiveSha256 = "1cc14bdde317b38b861569c1d2aaacaf49907c1707b5aa38838e549b451549b1"
    }
    default {
        throw "Unsupported Windows architecture: $Architecture"
    }
}

$Target = Join-Path $InstallDir "proxy-core.exe"
if ((Test-Path -LiteralPath $Target) -and -not $Force) {
    $InstalledVersion = ""
    try {
        $InstalledVersion = (& $Target -v 2>&1 | Out-String).Trim()
    } catch {
        $InstalledVersion = ""
    }
    if ($InstalledVersion -like "*$Version*") {
        Write-Host "Mihomo $Version is already installed at $Target"
        exit 0
    }
    throw "$Target already exists; use -Force to replace it."
}

$TemporaryDir = Join-Path ([IO.Path]::GetTempPath()) ("gdl-mihomo-" + [Guid]::NewGuid())
$Archive = Join-Path $TemporaryDir $Asset
$ExtractDir = Join-Path $TemporaryDir "extracted"
$Url = "https://github.com/MetaCubeX/mihomo/releases/download/$Version/$Asset"
$TemporaryTarget = $null

try {
    New-Item -ItemType Directory -Path $TemporaryDir | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Host "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing

    $ActualArchiveSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
    if ($ActualArchiveSha256 -ne $ArchiveSha256) {
        throw "Archive SHA-256 mismatch. Expected $ArchiveSha256, got $ActualArchiveSha256."
    }

    Expand-Archive -LiteralPath $Archive -DestinationPath $ExtractDir
    $Executables = @(Get-ChildItem -LiteralPath $ExtractDir -Recurse -File -Filter "*.exe")
    if ($Executables.Count -ne 1) {
        throw "The release archive must contain exactly one executable; found $($Executables.Count)."
    }
    $Executable = $Executables[0]

    $VersionOutput = (& $Executable.FullName -v 2>&1 | Out-String).Trim()
    if ($VersionOutput -notlike "*$Version*") {
        throw "Downloaded executable reported an unexpected version: $VersionOutput"
    }

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $TemporaryTarget = Join-Path $InstallDir (".proxy-core-" + [Guid]::NewGuid() + ".exe")
    Copy-Item -LiteralPath $Executable.FullName -Destination $TemporaryTarget
    Unblock-File -LiteralPath $TemporaryTarget -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $TemporaryTarget -Destination $Target -Force

    $ExecutableSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash.ToLowerInvariant()
    Write-Host "Installed $Target"
    Write-Host "Executable SHA-256: $ExecutableSha256"
    Write-Host $VersionOutput
} finally {
    if ($TemporaryTarget -and (Test-Path -LiteralPath $TemporaryTarget)) {
        Remove-Item -LiteralPath $TemporaryTarget -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $TemporaryDir) {
        Remove-Item -LiteralPath $TemporaryDir -Recurse -Force
    }
}
