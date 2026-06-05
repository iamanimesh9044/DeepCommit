"""Wave 2 — prompt FIELD-INJECTION tests for build_milestone_image/render_prompt.py.

Scope (deliberately narrow): verify that the fields the PIPELINE injects through
``_base_context()`` are computed correctly and actually land in the rendered
prompt. We do **not** assert on prose / structure / wording — that is reserved
for AI review (Family B). The Jinja environment uses ``StrictUndefined``, so a
referenced-but-missing variable already hard-fails render; these tests pin the
injected *values*, the derivation logic, and the template<->pipeline variable
contract.

All expected values were ground-truthed by executing the real functions.
"""

import json
from pathlib import Path

import pytest
from jinja2 import Environment, meta

from deepcommit.harness.prepare_images.build_milestone_image import render_prompt as rp


def _make_inputs(tmp_path: Path, **overrides):
    """Build a realistic ``render_env_setup_prompt(...)`` kwargs dict over tmp_path.

    ``_base_context`` reads ``<work_dir>/../base/baseline_metrics.json``, so we lay
    that out. Every other path is only string-interpolated, so it need not exist.
    """
    work_dir = tmp_path / "scenario"
    work_dir.mkdir(exist_ok=True)
    base_dir = tmp_path / "base"
    base_dir.mkdir(exist_ok=True)
    (base_dir / "baseline_metrics.json").write_text(json.dumps({"collection": {"total_collected": 42}}))
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)

    inputs = {
        "milestone_id": "M5",
        "milestone_commits": ["c1", "c2"],
        "base_state": "0 passed",
        "end_state": "2 passed",
        "repo_path": repo,
        "repo_src_dirs": ["src/pkg"],
        "test_dir": "tests",
        "base_image": "python:3.11",
        "base_dockerfile_path": tmp_path / "Dockerfile.base",
        "commit_changed_tests_dir": tmp_path / "changes",
        "test_runner_script": "run_tests.sh",
        "test_results_dir": tmp_path / "results",
        "output_dir": out,
        "work_dir": work_dir,
        "repo_id": "acme/widget",
        "language": "python",
        "test_framework": None,
    }
    inputs.update(overrides)
    return inputs


def _context(tmp_path: Path, **overrides):
    return rp._base_context(**_make_inputs(tmp_path, **overrides))


# ---------------------------------------------------------------------------
# (a) the injection builder _base_context: derived / serialized fields
# ---------------------------------------------------------------------------
def test_base_context_milestone_id_alias_is_consistent(tmp_path):
    # The id is injected under TWO keys; both must equal the input so a template
    # using either `m_id` or `milestone_id` can never drift apart.
    ctx = _context(tmp_path, milestone_id="M5")
    assert ctx["milestone_id"] == "M5"
    assert ctx["m_id"] == "M5"


def test_base_context_serializes_lists_to_json(tmp_path):
    ctx = _context(tmp_path, milestone_commits=["c1", "c2"], repo_src_dirs=["src/pkg"])
    assert ctx["milestone_commits"] == ["c1", "c2"]
    assert ctx["milestone_commits_json"] == '["c1", "c2"]'  # == json.dumps([...])
    assert ctx["repo_src_dirs_json"] == json.dumps(["src/pkg"])
    # the _json field must stay in sync with its source list
    assert json.loads(ctx["repo_src_dirs_json"]) == ctx["repo_src_dirs"]


def test_base_context_relativizes_paths_to_absolute_strings(tmp_path):
    inputs = _make_inputs(tmp_path)
    ctx = rp._base_context(**inputs)
    # get_relative_path -> str(Path(...).absolute()); pin that exact contract.
    assert ctx["repo_path"] == str(inputs["repo_path"].absolute())
    assert Path(ctx["repo_path"]).is_absolute()


def test_base_context_reads_base_collected_total_from_baseline(tmp_path):
    assert _context(tmp_path)["base_collected_total"] == 42  # from baseline_metrics.json


