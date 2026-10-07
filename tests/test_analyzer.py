"""Análisis de vocabulario.

- Los tests sin marca usan un cliente que devuelve respuestas fijas para probar
  el parseo/validación de forma determinista (no simulan la app final).
- Los tests marcados @llm usan Ollama y el modelo real.
"""

import json

import pytest

from vocablens.ai.prompts import build_messages
from vocablens.ai.vocabulary_analyzer import VocabularyAnalyzer
from vocablens.config.settings import Settings


class CannedClient:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, model, messages, **kw):
        self.calls.append(messages)
        return {"message": {"content": self.content}}


def analyzer_with(content):
    client = CannedClient(content)
    return VocabularyAnalyzer(Settings(), client), client


def test_valid_response_is_validated_and_canonicalised():
    content = json.dumps({"items": [
        {"line": 1, "expression": "figure out", "translation": "averiguar / descubrir"},
        {"line": 1, "expression": "getting at", "translation": "insinuar, querer decir"},
        {"line": 2, "expression": "supermarket", "translation": "supermercado"},
        {"line": 9, "expression": "went", "translation": "fui"},
    ]})
    an, _ = analyzer_with(content)
    items = an.analyze(["I couldn't figure out what he was getting at.", "I went to the supermarket yesterday."])
    assert [(i.expression, i.translation) for i in items] == [
        ("figure out", "averiguar / descubrir"),
        ("get at", "insinuar / querer decir"),
    ]
    assert items[0].context == "I couldn't figure out what he was getting at."


@pytest.mark.parametrize("garbage", ["", "lo siento, no puedo", '{"items": [{"foo": 1}]}', "{{{{"])
def test_garbage_response_produces_nothing(garbage):
    an, _ = analyzer_with(garbage)
    assert an.analyze(["I couldn't figure it out."]) == []


def test_trivial_lines_skip_llm():
    an, client = analyzer_with('{"items": []}')
    assert an.analyze(["Where are you going?", "Thank you."]) == []
    assert client.calls == []


def test_prompt_contains_full_context_and_numbering():
    msgs = build_messages(["First line here.", "Second line here."])
    assert msgs[0]["role"] == "system" and "B2" in msgs[0]["content"] and "C1" in msgs[0]["content"]
    assert msgs[-1]["content"] == "1. First line here.\n2. Second line here."
    # El prefijo (system + ejemplos) es idéntico entre llamadas -> caché de Ollama
    assert build_messages(["x"])[:-1] == msgs[:-1]


# ---------------------------------------------------------------- LLM real
@pytest.fixture(scope="module")
def real():
    return VocabularyAnalyzer(Settings())


@pytest.mark.llm
def test_llm_example_from_spec(real):
    items = real.analyze(["I couldn't figure out what he was getting at."])
    exprs = {i.expression for i in items}
    assert "figure out" in exprs
    assert all(i.translation for i in items)


@pytest.mark.llm
def test_llm_take_for_granted(real):
    exprs = {i.expression for i in real.analyze(["You shouldn't take it for granted."])}
    assert exprs & {"take for granted", "take it for granted"}


@pytest.mark.llm
def test_llm_no_basic_vocabulary(real):
    items = real.analyze(["I went to the supermarket yesterday."])
    assert not {i.expression for i in items} & {"went", "go", "supermarket", "yesterday"}


@pytest.mark.llm
def test_llm_translation_is_spanish_and_contextual(real):
    items = real.analyze(["He was completely overwhelmed by the situation."])
    item = next(i for i in items if i.expression == "overwhelmed")
    assert any(w in item.translation.lower() for w in ("abrumado", "sobrepasado", "agobiado", "desbordado"))
