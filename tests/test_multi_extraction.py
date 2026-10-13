"""Extracción de VARIOS elementos por subtítulo (versión 1.1).

Antes, el modelo solía devolver sólo el primer elemento de cada línea y una
respuesta JSON mal cerrada hacía perder la línea entera. Estos tests cubren:
- que nada después del LLM recorte la lista (validación, deduplicación, Word);
- la recuperación de JSON roto;
- los candidatos que guían al modelo y la segunda pasada;
- el filtro de nivel (no se cuelan palabras básicas);
- con el modelo real (marca llm): 0, 1, 2, 3 y 4 elementos por frase.
"""

from __future__ import annotations

import json

import pytest

from wordlens.ai.prompts import FEW_SHOT, build_messages, build_review_messages, render_lines
from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer, _prefer_longest
from wordlens.config.settings import Settings
from wordlens.document.word_writer import WordWriter
from wordlens.vocabulary.candidates import find_candidates, hint_line, proper_nouns
from wordlens.vocabulary.deduplicator import Deduplicator
from wordlens.vocabulary.extractor import parse_llm_json, validate_item
from wordlens.vocabulary.models import VocabularyItem


class ScriptedClient:
    """Cliente falso: devuelve una respuesta distinta en cada llamada."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, model, messages, **kw):
        self.calls.append(messages)
        content = self.responses.pop(0) if self.responses else '{"items":[]}'
        return {"message": {"content": content}}


def items_json(*items, line=1):
    return json.dumps({"items": [{"line": line, "expression": e, "translation": t} for e, t in items]})


THREE = "Despite the setback, she persevered and eventually pulled it off."
FOUR = "The board decided to shelve the merger after the stock plummeted and shareholders started to balk."


# ---------------------------------------------------------------- sin recortes
def test_all_items_of_one_line_are_kept():
    line = "The CEO's blunt remarks backfired and tarnished the company's reputation."
    client = ScriptedClient(
        items_json(("blunt", "directo / franco"), ("backfire", "salir el tiro por la culata"), ("tarnish", "empañar"))
    )
    items = VocabularyAnalyzer(Settings(), client).analyze([line])
    assert [i.expression for i in items] == ["blunt", "backfire", "tarnish"]
    assert all(i.context == line for i in items)
    assert len(client.calls) == 1  # todo evaluado: no hace falta segunda pasada


def test_four_items_survive_dedup_and_are_all_written(tmp_path):
    client = ScriptedClient(
        items_json(
            ("shelve", "aparcar / posponer"),
            ("merger", "fusión"),
            ("plummet", "desplomarse / caer en picado"),
            ("balk", "resistirse / echarse atrás"),
        )
    )
    items = VocabularyAnalyzer(Settings(), client).analyze([FOUR])
    assert len(items) == 4
    dedup = Deduplicator()
    new = dedup.filter_new(items)
    assert len(new) == 4
    writer = WordWriter(tmp_path / "v.docx", backup_dir=tmp_path / "b")
    writer.ensure_exists()
    written = writer.add_entries(new)
    assert [w.expression for w in written] == ["shelve", "merger", "plummet", "balk"]
    assert writer.read_expressions() == ["shelve", "merger", "plummet", "balk"]


def test_items_from_several_lines_in_one_batch():
    lines = ["I can't put up with his constant whining anymore.", "We had to scrap the plan and start from scratch."]
    content = json.dumps({"items": [
        {"line": 1, "expression": "put up with", "translation": "aguantar / soportar"},
        {"line": 1, "expression": "whining", "translation": "quejas / lloriqueo"},
        {"line": 2, "expression": "scrap", "translation": "descartar / desechar"},
        {"line": 2, "expression": "from scratch", "translation": "desde cero"},
    ]})
    items = VocabularyAnalyzer(Settings(), ScriptedClient(content)).analyze(lines)
    assert [(i.expression, i.context) for i in items] == [
        ("put up with", lines[0]), ("whining", lines[0]), ("scrap", lines[1]), ("from scratch", lines[1]),
    ]


# ---------------------------------------------------------- JSON mal formado
def test_broken_json_keeps_every_complete_item():
    # Respuestas reales del modelo (falta la llave de cierre de un objeto).
    raw = ('{"items":[{"line":1,"expression":"setback","translation":"revés / obstáculo"},'
           '{"line":1,"expression":"persevere","translation":"perseverar"], '
           '"line":1,"expression":"pull off","translation":"lograr / llevar a cabo"}]}')
    assert [i["expression"] for i in parse_llm_json(raw)] == ["setback", "persevere", "pull off"]
    raw2 = '{"items":[{"line":1,"expression":"plugging in","translation":"conectar"], "line":2,"expression":"saving","translation":"guardar"}]}'
    assert [(i["line"], i["expression"]) for i in parse_llm_json(raw2)] == [(1, "plugging in"), (2, "saving")]


def test_truncated_json_keeps_finished_items():
    raw = '{"items":[{"line":1,"expression":"shelve","translation":"aparcar"},{"line":1,"expression":"mer'
    assert parse_llm_json(raw) == [{"line": 1, "expression": "shelve", "translation": "aparcar"}]


def test_escaped_quotes_in_salvaged_items():
    raw = r'{"items":[{"line":1,"expression":"spot on","translation":"\"en el clavo\""}'
    assert parse_llm_json(raw)[0]["translation"] == '"en el clavo"'


# ------------------------------------------------------------- candidatos
def test_candidates_find_rare_words_and_phrasal_verbs():
    c = find_candidates("He came up with a far-fetched excuse to get out of the meeting.")
    assert c.rare_words == ["far-fetched"]
    assert c.phrasal == ["come up with", "get out of"]
    c = find_candidates(FOUR)
    assert {"shelve", "merger", "plummeted", "balk"} <= set(c.rare_words)
    assert "board" not in c.rare_words and "decided" not in c.rare_words


def test_candidates_skip_basic_words_names_and_ocr_noise():
    assert not find_candidates("Where are you going tonight? I think we should order pizza.")
    assert find_candidates("We called it Shocky and drove to Queens.").rare_words == []
    assert "detectlve" not in find_candidates("The detectlve found it.").rare_words
    assert proper_nouns("Yesterday Kevin met Lopez. Then he left.") == {"kevin", "lopez"}


def test_phrasal_candidates_skip_objects():
    assert find_candidates("I'm going to track him down.").phrasal == ["track down"]
    assert find_candidates("I can't believe you talked me into this.").phrasal == ["talk into"]


def test_hints_are_appended_to_each_line():
    text = render_lines(["He was reluctant to admit it.", "Where are you?"])
    assert text.splitlines()[0].endswith("[less common words: reluctant]")
    assert text.splitlines()[1] == "2. Where are you?"
    assert render_lines(["He was reluctant."], hints=False) == "1. He was reluctant."
    assert hint_line("Thanks, see you tomorrow.") == ""


# ------------------------------------------------------------------ prompt
def test_prompt_asks_for_every_item_and_shows_multi_item_examples():
    msgs = build_messages([THREE])
    system = msgs[0]["content"]
    assert "Do not stop after the first one" in system
    assert "0, 1, 2, 3 or more items" in system
    assert "C1" in system and "C2" in system
    counts = [len(json.loads(answer)["items"]) for _, answer in FEW_SHOT]
    assert 0 in counts and max(counts) >= 3 and sum(c >= 2 for c in counts) >= 3
    assert msgs[-1]["content"].startswith(f"1. {THREE}")
    assert "less common words: setback, persevered" in msgs[-1]["content"]


def test_examples_are_not_benchmark_sentences():
    from pathlib import Path

    bench = Path(__file__).parent / "benchmark" / "extraction_cases.json"
    texts = {c["text"] for c in json.loads(bench.read_text(encoding="utf-8"))["cases"]}
    for examples, _ in FEW_SHOT:
        assert not texts.intersection(examples)


# -------------------------------------------------------- segunda pasada
def test_review_pass_recovers_words_the_model_skipped():
    line = "The witness was adamant that she had never seen the defendant."
    client = ScriptedClient(
        '{"items":[]}',  # primera pasada: el modelo se rinde (caso real)
        items_json(("adamant", "firme / categórico"), ("defendant", "acusado")),
    )
    items = VocabularyAnalyzer(Settings(), client).analyze([line])
    assert [i.expression for i in items] == ["adamant", "defendant"]
    review_user = client.calls[1][-1]["content"]
    assert review_user == f"1. {line}\n   check: adamant, defendant"


def test_review_only_asks_about_words_not_yet_evaluated():
    client = ScriptedClient(items_json(("shelve", "aparcar"), ("balk", "resistirse")), '{"items":[]}')
    VocabularyAnalyzer(Settings(), client).analyze([FOUR])
    check = client.calls[1][-1]["content"].split("check: ")[1]
    assert "shelve" not in check and "balk" not in check
    assert "merger" in check and "plummeted" in check


def test_rejected_items_are_not_asked_again():
    # El modelo ya valoró "supermarket" (el validador lo descarta por cognado): no hay segunda llamada.
    client = ScriptedClient(items_json(("supermarket", "supermercado")))
    assert VocabularyAnalyzer(Settings(), client).analyze(["I went to the supermarket with my sister."]) == []
    assert len(client.calls) == 1


def test_review_pass_can_be_disabled():
    client = ScriptedClient('{"items":[]}')
    VocabularyAnalyzer(Settings(review_pass=False), client).analyze(["The witness was adamant about it."])
    assert len(client.calls) == 1


def test_review_does_not_duplicate_items():
    line = "Despite the setback, she pulled it off."
    client = ScriptedClient(items_json(("pull off", "lograr")), items_json(("setback", "revés"), ("pull off", "lograr")))
    items = VocabularyAnalyzer(Settings(), client).analyze([line])
    assert sorted(i.expression for i in items) == ["pull off", "setback"]


def test_review_messages_format():
    msgs = build_review_messages([("A line here.", ["alpha", "beta"])])
    assert msgs[-1]["content"] == "1. A line here.\n   check: alpha, beta"
    assert "Judge EACH word" in msgs[0]["content"]


def test_longest_unit_wins():
    ctx = "sooner or later it'll come back to bite us"
    items = [VocabularyItem("come back", "volver", ctx), VocabularyItem("come back to bite", "pasar factura", ctx),
             VocabularyItem("come back", "volver", "other line")]
    assert [(i.expression, i.context) for i in _prefer_longest(items)] == [
        ("come back to bite", ctx), ("come back", "other line"),
    ]


# ---------------------------------------------- filtro de nivel y limpieza
@pytest.mark.parametrize(
    "expression,translation,context,expected",
    [
        ("incredibly tacky", "hortera", "the incredibly tacky swords", "tacky"),
        ("utterly uncalled for", "fuera de lugar", "His remarks were utterly uncalled for.", "uncalled for"),
        ("reluctant", "reluctante / poco dispuesto", "He was reluctant to go.", "reluctant"),
    ],
)
def test_cleanup(expression, translation, context, expected):
    item = validate_item({"expression": expression, "translation": translation}, context)
    assert item is not None and item.expression == expected


def test_invented_cognate_translation_is_dropped():
    item = validate_item({"expression": "reluctant", "translation": "reluctante / poco dispuesto"}, "He was reluctant.")
    assert item.translation == "poco dispuesto"
    assert validate_item({"expression": "reluctant", "translation": "reluctante"}, "He was reluctant.") is None


@pytest.mark.parametrize(
    "expression,translation,context",
    [
        ("Shocky", "Shocky", "We called it Shocky."),  # nombre propio
        ("Queens", "Queens", "We were in Queens and we left."),
        ("detectlve", "detective", "The detectlve found a clue."),  # error de OCR
        ("tell what you want", "dime lo que quieres", "Stop it and tell me what you want."),  # oración
        ("admit", "admitir", "He refused to admit it."),
        ("situation", "situación", "The situation is bad."),
        ("ridiculous", "ridículo", "That's ridiculous."),
    ],
)
def test_basic_or_invalid_items_are_rejected(expression, translation, context):
    assert validate_item({"expression": expression, "translation": translation}, context) is None


# ------------------------------------------------------ con el modelo real
CASES = {
    0: [
        "I went to the supermarket yesterday with my sister.",
        "Can you help me carry these boxes to the car?",
        "When was the last time you were there? Three months ago.",
        "I think we should order pizza and watch a movie.",
    ],
    1: [
        ("The bridge looked too flimsy to hold our weight.", {"flimsy"}),
        ("Her explanation was pretty convoluted.", {"convoluted"}),
        ("I'm not going to sugarcoat it, the results are bad.", {"sugarcoat"}),
    ],
    2: [
        ("He was reluctant to admit that he had completely underestimated the situation.", {"reluctant", "underestimate"}),
        ("The plan seemed foolproof until the investors pulled out.", {"foolproof", "pull out"}),
        ("I'm swamped with work, so I'll have to take a rain check.", {"swamped", "take a rain check"}),
        ("The negotiations stalled because neither side was willing to budge.", {"stall", "budge"}),
    ],
    3: [
        ("The CEO's blunt remarks backfired and tarnished the company's reputation.", {"blunt", "backfire", "tarnish"}),
        ("He came up with a far-fetched excuse to get out of the meeting.", {"come up with", "far-fetched", "get out of"}),
    ],
    4: [
        ("I'm fed up with your lame excuses; quit stalling and come clean about what happened.",
         {"fed up with", "lame", "stall", "come clean"}),
        (FOUR, {"shelve", "merger", "plummet", "balk"}),
    ],
}


def _matches(found: str, expected: str) -> bool:
    from wordlens.vocabulary.normalization import lemma

    f = [lemma(t) for t in found.replace("-", " ").split() if t not in {"a", "the", "be"}]
    e = [lemma(t) for t in expected.replace("-", " ").split() if t not in {"a", "the", "be"}]
    return f == e or (len(e) == 1 and e[0] in f and len(f) <= 2) or (len(f) == 1 and f[0] in e)


@pytest.mark.llm
def test_real_model_detects_several_items_per_line_without_basic_words():
    analyzer = VocabularyAnalyzer(Settings())
    for line in CASES[0]:
        assert analyzer.analyze([line]) == [], line
    report = []
    for n in (1, 2, 3, 4):
        for line, expected in CASES[n]:
            found = [i.expression for i in analyzer.analyze([line])]
            hits = {e for e in expected if any(_matches(f, e) for f in found)}
            report.append((n, len(hits), line, found))
    print("\n".join(f"{n} esperados, {h} detectados: {found}  <- {line}" for n, h, line, found in report))
    multi = [r for r in report if r[0] >= 2]
    # Lo que se arregla: con varios elementos ya no se queda sólo con el primero.
    assert sum(h >= 2 for _, h, _, _ in multi) >= len(multi) - 1
    assert sum(h for _, h, _, _ in report) >= 0.75 * sum(n for n, *_ in report)
    assert any(n >= 3 and h >= 3 for n, h, _, _ in report)