def test_base_context_framework_defaults_from_language(tmp_path):
    # test_framework=None -> default by language (python -> pytest)
    assert _context(tmp_path, language="python", test_framework=None)["test_framework"] == "pytest"
    # an explicit framework overrides the language default
    assert _context(tmp_path, test_framework="go_test")["test_framework"] == "go_test"


def test_base_context_derives_artifact_paths_from_milestone_id(tmp_path):
    ctx = _context(tmp_path, milestone_id="M9")
    assert ctx["build_status_path"] == "dockerfiles/M9/build_status.json"
    assert ctx["patched_tests_path"] == "milestone_patched_tests/M9.json"


# ---------------------------------------------------------------------------
# (a2) injection guards: a bad/missing baseline must fail loudly, never inject junk
# ---------------------------------------------------------------------------
def test_base_context_requires_work_dir(tmp_path):
    with pytest.raises(ValueError, match="work_dir"):
        rp._base_context(**_make_inputs(tmp_path, work_dir=None))


def test_base_context_missing_baseline_raises(tmp_path):
    inputs = _make_inputs(tmp_path)
    (tmp_path / "base" / "baseline_metrics.json").unlink()
    with pytest.raises(FileNotFoundError):
        rp._base_context(**inputs)


def test_base_context_non_int_total_collected_raises(tmp_path):
    inputs = _make_inputs(tmp_path)
    (tmp_path / "base" / "baseline_metrics.json").write_text(json.dumps({"collection": {"total_collected": "oops"}}))
    with pytest.raises(ValueError, match="total_collected"):
        rp._base_context(**inputs)


def test_base_context_bad_json_baseline_raises(tmp_path):
    inputs = _make_inputs(tmp_path)
    (tmp_path / "base" / "baseline_metrics.json").write_text("{not valid json")
    with pytest.raises(ValueError, match="JSON"):
        rp._base_context(**inputs)


# ---------------------------------------------------------------------------
# (b) full render: injected VALUES land + the StrictUndefined contract holds
#     across the whole {% include %} tree. Prose is intentionally NOT asserted.
# ---------------------------------------------------------------------------
def test_render_injects_values_into_the_prompt(tmp_path):
    inputs = _make_inputs(tmp_path)
    rc = rp.render_env_setup_prompt(**inputs)
    # rc == 0 means StrictUndefined rendered the entire base.md.j2 + includes with
    # NO missing variable — i.e. every referenced field was injected.
    assert rc == 0
    rendered = (inputs["output_dir"] / "M5" / "env_setup_prompt.md").read_text()
    # injected values reached the prompt
    assert "M5" in rendered  # milestone_id / m_id
    assert json.dumps(["c1", "c2"]) in rendered  # milestone_commits_json
    assert "python:3.11" in rendered  # base_image
    # the specific placeholders were substituted, not emitted raw
    assert "{{ milestone_id }}" not in rendered
    assert "{{ m_id }}" not in rendered


def test_render_is_deterministic_for_identical_inputs(tmp_path):
    inputs = _make_inputs(tmp_path)
    out_file = inputs["output_dir"] / "M5" / "env_setup_prompt.md"
    assert rp.render_env_setup_prompt(**inputs) == 0
    first = out_file.read_text()
    assert rp.render_env_setup_prompt(**inputs) == 0
    assert out_file.read_text() == first


# ---------------------------------------------------------------------------
# (c) variable contract: every var base.md.j2 references is injected by the
#     pipeline. (Single-file analysis — the {% include %} tree is covered by the
#     StrictUndefined render in (b).)
# ---------------------------------------------------------------------------
def test_base_template_references_only_injected_vars(tmp_path):
    src = (rp.PROMPTS_DIR / "base.md.j2").read_text()
    referenced = meta.find_undeclared_variables(Environment().parse(src))
    provided = set(rp._base_context(**_make_inputs(tmp_path)).keys())
    # render_env_setup_prompt adds these two to the context just before rendering:
    provided |= {"repo_pack_content", "repo_pack_name"}
    missing = referenced - provided
    assert missing == set(), f"base.md.j2 references fields the pipeline never injects: {sorted(missing)}"
