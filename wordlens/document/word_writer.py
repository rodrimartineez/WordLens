"""Lectura y escritura segura del documento Word permanente.

Formato de cada entrada (un párrafo):  **expresión** — traducción

Estrategia de seguridad:
- Nunca se crea un documento nuevo si ya existe uno en la ruta.
- Si el archivo existente no se puede abrir como .docx, no se toca (error).
- Al iniciar cada sesión se guarda una copia rotativa en la carpeta de copias
  (la de datos de WordLens en la app; junto al documento si no se indica).
- Cada guardado se hace en un archivo temporal del mismo directorio y luego se
  sustituye atómicamente con os.replace: un corte a mitad nunca deja el
  documento a medias.
- Antes de cada escritura se vuelve a leer el documento del disco, así que se
  respetan los cambios que hagas a mano mientras la herramienta está activa.
- Si Word tiene el archivo bloqueado, las entradas se quedan en cola y se
  reintentan en la siguiente escritura.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import docx
from docx.document import Document as DocxDocument

from wordlens.vocabulary.models import VocabularyItem
from wordlens.vocabulary.normalization import dedup_key

log = logging.getLogger(__name__)

SEPARATOR = " — "
_SEPARATORS = (" — ", " – ", " - ", "—", "–")
BACKUP_DIR_NAME = ".wordlens_backups"


class DocumentError(Exception):
    pass


class DocumentLockedError(DocumentError):
    """El documento está abierto/bloqueado por otro programa (normalmente Word)."""


def parse_entry(paragraph_text: str, bold_text: str = "") -> str | None:
    """Obtiene la expresión inglesa de un párrafo del documento."""
    text = paragraph_text.strip()
    if not text:
        return None
    for sep in _SEPARATORS:
        if sep in text:
            left = text.split(sep, 1)[0].strip()
            if left:
                return left
    bold = bold_text.strip()
    return bold or None


class WordWriter:
    def __init__(
        self,
        path: Path | str,
        backup_count: int = 10,
        *,
        backup_dir: Path | None = None,
        lang: str = "en",
    ) -> None:
        self.path = Path(path)
        self.backup_count = backup_count
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent / BACKUP_DIR_NAME
        self.lang = lang
        self._lock = threading.Lock()
        self._pending: list[VocabularyItem] = []

    # ---------------------------------------------------------------- lectura
    def _load(self) -> DocxDocument:
        if not self.path.exists():
            return docx.Document()
        # is_zipfile() devuelve False también si el archivo está bloqueado en
        # exclusiva; hay que distinguirlo de un archivo corrupto.
        try:
            with open(self.path, "rb"):
                pass
        except PermissionError as exc:
            raise DocumentLockedError(f"{self.path.name} is locked by another program (is it open in Word?)") from exc
        if not zipfile.is_zipfile(self.path):
            raise DocumentError(
                f"{self.path.name} exists but is not a valid .docx file; it will not be modified."
            )
        try:
            return docx.Document(str(self.path))
        except PermissionError as exc:
            raise DocumentLockedError(str(exc)) from exc
        except Exception as exc:  # docx lanza varios tipos según la corrupción
            raise DocumentError(f"Could not open {self.path.name}: {exc}") from exc

    @staticmethod
    def _expressions_in(document: DocxDocument) -> list[str]:
        found = []
        for p in document.paragraphs:
            bold = "".join(r.text for r in p.runs if r.bold)
            expr = parse_entry(p.text, bold)
            if expr:
                found.append(expr)
        return found

    def read_expressions(self) -> list[str]:
        with self._lock:
            return self._expressions_in(self._load())

    # ------------------------------------------------------------- escritura
    def ensure_exists(self) -> bool:
        """Crea el documento si no existe. Devuelve True si lo ha creado."""
        with self._lock:
            if self.path.exists():
                self._load()  # valida que sea un docx legible
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._save(docx.Document())
            log.info("Documento creado: %s", self.path)
            return True

    def check_writable(self) -> None:
        """Comprueba que se puede guardar en la carpeta del documento.

        Leer un .docx existente no basta: carpetas protegidas (p. ej. Documentos con
        el acceso controlado de Windows) dejan leer pero no escribir. Se crea y se
        borra un archivo temporal, igual que hace cada guardado.
        """
        probe = self.path.with_name(f"~wordlens_{os.getpid()}_probe.tmp")
        try:
            probe.write_bytes(b"")
        finally:
            probe.unlink(missing_ok=True)

    def backup(self) -> Path | None:
        """Copia de seguridad rotativa del documento actual."""
        with self._lock:
            if not self.path.exists():
                return None
            backup_dir = self.backup_dir
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = backup_dir / f"{self.path.stem}_{stamp}{self.path.suffix}"
            shutil.copy2(self.path, target)
            backups = sorted(backup_dir.glob(f"{self.path.stem}_*{self.path.suffix}"))
            for old in backups[: max(0, len(backups) - self.backup_count)]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return target

    def _save(self, document: DocxDocument) -> None:
        tmp = self.path.with_name(f"~wordlens_{os.getpid()}_{self.path.name}")
        try:
            document.save(str(tmp))
            os.replace(tmp, self.path)
        except PermissionError as exc:
            raise DocumentLockedError(
                f"Can't write {self.path.name}. Is it open in Word?"
            ) from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _append(document: DocxDocument, item: VocabularyItem) -> None:
        paragraph = document.add_paragraph()
        expr_run = paragraph.add_run(item.expression)
        expr_run.bold = True
        trans_run = paragraph.add_run(f"{SEPARATOR}{item.translation}")
        trans_run.bold = False

    def add_entries(self, items: Iterable[VocabularyItem]) -> list[VocabularyItem]:
        """Añade al final las entradas que no estén ya en el documento.

        Devuelve las entradas realmente escritas. Si el documento está bloqueado,
        las guarda en cola y lanza DocumentLockedError.
        """
        with self._lock:
            queue = self._pending + list(items)
            self._pending = []
            if not queue:
                return []
            try:
                document = self._load()
            except DocumentError:
                # Bloqueado o ilegible: nada se pierde, queda en espera.
                self._pending = queue
                raise
            existing = {dedup_key(e, self.lang) for e in self._expressions_in(document)}
            written: list[VocabularyItem] = []
            for item in queue:
                key = dedup_key(item.expression, self.lang)
                if not key or key in existing:
                    continue
                existing.add(key)
                self._append(document, item)
                written.append(item)
            if not written:
                return []
            if not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._save(document)
            except DocumentLockedError:
                self._pending = queue
                raise
            return written

    def take_pending(self) -> list[VocabularyItem]:
        """Saca de la cola las entradas pendientes (para volcarlas a otro sitio)."""
        with self._lock:
            items, self._pending = self._pending, []
            return items

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
