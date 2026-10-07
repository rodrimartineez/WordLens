import json
from pathlib import Path

from wordlens.app import WordLensApp
from wordlens.config.settings import (
    LEGACY_APP_NAME,
    PROJECT_ROOT,
    Settings,
    load_settings,
    resolve_project_path,
    to_project_relative,
)
from wordlens.document.word_writer import BACKUP_DIR_NAME, DocumentLockedError, WordWriter
from wordlens.vocabulary.models import VocabularyItem


def test_default_document_is_inside_project():
    s = Settings()
    assert s.document_path == "Vocabulario.docx"  # relativa: sobrevive a mover la carpeta
    assert s.document == PROJECT_ROOT / "Vocabulario.docx"


def test_default_bands_are_thirds():
    s = Settings()
    assert s.top_band_fraction == 0.33 and s.bottom_band_fraction == 0.33


def test_relative_and_absolute_paths():
    assert resolve_project_path("docs/v.docx") == PROJECT_ROOT / "docs" / "v.docx"
    assert resolve_project_path(r"D:\otro\v.docx") == Path(r"D:\otro\v.docx")
    assert to_project_relative(PROJECT_ROOT / "Vocabulario.docx") == "Vocabulario.docx"
    assert to_project_relative(Path(r"D:\otro\v.docx")) == r"D:\otro\v.docx"


def write_config(tmp_path, **values) -> Path:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(values), encoding="utf-8")
    return cfg


def test_migrates_old_documents_folder_default(tmp_path):
    old = str(Path.home() / "Documents" / LEGACY_APP_NAME / "Vocabulario.docx")
    cfg = write_config(tmp_path, document_path=old, top_band_fraction=0.25, bottom_band_fraction=0.3)
    s = load_settings(cfg)
    assert s.document == PROJECT_ROOT / "Vocabulario.docx"
    assert (s.top_band_fraction, s.bottom_band_fraction) == (0.33, 0.33)
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["document_path"] == "Vocabulario.docx"
    assert saved["top_band_fraction"] == 0.33


def test_migrates_config_from_vocablens_version(tmp_path):
    """config.json de la versión anterior: nombre antiguo y ruta absoluta dentro
    de la carpeta del proyecto con su nombre antiguo (Proyectos/VocabLens)."""
    old_doc = PROJECT_ROOT.parent / LEGACY_APP_NAME / "Vocabulario.docx"
    cfg = write_config(tmp_path, app_name=LEGACY_APP_NAME, document_path=str(old_doc))
    s = load_settings(cfg)
    assert s.app_name == "WordLens"
    assert s.document_path == "Vocabulario.docx"
    assert s.document == PROJECT_ROOT / "Vocabulario.docx"


def test_custom_app_name_is_kept(tmp_path):
    assert load_settings(write_config(tmp_path, app_name="MiLector")).app_name == "MiLector"


def test_absolute_path_inside_project_becomes_relative(tmp_path):
    cfg = write_config(tmp_path, document_path=str(PROJECT_ROOT / "Vocabulario.docx"))
    assert load_settings(cfg).document_path == "Vocabulario.docx"


def test_external_document_and_custom_bands_are_respected(tmp_path):
    cfg = write_config(
        tmp_path, document_path=str(tmp_path / "mio.docx"), top_band_fraction=0.2, bottom_band_fraction=0.4
    )
    s = load_settings(cfg)
    assert s.document == tmp_path / "mio.docx"
    assert (s.top_band_fraction, s.bottom_band_fraction) == (0.2, 0.4)


def test_backups_and_pending_go_next_to_document(tmp_path, monkeypatch):
    import os

    doc = tmp_path / "proyecto" / "Vocabulario.docx"
    writer = WordWriter(doc)
    writer.add_entries([VocabularyItem("figure out", "averiguar")])
    backup = writer.backup()
    assert backup.parent == doc.parent / BACKUP_DIR_NAME

    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError()))
    try:
        writer.add_entries([VocabularyItem("get at", "insinuar")])
    except DocumentLockedError:
        pass
    app = WordLensApp(Settings(document_path=str(doc), notifications=False))
    app.writer = writer
    app._flush_pending(final=True)
    pending = doc.parent / "Vocabulario.pendientes.txt"
    assert pending.read_text(encoding="utf-8").strip() == "get at — insinuar"
