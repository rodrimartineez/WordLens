"""Ollama como dependencia externa: detectar, arrancar y comprobar el modelo.

WordLens no incluye Ollama ni el modelo (varios GB): se instalan aparte una vez.
Aquí se resuelve lo necesario para que la app instalada funcione sin terminal:
- localizar la instalación de Ollama (PATH o rutas por defecto de su instalador);
- arrancarlo en segundo plano si está instalado pero apagado;
- distinguir "no instalado" / "apagado" / "falta el modelo" para mostrar un
  mensaje útil y la acción adecuada en la interfaz.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .ollama_client import OllamaClient, OllamaError

log = logging.getLogger(__name__)

DOWNLOAD_URL = "https://ollama.com/download/windows"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


class OllamaState(Enum):
    READY = "ready"
    NOT_INSTALLED = "not_installed"
    NOT_RUNNING = "not_running"
    MODEL_MISSING = "model_missing"


@dataclass(frozen=True)
class OllamaStatus:
    state: OllamaState
    message: str
    version: str = ""

    @property
    def ok(self) -> bool:
        return self.state is OllamaState.READY


def _candidate_dirs() -> list[Path]:
    dirs = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Programs" / "Ollama")
    for env in ("ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Ollama")
    return dirs


def find_ollama() -> Path | None:
    """Ruta de ollama.exe, o None si no está instalado."""
    found = shutil.which("ollama")
    if found:
        return Path(found)
    for d in _candidate_dirs():
        exe = d / "ollama.exe"
        if exe.exists():
            return exe
    return None


def launch_ollama() -> bool:
    """Arranca Ollama en segundo plano. Devuelve False si no está instalado.

    Se prefiere "ollama app.exe" (lo mismo que abrirlo desde Inicio: servidor +
    icono propio); si no existe, "ollama serve" sin ventana.
    """
    exe = find_ollama()
    if exe is None:
        return False
    tray_app = exe.with_name("ollama app.exe")
    try:
        if tray_app.exists():
            subprocess.Popen([str(tray_app)], creationflags=DETACHED_PROCESS, close_fds=True)
        else:
            subprocess.Popen(
                [str(exe), "serve"],
                creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
    except OSError:
        log.exception("No se pudo arrancar Ollama")
        return False
    log.info("Arrancando Ollama: %s", tray_app if tray_app.exists() else exe)
    return True


def wait_until_running(client: OllamaClient, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.version()
            return True
        except OllamaError:
            time.sleep(0.5)
    return False


def check(client: OllamaClient, model: str, *, autostart: bool = True) -> OllamaStatus:
    """Estado de Ollama y del modelo. Si está apagado y autostart, intenta arrancarlo."""
    try:
        version = client.version()
    except OllamaError:
        if find_ollama() is None:
            return OllamaStatus(
                OllamaState.NOT_INSTALLED,
                "Ollama is not installed. WordLens needs it to run the local AI model.",
            )
        if not (autostart and launch_ollama() and wait_until_running(client)):
            return OllamaStatus(
                OllamaState.NOT_RUNNING,
                "Ollama is installed but not responding. Open Ollama from the Start menu and try again.",
            )
        version = client.version()
    try:
        present = client.has_model(model)
    except OllamaError as exc:
        return OllamaStatus(OllamaState.NOT_RUNNING, f"Ollama is not responding ({exc}).", version)
    if not present:
        return OllamaStatus(
            OllamaState.MODEL_MISSING,
            f"The AI model {model} is not downloaded yet.",
            version,
        )
    return OllamaStatus(OllamaState.READY, f"Ollama {version} · {model}", version)
