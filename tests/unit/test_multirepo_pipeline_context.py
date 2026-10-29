"""TrialContext in single and multi-repo mode (path vars, prefixed dirs, gated prompt rendering)."""

import json
from pathlib import Path

import pytest

from deepcommit.multirepo import merge_workspace, parse_workspace
from deepcommit.pipeline.context import TrialContext
from tests.unit.test_multirepo_core import data_root, manifest  # noqa: F401  (fixtures)

PROJECT = Path(__file__).resolve().parents[2]


def _ctx(**kw):
    return TrialContext(
        trial_name="t",
        repo_id=kw.pop("repo_id", "acme_server:v2.0_v2.1"),
        prompt_file="multi_stage_pipeline.yaml",
        agents="claudecode",
        code_commit_sha="x",
        workspace_root=str(PROJECT),
        is_multi_stage=True,
        **kw,
    )


@pytest.fixture
def ws_dir(data_root):  # noqa: F811
    return merge_workspace(parse_workspace(manifest()), data_root).out_dir


def test_single_mode_unchanged():
    ctx = _ctx(repo_src_dirs=["src/x"])
    assert not ctx.is_multi_repo and ctx.get_workspace_dir() is None and ctx.repos is None
    assert ctx.get_repo_src_dirs() == ["src/x"]
    m = ctx._build_template_mapping(ctx._get_stage_config("stage2_mstone"))
    assert m["is_multi_repo"] == "false" and m["workspace_id"] == "acme_server_v2.0_v2.1"
    assert m["workspace_repos_table"].splitlines()[-1] == "| acme/server | library | `repo_data` | `src/x` |"
    assert m["repo_name"] == "acme/server" and m["version_range"] == "v2.0 to v2.1"
    assert m["start_tag"] == "v2.0" and m["end_tag"] == "v2.1"


def test_multi_mode_vars(ws_dir):
    ctx = _ctx(repo_id="stack_v1", workspace_dir=str(ws_dir), repo_src_dirs=["ignored"])
    assert ctx.is_multi_repo and ctx.workspace_id == ws_dir.name
    assert set(ctx.repos) == {"protocol", "server"}
    assert ctx.repos["server"]["role"] == "server"
    assert ctx.get_repo_src_dirs() == ["protocol/protobufs/", "server/src/"]
    assert ctx.get_repo_exclude_patterns() == ["server/tests/**", "**/*_test.go"]
    m = ctx._build_template_mapping(ctx._get_stage_config("stage2_mstone"))
    assert m["is_multi_repo"] == "true"
    assert m["repo_src_dirs"] == "protocol/protobufs/,server/src/"
    assert "| protocol | contract | `repo_data/protocol` | `protocol/protobufs/` |" in m["workspace_repos_table"]
    assert m["repo_name"] == "acme/protocol, acme/server"
    assert m["version_range"] == "protocol: v1.0 to v1.1; server: v2.0 to v2.1"


@pytest.mark.parametrize("stage", ["stage1_seed", "stage2_mstone", "stage3_depend", "stage4_decomp"])
def test_gated_prompts_render_in_both_modes(ws_dir, stage):
    for ctx in (_ctx(), _ctx(repo_id="stack_v1", workspace_dir=str(ws_dir))):
        try:
            ctx._get_stage_config(stage)
        except Exception:
            pytest.skip(f"stage {stage} not configured")
        rendered, _ = ctx.render_prompt(stage, save=False)
        assert "{{#if" not in rendered and "{{/if}}" not in rendered
        if ctx.is_multi_repo:
            assert "repo_data/protocol" in rendered or "protocol" in rendered


def test_to_meta_carries_workspace_id(ws_dir):
    ctx = _ctx(repo_id="stack_v1", workspace_dir=str(ws_dir))
    assert ctx.to_meta()["workspace_id"] == ws_dir.name and ctx.to_meta()["repo_id"] == "stack_v1"
    assert "workspace_id" not in _ctx().to_meta()
    json.dumps(ctx.repos)
