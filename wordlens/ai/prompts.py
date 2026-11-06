"""Prompts de la extracción de vocabulario (sólo identificar; traducir va aparte).

El prompt de sistema y los ejemplos son fijos para que Ollama reutilice la caché
de prefijo (KV cache): sólo se procesa de nuevo el mensaje final con las líneas.

La extracción es exhaustiva: el modelo no juzga la dificultad ni el interés de
nada (eso lo decide el usuario al revisar). Debe listar cada expresión con
significado propio y cada palabra de contenido, en forma de diccionario.
Ningún ejemplo coincide con las frases de los benchmarks
(tests/benchmark/exhaustive_cases.jsonl), para que la medida sea honesta.
"""

from __future__ import annotations

import json

from wordlens.vocabulary.candidates import hint_line

KINDS = ("word", "phrasal verb", "idiom", "collocation", "expression")

SYSTEM_PROMPT = """You list the vocabulary of {source} subtitle lines for a language learner's notebook. The learner decides later what to study, so never judge difficulty or usefulness: list every lexical unit, easy or hard.

For each line list:
1. Every multi-word expression with its own meaning: phrasal verbs (give up, put up with, go ahead with), idioms (hit the nail on the head, on the fence), colloquial and fixed expressions (no way, fair enough, catch you later), collocations (heavy rain, make a decision, take into account) and verb + preposition units (deal with, rely on).
2. Every content word that is not part of one of those expressions: nouns, verbs, adjectives, adverbs, slang and insults.

Never list: articles, pronouns, prepositions, conjunctions, auxiliary or modal verbs (be, have, do, can, will, would), numbers, interjections (oh, wow, yeah), names of people, places or brands, grammar patterns (have to, going to, used to, there is) or whole sentences.
A word that belongs to a listed expression is not listed again on its own: "brushed off" gives "brush off", not "brush".

"expression": dictionary form in lowercase. Base form of verbs ("went ahead with" -> "go ahead with", "stalled" -> "stall"), singular nouns ("concerns" -> "concern"), no personal objects or possessives ("talked me into" -> "talk into", "made up our minds" -> "make up one's mind").
"type": word, phrasal verb, idiom, collocation or expression.
Every item must come from the line: never add words that are not in it. Skip words broken by the OCR.
A line may be followed by [possible phrasal verbs: ...], found automatically: keep only the real ones.

Output JSON only: {{"items":[{{"line":1,"expression":"...","type":"..."}}],"names":["..."]}}. "names" lists the names of people or places found in the lines."""

SYSTEM_PROMPT_ES = """You list the vocabulary of Spanish subtitle lines for a language learner's notebook. The learner decides later what to study, so never judge difficulty or usefulness: list every lexical unit, easy or hard.

For each line list:
1. Every multi-word expression with its own meaning: verbal phrases and pronominal verbs with a special meaning (darse cuenta, echar de menos, llevarse bien), idioms (tomar el pelo, estar hasta las narices), colloquial and fixed expressions (menos mal, vale la pena, ni de broma) and collocations (tomar una decisión).
2. Every content word that is not part of one of those expressions: nouns, verbs, adjectives, adverbs, slang and insults.

Never list: articles, pronouns, prepositions, conjunctions, auxiliary verbs (ser, estar, haber when they are auxiliary), numbers, interjections, names of people, places or brands, grammar patterns (tener que, ir a, hay que) or whole sentences.
A word that belongs to a listed expression is not listed again on its own.

"expression": dictionary form in lowercase: infinitive for verbs ("me di cuenta" -> "darse cuenta"), singular nouns, masculine singular adjectives.
"type": word, phrasal verb, idiom, collocation or expression.
Every item must come from the line: never add words that are not in it. Skip words broken by the OCR.

Output JSON only: {{"items":[{{"line":1,"expression":"...","type":"..."}}],"names":["..."]}}. "names" lists the names of people or places found in the lines."""


def _answer(items: list[tuple[int, str, str]], names: list[str]) -> str:
    return json.dumps(
        {"items": [{"line": n, "expression": e, "type": t} for n, e, t in items], "names": names},
        ensure_ascii=False,
    )


FEW_SHOT: list[tuple[list[str], str]] = [
    (
        [
            "She shrugged off the criticism and kept plugging away at her novel.",
            "Honestly, Tom, you're driving me nuts with that racket.",
            "I have to call my mom.",
            "Yeah, I know.",
        ],
        _answer(
            [
                (1, "shrug off", "phrasal verb"),
                (1, "criticism", "word"),
                (1, "keep", "word"),
                (1, "plug away at", "phrasal verb"),
                (1, "novel", "word"),
                (2, "honestly", "word"),
                (2, "drive someone nuts", "idiom"),
                (2, "racket", "word"),
                (3, "call", "word"),
                (3, "mom", "word"),
                (4, "know", "word"),
            ],
            ["Tom"],
        ),
    ),
    (
        [
            "The board shelved the takeover after the stock plummeted.",
            "We grabbed a quick bite before the gig in Leeds.",
            "I'm not gonna sugarcoat it: we're broke and the landlord is fuming.",
        ],
        _answer(
            [
                (1, "board", "word"),
                (1, "shelve", "word"),
                (1, "takeover", "word"),
                (1, "stock", "word"),
                (1, "plummet", "word"),
                (2, "grab a bite", "collocation"),
                (2, "quick", "word"),
                (2, "gig", "word"),
                (3, "sugarcoat", "word"),
                (3, "broke", "word"),
                (3, "landlord", "word"),
                (3, "fuming", "word"),
            ],
            ["Leeds"],
        ),
    ),
]

FEW_SHOT_ES: list[tuple[list[str], str]] = [
    (
        [
            "Me di cuenta de que Marta me estaba tomando el pelo.",
            "Menos mal que llegaste, estaba agobiado con tanto papeleo.",
        ],
        _answer(
            [
                (1, "darse cuenta", "expression"),
                (1, "tomar el pelo", "idiom"),
                (2, "menos mal", "expression"),
                (2, "llegar", "word"),
                (2, "agobiado", "word"),
                (2, "papeleo", "word"),
            ],
            ["Marta"],
        ),
    ),
]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "expression": {"type": "string"},
                    "type": {"type": "string", "enum": list(KINDS)},
                },
                "required": ["line", "expression", "type"],
            },
        },
        "names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items"],
}


def build_messages(
    lines: list[str],
    *,
    source_language: str = "English",
    hints: bool = True,
) -> list[dict[str, str]]:
    spanish_source = source_language == "Spanish"
    system = (SYSTEM_PROMPT_ES if spanish_source else SYSTEM_PROMPT).format(source=source_language)
    lang = "es" if spanish_source else "en"
    messages = [{"role": "system", "content": system}]
    for examples, assistant in FEW_SHOT_ES if spanish_source else FEW_SHOT:
        messages.append({"role": "user", "content": render_lines(examples, lang, hints)})
        messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": render_lines(lines, lang, hints)})
    return messages


def render_lines(lines: list[str], lang: str = "en", hints: bool = True) -> str:
    """Líneas numeradas, cada una con sus posibles phrasal verbs (ver vocabulary/candidates.py)."""
    return "\n".join(
        f"{i}. {line}{hint_line(line, lang) if hints else ''}" for i, line in enumerate(lines, start=1)
    )
