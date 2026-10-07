"""Datos por idioma de origen (el idioma de los subtítulos).

Inglés tiene el tratamiento completo (phrasal verbs, lematización ligera).
Español usa la misma cadena con sus propias listas: palabras funcionales,
artículos y un patrón de caracteres válido con tildes y eñe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    function_words: frozenset[str]
    subject_starters: frozenset[str]
    particles: frozenset[str]
    articles: frozenset[str]
    expression_pattern: re.Pattern[str]


EN_PARTICLES = frozenset(
    "up out off on in down over away back through around about at into onto along "
    "across after apart by for forward with without together under behind ahead".split()
)
EN_ARTICLES = frozenset({"a", "an", "the"})
EN_SUBJECT_STARTERS = frozenset(
    "i you he she we they it where what who how why when is are do does did can could will would".split()
)
# Palabras funcionales: no cuentan como "contenido" de una expresión.
EN_FUNCTION_WORDS = frozenset(
    """a an the and or but so if of to in on at by for with from as than then
    i you he she it we they me him her us them my your his its our their one's
    this that these those there here some any something anything nothing everything
    someone anyone somebody anybody be am is are was were been being do does did
    have has had will would can could shall should may might must not no yes
    very really just too also all""".split()
)

ES_ARTICLES = frozenset({"el", "la", "los", "las", "un", "una", "unos", "unas", "lo"})
ES_SUBJECT_STARTERS = frozenset(
    "yo tú tu él ella usted nosotros nosotras vosotros vosotras ellos ellas ustedes "
    "qué que quién quien cómo como dónde donde cuándo cuando por es son está están".split()
)
ES_FUNCTION_WORDS = frozenset(
    """el la los las un una unos unas lo al del de a en con por para sin sobre entre
    y e o u ni pero sino que si no sí ya muy más menos tan también
    yo tú tu él ella usted nosotros nosotras vosotros vosotras ellos ellas ustedes
    me te se le les nos os mi mis tus su sus nuestro nuestra vuestro vuestra
    este esta esto estos estas ese esa eso esos esas aquel aquella aquello
    ser es son era fue soy eres somos estar está están estoy estaba
    haber he ha has hemos han había hay tener tengo tiene algo nada todo""".split()
)

ENGLISH = LanguageProfile(
    code="en",
    function_words=EN_FUNCTION_WORDS,
    subject_starters=EN_SUBJECT_STARTERS,
    particles=EN_PARTICLES,
    articles=EN_ARTICLES,
    expression_pattern=re.compile(r"[a-z' \-]+"),
)
SPANISH = LanguageProfile(
    code="es",
    function_words=ES_FUNCTION_WORDS,
    subject_starters=ES_SUBJECT_STARTERS,
    particles=frozenset(),
    articles=ES_ARTICLES,
    expression_pattern=re.compile(r"[a-záéíóúüñ' \-]+"),
)

PROFILES = {"en": ENGLISH, "es": SPANISH}

# Palabras que no cuentan al comparar una traducción con la expresión original
# (cognados): artículos y verbos auxiliares del idioma de la traducción.
TRANSLATION_STOPWORDS = {
    "es": frozenset({"el", "la", "los", "las", "un", "una", "lo", "ser", "estar"}),
    "en": frozenset({"the", "a", "an", "to", "be"}),
}


def profile(code: str) -> LanguageProfile:
    return PROFILES.get(code, ENGLISH)
