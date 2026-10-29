"""Image-build prompt rendering: single repo keeps /testbed wording, workspace mode adds the multi-root layout."""

import json
import sys
from pathlib import Path

from deepcommit.harness.prepare_images.build_milestone_image import render_prompt as rp
from deepcommit.harness.test_runner.core.docker import DockerRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "golden"))
from test_render_prompt_injection import _make_inputs  # noqa: E402


def _render(tmp_path, workspace: bool) -> str:
    kw = _make_inputs(tmp_path)
    if workspace:
        wd = kw["work_dir"]
        (wd / "testbed").mkdir()
        (wd / "testbed" / "manifest.json").write_text(json.dumps({"repos": {"server": {}, "sdk": {}}}))
        (wd / "metadata.json").write_text(
            json.dumps(
                {
                    "repos": {
                        "server": {"role": "service", "repo_src_dirs": ["pkg/"]},
                        "sdk": {"repo_src_dirs": ["src/"]},
                    }
                }
            )
        )
    assert rp.render_env_setup_prompt(**kw) == 0
    return (kw["output_dir"] / "M5" / "env_setup_prompt.md").read_text()


def test_single_repo_prompt_has_no_workspace_text(tmp_path):
    text = _render(tmp_path, workspace=False)
    assert "Multi-root workspace layout" not in text and "COPY . /workspace/" not in text
    assert "COPY . /testbed/" in text


def test_workspace_prompt_describes_layout(tmp_path):
    text = _render(tmp_path, workspace=True)
    assert "Multi-root workspace layout (this milestone spans 2 repos)" in text
    assert "| `server` | `/workspace/server` | service | pkg/ |" in text
    assert "| `sdk` | `/workspace/sdk` | library | src/ |" in text
    assert "COPY . /workspace/" in text and "list of suites" in text and "--no-network" in text
    assert "Workspace replacement is present instead" in text


def test_docker_runner_network_none():
    assert DockerRunner("img").network_none is False
    assert DockerRunner("img", network_none=True).network_none is True
    try:
        DockerRunner("img", network_none=True, use_host_network=True)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
