param([string]$ExternalRoot = (Split-Path $PSScriptRoot -Parent | Split-Path -Parent), [switch]$CloneMissing)
$ErrorActionPreference = 'Stop'
$repositories = @{
    MMD2Unreal = 'https://github.com/BlackLnt-OxO/MMD2Unreal'
    KawaiiPhysics = 'https://github.com/pafuhana1213/KawaiiPhysics'
}
foreach ($name in $repositories.Keys) {
    $target = Join-Path $ExternalRoot $name
    Write-Host "$name : $($repositories[$name]) -> $target"
    if ($CloneMissing -and -not (Test-Path -LiteralPath $target)) {
        & git clone $repositories[$name] $target
        if ($LASTEXITCODE -ne 0) { throw "$name clone failed" }
    }
    Write-Host "UE 5.8 release: $($repositories[$name])/releases"
}
Write-Host 'Clone is a source/reference checkout, not installation. Install matching UE 5.8 release plugins into <UEProject>/Plugins/MMD2Unreal and <UEProject>/Plugins/KawaiiPhysics, then restart UE.'
