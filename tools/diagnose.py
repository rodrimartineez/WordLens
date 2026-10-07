"""Comprueba que todas las piezas funcionan en este equipo.

  .venv\\Scripts\\python.exe tools\\diagnose.py
  .venv\\Scripts\\python.exe tools\\diagnose.py --skip-llm

Usa la configuración de desarrollo (.devdata). La aplicación instalada tiene su
propia comprobación: WordLens.exe --self-test (ver README > Build).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OK, FAIL, WARN = "[ OK ]", "[FALLO]", "[AVISO]"
results: list[bool] = []


def check(name: str):
    def deco(fn):
        def run(*a, **kw):
            print(f"\n== {name}")
            try:
                ok = fn(*a, **kw)
                results.append(ok is not False)
            except Exception as exc:  # noqa: BLE001
                print(f"{FAIL} {exc}")
                traceback.print_exc(limit=2)
                results.append(False)
        return run
    return deco


@check("Python y paquetes")
def check_python():
    print(f"{OK} Python {sys.version.split()[0]} ({sys.executable})")
    import docx, mss, numpy, PIL, pystray, wordfreq  # noqa: E401,F401
    import winrt.windows.media.ocr  # noqa: F401
    print(f"{OK} Paquetes importados correctamente")


@check("OCR de Windows")
def check_ocr(settings):
    from PIL import Image, ImageDraw, ImageFont

    from wordlens.capture.screen_capture import prepare_for_ocr
    from wordlens.capture.subtitle_detector import extract_subtitles
    from wordlens.ocr.ocr_engine import WindowsOcr, available_languages

    langs = available_languages()
    print(f"{OK} Idiomas OCR instalados: {langs}")
    ocr = WindowsOcr(settings.ocr_tag)
    img = Image.new("RGB", (1920, 300), (40, 70, 110))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("arial.ttf", 48)
    text = "I couldn't figure out what he was getting at."
    w = d.textlength(text, font=font)
    d.text(((1920 - w) / 2, 180), text, font=font, fill="white", stroke_width=3, stroke_fill="black")
    prepared, _ = prepare_for_ocr(img, settings.ocr_preprocess)
    t0 = time.perf_counter()
    lines = ocr.recognize(prepared)
    ms = (time.perf_counter() - t0) * 1000
    subs = extract_subtitles(lines, prepared.width)
    print(f"   Reconocido: {subs} ({ms:.0f} ms)")
    if subs and subs[0].lower().rstrip(".") == text.lower().rstrip("."):
        print(f"{OK} OCR exacto")
    else:
        print(f"{FAIL} El OCR no coincide con el texto esperado")
        return False


@check("Captura de pantalla")
def check_capture(settings):
    from wordlens.capture.screen_capture import ScreenCapturer, enable_dpi_awareness

    enable_dpi_awareness()
    cap = ScreenCapturer(settings.monitor, settings.top_band_fraction, settings.bottom_band_fraction)
    mon = cap.monitor
    print(f"{OK} Monitor {settings.monitor}: {mon['width']}x{mon['height']}")
    t0 = time.perf_counter()
    shots = cap.grab()
    ms = (time.perf_counter() - t0) * 1000
    for s in shots:
        print(f"   Franja {s.band.name}: {s.image.size} (sin guardar en disco)")
    print(f"{OK} Captura de {len(shots)} franjas en {ms:.0f} ms")
    cap.close()


@check("Ollama y modelo")
def check_ollama(settings, skip_llm: bool):
    from wordlens.ai.ollama_client import OllamaClient
    from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer

    client = OllamaClient(settings.ollama_url, settings.ollama_timeout_seconds)
    print(f"{OK} Ollama {client.version()} en {settings.ollama_url}")
    if not client.has_model(settings.ollama_model):
        print(f"{FAIL} Falta el modelo. Ejecuta:  ollama pull {settings.ollama_model}")
        return False
    print(f"{OK} Modelo disponible: {settings.ollama_model}")
    if skip_llm:
        return True
    print("   Cargando el modelo y analizando una frase (la primera vez puede tardar ~1 min)...")
    t0 = time.perf_counter()
    items = VocabularyAnalyzer(settings, client).analyze(["I couldn't figure out what he was getting at."])
    print(f"   {time.perf_counter() - t0:.1f}s -> {[(i.expression, i.translation) for i in items]}")
    if any(i.expression == "figure out" for i in items):
        print(f"{OK} El LLM detecta 'figure out'")
    else:
        print(f"{WARN} El LLM no devolvió 'figure out' (posible variabilidad del modelo)")


@check("Documento Word (en carpeta temporal)")
def check_document():
    from wordlens.document.word_writer import WordWriter
    from wordlens.vocabulary.models import VocabularyItem

    with tempfile.TemporaryDirectory() as tmp:
        w = WordWriter(Path(tmp) / "prueba.docx")
        w.ensure_exists()
        first = w.add_entries([VocabularyItem("figure out", "averiguar / descubrir")])
        second = w.add_entries([VocabularyItem("Figure Out", "averiguar")])
        exprs = w.read_expressions()
        print(f"   1ª escritura: {len(first)} | 2ª (duplicado): {len(second)} | contenido: {exprs}")
        if len(first) == 1 and not second and exprs == ["figure out"]:
            print(f"{OK} Escritura y deduplicación correctas")
        else:
            return False


@check("Atajos globales")
def check_hotkeys(settings):
    from wordlens.input.hotkeys import parse_hotkey, user32

    for spec in (settings.hotkey_start, settings.hotkey_stop):
        mods, vk = parse_hotkey(spec)
        if user32.RegisterHotKey(None, 0x7F00, mods, vk):
            user32.UnregisterHotKey(None, 0x7F00)
            print(f"{OK} {spec} disponible")
        else:
            print(f"{WARN} {spec} no disponible (¿WordLens ya en marcha u otra app lo usa?)")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()
    from wordlens.config.settings import load_settings

    settings = load_settings()
    print(f"Configuración: modelo={settings.ollama_model} documento={settings.document}")
    check_python()
    check_ocr(settings)
    check_capture(settings)
    check_document()
    check_hotkeys(settings)
    check_ollama(settings, args.skip_llm)
    failed = results.count(False)
    print("\n" + ("Todo correcto." if not failed else f"{failed} comprobación(es) con fallos."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
