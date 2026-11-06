"""Traducción contextual de candidatos, separada de la extracción.

La extracción decide QUÉ unidades hay en un subtítulo; aquí se decide qué
significan EN ESE subtítulo. Cada petición lleva la línea completa y la lista
numerada de unidades de esa línea, así el modelo distingue "deal" (acuerdo) de
"deal" (repartir cartas) sin traducir palabras sueltas a ciegas.

El motor es intercambiable (interfaz Translator). El elegido tras comparar
alternativas (ver tools/benchmark_translation.py y README) es un modelo local
de Ollama: coste 0 €, sin cuentas, sin red, sin límites de uso.

engine_id identifica motor + modelo + versión del prompt: las traducciones
guardadas con otro engine_id se consideran obsoletas y se regeneran.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from wordfreq import zipf_frequency

from wordlens.ai.ollama_client import OllamaClient
from wordlens.vocabulary.extractor import clean_translation
from wordlens.vocabulary.normalization import fold

log = logging.getLogger(__name__)

PROMPT_VERSION = 1


@dataclass(frozen=True)
class TranslationItem:
    expression: str
    kind: str = "word"  # word | phrasal verb | idiom | collocation | expression


class Translator(Protocol):
    engine_id: str

    def translate(self, sentence: str, items: list[TranslationItem]) -> list[str | None]:
        """Una traducción por ítem (None si el motor no dio una válida).

        Lanza OllamaError (u otra excepción del motor) si el motor no responde:
        quien llama conserva los ítems y reintenta más tarde.
        """
        ...


# ----------------------------------------------------------------- prompt
SYSTEM_PROMPT = """You write the {target} side of a {source} learner's vocabulary notebook.
You get one subtitle line and a numbered list of words or expressions taken from that line.
For each one, write the {target} equivalent of what it means IN THAT LINE.

