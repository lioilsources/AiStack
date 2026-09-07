"""SQLite úložiště jobů — reprodukovatelnost (prompt, model, seed, hash).

Stav přežije restart kontejneru; to je rozdíl proti gen-queue, která drží
joby jen v paměti. Assety se generují jednou a regenerují podle seedu, takže
ztratit záznam o tom, čím a s jakým seedem vznikly, je horší než ztratit job.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    request      TEXT NOT NULL,
    outputs      TEXT NOT NULL DEFAULT '[]',
    error        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created_at);
"""


class Store:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + zámek: joby běží ve vlastních vláknech,
        # ale zápisů je málo, takže jedno spojení pod zámkem stačí.
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create(self, kind: str, request: dict[str, Any], model: str) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (job_id, kind, status, model, request, created_at) "
                "VALUES (?, ?, 'queued', ?, ?, ?)",
                (job_id, kind, model, json.dumps(request, ensure_ascii=False), time.time()),
            )
            self._db.commit()
        return job_id

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE job_id=?",
                (time.time(), job_id),
            )
            self._db.commit()

    def mark_done(self, job_id: str, outputs: list[dict[str, Any]]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status='done', outputs=?, finished_at=? WHERE job_id=?",
                (json.dumps(outputs, ensure_ascii=False), time.time(), job_id),
            )
            self._db.commit()

    def mark_error(self, job_id: str, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status='error', error=?, finished_at=? WHERE job_id=?",
                (error[:4000], time.time(), job_id),
            )
            self._db.commit()

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def requeue_orphans(self) -> int:
        """Joby zachycené restartem ve stavu running označí jako chybové.

        Worker, který je zpracovával, je pryč, takže by v running zůstaly navždy.
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET status='error', error='přerušeno restartem služby', "
                "finished_at=? WHERE status IN ('running','queued')",
                (time.time(),),
            )
            self._db.commit()
            return cur.rowcount


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["request"] = json.loads(d["request"])
    d["outputs"] = json.loads(d["outputs"])
    return d
