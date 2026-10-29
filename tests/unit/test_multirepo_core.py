"""Core multi-repo behaviour: manifest, cross-repo edges, merge, resolver."""

import json
from pathlib import Path

import pytest

from deepcommit.multirepo import (
    CommitKeyCollision,
    MissingPreprocessed,
    WorkspaceError,
    WorkspaceResolver,
    merge_workspace,
    parse_workspace,
    split_path,
)
from deepcommit.multirepo.merge import compute_dag_stats
from deepcommit.multirepo.xrepo import extract_contract_identifiers, glob_match, scan_references, words_of

PROTO_PATCH = """diff --git a/protobufs/points.proto b/protobufs/points.proto
--- a/protobufs/points.proto
+++ b/protobufs/points.proto
@@ -1,3 +1,8 @@
+message AcoshExpression {
+  string payload_cursor = 2;
+  rpc StreamFrobnicate(Req) returns (Resp);
+  FROBNICATE_MODE = 3;
+}
"""

CONSUMER_PATCH = """diff --git a/src/expr.rs b/src/expr.rs
--- a/src/expr.rs
+++ b/src/expr.rs
@@ -1,2 +1,4 @@
+pub fn new_acosh_expression() -> AcoshExpression { AcoshExpression::default() }
+fn stream_frobnicate(c: &Client) { c.stream_frobnicate(); }
diff --git a/src/points.pb.go b/src/points.pb.go
--- a/src/points.pb.go
+++ b/src/points.pb.go
@@ -1 +1,2 @@
+type Zzzunrelated struct{ Payload_cursor int }
"""

PIN_PATCH_TMPL = """diff --git a/go.mod b/go.mod
--- a/go.mod
+++ b/go.mod
@@ -1,2 +1,2 @@
-	github.com/acme/protocol v1.0.0-20260101000000-aaaaaaaaaaaa
+	github.com/acme/protocol v1.1.1-0.20261016215345-{pin}
"""


def manifest(**over):
    data = {
        "workspace_id": "stack_v1",
        "repos": [
            {
                "name": "protocol",
                "owner": "acme",
                "repo": "protocol",
                "role": "contract",
                "start": "v1.0",
                "end": "v1.1",
                "repo_src_dirs": ["protobufs/"],
                "contract_paths": ["**/*.proto"],
            },
            {
                "name": "server",
                "owner": "acme",
                "repo": "server",
                "role": "server",
                "start": "v2.0",
                "end": "v2.1",
                "repo_src_dirs": ["src/"],
                "test_dirs": ["tests/**", "**/*_test.go"],
            },
        ],
    }
    data.update(over)
    return data


# ---------------------------------------------------------------- manifest


def test_parse_ok_and_prefixes():
    ws = parse_workspace(manifest())
    assert ws.is_multi and ws.names == ["protocol", "server"]
    assert ws.prefixed_src_dirs() == ["protocol/protobufs/", "server/src/"]
    # root-anchored glob gets the repo prefix, ** glob stays portable
    assert ws.prefixed_test_dirs() == ["server/tests/**", "**/*_test.go"]
    assert ws.get("server").preprocessed_dirname == "acme_server_v2.0_v2.1"


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda m: m["repos"].append(dict(m["repos"][0])), "duplicate"),
        (lambda m: m["repos"][0].update(role="boss"), "role"),
        (lambda m: m["repos"][0].update(repo_src_dirs=["src/*"]), "glob"),
        (lambda m: m["repos"][0].pop("contract_paths"), "contract_paths"),
        (lambda m: m["repos"][1].update(start="", end=""), "start"),
        (lambda m: m.update(workspace_id="bad id"), "workspace_id"),
        (lambda m: m.update(repos=[]), "repos"),
    ],
)
def test_parse_rejects(mutate, msg):
    m = manifest()
    mutate(m)
    with pytest.raises(WorkspaceError, match=msg):
        parse_workspace(m)


def test_max_five_repos():
    m = manifest()
    m["repos"] = [
        {"name": f"r{i}", "owner": "o", "repo": f"r{i}", "start": "a", "end": "b", "repo_src_dirs": ["src/"]}
        for i in range(6)
    ]
    with pytest.raises(WorkspaceError, match="at most 5"):
        parse_workspace(m)


def test_split_path_longest_name_wins():
    assert split_path("app-web/src/x.ts", ["app", "app-web"]) == ("app-web", "src/x.ts")
    with pytest.raises(KeyError):
        split_path("other/x", ["app"])


# ---------------------------------------------------------------- identifiers / refs


def test_words_and_globs():
    assert words_of("NewExpressionAcosh") == ("new", "expression", "acosh")
    assert words_of("payload_cursor") == ("payload", "cursor")
    assert glob_match("a/b/c.proto", ["**/*.proto"]) and glob_match("x.proto", ["**/*.proto"])
    assert not glob_match("a/b.go", ["**/*.proto"])


