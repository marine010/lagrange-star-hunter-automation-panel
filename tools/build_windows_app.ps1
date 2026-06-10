param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Assert-PathInsideRepo {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Path))
    $repoFullPath = [System.IO.Path]::GetFullPath($RepoRoot)
    if (-not $fullPath.StartsWith($repoFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside repository: $fullPath"
    }
    return $fullPath
}

if (-not $SkipDependencyInstall) {
    python -m pip install --upgrade pip
    python -m pip install -r requirements-build.txt
}

$distApp = Assert-PathInsideRepo "dist\LagrangeStarHunter"
$buildApp = Assert-PathInsideRepo "build\LagrangeStarHunter"

foreach ($target in @($distApp, $buildApp)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

python -m PyInstaller --noconfirm --clean packaging\pyinstaller\LagrangeStarHunter.spec

$payloadRoot = Assert-PathInsideRepo "dist\LagrangeStarHunter"
if (-not (Test-Path -LiteralPath (Join-Path $payloadRoot "LagrangeStarHunter.exe"))) {
    throw "PyInstaller build did not produce LagrangeStarHunter.exe"
}

foreach ($dirName in @("configs", "templates")) {
    $source = Join-Path $RepoRoot $dirName
    $destination = Join-Path $payloadRoot $dirName
    if (Test-Path -LiteralPath $destination) {
        Remove-Item -LiteralPath $destination -Recurse -Force
    }
    Copy-Item -LiteralPath $source -Destination $destination -Recurse
}

foreach ($fileName in @("README.md", "RELEASE_README_CN.txt", "LICENSE")) {
    $source = Join-Path $RepoRoot $fileName
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $payloadRoot $fileName)
    }
}

Write-Host "Built portable app folder: $payloadRoot"
Write-Host "Main executable: $(Join-Path $payloadRoot 'LagrangeStarHunter.exe')"
