"""Unit tests for the m3 verb-suite shared context kernel.

context.py owns workspace resolution, atomic ``.tmp``+``os.replace`` CSV/JSON IO,
the unifying backup-on-write convention (``*.bak_round{NNN}_pre``), mtime /
freshness helpers, the canonical/legacy CSV vocabulary readers, and the
JSON-stdout-or-``--output`` emit contract.
"""

import json
import os

import pytest

from deepcommit.harness.main_orch import context as ctx


# ------------------------------------------------------------ workspace resolve
def _make_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "milestones.csv").write_text("id,commits\nM1,a;b\n", encoding="utf-8")
    (ws / "dependencies.csv").write_text("prerequisite_id,dependant_id\n", encoding="utf-8")
    return ws


def test_resolve_workspace_accepts_valid_workspace(tmp_path):
    ws = _make_workspace(tmp_path)
    resolved = ctx.resolve_workspace(ws)
    assert resolved == ws.resolve()


def test_resolve_workspace_rejects_missing_required_file(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws / "dependencies.csv").unlink()
    with pytest.raises(ctx.InputError):
        ctx.resolve_workspace(ws)


def test_resolve_workspace_rejects_non_directory(tmp_path):
    f = tmp_path / "not_a_dir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ctx.InputError):
        ctx.resolve_workspace(f)


# ------------------------------------------------------------------- atomic IO
def test_atomic_write_text_writes_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "out.txt"
    ctx.atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    # no leftover temp files in the directory
    assert [q.name for q in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_text_overwrites_existing(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("old", encoding="utf-8")
    ctx.atomic_write_text(p, "new")
    assert p.read_text(encoding="utf-8") == "new"


def test_write_json_atomic_roundtrips(tmp_path):
    p = tmp_path / "d.json"
    ctx.write_json_atomic(p, {"a": 1, "b": ["x"]})
    assert ctx.read_json(p) == {"a": 1, "b": ["x"]}


def test_read_json_raises_input_error_on_missing(tmp_path):
    with pytest.raises(ctx.InputError):
        ctx.read_json(tmp_path / "nope.json")


def test_write_csv_atomic_roundtrips_fields_and_rows(tmp_path):
    p = tmp_path / "m.csv"
    rows = [{"id": "M1", "commits": "a;b"}, {"id": "M2", "commits": "c"}]
    ctx.write_csv_atomic(p, rows, ["id", "commits"])
    read_rows, fieldnames = ctx.read_csv(p)
    assert fieldnames == ["id", "commits"]
    assert read_rows == rows


# ------------------------------------------------------------------ backup-on-write
def test_backup_file_uses_round_convention(tmp_path):
    p = tmp_path / "milestones.csv"
    p.write_text("id\nM1\n", encoding="utf-8")
    backup = ctx.backup_file(p, 7)
    assert backup.name == "milestones.csv.bak_round007_pre"
    assert backup.read_text(encoding="utf-8") == "id\nM1\n"


# --------------------------------------------------------------- mtime / freshness
def test_newer_than_compares_mtimes(tmp_path):
    new = tmp_path / "new"
    old = tmp_path / "old"
    new.write_text("n", encoding="utf-8")
    old.write_text("o", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert ctx.newer_than(new, old) is True
    assert ctx.newer_than(old, new) is False


def test_newer_than_is_fresh_when_equal_mtime(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    os.utime(a, (1500, 1500))
    os.utime(b, (1500, 1500))
    assert ctx.newer_than(a, b) is True  # equal = same write batch = not stale


def test_mtime_returns_none_for_missing(tmp_path):
    assert ctx.mtime(tmp_path / "ghost") is None


# ------------------------------------------------------- CSV vocabulary readers
def test_load_edges_reads_canonical_header(tmp_path):
    p = tmp_path / "dependencies.csv"
    p.write_text("prerequisite_id,dependant_id,type\nA,B,TEXT\nB,C,FUNC\n", encoding="utf-8")
    assert ctx.load_edges(p) == [("A", "B"), ("B", "C")]


def test_load_edges_accepts_legacy_header(tmp_path):
    p = tmp_path / "dependencies.csv"
    p.write_text("source_id,target_id\nA,B\n", encoding="utf-8")
    assert ctx.load_edges(p) == [("A", "B")]


def test_load_milestone_commits_parses_commits_column(tmp_path):
    p = tmp_path / "milestones.csv"
    p.write_text("id,commits\nM1,a;b\nM2,c\n", encoding="utf-8")
    assert ctx.load_milestone_commits(p) == {"M1": ["a", "b"], "M2": ["c"]}


# ------------------------------------------------------------- frozen prefix read
def test_read_frozen_prefix_reads_state(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws / "_frozen_prefix.json").write_text(
        json.dumps({"frozen_milestones": ["M1"], "frozen_round": 3}), encoding="utf-8"
    )
    fp = ctx.read_frozen_prefix(ws)
    assert fp["frozen_milestones"] == ["M1"]


def test_read_frozen_prefix_defaults_when_absent(tmp_path):
    ws = _make_workspace(tmp_path)
    assert ctx.read_frozen_prefix(ws) == {}


# --------------------------------------------------------------------- emit JSON
def test_emit_writes_json_to_stdout(tmp_path, capsys):
    ctx.emit({"ok": True}, None)
    out = capsys.readouterr().out
    assert json.loads(out) == {"ok": True}


def test_emit_writes_json_to_output_path(tmp_path):
    out = tmp_path / "nested" / "report.json"
    ctx.emit({"ok": True}, out)
    assert json.loads(out.read_text(encoding="utf-8")) == {"ok": True}


# ----------------------------------------------------------------- id-list parse
def test_parse_id_list_splits_comma_and_space():
    assert ctx.parse_id_list("A, B ,C") == ["A", "B", "C"]
    assert ctx.parse_id_list("A B C") == ["A", "B", "C"]
    assert ctx.parse_id_list("") == []
    assert ctx.parse_id_list(None) == []
