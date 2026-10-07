"""Máquina de estados de la aplicación.

    INACTIVE ──start──▶ CAPTURING ◀──▶ PROCESSING
                            │               │
                            └────stop───────┴──▶ STOPPED ──start──▶ CAPTURING
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import Enum

log = logging.getLogger(__name__)


class AppState(Enum):
    INACTIVE = "Inactivo"
    CAPTURING = "Capturando"
    PROCESSING = "Procesando"
    STOPPED = "Detenido"


TRANSITIONS: dict[AppState, frozenset[AppState]] = {
    AppState.INACTIVE: frozenset({AppState.CAPTURING}),
    AppState.CAPTURING: frozenset({AppState.PROCESSING, AppState.STOPPED}),
    AppState.PROCESSING: frozenset({AppState.CAPTURING, AppState.STOPPED}),
    AppState.STOPPED: frozenset({AppState.CAPTURING}),
}

Listener = Callable[[AppState, AppState], None]


class StateMachine:
    def __init__(self, initial: AppState = AppState.INACTIVE) -> None:
        self._state = initial
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def is_active(self) -> bool:
        return self.state in (AppState.CAPTURING, AppState.PROCESSING)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def can_transition(self, target: AppState) -> bool:
        with self._lock:
            return target in TRANSITIONS[self._state]

    def transition(self, target: AppState, *, only_from: set[AppState] | None = None) -> bool:
        """Cambia de estado si la transición es válida. Devuelve True si cambió.

        only_from permite transiciones condicionales atómicas, p. ej. volver a
        CAPTURING sólo si seguimos en PROCESSING (y no se ha detenido entretanto).
        """
        with self._lock:
            old = self._state
            if only_from is not None and old not in only_from:
                return False
            if target not in TRANSITIONS[old]:
                log.debug("Transición ignorada %s -> %s", old.name, target.name)
                return False
            self._state = target
        log.info("Estado: %s -> %s", old.name, target.name)
        for listener in list(self._listeners):
            try:
                listener(old, target)
            except Exception:  # un listener roto no debe romper la app
                log.exception("Error en listener de estado")
        return True
