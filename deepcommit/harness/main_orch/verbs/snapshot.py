"""snapshot — copy-backup of a milestone's recipe files before surgical / validate_only.

M2 recipes are scarce and non-regenerable, so the safe isolation is a pure copy
(no execution). ``--restore`` reverses it. Detection of *when* to restore is the
``preflight`` verb's job (its ``rebuild_crashed`` finding, kept separate so
detection stays deterministic while the restore decision stays with the agent).

Snapshots live at ``_orch_runs/round_NNN/snapshots/seq_XX_pre/{MID}/``.
Exit: 0 ok · 1 input invalid · 2 snapshot missing (on restore).
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any, Sequence

from deepcommit.harness.main_orch import context as ctx

# recipe trio + signals + the build_status that validate_only can destructively overwrite
SNAPSHOT_FILES = (
    "Dockerfile",
    "test_config.json",
    "module2_waivers.json",
    "module2_signals.json",
    "build_status.json",
)


def _snapshot_dir(ws: Path, round_n: int, seq: int) -> Path:
    return ws / "_orch_runs" / f"round_{round_n:03d}" / "snapshots" / f"seq_{seq:02d}_pre"


def run_snapshot(
    workspace: str | Path,
    *,
    round_n: int | None,
    seq: int | None,
    mids: Sequence[str],
    restore: bool = False,
) -> tuple[dict[str, Any], int]:
    ws = ctx.resolve_workspace(workspace)
    if round_n is None or seq is None:
        raise ctx.InputError("--round and --seq are required")
    if not mids:
        raise ctx.InputError("--mids is required (at least one milestone id)")

    snap_dir = _snapshot_dir(ws, round_n, seq)
    per_mid: dict[str, Any] = {}
    code = ctx.OK

    for mid in mids:
        recipe_dir = ctx.dockerfiles_dir(ws) / mid
        mid_snap = snap_dir / mid
        if restore:
            if not mid_snap.is_dir():
                per_mid[mid] = {"restored": [], "error": f"snapshot dir missing: {mid_snap}"}
                code = ctx.VIOLATION
                continue
            restored: list[str] = []
            recipe_dir.mkdir(parents=True, exist_ok=True)
            for name in SNAPSHOT_FILES:
                src = mid_snap / name
                if src.exists():
                    shutil.copy2(src, recipe_dir / name)
                    restored.append(name)
            per_mid[mid] = {"restored": restored, "from": str(mid_snap)}
        else:
            copied: list[str] = []
            for name in SNAPSHOT_FILES:
                src = recipe_dir / name
                if src.exists():
                    mid_snap.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, mid_snap / name)
                    copied.append(name)
            per_mid[mid] = {"snapshot_dir": str(mid_snap), "copied": copied}

    report = {
        "verb": "snapshot",
        "mode": "restore" if restore else "snapshot",
        "workspace": str(ws),
        "round": round_n,
        "seq": seq,
        "snapshot_dir": str(snap_dir),
        "per_mid": per_mid,
        "passed": code == ctx.OK,
    }
    return report, code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m3 snapshot", description="Snapshot / restore recipe files before surgical edits."
    )
    parser.add_argument("--workspace", default=None, help="Harness workspace directory")
    parser.add_argument("--round", type=int, default=None, dest="round_n", help="Round number (NNN)")
    parser.add_argument("--seq", type=int, default=None, help="Event seq (XX) — names snapshots/seq_XX_pre/")
    parser.add_argument("--mids", default=None, help="Comma/space-separated milestone ids")
    parser.add_argument(
        "--restore", action="store_true", help="Reverse-restore from the snapshot instead of taking one"
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path; defaults to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report, code = run_snapshot(
            args.workspace,
            round_n=args.round_n,
            seq=args.seq,
            mids=ctx.parse_id_list(args.mids),
            restore=args.restore,
        )
    except ctx.InputError as exc:
        ctx.emit({"verb": "snapshot", "errors": [str(exc)], "passed": False}, args.output)
        return ctx.INPUT_INVALID
    ctx.emit(report, args.output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
