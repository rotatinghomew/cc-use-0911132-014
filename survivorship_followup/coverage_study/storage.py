"""覆盖研究与既有 SQLite 运行台账的衔接。

复用 ``runtime.SQLiteLedger`` 的数据库文件，新增 coverage_study_runs 表保存每次
研究运行；同时从 runs 表查找 baseline_case_id 对应的既有产品运行，证明分析与
enrollment / timewheel / merge 流水线处于同一台账，而不是另起静态样例。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from survivorship_followup.runtime import fingerprint, stable_json, utc_now

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS coverage_study_runs(
    study_id TEXT PRIMARY KEY,
    input_fingerprint TEXT NOT NULL,
    baseline_case_id TEXT,
    baseline_run_id TEXT,
    result_json TEXT NOT NULL,
    source_versions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _connect(database: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(database), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=20000")
    con.execute(TABLE_DDL)
    return con


def find_baseline_run(database: str | Path, baseline_case_id: str | None) -> dict[str, Any] | None:
    """查找既有产品流水线（含 enrollment/timewheel/merge 阶段）的最近一次运行。"""
    if not baseline_case_id:
        return None
    con = _connect(database)
    try:
        tables = {row["name"] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "runs" not in tables:
            return None
        row = con.execute(
            "SELECT run_id, case_id, state, updated_at FROM runs "
            "WHERE case_id=? ORDER BY updated_at DESC LIMIT 1",
            (baseline_case_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def attach_study_run(database: str | Path, study_id: str, result: Mapping[str, Any],
                     baseline: Mapping[str, Any] | None,
                     baseline_case_id: str | None = None) -> None:
    """幂等写入研究运行；同一 study_id 重放不产生重复记录。"""
    con = _connect(database)
    try:
        con.execute(
            "INSERT OR IGNORE INTO coverage_study_runs VALUES(?,?,?,?,?,?,?)",
            (study_id,
             fingerprint({"study_id": study_id, "source": result.get("source_versions")}),
             baseline_case_id,
             baseline["run_id"] if baseline else None,
             stable_json(result),
             stable_json(result.get("source_versions", {})),
             utc_now()))
        con.commit()
    finally:
        con.close()


def load_study_run(database: str | Path, study_id: str) -> dict[str, Any] | None:
    con = _connect(database)
    try:
        row = con.execute(
            "SELECT result_json FROM coverage_study_runs WHERE study_id=?",
            (study_id,)).fetchone()
        if row is None:
            return None
        import json
        return json.loads(row["result_json"])
    finally:
        con.close()
