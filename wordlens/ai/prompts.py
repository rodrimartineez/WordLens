"""Prompts del filtro de vocabulario.

El prompt de sistema y los ejemplos son fijos para que Ollama reutilice la caché
de prefijo (KV cache): sólo se procesa de nuevo el mensaje final con las líneas,
lo que reduce mucho la latencia por llamada.
"""

from __future__ import annotations

SYSTEM_PROMPT = """You extract study vocabulary from {source} subtitles for a {target} speaker at {low} level who wants to reach {high}.

Return ONLY items a {low} learner would likely NOT know or would misunderstand:
- phrasal verbs (figure out, get at, make out, put up with)
- idioms and fixed expressions (take for granted, call it a day, a long shot)
- strong collocations and colloquial phrases
- advanced or infrequent words (overwhelmed, flimsy, reckless)
- words whose meaning here is different from the usual one

NEVER return: basic everyday words (go, house, eat, good, day, people, make, have, supermarket, yesterday, time, weeks), whole sentences, questions, greetings, names, pronouns, numbers, or simple combinations whose meaning is obvious from the parts ("for weeks", "what she was saying", "go to the supermarket").
Ignore words that look like OCR errors.

Rules for "expression":
- dictionary form: base verb ("getting at" -> "get at", "running out of" -> "run out of")
- remove personal objects ("figure him out" -> "figure out", "under my skin" -> "under one's skin")
- 1 to 5 words, lowercase

Rules for "translation": natural {target} for the meaning IN THIS CONTEXT, 1 or 2 options separated by " / ", no explanations.

Be conservative: it is better to miss an item than to save a basic one. Most lines contain nothing worth saving; then return {{"items": []}}.
Output JSON only: {{"items":[{{"line":1,"expression":"...","translation":"..."}}]}}"""

SYSTEM_PROMPT_ES = """You extract study vocabulary from Spanish subtitles for a {target} speaker at {low} level who wants to reach {high}.

Return ONLY items a {low} learner would likely NOT know or would misunderstand:
- idioms and fixed expressions (echar de menos, tomar el pelo, estar hasta las narices, vale la pena)
- pronominal verbs and verbal phrases with a special meaning (darse cuenta, quedarse con, llevarse bien, acabar de)
- strong collocations and colloquial phrases (menos mal, dar igual)
- advanced or infrequent words (agobiado, imprescindible, desamparado)
- words whose meaning here is different from the usual one

NEVER return: basic everyday words (ir, casa, comer, bueno, día, gente, hacer, tener, supermercado, ayer, tiempo, semanas), whole sentences, questions, greetings, names, pronouns, numbers, or simple combinations whose meaning is obvious from the parts ("por semanas", "lo que decía", "ir al supermercado").
Ignore words that look like OCR errors.

Rules for "expression":
- dictionary form: infinitive ("me di cuenta" -> "darse cuenta", "te echo de menos" -> "echar de menos")
- remove personal objects that are not part of the expression ("le tomó el pelo" -> "tomar el pelo")
- 1 to 5 words, lowercase, Spanish spelling with accents

Rules for "translation": natural {target} for the meaning IN THIS CONTEXT, 1 or 2 options separated by " / ", no explanations.

Be conservative: it is better to miss an item than to save a basic one. Most lines contain nothing worth saving; then return {{"items": []}}.
Output JSON only: {{"items":[{{"line":1,"expression":"...","translation":"..."}}]}}"""

# Ejemplos few-shot (inglés -> español). Enseñan formato, lematización y criterio.
FEW_SHOT = [
    (
        "1. I couldn't figure out what he was getting at.\n2. I went to the supermarket yesterday.",
        '{"items":[{"line":1,"expression":"figure out","translation":"averiguar / descubrir"},'
        '{"line":1,"expression":"get at","translation":"insinuar / querer decir"}]}',
    ),
    (
        "1. Where are you going?\n2. He was completely overwhelmed by the situation.",
        '{"items":[{"line":2,"expression":"overwhelmed","translation":"abrumado / sobrepasado"}]}',
    ),
    (
        "1. Thanks, see you tomorrow.\n2. You shouldn't take it for granted.",
        '{"items":[{"line":2,"expression":"take for granted","translation":"dar por sentado"}]}',
    ),
]

# Ejemplos few-shot para subtítulos en español (español -> inglés).
FEW_SHOT_ES = [
    (
        "1. No me di cuenta de lo que estaba pasando.\n2. Ayer fui al supermercado.",
        '{"items":[{"line":1,"expression":"darse cuenta","translation":"to realize / to notice"}]}',
    ),
    (
        "1. ¿Adónde vas?\n2. Estaba completamente agobiado por la situación.",
        '{"items":[{"line":2,"expression":"agobiado","translation":"overwhelmed / stressed out"}]}',
    ),
    (
        "1. Gracias, hasta mañana.\n2. Te echo mucho de menos.",
        '{"items":[{"line":2,"expression":"echar de menos","translation":"to miss"}]}',
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
) -> list[dict[str, str]]:
    low, _, high = target_level.partition("-")
    spanish_source = source_language == "Spanish"
    template = SYSTEM_PROMPT_ES if spanish_source else SYSTEM_PROMPT
    system = template.format(
        source=source_language, target=target_language, low=low or "B2", high=high or "C1"
    )
    messages = [{"role": "system", "content": system}]
    for user, assistant in FEW_SHOT_ES if spanish_source else FEW_SHOT:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1))
    messages.append({"role": "user", "content": numbered})
    return messages
