from __future__ import annotations

"""SQLite-backed persistence for graphs, runs, and step logs."""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SQLitePersistence:
    """Thin wrapper around sqlite3 for storing graphs and run history."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_db(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS graphs (
                    id TEXT PRIMARY KEY,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    graph_id TEXT,
                    status TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    initial_state_json TEXT,
                    final_state_json TEXT,
                    last_error TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS run_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    step_no INTEGER,
                    node TEXT,
                    timestamp TEXT,
                    success INTEGER,
                    error TEXT,
                    error_type TEXT,
                    state_diff_json TEXT,
                    duration_ms REAL
                )
                """
            )
            conn.commit()

    def save_graph(self, graph_record: Dict[str, Any]) -> None:
        """Persist a graph definition as JSON."""
        payload = json.dumps(graph_record)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO graphs (id, definition_json, created_at)
                VALUES (?, ?, ?)
                """,
                (graph_record["id"], payload, created_at),
            )
            conn.commit()

    def list_graphs(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT id, definition_json FROM graphs").fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            results.append(json.loads(row[1]))
        return results

    def get_graph(self, graph_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT definition_json FROM graphs WHERE id = ?", (graph_id,)
            ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def record_run_start(
        self,
        run_id: str,
        graph_id: str,
        started_at: datetime,
        initial_state: Dict[str, Any],
        status: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs
                    (id, graph_id, status, started_at, initial_state_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    graph_id,
                    status,
                    started_at.isoformat(),
                    json.dumps(initial_state),
                ),
            )
            conn.commit()

    def record_run_finish(
        self,
        run_id: str,
        status: str,
        finished_at: datetime,
        final_state: Dict[str, Any],
        last_error: Optional[str],
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?, final_state_json = ?, last_error = ?
                WHERE id = ?
                """,
                (status, finished_at.isoformat(), json.dumps(final_state), last_error, run_id),
            )
            conn.commit()

    def append_step(
        self,
        run_id: str,
        step_no: int,
        node: str,
        timestamp: datetime,
        success: bool,
        error: Optional[str],
        error_type: Optional[str],
        state_diff: Dict[str, Any],
        duration_ms: float,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_steps
                    (run_id, step_no, node, timestamp, success, error, error_type, state_diff_json, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_no,
                    node,
                    timestamp.isoformat(),
                    1 if success else 0,
                    error,
                    error_type,
                    json.dumps(state_diff),
                    duration_ms,
                ),
            )
            conn.commit()

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, graph_id, status, started_at, finished_at, last_error
                FROM runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for rid, graph_id, status, started, finished, err in rows:
            result.append(
                {
                    "run_id": rid,
                    "graph_id": graph_id,
                    "status": status,
                    "started_at": started,
                    "finished_at": finished,
                    "last_error": err,
                }
            )
        return result

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, graph_id, status, started_at, finished_at, initial_state_json, final_state_json, last_error
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "run_id": row[0],
            "graph_id": row[1],
            "status": row[2],
            "started_at": row[3],
            "finished_at": row[4],
            "initial_state": json.loads(row[5]) if row[5] else {},
            "final_state": json.loads(row[6]) if row[6] else {},
            "last_error": row[7],
        }

    def get_run_steps(self, run_id: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT step_no, node, timestamp, success, error, error_type, state_diff_json, duration_ms
                FROM run_steps
                WHERE run_id = ?
                ORDER BY step_no ASC
                """,
                (run_id,),
            ).fetchall()
        steps: List[Dict[str, Any]] = []
        for step_no, node, timestamp, success, error, error_type, state_diff_json, duration_ms in rows:
            steps.append(
                {
                    "step_no": step_no,
                    "node": node,
                    "timestamp": timestamp,
                    "success": bool(success),
                    "error": error,
                    "error_type": error_type,
                    "state_diff": json.loads(state_diff_json),
                    "duration_ms": duration_ms,
                }
            )
        return steps


def _create_default_persistence() -> Optional[SQLitePersistence]:
    """Create a default persistence instance, falling back to None if unavailable."""
    db_path = Path(__file__).resolve().parent.parent / "workflow.db"
    try:
        return SQLitePersistence(db_path)
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("SQLite persistence disabled: %s", exc)
        return None


persistence: Optional[SQLitePersistence] = _create_default_persistence()
