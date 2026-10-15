"""The unified document registry: one row per document, whichever service extracts it.

A separate SQLite file (``<data_dir>/registry.db``). It does not touch the IR_OCR database or the charge-sheet worker's
database: both keep owning their own OCR results, corrections and verification. The registry only records *which
adapter owns which document*, the original file, how the type was decided, and a log of processing warnings/errors.

``document_id`` is the id the owning service assigned (IR_OCR and the charge-sheet service both use UUID4), so the
existing API paths and the person-image folders keep working unchanged.

A document whose type could not be decided first waits in ``intakes`` (status ``awaiting_type``); it gets a
``document_id`` only when it is routed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    intake_id TEXT,
    original_filename TEXT NOT NULL,
    stored_original TEXT NOT NULL,
    detected_kind TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    document_type TEXT NOT NULL,
    type_source TEXT NOT NULL,
    adapter TEXT NOT NULL,
    native_id TEXT NOT NULL,
    classification_json TEXT,
    uploaded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_documents_adapter ON documents(adapter);
CREATE TABLE IF NOT EXISTS intakes (
    intake_id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    stored_original TEXT NOT NULL,
    detected_kind TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    classification_json TEXT,
    status TEXT NOT NULL,
    document_id TEXT,
    uploaded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_doc ON events(document_id);
"""

_UNSAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stored_name(original_filename: str, ext: str) -> str:
    """Server-side file name for the kept original (never the client's path)."""
    stem = Path(str(original_filename or "document").replace("\\", "/")).stem
    stem = _UNSAFE.sub("_", stem).strip("._")[:80] or "document"
    return f"{uuid.uuid4().hex}_{stem}{ext}"


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    raw = out.pop("classification_json", None)
    try:
        out["classification"] = json.loads(raw) if raw else None
    except ValueError:
        out["classification"] = None
    if "deleted" in out:
        out["deleted"] = bool(out["deleted"])
    return out


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.path), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------ intakes
    def create_intake(self, *, original_filename: str, stored_original: str, detected_kind: Optional[str],
                      content_type: Optional[str], size_bytes: int, sha256: str, classification: Dict[str, Any],
                      uploaded_by: Optional[str], status: str = "awaiting_type") -> str:
        intake_id = str(uuid.uuid4())
        now = utcnow()
        with self._conn() as c:
            c.execute(
                "INSERT INTO intakes (intake_id, original_filename, stored_original, detected_kind, content_type,"
                " size_bytes, sha256, classification_json, status, uploaded_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (intake_id, original_filename, stored_original, detected_kind, content_type, size_bytes, sha256,
                 json.dumps(classification, ensure_ascii=False), status, uploaded_by, now, now),
            )
        return intake_id

    def get_intake(self, intake_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM intakes WHERE intake_id=?", (intake_id,)).fetchone())

    def update_intake(self, intake_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE intakes SET {cols} WHERE intake_id=?", (*fields.values(), intake_id))

    def claim_intake(self, intake_id: str) -> bool:
        """Atomically move ``awaiting_type`` -> ``routing`` so two requests cannot route the same intake twice."""
        with self._conn() as c:
            cur = c.execute("UPDATE intakes SET status='routing', updated_at=? WHERE intake_id=? AND status='awaiting_type'",
                            (utcnow(), intake_id))
            return cur.rowcount == 1

    def list_intakes(self, status: str = "awaiting_type") -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [_row(r) for r in c.execute("SELECT * FROM intakes WHERE status=? ORDER BY created_at DESC", (status,))]

    # ---------------------------------------------------------------- documents
    def add_document(self, *, document_id: str, intake_id: Optional[str], original_filename: str, stored_original: str,
                     detected_kind: Optional[str], content_type: Optional[str], size_bytes: int, sha256: str,
                     document_type: str, type_source: str, adapter: str, native_id: str,
                     classification: Dict[str, Any], uploaded_by: Optional[str]) -> None:
        now = utcnow()
        with self._conn() as c:
            c.execute(
                "INSERT INTO documents (document_id, intake_id, original_filename, stored_original, detected_kind,"
                " content_type, size_bytes, sha256, document_type, type_source, adapter, native_id,"
                " classification_json, uploaded_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (document_id, intake_id, original_filename, stored_original, detected_kind, content_type, size_bytes,
                 sha256, document_type, type_source, adapter, native_id,
                 json.dumps(classification, ensure_ascii=False), uploaded_by, now, now),
            )

    def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone())

    def adapter_of(self, document_id: str) -> Optional[str]:
        """The owning adapter, or None for a document the registry does not know (e.g. uploaded before the merge)."""
        with self._conn() as c:
            row = c.execute("SELECT adapter, deleted FROM documents WHERE document_id=?", (document_id,)).fetchone()
        return row["adapter"] if row else None

    def list_documents(self, adapter: Optional[str] = None, include_deleted: bool = False) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM documents WHERE 1=1", []
        if adapter:
            sql += " AND adapter=?"
            args.append(adapter)
        if not include_deleted:
            sql += " AND deleted=0"
        with self._conn() as c:
            return [_row(r) for r in c.execute(sql + " ORDER BY created_at DESC", args)]

    def mark_deleted(self, document_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE documents SET deleted=1, updated_at=? WHERE document_id=?", (utcnow(), document_id))

    # ------------------------------------------------------------------- events
    def add_event(self, document_id: str, level: str, stage: str, message: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events (document_id, ts, level, stage, message) VALUES (?,?,?,?,?)",
                      (document_id, utcnow(), level, stage, str(message)[:500]))

    def events(self, document_id: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT ts, level, stage, message FROM events WHERE document_id=? ORDER BY id", (document_id,))]
