"""Integración: sesión -> detener -> terminar pendientes -> revisión -> ✓ Word / ✕ exclusión.

Usa la aplicación real (WordLensApp, almacén SQLite real, documento .docx real
en una carpeta temporal) con el modelo y el traductor sustituidos por dobles
deterministas. Prueba el flujo y la protección de datos, no la calidad del
modelo (eso lo miden los benchmarks y los tests @llm).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

import pytest

from wordlens.ai.ollama_client import OllamaUnavailableError
from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer
from wordlens.app import WordLensApp
from wordlens.config.settings import Settings
from wordlens.document.word_writer import WordWriter
from wordlens.state import AppState
from wordlens.vocabulary.models import VocabularyItem
from wordlens.vocabulary.store import StoreError, VocabularyStore

BRUSH = "He brushed off my concerns and went ahead with the deal."
NAIL = "You really hit the nail on the head."
GLOSSARY = {"brush off": "restar importancia a", "concern": "preocupación", "go ahead with": "seguir adelante con",
            "deal": "acuerdo", "hit the nail on the head": "dar en el clavo", "go": "ir", "ahead": "adelante",
            "put up with": "aguantar", "anymore": "ya no"}


class FakeModel:
    """Extracción: respuestas por línea; puede fallar como Ollama caído."""

    def __init__(self, answers=None, fail=False):
        self.answers = answers or {
            BRUSH: [("brush off", "phrasal verb"), ("concern", "word"), ("go ahead with", "phrasal verb"),
                    ("deal", "word")],
            NAIL: [("hit the nail on the head", "idiom")],
        }
        self.fail = fail
        self.calls = 0

    def chat(self, model, messages, **kw):
        self.calls += 1
        if self.fail:
            raise OllamaUnavailableError("Ollama no responde")
        lines = [ln.split(". ", 1)[1].split("   [")[0] for ln in messages[-1]["content"].splitlines()]
        items = [{"line": i, "expression": e, "type": t}
                 for i, line in enumerate(lines, 1) for e, t in self.answers.get(line, [])]
        return {"message": {"content": json.dumps({"items": items, "names": []})}}

    def abort(self):
        pass


class FakeTranslator:
    engine_id = "fake:1"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def translate(self, sentence, items):
        self.calls.append((sentence, [i.expression for i in items]))
        if self.fail:
            raise OllamaUnavailableError("down")
        return [GLOSSARY.get(i.expression, f"es:{i.expression}") for i in items]


@pytest.fixture
def env(tmp_path):
    return {"doc": tmp_path / "Vocab.docx", "db": tmp_path / "data" / "vocabulary.db", "tmp": tmp_path}


def make_app(env, model=None, translator=None):
    settings = Settings(document_path=str(env["doc"]), setup_complete=True, notifications=False)
    store = VocabularyStore(env["db"], backup_dir=env["tmp"] / "data" / "backups").open()
    app = WordLensApp(settings, store=store, translator=translator or FakeTranslator())
    app.analyzer = VocabularyAnalyzer(settings, model or FakeModel())
    app.client = app.analyzer.client
    app.notify = lambda msg, **kw: app.notices.append(msg)
    app.notices = []
    app.saver.wake = lambda: None  # sin hilo: cada test decide cuándo se intenta guardar (save_now)
    return app


def run_session(app, *lines):
    """Una sesión sin captura de pantalla: los subtítulos llegan por submit_text."""
    app.session_id = app.store.start_session(str(app.settings.document))
    app.state.transition(AppState.CAPTURING)
    for line in lines:
        app.submit_text(line)
    ready = []
    app.on_review_ready(ready.append)
    assert app.stop()
    app._finisher.join(timeout=30)
    return ready


def doc_entries(env):
    return WordWriter(env["doc"]).read_expressions() if env["doc"].exists() else []


def keys(app):
    return [e.key for e in app.review_entries() if e.status == "pending"]


# ------------------------------------------------------------------ flujo
def test_stop_finishes_pending_lines_translates_and_opens_review(env):
    app = make_app(env)
    ready = run_session(app, BRUSH, NAIL, BRUSH)
    assert ready == [5]  # la revisión se abre con 5 entradas (sin duplicados)
    assert keys(app) == ["brush off", "concern", "go ahead with", "deal", "hit the nail on the head"]
    assert {e.translation for e in app.review_entries()} >= {"restar importancia a", "acuerdo", "dar en el clavo"}
    assert app.state.state is AppState.STOPPED
    assert app.store.count_pending_lines("en") == 0
    assert doc_entries(env) == []  # nada se escribe sin aceptar


def test_no_review_window_when_nothing_new(env):
    app = make_app(env)
    run_session(app, BRUSH)
    for k in keys(app):
        app.exclude(k)
    ready = run_session(app, BRUSH)
    assert ready == []
    assert any("No new vocabulary" in n for n in app.notices)


def test_translation_uses_the_whole_line(env):
    tr = FakeTranslator()
    app = make_app(env, translator=tr)
    run_session(app, BRUSH)
    assert tr.calls == [(BRUSH, ["brush off", "concern", "go ahead with", "deal"])]


def test_accept_writes_to_word_and_leaves_review(env):
    app = make_app(env)
    run_session(app, BRUSH)
    assert app.accept("deal")
    assert app.saver.save_now() == 0
    assert doc_entries(env) == ["deal"]
    assert "deal" not in [e.key for e in app.review_entries()]
    assert app.store.is_accepted("en", "deal")


def test_exclude_goes_to_blacklist_not_to_word(env):
    app = make_app(env)
    run_session(app, BRUSH)
    assert app.exclude("brush off")
    assert app.store.is_excluded("en", "brush off")
    assert "brush off" not in keys(app)
    app.saver.save_now()
    assert doc_entries(env) == []


def test_restart_keeps_everything(env):
    app = make_app(env)
    run_session(app, BRUSH, NAIL)
    app.accept("deal")
    app.saver.save_now()
    app.exclude("brush off")
    app.shutdown()

    app2 = make_app(env)
    assert keys(app2) == ["concern", "go ahead with", "hit the nail on the head"]  # sin revisar: siguen
    assert app2.store.is_excluded("en", "brush off")
    assert app2.store.is_accepted("en", "deal")
    ready = run_session(app2, BRUSH)  # aparecen otra vez en un subtítulo
    assert ready == [3]
    assert "brush off" not in keys(app2) and "deal" not in keys(app2)  # ni excluida ni aceptada vuelven
    app2.shutdown()


def test_restored_exclusion_is_proposed_again_but_not_written(env):
    app = make_app(env)
    run_session(app, BRUSH)
    app.exclude("deal")
    app.store.restore("en", ["deal"])
    app.saver.save_now()
    assert doc_entries(env) == []
    run_session(app, BRUSH)
    assert "deal" in keys(app)


def test_deleting_from_word_does_not_bring_it_back(env):
    app = make_app(env)
    run_session(app, BRUSH)
    app.accept("deal")
    app.saver.save_now()
    env["doc"].unlink()  # el usuario borra el documento (o la línea)
    run_session(app, BRUSH)
    assert "deal" not in keys(app)


# ------------------------------------------------------- escritura en Word
def test_locked_document_keeps_entry_and_retries_without_duplicates(env, monkeypatch):
    app = make_app(env)
    run_session(app, BRUSH)
    WordWriter(env["doc"]).ensure_exists()
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("locked by Word")))
    app.accept("deal")
    assert app.saver.save_now() == 1
    [entry] = [e for e in app.review_entries() if e.key == "deal"]
    assert entry.status == "saving" and "Close it in Word" in entry.error
    assert app.store.is_accepted("en", "deal")  # la decisión no se pierde
    assert doc_entries(env) == []

    monkeypatch.setattr(os, "replace", real_replace)
    assert app.saver.save_now() == 0
    assert app.saver.save_now() == 0
    assert doc_entries(env) == ["deal"]
    assert [e.key for e in app.review_entries()].count("deal") == 0


def test_really_locked_file(env):
    """Bloqueo real de Windows (como Word): el archivo no se puede ni leer."""
    import msvcrt

    app = make_app(env)
    run_session(app, BRUSH)
    WordWriter(env["doc"]).add_entries([VocabularyItem("figure out", "averiguar")])
    fh = open(env["doc"], "r+b")  # noqa: SIM115
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, os.path.getsize(env["doc"]))
        app.accept("deal")
        assert app.saver.save_now() == 1
        [entry] = [e for e in app.review_entries() if e.key == "deal"]
        assert "Close it in Word" in entry.error  # bloqueado, no "archivo dañado"
    finally:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, os.path.getsize(env["doc"]))
        fh.close()
    assert app.saver.save_now() == 0
    assert doc_entries(env) == ["figure out", "deal"]


def test_uncertain_write_is_not_duplicated(env):
    """La app se cerró justo después de guardar y antes de anotarlo: el reintento no duplica."""
    app = make_app(env)
    run_session(app, BRUSH)
    app.accept("deal")
    WordWriter(env["doc"]).save_confirmed([VocabularyItem("deal", "acuerdo")])  # escrito, sin confirmar
    assert app.store.unsaved("en")
    assert app.saver.save_now() == 0
    assert doc_entries(env) == ["deal"]
    assert not app.store.unsaved("en")


def test_write_error_other_than_lock(env, tmp_path):
    app = make_app(env)
    run_session(app, BRUSH)
    env["doc"].write_bytes(b"not a docx")  # documento dañado: no se toca
    app.accept("deal")
    assert app.saver.save_now() == 1
    assert env["doc"].read_bytes() == b"not a docx"
    assert app.store.unsaved("en")[0].error


def test_other_documents_are_never_touched(env):
    other = env["tmp"] / "Other.docx"
    WordWriter(other).add_entries([VocabularyItem("figure out", "averiguar")])
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    app = make_app(env)
    run_session(app, BRUSH)
    app.accept("deal")
    app.saver.save_now()
    assert hashlib.sha256(other.read_bytes()).hexdigest() == digest
    assert doc_entries(env) == ["deal"]


def test_accept_all_with_partial_failure(env, monkeypatch):
    app = make_app(env)
    run_session(app, BRUSH)
    app.store.set_translations("en", "fake:1", [])  # nada
    # "concern" se queda sin traducción: no se puede aceptar todavía.
    with app.store._tx() as c:
        c.execute("UPDATE candidates SET translation=NULL WHERE key='concern'")
    accepted, refused = [], []
    for k in keys(app):
        try:
            (accepted if app.accept(k) else refused).append(k)
        except StoreError:
            refused.append(k)
    assert refused == ["concern"] and len(accepted) == 3
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError()))
    assert app.saver.save_now() == 3  # falla la escritura: las 3 siguen recuperables
    monkeypatch.undo()
    assert app.saver.save_now() == 0
    assert sorted(doc_entries(env)) == ["brush off", "deal", "go ahead with"]
    assert keys(app) == ["concern"]


# ------------------------------------------------------- fallos y recuperación
def test_ollama_down_keeps_subtitles_for_later(env):
    model = FakeModel(fail=True)
    app = make_app(env, model=model)
    ready = run_session(app, BRUSH, NAIL)
    assert app.store.count_pending_lines("en") == 2
    assert ready == []
    assert any("kept for later" in n for n in app.notices)
    model.fail = False
    app._start_finisher(open_review=True)
    app._finisher.join(timeout=30)
    assert app.store.count_pending_lines("en") == 0
    assert "hit the nail on the head" in keys(app)


def test_translation_failure_keeps_candidates_and_retries(env):
    tr = FakeTranslator(fail=True)
    app = make_app(env, translator=tr)
    run_session(app, BRUSH)
    assert keys(app) == ["brush off", "concern", "go ahead with", "deal"]
    assert all(e.translation is None for e in app.review_entries())
    tr.fail = False
    assert app.translate_pending() == 4
    assert all(e.translation for e in app.review_entries())


def test_crash_mid_session_resumes_on_next_start(env):
    """Subtítulos guardados y app cerrada de golpe: al volver a abrir se terminan solos."""
    app = make_app(env)
    app.session_id = app.store.start_session("x")
    app.submit_text(BRUSH)
    app.store.close()  # sin stop(): como un cierre inesperado

    app2 = make_app(env)
    app2.boot()
    for _ in range(100):
        if app2._finisher is not None:
            break
        threading.Event().wait(0.05)
    app2._finisher.join(timeout=30)
    assert keys(app2) == ["brush off", "concern", "go ahead with", "deal"]
    assert app2.state.state is AppState.INACTIVE
    app2.shutdown()


def test_start_during_finishing_cancels_it_without_losing_work(env):
    app = make_app(env)
    gate = threading.Event()
    real = app.analyzer.analyze

    def slow(lines):
        gate.wait(5)
        return real(lines)

    app.analyzer.analyze = slow
    app.session_id = app.store.start_session("x")
    app.state.transition(AppState.CAPTURING)
    app.submit_text(BRUSH)
    app.stop()
    app._cancel_finisher()
    gate.set()
    assert app._finisher is not None and not app._finisher.is_alive()
    # La línea sigue guardada (pendiente o ya analizada), nunca perdida.
    assert app.store.count_pending_lines("en") + len(keys(app)) > 0


def test_untranslated_items_fall_back_to_the_extraction_model(env):
    """Si el traductor principal no da traducción válida, se intenta con el modelo de extracción."""
    from wordlens.translation.translator import OllamaTranslator

    class Client:
        def __init__(self):
            self.models = []

        def chat(self, model, messages, **kw):
            self.models.append(model)
            text = "basuraleza" if model == "gemma3:4b" else "pocilga"
            return {"message": {"content": json.dumps({"translations": [{"n": 1, "t": text}]})}}

        def abort(self):
            pass

    app = make_app(env, model=FakeModel(answers={"This place is a dump.": [("dump", "word")]}))
    app._translator = None
    app.translate_client = Client()
    app.translator = lambda: OllamaTranslator(app.translate_client, "gemma3:4b")
    run_session(app, "This place is a dump.")
    assert {e.key: e.translation for e in app.review_entries()}["dump"] == "pocilga"
    assert app.translate_client.models[:2] == ["gemma3:4b", "maternion/ling-3.0-tiny:8b"]


def test_manual_translation_fix_before_accepting(env):
    app = make_app(env)
    run_session(app, BRUSH)
    assert app.store.set_translation("en", "deal", "trato")
    app.accept("deal")
    app.saver.save_now()
    from docx import Document

    assert any(p.text == "deal — trato" for p in Document(str(env["doc"])).paragraphs)
