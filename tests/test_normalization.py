import pytest

from wordlens.vocabulary.normalization import canonical_expression, dedup_key, lemma, normalize_text


@pytest.mark.parametrize(
    "word,expected",
    [
        ("getting", "get"), ("running", "run"), ("figured", "figure"), ("made", "make"),
        ("calling", "call"), ("stopped", "stop"), ("carried", "carry"), ("bailed", "bail"),
        ("dodging", "dodge"), ("taking", "take"), ("figure", "figure"),
    ],
)
def test_lemma(word, expected):
    assert lemma(word) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("getting at", "get at"),
        ("Figure him out", "figure out"),
        ("take it for granted", "take it for granted"),
        ("call it a day", "call it a day"),
        ("getting under my skin", "get under one's skin"),
        ("running out of", "run out of"),
        ("to put up with", "put up with"),
        ("overwhelmed", "overwhelmed"),  # palabras sueltas no se lematizan
        ("living room", "living room"),  # sin patrón verbal: no se toca
        ("make it", "make it"),
        ("bailed on us", "bail on"),
        ("“Long shot.”", "long shot"),
    ],
)
def test_canonical_expression(raw, expected):
    assert canonical_expression(raw) == expected


@pytest.mark.parametrize(
    "a,b",
    [
        ("figure out", "Figure out"),
        ("figure out", "FIGURE OUT"),
        ("figure out", "figure it out"),
        ("figure out", "to figure out"),
        ("take for granted", "take it for granted"),
        ("take for granted", "take something for granted"),
        ("get under one's skin", "get under your skin"),
        ("a long shot", "long shot"),
        ("get at", "getting at"),
        ("make up one's mind", "make up my mind"),
    ],
)
def test_dedup_equivalent(a, b):
    assert dedup_key(a) == dedup_key(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("get at", "get out"),
        ("make out", "make up"),
        ("put up", "put up with"),
        ("make it", "make"),
        ("overwhelmed", "overwhelm"),  # categorías distintas: no se fusionan
        ("call it a day", "call it a night"),
    ],
)
def test_dedup_different(a, b):
    assert dedup_key(a) != dedup_key(b)


def test_normalize_text():
    assert normalize_text("I COULDN’T  figure it out!!") == "i couldn't figure it out"
    assert normalize_text("- Hello, there.") == "hello there"
