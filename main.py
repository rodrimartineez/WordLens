"""VocabLens — punto de entrada.

Uso habitual (sin consola):   start_vocablens.bat
Con consola para ver qué pasa: .venv\\Scripts\\python.exe main.py
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from vocablens.config.settings import DEFAULT_CONFIG_PATH, LOG_DIR, load_settings


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_DIR / "vocablens.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stdout is not None:  # con pythonw.exe no hay consola
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


def acquire_single_instance() -> object | None:
    """Evita dos instancias (competirían por los atajos y el documento)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, "Local\\VocabLens_single_instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VocabLens: vocabulario B2-C1 desde subtítulos")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Ruta de config.json")
    parser.add_argument("--document", help="Documento Word de destino (sustituye al de la configuración)")
    parser.add_argument("--start", action="store_true", help="Empezar a capturar inmediatamente")
    parser.add_argument("--no-tray", action="store_true", help="Sin icono de bandeja (Ctrl+C para salir)")
    parser.add_argument("--duration", type=float, help="Detener y salir tras N segundos (pruebas)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    log = logging.getLogger("vocablens")

    mutex = acquire_single_instance()
    if mutex is None:
        log.error("VocabLens ya se está ejecutando (mira el icono junto al reloj).")
        ctypes.windll.user32.MessageBoxW(
            None, "VocabLens ya se está ejecutando.\nBusca su icono en la bandeja del sistema.",
            "VocabLens", 0x40,
        )
        return 1

    settings = load_settings(args.config)
    if args.document:
        settings.document_path = args.document

    from vocablens.app import VocabLensApp
    from vocablens.input.hotkeys import HotkeyManager

    app = VocabLensApp(settings)
    log.info("VocabLens iniciado. Modelo: %s | Documento: %s", settings.ollama_model, settings.document)

    hotkeys = HotkeyManager()
    hotkeys.add(settings.hotkey_start, lambda: threading.Thread(target=app.start, daemon=True).start())
    hotkeys.add(settings.hotkey_stop, lambda: threading.Thread(target=app.stop, daemon=True).start())
    failed = hotkeys.start()

    def warn_failed_hotkeys() -> None:
        for spec, why in failed.items():
            app.notify(f"No se pudo registrar el atajo {spec} ({why}). Cámbialo en config.json.")

    done = threading.Event()
    tray = None

    if args.duration:
        def _timer() -> None:
            time.sleep(args.duration)
            log.info("Duración alcanzada; cerrando")
            app.shutdown()
            if tray is not None:
                tray.icon.stop()
            else:
                done.set()

        threading.Thread(target=_timer, daemon=True).start()
    try:
        if args.no_tray:
            warn_failed_hotkeys()
            log.info(
                "Modo sin bandeja. %s para iniciar, %s para detener, Ctrl+C para salir.",
                settings.hotkey_start, settings.hotkey_stop,
            )
            if args.start:
                app.start()
            signal.signal(signal.SIGINT, lambda *_: done.set())
            while not done.wait(0.5):
                pass
            app.shutdown()
        else:
            from vocablens.ui.tray import TrayUI

            tray = TrayUI(app)

            def _after_tray_ready() -> None:
                time.sleep(1.0)
                warn_failed_hotkeys()
                if args.start:
                    app.start()
                elif not failed:
                    app.notify(
                        f"Listo. Pulsa {settings.hotkey_start} para empezar a capturar "
                        f"y {settings.hotkey_stop} para detener."
                    )

            threading.Thread(target=_after_tray_ready, daemon=True).start()
            tray.run()
    finally:
        hotkeys.stop()
        app.shutdown()
        log.info("VocabLens cerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
