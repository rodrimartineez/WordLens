"""De líneas OCR a subtítulos nuevos.

1. extract_subtitles(): descarta lo que no parece un subtítulo (relojes, logos,
   menús, basura del OCR, texto no inglés) y une los subtítulos de dos líneas.
2. SubtitleTracker: exige que un subtítulo se vea en N capturas consecutivas
   (estabilidad, evita leerlo a medio fundido) y recuerda los ya emitidos
   durante una ventana de tiempo para no reenviarlos al LLM. Pasada esa ventana,
   la misma frase puede volver a procesarse si reaparece legítimamente.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from wordfreq import zipf_frequency

from vocablens.ocr.ocr_engine import OcrLine
from vocablens.vocabulary.normalization import fold, normalize_text, tokenize

# Correcciones típicas del OCR en subtítulos.
_OCR_FIXES = [
    (re.compile(r"(?<![A-Za-z])[|l](?=['’]?[mlvd]?\s)"), "I"),  # "| don't", "l'm"
    (re.compile(r"\s+([,.!?;:])"), r"\1"),
    (re.compile(r"\s{2,}"), " "),
]
_LEADING_JUNK = re.compile(r"^[\s\-–—•·*>\[\]♪#]+")
_TRAILING_JUNK = re.compile(r"[\s♪#*]+$")


def clean_ocr_text(text: str) -> str:
    text = fold(text).strip()
    text = _LEADING_JUNK.sub("", text)
    text = _TRAILING_JUNK.sub("", text)
    for pattern, repl in _OCR_FIXES:
        text = pattern.sub(repl, text)
    return text.strip()


def _is_english_token(token: str) -> bool:
    en = zipf_frequency(token, "en")
    # Se compara con el español: "la", "en", "para" existen en corpus ingleses,
    # pero son mucho más frecuentes en español (menús de juegos, apps, etc.).
    return en >= 1.5 and en + 1.0 >= zipf_frequency(token, "es")


def english_ratio(text: str) -> float:
    tokens = tokenize(text)
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if _is_english_token(t)) / len(tokens)


def looks_like_subtitle(
    line: OcrLine,
    band_width: float,
    *,
    min_words: int = 2,
    min_english_ratio: float = 0.7,
    require_centered: bool = True,
) -> bool:
    text = clean_ocr_text(line.text)
    words = tokenize(text)
    if len(words) < min_words:
        return False
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return False
    letters = sum(c.isalpha() for c in visible)
    if letters / len(visible) < 0.6:  # "00:12:34", "1080p", "<< >> ||"
        return False
    if require_centered and band_width > 0:
        rel = line.center_x / band_width
        if not 0.2 <= rel <= 0.8:  # menús laterales, logos de canal, relojes
            return False
    return english_ratio(text) >= min_english_ratio


def group_lines(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """Agrupa líneas verticalmente contiguas (subtítulos de 2-3 líneas)."""
    groups: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda ln: ln.y):
        if groups:
            last = groups[-1][-1]
            gap = line.y - last.bottom
            if gap <= max(last.height, line.height) * 1.0:
                groups[-1].append(line)
                continue
        groups.append([line])
    return groups


def extract_subtitles(
    lines: Iterable[OcrLine],
    band_width: float,
    *,
    min_words: int = 2,
    min_english_ratio: float = 0.7,
    require_centered: bool = True,
) -> list[str]:
    kept = [
        ln
        for ln in lines
        if looks_like_subtitle(
            ln,
            band_width,
            min_words=min_words,
            min_english_ratio=min_english_ratio,
            require_centered=require_centered,
        )
    ]
    subtitles = []
    for group in group_lines(kept):
        text = " ".join(clean_ocr_text(ln.text) for ln in group)
        text = re.sub(r"\s{2,}", " ", text).strip()
        if text:
            subtitles.append(text)
    return subtitles


def similar(a: str, b: str, threshold: float) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    # Descarte rápido por longitud antes del cálculo caro.
    if min(len(a), len(b)) / max(len(a), len(b)) < threshold - 0.1:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= threshold


@dataclass
class _Pending:
    norm: str
    text: str
    count: int
    first_seen: float


class SubtitleTracker:
    """Decide qué subtítulos son nuevos. Memoria acotada."""

    def __init__(
        self,
        *,
        stability_frames: int = 2,
        repeat_window_seconds: float = 180.0,
        similarity_threshold: float = 0.85,
        max_history: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stability_frames = max(1, stability_frames)
        self.repeat_window = repeat_window_seconds
        self.threshold = similarity_threshold
        self._clock = clock
        self._pending: list[_Pending] = []
        self._history: deque[tuple[str, float]] = deque(maxlen=max_history)

    def _purge(self, now: float) -> None:
        while self._history and now - self._history[0][1] > self.repeat_window:
            self._history.popleft()

    def _recently_emitted(self, norm: str) -> bool:
        return any(similar(norm, old, self.threshold) for old, _ in self._history)

    def update(self, subtitles: Iterable[str]) -> list[str]:
        """Recibe los subtítulos visibles en esta captura; devuelve los nuevos a procesar."""
        now = self._clock()
        self._purge(now)
        next_pending: list[_Pending] = []
        emitted: list[str] = []
        unmatched = list(self._pending)
        for text in subtitles:
            norm = normalize_text(text)
            if not norm:
                continue
            # Emparejamiento 1:1 con la entrada pendiente más parecida.
            match = max(
                (p for p in unmatched if similar(norm, p.norm, self.threshold)),
                key=lambda p: 2.0 if p.norm == norm else SequenceMatcher(None, norm, p.norm).ratio(),
                default=None,
            )
            if match is not None:
                unmatched.remove(match)
            if match is None:
                entry = _Pending(norm, text, 1, now)
            else:
                # Nos quedamos con la lectura más larga (suele ser la más completa).
                best = text if len(text) > len(match.text) else match.text
                entry = _Pending(normalize_text(best), best, match.count + 1, match.first_seen)
            if any(p.norm == entry.norm for p in next_pending):
                continue
            next_pending.append(entry)
            if entry.count == self.stability_frames and not self._recently_emitted(entry.norm):
                self._history.append((entry.norm, now))
                emitted.append(entry.text)
        # Los subtítulos que ya no se ven se olvidan del estado "pendiente".
        self._pending = next_pending
        return emitted

    def reset(self) -> None:
        self._pending.clear()
        self._history.clear()

    @property
    def history_size(self) -> int:
        return len(self._history)
