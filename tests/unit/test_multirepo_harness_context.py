"""HarnessContext workspace fields + stage 1 multi-repo testbed validation."""

import json
import logging
import shutil

from deepcommit.harness.prepare_repo import multirepo_testbed as mt
from deepcommit.pipeline.harness_context import HarnessContext
from deepcommit.pipeline.stages_harness.stage1_build_testbed import Stage1BuildTestbed
from tests.unit.test_multirepo_harness_testbed import ws  # noqa: F401  (fixture)


def _ctx(root, **kw):
    return HarnessContext(
        trial_name="t",
        repo_id=kw.pop("repo_id", "acme_server:v1_v2"),
        config_file="harness_pipeline.yaml",
        agents="claude",
        code_commit_sha="x",
        workspace_root=str(root),
        **kw,
    )


def test_single_repo_context_unchanged(tmp_path):
    ctx = _ctx(tmp_path, repo_src_dirs=["src"])
    assert not ctx.is_multi_repo and ctx.repos is None and ctx.repo_names() == []
    assert ctx.repo_range == "acme_server_v1_v2" and ctx.get_repo_src_dirs() == ["src"]
    assert "workspace_id" not in ctx.to_meta()


def test_workspace_context_reads_repos_json(tmp_path):
    wd = tmp_path / "workspaces" / "ws1"
    wd.mkdir(parents=True)
    spec = lambda n: {  # noqa: E731
        "role": "library",
        "checkout_path": f"/c/{n}",
        "repo_src_dirs": ["src/"],
        "prefixed_src_dirs": [f"{n}/src/"],
        "prefixed_test_dirs": [f"{n}/tests/**"],
        "prefixed_exclude": [],
    }
    (wd / "repos.json").write_text(json.dumps({"a": spec("a"), "b": spec("b")}))
    ctx = _ctx(tmp_path, workspace_dir=str(wd), repo_src_dirs=["ignored"])
    assert ctx.is_multi_repo and ctx.workspace_id == "ws1" and ctx.repo_names() == ["a", "b"]
    assert ctx.get_repo_src_dirs() == ["a/src/", "b/src/"] and ctx.test_dirs == ["a/tests/**", "b/tests/**"]
    assert ctx.to_meta()["repos"] == ["a", "b"]
    assert ctx.repo_range == "acme_server_v1_v2"  # repo_id behaviour kept


def test_stage1_validates_per_repo_tags(ws, tmp_path):  # noqa: F811
    mt.build_workspace_testbed(
        workspace_dir=ws["wsdir"], milestone_csv=ws["mcsv"], dependencies_csv=ws["dcsv"], output_dir=ws["out"]
    )
    out = ws["out"]
    shutil.copy(ws["mcsv"], out / "milestones.csv")
    shutil.copy(ws["dcsv"], out / "dependencies.csv")
    ctx = _ctx(out, workspace_dir=str(ws["wsdir"]))
    stage = Stage1BuildTestbed.__new__(Stage1BuildTestbed)
    stage.ctx, stage.logger = ctx, logging.getLogger("t")
    assert stage._validate_outputs() is None
    import subprocess

    subprocess.run(
        ["git", "tag", "-d", "milestone-M2-end"], cwd=out / "testbed" / "beta", check=True, capture_output=True
    )
    err = stage._validate_outputs()
    assert err and "beta:milestone-M2-end" in err

    # single-repo layout still validated against testbed/ itself
    single = _ctx(out)
    stage.ctx = single
    assert "testbed/ is not a git repository" in stage._validate_outputs()