def test_extract_contract_identifiers_filters_generic():
    ids = extract_contract_identifiers(PROTO_PATCH, ["**/*.proto"])
    assert ("acosh", "expression") in ids and ("stream", "frobnicate") in ids and ("frobnicate", "mode") in ids
    assert ("payload", "cursor") in ids
    # not a contract file -> nothing
    assert extract_contract_identifiers(PROTO_PATCH, ["**/*.yaml"]) == set()


def test_scan_references_only_workspace_repos():
    ws = parse_workspace(manifest())
    text = "Requires acme/protocol#809, see https://github.com/acme/server/pull/12 and other/thing#5"
    assert scan_references(text, ws) == [("protocol", 809), ("server", 12)]


# ---------------------------------------------------------------- fixtures for merge


def write_repo(root: Path, spec, commits, edges, patches, prs=(), releases=()):
    pre = root / "github_data" / "preprocessed" / spec["pre"]
    (pre / "patches").mkdir(parents=True)
    (pre / "raw_data").mkdir()
    rows, raws, nodes = [], [], {}
    for c in commits:
        rows.append(
            {
                "sha": c["sha"],
                "message": c.get("message", "msg"),
                "merged_via_pr": c.get("pr"),
                "files_changed": [{"path": p, "additions": 1, "deletions": 0} for p in c["files"]],
                "commit_dag_stat": {},
            }
        )
        raws.append(
            {
                "sha": c["sha"] + "0" * 33,
                "author": {"name": "A", "email": c.get("email", "a@x.io"), "time": c["time"]},
                "committer": {"time": c["time"]},
            }
        )
        nodes[c["sha"]] = {"subject": c.get("message", "msg"), "layer": 0, "in_degree": 0, "out_degree": 0,
                           "ancestors_count": 0, "descendants_count": 0}
    for sha, text in patches.items():
        (pre / "patches" / f"{sha}.patch").write_text(text)
    (pre / "main_commits.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (pre / "raw_data" / "main_commits.jsonl").write_text("\n".join(json.dumps(r) for r in raws) + "\n")
    (pre / "commit_dag.json").write_text(
        json.dumps({"meta": {"dag_info": {"detection_fuzz": 3}}, "nodes": nodes,
                    "edges_dag": [{"source": s, "target": t, "files_ref": [f], "edge_types": ["modify_overlap"]}
                                  for s, t, f in edges], "layers": {}, "topo_order": []})
    )
    (pre / "pull_requests.jsonl").write_text("\n".join(json.dumps(p) for p in prs) + ("\n" if prs else ""))
    (pre / "releases.jsonl").write_text("\n".join(json.dumps(r) for r in releases) + ("\n" if releases else ""))
    (pre / "codemaat_analysis.json").write_text(
        json.dumps({"hotspots": {"top_20": [{"entity": spec["files"], "n-revs": "3"}], "top_50": [], "all": []},
                    "coupling": {"threshold_40": []}, "entity_churn": {"top_20": [], "top_50": [], "all": []},
                    "temporal_churn": {"all": [{"date": "2026-01-01", "added": "5", "deleted": "1"}]}})
    )


@pytest.fixture
def data_root(tmp_path):
    write_repo(
        tmp_path,
        {"pre": "acme_protocol_v1.0_v1.1", "files": "protobufs/points.proto"},
        [
            {"sha": "aaaaaaa", "files": ["protobufs/points.proto"], "time": "2026-10-01T00:00:00+00:00", "pr": 809},
            {"sha": "aaaaaab", "files": ["protobufs/points.proto"], "time": "2026-10-02T00:00:00+00:00"},
        ],
        [("aaaaaaa", "aaaaaab", "protobufs/points.proto")],
        {"aaaaaaa": PROTO_PATCH},
        prs=[{"number": 809, "title": "acosh", "body": "", "merge_commit_sha": "aaaaaaa0", "pr_commits": "[]"}],
    )
    write_repo(
        tmp_path,
        {"pre": "acme_server_v2.0_v2.1", "files": "src/expr.rs"},
        [
            {"sha": "bbbbbbb", "files": ["src/expr.rs"], "time": "2026-10-05T00:00:00+00:00", "email": "dev@x.io"},
            {"sha": "bbbbbbc", "files": ["src/other.rs"], "time": "2026-10-06T00:00:00+00:00", "email": "dev@x.io",
             "message": "Requires acme/protocol#809", "pr": 7},
            {"sha": "bbbbbbd", "files": ["src/pin.rs"], "time": "2026-10-07T00:00:00+00:00"},
        ],
        [("bbbbbbb", "bbbbbbc", "src/expr.rs")],
        {
            "bbbbbbb": CONSUMER_PATCH,
            "bbbbbbc": "diff --git a/src/other.rs b/src/other.rs\n--- a/src/other.rs\n+++ b/src/other.rs\n+fn x(){}\n",
            "bbbbbbd": PIN_PATCH_TMPL.format(pin="aaaaaaa00000"),
        },
        prs=[{"number": 7, "title": "t", "body": "", "merge_commit_sha": "bbbbbbc0", "pr_commits": "[]"}],
    )
    return tmp_path


# ---------------------------------------------------------------- merge


def test_merge_end_to_end(data_root):
    ws = parse_workspace(manifest())
    res = merge_workspace(ws, data_root)
    m = res.merged_dir
    dag = json.loads((m / "commit_dag.json").read_text())

    cross = {(e["source"], e["target"]): e for e in dag["edges_dag"] if e.get("xrepo")}
    # contract identifiers used in hand-written code -> hard (2 idents + lag bonus >= 0.7)
    assert ("aaaaaaa", "bbbbbbb") in cross and "xrepo_contract" in cross[("aaaaaaa", "bbbbbbb")]["edge_types"]
    # pr link from "Requires acme/protocol#809"
    assert ("aaaaaaa", "bbbbbbc") in cross and "xrepo_pr_link" in cross[("aaaaaaa", "bbbbbbc")]["edge_types"]
    # go.mod pseudo-version pin of a commit that changes the contract -> hard
    assert ("aaaaaaa", "bbbbbbd") in cross and "xrepo_pin" in cross[("aaaaaaa", "bbbbbbd")]["edge_types"]
    assert dag["nodes"]["bbbbbbb"]["repo"] == "server"
    assert dag["nodes"]["aaaaaaa"]["descendants_count"] >= 4
    assert dag["meta"]["workspace"]["xrepo_hard_edges"] == len(cross)
    assert dag["topo_order"].index("aaaaaaa") < dag["topo_order"].index("bbbbbbb")

    # generated file must not have produced evidence on its own
    assert not any("zzzunrelated" in "".join(e["evidence"]).lower() for e in cross.values())

    rows = [json.loads(line) for line in (m / "main_commits.jsonl").read_text().splitlines()]
    by = {r["sha"]: r for r in rows}
    assert by["bbbbbbb"]["repo"] == "server"
    assert by["bbbbbbb"]["files_changed"][0]["path"] == "server/src/expr.rs"
    assert by["bbbbbbc"]["merged_via_pr_key"] == "server#7"
    assert by["bbbbbbb"]["commit_dag_stat"]["in_degree"] >= 1
    assert (m / "patches" / "aaaaaaa.patch").exists()

    cm = json.loads((m / "codemaat_analysis.json").read_text())
    assert {r["entity"] for r in cm["hotspots"]["top_20"]} == {"protocol/protobufs/points.proto", "server/src/expr.rs"}
    assert cm["temporal_churn"]["all"] == [{"date": "2026-01-01", "added": "10", "deleted": "2"}]

    prs = [json.loads(line) for line in (m / "pull_requests.jsonl").read_text().splitlines()]
    assert {p["pr_key"] for p in prs} == {"protocol#809", "server#7"}


def test_resolver_roundtrip(data_root):
    ws = parse_workspace(manifest())
    res = merge_workspace(ws, data_root)
    r = WorkspaceResolver.from_dir(res.out_dir)
    assert r.multi and r.repo_for("AAAAAAA0123") == "protocol" and r.repo_for("bbbbbbc") == "server"
    assert r.repo_path_for("bbbbbbb").endswith("github_data/repos/acme_server")
    assert r.strip_prefix("server/src/x.rs") == ("server", "src/x.rs")
    assert r.group_commits_by_repo(["aaaaaaa", "bbbbbbb", "aaaaaab"]) == {
        "protocol": ["aaaaaaa", "aaaaaab"],
        "server": ["bbbbbbb"],
    }
    assert r.prefixed_src_dirs() == ["protocol/protobufs/", "server/src/"]


def test_single_resolver_is_passthrough():
    r = WorkspaceResolver.single("/r", ["src/"])
    assert not r.multi and r.repo_for("abc1234") == "" and r.repo_path_for("abc1234") == "/r"
    assert r.strip_prefix("src/a.py") == ("", "src/a.py") and r.prefixed_src_dirs() == ["src/"]


def test_collision_is_loud(data_root):
    pre = data_root / "github_data" / "preprocessed" / "acme_server_v2.0_v2.1"
    rows = [json.loads(line) for line in (pre / "main_commits.jsonl").read_text().splitlines()]
    rows[0]["sha"] = "aaaaaaa"
    (pre / "main_commits.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    with pytest.raises(CommitKeyCollision):
        merge_workspace(parse_workspace(manifest()), data_root)


def test_missing_preprocessed(tmp_path):
    with pytest.raises(MissingPreprocessed):
        merge_workspace(parse_workspace(manifest()), tmp_path)


def test_cycle_makes_hard_edge_soft(data_root):
    # intra-repo edge protocol:aaaaaab -> ... plus a server edge that would close a loop through xrepo edge
    pre = data_root / "github_data" / "preprocessed" / "acme_server_v2.0_v2.1"
    dag = json.loads((pre / "commit_dag.json").read_text())
    dag["edges_dag"].append({"source": "bbbbbbb", "target": "bbbbbbd", "files_ref": [], "edge_types": ["modify_overlap"]})
    (pre / "commit_dag.json").write_text(json.dumps(dag))
    # make the protocol's second commit depend on server commit bbbbbbd via an intra edge in protocol repo is impossible,
    # so craft a reverse xrepo link: protocol commit aaaaaab references server PR via message
    prot = data_root / "github_data" / "preprocessed" / "acme_protocol_v1.0_v1.1"
    rows = [json.loads(line) for line in (prot / "main_commits.jsonl").read_text().splitlines()]
    rows[0]["message"] = "uses acme/server#7"
    (prot / "main_commits.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    res = merge_workspace(parse_workspace(manifest()), data_root)
    dag = json.loads((res.merged_dir / "commit_dag.json").read_text())
    # graph stays acyclic regardless and stats are computable
    stats, _, topo = compute_dag_stats(dag["nodes"].keys(), [(e["source"], e["target"]) for e in dag["edges_dag"]])
    assert len(topo) == len(dag["nodes"])
    assert res.xrepo_hard + res.xrepo_soft >= 3


def test_compute_dag_stats_detects_cycle():
    with pytest.raises(ValueError):
        compute_dag_stats(["a", "b"], [("a", "b"), ("b", "a")])


def test_compute_dag_stats_values():
    stats, layers, topo = compute_dag_stats("abcd", [("a", "b"), ("b", "c"), ("a", "c")])
    assert stats["a"]["descendants_count"] == 2 and stats["c"]["ancestors_count"] == 2
    assert layers == {0: ["a", "d"], 1: ["b"], 2: ["c"]} and topo[0] == "a"


# ---------------------------------------------------------------- identifier rarity weighting


def _proto_patch(*names):
    return "diff --git a/p/x.proto b/p/x.proto\n+++ b/p/x.proto\n" + "".join(f"+message {n} {{}}\n" for n in names)


def _use_patch(*names):
    return "diff --git a/src/a.rs b/src/a.rs\n+++ b/src/a.rs\n" + "".join(f"+let _ = {n};\n" for n in names)


def _rec(sha, repo, patch, when="2026-10-0%dT00:00:00+00:00", day=1):
    from datetime import datetime

    from deepcommit.multirepo.xrepo import CommitRec

    return CommitRec(sha=sha, repo=repo, committed_at=datetime.fromisoformat(when % day), patch=lambda p=patch: p)


def test_rare_identifier_is_hard_and_generic_one_is_ignored():
    from deepcommit.multirepo.xrepo import HARD, contract_edges

    ws = parse_workspace(manifest())
    # "Payload" is added by 4 contract commits -> generic; "Acosh" only by one -> distinctive
    contract = [_rec("c000001", "protocol", _proto_patch("Payload", "Acosh"), day=1)] + [
        _rec(f"c00000{i}", "protocol", _proto_patch("Payload"), day=i) for i in range(2, 5)
    ]
    consumers = [_rec("s000001", "server", _use_patch("Payload", "Acosh"), day=6)] + [
        _rec(f"s00000{i}", "server", _use_patch("Payload"), day=6) for i in range(2, 6)
    ]
    edges = contract_edges(ws, {"protocol": contract, "server": consumers})
    by = {(e.source, e.target): e for e in edges}
    assert by[("c000001", "s000001")].edge_class == HARD  # only the rare identifier counted
    assert "acosh" in by[("c000001", "s000001")].evidence[0].lower()
    # a consumer that only uses the generic identifier gets no edge at all
    assert not [e for e in edges if e.target == "s000002"]


def test_single_unique_identifier_beyond_lag_still_hard():
    from deepcommit.multirepo.xrepo import HARD, contract_edges

    ws = parse_workspace(manifest())
    old = _rec("c000001", "protocol", _proto_patch("Acosh"), when="2026-06-%02dT00:00:00+00:00", day=1)
    late = _rec("s000001", "server", _use_patch("Acosh"), when="2026-10-%02dT00:00:00+00:00", day=1)  # ~120 days later
    (edge,) = contract_edges(ws, {"protocol": [old], "server": [late]})
    assert edge.edge_class == HARD and edge.confidence == 0.75  # no lag bonus, still >= 0.7
