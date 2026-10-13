"""Candidatos de vocabulario detectados sin LLM, para guiar al modelo.

El modelo local es pequeño y tiende a quedarse con el primer elemento de cada
línea. Antes de llamarlo se marcan en la línea las palabras poco frecuentes y
las combinaciones verbo + partícula: el modelo las recibe como lista de
comprobación y DECIDE él qué merece guardarse (una palabra rara no es por sí
sola vocabulario útil). Nada de esto se guarda sin pasar por el LLM y por
validate_item().
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from wordfreq import zipf_frequency

from .languages import profile
from .normalization import IRREGULAR, fold, lemma

# Frecuencia (escala Zipf de wordfreq) por debajo de la cual una palabra se
# marca como "poco frecuente". 4.0 ~ palabras que un B2 suele no dominar
# (reluctant 3.8, defendant 4.0, budge 3.2); admit 4.6 o situation 5.1 quedan fuera.
RARE_ZIPF = 4.0
# Si el lema es muy frecuente, la forma flexionada no se marca (swords -> sword 4.4).
RARE_LEMMA_ZIPF = 4.3
# Por debajo de esto la "palabra" casi seguro no existe (error de OCR, nombre inventado).
MIN_REAL_ZIPF = 1.5

_PHRASAL_PARTICLES = frozenset(
    "up out off down away back over into through around along apart aside".split()
)
_OBJECTS = frozenset("it him her them me us you this that something someone everything".split())
_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")


@dataclass
class Candidates:
    rare_words: list[str] = field(default_factory=list)
    phrasal: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.rare_words or self.phrasal)


def proper_nouns(text: str) -> set[str]:
    """Palabras con mayúscula que no van a principio de frase (nombres propios)."""
    names: set[str] = set()
    sentence_start = True
    for m in re.finditer(r"[^\W\d_]+(?:['’][^\W\d_]+)?|[.!?¿¡:;\"]", fold(text)):
        tok = m.group(0)
        if tok in ".!?¿¡:;\"":
            sentence_start = True
            continue
        if not sentence_start and tok[0].isupper() and tok.lower() != "i":
            names.add(tok.lower())
        sentence_start = False
    return names


def find_candidates(text: str, lang: str = "en") -> Candidates:
    prof = profile(lang)
    names = proper_nouns(text)
    words = [w.lower().replace("’", "'") for w in _WORD.findall(fold(text))]
    out = Candidates()
    phrasal_words = [p for w in words for p in w.split("-")]
    seen: set[str] = set()
    for w in words:
        if w in seen or w in names or w in prof.function_words or len(w) < 4 or "'" in w:
            continue
        seen.add(w)
        if "-" in w:  # far-fetched, half-baked: compuesto, se valora entero
            parts = [p for p in w.split("-") if p not in prof.function_words]
            if parts and len(w) >= 7 and min(zipf_frequency(p, lang) for p in parts) >= MIN_REAL_ZIPF:
                out.rare_words.append(w)
            continue
        freq = zipf_frequency(w, lang)
        if not MIN_REAL_ZIPF <= freq < RARE_ZIPF:
            continue
        if lang == "en" and zipf_frequency(lemma(w), "en") >= RARE_LEMMA_ZIPF:
            continue
        out.rare_words.append(w)
    if lang == "en":
        out.phrasal = _phrasal_candidates(phrasal_words, prof.function_words)
    return out


def _phrasal_candidates(words: list[str], function_words: frozenset[str]) -> list[str]:
    """verbo + (objeto) + partícula: "talked me into" -> "talk into", "track him down"."""
    found: list[str] = []
    for i, w in enumerate(words[:-1]):
        if w in function_words or w in _PHRASAL_PARTICLES or len(w) < 2:
            continue
        j = i + 1
        if words[j] in _OBJECTS and j + 1 < len(words):
            j += 1
        if words[j] not in _PHRASAL_PARTICLES:
            continue
        verb = IRREGULAR.get(w) or lemma(w)
        phrase = f"{verb} {words[j]}"
        # Partícula + preposición: "cut down on", "put up with", "get out of".
        if j + 1 < len(words) and words[j + 1] in {"on", "with", "of"}:
            phrase += f" {words[j + 1]}"
        if phrase not in found:
            found.append(phrase)
    return found


def hint_line(text: str, lang: str = "en") -> str:
    """Texto de pista que acompaña a la línea en el mensaje al modelo."""
    c = find_candidates(text, lang)
    parts = []
    if c.rare_words:
        parts.append("less common words: " + ", ".join(c.rare_words))
    if c.phrasal:
        parts.append("possible phrasal verbs: " + ", ".join(c.phrasal))
    return f"   [{'; '.join(parts)}]" if parts else ""
