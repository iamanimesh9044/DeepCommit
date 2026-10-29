"""Multi-repo routing of the git / DAG tools (Stages 1-4) against tiny real git repos."""

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from deepcommit.multirepo import WorkspaceResolver, merge_workspace, parse_workspace
from deepcommit.tools.commit_analysis import calculate_commits_src_loc
from deepcommit.tools.conflict_analysis.milestone_dependency_analyzer import MilestoneDependencyAnalyzer
from deepcommit.tools.conflict_analysis.milestone_internal_conflict_analyzer import analyze_all_milestones
from deepcommit.tools.milestone_analysis import analyze_milestone_size_balance
from deepcommit.tools.milestone_decomposition.commit_split_verifier import verify_commit_split_equivalence
from deepcommit.tools.repo_entity import repo_entity_retriever as analysis
from deepcommit.tools.repo_entity.routed import RoutedEntityTools
from deepcommit.utils.data.commit_dag_loader import CommitDAGLoader

from .test_multirepo_core import CONSUMER_PATCH, PROTO_PATCH, manifest, write_repo

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@x.io",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@x.io",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_ENV).stdout.strip()


def commit_files(repo: Path, files: dict, message: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Two real repos (protocol: protobufs/, server: src/ + tests/), preprocessed stubs, merged workspace."""
    root = tmp_path / "data"
    proto = root / "github_data" / "repos" / "acme_protocol"
    server = root / "github_data" / "repos" / "acme_server"
    init_repo(proto)
    init_repo(server)

    commit_files(proto, {"README": "r\n"}, "root")  # LocalGitRepo.get_commit_loc mis-handles root commits
    commit_files(server, {"README": "r\n"}, "root")
    c1 = commit_files(proto, {"protobufs/points.proto": "a\nb\nc\nd\ne\n"}, "proto one")
    c2 = commit_files(proto, {"protobufs/points.proto": "a\nb\nc\nd\ne\nf\n"}, "proto two")
    git(proto, "tag", "v1.0", c1)
    s1 = commit_files(server, {"src/expr.rs": "x\ny\nz\n", "tests/t.rs": "1\n2\n3\n4\n"}, "server one")
    s2 = commit_files(server, {"src/other.rs": "o\n"}, "server two")
    # s3 touches two files; its blame-old-lines in expr.rs point at s1
    s3 = commit_files(server, {"src/expr.rs": "x\nY\nz\n", "src/b.rs": "b1\nb2\n"}, "server three")
    short = lambda sha: sha[:7]  # noqa: E731
    c1s, c2s, s1s, s2s, s3s = map(short, (c1, c2, s1, s2, s3))

    write_repo(
        root,
        {"pre": "acme_protocol_v1.0_v1.1", "files": "protobufs/points.proto"},
        [
            {"sha": c1s, "files": ["protobufs/points.proto"], "time": "2026-10-01T00:00:00+00:00"},
            {"sha": c2s, "files": ["protobufs/points.proto"], "time": "2026-10-02T00:00:00+00:00"},
        ],
        [(c1s, c2s, "protobufs/points.proto")],
        {c1s: PROTO_PATCH},
    )
    write_repo(
        root,
        {"pre": "acme_server_v2.0_v2.1", "files": "src/expr.rs"},
        [
            {"sha": s1s, "files": ["src/expr.rs", "tests/t.rs"], "time": "2026-10-05T00:00:00+00:00", "email": "d@x.io"},
            {"sha": s2s, "files": ["src/other.rs"], "time": "2026-10-06T00:00:00+00:00", "email": "d@x.io"},
            {"sha": s3s, "files": ["src/expr.rs", "src/b.rs"], "time": "2026-10-07T00:00:00+00:00", "email": "d@x.io"},
        ],
        [(s1s, s3s, "src/expr.rs")],
        {s1s: CONSUMER_PATCH},
    )
    ws = parse_workspace(manifest())
    res = merge_workspace(ws, root)
    resolver = WorkspaceResolver.from_dir(res.out_dir)
    monkeypatch.setenv("DEEPCOMMIT_WORKSPACE_DIR", str(res.out_dir))
    return dict(
        root=root, proto=proto, server=server, res=res, resolver=resolver,
        c1=c1s, c2=c2s, s1=s1s, s2=s2s, s3=s3s, s3_full=s3, tmp=tmp_path,
    )


def write_csv(path: Path, rows) -> str:
    lines = ["id,title,category,commits,core_pr_commits,main_commits"]
    for mid, commits in rows:
        c = ";".join(commits)
        lines.append(f"{mid},t{mid},feature,{c},{c},{c}")
    path.write_text("\n".join(lines) + "\n")
    return str(path)


# ---------------------------------------------------------------- src_loc


def test_src_loc_routes_by_commit(ws_env):
    e = ws_env
    out = calculate_commits_src_loc([e["c1"], e["s1"], "deadbee"], repo_path="unused", resolver=e["resolver"])
    by = {c["sha"]: c for c in out["commits"]}
    assert by[e["c1"]]["total"] == 5 and by[e["c1"]]["repo"] == "protocol"
    # tests/t.rs is excluded by the server's test_dirs, src/expr.rs counts
    assert by[e["s1"]]["total"] == 3 and by[e["s1"]]["repo"] == "server"
    assert by["deadbee"]["exists"] is False
    assert out["valid_commits"] == 2 and out["invalid_commits"] == 1 and out["total_src_loc"] == 8
    assert out["path_prefix"] == ["protocol/protobufs/", "server/src/"]


def test_src_loc_single_repo_unchanged(ws_env):
    e = ws_env
    kw = dict(commits=[e["c1"], e["c2"]], repo_path=str(e["proto"]), path_prefix=["protobufs/"])
    old = calculate_commits_src_loc(**kw)
    single = WorkspaceResolver.single(str(e["proto"]), ["protobufs/"])
    assert calculate_commits_src_loc(**kw, resolver=single) == old
    assert "repo" not in old["commits"][0]


def test_size_balance_routes_and_single_unchanged(ws_env):
    e = ws_env
    csv_path = write_csv(e["tmp"] / "m.csv", [("M1", [e["c1"], e["s1"]]), ("M2", [e["c2"], e["s2"]])])
    out = analyze_milestone_size_balance(csv_path, repo_path="unused", resolver=e["resolver"])
    # M1 = 5 + 3, M2 = 1 + 1 (src/other.rs)
    assert out["loc_stats"]["loc_max"] == 8 and out["loc_stats"]["loc_min"] == 2
    assert out["loc_stats"]["path_prefix"] == ["protocol/protobufs/", "server/src/"]

    csv1 = write_csv(e["tmp"] / "m1.csv", [("A", [e["c1"]]), ("B", [e["c2"]])])
    kw = dict(csv_file=csv1, repo_path=str(e["proto"]), repo_src_dir=["protobufs/"])
    single = WorkspaceResolver.single(str(e["proto"]), ["protobufs/"])
    assert analyze_milestone_size_balance(**kw, resolver=single) == analyze_milestone_size_balance(**kw)


# ---------------------------------------------------------------- repo_entity tools


def test_commit_context_routes_and_prefixes(ws_env):
    e = ws_env
    tools = RoutedEntityTools(e["resolver"], str(e["res"].merged_dir))
    pc = tools.get_commit_context(sha=e["c1"], expand=["files"])
    sc = tools.get_commit_context(sha=e["s1"], expand=["files"])
    assert pc["repo"] == "protocol" and [f["path"] for f in pc["files"]] == ["protocol/protobufs/points.proto"]
    assert sc["repo"] == "server" and sorted(f["path"] for f in sc["files"]) == ["server/src/expr.rs", "server/tests/t.rs"]
    assert pc["sha"].startswith(e["c1"]) and sc["sha"].startswith(e["s1"])


def test_commit_context_single_repo_golden(ws_env):
    e = ws_env
    single = WorkspaceResolver.single(str(e["proto"]), ["protobufs/"])
    data_dir = str(e["res"].merged_dir)
    got = RoutedEntityTools(single, data_dir).get_commit_context(sha=e["c1"], expand=["files", "stats"])
    want = analysis.get_commit_context(repo_path=str(e["proto"]), data_dir=data_dir, sha=e["c1"], expand=["files", "stats"])
    assert got == want and "repo" not in got


def test_commits_diff_infers_repo_from_sha_and_prefix(ws_env):
    e = ws_env
    tools = RoutedEntityTools(e["resolver"], str(e["res"].merged_dir))
    d = tools.get_commits_diff(base_ref=e["c1"], head_ref=e["c2"], expand=["files"])
    assert d["repo"] == "protocol" and d["files"][0]["path"] == "protocol/protobufs/points.proto"
    d2 = tools.get_commits_diff(base_ref=f"protocol:{e['c1']}", head_ref="protocol:" + e["c2"], expand=["files"],
                                paths_glob=["protocol/protobufs/"])
    assert d2["repo"] == "protocol"
    with pytest.raises(ValueError, match="repo"):
        tools.get_commits_diff(base_ref="v1.0", head_ref="main")


def test_release_issue_pr_need_repo(ws_env):
    e = ws_env
    tools = RoutedEntityTools(e["resolver"], str(e["res"].merged_dir))
    with pytest.raises(ValueError, match="repo"):
        tools.get_issue_context(issue_number=1)
    with pytest.raises(ValueError, match="repo"):
        tools.get_pr_context(pr_number=1)
    # tag only exists in protocol -> inferred
    assert tools.get_release_context(tag="v1.0")["repo"] == "protocol"


def test_search_entities_merges_repos(ws_env):
    e = ws_env
    tools = RoutedEntityTools(e["resolver"], str(e["res"].merged_dir))
    out = tools.search_entities(types=["commit"], query="one", expand=["commit.files"])
    repos = {h["repo"]: h for h in out["hits"]}
    assert set(repos) == {"protocol", "server"}
    assert repos["protocol"]["files"] == ["protocol/protobufs/points.proto"]


# ---------------------------------------------------------------- DAG loader / analyzers


def merged_dag_with_soft(e) -> str:
    """Merged DAG copy with one extra soft edge (c2 -> s2) that must never be enforced."""
    dag = json.loads((e["res"].merged_dir / "commit_dag.json").read_text())
    dag["edges_soft"] = dag.get("edges_soft", []) + [
        {"source": e["c2"], "target": e["s2"], "edge_types": ["xrepo_time"], "confidence": 0.3, "evidence": ["t"]}
    ]
    out = e["tmp"] / "dag_soft.json"
    out.write_text(json.dumps(dag))
    return str(out)


def test_loader_soft_edges_ignored_and_helpers(ws_env):
    e = ws_env
    loader = CommitDAGLoader(merged_dag_with_soft(e))
    assert loader.repo_of(e["s1"]) == "server" and loader.repo_of(e["c1"]) == "protocol"
    assert [(x["source"], x["target"]) for x in loader.get_soft_edges()] == [(e["c2"], e["s2"])]
    assert not loader.has_dependency_path(e["c2"], e["s2"])
    assert not loader.has_direct_dependency(e["c2"], e["s2"])
    assert not loader.is_xrepo_edge(e["c2"], e["s2"])
    assert e["s2"] not in loader.reverse_edges_map.get(e["c2"], [])
    xedges = [x for x in json.loads((e["res"].merged_dir / "commit_dag.json").read_text())["edges_dag"] if x.get("xrepo")]
    assert xedges, "fixture should yield a hard cross-repo edge"
    x = xedges[0]
    assert loader.is_xrepo_edge(x["source"], x["target"])


def test_cross_repo_commit_group_dependency(ws_env):
    e = ws_env
    analyzer = MilestoneDependencyAnalyzer(
        commit_dag_path=str(e["res"].merged_dir / "commit_dag.json"),
        milestone_csv_path=write_csv(e["tmp"] / "d.csv", [("M1", [e["c1"], e["c2"]]), ("M2", [e["s1"], e["s3"]])]),
        resolver=e["resolver"],
    )
    # server commit s1 consumes the protocol contract added in c1 (hard xrepo edge). The analyzer's flag naming is
    # "closure(a) hits b" with closure(src) = dependents, so the contract group is the one whose flag is set.
    a_flag, b_flag, details = analyzer.check_dependency_between_commits([e["c1"], e["c2"]], [e["s1"], e["s3"]])
    assert a_flag and not b_flag
    assert {(d["from"], d["to"]) for d in details["a_to_b_dependencies"]} >= {(e["c1"], e["s1"])}


def test_conflict_analyzer_cross_repo_milestone(ws_env):
    e = ws_env
    csv_path = write_csv(e["tmp"] / "c.csv", [("M1", [e["c1"], e["s1"], "deadbee"])])
    out = analyze_all_milestones(
        repo_path="does-not-exist",
        milestone_csv_path=csv_path,
        commit_dag_path=str(e["res"].merged_dir / "commit_dag.json"),
        resolver=e["resolver"],
    )
    m = out["milestones"]["M1"]
    assert m["valid_commits"] == 2 and m["invalid_commits"] == ["deadbee"] and m["conflict_count"] == 0


def test_split_online_merge_runs_in_commit_repo(ws_env):
    e = ws_env
    server = e["server"]
    git(server, "checkout", "-q", "-b", f"split-{e['s3']}", f"{e['s3_full']}^")
    a = commit_files(server, {"src/b.rs": "b1\nb2\n"}, "split a")
    b = commit_files(server, {"src/expr.rs": "x\nY\nz\n"}, "split b")
    git(server, "checkout", "-q", "main")
    meta_dir = e["tmp"] / "splits" / f"split-{e['s3']}"
    meta_dir.mkdir(parents=True)
    (meta_dir / "METADATA.json").write_text(
        json.dumps(
            {
                "original_commit": {"short_hash": e["s3"], "hash": e["s3_full"]},
                "split_result": {
                    "src_commits": [
                        {"short_hash": a[:7], "hash": a, "files": ["src/b.rs"]},
                        {"short_hash": b[:7], "hash": b, "files": ["src/expr.rs"]},
                    ]
                },
            }
        )
    )
    dag = json.loads((e["res"].merged_dir / "commit_dag.json").read_text())
    dag["edges_dag"].append(
        {"source": e["c1"], "target": e["s3"], "files_ref": [], "edge_types": ["xrepo_contract"], "xrepo": True,
         "confidence": 0.9, "evidence": []}
    )
    dag_path = e["tmp"] / "dag_split.json"
    dag_path.write_text(json.dumps(dag))

    loader = CommitDAGLoader(str(dag_path))
    n = loader.apply_split_metadata(str(e["tmp"] / "splits"), resolver=e["resolver"])
    assert n == 1 and e["s3"] not in loader.nodes
    assert loader.repo_of(a[:7]) == "server" and loader.repo_of(b[:7]) == "server"
    # s1 -> s3 (src/expr.rs) is remapped by blame to the split that edits expr.rs, in the server checkout
    assert loader.has_direct_dependency(e["s1"], b[:7])
    # the hard cross-repo edge survives the remap and stays flagged
    assert any(loader.is_xrepo_edge(e["c1"], t) for t in (a[:7], b[:7]))
    assert not loader.is_xrepo_edge(e["s1"], b[:7])


def test_split_verifier_uses_commit_repo(ws_env):
    e = ws_env
    res = verify_commit_split_equivalence("does-not-exist", e["s3_full"], [e["s3_full"]], resolver=e["resolver"])
    assert res["is_equivalent"]


def test_loader_edges_map_single_repo_shape(ws_env):
    """A single-repo DAG (no xrepo keys) keeps the old edges_map entry shape."""
    e = ws_env
    dag = json.loads((e["res"].merged_dir / "commit_dag.json").read_text())
    dag["edges_dag"] = [x for x in dag["edges_dag"] if not x.get("xrepo")]
    p = e["tmp"] / "dag_single.json"
    p.write_text(json.dumps(copy.deepcopy(dag)))
    loader = CommitDAGLoader(str(p))
    for deps in loader.edges_map.values():
        for dep in deps:
            assert set(dep) == {"source", "files_ref"}
    assert loader.get_soft_edges() == loader.dag_data.get("edges_soft", [])

