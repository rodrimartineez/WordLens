"""Extracción exhaustiva: parseo, validación, palabras por reglas y consolidación.

Los tests sin marca usan un cliente con respuestas fijas (ScriptedClient): prueban
el código que hay alrededor del modelo de forma determinista, NO demuestran que
el modelo real responda así. Los tests @llm usan Ollama y el modelo real.
"""

from __future__ import annotations

import json

import pytest

from wordlens.ai.prompts import build_messages
from wordlens.ai.vocabulary_analyzer import VocabularyAnalyzer
from wordlens.config.settings import Settings
from wordlens.vocabulary.extractor import parse_llm_json, parse_llm_response, validate_candidate
from wordlens.vocabulary.lexicon import is_grammar_construction, lexical_words, word_lemma


class ScriptedClient:
    """Devuelve respuestas fijas, una por llamada, y registra los mensajes."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, model, messages, **kw):
        self.calls.append(messages)
        content = self.responses.pop(0) if self.responses else '{"items": [], "names": []}'
        return {"message": {"content": content}}


def answer(*items, names=()):
    return json.dumps(
        {"items": [{"line": n, "expression": e, "type": t} for n, e, t in items], "names": list(names)}
    )


def analyze(lines, *responses, settings=None):
    client = ScriptedClient(*responses)
    an = VocabularyAnalyzer(settings or Settings(), client)
    return an.analyze(lines), client


def exprs(cands):
    return [c.expression for c in cands]


# ------------------------------------------------------------------ parseo
class TestParse:
    def test_valid_with_names(self):
        items, names = parse_llm_response(answer((1, "brush off", "phrasal verb"), names=["Sarah"]))
        assert items == [{"line": 1, "expression": "brush off", "type": "phrasal verb"}]
        assert names == ["Sarah"]

    @pytest.mark.parametrize("raw", ["", "not json", "{broken", '{"items": "nope"}', None, "null", '"str"'])
    def test_invalid_never_raises(self, raw):
        assert parse_llm_json(raw) == []

    def test_truncated_json_keeps_complete_items(self):
        raw = ('{"items":[{"line":1,"expression":"brush off","type":"phrasal verb"},'
               '{"line":1,"expression":"concern","type":"word"},{"line":1,"expression":"de')
        assert [i["expression"] for i in parse_llm_json(raw)] == ["brush off", "concern"]

    def test_missing_brace_does_not_lose_following_items(self):
        raw = ('{"items":[{"line":1,"expression":"deal","type":"word"]},'
               '{"line":1,"expression":"go ahead with","type":"phrasal verb"}],"names":["Tom"]}')
        items, names = parse_llm_response(raw)
        assert [i["expression"] for i in items] == ["deal", "go ahead with"]
        assert names == ["Tom"]


# -------------------------------------------------------------- validación
class TestValidate:
    ctx = "He brushed off my concerns and went ahead with the deal."

    def v(self, expression, kind="word", ctx=None, **kw):
        return validate_candidate({"expression": expression, "type": kind}, ctx or self.ctx, **kw)

    def test_canonical_forms(self):
        assert self.v("brushed off", "phrasal verb").expression == "brush off"
        assert self.v("went ahead with", "phrasal verb").expression == "go ahead with"
        assert self.v("concerns").expression == "concern"
        assert self.v("The deal").expression == "deal"

    def test_kind_is_kept_or_inferred(self):
        assert self.v("brush off", "phrasal verb").kind == "phrasal verb"
        assert self.v("brush off", None).kind == "phrasal verb"
        assert self.v("deal", "idiom").kind == "word"  # una palabra es siempre "word"
        hit = validate_candidate({"expression": "hit the nail on the head", "type": "idiom"},
                                 "You really hit the nail on the head.")
        assert hit.kind == "idiom"

    @pytest.mark.parametrize(
        "expr,ctx",
        [
            ("go", "I have to go to the television store."),
            ("television", "I have to go to the television store."),
            ("store", "I have to go to the television store."),
            ("deal", "He brushed off my concerns and went ahead with the deal."),
            ("apartment", "This apartment is a total dump."),
            ("finally", "He finally gave in."),
            ("know", "Yes, I know."),
        ],
    )
    def test_no_difficulty_filter(self, expr, ctx):
        """Palabras frecuentes o fáciles con significado propio SÍ son candidatas."""
        assert validate_candidate({"expression": expr, "type": "word"}, ctx) is not None

    @pytest.mark.parametrize(
        "expr,kind,ctx,why",
        [
            ("the", "word", "I have to go to the television store.", "artículo"),
            ("to", "word", "I have to go to the television store.", "preposición"),
            ("have to", "expression", "I have to go to the television store.", "construcción gramatical"),
            ("i have to", "expression", "I have to go to the television store.", "construcción gramatical"),
            ("going to", "expression", "I'm going to call her.", "construcción gramatical"),
            ("do", "word", "What do you want?", "auxiliar"),
            ("would", "word", "I would never do that.", "modal"),
            ("and", "word", "He left and came back.", "conjunción"),
            ("you're", "word", "You're late.", "contracción"),
            ("oh", "word", "Oh, it's you.", "interjección"),
            ("sarah", "word", "Mike said that Sarah was scared.", "nombre propio"),
            ("put up with", "phrasal verb", "I went to the supermarket yesterday.", "no está en la línea"),
            ("dragon", "word", "We grabbed a quick bite.", "inventada"),
            ("tbe", "word", "Tbe negotiations stalled.", "error de OCR"),
            ("i can't put up with this", "expression", "I can't put up with this anymore.", "frase entera"),
        ],
    )
    def test_rejected(self, expr, kind, ctx, why):
        assert validate_candidate({"expression": expr, "type": kind}, ctx) is None, why

    def test_names_from_model_are_rejected(self):
        ctx = "Sarah, grab your jacket."
        assert validate_candidate({"expression": "sarah"}, ctx, names={"sarah"}) is None

    def test_free_filler_is_reduced_to_the_word(self):
        assert self.v("what happened", "expression", "Tell me what happened.").expression in ("happen", "happened")
        assert self.v("for years", "collocation", "My dad smoked for years.").expression == "year"

    def test_idiom_head_in_base_form(self):
        c = self.v("cutting corners", "idiom", "They're cutting corners to save money.")
        assert c.expression == "cut corners"

    def test_ocr_error_correction_is_grounded(self):
        """'rnistake' no existe: 'mistake' es una corrección evidente, no una invención."""
        assert self.v("mistake", "word", "He finally owned up to his rnistake.") is not None

    def test_case_and_punctuation(self):
        assert self.v("BRUSH OFF!", "phrasal verb", "HE BRUSHED OFF MY CONCERNS!!").expression == "brush off"


# ------------------------------------------------------------- palabras por reglas
class TestLexicon:
    def test_content_words_of_required_sentence(self):
        words = [t.lemma for t in lexical_words("He brushed off my concerns and went ahead with the deal.")]
        assert words == ["brush", "concern", "go", "ahead", "deal"]

    def test_television_store(self):
        words = [t.lemma for t in lexical_words("I have to go to the television store.")]
        assert words == ["go", "television", "store"]  # nada de "i", "have", "to", "the"

    def test_function_only_lines(self):
        for line in ("Oh, it's you.", "What are you doing here?", "I have to.", "Yes, it is."):
            assert lexical_words(line) == [], line

    def test_names_and_shouting(self):
        assert [t.lemma for t in lexical_words("SHE TOTALLY NAILED THE INTERVIEW!!")] == ["totally", "nail", "interview"]
        assert "sarah" not in [t.lemma for t in lexical_words("Mike said that Sarah was scared.")]

    def test_conservative_lemmas(self):
        assert [word_lemma(w) for w in ("concerns", "kids", "stalled", "nailed", "brushed")] == [
            "concern", "kid", "stall", "nail", "brush"]
        # Adjetivos y sustantivos que no son flexiones: se quedan como están.
        for w in ("interesting", "boring", "worried", "scared", "news", "evening", "meeting", "glasses"):
            assert word_lemma(w) == w

    def test_grammar_constructions(self):
        assert is_grammar_construction("I have to")
        assert is_grammar_construction("gonna")
        assert not is_grammar_construction("give up")


# ---------------------------------------------------------- consolidación
BRUSH = "He brushed off my concerns and went ahead with the deal."


class TestAnalyzer:
    def test_required_example_with_full_model_answer(self):
        cands, _ = analyze([BRUSH], answer(
            (1, "brush off", "phrasal verb"), (1, "concerns", "word"),
            (1, "go ahead with", "phrasal verb"), (1, "deal", "word"),
        ))
        assert exprs(cands) == ["brush off", "concern", "go ahead with", "deal"]
        assert [c.kind for c in cands] == ["phrasal verb", "word", "phrasal verb", "word"]
        assert all(c.context == BRUSH for c in cands)

    def test_words_the_model_skips_are_still_proposed(self):
        """El modelo sólo devuelve 'brush off': las demás palabras salen igual (red de seguridad)."""
        cands, _ = analyze([BRUSH], answer((1, "brush off", "phrasal verb")))
        assert exprs(cands) == ["brush off", "concern", "go", "ahead", "deal"]
        assert "brush" not in exprs(cands)  # ya forma parte de "brush off"

    def test_model_failure_to_answer_still_gives_words(self):
        cands, _ = analyze([BRUSH], "lo siento, no puedo")
        assert {"concern", "deal"} <= set(exprs(cands))

    def test_put_up_with(self):
        cands, _ = analyze(["I can't put up with this anymore."], answer((1, "put up with", "phrasal verb")))
        assert "put up with" in exprs(cands)
        assert not {"put", "can", "this", "i"} & set(exprs(cands))

    def test_idiom_replaces_its_words(self):
        cands, _ = analyze(["You really hit the nail on the head."],
                           answer((1, "hit the nail on the head", "idiom"), (1, "nail", "word")))
        assert exprs(cands) == ["hit the nail on the head"]

    def test_have_to_television_store(self):
        line = "I have to go to the television store."
        cands, _ = analyze([line], answer((1, "have to", "expression"), (1, "go", "word"),
                                          (1, "television", "word"), (1, "store", "word")))
        assert {"television", "store"} <= set(exprs(cands))
        assert not {"have to", "i", "have", "to", "the", "i have to"} & set(exprs(cands))

    def test_collocation_keeps_its_words(self):
        cands, _ = analyze(["We had heavy rain all night."], answer((1, "heavy rain", "collocation")))
        assert exprs(cands)[0] == "heavy rain"
        assert {"heavy", "rain", "night"} <= set(exprs(cands))

    def test_slang_and_insults(self):
        cands, _ = analyze(["That movie was sick, dude!"], answer((1, "sick", "word")))
        assert {"movie", "sick", "dude"} <= set(exprs(cands))
        cands, _ = analyze(["Let go of my hand, you creep!"], answer((1, "let go of", "phrasal verb")))
        assert {"let go of", "hand", "creep"} <= set(exprs(cands))

    def test_names_at_sentence_start_are_not_proposed(self):
        cands, _ = analyze(["Sarah, grab your jacket, we're leaving."], answer((1, "grab", "word"), (1, "jacket", "word")))
        assert "sarah" not in exprs(cands)
        cands, _ = analyze(["Honestly, I couldn't care less."], answer((1, "couldn't care less", "idiom")))
        assert "honestly" in exprs(cands)

    def test_names_learned_during_session(self):
        an = VocabularyAnalyzer(Settings(), ScriptedClient())
        an.analyze(["I told Brenda about it."])  # mayúscula en mitad de frase: nombre
        assert "brenda" not in exprs(an.analyze(["Brenda, wait for me!"]))

    def test_function_only_line_does_not_call_the_model(self):
        cands, client = analyze(["Oh, it's you."])
        assert cands == [] and client.calls == []

    def test_no_indiscriminate_combinations(self):
        """Las reglas sólo dan palabras sueltas; las combinaciones vienen del modelo y se validan."""
        cands, _ = analyze(["We need to talk about the house."], answer())
        assert all(len(c.expression.split()) == 1 for c in cands)

    def test_repeated_candidates_in_a_batch_are_merged(self):
        lines = ["The deal is off.", "What a deal!", "No deal."]
        cands, _ = analyze(lines, answer((1, "deal", "word"), (2, "deal", "word"), (3, "deal", "word")))
        assert exprs(cands).count("deal") == 1

    def test_case_and_punctuation_variants(self):
        cands, _ = analyze(["GIVE UP?!"], answer((1, "Give Up", "phrasal verb")))
        assert exprs(cands) == ["give up"]

    def test_ocr_noise_word_is_not_proposed(self):
        cands, _ = analyze(["Tbe negotiations stalled over the weekend."], answer((1, "negotiations", "word")))
        assert "tbe" not in exprs(cands)
        assert {"negotiation", "stall", "weekend"} <= set(exprs(cands))

    def test_batch_line_numbers(self):
        cands, _ = analyze(
            ["I can't put up with this anymore.", "You really hit the nail on the head."],
            answer((1, "put up with", "phrasal verb"), (2, "hit the nail on the head", "idiom")),
        )
        by_expr = {c.expression: c.context for c in cands}
        assert by_expr["put up with"].startswith("I can't")
        assert by_expr["hit the nail on the head"].startswith("You really")


def test_prompt_is_exhaustive_and_has_no_difficulty_filter():
    msgs = build_messages(["x"])
    system = msgs[0]["content"]
    assert "never judge difficulty" in system
    for banned in ("B2", "C1", "worth learning", "advanced"):
        assert banned not in system
    assert '"type"' in system and '"translation"' not in system


# ------------------------------------------------------------- modelo real
@pytest.mark.llm
def test_llm_required_examples():
    """Con el modelo real (ling): los ejemplos obligatorios del enunciado."""
    an = VocabularyAnalyzer(Settings())
    found = set(exprs(an.analyze([BRUSH])))
    assert {"brush off", "concern", "deal"} <= found
    assert "go ahead with" in found or "go ahead" in found
    assert "put up with" in exprs(an.analyze(["I can't put up with this anymore."]))
    assert "hit the nail on the head" in exprs(an.analyze(["You really hit the nail on the head."]))
    store = set(exprs(an.analyze(["I have to go to the television store."])))
    assert {"television", "store"} <= store or "television store" in store
    assert not {"have to", "i have to", "the", "to", "i"} & store


def test_model_dictionary_form_is_respected():
    """'gutted' (destrozado) no es 'gut': la forma que da el modelo no se recorta."""
    c = validate_candidate({"expression": "gutted", "type": "word"}, "Deep down she was gutted.")
    assert c.expression == "gutted"
    from wordlens.vocabulary.normalization import entry_key

    assert entry_key("gutted") != entry_key("gut")
    assert entry_key("concerns") == entry_key("concern")


def test_irregular_forms_from_the_model_are_normalised():
    cands, _ = analyze(["Mike said that Sarah was scared."], answer((1, "said", "word"), (1, "scared", "word")))
    assert exprs(cands) == ["say", "scared"]
