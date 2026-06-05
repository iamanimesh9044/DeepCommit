from __future__ import annotations

import sqlite3
from pathlib import Path

from .utils import get_project_root


def refresh_views(db_path: Path | None = None, sql_path: Path | None = None) -> None:
    project_root = get_project_root()
    db_path = (db_path or (project_root / "DATA" / "result" / "trials.sqlite")).expanduser().resolve()
    sql_path = (sql_path or (project_root / "scripts" / "sql" / "views.sql")).expanduser().resolve()

    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        with sql_path.open("r", encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.commit()
    finally:
        conn.close()
