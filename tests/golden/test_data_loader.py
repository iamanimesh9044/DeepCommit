"""Golden/characterization tests for utils/data/data_loader.py (P1->P2 handoff loaders).

load_dependency_graph accepts BOTH the canonical ``prerequisite_id/dependant_id``
schema and the legacy ``source_id/target_id`` schema and must produce an IDENTICAL
graph. load_milestones branches on old vs new CSV format. Both print to stdout
(suppressed). Milestone commits come back as ``list(set(...))`` -> order is
hash-seed-dependent, so they are compared as sets.

Values traced from the real functions; normalize_commit_sha = lowercase + 7-char.
"""

import contextlib
import io
from pathlib import Path

import pytest

from deepcommit.utils.data.data_loader import load_dependency_graph, load_milestones


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _load_deps(path, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return load_dependency_graph(path, **kw)


# ---- load_dependency_graph: canonical vs legacy schema -> identical graph ----
def test_deps_prerequisite_schema(tmp_path):
    csv = _write(tmp_path, "deps.csv", "prerequisite_id,dependant_id\nM1,M2\nM1,M3\nM2,M3\n")
    assert dict(_load_deps(csv)) == {"M1": {"M2", "M3"}, "M2": {"M3"}}


def test_deps_legacy_schema_is_identical(tmp_path):
    new = _write(tmp_path, "new.csv", "prerequisite_id,dependant_id\nM1,M2\nM1,M3\n")
    old = _write(tmp_path, "old.csv", "source_id,target_id\nM1,M2\nM1,M3\n")
    assert dict(_load_deps(new)) == dict(_load_deps(old)) == {"M1": {"M2", "M3"}}


def test_deps_return_nodes(tmp_path):
    csv = _write(tmp_path, "deps.csv", "prerequisite_id,dependant_id\nM1,M2\nM2,M3\n")
    deps, nodes = _load_deps(csv, return_nodes=True)
    assert dict(deps) == {"M1": {"M2"}, "M2": {"M3"}}
    assert nodes == {"M1", "M2", "M3"}


def test_deps_missing_columns_raises(tmp_path):
    csv = _write(tmp_path, "bad.csv", "foo,bar\nM1,M2\n")
    with pytest.raises(ValueError, match="prerequisite_id"):
        _load_deps(csv)


def test_deps_missing_file_returns_empty(tmp_path):
    assert dict(_load_deps(str(tmp_path / "nope.csv"))) == {}


# ---- load_milestones: new vs old format ----
def test_milestones_new_format(tmp_path):
    csv = _write(
        tmp_path,
        "m.csv",
        "id,title,start_time,end_time,commits,loc,src_loc,touched_src_files\n"
        "M1,First,2020,2021,AAAAAAA;BBBBBBB,100,80,src/a.py;src/b.py\n",
    )
    m = load_milestones(csv)["M1"]
    assert m["title"] == "First"
    assert (m["start_time"], m["end_time"]) == ("2020", "2021")
    assert (m["loc"], m["src_loc"]) == (100, 80)
    assert m["touched_src_files"] == ["src/a.py", "src/b.py"]
    assert set(m["commits"]) == {"aaaaaaa", "bbbbbbb"}  # normalized + deduped (set order)
    assert "start_date" not in m  # new-format branch omits old-format fields


def test_milestones_old_format(tmp_path):
    csv = _write(
        tmp_path,
        "m.csv",
        "id,title,start_date,end_date,main_commits,core_prs\nM2,Old,2019,2020,CCCCCCC;DDDDDDD,#1;#2\n",
    )
    m = load_milestones(csv)["M2"]
    assert (m["start_date"], m["end_date"]) == ("2019", "2020")
    assert m["core_prs"] == ["#1", "#2"]
    assert set(m["commits"]) == {"ccccccc", "ddddddd"}
    assert "start_time" not in m  # old-format branch


def test_milestones_commits_dedup_and_normalize(tmp_path):
    # same SHA across columns + mixed case/length -> all collapse to one 7-char lowercase sha
    csv = _write(tmp_path, "m.csv", "id,commits,main_commits\nM3,ABCDEF1234;abcdef1234,ABCDEF1234\n")
    assert set(load_milestones(csv)["M3"]["commits"]) == {"abcdef1"}


def test_milestones_missing_id_column_raises(tmp_path):
    csv = _write(tmp_path, "m.csv", "name,commits\nX,AAA\n")
    with pytest.raises(KeyError):
        load_milestones(csv)
