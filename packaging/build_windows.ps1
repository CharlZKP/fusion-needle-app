# Build the single-folder Windows executable. Run on Windows, from anywhere:
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 [-WithEngine] [-Console]
#
#   -WithEngine   copy the Needle engine DLL from this machine's cache into the build, so the
#                 first start needs no download of it (run `.venv\Scripts\needle fetch` first)
#   -Console      keep a console window (useful while debugging)
#
# NOT TESTED: written on Linux, where PyInstaller cannot produce a Windows build.
# Steps, smoke test and what to send back when it fails: packaging\README.md.
# PyInstaller is installed into a separate build environment (build\pyi-venv),
# never into the repo's .venv; that environment only reads the repo's site-packages.
#
# Works in Windows PowerShell 5.1 and PowerShell 7. Keep this file plain ASCII: 5.1 reads a
# script without a byte-order mark in the ANSI code page.
param([switch]$WithEngine, [switch]$Console)
$ErrorActionPreference = "Stop"
Write-Host "PowerShell $($PSVersionTable.PSVersion)  on  $([System.Environment]::OSVersion.VersionString)"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = Split-Path -Parent $Here
$Root = $Desktop
$Py = if ($env:FN_PYTHON) { $env:FN_PYTHON } else { Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Py -PathType Leaf)) { throw "No Python at $Py. Create the repo's .venv first (py -3.12 -m venv .venv; .venv\Scripts\python -m pip install -e .) or set FN_PYTHON." }

# 64-bit Python 3.12+ with stepserver and needle: say so here, not as a PyInstaller traceback later
& $Py -c "import sys, stepserver, needle; print('build python', sys.version); raise SystemExit(0 if sys.version_info >= (3, 12) and sys.maxsize > 2**32 else 3)"
if ($LASTEXITCODE -ne 0) { throw "$Py must be a 64-bit Python 3.12+ that can import stepserver and needle (exit code $LASTEXITCODE). From the repo folder: .venv\Scripts\python -m pip install -e ." }

$Site = & $Py -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" | Select-Object -Last 1
if ($LASTEXITCODE -ne 0 -or -not $Site) { throw "could not read the site-packages folder of $Py" }
$Site = "$Site".Trim()
$BuildVenv = Join-Path $Desktop "build\pyi-venv"
$BuildPy = Join-Path $BuildVenv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $BuildPy -PathType Leaf)) {
    & $Py -m venv $BuildVenv
    if ($LASTEXITCODE -ne 0) { throw "could not create $BuildVenv" }
}
# asked on every run: an earlier run may have created the environment and then failed to install into it
# (no "2>" on a native command here: with ErrorActionPreference Stop, PowerShell 5.1 turns redirected
#  stderr text into a terminating error)
& $BuildPy -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)"
if ($LASTEXITCODE -ne 0) {
    & $BuildPy -m pip install --upgrade pip "pyinstaller>=6.6"
    if ($LASTEXITCODE -ne 0) { throw "could not install PyInstaller (is this machine online? pip needs pypi.org)" }
}

$env:PYTHONPATH = "$Site;$Desktop"
if ($Console) { $env:FN_CONSOLE = "1" } else { Remove-Item Env:FN_CONSOLE -ErrorAction SilentlyContinue }
Push-Location $Desktop
try {
    & $BuildPy -m PyInstaller --noconfirm --clean --distpath (Join-Path $Desktop "dist") `
        --workpath (Join-Path $Desktop "build\work") (Join-Path $Here "fusion_needle.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally { Pop-Location }

$Out = Join-Path $Desktop "dist\FusionNeedle"
$Exe = Join-Path $Out "FusionNeedle.exe"
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw "PyInstaller reported success but $Exe is missing" }
if ($WithEngine) {
    $Engine = & $Py -c "from needle.agent import fetch; import os; print(os.path.join(fetch.cache_dir(3), fetch.lib_name(3)))"
    if ($LASTEXITCODE -ne 0 -or -not $Engine -or -not (Test-Path -LiteralPath $Engine -PathType Leaf)) { throw "No engine at '$Engine'. Run .venv\Scripts\needle fetch first." }
    # needle looks for libneedle3.dll inside its own package folder before the cache
    Copy-Item -LiteralPath $Engine -Destination (Join-Path $Out "_internal\needle\libneedle3.dll") -Force
    Write-Host "engine copied into the build"
}
Write-Host "built $Out"
Write-Host "next: the smoke test in packaging\README.md"
