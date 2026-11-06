"""Parseo y validación de la respuesta del modelo de extracción.

El modelo puede devolver JSON roto, frases enteras, palabras gramaticales,
nombres propios o palabras que no están en el subtítulo. Aquí se filtra todo
eso de forma determinista. NO se filtra por dificultad, frecuencia ni interés:
una palabra fácil con significado propio es un candidato válido.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .languages import profile
from .lexicon import is_grammar_construction, is_grammar_word, is_real_word, names_in, word_lemma
from .models import Candidate
from .normalization import canonical_expression, fold, lemma, tokenize

log = logging.getLogger(__name__)

KINDS = ("word", "phrasal verb", "idiom", "collocation", "expression")
_KIND_ALIASES = {
    "phrasal": "phrasal verb",
    "phrasal_verb": "phrasal verb",
    "phrasalverb": "phrasal verb",
    "verb phrase": "phrasal verb",
    "idiomatic expression": "idiom",
    "slang": "word",
    "noun": "word",
    "verb": "word",
    "adjective": "word",
    "adverb": "word",
    "insult": "word",
    "other": "expression",
    "fixed expression": "expression",
    "colloquial expression": "expression",
}

# Adverbios de grado que el modelo arrastra delante de una palabra ("incredibly tacky").
_INTENSIFIERS = frozenset(
    """very really so too pretty quite totally completely utterly incredibly extremely
    truly absolutely highly deeply terribly awfully super fairly rather""".split()
)
_PLACEHOLDER_TOKENS = frozenset(
    "one's oneself someone somebody something sb sth it him her them me us you".split()
)
# Relleno gramatical que no convierte una palabra en expresión.
_FILLER = frozenset(
    "a an the to my your his her our their its this that these those what who which where when why how "
    "i you he she we they it me him us them".split()
)
_ES_PLACEHOLDERS = frozenset("algo alguien uno una se me te nos os le les".split())
_ES_INFINITIVE = re.compile(r"^(\w+?)(?:ar|er|ir)(?:se|me|te|nos|os|le|la|lo|les)?$")


# ----------------------------------------------------------------- parseo
def parse_llm_json(raw: str) -> list[dict[str, Any]]:
    """Extrae la lista "items" de la respuesta. Nunca lanza excepciones."""
    return parse_llm_response(raw)[0]


def parse_llm_response(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    """(items, nombres propios) de la respuesta del modelo. Nunca lanza excepciones."""
    if not raw or not isinstance(raw, str):
        return [], []
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
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
        items = _salvage_items(text)
        log.warning("Respuesta del modelo no es JSON válido (%d elemento(s) recuperados): %.300r", len(items), raw)
        return items, _salvage_names(text)
    names: list[str] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items", [])
        raw_names = data.get("names", [])
        if isinstance(raw_names, list):
            names = [n for n in raw_names if isinstance(n, str)]
    else:
        return [], []
    items = [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []
    return items, names


_JSON_STRING = r'"((?:[^"\\]|\\.)*)"'
_FIELD = re.compile(r'"(line|expression|type|translation)"\s*:\s*(?:' + _JSON_STRING + r"|(-?\d+))")


def _salvage_items(text: str) -> list[dict[str, Any]]:
    """Recupera los elementos completos de un JSON mal formado o cortado.

    Los campos se leen en orden y una clave repetida marca el comienzo del
    siguiente elemento: un elemento roto no arrastra a los que están bien.
    """
    items: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    def flush() -> None:
        if isinstance(current.get("expression"), str):
            items.append(dict(current))
        current.clear()

    names_at = text.find('"names"')
    body = text if names_at == -1 else text[:names_at]
    for m in _FIELD.finditer(body):
        key, string, number = m.group(1), m.group(2), m.group(3)
        if key in current:
            flush()
        if string is None:
            current[key] = int(number)
            continue
        try:
            current[key] = json.loads(f'"{string}"')
        except json.JSONDecodeError:
            current[key] = string
    flush()
    return items


def _salvage_names(text: str) -> list[str]:
    m = re.search(r'"names"\s*:\s*\[([^\]]*)', text)
    return re.findall(_JSON_STRING, m.group(1)) if m else []


# --------------------------------------------------------- traducciones
def clean_translation(text: str) -> str:
    """Normaliza una traducción: sin paréntesis, opciones separadas por " / ", máx. 2."""
    text = fold(text).strip()
    text = re.sub(r"\s*\(.*?\)\s*", " ", text)  # explicaciones entre paréntesis
    text = re.sub(r"\s*[,;]\s*", " / ", text) if "/" not in text else text
    parts = [p.strip(" \t\"'.:-—–") for p in text.split("/")]
    seen: list[str] = []
    for p in parts:
        p = re.sub(r"\s+", " ", p)
        if not p:
            continue
        # "fusion / fusión": la misma opción con y sin tilde cuenta como una (gana la que lleva tilde).
        same = next((i for i, s in enumerate(seen) if _strip_accents(s) == _strip_accents(p)), None)
        if same is None:
            seen.append(p)
        elif p.lower() != seen[same].lower() and p.lower() != _strip_accents(p):
            seen[same] = p
    return " / ".join(seen[:2])


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


# ------------------------------------------------------------ validación
def normalize_kind(kind: Any, expression: str, lang: str = "en") -> str:
    words = expression.split()
    if len(words) == 1:
        return "word"
    if isinstance(kind, str):
        k = _KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
        if k in KINDS and k != "word":
            return k
    if lang == "en" and words[1] in profile(lang).particles:
        return "phrasal verb"
    return "expression"


def _grounded(expression: str, context: str, lang: str = "en") -> bool:
    """Cada palabra de contenido de la expresión debe aparecer en el subtítulo.

    Se admiten flexiones ("went" ~ "go") y un error evidente de OCR: si el
    subtítulo tiene una "palabra" que no existe muy parecida a la propuesta
    ("rnistake" ~ "mistake"), el modelo la ha corregido, no inventado.
    """
    ctx_tokens = tokenize(context)
    ctx_forms = set(ctx_tokens) | {lemma(t, lang) for t in ctx_tokens} | {word_lemma(t, lang) for t in ctx_tokens}
    placeholders = _PLACEHOLDER_TOKENS if lang == "en" else _ES_PLACEHOLDERS
    parts = expression.replace("-", " ").split()
    for i, tok in enumerate(parts):
        if tok in placeholders:
            continue
        if tok in ctx_forms or lemma(tok, lang) in ctx_forms or word_lemma(tok, lang) in ctx_forms:
            continue
        # Pequeñas diferencias de flexión/ortografía (plugging ~ plug, whining ~ whine).
        if len(tok) >= 4 and any(
            c.startswith(tok[:4]) and SequenceMatcher(None, tok, c).ratio() >= 0.6 for c in ctx_tokens
        ):
            continue
        if any(not is_real_word(c, lang) and SequenceMatcher(None, tok, c).ratio() >= 0.75 for c in ctx_tokens):
            continue  # corrección de un error de OCR
        if lang == "es" and i == 0 and len(parts) > 1 and _es_verb_grounded(tok, ctx_tokens):
            continue
        return False
    return True


def _es_verb_grounded(token: str, ctx_tokens: list[str]) -> bool:
    """Infinitivo español frente a una forma conjugada ("echar" ~ "echo", "darse" ~ "di")."""
    m = _ES_INFINITIVE.match(token)
    if not m:
        return False
    stem = m.group(1)
    if len(stem) <= 2:
        return True
    return any(c.startswith(stem[:4]) or c.startswith(stem) for c in ctx_tokens)


def validate_candidate(
    raw_item: dict[str, Any],
    context: str,
    *,
    max_words: int = 6,
    source: str = "en",
    names: set[str] | frozenset[str] = frozenset(),
) -> Candidate | None:
    """Devuelve un Candidate limpio o None si no es una unidad léxica de la línea."""
    prof = profile(source)
    expression = raw_item.get("expression")
    if not isinstance(expression, str) or re.search(r"\d", expression):
        return None

    if is_grammar_construction(expression):
        log.info("Descartado %r (construcción gramatical)", expression)
        return None
    names = set(names) | names_in(context)
    expr = canonical_expression(expression, source)
    words = expr.split()
    while len(words) > 1 and (words[0] in prof.articles or (source == "en" and words[0] in _INTENSIFIERS)):
        words = words[1:]  # "the deal" -> "deal", "incredibly tacky" -> "tacky"
    expr = " ".join(words)
    kind = normalize_kind(raw_item.get("type"), expr, source)

    content = [w for w in words if not is_grammar_word(w, source) and w not in _PLACEHOLDER_TOKENS]
    has_particle = source == "en" and any(w in prof.particles for w in words[1:])
    filler = all(w in content or w in _FILLER for w in words)
    if len(words) > 1 and len(content) == 1 and not has_particle and (kind == "collocation" or filler):
        # "for years", "my mom", "what happened", "go to": una sola palabra de
        # contenido con relleno gramatical no es una expresión.
        expr = word_lemma(content[0], source)
        words, kind = [expr], "word"
    elif len(words) > 1 and kind in ("idiom", "phrasal verb") and source == "en":
        # "cutting corners" -> "cut corners", "raining cats and dogs" -> "rain cats and dogs"
        head = word_lemma(words[0], source)
        if head != words[0] and not is_grammar_word(head, source):
            words = [head] + words[1:]
            expr = " ".join(words)
    elif len(words) == 1:
        expr = word_lemma(expr, source, inflections=False)  # el modelo ya da la forma de diccionario
        words = [expr]

    reason = None
    if not expr:
        reason = "vacío"
    elif any(w in names for w in words):
        reason = "nombre propio"
    elif len(words) > max_words or len(expr) > 60:
        reason = "demasiado larga"
    elif not prof.expression_pattern.fullmatch(expr):
        reason = "caracteres no válidos"
    elif is_grammar_construction(expr):
        reason = "construcción gramatical"
    elif len(words) == 1 and (is_grammar_word(expr, source) or len(expr) < 2 or "'" in expr):
        reason = "palabra gramatical"
    elif not content and kind != "idiom":
        reason = "sólo palabras gramaticales"
    elif len(words) == 1 and not is_real_word(expr, source):
        reason = "no es una palabra real (¿error de OCR?)"
    elif len(words) >= 3 and words[0] in prof.subject_starters:
        reason = "parece una frase completa"
    elif not _grounded(expr, context, source):
        reason = "no aparece en el subtítulo"

    if reason:
        log.info("Descartado %r (%s)", expression, reason)
        return None
    return Candidate(expression=expr, kind=kind, context=context)
