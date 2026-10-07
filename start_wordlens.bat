@echo off
rem DESARROLLO: inicia WordLens desde el codigo fuente, sin consola.
rem La aplicacion instalada se abre desde el menu Inicio (ver README).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo No se encuentra el entorno virtual. Ejecuta primero build.ps1
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py %*
