@echo off
rem Inicia WordLens en segundo plano (sin ventana de consola).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo No se encuentra el entorno virtual. Ejecuta primero install.ps1
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py %*
