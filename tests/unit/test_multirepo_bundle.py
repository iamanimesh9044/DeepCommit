"""Cross-repo milestone bundling from hard xrepo edges."""

from deepcommit.multirepo.bundle import BundleParams, bundle_milestones

REPOS = {"s1": "server", "s2": "server", "s3": "server", "s4": "server", "c1": "client", "c2": "client", "c3": "client",
         "r1": "rust"}


def row(mid, *commits, **extra):
    return {"id": mid, "title": f"T{mid}", "category": "Feature", "commits": ";".join(commits), "mini_srs": f"srs {mid}",
            "confidence_score": "0.9", "src_loc": "10", **extra}


def edge(a, b, conf=0.9):
    return {"source": a, "target": b, "xrepo": True, "confidence": conf, "edge_types": ["xrepo_contract"]}


def test_dedicated_pair_is_merged():
    rows = [row("M1", "s1"), row("M2", "c1"), row("M3", "s2")]
    out, rep = bundle_milestones(rows, [edge("s1", "c1")], REPOS)
    ids = [r["id"] for r in out]
    assert ids == ["M1", "M3"] and rep.before == 3 and rep.after == 2
    merged = out[0]
    assert merged["commits"] == "s1;c1" and merged["repos"] == "client;server" and merged["src_loc"] == "20"
    assert "(cross-repo)" in merged["title"] and "[server]" in merged["mini_srs"]
    assert rep.multi_repo_milestones == ["M1"] and rep.merges[0]["into"] == "M1"


def test_release_sync_commit_does_not_glue_everything():
    # one client commit consumes four server features -> diluted share, no merge
    rows = [row("M1", "s1"), row("M2", "s2"), row("M3", "s3"), row("M4", "s4"), row("M5", "c1")]
    edges = [edge(s, "c1") for s in ("s1", "s2", "s3", "s4")]
    out, rep = bundle_milestones(rows, edges, REPOS)
    assert len(out) == 5 and not rep.merges
    assert len(rep.leftover_links) == 4  # reported so stage 3 can add XREPO dependencies


def test_low_confidence_edges_do_not_count():
    rows = [row("M1", "s1"), row("M2", "c1")]
    out, rep = bundle_milestones(rows, [edge("s1", "c1", conf=0.5)], REPOS)
    assert len(out) == 2 and not rep.merges


def test_same_repo_and_non_xrepo_edges_are_ignored():
    rows = [row("M1", "s1"), row("M2", "s2")]
    out, _ = bundle_milestones(rows, [edge("s1", "s2"), {"source": "s1", "target": "s2", "confidence": 1.0}], REPOS)
    assert len(out) == 2


def test_caps_block_merge():
    rows = [row("M1", "s1", "s2", "s3"), row("M2", "c1", "c2")]
    out, rep = bundle_milestones(rows, [edge("s1", "c1")], REPOS, BundleParams(max_commits=4))
    assert len(out) == 2 and not rep.merges
    rows = [row("M1", "s1"), row("M2", "c1"), row("M3", "r1")]
    edges = [edge("s1", "c1"), edge("c1", "r1")]
    out, _ = bundle_milestones(rows, edges, REPOS, BundleParams(max_repos=2))
    assert max(len(r["repos"].split(";")) for r in out) <= 2


def test_columns_union_and_times():
    a = row("M1", "s1", touched_src_files="a.py;b.py", start_time="2026-01-02", end_time="2026-01-03")
    b = row("M2", "c1", touched_src_files="b.py;c.py", start_time="2026-01-01", end_time="2026-01-09")
    out, _ = bundle_milestones([a, b], [edge("s1", "c1")], REPOS)
    m = out[0]
    assert m["touched_src_files"] == "a.py;b.py;c.py" and m["start_time"] == "2026-01-01" and m["end_time"] == "2026-01-09"
    assert m["confidence_score"] == "0.9"
