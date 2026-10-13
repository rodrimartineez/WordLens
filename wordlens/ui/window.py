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

# Paleta
BG = "#F6F7FB"
CARD = "#FFFFFF"
BORDER = "#E3E6EE"
TEXT = "#141827"
MUTED = "#6B7280"
SUBTLE = "#9AA1AE"
ACCENT = "#4F46E5"
ACCENT_HOVER = "#4338CA"
STOP = "#1F2433"
STOP_HOVER = "#363C4F"
SECONDARY = "#EEF0F7"
SECONDARY_HOVER = "#E2E5F0"
DISABLED = "#C9CCD8"

STATUS_COLORS = {
    "Ready": "#4F46E5",
    "Starting…": "#D97706",
    "Capturing": "#16A34A",
    "Processing": "#D97706",
    "Stopped": "#64748B",
    "Error": "#DC2626",
    "Setup": "#4F46E5",
}
STATE_LABELS = {
    AppState.INACTIVE: "Ready",
    AppState.CAPTURING: "Capturing",
    AppState.PROCESSING: "Processing",
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


class RoundButton(tk.Canvas):
    """Botón con esquinas redondeadas y antialiasing (dibujado con Pillow)."""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        width: int,
        height: int,
        scale: float,
        bg: str = ACCENT,
        hover: str = ACCENT_HOVER,
        fg: str = "#FFFFFF",
        font: tuple = ("Segoe UI Semibold", 11),
        parent_bg: str = BG,
        radius: int = 10,
    ) -> None:
        self._bw, self._bh = int(width * scale), int(height * scale)
        super().__init__(
            master, width=self._bw, height=self._bh, bg=parent_bg, highlightthickness=0, bd=0, cursor="hand2"
        )
        self._text, self._command, self._font, self._fg = text, command, font, fg
        self._bg, self._hover = bg, hover
        self._radius = int(radius * scale)
        self._enabled, self._hovering = True, False
        self._images: dict[str, ImageTk.PhotoImage] = {}
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _image(self, color: str) -> ImageTk.PhotoImage:
        if color not in self._images:
            k = 3
            img = Image.new("RGBA", (self._bw * k, self._bh * k), (0, 0, 0, 0))
            ImageDraw.Draw(img).rounded_rectangle(
                (0, 0, self._bw * k - 1, self._bh * k - 1), radius=self._radius * k, fill=color
            )
            self._images[color] = ImageTk.PhotoImage(img.resize((self._bw, self._bh), Image.LANCZOS))
        return self._images[color]

    def _draw(self) -> None:
        self.delete("all")
        color = DISABLED if not self._enabled else (self._hover if self._hovering else self._bg)
        self.create_image(0, 0, image=self._image(color), anchor="nw")
        self.create_text(self._bw / 2, self._bh / 2, text=self._text, fill=self._fg, font=self._font)

    def _set_hover(self, value: bool) -> None:
        self._hovering = value
        self._draw()

    def _click(self, event: tk.Event) -> None:
        if self._enabled and 0 <= event.x <= self._bw and 0 <= event.y <= self._bh:
            self._command()

    def update_button(
        self, *, text: str | None = None, bg: str | None = None, hover: str | None = None, enabled: bool | None = None
    ) -> None:
        if text is not None:
            self._text = text
        if bg is not None:
            self._bg = bg
        if hover is not None:
            self._hover = hover
        if enabled is not None:
            self._enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()


