import pytest

from wordlens.vocabulary.extractor import (
    clean_translation,
    is_transparent_cognate,
    is_trivial_line,
    parse_llm_json,
    validate_item,
)


class TestParseJson:
    def test_valid(self):
        raw = '{"items":[{"expression":"figure out","translation":"averiguar"}]}'
        assert parse_llm_json(raw) == [{"expression": "figure out", "translation": "averiguar"}]

    def test_code_fence_and_prose(self):
        raw = 'Sure!\n```json\n{"items":[{"expression":"get at","translation":"insinuar"}]}\n```'
        assert parse_llm_json(raw)[0]["expression"] == "get at"

    def test_embedded_object(self):
        raw = 'Here you go: {"items": [{"expression": "a", "translation": "b"}]} hope it helps'
        assert len(parse_llm_json(raw)) == 1

    @pytest.mark.parametrize(
        "raw",
        ["", "not json", "{broken", '{"items": "nope"}', "[1, 2, 3]", '{"other": []}', None, "null", '"str"'],
    )
    def test_invalid_never_raises(self, raw):
        assert parse_llm_json(raw) == []

    def test_bare_list_and_non_dict_items(self):
        assert parse_llm_json('[{"expression":"x","translation":"y"}, 5, "z"]') == [
            {"expression": "x", "translation": "y"}
        ]


class TestValidate:
    ctx = "I couldn't figure out what he was getting at."

    def v(self, expression, translation, ctx=None):
        return validate_item({"expression": expression, "translation": translation}, ctx or self.ctx)

    def test_good_items(self):
        assert self.v("figure out", "averiguar / descubrir").expression == "figure out"
        item = self.v("getting at", "insinuar / querer decir")
        assert (item.expression, item.translation) == ("get at", "insinuar / querer decir")

    def test_context_kept(self):
        assert self.v("figure out", "averiguar").context == self.ctx

    def test_overwhelmed(self):
        item = self.v("overwhelmed", "abrumado / sobrepasado", "He was completely overwhelmed by the situation.")
        assert item is not None

    @pytest.mark.parametrize(
        "expr,trans,ctx",
        [
            ("went", "fui", "I went to the supermarket yesterday."),
            ("yesterday", "ayer", "I went to the supermarket yesterday."),
            ("supermarket", "supermercado", "I went to the supermarket yesterday."),  # cognado
            ("situation", "situación", "He was overwhelmed by the situation."),
            ("he", "él", "he was getting at"),
            ("where are you going", "a dónde vas", "Where are you going?"),  # frase
            ("for weeks", "durante semanas", "for weeks"),  # combinación trivial
            ("eat something", "comer algo", "let's eat something"),
            ("bamboozle", "engañar", "I couldn't figure it out."),  # no aparece: alucinación
            ("figure out", "", "I couldn't figure it out."),
            ("figure out", "figure out", "I couldn't figure it out."),  # sin traducir
            ("a b c d e f g h", "x", "a b c d e f g h"),
            ("2 times", "2 veces", "2 times"),
        ],
    )
    def test_rejected(self, expr, trans, ctx):
        assert self.v(expr, trans, ctx) is None

    @pytest.mark.parametrize("bad", [{"expression": 5, "translation": "x"}, {"expression": "x"}, {}])
    def test_wrong_types(self, bad):
        assert validate_item(bad, "x") is None

    def test_grounding_tolerates_inflection(self):
        assert self.v("run out of", "quedarse sin", "We're running out of time.") is not None
        assert self.v("get under one's skin", "molestar mucho", "it's getting under my skin") is not None

    def test_grounding_text_overrides(self):
        item = validate_item(
            {"expression": "put up with", "translation": "aguantar"},
            "Okay.",
            grounding_text="Okay. I won't put up with this.",
        )
        assert item is not None and item.context == "Okay."


def test_clean_translation():
    assert clean_translation("averiguar, descubrir") == "averiguar / descubrir"
    assert clean_translation("abrumado (emocionalmente) / sobrepasado / agobiado") == "abrumado / sobrepasado"
    assert clean_translation("dar por sentado / Dar por sentado") == "dar por sentado"


@pytest.mark.parametrize(
    "expr,trans,expected",
    [
        ("supermarket", "supermercado", True),
        ("situation", "situación", True),
        ("evidence", "la evidencia", True),
        ("overwhelmed", "abrumado", False),
        ("ubiquitous", "omnipresente / ubicuo", False),
        ("actually", "en realidad", False),
        ("frankly", "francamente", False),
    ],
)
def test_cognates(expr, trans, expected):
    assert is_transparent_cognate(expr, trans) is expected


@pytest.mark.parametrize(
    "line,trivial",
    [
        ("Where are you going?", True),
        ("Okay. See you tomorrow.", True),
        ("Thank you.", True),
        ("I can't figure him out.", False),  # partícula -> posible phrasal verb
        ("He was overwhelmed.", False),
        ("Let's call it a day, we're running out of time.", False),
    ],
)
def test_trivial_lines(line, trivial):
    assert is_trivial_line(line) is trivial
