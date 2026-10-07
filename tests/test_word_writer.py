import os

import docx
import pytest

from vocablens.document.word_writer import (
    BACKUP_DIR_NAME,
    DocumentError,
    DocumentLockedError,
    WordWriter,
    parse_entry,
)
from vocablens.vocabulary.deduplicator import Deduplicator
from vocablens.vocabulary.models import VocabularyItem as V


def test_creates_document_and_folders(tmp_path):
    path = tmp_path / "sub" / "folder" / "vocab.docx"
    w = WordWriter(path)
    assert w.ensure_exists() is True
    assert path.exists()
    assert w.ensure_exists() is False  # no lo recrea


def test_format_bold_expression_normal_translation(tmp_path):
    path = tmp_path / "v.docx"
    w = WordWriter(path)
    w.ensure_exists()
    w.add_entries([V("figure out", "averiguar / descubrir"), V("get at", "insinuar / querer decir")])
    paras = [p for p in docx.Document(str(path)).paragraphs if p.text]
    assert [p.text for p in paras] == [
        "figure out — averiguar / descubrir",
        "get at — insinuar / querer decir",
    ]
    for p in paras:
        assert p.runs[0].bold is True
        assert all(not r.bold for r in p.runs[1:])
        assert "—" in p.runs[1].text


def test_appends_to_end_and_preserves_existing_content(tmp_path):
    path = tmp_path / "existing.docx"
    d = docx.Document()
    d.add_heading("Mi vocabulario", 1)
    p = d.add_paragraph()
    p.add_run("Take for granted").bold = True
    p.add_run(" — dar por sentado")
    d.add_paragraph("Una nota libre sin separador")
    d.save(str(path))

    w = WordWriter(path)
    w.ensure_exists()
    assert "Take for granted" in w.read_expressions()
    written = w.add_entries([V("take it for granted", "dar por hecho"), V("overwhelmed", "abrumado")])
    assert [x.expression for x in written] == ["overwhelmed"]
    texts = [p.text for p in docx.Document(str(path)).paragraphs]
    assert texts[0] == "Mi vocabulario"
    assert texts[-1] == "overwhelmed — abrumado"
    assert "Una nota libre sin separador" in texts


@pytest.mark.parametrize("variant", ["figure out", "Figure out", "FIGURE OUT", "figure it out"])
def test_no_duplicates_case_and_pronouns(tmp_path, variant):
    path = tmp_path / "v.docx"
    w = WordWriter(path)
    w.add_entries([V("figure out", "averiguar")])
    assert w.add_entries([V(variant, "averiguar")]) == []
    assert w.read_expressions() == ["figure out"]


def test_duplicates_within_batch(tmp_path):
    w = WordWriter(tmp_path / "v.docx")
    written = w.add_entries([V("make out", "distinguir"), V("Make out", "entender")])
    assert len(written) == 1


def test_different_meanings_are_kept(tmp_path):
    w = WordWriter(tmp_path / "v.docx")
    written = w.add_entries([V("get at", "insinuar"), V("get out", "salir"), V("make out", "distinguir")])
    assert len(written) == 3


def test_reads_manual_edits_between_writes(tmp_path):
    path = tmp_path / "v.docx"
    w = WordWriter(path)
    w.add_entries([V("figure out", "averiguar")])
    d = docx.Document(str(path))
    d.add_paragraph("long shot – posibilidad remota")  # editado a mano con otro guion
    d.save(str(path))
    assert w.add_entries([V("long shot", "algo improbable")]) == []


def test_corrupt_file_is_never_overwritten(tmp_path):
    path = tmp_path / "v.docx"
    path.write_bytes(b"esto no es un docx")
    w = WordWriter(path)
    with pytest.raises(DocumentError):
        w.ensure_exists()
    with pytest.raises(DocumentError):
        w.add_entries([V("figure out", "averiguar")])
    assert path.read_bytes() == b"esto no es un docx"


def test_backup_rotation(tmp_path):
    path = tmp_path / "v.docx"
    w = WordWriter(path, backup_count=2)
    w.add_entries([V("figure out", "averiguar")])
    import time

    for _ in range(3):
        assert w.backup() is not None
        time.sleep(1.05)  # nombres con resolución de segundos
    backups = list((tmp_path / BACKUP_DIR_NAME).glob("*.docx"))
    assert len(backups) == 2


def test_locked_document_keeps_entries_pending(tmp_path, monkeypatch):
    path = tmp_path / "v.docx"
    w = WordWriter(path)
    w.ensure_exists()

    real_replace = os.replace

    def locked(*_a, **_k):
        raise PermissionError("locked by Word")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(DocumentLockedError):
        w.add_entries([V("figure out", "averiguar")])
    assert w.pending_count == 1
    assert not any(p.name.startswith("~vocablens") for p in tmp_path.iterdir())  # sin temporales

    monkeypatch.setattr(os, "replace", real_replace)
    written = w.add_entries([])  # reintento
    assert [x.expression for x in written] == ["figure out"]
    assert w.pending_count == 0


def test_parse_entry():
    assert parse_entry("figure out — averiguar") == "figure out"
    assert parse_entry("figure out - averiguar") == "figure out"
    assert parse_entry("solo texto", bold_text="") is None
    assert parse_entry("overwhelmed abrumado", bold_text="overwhelmed") == "overwhelmed"
    assert parse_entry("   ") is None


def test_deduplicator_roundtrip(tmp_path):
    d = Deduplicator(["Figure out", "take it for granted"])
    assert d.contains("figure out")
    assert d.contains("take for granted")
    assert not d.contains("get at")
    assert d.add("get at") is True
    assert d.add("Get at") is False
    items = d.filter_new([V("get at", "x"), V("make out", "y"), V("MAKE OUT", "z")])
    assert [i.expression for i in items] == ["make out"]
