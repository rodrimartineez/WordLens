"""Estado persistente del vocabulario: una única fuente de verdad (SQLite).

Archivo: <carpeta de datos>/vocabulary.db (%LOCALAPPDATA%\\WordLens en la app
instalada). Nunca dentro del documento Word ni de la carpeta de instalación,
así que sobrevive a cerrar la app, reiniciar Windows, cambiar de documento y
actualizar o reinstalar WordLens.

Tablas (cada entrada se identifica por (lang, key), ver normalization.entry_key):

  lines        subtítulos capturados y su estado (pending / done). Un subtítulo
               no se pierde si la app se cierra o Ollama falla: se reanaliza.
  candidates   candidatos pendientes de revisión, ya consolidados (uno por
               clave, con el número de apariciones) y su traducción.
  accepted     decisiones ✓ (globales, independientes del documento) con el
               estado de escritura en Word: pending -> written. Un fallo deja
               la entrada en pending con el error, para reintentarla.
  excluded     decisiones ✕ (globales). Restaurar = borrar la fila.
  translations caché de traducciones por (clave, contexto, motor).
  meta         versión del esquema y marcas de migración.

Reglas que garantiza este módulo:
- Una clave aceptada o excluida no vuelve a entrar en candidates.
- Aceptar/excluir mueve la entrada en una sola transacción: nunca queda en
  dos sitios ni en ninguno.
- Restaurar una exclusión no acepta nada ni escribe en Word; si la clave
  también estaba aceptada, la aceptación sigue mandando.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import Candidate
from .normalization import entry_key

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    ended REAL,
    document TEXT
);
CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    lang TEXT NOT NULL,
    text TEXT NOT NULL,
    created REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS lines_status ON lines(status, id);
CREATE TABLE IF NOT EXISTS candidates (
    lang TEXT NOT NULL,
    key TEXT NOT NULL,
    expression TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'word',
    context TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'model',
    occurrences INTEGER NOT NULL DEFAULT 1,
    session_id INTEGER,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    translation TEXT,
    engine TEXT,
    translate_error TEXT,
    PRIMARY KEY (lang, key)
);
CREATE TABLE IF NOT EXISTS accepted (
    lang TEXT NOT NULL,
    key TEXT NOT NULL,
    expression TEXT NOT NULL,
    translation TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'word',
    context TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'review',
    accepted_at REAL NOT NULL,
    write_state TEXT NOT NULL DEFAULT 'pending',
    document TEXT,
    written_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (lang, key)
);
CREATE INDEX IF NOT EXISTS accepted_state ON accepted(write_state);
CREATE TABLE IF NOT EXISTS excluded (
    lang TEXT NOT NULL,
    key TEXT NOT NULL,
    expression TEXT NOT NULL,
    excluded_at REAL NOT NULL,
    PRIMARY KEY (lang, key)
);
CREATE TABLE IF NOT EXISTS translations (
    lang TEXT NOT NULL,
    key TEXT NOT NULL,
    context TEXT NOT NULL,
    engine TEXT NOT NULL,
    translation TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (lang, key, context, engine)
);
"""


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class ReviewEntry:
    """Una fila de la ventana de revisión."""

    key: str
    expression: str
    translation: str | None
    kind: str
    context: str
    occurrences: int
    status: str  # "pending" (por decidir) | "saving" (aceptada, falta confirmar Word)
    error: str | None = None


@dataclass(frozen=True)
class AcceptedEntry:
    key: str
    expression: str
    translation: str
    context: str
    attempts: int
    error: str | None


@dataclass(frozen=True)
class ExcludedEntry:
    key: str
    expression: str
    excluded_at: float


