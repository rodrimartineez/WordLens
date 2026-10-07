"""OCR real (Windows.Media.Ocr) sobre imágenes de subtítulos sintéticas."""

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from vocablens.capture.screen_capture import (
    ChangeDetector,
    ScreenCapturer,
    compute_bands,
    prepare_for_ocr,
)
from vocablens.capture.subtitle_detector import extract_subtitles
from vocablens.ocr.ocr_engine import WindowsOcr

W = 1920


@pytest.fixture(scope="module")
def ocr():
    engine = WindowsOcr("en-US")
    yield engine
    engine.close()


def background(kind: str, h: int = 320) -> Image.Image:
    rng = np.random.default_rng(1)
    if kind == "noise":
        arr = rng.random((h, W, 3)) * 120 + 40
    elif kind == "gradient":
        arr = np.dstack([np.tile(np.linspace(0, 255, W), (h, 1))] * 3)
    else:
        arr = np.full((h, W, 3), (30, 60, 90))
    return Image.fromarray(arr.astype("uint8"))


def draw(img: Image.Image, lines, y0: int, color="white", size=46, x_center=W / 2) -> Image.Image:
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("arial.ttf", size)
    for i, text in enumerate(lines):
        w = d.textlength(text, font=font)
        d.text((x_center - w / 2, y0 + i * (size + 12)), text, font=font, fill=color,
               stroke_width=3, stroke_fill="black")
    return img


def read(ocr, img, mode="subtitle"):
    prepared, _ = prepare_for_ocr(img, mode)
    return extract_subtitles(ocr.recognize(prepared), prepared.width)


@pytest.mark.parametrize("bg", ["flat", "noise", "gradient"])
def test_single_line_bottom(ocr, bg):
    img = draw(background(bg), ["You shouldn't take it for granted."], 200)
    assert read(ocr, img) == ["You shouldn't take it for granted."]


def test_two_line_subtitle_is_merged(ocr):
    img = draw(background("noise"), ["I couldn't figure out what", "he was getting at."], 150)
    assert read(ocr, img) == ["I couldn't figure out what he was getting at."]


def test_yellow_subtitle(ocr):
    img = draw(background("flat"), ["He was completely overwhelmed."], 120, color="#ffe14d")
    assert read(ocr, img) == ["He was completely overwhelmed."]


def test_top_band_subtitle(ocr):
    img = draw(background("noise", 270), ["I couldn't make out what she was saying."], 40)
    assert read(ocr, img) == ["I couldn't make out what she was saying."]


def test_gray_mode_for_other_colours(ocr):
    img = draw(background("flat"), ["Let's call it a day."], 120, color="#7fd0ff")
    assert read(ocr, img, mode="gray") == ["Let's call it a day."]


def test_ignores_clock_and_side_text(ocr):
    img = background("flat")
    d = ImageDraw.Draw(img)
    d.text((30, 20), "01:23:45 / 02:10:00", font=ImageFont.truetype("arial.ttf", 28), fill="white")
    draw(img, ["Netflix Original Series"], 40, size=30, x_center=180)  # texto lateral
    draw(img, ["I won't put up with this nonsense."], 200)
    assert read(ocr, img) == ["I won't put up with this nonsense."]


def test_empty_frame(ocr):
    assert read(ocr, background("noise")) == []


def test_ocr_speed_is_reasonable(ocr):
    import time

    img = draw(background("noise"), ["I couldn't figure out what he was getting at."], 200)
    prepared, _ = prepare_for_ocr(img)
    ocr.recognize(prepared)
    t0 = time.perf_counter()
    for _ in range(5):
        ocr.recognize(prepared)
    assert (time.perf_counter() - t0) / 5 < 0.25


def test_compute_bands():
    mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    bands = compute_bands(mon, 0.25, 0.30, True, True)
    assert [(b.name, b.top, b.height) for b in bands] == [("top", 0, 270), ("bottom", 756, 324)]
    assert [b.name for b in compute_bands(mon, 0.25, 0.3, False, True)] == ["bottom"]


def test_real_screen_capture():
    cap = ScreenCapturer(1, 0.25, 0.30)
    shots = cap.grab()
    cap.close()
    assert [s.band.name for s in shots] == ["top", "bottom"]
    for s in shots:
        assert s.image.size == (s.band.width, s.band.height)


def test_change_detector():
    det = ChangeDetector()
    a = draw(background("flat"), ["First subtitle line here."], 200)
    b = draw(background("flat"), ["A different subtitle now!"], 200)
    assert det.changed("bottom", a) is True
    assert det.changed("bottom", a.copy()) is False
    assert det.changed("bottom", b) is True
