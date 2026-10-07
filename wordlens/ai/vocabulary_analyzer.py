"""Frases de subtítulo -> elementos de vocabulario validados."""

from __future__ import annotations

import logging
import time

from wordlens.config.settings import Settings
from wordlens.vocabulary.extractor import is_trivial_line, parse_llm_json, validate_item
from wordlens.vocabulary.models import VocabularyItem

from .ollama_client import OllamaClient
from .prompts import RESPONSE_SCHEMA, build_messages

log = logging.getLogger(__name__)


class VocabularyAnalyzer:
    def __init__(self, settings: Settings, client: OllamaClient | None = None) -> None:
        self.settings = settings
        self.client = client or OllamaClient(settings.ollama_url, settings.ollama_timeout_seconds)

    def analyze(self, lines: list[str]) -> list[VocabularyItem]:
        """Envía un lote de frases al LLM y devuelve sólo elementos válidos.

        Lanza OllamaError si el servidor falla; nunca por una respuesta mal formada.
        """
        s = self.settings
        lines = [ln.strip() for ln in lines if ln and ln.strip()]
        if s.prefilter_trivial_lines:
            skipped = [ln for ln in lines if is_trivial_line(ln, s.source_code)]
            if skipped:
                log.info("Líneas triviales omitidas: %s", skipped)
            lines = [ln for ln in lines if not is_trivial_line(ln, s.source_code)]
        if not lines:
            return []

        messages = build_messages(
            lines,
            target_level=s.target_level,
            source_language=s.source_language,
            target_language=s.target_language,
        )
        start = time.perf_counter()
        response = self.client.chat(
            s.ollama_model,
            messages,
            schema=RESPONSE_SCHEMA,
            temperature=s.temperature,
            num_predict=120 + 80 * len(lines),
            keep_alive=s.ollama_keep_alive,
        )
        raw = response.get("message", {}).get("content", "")
        log.info("LLM %.1fs para %d línea(s): %s", time.perf_counter() - start, len(lines), raw)

        items: list[VocabularyItem] = []
        for raw_item in parse_llm_json(raw):
            context = self._context_for(raw_item, lines)
            item = validate_item(
                raw_item,
                context,
                max_single_word_zipf=s.max_single_word_zipf,
                max_words=s.max_expression_words,
                grounding_text=" ".join(lines),
                source=s.source_code,
                target=s.target_code,
            )
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _context_for(raw_item: dict, lines: list[str]) -> str:
        idx = raw_item.get("line")
        if isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= len(lines):
            return lines[idx - 1]
        # Número de línea ausente o erróneo: se valida contra todo el lote.
        return " ".join(lines)
