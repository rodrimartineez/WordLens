"""Migración de una instalación 1.x en uso: nada se borra, nada se pierde."""

from __future__ import annotations

import json

import pytest

from wordlens import migration
from wordlens.config.settings import Settings, load_settings
from wordlens.document.word_writer import WordWriter
from wordlens.vocabulary.models import Candidate, VocabularyItem
from wordlens.vocabulary.store import VocabularyStore

V11_CONFIG = {
    "app_name": "WordLens", "target_level": "B2-C1", "source_language": "English", "target_language": "Spanish",
    "setup_complete": True, "hotkey_start": "ctrl+shift+s", "hotkey_stop": "ctrl+shift+x",
    "document_path": "", "backup_count": 10, "capture_monitor": 3, "capture_monitor_id": "\\\\?\\DISPLAY#GSM0001",
    "ollama_model": "maternion/ling-3.0-tiny:8b", "batch_max_lines": 4, "max_queue_lines": 40,
    "prefilter_trivial_lines": True, "candidate_hints": True, "review_pass": True,
    "max_single_word_zipf": 4.5, "max_expression_words": 6, "notifications": True,
}


@pytest.fixture
def legacy(tmp_path):
    """Datos de una instalación 1.1 real: config, documento con entradas y pendientes."""
    data = tmp_path / "WordLens"
    data.mkdir()
    doc = tmp_path / "Ingles" / "Vocabulary.docx"
    WordWriter(doc).add_entries([VocabularyItem("figure out", "averiguar"), VocabularyItem("landlady", "casera")])
    cfg = dict(V11_CONFIG, document_path=str(doc))
    (data / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    pending = data / "pending"
    pending.mkdir()
    (pending / "Vocabulary_ab12cd34.pending.txt").write_text("get at — insinuar\nspilled — derramado\n", encoding="utf-8")
    return {"data": data, "doc": doc, "pending": pending, "cfg": cfg}


def test_config_from_v11_keeps_user_values_and_is_backed_up(legacy):
    path = legacy["data"] / "config.json"
    before = path.read_text(encoding="utf-8")
    s = load_settings(path)
    assert (s.hotkey_start, s.capture_monitor, s.document) == ("ctrl+shift+s", 3, legacy["doc"])
    assert s.ollama_model == "maternion/ling-3.0-tiny:8b"  # el modelo de extracción no cambia
    assert s.translation_model == "gemma3:4b"
    assert s.batch_max_lines == 1  # el valor por defecto antiguo pasa al nuevo
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "translation_model" in saved and "review_pass" not in saved
    backups = list(legacy["data"].glob("config_*.json"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == before


def test_user_batch_size_is_respected(legacy):
    path = legacy["data"] / "config.json"
    path.write_text(json.dumps(dict(legacy["cfg"], batch_max_lines=2)), encoding="utf-8")
    assert load_settings(path).batch_max_lines == 2


def run_migration(legacy):
    settings = Settings(document_path=str(legacy["doc"]), setup_complete=True)
    store = VocabularyStore(legacy["data"] / "vocabulary.db").open()
    migration.backup_legacy_data(store, config_path=legacy["data"] / "config.json",
                                 pending_dir=legacy["pending"], backup_dir=legacy["data"] / "backups")
    migration.import_document(store, settings)
    migration.import_legacy_pending(store, settings, legacy["pending"])
    return store, settings


def test_document_entries_become_accepted_and_doc_is_untouched(legacy):
    before = legacy["doc"].read_bytes()
    store, _ = run_migration(legacy)
    assert store.is_accepted("en", "figure out") and store.is_accepted("en", "landlady")
    assert legacy["doc"].read_bytes() == before
    # Aunque se borren del documento, no se vuelven a proponer.
    assert store.finish_lines([], [Candidate("landlady"), Candidate("deal")], "en", None) == ["deal"]
    store.close()


def test_legacy_pending_become_unsaved_and_file_is_kept(legacy):
    store, _ = run_migration(legacy)
    assert sorted(u.key for u in store.unsaved("en")) == ["get at", "spilled"]
    assert not list(legacy["pending"].glob("*.pending.txt"))
    assert list(legacy["pending"].glob("*.pending.txt.imported"))  # renombrado, no borrado
    backup = next((legacy["data"] / "backups").glob("migration_*"))
    assert (backup / "config.json").exists()
    assert (backup / "pending" / "Vocabulary_ab12cd34.pending.txt").exists()
    store.close()


def test_migration_is_idempotent(legacy):
    store, settings = run_migration(legacy)
    store.close()
    store, _ = run_migration(legacy)
    assert len(store.unsaved("en")) == 2
    assert len(list((legacy["data"] / "backups").glob("migration_*"))) == 1
    store.close()


def test_locked_document_is_retried_next_time(legacy, monkeypatch):
    settings = Settings(document_path=str(legacy["doc"]), setup_complete=True)
    store = VocabularyStore(legacy["data"] / "vocabulary.db").open()
    from wordlens.document import word_writer

    def locked(self):
        raise word_writer.DocumentLockedError("open in Word")

    monkeypatch.setattr(word_writer.WordWriter, "read_expressions", locked)
    assert migration.import_document(store, settings) is None
    monkeypatch.undo()
    assert migration.import_document(store, settings) == 2
    store.close()


def test_run_never_raises(legacy, monkeypatch):
    store = VocabularyStore(legacy["data"] / "vocabulary.db").open()
    monkeypatch.setattr(migration, "import_document", lambda *a: 1 / 0)
    migration.run(store, Settings(document_path=str(legacy["doc"]), setup_complete=True))  # sólo registra el error
    store.close()