class LinkButton(tk.Label):
    def __init__(self, master: tk.Misc, text: str, command: Callable[[], None], *, bg: str, font=("Segoe UI", 9)):
        super().__init__(master, text=text, bg=bg, fg=ACCENT, font=font, cursor="hand2")
        self._command = command
        self.bind("<Enter>", lambda _e: self.configure(fg=ACCENT_HOVER, font=font + ("underline",)))
        self.bind("<Leave>", lambda _e: self.configure(fg=ACCENT, font=font))
        self.bind("<ButtonRelease-1>", lambda _e: self._command())


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
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        if ICON_PATH.exists():
            self.root.iconbitmap(default=str(ICON_PATH))
        self._setup_styles()
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Unmap>", self._on_unmap)

        self._logo = ImageTk.PhotoImage(render_icon(int(40 * self.scale)))
        self._dots: dict[str, ImageTk.PhotoImage] = {}
        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(fill="both", expand=True)

        app.state.subscribe(lambda _o, _n: self.call_soon(self.refresh))
        app.on_stats(lambda _s: self.call_soon(self.refresh))
        app.on_error(lambda _e: self.call_soon(self.refresh))
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
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "WL.TCombobox",
            fieldbackground=CARD,
            background=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=CARD,
            darkcolor=CARD,
            arrowcolor=MUTED,
            padding=(self.px(8), self.px(5)),
            arrowsize=self.px(12),
        )
        style.map(
            "WL.TCombobox",
            fieldbackground=[("readonly", CARD), ("disabled", BG)],
            foreground=[("disabled", SUBTLE)],
            bordercolor=[("focus", ACCENT), ("hover", "#C7CBE0")],
            selectbackground=[("readonly", CARD)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "WL.TEntry",
            fieldbackground=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=CARD,
            darkcolor=CARD,
            padding=(self.px(8), self.px(5)),
        )
        style.map("WL.TEntry", bordercolor=[("focus", ACCENT)])
        style.configure(
            "WL.TCheckbutton",
            background=CARD,
            foreground=TEXT,
            font=("Segoe UI", 10),
            indicatorsize=self.px(14),
            indicatormargin=(0, 0, self.px(8), 0),
            indicatorbackground=CARD,
            indicatorforeground="#FFFFFF",
            upperbordercolor="#B9BED0",
            lowerbordercolor="#B9BED0",
        )
        style.map(
            "WL.TCheckbutton",
            background=[("active", CARD)],
            indicatorbackground=[("selected", ACCENT), ("active", SECONDARY)],
            upperbordercolor=[("selected", ACCENT)],
            lowerbordercolor=[("selected", ACCENT)],
        )
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.background", CARD)

    def _clear(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    def _header(self, parent: tk.Misc, subtitle: str) -> None:
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(0, self.px(16)))
        tk.Label(head, image=self._logo, bg=BG).pack(side="left")
        names = tk.Frame(head, bg=BG)
        names.pack(side="left", padx=(self.px(12), 0))
        tk.Label(names, text="WordLens", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 16)).pack(anchor="w")
        tk.Label(names, text=subtitle, bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")

    def _section_label(self, parent: tk.Misc, text: str, bg: str = BG) -> tk.Label:
        return tk.Label(parent, text=text.upper(), bg=bg, fg=MUTED, font=("Segoe UI Semibold", 8))

    def _language_pickers(self, parent: tk.Misc) -> tuple[ttk.Combobox, ttk.Combobox]:
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1, uniform="lang")
        row.columnconfigure(1, weight=1, uniform="lang")
        self._section_label(row, "Subtitle language").grid(row=0, column=0, sticky="w")
        self._section_label(row, "Translation language").grid(row=0, column=1, sticky="w", padx=(self.px(12), 0))
        names = list(LANGUAGES)
        src = ttk.Combobox(row, values=names, state="readonly", style="WL.TCombobox", font=("Segoe UI", 10), width=1)
        dst = ttk.Combobox(row, values=names, state="readonly", style="WL.TCombobox", font=("Segoe UI", 10), width=1)
        src.set(self.settings.source_language)
        dst.set(self.settings.target_language)
        src.grid(row=1, column=0, sticky="ew", pady=(self.px(4), 0))
        dst.grid(row=1, column=1, sticky="ew", padx=(self.px(12), 0), pady=(self.px(4), 0))

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
        body = tk.Frame(self.container, bg=BG, padx=self.px(24), pady=self.px(22))
        body.pack(fill="both", expand=True)
        self._header(body, "First setup")
        tk.Label(
            body,
            text="Choose where to keep your vocabulary document and the languages you are working with.",
            bg=BG, fg=TEXT, font=("Segoe UI", 10), wraplength=self.px(330), justify="left",
        ).pack(anchor="w", pady=(0, self.px(16)))

        current = self.settings.document if self.settings.document_path else default_document()
        self._setup_folder = tk.StringVar(value=str(current.parent))
        self._setup_name = tk.StringVar(value=current.name)

        self._section_label(body, "Folder").pack(anchor="w")
        frow = tk.Frame(body, bg=BG)
        frow.pack(fill="x", pady=(self.px(4), self.px(12)))
        ttk.Entry(frow, textvariable=self._setup_folder, style="WL.TEntry", font=("Segoe UI", 10), width=1).pack(
            side="left", fill="x", expand=True
        )
        RoundButton(
            frow, "Browse…", self._setup_browse_folder, width=84, height=32, scale=self.scale,
            bg=SECONDARY, hover=SECONDARY_HOVER, fg=TEXT, font=("Segoe UI", 10), radius=8,
        ).pack(side="left", padx=(self.px(8), 0))

        self._section_label(body, "File name").pack(anchor="w")
        ttk.Entry(body, textvariable=self._setup_name, style="WL.TEntry", font=("Segoe UI", 10), width=1).pack(
            fill="x", pady=(self.px(4), self.px(4))
        )
        LinkButton(body, "Use an existing document…", self._setup_pick_existing, bg=BG).pack(
            anchor="w", pady=(0, self.px(16))
        )

        self._setup_src, self._setup_dst = self._language_pickers(body)

        self._setup_error = tk.Label(body, text="", bg=BG, fg=STATUS_COLORS["Error"], font=("Segoe UI", 9),
                                     wraplength=self.px(330), justify="left")
        self._setup_error.pack(anchor="w", pady=(self.px(12), 0))
        RoundButton(
            body, "Continue", self._setup_finish, width=332, height=44, scale=self.scale,
            font=("Segoe UI Semibold", 11),
        ).pack(pady=(self.px(10), 0))
        tk.Label(
            body,
            text="You can change all of this later.",
            bg=BG, fg=SUBTLE, font=("Segoe UI", 9),
        ).pack(pady=(self.px(8), 0))

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
        self._clear()
        self.view = "main"
        p = self.px
        body = tk.Frame(self.container, bg=BG, padx=p(24), pady=p(22))
        body.pack(fill="both", expand=True)
        self._header(body, "Learn vocabulary from subtitles")

        # Estado
        card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=p(16), pady=p(12))
        card.pack(fill="x", pady=(0, p(18)))
        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x")
        self.status_dot = tk.Label(top, bg=CARD)
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(top, text="", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 12))
        self.status_label.pack(side="left", padx=(p(8), 0))
        self.count_label = tk.Label(top, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.count_label.pack(side="right")
        self.detail_label = tk.Label(card, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9), height=2,
                                     wraplength=p(296), justify="left", anchor="nw")
        self.detail_label.pack(fill="x", pady=(p(4), 0))
        # Altura fija: que la ventana no cambie de tamaño al aparecer una acción.
        self.action_row = tk.Frame(card, bg=CARD, height=p(24))
        self.action_row.pack(fill="x")
        self.action_row.pack_propagate(False)

        # Idiomas
        self.src_combo, self.dst_combo = self._language_pickers(body)

        # Pantalla de captura
        self._build_screen_picker(body)

        # Documento
        self._section_label(body, "Vocabulary document").pack(anchor="w", pady=(p(16), p(4)))
        doc = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=p(12), pady=p(10))
        doc.pack(fill="x")
        names = tk.Frame(doc, bg=CARD)
        names.pack(side="left", fill="x", expand=True)
        self.doc_name = tk.Label(names, text="", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 10), anchor="w")
        self.doc_name.pack(fill="x")
        self.doc_folder = tk.Label(names, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w")
        self.doc_folder.pack(fill="x")
        links = tk.Frame(names, bg=CARD)
        links.pack(fill="x", pady=(p(2), 0))
        LinkButton(links, "Open", self.open_document, bg=CARD, font=("Segoe UI", 8)).pack(side="left")
        tk.Label(links, text="·", bg=CARD, fg=SUBTLE, font=("Segoe UI", 8)).pack(side="left", padx=p(4))
        LinkButton(links, "Show in folder", self.open_document_folder, bg=CARD, font=("Segoe UI", 8)).pack(side="left")
        self.change_btn = RoundButton(
            doc, "Change", self.change_document, width=76, height=30, scale=self.scale,
            bg=SECONDARY, hover=SECONDARY_HOVER, fg=TEXT, font=("Segoe UI", 9), parent_bg=CARD, radius=8,
        )
        self.change_btn.pack(side="right")

        # Start / Stop
        self.main_btn = RoundButton(
            body, "Start", self.toggle_capture, width=332, height=48, scale=self.scale,
            font=("Segoe UI Semibold", 12), radius=12,
        )
        self.main_btn.pack(pady=(p(20), p(8)))
        self.hotkey_label = tk.Label(body, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.hotkey_label.pack()
        tk.Label(
            body,
            text="Play a video with subtitles. New expressions are added to your document automatically.",
            bg=BG, fg=SUBTLE, font=("Segoe UI", 8), wraplength=p(320), justify="center",
        ).pack(pady=(p(4), 0))

        # Pie: estado de la IA + ajustes
        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(p(16), p(10)))
        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x")
        self.ai_dot = tk.Label(foot, bg=BG)
        self.ai_dot.pack(side="left")
        self.ai_label = tk.Label(foot, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.ai_label.pack(side="left", padx=(p(6), 0))
        LinkButton(foot, "Settings", self.open_settings, bg=BG).pack(side="right")

        self.refresh()
        self.check_ai_async()
        self.root.after(30_000, self._periodic_ai_check)

    # ------------------------------------------------------- pantalla de captura
    def _build_screen_picker(self, body: tk.Frame) -> None:
        p = self.px
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", pady=(p(16), p(4)))
        self._section_label(head, "Capture screen").pack(side="left")
        self.identify_link = LinkButton(head, "Identify", self.identify_screens, bg=BG, font=("Segoe UI", 8))
        self.identify_link.pack(side="right")
        self.screen_combo = ttk.Combobox(
            body, state="readonly", style="WL.TCombobox", font=("Segoe UI", 10), width=1
        )
        self.screen_combo.pack(fill="x")
        self.screen_combo.bind("<<ComboboxSelected>>", lambda _e: self._screen_chosen())
        self.screen_warning = tk.Label(
            body, text="", bg=BG, fg=STATUS_COLORS["Processing"], font=("Segoe UI", 8),
            wraplength=p(330), justify="left", anchor="w",
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
            self.screen_warning.pack(fill="x", pady=(self.px(3), 0), after=self.screen_combo)
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
            color = ACCENT if chosen else STOP
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
        if status in ("Capturing", "Processing", "Stopped"):
            self.count_label.configure(text=f"{stats.added} new this session")
        else:
            self.count_label.configure(text="")

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
        elif active:
            last = ", ".join(stats.last_added[:3])
            sel = self.app.capture_selection
            where = f"Screen {sel.monitor.number}" if sel and sel.monitor else "the screen"
            detail = f"Watching {where} for subtitles."
            if last:
                detail += f" Latest: {last}"
        elif status == "Stopped":
            detail = "Capture stopped. Press Start to begin a new session."
        else:
            detail = "Press Start, then play a video with subtitles."
        self.detail_label.configure(text=detail)

        self.doc_folder.configure(
            text=elide_middle(str(s.document.parent), tkfont.Font(font=("Segoe UI", 8)), self.px(215))
        )
        self.doc_name.configure(
            text=elide_middle(s.document.name, tkfont.Font(font=("Segoe UI Semibold", 10)), self.px(215))
        )
        busy = self._starting or self._download_client is not None
        if active:
            self.main_btn.update_button(text="Stop", bg=STOP, hover=STOP_HOVER, enabled=True)
        else:
            self.main_btn.update_button(text="Start", bg=ACCENT, hover=ACCENT_HOVER, enabled=not busy)
        locked = active or busy
        self.change_btn.update_button(enabled=not locked)
        combo_state = "disabled" if locked else "readonly"
        self.src_combo.configure(state=combo_state)
        self.dst_combo.configure(state=combo_state)
        self.hotkey_label.configure(
            text=f"{pretty_hotkey(s.hotkey_start)} to start   ·   {pretty_hotkey(s.hotkey_stop)} to stop"
        )
        self.ai_dot.configure(image=self.dot(self._ai_color, 8))
        self.ai_label.configure(text=self._ai_text)

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
            if len(self._ai_text) > 44:
                self._ai_text = self._ai_text[:43] + "…"
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

    # ------------------------------------------------------- ventana / bandeja
    def show(self) -> None:
        self._shown_at = time.monotonic()
        self.root.deiconify()
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
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

    def exit(self) -> None:
        self._on_exit()

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
        tk.Frame(body, bg=BG, width=p(340), height=0).pack()  # ancho fijo del diálogo
        tk.Label(body, text="Settings", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, p(12)))

        card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=p(14), pady=p(12))
        card.pack(fill="x")
        locked = win.app.state.is_active

        win._section_label(card, "AI model (Ollama)", bg=CARD).pack(anchor="w")
        self.model = ttk.Combobox(card, values=[s.ollama_model], style="WL.TCombobox", font=("Segoe UI", 10))
        self.model.set(s.ollama_model)
        self.model.pack(fill="x", pady=(p(4), p(12)))
        threading.Thread(target=self._load_models, daemon=True).start()

        win._section_label(card, "Start capture shortcut", bg=CARD).pack(anchor="w")
        self.hk_start = tk.StringVar(value=s.hotkey_start)
        ttk.Entry(card, textvariable=self.hk_start, style="WL.TEntry", font=("Segoe UI", 10), width=1).pack(fill="x", pady=(p(4), p(10)))
        win._section_label(card, "Stop capture shortcut", bg=CARD).pack(anchor="w")
        self.hk_stop = tk.StringVar(value=s.hotkey_stop)
        ttk.Entry(card, textvariable=self.hk_stop, style="WL.TEntry", font=("Segoe UI", 10), width=1).pack(fill="x", pady=(p(4), p(10)))
        tk.Label(card, text="Format: ctrl+shift+v, ctrl+alt+f9…", bg=CARD, fg=SUBTLE, font=("Segoe UI", 8)).pack(anchor="w")

        self.notifications = tk.BooleanVar(value=s.notifications)
        ttk.Checkbutton(card, text="Show notifications", variable=self.notifications, style="WL.TCheckbutton").pack(
            anchor="w", pady=(p(10), 0)
        )
        if locked:
            tk.Label(card, text="Stop the capture to change these settings.", bg=CARD, fg=STATUS_COLORS["Error"],
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(p(8), 0))

        links = tk.Frame(body, bg=BG)
        links.pack(fill="x", pady=(p(12), 0))
        LinkButton(links, "Open log", self._open_log, bg=BG).pack(side="left")
        tk.Label(links, text="·", bg=BG, fg=SUBTLE).pack(side="left", padx=p(4))
        LinkButton(links, "Data folder", lambda: os.startfile(DATA_DIR), bg=BG).pack(side="left")  # noqa: S606
        tk.Label(links, text="·", bg=BG, fg=SUBTLE).pack(side="left", padx=p(4))
        LinkButton(links, "Advanced (config.json)", self._open_config, bg=BG).pack(side="left")

        self.error = tk.Label(body, text="", bg=BG, fg=STATUS_COLORS["Error"], font=("Segoe UI", 9),
                              wraplength=p(320), justify="left")
        self.error.pack(anchor="w", pady=(p(8), 0))
        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x", pady=(p(6), 0))
        save = RoundButton(buttons, "Save", self._save, width=110, height=36, scale=win.scale, font=("Segoe UI Semibold", 10), radius=9)
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
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.lift()
        top.focus_force()
        exclude_from_capture(top)

    def _load_models(self) -> None:
        try:
            models = self.win.app.client.list_models()
        except OllamaError:
            return
        if models:
            self.win.call_soon(lambda: self.top.winfo_exists() and self.model.configure(values=models))

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
        s.notifications = bool(self.notifications.get())
        save_settings(s)
        if hotkeys_changed:
            failed = self.win._on_hotkeys_changed()
            if failed:
                spec, why = next(iter(failed.items()))
                self.error.configure(text=f"Couldn't register {spec}: {why}. Choose another shortcut.")
                return
        self.win.app.clear_error()
        self.win.check_ai_async()
        self.win.refresh()
        self.top.destroy()
