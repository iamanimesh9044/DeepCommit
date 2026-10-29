import json

from deepcommit.harness.utils.xrepo_gates import (
    compute_xrepo_gates,
    cross_repo_validity,
    single_repo_shortcut_check,
)

F2P = ["server::TestA", "sdk::test_b"]


def test_shortcut_detected_and_ok():
    r = single_repo_shortcut_check(
        F2P,
        {
            "server": {"server::TestA": "passed", "sdk::test_b": "failed"},
            "sdk": {"server::TestA": "failed", "sdk::test_b": "passed"},
        },
    )
    assert r["passed"] and r["shortcut_repos"] == [] and r["per_repo"]["server"]["still_failing"] == ["sdk::test_b"]
    r = single_repo_shortcut_check(F2P, {"server": ["server::TestA", "sdk::test_b"], "sdk": {}})
    assert not r["passed"] and r["shortcut_repos"] == ["server"]
    assert not single_repo_shortcut_check([], {"a": {}})["passed"]


def test_cross_repo_validity():
    assert cross_repo_validity(["a", "b"], ["a", "b"])["passed"]
    r = cross_repo_validity(["a", "b"], ["a"])
    assert not r["passed"] and r["reasons"] == ["fewer_than_2_repos_patched"]
    assert "patched_repo_not_declared" in cross_repo_validity(["a", "b"], ["a", "c"])["reasons"]
    assert "fewer_than_2_declared_repos" in cross_repo_validity(["a"], ["a"])["reasons"]


def test_compute_gates_workspace_only(tmp_path):
    ms = {"M1": {"repos": "server;sdk"}, "M2": {"repos": "server"}, "M3": {"repos": ""}}
    assert compute_xrepo_gates(tmp_path, {"M2": ms["M2"], "M3": ms["M3"]}, ["M2", "M3"]) == {}  # N=1-like: nothing
    for repo, body in (("server", "diff\n"), ("sdk", "diff\n")):
        d = tmp_path / "milestone_patches" / "M1"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{repo}.patch").write_text(body)
    g = compute_xrepo_gates(tmp_path, ms, ["M1", "M2"])
    assert g["X_cross_repo_validity"]["passed"]
    assert not g["X_single_repo_shortcut"]["passed"]  # no evidence yet
    (tmp_path / "xrepo_gate").mkdir()
    (tmp_path / "xrepo_gate" / "M1.json").write_text(
        json.dumps({"f2p_ids": F2P, "per_repo_results": {"server": {"server::TestA": "passed"}, "sdk": {}}})
    )
    g = compute_xrepo_gates(tmp_path, ms, ["M1", "M2"])
    assert g["X_single_repo_shortcut"]["passed"] and g["X_single_repo_shortcut"]["value"] == 1.0
