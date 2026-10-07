"""Captura periódica de las franjas superior e inferior de la pantalla.

Se usa mss (BitBlt de GDI vía ctypes): ~5-15 ms por franja, sin dependencias
nativas. Sólo se capturan las franjas donde suelen aparecer subtítulos y las
imágenes nunca se escriben en disco: viven en memoria y se descartan tras el OCR.
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass

import mss
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


def enable_dpi_awareness() -> None:
    """Sin esto, en pantallas con escalado (125 %, 150 %) se captura a menor resolución."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


@dataclass(frozen=True)
class Band:
    name: str  # "top" | "bottom"
    left: int
    top: int
    width: int
    height: int


@dataclass
class BandImage:
    band: Band
    image: Image.Image


def compute_bands(
    monitor: dict, top_fraction: float, bottom_fraction: float, scan_top: bool, scan_bottom: bool
) -> list[Band]:
    width, height = monitor["width"], monitor["height"]
    bands: list[Band] = []
    if scan_top and top_fraction > 0:
        h = max(1, int(height * min(top_fraction, 0.5)))
        bands.append(Band("top", monitor["left"], monitor["top"], width, h))
    if scan_bottom and bottom_fraction > 0:
        h = max(1, int(height * min(bottom_fraction, 0.5)))
        bands.append(Band("bottom", monitor["left"], monitor["top"] + height - h, width, h))
    return bands


class ScreenCapturer:
    """Debe crearse y usarse en el mismo hilo (requisito de mss en Windows)."""

    def __init__(
        self,
        monitor_index: int = 1,
        top_fraction: float = 0.25,
        bottom_fraction: float = 0.30,
        scan_top: bool = True,
        scan_bottom: bool = True,
    ) -> None:
        self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        monitors = self._sct.monitors
        if not 1 <= monitor_index < len(monitors):
            log.warning("Monitor %s no existe; se usa el principal", monitor_index)
            monitor_index = 1
        self.monitor = monitors[monitor_index]
        self.bands = compute_bands(self.monitor, top_fraction, bottom_fraction, scan_top, scan_bottom)

    def grab(self) -> list[BandImage]:
        out = []
        for band in self.bands:
            shot = self._sct.grab(
                {"left": band.left, "top": band.top, "width": band.width, "height": band.height}
            )
            # BGRA -> RGB sin copias intermedias innecesarias
            image = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX")
            out.append(BandImage(band, image))
        return out

    def close(self) -> None:
        self._sct.close()


def subtitle_mask(image: Image.Image) -> Image.Image:
    """Aísla texto blanco/amarillo brillante (los colores habituales de subtítulos).

    Devuelve texto negro sobre fondo blanco. Elimina el fondo del vídeo, lo que
    mejora la precisión del OCR y lo hace varias veces más rápido.
    """
    a = np.asarray(image.convert("RGB"), dtype=np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mask = (r > 175) & (g > 175) & ((b > 175) | ((r - b) > 60))
    return Image.fromarray(np.where(mask, 0, 255).astype(np.uint8))


def prepare_for_ocr(image: Image.Image, mode: str = "subtitle", max_width: int = 1440) -> tuple[Image.Image, float]:
    """Escala (texto de subtítulo a ~30-40 px) y preprocesa. Devuelve (imagen, escala)."""
    scale = 1.0
    if image.width > max_width:
        scale = max_width / image.width
        image = image.resize((max_width, max(1, int(image.height * scale))), Image.BILINEAR)
    if mode == "subtitle":
        image = subtitle_mask(image)
    elif mode == "gray":
        image = image.convert("L")
    return image, scale


class ChangeDetector:
    """Detecta si una franja ha cambiado respecto a la captura anterior.

    Permite saltarse el OCR cuando la imagen está quieta (vídeo en pausa, créditos
    estáticos, escritorio), ahorrando CPU durante sesiones largas.
    """

    def __init__(self, changed_fraction: float = 0.002, pixel_delta: int = 40) -> None:
        self.changed_fraction = changed_fraction
        self.pixel_delta = pixel_delta
        self._previous: dict[str, np.ndarray] = {}

    def changed(self, name: str, image: Image.Image) -> bool:
        # Miniatura 1/4: conserva la forma de las letras (una media global no
        # distinguiría dos frases distintas en la misma posición).
        w, h = image.size
        small = np.asarray(
            image.convert("L").resize((max(1, w // 4), max(1, h // 4)), Image.BILINEAR),
            dtype=np.int16,
        )
        prev = self._previous.get(name)
        self._previous[name] = small
        if prev is None or prev.shape != small.shape:
            return True
        diff = np.abs(small - prev) > self.pixel_delta
        return float(diff.mean()) >= self.changed_fraction

    def reset(self) -> None:
        self._previous.clear()
