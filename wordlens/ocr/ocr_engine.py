"""OCR local mediante el motor nativo de Windows (Windows.Media.Ocr).

Motivos de la elección (ver README, "Decisiones técnicas"):
- viene con Windows 10/11, no hay que instalar binarios ni modelos;
- rápido en CPU (decenas de ms por franja), sin GPU;
- buena precisión con texto impreso grande y limpio, como un subtítulo;
- 100 % local.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine as _WinOcrEngine
from winrt.windows.storage.streams import DataWriter

log = logging.getLogger(__name__)


class OcrUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrLine:
    text: str
    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def bottom(self) -> float:
        return self.y + self.height


def available_languages() -> list[str]:
    return [lang.language_tag for lang in _WinOcrEngine.available_recognizer_languages]


class WindowsOcr:
    """Envoltorio síncrono y thread-safe sobre la API asíncrona de WinRT."""

    def __init__(self, language: str = "en-US") -> None:
        lang = Language(language)
        if not _WinOcrEngine.is_language_supported(lang):
            raise OcrUnavailableError(
                f"El paquete OCR de Windows para '{language}' no está instalado. "
                f"Disponibles: {available_languages()}. Ver README (Solución de problemas)."
            )
        engine = _WinOcrEngine.try_create_from_language(lang)
        if engine is None:
            raise OcrUnavailableError(f"No se pudo crear el motor OCR para {language}")
        self._engine = engine
        self.language = language
        self.max_dimension = int(_WinOcrEngine.max_image_dimension)
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if not self._loop.is_closed():
                self._loop.close()

    @staticmethod
    def _prepare(image: Image.Image, max_dim: int) -> tuple[Image.Image, float]:
        """Ajusta el tamaño: el motor rinde mejor con texto de ~20-60 px de alto."""
        scale = 1.0
        w, h = image.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
        elif h < 120:  # franjas muy bajas: ampliar ayuda al reconocimiento
            scale = min(2.0, max_dim / max(w, h))
        if scale != 1.0:
            image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        return image, scale

    def _to_bitmap(self, image: Image.Image) -> SoftwareBitmap:
        rgba = np.asarray(image.convert("RGBA"))
        bgra = rgba[..., [2, 1, 0, 3]].copy()
        writer = DataWriter()
        writer.write_bytes(bgra.tobytes())
        buffer = writer.detach_buffer()
        h, w = bgra.shape[:2]
        return SoftwareBitmap.create_copy_from_buffer(buffer, BitmapPixelFormat.BGRA8, w, h)

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        """Devuelve las líneas de texto con su caja en coordenadas de la imagen original."""
        prepared, scale = self._prepare(image, self.max_dimension)
        bitmap = self._to_bitmap(prepared)
        with self._lock:
            result = self._loop.run_until_complete(self._engine.recognize_async(bitmap))
        lines: list[OcrLine] = []
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            xs = [w.bounding_rect.x for w in words]
            ys = [w.bounding_rect.y for w in words]
            x2 = [w.bounding_rect.x + w.bounding_rect.width for w in words]
            y2 = [w.bounding_rect.y + w.bounding_rect.height for w in words]
            lines.append(
                OcrLine(
                    text=line.text,
                    x=min(xs) / scale,
                    y=min(ys) / scale,
                    width=(max(x2) - min(xs)) / scale,
                    height=(max(y2) - min(ys)) / scale,
                )
            )
        return lines
