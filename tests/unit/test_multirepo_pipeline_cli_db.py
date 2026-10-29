"""Multi-repo pipeline wiring: CLI functions, models, stage 3 candidates, coverage union, DB migration."""

import csv
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from deepcommit.cli import workspace as cli_ws
from deepcommit.multirepo import WorkspaceError
from deepcommit.pipeline.models import DependencyEdgeModel, MilestoneModel
from deepcommit.pipeline.stages import upload
from deepcommit.pipeline.stages.analysis import analyze_multirepo
from deepcommit.pipeline.stages_multi.stage3_dependency_inference import Stage3DependencyInference
from tests.unit.test_multirepo_core import data_root, manifest  # noqa: F401  (fixtures)


@pytest.fixture
def manifest_path(tmp_path):
    p = tmp_path / "stack_v1.workspace.yaml"
    p.write_text(yaml.safe_dump(manifest()))
    return p


def _write_milestones(path: Path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "title", "commits"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- CLI


def test_validate_reports_missing_phase1(manifest_path, tmp_path, capsys):
    ws, warnings = cli_ws.validate_workspace(manifest_path, tmp_path / "empty")
    assert ws.names == ["protocol", "server"]
    assert len(warnings) == 4 and "Phase-1 output missing" in warnings[0]
    assert cli_ws.run_validate(manifest_path, tmp_path / "empty") == 0
    assert "WARNING" in capsys.readouterr().out


def test_validate_rejects_bad_manifest(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"workspace_id": "x", "repos": []}))
    with pytest.raises(WorkspaceError):
        cli_ws.validate_workspace(bad)


def test_merge_command(manifest_path, data_root, capsys):  # noqa: F811
    res = cli_ws.run_merge(manifest_path, data_root)
    out = capsys.readouterr().out
    assert res.xrepo_hard >= 3 and "xrepo hard" in out
    assert (res.out_dir / "repos.json").exists() and (res.out_dir / "commit_repos.json").exists()
    _, warnings = cli_ws.validate_workspace(manifest_path, data_root)
    assert not [w for w in warnings if "Phase-1" in w]


def test_member_repo_spec(manifest_path):
    ws = cli_ws.load_workspace(manifest_path)
    assert cli_ws.member_repo_spec(ws.get("server")) == "acme_server:v2.0_v2.1"


# ---------------------------------------------------------------- models


def test_models():
    assert DependencyEdgeModel(prerequisite_id="M1", dependant_id="M2", type="XREPO").type == "XREPO"
    with pytest.raises(ValueError):
        DependencyEdgeModel(prerequisite_id="M1", dependant_id="M2", type="BOGUS")
    assert MilestoneModel(id="M1", title="t").repos == ""
    assert MilestoneModel(id="M1", title="t", repos="a;b").repos == "a;b"


# ---------------------------------------------------------------- stage 3


def test_xrepo_milestone_edges_lift():
    dag = {
        "edges_dag": [
            {"source": "aaaaaaa", "target": "aaaaaab"},
            {"source": "aaaaaaa", "target": "bbbbbbb", "xrepo": True, "confidence": 0.9, "evidence": ["e"]},
        ],
        "edges_soft": [
            {"source": "aaaaaab", "target": "bbbbbbc", "confidence": 0.4},
            {"source": "aaaaaaa", "target": "bbbbbbb", "confidence": 0.5},  # same pair as hard -> dropped
        ],
    }
    mc = {"M1": ["aaaaaaa", "aaaaaab"], "M2": ["bbbbbbb"], "M3": ["bbbbbbc"]}
    hard, soft = Stage3DependencyInference._xrepo_milestone_edges(dag, mc)
    assert [(h["source_milestone"], h["target_milestone"]) for h in hard] == [("M1", "M2")]
    assert [(h["source_milestone"], h["target_milestone"]) for h in soft] == [("M1", "M3")]


