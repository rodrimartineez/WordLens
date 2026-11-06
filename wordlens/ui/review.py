"""Panel de vocabulario (integrado en la ventana principal) y vocabulario excluido.

ReviewPanel    lista de candidatos (inglés · español) con ✓ (al documento) y
               ✕ (a la lista de exclusión). Siempre visible a la derecha de la
               ventana principal; la lista se actualiza sola conforme se detectan
               palabras nuevas. Cada decisión se aplica tras unos segundos, durante
               los que se puede deshacer; la traducción se corrige en la propia fila;
               se puede filtrar por tipo, ordenar por frecuencia y usar el teclado.
ExcludedDialog lista de exclusión (Ajustes): buscar y restaurar.

La lista es virtual: sólo existen las filas visibles y se reutilizan al
desplazarse, así que una sesión con miles de candidatos no crea miles de
widgets. Los datos se leen siempre de vocabulary.db.
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from PIL import Image, ImageTk

from wordlens.ui.icon import render_icon
from wordlens.vocabulary.store import ReviewEntry, StoreError

from .window import (
    ACCENT,
    BG,
    BORDER,
    CARD,
    CARD_HOVER,
    DANGER_DIM,
    MUTED,
    ON_TEAL,
    SECONDARY,
    SECONDARY_HOVER,
    STATUS_COLORS,
    SUBTLE,
    TEAL,
    TEAL_DIM,
    TEAL_SOLID,
    TEXT,
    LinkButton,
    PillLabel,
    RoundButton,
    RoundedCard,
    exclude_from_capture,
    rounded_field,
    rounded_image,
    style_title_bar,
)

if TYPE_CHECKING:
    from .window import MainWindow

log = logging.getLogger(__name__)

KIND_LABELS = {"word": "", "phrasal verb": "phrasal verb", "idiom": "idiom", "collocation": "collocation",
               "expression": "expression"}
# Filtros por tipo: etiqueta -> tipos que incluye (None = todos).
KIND_FILTERS: dict[str, set[str] | None] = {
    "All": None,
    "Words": {"word"},
    "Phrasal verbs": {"phrasal verb"},
    "Idioms": {"idiom"},
    "Expressions": {"collocation", "expression"},
}
SORTS = ("Recent", "Frequent")
FRESH_SECONDS = 8  # tiempo que una palabra recién detectada queda marcada
CONTEXT_HINT = "↑ ↓ select   ·   A accept   ·   X exclude   ·   E edit   ·   Ctrl+Z undo   ·   Ctrl+F search"


def _matches(entry: ReviewEntry, query: str) -> bool:
    q = query.strip().lower()
    return not q or q in entry.expression.lower() or q in (entry.translation or "").lower()


@dataclass
class _Pending:
    """Decisión a la espera: se aplica al vencer el plazo (o antes, si llega otra)."""

    entry: ReviewEntry
    action: str  # "accept" | "exclude"
    timer: str | None = None


class _Chip(PillLabel):
    """Pastilla seleccionable (filtros y orden)."""

    def __init__(self, master: tk.Misc, text: str, command, *, scale: float) -> None:
        self._selected = False
        super().__init__(master, text, fill=SECONDARY, fg=MUTED, font=("Segoe UI", 8), scale=scale, padx=10, pady=3,
                         cursor="hand2")
        self.bind("<ButtonRelease-1>", lambda _e: command())
        self.bind("<Enter>", lambda _e: self._paint(hover=True))
        self.bind("<Leave>", lambda _e: self._paint())

    def select(self, value: bool) -> None:
        self._selected = value
        self._paint()

    def _paint(self, hover: bool = False) -> None:
        if self._selected:
            self.set(fill=TEAL_DIM, fg=TEAL)
        else:
            self.set(fill=SECONDARY_HOVER if hover else SECONDARY, fg=TEXT if hover else MUTED)


class _Row:
    """Fila reutilizable de la lista virtual."""

    def __init__(self, review: ReviewPanel, parent: tk.Frame) -> None:
        win = review.win
        p = win.px
        self.review = review
        self.entry: ReviewEntry | None = None
        self.frame = tk.Frame(parent, bg=CARD)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        # Fondo redondeado de la fila (resaltado al seleccionar; barra de acento si es nueva).
        self.tile = tk.Label(self.frame, bd=0, highlightthickness=0, bg=CARD)
        self.tile.place(x=0, y=0, relwidth=1, relheight=1, bordermode="outside")
        self.frame.bind("<Configure>", lambda _e: self._paint_tile(), add="+")
        self.texts = tk.Frame(self.frame, bg=CARD)
        self.texts.grid(row=0, column=0, sticky="we", padx=(p(16), p(8)))
        top = tk.Frame(self.texts, bg=CARD)
        top.pack(fill="x")
        self.expr = tk.Label(top, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 11), anchor="w")
        self.expr.pack(side="left")
        self.occ = tk.Label(top, bg=CARD, fg=SUBTLE, font=("Segoe UI", 8), anchor="w")
        self.occ.pack(side="left", padx=(p(5), 0), pady=(p(2), 0))
        self.kind = PillLabel(top, "", fill=SECONDARY, fg=MUTED, font=("Segoe UI", 7), scale=win.scale, padx=7, pady=1)
        self.trans = tk.Label(self.texts, bg=CARD, fg=MUTED, font=("Segoe UI", 10), anchor="w", justify="left")
        self.trans.pack(fill="x")
        self.actions = tk.Frame(self.frame, bg=CARD)
        self.actions.grid(row=0, column=1, sticky="e", padx=(0, p(12)))
        self.ok = RoundButton(self.actions, "✓", self._accept, width=36, height=32, scale=win.scale,
                              bg=TEAL_DIM, hover=TEAL_SOLID, fg=TEAL, hover_fg=ON_TEAL,
                              font=("Segoe UI Semibold", 12), parent_bg=CARD, radius=9)
        self.no = RoundButton(self.actions, "✕", self._exclude, width=36, height=32, scale=win.scale,
                              bg=SECONDARY, hover=DANGER_DIM, fg=MUTED, hover_fg=STATUS_COLORS["Error"],
                              font=("Segoe UI Semibold", 10), parent_bg=CARD, radius=9)
        self.status = tk.Label(self.actions, bg=CARD, fg=STATUS_COLORS["Processing"], font=("Segoe UI", 8),
                               justify="right", wraplength=p(150))
        self.retry = LinkButton(self.actions, "Retry", review.retry_saving, bg=CARD, font=("Segoe UI", 8))
        self.divider = tk.Frame(self.frame, bg=BORDER, height=1)
        self.divider.place(x=p(16), rely=1.0, y=-1, relwidth=1.0, width=-p(32), height=1, bordermode="outside")
        self._tinted = (self.frame, self.texts, top, self.expr, self.occ, self.trans, self.actions, self.status,
                        self.retry)
        self.selected = False
        self.fresh = False
        for widget in (self.frame, self.texts, top, self.expr, self.occ, self.kind, self.trans, self.actions):
            widget.bind("<Enter>", self._hover)
            widget.bind("<MouseWheel>", review.on_wheel)
        for widget in (self.ok, self.no):
            widget.bind("<MouseWheel>", review.on_wheel, add="+")
        self.trans.configure(cursor="xterm")
        self.trans.bind("<Double-Button-1>", lambda _e: self.entry and review.edit(self.entry))

    def show(self, entry: ReviewEntry, fresh: bool, selected: bool) -> None:
        self.entry = entry
        self.fresh = fresh
        width = self.review.text_width()
        self.expr.configure(text=entry.expression)
        self.occ.configure(text=f"×{entry.occurrences}" if entry.occurrences > 1 else "")
        kind = KIND_LABELS.get(entry.kind, "")
        if kind:
            px = self.review.win.px
            self.kind.set(kind)
            self.kind.pack(side="left", padx=(px(8), 0), pady=(px(3), 0))
        else:
            self.kind.pack_forget()
        if entry.translation:
            self.trans.configure(text=entry.translation, fg=MUTED, wraplength=width)
        else:
            self.trans.configure(text="Translating…  (double-click to write one)", fg=SUBTLE, wraplength=width)
        for w in (self.ok, self.no, self.status, self.retry):
            w.pack_forget()
        if entry.status == "saving":
            if entry.error:
                self.status.configure(text=entry.error, fg=STATUS_COLORS["Error"])
                self.status.pack(side="top", anchor="e")
                self.retry.pack(side="top", anchor="e")
            else:
                self.status.configure(text="Saving to Word…", fg=STATUS_COLORS["Processing"])
                self.status.pack(side="top", anchor="e")
        else:
            self.ok.pack(side="left", padx=(0, self.review.win.px(6)))
            self.no.pack(side="left")
            self.ok.update_button(enabled=bool(entry.translation))
        self.set_selected(selected)

    def set_selected(self, on: bool) -> None:
        self.selected = on
        color = CARD_HOVER if on else CARD
        for w in self._tinted:
            w.configure(bg=color)
        self.ok.configure(bg=color)
        self.no.configure(bg=color)
        self.kind.configure(bg=color)
        self._paint_tile()

    def _paint_tile(self) -> None:
        w, h = self.frame.winfo_width(), self.frame.winfo_height()
        if w <= 1 or h <= 1:
            return
        p = self.review.win.px
        color = CARD_HOVER if self.selected else CARD
        strip = TEAL if self.fresh else (ACCENT if self.selected else None)
        self.tile.configure(image=rounded_image(self.frame, w, h, color, radius=p(12), corner_bg=CARD, strip=strip,
                                                strip_width=p(3)))
        # Sin divisor junto a la fila resaltada: el resaltado ya separa.
        self.divider.configure(bg=CARD if self.selected else BORDER)

    def _hover(self, _e: tk.Event) -> None:
        if self.entry is not None:
            self.review.select(self.entry.key)

    def _accept(self) -> None:
        if self.entry is not None:
            self.review.accept(self.entry)

    def _exclude(self) -> None:
        if self.entry is not None:
            self.review.exclude(self.entry)


class ReviewPanel:
    """Panel de vocabulario de la ventana principal (siempre visible)."""

    UNDO_SECONDS = 5.0  # plazo para deshacer una decisión (0 = se aplica al momento)

    def __init__(self, win: MainWindow, parent: tk.Frame) -> None:
        self.win = win
        self.app = win.app
        p = win.px
        self.entries: list[ReviewEntry] = []
        self.visible: list[ReviewEntry] = []
        self.offset = 0  # píxeles desplazados en la lista virtual
        self.row_h = p(60)
        self._reload_pending = False
        self._last_reload = 0.0
        self._seen: set[str] | None = None  # claves ya mostradas (para marcar las nuevas)
        self._fresh: dict[str, float] = {}
        self._had_entries = False
        self.selected_key: str | None = None
        self.kind_filter = "All"
        self.sort = "Recent"
        self.pending: _Pending | None = None
        self._edit_key: str | None = None

        body = tk.Frame(parent, bg=BG, padx=p(26), pady=p(22))
        body.pack(fill="both", expand=True)
        self.body = body
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left")
        tk.Label(titles, text="Session vocabulary", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(
            titles, text="✓ adds a word to your document   ·   ✕ hides it from future sessions",
            bg=BG, fg=SUBTLE, font=("Segoe UI", 8), anchor="w",
        ).pack(anchor="w")
        self.counter = PillLabel(head, "", fill=CARD, fg=MUTED, font=("Segoe UI Semibold", 9), scale=win.scale,
                                 padx=11, pady=4)
        self.counter.pack(side="right", pady=(p(4), 0))

        tools = tk.Frame(body, bg=BG)
        tools.pack(fill="x", pady=(p(16), p(8)))
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.refilter())
        search = rounded_field(tools, lambda m: ttk.Entry(m, textvariable=self.query, style="WL.TEntry",
                                                          font=("Segoe UI", 10), width=18), scale=win.scale)
        search.wrapper.pack(side="left", fill="x", expand=True)
        self._placeholder(search, "⌕  Search words or translations…")
        search.bind("<Escape>", lambda _e: self._leave_search())
        search.bind("<Down>", lambda _e: self._leave_search())
        self.exclude_all_btn = RoundButton(tools, "Exclude all", self.exclude_all, width=92, height=32,
                                           scale=win.scale, bg=SECONDARY, hover=SECONDARY_HOVER, fg=MUTED,
                                           hover_fg=TEXT, font=("Segoe UI", 9), radius=8)
        self.exclude_all_btn.pack(side="right")
        self.accept_all_btn = RoundButton(tools, "✓  Accept all", self.accept_all, width=104, height=32,
                                          scale=win.scale, bg=TEAL_DIM, hover=TEAL_SOLID, fg=TEAL,
                                          hover_fg=ON_TEAL, font=("Segoe UI Semibold", 9), radius=8)
        self.accept_all_btn.pack(side="right", padx=(p(8), p(6)))

        # Filtros por tipo y orden.
        chips = tk.Frame(body, bg=BG)
        chips.pack(fill="x", pady=(0, p(10)))
        self.kind_chips: dict[str, _Chip] = {}
        for label in KIND_FILTERS:
            chip = _Chip(chips, label, lambda v=label: self.set_kind_filter(v), scale=win.scale)
            chip.pack(side="left", padx=(0, p(6)))
            self.kind_chips[label] = chip
        self.sort_chips: dict[str, _Chip] = {}
        for label in reversed(SORTS):
            chip = _Chip(chips, label, lambda v=label: self.set_sort(v), scale=win.scale)
            chip.pack(side="right", padx=(p(6), 0))
            self.sort_chips[label] = chip
        tk.Label(chips, text="Sort", bg=BG, fg=SUBTLE, font=("Segoe UI", 8)).pack(side="right")
        self._paint_chips()

        self.banner = RoundedCard(body, fill=DANGER_DIM, border=None, radius=12, scale=win.scale, padx=p(12), pady=p(7))
        self.banner_label = tk.Label(self.banner, text="", bg=DANGER_DIM, fg=STATUS_COLORS["Error"], font=("Segoe UI", 9),
                                     anchor="w", justify="left", wraplength=p(560))
        self.banner_label.pack(fill="x")

        frame = RoundedCard(body, radius=16, scale=win.scale, padx=p(6), pady=p(6))
        frame.pack(fill="both", expand=True)
        self.list_frame = frame
        self.viewport = tk.Frame(frame, bg=CARD)
        self.viewport.pack(side="left", fill="both", expand=True)
        self.scroll = ttk.Scrollbar(frame, orient="vertical", command=self.on_scroll, style="WL.Vertical.TScrollbar")
        self.rows: list[_Row] = []
        self.viewport.bind("<Configure>", lambda _e: self.render())
        self.viewport.bind("<MouseWheel>", self.on_wheel)

        # Edición en la propia fila.
        self.editor_var = tk.StringVar()
        self.editor = rounded_field(self.viewport, lambda m: ttk.Entry(m, textvariable=self.editor_var, style="WL.TEntry",
                                                                       font=("Segoe UI", 10)), scale=win.scale)
        self.editor.bind("<Return>", lambda _e: self._finish_edit(save=True))
        self.editor.bind("<KP_Enter>", lambda _e: self._finish_edit(save=True))
        self.editor.bind("<Escape>", lambda _e: self._finish_edit(save=False))
        self.editor.bind("<FocusOut>", lambda _e: self._finish_edit(save=True))

        # Estado vacío: icono tenue + título + explicación.
        self.empty_box = tk.Frame(self.viewport, bg=CARD)
        faded = render_icon(p(56))
        faded.putalpha(faded.getchannel("A").point(lambda a: a * 0.55))
        self._empty_icon = ImageTk.PhotoImage(Image.alpha_composite(Image.new("RGBA", faded.size, CARD), faded))
        tk.Label(self.empty_box, image=self._empty_icon, bg=CARD).pack()
        self.empty_title = tk.Label(self.empty_box, text="", bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 12))
        self.empty_title.pack(pady=(p(12), p(4)))
        self.empty = tk.Label(self.empty_box, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 9), justify="center")
        self.empty.pack()
        for w in (self.empty_box, *self.empty_box.winfo_children()):
            w.bind("<MouseWheel>", self.on_wheel)

        # Aviso «Deshacer» sobre la parte inferior de la lista.
        self.undo_bar = RoundedCard(self.viewport, fill=TEXT, border=None, radius=14, scale=win.scale, padx=p(16),
                                    pady=p(8))
        self.undo_label = tk.Label(self.undo_bar, text="", bg=TEXT, fg=CARD, font=("Segoe UI", 9))
        self.undo_label.pack(side="left")
        LinkButton(self.undo_bar, "Undo", self.undo, bg=TEXT, fg=TEAL_SOLID, hover_fg=TEAL_SOLID,
                   font=("Segoe UI Semibold", 9)).pack(side="left", padx=(p(14), 0))

        self.context = tk.Label(body, text=CONTEXT_HINT, bg=BG, fg=SUBTLE, font=("Segoe UI", 9, "italic"), anchor="w",
                                justify="left", wraplength=p(560), height=2)
        self.context.pack(fill="x", pady=(p(8), 0))
        body.bind("<Configure>", lambda e: self._wrap(e.width))
        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", pady=(p(2), 0))
        self.foot_status = tk.Label(foot, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w")
        self.foot_status.pack(side="left")
        LinkButton(foot, "Open document  ↗", win.open_document, bg=BG, font=("Segoe UI", 8)).pack(side="right")

        win.root.bind("<KeyPress>", self._on_key, add="+")
        self.app.store.subscribe(self._store_changed)
        self.reload()

    # -------------------------------------------------------------- utilidades
    def _placeholder(self, entry: ttk.Entry, text: str) -> None:
        def show(_e=None):
            if not self.query.get():
                entry._ph = True  # type: ignore[attr-defined]
                entry.configure(foreground=SUBTLE)
                entry.insert(0, text)

        def hide(_e=None):
            if getattr(entry, "_ph", False):
                entry._ph = False  # type: ignore[attr-defined]
                entry.delete(0, "end")
                entry.configure(foreground=TEXT)

        entry.bind("<FocusIn>", hide)
        entry.bind("<FocusOut>", show)
        self._ph_entry = entry
        show()

    def search_text(self) -> str:
        return "" if getattr(self._ph_entry, "_ph", False) else self.query.get()

    def text_width(self) -> int:
        return max(self.win.px(160), self.viewport.winfo_width() - self.win.px(140))

    def _wrap(self, width: int) -> None:
        inner = max(self.win.px(200), width - self.win.px(56))
        self.context.configure(wraplength=inner)
        self.banner_label.configure(wraplength=inner - self.win.px(24))

    def exists(self) -> bool:
        try:
            return bool(self.body.winfo_exists())
        except tk.TclError:
            return False

    def _leave_search(self) -> str:
        self.win.root.focus_set()
        if self.visible and self.selected_key is None:
            self.select(self.visible[0].key)
        return "break"

    # ------------------------------------------------------------------- datos
    def _store_changed(self) -> None:
        # Desde cualquier hilo: se agrupa en una recarga como mucho cada 250 ms.
        if self._reload_pending:
            return
        self._reload_pending = True
        self.win.call_soon(lambda: self.body.after(250, self._deferred_reload) if self.exists() else None)

    def _deferred_reload(self) -> None:
        self._reload_pending = False
        if self.exists():
            self.reload()

    def reload(self) -> None:
        try:
            self.entries = self.app.review_entries()
        except Exception:
            log.exception("No se pudo leer la lista de revisión")
            self.entries = []
        now = time.monotonic()
        self._last_reload = now
        keys = {e.key for e in self.entries}
        if self._seen is not None and keys - self._seen:
            for key in keys - self._seen:
                self._fresh[key] = now
            self.body.after(FRESH_SECONDS * 1000 + 100, self.render)
        self._seen = keys
        self._fresh = {k: t for k, t in self._fresh.items() if now - t < FRESH_SECONDS}
        self._had_entries = self._had_entries or bool(self.entries)
        self.refilter(keep_offset=True)

    def refilter(self, keep_offset: bool = False) -> None:
        if not hasattr(self, "viewport"):
            return  # todavía construyendo el panel
        q = self.search_text()
        kinds = KIND_FILTERS[self.kind_filter]
        hidden = self.pending.entry.key if self.pending else None
        self.visible = [
            e for e in self.entries
            if e.key != hidden and _matches(e, q) and (kinds is None or e.kind in kinds)
        ]
        if self.sort == "Frequent":
            self.visible.sort(key=lambda e: -e.occurrences)  # estable: a igualdad, el orden reciente
        if not keep_offset:
            self.offset = 0
        if self.selected_key not in {e.key for e in self.visible}:
            self.selected_key = None
        self.render()
        self.update_header()

    def set_kind_filter(self, label: str) -> None:
        self.kind_filter = label
        self._paint_chips()
        self.refilter()

    def set_sort(self, label: str) -> None:
        self.sort = label
        self._paint_chips()
        self.refilter()

    def _paint_chips(self) -> None:
        for label, chip in self.kind_chips.items():
            chip.select(label == self.kind_filter)
        for label, chip in self.sort_chips.items():
            chip.select(label == self.sort)

    def update_header(self) -> None:
        pending = sum(e.status == "pending" for e in self.entries) - (1 if self.pending else 0)
        saving = [e for e in self.entries if e.status == "saving"]
        failed = [e for e in saving if e.error]
        parts = [f"{pending} to review"]
        if saving:
            parts.append(f"{len(saving)} saving")
        self.counter.set("  ·  ".join(parts), fg=TEAL if pending else MUTED, fill=TEAL_DIM if pending else CARD)
        if failed:
            self.banner_label.configure(text=f"⚠  {len(failed)} accepted entr{'y is' if len(failed) == 1 else 'ies are'} not "
                                       f"saved yet: {failed[0].error}  They are kept and retried automatically.")
            self.banner.pack(fill="x", pady=(0, self.win.px(8)), before=self.list_frame)
        else:
            self.banner.pack_forget()
        actionable = [e for e in self.visible if e.status == "pending"]
        self.accept_all_btn.update_button(enabled=any(e.translation for e in actionable))
        self.exclude_all_btn.update_button(enabled=bool(actionable))
        for label, chip in self.kind_chips.items():
            kinds = KIND_FILTERS[label]
            n = sum(1 for e in self.entries if kinds is None or e.kind in kinds)
            chip.set(f"{label}  {n}" if n else label)
        app = self.app
        phase = app.stats.phase if app.finishing else ""
        untranslated = sum(1 for e in self.entries if e.status == "pending" and not e.translation)
        if phase:
            self.foot_status.configure(text=phase, fg=STATUS_COLORS["Processing"])
        elif untranslated:
            self.foot_status.configure(text=f"{untranslated} without translation yet (they are retried automatically).",
                                       fg=MUTED)
        else:
            self.foot_status.configure(text=f"Saving to  {app.settings.document.name}", fg=SUBTLE)
        if not self.visible:
            self._show_empty()  # el texto depende del estado de la sesión

    # ----------------------------------------------------------- lista virtual
    def _max_offset(self) -> int:
        return max(0, len(self.visible) * self.row_h - self.viewport.winfo_height())

    def _show_empty(self) -> None:
        if self.entries and (self.search_text() or self.kind_filter != "All"):
            title, text = "No matches", "Try a different word, translation or filter."
        elif self.app.finishing:
            title, text = "Finishing the session…", "New words will appear here in a moment."
        elif self.app.state.is_active:
            title, text = "Listening for subtitles…", "Words appear here as they are detected.\nKeep watching."
        elif self._had_entries:
            title, text = "All caught up", "There is nothing left to review.\nStart a new session to find more words."
        else:
            title, text = ("No words yet", "Start a session and play a video with subtitles.\n"
                                            "Detected words will appear here for you to review.")
        self.empty_title.configure(text=title)
        self.empty.configure(text=text)
        self.empty_box.place(relx=0.5, rely=0.45, anchor="center")

    def render(self) -> None:
        height = self.viewport.winfo_height()
        if height <= 1:
            return
        self.offset = max(0, min(self.offset, self._max_offset()))
        needed = height // self.row_h + 2
        while len(self.rows) < needed:
            self.rows.append(_Row(self, self.viewport))
        if not self.visible:
            for row in self.rows:
                row.frame.place_forget()
            self._show_empty()
            self.scroll.set(0, 1)
            self._show_scrollbar(False)
            self._place_editor()
            return
        self.empty_box.place_forget()
        now = time.monotonic()
        first = self.offset // self.row_h
        for i, row in enumerate(self.rows):
            idx = first + i
            if idx < len(self.visible):
                entry = self.visible[idx]
                fresh = now - self._fresh.get(entry.key, -1e9) < FRESH_SECONDS
                row.show(entry, fresh, entry.key == self.selected_key)
                row.frame.place(x=0, y=idx * self.row_h - self.offset, relwidth=1, height=self.row_h - self.win.px(2))
            else:
                row.entry = None
                row.frame.place_forget()
        total = max(1, len(self.visible) * self.row_h)
        self.scroll.set(self.offset / total, min(1.0, (self.offset + height) / total))
        self._show_scrollbar(total > height)
        self._place_editor()

    def _show_scrollbar(self, needed: bool) -> None:
        """La barra sólo aparece cuando hay más filas de las que caben."""
        if needed and not self.scroll.winfo_manager():
            self.scroll.pack(side="right", fill="y", padx=(0, self.win.px(3)), pady=self.win.px(4), before=self.viewport)
        elif not needed and self.scroll.winfo_manager():
            self.scroll.pack_forget()

    def on_scroll(self, *args: str) -> None:
        if args[0] == "moveto":
            self.offset = int(float(args[1]) * len(self.visible) * self.row_h)
        elif args[0] == "scroll":
            step = self.row_h if args[2] == "units" else self.viewport.winfo_height()
            self.offset += int(args[1]) * step
        self.render()

    def on_wheel(self, event: tk.Event) -> None:
        self.offset -= int(event.delta / 120 * self.row_h)
        self.render()

    # --------------------------------------------------------------- selección
    def select(self, key: str | None, *, scroll: bool = False) -> None:
        self.selected_key = key
        idx = next((i for i, e in enumerate(self.visible) if e.key == key), None)
        if scroll and idx is not None:
            top, height = idx * self.row_h, self.viewport.winfo_height()
            if top < self.offset:
                self.offset = top
            elif top + self.row_h > self.offset + height:
                self.offset = top + self.row_h - height
            self.render()
        else:
            for row in self.rows:
                if row.entry is not None:
                    row.set_selected(row.entry.key == key)
        self.show_context(self.visible[idx] if idx is not None else None)

    def selected_entry(self) -> ReviewEntry | None:
        return next((e for e in self.visible if e.key == self.selected_key), None)

    def move_selection(self, step: int) -> None:
        if not self.visible:
            return
        keys = [e.key for e in self.visible]
        if self.selected_key in keys:
            idx = max(0, min(len(keys) - 1, keys.index(self.selected_key) + step))
        else:
            idx = 0 if step >= 0 else len(keys) - 1
        self.select(keys[idx], scroll=True)

    def _on_key(self, event: tk.Event) -> str | None:
        """Atajos del panel. No actúan mientras se escribe en un campo."""
        focus = self.win.root.focus_get()
        if not self.exists() or isinstance(focus, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return None
        if focus is not None and focus.winfo_toplevel() is not self.win.root:
            return None
        ctrl = bool(event.state & 0x4)
        key = event.keysym.lower()
        if ctrl and key == "z":
            self.undo()
        elif ctrl and key == "f":
            self._ph_entry.focus_set()
        elif ctrl:
            return None
        elif key in ("down", "j"):
            self.move_selection(1)
        elif key in ("up", "k"):
            self.move_selection(-1)
        elif key == "home" and self.visible:
            self.select(self.visible[0].key, scroll=True)
        elif key == "end" and self.visible:
            self.select(self.visible[-1].key, scroll=True)
        elif key in ("a", "return", "kp_enter"):
            entry = self.selected_entry()
            if entry and entry.status == "pending" and entry.translation:
                self.accept(entry)
        elif key in ("x", "delete"):
            entry = self.selected_entry()
            if entry and entry.status == "pending":
                self.exclude(entry)
        elif key in ("e", "f2"):
            entry = self.selected_entry()
            if entry:
                self.edit(entry)
        else:
            return None
        return "break"

    def show_context(self, entry: ReviewEntry | None) -> None:
        if entry is not None and entry.context:
            self.context.configure(text=f"“{entry.context}”", fg=MUTED)
        else:
            self.context.configure(text=CONTEXT_HINT, fg=SUBTLE)

    # ----------------------------------------------------- edición en la fila
    def edit(self, entry: ReviewEntry) -> None:
        """Corregir la traducción en la propia fila (antes de aceptarla o mientras espera a guardarse)."""
        self._edit_key = entry.key
        self.editor_var.set(entry.translation or "")
        self.select(entry.key, scroll=True)
        self._place_editor()
        self.editor.focus_set()
        self.editor.select_range(0, "end")
        self.editor.icursor("end")

    def _place_editor(self) -> None:
        if self._edit_key is None:
            self.editor.wrapper.place_forget()
            return
        idx = next((i for i, e in enumerate(self.visible) if e.key == self._edit_key), None)
        if idx is None:
            self._edit_key = None  # la fila ya no está (aceptada, filtrada…)
            self.editor.wrapper.place_forget()
            return
        p = self.win.px
        self.editor.wrapper.place(x=p(10), y=idx * self.row_h - self.offset + p(22), width=self.text_width() + p(30))
        self.editor.wrapper.lift()

    def _finish_edit(self, *, save: bool) -> str:
        key, self._edit_key = self._edit_key, None
        self.editor.wrapper.place_forget()
        value = " ".join(self.editor_var.get().split())
        if save and key is not None and value:
            entry = next((e for e in self.entries if e.key == key), None)
            if entry is not None and value != (entry.translation or ""):
                self.app.store.set_translation(self.app.lang, key, value)
        if key is not None:
            self.win.root.focus_set()
            self.reload()
        return "break"

    # ---------------------------------------------------------------- acciones
    def accept(self, entry: ReviewEntry) -> None:
        self._decide(entry, "accept")

    def exclude(self, entry: ReviewEntry) -> None:
        self._decide(entry, "exclude")

    def _decide(self, entry: ReviewEntry, action: str) -> None:
        """La decisión se aplica tras UNDO_SECONDS (o al llegar la siguiente)."""
        next_key = self._neighbour(entry.key)
        self.flush()
        if self.UNDO_SECONDS <= 0:
            self._apply(entry, action)
            self.selected_key = next_key
            self.reload()
            return
        self.pending = _Pending(entry, action)
        self.pending.timer = self.body.after(int(self.UNDO_SECONDS * 1000), self.flush)
        verb = f"Added to {self.app.settings.document.name}" if action == "accept" else "Excluded"
        self.undo_label.configure(text=f"{verb}:  {entry.expression}")
        self.undo_bar.place(relx=0.5, rely=1.0, anchor="s", y=-self.win.px(12))
        self.undo_bar.lift()
        self.selected_key = next_key
        self.refilter(keep_offset=True)

    def _neighbour(self, key: str) -> str | None:
        keys = [e.key for e in self.visible]
        if key not in keys:
            return self.selected_key
        i = keys.index(key)
        rest = keys[i + 1:] + keys[:i][::-1]
        return rest[0] if rest else None

    def _apply(self, entry: ReviewEntry, action: str) -> None:
        if action == "accept":
            try:
                self.app.accept(entry.key)
            except StoreError as exc:
                messagebox.showerror("WordLens", str(exc), parent=self.win.root)
        else:
            self.app.exclude(entry.key)

    def flush(self) -> None:
        """Aplica ya la decisión a la espera (si la hay)."""
        pending, self.pending = self.pending, None
        if pending is None:
            return
        if pending.timer is not None:
            try:
                self.body.after_cancel(pending.timer)
            except tk.TclError:
                pass
        if self.exists():
            self.undo_bar.place_forget()
        self._apply(pending.entry, pending.action)
        if self.exists():
            self.reload()

    def undo(self) -> None:
        pending, self.pending = self.pending, None
        if pending is None:
            return
        if pending.timer is not None:
            self.body.after_cancel(pending.timer)
        self.undo_bar.place_forget()
        self.selected_key = pending.entry.key
        self.refilter(keep_offset=True)
        self.select(pending.entry.key, scroll=True)

    def retry_saving(self) -> None:
        self.app.saver.wake()

    def accept_all(self) -> None:
        self.flush()
        targets = [e for e in self.visible if e.status == "pending" and e.translation]
        if not targets:
            return
        filtered = " shown" if self.search_text() or self.kind_filter != "All" else ""
        if not messagebox.askyesno(
            "WordLens", f"Add the {len(targets)}{filtered} entries to {self.app.settings.document.name}?",
            parent=self.win.root,
        ):
            return
        done = 0
        for e in targets:
            try:
                done += self.app.accept(e.key)
            except StoreError:
                log.exception("No se pudo aceptar %s", e.key)
        log.info("Aceptar todo: %d de %d", done, len(targets))
        self.reload()

    def exclude_all(self) -> None:
        self.flush()
        targets = [e for e in self.visible if e.status == "pending"]
        if not targets:
            return
        filtered = " shown" if self.search_text() or self.kind_filter != "All" else ""
        if not messagebox.askyesno(
            "WordLens",
            f"Exclude the {len(targets)}{filtered} entries? They won't be suggested again "
            "(you can restore them in Settings › Excluded vocabulary).",
            parent=self.win.root,
        ):
            return
        for e in targets:
            self.app.exclude(e.key)
        self.reload()


class ExcludedDialog:
    """Ajustes › Vocabulario excluido: buscar y restaurar entradas."""

    LIMIT = 5000

    def __init__(self, win: MainWindow, parent: tk.Misc | None = None) -> None:
        self.win = win
        self.app = win.app
        p = win.px
        top = tk.Toplevel(parent or win.root)
        self.top = top
        top.title("WordLens — Excluded vocabulary")
        top.configure(bg=BG)
        top.geometry(f"{p(460)}x{p(540)}")
        top.minsize(p(360), p(360))
        top.transient(parent or win.root)
        body = tk.Frame(top, bg=BG, padx=p(20), pady=p(18))
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Excluded vocabulary", bg=BG, fg=TEXT, font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(
            body, text="These words and expressions are never suggested. Restoring one lets it be suggested again "
                       "in future sessions; it is not added to your document.",
            bg=BG, fg=SUBTLE, font=("Segoe UI", 8), wraplength=p(410), justify="left", anchor="w",
        ).pack(fill="x", pady=(p(2), p(12)))
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.load())
        rounded_field(body, lambda m: ttk.Entry(m, textvariable=self.query, style="WL.TEntry", font=("Segoe UI", 10),
                                                width=10), scale=win.scale).wrapper.pack(fill="x", pady=(0, p(10)))

        frame = RoundedCard(body, radius=16, scale=win.scale, padx=p(8), pady=p(8))
        frame.pack(fill="both", expand=True)
        style = ttk.Style(top)
        style.configure("WL.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=p(30), font=("Segoe UI", 10), borderwidth=0, bordercolor=CARD,
                        lightcolor=CARD, darkcolor=CARD)
        style.configure("WL.Treeview.Heading", font=("Segoe UI Semibold", 8), background=CARD, foreground=SUBTLE,
                        borderwidth=0, bordercolor=CARD, lightcolor=CARD, darkcolor=CARD, relief="flat",
                        padding=(p(8), p(6)))
        style.map("WL.Treeview.Heading", background=[("active", CARD)])
        style.map("WL.Treeview", background=[("selected", CARD_HOVER)], foreground=[("selected", TEXT)])
        self.tree = ttk.Treeview(frame, columns=("expr", "date"), show="headings", style="WL.Treeview",
                                 selectmode="extended")
        self.tree.heading("expr", text="WORD OR EXPRESSION", anchor="w")
        self.tree.heading("date", text="EXCLUDED", anchor="w")
        self.tree.column("expr", width=p(250), anchor="w")
        self.tree.column("date", width=p(110), anchor="w", stretch=False)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview, style="WL.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(p(4), 0), pady=p(4))
        sb.pack(side="right", fill="y", padx=(0, p(3)), pady=p(4))
        self.empty = tk.Label(frame, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 10), justify="center")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())

        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", pady=(p(12), 0))
        self.count = tk.Label(foot, text="", bg=BG, fg=SUBTLE, font=("Segoe UI", 8))
        self.count.pack(side="left")
        RoundButton(foot, "Close", top.destroy, width=80, height=32, scale=win.scale, bg=SECONDARY,
                    hover=SECONDARY_HOVER, fg=TEXT, font=("Segoe UI", 9), radius=8).pack(side="right")
        self.restore_btn = RoundButton(foot, "Restore", self.restore, width=96, height=32, scale=win.scale,
                                       font=("Segoe UI Semibold", 9), radius=8)
        self.restore_btn.pack(side="right", padx=(0, p(8)))
        self.load()
        top.update_idletasks()
        anchor = parent if parent is not None and parent.winfo_viewable() else win.root
        if anchor.winfo_viewable():
            x = anchor.winfo_rootx() + (anchor.winfo_width() - top.winfo_width()) // 2
            y = anchor.winfo_rooty() + p(30)
        else:
            x = (top.winfo_screenwidth() - top.winfo_width()) // 2
            y = (top.winfo_screenheight() - top.winfo_height()) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        style_title_bar(top, BG)
        exclude_from_capture(top)
        top.lift()
        top.focus_force()

    def load(self) -> None:
        entries, total = self.app.store.excluded_entries(self.app.lang, self.query.get(), limit=self.LIMIT)
        self.tree.delete(*self.tree.get_children())
        for e in entries:
            when = datetime.fromtimestamp(e.excluded_at).strftime("%d %b %Y")
            self.tree.insert("", "end", iid=e.key, values=(e.expression, when))
        if not entries:
            self.empty.configure(text="No matches." if self.query.get().strip() else
                                 "Nothing excluded yet.\nWords you mark with ✕ appear here.")
            self.empty.place(relx=0.5, rely=0.4, anchor="center")
        else:
            self.empty.place_forget()
        shown = f"{len(entries)} of {total}" if total > len(entries) else f"{total}"
        self.count.configure(text=f"{shown} excluded" + (" (matching)" if self.query.get().strip() else ""))
        self._update_buttons()

    def _update_buttons(self) -> None:
        n = len(self.tree.selection())
        self.restore_btn.update_button(text=f"Restore ({n})" if n > 1 else "Restore", enabled=n > 0)

    def restore(self) -> None:
        keys = list(self.tree.selection())
        if not keys:
            return
        names = [self.tree.set(k, "expr") for k in keys]
        label = f"“{names[0]}”" if len(names) == 1 else f"{len(names)} entries"
        if not messagebox.askyesno(
            "WordLens", f"Restore {label}? It can be suggested again in future sessions (it is not added to Word).",
            parent=self.top,
        ):
            return
        restored = self.app.store.restore(self.app.lang, keys)
        log.info("Restauradas %d exclusiones: %s", restored, names)
        self.load()
