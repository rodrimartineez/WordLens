"""Frases de subtítulo -> candidatos de vocabulario (sin traducir).

Extracción exhaustiva en dos fuentes que se consolidan:

1. Modelo (Ollama, el de siempre): lista cada expresión con significado propio
   (phrasal verbs, modismos, colocaciones, expresiones coloquiales) y cada
   palabra de contenido en forma de diccionario. Todo pasa por
   validate_candidate(): debe estar en la línea y no ser gramática.
2. Reglas (vocabulary/lexicon.py): todas las palabras de contenido de la línea.
   Es la red de seguridad: si el modelo se salta una palabra, se propone igual.
   Una palabra que ya forma parte de una expresión detectada no se repite
   ("brushed off" -> "brush off", no también "brush").

Nada se descarta por dificultad o interés: decide el usuario al revisar.
"""

from __future__ import annotations

import logging
import time
from difflib import SequenceMatcher

from wordlens.config.settings import Settings
from wordlens.vocabulary.extractor import parse_llm_response, validate_candidate
from wordlens.vocabulary.lexicon import FIRST_NAMES, Token, lexical_words, names_in, tokens, word_lemma
from wordlens.vocabulary.models import Candidate
from wordlens.vocabulary.normalization import entry_key, lemma

from .ollama_client import OllamaClient
from .prompts import RESPONSE_SCHEMA, build_messages

log = logging.getLogger(__name__)


class VocabularyAnalyzer:
    def __init__(self, settings: Settings, client: OllamaClient | None = None) -> None:
        self.settings = settings
        self.client = client or OllamaClient(settings.ollama_url, settings.ollama_timeout_seconds)
        # Nombres propios vistos en la sesión: "Sarah" a principio de frase no se
        # puede distinguir de una palabra, pero si ya apareció como nombre, lo es.
        self.known_names: set[str] = set()

    def analyze(self, lines: list[str]) -> list[Candidate]:
        """Candidatos de un lote de líneas, en orden de aparición.

        Lanza OllamaError si el modelo no responde (quien llama conserva las
        líneas y reintenta); una respuesta mal formada nunca lanza.
        """
        s = self.settings
        lang = s.source_code
        lines = [ln.strip() for ln in lines if ln and ln.strip()]
        for line in lines:
            self.known_names |= names_in(line)
        # Sin palabras de contenido no puede haber vocabulario ("Oh, it's you.").
        ask = [ln for ln in lines if lexical_words(ln, lang) or _has_particle_pair(ln)]
        raw_items: list[dict] = []
        if ask:
            raw_items, names = self._ask(ask)
            self.known_names |= {n.lower() for name in names for n in name.split()}
        out: list[Candidate] = []
        for i, line in enumerate(lines):
            index = ask.index(line) + 1 if line in ask else None
            mine = [r for r in raw_items if index is not None and _line_of(r, len(ask)) in (index, None)]
            out += self._consolidate(line, mine)
        return _unique(out, lang)

    # ------------------------------------------------------------------ pasos
    def _ask(self, lines: list[str]) -> tuple[list[dict], list[str]]:
        s = self.settings
        start = time.perf_counter()
        response = self.client.chat(
            s.ollama_model,
            build_messages(lines, source_language=s.source_language, hints=s.candidate_hints),
            schema=RESPONSE_SCHEMA,
            temperature=s.temperature,
            num_predict=200 + 260 * len(lines),
            keep_alive=s.ollama_keep_alive,
        )
        raw = response.get("message", {}).get("content", "")
        log.info("LLM %.1fs para %d línea(s): %s", time.perf_counter() - start, len(lines), raw)
        return parse_llm_response(raw)

    def _consolidate(self, line: str, raw_items: list[dict]) -> list[Candidate]:
        s = self.settings
        lang = s.source_code
        names = self.known_names
        model: list[Candidate] = []
        for raw in raw_items:
            cand = validate_candidate(raw, line, max_words=s.max_expression_words, source=lang, names=names)
            if cand is not None:
                model.append(cand)
        model = _prefer_longest(model)
        line_tokens = tokens(line)
        # Palabras sueltas del modelo que forman parte de un modismo o phrasal verb
        # de la misma línea ("cut corners" + "corner"): la unidad es la expresión.
        locked: set[int] = set()
        for cand in model:
            if cand.kind in ("phrasal verb", "idiom"):
                locked |= _covered_by(cand.expression, line_tokens, lang)
        model = [
            c
            for c in model
            if len(c.expression.split()) > 1 or not (_covered_by(c.expression, line_tokens, lang) <= locked and locked)
        ]

        covered: set[int] = set()
        mentioned: set[int] = set()  # palabras que el modelo ha tenido en cuenta
        for cand in model:
            span = _covered_by(cand.expression, line_tokens, lang)
            mentioned |= span
            # Un phrasal verb o un modismo sustituye a sus palabras ("brush off", no
            # también "brush"). En una colocación o expresión ("heavy rain", "moving
            # abroad") cada palabra sigue siendo una unidad: el modelo etiqueta como
            # "expression" muchas combinaciones libres y ocultarlas perdería palabras.
            if cand.kind in ("phrasal verb", "idiom") or len(cand.expression.split()) == 1:
                covered |= span
        extra = [
            Candidate(tok.lemma, "word", line, origin="lexical")
            for tok in lexical_words(line, lang, names)
            if tok.index not in covered
            and not (tok.capitalized and tok.index not in mentioned and _probable_name(tok))
        ]
        if extra:
            log.info("Palabras añadidas por reglas en %r: %s", line, [c.expression for c in extra])
        return model + extra


