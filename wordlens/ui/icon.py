"""Icono de WordLens: una lupa con la W dentro y un mango de pie cuya base son dos L espejadas.

Simétrico, de trazo blanco uniforme y redondeado sobre un degradado radial (verde azulado en el
centro, índigo hacia los bordes). Se dibuja por código para poder regenerarlo:
    python tools/make_icon.py   ->  assets/wordlens.ico, assets/wordlens.png
En tamaños pequeños (<= 24 px) los trazos son más gruesos para que siga siendo legible.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

# Paradas del degradado radial (posición 0..1 desde el centro, color).
GRADIENT_STOPS = ((0.0, (45, 212, 191)), (0.45, (91, 108, 240)), (1.0, (46, 42, 143)))
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _radial_color(t: float) -> tuple[int, int, int]:
    t = min(1.0, t)
    for (t0, c0), (t1, c1) in zip(GRADIENT_STOPS, GRADIENT_STOPS[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))
    return GRADIENT_STOPS[-1][1]


def _gradient(size: int) -> Image.Image:
    """Degradado radial centrado algo por encima del medio (se calcula pequeño y se escala: es suave)."""
    n = min(size, 128)
    grad = Image.new("RGB", (n, n))
    cx, cy, reach = n * 0.5, n * 0.45, n * 0.7
    for y in range(n):
        for x in range(n):
            grad.putpixel((x, y), _radial_color(((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5 / reach))
    return grad.resize((size, size), Image.BICUBIC)


def _stroke(d: ImageDraw.ImageDraw, points, width: float) -> None:
    """Polilínea blanca con uniones y extremos redondeados."""
    white = (255, 255, 255, 255)
    d.line(points, fill=white, width=round(width), joint="curve")
    for x, y in points:
        d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=white)


def render_icon(size: int) -> Image.Image:
    """Icono cuadrado RGBA de `size` px (se dibuja a 4x y se reduce: bordes suaves)."""
    s = size * 4
    small = size <= 24
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * (0.2 if small else 0.24)), fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(_gradient(s), (0, 0), mask)
    d = ImageDraw.Draw(img)
    u = s / 100  # rejilla de 100 unidades
    ring, letter = (9, 8) if small else (7, 6)

    # Lente.
    cx, cy, r = 50 * u, 42 * u, 24 * u
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 255), width=round(ring * u))
    # W dentro de la lente.
    _stroke(d, [(x * u, y * u) for x, y in ((39, 35), (44.5, 50), (50, 41), (55.5, 50), (61, 35))], letter * u)
    # Mango de pie y base: dos L espejadas.
    _stroke(d, [(50 * u, 66 * u), (50 * u, 80 * u)], ring * u)
    _stroke(d, [(40 * u, 80 * u), (60 * u, 80 * u)], ring * u)
    return img.resize((size, size), Image.LANCZOS)


def with_status_dot(base: Image.Image, color: tuple[int, int, int] | None) -> Image.Image:
    """Icono de bandeja: el icono de la app con un punto de estado abajo a la derecha."""
    img = base.copy()
    if color is None:
        return img
    size = img.width
    d = ImageDraw.Draw(img)
    r = size * 0.17
    cx = cy = size - r - 1
    d.ellipse((cx - r - size * 0.05, cy - r - size * 0.05, cx + r + size * 0.05, cy + r + size * 0.05), fill=(255, 255, 255, 255))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (255,))
    return img


def save_icon_files(ico_path, png_path) -> None:
    images = [render_icon(sz) for sz in ICO_SIZES]
    images[-1].save(ico_path, format="ICO", sizes=[(sz, sz) for sz in ICO_SIZES], append_images=images[:-1])
    images[-1].save(png_path, format="PNG")