def _stage3(tmp_path, multi, dag, rows):
    ms, mcf, pre = tmp_path / "m.csv", tmp_path / "main.jsonl", tmp_path / "pre.json"
    _write_milestones(ms, rows)
    mcf.write_text("".join(json.dumps({"sha": c + "0" * 33}) + "\n" for r in rows for c in r["commits"].split(";")))
    dagp = tmp_path / "dag.json"
    dagp.write_text(json.dumps(dag))
    inputs = {
        "milestone_csv": ms,
        "main_commits": mcf,
        "commit_dag": dagp,
        "pre_selected_candidates": pre,
        "symbol": tmp_path / "none.jsonl",
    }
    saved = {}
    logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        save_json=lambda data, full_path, archive=False: saved.update(data=data),
    )
    stage = Stage3DependencyInference.__new__(Stage3DependencyInference)
    stage.ctx = SimpleNamespace(get_input=lambda n: str(inputs[n]), is_multi_repo=multi)
    stage.logger = logger
    return stage, saved


def test_stage3_candidates_multi_and_single(tmp_path):
    rows = [
        {"id": "M1", "title": "a", "commits": "aaaaaaa"},
        {"id": "M2", "title": "b", "commits": "bbbbbbb"},
        {"id": "M3", "title": "c", "commits": "bbbbbbc"},
    ]
    dag = {
        "edges_dag": [{"source": "aaaaaaa", "target": "bbbbbbb", "xrepo": True, "confidence": 0.9}],
        "edges_soft": [{"source": "aaaaaaa", "target": "bbbbbbc", "confidence": 0.4}],
    }
    stage, saved = _stage3(tmp_path, True, dag, rows)
    stage._save_pre_selected_candidates([["M2", "M3"]])
    cands = saved["data"]["candidates"]
    assert [(c["source_milestone"], c["target_milestone"]) for c in cands] == [("M1", "M2"), ("M2", "M3"), ("M1", "M3")]
    assert cands[0]["type"] == "XREPO" and cands[0]["inferred_from"] == "xrepo" and cands[0]["pre_selected"]
    assert cands[2]["class"] == "soft" and cands[2]["rank"] == 3
    assert saved["data"]["metadata"]["xrepo_hard"] == 1

    stage, saved = _stage3(tmp_path, False, dag, rows)
    stage._save_pre_selected_candidates([["M2", "M3"]])
    assert [c["source_milestone"] for c in saved["data"]["candidates"]] == ["M2"]
    assert "xrepo_hard" not in saved["data"]["metadata"]


# ---------------------------------------------------------------- coverage / validity


def test_coverage_union_and_warnings(manifest_path, data_root, tmp_path):  # noqa: F811
    res = cli_ws.run_merge(manifest_path, data_root)
    ms = tmp_path / "milestones.csv"
    _write_milestones(
        ms,
        [
            {"id": "M1", "title": "proto", "commits": "aaaaaaa;aaaaaab"},
            {"id": "M2", "title": "srv", "commits": "bbbbbbb;bbbbbbc"},
        ],
    )
    out = analyze_multirepo(ms, res.out_dir, res.merged_dir / "main_commits.jsonl", res.merged_dir / "commit_dag.json")
    assert out["per_repo_coverage"]["protocol"]["percentage"] == 100.0
    assert out["per_repo_coverage"]["server"]["covered"] == 2 and out["per_repo_coverage"]["server"]["total"] == 3
    assert out["per_repo_coverage"]["server"]["uncovered"] == ["bbbbbbd"]
    assert out["union_coverage"] == {"total": 5, "covered": 4, "percentage": 80.0}
    assert out["milestone_repos"] == {"M1": ["protocol"], "M2": ["server"]}
    # both milestones are single-repo and a hard edge links them -> warnings for each
    assert {w["milestone"] for w in out["warnings"]} == {"M1", "M2"}

    _write_milestones(ms, [{"id": "M1", "title": "all", "commits": "aaaaaaa;aaaaaab;bbbbbbb;bbbbbbc;bbbbbbd"}])
    out = analyze_multirepo(ms, res.out_dir, res.merged_dir / "main_commits.jsonl", res.merged_dir / "commit_dag.json")
    assert out["warnings"] == [] and out["milestone_repos"]["M1"] == ["protocol", "server"]


# ---------------------------------------------------------------- DB


