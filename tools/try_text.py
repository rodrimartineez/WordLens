"""Prueba el análisis con frases de ejemplo, sin vídeo ni captura de pantalla.

Usa el LLM real (Ollama) y, salvo --dry-run, escribe en el documento Word real
(o en el que indiques con --document) aplicando la deduplicación.

Ejemplos:
  .venv\\Scripts\\python.exe tools\\try_text.py "I couldn't figure out what he was getting at."
  .venv\\Scripts\\python.exe tools\\try_text.py --dry-run "You shouldn't take it for granted."
  .venv\\Scripts\\python.exe tools\\try_text.py --file frases.txt --document C:\\tmp\\prueba.docx
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer  # noqa: E402
from wordlens.config.settings import load_settings  # noqa: E402
from wordlens.document.word_writer import WordWriter  # noqa: E402
from wordlens.vocabulary.deduplicator import Deduplicator  # noqa: E402

SAMPLES = [
    "I couldn't figure out what he was getting at.",
    "You shouldn't take it for granted.",
    "He was completely overwhelmed by the situation.",
    "I went to the supermarket yesterday.",
    "I couldn't make out what she was saying.",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sentences", nargs="*", help="Frases a analizar (por defecto, ejemplos incluidos)")
    parser.add_argument("--file", type=Path, help="Archivo de texto con una frase por línea")
    parser.add_argument("--document", help="Documento Word de destino")
    parser.add_argument("--dry-run", action="store_true", help="No escribir en Word")
    parser.add_argument("--batch", type=int, default=None, help="Líneas por llamada al LLM")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    settings = load_settings()
    if args.document:
        settings.document_path = args.document
    sentences = list(args.sentences)
    if args.file:
        sentences += [ln.strip() for ln in args.file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    sentences = sentences or SAMPLES
    batch_size = args.batch or settings.batch_max_lines

    analyzer = VocabularyAnalyzer(settings)
    writer = None if args.dry_run else WordWriter(settings.document, settings.backup_count)
    dedup = Deduplicator()
    if writer:
        writer.ensure_exists()
        dedup.reset(writer.read_expressions())
        print(f"Documento: {settings.document} ({len(dedup)} expresiones existentes)\n")

    total_written = 0
    for i in range(0, len(sentences), batch_size):
        batch = sentences[i : i + batch_size]
        items = analyzer.analyze(batch)
        for line in batch:
            print(f"> {line}")
        if not items:
            print("   (nada relevante)")
        for item in items:
            status = "duplicado" if dedup.contains(item.expression) else "nuevo"
            print(f"   {item.expression} — {item.translation}   [{status}]")
        if writer:
            written = writer.add_entries(dedup.filter_new(items))
            for w in written:
                dedup.add(w.expression)
            total_written += len(written)
        print()
    if writer:
        print(f"Entradas añadidas al documento: {total_written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
