"""Pantallas conectadas y pantalla elegida para capturar los subtítulos.

Cada pantalla se identifica por el identificador del monitor físico que da
Windows (EnumDisplayDevices con EDD_GET_DEVICE_INTERFACE_NAME), no por su
posición en la lista: así la elección sobrevive a desconectar y volver a
conectar monitores, aunque cambie el orden o el nombre \\\\.\\DISPLAYn.

Numeración que ve el usuario: Screen 1 es la principal y las demás siguen de
izquierda a derecha (y de arriba abajo). El botón "Identify" de la ventana
muestra el número sobre cada pantalla.

Las coordenadas son píxeles físicos del escritorio virtual: el proceso debe ser
"DPI aware" (main.py y enable_dpi_awareness() lo garantizan), igual que mss.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

MONITORINFOF_PRIMARY = 1
EDD_GET_DEVICE_INTERFACE_NAME = 1


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


_MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


@dataclass(frozen=True)
class Monitor:
    number: int  # número que ve el usuario (1 = principal)
    device_id: str  # identificador estable del monitor físico
    device: str  # \\.\DISPLAYn (puede cambiar al reconectar)
    left: int
    top: int
    width: int
    height: int
    primary: bool

    @property
    def rect(self) -> dict[str, int]:
        """Formato de mss: {"left", "top", "width", "height"}."""
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}

    @property
    def label(self) -> str:
        extra = " · main" if self.primary else ""
        return f"Screen {self.number} · {self.width}×{self.height}{extra}"


def _device_id(device: str) -> str:
    dd = _DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(dd)
    try:
        if ctypes.windll.user32.EnumDisplayDevicesW(device, 0, ctypes.byref(dd), EDD_GET_DEVICE_INTERFACE_NAME):
            if dd.DeviceID:
                return dd.DeviceID
    except (AttributeError, OSError):
        pass
    return device


def list_monitors() -> list[Monitor]:
    """Pantallas conectadas ahora mismo (nunca lanza: lista vacía si falla la API)."""
    raw: list[tuple[str, wintypes.RECT, bool]] = []

    def callback(hmon, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            raw.append((info.szDevice, info.rcMonitor, bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    try:
        ctypes.windll.user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(callback), 0)
    except (AttributeError, OSError):
        log.exception("No se pudieron enumerar las pantallas")
        return []
    raw.sort(key=lambda m: (not m[2], m[1].left, m[1].top))
    monitors = []
    for number, (device, r, primary) in enumerate(raw, start=1):
        monitors.append(
            Monitor(
                number=number,
                device_id=_device_id(device),
                device=device,
                left=r.left,
                top=r.top,
                width=r.right - r.left,
                height=r.bottom - r.top,
                primary=primary,
            )
        )
    return monitors


@dataclass(frozen=True)
class Selection:
    monitor: Monitor | None  # pantalla que se capturará (None: no hay ninguna)
    missing: bool  # la pantalla elegida no está conectada: se usa la principal


def resolve(monitors: list[Monitor], device_id: str, number: int = 1) -> Selection:
    """Pantalla a capturar según la configuración.

    1. La pantalla con ese identificador, si está conectada.
    2. Sin identificador guardado (configuración antigua): la de ese número.
    3. Si la elegida no está conectada: la principal, marcando missing=True. La
       configuración no se toca, así que al volver a conectarla se usa otra vez.
    """
    if not monitors:
        return Selection(None, bool(device_id))
    if device_id:
        for m in monitors:
            if m.device_id == device_id:
                return Selection(m, False)
        primary = next((m for m in monitors if m.primary), monitors[0])
        return Selection(primary, True)
    for m in monitors:
        if m.number == number:
            return Selection(m, False)
    return Selection(next((m for m in monitors if m.primary), monitors[0]), False)


def signature(monitors: list[Monitor]) -> tuple:
    """Huella de la configuración de pantallas, para detectar cambios."""
    return tuple((m.device_id, m.left, m.top, m.width, m.height, m.primary) for m in monitors)
