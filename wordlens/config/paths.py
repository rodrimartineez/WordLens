"""Dónde vive cada cosa: archivos de la aplicación vs. datos del usuario.

Producción (aplicación instalada, sys.frozen):
    código y recursos   -> carpeta de instalación (sólo lectura)
    datos internos      -> %LOCALAPPDATA%\\WordLens\\  (config.json, logs, backups, pending)
    documento Word      -> donde elija el usuario

Desarrollo (python main.py):
    datos internos      -> <proyecto>\\.devdata\\  (nunca se mezcla con producción)

WORDLENS_DATA_DIR permite forzar otra carpeta de datos (tests, pruebas aisladas).
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

APP_NAME = "WordLens"
IS_FROZEN = bool(getattr(sys, "frozen", False))

# Carpeta del código fuente (desarrollo) o del bundle de PyInstaller (producción).
if IS_FROZEN:
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    RESOURCE_ROOT = PROJECT_ROOT

ASSETS_DIR = RESOURCE_ROOT / "assets"
ICON_PATH = ASSETS_DIR / "wordlens.ico"


def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) if base else Path.home() / "AppData" / "Local"


def data_dir() -> Path:
    override = os.environ.get("WORDLENS_DATA_DIR")
    if override:
        path = Path(override)
    elif IS_FROZEN:
        path = _local_appdata() / APP_NAME
    else:
        path = PROJECT_ROOT / ".devdata"
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = DATA_DIR / "backups"
PENDING_DIR = DATA_DIR / "pending"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def documents_dir() -> Path:
    """Carpeta "Documentos" real del usuario (respeta la redirección a OneDrive)."""
    folder_id = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
    guid = _GUID.from_buffer_copy(folder_id.bytes_le)
    out = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except (AttributeError, OSError):
        pass
    return Path.home() / "Documents"


def controlled_folder_access_enabled() -> bool:
    """¿Está activo el "Acceso controlado a carpetas" de Windows Defender?

    Con él activo, las apps no autorizadas no pueden escribir en Documentos,
    Imágenes, Escritorio... (Windows devuelve "archivo no encontrado").
    """
    import winreg

    keys = (
        r"SOFTWARE\Policies\Microsoft\Windows Defender\Windows Defender Exploit Guard\Controlled Folder Access",
        r"SOFTWARE\Microsoft\Windows Defender\Windows Defender Exploit Guard\Controlled Folder Access",
    )
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "EnableControlledFolderAccess")
                return value == 1  # 2 = sólo auditoría: no bloquea
        except OSError:
            continue
    return False


def default_document() -> Path:
    """Ubicación sugerida en el primer uso: Documentos/WordLens.

    Si el acceso controlado a carpetas está activo, Documentos está protegida y
    WordLens no podría escribir ahí: se sugiere la carpeta personal del usuario.
    """
    base = Path.home() if controlled_folder_access_enabled() else documents_dir()
    return base / APP_NAME / "Vocabulary.docx"