def _legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE experiments (trial TEXT PRIMARY KEY, repo_id TEXT);
        CREATE TABLE milestones (trial TEXT NOT NULL, milestone_id TEXT NOT NULL, title TEXT,
                                 PRIMARY KEY (trial, milestone_id));
        INSERT INTO milestones VALUES ('old', 'M1', 't');
        """
    )
    conn.commit()
    return conn


def test_db_migration_keeps_old_rows(tmp_path):
    conn = _legacy_db(tmp_path / "t.sqlite")
    upload._ensure_schema(conn)
    upload._ensure_schema(conn)  # idempotent
    cols = {r[1] for r in conn.execute("PRAGMA table_info(milestones)")}
    assert "repos" in cols
    assert conn.execute("SELECT trial, repos FROM milestones").fetchall() == [("old", None)]


def test_upload_multi_repo_rows(manifest_path, data_root, tmp_path):  # noqa: F811
    res = cli_ws.run_merge(manifest_path, data_root)
    result = tmp_path / "result"
    result.mkdir()
    with open(result / "milestones.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "title", "commits", "repos"])
        w.writeheader()
        w.writerow({"id": "M1", "title": "x", "commits": "aaaaaaa;bbbbbbb", "repos": "protocol;server"})
    (result / "milestone_commits.csv").write_text("milestone_id,commit_sha\nM1,aaaaaaa\nM1,bbbbbbb\n")
    meta = {"trial": "t1", "repo_id": "stack_v1"}
    db = str(tmp_path / "trials.sqlite")
    upload._upsert_to_sqlite(
        db_path=db, meta=meta, result_dir=result, logger=logging.getLogger("t"), workspace_dir=res.out_dir
    )
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT repo_id FROM experiments WHERE trial='t1'").fetchone() == ("stack_v1",)
    assert conn.execute("SELECT repos FROM milestones").fetchone() == ("protocol;server",)
    assert dict(conn.execute("SELECT commit_sha, repo_id FROM milestone_commits")) == {
        "aaaaaaa": "protocol",
        "bbbbbbb": "server",
    }

    # single repo: repo_id stays the trial repo_id
    upload._upsert_to_sqlite(
        db_path=db, meta={"trial": "t2", "repo_id": "o_r"}, result_dir=result, logger=logging.getLogger("t")
    )
    assert {r[0] for r in conn.execute("SELECT repo_id FROM milestone_commits WHERE trial='t2'")} == {"o_r"}


# ---------------------------------------------------------------- enrichment across checkouts


def _git_commit(repo: Path, rel: str) -> str:
    import subprocess

    repo.mkdir(parents=True)
    env = {
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@x",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@x",
        "PATH": __import__("os").environ["PATH"],
    }
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("l1\nl2\n")
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "c"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def test_enrichment_routes_commits_to_checkouts(tmp_path):
    from deepcommit.multirepo.resolver import RepoInfo, WorkspaceResolver
    from deepcommit.pipeline.stages.milestone_enrichment import run_milestone_enrichment

    sha_a = _git_commit(tmp_path / "repo_data" / "protocol", "protobufs/p.proto")
    sha_b = _git_commit(tmp_path / "repo_data" / "server", "src/x.rs")
    repos = {
        "protocol": RepoInfo("protocol", str(tmp_path / "repo_data" / "protocol"), ["protobufs/"], [], []),
        "server": RepoInfo("server", str(tmp_path / "repo_data" / "server"), ["src/"], [], []),
    }
    resolver = WorkspaceResolver(repos, {sha_a[:7]: "protocol", sha_b[:7]: "server"}, multi=True)
    ms = tmp_path / "milestones.csv"
    _write_milestones(ms, [{"id": "M1", "title": "t", "commits": f"{sha_a[:7]};{sha_b[:7]}"}])
    res = run_milestone_enrichment(
        ms, tmp_path / "repo_data", repo_src_dirs=["protocol/protobufs/", "server/src/"], resolver=resolver
    )
    assert res["status"] == "ok" and res["missing_commits"] == []
    row = next(csv.DictReader(open(ms)))
    assert row["repos"] == "protocol;server" and row["src_loc"] == "4"
    assert row["touched_src_files"] == "protocol/protobufs/p.proto;server/src/x.rs"
