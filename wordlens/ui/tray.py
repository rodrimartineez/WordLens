"""Icono en la bandeja del sistema: estado, menú y notificaciones."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

from wordlens.app import SessionStats, WordLensApp
from wordlens.config.settings import DEFAULT_CONFIG_PATH, LOG_DIR, save_settings, to_project_relative
from wordlens.state import AppState

log = logging.getLogger(__name__)

STATE_COLORS = {
    AppState.INACTIVE: (128, 128, 128),
    AppState.CAPTURING: (46, 160, 67),
    AppState.PROCESSING: (230, 145, 30),
    AppState.STOPPED: (200, 50, 50),
}


def make_icon(state: AppState, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 2, size - 2), fill=STATE_COLORS[state] + (255,))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.6))
    except OSError:
        font = ImageFont.load_default()
    draw.text((size / 2, size / 2), "W", font=font, fill="white", anchor="mm")
    return img


def pretty_hotkey(spec: str) -> str:
    return "+".join(p.capitalize() for p in spec.split("+"))


class TrayUI:
    def __init__(self, app: WordLensApp) -> None:
        self.app = app
        s = app.settings
        self.icon = pystray.Icon(
            "WordLens",
            icon=make_icon(app.state.state),
            title=self._title(),
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: f"Estado: {self.app.state.state.value}", None, enabled=False),
                pystray.MenuItem(
                    lambda _: f"Documento: {self.app.settings.document.name}", None, enabled=False
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    f"Iniciar captura ({pretty_hotkey(s.hotkey_start)})",
                    self._on_start,
                    enabled=lambda _: not self.app.state.is_active,
                    default=True,
                ),
                pystray.MenuItem(
                    f"Detener captura ({pretty_hotkey(s.hotkey_stop)})",
                    self._on_stop,
                    enabled=lambda _: self.app.state.is_active,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Abrir documento Word", self._open_document),
                pystray.MenuItem(
                    "Elegir documento Word…",
                    self._choose_document,
                    enabled=lambda _: not self.app.state.is_active,
                ),
                pystray.MenuItem("Abrir carpeta del documento", self._open_folder),
                pystray.MenuItem("Editar configuración", self._open_config),
                pystray.MenuItem("Ver registro", self._open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", self._quit),
            ),
        )
        app.state.subscribe(lambda _old, _new: self.refresh())
        app.on_stats(lambda _stats: self.refresh())
        app.set_notifier(self.notify)

    def _title(self) -> str:
        st: SessionStats = self.app.stats
        title = f"{self.app.settings.app_name} — {self.app.state.state.value}"
        if self.app.state.state is not AppState.INACTIVE:
            title += f" ({st.added} nuevas)"
        return title[:127]  # límite de Windows para tooltips

    def refresh(self) -> None:
        try:
            self.icon.icon = make_icon(self.app.state.state)
            self.icon.title = self._title()
            self.icon.update_menu()
        except Exception:
            log.debug("No se pudo refrescar el icono", exc_info=True)

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message[:250], title)
        except Exception:
            log.debug("Notificación no disponible", exc_info=True)

    # --------------------------------------------------------------- acciones
    def _on_start(self, *_):
        threading.Thread(target=self.app.start, daemon=True).start()

    def _on_stop(self, *_):
        threading.Thread(target=self.app.stop, daemon=True).start()

    def _open_document(self, *_):
        path = self.app.settings.document
        if path.exists():
            os.startfile(path)  # noqa: S606
        else:
            self.notify(self.app.settings.app_name, "El documento aún no existe; se creará al iniciar.")

    def _open_folder(self, *_):
        path = self.app.settings.document
        folder = path.parent
        folder.mkdir(parents=True, exist_ok=True)
        if path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(folder)  # noqa: S606

    def _open_config(self, *_):
        subprocess.Popen(["notepad.exe", str(DEFAULT_CONFIG_PATH)])
        self.notify(
            self.app.settings.app_name,
            "Tras guardar config.json, reinicia WordLens para aplicar los cambios.",
        )

    def _open_log(self, *_):
        log_file = LOG_DIR / "wordlens.log"
        if log_file.exists():
            subprocess.Popen(["notepad.exe", str(log_file)])

    def _choose_document(self, *_):
        threading.Thread(target=self._choose_document_dialog, daemon=True).start()

    def _choose_document_dialog(self) -> None:
        import tkinter as tk
        from tkinter import filedialog

        current = self.app.settings.document
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            chosen = filedialog.asksaveasfilename(
                parent=root,
                title="Elige o crea el documento de vocabulario (no se sobrescribe si ya existe)",
                initialdir=str(current.parent if current.parent.exists() else Path.home()),
                initialfile=current.name,
                defaultextension=".docx",
                filetypes=[("Documento de Word", "*.docx")],
                confirmoverwrite=False,
            )
        finally:
            root.destroy()
        if not chosen:
            return
        self.app.settings.document_path = to_project_relative(chosen)
        save_settings(self.app.settings)
        state = "existente (se conservará su contenido)" if Path(chosen).exists() else "nuevo"
        self.notify(self.app.settings.app_name, f"Documento {state}: {Path(chosen).name}")
        self.refresh()

    def _quit(self, *_):
        def _do():
            self.app.shutdown()
            self.icon.stop()

        threading.Thread(target=_do, daemon=True).start()

    def run(self) -> None:
        self.icon.run()
