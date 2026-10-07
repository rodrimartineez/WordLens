"""Comprobación rápida de las piezas que más fallan al empaquetar.

  WordLens.exe --self-test      (lo ejecuta build.ps1 tras generar el ejecutable)

Usa los mismos módulos que la aplicación: OCR de Windows (inglés y español),
datos de wordfreq, plantillas de python-docx, captura de pantalla y Ollama.
No escribe nada fuera de una carpeta temporal. Devuelve 0 si todo va bien.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

log = logging.getLogger("wordlens.selftest")


def run() -> int:
    from PIL import Image, ImageDraw, ImageFont

    from wordlens.ai import ollama_setup
    from wordlens.ai.ollama_client import OllamaClient
    from wordlens.capture.screen_capture import ScreenCapturer, prepare_for_ocr
    from wordlens.capture.subtitle_detector import extract_subtitles
    from wordlens.config.paths import ICON_PATH
    from wordlens.config.settings import load_settings
    from wordlens.document.word_writer import WordWriter
    from wordlens.ocr.ocr_engine import WindowsOcr, available_languages
    from wordlens.vocabulary.extractor import validate_item
    from wordlens.vocabulary.models import VocabularyItem

    failures = 0

    def result(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        log.info("SELFTEST %s %s %s", "OK  " if ok else "FAIL", name, detail)

    result("icon", ICON_PATH.exists(), str(ICON_PATH))
    langs = available_languages()
    for tag, text in (("en-US", "I couldn't figure out what he was getting at."),
                      ("es-ES", "No me di cuenta de lo que estaba pasando.")):
        if tag not in langs:
            result(f"ocr {tag}", tag != "en-US", "language pack not installed")
            continue
        try:
            img = Image.new("RGB", (1920, 300), (40, 70, 110))
            d = ImageDraw.Draw(img)
            font = ImageFont.truetype("arial.ttf", 46)
            w = d.textlength(text, font=font)
            d.text(((1920 - w) / 2, 180), text, font=font, fill="white", stroke_width=3, stroke_fill="black")
            ocr = WindowsOcr(tag)
            prepared, _ = prepare_for_ocr(img)
            subs = extract_subtitles(ocr.recognize(prepared), prepared.width, language=tag[:2])
            ocr.close()
            result(f"ocr {tag}", subs == [text], repr(subs))
        except Exception as exc:  # noqa: BLE001
            result(f"ocr {tag}", False, repr(exc))

    try:
        item = validate_item({"expression": "figure out", "translation": "averiguar"},
                             "I couldn't figure it out.")
        result("wordfreq + validation", item is not None)
    except Exception as exc:  # noqa: BLE001
        result("wordfreq + validation", False, repr(exc))

    try:
        with tempfile.TemporaryDirectory() as tmp:
            writer = WordWriter(Path(tmp) / "selftest.docx", backup_dir=Path(tmp) / "b")
            writer.ensure_exists()
            writer.add_entries([VocabularyItem("figure out", "averiguar")])
            result("docx", writer.read_expressions() == ["figure out"])
    except Exception as exc:  # noqa: BLE001
        result("docx", False, repr(exc))

    try:
        cap = ScreenCapturer()
        shots = cap.grab()
        cap.close()
        result("screen capture", len(shots) == 2, str([s.image.size for s in shots]))
    except Exception as exc:  # noqa: BLE001
        result("screen capture", False, repr(exc))

    settings = load_settings()
    status = ollama_setup.check(OllamaClient(settings.ollama_url, 10), settings.ollama_model, autostart=False)
    # Ollama es externo: se informa, pero no hace fallar la prueba del ejecutable.
    log.info("SELFTEST INFO ollama %s: %s", status.state.value, status.message)

    log.info("SELFTEST %s (%d fallo(s))", "PASSED" if not failures else "FAILED", failures)
    return 1 if failures else 0
