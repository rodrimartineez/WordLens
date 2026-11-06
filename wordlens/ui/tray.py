"""Icono en la bandeja del sistema: estado, menú y notificaciones.

pystray ejecuta su bucle de mensajes en su propio hilo (run_detached); las
acciones del menú se pasan a la ventana con call_soon() para que se ejecuten
en el hilo de Tk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pystray

from wordlens.state import AppState
from wordlens.ui.icon import render_icon, with_status_dot

if TYPE_CHECKING:
    from wordlens.ui.window import MainWindow

log = logging.getLogger(__name__)

DOT_COLORS = {
    "Capturing": (22, 163, 74),
    "Processing": (217, 119, 6),
    "Finishing": (217, 119, 6),
    "Starting…": (217, 119, 6),
    "Error": (220, 38, 38),
}


def pretty_hotkey(spec: str) -> str:
    return "+".join(p.capitalize() for p in spec.split("+"))


class TrayUI:
    def __init__(self, window: MainWindow) -> None:
        self.window = window
        self.app = window.app
        self._base = render_icon(64)
        self._icons: dict[str, object] = {}
        self._last_status = ""
        w = window
        self.icon = pystray.Icon(
            "WordLens",
            icon=self._icon_for(self._status()),
            title=self._title(),
            menu=pystray.Menu(
                pystray.MenuItem("Open WordLens", lambda *_: w.call_soon(w.show), default=True),
                pystray.MenuItem(lambda _: f"Status: {self._status()}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda _: f"Start  ({pretty_hotkey(self.app.settings.hotkey_start)})",
                    lambda *_: w.call_soon(w.start_capture),
                    enabled=lambda _: not self.app.state.is_active and self.app.settings.setup_complete,
                ),
                pystray.MenuItem(
                    lambda _: f"Stop  ({pretty_hotkey(self.app.settings.hotkey_stop)})",
                    lambda *_: w.call_soon(w.stop_capture),
                    enabled=lambda _: self.app.state.is_active,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda _: f"Review vocabulary ({self._to_review()})…",
                    lambda *_: w.call_soon(w.open_review),
                    enabled=lambda _: self.app.settings.setup_complete,
                ),
                pystray.MenuItem(
                    "Open vocabulary document",
                    lambda *_: w.call_soon(w.open_document),
                    enabled=lambda _: self.app.settings.setup_complete,
                ),
                pystray.MenuItem(
                    "Change vocabulary document…",
                    lambda *_: w.call_soon(self._change_document),
                    enabled=lambda _: not self.app.state.is_active and self.app.settings.setup_complete,
                ),
                pystray.MenuItem("Settings…", lambda *_: w.call_soon(self._settings)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda *_: w.call_soon(w.exit)),
            ),
        )

    def _to_review(self) -> int:
        try:
            c = self.app.counts()
            return c["pending"] + c["unsaved"]
        except Exception:  # noqa: BLE001
            return 0

    def _status(self) -> str:
        try:
            return self.window.status_text()
        except Exception:  # durante el arranque
            return "Ready"

    def _icon_for(self, status: str):
        if status not in self._icons:
            self._icons[status] = with_status_dot(self._base, DOT_COLORS.get(status))
        return self._icons[status]

    def _title(self) -> str:
        status = self._status()
        title = f"WordLens — {status}"
        if self.app.state.state is not AppState.INACTIVE and status != "Error":
            title += f" ({self._to_review()} to review)"
        return title[:127]  # límite de Windows para tooltips

    def _change_document(self) -> None:
        self.window.show()
        self.window.change_document()

    def _settings(self) -> None:
        self.window.show()
        self.window.open_settings()

    def refresh(self) -> None:
        try:
            status = self._status()
            if status != self._last_status:
                self._last_status = status
                self.icon.icon = self._icon_for(status)
            self.icon.title = self._title()
            self.icon.update_menu()
        except Exception:
            log.debug("No se pudo refrescar el icono", exc_info=True)

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message[:250], title)
        except Exception:
            log.debug("Notificación no disponible", exc_info=True)

    def start(self) -> None:
        self.icon.run_detached()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            log.debug("No se pudo cerrar el icono", exc_info=True)
