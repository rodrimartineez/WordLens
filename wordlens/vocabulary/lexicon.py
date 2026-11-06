"""Unidades léxicas de una línea sin LLM: palabras con significado propio.

Criterio: una palabra se propone si es una unidad léxica (sustantivo, verbo,
adjetivo, adverbio, slang...), sea fácil o difícil. Sólo se excluyen como
candidatos independientes las palabras gramaticales (artículos, pronombres,
preposiciones, auxiliares, conjunciones, interjecciones, números) y las
construcciones gramaticales básicas ("have to", "going to"). Esas palabras
pueden seguir formando parte de una expresión ("give in", "on the fence").

La frecuencia NO se usa para decidir si algo es interesante; sólo para
reconocer palabras inexistentes (errores de OCR) y para lematizar con cuidado.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from wordfreq import zipf_frequency

from .languages import ES_FUNCTION_WORDS
from .normalization import fold

# Palabras gramaticales del inglés (no se proponen solas).
EN_GRAMMAR_WORDS = frozenset(
    """
    a an the
    i you he she it we they me him her us them my your his its our their
    mine yours hers ours theirs myself yourself himself herself itself ourselves yourselves themselves
    one's oneself this that these those
    who whom whose which what where when why how whoever whatever's
    there here
    someone somebody something anyone anybody anything everyone everybody everything
    no-one nobody nothing none
    some any each every either neither both all few many much more most other another such
    and or but nor so yet if than then though although because while whereas unless whether
    since until till as
    of to in on at by for with from into onto upon about above across after against along
    among around before behind below beneath beside besides between beyond down during
    except inside near off out outside over past per through throughout toward towards under
    underneath up via within without
    be am is are was were been being
    do does did done doing
    have has had having
    would shall should can could may might must ought
    not no yes
    very too also just so really quite rather
    oh ah uh um umm hmm mm huh hey wow ooh oops ow eh er ha haha yeah yep yup nope okay ok
    one two three four five six seven eight nine ten eleven twelve hundred thousand million
    mr mrs ms dr
    """.split()
)

# Contracciones: nunca son vocabulario por sí solas.
_CONTRACTION = re.compile(r"^[a-z]+'(?:s|re|ve|ll|d|m|t)$|^[a-z]+n't$")

# Construcciones gramaticales que no son entradas de vocabulario.
GRAMMAR_CONSTRUCTIONS = frozenset(
    """
    have to|has to|had to|having to|have got to|has got to|got to|gotta|
    going to|gonna|wanna|want to|used to|use to|be able to|able to|
    be going to|there is|there are|there was|there were|it is|it's|
    i have to|you have to|i am|i'm|you are|would like|would like to|
    have been|has been|had been|will be|would be|could be|should be|
    do not|does not|did not|don't|doesn't|didn't|can't|cannot|won't|
    need to|ought to|let's|let us|kind of|sort of|a lot|lots of|a lot of
    """.replace("\n", "").split("|")
)
GRAMMAR_CONSTRUCTIONS = frozenset(c.strip() for c in GRAMMAR_CONSTRUCTIONS if c.strip())

# Formas irregulares sin ambigüedad (left, saw, found, felt, lay... también son
# palabras propias y se dejan como están).
_IRREGULAR = {
    "went": "go", "gone": "go", "goes": "go", "took": "take", "taken": "take", "gave": "give",
    "given": "give", "came": "come", "ran": "run", "brought": "bring",
    "caught": "catch", "fallen": "fall", "held": "hold", "kept": "keep", "stood": "stand",
    "thought": "think", "told": "tell", "said": "say", "seen": "see", "knew": "know",
    "known": "know", "threw": "throw", "thrown": "throw", "wore": "wear", "worn": "wear",
    "tore": "tear", "drew": "draw", "drawn": "draw", "blew": "blow", "blown": "blow",
    "grew": "grow", "grown": "grow", "shook": "shake", "shaken": "shake", "woke": "wake",
    "woken": "wake", "dug": "dig", "struck": "strike", "swore": "swear", "sworn": "swear",
    "bitten": "bite", "hid": "hide", "hidden": "hide", "rode": "ride", "fought": "fight",
    "sought": "seek", "sold": "sell", "sent": "send", "spent": "spend", "built": "build",
    "met": "meet", "paid": "pay", "laid": "lay", "meant": "mean", "dealt": "deal",
    "slept": "sleep", "swept": "sweep", "won": "win", "ate": "eat", "eaten": "eat",
    "began": "begin", "begun": "begin", "chose": "choose", "chosen": "choose", "drove": "drive",
    "driven": "drive", "forgot": "forget", "forgotten": "forget", "spoke": "speak",
    "spoken": "speak", "stole": "steal", "stolen": "steal", "wrote": "write", "written": "write",
    "got": "get", "gotten": "get", "going": "go", "made": "make", "bought": "buy", "taught": "teach",
    "understood": "understand", "became": "become", "sang": "sing", "sung": "sing",
    "swam": "swim", "flew": "fly", "flown": "fly", "froze": "freeze", "frozen": "freeze",
    "lied": "lie", "fed": "feed", "fled": "flee", "hung": "hang", "lent": "lend", "bent": "bend", "wept": "weep", "crept": "creep",
    "rang": "ring", "rung": "ring", "sank": "sink", "sunk": "sink", "shrank": "shrink",
    "spun": "spin", "stung": "sting", "swung": "swing", "beaten": "beat", "forgave": "forgive",
    "forgiven": "forgive", "mistook": "mistake", "mistaken": "mistake",
}

# Formas en -s que no son plurales de otra palabra (news != new).
_KEEP_AS_IS = frozenset(
    """news clothes glasses series species means physics politics economics mathematics
    pants jeans trousers scissors thanks goods arms customs belongings outskirts surroundings
    headquarters premises remains savings earnings odds congratulations regards lens
    always perhaps besides sometimes towards afterwards nowadays""".split()
)

# Nombres de pila frecuentes en series y películas en inglés. Sirven para no
# proponer "Sarah" cuando va a principio de frase (ahí la mayúscula no dice nada).
FIRST_NAMES = frozenset(
    """
    aaron adam alan alex alice amanda amy andrew angela anna anne anthony ashley ben benjamin
    beth betty billy bob bobby brad brandon brian bruce carl carol caroline charles charlie
    chris christine claire daniel danny dave david debbie dennis diana diane dorothy doug
    dylan ed eddie edward elizabeth ellie emily emma eric ethan fred gary george greg
    hannah harry heather helen henry ian jackie jacob jake james jamie jane janet jason jeff
    jen jenny jennifer jeremy jerry jess jessica jill jim jimmy joe joel joey john johnny jon
    jonathan jordan joseph josh joshua julia julie justin karen kate katie kathy keith kelly ken
    kevin kim kyle laura lauren leo leonard linda lisa liz lucy luke maggie marc maria marie
    martha martin mary matt matthew megan melissa michael michelle mike molly monica nancy
    nate nathan nick nicole noah oliver olivia pam patrick paul peter phil phoebe rachel ray
    rebecca richard rick robert robin ron ross ryan sam samantha sandra sara sarah scott sean
    sheldon sophie stephen steve steven susan ted terry thomas tim todd tom tommy tony tyler
    walter will william zach zoe
    """.split()
)

MIN_REAL_ZIPF = 1.5  # por debajo, casi seguro un error de OCR o una palabra inventada
_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")


def is_grammar_word(word: str, lang: str = "en") -> bool:
    w = fold(word).lower().strip("'")
    if lang == "en":
        return w in EN_GRAMMAR_WORDS or bool(_CONTRACTION.match(w))
    return w in ES_FUNCTION_WORDS


def is_grammar_construction(expression: str) -> bool:
    return " ".join(fold(expression).lower().split()) in GRAMMAR_CONSTRUCTIONS


def is_real_word(word: str, lang: str = "en") -> bool:
    """¿Existe la palabra? (wordfreq hace de diccionario).

    Las palabras muy cortas necesitan más frecuencia: los errores de OCR de
    tres letras ("tbe", "rn") aparecen a menudo en los corpus.
    """
    if "-" in word:
        return all(is_real_word(p, lang) for p in word.split("-") if p)
    threshold = 3.0 if len(word) <= 3 else MIN_REAL_ZIPF
    return zipf_frequency(word, lang) >= threshold


# Sustantivos en -ing cuyo "verbo" es otra palabra (evening != even).
_ING_NOUNS = frozenset(
    """evening morning feeling painting ending beginning warning saying crossing landing
    opening being timing spring string ring king wing swing thing ceiling wedding building
    meeting clothing lightning darling pudding sibling""".split()
)


@lru_cache(maxsize=20000)
def word_lemma(word: str, lang: str = "en", *, inflections: bool = True) -> str:
    """Forma de diccionario prudente de una palabra suelta.

    - Plurales: kids -> kid, negotiations -> negotiation (salvo news, glasses...).
    - -ed / -ing: sólo si el verbo base es claramente más frecuente que la forma
      (stalled -> stall, arguing -> argue), así los adjetivos se quedan como
      están (interesting, worried, scared, amazing, boring).
    Sin contexto no se puede hacer mejor: el modelo, que sí ve la frase, da su
    propia forma de diccionario y tiene preferencia (ver vocabulary_analyzer).
    inflections=False no toca -ed / -ing (sólo plurales e irregulares): para
    formas que ya vienen del modelo o de una entrada ("gutted" = destrozado
    no es "gut").
    """
    w = fold(word).lower()
    if lang != "en" or "-" in w or "'" in w or w in _KEEP_AS_IS or w in _ING_NOUNS:
        return w
    if w in _IRREGULAR:  # formas irregulares sin ambigüedad (said, went, got)
        return _IRREGULAR[w]
    own = zipf_frequency(w, "en")
    plural: list[str] = []
    verbal: list[str] = []
    if w.endswith("ing") and len(w) > 5:
        stem = w[:-3]
        verbal += [stem, stem + "e"]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            verbal.append(stem[:-1])
    elif w.endswith("ied") and len(w) > 4:
        verbal.append(w[:-3] + "y")
    elif w.endswith("ed") and len(w) > 4:
        stem = w[:-2]
        verbal += [stem, w[:-1]]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            verbal.append(stem[:-1])
    elif w.endswith("ies") and len(w) > 4:
        plural.append(w[:-3] + "y")
    elif w.endswith(("ches", "shes", "sses", "xes", "zes", "oes")) and len(w) > 4:
        plural += [w[:-2], w[:-1]]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        plural.append(w[:-1])
    best, best_freq = w, 0.0
    for cand in plural:  # el singular gana aunque el plural sea algo más frecuente
        freq = zipf_frequency(cand, "en")
        if freq >= 3.0 and freq >= own - 1.0 and freq > best_freq and cand not in EN_GRAMMAR_WORDS:
            best, best_freq = cand, freq
    for cand in verbal if inflections else []:
        freq = zipf_frequency(cand, "en")
        if freq > own + 0.25 and freq > best_freq and len(cand) >= 3 and cand not in EN_GRAMMAR_WORDS:
            best, best_freq = cand, freq
    return best


@dataclass(frozen=True)
class Token:
    surface: str  # tal como aparece (en minúsculas)
    index: int  # posición en la línea
    lemma: str
    capitalized: bool = False  # empieza por mayúscula (en una línea que no va toda en mayúsculas)


def _mostly_upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.8


def tokens(text: str) -> list[Token]:
    """Palabras de la línea en minúsculas, con su lema prudente."""
    folded = fold(text)
    shouting = _mostly_upper(folded)
    out = []
    for i, m in enumerate(_WORD.finditer(folded)):
        raw = m.group(0)
        w = raw.lower().replace("’", "'")
        out.append(Token(w, i, word_lemma(w), capitalized=raw[0].isupper() and not shouting))
    return out


def names_in(text: str) -> set[str]:
    """Nombres propios probables: con mayúscula y no a principio de frase.

    Una línea entera en mayúsculas no aporta información (no hay nombres).
    """
    folded = fold(text)
    if _mostly_upper(folded):
        return set()
    names: set[str] = set()
    sentence_start = True
    for m in re.finditer(r"[^\W\d_]+(?:['’][^\W\d_]+)?|[.!?¿¡:;\"]", folded):
        tok = m.group(0)
        if tok in ".!?¿¡:;\"":
            sentence_start = True
            continue
        if not sentence_start and tok[0].isupper() and tok.lower() != "i":
            names.add(tok.lower())
        sentence_start = False
    return names


def lexical_words(text: str, lang: str = "en", names: set[str] | frozenset[str] = frozenset()) -> list[Token]:
    """Palabras de la línea que son unidades léxicas por sí mismas.

    Fuera: palabras gramaticales, contracciones, nombres propios, palabras que
    no existen (errores de OCR) y letras sueltas. Un posesivo "dad's" cuenta
    como "dad". Sin repetir lemas dentro de la línea.
    """
    found: list[Token] = []
    seen: set[str] = set()
    local_names = names_in(text) | set(names)
    for tok in tokens(text):
        w = tok.surface
        if w.endswith("'s") and w[:-2] not in EN_GRAMMAR_WORDS and lang == "en":
            w = w[:-2]
            tok = Token(w, tok.index, word_lemma(w), tok.capitalized)
        if len(w.replace("-", "")) < 2 or is_grammar_word(w, lang) or "'" in w:
            continue
        if w in local_names or tok.lemma in local_names:
            continue
        if not is_real_word(w, lang):
            continue
        if tok.lemma in seen:
            continue
        seen.add(tok.lemma)
        found.append(tok)
    return found
