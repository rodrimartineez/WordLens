"""Configuración del usuario (config.json en la carpeta de datos, ver paths.py)."""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .paths import CONFIG_PATH, IS_FROZEN, PROJECT_ROOT, default_document, documents_dir

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = CONFIG_PATH

# Idiomas soportados de punta a punta (OCR, filtros, prompts): nombre -> código.
LANGUAGES = {"English": "en", "Spanish": "es"}
OCR_TAGS = {"en": "en-US", "es": "es-ES"}


def resolve_document_path(value: str) -> Path:
    """Ruta del documento. Las relativas se interpretan dentro de "Documentos"."""
    if not value:
        return default_document()
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else documents_dir() / path


@dataclass
class Settings:
    # General
    app_name: str = "WordLens"
    source_language: str = "English"
    target_language: str = "Spanish"
    # False hasta completar la configuración inicial (documento + idiomas).
    setup_complete: bool = False
    # Apariencia de la interfaz: "dark" | "light".
    theme: str = "dark"

    # Atajos globales
    hotkey_start: str = "ctrl+shift+v"
    hotkey_stop: str = "ctrl+shift+x"

    # Documento Word de destino (ruta absoluta elegida por el usuario)
    document_path: str = ""
    backup_count: int = 10

    # Captura de pantalla
    # Pantalla donde se buscan los subtítulos (ver capture/monitors.py).
    # capture_monitor_id identifica el monitor físico y es lo que manda;
    # capture_monitor es el número que ve el usuario (1 = principal).
    capture_monitor: int = 1
    capture_monitor_id: str = ""
    capture_interval_seconds: float = 0.5
    top_band_fraction: float = 0.33  # tercio superior
    bottom_band_fraction: float = 0.33  # tercio inferior
    scan_top: bool = True
    scan_bottom: bool = True

    # OCR / detección de subtítulos
    # "auto": según source_language (en-US / es-ES). Se puede forzar una etiqueta.
    ocr_language: str = "auto"
    # "subtitle": aísla texto blanco/amarillo (más rápido y preciso);
    # "gray": imagen completa en grises (para subtítulos de otros colores).
    ocr_preprocess: str = "subtitle"
    require_centered: bool = True
    min_words_per_line: int = 2
    # Proporción mínima de palabras del idioma de origen (nombre histórico).
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
    batch_max_lines: int = 1  # subtítulos por llamada al modelo de extracción (1 = más fiable)
    # Marca en cada línea las combinaciones verbo + partícula para que el modelo
    # no pase por alto un phrasal verb (ver vocabulary/candidates.py).
    candidate_hints: bool = True
    max_expression_words: int = 6

    # Traducción (ver translation/translator.py). Modelo local de Ollama elegido
    # por calidad tras comparar alternativas gratuitas; si no está instalado se
    # traduce con ollama_model.
    translation_model: str = "gemma3:4b"
    translation_keep_alive: str = "10m"

    # Interfaz
    notifications: bool = True

    def __post_init__(self) -> None:
        if self.source_language not in LANGUAGES:
            self.source_language = "English"
        if self.target_language not in LANGUAGES:
            self.target_language = "Spanish"
        if self.source_language == self.target_language:
            self.target_language = "Spanish" if self.source_language == "English" else "English"

    @property
    def document(self) -> Path:
        return resolve_document_path(self.document_path)

    @property
    def source_code(self) -> str:
        return LANGUAGES[self.source_language]

    @property
    def target_code(self) -> str:
        return LANGUAGES[self.target_language]

    @property
    def ocr_tag(self) -> str:
        tag = self.ocr_language
        if not tag or tag == "auto" or not tag.lower().startswith(self.source_code):
            return OCR_TAGS[self.source_code]
        return tag


