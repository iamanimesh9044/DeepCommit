"""Tests for the CSF task conformance checker (deepcommit/export/csf_check.py)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "deepcommit" / "export" / "csf_check.py"
_spec = importlib.util.spec_from_file_location("csf_check", _SRC)
csf_check = importlib.util.module_from_spec(_spec)
sys.modules["csf_check"] = csf_check
_spec.loader.exec_module(csf_check)
check_task = csf_check.check_task

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "csf"

# ---------------------------------------------------------------------------------------------------------------
# synthetic task builders
# ---------------------------------------------------------------------------------------------------------------

GOLD = """diff --git a/src/{n}.txt b/src/{n}.txt
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/src/{n}.txt
@@ -0,0 +1,2 @@
+hello
+world
"""

TEST_SH = """#!/usr/bin/env bash
set -uo pipefail
. /tests/step.env
give_up() { echo "$1"; python3 /tests/grade.py --error "$1"; exit 0; }
cd /app || give_up "no /app"
@@BODY@@
python3 /tests/grade.py
"""

MULTI_BODY = """for repo in {repos}; do
  git -C /app/$repo apply /logs/artifacts/model.$repo.patch || give_up "bad patch"
  cp -a /tests/hidden/$repo/. /app/$repo/
done
python3 /tests/run_suites.py /tests/suites.json"""

GRADE = """import json
keys = ["reward", "f2p_total", "f2p_passed", "p2p_total", "p2p_passed"]
{extra}
json.dump({{k: 0 for k in keys}}, open("/logs/verifier/reward.json", "w"))
"""

COLLECT_SINGLE = """#!/bin/sh
set -eu
out=/logs/artifacts
mkdir -p "$out"
export GIT_DIR=/opt/csf/base.git GIT_WORK_TREE=/app
git add -A
git diff --cached --binary --no-color --no-renames HEAD > "$out/model.patch"
"""

COLLECT_MULTI = """#!/bin/sh
set -eu
out=/logs/artifacts
mkdir -p "$out"
for repo in {repos}; do
  export GIT_DIR=/opt/csf/base/$repo.git GIT_WORK_TREE=/app/$repo
  git add -A
  git diff --cached --binary --no-color --no-renames HEAD > "$out/model.$repo.patch"
done
"""

DOCKERFILE = """FROM ubuntu:22.04
RUN --mount=type=bind,source=base.patch,target=/tmp/csf/base.patch \\
    cd /app && git apply /tmp/csf/base.patch
COPY csf-collect /usr/local/bin/csf-collect
"""

README = "# t\n\n## Status\nx\n\n## How grading works\nx\n\n## Known limits\nx\n"


def _write(p: Path, text: str, mode: int | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode is not None:
        p.chmod(mode)
    return p


def build_single(base: Path, n_steps: int = 2) -> Path:
    root = base / "csf-demo-001"
    names = [f"m{k}-feature-{k}" for k in range(1, n_steps + 1)]
    steps_toml = "".join(f'\n[[steps]]\nname = "{n}"\n' for n in names)
    dag = "; ".join(f"m{k} -> m{k + 1}" for k in range(1, n_steps)) or "m1"
    _write(
        root / "task.toml",
        f"""schema_version = "1.4"
multi_step_reward_strategy = "mean"

[task]
name = "csf/demo-001"
version = "0.1.0"
keywords = ["csf", "demo"]

[metadata]
family = "feature-evolution"
languages = ["python"]
milestones_planned = {n_steps + 1}
milestones_built = {n_steps}
source_repo = "https://example.com/demo"
source_commit = "abc123"
csf_milestone_dag = "{dag}"

[environment]
build_timeout_sec = 600
cpus = 2
memory_mb = 4096
workdir = "/app"

[agent]
timeout_sec = 3600

[verifier]
environment_mode = "separate"
timeout_sec = 600

[[verifier.collect]]
command = "csf-collect"
timeout_sec = 300
{steps_toml}""",
    )
    _write(root / "README.md", README)
    _write(root / "environment" / "Dockerfile", DOCKERFILE)
    _write(root / "environment" / "base.patch", "")
    _write(root / "environment" / "csf-collect", COLLECT_SINGLE, 0o755)
    _write(root / "tests" / "test.sh", TEST_SH.replace("@@BODY@@", "git apply /logs/artifacts/model.patch"), 0o755)
    _write(root / "tests" / "grade.py", GRADE.format(extra=""))
    _write(root / "tests" / "p2p_core.txt", "# core\ntests/test_core.py\n")
    _write(root / "tests" / "hidden" / "tests" / "test_new.py", "def test_x():\n    pass\n")
    earlier = ""
    for k, n in enumerate(names, start=1):
        s = root / "steps" / n
        _write(s / "instruction.md", f"# Milestone {k}\n\nAdd the feature.\n")
        _write(
            s / "solution" / "solve.sh", "#!/usr/bin/env bash\nset -e\ncd /app\ngit apply /solution/gold.patch\n", 0o755
        )
        _write(s / "solution" / "gold.patch", GOLD.format(n=k))
        _write(s / "tests" / "step.env", f'STEP={n}\nF2P="tests/test_m{k}.py"\nP2P_EARLIER="{earlier}"\n')
        earlier = (earlier + f" tests/test_m{k}.py").strip()
    return root


def build_multi(base: Path) -> Path:
    root = base / "csf-cross-001"
    repos = ["server", "sdk"]
    names = ["m1-wire-format", "m2-client-retry"]
    steps_toml = "".join(f'\n[[steps]]\nname = "{n}"\n' for n in names)
    _write(
        root / "task.toml",
        f"""schema_version = "1.4"
