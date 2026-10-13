"""Controlador de la aplicación: sesiones de captura y su procesamiento.

Hilos de una sesión:
  capture  : captura franjas -> OCR -> detector de subtítulos -> cola
  analyzer : cola -> lotes -> LLM -> validación -> deduplicación -> Word

La captura nunca espera al LLM: si el modelo va más lento que los subtítulos,
la cola (acotada) descarta los más antiguos en lugar de crecer sin límite.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from wordlens.ai import ollama_setup
from wordlens.ai.ollama_client import (
    OllamaAborted,
    OllamaClient,
    OllamaError,
    OllamaUnavailableError,
)
from wordlens.ai.ollama_setup import OllamaState
from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer
from wordlens.capture import monitors
from wordlens.config.paths import BACKUP_DIR, PENDING_DIR
from wordlens.config.settings import Settings
from wordlens.document.word_writer import DocumentError, DocumentLockedError, WordWriter
from wordlens.state import AppState, StateMachine
from wordlens.vocabulary.deduplicator import Deduplicator
from wordlens.vocabulary.models import SubtitleLine, VocabularyItem

log = logging.getLogger(__name__)

Notifier = Callable[[str, str], None]  # (título, mensaje)


@dataclass(frozen=True)
class AppError:
    """Último error relevante para el usuario (la interfaz lo muestra con su acción)."""

    kind: str  # "document" | "ollama_missing" | "ollama_down" | "model_missing" | "capture"
    message: str


@dataclass
class SessionStats:
    started_at: float = 0.0
    frames: int = 0
    ocr_runs: int = 0
    subtitles: int = 0
    dropped: int = 0
    llm_calls: int = 0
    added: int = 0
    last_added: list[str] = field(default_factory=list)


class WordLensApp:
    def __init__(self, settings: Settings, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.state = StateMachine()
        self.dedup = Deduplicator()
        self.client = OllamaClient(settings.ollama_url, settings.ollama_timeout_seconds)
        self.analyzer = VocabularyAnalyzer(settings, self.client)
        self.writer: WordWriter | None = None
        self.stats = SessionStats()
        self._notifier = notifier
        self._session_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._queue: queue.Queue[SubtitleLine] = queue.Queue(maxsize=settings.max_queue_lines)
        self._last_notice: dict[str, float] = {}
        self._stats_listeners: list[Callable[[SessionStats], None]] = []
        self._error_listeners: list[Callable[[AppError | None], None]] = []
        self.last_error: AppError | None = None
        self._last_relaunch = 0.0
        # Pantalla que se está capturando (la elegida o, si no está conectada, la principal).
        self.capture_selection: monitors.Selection | None = None

    # ------------------------------------------------------------ utilidades
    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def on_stats(self, listener: Callable[[SessionStats], None]) -> None:
        self._stats_listeners.append(listener)

    def on_error(self, listener: Callable[[AppError | None], None]) -> None:
        self._error_listeners.append(listener)

    def _set_error(self, kind: str | None, message: str = "") -> None:
        self.last_error = AppError(kind, message) if kind else None
        for listener in list(self._error_listeners):
            try:
                listener(self.last_error)
            except Exception:
                log.exception("Error en listener de errores")

    def clear_error(self) -> None:
        if self.last_error is not None:
            self._set_error(None)

    def _fail(self, kind: str, message: str) -> bool:
        self._set_error(kind, message)
        self.notify(message)
        return False

    def _emit_stats(self) -> None:
        for listener in list(self._stats_listeners):
            try:
                listener(self.stats)
            except Exception:
                log.exception("Error en listener de estadísticas")

    def notify(self, message: str, *, key: str | None = None, every: float = 0.0) -> None:
        """Notificación al usuario, con limitación opcional de frecuencia por clave."""
        log.info("Aviso: %s", message)
        if key is not None:
            now = time.monotonic()
            if now - self._last_notice.get(key, -1e9) < every:
                return
            self._last_notice[key] = now
        if self._notifier and self.settings.notifications:
            try:
                self._notifier(self.settings.app_name, message)
            except Exception:
                log.exception("No se pudo mostrar la notificación")

    # --------------------------------------------------------------- sesión
    def start(self) -> bool:
        """Inicia una sesión de captura. No inicia una segunda si ya hay una activa."""
        with self._session_lock:
            if self.state.is_active:
                log.info("Ya hay una sesión activa; se ignora el inicio")
                return False
            for t in self._threads:
                t.join(timeout=5)
            if any(t.is_alive() for t in self._threads):
                self.notify("The previous session is still closing; try again in a few seconds.")
                return False
            self._threads = []

            s = self.settings
            writer = WordWriter(
                s.document, backup_count=s.backup_count, backup_dir=BACKUP_DIR, lang=s.source_code
            )
            try:
                created = writer.ensure_exists()
                if not created:
                    writer.backup()
                existing = writer.read_expressions()
            except DocumentLockedError:
                return self._fail(
                    "document",
                    f"Can't open {s.document.name}: it is locked by another program (close it in Word).",
                )
            except (DocumentError, OSError) as exc:
                return self._fail("document", f"Problem with the vocabulary document: {exc}")
            self.writer = writer
            self.dedup.reset(existing, lang=s.source_code)
            self._recover_pending(writer)

            status = ollama_setup.check(self.client, s.ollama_model)
            if not status.ok:
                kind = {
                    OllamaState.NOT_INSTALLED: "ollama_missing",
                    OllamaState.NOT_RUNNING: "ollama_down",
                    OllamaState.MODEL_MISSING: "model_missing",
                }[status.state]
                return self._fail(kind, status.message)

            self._stop_event = threading.Event()
            self._queue = queue.Queue(maxsize=s.max_queue_lines)
            self.stats = SessionStats(started_at=time.time())
            if not self.state.transition(AppState.CAPTURING):
                return False
            stop, q = self._stop_event, self._queue
            self._threads = [
                threading.Thread(target=self._capture_loop, args=(stop, q), name="capture", daemon=True),
                threading.Thread(target=self._analyzer_loop, args=(stop, q), name="analyzer", daemon=True),
            ]
            self._set_error(None)
            for t in self._threads:
                t.start()
            self.notify(f"Capturing. {len(existing)} expressions already in {s.document.name}.")
            self._emit_stats()
            return True

    def stop(self) -> bool:
        with self._session_lock:
            if not self.state.is_active:
                return False
            self._stop_event.set()
            self.client.abort()
            self.state.transition(AppState.STOPPED)
        # Fuera del lock: esperar a los hilos sin bloquear un posible start().
        for t in self._threads:
            t.join(timeout=5)
        self._flush_pending()
        self.notify(f"Stopped. {self.stats.added} new expressions this session.")
        self._emit_stats()
        return True

    def shutdown(self) -> None:
        self.stop()
        self._flush_pending(final=True)

    def _flush_pending(self, final: bool = False) -> None:
        writer = self.writer
        if writer is None or writer.pending_count == 0:
            return
        try:
            written = writer.add_entries([])
            self._record_written(written)
        except DocumentError:
            if final:
                self._dump_pending(writer)
            else:
                self.notify(
                    f"{writer.pending_count} expressions are waiting: close the document in Word.",
                    key="locked",
                    every=60,
                )

    def _dump_pending(self, writer: WordWriter) -> None:
        """Último recurso al salir con Word bloqueando el documento: no perder nada.

        Se guardan en la carpeta de datos de WordLens (no junto al documento) y se
        recuperan automáticamente en la siguiente sesión con ese documento.
        """
        items = writer.take_pending()
        if not items:
            return
        target = pending_file_for(writer.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            for it in items:
                fh.write(f"{it.expression} — {it.translation}\n")
        log.warning("%d entradas pendientes guardadas en %s", len(items), target)

    def _recover_pending(self, writer: WordWriter) -> None:
        """Vuelve a encolar lo que quedó pendiente al salir la última vez."""
        source = pending_file_for(writer.path)
        if not source.exists():
            return
        items = []
        for line in source.read_text(encoding="utf-8").splitlines():
            expr, sep, trans = line.partition(" — ")
            if sep and expr.strip() and trans.strip():
                items.append(VocabularyItem(expr.strip(), trans.strip(), ""))
        try:
            written = writer.add_entries(items)
        except DocumentError:
            log.warning("No se pudieron recuperar todavía las entradas pendientes de %s", source)
            return
        source.unlink(missing_ok=True)
        self._record_written(written)
        log.info("Recuperadas %d entradas pendientes de %s", len(written), source)

    # ---------------------------------------------------------- hilo captura
    def _capture_loop(self, stop: threading.Event, q: queue.Queue[SubtitleLine]) -> None:
        # Imports aquí: mss/WinRT deben inicializarse en este hilo.
        from wordlens.capture.screen_capture import (
            ChangeDetector,
            ScreenCapturer,
            enable_dpi_awareness,
            prepare_for_ocr,
        )
        from wordlens.capture.subtitle_detector import SubtitleTracker, extract_subtitles
        from wordlens.ocr.ocr_engine import OcrUnavailableError, WindowsOcr

        s = self.settings
        enable_dpi_awareness()
        try:
            ocr = WindowsOcr(s.ocr_tag)
            selection = self.resolve_capture_monitor()
            capturer = ScreenCapturer(
                selection.monitor.rect if selection.monitor else None,
                s.top_band_fraction,
                s.bottom_band_fraction,
                s.scan_top,
                s.scan_bottom,
            )
        except (OcrUnavailableError, Exception) as exc:  # noqa: BLE001
            log.exception("No se pudo iniciar la captura")
            self._fail("capture", f"Could not start screen capture/OCR: {exc}")
            threading.Thread(target=self.stop, daemon=True).start()
            return

        tracker = SubtitleTracker(
            stability_frames=s.stability_frames,
            repeat_window_seconds=s.repeat_window_seconds,
            similarity_threshold=s.similarity_threshold,
        )
        changes = ChangeDetector()
        last_seen: dict[str, list[str]] = {}
        errors = 0
        log.info(
            "Captura iniciada en %s: %s",
            selection.monitor.label if selection.monitor else "pantalla principal",
            [(b.name, b.left, b.top, b.width, b.height) for b in capturer.bands],
        )
        next_monitor_check = time.monotonic() + 1.0
        try:
            while not stop.is_set():
                t0 = time.perf_counter()
                if time.monotonic() >= next_monitor_check:
                    # La pantalla elegida puede cambiar en plena sesión (selección
                    # del usuario, monitor desconectado o reconectado, resolución).
                    next_monitor_check = time.monotonic() + 1.0
                    selection = self.resolve_capture_monitor()
                    if selection.monitor and selection.monitor.rect != capturer.monitor:
                        capturer.set_monitor(selection.monitor.rect)
                        changes.reset()
                        last_seen.clear()
                        log.info(
                            "Captura movida a %s: %s",
                            selection.monitor.label,
                            [(b.name, b.left, b.top, b.width, b.height) for b in capturer.bands],
                        )
                try:
                    visible: list[str] = []
                    for shot in capturer.grab():
                        image, _ = prepare_for_ocr(shot.image, s.ocr_preprocess)
                        name = shot.band.name
                        if changes.changed(name, image) or name not in last_seen:
                            lines = ocr.recognize(image)
                            self.stats.ocr_runs += 1
                            last_seen[name] = extract_subtitles(
                                lines,
                                image.width,
                                min_words=s.min_words_per_line,
                                min_english_ratio=s.min_english_ratio,
                                require_centered=s.require_centered,
                                language=s.source_code,
                            )
                        visible.extend(last_seen[name])
                        del image, shot  # nada de imágenes acumuladas en memoria
                    self.stats.frames += 1
                    for text in tracker.update(visible):
                        self._enqueue(q, SubtitleLine(text, "screen", time.time()))
                    errors = 0
                except Exception:
                    errors += 1
                    log.exception("Error en captura/OCR (%d seguidos)", errors)
                    if errors >= 20:
                        self._fail("capture", "Too many screen capture/OCR errors; session stopped.")
                        threading.Thread(target=self.stop, daemon=True).start()
                        return
                elapsed = time.perf_counter() - t0
                stop.wait(max(0.05, s.capture_interval_seconds - elapsed))
        finally:
            capturer.close()
            ocr.close()
            log.info("Captura finalizada (%d capturas, %d OCR)", self.stats.frames, self.stats.ocr_runs)

    def resolve_capture_monitor(self) -> monitors.Selection:
        """Pantalla a capturar ahora mismo. Avisa al entrar o salir del modo de reserva."""
        s = self.settings
        selection = monitors.resolve(monitors.list_monitors(), s.capture_monitor_id, s.capture_monitor)
        previous = self.capture_selection
        self.capture_selection = selection
        if previous is not None and previous.missing != selection.missing and self.state.is_active:
            if selection.missing:
                log.warning("La pantalla elegida (%s) no está conectada", s.capture_monitor_id)
                self.notify(
                    f"Screen {s.capture_monitor} is not connected. Capturing on the main screen until it is back.",
                    key="monitor",
                )
            else:
                self.notify(f"Screen {s.capture_monitor} is back. Capturing on it again.", key="monitor")
            self._emit_stats()
        return selection

    def _enqueue(self, q: queue.Queue[SubtitleLine], line: SubtitleLine) -> None:
        log.info("Subtítulo nuevo: %s", line.text)
        self.stats.subtitles += 1
        while True:
            try:
                q.put_nowait(line)
                return
            except queue.Full:
                try:
                    q.get_nowait()
                    self.stats.dropped += 1
                    log.warning("Cola llena: el LLM va por detrás; se descarta la línea más antigua")
                except queue.Empty:
                    pass

    # ---------------------------------------------------------- hilo análisis
    def submit_text(self, text: str) -> None:
        """Permite inyectar texto en la sesión activa (pruebas / depuración)."""
        self._enqueue(self._queue, SubtitleLine(text, "manual", time.time()))

    def _analyzer_loop(self, stop: threading.Event, q: queue.Queue[SubtitleLine]) -> None:
        s = self.settings
        try:
            self.client.warm_up(s.ollama_model, s.ollama_keep_alive)
            log.info("Modelo %s cargado", s.ollama_model)
        except OllamaAborted:
            return
        except OllamaError as exc:
            log.warning("No se pudo precargar el modelo: %s", exc)

        last_retry = time.monotonic()
        while not stop.is_set():
            try:
                first = q.get(timeout=0.5)
            except queue.Empty:
                if self.writer and self.writer.pending_count and time.monotonic() - last_retry > 10:
                    last_retry = time.monotonic()
                    self._write([])
                continue
            batch = [first.text]
            deadline = time.monotonic() + s.batch_wait_seconds
            while len(batch) < s.batch_max_lines and not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(q.get(timeout=remaining).text)
                except queue.Empty:
                    break
            if stop.is_set():
                break
            if not self.state.transition(AppState.PROCESSING, only_from={AppState.CAPTURING}):
                break
            try:
                self.stats.llm_calls += 1
                items = self.analyzer.analyze(batch)
                if not stop.is_set() or items:
                    self._write(self.dedup.filter_new(items))
            except OllamaAborted:
                break
            except OllamaUnavailableError as exc:
                log.warning("Ollama no responde: %s", exc)
                self.notify("Ollama is not responding. Trying to reconnect…", key="ollama", every=120)
                self._try_relaunch_ollama(stop)
            except OllamaError as exc:
                log.error("Error del LLM: %s", exc)
                self.notify(f"AI model error: {exc}", key="llm", every=120)
            except Exception:
                log.exception("Error inesperado analizando %s", batch)
            finally:
                self.state.transition(AppState.CAPTURING, only_from={AppState.PROCESSING})
                self._emit_stats()

    def _try_relaunch_ollama(self, stop: threading.Event) -> None:
        """Ollama se ha cerrado durante la sesión: intenta arrancarlo (como mucho cada minuto)."""
        if time.monotonic() - self._last_relaunch > 60 and ollama_setup.launch_ollama():
            self._last_relaunch = time.monotonic()
            deadline = time.monotonic() + 30
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    self.client.version()
                    log.info("Ollama vuelve a responder")
                    return
                except OllamaError:
                    stop.wait(1)
        stop.wait(5)

    def _write(self, items: list[VocabularyItem]) -> None:
        if self.writer is None:
            return
        try:
            written = self.writer.add_entries(items)
        except DocumentLockedError:
            self.notify(
                "Can't save: close the document in Word (new expressions are kept until then).",
                key="locked",
                every=60,
            )
            return
        except DocumentError as exc:
            self.notify(f"Error writing the document: {exc}", key="docerr", every=60)
            return
        self._record_written(written)

    def _record_written(self, written: list[VocabularyItem]) -> None:
        for item in written:
            self.dedup.add(item.expression)
            log.info("AÑADIDO: %s — %s   [%s]", item.expression, item.translation, item.context)
        if written:
            self.stats.added += len(written)
            self.stats.last_added = ([w.expression for w in written] + self.stats.last_added)[:5]
            self._emit_stats()

    @property
    def document_path(self) -> Path:
        return self.settings.document


def pending_file_for(document: Path) -> Path:
    """Archivo de pendientes de un documento, dentro de la carpeta de datos."""
    digest = hashlib.sha1(str(document.resolve()).lower().encode("utf-8")).hexdigest()[:8]
    return PENDING_DIR / f"{document.stem}_{digest}.pending.txt"
