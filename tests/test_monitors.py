"""Selección de la pantalla de captura (versión 1.1).

- Resolución de la pantalla elegida con una, dos y tres pantallas, y al
  desconectar/reconectar (listas simuladas).
- Enumeración real de las pantallas de este equipo.
- Captura real: las franjas se calculan respecto a la pantalla elegida y el
  OCR lee lo que hay en ESA pantalla (si hay dos o más conectadas).
"""

from __future__ import annotations

import time

import pytest

from wordlens.capture import monitors
from wordlens.capture.monitors import Monitor, resolve
from wordlens.capture.screen_capture import ScreenCapturer, enable_dpi_awareness

enable_dpi_awareness()

LAPTOP = Monitor(1, "ID-LAPTOP", r"\\.\DISPLAY1", 0, 0, 1920, 1080, True)
TV = Monitor(2, "ID-TV", r"\\.\DISPLAY5", 1920, 0, 1920, 1080, False)
THIRD = Monitor(3, "ID-THIRD", r"\\.\DISPLAY6", -2560, 0, 2560, 1440, False)


def test_single_screen():
    sel = resolve([LAPTOP], "", 1)
    assert sel.monitor == LAPTOP and not sel.missing


def test_choose_screen_1_or_2():
    assert resolve([LAPTOP, TV], "ID-LAPTOP", 1).monitor == LAPTOP
    assert resolve([LAPTOP, TV], "ID-TV", 2).monitor == TV


def test_identifier_wins_over_number():
    # Al reconectar, la tele puede pasar a ser la pantalla 3: manda el identificador.
    tv_as_3 = Monitor(3, "ID-TV", r"\\.\DISPLAY7", 1920, 0, 1920, 1080, False)
    sel = resolve([LAPTOP, THIRD, tv_as_3], "ID-TV", 2)
    assert sel.monitor == tv_as_3 and not sel.missing


def test_disconnected_screen_falls_back_to_main_and_comes_back():
    sel = resolve([LAPTOP], "ID-TV", 2)  # tele desconectada
    assert sel.monitor == LAPTOP and sel.missing
    sel = resolve([LAPTOP, TV], "ID-TV", 2)  # reconectada: vuelve sola
    assert sel.monitor == TV and not sel.missing


def test_old_config_without_identifier_uses_number():
    assert resolve([LAPTOP, TV], "", 2).monitor == TV
    assert resolve([LAPTOP, TV], "", 7).monitor == LAPTOP  # número inexistente -> principal


def test_no_screens_at_all():
    assert resolve([], "ID-TV", 2) == monitors.Selection(None, True)
    assert resolve([], "", 1) == monitors.Selection(None, False)


def test_labels():
    assert LAPTOP.label == "Screen 1 · 1920×1080 · main"
    assert TV.label == "Screen 2 · 1920×1080"
    assert TV.rect == {"left": 1920, "top": 0, "width": 1920, "height": 1080}


def test_settings_migrate_old_monitor_key(tmp_path):
    import json

    from wordlens.config.settings import load_settings

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"setup_complete": True, "monitor": 1}), encoding="utf-8")
    s = load_settings(path)
    assert s.capture_monitor == 1 and s.capture_monitor_id == ""
    assert "monitor" not in json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({"monitor": 2}), encoding="utf-8")
    assert load_settings(path).capture_monitor == 2


def test_app_switches_and_warns_when_screen_disappears(monkeypatch):
    from wordlens.app import WordLensApp
    from wordlens.config.settings import Settings
    from wordlens.state import AppState

    connected = [[LAPTOP, TV]]
    monkeypatch.setattr(monitors, "list_monitors", lambda: connected[0])
    app = WordLensApp(Settings(capture_monitor=2, capture_monitor_id="ID-TV"))
    notices = []
    app.notify = lambda msg, **kw: notices.append(msg)
    app.state.transition(AppState.CAPTURING)

    assert app.resolve_capture_monitor().monitor == TV
    connected[0] = [LAPTOP]  # se desconecta la tele
    sel = app.resolve_capture_monitor()
    assert sel.monitor == LAPTOP and sel.missing
    assert "not connected" in notices[-1]
    connected[0] = [LAPTOP, TV]  # se vuelve a conectar
    assert app.resolve_capture_monitor().monitor == TV
    assert "is back" in notices[-1]
    assert len(notices) == 2


# ------------------------------------------------------------ pantallas reales
def test_real_monitor_list():
    found = monitors.list_monitors()
    assert found, "Windows debería informar al menos de una pantalla"
    assert found[0].primary and sum(m.primary for m in found) == 1
    assert [m.number for m in found] == list(range(1, len(found) + 1))
    assert len({m.device_id for m in found}) == len(found)
    for m in found:
        assert m.width > 0 and m.height > 0 and m.device_id


def test_capture_bands_follow_the_selected_screen():
    for m in monitors.list_monitors():
        cap = ScreenCapturer(m.rect, 0.33, 0.33)
        try:
            top, bottom = cap.bands
            assert (top.left, top.top, top.width) == (m.left, m.top, m.width)
            assert bottom.top + bottom.height == m.top + m.height
            shots = cap.grab()
            assert [s.image.size for s in shots] == [(b.width, b.height) for b in cap.bands]
        finally:
            cap.close()


def _show_text_on(monitor: Monitor, text: str):
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    tk.Label(root, text=text, bg="black", fg="white", font=("Arial", 30, "bold")).pack(expand=True)
    root.geometry(f"{monitor.width}x160+{monitor.left}+{monitor.top + monitor.height - 230}")
    root.update()
    return root


@pytest.mark.skipif(len(monitors.list_monitors()) < 2, reason="hace falta más de una pantalla")
def test_ocr_reads_only_the_selected_screen():
    from wordlens.capture.screen_capture import prepare_for_ocr
    from wordlens.capture.subtitle_detector import extract_subtitles
    from wordlens.ocr.ocr_engine import WindowsOcr

    first, second = monitors.list_monitors()[:2]
    texts = {first.device_id: "I could not figure out what he meant.", second.device_id: "She decided to back out of the deal."}
    windows = [_show_text_on(m, texts[m.device_id]) for m in (first, second)]
    ocr = WindowsOcr("en-US")
    try:
        time.sleep(0.4)
        for m in (first, second):
            windows[0].update()
            windows[1].update()
            cap = ScreenCapturer(m.rect, 0.33, 0.33)
            try:
                bottom = cap.grab()[1]
            finally:
                cap.close()
            image, _ = prepare_for_ocr(bottom.image)
            subs = extract_subtitles(ocr.recognize(image), image.width)
            assert texts[m.device_id] in subs, (m.label, subs)
            other = next(t for k, t in texts.items() if k != m.device_id)
            assert other not in subs
    finally:
        ocr.close()
        for w in windows:
            w.destroy()
