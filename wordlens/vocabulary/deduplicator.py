"""Registro de expresiones ya presentes en el documento."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from .models import VocabularyItem
from .normalization import dedup_key


class Deduplicator:
    """Conjunto thread-safe de claves normalizadas."""

    def __init__(self, existing: Iterable[str] = (), lang: str = "en") -> None:
        self.lang = lang
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        self.reset(existing)

    def reset(self, expressions: Iterable[str], lang: str | None = None) -> None:
        with self._lock:
            if lang is not None:
                self.lang = lang
            self._keys = {k for k in (dedup_key(e, self.lang) for e in expressions) if k}

    def contains(self, expression: str) -> bool:
        key = dedup_key(expression, self.lang)
        with self._lock:
            return key in self._keys

    def add(self, expression: str) -> bool:
        """Añade la expresión; devuelve False si ya existía."""
        key = dedup_key(expression, self.lang)
        if not key:
            return False
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            return True

    def filter_new(self, items: Iterable[VocabularyItem]) -> list[VocabularyItem]:
        """Devuelve sólo los ítems nuevos (también deduplica dentro del lote)."""
        new: list[VocabularyItem] = []
        with self._lock:
            batch_keys: set[str] = set()
            for item in items:
                key = dedup_key(item.expression, self.lang)
                if key and key not in self._keys and key not in batch_keys:
                    batch_keys.add(key)
                    new.append(item)
        return new

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)
