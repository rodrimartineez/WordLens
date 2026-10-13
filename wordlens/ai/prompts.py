"""Prompts del filtro de vocabulario.

El prompt de sistema y los ejemplos son fijos para que Ollama reutilice la caché
de prefijo (KV cache): sólo se procesa de nuevo el mensaje final con las líneas,
lo que reduce mucho la latencia por llamada.

Los ejemplos enseñan sobre todo dos cosas que el modelo pequeño no hace solo:
recorrer la línea entera (una línea puede dar 0, 1, 2, 3 o más elementos) y
descartar lo que un B2 ya conoce. Ninguno coincide con las frases del benchmark
(tests/benchmark/extraction_cases.json), para que la medida sea honesta.
"""

from __future__ import annotations

from wordlens.vocabulary.candidates import hint_line

SYSTEM_PROMPT = """You find study vocabulary in {source} subtitles for an advanced learner whose native language is {target}. The learner is {low} level and wants to reach {high} and beyond: save only what is worth learning at upper-B2, C1 or C2 level.

Read EACH line from start to end and list EVERY item worth learning. Do not stop after the first one: a line can contain 0, 1, 2, 3 or more items.
A line may be followed by [less common words: ...; possible phrasal verbs: ...], found automatically. Use it as a checklist: judge each one in context and keep it only if it is worth learning (many are names, basic words or false phrasal verbs). It is incomplete: idioms and expressions made of common words are never listed there, so look for them too.

Worth learning:
- advanced words (reluctant, flimsy, tarnish, setback, gutted, scathing)
- phrasal verbs (back out, chip in, track down, put up with, call off)
- idioms and fixed expressions (call it a day, on the fence, cut corners, spot on)
- collocations whose meaning is not obvious from the words (take a rain check, save face)
- common words with a special meaning in this context ("nailed it" = did it perfectly)

NOT worth learning (skip them):
- words a B2 learner already knows: admit, situation, decide, believe, apartment, coffee, terrible, finally, problem, important, remember, explain, broken
- free combinations of basic words: "make the coffee", "order pizza", "last time", "terrible food", "new phone"
- names of people, places, brands; numbers; greetings; whole sentences
- {target} cognates with the same meaning (ridiculous, crucial, intelligent, decision)
- OCR errors and words that do not exist

Choose the smallest unit that carries the meaning:
- "backed out of the deal" -> "back out of"; keep an idiom whole and do not list its words again separately
- drop intensifiers: "incredibly shabby" -> "shabby", "utterly pointless" -> "pointless"

"expression": dictionary form, lowercase, 1 to 5 words: base verb ("getting at" -> "get at"), no personal objects ("figure him out" -> "figure out", "made up our minds" -> "make up one's mind").
"translation": the natural {target} equivalent for the meaning IN THIS CONTEXT, never word for word (idioms -> an equivalent {target} expression). 1 or 2 options separated by " / ", no explanations.

Output JSON only: {{"items":[{{"line":1,"expression":"...","translation":"..."}}]}}. If nothing is worth learning, return {{"items":[]}}."""