class VocabularyStore:
    def __init__(self, path: Path | str, *, backup_dir: Path | None = None, backups_to_keep: int = 10) -> None:
        self.path = Path(path)
        self.backup_dir = backup_dir
        self.backups_to_keep = backups_to_keep
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.recovered: str | None = None  # descripción si hubo que recuperar el archivo
        self._listeners: list = []

    # ------------------------------------------------------------ apertura
    def open(self) -> VocabularyStore:
        """Abre (o crea) la base de datos. Un archivo dañado se aparta, nunca se borra."""
        with self._lock:
            if self._conn is not None:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._conn = self._connect()
            except sqlite3.DatabaseError as exc:
                self._conn = None
                self._recover(exc)
            return self

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            result = conn.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {result}")
            conn.executescript(SCHEMA)
            version = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            if version is None:
                conn.execute("INSERT INTO meta(k, v) VALUES('schema', ?)", (str(SCHEMA_VERSION),))
                conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('created', ?)", (str(time.time()),))
            elif int(version[0]) > SCHEMA_VERSION:
                log.warning("vocabulary.db es de una versión más nueva (%s); se usa sin cambiar el esquema", version[0])
        except Exception:
            conn.close()
            raise
        return conn

    def _recover(self, exc: Exception) -> None:
        """El archivo no se puede abrir: se aparta y se restaura la última copia buena."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        damaged = self.path.with_name(f"{self.path.stem}.damaged_{stamp}{self.path.suffix}")
        log.error("vocabulary.db dañado (%s); se aparta como %s", exc, damaged.name)
        try:
            self.path.replace(damaged)
        except OSError as move_exc:
            raise StoreError(f"No se puede abrir ni apartar {self.path}: {move_exc}") from exc
        for backup in reversed(self._backups()):
            try:
                shutil.copy2(backup, self.path)
                self._conn = self._connect()
                self.recovered = f"restored from backup {backup.name} (damaged file kept as {damaged.name})"
                log.warning("vocabulary.db restaurado desde %s", backup)
                return
            except (OSError, sqlite3.DatabaseError) as err:
                log.error("La copia %s tampoco sirve: %s", backup, err)
                self.path.unlink(missing_ok=True)
        self._conn = self._connect()
        self.recovered = f"no valid backup; started empty (damaged file kept as {damaged.name})"

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        assert self._conn is not None
        return self._conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Transacción de escritura: todo o nada."""
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        self._changed()

    def _query(self, sql: str, params: Iterable = ()) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    # Avisos de cambios (la ventana de revisión y los contadores se refrescan solos).
    def subscribe(self, listener) -> None:
        self._listeners.append(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Error en un listener del almacén")

    # -------------------------------------------------------------- copias
    def _backups(self) -> list[Path]:
        if self.backup_dir is None or not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("vocabulary_*.db"))

    def backup(self) -> Path | None:
        """Copia consistente (API de backup de SQLite) en la carpeta de copias, rotativa."""
        if self.backup_dir is None:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"vocabulary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        with self._lock:
            dest = sqlite3.connect(target)
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        for old in self._backups()[: -self.backups_to_keep]:
            old.unlink(missing_ok=True)
        return target

    # ---------------------------------------------------------------- meta
    def get_meta(self, k: str) -> str | None:
        row = self._query("SELECT v FROM meta WHERE k=?", (k,))
        return row[0][0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ------------------------------------------------------------ sesiones
    def start_session(self, document: str) -> int:
        with self._tx() as c:
            return int(c.execute("INSERT INTO sessions(started, document) VALUES(?, ?)", (time.time(), document)).lastrowid)

    def end_session(self, session_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE sessions SET ended=? WHERE id=?", (time.time(), session_id))

    # ---------------------------------------------------------- subtítulos
    def add_line(self, text: str, lang: str, session_id: int | None) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO lines(session_id, lang, text, created) VALUES(?, ?, ?, ?)",
                (session_id, lang, text, time.time()),
            )
            return int(cur.lastrowid)

    def pending_lines(self, lang: str, limit: int = 100) -> list[tuple[int, str]]:
        return [
            (int(i), t)
            for i, t in self._query(
                "SELECT id, text FROM lines WHERE status='pending' AND lang=? ORDER BY id LIMIT ?", (lang, limit)
            )
        ]

    def count_pending_lines(self, lang: str | None = None) -> int:
        if lang is None:
            return int(self._query("SELECT COUNT(*) FROM lines WHERE status='pending'")[0][0])
        return int(self._query("SELECT COUNT(*) FROM lines WHERE status='pending' AND lang=?", (lang,))[0][0])

    def finish_lines(
        self,
        line_ids: list[int],
        candidates: list[Candidate],
        lang: str,
        session_id: int | None,
        known_keys: Iterable[str] = (),
    ) -> list[str]:
        """Guarda los candidatos de unas líneas y las marca como analizadas (atómico).

        Devuelve las claves que han entrado nuevas en la revisión. Lo aceptado,
        lo excluido y lo que ya está en el documento (known_keys) se ignora; lo
        que ya estaba pendiente sólo suma una aparición.
        """
        known = set(known_keys)
        now = time.time()
        added: list[str] = []
        with self._tx() as c:
            for cand in candidates:
                key = entry_key(cand.expression, lang)
                if not key or key in known:
                    continue
                if c.execute("SELECT 1 FROM accepted WHERE lang=? AND key=?", (lang, key)).fetchone():
                    continue
                if c.execute("SELECT 1 FROM excluded WHERE lang=? AND key=?", (lang, key)).fetchone():
                    continue
                cur = c.execute(
                    "UPDATE candidates SET occurrences=occurrences+1, last_seen=? WHERE lang=? AND key=?",
                    (now, lang, key),
                )
                if cur.rowcount:
                    continue
                c.execute(
                    "INSERT INTO candidates(lang, key, expression, kind, context, origin, session_id, first_seen, last_seen)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (lang, key, cand.expression, cand.kind, cand.context, cand.origin, session_id, now, now),
                )
                added.append(key)
            c.executemany("UPDATE lines SET status='done' WHERE id=?", [(i,) for i in line_ids])
        return added

    # ------------------------------------------------------------ traducción
    def untranslated(self, lang: str, engine_id: str, limit: int = 1000) -> list[tuple[str, str, str, str]]:
        """(key, expression, kind, context) sin traducción o traducidas con otro motor."""
        return self._query(
            "SELECT key, expression, kind, context FROM candidates WHERE lang=? "
            "AND (translation IS NULL OR engine IS NULL OR engine<>?) ORDER BY first_seen, rowid LIMIT ?",
            (lang, engine_id, limit),
        )

    def count_untranslated(self, lang: str, engine_id: str) -> int:
        return int(
            self._query(
                "SELECT COUNT(*) FROM candidates WHERE lang=? AND (translation IS NULL OR engine IS NULL OR engine<>?)",
                (lang, engine_id),
            )[0][0]
        )

    def cached_translation(self, lang: str, key: str, context: str, engine_id: str) -> str | None:
        row = self._query(
            "SELECT translation FROM translations WHERE lang=? AND key=? AND context=? AND engine=?",
            (lang, key, context, engine_id),
        )
        return row[0][0] if row else None

    def set_translations(self, lang: str, engine_id: str, results: list[tuple[str, str, str | None]]) -> None:
        """results: (key, context, traducción o None si el motor no dio una válida)."""
        now = time.time()
        with self._tx() as c:
            for key, context, translation in results:
                if translation:
                    c.execute(
                        "UPDATE candidates SET translation=?, engine=?, translate_error=NULL WHERE lang=? AND key=?",
                        (translation, engine_id, lang, key),
                    )
                    c.execute(
                        "INSERT OR REPLACE INTO translations(lang, key, context, engine, translation, created)"
                        " VALUES(?, ?, ?, ?, ?, ?)",
                        (lang, key, context, engine_id, translation, now),
                    )
                else:
                    c.execute(
                        "UPDATE candidates SET translate_error=? WHERE lang=? AND key=?",
                        ("no translation", lang, key),
                    )

    def set_translation(self, lang: str, key: str, translation: str) -> bool:
        """Corrección manual de una traducción pendiente (o de una aceptada aún sin guardar)."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE candidates SET translation=?, translate_error=NULL WHERE lang=? AND key=?",
                (translation, lang, key),
            )
            if cur.rowcount:
                return True
            cur = c.execute(
                "UPDATE accepted SET translation=? WHERE lang=? AND key=? AND write_state='pending'",
                (translation, lang, key),
            )
            return bool(cur.rowcount)

    # -------------------------------------------------------------- revisión
    def review_entries(self, lang: str) -> list[ReviewEntry]:
        """Pendientes de decidir + aceptadas que todavía no están confirmadas en Word."""
        rows = self._query(
            "SELECT key, expression, translation, kind, context, occurrences FROM candidates WHERE lang=? "
            "ORDER BY session_id, first_seen, rowid",
            (lang,),
        )
        out = [ReviewEntry(k, e, t, kd, ctx, int(n), "pending") for k, e, t, kd, ctx, n in rows]
        rows = self._query(
            "SELECT key, expression, translation, kind, context, error FROM accepted "
            "WHERE lang=? AND write_state='pending' ORDER BY accepted_at",
            (lang,),
        )
        out += [ReviewEntry(k, e, t, kd, ctx, 1, "saving", err) for k, e, t, kd, ctx, err in rows]
        return out

    def counts(self, lang: str) -> dict[str, int]:
        q = self._query
        return {
            "pending": int(q("SELECT COUNT(*) FROM candidates WHERE lang=?", (lang,))[0][0]),
            "unsaved": int(q("SELECT COUNT(*) FROM accepted WHERE lang=? AND write_state='pending'", (lang,))[0][0]),
            "accepted": int(q("SELECT COUNT(*) FROM accepted WHERE lang=?", (lang,))[0][0]),
            "excluded": int(q("SELECT COUNT(*) FROM excluded WHERE lang=?", (lang,))[0][0]),
            "lines": self.count_pending_lines(lang),
        }

    def accept(self, lang: str, key: str, document: str, translation: str | None = None) -> bool:
        """✓: registra la aceptación (pendiente de escribir) y la quita de la revisión.

        No escribe en Word: lo hace el escritor (app.SaveWorker), que confirma
        con mark_written(). Devuelve False si la clave ya no estaba pendiente.
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT expression, translation, kind, context FROM candidates WHERE lang=? AND key=?", (lang, key)
            ).fetchone()
            if row is None:
                return False
            expression, stored, kind, context = row
            final = (translation or stored or "").strip()
            if not final:
                raise StoreError(f"{expression!r} has no translation yet")
            c.execute(
                "INSERT INTO accepted(lang, key, expression, translation, kind, context, origin, accepted_at, document)"
                " VALUES(?, ?, ?, ?, ?, ?, 'review', ?, ?)"
                " ON CONFLICT(lang, key) DO NOTHING",
                (lang, key, expression, final, kind, context, time.time(), document),
            )
            c.execute("DELETE FROM candidates WHERE lang=? AND key=?", (lang, key))
            return True

    def exclude(self, lang: str, key: str, expression: str | None = None) -> bool:
        """✕: añade a la lista de exclusión y quita de la revisión. Nunca toca Word."""
        with self._tx() as c:
            row = c.execute("SELECT expression FROM candidates WHERE lang=? AND key=?", (lang, key)).fetchone()
            name = row[0] if row else expression
            if not name:
                return False
            c.execute(
                "INSERT INTO excluded(lang, key, expression, excluded_at) VALUES(?, ?, ?, ?)"
                " ON CONFLICT(lang, key) DO NOTHING",
                (lang, key, name, time.time()),
            )
            c.execute("DELETE FROM candidates WHERE lang=? AND key=?", (lang, key))
            return True

    # ------------------------------------------------------- lista de exclusión
    def excluded_entries(self, lang: str, search: str = "", limit: int = 200, offset: int = 0) -> tuple[list[ExcludedEntry], int]:
        like = f"%{search.strip().lower()}%"
        rows = self._query(
            "SELECT key, expression, excluded_at FROM excluded WHERE lang=? AND (lower(expression) LIKE ? OR key LIKE ?) "
            "ORDER BY expression COLLATE NOCASE LIMIT ? OFFSET ?",
            (lang, like, like, limit, offset),
        )
        total = self._query(
            "SELECT COUNT(*) FROM excluded WHERE lang=? AND (lower(expression) LIKE ? OR key LIKE ?)", (lang, like, like)
        )[0][0]
        return [ExcludedEntry(k, e, float(t)) for k, e, t in rows], int(total)

    def restore(self, lang: str, keys: Iterable[str]) -> int:
        """Saca claves de la lista de exclusión. No acepta ni escribe nada en Word."""
        with self._tx() as c:
            return sum(c.execute("DELETE FROM excluded WHERE lang=? AND key=?", (lang, k)).rowcount for k in keys)

    def is_excluded(self, lang: str, key: str) -> bool:
        return bool(self._query("SELECT 1 FROM excluded WHERE lang=? AND key=?", (lang, key)))

    def is_accepted(self, lang: str, key: str) -> bool:
        return bool(self._query("SELECT 1 FROM accepted WHERE lang=? AND key=?", (lang, key)))

    # ---------------------------------------------------- escritura en Word
    def unsaved(self, lang: str | None = None) -> list[AcceptedEntry]:
        sql = "SELECT key, expression, translation, context, attempts, error FROM accepted WHERE write_state='pending'"
        params: tuple = ()
        if lang is not None:
            sql += " AND lang=?"
            params = (lang,)
        rows = self._query(sql + " ORDER BY accepted_at", params)
        return [AcceptedEntry(k, e, t, ctx, int(a), err) for k, e, t, ctx, a, err in rows]

    def mark_written(self, lang: str, keys: Iterable[str], document: str) -> None:
        now = time.time()
        with self._tx() as c:
            c.executemany(
                "UPDATE accepted SET write_state='written', written_at=?, document=?, error=NULL, attempts=attempts+1"
                " WHERE lang=? AND key=?",
                [(now, document, lang, k) for k in keys],
            )

    def mark_write_failed(self, lang: str, keys: Iterable[str], error: str) -> None:
        with self._tx() as c:
            c.executemany(
                "UPDATE accepted SET error=?, attempts=attempts+1 WHERE lang=? AND key=? AND write_state='pending'",
                [(error, lang, k) for k in keys],
            )

    def import_accepted(self, lang: str, entries: Iterable[tuple[str, str]], *, origin: str, written: bool, document: str = "") -> int:
        """Registra como aceptadas entradas que ya existían (documento, pendientes antiguos)."""
        now = time.time()
        count = 0
        with self._tx() as c:
            for expression, translation in entries:
                key = entry_key(expression, lang)
                if not key:
                    continue
                cur = c.execute(
                    "INSERT INTO accepted(lang, key, expression, translation, origin, accepted_at, write_state, document, written_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(lang, key) DO NOTHING",
                    (lang, key, expression, translation, origin, now, "written" if written else "pending", document,
                     now if written else None),
                )
                count += cur.rowcount
                if cur.rowcount:
                    c.execute("DELETE FROM candidates WHERE lang=? AND key=?", (lang, key))
        return count
