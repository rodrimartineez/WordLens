"""Controlador de la aplicación: sesiones de captura, análisis, revisión y guardado.

Hilos:
  capture   : captura franjas -> OCR -> detector de subtítulos -> vocabulary.db (lines)
  analyzer  : lines pendientes -> modelo de extracción -> candidatos consolidados
  finisher  : al detener: termina las líneas pendientes, traduce y avisa a la
              interfaz para que abra la ventana de revisión
  saver     : escribe en Word las entradas aceptadas (✓) y confirma cada una

Todo lo que importa vive en vocabulary.db (ver vocabulary/store.py): ni un
subtítulo, ni un candidato, ni una aceptación se quedan sólo en memoria. Si la
app se cierra a mitad, lo pendiente se retoma la próxima vez.

La captura nunca espera al modelo: los subtítulos se guardan y el analizador
los va sacando a su ritmo, sin descartar ninguno.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
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
from wordlens.config.paths import BACKUP_DIR, DATA_DIR
from wordlens.config.settings import Settings
from wordlens.document.word_writer import DocumentError, DocumentLockedError, WordWriter, is_lock_error
from wordlens.state import AppState, StateMachine
from wordlens.translation.translator import OllamaTranslator, TranslationItem, Translator
from wordlens.vocabulary.models import SubtitleLine, VocabularyItem
from wordlens.vocabulary.normalization import entry_key
from wordlens.vocabulary.store import StoreError, VocabularyStore

log = logging.getLogger(__name__)

Notifier = Callable[[str, str], None]  # (título, mensaje)

DB_NAME = "vocabulary.db"
TRANSLATE_BATCH = 8  # ítems por petición (todos de la misma línea)


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
    dropped: int = 0  # se mantiene por compatibilidad: ya no se descarta nada
    llm_calls: int = 0
    added: int = 0  # candidatos nuevos para revisar en esta sesión
    last_added: list[str] = field(default_factory=list)
    phase: str = ""  # texto de progreso mientras se termina (FINISHING)


class WordLensApp:
    def __init__(
        self,
        settings: Settings,
        notifier: Notifier | None = None,
        *,
        store: VocabularyStore | None = None,
        translator: Translator | None = None,
    ) -> None:
        self.settings = settings
        self.state = StateMachine()
        self.client = OllamaClient(settings.ollama_url, settings.ollama_timeout_seconds)
        self.translate_client = OllamaClient(settings.ollama_url, settings.ollama_timeout_seconds)
        self.analyzer = VocabularyAnalyzer(settings, self.client)
        self.store = store or VocabularyStore(DATA_DIR / DB_NAME, backup_dir=BACKUP_DIR / "vocabulary").open()
        self._translator = translator
        self.stats = SessionStats()
        self._notifier = notifier
        self._session_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()  # hay subtítulos nuevos para el analizador
        self._last_notice: dict[str, float] = {}
        self._stats_listeners: list[Callable[[SessionStats], None]] = []
        self._error_listeners: list[Callable[[AppError | None], None]] = []
        self._review_listeners: list[Callable[[int], None]] = []
        self.last_error: AppError | None = None
        self._last_relaunch = 0.0
        self.session_id: int | None = None
        self.document_keys: set[str] = set()
        # Pantalla que se está capturando (la elegida o, si no está conectada, la principal).
        self.capture_selection: monitors.Selection | None = None
        self._finisher: threading.Thread | None = None
        self._finish_cancel = threading.Event()
        self.saver = SaveWorker(self)

    # ------------------------------------------------------------ utilidades
    @property
    def lang(self) -> str:
        return self.settings.source_code

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def on_stats(self, listener: Callable[[SessionStats], None]) -> None:
        self._stats_listeners.append(listener)

    def on_error(self, listener: Callable[[AppError | None], None]) -> None:
        self._error_listeners.append(listener)

    def on_review_ready(self, listener: Callable[[int], None]) -> None:
        """listener(n) cuando termina una sesión y hay n entradas para revisar."""
        self._review_listeners.append(listener)

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

    def counts(self) -> dict[str, int]:
        return self.store.counts(self.lang)

    @property
    def finishing(self) -> bool:
        return self._finisher is not None and self._finisher.is_alive()

    # ------------------------------------------------------------- arranque
    def boot(self) -> None:
        """Al abrir la app: copia de la base de datos, guardar lo aceptado sin guardar
        y terminar el trabajo que quedó a medias (subtítulos sin analizar, traducciones).
        """
        try:
            self.store.backup()
        except Exception:
            log.exception("No se pudo copiar vocabulary.db")
        if self.store.recovered:
            self.notify(f"The vocabulary database was damaged and has been {self.store.recovered}.")
        self.saver.start()
        threading.Thread(target=self._resume_pending_work, name="resume", daemon=True).start()

    def _resume_pending_work(self) -> None:
        try:
            engine = self.translator().engine_id
            c = self.counts()
            if c["lines"] or self.store.count_untranslated(self.lang, engine):
                log.info("Trabajo pendiente al arrancar: %s", c)
                with self._session_lock:
                    if not self.state.is_active:
                        self._start_finisher(open_review=False)
        except Exception:
            log.exception("No se pudo retomar el trabajo pendiente")

    def _document_keys(self, *, quiet: bool = False) -> set[str]:
        """Claves de las entradas que ya están en el documento (no se proponen)."""
        s = self.settings
        try:
            existing = WordWriter(s.document, lang=s.source_code).read_expressions() if s.document.exists() else []
        except (DocumentError, OSError):
            if not quiet:
                raise
            return set()
        return {k for k in (entry_key(e, s.source_code) for e in existing) if k}

    # --------------------------------------------------------------- sesión
    def start(self) -> bool:
        """Inicia una sesión de captura. No inicia una segunda si ya hay una activa."""
        with self._session_lock:
            if self.state.is_active:
                log.info("Ya hay una sesión activa; se ignora el inicio")
                return False
            self._cancel_finisher()
            for t in self._threads:
                t.join(timeout=5)
            if any(t.is_alive() for t in self._threads):
                self.notify("The previous session is still closing; try again in a few seconds.")
                return False
            self._threads = []

            s = self.settings
            writer = WordWriter(s.document, backup_count=s.backup_count, backup_dir=BACKUP_DIR, lang=s.source_code)
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
            self.document_keys = {k for k in (entry_key(e, s.source_code) for e in existing) if k}

            status = ollama_setup.check(self.client, s.ollama_model)
            if not status.ok:
                kind = {
                    OllamaState.NOT_INSTALLED: "ollama_missing",
                    OllamaState.NOT_RUNNING: "ollama_down",
                    OllamaState.MODEL_MISSING: "model_missing",
                }[status.state]
                return self._fail(kind, status.message)

            self._stop_event = threading.Event()
            self.stats = SessionStats(started_at=time.time())
            self.analyzer.known_names = set()
            self.session_id = self.store.start_session(str(s.document))
            if not self.state.transition(AppState.CAPTURING):
                return False
            stop = self._stop_event
            self._threads = [
                threading.Thread(target=self._capture_loop, args=(stop,), name="capture", daemon=True),
                threading.Thread(target=self._analyzer_loop, args=(stop,), name="analyzer", daemon=True),
            ]
            self._set_error(None)
            for t in self._threads:
                t.start()
            self.notify(f"Capturing. New vocabulary will be ready to review when you stop.")
            self._emit_stats()
            return True

    def stop(self) -> bool:
        """Detiene la captura. Los subtítulos pendientes se terminan en segundo plano
        y, al acabar, se abre la revisión (on_review_ready)."""
        with self._session_lock:
            if not self.state.is_active:
                return False
            self._stop_event.set()
            self.client.abort()  # la llamada en curso se repite al terminar: la línea sigue pendiente
            self.state.transition(AppState.FINISHING)
        # Fuera del lock: esperar a los hilos sin bloquear un posible start().
        for t in self._threads:
            t.join(timeout=5)
        if self.session_id is not None:
            self.store.end_session(self.session_id)
        self._start_finisher(open_review=True)
        return True

    def shutdown(self) -> None:
        """Salir de la app: nada se pierde (lo pendiente está en vocabulary.db)."""
        self.stop()
        self._cancel_finisher()
        self.saver.stop()
        if self.state.state is AppState.FINISHING:
            self.state.transition(AppState.STOPPED)
        self.store.close()

    # ------------------------------------------------------- terminar sesión
    def _start_finisher(self, *, open_review: bool) -> None:
        if self.finishing:
            return
        self._finish_cancel = threading.Event()
        # Al arrancar la app (sin sesión) se vuelve a "Ready" al terminar; tras una sesión, a "Stopped".
        back_to = AppState.INACTIVE if self.state.state is AppState.INACTIVE else AppState.STOPPED
        if self.state.state is not AppState.FINISHING:
            self.state.transition(AppState.FINISHING)
        self._finisher = threading.Thread(
            target=self._finish, args=(self._finish_cancel, open_review, back_to), name="finisher", daemon=True
        )
        self._finisher.start()

    def _cancel_finisher(self) -> None:
        thread = self._finisher
        if thread is None or not thread.is_alive():
            return
        self._finish_cancel.set()
        self.client.abort()
        self.translate_client.abort()
        thread.join(timeout=10)

    def _finish(self, cancel: threading.Event, open_review: bool, back_to: AppState = AppState.STOPPED) -> None:
        try:
            if not self.document_keys:
                self.document_keys = self._document_keys(quiet=True)
            self._drain_lines(cancel)
            if not cancel.is_set():
                self.translate_pending(cancel)
        except Exception:
            log.exception("Error terminando la sesión")
        finally:
            self.stats.phase = ""
            cancelled = cancel.is_set()
            if not cancelled and self.state.state is AppState.FINISHING:
                self.state.transition(back_to)
            self._emit_stats()
        if cancelled:
            return
        c = self.counts()
        reviewable = c["pending"] + c["unsaved"]
        log.info("Sesión terminada: %s", c)
        if open_review:
            if reviewable:
                for listener in list(self._review_listeners):
                    try:
                        listener(reviewable)
                    except Exception:
                        log.exception("Error en listener de revisión")
                self.notify(f"Stopped. {reviewable} words and expressions are ready to review.")
            else:
                self.notify("Stopped. No new vocabulary this time.")

    def _drain_lines(self, cancel: threading.Event) -> None:
        """Analiza los subtítulos que quedaron pendientes (de esta sesión o de antes)."""
        failures = 0
        while not cancel.is_set():
            left = self.store.count_pending_lines(self.lang)
            if not left:
                return
            self.stats.phase = f"Analyzing {left} remaining subtitle{'s' if left != 1 else ''}…"
            self._emit_stats()
            if self._analyze_next_batch():
                failures = 0
                continue
            failures += 1
            if failures >= 3:
                # Ollama no responde: los subtítulos siguen guardados y se analizarán más adelante.
                self.notify(f"{left} subtitles could not be analyzed now (is Ollama running?). They are kept for later.")
                return
            cancel.wait(5)

    # ----------------------------------------------------------- traducción
    def translator(self) -> Translator:
        if self._translator is not None:
            return self._translator
        s = self.settings
        model = s.translation_model or s.ollama_model
        try:
            if model != s.ollama_model and not self.translate_client.has_model(model):
                log.warning("Modelo de traducción %s no instalado; se traduce con %s", model, s.ollama_model)
                model = s.ollama_model
        except OllamaError:
            pass
        return OllamaTranslator(
            self.translate_client, model, source=s.source_code, target=s.target_code, keep_alive=s.translation_keep_alive
        )

    def translate_pending(self, cancel: threading.Event | None = None) -> int:
        """Traduce los candidatos sin traducción (o traducidos con otro motor). Devuelve cuántos.

        Se agrupan por subtítulo: el motor ve la línea completa y todas las
        unidades de esa línea. Si el motor falla, los candidatos se quedan sin
        traducir (y se reintentan la próxima vez); nunca se pierden.
        """
        cancel = cancel or threading.Event()
        translator = self.translator()
        engine = translator.engine_id
        rows = self.store.untranslated(self.lang, engine)
        if not rows:
            return 0
        done = 0
        results: list[tuple[str, str, str | None]] = []
        groups: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for key, expression, kind, context in rows:
            cached = self.store.cached_translation(self.lang, key, context, engine)
            if cached:
                results.append((key, context, cached))
            else:
                groups[context].append((key, expression, kind))
        if results:
            self.store.set_translations(self.lang, engine, results)
            done += len(results)
        total = len(rows)
        failures = 0
        for context, items in groups.items():
            for i in range(0, len(items), TRANSLATE_BATCH):
                if cancel.is_set():
                    return done
                chunk = items[i : i + TRANSLATE_BATCH]
                self.stats.phase = f"Translating… {done}/{total}"
                self._emit_stats()
                try:
                    out = translator.translate(context, [TranslationItem(e, k) for _, e, k in chunk])
                except OllamaAborted:
                    return done
                except OllamaError as exc:
                    log.warning("Fallo de traducción (%s); se reintentará más tarde", exc)
                    failures += 1
                    if failures >= 3:
                        self.notify("Translations are not available right now (is Ollama running?). Will retry later.",
                                    key="translate", every=300)
                        return done
                    continue
                missing = [i for i, tr in enumerate(out) if not tr]
                if missing:
                    out = self._fallback_translate(translator, context, chunk, out, missing)
                self.store.set_translations(
                    self.lang, engine, [(key, context, tr) for (key, _, _), tr in zip(chunk, out, strict=True)]
                )
                done += len(chunk)
        return done

    def _fallback_translate(self, translator, context, chunk, out, missing) -> list[str | None]:
        """Lo que el motor principal no tradujo (o tradujo con palabras inexistentes)
        se intenta una vez con el modelo de extracción; si tampoco, queda pendiente."""
        s = self.settings
        if self._translator is not None or getattr(translator, "model", s.ollama_model) == s.ollama_model:
            return out
        backup = OllamaTranslator(
            self.translate_client, s.ollama_model, source=s.source_code, target=s.target_code,
            keep_alive=s.translation_keep_alive,
        )
        try:
            extra = backup.translate(context, [TranslationItem(chunk[i][1], chunk[i][2]) for i in missing])
        except OllamaError as exc:
            log.warning("Tampoco se pudo traducir con %s: %s", s.ollama_model, exc)
            return out
        out = list(out)
        for i, tr in zip(missing, extra, strict=True):
            if tr:
                log.info("Traducción de reserva (%s): %s -> %s", s.ollama_model, chunk[i][1], tr)
                out[i] = tr
        return out

    # ---------------------------------------------------------- hilo captura
    def _capture_loop(self, stop: threading.Event) -> None:
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
                        self._enqueue(SubtitleLine(text, "screen", time.time()))
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

    def _enqueue(self, line: SubtitleLine) -> None:
        """Guarda el subtítulo (persistente) y despierta al analizador."""
        log.info("Subtítulo nuevo: %s", line.text)
        self.stats.subtitles += 1
        self.store.add_line(line.text, self.lang, self.session_id)
        self._wake.set()

    def submit_text(self, text: str) -> None:
        """Permite inyectar texto en la sesión activa (pruebas / depuración)."""
        self._enqueue(SubtitleLine(text, "manual", time.time()))

    # ---------------------------------------------------------- hilo análisis
    def _analyzer_loop(self, stop: threading.Event) -> None:
        s = self.settings
        try:
            self.client.warm_up(s.ollama_model, s.ollama_keep_alive)
            log.info("Modelo %s cargado", s.ollama_model)
        except OllamaAborted:
            return
        except OllamaError as exc:
            log.warning("No se pudo precargar el modelo: %s", exc)

        while not stop.is_set():
            if not self.store.count_pending_lines(self.lang):
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if not self.state.transition(AppState.PROCESSING, only_from={AppState.CAPTURING}):
                break
            try:
                ok = self._analyze_next_batch()
            finally:
                self.state.transition(AppState.CAPTURING, only_from={AppState.PROCESSING})
                self._emit_stats()
            if not ok and not stop.is_set():
                self._try_relaunch_ollama(stop)

    def _analyze_next_batch(self) -> bool:
        """Analiza el siguiente lote de subtítulos pendientes. False si el modelo falló.

        Las líneas sólo se marcan como analizadas junto con sus candidatos (en
        la misma transacción): si algo falla, siguen pendientes.
        """
        batch = self.store.pending_lines(self.lang, max(1, self.settings.batch_max_lines))
        if not batch:
            return True
        ids = [i for i, _ in batch]
        try:
            self.stats.llm_calls += 1
            candidates = self.analyzer.analyze([t for _, t in batch])
        except OllamaAborted:
            return False
        except OllamaUnavailableError as exc:
            log.warning("Ollama no responde: %s", exc)
            self.notify("Ollama is not responding. Subtitles are kept and will be analyzed later.", key="ollama", every=120)
            return False
        except OllamaError as exc:
            log.error("Error del modelo: %s", exc)
            self.notify(f"AI model error: {exc}", key="llm", every=120)
            return False
        except Exception:
            log.exception("Error inesperado analizando %s", batch)
            # Una línea que rompe el análisis no debe bloquear las demás para siempre.
            self.store.finish_lines(ids, [], self.lang, self.session_id)
            return True
        added = self.store.finish_lines(ids, candidates, self.lang, self.session_id, self.document_keys)
        if added:
            self.stats.added += len(added)
            self.stats.last_added = (
                [c.expression for c in candidates if entry_key(c.expression, self.lang) in set(added)]
                + self.stats.last_added
            )[:5]
            log.info("Candidatos nuevos: %s", added)
        return True

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

    # ---------------------------------------------------------- revisión
    def review_entries(self):
        return self.store.review_entries(self.lang)

    def accept(self, key: str, translation: str | None = None) -> bool:
        """✓ en la revisión: registra la aceptación y pide al escritor que la guarde."""
        ok = self.store.accept(self.lang, key, str(self.settings.document), translation)
        if ok:
            log.info("Aceptado: %s", key)
            self.saver.wake()
        return ok

    def exclude(self, key: str) -> bool:
        """✕ en la revisión: a la lista de exclusión (en vocabulary.db, nunca en Word)."""
        ok = self.store.exclude(self.lang, key)
        if ok:
            log.info("Excluido: %s", key)
        return ok

    @property
    def document_path(self) -> Path:
        return self.settings.document


class SaveWorker:
    """Escribe en Word las entradas aceptadas que aún no están confirmadas.

    Estados de una entrada aceptada (tabla accepted):
      pending  aceptada, todavía no confirmada en el documento (puede tener un
               error recuperable: documento abierto en Word, carpeta protegida…)
      written  confirmada: se ha vuelto a leer el documento y está.
    Un reintento tras un resultado incierto no duplica nada: lo que ya está en
    el documento cuenta como guardado. Sólo se escribe en el documento
    seleccionado en ese momento.
    """

    RETRY_SECONDS = 15.0

    def __init__(self, app: WordLensApp) -> None:
        self.app = app
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backed_up: set[str] = set()
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="saver", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._event.set()
        if self._thread is None:
            self.save_now()  # sin hilo (pruebas): guardar en el acto

    def _run(self) -> None:
        while not self._stop.is_set():
            pending = self.save_now()
            self._event.wait(self.RETRY_SECONDS if pending else 3600)
            self._event.clear()

    def save_now(self) -> int:
        """Un intento de guardado. Devuelve cuántas entradas siguen sin guardar."""
        app = self.app
        entries = app.store.unsaved(app.lang)
        if not entries:
            self.last_error = None
            return 0
        s = app.settings
        doc = s.document
        writer = WordWriter(doc, backup_count=s.backup_count, backup_dir=BACKUP_DIR, lang=s.source_code)
        keys = [e.key for e in entries]
        try:
            if str(doc) not in self._backed_up and doc.exists():
                writer.backup()
                self._backed_up.add(str(doc))
            confirmed = writer.save_confirmed(
                [VocabularyItem(e.expression, e.translation, e.context) for e in entries]
            )
        except (DocumentError, OSError) as exc:
            if isinstance(exc, DocumentLockedError) or (isinstance(exc, OSError) and is_lock_error(exc)):
                message = f"{doc.name} is open in another program. Close it in Word and it will be saved automatically."
                return self._failed(keys, message, "locked")
            return self._failed(keys, f"Couldn't save to {doc.name}: {exc}", "docerr")
        except StoreError as exc:
            log.error("Error del almacén al guardar: %s", exc)
            return len(keys)
        # La clave del documento puede diferir de la del almacén si cambió el idioma.
        by_doc_key = {entry_key(e.expression, s.source_code): e.key for e in entries}
        written = [by_doc_key[k] for k in confirmed if k in by_doc_key]
        app.store.mark_written(app.lang, written, str(doc))
        missing = [k for k in keys if k not in set(written)]
        if missing:
            return self._failed(missing, f"Not confirmed in {doc.name}; will retry.", "docerr")
        self.last_error = None
        for e in entries:
            log.info("GUARDADO en Word: %s — %s", e.expression, e.translation)
        return 0

    def _failed(self, keys: list[str], message: str, notice_key: str) -> int:
        log.warning("No se pudo guardar (%d entradas): %s", len(keys), message)
        self.last_error = message
        self.app.store.mark_write_failed(self.app.lang, keys, message)
        self.app.notify(message, key=notice_key, every=120)
        return len(keys)
