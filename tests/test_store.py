"""Estado persistente: deduplicación, aceptadas, excluidas, pendientes y traducciones."""

from __future__ import annotations

import sqlite3

import pytest

from wordlens.vocabulary.models import Candidate
from wordlens.vocabulary.normalization import entry_key
from wordlens.vocabulary.store import StoreError, VocabularyStore

EN = "en"


@pytest.fixture
def store(tmp_path):
    s = VocabularyStore(tmp_path / "vocabulary.db", backup_dir=tmp_path / "backups").open()
    yield s
    s.close()


def add(store, *expressions, context="ctx", session=None, known=()):
    cands = [Candidate(e, "phrasal verb" if " " in e else "word", context) for e in expressions]
    return store.finish_lines([], cands, EN, session, known)


def pending_keys(store):
    return [e.key for e in store.review_entries(EN) if e.status == "pending"]


def translate_all(store, text="traducción"):
    rows = store.untranslated(EN, "test-engine")
    store.set_translations(EN, "test-engine", [(k, ctx, f"{text} {e}") for k, e, _, ctx in rows])


# ----------------------------------------------------------- deduplicación
def test_fifty_occurrences_one_entry(store):
    for _ in range(50):
        add(store, "deal")
    entries = store.review_entries(EN)
    assert [(e.expression, e.occurrences) for e in entries] == [("deal", 50)]


def test_same_entry_in_different_sentences_and_forms_is_merged(store):
    add(store, "concerns", context="My concerns are real.")
    add(store, "concern", context="That's not my concern.")
    add(store, "Concern", context="CONCERN!")
    assert pending_keys(store) == ["concern"]


def test_same_entry_in_a_later_session(store, tmp_path):
    add(store, "deal", session=1)
    store.close()
    again = VocabularyStore(tmp_path / "vocabulary.db").open()
    assert add(again, "deal", session=2) == []  # ya estaba pendiente: no se duplica
    assert [(e.key, e.occurrences) for e in again.review_entries(EN)] == [("deal", 2)]
    again.close()


def test_entries_already_in_the_document_are_not_proposed(store):
    assert add(store, "deal", "brush off", known={"deal"}) == ["brush off"]


# ---------------------------------------------------------- exclusiones
def test_exclude_single_word_keeps_expressions_with_it(store):
    add(store, "nail")
    store.exclude(EN, "nail")
    assert add(store, "nail", "nails", "hit the nail on the head", "nail polish") == [
        "hit the nail on the head", "nail polish"]


def test_exclude_expression_keeps_its_words_and_other_expressions(store):
    add(store, "brush off")
    store.exclude(EN, "brush off")
    assert add(store, "brushed off", "brush off", "brush", "brush up on") == ["brush", "brush up on"]


def test_give_up_and_give_in_are_different(store):
    add(store, "give up", "give in")
    store.exclude(EN, "give up")
    assert pending_keys(store) == ["give in"]
    assert add(store, "gave up") == []
    assert store.is_excluded(EN, "give up") and not store.is_excluded(EN, "give in")


def test_exclusion_key_normalization(store):
    store.exclude(EN, entry_key("  Brush   OFF! "), "brush off")
    assert store.is_excluded(EN, "brush off")
    assert add(store, "BRUSH OFF", "brushing off") == []


def test_exclude_removes_from_review_immediately_and_not_written(store):
    add(store, "deal")
    assert store.exclude(EN, "deal")
    assert store.review_entries(EN) == []
    assert store.unsaved(EN) == []
    assert not store.is_accepted(EN, "deal")


def test_restore_lets_it_be_proposed_again_without_accepting(store):
    add(store, "restaurant")
    store.exclude(EN, "restaurant")
    assert add(store, "restaurant") == []
    assert store.restore(EN, ["restaurant"]) == 1
    assert not store.is_accepted(EN, "restaurant")
    assert store.unsaved(EN) == []  # restaurar no escribe nada en Word
    assert add(store, "restaurant") == ["restaurant"]


def test_restore_does_not_override_an_acceptance(store):
    add(store, "restaurant")
    translate_all(store)
    store.accept(EN, "restaurant", "doc.docx")
    # Estado incoherente posible tras importar datos: aceptada y excluida a la vez.
    store.exclude(EN, "restaurant", "restaurant")
    store.restore(EN, ["restaurant"])
    assert add(store, "restaurant") == []  # la aceptación sigue mandando


def test_excluded_list_search_and_large_lists(store):
    for i in range(1200):
        store.exclude(EN, f"word{i:04d}", f"word{i:04d}")
    store.exclude(EN, "brush off", "brush off")
    page, total = store.excluded_entries(EN, limit=100)
    assert total == 1201 and len(page) == 100
    found, n = store.excluded_entries(EN, "brush")
    assert n == 1 and found[0].expression == "brush off"


# ------------------------------------------------------------ aceptadas
def test_accept_moves_to_unsaved_and_out_of_review(store):
    add(store, "deal")
    translate_all(store)
    assert store.accept(EN, "deal", "C:/doc.docx")
    entries = store.review_entries(EN)
    assert [(e.key, e.status) for e in entries] == [("deal", "saving")]  # visible hasta confirmar Word
    assert [u.key for u in store.unsaved(EN)] == ["deal"]
    store.mark_written(EN, ["deal"], "C:/doc.docx")
    assert store.review_entries(EN) == [] and store.unsaved(EN) == []


def test_accepted_never_comes_back_even_if_deleted_from_word(store):
    add(store, "deal")
    translate_all(store)
    store.accept(EN, "deal", "doc")
    store.mark_written(EN, ["deal"], "doc")
    # Aunque el documento ya no lo tenga (known vacío), no se vuelve a proponer.
    assert add(store, "deal", "deals", known=set()) == []


