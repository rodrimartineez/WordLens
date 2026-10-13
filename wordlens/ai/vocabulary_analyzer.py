"""Frases de subtítulo -> elementos de vocabulario validados.

1. Primera pasada: el LLM recorre cada línea entera (con pistas de candidatos)
   y devuelve todas las unidades que merecen aprenderse.
2. Segunda pasada (sólo si hace falta): las palabras poco frecuentes de la
   línea que el modelo no llegó a evaluar se le presentan una a una. Corrige el
   fallo típico del modelo pequeño: quedarse con el primer elemento o devolver
   una lista vacía en una línea que sí tiene vocabulario avanzado.
3. Todo pasa por validate_item(); si una unidad contiene a otra de la misma
   línea ("come back" / "come back to bite"), se queda la más larga.
"""

from __future__ import annotations

import logging
import time

from wordlens.config.settings import Settings
from wordlens.vocabulary.candidates import find_candidates
from wordlens.vocabulary.extractor import is_trivial_line, parse_llm_json, validate_item
from wordlens.vocabulary.models import VocabularyItem
from wordlens.vocabulary.normalization import dedup_key, lemma, tokenize

from .ollama_client import OllamaClient
from .prompts import RESPONSE_SCHEMA, build_messages, build_review_messages

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
            hints=s.candidate_hints,
        )
        raw_items = self._ask(messages, lines, "LLM")
        items = self._validate(raw_items, lines)

        if s.review_pass:
            checks = self._unreviewed(raw_items, lines)
            if checks:
                review = build_review_messages(
                    checks,
                    target_level=s.target_level,
                    source_language=s.source_language,
                    target_language=s.target_language,
                )
                checked_lines = [line for line, _ in checks]
                extra = self._validate(self._ask(review, checked_lines, "LLM revisión"), checked_lines)
                known = {dedup_key(i.expression, s.source_code) for i in items}
                items += [i for i in extra if dedup_key(i.expression, s.source_code) not in known]
        return _prefer_longest(items)

    # ------------------------------------------------------------------ pasos
    def _ask(self, messages: list[dict[str, str]], lines: list[str], tag: str) -> list[dict]:
        s = self.settings
        start = time.perf_counter()
        response = self.client.chat(
            s.ollama_model,
            messages,
            schema=RESPONSE_SCHEMA,
            temperature=s.temperature,
            num_predict=160 + 160 * len(lines),  # varias unidades por línea
            keep_alive=s.ollama_keep_alive,
        )
        raw = response.get("message", {}).get("content", "")
        log.info("%s %.1fs para %d línea(s): %s", tag, time.perf_counter() - start, len(lines), raw)
        return parse_llm_json(raw)

    def _validate(self, raw_items: list[dict], lines: list[str]) -> list[VocabularyItem]:
        s = self.settings
        items: list[VocabularyItem] = []
        for raw_item in raw_items:
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

    def _unreviewed(self, raw_items: list[dict], lines: list[str]) -> list[tuple[str, list[str]]]:
        """Palabras poco frecuentes de cada línea que el modelo no ha valorado.

        Lo que el modelo devolvió cuenta como valorado aunque el validador lo
        descartara (un cognado, por ejemplo): no se le vuelve a preguntar.
        """
        lang = self.settings.source_code
        answered: dict[int, set[str]] = {}
        for raw_item in raw_items:
            expression = raw_item.get("expression")
            if not isinstance(expression, str):
                continue
            idx = raw_item.get("line")
            valid_idx = isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= len(lines)
            targets = [idx - 1] if valid_idx else range(len(lines))
            forms = {f for tok in tokenize(expression) for f in (tok, lemma(tok, lang))}
            for t in targets:
                answered.setdefault(t, set()).update(forms)
        checks = []
        for i, line in enumerate(lines):
            seen = answered.get(i, set())
            pending = [
                w
                for w in find_candidates(line, lang).rare_words
                if w not in seen and lemma(w, lang) not in seen and not any(p in seen for p in w.split("-"))
            ]
            if pending:
                checks.append((line, pending))
        return checks

    @staticmethod
    def _context_for(raw_item: dict, lines: list[str]) -> str:
        idx = raw_item.get("line")
        if isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= len(lines):
            return lines[idx - 1]
        # Número de línea ausente o erróneo: se valida contra todo el lote.
        return " ".join(lines)


def _prefer_longest(items: list[VocabularyItem]) -> list[VocabularyItem]:
    """Quita una unidad contenida en otra de la misma línea ("come back" ⊂ "come back to bite")."""

    def contained(small: list[str], big: list[str]) -> bool:
        n = len(small)
        return n < len(big) and any(big[i : i + n] == small for i in range(len(big) - n + 1))

    tokens = [item.expression.split() for item in items]
    return [
        item
        for i, item in enumerate(items)
        if not any(
            j != i and items[j].context == item.context and contained(tokens[i], tokens[j])
            for j in range(len(items))
        )
    ]
