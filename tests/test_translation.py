"""Traducción contextual: parseo, limpieza, motor Ollama, errores y coste cero.

Los tests sin marca usan un cliente con respuestas fijas: prueban el código del
traductor, NO la calidad del modelo. La calidad real se mide con
tools/benchmark_translation.py (ver README) y con los tests @llm.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from wordlens.ai.ollama_client import OllamaClient, OllamaUnavailableError
from wordlens.translation.translator import (
    PROMPT_VERSION,
    OllamaTranslator,
    TranslationItem,
    build_messages,
    parse_response,
    tidy,
)

ROOT = Path(__file__).resolve().parents[1]
BRUSH = "He brushed off my concerns and went ahead with the deal."
ITEMS = [TranslationItem("brush off", "phrasal verb"), TranslationItem("concern", "word"),
         TranslationItem("go ahead with", "phrasal verb"), TranslationItem("deal", "word")]


class CannedClient:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []

    def chat(self, model, messages, **kw):
        self.calls.append((model, messages, kw))
        if self.error:
            raise self.error
        return {"message": {"content": self.content}}


def reply(*texts):
    return json.dumps({"translations": [{"n": i, "t": t} for i, t in enumerate(texts, 1)]}, ensure_ascii=False)


def test_request_carries_the_whole_sentence_and_every_item():
    msgs = build_messages(BRUSH, ITEMS)
    last = msgs[-1]["content"]
    assert BRUSH in last
    for i, it in enumerate(ITEMS, 1):
        assert f"{i}. {it.expression} ({it.kind})" in last
    assert "IN THAT LINE" in msgs[0]["content"]
    # Los ejemplos no contienen ninguna frase del benchmark.
    cases = (ROOT / "tests" / "benchmark" / "translation_cases.jsonl").read_text(encoding="utf-8")
    for m in msgs[1:-1]:
        if m["role"] == "user":
            sentence = m["content"].splitlines()[0].removeprefix("Line: ")
            assert sentence not in cases


def test_translate_one_per_item_in_order():
    client = CannedClient(reply("restar importancia a", "preocupación", "seguir adelante con", "acuerdo"))
    out = OllamaTranslator(client, "gemma3:4b").translate(BRUSH, ITEMS)
    assert out == ["restar importancia a", "preocupación", "seguir adelante con", "acuerdo"]
    assert client.calls[0][0] == "gemma3:4b"


def test_missing_or_garbage_items_are_none():
    raw = '{"translations": [{"n": 2, "t": "preocupación"}, {"n": 9, "t": "x"}]}'
    assert parse_response(raw, 3) == [None, "preocupación", None]
    assert parse_response("sorry, I can't", 2) == [None, None]
    truncated = '{"translations": [{"n": 1, "t": "aguantar"}, {"n": 2, "t": "sopor'
    assert parse_response(truncated, 2) == ["aguantar", None]


def test_tidy():
    assert tidy("aguantar, soportar, tolerar", "put up with") == "aguantar / soportar"
    assert tidy("dar en el clavo (acertar)", "hit the nail on the head") == "dar en el clavo"
    assert tidy("deal", "deal") is None  # sin traducir
    assert tidy("", "deal") is None
    assert tidy("fusion / fusión", "merger") == "fusión"


def test_engine_errors_propagate_so_candidates_are_kept():
    tr = OllamaTranslator(CannedClient(error=OllamaUnavailableError("down")), "gemma3:4b")
    with pytest.raises(OllamaUnavailableError):
        tr.translate(BRUSH, ITEMS)


def test_engine_id_changes_with_model_and_prompt():
    a = OllamaTranslator(CannedClient(), "gemma3:4b").engine_id
    b = OllamaTranslator(CannedClient(), "maternion/ling-3.0-tiny:8b").engine_id
    assert a != b and f"p{PROMPT_VERSION}" in a


def test_spanish_to_english_direction():
    msgs = build_messages("No me di cuenta.", [TranslationItem("darse cuenta", "expression")], "es", "en")
    assert "English side of a Spanish learner" in msgs[0]["content"]
    assert len(msgs) == 2  # sin ejemplos en->es


# ------------------------------------------------------------- coste cero
def test_translator_only_talks_to_local_ollama():
    """El traductor elegido sólo puede hablar con Ollama en este PC: no hay ruta de pago."""
    assert OllamaClient("http://localhost:11434").host == "127.0.0.1"
    for path in (ROOT / "wordlens" / "translation").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "https://" not in source and "http://" not in source, path
        tree = ast.parse(source)
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not imported & {"requests", "urllib", "httpx", "http", "socket", "aiohttp"}, path


def test_no_paid_translation_api_anywhere():
    banned = ("translate.googleapis", "translation.googleapis", "api.deepl.com", "api-free.deepl.com",
              "cognitive.microsofttranslator", "api.cognitive", "libretranslate.com", "mymemory")
    for path in (ROOT / "wordlens").rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for host in banned:
            assert host not in text, f"{host} en {path}"


# ---------------------------------------------------------- modelo real
@pytest.mark.llm
def test_llm_contextual_translation_of_required_example():
    tr = OllamaTranslator(OllamaClient(timeout=300), "gemma3:4b")
    out = tr.translate(BRUSH, ITEMS)
    joined = [o or "" for o in out]
    assert any(w in joined[1] for w in ("preocupaci", "inquietud"))
    assert any(w in joined[3] for w in ("acuerdo", "trato", "negocio"))
    assert all(joined), out


@pytest.mark.llm
def test_llm_polysemy_deal():
    tr = OllamaTranslator(OllamaClient(timeout=300), "gemma3:4b")
    [cards] = tr.translate("She deals cards at the casino.", [TranslationItem("deal", "word")])
    [business] = tr.translate("We closed the deal yesterday.", [TranslationItem("deal", "word")])
    assert cards != business


def test_invented_words_are_dropped():
    assert tidy("pocilga, basuraleza", "dump") == "pocilga"
    assert tidy("basuraleza", "dump") is None
    assert tidy("desquitarse con", "take out on") == "desquitarse con"  # pronombre pegado: existe
    assert tidy("estar pachucho", "under the weather") == "estar pachucho"
