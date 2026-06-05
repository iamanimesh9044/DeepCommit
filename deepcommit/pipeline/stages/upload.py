from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Tuple

from pipeline.utils.pipeline_logger import PipelineLoggerAdapter

from ..context import TrialContext
from ..types import StageResult


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable objects to serializable types.

    Handles:
    - set -> list
    - Other types -> str (as fallback)
    """
    if isinstance(obj, set):
        return sorted(list(obj))  # Convert set to sorted list
    elif isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        # Fallback for other types (like datetime, Path, etc.)
        return str(obj)


def upload_results(
    ctx: TrialContext, analysis: StageResult, agent_run: StageResult, logger: logging.Logger | PipelineLoggerAdapter
) -> StageResult:
    """Placeholder for uploading results to a DB.

    Template writes meta.json into <result_path>/.
    """

    print()
    logger.info("[8/8] Upload (write meta.json) -> %s", ctx.result_path)

    try:
        # Construct meta.json
        meta = ctx.to_meta()
        meta.update(
            {
                "metrics": analysis.artifacts.get("metrics", {}),
            }
        )
        if agent_run and isinstance(agent_run.artifacts, dict):
            meta.update(agent_run.artifacts)

        out_path = os.path.join(ctx.result_path, "meta.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False, default=str)

        # After writing meta.json, upsert into SQLite DB and ingest CSVs.
        try:
            db_path = os.path.realpath(os.path.join(ctx.result_path, "..", "trials.sqlite"))
            _upsert_to_sqlite(db_path=db_path, meta=meta, result_dir=Path(ctx.result_path), logger=logger)
            artifacts = {"meta_path": out_path, "db_path": db_path}
            return StageResult(status="ok", artifacts=artifacts)
        except Exception as db_err:
            logger.exception("SQLite upload failed: %s", db_err)
            # Keep pipeline green; record note and return ok with meta path only
            return StageResult(status="ok", artifacts={"meta_path": out_path}, notes=f"SQLite upload failed: {db_err}")
    except Exception as e:
        logger.exception("Upload/meta write failed: %s", e)
        return StageResult(status="error", error=str(e))


def _with_connection(db_path: str, logger: logging.Logger | PipelineLoggerAdapter) -> sqlite3.Connection:
    """Open SQLite connection with WAL and sane timeouts for light concurrency."""

    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    # experiments table (flattened meta; keep only metrics_json for extensible metrics)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            trial TEXT PRIMARY KEY,
            workspace_root TEXT,
            repo_id TEXT,
            agents TEXT,
            model TEXT,
            prompt_path TEXT,
            code_commit_sha TEXT,
            max_retries INTEGER,
            start_ts TEXT,
            end_ts TEXT,
            duration_sec REAL,
            status TEXT,
            status_str TEXT,
            validation_error TEXT,
            agent_cli_status TEXT,
            agent_cli_exit_code INTEGER,
            agent_cli_num_turns INTEGER,
            agent_cli_stderr TEXT,
            prompt_rendered_path TEXT,
            mcp_config_path TEXT,
            milestones_csv_path TEXT,
            dependencies_csv_path TEXT,
            agent_cli_log_path TEXT,
            milestones_count INTEGER,
            dependencies_count INTEGER,
            metrics_json TEXT
        );
        """
    )

    # milestones table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS milestones (
            trial TEXT NOT NULL,
            milestone_id TEXT NOT NULL,
            title TEXT,
            PRIMARY KEY (trial, milestone_id),
            FOREIGN KEY (trial) REFERENCES experiments(trial) ON DELETE CASCADE
        );
        """
    )

    # dependencies table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dependencies (
            trial TEXT NOT NULL,
            prerequisite_id TEXT NOT NULL,
            dependant_id TEXT NOT NULL,
            PRIMARY KEY (trial, prerequisite_id, dependant_id),
            FOREIGN KEY (trial) REFERENCES experiments(trial) ON DELETE CASCADE
        );
        """
    )

    # Schema migration: legacy DBs had columns (source_id, target_id).
    # If present, rename via ALTER TABLE so subsequent INSERTs work. Idempotent.
    cols = {row[1] for row in cur.execute("PRAGMA table_info(dependencies)").fetchall()}
    if "source_id" in cols and "prerequisite_id" not in cols:
        cur.execute("ALTER TABLE dependencies RENAME COLUMN source_id TO prerequisite_id")
    if "target_id" in cols and "dependant_id" not in cols:
        cur.execute("ALTER TABLE dependencies RENAME COLUMN target_id TO dependant_id")

    # milestone_commits table (optional ingest if file present)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS milestone_commits (
            trial TEXT NOT NULL,
            repo_id TEXT,
            milestone_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            PRIMARY KEY (trial, milestone_id, commit_sha),
            FOREIGN KEY (trial) REFERENCES experiments(trial) ON DELETE CASCADE
        );
        """
    )
    conn.commit()


def _upsert_experiment(conn: sqlite3.Connection, meta: dict) -> None:
    payload = {
        "trial": meta.get("trial"),
        "workspace_root": meta.get("workspace_root"),
        "repo_id": meta.get("repo_id"),
        "agents": meta.get("agents"),
        "model": meta.get("model"),
        "prompt_path": meta.get("prompt_path"),
        "code_commit_sha": meta.get("code_commit_sha"),
        "max_retries": meta.get("max_retries"),
        "start_ts": meta.get("start_ts") or meta.get("start_timestamp"),
        "end_ts": meta.get("end_ts") or meta.get("end_timestamp"),
        "duration_sec": meta.get("duration_sec"),
        "status": meta.get("status"),
        "status_str": meta.get("status_str"),
        "validation_error": meta.get("validation_error"),
        "agent_cli_status": meta.get("agent_cli_status"),
        "agent_cli_exit_code": meta.get("agent_cli_exit_code"),
        "agent_cli_num_turns": meta.get("agent_cli_num_turns"),
        "agent_cli_stderr": meta.get("agent_cli_stderr"),
        "prompt_rendered_path": str(meta.get("prompt_rendered_path") or ""),
        "mcp_config_path": str(meta.get("mcp_config_path") or ""),
        "milestones_csv_path": str(meta.get("milestones_csv_path") or ""),
        "dependencies_csv_path": str(meta.get("dependencies_csv_path") or ""),
        "agent_cli_log_path": str(meta.get("agent_cli_log_path") or ""),
        "milestones_count": meta.get("milestones_count"),
        "dependencies_count": meta.get("dependencies_count"),
        "metrics_json": json.dumps(_sanitize_for_json(meta.get("metrics") or {}), ensure_ascii=False),
    }

    cols = ",".join(payload.keys())
    placeholders = ":" + ",:".join(payload.keys())
    set_clause = ", ".join([f"{k}=excluded.{k}" for k in payload.keys() if k != "trial"])
    sql = f"""
        INSERT INTO experiments ({cols})
        VALUES ({placeholders})
        ON CONFLICT(trial) DO UPDATE SET {set_clause}
    """
    conn.execute(sql, payload)


def _read_csv_dicts(path: Path) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield {k: (v or "").strip() for k, v in row.items()}


def _ingest_milestones_and_dependencies(conn: sqlite3.Connection, meta: dict, result_dir: Path) -> Tuple[int, int]:
    trial = meta.get("trial")
    milestones_csv = result_dir / "milestones.csv"
    deps_csv = result_dir / "dependencies.csv"
    ms_count = 0
    dep_count = 0

    if milestones_csv.is_file():
        for row in _read_csv_dicts(milestones_csv):
            milestone_id = row.get("id") or row.get("milestone_id")
            title = row.get("title")
            if not milestone_id:
                continue
            conn.execute(
                """
                INSERT INTO milestones (trial, milestone_id, title)
                VALUES (?, ?, ?)
                ON CONFLICT(trial, milestone_id) DO UPDATE SET title=excluded.title
                """,
                (trial, milestone_id, title),
            )
            ms_count += 1

    if deps_csv.is_file():
        for row in _read_csv_dicts(deps_csv):
            # Accept both canonical (prerequisite_id / dependant_id) and
            # legacy (source_id / target_id) column headers. Same direction.
            src = row.get("prerequisite_id") or row.get("source_id") or row.get("source")
            tgt = row.get("dependant_id") or row.get("target_id") or row.get("target")
            if not src or not tgt:
                continue
            conn.execute(
                """
                INSERT INTO dependencies (trial, prerequisite_id, dependant_id)
                VALUES (?, ?, ?)
                ON CONFLICT(trial, prerequisite_id, dependant_id) DO NOTHING
                """,
                (trial, src, tgt),
            )
            dep_count += 1

    return ms_count, dep_count


def _ingest_milestone_commits_optional(conn: sqlite3.Connection, meta: dict, result_dir: Path) -> int:
    trial = meta.get("trial")
    repo_id = meta.get("repo_id")
    mc_csv = result_dir / "milestone_commits.csv"
    mc_count = 0
    if not mc_csv.is_file():
        return 0
    for row in _read_csv_dicts(mc_csv):
        ms_id = row.get("milestone_id") or row.get("id")
        sha = row.get("commit_sha") or row.get("sha")
        if not ms_id or not sha:
            continue
        conn.execute(
            """
            INSERT INTO milestone_commits (trial, repo_id, milestone_id, commit_sha)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trial, milestone_id, commit_sha) DO NOTHING
            """,
            (trial, repo_id, ms_id, sha),
        )
        mc_count += 1
    return mc_count


def _upsert_to_sqlite(
    *, db_path: str, meta: dict, result_dir: Path, logger: logging.Logger | PipelineLoggerAdapter
) -> None:
    # Light retry loop in case of transient locks
    attempts = 0
    last_err = None
    while attempts < 3:
        attempts += 1
        try:
            with _with_connection(db_path, logger) as conn:
                _ensure_schema(conn)
                _upsert_experiment(conn, meta)
                ms_count, dep_count = _ingest_milestones_and_dependencies(conn, meta, result_dir)
                mc_count = _ingest_milestone_commits_optional(conn, meta, result_dir)
                # Update counts based on actual ingested rows
                conn.execute(
                    "UPDATE experiments SET milestones_count=?, dependencies_count=? WHERE trial=?",
                    (ms_count, dep_count, meta.get("trial")),
                )
                conn.commit()
                logger.info(
                    "SQLite upsert ok: trial=%s, milestones=%d, dependencies=%d, milestone_commits=%d",
                    meta.get("trial"),
                    ms_count,
                    dep_count,
                    mc_count,
                )
                return
        except sqlite3.OperationalError as e:
            last_err = e
            if "database is locked" in str(e).lower():
                time.sleep(0.2 * attempts)
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    if last_err:
        raise last_err
