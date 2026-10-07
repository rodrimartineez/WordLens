"""Icono de WordLens: una lupa sobre líneas de subtítulo, en un cuadrado redondeado.

Se dibuja por código (sin archivos de diseño) para poder regenerarlo:
    python tools/make_icon.py   ->  assets/wordlens.ico, assets/wordlens.png
En tamaños pequeños (<= 24 px) se simplifica para que siga siendo legible.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

BRAND_TOP = (99, 102, 241)  # índigo
BRAND_BOTTOM = (20, 184, 166)  # verde azulado
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _gradient(size: int) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(BRAND_TOP, BRAND_BOTTOM)))
    return grad.resize((size, size))


def render_icon(size: int) -> Image.Image:
    """Icono cuadrado RGBA de `size` px (se dibuja a 4x y se reduce: bordes suaves)."""
    s = size * 4
    small = size <= 24
    mask = Image.new("L", (s, s), 0)
    radius = int(s * (0.2 if small else 0.22))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(_gradient(s), (0, 0), mask)
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)

    # Lupa: aro + mango.
    cx, cy, r = s * 0.44, s * 0.42, s * (0.25 if small else 0.235)
    ring = s * (0.085 if small else 0.06)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=white, width=int(ring))
    hx, hy = cx + r * 0.72, cy + r * 0.72
    d.line((hx, hy, s * 0.80, s * 0.80), fill=white, width=int(ring * 1.35))
    d.ellipse((s * 0.80 - ring * 0.67, s * 0.80 - ring * 0.67, s * 0.80 + ring * 0.67, s * 0.80 + ring * 0.67), fill=white)

    # Dentro de la lupa: dos líneas de "subtítulo" (sólo en tamaños grandes).
    if not small:
        lw = int(s * 0.04)
        for dy, half in ((-0.055, 0.11), (0.055, 0.07)):
            y = cy + s * dy
            d.line((cx - s * half, y, cx + s * half, y), fill=white, width=lw)
            for x in (cx - s * half, cx + s * half):
                d.ellipse((x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2), fill=white)
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
