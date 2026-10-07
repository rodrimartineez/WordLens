"""Atajos de teclado globales con la API nativa RegisterHotKey de Win32.

Ventajas frente a hooks de teclado (librerías keyboard/pynput):
- no requiere permisos de administrador;
- no intercepta ni "ve" el resto de teclas que pulsas (mejor privacidad);
- Windows avisa si el atajo ya lo usa otra aplicación.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT,
    "alt": MOD_ALT, "win": MOD_WIN, "windows": MOD_WIN,
}
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "pause": 0x13, "printscreen": 0x2C,
}
for _i in range(1, 25):
    _NAMED_KEYS[f"f{_i}"] = 0x6F + _i
for _i in range(10):
    _NAMED_KEYS[f"num{_i}"] = 0x60 + _i

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


class HotkeyError(ValueError):
    pass


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+shift+v' -> (modificadores, código de tecla virtual)."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"Atajo vacío: {spec!r}")
    mods, key = 0, None
    for part in parts:
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
        elif key is not None:
            raise HotkeyError(f"Atajo con más de una tecla principal: {spec!r}")
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif part in _NAMED_KEYS:
            key = _NAMED_KEYS[part]
        else:
            raise HotkeyError(f"Tecla desconocida {part!r} en {spec!r}")
    if key is None:
        raise HotkeyError(f"Falta la tecla principal en {spec!r}")
    if mods == 0 and not (0x70 <= key <= 0x87):
        raise HotkeyError(f"Un atajo global necesita modificadores (Ctrl/Shift/Alt): {spec!r}")
    return mods, key


class HotkeyManager:
    """Registra atajos y despacha sus callbacks en un hilo dedicado."""

    def __init__(self) -> None:
        self._bindings: dict[int, tuple[str, Callable[[], None]]] = {}
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self.failed: dict[str, str] = {}

    def add(self, spec: str, callback: Callable[[], None]) -> None:
        if self._thread is not None:
            raise RuntimeError("Añade los atajos antes de start()")
        parse_hotkey(spec)  # valida pronto
        self._bindings[len(self._bindings) + 1] = (spec, callback)

    def start(self, timeout: float = 5.0) -> dict[str, str]:
        """Arranca el hilo. Devuelve {atajo: error} de los que no se pudieron registrar."""
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.failed

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        for hk_id, (spec, _) in self._bindings.items():
            mods, vk = parse_hotkey(spec)
            if user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                registered.append(hk_id)
                log.info("Atajo registrado: %s", spec)
            else:
                err = ctypes.get_last_error()
                msg = "already used by another application" if err == 1409 else f"Win32 error {err}"
                self.failed[spec] = msg
                log.error("No se pudo registrar el atajo %s: %s", spec, msg)
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in self._bindings:
                    spec, callback = self._bindings[msg.wParam]
                    log.debug("Atajo pulsado: %s", spec)
                    try:
                        callback()
                    except Exception:
                        log.exception("Error en el callback del atajo %s", spec)
        finally:
            for hk_id in registered:
                user32.UnregisterHotKey(None, hk_id)

    def stop(self) -> None:
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)
