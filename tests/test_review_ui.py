"""Panel de vocabulario integrado y vocabulario excluido (Tk real, sin mainloop).

Crea la ventana principal (con el panel) de verdad y acciona sus botones como lo haría el usuario
(llamando a los mismos callbacks). Los diálogos de confirmación se responden
con monkeypatch.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer
from wordlens.app import WordLensApp
from wordlens.config.settings import Settings
from wordlens.document.word_writer import WordWriter
from wordlens.vocabulary.models import Candidate
from wordlens.vocabulary.store import VocabularyStore


class _NoModel:
    def chat(self, *a, **k):
        return {"message": {"content": "{}"}}

    def abort(self):
        pass


class _Translator:
    engine_id = "fake:1"

    def translate(self, sentence, items):
        return [f"es:{i.expression}" for i in items]


@pytest.fixture
def ui(tmp_path, monkeypatch):
    settings = Settings(document_path=str(tmp_path / "Vocab.docx"), setup_complete=True, notifications=False)
    store = VocabularyStore(tmp_path / "vocabulary.db").open()
    app = WordLensApp(settings, store=store, translator=_Translator())
    app.analyzer = VocabularyAnalyzer(settings, _NoModel())
    app.saver.wake = app.saver.save_now  # guardado inmediato y síncrono en el test
    from wordlens.ui import window as window_mod

    monkeypatch.setattr(window_mod.MainWindow, "check_ai_async", lambda self: None)
    from wordlens.ui import review as review_mod

    monkeypatch.setattr(review_mod.ReviewPanel, "UNDO_SECONDS", 0)  # decisiones al momento (salvo en los tests de deshacer)
    # Crear muchos intérpretes Tcl seguidos en un mismo proceso falla a veces al
    # buscar init.tcl (problema de Tcl en Windows, no de WordLens): se reintenta.
    for attempt in range(3):
        try:
            win = window_mod.MainWindow(app, on_hotkeys_changed=lambda: {}, on_exit=lambda: None)
            break
        except tk.TclError as exc:
            if attempt == 2:
                pytest.skip(f"Tk no disponible: {exc}")
    yield app, win, tmp_path
    try:
        win.root.destroy()
    except tk.TclError:
        pass
    store.close()


def seed(app, *expressions, context="He brushed off my concerns and went ahead with the deal."):
    app.store.finish_lines([], [Candidate(e, "phrasal verb" if " " in e else "word", context) for e in expressions],
                           "en", None)
    app.translate_pending()


def pump(win, n=5):
    for _ in range(n):
        win.root.update()
        win._drain_calls()


def visible_rows(review):
    return [r.entry.expression for r in review.rows if r.entry is not None and r.frame.winfo_ismapped()]


def test_review_window_shows_entries_and_buttons_work(ui):
    app, win, tmp = ui
    seed(app, "brush off", "concern", "go ahead with", "deal")
    win.open_review()
    pump(win)
    review = win._review
    assert visible_rows(review) == ["brush off", "concern", "go ahead with", "deal"]
    assert review.counter.cget("text") == "4 to review"
    row = review.rows[3]
    assert row.trans.cget("text") == "es:deal"

    row._accept()  # ✓ en "deal"
    pump(win)
    assert WordWriter(tmp / "Vocab.docx").read_expressions() == ["deal"]
    assert "deal" not in visible_rows(review)

    review.rows[0]._exclude()  # ✕ en "brush off"
    pump(win)
    assert app.store.is_excluded("en", "brush off")
    assert visible_rows(review) == ["concern", "go ahead with"]
    assert review.counter.cget("text") == "2 to review"


def test_search_and_empty_states(ui):
    app, win, _ = ui
    win.open_review()
    pump(win)
    review = win._review
    assert review.empty_title.cget("text") == "No words yet"
    assert "Start a session" in review.empty.cget("text")
    seed(app, "brush off", "deal")
    review.reload()
    review._ph_entry.event_generate("<FocusIn>")
    review.query.set("bru")
    pump(win)
    assert visible_rows(review) == ["brush off"]
    review.query.set("zzz")
    pump(win)
    assert review.empty_title.cget("text") == "No matches"


def test_bulk_actions_confirm_first(ui, monkeypatch):
    app, win, tmp = ui
    seed(app, "brush off", "concern", "deal")
    win.open_review()
    pump(win)
    review = win._review
    from wordlens.ui import review as review_mod

    monkeypatch.setattr(review_mod.messagebox, "askyesno", lambda *a, **k: False)
    review.accept_all()
    assert app.store.counts("en")["pending"] == 3  # cancelado: nada cambia
    monkeypatch.setattr(review_mod.messagebox, "askyesno", lambda *a, **k: True)
    review.accept_all()
    pump(win)
    assert sorted(WordWriter(tmp / "Vocab.docx").read_expressions()) == ["brush off", "concern", "deal"]
    assert visible_rows(review) == []


def test_saving_errors_are_visible_and_recoverable(ui, monkeypatch):
    app, win, tmp = ui
    seed(app, "deal")
    import os

    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError()))
    win.open_review()
    pump(win)
    review = win._review
    review.rows[0]._accept()
    pump(win)
    row = review.rows[0]
    assert row.entry.status == "saving" and row.status.winfo_ismapped()
    assert "Close it in Word" in row.status.cget("text")
    assert review.banner.winfo_ismapped()
    monkeypatch.undo()
    app.saver.save_now()
    review.reload()
    pump(win)
    assert visible_rows(review) == []
    assert WordWriter(tmp / "Vocab.docx").read_expressions() == ["deal"]


def test_many_entries_use_few_widgets(ui):
    app, win, _ = ui
    words = ["zq" + "".join(chr(97 + (i // 26**k) % 26) for k in range(3)) for i in range(2000)]
    seed(app, *words)
    win.open_review()
    pump(win)
    review = win._review
    assert len(review.visible) == 2000
    assert len(review.rows) < 30  # filas reutilizadas, no 2000 widgets
    review.on_scroll("moveto", "0.5")
    pump(win)
    middle = visible_rows(review)
    assert middle and middle[0] not in words[:20]


def test_panel_is_always_visible_and_updates_live(ui):
    app, win, _ = ui
    win.show()
    pump(win)
    review = win._review
    assert review.list_frame.winfo_ismapped()  # integrado, también sin palabras
    assert review.empty_box.winfo_ismapped()
    seed(app, "deal")  # palabra detectada durante la sesión: llega por store.subscribe
    for _ in range(10):
        pump(win)
        win.root.after(60)
    win.root.update()
    review._deferred_reload()
    pump(win)
    assert visible_rows(review) == ["deal"]
    assert not review.empty_box.winfo_ismapped()
    assert review.counter.cget("text") == "1 to review"
    assert win.review_count_label.cget("text") == "1"
    win.open_review()  # bandeja / fin de sesión: muestra la ventana con el panel
    pump(win)
    assert visible_rows(review) == ["deal"]


def test_excluded_dialog_search_restore(ui, monkeypatch):
    app, win, _ = ui
    for e in ("nail", "brush off", "give up"):
        app.store.exclude("en", e, e)
    from wordlens.ui import review as review_mod

    dialog = review_mod.ExcludedDialog(win)
    pump(win)
    assert set(dialog.tree.get_children()) == {"nail", "brush off", "give up"}
    dialog.query.set("brush")
    pump(win)
    assert dialog.tree.get_children() == ("brush off",)
    dialog.tree.selection_set("brush off")
    monkeypatch.setattr(review_mod.messagebox, "askyesno", lambda *a, **k: True)
    dialog.restore()
    assert not app.store.is_excluded("en", "brush off")
    assert not app.store.is_accepted("en", "brush off")  # restaurar no acepta ni escribe
    dialog.query.set("")
    pump(win)
    assert set(dialog.tree.get_children()) == {"nail", "give up"}
    for k in ("nail", "give up"):
        app.store.restore("en", [k])
    dialog.load()
    assert "Nothing excluded yet" in dialog.empty.cget("text")


def test_edit_translation_by_double_click(ui):
    app, win, tmp = ui
    seed(app, "deal")
    win.open_review()
    pump(win)
    row = win._review.rows[0]
    assert row.trans.bind("<Double-Button-1>")  # el doble clic está enlazado a editar
    win._review.edit(row.entry)  # edición en la propia fila
    win._review.editor_var.set("  trato   comercial ")
    win._review._finish_edit(save=True)
    pump(win)
    assert win._review.rows[0].trans.cget("text") == "trato comercial"


def key(win, keysym, state=0):
    win.root.focus_set()
    event = type("E", (), {"keysym": keysym, "state": state})()
    return win._review._on_key(event)


def test_undo_keeps_the_decision_pending_until_it_expires(ui, monkeypatch):
    app, win, tmp = ui
    from wordlens.ui import review as review_mod

    monkeypatch.setattr(review_mod.ReviewPanel, "UNDO_SECONDS", 30)
    seed(app, "brush off", "deal")
    win.open_review()
    pump(win)
    review = win._review
    review.rows[1]._accept()  # ✓ "deal": oculta y con «Undo», todavía sin aplicar
    pump(win)
    assert visible_rows(review) == ["brush off"]
    assert review.undo_bar.winfo_ismapped()
    assert not app.store.is_accepted("en", "deal")
    review.undo()
    pump(win)
    assert visible_rows(review) == ["brush off", "deal"]
    assert not review.undo_bar.winfo_ismapped()

    review.rows[0]._exclude()  # ✕ "brush off" ...
    review.rows[0]._accept()  # ... y la siguiente decisión aplica la anterior
    pump(win)
    assert app.store.is_excluded("en", "brush off")
    assert not app.store.is_accepted("en", "deal")
    win.exit()  # salir aplica lo que esperaba
    assert WordWriter(tmp / "Vocab.docx").read_expressions() == ["deal"]


def test_keyboard_navigation_and_actions(ui, monkeypatch):
    app, win, tmp = ui
    from wordlens.ui import review as review_mod
    # Pasar el ratón por una fila la selecciona: que el puntero real no interfiera con el teclado.
    monkeypatch.setattr(review_mod._Row, "_hover", lambda self, _e: None)
    seed(app, "brush off", "concern", "deal")
    win.open_review()
    pump(win)
    review = win._review
    assert key(win, "Down") == "break"
    assert review.selected_key == "brush off"
    key(win, "Down")
    assert review.selected_key == "concern"
    key(win, "x")  # excluir la seleccionada; la selección pasa a la siguiente
    pump(win)
    assert app.store.is_excluded("en", "concern")
    assert review.selected_key == "deal"
    key(win, "Return")
    pump(win)
    assert WordWriter(tmp / "Vocab.docx").read_expressions() == ["deal"]
    review._ph_entry.focus_set()
    pump(win)
    # mientras se escribe en la búsqueda, las teclas no actúan
    assert review._on_key(type("E", (), {"keysym": "x", "state": 0})()) is None


def test_inline_edit_of_a_translation(ui):
    app, win, _ = ui
    seed(app, "deal")
    win.open_review()
    pump(win)
    review = win._review
    review.edit(review.visible[0])
    pump(win)
    assert review.editor.winfo_ismapped()
    review.editor_var.set("  trato   cerrado ")
    review._finish_edit(save=True)
    pump(win)
    assert not review.editor.winfo_ismapped()
    assert review.visible[0].translation == "trato cerrado"
    review.edit(review.visible[0])
    review.editor_var.set("otra cosa")
    review._finish_edit(save=False)  # Esc: no cambia nada
    assert review.visible[0].translation == "trato cerrado"


def test_kind_filters_and_frequency_sort(ui):
    app, win, _ = ui
    seed(app, "deal", "brush off", "hit the nail on the head")
    seed(app, "deal", "deal")  # más apariciones
    win.open_review()
    pump(win)
    review = win._review
    review.set_kind_filter("Phrasal verbs")
    pump(win)
    assert [e.expression for e in review.visible] == [e.expression for e in review.entries if e.kind == "phrasal verb"]
    review.set_kind_filter("All")
    review.set_sort("Frequent")
    pump(win)
    assert review.visible[0].expression == "deal"
    assert review.visible[0].occurrences >= review.visible[-1].occurrences


def test_theme_switch_rebuilds_the_window(ui):
    app, win, _ = ui
    from wordlens.ui import window as window_mod

    seed(app, "deal")
    win.show()
    pump(win)
    win.set_theme("light")
    pump(win)
    assert app.settings.theme == "light" and window_mod.current_theme == "light"
    assert win.root.cget("bg") == window_mod.PALETTES["light"]["BASE"]
    assert visible_rows(win._review) == ["deal"]
    win.set_theme("dark")
    pump(win)
    assert win.root.cget("bg") == window_mod.PALETTES["dark"]["BASE"]
