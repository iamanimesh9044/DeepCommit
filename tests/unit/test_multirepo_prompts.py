"""Phase 2 prompts are multi-repo only: no conditional gates, workspace table present, key rules stated."""

import re
import subprocess
from pathlib import Path

import pytest

from deepcommit.pipeline.utils.template import render_template_string

ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = ROOT / "deepcommit/pipeline/prompts/stage_breakdown_v1"
EDITED = [
    "seed_discovery.md",
    "milestone_consolidate.md",
    "seed_expansion.md",
    "dep_infer.md",
    "milestone_decompose.md",
    "milestone_decompose_subagent.md",
    "milestone_deep_analysis_subagent.md",
    "verify_split_compilation.md",
]
TABLE3 = (
    "| name | role | repo dir | src dirs |\n| --- | --- | --- | --- |\n"
    "| contract | contract | `repo_data/contract` | `contract/proto/` |\n"
    "| server | library | `repo_data/server` | `server/src/` |\n"
    "| sdk | library | `repo_data/sdk` | `sdk/src/` |"
)
TABLE1 = (
    "| name | role | repo dir | src dirs |\n| --- | --- | --- | --- |\n| acme/server | library | `repo_data` | `src/` |"
)
MAPPINGS = {
    "three": {"repo_path": "repo_data", "workspace_id": "ws1", "workspace_repos_table": TABLE3, "repo_src_dirs": "a"},
    "one": {
        "repo_path": "repo_data",
        "workspace_id": "acme_server",
        "workspace_repos_table": TABLE1,
        "repo_src_dirs": "a",
    },
}
TABLE_MARKER = {"three": "| sdk | library | `repo_data/sdk` |", "one": "| acme/server | library | `repo_data` |"}
COMMON = ["`edges_soft`", '"xrepo": true', "git -C repo_data/<repo>"]
KEY_RULES = {
    "seed_discovery.md": ["about 20 seeds **per repository**", "`repo` field", "first consumer"],
    "milestone_consolidate.md": [
        "UNION",
        "textual edges inside each repository and on HARD cross-repo edges",
        "MUST be placed in the same group",
        "`repos` column",
        "at least 2 repositories",
        "`XREPO`",
    ],
    "seed_expansion.md": [
        "over the union of commits of all repositories",
        "Never split a contract change from its first consumer",
        "at least 2 repositories",
        "**repos**:",
    ],
    "dep_infer.md": ["**XREPO**", "LOW confidence", "NOT assumed correct"],
    "milestone_decompose.md": [
        "src_loc per repository",
        "The CV is computed on the sum",
        "workspace totals",
        "one per repository",
        "**repos**:",
    ],
    "milestone_decompose_subagent.md": [
        "split repository",
        "<repo dir>",
        "`integration_test_commit` belongs to the same repository",
    ],
    "milestone_deep_analysis_subagent.md": ["first consumer", "at least 2 repositories", "judge the CV on the sum"],
    "verify_split_compilation.md": ["skip this verification", "skipped (multi-repo milestone)"],
}
# single-repo-only wording that was rewritten
GONE = [
    "git -C {{repo_path}}",
    "(target: < 20)",
    "Identify < 20 seeds",
    "`integration_test_commit`: Must be empty or a single SHA",
]
TAG = re.compile(r"\{\{\s*[a-zA-Z0-9_]+\s*\}\}")


def _original(name: str) -> str:
    rel = f"deepcommit/pipeline/prompts/stage_breakdown_v1/{name}"
    for rev in ("origin/main", "main", "HEAD"):
        out = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=ROOT, capture_output=True, text=True)
        if out.returncode == 0 and "{{#if" not in out.stdout:
            return out.stdout
    pytest.skip("pristine original not available from git")


def _render(name: str, mapping) -> str:
    return render_template_string((PROMPT_DIR / name).read_text(encoding="utf-8"), mapping)


@pytest.mark.parametrize("name", EDITED)
@pytest.mark.parametrize("kind", ["three", "one"])
def test_multirepo_only_rendering(name, kind):
    mapping = MAPPINGS[kind]
    raw = (PROMPT_DIR / name).read_text(encoding="utf-8")
    assert "{{#if" not in raw and "{{/if" not in raw and "is_multi_repo" not in raw
    rendered = _render(name, mapping)
    assert "{{#if" not in rendered and "{{/if" not in rendered
    # no unresolved variable beyond those the original already leaves open
    original_open = set(TAG.findall(render_template_string(_original(name), mapping)))
    assert set(TAG.findall(rendered)) <= original_open
    assert TABLE_MARKER[kind] in rendered
    for needle in KEY_RULES[name]:
        assert needle in rendered, (name, needle)
    for gone in GONE:
        assert gone not in rendered, (name, gone)


@pytest.mark.parametrize("name", [n for n in EDITED if n != "verify_split_compilation.md"])
def test_common_workspace_rules(name):
    rendered = _render(name, MAPPINGS["three"])
    for needle in COMMON:
        assert needle in rendered, (name, needle)


def test_conditional_mechanism_still_supported():
    t = "a\n{{#if x}}\nB {{y}}\n{{/if}}\nc"
    assert render_template_string(t, {"y": "1"}) == "a\nc"
    assert render_template_string(t, {"x": "true", "y": "1"}) == "a\nB 1\nc"
    assert render_template_string("no syntax {{v}} {{unknown}}", {"v": "ok"}) == "no syntax ok {{unknown}}"


def test_srs_template_has_no_workspace_branching():
    raw = (ROOT / "deepcommit/harness/gen_srs/srs_gen_v1.tmpl").read_text(encoding="utf-8")
    assert "workspace_mode" not in raw and "Interface Contract Section" in raw and "Boundary-name rule" in raw


@pytest.mark.parametrize("name", ["qdrant_clients.workspace.yaml", "temporal_stack.workspace.yaml"])
def test_example_manifests_load(name):
    from deepcommit.multirepo import load_workspace

    ws = load_workspace(ROOT / "config/examples" / name)
    assert 2 <= len(ws.repos) <= 5
