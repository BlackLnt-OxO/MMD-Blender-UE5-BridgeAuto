param(
    [string]$PythonExe,
    [string]$Unreal,
    [switch]$Dev
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'

function Assert-Python([string]$Executable, [string[]]$Prefix) {
    & $Executable @Prefix -c "import sys; assert sys.platform == 'win32' and sys.maxsize > 2**32 and (3,11) <= sys.version_info[:2] < (3,13), 'Use 64-bit Python 3.12 or 3.11'"
    if ($LASTEXITCODE -ne 0) { throw 'Use 64-bit Python 3.12 or 3.11. See docs/INSTALL_WINDOWS.md.' }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    if (Test-Path -LiteralPath (Join-Path $repoRoot '.venv')) {
        throw 'Existing .venv is incomplete. Rename it before retrying; setup will not delete it.'
    }
    $pythonPrefix = @()
    if ($PythonExe) {
        $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
    } else {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            foreach ($version in @('-3.12', '-3.11')) {
                try { & $launcher.Source $version -c 'import sys; assert sys.maxsize > 2**32' 2>$null }
                catch { continue }
                if ($LASTEXITCODE -eq 0) {
                    $PythonExe = $launcher.Source
                    $pythonPrefix = @($version)
                    break
                }
            }
        }
        if (-not $PythonExe) {
            $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
            if (-not $pythonCommand) { throw 'Install 64-bit Python 3.12, then retry. See docs/INSTALL_WINDOWS.md.' }
            $PythonExe = $pythonCommand.Source
        }
    }
    Assert-Python $PythonExe $pythonPrefix
    & $PythonExe @pythonPrefix -m venv (Join-Path $repoRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
Assert-Python $venvPython @()
$installTarget = $repoRoot
if ($Dev) { $installTarget += '[dev]' }
& $venvPython -m pip install --editable $installTarget
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the pip output and retry setup.' }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Installed Python dependencies are inconsistent.' }
& $venvPython (Join-Path $PSScriptRoot 'check_installation.py')
if ($LASTEXITCODE -ne 0) { throw 'Required source resources or Python dependencies are missing.' }
if ($Unreal) {
    & $venvPython (Join-Path $PSScriptRoot 'build_viewport_helper.py') --unreal $Unreal
    if ($LASTEXITCODE -ne 0) { throw 'UE helper compilation failed; see the build output.' }
}
Write-Host 'Ready. Run start.cmd. External software and UE projects are selected in the GUI.'
if (-not $Unreal) { Write-Host 'For viewport fog/PT switching and selected glass geometry, build the UE helper as described in docs/INSTALL_WINDOWS.md.' }
