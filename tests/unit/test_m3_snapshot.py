"""Tests for the snapshot verb — copy-backup of recipe files before surgical /
validate_only, with a --restore convenience mode.

0 = ok · 1 = input invalid · 2 = snapshot missing (on restore).
"""

from deepcommit.harness.main_orch import context as ctx
from deepcommit.harness.main_orch.verbs import snapshot as snap

from . import m3_fixtures as fx


def _recipe(ws, mid, name, text):
    p = ws / "dockerfiles" / mid / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_snapshot_copies_recipe_and_build_status(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    _recipe(ws, "M1", "Dockerfile", "FROM base\n")
    _recipe(ws, "M1", "test_config.json", "{}")
    fx.write_build_status(ws, "M1")
    report, code = snap.run_snapshot(ws, round_n=2, seq=5, mids=["M1"])
    assert code == ctx.OK
    snap_dir = ws / "_orch_runs" / "round_002" / "snapshots" / "seq_05_pre" / "M1"
    assert (snap_dir / "Dockerfile").read_text(encoding="utf-8") == "FROM base\n"
    assert (snap_dir / "build_status.json").exists()
    assert "Dockerfile" in report["per_mid"]["M1"]["copied"]


def test_snapshot_only_copies_present_files(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    _recipe(ws, "M1", "Dockerfile", "FROM base\n")  # no test_config / build_status
    report, code = snap.run_snapshot(ws, round_n=1, seq=1, mids=["M1"])
    assert code == ctx.OK
    assert report["per_mid"]["M1"]["copied"] == ["Dockerfile"]


def test_restore_reverses_snapshot(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    dockerfile = _recipe(ws, "M1", "Dockerfile", "ORIGINAL\n")
    snap.run_snapshot(ws, round_n=1, seq=2, mids=["M1"])
    dockerfile.write_text("MODIFIED\n", encoding="utf-8")  # surgical edit
    report, code = snap.run_snapshot(ws, round_n=1, seq=2, mids=["M1"], restore=True)
    assert code == ctx.OK
    assert dockerfile.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert "Dockerfile" in report["per_mid"]["M1"]["restored"]


def test_restore_missing_snapshot_returns_violation(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    _recipe(ws, "M1", "Dockerfile", "X\n")
    report, code = snap.run_snapshot(ws, round_n=9, seq=9, mids=["M1"], restore=True)
    assert code == ctx.VIOLATION
    assert "missing" in report["per_mid"]["M1"]["error"].lower()


def test_snapshot_requires_mids(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    import pytest

    with pytest.raises(ctx.InputError):
        snap.run_snapshot(ws, round_n=1, seq=1, mids=[])


def test_main_snapshot_then_restore_roundtrip(tmp_path):
    ws = fx.base_workspace(tmp_path, mids=("M1",))
    dockerfile = _recipe(ws, "M1", "Dockerfile", "V1\n")
    assert snap.main(["--workspace", str(ws), "--round", "3", "--seq", "7", "--mids", "M1"]) == ctx.OK
    dockerfile.write_text("V2\n", encoding="utf-8")
    assert snap.main(["--workspace", str(ws), "--round", "3", "--seq", "7", "--mids", "M1", "--restore"]) == ctx.OK
    assert dockerfile.read_text(encoding="utf-8") == "V1\n"


def test_main_missing_workspace_returns_input_invalid():
    assert snap.main(["--workspace", "/no/such/ws", "--round", "1", "--seq", "1", "--mids", "M1"]) == ctx.INPUT_INVALID
