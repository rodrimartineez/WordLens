# Construye WordLens desde cero: entorno, tests, ejecutable e instalador.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipTests
#
# Resultado:
#   dist\WordLens\WordLens.exe                    aplicación (carpeta)
#   dist\installer\WordLens-Setup-<versión>.exe   instalador
#
# La aplicación instalada NO depende de este script ni de Python.

param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "[FALLO] $msg" -ForegroundColor Red; exit 1 }

$version = (Select-String -Path "wordlens\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "WordLens $version" -ForegroundColor Green

Step "Entorno de desarrollo (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $py = if (Get-Command py -ErrorAction SilentlyContinue) { "py -3" } else { "python" }
    & cmd /c "$py -m venv .venv"
    if ($LASTEXITCODE -ne 0) { Fail "No se pudo crear el entorno virtual (¿Python 3.10+ instalado?)" }
}
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { Fail "Falló la instalación de paquetes" }

if (-not $SkipTests) {
    Step "Tests (sin los que usan el modelo real)"
    & $python -m pytest -q -m "not llm"
    if ($LASTEXITCODE -ne 0) { Fail "Hay tests fallando" }
}

Step "Icono"
& $python tools\make_icon.py

Step "Ejecutable (PyInstaller)"
& $python -m PyInstaller packaging\wordlens.spec --noconfirm --clean --distpath dist --workpath build --log-level WARN
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller falló" }

Step "Prueba del ejecutable empaquetado (--self-test)"
$testData = Join-Path $env:TEMP "wordlens-build-selftest"
Remove-Item -Recurse -Force $testData -ErrorAction SilentlyContinue
$env:WORDLENS_DATA_DIR = $testData
$proc = Start-Process "dist\WordLens\WordLens.exe" -ArgumentList "--self-test" -Wait -PassThru
Remove-Item Env:\WORDLENS_DATA_DIR
Get-Content -Encoding UTF8 (Join-Path $testData "logs\wordlens.log") | Select-String "SELFTEST" | ForEach-Object { Write-Host "   $($_.Line.Substring(24))" }
if ($proc.ExitCode -ne 0) { Fail "El ejecutable empaquetado no supera la prueba" }

if (-not $SkipInstaller) {
    Step "Instalador (Inno Setup)"
    $iscc = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { Fail "Inno Setup no está instalado. Instálalo con: winget install JRSoftware.InnoSetup" }
    & $iscc /Q "/DAppVersion=$version" packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { Fail "Inno Setup falló" }
    $setup = Resolve-Path "dist\installer\WordLens-Setup-$version.exe"
    Write-Host "`nInstalador: $setup" -ForegroundColor Green
    Write-Host ("Tamaño: {0:N1} MB" -f ((Get-Item $setup).Length / 1MB))
}