Rules:
- Translate the meaning in this context, not the most common meaning of the word.
- Dictionary form: infinitive for verbs and phrasal verbs, singular for nouns unless only the plural makes sense, masculine singular for adjectives.
- Idioms and set phrases: the natural {target} equivalent expression. If none exists, a short plain meaning. Never a word-for-word calque.
- Slang, swear words and insults: an informal {target} word with the same tone.
- Keep it short, as in a dictionary: usually 1 to 4 words. Give a second option only when both are common and correct, separated by ", ".
- No explanations, no parentheses, no {source} words, no quotes.
{variety}
Answer only with JSON: {{"translations": [{{"n": 1, "t": "..."}}, {{"n": 2, "t": "..."}}]}} with one entry per item, in order."""

VARIETY = {
    "es": "- Use standard Spanish from Spain that is also understood in Latin America.",
    "en": "- Use standard English.",
}

FEW_SHOT_EN_ES = [
    (
        "She shrugged off the criticism and kept plugging away at her novel.",
        [("shrug off", "phrasal verb"), ("criticism", "word"), ("plug away at", "phrasal verb"), ("novel", "word")],
        ["restar importancia a", "críticas", "seguir dándole a, perseverar en", "novela"],
    ),
    (
        "Cut it out, you're driving me nuts! We can barely make ends meet.",
        [("cut it out", "idiom"), ("drive someone nuts", "idiom"), ("barely", "word"), ("make ends meet", "idiom")],
        ["¡basta ya!, déjalo ya", "volver loco a alguien, sacar de quicio", "apenas", "llegar a fin de mes"],
    ),
    (
        "The board shelved the takeover, and the stock plummeted.",
        [("board", "word"), ("shelve", "word"), ("takeover", "word"), ("stock", "word"), ("plummet", "word")],
        ["consejo de administración", "aparcar, archivar", "adquisición, absorción", "acciones", "desplomarse"],
    ),
]

LANGUAGE_NAMES = {"en": "English", "es": "Spanish"}


def render_request(sentence: str, items: list[TranslationItem]) -> str:
    lines = [f"Line: {sentence.strip()}", "Items:"]
    lines += [f"{i}. {it.expression} ({it.kind})" for i, it in enumerate(items, 1)]
    return "\n".join(lines)


def build_messages(sentence: str, items: list[TranslationItem], source: str = "en", target: str = "es") -> list[dict]:
    system = SYSTEM_PROMPT.format(
        source=LANGUAGE_NAMES.get(source, source),
        target=LANGUAGE_NAMES.get(target, target),
        variety=VARIETY.get(target, ""),
    )
    messages = [{"role": "system", "content": system}]
    if (source, target) == ("en", "es"):
        for line, shot_items, answers in FEW_SHOT_EN_ES:
            req = [TranslationItem(e, k) for e, k in shot_items]
            messages.append({"role": "user", "content": render_request(line, req)})
            payload = {"translations": [{"n": i, "t": a} for i, a in enumerate(answers, 1)]}
            messages.append({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)})
    messages.append({"role": "user", "content": render_request(sentence, items)})
    return messages


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "t": {"type": "string"}},
                "required": ["n", "t"],
            },
        }
    },
    "required": ["translations"],
}

_PAIR = re.compile(r'"n"\s*:\s*(\d+)\s*,\s*"t"\s*:\s*"((?:[^"\\]|\\.)*)"')


def parse_response(raw: str, count: int) -> list[str | None]:
    """Respuesta del modelo -> una traducción (o None) por ítem, en orden.

    Tolera JSON truncado o con basura alrededor: se recuperan los pares n/t
    que estén completos. Un número fuera de rango se ignora.
    """
    out: list[str | None] = [None] * count
    pairs: list[tuple[int, str]] = []
    try:
        data = json.loads(raw)
        for entry in data.get("translations", []) if isinstance(data, dict) else []:
            n, t = entry.get("n"), entry.get("t")
            if isinstance(n, int) and isinstance(t, str):
                pairs.append((n, t))
    except (json.JSONDecodeError, AttributeError):
        for n, t in _PAIR.findall(raw):
            try:
                pairs.append((int(n), json.loads(f'"{t}"')))
            except json.JSONDecodeError:
                continue
    for n, t in pairs:
        if 1 <= n <= count and out[n - 1] is None:
            out[n - 1] = t
    return out


_CLITICS = ("selas", "selos", "sela", "selo", "nos", "les", "los", "las", "se", "me", "te", "le", "lo", "la", "os")


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def is_invented(word: str, lang: str = "es") -> bool:
    """¿Palabra que no existe en el idioma destino? ("basuraleza").

    wordfreq hace de diccionario. Un verbo con pronombres pegados
    ("desquitarse", "cagársela") se comprueba también sin ellos.
    """
    w = word.lower()
    if len(w) < 4 or zipf_frequency(w, lang) > 0:
        return False
    if lang == "es":
        for clitic in _CLITICS:
            if w.endswith(clitic) and zipf_frequency(_strip_accents(w[: -len(clitic)]), lang) > 0:
                return False
    return True


def tidy(translation: str | None, expression: str, target: str = "es") -> str | None:
    """Limpieza: opciones duplicadas, paréntesis, comillas, máximo dos opciones.

    Se quitan las opciones con palabras que no existen en el idioma destino.
    Devuelve None si no queda traducción útil (vacía, inventada o igual al original).
    """
    if not translation:
        return None
    text = clean_translation(translation.strip())
    kept = [
        opt for opt in text.split(" / ")
        if opt and not any(is_invented(w, target) for w in re.findall(r"[^\W\d_]+", opt))
    ]
    if len(kept) < len([o for o in text.split(" / ") if o]):
        log.info("Traducción con palabras inexistentes descartada: %r -> %r", expression, text)
    text = " / ".join(kept)
    if not text:
        return None
    if fold(text).lower().strip(" !?¡¿") == fold(expression).lower().strip():
        return None
    return text


class OllamaTranslator:
    """Traductor con un modelo local de Ollama, una petición por subtítulo."""

    def __init__(
        self,
        client: OllamaClient,
        model: str,
        *,
        source: str = "en",
        target: str = "es",
        keep_alive: str = "10m",
        temperature: float = 0.0,
    ) -> None:
        self.client = client
        self.model = model
        self.source = source
        self.target = target
        self.keep_alive = keep_alive
        self.temperature = temperature
        self.engine_id = f"ollama:{model}:{source}-{target}:p{PROMPT_VERSION}"

    def translate(self, sentence: str, items: list[TranslationItem]) -> list[str | None]:
        if not items:
            return []
        start = time.perf_counter()
        response = self.client.chat(
            self.model,
            build_messages(sentence, items, self.source, self.target),
            schema=RESPONSE_SCHEMA,
            temperature=self.temperature,
            num_predict=80 + 40 * len(items),
            keep_alive=self.keep_alive,
        )
        raw = response.get("message", {}).get("content", "")
        log.info("Traducción %.1fs (%s, %d ítems): %s", time.perf_counter() - start, self.model, len(items), raw)
        parsed = parse_response(raw, len(items))
        return [tidy(t, it.expression, self.target) for t, it in zip(parsed, items, strict=True)]