def _probable_name(tok: Token) -> bool:
    """¿Es un nombre una palabra con mayúscula que el modelo no ha listado?

    A principio de frase la mayúscula no distingue "Sarah" de "Honestly": sólo
    se descarta si es un nombre de pila frecuente.
    """
    return tok.surface in FIRST_NAMES


def _line_of(raw: dict, count: int) -> int | None:
    idx = raw.get("line")
    if isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= count:
        return idx
    return None  # sin número (o erróneo): se valida contra cada línea


def _has_particle_pair(line: str) -> bool:
    from wordlens.vocabulary.candidates import find_candidates

    return bool(find_candidates(line).phrasal)


def _covered_by(expression: str, line_tokens: list[Token], lang: str) -> set[int]:
    """Posiciones de la línea que corresponden a las palabras de la expresión."""
    covered: set[int] = set()
    for part in expression.replace("-", " ").split():
        for tok in line_tokens:
            if tok.index in covered:
                continue
            forms = {tok.surface, tok.lemma, lemma(tok.surface, lang), word_lemma(tok.surface, lang)}
            similar = (
                len(part) >= 4
                and tok.surface[:3] == part[:3]
                and SequenceMatcher(None, part, tok.surface).ratio() >= 0.7
            )
            if part in forms or similar:
                covered.add(tok.index)
                break
    return covered


def _prefer_longest(items: list[Candidate]) -> list[Candidate]:
    """Quita una unidad contenida en otra de la misma línea ("come back" ⊂ "come back to bite").

    Una palabra suelta dentro de una expresión también sobra ("nail" ⊂ "hit the
    nail on the head"): la expresión ya la contiene.
    """

    def contained(small: list[str], big: list[str]) -> bool:
        n = len(small)
        return n < len(big) and any(big[i : i + n] == small for i in range(len(big) - n + 1))

    words = [item.expression.split() for item in items]
    return [
        item
        for i, item in enumerate(items)
        if not any(j != i and contained(words[i], words[j]) for j in range(len(items)))
    ]


def _unique(items: list[Candidate], lang: str) -> list[Candidate]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = entry_key(item.expression, lang)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out