multi_step_reward_strategy = "mean"

[task]
name = "csf/cross-001"
version = "0.1.0"
keywords = ["csf", "cross-repo-change"]

[metadata]
family = "cross-repo-change"
languages = ["go", "rust"]
milestones_planned = 2
milestones_built = 2
csf_task_source = "derived"
repos = ["server", "sdk"]
csf_milestone_dag = "m1-wire-format -> m2-client-retry"

[metadata.repo_roles]
server = "service"
sdk = "client"

[[metadata.source_repos]]
name = "server"
url = "https://example.com/server"
commit = "a1"

[[metadata.source_repos]]
name = "sdk"
url = "https://example.com/sdk"
commit = "b2"

[environment]
build_timeout_sec = 600
cpus = 2
memory_mb = 4096
workdir = "/app"

[agent]
timeout_sec = 3600

[verifier]
environment_mode = "separate"
timeout_sec = 600

[[verifier.collect]]
command = "csf-collect"
timeout_sec = 300
{steps_toml}""",
    )
    _write(root / "README.md", README)
    _write(root / "environment" / "Dockerfile", DOCKERFILE)
    _write(root / "environment" / "base.patch", "")
    _write(root / "environment" / "csf-collect", COLLECT_MULTI.format(repos=" ".join(repos)), 0o755)
    _write(root / "tests" / "test.sh", TEST_SH.replace("@@BODY@@", MULTI_BODY.format(repos=" ".join(repos))), 0o755)
    _write(root / "tests" / "grade.py", GRADE.format(extra='keys += [f"f2p_total__{r}" for r in ("a",)]'))
    _write(root / "tests" / "p2p_core.txt", "# core\nserver::tests/core\n")
    suites = [
        {
            "repo": r,
            "workdir": f"/app/{r}",
            "language": "x",
            "framework": "y",
            "run_cmd": "make test",
            "parser": "junit",
        }
        for r in repos
    ]
    import json

    _write(root / "tests" / "suites.json", json.dumps(suites))
    earlier = {r: "" for r in repos}
    for k, n in enumerate(names, start=1):
        s = root / "steps" / n
        _write(s / "instruction.md", f"# Milestone {k}\n\nChange the wire contract on both sides.\n")
        _write(
            s / "solution" / "solve.sh",
            "#!/usr/bin/env bash\nset -e\n"
            + "".join(f"git -C /app/{r} apply /solution/gold/{r}.patch\n" for r in repos),
            0o755,
        )
        env = f"STEP={n}\n"
        for r in repos:
            _write(s / "solution" / "gold" / f"{r}.patch", GOLD.format(n=f"{r}{k}"))
            _write(s / "tests" / "hidden" / r / "tests" / f"test_{k}.txt", "t\n")
            env += f'F2P_{r.upper()}="{r}::t{k}"\nP2P_EARLIER_{r.upper()}="{earlier[r]}"\n'
            earlier[r] = f"{r}::t{k}"
        _write(s / "tests" / "step.env", env)
    return root


def rules(report, severity="error") -> set[str]:
    return {i.rule_id for i in (report.errors if severity == "error" else report.warnings)}


# ---------------------------------------------------------------------------------------------------------------
# positive
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["csf-chatwoot-automation-001", "csf-voting-app-evolution-001"])
def test_real_task_fixtures_pass(name):
    # Ground truth: these are copies of the real tasks (steps/ is generated and not copied here).
    report = check_task(FIXTURES / name, allow_missing_steps=True)
    assert report.ok, report.format()
    assert "CSF019" in rules(report, "warning")


def test_real_task_flavours_detected():
    assert check_task(FIXTURES / "csf-chatwoot-automation-001", allow_missing_steps=True).flavour == "history"
    assert check_task(FIXTURES / "csf-voting-app-evolution-001", allow_missing_steps=True).flavour == "services"


def test_missing_steps_is_error_by_default():
    assert "CSF019" in rules(check_task(FIXTURES / "csf-chatwoot-automation-001"))


def test_synthetic_single_repo_passes(tmp_path):
    report = check_task(build_single(tmp_path))
    assert report.ok, report.format()
    assert report.flavour == "history"
    assert not report.warnings


def test_synthetic_multi_repo_passes(tmp_path):
    report = check_task(build_multi(tmp_path))
    assert report.ok, report.format()
    assert report.flavour == "multirepo"


def test_report_shapes(tmp_path):
    root = build_single(tmp_path)
    (root / "README.md").unlink()
    report = check_task(root)
    d = report.to_dict()
    assert d["ok"] is True and d["warnings"][0]["rule_id"] == "CSF027"
    assert "WARNING CSF027" in report.format()


# ---------------------------------------------------------------------------------------------------------------
# negative
# ---------------------------------------------------------------------------------------------------------------


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    text = p.read_text()
    assert old in text, (rel, old)
    p.write_text(text.replace(old, new))


def test_wrong_schema_version(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "task.toml", 'schema_version = "1.4"', 'schema_version = "1.3"')
    assert "CSF002" in rules(check_task(root))


def test_wrong_reward_strategy(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "task.toml", '"mean"', '"sum"')
    assert "CSF003" in rules(check_task(root))


def test_toml_does_not_parse(tmp_path):
    root = build_single(tmp_path)
    (root / "task.toml").write_text("schema_version = ")
    assert rules(check_task(root)) == {"CSF001"}


def test_dag_names_missing_step(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "task.toml", 'csf_milestone_dag = "m1 -> m2"', 'csf_milestone_dag = "m1 -> m9"')
    assert "CSF007" in rules(check_task(root))


def test_dag_wrong_order(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "task.toml", 'csf_milestone_dag = "m1 -> m2"', 'csf_milestone_dag = "m2 -> m1"')
    r = check_task(root)
    assert "CSF007" in rules(r) and "topological" in r.format()


def test_dag_must_cover_all_steps(tmp_path):
    root = build_single(tmp_path, n_steps=3)
    _edit(root, "task.toml", 'csf_milestone_dag = "m1 -> m2; m2 -> m3"', 'csf_milestone_dag = "m1 -> m2"')
    assert "CSF007" in rules(check_task(root))


def test_non_consecutive_step_names(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "task.toml", 'name = "m2-feature-2"', 'name = "m3-feature-2"')
    assert "CSF008" in rules(check_task(root))


def test_missing_collect_and_artifacts(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "task.toml", '[[verifier.collect]]\ncommand = "csf-collect"\ntimeout_sec = 300\n', "")
    assert "CSF010" in rules(check_task(root))


def test_dockerfile_copies_tests(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "environment/Dockerfile", "COPY csf-collect", "COPY tests/ /tests/\nCOPY csf-collect")
    assert "CSF013" in rules(check_task(root))


def test_dockerfile_copies_base_patch(tmp_path):
    root = build_single(tmp_path)
    (root / "environment" / "Dockerfile").write_text(
        "FROM ubuntu\nCOPY base.patch /tmp/b.patch\nCOPY csf-collect /bin/\n"
    )
    assert "CSF014" in rules(check_task(root))


def test_dockerfile_first_instruction(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "environment/Dockerfile", "FROM ubuntu:22.04", "RUN true\nFROM ubuntu:22.04")
    assert "CSF012" in rules(check_task(root))


def test_collect_not_executable(tmp_path):
    root = build_single(tmp_path)
    (root / "environment" / "csf-collect").chmod(0o644)
    assert "CSF015" in rules(check_task(root))


def test_collect_git_dir_inside_app(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "environment/csf-collect", "GIT_DIR=/opt/csf/base.git", "GIT_DIR=/app/.git")
    assert "CSF015" in rules(check_task(root))


def test_test_sh_missing_step_env(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "tests/test.sh", ". /tests/step.env", "true")
    assert "CSF017" in rules(check_task(root))


def test_test_sh_without_failsafe(tmp_path):
    root = build_single(tmp_path)
    (root / "tests" / "test.sh").write_text("#!/usr/bin/env bash\n. /tests/step.env\npython3 /tests/grade.py\n")
    assert "CSF017" in rules(check_task(root))


def test_test_sh_syntax_error(tmp_path):
    root = build_single(tmp_path)
    with (root / "tests" / "test.sh").open("a") as fh:
        fh.write("if then fi (\n")
    assert "CSF017" in rules(check_task(root))


def test_grader_missing_reward_keys(tmp_path):
    root = build_single(tmp_path)
    _write(root / "tests" / "grade.py", 'import json\njson.dump({"reward": 0}, open("r.json", "w"))\n')
    r = check_task(root)
    assert "CSF018" in rules(r) and "f2p_total" in r.format()


def test_step_without_solve_sh(tmp_path):
    root = build_single(tmp_path)
    (root / "steps" / "m1-feature-1" / "solution" / "solve.sh").unlink()
    assert "CSF020" in rules(check_task(root))


def test_gold_patch_escapes_repo(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "steps/m1-feature-1/solution/gold.patch", "b/src/1.txt", "b/../evil.txt")
    assert "CSF021" in rules(check_task(root))


def test_gold_patch_bad_hunk(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "steps/m1-feature-1/solution/gold.patch", "@@ -0,0 +1,2 @@", "@@ -0,0 +1,5 @@")
    assert "CSF021" in rules(check_task(root))


def test_instruction_contains_diff(tmp_path):
    root = build_single(tmp_path)
    with (root / "steps" / "m1-feature-1" / "instruction.md").open("a") as fh:
        fh.write("\ndiff --git a/x b/x\n")
    assert "CSF030" in rules(check_task(root))


def test_instruction_mentions_hidden_only_path(tmp_path):
    root = build_single(tmp_path)
    with (root / "steps" / "m1-feature-1" / "instruction.md").open("a") as fh:
        fh.write("\nSee tests/test_new.py for the checks.\n")
    assert "CSF026" in rules(check_task(root))


def test_step_env_wrong_step(tmp_path):
    root = build_single(tmp_path)
    _edit(root, "steps/m2-feature-2/tests/step.env", "STEP=m2-feature-2", "STEP=m1-feature-1")
    assert "CSF022" in rules(check_task(root))


def test_hidden_tests_missing(tmp_path):
    root = build_single(tmp_path)
    shutil.rmtree(root / "tests" / "hidden")
    assert "CSF025" in rules(check_task(root))


def test_hidden_test_leaks_into_environment(tmp_path):
    root = build_single(tmp_path)
    _write(root / "environment" / "tests" / "test_new.py", "x")
    assert "CSF023" in rules(check_task(root)) or "CSF013" in rules(check_task(root))


def test_p2p_core_bad_line(tmp_path):
    root = build_single(tmp_path)
    (root / "tests" / "p2p_core.txt").write_text("two words\n")
    assert "CSF024" in rules(check_task(root))


def test_multi_repo_collect_missing_repo(tmp_path):
    root = build_multi(tmp_path)
    _edit(root, "environment/csf-collect", "for repo in server sdk", "for repo in server")
    assert "CSF016" in rules(check_task(root))


def test_multi_repo_missing_suites(tmp_path):
    root = build_multi(tmp_path)
    (root / "tests" / "suites.json").unlink()
    assert "CSF029" in rules(check_task(root))


def test_multi_repo_single_gold_patch_is_error_without_any_cross_repo_step(tmp_path):
    root = build_multi(tmp_path)
    for gold in (root / "steps").glob("*/solution/gold/sdk.patch"):
        gold.unlink()
    assert "CSF020" in rules(check_task(root))


def test_multi_repo_single_repo_step_is_warning_when_another_step_is_cross_repo(tmp_path):
    root = build_multi(tmp_path)
    steps = sorted((root / "steps").iterdir())
    if len(steps) < 2:
        pytest.skip("builder has a single step")
    (steps[-1] / "solution" / "gold" / "sdk.patch").unlink()
    report = check_task(root)
    assert "CSF020" not in rules(report) and "CSF020" in rules(report, "warning")


def test_multi_repo_unnamespaced_f2p(tmp_path):
    root = build_multi(tmp_path)
    (root / "steps" / "m1-wire-format" / "tests" / "step.env").write_text(
        'STEP=m1-wire-format\nF2P="a b"\nP2P_EARLIER=""\n'
    )
    assert "CSF022" in rules(check_task(root))


def test_multi_repo_metadata_missing_roles(tmp_path):
    root = build_multi(tmp_path)
    _edit(root, "task.toml", '[metadata.repo_roles]\nserver = "service"\nsdk = "client"\n', "")
    assert "CSF028" in rules(check_task(root))


def test_missing_readme_is_warning_only(tmp_path):
    root = build_single(tmp_path)
    (root / "README.md").unlink()
    r = check_task(root)
    assert r.ok and "CSF027" in rules(r, "warning")


def test_bad_folder_and_name(tmp_path):
    root = build_single(tmp_path)
    other = root.rename(tmp_path / "wrong-folder")
    assert "CSF004" in rules(check_task(other))


def test_contract_only_repo_must_be_a_contract_repo_without_suite(tmp_path):
    root = build_multi(tmp_path)
    toml = root / "task.toml"
    toml.write_text(
        toml.read_text().replace(
            'family = "cross-repo-change"', 'family = "cross-repo-change"\ncontract_only_repos = ["nope"]', 1
        )
    )
    assert "CSF028" in rules(check_task(root))
