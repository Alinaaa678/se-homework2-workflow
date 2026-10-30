"""SQLite-backed run checkpoints and artifact history."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from typing import Any, Dict, List

from .artifacts import content_hash
from .state import WorkflowState


def flow_hash(flow: Dict[str, Any]) -> str:
    return content_hash(flow)


class WorkflowStore:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    flow_name TEXT NOT NULL,
                    flow_hash TEXT NOT NULL,
                    flow_def TEXT NOT NULL,
                    current_node TEXT NOT NULL,
                    prev_node TEXT NOT NULL,
                    visits TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    log_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    visit INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

    def save_checkpoint(
        self,
        run_id: str,
        flow: Dict[str, Any],
        state: WorkflowState,
        current_node: str,
        prev_node: str,
        visits: Dict[str, int],
        log_path: str,
    ) -> None:
        now = time.time()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO runs (
                    run_id, flow_name, flow_hash, flow_def, current_node,
                    prev_node, visits, state, status, error, log_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    flow_name=excluded.flow_name,
                    flow_hash=excluded.flow_hash,
                    flow_def=excluded.flow_def,
                    current_node=excluded.current_node,
                    prev_node=excluded.prev_node,
                    visits=excluded.visits,
                    state=excluded.state,
                    status=excluded.status,
                    error=excluded.error,
                    log_path=excluded.log_path,
                    updated_at=excluded.updated_at
            """, (
                run_id, flow.get("name", "unnamed"), flow_hash(flow),
                json.dumps(flow, ensure_ascii=False), current_node, prev_node,
                json.dumps(visits), json.dumps(state.to_dict(), ensure_ascii=False),
                state.status, state.error, log_path, now, now,
            ))

    def load_run(self, run_id: str) -> Dict[str, Any]:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"找不到运行记录: {run_id}")
        result = dict(row)
        for key in ("flow_def", "visits", "state"):
            result[key] = json.loads(result[key])
        return result

    def record_artifact(
        self,
        run_id: str,
        node: str,
        visit: int,
        key: str,
        value: Any,
        metadata: Dict[str, Any],
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO artifacts (run_id, node, visit, key, value, hash, version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, node, visit, key, json.dumps(value, ensure_ascii=False),
                metadata["hash"], metadata["version"], time.time(),
            ))

    def artifact_history(self, run_id: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item["value"])
            result.append(item)
        return result
