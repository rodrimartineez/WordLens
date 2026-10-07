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
