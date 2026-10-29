"""MCP server wiring + MCP config rendering for multi-repo workspaces."""

import importlib
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from deepcommit.pipeline.stages.tools import configure_tools

from .test_multirepo_tools_routing import ws_env  # noqa: F401  (fixture)


def load_server(monkeypatch, dag_path, workspace_dir=None):
    monkeypatch.setenv("DEEPCOMMIT_COMMIT_DAG_PATH", str(dag_path))
    monkeypatch.setenv("REPO_SRC_DIRS", '["protobufs/"]')
    monkeypatch.setenv(
        "ENABLED_MCP_TOOLS",
        "get_issue_context,get_pr_context,get_commit_context,get_release_context,get_commits_diff,"
        "search_entities,calculate_commits_src_loc",
    )
    if workspace_dir is None:
        monkeypatch.delenv("DEEPCOMMIT_WORKSPACE_DIR", raising=False)
    else:
        monkeypatch.setenv("DEEPCOMMIT_WORKSPACE_DIR", str(workspace_dir))
    import deepcommit.mcp.mcp_server as server

    return importlib.reload(server)


def test_mcp_single_repo_schema_and_passthrough(ws_env, monkeypatch):  # noqa: F811
    e = ws_env
    monkeypatch.setenv("DEEPCOMMIT_REPO_DIR", str(e["proto"]))
    server = load_server(monkeypatch, e["res"].merged_dir / "commit_dag.json")
    assert not server.RESOLVER.multi
    for name in ("get_issue_context", "get_pr_context", "get_release_context", "get_commits_diff"):
        assert "repo" not in inspect.signature(getattr(server, name)).parameters
    out = server.calculate_commits_src_loc([e["c1"]])
    assert out["total_src_loc"] == 5 and "repo" not in out["commits"][0] and out["path_prefix"] == ["protobufs/"]
    assert "repo" not in server.get_commit_context(e["c1"], expand=["files"])


def test_mcp_multi_repo_routes_every_tool(ws_env, monkeypatch):  # noqa: F811
    e = ws_env
    monkeypatch.setenv("DEEPCOMMIT_REPO_DIR", "unused")
    monkeypatch.setenv("DEEPCOMMIT_PREPROC_DIR", str(e["res"].merged_dir))
    server = load_server(monkeypatch, e["res"].merged_dir / "commit_dag.json", e["res"].out_dir)
    assert server.RESOLVER.multi
    assert "repo" in inspect.signature(server.get_pr_context).parameters
    out = server.calculate_commits_src_loc([e["c1"], e["s1"]])
    assert out["total_src_loc"] == 8 and {c["repo"] for c in out["commits"]} == {"protocol", "server"}
    ctx = server.get_commit_context(e["s1"], expand=["files"])
    assert ctx["repo"] == "server" and all(f["path"].startswith("server/") for f in ctx["files"])


class _Logger:
    def __init__(self):
        self.log = logging.getLogger("t")

    def info(self, *a, **k): ...

    def error(self, *a, **k): ...

    def save_json(self, data, full_path, archive=False):
        Path(full_path).write_text(json.dumps(data))
        return full_path


def _ctx(tmp_path, **extra):
    cfg = tmp_path / "mcp"
    cfg.mkdir(exist_ok=True)
    src = Path(__file__).resolve().parents[2] / "deepcommit" / "configs" / "mcp" / "git_analysis.json"
    (cfg / "git_analysis.json").write_text(src.read_text())
    return SimpleNamespace(
        tools_config_path=str(cfg),
        is_multi_stage=False,
        requested_tools=lambda: ["get_commit_context"],
        get_repo=lambda: "/r",
        get_preprocess=lambda: "/p",
        get_input=lambda k: "/dag.json",
        get_repo_src_dirs=lambda: ["src/"],
        test_dirs=None,
        exclude=None,
        get_mcp_config_path=lambda: str(tmp_path / "trial.mcp.json"),
        **extra,
    )


def test_configure_tools_workspace_env_only_when_set(tmp_path):
    configure_tools(_ctx(tmp_path), _Logger())
    single = json.loads((tmp_path / "trial.mcp.json").read_text())
    assert "DEEPCOMMIT_WORKSPACE_DIR" not in single["mcpServers"]["git_analysis"]["env"]
    assert "{{" not in json.dumps(single)

    configure_tools(_ctx(tmp_path, workspace_dir="/ws/stack"), _Logger())
    multi = json.loads((tmp_path / "trial.mcp.json").read_text())
    assert multi["mcpServers"]["git_analysis"]["env"]["DEEPCOMMIT_WORKSPACE_DIR"] == "/ws/stack"
    del multi["mcpServers"]["git_analysis"]["env"]["DEEPCOMMIT_WORKSPACE_DIR"]
    assert multi == single
