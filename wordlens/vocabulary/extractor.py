"""Parseo y validación de la respuesta del LLM.

El modelo puede devolver JSON roto, claves extra, frases enteras, palabras básicas
o expresiones inventadas que no están en el subtítulo. Aquí se filtra todo eso de
forma determinista antes de tocar el documento.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from wordfreq import zipf_frequency

from .languages import TRANSLATION_STOPWORDS, profile
from .models import VocabularyItem
from .normalization import (
    canonical_expression,
    dedup_key,
    fold,
    lemma,
    tokenize,
)

log = logging.getLogger(__name__)

_PLACEHOLDER_TOKENS = frozenset(
    "one's oneself someone somebody something sb sth it him her them me us you".split()
)


def parse_llm_json(raw: str) -> list[dict[str, Any]]:
    """Extrae la lista "items" de la respuesta. Nunca lanza excepciones."""
    if not raw or not isinstance(raw, str):
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                data = None
    if data is None:
        log.warning("Respuesta del LLM no es JSON válido: %.200r", raw)
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items", [])
    else:
        return []
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


def clean_translation(text: str) -> str:
    text = fold(text).strip()
    text = re.sub(r"\s*\(.*?\)\s*", " ", text)  # explicaciones entre paréntesis
    text = re.sub(r"\s*[,;]\s*", " / ", text) if "/" not in text else text
    parts = [p.strip(" \t\"'.:-—–") for p in text.split("/")]
    seen: list[str] = []
    for p in parts:
        p = re.sub(r"\s+", " ", p)
        if p and p.lower() not in (s.lower() for s in seen):
            seen.append(p)
    return " / ".join(seen[:2])


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn"
    )


def is_transparent_cognate(expression: str, translation: str, target: str = "es") -> bool:
    """True si la traducción es casi la misma palabra (supermarket -> supermercado).

    Un cognado transparente no aporta nada a un hispanohablante. Los falsos amigos
    no se ven afectados: su traducción en contexto es otra palabra distinta.
    """
    expr = _strip_accents(expression)
    stopwords = TRANSLATION_STOPWORDS.get(target, frozenset())
    for option in translation.split("/"):
        words = [w for w in re.findall(r"[a-zñ]+", _strip_accents(option)) if w not in stopwords]
        if len(words) != 1:
            continue
        word = words[0]
        if len(os.path.commonprefix([expr, word])) >= 6:
            return True
        if SequenceMatcher(None, expr, word).ratio() >= 0.75:
            return True
    return False


_ES_INFINITIVE = re.compile(r"^(\w+?)(?:ar|er|ir)(?:se|me|te|nos|os|le|la|lo|les)?$")
_ES_PLACEHOLDERS = frozenset("algo alguien uno una se me te nos os le les".split())


def _grounded(expression: str, context: str, lang: str = "en") -> bool:
    """Cada palabra de contenido de la expresión debe aparecer en el subtítulo."""
    ctx_tokens = tokenize(context)
    ctx_forms = set(ctx_tokens) | {lemma(t, lang) for t in ctx_tokens}
    placeholders = _PLACEHOLDER_TOKENS if lang == "en" else _ES_PLACEHOLDERS
    tokens = expression.replace("-", " ").split()
    for i, tok in enumerate(tokens):
        if tok in placeholders:
            continue
        if tok in ctx_forms or lemma(tok, lang) in ctx_forms:
            continue
        # Tolerancia a pequeñas diferencias de flexión/ortografía.
        if len(tok) >= 5 and any(c.startswith(tok[:5]) for c in ctx_tokens):
            continue
        if lang == "es" and i == 0 and len(tokens) > 1 and _es_verb_grounded(tok, ctx_tokens):
            continue
        return False
    return True


def _es_verb_grounded(token: str, ctx_tokens: list[str]) -> bool:
    """Infinitivo español frente a una forma conjugada ("echar" ~ "echo", "darse" ~ "di").

    Se acepta si la raíz aparece en el subtítulo; los verbos muy irregulares
    (raíz de 1-2 letras) se aceptan siempre que el resto de la expresión sí esté.
    """
    m = _ES_INFINITIVE.match(token)
    if not m:
        return False
    stem = m.group(1)
    if len(stem) <= 2:
        return True
    return any(c.startswith(stem[:4]) or c.startswith(stem) for c in ctx_tokens)


def validate_item(
    raw_item: dict[str, Any],
    context: str,
    *,
    max_single_word_zipf: float = 4.5,
    max_words: int = 6,
    grounding_text: str | None = None,
    source: str = "en",
    target: str = "es",
) -> VocabularyItem | None:
    """Devuelve un VocabularyItem limpio o None si no merece guardarse."""
    lang = profile(source)
    expression = raw_item.get("expression")
    translation = raw_item.get("translation")
    if not isinstance(expression, str) or not isinstance(translation, str):
        return None
    if re.search(r"\d", expression):
        return None

    expr = canonical_expression(expression, source)
    trans = clean_translation(translation)
    words = expr.split()

    reason = None
    if not expr or not trans:
        reason = "vacío"
    elif len(words) > max_words or len(expr) > 60:
        reason = "demasiado larga"
    elif len(trans) > 90:
        reason = "traducción demasiado larga"
    elif not lang.expression_pattern.fullmatch(expr):
        reason = "caracteres no válidos"
    elif dedup_key(trans, source) == dedup_key(expr, source):
        reason = "sin traducir"
    elif len(words) >= 3 and words[0] in lang.subject_starters:
        reason = "parece una frase completa"
    elif (grounding_text or context) and not _grounded(expr, grounding_text or context, source):
        reason = "no aparece en el subtítulo"
    elif len(words) == 1:
        if expr in lang.function_words or len(expr) < 3:
            reason = "palabra funcional"
        elif zipf_frequency(expr, source) >= max_single_word_zipf and zipf_frequency(
            lemma(expr, source), source
        ) >= max_single_word_zipf:
            reason = "palabra demasiado básica"
        elif is_transparent_cognate(expr, trans, target):
            reason = "cognado transparente"
    else:
        content = [w for w in words if w not in lang.function_words and w not in _PLACEHOLDER_TOKENS]
        has_particle = any(w in lang.particles for w in words[1:])
        if source == "es" and _ES_INFINITIVE.match(words[0]):
            # Locución verbal ("echar de menos", "dar igual"): el equivalente a un phrasal verb.
            has_particle = True
        if not content:
            reason = "sin palabras de contenido"
        elif (
            len(content) == 1
            and not has_particle
            and zipf_frequency(lemma(content[0], source), source) >= max_single_word_zipf
        ):
            # "for weeks", "eat something": combinación libre de una palabra básica.
            reason = "combinación trivial"

    if reason:
        log.info("Descartado %r (%s)", expression, reason)
        return None
    return VocabularyItem(expression=expr, translation=trans, context=context)


_TRIVIAL_ZIPF = 5.0


def is_trivial_line(text: str, lang: str = "en") -> bool:
    """True si la línea es tan básica que no merece una llamada al LLM.

    Sólo se aplica a líneas cortas ("Where are you going?", "See you tomorrow.")
    sin partículas que puedan formar phrasal verbs.
    """
    tokens = tokenize(text)
    if not tokens:
        return True
    if len(tokens) > 4:
        return False
    prof = profile(lang)
    if any(t in prof.particles for t in tokens[1:]):
        return False
    return all(t in prof.function_words or zipf_frequency(t, lang) >= _TRIVIAL_ZIPF for t in tokens)
