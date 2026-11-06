r"""Prueba la extracción y la traducción con frases de ejemplo, sin vídeo ni captura.

Usa los modelos reales de Ollama. No escribe en ningún documento ni toca los
datos de WordLens: muestra los candidatos que irían a la revisión y su traducción.

Ejemplos:
  .venv\Scripts\python.exe tools\try_text.py "He brushed off my concerns and went ahead with the deal."
  .venv\Scripts\python.exe tools\try_text.py --file frases.txt
  .venv\Scripts\python.exe tools\try_text.py --no-translate "You shouldn't take it for granted."
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wordlens.ai.ollama_client import OllamaClient  # noqa: E402
from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer  # noqa: E402
from wordlens.config.settings import load_settings  # noqa: E402
from wordlens.translation.translator import OllamaTranslator, TranslationItem  # noqa: E402

SAMPLES = [
    "He brushed off my concerns and went ahead with the deal.",
    "I can't put up with this anymore.",
    "You really hit the nail on the head.",
    "I have to go to the television store.",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sentences", nargs="*", help="Frases a analizar (por defecto, ejemplos incluidos)")
    parser.add_argument("--file", type=Path, help="Archivo de texto con una frase por línea")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")

    settings = load_settings()
    sentences = list(args.sentences)
    if args.file:
        sentences += [ln.strip() for ln in args.file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    sentences = sentences or SAMPLES

    analyzer = VocabularyAnalyzer(settings)
    translator = OllamaTranslator(
        OllamaClient(settings.ollama_url, settings.ollama_timeout_seconds),
        settings.translation_model or settings.ollama_model,
        source=settings.source_code,
        target=settings.target_code,
    )
    for sentence in sentences:
        t0 = time.perf_counter()
        cands = analyzer.analyze([sentence])
        t1 = time.perf_counter()
        print(f"\n{sentence}   [extracción {t1 - t0:.1f}s]")
        translations = [None] * len(cands)
        if cands and not args.no_translate:
            translations = translator.translate(sentence, [TranslationItem(c.expression, c.kind) for c in cands])
            print(f"   [traducción {time.perf_counter() - t1:.1f}s con {translator.model}]")
        for cand, tr in zip(cands, translations, strict=True):
            print(f"   {cand.expression:28} {cand.kind:13} {tr or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
