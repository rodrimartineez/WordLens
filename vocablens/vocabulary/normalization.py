"""Normalización de texto y expresiones.

Se usa en tres sitios:
- comparar subtítulos entre capturas (normalize_text),
- llevar las expresiones del LLM a forma de diccionario (canonical_expression),
- generar la clave de deduplicación del documento (dedup_key).
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from wordfreq import zipf_frequency

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'", "“": '"', "”": '"'})

PARTICLES = frozenset(
    "up out off on in down over away back through around about at into onto along "
    "across after apart by for forward with without together under behind ahead".split()
)
OBJECT_PRONOUNS = frozenset("it him her them me us you someone somebody something sb sth".split())
POSSESSIVES = frozenset("my your his her our their one's someone's somebody's".split())
ARTICLES = frozenset({"a", "an", "the"})
SUBJECT_STARTERS = frozenset(
    "i you he she we they it where what who how why when is are do does did can could will would".split()
)
# Palabras funcionales: no cuentan como "contenido" de una expresión.
FUNCTION_WORDS = frozenset(
    """a an the and or but so if of to in on at by for with from as than then
    i you he she it we they me him her us them my your his its our their one's
    this that these those there here some any something anything nothing everything
    someone anyone somebody anybody be am is are was were been being do does did
    have has had will would can could shall should may might must not no yes
    very really just too also all""".split()
)

IRREGULAR = {
    "got": "get", "gotten": "get", "gets": "get", "getting": "get",
    "went": "go", "gone": "go", "goes": "go", "going": "go",
    "made": "make", "took": "take", "taken": "take", "gave": "give", "given": "give",
    "came": "come", "ran": "run", "brought": "bring", "broke": "break", "broken": "break",
    "caught": "catch", "fell": "fall", "fallen": "fall", "found": "find", "held": "hold",
    "kept": "keep", "left": "leave", "let": "let", "put": "put", "set": "set", "sat": "sit",
    "stood": "stand", "thought": "think", "told": "tell", "said": "say", "saw": "see",
    "seen": "see", "knew": "know", "known": "know", "threw": "throw", "thrown": "throw",
    "wore": "wear", "worn": "wear", "tore": "tear", "torn": "tear", "drew": "draw",
    "drawn": "draw", "blew": "blow", "blown": "blow", "grew": "grow", "grown": "grow",
    "shook": "shake", "shaken": "shake", "woke": "wake", "woken": "wake", "hung": "hang",
    "dug": "dig", "stuck": "stick", "struck": "strike", "swore": "swear", "sworn": "swear",
    "bit": "bite", "bitten": "bite", "hid": "hide", "hidden": "hide", "rode": "ride",
    "fought": "fight", "sought": "seek", "sold": "sell", "sent": "send", "spent": "spend",
    "built": "build", "felt": "feel", "met": "meet", "paid": "pay", "laid": "lay",
    "lay": "lie", "lain": "lie", "led": "lead", "lost": "lose", "meant": "mean",
    "dealt": "deal", "slept": "sleep", "swept": "sweep", "won": "win", "ate": "eat",
    "eaten": "eat", "did": "do", "done": "do", "does": "do", "had": "have", "has": "have",
    "was": "be", "were": "be", "been": "be", "is": "be", "are": "be", "am": "be",
    "began": "begin", "begun": "begin", "chose": "choose", "chosen": "choose",
    "drove": "drive", "driven": "drive", "forgot": "forget", "forgotten": "forget",
    "spoke": "speak", "spoken": "speak", "stole": "steal", "stolen": "steal",
    "wrote": "write", "written": "write", "cut": "cut", "hit": "hit", "shut": "shut",
}


def fold(text: str) -> str:
    """Unifica comillas/apóstrofos, quita marcas invisibles y normaliza Unicode."""
    text = unicodedata.normalize("NFKC", text).translate(_APOSTROPHES)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def normalize_text(text: str) -> str:
    """Forma comparable de un subtítulo: minúsculas, sin puntuación, espacios simples."""
    text = fold(text).lower()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    text = text.replace(" '", " ").replace("' ", " ")
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]+(?:'[a-z]+)?", fold(text).lower())


@lru_cache(maxsize=20000)
def lemma(word: str) -> str:
    """Lematizador ligero de verbos (sin dependencias pesadas).

    Genera candidatos quitando -ing/-ed/-s y elige el más frecuente en inglés
    según wordfreq, que hace aquí de diccionario.
    """
    w = word.lower()
    if w in IRREGULAR:
        return IRREGULAR[w]
    candidates: list[str] = []
    if w.endswith("ing") and len(w) > 5:
        stem = w[:-3]
        candidates += [stem, stem + "e"]
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
            candidates.append(stem[:-1])
    elif w.endswith("ied") and len(w) > 4:
        candidates.append(w[:-3] + "y")
    elif w.endswith("ed") and len(w) > 4:
        stem = w[:-2]
        candidates += [stem, w[:-1]]
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
            candidates.append(stem[:-1])
    elif w.endswith("ies") and len(w) > 4:
        candidates.append(w[:-3] + "y")
    elif w.endswith("es") and len(w) > 4:
        candidates += [w[:-2], w[:-1]]
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        candidates.append(w[:-1])
    best, best_freq = w, 2.5  # sólo aceptamos candidatos que existan de verdad
    for cand in candidates:
        freq = zipf_frequency(cand, "en")
        if freq > best_freq:
            best, best_freq = cand, freq
    return best


def clean_expression(text: str) -> str:
    text = fold(text).lower().strip()
    text = re.sub(r"\(.*?\)", " ", text)  # "(something)" explicativos
    text = text.strip(" \t\"'.,;:!?¡¿-—–…")
    text = re.sub(r"^to\s+", "", text)  # "to figure out" -> "figure out"
    text = re.sub(r"[^a-z' \-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_expression(text: str) -> str:
    """Forma de diccionario de una expresión multi-palabra.

    - "getting at"           -> "get at"
    - "figure him out"       -> "figure out"
    - "take it for granted"  -> "take for granted"
    - "under my skin"        -> "under one's skin"
    Las palabras sueltas no se tocan ("overwhelmed" sigue siendo "overwhelmed").
    """
    tokens = clean_expression(text).split()
    if len(tokens) < 2:
        return " ".join(tokens)

    out: list[str] = []
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok in {"my", "your", "his", "our", "their"} or (
            tok == "her" and nxt is not None and nxt not in PARTICLES and nxt not in ARTICLES
        ):
            out.append("one's")
        elif tok in _PERSONAL_OBJECTS and i > 0:
            continue  # "figure him out" -> "figure out"
        else:
            out.append(tok)
    if len(out) < 2:
        out = tokens

    # El primer token sólo se lematiza con patrón verbal (verbo + partícula/objeto),
    # para no estropear "living room" o "boiling point".
    head_next = tokens[1]
    if head_next in PARTICLES or head_next in ARTICLES or head_next in OBJECT_PRONOUNS or (
        head_next in POSSESSIVES
    ):
        out[0] = lemma(out[0])
    return " ".join(out)


# "it" se conserva al escribir ("call it a day"); sólo se quitan objetos personales.
_PERSONAL_OBJECTS = frozenset({"him", "her", "them", "me", "us"})
_PLACEHOLDERS = OBJECT_PRONOUNS | {"one's", "oneself", "yourself", "himself", "herself"}


def dedup_key(expression: str) -> str:
    """Clave para detectar duplicados en el documento.

    Ignora mayúsculas, puntuación, artículo inicial, "to" inicial y los huecos de
    objeto/posesivo ("take it for granted" == "take something for granted" ==
    "take for granted"). No usa similitud difusa: "get at" y "get out" son
    expresiones distintas y deben seguir siéndolo.
    """
    canon = canonical_expression(expression)
    tokens = [t for t in canon.replace("-", " ").split() if t]
    if len(tokens) > 1 and tokens[0] in ARTICLES:
        tokens = tokens[1:]
    tokens = ["one's" if t in POSSESSIVES else t for t in tokens]
    loose = [t for i, t in enumerate(tokens) if i == 0 or t not in _PLACEHOLDERS]
    return " ".join(loose if len(loose) >= 2 or len(tokens) < 2 else tokens)