# Valores de versiones anteriores que se actualizan al cargar. El proyecto se
# llamaba "VocabLens": estas referencias al nombre antiguo son intencionadas.
LEGACY_APP_NAME = "VocabLens"
_LEGACY_BAND_DEFAULTS = {"top_band_fraction": 0.25, "bottom_band_fraction": 0.30}
# config.json de cuando los datos vivían en la carpeta del proyecto.
LEGACY_PROJECT_CONFIG = PROJECT_ROOT / "config.json"


def _migrate(data: dict) -> bool:
    """Actualiza un config.json antiguo. Devuelve True si ha cambiado algo."""
    changed = False
    if data.get("app_name") == LEGACY_APP_NAME:
        data["app_name"] = Settings.app_name
        changed = True
    for key, legacy in _LEGACY_BAND_DEFAULTS.items():
        if data.get(key) == legacy:
            data[key] = getattr(Settings, key)
            changed = True
    if data.get("ocr_language") == "en-US":  # antiguo valor fijo -> automático
        data["ocr_language"] = "auto"
        changed = True
    if "translation_model" not in data and data.get("batch_max_lines") == 4:
        # 1.x: 4 líneas por llamada. La extracción exhaustiva es más fiable de una en una.
        data["batch_max_lines"] = Settings.batch_max_lines
        changed = True
    if "monitor" in data:  # 1.0: índice de mss (1 = primer monitor) -> capture_monitor
        legacy = data.pop("monitor")
        if isinstance(legacy, int) and legacy > 1 and "capture_monitor" not in data:
            data["capture_monitor"] = legacy
        changed = True
    return changed


def _import_legacy_project_config(path: Path) -> dict | None:
    """Desarrollo: reutiliza el config.json que antes vivía junto al código.

    Las rutas relativas del documento eran relativas al proyecto; se guardan ya
    como absolutas. La configuración importada se da por completada.
    """
    if (
        IS_FROZEN
        or os.environ.get("WORDLENS_DATA_DIR")
        or path != DEFAULT_CONFIG_PATH
        or not LEGACY_PROJECT_CONFIG.exists()
    ):
        return None
    try:
        data = json.loads(LEGACY_PROJECT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    doc = data.get("document_path")
    if isinstance(doc, str) and doc and not Path(doc).is_absolute():
        data["document_path"] = str(PROJECT_ROOT / doc)
    data.setdefault("setup_complete", bool(doc))
    log.info("Importada la configuración antigua de %s", LEGACY_PROJECT_CONFIG)
    return data


def load_settings(path: Path = DEFAULT_CONFIG_PATH) -> Settings:
    """Carga config.json. Si no existe, devuelve valores por defecto (primer uso).

    Las claves desconocidas se ignoran y las ausentes toman el valor por defecto,
    así que un config.json antiguo sigue funcionando tras actualizar.
    """
    defaults = Settings()
    data: dict | None = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("No se pudo leer %s (%s); se usan valores por defecto", path, exc)
            return defaults
    else:
        data = _import_legacy_project_config(path)
        if data is None:
            return defaults
        _migrate(data)
        settings = _from_dict(data, defaults)
        save_settings(settings, path)
        return settings
    if not isinstance(data, dict):
        return defaults
    migrated = _migrate(data)
    settings = _from_dict(data, defaults)
    # Añade al archivo las claves nuevas que no tuviera.
    known = {f.name for f in fields(Settings)}
    if migrated or known - set(data) or set(data) - known:
        _backup_config(path)
        save_settings(settings, path)
    return settings


def _backup_config(path: Path) -> None:
    """Antes de reescribir un config.json existente al actualizar, se guarda una copia."""
    from datetime import datetime

    from .paths import BACKUP_DIR

    try:
        target_dir = BACKUP_DIR if path == DEFAULT_CONFIG_PATH else path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, target_dir / f"config_{stamp}.json")
    except OSError as exc:
        log.warning("No se pudo copiar %s antes de actualizarlo: %s", path, exc)


def _from_dict(data: dict, defaults: Settings) -> Settings:
    known = {f.name for f in fields(Settings)}
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
    return Settings(**kwargs)


def save_settings(settings: Settings, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
