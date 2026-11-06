"""Ventana principal de WordLens (tkinter).

Una ventana pequeña con lo esencial: estado, idiomas, documento y Start/Stop.
La primera vez muestra la configuración inicial (documento + idiomas).

Hilos: tkinter vive en el hilo principal. Todo lo que llega de otros hilos
(atajos, bandeja, estado de la sesión) pasa por call_soon(), que lo encola y
lo ejecuta en el hilo de Tk.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from wordlens.ai import ollama_setup
from wordlens.ai.ollama_client import OllamaAborted, OllamaClient, OllamaError
from wordlens.app import AppError, WordLensApp
from wordlens.capture import monitors
from wordlens.config.paths import (
    CONFIG_PATH,
    DATA_DIR,
    ICON_PATH,
    LOG_DIR,
    controlled_folder_access_enabled,
    default_document,
)
from wordlens.config.settings import LANGUAGES, save_settings
from wordlens.document.word_writer import DocumentError, WordWriter
from wordlens.input.hotkeys import HotkeyError, parse_hotkey
from wordlens.state import AppState
from wordlens.ui.icon import render_icon

log = logging.getLogger(__name__)

# Paletas. Los nombres de módulo (BG, CARD…) se reasignan con apply_theme();
# las vistas leen el valor en el momento de construirse, así que cambiar de tema
# reconstruye la ventana principal.
PALETTES: dict[str, dict[str, object]] = {
    # «ink»: base oscura, acentos del icono (índigo → verde azulado).
    "dark": {
        "BASE": "#0B0D13",  # barra lateral
        "BG": "#10131B",  # fondo general y del panel de vocabulario
        "CARD": "#161A24",  # tarjetas y filas
        "CARD_HOVER": "#1C2130",
        "BORDER": "#252B3A",
        "FIELD_HOVER": "#3A4256",  # borde de campos al pasar el ratón
        "SELECTION": "#2E3550",
        "SCROLL_THUMB": "#2C3344",
        "TEXT": "#E8EAF2",
        "MUTED": "#9AA3B6",
        "SUBTLE": "#626A7E",
        "ACCENT": "#7B7FFA",
        "ACCENT_HOVER": "#9599FF",
        "TEAL": "#2DD4BF",
        "TEAL_DIM": "#11312F",
        "TEAL_SOLID": "#14B8A6",
        "ON_TEAL": "#04221F",
        "STOP": "#2A1B23",
        "STOP_HOVER": "#3A222D",
        "STOP_FG": "#FF8C93",
        "SECONDARY": "#1E2331",
        "SECONDARY_HOVER": "#282E3F",
        "DANGER_DIM": "#3A1F27",
        "DISABLED": "#1B1F2A",
        "DISABLED_FG": "#4C5366",
        "STATUS_COLORS": {
            "Ready": "#8B8FFF", "Starting…": "#FBBF24", "Capturing": "#34D399", "Processing": "#FBBF24",
            "Stopped": "#7C8599", "Finishing": "#FBBF24", "Error": "#F87171", "Setup": "#8B8FFF",
        },
    },
    # «paper»: mismo carácter, sobre blanco.
    "light": {
        "BASE": "#F1F2F7",
        "BG": "#F8F9FC",
        "CARD": "#FFFFFF",
        "CARD_HOVER": "#F2F4FA",
        "BORDER": "#E1E4ED",
        "FIELD_HOVER": "#C4C9D8",
        "SELECTION": "#DFE3FB",
        "SCROLL_THUMB": "#D2D6E1",
        "TEXT": "#151826",
        "MUTED": "#596073",
        "SUBTLE": "#949BAC",
        "ACCENT": "#5357E6",
        "ACCENT_HOVER": "#3F43D2",
        "TEAL": "#0E8F80",
        "TEAL_DIM": "#DDF4F0",
        "TEAL_SOLID": "#14B8A6",
        "ON_TEAL": "#FFFFFF",
        "STOP": "#FDECEF",
        "STOP_HOVER": "#FADADF",
        "STOP_FG": "#D13B4B",
        "SECONDARY": "#EDEFF5",
        "SECONDARY_HOVER": "#E2E5EE",
        "DANGER_DIM": "#FDE7EA",
        "DISABLED": "#ECEEF3",
        "DISABLED_FG": "#A7ADBB",
        "STATUS_COLORS": {
            "Ready": "#5357E6", "Starting…": "#D97706", "Capturing": "#16A34A", "Processing": "#D97706",
            "Stopped": "#64748B", "Finishing": "#D97706", "Error": "#DC2626", "Setup": "#5357E6",
        },
    },
}
THEMES = {"Dark": "dark", "Light": "light"}
# Colores del tema activo (los asigna apply_theme()).
BASE: str
BG: str
CARD: str
CARD_HOVER: str
BORDER: str
FIELD_HOVER: str
SELECTION: str
SCROLL_THUMB: str
TEXT: str
MUTED: str
SUBTLE: str
ACCENT: str
ACCENT_HOVER: str
TEAL: str
TEAL_DIM: str
TEAL_SOLID: str
ON_TEAL: str
STOP: str
STOP_HOVER: str
STOP_FG: str
SECONDARY: str
SECONDARY_HOVER: str
DANGER_DIM: str
DISABLED: str
DISABLED_FG: str
GRADIENT = ("#6366F1", "#14B8A6")
GRADIENT_HOVER = ("#7679FF", "#1FCAB7")
STATUS_COLORS: dict[str, str] = {}
current_theme = "dark"


def apply_theme(name: str) -> str:
    """Activa una paleta: reasigna los colores de este módulo y de los que los importaron."""
    import sys

    global current_theme
    name = name if name in PALETTES else "dark"
    current_theme = name
    palette = PALETTES[name]
    targets = [globals()] + [vars(m) for n, m in list(sys.modules.items()) if n == "wordlens.ui.review" and m]
    for key, value in palette.items():
        if key == "STATUS_COLORS":
            STATUS_COLORS.clear()
            STATUS_COLORS.update(value)  # type: ignore[arg-type]
            continue
        for g in targets:
            if key in g or g is globals():
                g[key] = value
    return name


apply_theme("dark")

STATE_LABELS = {
    AppState.INACTIVE: "Ready",
    AppState.CAPTURING: "Capturing",
    AppState.PROCESSING: "Processing",
    AppState.FINISHING: "Finishing",
    AppState.STOPPED: "Stopped",
}
ERROR_ACTIONS = {
    "ollama_missing": "Get Ollama",
    "ollama_down": "Retry",
    "model_missing": "Download model…",
    "document": "Change document…",
}


def pretty_hotkey(spec: str) -> str:
    return " + ".join(p.strip().capitalize() for p in spec.split("+"))


def elide_middle(text: str, font: tkfont.Font, max_px: int) -> str:
    """Recorta por el centro para que quepa en max_px."""
    if font.measure(text) <= max_px:
        return text
    head, tail = len(text) // 3, len(text) - len(text) // 3
    while head > 3 and font.measure(text[:head] + "…" + text[tail:]) > max_px:
        head -= 1
        tail += 1
    return text[:head] + "…" + text[tail:]


Fill = str | tuple[str, str]  # color liso o degradado horizontal (izquierda, derecha)


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


class RoundButton(tk.Canvas):
    """Botón con esquinas redondeadas y antialiasing (dibujado con Pillow).

    El fondo puede ser un color o un degradado; `hover_fg` cambia el color del
    texto al pasar el ratón (botones fantasma que se rellenan al hover).
    """

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        width: int,
        height: int,
        scale: float,
        bg: Fill | None = None,
        hover: Fill | None = None,
        fg: str = "#FFFFFF",
        hover_fg: str | None = None,
        font: tuple = ("Segoe UI Semibold", 11),
        parent_bg: str | None = None,
        radius: int = 10,
    ) -> None:
        bg = ACCENT if bg is None else bg
        hover = ACCENT_HOVER if hover is None else hover
        parent_bg = BG if parent_bg is None else parent_bg
        self._bw, self._bh = int(width * scale), int(height * scale)
        super().__init__(
            master, width=self._bw, height=self._bh, bg=parent_bg, highlightthickness=0, bd=0, cursor="hand2"
        )
        self._text, self._command, self._font = text, command, font
        self._fg, self._hover_fg = fg, hover_fg
        self._bg, self._hover = bg, hover
        self._radius = int(radius * scale)
        self._enabled, self._hovering = True, False
        self._images: dict[Fill, ImageTk.PhotoImage] = {}
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _image(self, fill: Fill) -> ImageTk.PhotoImage:
        if fill not in self._images:
            k = 3
            w, h = self._bw * k, self._bh * k
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=self._radius * k, fill=255)
            if isinstance(fill, tuple):
                (r1, g1, b1), (r2, g2, b2) = _rgb(fill[0]), _rgb(fill[1])
                band = Image.new("RGB", (256, 1))
                for x in range(256):
                    t = x / 255
                    band.putpixel((x, 0), (round(r1 + (r2 - r1) * t), round(g1 + (g2 - g1) * t), round(b1 + (b2 - b1) * t)))
                paint = band.resize((w, h))
            else:
                paint = Image.new("RGB", (w, h), fill)
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            img.paste(paint, (0, 0), mask)
            self._images[fill] = ImageTk.PhotoImage(img.resize((self._bw, self._bh), Image.LANCZOS))
        return self._images[fill]

    def _draw(self) -> None:
        self.delete("all")
        if not self._enabled:
            fill, fg = DISABLED, DISABLED_FG
        elif self._hovering:
            fill, fg = self._hover, self._hover_fg or self._fg
        else:
            fill, fg = self._bg, self._fg
        self.create_image(0, 0, image=self._image(fill), anchor="nw")
        self.create_text(self._bw / 2, self._bh / 2, text=self._text, fill=fg, font=self._font)

    def _set_hover(self, value: bool) -> None:
        self._hovering = value
        self._draw()

    def _click(self, event: tk.Event) -> None:
        if self._enabled and 0 <= event.x <= self._bw and 0 <= event.y <= self._bh:
            self._command()

    def update_button(
        self,
        *,
        text: str | None = None,
        bg: Fill | None = None,
        hover: Fill | None = None,
        fg: str | None = None,
        hover_fg: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        if text is not None:
            self._text = text
        if bg is not None:
            self._bg = bg
        if hover is not None:
            self._hover = hover
        if fg is not None:
            self._fg = fg
        if hover_fg is not None:
            self._hover_fg = hover_fg
        if enabled is not None:
            self._enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()


class LinkButton(tk.Label):
    def __init__(
        self, master: tk.Misc, text: str, command: Callable[[], None], *, bg: str, font=("Segoe UI", 9),
        fg: str | None = None, hover_fg: str | None = None,
    ):
        fg = ACCENT if fg is None else fg
        hover_fg = ACCENT_HOVER if hover_fg is None else hover_fg
        super().__init__(master, text=text, bg=bg, fg=fg, font=font, cursor="hand2")
        self._command = command
        self.bind("<Enter>", lambda _e: self.configure(fg=hover_fg))
        self.bind("<Leave>", lambda _e: self.configure(fg=fg))
        self.bind("<ButtonRelease-1>", lambda _e: self._command())


def rounded_image(
    widget: tk.Misc, width: int, height: int, fill: str, *, border: str | None = None, radius: int,
    corner_bg: str | None = None, strip: str | None = None, strip_width: int = 0,
) -> ImageTk.PhotoImage:
    """Rectángulo redondeado con antialiasing (cacheado por intérprete Tk).

    corner_bg rellena las esquinas (si no, transparentes); strip dibuja una barra
    de acento redondeada a la izquierda (filas seleccionadas o nuevas).
    """
    root = widget._root()  # type: ignore[attr-defined]
    cache: dict = root.__dict__.setdefault("_wl_rounded", {})
    key = (width, height, fill, border, radius, corner_bg, strip, strip_width)
    if key not in cache:
        if len(cache) > 400:
            cache.clear()
        k = 3
        w, h, r = max(1, width) * k, max(1, height) * k, max(0, min(radius, height // 2, width // 2)) * k
        base = (0, 0, 0, 0) if corner_bg is None else _rgb(corner_bg) + (255,)
        img = Image.new("RGBA", (w, h), base)
        d = ImageDraw.Draw(img)
        if border:
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=border)
            d.rounded_rectangle((k, k, w - 1 - k, h - 1 - k), radius=max(0, r - k), fill=fill)
        else:
            d.rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=fill)
        if strip and strip_width:
            sw = strip_width * k
            inset = max(r // 2, 4 * k)
            d.rounded_rectangle((2 * k, inset, 2 * k + sw, h - 1 - inset), radius=sw // 2, fill=strip)
        cache[key] = ImageTk.PhotoImage(img.resize((max(1, width), max(1, height)), Image.LANCZOS))
    return cache[key]


class RoundedCard(tk.Frame):
    """Marco con fondo redondeado (los hijos usan bg=fill; las esquinas toman el color del padre)."""

    def __init__(
        self, master: tk.Misc, *, fill: str | None = None, border: str | None = "", radius: int = 14,
        scale: float = 1.0, padx: int = 0, pady: int = 0,
    ) -> None:
        parent_bg = master.cget("bg")
        super().__init__(master, bg=parent_bg, padx=padx, pady=pady)
        self.fill = CARD if fill is None else fill
        self.border = BORDER if border == "" else border
        self.radius = int(radius * scale)
        self._bg = tk.Label(self, bd=0, highlightthickness=0, bg=parent_bg)
        self._bg.place(x=0, y=0, relwidth=1, relheight=1, bordermode="outside")
        self._pending = False
        self.bind("<Configure>", lambda _e: self._schedule(), add="+")

    def _schedule(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self.redraw)

    def redraw(self) -> None:
        self._pending = False
        if not self.winfo_exists():
            return
        w, h = self.winfo_width(), self.winfo_height()
        if w > 1 and h > 1:
            self._bg.configure(image=rounded_image(self, w, h, self.fill, border=self.border, radius=self.radius))

    def set_colors(self, *, fill: str | None = None, border: str | None = "") -> None:
        if fill is not None:
            self.fill = fill
        if border != "":
            self.border = border
        self.redraw()


def rounded_field(parent: tk.Misc, factory: Callable[[tk.Misc], tk.Misc], *, scale: float) -> tk.Misc:
    """Campo (Entry/Combobox) dentro de un marco redondeado que resalta el foco y el hover.

    Devuelve el widget; su marco está en `widget.wrapper` (es lo que se coloca).
    """
    wrap = RoundedCard(parent, fill=CARD, radius=10, scale=scale, padx=int(5 * scale), pady=int(3 * scale))
    widget = factory(wrap)
    widget.pack(fill="x")
    state = {"focus": False, "hover": False}

    def paint() -> None:
        wrap.set_colors(border=ACCENT if state["focus"] else (FIELD_HOVER if state["hover"] else BORDER))

    def on(name: str, value: bool) -> None:
        state[name] = value
        paint()

    widget.bind("<FocusIn>", lambda _e: on("focus", True), add="+")
    widget.bind("<FocusOut>", lambda _e: on("focus", False), add="+")
    widget.bind("<Enter>", lambda _e: on("hover", True), add="+")
    widget.bind("<Leave>", lambda _e: on("hover", False), add="+")
    widget.wrapper = wrap  # type: ignore[attr-defined]
    return widget


class PillLabel(tk.Label):
    """Texto sobre una pastilla redondeada (contadores, etiquetas, filtros)."""

    def __init__(self, master: tk.Misc, text: str = "", *, fill: str, fg: str, font: tuple, scale: float,
                 padx: int = 9, pady: int = 3, **kw) -> None:
        super().__init__(master, bd=0, highlightthickness=0, bg=master.cget("bg"), compound="center",
                         padx=0, pady=0, **kw)
        self._font, self._scale, self._padx, self._pady = font, scale, padx, pady
        self.set(text, fill=fill, fg=fg)

    def set(self, text: str | None = None, *, fill: str | None = None, fg: str | None = None) -> None:
        if text is not None:
            self._text = text
        if fill is not None:
            self._fill = fill
        if fg is not None:
            self._fg = fg
        f = tkfont.Font(font=self._font)
        w = f.measure(self._text) + 2 * int(self._padx * self._scale)
        h = f.metrics("linespace") + 2 * int(self._pady * self._scale)
        img = rounded_image(self, w, h, self._fill, radius=h // 2)
        self.configure(image=img, text=self._text, fg=self._fg, font=self._font)


def style_title_bar(widget: tk.Misc, caption: str | None = None) -> None:
    """Barra de título del tema (oscura en Windows 10 20H1+) y, en Windows 11, del color de la app."""
    caption = BASE if caption is None else caption
    try:
        widget.update_idletasks()
        hwnd = int(widget.wm_frame(), 16)
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1 if current_theme == "dark" else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))  # DWMWA_USE_IMMERSIVE_DARK_MODE
        r, g, b = _rgb(caption)
        color = ctypes.c_int(r | (g << 8) | (b << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))  # DWMWA_CAPTION_COLOR
    except Exception:  # noqa: BLE001 - versiones antiguas de Windows: barra estándar
        log.debug("No se pudo oscurecer la barra de título", exc_info=True)


def exclude_from_capture(widget: tk.Misc) -> None:
    """Oculta la ventana a la captura de pantalla (WDA_EXCLUDEFROMCAPTURE).

    WordLens lee el texto de la pantalla: sin esto, su propia ventana (con frases
    en inglés como "Press Start…") se procesaría como si fuera un subtítulo.
    El usuario la sigue viendo con normalidad. Requiere Windows 10 2004 o posterior.
    WORDLENS_CAPTURE_VISIBLE=1 lo desactiva (capturas de pantalla de la propia app).
    """
    if os.environ.get("WORDLENS_CAPTURE_VISIBLE") == "1":
        return
    try:
        widget.update_idletasks()
        hwnd = int(widget.wm_frame(), 16)
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
    except Exception:  # noqa: BLE001 - versiones antiguas de Windows: sin efecto
        log.debug("No se pudo excluir la ventana de la captura", exc_info=True)


def _mix(color: str, other: str, t: float) -> str:
    """Mezcla dos colores #RRGGBB (t=0 → color, t=1 → other)."""
    a, b = _rgb(color), _rgb(other)
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def _doc_glyph(size: int) -> Image.Image:
    """Icono de documento: hoja con esquina doblada y líneas, sobre un cuadrado tenue."""
    k = 4
    s = size * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.26), fill=_rgb(SECONDARY))
    x0, y0, x1, y1, fold = s * 0.31, s * 0.22, s * 0.69, s * 0.78, s * 0.13
    w = max(1, int(s * 0.05))
    color = _rgb(ACCENT)
    d.line([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1), (x0, y0)], fill=color, width=w, joint="curve")
    d.line([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)], fill=color, width=w)
    for i, frac in enumerate((0.48, 0.58, 0.68)):
        y = s * frac
        d.line((x0 + s * 0.07, y, x1 - s * (0.07 if i < 2 else 0.15), y), fill=_rgb(TEAL), width=w)
    return img.resize((size, size), Image.LANCZOS)


