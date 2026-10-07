"""Controlador de la aplicación: sesiones de captura y su procesamiento.

Hilos de una sesión:
  capture  : captura franjas -> OCR -> detector de subtítulos -> cola
  analyzer : cola -> lotes -> LLM -> validación -> deduplicación -> Word

La captura nunca espera al LLM: si el modelo va más lento que los subtítulos,
la cola (acotada) descarta los más antiguos en lugar de crecer sin límite.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from vocablens.ai.ollama_client import (
    OllamaAborted,
    OllamaClient,
    OllamaError,
    OllamaUnavailableError,
)
from vocablens.ai.vocabulary_analyzer import VocabularyAnalyzer
from vocablens.config.settings import Settings
from vocablens.document.word_writer import DocumentError, DocumentLockedError, WordWriter
from vocablens.state import AppState, StateMachine
from vocablens.vocabulary.deduplicator import Deduplicator
from vocablens.vocabulary.models import SubtitleLine, VocabularyItem

log = logging.getLogger(__name__)

Notifier = Callable[[str, str], None]  # (título, mensaje)


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


class VocabLensApp:
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

    # ------------------------------------------------------------ utilidades
    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def on_stats(self, listener: Callable[[SessionStats], None]) -> None:
        self._stats_listeners.append(listener)

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
                self.notify("La sesión anterior aún se está cerrando; inténtalo en unos segundos.")
                return False
            self._threads = []

            s = self.settings
            writer = WordWriter(s.document, backup_count=s.backup_count)
            try:
                created = writer.ensure_exists()
                if not created:
                    writer.backup()
                existing = writer.read_expressions()
            except DocumentLockedError:
                self.notify(f"No se puede abrir {s.document.name}: está bloqueado por otro programa.")
                return False
            except (DocumentError, OSError) as exc:
                self.notify(f"Error con el documento: {exc}")
                return False
            self.writer = writer
            self.dedup.reset(existing)

            try:
                if not self.client.has_model(s.ollama_model):
                    self.notify(
                        f"El modelo {s.ollama_model} no está descargado. "
                        f"Ejecuta: ollama pull {s.ollama_model}"
                    )
                    return False
            except OllamaError as exc:
                self.notify(f"Ollama no responde. Ábrelo y vuelve a intentarlo. ({exc})")
                return False

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
            for t in self._threads:
                t.start()
            self.notify(
                f"Capturando. {len(existing)} expresiones ya registradas en {s.document.name}."
            )
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
        self.notify(f"Detenido. {self.stats.added} expresiones nuevas en esta sesión.")
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
                    f"Hay {writer.pending_count} expresiones pendientes: cierra el documento en Word.",
                    key="locked",
                    every=60,
                )

    def _dump_pending(self, writer: WordWriter) -> None:
        """Último recurso al salir con Word bloqueando el documento: no perder nada."""
        target = writer.path.with_suffix(".pendientes.txt")
        items = list(writer._pending)  # noqa: SLF001 - acceso interno intencionado
        with target.open("a", encoding="utf-8") as fh:
            for it in items:
                fh.write(f"{it.expression} — {it.translation}\n")
        log.warning("%d entradas pendientes guardadas en %s", len(items), target)

    # ---------------------------------------------------------- hilo captura
    def _capture_loop(self, stop: threading.Event, q: queue.Queue[SubtitleLine]) -> None:
        # Imports aquí: mss/WinRT deben inicializarse en este hilo.
        from vocablens.capture.screen_capture import (
            ChangeDetector,
            ScreenCapturer,
            enable_dpi_awareness,
            prepare_for_ocr,
        )
        from vocablens.capture.subtitle_detector import SubtitleTracker, extract_subtitles
        from vocablens.ocr.ocr_engine import OcrUnavailableError, WindowsOcr

        s = self.settings
        enable_dpi_awareness()
        try:
            ocr = WindowsOcr(s.ocr_language)
            capturer = ScreenCapturer(
                s.monitor, s.top_band_fraction, s.bottom_band_fraction, s.scan_top, s.scan_bottom
            )
        except (OcrUnavailableError, Exception) as exc:  # noqa: BLE001
            log.exception("No se pudo iniciar la captura")
            self.notify(f"No se pudo iniciar la captura/OCR: {exc}")
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
        log.info("Captura iniciada: %s", [(b.name, b.width, b.height) for b in capturer.bands])
        try:
            while not stop.is_set():
                t0 = time.perf_counter()
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
                        self.notify("Demasiados errores de captura/OCR; sesión detenida.")
                        threading.Thread(target=self.stop, daemon=True).start()
                        return
                elapsed = time.perf_counter() - t0
                stop.wait(max(0.05, s.capture_interval_seconds - elapsed))
        finally:
            capturer.close()
            ocr.close()
            log.info("Captura finalizada (%d capturas, %d OCR)", self.stats.frames, self.stats.ocr_runs)

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
                self.notify(f"Ollama no responde: {exc}", key="ollama", every=120)
                stop.wait(5)
            except OllamaError as exc:
                log.error("Error del LLM: %s", exc)
                self.notify(f"Error del modelo: {exc}", key="llm", every=120)
            except Exception:
                log.exception("Error inesperado analizando %s", batch)
            finally:
                self.state.transition(AppState.CAPTURING, only_from={AppState.PROCESSING})
                self._emit_stats()

    def _write(self, items: list[VocabularyItem]) -> None:
        if self.writer is None:
            return
        try:
            written = self.writer.add_entries(items)
        except DocumentLockedError:
            self.notify(
                "No puedo guardar: cierra el documento en Word (las expresiones quedan en espera).",
                key="locked",
                every=60,
            )
            return
        except DocumentError as exc:
            self.notify(f"Error escribiendo el documento: {exc}", key="docerr", every=60)
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