SYSTEM_PROMPT_ES = """You find study vocabulary in Spanish subtitles for an advanced learner whose native language is {target}. The learner is {low} level and wants to reach {high} and beyond: save only what is worth learning at upper-B2, C1 or C2 level.

Read EACH line from start to end and list EVERY item worth learning. Do not stop after the first one: a line can contain 0, 1, 2, 3 or more items.
A line may be followed by [less common words: ...], found automatically. Use it as a checklist: judge each one in context and keep it only if it is worth learning. It is incomplete: idioms and expressions made of common words are never listed there, so look for them too.

Worth learning:
- advanced words (agobiado, imprescindible, desamparado, rencoroso)
- pronominal verbs and verbal phrases with a special meaning (darse cuenta, quedarse con, llevarse bien, acabar de)
- idioms and fixed expressions (echar de menos, tomar el pelo, estar hasta las narices, vale la pena)
- collocations and colloquial phrases whose meaning is not obvious (menos mal, dar igual)
- common words with a special meaning in this context

NOT worth learning (skip them):
- words a B2 learner already knows: ir, casa, comer, bueno, día, gente, hacer, tener, supermercado, ayer, tiempo, semanas, problema
- free combinations of basic words: "ir al supermercado", "por semanas", "lo que decía"
- names of people, places, brands; numbers; greetings; whole sentences
- {target} cognates with the same meaning (importante, situación, ridículo)
- OCR errors and words that do not exist

Choose the smallest unit that carries the meaning; keep an idiom whole and do not list its words again separately.

"expression": dictionary form, lowercase, 1 to 5 words, Spanish spelling with accents: infinitive ("me di cuenta" -> "darse cuenta", "te echo de menos" -> "echar de menos"), no personal objects that are not part of the expression ("le tomó el pelo" -> "tomar el pelo").
"translation": the natural {target} equivalent for the meaning IN THIS CONTEXT, never word for word. 1 or 2 options separated by " / ", no explanations.

Output JSON only: {{"items":[{{"line":1,"expression":"...","translation":"..."}}]}}. If nothing is worth learning, return {{"items":[]}}."""

# Ejemplos few-shot (inglés -> español): varias unidades por línea, líneas sin
# nada, intensificadores, nombres propios y palabras básicas descartadas.
FEW_SHOT = [
    (
        ["Honestly, I was gutted when they called off the wedding at the last minute."],
        '{"items":[{"line":1,"expression":"gutted","translation":"destrozado / hecho polvo"},'
        '{"line":1,"expression":"call off","translation":"cancelar / suspender"}]}',
    ),
    (
        [
            "Where did you park the car?",
            "Her colleagues resented the way she kept brushing off their concerns and cutting corners.",
        ],
        '{"items":[{"line":2,"expression":"resent","translation":"tomarse a mal / estar resentido por"},'
        '{"line":2,"expression":"brush off","translation":"ignorar / no hacer caso de"},'
        '{"line":2,"expression":"cut corners","translation":"hacer las cosas de cualquier manera / escatimar esfuerzos"}]}',
    ),
    (
        [
            "Mike said the hotel was incredibly shabby, but the staff were really nice.",
            "I couldn't figure out what he was getting at.",
        ],
        '{"items":[{"line":1,"expression":"shabby","translation":"destartalado / cutre"},'
        '{"line":2,"expression":"figure out","translation":"averiguar / entender"},'
        '{"line":2,"expression":"get at","translation":"insinuar / querer decir"}]}',
    ),
    (
        ["We had dinner near the supermarket and then went home.", "Thanks, see you tomorrow."],
        '{"items":[]}',
    ),
    (
        ["It was a scathing review, but deep down he knew it was spot on."],
        '{"items":[{"line":1,"expression":"scathing","translation":"mordaz / demoledor"},'
        '{"line":1,"expression":"deep down","translation":"en el fondo"},'
        '{"line":1,"expression":"spot on","translation":"totalmente acertado / en el clavo"}]}',
    ),
]

# Ejemplos few-shot para subtítulos en español (español -> inglés).
FEW_SHOT_ES = [
    (
        ["No me di cuenta de lo que estaba pasando.", "Ayer fui al supermercado."],
        '{"items":[{"line":1,"expression":"darse cuenta","translation":"to realize / to notice"}]}',
    ),
    (
        ["¿Adónde vas?", "Estaba completamente agobiado y, encima, mi jefe me tomó el pelo."],
        '{"items":[{"line":2,"expression":"agobiado","translation":"overwhelmed / stressed out"},'
        '{"line":2,"expression":"encima","translation":"on top of that / what\'s more"},'
        '{"line":2,"expression":"tomar el pelo","translation":"to pull someone\'s leg / to tease"}]}',
    ),
    (
        ["Gracias, hasta mañana.", "Te echo mucho de menos."],
        '{"items":[{"line":2,"expression":"echar de menos","translation":"to miss"}]}',
    ),
]