def _dot(color: str, size: int) -> ImageTk.PhotoImage:
    k = 4
    img = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, size * k - 1, size * k - 1), fill=color)
    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))


class MainWindow:
    def __init__(
        self,
        app: WordLensApp,
        *,
        on_hotkeys_changed: Callable[[], dict[str, str]],
        on_exit: Callable[[], None],
    ) -> None:
        self.app = app
        self.settings = app.settings
        self._on_hotkeys_changed = on_hotkeys_changed
        self._on_exit = on_exit
        self._calls: queue.Queue[Callable[[], None]] = queue.Queue()
        self._starting = False
        self._busy_text: str | None = None
        self._told_about_tray = False
        self._shown_at = 0.0
        self._download_client: OllamaClient | None = None
        self._ai_text = "Checking AI model…"
        self._ai_color = SUBTLE
        self.tray = None  # lo asigna main.py
        self._watching_screens = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        log.info("Escala de la interfaz: %.2f (tk scaling %s)", self.scale, self.root.tk.call("tk", "scaling"))
        self.root.title("WordLens")
        apply_theme(self.settings.theme)
        self.root.configure(bg=BASE)
        if ICON_PATH.exists():
            self.root.iconbitmap(default=str(ICON_PATH))
        self._setup_styles()
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Unmap>", self._on_unmap)

        self._logo = ImageTk.PhotoImage(render_icon(int(36 * self.scale)))
        self._dots: dict[str, ImageTk.PhotoImage] = {}
        self._pulse_on = False
        self._generation = 0  # se incrementa al reconstruir la vista (cambio de tema)
        self._ai_polling = False
        self.container = tk.Frame(self.root, bg=BASE)
        self.container.pack(fill="both", expand=True)

        app.state.subscribe(lambda _o, _n: self.call_soon(self.refresh))
        app.on_stats(lambda _s: self.call_soon(self.refresh))
        app.on_error(lambda _e: self.call_soon(self.refresh))
        app.on_review_ready(lambda _n: self.call_soon(self.open_review))
        app.store.subscribe(lambda: self.call_soon(self.refresh))
        self._review = None
        self.root.after(80, self._drain_calls)

        if self.settings.setup_complete:
            self._build_main()
        else:
            self._build_setup()

    # ---------------------------------------------------------------- utilidades
    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Ejecuta fn en el hilo de Tk (seguro desde cualquier hilo)."""
        self._calls.put(fn)

    def _drain_calls(self) -> None:
        try:
            while True:
                fn = self._calls.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("Error en una acción de la interfaz")
        except queue.Empty:
            pass
        self.root.after(80, self._drain_calls)

    def dot(self, color: str, size: int = 10) -> ImageTk.PhotoImage:
        key = f"{color}:{size}"
        if key not in self._dots:
            self._dots[key] = _dot(color, self.px(size))
        return self._dots[key]

    def _setup_styles(self) -> None:
        p = self.px
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure(
            "WL.TCombobox",
            fieldbackground=CARD,
            background=CARD,
            foreground=TEXT,
            bordercolor=CARD,
            lightcolor=CARD,
            darkcolor=CARD,
            arrowcolor=MUTED,
            insertcolor=TEXT,
            padding=(p(5), p(3)),
            arrowsize=p(11),
        )
        style.map(
            "WL.TCombobox",
            fieldbackground=[("readonly", CARD), ("disabled", CARD)],
            background=[("active", CARD), ("disabled", CARD)],
            foreground=[("disabled", SUBTLE)],
            arrowcolor=[("disabled", SUBTLE), ("active", TEXT)],
            bordercolor=[("focus", CARD), ("hover", CARD), ("disabled", CARD)],
            lightcolor=[("focus", CARD), ("disabled", CARD)],
            darkcolor=[("focus", CARD), ("disabled", CARD)],
            selectbackground=[("readonly", CARD), ("!readonly", SELECTION)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "WL.TEntry",
            fieldbackground=CARD,
            foreground=TEXT,
            bordercolor=CARD,
            lightcolor=CARD,
            darkcolor=CARD,
            insertcolor=TEXT,
            selectbackground=SELECTION,
            selectforeground=TEXT,
            padding=(p(5), p(3)),
        )
        style.map("WL.TEntry", bordercolor=[("focus", CARD), ("hover", CARD)], lightcolor=[("focus", CARD)],
                  darkcolor=[("focus", CARD)])
        for name, bg in (("WL.TCheckbutton", CARD), ("WL.Base.TCheckbutton", BG)):
            style.configure(
                name,
                background=bg,
                foreground=TEXT,
                font=("Segoe UI", 10),
                indicatorsize=p(14),
                indicatormargin=(0, 0, p(8), 0),
                indicatorbackground=BASE,
                indicatorforeground="#FFFFFF",
                upperbordercolor=FIELD_HOVER,
                lowerbordercolor=FIELD_HOVER,
            )
            style.map(
                name,
                background=[("active", bg)],
                indicatorbackground=[("selected", ACCENT), ("active", SECONDARY)],
                upperbordercolor=[("selected", ACCENT)],
                lowerbordercolor=[("selected", ACCENT)],
            )
        # Barra de desplazamiento fina, sin flechas y con el «pulgar» redondeado (elemento de imagen).
        thumb = f"WL{current_theme}.thumb"
        if thumb not in style.element_names():
            normal = rounded_image(self.root, p(6), p(30), SCROLL_THUMB, radius=p(3))
            active = rounded_image(self.root, p(6), p(30), FIELD_HOVER, radius=p(3))
            self.__dict__.setdefault("_thumbs", {})[current_theme] = (normal, active)  # que no las libere la caché
            style.element_create(thumb, "image", normal, ("active", active), ("pressed", active),
                                 border=p(3), sticky="ns")
        style.layout(
            "WL.Vertical.TScrollbar",
            [("Vertical.Scrollbar.trough", {"children": [(thumb, {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})],
        )
        style.configure("WL.Vertical.TScrollbar", troughcolor=CARD, bordercolor=CARD, lightcolor=CARD, darkcolor=CARD,
                        gripcount=0, arrowsize=p(7), width=p(8))
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SELECTION)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

    def _clear(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    def _header(self, parent: tk.Misc, subtitle: str, bg: str | None = None) -> None:
        bg = BASE if bg is None else bg
        head = tk.Frame(parent, bg=bg)
        head.pack(fill="x", pady=(0, self.px(20)))
        tk.Label(head, image=self._logo, bg=bg).pack(side="left")
        names = tk.Frame(head, bg=bg)
        names.pack(side="left", padx=(self.px(11), 0))
        tk.Label(names, text="WordLens", bg=bg, fg=TEXT, font=("Segoe UI Semibold", 14)).pack(anchor="w")
        tk.Label(names, text=subtitle, bg=bg, fg=SUBTLE, font=("Segoe UI", 8)).pack(anchor="w")

    def _section_label(self, parent: tk.Misc, text: str, bg: str | None = None) -> tk.Label:
        bg = BASE if bg is None else bg
        return tk.Label(parent, text=text.upper(), bg=bg, fg=SUBTLE, font=("Segoe UI Semibold", 7))

    def _language_pickers(self, parent: tk.Misc, bg: str | None = None) -> tuple[ttk.Combobox, ttk.Combobox]:
        bg = BASE if bg is None else bg
        row = tk.Frame(parent, bg=bg)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1, uniform="lang")
        row.columnconfigure(2, weight=1, uniform="lang")
        self._section_label(row, "Subtitles in", bg).grid(row=0, column=0, sticky="w")
        self._section_label(row, "Translate to", bg).grid(row=0, column=2, sticky="w")
        names = list(LANGUAGES)
        def combo(master: tk.Misc) -> ttk.Combobox:
            return ttk.Combobox(master, values=names, state="readonly", style="WL.TCombobox", font=("Segoe UI", 10),
                                width=1)

        src = rounded_field(row, combo, scale=self.scale)
        dst = rounded_field(row, combo, scale=self.scale)
        src.set(self.settings.source_language)
        dst.set(self.settings.target_language)
        src.wrapper.grid(row=1, column=0, sticky="ew", pady=(self.px(5), 0))
        tk.Label(row, text="→", bg=bg, fg=SUBTLE, font=("Segoe UI", 11)).grid(
            row=1, column=1, padx=self.px(7), pady=(self.px(5), 0)
        )
        dst.wrapper.grid(row=1, column=2, sticky="ew", pady=(self.px(5), 0))

        def keep_different(changed: ttk.Combobox, other: ttk.Combobox) -> None:
            if changed.get() == other.get():
                other.set(next(n for n in names if n != changed.get()))

        src.bind("<<ComboboxSelected>>", lambda _e: (keep_different(src, dst), self._languages_changed(src, dst)))
        dst.bind("<<ComboboxSelected>>", lambda _e: (keep_different(dst, src), self._languages_changed(src, dst)))
        return src, dst

    def _languages_changed(self, src: ttk.Combobox, dst: ttk.Combobox) -> None:
        if not self.settings.setup_complete:
            return  # en el asistente se guardan al pulsar Continue
        self.settings.source_language, self.settings.target_language = src.get(), dst.get()
        save_settings(self.settings)
        log.info("Idiomas: %s -> %s", src.get(), dst.get())

    # ------------------------------------------------------------- primer uso
    def _build_setup(self) -> None:
        self._clear()
        self.view = "setup"
        p = self.px
        self.root.resizable(False, False)
        self.root.minsize(1, 1)
        self.root.geometry("")
        body = tk.Frame(self.container, bg=BASE, padx=p(30), pady=p(26))
        body.pack(fill="both", expand=True)
        self._header(body, "Learn vocabulary from subtitles")
        tk.Label(body, text="Let's set things up", bg=BASE, fg=TEXT, font=("Segoe UI Semibold", 16)).pack(anchor="w")
        tk.Label(
            body,
            text="Choose where to keep your vocabulary document and the languages you are working with.",
            bg=BASE, fg=MUTED, font=("Segoe UI", 10), wraplength=p(360), justify="left",
        ).pack(anchor="w", pady=(p(4), p(18)))

        current = self.settings.document if self.settings.document_path else default_document()
        self._setup_folder = tk.StringVar(value=str(current.parent))
        self._setup_name = tk.StringVar(value=current.name)

        card = RoundedCard(body, scale=self.scale, padx=p(16), pady=p(14))
        card.pack(fill="x")
        self._section_label(card, "Folder", CARD).pack(anchor="w")
        frow = tk.Frame(card, bg=CARD)
        frow.pack(fill="x", pady=(p(5), p(12)))
        rounded_field(frow, lambda m: ttk.Entry(m, textvariable=self._setup_folder, style="WL.TEntry",
                                                font=("Segoe UI", 10), width=1), scale=self.scale).wrapper.pack(
            side="left", fill="x", expand=True
        )
        RoundButton(
            frow, "Browse…", self._setup_browse_folder, width=84, height=32, scale=self.scale,
            bg=SECONDARY, hover=SECONDARY_HOVER, fg=TEXT, font=("Segoe UI", 9), parent_bg=CARD, radius=8,
        ).pack(side="left", padx=(p(8), 0))

        self._section_label(card, "File name", CARD).pack(anchor="w")
        rounded_field(card, lambda m: ttk.Entry(m, textvariable=self._setup_name, style="WL.TEntry",
                                                font=("Segoe UI", 10), width=1), scale=self.scale).wrapper.pack(
            fill="x", pady=(p(5), p(6))
        )
        LinkButton(card, "Use an existing document…", self._setup_pick_existing, bg=CARD).pack(anchor="w")

        langs = RoundedCard(body, scale=self.scale, padx=p(16), pady=p(14))
        langs.pack(fill="x", pady=(p(12), 0))
        self._setup_src, self._setup_dst = self._language_pickers(langs, CARD)

        self._setup_error = tk.Label(body, text="", bg=BASE, fg=STATUS_COLORS["Error"], font=("Segoe UI", 9),
                                     wraplength=p(360), justify="left")
        self._setup_error.pack(anchor="w", pady=(p(10), 0))
        RoundButton(
            body, "Continue  →", self._setup_finish, width=362, height=44, scale=self.scale,
            bg=GRADIENT, hover=GRADIENT_HOVER, font=("Segoe UI Semibold", 11), parent_bg=BASE, radius=11,
        ).pack(pady=(p(8), 0))
        tk.Label(
            body,
            text="You can change all of this later.",
            bg=BASE, fg=SUBTLE, font=("Segoe UI", 8),
        ).pack(pady=(p(8), 0))

    def _setup_browse_folder(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.root, title="Folder for your vocabulary document", initialdir=self._setup_folder.get()
        )
        if folder:
            self._setup_folder.set(str(Path(folder)))

    def _setup_pick_existing(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Choose your existing vocabulary document",
            initialdir=self._setup_folder.get(),
            filetypes=[("Word document", "*.docx")],
        )
        if chosen:
            path = Path(chosen)
            self._setup_folder.set(str(path.parent))
            self._setup_name.set(path.name)

    def _setup_finish(self) -> None:
        folder = self._setup_folder.get().strip()
        name = self._setup_name.get().strip()
        if not folder or not Path(folder).is_absolute():
            self._setup_error.configure(text="Choose a folder for the document.")
            return
        if not name or any(c in name for c in '<>:"/\\|?*'):
            self._setup_error.configure(text="Enter a valid file name.")
            return
        if not name.lower().endswith(".docx"):
            name += ".docx"
        path = Path(folder) / name
        error = self._validate_document(path)
        if error:
            self._setup_error.configure(text=error)
            return
        self.settings.document_path = str(path)
        self.settings.source_language = self._setup_src.get()
        self.settings.target_language = self._setup_dst.get()
        self.settings.setup_complete = True
        save_settings(self.settings)
        log.info("Configuración inicial completada: %s (%s -> %s)", path,
                 self.settings.source_language, self.settings.target_language)
        self._build_main()
        self.show()

    def _validate_document(self, path: Path) -> str | None:
        """Crea el documento si no existe y comprueba que sea un .docx en el que se puede escribir."""
        writer = WordWriter(path)
        try:
            writer.ensure_exists()
            writer.check_writable()
        except DocumentError as exc:
            return str(exc)
        except OSError as exc:
            log.warning("No se puede usar %s: %s", path, exc)
            if controlled_folder_access_enabled() and isinstance(exc, (FileNotFoundError, PermissionError)):
                return (
                    "Windows is blocking WordLens from writing in this folder (Controlled folder access). "
                    "Choose another folder, or allow WordLens in Windows Security › Virus & threat protection "
                    "› Ransomware protection › Allow an app through Controlled folder access."
                )
            return f"Can't use this location: {exc.strerror or exc}"
        return None

    # -------------------------------------------------------- ventana principal
    def _build_main(self) -> None:
        from wordlens.ui.review import ReviewPanel

        self._clear()
        self.view = "main"
        p = self.px
        self.root.resizable(True, True)
        self.root.minsize(p(860), p(660))
        self.root.geometry(f"{p(1000)}x{p(690)}")
        self.container.columnconfigure(1, weight=1)
        self.container.rowconfigure(0, weight=1)

        # ---- Barra lateral: sesión y configuración
        side = tk.Frame(self.container, bg=BASE, width=p(330), padx=p(22), pady=p(20))
        side.grid(row=0, column=0, sticky="ns")
        side.pack_propagate(False)
        tk.Frame(self.container, bg=BORDER, width=1).grid(row=0, column=0, sticky="nse")
        inner_w = 330 - 44  # ancho útil de la barra (px lógicos)
        # Pie (se empaqueta antes que el resto para que nunca quede recortado): estado de la IA + ajustes
        foot = tk.Frame(side, bg=BASE)
        foot.pack(side="bottom", fill="x")
        self.ai_dot = tk.Label(foot, bg=BASE)
        self.ai_dot.pack(side="left")
        self.ai_label = tk.Label(foot, text="", bg=BASE, fg=SUBTLE, font=("Segoe UI", 8))
        self.ai_label.pack(side="left", padx=(p(6), 0))
        LinkButton(foot, "⚙  Settings", self.open_settings, bg=BASE, fg=MUTED, hover_fg=TEXT).pack(side="right")
        tk.Frame(side, bg=BORDER, height=1).pack(side="bottom", fill="x", pady=(0, p(12)))
        self._header(side, "Learn vocabulary from subtitles")

        # Tarjeta de sesión
        card = RoundedCard(side, scale=self.scale, radius=16, padx=p(16), pady=p(14))
        card.pack(fill="x")
        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x")
        self.status_dot = tk.Label(top, bg=CARD)
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(top, text="", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 12))
        self.status_label.pack(side="left", padx=(p(8), 0))
        self.timer_label = tk.Label(top, text="", bg=CARD, fg=MUTED, font=("Consolas", 10))
        self.timer_label.pack(side="right")
        self.detail_label = tk.Label(card, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9), height=2,
                                     wraplength=p(inner_w - 34), justify="left", anchor="nw")
        self.detail_label.pack(fill="x", pady=(p(6), 0))
        # Altura fija: que la ventana no cambie de tamaño al aparecer una acción.
        self.action_row = tk.Frame(card, bg=CARD, height=p(22))
        self.action_row.pack(fill="x")
        self.action_row.pack_propagate(False)

        metrics = tk.Frame(card, bg=CARD)
        metrics.pack(fill="x", pady=(p(4), p(14)))
        metrics.columnconfigure(0, weight=1, uniform="m")
        metrics.columnconfigure(1, weight=1, uniform="m")
        self.count_label = self._metric(metrics, 0, "New this session")
        self.review_count_label = self._metric(metrics, 1, "To review")

        self.main_btn = RoundButton(
            card, "▶  Start session", self.toggle_capture, width=inner_w - 34, height=44, scale=self.scale,
            bg=GRADIENT, hover=GRADIENT_HOVER, font=("Segoe UI Semibold", 11), parent_bg=CARD, radius=11,
        )
        self.main_btn.pack()
        self.hotkey_label = tk.Label(card, text="", bg=CARD, fg=SUBTLE, font=("Segoe UI", 8))
        self.hotkey_label.pack(pady=(p(8), 0))

        # Idiomas
        tk.Frame(side, bg=BASE, height=p(20)).pack(fill="x")
        self.src_combo, self.dst_combo = self._language_pickers(side)

        # Pantalla de captura
        self._build_screen_picker(side)

        # Documento
        self._section_label(side, "Vocabulary document").pack(anchor="w", pady=(p(16), p(6)))
        doc = RoundedCard(side, scale=self.scale, padx=p(12), pady=p(10))
        doc.pack(fill="x")
        self._doc_icon = ImageTk.PhotoImage(_doc_glyph(p(30)))
        tk.Label(doc, image=self._doc_icon, bg=CARD).pack(side="left", padx=(0, p(10)))
        names = tk.Frame(doc, bg=CARD)
        names.pack(side="left", fill="x", expand=True)
        self.doc_name = tk.Label(names, text="", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 10), anchor="w")
        self.doc_name.pack(fill="x")
        self.doc_folder = tk.Label(names, text="", bg=CARD, fg=SUBTLE, font=("Segoe UI", 8), anchor="w")
        self.doc_folder.pack(fill="x")
        links = tk.Frame(names, bg=CARD)
        links.pack(fill="x", pady=(p(2), 0))
        LinkButton(links, "Open", self.open_document, bg=CARD, font=("Segoe UI", 8)).pack(side="left")
        tk.Label(links, text="·", bg=CARD, fg=SUBTLE, font=("Segoe UI", 8)).pack(side="left", padx=p(4))
        LinkButton(links, "Show in folder", self.open_document_folder, bg=CARD, font=("Segoe UI", 8)).pack(side="left")
        self.change_btn = RoundButton(
            doc, "Change", self.change_document, width=64, height=28, scale=self.scale,
            bg=SECONDARY, hover=SECONDARY_HOVER, fg=TEXT, font=("Segoe UI", 9), parent_bg=CARD, radius=8,
        )
        self.change_btn.pack(side="right")


        # ---- Panel de vocabulario integrado (siempre visible)
        panel = tk.Frame(self.container, bg=BG)
        panel.grid(row=0, column=1, sticky="nsew")
        self._review = ReviewPanel(self, panel)

        self.refresh()
        self._generation += 1
        self._tick(self._generation)
        self.check_ai_async()
        if not self._ai_polling:
            self._ai_polling = True
            self.root.after(30_000, self._periodic_ai_check)

    def _metric(self, parent: tk.Frame, column: int, caption: str) -> tk.Label:
        """Cifra grande con su rótulo (tarjeta de sesión)."""
        box = tk.Frame(parent, bg=CARD)
        box.grid(row=0, column=column, sticky="w")
        value = tk.Label(box, text="0", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 18))
        value.pack(anchor="w")
        tk.Label(box, text=caption.upper(), bg=CARD, fg=SUBTLE, font=("Segoe UI Semibold", 7)).pack(anchor="w")
        return value

    def _tick(self, generation: int) -> None:
        """Cada medio segundo: cronómetro de la sesión y latido del punto de estado."""
        if generation != self._generation or getattr(self, "view", "") != "main":
            return
        if self.app.state.is_active and self.app.stats.started_at:
            secs = int(time.time() - self.app.stats.started_at)
            self.timer_label.configure(text=f"{secs // 3600:d}:{secs // 60 % 60:02d}:{secs % 60:02d}")
            self._pulse_on = not self._pulse_on
            color = STATUS_COLORS[self.status_text()]
            self.status_dot.configure(image=self.dot(color if self._pulse_on else _mix(color, CARD, 0.55)))
        elif self.timer_label.cget("text"):
            self.timer_label.configure(text="")
        self.root.after(500, lambda: self._tick(generation))

    # ------------------------------------------------------- pantalla de captura
    def _build_screen_picker(self, body: tk.Frame) -> None:
        p = self.px
        head = tk.Frame(body, bg=BASE)
        head.pack(fill="x", pady=(p(16), p(6)))
        self._section_label(head, "Capture screen").pack(side="left")
        self.identify_link = LinkButton(head, "Identify", self.identify_screens, bg=BASE, font=("Segoe UI", 8))
        self.identify_link.pack(side="right")
        self.screen_combo = rounded_field(
            body, lambda m: ttk.Combobox(m, state="readonly", style="WL.TCombobox", font=("Segoe UI", 10), width=1),
            scale=self.scale,
        )
        self.screen_combo.wrapper.pack(fill="x")
        self.screen_combo.bind("<<ComboboxSelected>>", lambda _e: self._screen_chosen())
        self.screen_warning = tk.Label(
            body, text="", bg=BASE, fg=STATUS_COLORS["Processing"], font=("Segoe UI", 8),
            wraplength=p(280), justify="left", anchor="w",
        )
        self._monitors: list[monitors.Monitor] = []
        self._monitor_sig: tuple | None = None
        self._update_screens(force=True)
        if not self._watching_screens:
            self._watching_screens = True
            self.root.after(2000, self._watch_screens)

    def _watch_screens(self) -> None:
        """Detecta monitores conectados o desconectados mientras la ventana existe."""
        try:
            if getattr(self, "view", "") == "main":
                self._update_screens()
        finally:
            self.root.after(2000, self._watch_screens)

    def _update_screens(self, force: bool = False) -> None:
        found = monitors.list_monitors()
        sig = monitors.signature(found)
        if sig == self._monitor_sig and not force:
            return
        if self._monitor_sig is not None and sig != self._monitor_sig:
            log.info("Pantallas conectadas: %s", [f"{m.label} {m.device}" for m in found])
        self._monitors, self._monitor_sig = found, sig
        s = self.settings
        selection = monitors.resolve(found, s.capture_monitor_id, s.capture_monitor)
        if selection.monitor and not selection.missing and s.capture_monitor != selection.monitor.number:
            # La misma pantalla puede cambiar de número al conectar/desconectar otras.
            s.capture_monitor = selection.monitor.number
            save_settings(s)
        labels = [m.label for m in found]
        self.screen_combo.configure(values=labels)
        if selection.missing:
            self.screen_combo.set(f"Screen {s.capture_monitor} · not connected")
            main = selection.monitor.label if selection.monitor else "no screen"
            self.screen_warning.configure(
                text=f"The screen you chose is not connected. Using {main} until you "
                "reconnect it or choose another one."
            )
            self.screen_warning.pack(fill="x", pady=(self.px(3), 0), after=self.screen_combo.wrapper)
        else:
            self.screen_combo.set(selection.monitor.label if selection.monitor else "No screen detected")
            self.screen_warning.pack_forget()
        # Con una sola pantalla no hay nada que elegir (se muestra, pero fija).
        single = len(found) <= 1 and not selection.missing
        self.screen_combo.configure(state="disabled" if single else "readonly")

    def _screen_chosen(self) -> None:
        idx = self.screen_combo.current()
        if not 0 <= idx < len(self._monitors):
            return
        chosen = self._monitors[idx]
        s = self.settings
        s.capture_monitor, s.capture_monitor_id = chosen.number, chosen.device_id
        save_settings(s)
        log.info("Pantalla de captura: %s (%s, %s)", chosen.label, chosen.device, chosen.device_id)
        self._update_screens(force=True)  # la sesión activa la aplica en ~1 s

    def identify_screens(self) -> None:
        """Muestra el número de cada pantalla sobre ella durante unos segundos."""
        size = self.px(150)
        for m in monitors.list_monitors():
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            chosen = m.device_id == self.settings.capture_monitor_id or (
                not self.settings.capture_monitor_id and m.number == self.settings.capture_monitor
            )
            color = GRADIENT[0] if chosen else SECONDARY
            top.configure(bg=color)
            tk.Label(top, text=str(m.number), bg=color, fg="#FFFFFF", font=("Segoe UI Semibold", 64)).pack(
                expand=True, fill="both"
            )
            x = m.left + (m.width - size) // 2
            y = m.top + (m.height - size) // 2
            top.geometry(f"{size}x{size}+{x}+{y}")
            exclude_from_capture(top)
            top.after(2500, top.destroy)

    def _periodic_ai_check(self) -> None:
        """Mantiene al día el estado de Ollama mostrado en el pie (p. ej. si se cierra)."""
        if not self.app.state.is_active and not self._starting and self._download_client is None:
            self.check_ai_async()
        self.root.after(30_000, self._periodic_ai_check)

    # --------------------------------------------------------------- refresco
    def status_text(self) -> str:
        if not self.settings.setup_complete:
            return "Setup"
        if self._starting:
            return "Starting…"
        state = self.app.state.state
        if self.app.last_error is not None and not self.app.state.is_active:
            return "Error"
        return STATE_LABELS[state]

    def refresh(self) -> None:
        if self.tray is not None:
            self.tray.refresh()
        if getattr(self, "view", "") != "main":
            return
        s = self.settings
        status = self.status_text()
        color = STATUS_COLORS[status]
        self.status_dot.configure(image=self.dot(color))
        self.status_label.configure(text=status)
        stats = self.app.stats
        active = self.app.state.is_active
        in_session = status in ("Capturing", "Processing", "Finishing", "Stopped") and stats.started_at
        self.count_label.configure(text=str(stats.added if in_session else 0), fg=TEAL if in_session and stats.added else TEXT)
        try:
            counts = self.app.counts()
        except Exception:  # noqa: BLE001 - la base de datos se está cerrando
            counts = {"pending": 0, "unsaved": 0}
        to_review = counts["pending"] + counts["unsaved"]
        self.review_count_label.configure(text=str(to_review))

        for child in self.action_row.winfo_children():
            child.destroy()
        err = self.app.last_error
        if self._busy_text:
            detail = self._busy_text
            if self._download_client is not None:
                LinkButton(self.action_row, "Cancel", self._cancel_download, bg=CARD).pack(anchor="w")
        elif status == "Error" and err is not None:
            detail = err.message
            action = ERROR_ACTIONS.get(err.kind)
            if action:
                LinkButton(self.action_row, action, lambda e=err: self._error_action(e), bg=CARD).pack(anchor="w")
        elif status == "Starting…":
            detail = "Checking the document and the AI model…"
        elif status == "Finishing":
            detail = stats.phase or "Finishing the session…"
        elif active:
            last = ", ".join(stats.last_added[:3])
            sel = self.app.capture_selection
            where = f"Screen {sel.monitor.number}" if sel and sel.monitor else "the screen"
            detail = f"Watching {where} for subtitles."
            if last:
                detail += f" Latest: {last}"
        elif status == "Stopped":
            detail = "Session ended. Review the words on the right, or start a new session."
        else:
            detail = "Start a session, then play a video with subtitles."
        if counts["unsaved"] and self.app.saver.last_error and not self._busy_text:
            detail = f"{counts['unsaved']} accepted not saved yet: {self.app.saver.last_error}"
        self.detail_label.configure(text=detail)

        self.doc_folder.configure(
            text=elide_middle(str(s.document.parent), tkfont.Font(font=("Segoe UI", 8)), self.px(130))
        )
        self.doc_name.configure(
            text=elide_middle(s.document.name, tkfont.Font(font=("Segoe UI Semibold", 10)), self.px(130))
        )
        busy = self._starting or self._download_client is not None
        if active:
            self.main_btn.update_button(text="■  Stop session", bg=STOP, hover=STOP_HOVER, fg=STOP_FG, enabled=True)
        else:
            self.main_btn.update_button(text="▶  Start session", bg=GRADIENT, hover=GRADIENT_HOVER, fg="#FFFFFF",
                                        enabled=not busy)
        locked = active or busy
        self.change_btn.update_button(enabled=not locked)
        combo_state = "disabled" if locked else "readonly"
        self.src_combo.configure(state=combo_state)
        self.dst_combo.configure(state=combo_state)
        self.hotkey_label.configure(
            text=f"{pretty_hotkey(s.hotkey_start)}  start   ·   {pretty_hotkey(s.hotkey_stop)}  stop"
        )
        self.ai_dot.configure(image=self.dot(self._ai_color, 8))
        self.ai_label.configure(text=self._ai_text)
        if self._review is not None:
            self._review.update_header()

    # ------------------------------------------------------------ IA (Ollama)
    def check_ai_async(self) -> None:
        def work() -> None:
            status = ollama_setup.check(self.app.client, self.settings.ollama_model, autostart=False)
            self.call_soon(lambda: self._set_ai_status(status))

        threading.Thread(target=work, name="ai-check", daemon=True).start()

    def _set_ai_status(self, status: ollama_setup.OllamaStatus) -> None:
        model = self.settings.ollama_model
        if status.ok:
            self._ai_text, self._ai_color = f"AI ready · {model}", STATUS_COLORS["Capturing"]
            if len(self._ai_text) > 32:
                self._ai_text = self._ai_text[:31] + "…"
        elif status.state is ollama_setup.OllamaState.NOT_RUNNING:
            self._ai_text, self._ai_color = "Ollama is off · starts with Start", STATUS_COLORS["Processing"]
        elif status.state is ollama_setup.OllamaState.MODEL_MISSING:
            self._ai_text, self._ai_color = "AI model not downloaded", STATUS_COLORS["Error"]
        else:
            self._ai_text, self._ai_color = "Ollama is not installed", STATUS_COLORS["Error"]
        self.refresh()

    def _error_action(self, err: AppError) -> None:
        if err.kind == "ollama_missing":
            webbrowser.open(ollama_setup.DOWNLOAD_URL)
        elif err.kind == "model_missing":
            self.download_model()
        elif err.kind == "document":
            self.change_document()
        else:
            self.start_capture()

    def download_model(self) -> None:
        model = self.settings.ollama_model
        ok = messagebox.askyesno(
            "Download AI model",
            f"WordLens needs the local AI model “{model}”.\n\n"
            "This is a one-time download of several gigabytes (about 5.3 GB for the default model) "
            "and may take a while. It is stored by Ollama, not by WordLens.\n\nDownload it now?",
            parent=self.root,
        )
        if not ok:
            return
        client = OllamaClient(self.settings.ollama_url, timeout=60)
        self._download_client = client
        self._busy_text = "Preparing download…"
        self.app.clear_error()
        self.refresh()

        def progress(status: str, done: int, total: int) -> None:
            if total:
                text = f"Downloading {model}… {done * 100 // total}% ({done / 1e9:.1f} of {total / 1e9:.1f} GB)"
            else:
                text = f"Downloading {model}… {status}"

            def apply() -> None:
                self._busy_text = text
                self.refresh()

            self.call_soon(apply)

        def work() -> None:
            message = None
            try:
                if not ollama_setup.wait_until_running(client, 1) and not (
                    ollama_setup.launch_ollama() and ollama_setup.wait_until_running(client)
                ):
                    message = "Ollama is not running. Open it from the Start menu and try again."
                else:
                    client.pull(model, progress)
            except OllamaAborted:
                message = "Download cancelled."
            except OllamaError as exc:
                message = f"Download failed: {exc}"

            def done() -> None:
                self._download_client = None
                self._busy_text = None
                if message:
                    self.app.notify(message)
                    messagebox.showwarning("WordLens", message, parent=self.root)
                else:
                    self.app.notify(f"Model {model} downloaded. You can press Start now.")
                self.check_ai_async()
                self.refresh()

            self.call_soon(done)

        threading.Thread(target=work, name="model-pull", daemon=True).start()

    def _cancel_download(self) -> None:
        if self._download_client is not None:
            self._download_client.abort()

    # ---------------------------------------------------------------- acciones
    def toggle_capture(self) -> None:
        if self.app.state.is_active:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self) -> None:
        if not self.settings.setup_complete:
            self.show()
            return
        if self._starting or self.app.state.is_active or self._download_client is not None:
            return
        self._starting = True
        self.refresh()

        def work() -> None:
            ok = False
            try:
                ok = self.app.start()
            finally:
                def done() -> None:
                    self._starting = False
                    self.refresh()
                    if not ok and self.app.last_error is not None:
                        self.show()  # que el error y su solución se vean
                    self.check_ai_async()

                self.call_soon(done)

        threading.Thread(target=work, name="start", daemon=True).start()

    def stop_capture(self) -> None:
        threading.Thread(target=self.app.stop, name="stop", daemon=True).start()

    def change_document(self) -> None:
        if self.app.state.is_active:
            messagebox.showinfo("WordLens", "Stop the capture before changing the document.", parent=self.root)
            return
        current = self.settings.document
        chosen = filedialog.asksaveasfilename(
            parent=self.root,
            title="Choose or create the vocabulary document (an existing one is never overwritten)",
            initialdir=str(current.parent if current.parent.exists() else Path.home()),
            initialfile=current.name,
            defaultextension=".docx",
            filetypes=[("Word document", "*.docx")],
            confirmoverwrite=False,
        )
        if not chosen:
            return
        path = Path(chosen)
        existed = path.exists()
        error = self._validate_document(path)
        if error:
            messagebox.showerror("WordLens", error, parent=self.root)
            return
        self.settings.document_path = str(path)
        save_settings(self.settings)
        self.app.clear_error()
        log.info("Documento: %s", path)
        self.app.notify(
            f"Using {'existing' if existed else 'new'} document: {path.name}"
            + (" (its content is kept)" if existed else "")
        )
        self.refresh()

    def open_document(self) -> None:
        path = self.settings.document
        if path.exists():
            os.startfile(path)  # noqa: S606
        else:
            messagebox.showinfo("WordLens", "The document will be created when you press Start.", parent=self.root)

    def open_document_folder(self) -> None:
        path = self.settings.document
        if path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif path.parent.exists():
            os.startfile(path.parent)  # noqa: S606

    def open_settings(self) -> None:
        SettingsDialog(self)

    def open_review(self) -> None:
        """Muestra la ventana principal: el panel de vocabulario está integrado en ella."""
        if self._review is None:
            return  # todavía en la configuración inicial
        self.show()
        self._review.reload()

    def open_excluded(self, parent: tk.Misc | None = None) -> None:
        from wordlens.ui.review import ExcludedDialog

        ExcludedDialog(self, parent)

    # ------------------------------------------------------- ventana / bandeja
    def show(self) -> None:
        self._shown_at = time.monotonic()
        self.root.deiconify()
        style_title_bar(self.root)
        exclude_from_capture(self.root)
        self.root.state("normal")
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def hide(self) -> None:
        """Cerrar o minimizar no sale de WordLens: sigue en la bandeja."""
        self.root.withdraw()
        if not self._told_about_tray and self.tray is not None:
            self._told_about_tray = True
            self.tray.notify(
                "WordLens",
                "WordLens is still running in the system tray. Right-click its icon to exit.",
            )

    def _on_unmap(self, event: tk.Event) -> None:
        if event.widget is not self.root or self.root.state() != "iconic":
            return
        if time.monotonic() - self._shown_at < 1.5:
            # Minimizada por Windows al aparecer (p. ej. lanzada con SW_SHOWMINNOACTIVE),
            # no por el usuario: se restaura en lugar de ocultarla en la bandeja.
            self.root.after(10, lambda: self.root.state("normal"))
            return
        self.root.after(10, self.hide)

    def center(self) -> None:
        self.root.update_idletasks()
        size = self.root.geometry().split("+")[0].split("x")
        w = max(self.root.winfo_reqwidth(), int(size[0]))
        h = max(self.root.winfo_reqheight(), int(size[1]))
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

    def exit(self) -> None:
        if self._review is not None:
            self._review.flush()  # decisiones a la espera de «Undo»: se aplican antes de salir
        self._on_exit()

    def set_theme(self, name: str) -> None:
        """Cambia de tema y reconstruye la ventana principal con la nueva paleta."""
        if self._review is not None:
            self._review.flush()
        self.settings.theme = apply_theme(name)
        save_settings(self.settings)
        self._dots.clear()
        self._setup_styles()
        self.root.configure(bg=BASE)
        self.container.configure(bg=BASE)
        self._review = None
        self._build_main()
        style_title_bar(self.root)

    def run(self, *, hidden: bool = False) -> None:
        self.center()
        if not hidden or not self.settings.setup_complete:
            self.show()
        self.root.mainloop()

    def quit(self) -> None:
        self.root.quit()


class SettingsDialog:
    """Ajustes poco frecuentes: modelo, atajos, notificaciones y carpetas internas."""

    def __init__(self, win: MainWindow) -> None:
        self.win = win
        s = win.settings
        p = win.px
        if getattr(win, "_settings_top", None) is not None and win._settings_top.winfo_exists():
            win._settings_top.deiconify()
            win._settings_top.lift()
            return
        top = tk.Toplevel(win.root)
        win._settings_top = top
        self.top = top
        top.title("WordLens settings")
        top.configure(bg=BG)
        top.resizable(False, False)
        top.transient(win.root)
        body = tk.Frame(top, bg=BG, padx=p(22), pady=p(20))
        body.pack(fill="both", expand=True)
        tk.Frame(body, bg=BG, width=p(380), height=0).pack()  # ancho fijo del diálogo
        tk.Label(body, text="Settings", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(body, text="Models, shortcuts and vocabulary preferences.", bg=BG, fg=SUBTLE,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, p(10)))
        locked = win.app.state.is_active

        def section(title: str) -> tk.Frame:
            tk.Label(body, text=title, bg=BG, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(0, p(6)))
            frame = RoundedCard(body, scale=win.scale, padx=p(14), pady=p(10))
            frame.pack(fill="x", pady=(0, p(10)))
            return frame

        card = section("AI models")
        win._section_label(card, "Vocabulary extraction (Ollama)", bg=CARD).pack(anchor="w")
        self.model = rounded_field(card, lambda m: ttk.Combobox(m, values=[s.ollama_model], style="WL.TCombobox",
                                                                font=("Segoe UI", 10)), scale=win.scale)
        self.model.set(s.ollama_model)
        self.model.wrapper.pack(fill="x", pady=(p(5), p(10)))
        threading.Thread(target=self._load_models, daemon=True).start()

        win._section_label(card, "Translation (Ollama)", bg=CARD).pack(anchor="w")
        self.tmodel = rounded_field(card, lambda m: ttk.Combobox(m, values=[s.translation_model], style="WL.TCombobox",
                                                                 font=("Segoe UI", 10)), scale=win.scale)
        self.tmodel.set(s.translation_model)
        self.tmodel.wrapper.pack(fill="x", pady=(p(5), 0))

        card = section("General")
        look = tk.Frame(card, bg=CARD)
        look.pack(fill="x", pady=(0, p(12)))
        win._section_label(look, "Appearance", bg=CARD).pack(side="left")
        self.theme = rounded_field(look, lambda m: ttk.Combobox(m, values=list(THEMES), state="readonly",
                                                                style="WL.TCombobox", font=("Segoe UI", 10), width=10),
                                   scale=win.scale)
        self.theme.set(next((label for label, key in THEMES.items() if key == current_theme), "Dark"))
        self.theme.wrapper.pack(side="right")
        keys = tk.Frame(card, bg=CARD)
        keys.pack(fill="x")
        keys.columnconfigure(0, weight=1, uniform="k")
        keys.columnconfigure(1, weight=1, uniform="k")
        win._section_label(keys, "Start session", bg=CARD).grid(row=0, column=0, sticky="w")
        win._section_label(keys, "Stop session", bg=CARD).grid(row=0, column=1, sticky="w", padx=(p(10), 0))
        self.hk_start = tk.StringVar(value=s.hotkey_start)
        rounded_field(keys, lambda m: ttk.Entry(m, textvariable=self.hk_start, style="WL.TEntry", font=("Consolas", 10),
                                                width=1), scale=win.scale).wrapper.grid(
            row=1, column=0, sticky="ew", pady=(p(5), 0))
        self.hk_stop = tk.StringVar(value=s.hotkey_stop)
        rounded_field(keys, lambda m: ttk.Entry(m, textvariable=self.hk_stop, style="WL.TEntry", font=("Consolas", 10),
                                                width=1), scale=win.scale).wrapper.grid(
            row=1, column=1, sticky="ew", padx=(p(10), 0), pady=(p(5), 0))
        tk.Label(card, text="Format: ctrl+shift+v, ctrl+alt+f9…", bg=CARD, fg=SUBTLE, font=("Segoe UI", 8)).pack(
            anchor="w", pady=(p(5), 0))

        self.notifications = tk.BooleanVar(value=s.notifications)
        ttk.Checkbutton(card, text="Show notifications", variable=self.notifications, style="WL.TCheckbutton").pack(
            anchor="w", pady=(p(10), 0)
        )
        if locked:
            tk.Label(card, text="Stop the session to change these settings.", bg=CARD, fg=STATUS_COLORS["Error"],
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(p(8), 0))

        excluded = section("Vocabulary")
        win._section_label(excluded, "Excluded vocabulary", bg=CARD).pack(anchor="w")
        n_excluded = win.app.counts()["excluded"]
        tk.Label(
            excluded,
            text=("Nothing excluded yet." if n_excluded == 0 else
                  f"{n_excluded} {'entry' if n_excluded == 1 else 'words and expressions'} you marked with ✕."),
            bg=CARD, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(p(2), p(4)))
        LinkButton(excluded, "Manage excluded vocabulary…", lambda: win.open_excluded(top), bg=CARD).pack(anchor="w")

        links = tk.Frame(body, bg=BG)
        links.pack(fill="x")
        LinkButton(links, "Open log", self._open_log, bg=BG, fg=MUTED, hover_fg=TEXT).pack(side="left")
        tk.Label(links, text="·", bg=BG, fg=SUBTLE).pack(side="left", padx=p(4))
        LinkButton(links, "Data folder", lambda: os.startfile(DATA_DIR), bg=BG, fg=MUTED,  # noqa: S606
                   hover_fg=TEXT).pack(side="left")
        tk.Label(links, text="·", bg=BG, fg=SUBTLE).pack(side="left", padx=p(4))
        LinkButton(links, "Advanced (config.json)", self._open_config, bg=BG, fg=MUTED, hover_fg=TEXT).pack(side="left")

        self.error = tk.Label(body, text="", bg=BG, fg=STATUS_COLORS["Error"], font=("Segoe UI", 9),
                              wraplength=p(320), justify="left")
        self.error.pack(anchor="w", pady=(p(8), 0))
        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x", pady=(p(6), 0))
        save = RoundButton(buttons, "Save", self._save, width=110, height=36, scale=win.scale, bg=GRADIENT,
                           hover=GRADIENT_HOVER, font=("Segoe UI Semibold", 10), radius=9)
        save.pack(side="right")
        save.update_button(enabled=not locked)
        RoundButton(buttons, "Cancel", top.destroy, width=90, height=36, scale=win.scale, bg=SECONDARY,
                    hover=SECONDARY_HOVER, fg=TEXT, font=("Segoe UI", 10), radius=9).pack(side="right", padx=(0, p(8)))

        top.update_idletasks()
        x = win.root.winfo_rootx() + (win.root.winfo_width() - top.winfo_reqwidth()) // 2
        y = win.root.winfo_rooty() + p(40)
        if not win.root.winfo_viewable():
            x = (top.winfo_screenwidth() - top.winfo_reqwidth()) // 2
            y = (top.winfo_screenheight() - top.winfo_reqheight()) // 3
        y = min(y, top.winfo_screenheight() - top.winfo_reqheight() - p(90))  # botones por encima de la barra de tareas
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.lift()
        top.focus_force()
        style_title_bar(top, BG)
        exclude_from_capture(top)

    def _load_models(self) -> None:
        try:
            models = self.win.app.client.list_models()
        except OllamaError:
            return
        if models:
            def apply() -> None:
                if self.top.winfo_exists():
                    self.model.configure(values=models)
                    self.tmodel.configure(values=models)

            self.win.call_soon(apply)

    def _open_log(self) -> None:
        log_file = LOG_DIR / "wordlens.log"
        if log_file.exists():
            subprocess.Popen(["notepad.exe", str(log_file)])

    def _open_config(self) -> None:
        save_settings(self.win.settings)
        subprocess.Popen(["notepad.exe", str(CONFIG_PATH)])
        messagebox.showinfo(
            "WordLens", "Advanced options are applied the next time WordLens starts.", parent=self.top
        )

    def _save(self) -> None:
        s = self.win.settings
        start, stop = self.hk_start.get().strip().lower(), self.hk_stop.get().strip().lower()
        try:
            parse_hotkey(start)
            parse_hotkey(stop)
        except HotkeyError as exc:
            self.error.configure(text=f"Invalid shortcut: {exc}")
            return
        if start == stop:
            self.error.configure(text="Start and stop shortcuts must be different.")
            return
        model = self.model.get().strip()
        if not model:
            self.error.configure(text="Enter the name of an Ollama model.")
            return
        hotkeys_changed = (start, stop) != (s.hotkey_start, s.hotkey_stop)
        s.hotkey_start, s.hotkey_stop = start, stop
        s.ollama_model = model
        s.translation_model = self.tmodel.get().strip() or model
        s.notifications = bool(self.notifications.get())
        new_theme = THEMES.get(self.theme.get(), "dark")
        save_settings(s)
        if hotkeys_changed:
            failed = self.win._on_hotkeys_changed()
            if failed:
                spec, why = next(iter(failed.items()))
                self.error.configure(text=f"Couldn't register {spec}: {why}. Choose another shortcut.")
                return
        self.win.app.clear_error()
        self.top.destroy()
        if new_theme != current_theme:
            self.win.set_theme(new_theme)
        self.win.check_ai_async()
        self.win.refresh()
