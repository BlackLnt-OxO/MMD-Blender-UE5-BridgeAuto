param(
    [Parameter(Mandatory=$true)][string]$Project,
    [Parameter(Mandatory=$true)][string]$Unreal
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$projectFile = Get-Item -LiteralPath $Project
if ($projectFile.Extension -ne '.uproject') { throw 'Select a .uproject file.' }
$engineExe = Get-Item -LiteralPath $Unreal
$engine = $engineExe.Directory.Parent.Parent.FullName
$version = Get-Content -LiteralPath (Join-Path $engine 'Build/Build.version') -Raw | ConvertFrom-Json
if ($version.MajorVersion -ne 5 -or $version.MinorVersion -ne 8) { throw 'Only UE 5.8 is supported.' }
$engineModules = Get-Content -LiteralPath (Join-Path $engineExe.Directory.FullName 'UnrealEditor.modules') -Raw | ConvertFrom-Json
$projectRoot = $projectFile.Directory.FullName
$pluginRoot = Join-Path $projectRoot 'Plugins'
$info = Get-Content -LiteralPath $projectFile.FullName -Raw | ConvertFrom-Json
$running = Get-CimInstance Win32_Process -Filter "Name = 'UnrealEditor.exe'"
if ($running | Where-Object { -not $_.CommandLine -or $_.CommandLine.IndexOf($projectFile.FullName, [StringComparison]::OrdinalIgnoreCase) -ge 0 }) {
    throw 'Save and close the target UE editor before installing plugins. No editor will be closed automatically.'
}
$packages = @(
    @{ Name='MMD2Unreal'; Url='https://codeload.github.com/BlackLnt-OxO/MMD2Unreal/zip/854b89510e4d35c549b18a7a7f6189f0ca9c1938'; Hash='69d8374268dd7467b836639797ba21ce827b6e5bc4e42a15221faf1154c45ac2' },
    @{ Name='KawaiiPhysics'; Url='https://github.com/pafuhana1213/KawaiiPhysics/releases/download/20260624-v1.21.0/KawaiiPhysics_5.8_1.21.0.zip'; Hash='2876aa19090ee28f3e8ed96a5d621a793baeafd5b475986f21510182998fe6e2' }
)
function Validate-Plugin([string]$Folder, [string]$Name) {
    $descriptor = Get-Content -LiteralPath (Join-Path $Folder "$Name.uplugin") -Raw | ConvertFrom-Json
    $binaryRoot = Join-Path $Folder 'Binaries/Win64'
    $modules = Get-Content -LiteralPath (Join-Path $binaryRoot 'UnrealEditor.modules') -Raw | ConvertFrom-Json
    if ($modules.BuildId -ne $engineModules.BuildId) { throw "$Name binary BuildId does not match the selected UE. No compilation or replacement was attempted." }
    if (-not $modules.Modules.PSObject.Properties.Count) { throw "$Name has no precompiled modules." }
    foreach ($entry in $modules.Modules.PSObject.Properties) {
        if (-not (Test-Path -LiteralPath (Join-Path $binaryRoot $entry.Value) -PathType Leaf)) { throw "$Name DLL is missing: $($entry.Value)" }
    }
}
$cache = Join-Path $env:LOCALAPPDATA ('MMDBridge/plugin-downloads/' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $cache -Force | Out-Null
$ready = @()
foreach ($package in $packages) {
    $name = $package.Name
    $existing = @()
    foreach ($root in @($pluginRoot, (Join-Path $engine 'Plugins'))) {
        if (Test-Path -LiteralPath $root) { $existing += @(Get-ChildItem -LiteralPath $root -Filter "$name.uplugin" -Recurse -File) }
    }
    if ($existing.Count -gt 1) { throw "Multiple $name installations found. Resolve duplicates first." }
    if ($existing.Count -eq 1) {
        Validate-Plugin $existing[0].Directory.FullName $name
        Write-Output "Already installed: $name"
        continue
    }
    $destination = Join-Path $pluginRoot $name
    if (Test-Path -LiteralPath $destination) { throw "Existing folder will not be overwritten: $destination" }
    $zip = Join-Path $cache "$name.zip"
    Write-Output "Downloading $name from $($package.Url)"
    Invoke-WebRequest -UseBasicParsing -Uri $package.Url -OutFile $zip -TimeoutSec 600
    $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($package.Hash -and $hash -ne $package.Hash) { throw "$name checksum mismatch." }
    $extract = Join-Path $cache $name
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $prefix = [IO.Path]::GetFullPath($extract) + [IO.Path]::DirectorySeparatorChar
        foreach ($entry in $archive.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $extract $entry.FullName))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe archive path.' }
        }
    } finally { $archive.Dispose() }
    Expand-Archive -LiteralPath $zip -DestinationPath $extract
    $descriptors = @(Get-ChildItem -LiteralPath $extract -Recurse -Filter "$name.uplugin" -File)
    if ($descriptors.Count -ne 1) { throw "$name archive does not contain exactly one plugin." }
    $source = $descriptors[0].Directory.FullName
    Validate-Plugin $source $name
    $ready += @{Name=$name; Source=$source; Destination=$destination; Url=$package.Url; SHA256=$hash}
}
# Validate every package before modifying the project. Never overwrite a plugin.
New-Item -ItemType Directory -Path $pluginRoot -Force | Out-Null
foreach ($item in $ready) {
    Copy-Item -LiteralPath $item.Source -Destination $item.Destination -Recurse -ErrorAction Stop
    Write-Output "Installed: $($item.Destination)"
}
$backup = $projectFile.FullName + '.plugins-' + [guid]::NewGuid().ToString('N') + '.bak'
Copy-Item -LiteralPath $projectFile.FullName -Destination $backup
if (-not $info.PSObject.Properties['Plugins']) { $info | Add-Member -NotePropertyName Plugins -NotePropertyValue @() }
foreach ($package in $packages) {
    $entry = $info.Plugins | Where-Object { $_.Name -eq $package.Name } | Select-Object -First 1
    if ($entry) { $entry | Add-Member -NotePropertyName Enabled -NotePropertyValue $true -Force }
    else { $info.Plugins = @($info.Plugins) + [pscustomobject]@{Name=$package.Name; Enabled=$true} }
}
$json = $info | ConvertTo-Json -Depth 100
[IO.File]::WriteAllText($projectFile.FullName, $json, (New-Object Text.UTF8Encoding($false)))
@{ Project=$projectFile.FullName; Packages=$ready; Backup=$backup } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $cache 'install-receipt.json') -Encoding UTF8
Write-Output 'Installation complete. Open the UE project again, then retry the import.'
