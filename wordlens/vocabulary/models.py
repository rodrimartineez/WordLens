"""Estructuras de datos compartidas."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VocabularyItem:
    expression: str
    translation: str
    context: str = field(default="", compare=False)


@dataclass(frozen=True)
class SubtitleLine:
    text: str
    region: str  # "top" | "bottom" | "manual"
    timestamp: float


@dataclass(frozen=True)
class Candidate:
    """Unidad léxica detectada en un subtítulo, pendiente de traducir y revisar."""

    expression: str  # forma de diccionario ("brush off", "concern")
    kind: str = "word"  # word | phrasal verb | idiom | collocation | expression
    context: str = field(default="", compare=False)  # subtítulo original
    origin: str = field(default="model", compare=False)  # "model" (LLM) | "lexical" (reglas)
