"""Subtítulos en español (traducción al inglés): la misma cadena con datos del español."""

import pytest
from PIL import Image, ImageDraw, ImageFont

from wordlens.ai.prompts import build_messages
from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer
from wordlens.capture.screen_capture import prepare_for_ocr
from wordlens.capture.subtitle_detector import extract_subtitles, language_ratio
from wordlens.config.settings import Settings
from wordlens.document.word_writer import WordWriter
from wordlens.ocr.ocr_engine import OcrLine, WindowsOcr, available_languages
from wordlens.vocabulary.deduplicator import Deduplicator
from wordlens.vocabulary.extractor import is_trivial_line, validate_item
from wordlens.vocabulary.models import VocabularyItem
from wordlens.vocabulary.normalization import dedup_key, normalize_text, tokenize

ES = dict(source="es", target="en")


def test_accents_are_kept():
    assert normalize_text("¿Qué está pasando, Muñoz?") == "qué está pasando muñoz"
    assert tokenize("Él no se dio cuenta") == ["él", "no", "se", "dio", "cuenta"]
    # Inglés sin cambios
    assert normalize_text("I couldn't figure it out!") == "i couldn't figure it out"


def test_language_ratio():
    assert language_ratio("No me di cuenta de lo que estaba pasando", "es") >= 0.9
    assert language_ratio("I couldn't figure out what he was getting at", "es") < 0.5
    assert language_ratio("I couldn't figure out what he was getting at", "en") >= 0.9


def test_extract_spanish_subtitles_and_reject_english():
    lines = [
        OcrLine("No me di cuenta de lo que", 700, 200, 520, 40),
        OcrLine("estaba pasando.", 820, 250, 280, 40),
        OcrLine("Press any key to continue", 700, 20, 500, 30),
    ]
    assert extract_subtitles(lines, 1920, language="es") == ["No me di cuenta de lo que estaba pasando."]


def test_dedup_keys():
    assert dedup_key("Echar de menos", "es") == dedup_key("echar de menos", "es")
    assert dedup_key("el arrebato", "es") == "arrebato"
    assert dedup_key("darse cuenta", "es") != dedup_key("dar cuenta", "es")
    d = Deduplicator(["Echar de menos"], lang="es")
    assert d.contains("echar de menos")
    assert not d.contains("tomar el pelo")


class TestValidateSpanish:
    def test_pronominal_verb_grounded_in_conjugated_form(self):
        item = validate_item({"expression": "darse cuenta", "translation": "to realize"},
                             "No me di cuenta de nada.", **ES)
        assert item == VocabularyItem("darse cuenta", "to realize", "No me di cuenta de nada.")

    def test_idiom(self):
        item = validate_item({"expression": "Echar de menos", "translation": "to miss"},
                             "Te echo mucho de menos.", **ES)
        assert item is not None and item.expression == "echar de menos"

    def test_advanced_word_with_accent(self):
        item = validate_item({"expression": "agobiado", "translation": "overwhelmed / stressed out"},
                             "Estaba completamente agobiado.", **ES)
        assert item is not None

    @pytest.mark.parametrize(
        "expression,translation,context",
        [
            ("casa", "house", "Vamos a casa ahora."),  # palabra básica
            ("situación", "situation", "Es una situación difícil."),  # cognado transparente
            ("tomar el pelo", "to pull someone's leg", "Estaba muy cansado."),  # no está en el subtítulo
            ("yo no lo sé", "I don't know", "Yo no lo sé."),  # frase completa
        ],
    )
    def test_rejected(self, expression, translation, context):
        assert validate_item({"expression": expression, "translation": translation}, context, **ES) is None


def test_trivial_spanish_line():
    assert is_trivial_line("Hola, ¿qué tal?", "es")
    assert not is_trivial_line("Estaba completamente agobiado por la situación.", "es")


def test_prompt_uses_spanish_examples():
    msgs = build_messages(["Te echo de menos."], source_language="Spanish", target_language="English")
    assert "Spanish subtitles for a English speaker" in msgs[0]["content"]
    assert "darse cuenta" in msgs[2]["content"]
    en = build_messages(["x"])
    assert "figure out" in en[2]["content"]


def test_writer_dedups_with_spanish_rules(tmp_path):
    w = WordWriter(tmp_path / "es.docx", lang="es")
    assert len(w.add_entries([VocabularyItem("echar de menos", "to miss")])) == 1
    assert w.add_entries([VocabularyItem("Echar de menos", "to miss")]) == []


@pytest.mark.skipif("es-ES" not in available_languages(), reason="Falta el OCR de español de Windows")
def test_real_spanish_ocr():
    img = Image.new("RGB", (1920, 300), (40, 70, 110))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("arial.ttf", 46)
    text = "¿Por qué no me dijiste que estabas agobiado?"
    w = d.textlength(text, font=font)
    d.text(((1920 - w) / 2, 180), text, font=font, fill="white", stroke_width=3, stroke_fill="black")
    ocr = WindowsOcr("es-ES")
    prepared, _ = prepare_for_ocr(img)
    assert extract_subtitles(ocr.recognize(prepared), prepared.width, language="es") == [text]
    ocr.close()


@pytest.mark.llm
def test_llm_spanish_to_english():
    s = Settings(source_language="Spanish", target_language="English")
    items = VocabularyAnalyzer(s).analyze(["No me di cuenta de lo que estaba pasando.", "Te echo mucho de menos."])
    exprs = {i.expression for i in items}
    assert exprs & {"darse cuenta", "echar de menos"}
