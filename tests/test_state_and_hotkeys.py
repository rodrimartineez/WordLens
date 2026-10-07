import ctypes
import threading
import time
from ctypes import wintypes

import pytest

from vocablens.input.hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    HotkeyError,
    HotkeyManager,
    parse_hotkey,
)
from vocablens.state import AppState, StateMachine


# ------------------------------------------------------------ máquina de estados
def test_full_cycle():
    sm = StateMachine()
    seen = []
    sm.subscribe(lambda old, new: seen.append((old, new)))
    assert sm.state is AppState.INACTIVE
    assert sm.transition(AppState.CAPTURING)
    assert sm.transition(AppState.PROCESSING)
    assert sm.transition(AppState.CAPTURING)
    assert sm.transition(AppState.STOPPED)
    assert sm.transition(AppState.CAPTURING)  # nueva sesión
    assert len(seen) == 5


@pytest.mark.parametrize(
    "start,target",
    [
        (AppState.INACTIVE, AppState.PROCESSING),
        (AppState.INACTIVE, AppState.STOPPED),
        (AppState.STOPPED, AppState.PROCESSING),
        (AppState.CAPTURING, AppState.CAPTURING),  # no hay dos sesiones simultáneas
        (AppState.PROCESSING, AppState.PROCESSING),
    ],
)
def test_invalid_transitions(start, target):
    sm = StateMachine(start)
    assert not sm.transition(target)
    assert sm.state is start


def test_conditional_transition_after_stop():
    """Si se detiene mientras el LLM trabaja, al terminar no debe volver a CAPTURING."""
    sm = StateMachine(AppState.PROCESSING)
    sm.transition(AppState.STOPPED)
    assert not sm.transition(AppState.CAPTURING, only_from={AppState.PROCESSING})
    assert sm.state is AppState.STOPPED


def test_concurrent_starts_only_one_wins():
    sm = StateMachine()
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(sm.transition(AppState.CAPTURING))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_broken_listener_does_not_break_machine():
    sm = StateMachine()
    sm.subscribe(lambda *_: 1 / 0)
    assert sm.transition(AppState.CAPTURING)


# ---------------------------------------------------------------------- atajos
def test_parse_hotkey():
    assert parse_hotkey("ctrl+shift+v") == (MOD_CONTROL | MOD_SHIFT, ord("V"))
    assert parse_hotkey("Ctrl + Alt + F9") == (MOD_CONTROL | MOD_ALT, 0x78)
    assert parse_hotkey("F10") == (0, 0x79)


@pytest.mark.parametrize("bad", ["", "ctrl+shift", "ctrl+v+x", "ctrl+banana", "v"])
def test_parse_hotkey_invalid(bad):
    with pytest.raises(HotkeyError):
        parse_hotkey(bad)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def send_keys(*vks):
    """Pulsa una combinación con SendInput, como lo haría el teclado físico."""
    events = []
    for vk in vks:
        events.append(INPUT(type=1, ki=KEYBDINPUT(wVk=vk)))
    for vk in reversed(vks):
        events.append(INPUT(type=1, ki=KEYBDINPUT(wVk=vk, dwFlags=2)))
    arr = (INPUT * len(events))(*events)
    ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def test_global_hotkey_fires_end_to_end():
    """Registra un atajo poco común y lo dispara con pulsaciones reales del sistema."""
    fired = threading.Event()
    mgr = HotkeyManager()
    mgr.add("ctrl+alt+shift+f11", fired.set)
    failed = mgr.start()
    try:
        if failed:
            pytest.skip(f"Atajo de prueba ocupado: {failed}")
        send_keys(0x11, 0x12, 0x10, 0x7A)  # Ctrl, Alt, Shift, F11
        assert fired.wait(3), "El atajo global no se disparó"
    finally:
        mgr.stop()


def test_duplicate_registration_reports_conflict():
    a, b = HotkeyManager(), HotkeyManager()
    a.add("ctrl+alt+shift+f12", lambda: None)
    b.add("ctrl+alt+shift+f12", lambda: None)
    try:
        assert a.start() == {}
        failed = b.start()
        assert "ctrl+alt+shift+f12" in failed
    finally:
        a.stop()
        b.stop()
    time.sleep(0.1)
