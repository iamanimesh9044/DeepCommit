"""The CSF service layout: render.py, svc_layout.py, the per-step runner (grade.py, test.sh), fold, ledger, checker."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import tomllib
import yaml

from deepcommit.export import curate, fold, svc_layout
from deepcommit.export.csf_check import check_task

TEMPLATES = Path(svc_layout.__file__).parent / "templates"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def render_mod():
    return _module(TEMPLATES / "render.py", "csf_render")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _patch(tmp: Path, before: dict, after: dict) -> bytes:
    """A real git patch (repo-relative paths) from file contents before/after."""
    repo = tmp / "patchrepo"
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir()
    _git(repo, "init", "-q")
    for f, c in before.items():
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(c)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a")
    for f, c in after.items():
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(c)
    _git(repo, "add", "-A")
    return subprocess.run(["git", "diff", "--cached", "--binary"], cwd=repo, check=True, capture_output=True).stdout


QDRANT = svc_layout.ServiceSpec(
    name="qdrant-server",
    runtime="qdrant-1.20",
    command="qdrant --disable-telemetry",
    port=6333,
    health="/healthz",
    env={"QDRANT__STORAGE__STORAGE_PATH": "/tmp/qdrant/storage"},
    localhost_ports=[6333, 6334],
)


# ------------------------------------------------------------------ svc_layout


def test_parse_services_validates(tmp_path):
    rt = tmp_path / "rt"
    rt.mkdir()
    (rt / "Dockerfile").write_text("FROM scratch\n")
    raw = {"csf_services": [{"name": "db", "runtime": {"name": "r", "dir": "rt"}, "command": "run", "port": 1, "health": "/h"}]}
    (svc,) = svc_layout.parse_services(raw, tmp_path, ["repo"])
    assert svc.kind == "app" and svc.runtime_dir == rt.resolve() and svc.health_cmd == "curl -fs db:1/h"
    for bad in (
        {"name": "repo", "runtime": {"name": "r", "dir": "rt"}, "command": "x"},  # collides with a repo
        {"name": "main", "runtime": {"name": "r", "dir": "rt"}, "command": "x"},
        {"name": "x", "runtime": {"name": "r", "dir": "missing"}, "command": "x"},
        {"name": "x", "kind": "infra"},  # infra needs compose.image/build
        {"name": "x", "runtime": {"name": "r", "dir": "rt"}, "command": "x", "health": "/h"},  # health without port
    ):
        with pytest.raises(svc_layout.LayoutError):
            svc_layout.parse_services({"csf_services": [bad]}, tmp_path, ["repo"])


def test_compose_links_main_and_forwards_localhost():
    infra = svc_layout.ServiceSpec(name="db", kind="infra", compose={"image": "postgres:16"})
    doc = yaml.safe_load(svc_layout.render_compose([QDRANT, infra]))
    main = doc["services"]["main"]
    assert main["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert main["volumes"] == ["app-src:/app"] and "appnet" in main["networks"]
    assert "TCP:qdrant-server:6334" in main["command"][2] and main["command"][2].rstrip().endswith("exec sleep infinity")
    assert "qdrant-server" not in doc["services"]  # app services come from manifests
    assert doc["services"]["db"] == {"image": "postgres:16", "networks": ["appnet"]}
    assert doc["networks"]["appnet"]["internal"] is True and "app-src" in doc["volumes"]


def test_manifest_has_only_harbor_keys():
    data = tomllib.loads(svc_layout.render_manifest(QDRANT))
    assert set(data) <= {"runtime", "build", "command", "port", "health", "depends_on", "env"}
    assert data["runtime"] == "qdrant-1.20" and data["env"]["QDRANT__STORAGE__STORAGE_PATH"] == "/tmp/qdrant/storage"


def _meta(steps):
    return {
        "slug": "demo-001", "description": "d", "family": "cross-repo-change", "planned": len(steps),
        "built": len(steps), "languages": ["python"], "repos": ["api", "sdk"],
        "repo_roles": {"api": "contract", "sdk": "sdk"}, "contract_only_repos": ["api"],
        "source_repos": [{"name": r, "url": f"https://github.com/o/{r}", "commit": "a" * 40} for r in ("api", "sdk")],
        "contract_repo": "api", "dag": " -> ".join(s.split("-")[0] for s in steps), "license": "MIT",
        "step_names": steps, "artifact_excludes": ["sdk/target"],
        "cards": [{"step": s, "milestones": ["M1"], "gold_repos": ["api", "sdk"]} for s in steps],
    }  # fmt: skip


def test_task_toml_service_layout():
    cfg = tomllib.loads(
        svc_layout.render_task_toml(_meta(["m1-a", "m2-b"]), [QDRANT], svc_layout.EnvironmentSpec(), {"qdrant-1.20": "runtimes/qdrant-1.20"})
    )
    assert cfg["artifacts"][0]["source"] == "/app" and "sdk/target" in cfg["artifacts"][0]["exclude"]
    sc = cfg["environment"]["service_control"]
    assert sc["enabled"] and sc["runtimes"] == {"qdrant-1.20": "runtimes/qdrant-1.20"}
    assert "deploy" in sc["app_services"]["allow"]
    assert cfg["environment"]["healthcheck"]["command"] == "curl -fs localhost:6333/healthz"
    assert [s["verifier"]["env"]["CSF_STEP"] for s in cfg["steps"]] == ["m1-a", "m2-b"]
    assert cfg["metadata"]["milestone_cards"][0]["gold_repos"] == ["api", "sdk"]
    assert "collect" not in cfg["verifier"]


def test_rewrite_cards_replaces_tables(tmp_path):
    (tmp_path / "task.toml").write_text(
        svc_layout.render_task_toml(_meta(["m1-a"]), [], svc_layout.EnvironmentSpec(), {})
    )
    svc_layout.rewrite_cards(tmp_path, [{"step": "m1-a", "milestones": ["M1", "M2"], "checks": {"f2p": 3}}])
    cfg = tomllib.loads((tmp_path / "task.toml").read_text())
    assert cfg["metadata"]["milestone_cards"] == [{"step": "m1-a", "milestones": ["M1", "M2"], "checks": {"f2p": 3}}]
    assert cfg["environment"]["workdir"] == "/app"


# ------------------------------------------------------------------ render.py


def test_prefix_patch_applies_from_app(tmp_path, render_mod):
    patch = _patch(tmp_path, {"src/a.py": "x = 1\n"}, {"src/a.py": "x = 2\n", "src/new.py": "y\n"})
    merged = render_mod.gold_patch({"sdk": patch})
    app = tmp_path / "app"
    (app / "sdk/src").mkdir(parents=True)
    (app / "sdk/src/a.py").write_text("x = 1\n")
    subprocess.run(["git", "apply", "-"], input=merged, cwd=app, check=True, env={"GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert (app / "sdk/src/a.py").read_text() == "x = 2\n" and (app / "sdk/src/new.py").exists()


def _src(tmp: Path, services=()):
    src = tmp / "_src" / "csf-demo-001"
    (src / "instructions").mkdir(parents=True)
    (src / "instructions" / "_context.md").write_text("## Environment\n\nctx\n")
    steps = []
    for i, name in enumerate(["m1-a", "m2-b"], 1):
        (src / "instructions" / f"{name}.md").write_text(f"# step {i}\n")
        (src / "gold" / name).mkdir(parents=True)
        (src / "gold" / name / "sdk.patch").write_bytes(_patch(tmp, {"s.py": f"v = {i - 1}\n"}, {"s.py": f"v = {i}\n"}))
        (src / "hidden" / name / "sdk" / "tests").mkdir(parents=True)
        (src / "hidden" / name / "sdk" / "tests" / f"test_{i}.py").write_text(f"def test_{i}(): pass\n")
        steps.append({"name": name, "milestone": f"M{i}", "gold_repos": ["sdk"],
                      "env": {"F2P_SDK": f"tests/test_{i}.py", "P2P_EARLIER_SDK": "tests/test_1.py" if i > 1 else ""}})
    (src / "steps.json").write_text(json.dumps({"task_dir_rel": "../../tasks/csf-demo-001", "steps": steps}))
    suites = {"suites": [{"repo": "sdk", "workdir": ".", "language": "python", "framework": "pytest",
                          "run_cmd": "pytest {tests}", "parser": "pytest", "test_excludes": ["tests/*"]}],
              "contract_only": {}, "services": [{"name": s.name, "kind": s.kind, "health": s.health_cmd} for s in services]}
    (src / "suites.json").write_text(json.dumps(suites))
    (src / "runner").mkdir()
    for r in ("test.sh", "grade.py"):
        shutil.copy(TEMPLATES / r, src / "runner" / r)
    return src


def test_render_writes_service_layout_steps(tmp_path, render_mod):
    src = _src(tmp_path, [QDRANT])
    task = tmp_path / "tasks" / "csf-demo-001"
    render_mod.render(src, task)
    m2 = task / "steps" / "m2-b"
    assert sorted(p.name for p in (m2 / "tests").iterdir()) == ["checks", "grade.py", "suite.json", "test.sh"]
    assert sorted(p.name for p in (m2 / "tests/checks/sdk/tests").iterdir()) == ["test_1.py", "test_2.py"]  # cumulative
    suite = json.loads((m2 / "tests/suite.json").read_text())
    assert suite["step"] == "m2-b" and suite["own_files"] == {"sdk": ["tests/test_2.py"]}
    assert suite["files"]["sdk"] == {"f2p": ["tests/test_2.py"], "p2p": ["tests/test_1.py"]}
    assert suite["services"][0] == {"name": "qdrant-server", "kind": "app", "health": "curl -fs localhost:6333/healthz"}
    assert "checks" not in suite
    solve = (m2 / "solution/solve.sh").read_text()
    assert "git apply" in solve and solve.rstrip().endswith("harbor-svc deploy --all")
    assert (m2 / "solution/gold.patch").read_text().startswith("diff --git a/sdk/s.py b/sdk/s.py")
    assert (m2 / "instruction.md").read_text().startswith("# step 2")


# ------------------------------------------------------------------ grade.py / test.sh


def _suite(tmp, **kw):
    tests, log = tmp / "tests", tmp / "log"
    tests.mkdir(exist_ok=True)
    log.mkdir(exist_ok=True)
    suite = {"step": "m1-a", "suites": [{"repo": "r", "parser": "pytest", "run_cmd": "x {tests}"}], "services": [], **kw}
    (tests / "suite.json").write_text(json.dumps(suite))
    return tests, log


def _grade(monkeypatch, tests, log):
    monkeypatch.setenv("CSF_TESTS_DIR", str(tests))
    monkeypatch.setenv("CSF_LOG_DIR", str(log))
    g = _module(TEMPLATES / "grade.py", "csf_grade_svc")
    g.grade()
    return json.loads((log / "reward.json").read_text()), json.loads((log / "checks.json").read_text())


def test_grade_deploy_check_gates_reward(tmp_path, monkeypatch):
    checks = [{"id": "deploy::db", "repo": "db", "bucket": "deploy"}, {"id": "r::t::a", "repo": "r", "bucket": "f2p"}]
    tests, log = _suite(tmp_path, services=[{"name": "db", "kind": "app", "health": "true"}], checks=checks)
    (log / "r.f2p.log").write_text("PASSED t::a\n")
    (log / "r.f2p.rc").write_text("0")
    (log / "deploy.db.rc").write_text("0")
    rewards, _ = _grade(monkeypatch, tests, log)
    assert rewards["reward"] == 1 and rewards["deploy_passed__db"] == 1
    (log / "deploy.db.rc").write_text("1")
    rewards, out = _grade(monkeypatch, tests, log)
    assert rewards["reward"] == 0 and {c["id"]: c["status"] for c in out}["deploy::db"] == "failed"


def test_grade_by_files_before_curation(tmp_path, monkeypatch):
    tests, log = _suite(tmp_path)
    (log / "r.f2p.log").write_text("PASSED t::a\nPASSED t::b\n")
    (log / "r.f2p.rc").write_text("0")
    rewards, _ = _grade(monkeypatch, tests, log)
    assert rewards["reward"] == 1 and rewards["f2p_total"] == 2
    (log / "r.f2p.log").write_text("collection error\n")
    (log / "r.f2p.rc").write_text("2")
    rewards, out = _grade(monkeypatch, tests, log)
    assert rewards["reward"] == 0 and out[0]["id"] == "r::f2p:exit-code-2"


def test_grade_suites_line_keeps_empty_fields(tmp_path, monkeypatch, capsys):
    tests, _ = _suite(tmp_path, files={"r": {"f2p": [], "p2p": ["tests/x.py"]}})
    monkeypatch.setenv("CSF_TESTS_DIR", str(tests))
    g = _module(TEMPLATES / "grade.py", "csf_grade_lines")
    g.main(["suites"])
    out = subprocess.run(["bash", "-c", "IFS=$'\\x1f' read -r repo w whole cmd f2p p2p; echo \"[$f2p][$p2p]\""],
                         input=capsys.readouterr().out, capture_output=True, text=True).stdout
    assert out.strip() == "[][tests/x.py]"


def test_test_dirs_expand_globs(tmp_path):
    g = _module(TEMPLATES / "grade.py", "csf_grade_dirs")
    for d in ("r/tests/unit", "r/src/a/tests/x", "r/target/debug/tests", "r/src/b"):
        (tmp_path / d).mkdir(parents=True)
    assert g.test_dirs("r", ["tests/*"], tmp_path) == ["tests"]
    assert g.test_dirs("r", ["**/tests/**"], tmp_path) == ["src/a/tests", "tests"]  # target/ is never walked


def test_test_sh_parses():
    subprocess.run(["bash", "-n", str(TEMPLATES / "test.sh")], check=True)


# ------------------------------------------------------------------ fold, ledger, checker


def _rendered(tmp_path, render_mod, services=(QDRANT,)):
    src = _src(tmp_path, list(services))
    task = tmp_path / "tasks" / "cross-repo-change" / "csf-demo-001"
    render_mod.render(src, task)
    catalog = {"qdrant-1.20": "runtimes/qdrant-1.20"} if services else {}
    (task / "task.toml").write_text(
        svc_layout.render_task_toml(_meta(["m1-a", "m2-b"]), list(services), svc_layout.EnvironmentSpec(), catalog)
    )
    env = task / "environment"
    for repo in ("api", "sdk"):
        (env / "app" / repo).mkdir(parents=True)
    (env / "app/sdk/s.py").write_text("v = 0\n")
    (env / "Dockerfile").write_text("FROM ubuntu:24.04\nCOPY app/ /app/\n")
    (env / "docker-compose.yaml").write_text(svc_layout.render_compose(list(services)))
    for s in services:
        (env / "app" / s.name).mkdir(parents=True, exist_ok=True)
        (env / "app" / s.name / svc_layout.MANIFEST).write_text(svc_layout.render_manifest(s))
        rt = env / "runtimes" / s.runtime
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "Dockerfile").write_text("FROM debian\n")
        for h in ("csf-run", "csf-health"):
            shutil.copy(TEMPLATES / "runtime" / h, rt / h)
    (task / "README.md").write_text("# demo\n## Status\n## How grading works\n## Known limits\n")
    return task


def test_fold_merges_gold_and_own_files(tmp_path, render_mod):
    task = _rendered(tmp_path, render_mod)
    rename = fold.apply(task, [["m1-a", "m2-b"]])
    assert rename == {"m1-a": "m1-a", "m2-b": "m1-a"}
    sd = task / "steps" / "m1-a"
    suite = svc_layout.read_suite(sd)
    assert suite["own_files"] == {"sdk": ["tests/test_1.py", "tests/test_2.py"]} and "checks" not in suite
    assert (sd / "solution/gold.patch").read_text().count("diff --git a/sdk/s.py") == 2
    cfg = tomllib.loads((task / "task.toml").read_text())
    assert [s["name"] for s in cfg["steps"]] == ["m1-a"] and cfg["steps"][0]["verifier"]["environment_mode"] == "separate"


def test_ledger_and_cards(tmp_path, render_mod):
    task = _rendered(tmp_path, render_mod)
    t1, t2 = "sdk::tests/test_1.py::test_1", "sdk::tests/test_2.py::test_2"
    curate.write(task, [curate.StepCuration("m1-a", f2p=[t1]), curate.StepCuration("m2-b", f2p=[t2], p2p=[t1])])
    ledger = svc_layout.write_ledger(task)["tests"]
    assert ledger[t1]["kind"] == "own" and ledger[t1]["buckets"] == {"m1-a": "f2p", "m2-b": "p2p"}
    assert ledger["deploy::qdrant-server"]["kind"] == "deploy"
    cards = svc_layout.step_cards(task, {"m1-a": ["M1"], "m2-b": ["M2"]})
    assert cards[1] == {"step": "m2-b", "milestones": ["M2"], "gold_repos": ["sdk"], "checks": {"deploy": 1, "f2p": 1, "p2p": 1}}


def _errors(task):
    return [i for i in check_task(task).issues if i.severity == "error"]


def test_checker_accepts_service_layout(tmp_path, render_mod):
    task = _rendered(tmp_path, render_mod)
    curate.write(task, [curate.StepCuration("m1-a", f2p=["sdk::tests/test_1.py::test_1"]),
                        curate.StepCuration("m2-b", f2p=["sdk::tests/test_2.py::test_2"])])
    report = check_task(task)
    assert report.flavour == "multirepo-svc"
    # the synthetic task changes one repo per step: only the cross-repo rule may fire
    assert {i.rule_id for i in _errors(task)} <= {"CSF020"}, report.format()


def test_checker_flags_unlinked_dockerfile_and_bad_manifest(tmp_path, render_mod):
    task = _rendered(tmp_path, render_mod)
    (task / "environment/stray").mkdir()
    (task / "environment/stray/Dockerfile").write_text("FROM x\n")
    (task / "environment/app/qdrant-server/csf-service.toml").write_text(
        'runtime = "qdrant-1.20"\ncommand = "q"\nports = ["6333:6333"]\n'
    )
    msgs = " | ".join(i.message for i in _errors(task))
    assert "not linked" in msgs and "not allowed" in msgs


def test_checker_flags_leaked_check(tmp_path, render_mod):
    task = _rendered(tmp_path, render_mod)
    leaked = task / "steps/m2-b/tests/checks/sdk/tests/test_2.py"
    leaked.write_text("def test_2():\n    assert compute_the_answer_of_milestone_two() == 42  # hidden expectation\n")
    (task / "environment/app/sdk/tests").mkdir(parents=True)
    shutil.copy(leaked, task / "environment/app/sdk/tests/test_2.py")
    assert any("leak" in i.message for i in _errors(task))
