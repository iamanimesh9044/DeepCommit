"""Workspace test_config suites, id namespacing, per-repo switch script (no Docker)."""

import json
from pathlib import Path
from types import SimpleNamespace

from deepcommit.harness.test_runner.core.classifier import TestClassifier
from deepcommit.harness.test_runner.core.report_parser import namespace_test_ids
from deepcommit.harness.test_runner.core.test_executor import get_workspace_switch_cmd
from deepcommit.harness.test_runner.core.types import MilestoneTestConfig
from deepcommit.harness.test_runner.run_milestone_tests import build_workspace_state_script, get_switch_cmd

SUITES = [
    {
        "repo": "server",
        "workdir": "/workspace/server",
        "language": "go",
        "framework": "go_test",
        "run_cmd": "go test -json ./... > /output/{output_file}",
        "report_file": "{state}_server.json",
    },
    {
        "repo": "sdk",
        "workdir": "/workspace/sdk",
        "language": "rust",
        "framework": "cargo",
        "run_cmd": "cargo test > /output/{output_file}",
        "kind": "integration",
        "test_states": ["end"],
    },
]


def test_suites_list_and_dict_forms(tmp_path):
    for payload in (SUITES, {"suites": SUITES}):
        p = tmp_path / "tc.json"
        p.write_text(json.dumps(payload))
        cfg = MilestoneTestConfig.from_file(p)
        assert cfg.is_workspace and cfg.workspace_repos() == ["server", "sdk"]
        assert cfg.modes[0].test_cmd.startswith("go test") and cfg.modes[0].report_file == "{state}_server.json"
        assert cfg.modes[1].name == "sdk-integration" and cfg.modes[1].kind == "integration"
        assert cfg.get_all_states() == ["end", "start"]
        # roundtrip keeps suite fields
        again = MilestoneTestConfig.from_list(cfg.to_list())
        assert again.modes[1].repo == "sdk" and again.modes[0].workdir == "/workspace/server"


def test_old_forms_unchanged(tmp_path):
    p = tmp_path / "tc.json"
    p.write_text(json.dumps([{"name": "default", "test_cmd": "pytest"}]))
    cfg = MilestoneTestConfig.from_file(p)
    assert not cfg.is_workspace and cfg.modes[0].repo is None
    assert "repo" not in cfg.to_list()[0]
    p.write_text(json.dumps({"run_cmd": "pytest -x"}))
    cfg = MilestoneTestConfig.from_file(p)
    assert len(cfg.modes) == 1 and cfg.modes[0].test_cmd == "pytest -x" and not cfg.is_workspace
    p.write_text(json.dumps({"pytest_extra_args": "-q"}))
    assert MilestoneTestConfig.from_file(p).modes[0].test_cmd == "-q"


def test_namespace_ids_and_merge_into_one_classification(tmp_path):
    def rep(ids_outcomes):
        return {"tests": [{"nodeid": i, "outcome": o} for i, o in ids_outcomes], "collectors": []}

    # same native id in two repos must not collide
    start = {
        "tests": namespace_test_ids(rep([("TestFoo", "passed")]), "server")["tests"]
        + namespace_test_ids(rep([("TestFoo", "failed")]), "sdk")["tests"],
        "collectors": [],
    }
    end = {
        "tests": namespace_test_ids(rep([("TestFoo", "passed")]), "server")["tests"]
        + namespace_test_ids(rep([("TestFoo", "passed")]), "sdk")["tests"],
        "collectors": [],
    }
    (tmp_path / "s.json").write_text(json.dumps(start))
    (tmp_path / "e.json").write_text(json.dumps(end))
    cls = TestClassifier(framework="pytest").classify_from_files(Path(tmp_path / "s.json"), Path(tmp_path / "e.json"))
    assert cls["fail_to_pass"] == ["sdk::TestFoo"] or [
        t if isinstance(t, str) else t.get("nodeid") for t in cls["fail_to_pass"]
    ] == ["sdk::TestFoo"]
    # idempotent + no-op without repo
    r = rep([("a", "passed")])
    assert namespace_test_ids(r, None)["tests"][0]["nodeid"] == "a"
    namespace_test_ids(r, "x")
    namespace_test_ids(r, "x")
    assert r["tests"][0]["nodeid"] == "x::a"


def test_workspace_scripts():
    assert get_switch_cmd("end", "M1") == "git checkout -f milestone-M1-end"  # N=1 unchanged
    cmd = get_workspace_switch_cmd("start", "M1", ["server", "sdk"])
    assert cmd.splitlines() == [
        "(cd /workspace/server && git checkout -f milestone-M1-start)",
        "(cd /workspace/sdk && git checkout -f milestone-M1-start)",
    ]
    mode = SimpleNamespace(name="sdk-integration", repo="sdk", workdir=None)
    script = build_workspace_state_script("end", "M1", ["server", "sdk"], mode, "cargo test")
    assert "cd /workspace/sdk\n" in script and "milestone-M1-end" in script and "cargo test || true" in script