# Segunda pasada: el modelo pequeño a veces devuelve una lista vacía (o se para
# antes de tiempo) con líneas que sí tienen vocabulario avanzado. Las palabras
# poco frecuentes que no evaluó se le vuelven a presentar una a una.
REVIEW_PROMPT = """You check study vocabulary from {source} subtitles for an advanced learner whose native language is {target} ({low} level, aiming at {high} and beyond).

Each line comes with "check:" words found in it. Judge EACH word as it is used in that line and return the ones worth learning at upper-B2, C1 or C2 level, with their translation.
- Keep: advanced words that are useful in general {source} (adjectives, verbs, nouns, adverbs).
- If the word is part of a phrasal verb, idiom or fixed expression in the line, return that whole expression instead.
- Skip: words a B2 learner already knows, names of people or places, {target} cognates with the same meaning, very technical terms, OCR errors.

"expression": dictionary form, lowercase. "translation": natural {target} for the meaning IN THIS CONTEXT, 1 or 2 options separated by " / ".
Output JSON only: {{"items":[{{"line":1,"expression":"...","translation":"..."}}]}}. Return {{"items":[]}} if none is worth learning."""

REVIEW_FEW_SHOT = [
    (
        "1. Her husband was a conscientious, if somewhat aloof, accountant.\n"
        "   check: conscientious, aloof, accountant",
        '{"items":[{"line":1,"expression":"conscientious","translation":"concienzudo / meticuloso"},'
        '{"line":1,"expression":"aloof","translation":"distante / reservado"}]}',
    ),
    (
        "1. Jenkins parked the motorbike next to the pharmacy.\n"
        "   check: jenkins, motorbike, pharmacy\n"
        "2. The kids were lugging their backpacks up the hill.\n"
        "   check: lugging, backpacks",
        '{"items":[{"line":2,"expression":"lug","translation":"cargar con / arrastrar"}]}',
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
                    "translation": {"type": "string"},
                },
                "required": ["line", "expression", "translation"],
            },
        }
    },
    "required": ["items"],
}


def build_messages(
    lines: list[str],
    *,
    target_level: str = "B2-C1",
    source_language: str = "English",
    target_language: str = "Spanish",
    hints: bool = True,
) -> list[dict[str, str]]:
    low, _, high = target_level.partition("-")
    spanish_source = source_language == "Spanish"
    template = SYSTEM_PROMPT_ES if spanish_source else SYSTEM_PROMPT
    system = template.format(
        source=source_language, target=target_language, low=low or "B2", high=high or "C1"
    )
    lang = "es" if spanish_source else "en"
    messages = [{"role": "system", "content": system}]
    for examples, assistant in FEW_SHOT_ES if spanish_source else FEW_SHOT:
        messages.append({"role": "user", "content": render_lines(examples, lang, hints)})
        messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": render_lines(lines, lang, hints)})
    return messages


def render_lines(lines: list[str], lang: str = "en", hints: bool = True) -> str:
    """Líneas numeradas, cada una con sus candidatos (ver vocabulary/candidates.py)."""
    return "\n".join(
        f"{i}. {line}{hint_line(line, lang) if hints else ''}" for i, line in enumerate(lines, start=1)
    )


def build_review_messages(
    checks: list[tuple[str, list[str]]],
    *,
    target_level: str = "B2-C1",
    source_language: str = "English",
    target_language: str = "Spanish",
) -> list[dict[str, str]]:
    """Mensajes de la segunda pasada: (línea, palabras por evaluar) por cada línea."""
    low, _, high = target_level.partition("-")
    system = REVIEW_PROMPT.format(
        source=source_language, target=target_language, low=low or "B2", high=high or "C1"
    )
    messages = [{"role": "system", "content": system}]
    if source_language == "English":
        for user, assistant in REVIEW_FEW_SHOT:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": assistant})
    body = "\n".join(
        f"{i}. {line}\n   check: {', '.join(words)}" for i, (line, words) in enumerate(checks, start=1)
    )
    messages.append({"role": "user", "content": body})
    return messages