def test_accept_without_translation_is_refused(store):
    add(store, "deal")
    with pytest.raises(StoreError):
        store.accept(EN, "deal", "doc")
    assert pending_keys(store) == ["deal"]


def test_accept_twice_is_harmless(store):
    add(store, "deal")
    translate_all(store)
    assert store.accept(EN, "deal", "doc")
    assert not store.accept(EN, "deal", "doc")
    assert len(store.unsaved(EN)) == 1


def test_write_failure_keeps_entry_with_error(store):
    add(store, "deal")
    translate_all(store)
    store.accept(EN, "deal", "doc")
    store.mark_write_failed(EN, ["deal"], "locked")
    [entry] = store.review_entries(EN)
    assert (entry.status, entry.error) == ("saving", "locked")
    assert store.unsaved(EN)[0].attempts == 1


# ----------------------------------------------------------- persistencia
def test_everything_survives_reopening(tmp_path):
    path = tmp_path / "vocabulary.db"
    s = VocabularyStore(path).open()
    add(s, "deal", "brush off", "concern")
    translate_all(s)
    s.accept(EN, "deal", "doc")
    s.exclude(EN, "brush off")
    line = s.add_line("He brushed off my concerns.", EN, None)
    s.close()

    s = VocabularyStore(path).open()
    assert pending_keys(s) == ["concern"]
    assert s.review_entries(EN)[0].translation == "traducción concern"
    assert [u.key for u in s.unsaved(EN)] == ["deal"]
    assert s.is_excluded(EN, "brush off")
    assert s.pending_lines(EN) == [(line, "He brushed off my concerns.")]
    s.close()


def test_lines_are_marked_done_with_their_candidates_atomically(store):
    a = store.add_line("one", EN, None)
    b = store.add_line("two", EN, None)
    store.finish_lines([a], [Candidate("deal", "word", "one")], EN, None)
    assert store.pending_lines(EN) == [(b, "two")]
    assert pending_keys(store) == ["deal"]


def test_languages_do_not_mix(store):
    store.finish_lines([], [Candidate("once", "word", "x")], "en", None)
    store.exclude("en", "once")
    assert store.finish_lines([], [Candidate("once", "word", "y")], "es", None) == ["once"]


def test_damaged_database_is_set_aside_and_restored_from_backup(tmp_path):
    path = tmp_path / "vocabulary.db"
    backups = tmp_path / "backups"
    s = VocabularyStore(path, backup_dir=backups).open()
    s.exclude(EN, "nail", "nail")
    s.backup()
    s.close()
    path.write_bytes(b"this is not a database" * 100)

    s = VocabularyStore(path, backup_dir=backups).open()
    assert s.recovered and "restored" in s.recovered
    assert s.is_excluded(EN, "nail")
    assert list(tmp_path.glob("vocabulary.damaged_*.db"))  # el archivo dañado se conserva
    s.close()


def test_damaged_database_without_backup_starts_empty_but_keeps_file(tmp_path):
    path = tmp_path / "vocabulary.db"
    path.write_bytes(b"garbage" * 1000)
    s = VocabularyStore(path, backup_dir=tmp_path / "none").open()
    assert s.recovered and "empty" in s.recovered
    assert s.review_entries(EN) == []
    assert list(tmp_path.glob("vocabulary.damaged_*.db"))
    s.close()


def test_backups_rotate(tmp_path, monkeypatch):
    s = VocabularyStore(tmp_path / "v.db", backup_dir=tmp_path / "b", backups_to_keep=3).open()
    import wordlens.vocabulary.store as mod

    for i in range(6):
        monkeypatch.setattr(mod, "datetime", type("D", (), {"now": staticmethod(lambda i=i: _Fake(i))}))
        s.backup()
    assert len(list((tmp_path / "b").glob("vocabulary_*.db"))) == 3
    s.close()


class _Fake:
    def __init__(self, i):
        self.i = i

    def strftime(self, _fmt):
        return f"2026010{self.i}_000000"


def test_transaction_rolls_back_on_error(store, monkeypatch):
    add(store, "deal")
    translate_all(store)
    with pytest.raises(sqlite3.Error), store._tx() as c:
        c.execute("DELETE FROM candidates")
        c.execute("INSERT INTO nope VALUES (1)")
    assert pending_keys(store) == ["deal"]


# ------------------------------------------------------------ traducciones
def test_translations_are_stored_and_reused(store):
    add(store, "deal", context="We made a deal.")
    assert store.untranslated(EN, "engine-a") == [("deal", "deal", "word", "We made a deal.")]
    store.set_translations(EN, "engine-a", [("deal", "We made a deal.", "acuerdo")])
    assert store.untranslated(EN, "engine-a") == []
    assert store.cached_translation(EN, "deal", "We made a deal.", "engine-a") == "acuerdo"


def test_changing_engine_marks_translations_stale(store):
    add(store, "deal", context="c")
    store.set_translations(EN, "engine-a", [("deal", "c", "acuerdo")])
    assert [r[0] for r in store.untranslated(EN, "engine-b")] == ["deal"]
    assert store.review_entries(EN)[0].translation == "acuerdo"  # mientras tanto se ve la anterior


def test_failed_translation_keeps_candidate(store):
    add(store, "deal", context="c")
    store.set_translations(EN, "engine-a", [("deal", "c", None)])
    [entry] = store.review_entries(EN)
    assert entry.translation is None
    assert [r[0] for r in store.untranslated(EN, "engine-a")] == ["deal"]  # se reintenta


def test_import_accepted(store):
    n = store.import_accepted(EN, [("figure out", ""), ("Figure Out", ""), ("get at", "")], origin="document", written=True)
    assert n == 2
    assert add(store, "figure out", "get at", "deal") == ["deal"]
