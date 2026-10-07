# Instalador de VocabLens. Ejecutar desde la carpeta del proyecto:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Es seguro ejecutarlo varias veces.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[ OK ] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[AVISO] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "[FALLO] $msg" -ForegroundColor Red }

Step "Python"
$py = $null
foreach ($cand in @("py -3", "python")) {
    try {
        $ver = & cmd /c "$cand -c ""import sys; print('%d.%d' % sys.version_info[:2])""" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Fail "Python no encontrado. Instálalo desde https://www.python.org/downloads/windows/ (marca 'Add python.exe to PATH') o con: winget install Python.Python.3.13"
    exit 1
}
$major, $minor = $ver.Split(".") | ForEach-Object { [int]$_ }
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) { Fail "Se necesita Python 3.10 o superior (tienes $ver)"; exit 1 }
Ok "Python $ver ($py)"

Step "Entorno virtual y paquetes"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & cmd /c "$py -m venv .venv"
    Ok "Entorno virtual creado en .venv"
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Fail "Falló la instalación de paquetes"; exit 1 }
Ok "Paquetes instalados"

Step "OCR de Windows (inglés)"
$langs = & .\.venv\Scripts\python.exe -c "from vocablens.ocr.ocr_engine import available_languages as a; print(','.join(a()))"
if ($langs -match "en-") {
    Ok "Idiomas OCR: $langs"
} else {
    Warn "Falta el paquete OCR de inglés (instalados: $langs)."
    Warn "Opción A: Configuración > Hora e idioma > Idioma y región > Agregar idioma > English (United States)."
    Warn "Opción B (PowerShell como administrador):"
    Warn "  Add-WindowsCapability -Online -Name 'Language.OCR~~~en-US~0.0.1.0'"
}

Step "Ollama"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Fail "Ollama no está instalado. Descárgalo de https://ollama.com/download/windows o: winget install Ollama.Ollama"
    Warn "Después vuelve a ejecutar este instalador."
    exit 1
}
Ok "Ollama: $(& ollama --version)"
$model = & .\.venv\Scripts\python.exe -c "from vocablens.config.settings import load_settings; print(load_settings().ollama_model)"
$list = & ollama list 2>$null
if ($LASTEXITCODE -ne 0) {
    Warn "Ollama no responde. Ábrelo desde el menú Inicio y vuelve a ejecutar el instalador."
    exit 1
}
if ($list -match [regex]::Escape($model)) {
    Ok "Modelo $model ya descargado"
} else {
    Write-Host "Descargando $model (~5.3 GB, solo la primera vez)..."
    & ollama pull $model
    if ($LASTEXITCODE -ne 0) { Fail "No se pudo descargar el modelo"; exit 1 }
    Ok "Modelo descargado"
}

Step "Diagnóstico"
& .\.venv\Scripts\python.exe tools\diagnose.py
Write-Host "`nListo. Inicia la aplicación con start_vocablens.bat" -ForegroundColor Green
