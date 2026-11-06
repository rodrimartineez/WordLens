"""Migración de datos de WordLens 1.x a la revisión de vocabulario (1.2).

En 1.x el documento Word era la única memoria: todo lo que el modelo
encontraba se escribía directamente. Desde 1.2 hay un registro propio
(vocabulary.db). Al arrancar por primera vez:

1. Copia de seguridad de config.json y de los pendientes antiguos en
   backups/migration_<fecha>/ (antes de tocar nada).
2. Las entradas que ya están en el documento se registran como aceptadas:
   no se volverán a proponer aunque luego borres alguna línea en Word.
3. Los pendientes que 1.x guardó porque Word tenía el documento bloqueado
   (carpeta pending/) pasan a ser aceptadas sin guardar: el escritor las
   llevará al documento. El archivo original se renombra a .imported, no se borra.

Cada paso se marca en la tabla meta al terminar bien; si algo falla (por
ejemplo, el documento abierto en Word), se reintenta en el siguiente arranque.
Nada de esto modifica el documento ni la configuración.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from wordlens.config.paths import BACKUP_DIR, CONFIG_PATH, PENDING_DIR
from wordlens.config.settings import Settings
from wordlens.document.word_writer import DocumentError, WordWriter
from wordlens.vocabulary.normalization import fold
from wordlens.vocabulary.store import VocabularyStore

log = logging.getLogger(__name__)

SEPARATORS = (" — ", " – ", " - ")


def backup_legacy_data(store: VocabularyStore, *, config_path: Path = CONFIG_PATH,
                       pending_dir: Path = PENDING_DIR, backup_dir: Path = BACKUP_DIR) -> Path | None:
    if store.get_meta("migration_backup"):
        return None
    target = backup_dir / f"migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        shutil.copy2(config_path, target / config_path.name)
    if pending_dir.exists():
        shutil.copytree(pending_dir, target / "pending", dirs_exist_ok=True)
    store.set_meta("migration_backup", str(target))
    log.info("Copia de seguridad previa a la migración: %s", target)
    return target


def import_document(store: VocabularyStore, settings: Settings) -> int | None:
    """Registra como aceptadas las entradas del documento actual (una vez por documento)."""
    if not settings.setup_complete:
        return None
    doc = settings.document
    flag = f"imported_document:{str(doc).lower()}"
    if store.get_meta(flag) or not doc.exists():
        return None
    try:
        expressions = WordWriter(doc, lang=settings.source_code).read_expressions()
    except (DocumentError, OSError) as exc:
        log.warning("No se pudo leer %s para la migración (%s); se reintentará", doc, exc)
        return None
    count = store.import_accepted(
        settings.source_code, [(e, "") for e in expressions], origin="document", written=True, document=str(doc)
    )
    store.set_meta(flag, datetime.now().isoformat(timespec="seconds"))
    log.info("Migración: %d entradas de %s registradas como aceptadas", count, doc.name)
    return count


def import_legacy_pending(store: VocabularyStore, settings: Settings, pending_dir: Path = PENDING_DIR) -> int:
    """Pendientes de 1.x ("expresión — traducción" por línea) -> aceptadas sin guardar."""
    if not pending_dir.exists():
        return 0
    total = 0
    for path in sorted(pending_dir.glob("*.pending.txt")):
        entries = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                for sep in SEPARATORS:
                    expr, found, trans = fold(line).partition(sep)
                    if found and expr.strip() and trans.strip():
                        entries.append((expr.strip(), trans.strip()))
                        break
        except OSError as exc:
            log.warning("No se pudo leer %s: %s", path, exc)
            continue
        total += store.import_accepted(settings.source_code, entries, origin="legacy-pending", written=False)
        path.replace(path.with_name(path.name + ".imported"))
        log.info("Migración: %d pendientes importados de %s", len(entries), path.name)
    return total


def run(store: VocabularyStore, settings: Settings) -> None:
    """Pasos idempotentes: se pueden ejecutar en cada arranque."""
    try:
        backup_legacy_data(store)
        import_document(store, settings)
        import_legacy_pending(store, settings)
    except Exception:
        # Un fallo aquí no impide usar la app; los datos originales siguen intactos.
        log.exception("Error durante la migración de datos; se reintentará en el próximo arranque")
