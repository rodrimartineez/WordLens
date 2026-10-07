"""Configuración local de la aplicación (config.json en la raíz del proyecto)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
LOG_DIR = PROJECT_ROOT / "logs"


# El documento vive dentro de la carpeta del proyecto. Se guarda como ruta
# relativa en config.json para que siga funcionando si la carpeta se mueve.
DEFAULT_DOCUMENT = "Vocabulario.docx"


def resolve_project_path(value: str) -> Path:
    """Rutas relativas -> relativas a la carpeta del proyecto; absolutas, tal cual."""
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else PROJECT_ROOT / path


def to_project_relative(path: Path | str) -> str:
    """Devuelve la ruta relativa al proyecto si está dentro de él; si no, absoluta."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


@dataclass
class Settings:
    # General
    app_name: str = "WordLens"
    target_level: str = "B2-C1"
    source_language: str = "English"
    target_language: str = "Spanish"

    # Atajos globales
    hotkey_start: str = "ctrl+shift+v"
    hotkey_stop: str = "ctrl+shift+x"

    # Documento Word de destino
    document_path: str = ""
    backup_count: int = 10

    # Captura de pantalla
    monitor: int = 1  # 1 = monitor principal (numeración de mss)
    capture_interval_seconds: float = 0.5
    top_band_fraction: float = 0.33  # tercio superior
    bottom_band_fraction: float = 0.33  # tercio inferior
    scan_top: bool = True
    scan_bottom: bool = True

    # OCR / detección de subtítulos
    ocr_language: str = "en-US"
    # "subtitle": aísla texto blanco/amarillo (más rápido y preciso);
    # "gray": imagen completa en grises (para subtítulos de otros colores).
    ocr_preprocess: str = "subtitle"
    require_centered: bool = True
    min_words_per_line: int = 2
    min_english_ratio: float = 0.7
    stability_frames: int = 2
    repeat_window_seconds: float = 180.0
    similarity_threshold: float = 0.85

    # LLM (Ollama local)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "maternion/ling-3.0-tiny:8b"
    ollama_timeout_seconds: float = 180.0
    ollama_keep_alive: str = "30m"
    temperature: float = 0.0
    batch_max_lines: int = 4
    batch_wait_seconds: float = 1.5
    max_queue_lines: int = 40
    prefilter_trivial_lines: bool = True

    # Filtro de vocabulario
    max_single_word_zipf: float = 4.5
    max_expression_words: int = 6

    # Interfaz
    notifications: bool = True

    def __post_init__(self) -> None:
        if not self.document_path:
            self.document_path = DEFAULT_DOCUMENT

    @property
    def document(self) -> Path:
        return resolve_project_path(self.document_path)


# Valores de versiones anteriores que se actualizan al cargar. El proyecto se
# llamaba "VocabLens": estas referencias al nombre antiguo son intencionadas.
LEGACY_APP_NAME = "VocabLens"
_LEGACY_BAND_DEFAULTS = {"top_band_fraction": 0.25, "bottom_band_fraction": 0.30}


def _migrate(data: dict) -> bool:
    """Actualiza un config.json antiguo. Devuelve True si ha cambiado algo."""
    changed = False
    if data.get("app_name") == LEGACY_APP_NAME:
        data["app_name"] = Settings.app_name
        changed = True
    doc = data.get("document_path")
    if isinstance(doc, str) and doc:
        old_default = Path.home() / "Documents" / LEGACY_APP_NAME / "Vocabulario.docx"
        old_project = PROJECT_ROOT.parent / LEGACY_APP_NAME  # carpeta antes del renombrado
        path = Path(os.path.expandvars(os.path.expanduser(doc)))
        if path == old_default:
            new = DEFAULT_DOCUMENT
        elif path.is_absolute() and path.is_relative_to(old_project) and old_project != PROJECT_ROOT:
            new = str(path.relative_to(old_project))
        elif path.is_absolute():
            new = to_project_relative(path)
        else:
            new = doc
        if new != doc:
            data["document_path"] = new
            changed = True
    for key, legacy in _LEGACY_BAND_DEFAULTS.items():
        if data.get(key) == legacy:
            data[key] = getattr(Settings, key)
            changed = True
    return changed


def load_settings(path: Path = DEFAULT_CONFIG_PATH) -> Settings:
    """Carga config.json. Si no existe, lo crea con los valores por defecto.

    Las claves desconocidas se ignoran y las ausentes toman el valor por defecto,
    así que un config.json antiguo sigue funcionando tras actualizar.
    """
    defaults = Settings()
    if not path.exists():
        save_settings(defaults, path)
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("No se pudo leer %s (%s); se usan valores por defecto", path, exc)
        return defaults
    migrated = _migrate(data)

    known = {f.name: f for f in fields(Settings)}
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            log.warning("Clave desconocida en config.json ignorada: %s", key)
            continue
        expected = type(getattr(defaults, key))
        try:
            kwargs[key] = expected(value) if expected is not bool else bool(value)
        except (TypeError, ValueError):
            log.warning("Valor inválido para %s: %r; se usa el valor por defecto", key, value)
    settings = Settings(**kwargs)
    # Añade al archivo las claves nuevas que no tuviera.
    if migrated or set(known) - set(data):
        save_settings(settings, path)
    return settings


def save_settings(settings: Settings, path: Path = DEFAULT_CONFIG_PATH) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
