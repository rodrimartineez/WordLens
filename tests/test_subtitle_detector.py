from wordlens.capture.subtitle_detector import (
    SubtitleTracker,
    clean_ocr_text,
    extract_subtitles,
    group_lines,
    looks_like_subtitle,
)
from wordlens.ocr.ocr_engine import OcrLine


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def line(text, x=700, y=200, w=500, h=40):
    return OcrLine(text, x, y, w, h)


def test_filters():
    assert looks_like_subtitle(line("I couldn't figure it out."), 1920)
    assert not looks_like_subtitle(line("00:12:34"), 1920)
    assert not looks_like_subtitle(line("Hello"), 1920)  # una palabra
    assert not looks_like_subtitle(line("This is on the left", x=0, w=200), 1920)
    assert not looks_like_subtitle(line("Xkqz wvtr plmn"), 1920)  # basura OCR
    assert not looks_like_subtitle(line("Configuración de reproducción avanzada"), 1920)  # no inglés
    # Textos reales de una interfaz en español que antes se colaban:
    assert not looks_like_subtitle(line("Te espera en la dársena del hangar. Acércate y pulsa para subir"), 1920)
    assert not looks_like_subtitle(line("Dinero ilimitado"), 1920)
    assert looks_like_subtitle(line("No, me neither. It's general knowledge."), 1920)


def test_grouping():
    lines = [line("he was getting at.", y=258), line("I couldn't figure out what", y=200)]
    groups = group_lines(lines)
    assert len(groups) == 1 and groups[0][0].text.startswith("I couldn't")
    far = [line("Top text here", y=10), line("Bottom text here", y=250)]
    assert len(group_lines(far)) == 2


def test_extract_cleans_dialogue_dashes():
    out = extract_subtitles([line("- Where were you? - Out.")], 1920, min_words=2)
    assert out == ["Where were you? - Out."]


def test_clean_ocr_text():
    assert clean_ocr_text("| don't know .") == "I don't know."
    assert clean_ocr_text("♪ singing in the rain ♪") == "singing in the rain"


def test_stability_requires_consecutive_frames():
    tr = SubtitleTracker(stability_frames=2, clock=Clock())
    assert tr.update(["I couldn't figure it out."]) == []
    assert tr.update(["I couldn't figure it out."]) == ["I couldn't figure it out."]
    # Sigue en pantalla varios segundos: no se reemite
    for _ in range(10):
        assert tr.update(["I couldn't figure it out."]) == []


def test_flicker_single_frame_ignored():
    tr = SubtitleTracker(stability_frames=2, clock=Clock())
    assert tr.update(["Half faded subtitle text"]) == []
    assert tr.update([]) == []
    assert tr.update(["Another line entirely"]) == []


def test_ocr_jitter_counts_as_same_subtitle():
    tr = SubtitleTracker(stability_frames=2, clock=Clock())
    tr.update(["I couldn't figure it out."])
    out = tr.update(["I couldnt figure it out"])  # variación típica del OCR
    assert len(out) == 1
    assert tr.update(["l couldn't figure it out."]) == []


def test_repeat_window_allows_legit_reappearance():
    clock = Clock()
    tr = SubtitleTracker(stability_frames=2, repeat_window_seconds=60, clock=clock)
    tr.update(["Are you serious right now?"])
    assert tr.update(["Are you serious right now?"]) != []
    tr.update([])
    clock.t = 30  # reaparece pronto: se ignora
    tr.update(["Are you serious right now?"])
    assert tr.update(["Are you serious right now?"]) == []
    tr.update([])
    clock.t = 200  # mucho después: se vuelve a procesar
    tr.update(["Are you serious right now?"])
    assert tr.update(["Are you serious right now?"]) == ["Are you serious right now?"]


def test_two_simultaneous_subtitles():
    tr = SubtitleTracker(stability_frames=2, clock=Clock())
    frame = ["Where on earth have you been?", "I was stuck in traffic for hours."]
    tr.update(frame)
    assert sorted(tr.update(frame)) == sorted(frame)


def test_history_is_bounded():
    clock = Clock()
    tr = SubtitleTracker(stability_frames=1, repeat_window_seconds=1e9, max_history=50, clock=clock)
    for i in range(500):
        tr.update([f"unique subtitle number {i} " + "x" * (i % 7)])
    assert tr.history_size <= 50
