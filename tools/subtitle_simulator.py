"""Simula subtítulos en pantalla para probar la cadena completa sin una serie real.

Abre una ventana a pantalla completa con un fondo que cambia (como un vídeo) y
va mostrando subtítulos abajo y, de vez en cuando, arriba. Deja WordLens
capturando y observa cómo se rellena el documento.

  .venv\\Scripts\\python.exe tools\\subtitle_simulator.py            (Esc para salir)
  .venv\\Scripts\\python.exe tools\\subtitle_simulator.py --seconds 3 --loops 2
  .venv\\Scripts\\python.exe tools\\subtitle_simulator.py --screen 2   (pantalla 2 de WordLens)
"""

from __future__ import annotations

import argparse
import random
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_SUBTITLES = [
    ("bottom", "I couldn't figure out what he was getting at."),
    ("bottom", "I went to the supermarket yesterday."),
    ("bottom", "You shouldn't take it for granted."),
    ("top", "He was completely overwhelmed by the situation."),
    ("bottom", "Where are you going?"),
    ("bottom", "I couldn't make out what she was saying."),
    ("bottom", "Let's call it a day, we're running out of time."),
    ("top", "I won't put up with this nonsense any longer."),
    ("bottom", "The evidence is flimsy at best."),
    ("bottom", "Frankly, it's a long shot."),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3.0, help="Duración de cada subtítulo")
    parser.add_argument("--gap", type=float, default=0.6, help="Pausa sin subtítulo entre líneas")
    parser.add_argument("--loops", type=int, default=1, help="Veces que se repite la lista (0 = infinito)")
    parser.add_argument("--file", help="Archivo con subtítulos (una línea cada uno; prefijo 'top:' para arriba)")
    parser.add_argument("--screen", type=int, default=1, help="Pantalla (numeración de WordLens: 1 = principal)")
    args = parser.parse_args()

    from wordlens.capture import monitors
    from wordlens.capture.screen_capture import enable_dpi_awareness

    enable_dpi_awareness()
    screen = next((m for m in monitors.list_monitors() if m.number == args.screen), None)
    if screen is None:
        parser.error(f"No hay pantalla {args.screen}")

    subtitles = DEFAULT_SUBTITLES
    if args.file:
        subtitles = []
        with open(args.file, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    pos = "top" if raw.lower().startswith("top:") else "bottom"
                    subtitles.append((pos, raw.split(":", 1)[1].strip() if pos == "top" else raw))

    root = tk.Tk()
    root.title("WordLens - simulador de subtítulos")
    root.geometry(f"{screen.width}x{screen.height}+{screen.left}+{screen.top}")
    root.update_idletasks()
    root.attributes("-fullscreen", True)
    root.configure(cursor="none")
    root.bind("<Escape>", lambda _e: root.destroy())
    w, h = screen.width, screen.height
    canvas = tk.Canvas(root, width=w, height=h, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    font_size = max(20, h // 24)
    font = ("Arial", font_size, "bold")
    state = {"i": 0, "loop": 0, "hue": random.randint(0, 360)}

    def background() -> None:
        # Fondo en movimiento con bloques de color, para que no sea un caso trivial.
        state["hue"] = (state["hue"] + 7) % 360
        canvas.delete("bg")
        for k in range(12):
            r = (state["hue"] * 3 + k * 40) % 200
            g = (state["hue"] * 5 + k * 25) % 160
            b = (state["hue"] * 7 + k * 60) % 220
            x0 = (k * w // 12 + state["hue"] * 4) % w
            canvas.create_rectangle(x0, 0, x0 + w // 6, h, fill=f"#{r:02x}{g:02x}{b:02x}", outline="", tags="bg")
        canvas.tag_lower("bg")
        root.after(200, background)

    def draw_subtitle(pos: str, text: str) -> None:
        canvas.delete("sub")
        y = int(h * 0.12) if pos == "top" else int(h * 0.88)
        for dx in (-3, -2, 0, 2, 3):  # contorno negro típico de subtítulos
            for dy in (-3, 0, 3):
                canvas.create_text(w // 2 + dx, y + dy, text=text, font=font, fill="black", tags="sub")
        canvas.create_text(w // 2, y, text=text, font=font, fill="white", tags="sub")

    def next_subtitle() -> None:
        if state["i"] >= len(subtitles):
            state["i"] = 0
            state["loop"] += 1
            if args.loops and state["loop"] >= args.loops:
                root.after(1500, root.destroy)
                canvas.delete("sub")
                return
        pos, text = subtitles[state["i"]]
        state["i"] += 1
        draw_subtitle(pos, text)
        root.after(int(args.seconds * 1000), clear)

    def clear() -> None:
        canvas.delete("sub")
        root.after(int(args.gap * 1000), next_subtitle)

    background()
    root.after(1500, next_subtitle)
    root.mainloop()


if __name__ == "__main__":
    main()
