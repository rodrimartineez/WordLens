import json
import os
from pathlib import Path


from wordlens.config import paths
from wordlens.config.settings import (
    LEGACY_APP_NAME,
    Settings,
    load_settings,
    resolve_document_path,
    save_settings,
)
from wordlens.document.word_writer import BACKUP_DIR_NAME, WordWriter
from wordlens.vocabulary.models import VocabularyItem


def test_data_dir_is_isolated_from_code_and_install_dir():
    # conftest fija WORDLENS_DATA_DIR: los tests nunca tocan datos reales.
    assert paths.DATA_DIR == Path(os.environ["WORDLENS_DATA_DIR"])
    assert paths.CONFIG_PATH.parent == paths.DATA_DIR
    for folder in (paths.LOG_DIR, paths.BACKUP_DIR, paths.PENDING_DIR):
        assert folder.parent == paths.DATA_DIR
    assert not paths.DATA_DIR.is_relative_to(paths.PROJECT_ROOT)


def test_first_run_defaults():
    s = Settings()
    assert s.setup_complete is False
    assert s.document_path == ""
    assert s.document == paths.default_document()
    assert s.document.name == "Vocabulary.docx"
    assert (s.source_language, s.target_language) == ("English", "Spanish")


def test_missing_config_means_first_run(tmp_path):
    s = load_settings(tmp_path / "config.json")
    assert s.setup_complete is False


def test_no_developer_specific_paths_in_defaults():
    s = Settings()
    assert "Proyectos" not in str(s.document)
    assert s.document.is_absolute()


def test_document_paths():
    assert resolve_document_path(r"D:\Inglés\Series B2-C1.docx") == Path(r"D:\Inglés\Series B2-C1.docx")
    assert resolve_document_path("WordLens/v.docx") == paths.documents_dir() / "WordLens" / "v.docx"


def test_default_bands_are_thirds():
    s = Settings()
    assert s.top_band_fraction == 0.33 and s.bottom_band_fraction == 0.33


def test_language_validation():
    assert Settings(source_language="Spanish").target_language == "English"  # nunca el mismo idioma
    assert Settings(source_language="Klingon").source_language == "English"
    s = Settings(source_language="Spanish", target_language="English")
    assert (s.source_code, s.target_code, s.ocr_tag) == ("es", "en", "es-ES")
    assert Settings().ocr_tag == "en-US"
    # Una etiqueta OCR forzada que no corresponde al idioma de origen se ignora.
    assert Settings(source_language="Spanish", target_language="English", ocr_language="en-US").ocr_tag == "es-ES"
    assert Settings(ocr_language="en-GB").ocr_tag == "en-GB"


def write_config(tmp_path, **values) -> Path:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(values), encoding="utf-8")
    return cfg


def test_roundtrip_persists_setup(tmp_path):
    cfg = tmp_path / "config.json"
    s = Settings(document_path=r"D:\Inglés\Series B2-C1.docx", source_language="Spanish",
                 target_language="English", setup_complete=True)
    save_settings(s, cfg)
    loaded = load_settings(cfg)
    assert loaded.setup_complete is True
    assert loaded.document == Path(r"D:\Inglés\Series B2-C1.docx")
    assert (loaded.source_language, loaded.target_language) == ("Spanish", "English")


def test_migrates_old_values(tmp_path):
    cfg = write_config(
        tmp_path, app_name=LEGACY_APP_NAME, top_band_fraction=0.25, bottom_band_fraction=0.3, ocr_language="en-US"
    )
    s = load_settings(cfg)
    assert s.app_name == "WordLens"
    assert (s.top_band_fraction, s.bottom_band_fraction) == (0.33, 0.33)
    assert s.ocr_language == "auto"
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["top_band_fraction"] == 0.33
    assert "setup_complete" in saved  # se añaden las claves nuevas


def test_custom_values_are_kept(tmp_path):
    cfg = write_config(tmp_path, app_name="MiLector", document_path=str(tmp_path / "mio.docx"),
                       top_band_fraction=0.2, bottom_band_fraction=0.4)
    s = load_settings(cfg)
    assert s.app_name == "MiLector"
    assert s.document == tmp_path / "mio.docx"
    assert (s.top_band_fraction, s.bottom_band_fraction) == (0.2, 0.4)


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{ not json", encoding="utf-8")
    assert load_settings(cfg) == Settings()


def test_backups_go_to_data_folder_not_next_to_document(tmp_path):
    doc = tmp_path / "Inglés" / "Vocabulario.docx"
    backups = tmp_path / "appdata" / "backups"
    writer = WordWriter(doc, backup_dir=backups)
    writer.add_entries([VocabularyItem("figure out", "averiguar")])
    backup = writer.backup()
    assert backup.parent == backups
    assert not (doc.parent / BACKUP_DIR_NAME).exists()
    assert sorted(p.name for p in doc.parent.iterdir()) == ["Vocabulario.docx"]


def test_default_document_avoids_protected_documents_folder(monkeypatch):
    monkeypatch.setattr(paths, "controlled_folder_access_enabled", lambda: True)
    assert paths.default_document() == Path.home() / "WordLens" / "Vocabulary.docx"
    monkeypatch.setattr(paths, "controlled_folder_access_enabled", lambda: False)
    assert paths.default_document() == paths.documents_dir() / "WordLens" / "Vocabulary.docx"


def test_check_writable(tmp_path):
    writer = WordWriter(tmp_path / "v.docx")
    writer.check_writable()  # no lanza y no deja rastro
    assert list(tmp_path.iterdir()) == []
    blocked = WordWriter(tmp_path / "no-existe" / "v.docx")
    try:
        blocked.check_writable()
        raise AssertionError("debería fallar")
    except OSError:
        pass
