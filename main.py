"""WordLens — punto de entrada.

Producción: WordLens.exe (aplicación instalada, ver README > Build).
Desarrollo:  .venv\\Scripts\\python.exe main.py          (ventana + bandeja, log en consola)
             .venv\\Scripts\\python.exe main.py --no-gui  (sólo atajos, Ctrl+C para salir)
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from wordlens import __version__
from wordlens.config.paths import DATA_DIR, IS_FROZEN, LOG_DIR
from wordlens.config.settings import DEFAULT_CONFIG_PATH, load_settings

APP_USER_MODEL_ID = "WordLens.WordLens"


def _instance_suffix() -> str:
    """Una carpeta de datos = una instancia. Con WORDLENS_DATA_DIR (pruebas aisladas)
    se usa otro nombre: no compite con la app instalada ni comparte sus datos."""
    import hashlib
    import os

    override = os.environ.get("WORDLENS_DATA_DIR")
    return "_" + hashlib.sha1(override.lower().encode("utf-8")).hexdigest()[:8] if override else ""


# Sin sufijo coincide con AppMutex del instalador (packaging/installer.iss).
MUTEX_NAME = "Local\\WordLens_single_instance" + _instance_suffix()
SHOW_EVENT_NAME = "Local\\WordLens_show_window" + _instance_suffix()

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.OpenEventW.restype = ctypes.c_void_p
kernel32.CreateMutexW.restype = ctypes.c_void_p


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_DIR / "wordlens.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stdout is not None:  # la app instalada (sin consola) no tiene stdout
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def acquire_single_instance() -> int | None:
    """Evita dos instancias (competirían por los atajos y el documento)."""
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def signal_running_instance() -> bool:
    """Pide a la instancia que ya está abierta que muestre su ventana."""
    EVENT_MODIFY_STATE = 0x0002
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
    if not handle:
        return False
    kernel32.SetEvent(ctypes.c_void_p(handle))
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


def listen_for_show_requests(callback) -> None:
    """Hilo que espera a que otra instancia pida mostrar la ventana."""
    event = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)

    def loop() -> None:
        while True:
            if kernel32.WaitForSingleObject(ctypes.c_void_p(event), 0xFFFFFFFF) == 0:
                callback()

    threading.Thread(target=loop, name="show-requests", daemon=True).start()


def prepare_process() -> None:
    from wordlens.capture.screen_capture import enable_dpi_awareness

    # Antes de crear ventanas: nitidez en pantallas con escalado y captura a
    # resolución real.
    enable_dpi_awareness()
    try:
        # Icono y agrupación correctos en la barra de tareas (coincide con el acceso directo).
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


class Hotkeys:
    """Atajos globales re-registrables (al cambiarlos en Ajustes)."""

    def __init__(self, settings, on_start, on_stop) -> None:
        self.settings = settings
        self._on_start, self._on_stop = on_start, on_stop
        self.manager = None

    def register(self) -> dict[str, str]:
        from wordlens.input.hotkeys import HotkeyManager

        self.unregister()
        manager = HotkeyManager()
        manager.add(self.settings.hotkey_start, self._on_start)
        manager.add(self.settings.hotkey_stop, self._on_stop)
        failed = manager.start()
        self.manager = manager
        return failed

    def unregister(self) -> None:
        if self.manager is not None:
            self.manager.stop()
            self.manager = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WordLens: vocabulary from on-screen subtitles")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Ruta de config.json")
    parser.add_argument("--document", help="Documento Word de destino (sustituye al de la configuración)")
    parser.add_argument("--start", action="store_true", help="Empezar a capturar inmediatamente")
    parser.add_argument("--hidden", action="store_true", help="Arrancar sólo en la bandeja")
    parser.add_argument("--no-gui", "--no-tray", dest="no_gui", action="store_true",
                        help="Sin ventana ni bandeja: sólo atajos (Ctrl+C para salir)")
    parser.add_argument("--duration", type=float, help="Detener y salir tras N segundos (pruebas)")
    parser.add_argument("--self-test", action="store_true", help="Comprobar OCR, Word, captura y Ollama y salir")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    log = logging.getLogger("wordlens")
    if args.self_test:
        from wordlens import selftest

        prepare_process()
        return selftest.run()

    mutex = acquire_single_instance()
    if mutex is None:
        if signal_running_instance():
            log.info("WordLens ya estaba abierto: se muestra su ventana")
        else:
            ctypes.windll.user32.MessageBoxW(None, "WordLens is already running.", "WordLens", 0x40)
        return 0

    prepare_process()
    settings = load_settings(args.config)
    if args.document:
        settings.document_path = str(Path(args.document).resolve())
        settings.setup_complete = True

    from wordlens import migration
    from wordlens.app import WordLensApp

    app = WordLensApp(settings)
    migration.run(app.store, settings)
    log.info(
        "WordLens %s iniciado (%s). Datos: %s | Modelo: %s | Documento: %s",
        __version__, "instalado" if IS_FROZEN else "desarrollo", DATA_DIR,
        settings.ollama_model, settings.document if settings.setup_complete else "(sin configurar)",
    )

    if args.no_gui:
        return run_headless(app, args)

    from wordlens.ui.tray import TrayUI
    from wordlens.ui.window import MainWindow

    shutting_down = threading.Event()
    window: MainWindow | None = None
    tray: TrayUI | None = None

    def exit_app() -> None:
        if shutting_down.is_set():
            return
        shutting_down.set()
        log.info("Saliendo…")
        window.root.withdraw()
        hotkeys.unregister()
        tray.stop()
        app.shutdown()
        window.quit()

    hotkeys = Hotkeys(
        settings,
        on_start=lambda: window.call_soon(window.start_capture),
        on_stop=lambda: window.call_soon(window.stop_capture),
    )

    def hotkeys_changed() -> dict[str, str]:
        failed = hotkeys.register()
        warn_failed(failed)
        return failed

    def warn_failed(failed: dict[str, str]) -> None:
        for spec, why in failed.items():
            app.notify(f"Couldn't register the shortcut {spec} ({why}). Change it in Settings.")

    window = MainWindow(app, on_hotkeys_changed=hotkeys_changed, on_exit=exit_app)
    tray = TrayUI(window)
    window.tray = tray
    app.set_notifier(tray.notify)
    tray.start()
    listen_for_show_requests(lambda: window.call_soon(window.show))
    failed = hotkeys.register()
    app.boot()  # después de la ventana: la revisión pendiente ya tiene quien la abra

    def after_ready() -> None:
        warn_failed(failed)
        if args.start:
            window.start_capture()

    window.root.after(800, after_ready)
    if args.duration:
        window.root.after(int(args.duration * 1000), exit_app)
    try:
        window.run(hidden=args.hidden)
    finally:
        if not shutting_down.is_set():
            hotkeys.unregister()
            tray.stop()
            app.shutdown()
        log.info("WordLens cerrado")
    return 0


def run_headless(app, args) -> int:
    """Modo de desarrollo sin interfaz: atajos + consola."""
    from wordlens.input.hotkeys import HotkeyManager

    log = logging.getLogger("wordlens")
    s = app.settings
    hotkeys = HotkeyManager()
    hotkeys.add(s.hotkey_start, lambda: threading.Thread(target=app.start, daemon=True).start())
    hotkeys.add(s.hotkey_stop, lambda: threading.Thread(target=app.stop, daemon=True).start())
    for spec, why in hotkeys.start().items():
        log.error("No se pudo registrar el atajo %s (%s)", spec, why)
    done = threading.Event()
    if args.duration:
        threading.Timer(args.duration, done.set).start()
    app.boot()
    log.info("Modo sin interfaz. %s para iniciar, %s para detener, Ctrl+C para salir.", s.hotkey_start, s.hotkey_stop)
    if args.start:
        app.start()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    try:
        while not done.wait(0.5):
            pass
    finally:
        hotkeys.stop()
        app.shutdown()
        log.info("WordLens cerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
