"""CSF task exporter: real tmp git repos, a hand-written workspace, no Docker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

from deepcommit.export import export_csf_task

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
WID = "toy_v1"
STEPS = ["m1-add-id-field", "m2-feature-in-sdk-b", "m3-extra-proto", "m4-new-module", "m5-late-feature-in-a"]
MANIFEST = """\
workspace_id: toy_v1
repos:
  - name: proto
    owner: acme
    repo: proto
    role: contract
    start: v1
    end: v2
    repo_src_dirs: [api/]
    test_dirs: ["tests/**"]
    contract_paths: ["api/*.proto"]
  - name: sdk-a
    owner: acme
    repo: sdk-a
    role: sdk
    start: v1
    end: v2
    repo_src_dirs: [sdk_a/]
    test_dirs: ["tests/**"]
  - name: sdk-b
    owner: acme
    repo: sdk-b
    role: sdk
    start: v1
    end: v2
    repo_src_dirs: [src/]
    test_dirs: ["tests/**"]
"""


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def build_world(root: Path) -> Path:
    """data_root with 3 repos, commit_repos.json and a trial's milestones/dependencies; returns the manifest path."""
    data = root / "DATA"
    repos = data / "github_data" / "repos"
    sha: dict[str, str] = {}
    for name in ("proto", "sdk-a", "sdk-b"):
        r = repos / f"acme_{name}"
        r.mkdir(parents=True)
        git(r, "init", "-q", "-b", "main")
    p, a, b = (repos / f"acme_{n}" for n in ("proto", "sdk-a", "sdk-b"))
    commit(
        p,
        {
            "api/schema.proto": "message A {\n}\n",
            "tests/test_old.py": "def test_old(): pass\n",
            "LICENSE": "MIT License\n",
        },
        "base",
    )
    commit(
        a,
        {"pyproject.toml": "[project]\nname='a'\n", "sdk_a/client.py": "VALUE = 1\n", "tests/test_old.py": "x = 1\n"},
        "base",
    )
    commit(b, {"Cargo.toml": "[package]\nname='b'\n", "src/lib.rs": "pub const X: u32 = 1;\n"}, "base")
    for r in (p, a, b):
        git(r, "tag", "v1")
    sha["p1"] = commit(p, {"api/schema.proto": "message A {\n  int32 id = 1;\n}\n"}, "p1 add field")
    sha["p2"] = commit(
        p, {"api/extra.proto": "message B {}\n", "tests/test_proto.py": "def test_p(): assert True\n"}, "p2"
    )
    sha["a1"] = commit(
        a, {"sdk_a/client.py": "VALUE = 2\n", "tests/test_client.py": "def test_c(): assert True\n"}, "a1"
    )
    # in no milestone, builds on a1: conflicts on the base, so it is retried right after a1 and attached to M01
    sha["a5"] = commit(a, {"sdk_a/client.py": "VALUE = 3\n"}, "a5 background after a1")
    sha["a2"] = commit(a, {"sdk_a/more.py": "MORE = 1\n"}, "a2 (M06: no tests)")
    sha["a3"] = commit(
        a,
        {"sdk_a/more.py": "MORE = 2\n", "sdk_a/late.py": "LATE = 1\n", "tests/test_late.py": "def test_l(): pass\n"},
        "a3",
    )
    sha["b0"] = commit(b, {"src/lib.rs": "pub const X: u32 = 2;\n"}, "b0 (in no milestone, M05 needs it)")
    sha["b1"] = commit(
        b, {"src/feature.rs": "pub fn f() {}\n", "tests/feature.rs": "#[test] fn t() {}\n"}, "b1 feature"
    )
    sha["b5"] = commit(
        b,
        {
            "src/lib.rs": "pub const X: u32 = 9;\n",
            "src/newmod.rs": "pub fn n() {}\n",
            "tests/y.rs": "#[test] fn y() {}\n",
        },
        "b5",
    )
    # M04 expects X = 9, which only M05 (later in the DAG order) provides: a real conflict
    sha["b2"] = commit(b, {"src/lib.rs": "pub const X: u32 = 3;\n", "tests/x.rs": "#[test] fn x() {}\n"}, "b2")
    sha["b4"] = commit(b, {"src/newmod.rs": "pub fn n2() {}\n"}, "b4 (background, builds on M05)")
    sha["b3"] = commit(b, {"src/other.rs": "pub fn o() {}\n", "tests/other.rs": "#[test] fn o() {}\n"}, "b3")
    for r in (p, a, b):
        git(r, "tag", "v2")

    wdir = data / "github_data" / "workspaces" / WID
    wdir.mkdir(parents=True)
    owner = {"p": "proto", "a": "sdk-a", "b": "sdk-b"}
    (wdir / "commit_repos.json").write_text(json.dumps({s[:7]: owner[k[0]] for k, s in sha.items()}))
    srs = wdir / "srs" / "run1" / "M01"
    srs.mkdir(parents=True)
    (srs / "SRS.md").write_text("# Add an id to A\n\nThe schema gains an id and clients expose it.\n")

    res = data / "result" / f"{WID}_t1"
    res.mkdir(parents=True)
    rows = [
        ("M01", "Add id field", "p1;a1", "proto;sdk-a"),
        ("M02", "Feature in sdk-b", "b1", "sdk-b"),
        ("M03", "Extra proto", "p2", "proto"),
        ("M04", "Bump constant", "b2", "sdk-b"),
        ("M05", "New module", "b5", "sdk-b"),
        ("M06", "More in a", "a2", "sdk-a"),
        ("M07", "Late feature in a", "a3", "sdk-a"),
        ("M08", "Other module", "b3", "sdk-b"),
    ]
    lines = ["id,title,category,commits,mini_srs,confidence_score,repos"]
    for mid, title, commits, repos_col in rows:
        shas = ";".join(sha[c][:7] for c in commits.split(";"))
        lines.append(f'{mid},{title},Feature,{shas},"Mini SRS for {title}.",0.9,{repos_col}')
    (res / "milestones.csv").write_text("\n".join(lines) + "\n")
    (res / "dependencies.csv").write_text(
        "prerequisite_id,dependant_id,type,strength,rationale,confidence_score\n"
        "M01,M02,FUNC,Strong,r,0.9\nM01,M03,FUNC,Strong,r,0.9\nM04,M08,FUNC,Strong,r,0.9\nM06,M07,FUNC,Strong,r,0.9\n"
    )
    manifest = root / "toy.workspace.yaml"
    manifest.write_text(MANIFEST)
    return manifest


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("world")
    manifest = build_world(root)
    out = root / "out" / "cross-repo-change"
    report = export_csf_task(manifest, "t1", root / "DATA", out, task_id="csf-toy-001")
    return root, manifest, out, report


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def by_mid(report):
    return {s["mid"]: s for s in report.steps}


def test_statuses_and_conflict_recorded(world):
    _, _, _, report = world
    s = by_mid(report)
    assert [x["mid"] for x in report.steps] == ["M01", "M02", "M03", "M04", "M05", "M06", "M07", "M08"]
    assert s["M04"]["status"] == "blocked" and s["M08"]["status"] == "skipped" and s["M06"]["status"] == "background"
    # a no-tests milestone is background, not a reason to skip its dependant M07
    assert all(s[m]["status"] == "built" for m in ("M01", "M02", "M03", "M05", "M07"))
    (c,) = report.conflicts
    assert (c["milestone"], c["repo"], c["files"]) == ("M04", "sdk-b", ["src/lib.rs"])
    assert len(c["commit"]) == 40
    saved = json.loads((report.src_dir / "export_report.json").read_text())
    assert saved["conflicts"] == report.conflicts
    readme = (report.task_dir / "README.md").read_text()
    assert "blocked: cherry-pick conflict in sdk-b" in readme and "pending: Phase 3 test curation" in readme
    assert report.check is not None and report.check.ok, report.check.format()
    assert report.ok


def test_layout(world):
    _, _, _, report = world
    t = report.task_dir
    for rel in (
        "task.toml", "README.md", "environment/Dockerfile", "environment/csf-collect", "tests/test.sh",
        "tests/grade.py", "tests/p2p_core.txt", "tests/suites.json",
    ):  # fmt: skip
        assert (t / rel).is_file(), rel
    names = [d.name for d in sorted((t / "steps").iterdir())]
    assert names == STEPS
    for n in names:
        for rel in ("instruction.md", "solution/solve.sh", "tests/step.env"):
            assert (t / "steps" / n / rel).is_file(), (n, rel)
    assert sorted(p.name for p in (t / "steps/m1-add-id-field/solution/gold").iterdir()) == [
        "proto.patch",
        "sdk-a.patch",
    ]
    assert b"pub const X: u32 = 2;" in (t / "environment" / "base.patch").read_bytes()  # b0 is background
    assert "source=base.patch" in (t / "environment" / "Dockerfile").read_text()
    src = report.src_dir
    assert src == t.parent.parent / "_src" / "csf-toy-001"
    for rel in (
        "milestones.csv",
        "render.py",
        "steps.json",
        "instructions/_context.md",
        "gold/m1-add-id-field/proto.patch",
    ):
        assert (src / rel).is_file(), rel
    assert "csf-toy-001" in (t / "README.md").read_text()


def test_task_toml(world):
    _, _, _, report = world
    d = tomllib.loads((report.task_dir / "task.toml").read_text())
    assert d["schema_version"] == "1.4" and d["multi_step_reward_strategy"] == "mean"
    assert d["task"]["name"] == "csf/toy-001" and d["task"]["keywords"][0] == "csf"
    m = d["metadata"]
    assert m["family"] == "cross-repo-change" and m["csf_task_source"] == "derived"
    assert m["milestones_planned"] == 8 and m["milestones_built"] == 5
    assert m["repos"] == ["proto", "sdk-a", "sdk-b"]
    assert m["repo_roles"] == {"proto": "contract", "sdk-a": "sdk", "sdk-b": "sdk"}
    assert m["languages"] == ["python", "rust"] and m["source_license"] == "MIT"
    assert [r["name"] for r in m["source_repos"]] == ["proto", "sdk-a", "sdk-b"]
    assert all(len(r["commit"]) == 40 and r["url"].startswith("https://github.com/acme/") for r in m["source_repos"])
    assert m["csf_milestone_dag"] == "m1 -> m2; m1 -> m3; m4; m5"
    assert d["environment"]["workdir"] == "/app" and d["verifier"]["environment_mode"] == "separate"
    assert d["verifier"]["collect"] == [{"command": "csf-collect", "timeout_sec": 300}]
    assert [s["name"] for s in d["steps"]] == STEPS


def test_gold_applies_in_sequence_and_hidden_split(world, tmp_path):
    root, _, _, report = world
    t = report.task_dir
    work = {}
    for repo in ("proto", "sdk-a", "sdk-b"):
        work[repo] = tmp_path / repo
        git(tmp_path, "clone", "-q", str(root / "DATA/github_data/repos" / f"acme_{repo}"), str(work[repo]))
        git(work[repo], "checkout", "-q", "v1")
    git(tmp_path, "apply", str(t / "environment" / "base.patch"))  # rooted at /app: a/<repo>/...
    for step in sorted((t / "steps").iterdir()):
        for patch in sorted((step / "solution" / "gold").glob("*.patch")):
            repo = patch.stem
            git(work[repo], "apply", "--check", str(patch))
            git(work[repo], "apply", str(patch))
    assert (work["proto"] / "api/schema.proto").read_text() == "message A {\n  int32 id = 1;\n}\n"
    assert (work["proto"] / "api/extra.proto").exists()
    assert (work["sdk-a"] / "sdk_a/late.py").exists()
    # a5 (background, builds on a1) was attached to M01's gold
    assert (work["sdk-a"] / "sdk_a/client.py").read_text() == "VALUE = 3\n"
    assert (work["sdk-a"] / "sdk_a/more.py").read_text() == "MORE = 2\n"  # M06 background + M07 gold
    assert (work["sdk-b"] / "src/feature.rs").exists() and not (work["sdk-b"] / "src/other.rs").exists()
    assert (work["sdk-b"] / "src/lib.rs").read_text() == "pub const X: u32 = 9\n".replace("9\n", "9;\n")
    assert (work["sdk-b"] / "src/newmod.rs").read_text() == "pub fn n2() {}\n"  # b4 attached to M05
    # no gold patch touches a test dir, and the gold-applied trees hold none of the hidden tests
    assert (
        not (work["sdk-a"] / "tests/test_client.py").exists() and not (work["proto"] / "tests/test_proto.py").exists()
    )
    for patch in t.glob("steps/*/solution/gold/*.patch"):
        assert "tests/" not in "".join(
            line for line in patch.read_text().splitlines(True) if line.startswith("diff --git")
        )
    # hidden tests: own step plus everything earlier (cumulative)
    s1, s2, s3, s4, s5 = (t / "steps" / n / "tests" for n in STEPS)
    assert (s1 / "hidden/sdk-a/tests/test_client.py").read_text() == "def test_c(): assert True\n"
    assert not (s1 / "hidden/proto").exists()
    assert (s2 / "hidden/sdk-b/tests/feature.rs").exists() and (s2 / "hidden/sdk-a/tests/test_client.py").exists()
    assert (s3 / "hidden/proto/tests/test_proto.py").exists()
    assert (s4 / "hidden/sdk-b/tests/y.rs").exists() and not (s4 / "hidden/sdk-b/tests/x.rs").exists()
    assert (s5 / "hidden/sdk-a/tests/test_late.py").exists() and (s5 / "hidden/proto/tests/test_proto.py").exists()
    # base tree (the start tag) has none of the hidden-only test files
    for repo, rel in (
        ("sdk-a", "tests/test_client.py"),
        ("sdk-b", "tests/feature.rs"),
        ("proto", "tests/test_proto.py"),
    ):
        res = subprocess.run(
            ["git", "cat-file", "-e", f"v1:{rel}"], cwd=root / "DATA/github_data/repos" / f"acme_{repo}"
        )
        assert res.returncode != 0
    env = dict(line.split("=", 1) for line in (s5 / "step.env").read_text().splitlines())
    assert env["STEP"] == STEPS[4] and env["F2P_SDK_A"] == "'tests/test_late.py'"
    assert env["P2P_EARLIER_SDK_A"] == "'tests/test_client.py'" and env["P2P_EARLIER_PROTO"] == "'tests/test_proto.py'"
    assert env["P2P_EARLIER_SDK_B"] == "'tests/feature.rs tests/y.rs'"
    suites = {s["repo"]: s for s in json.loads((t / "tests/suites.json").read_text())["suites"]}
    assert suites["sdk-b"]["parser"] == "cargo" and suites["sdk-a"]["parser"] == "pytest"
    assert suites["proto"]["language"] == "unknown"


def test_instructions(world):
    _, _, _, report = world
    one = (report.task_dir / "steps/m1-add-id-field/instruction.md").read_text()
    assert one.startswith("# Add an id to A") and "/app/sdk-a" in one and "api/*.proto" in one
    assert "diff --git" not in one and "VALUE = 2" not in one
    gen = (report.task_dir / "steps/m2-feature-in-sdk-b/instruction.md").read_text()
    assert gen.startswith("# Feature in sdk-b\n\nMini SRS for Feature in sdk-b.") and "no network" in gen


def test_scripts_are_executable_and_lint_clean(world):
    _, _, _, report = world
    t = report.task_dir
    scripts = [t / "environment/csf-collect", t / "tests/test.sh", *t.glob("steps/*/solution/solve.sh")]
    for s in scripts:
        assert os.access(s, os.X_OK), s
        assert subprocess.run(["bash", "-n", str(s)], capture_output=True).returncode == 0, s
    assert (report.src_dir / "render.py").stat().st_mode & 0o111
    first = (t / "environment/Dockerfile").read_text()
    assert first.count("git clone") == 3 and "/opt/csf/base" in first and "COPY csf-collect" in first


def run_grade(task: Path, logs: Path, *args: str):
    env = {**os.environ, "CSF_LOG_DIR": str(logs), "CSF_TESTS_DIR": str(task / "tests")}
    subprocess.run([sys.executable, str(task / "tests/grade.py"), *args], env=env, check=True, capture_output=True)
    return json.loads((logs / "reward.json").read_text())


def test_grader_emits_reward_keys(world, tmp_path):
    _, _, _, report = world
    task = report.task_dir
    logs = tmp_path / "ok"
    logs.mkdir()
    (logs / "sdk-b.f2p.log").write_text("test a ... ok\ntest b ... ok\n")
    (logs / "sdk-b.f2p.rc").write_text("0\n")
    (logs / "sdk-a.p2p.log").write_text("PASSED tests/test_client.py::test_c\nSKIPPED x\n")
    (logs / "sdk-a.p2p.rc").write_text("0\n")
    r = run_grade(task, logs)
    expected = {"reward", "f2p_total", "f2p_passed", "p2p_total", "p2p_passed"} | {
        f"{g}_{k}__{repo}" for g in ("f2p", "p2p") for k in ("total", "passed") for repo in ("proto", "sdk-a", "sdk-b")
    }
    assert set(r) == expected
    assert (r["reward"], r["f2p_total"], r["f2p_passed"], r["p2p_total"], r["p2p_passed"]) == (1, 2, 2, 1, 1)
    assert r["f2p_total__sdk-b"] == 2 and r["p2p_total__sdk-a"] == 1
    assert json.loads((logs / "ctrf.json").read_text())["results"]["summary"]["tests"] == 3

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "sdk-b.f2p.log").write_text("test a ... ok\ntest b ... FAILED\n")
    (bad / "sdk-b.f2p.rc").write_text("101\n")
    (bad / "sdk-a.p2p.log").write_text("PASSED t::x\n")
    (bad / "sdk-a.p2p.rc").write_text("0\n")
    r = run_grade(task, bad)
    assert (r["reward"], r["f2p_total"], r["f2p_passed"]) == (0, 2, 1)

    none = tmp_path / "none"
    none.mkdir()
    assert run_grade(task, none)["reward"] == 0
    err = tmp_path / "err"
    err.mkdir()
    r = run_grade(task, err, "--error", "boom")
    assert r["reward"] == 0 and r["verifier_error"] == 1


def test_verify_helpers_print(world):
    _, _, _, report = world
    env = {**os.environ, "CSF_TESTS_DIR": str(report.task_dir / "tests")}
    out = subprocess.run(
        [sys.executable, str(report.task_dir / "tests/grade.py"), "excludes", "sdk-a"],
        env=env,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "tests/*" in out
    suites = subprocess.run(
        [sys.executable, str(report.task_dir / "tests/grade.py"), "suites"], env=env, capture_output=True, text=True
    ).stdout.splitlines()
    assert len(suites) == 3 and suites[1].split("\t")[0] == "sdk-a"


def test_export_is_deterministic_and_render_reproduces_steps(world, tmp_path):
    root, manifest, _, report = world
    again = export_csf_task(
        manifest, "t1", root / "DATA", tmp_path / "out" / "cross-repo-change", task_id="csf-toy-001"
    )
    assert tree(again.task_dir) == tree(report.task_dir)
    assert tree(again.src_dir) == tree(report.src_dir)
    before = tree(report.task_dir / "steps")
    subprocess.run([sys.executable, str(report.src_dir / "render.py")], check=True, capture_output=True)
    assert tree(report.task_dir / "steps") == before


def test_milestone_subset_and_default_task_id(world, tmp_path):
    root, manifest, _, _ = world
    out = tmp_path / "o" / "fam"
    r1 = export_csf_task(manifest, "t1", root / "DATA", out, milestones=["M01", "M07"])
    assert r1.task_id == "csf-toy-v1-001" and [s["mid"] for s in r1.built] == ["M01", "M07"]  # a2 is background
    assert export_csf_task(manifest, "t1", root / "DATA", out, milestones=["M07"]).task_id == "csf-toy-v1-002"


def test_cross_repo_only_export_passes_the_checker(world, tmp_path):
    root, manifest, _, _ = world
    out = tmp_path / "o" / "fam"
    report = export_csf_task(manifest, "t1", root / "DATA", out, task_id="csf-toy-002", min_repos=2)
    s = by_mid(report)
    assert [m for m, x in s.items() if x["status"] == "built"] == ["M01"]
    assert s["M02"]["status"] == "background" and s["M07"]["status"] == "background"
    assert report.check.ok, report.check.format()
    assert report.ok


def test_cli_parsers():
    from deepcommit.cli.main import _parse_workspace_args

    a = _parse_workspace_args(["export-task", "m.yaml", "--trial-name", "v1", "--out", "o", "--task-id", "csf-x-001"])
    assert (a.workspace_cmd, a.trial_name, a.out, a.task_id, a.min_repos) == ("export-task", "v1", "o", "csf-x-001", 1)
    b = _parse_workspace_args(["check-task", "dir", "--allow-missing-steps"])
    assert (b.workspace_cmd, b.task_dir, b.allow_missing_steps) == ("check-task", "dir", True)


def test_background_first_report(world):
    _, _, _, report = world
    s = by_mid(report)
    (att1,) = s["M01"]["attached_background"]
    assert att1["repo"] == "sdk-a" and att1["resolution"] == "clean" and att1["files"] == ["sdk_a/client.py"]
    (att5,) = s["M05"]["attached_background"]
    assert att5["repo"] == "sdk-b" and att5["files"] == ["src/newmod.rs"]
    bg = report.background
    assert bg["sdk-a"]["attached"] == 1 and bg["sdk-a"]["applied_to_base"] == 1  # a2
    assert bg["sdk-b"]["attached"] == 1 and bg["sdk-b"]["applied_to_base"] == 1  # b0
    assert all(b["unresolved"] == 0 and b["unused"] == 0 for b in bg.values())
    saved = json.loads((report.src_dir / "export_report.json").read_text())
    assert saved["background"] == bg
    assert "attached_background" in saved["steps"][0]


def test_unresolvable_background_is_reported_not_skipped(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    manifest = build_world(root)
    b = root / "DATA/github_data/repos/acme_sdk-b"
    # a background commit on a file only the blocked M04 creates: conflicts at base and no step retries it
    stray = commit(b, {"tests/x.rs": "#[test] fn x2() {}\n"}, "builds on blocked M04")
    git(b, "tag", "-f", "v2")
    cr = root / "DATA/github_data/workspaces/toy_v1/commit_repos.json"
    data = json.loads(cr.read_text())
    data[stray[:7]] = "sdk-b"
    cr.write_text(json.dumps(data))
    report = export_csf_task(manifest, "t1", root / "DATA", tmp_path / "o" / "f", task_id="csf-toy-003")
    bg = report.background["sdk-b"]
    assert bg["unused"] == 1 and bg["unresolved"] == 0 and bg["deferred_commits"] == [stray]
    assert bg["applied_to_base"] == 1 and bg["attached"] == 1


# ------------------------------------------------------------------ contract-only repo

CONTRACT_MANIFEST = """\
workspace_id: cwx_v1
repos:
  - name: api
    owner: acme
    repo: api
    role: contract
    contract_only: true
    start: v1
    end: v2
    repo_src_dirs: [proto/]
    test_dirs: ["tests/**"]
    contract_paths: ["proto/**/*.proto"]
  - name: cl-a
    owner: acme
    repo: cl-a
    role: sdk
    start: v1
    end: v2
    repo_src_dirs: [cl_a/]
    test_dirs: ["tests/**"]
  - name: cl-b
    owner: acme
    repo: cl-b
    role: sdk
    start: v1
    end: v2
    repo_src_dirs: [src/]
    test_dirs: ["tests/**"]
"""


def build_contract_world(root: Path) -> Path:
    data = root / "DATA"
    repos = data / "github_data" / "repos"
    for name in ("api", "cl-a", "cl-b"):
        (repos / f"acme_{name}").mkdir(parents=True)
        git(repos / f"acme_{name}", "init", "-q", "-b", "main")
    api, a, b = (repos / f"acme_{n}" for n in ("api", "cl-a", "cl-b"))
    commit(
        api,
        {"proto/svc.proto": "v1\n", "src/server.rs": "s0\n", "tests/t.rs": "t\n", "LICENSE": "Apache License\n"},
        "base",
    )
    commit(
        a,
        {
            "pyproject.toml": "[project]\nname='a'\n",
            "cl_a/client.py": "A = 1\n",
            "tools/generate_grpc_client.sh": "#!/bin/sh\n",
        },
        "base",
    )
    commit(b, {"Cargo.toml": "[package]\nname='b'\n", "src/lib.rs": "B = 1\n"}, "base")
    for r in (api, a, b):
        git(r, "tag", "v1")
    sha = {}
    sha["s0"] = commit(api, {"proto/old.proto": "old\n"}, "s0 contract, in no milestone, older than every step")
    sha["s1"] = commit(api, {"proto/svc.proto": "v2\n", "src/server.rs": "s1\n"}, "s1 contract + impl mixed")
    sha["s2"] = commit(api, {"src/server.rs": "s2\n"}, "s2 impl only")
    sha["s3"] = commit(api, {"proto/bg.proto": "bg\n"}, "s3 contract, in no milestone")
    sha["s4"] = commit(api, {"proto/svc.proto": "v3\n"}, "s4 contract")
    sha["a1"] = commit(a, {"cl_a/client.py": "A = 2\n", "tests/test_c.py": "def test_c(): pass\n"}, "a1")
    sha["b1"] = commit(b, {"src/lib.rs": "B = 2\n", "tests/t.rs": "#[test] fn t() {}\n"}, "b1")
    sha["b2"] = commit(b, {"src/lib.rs": "B = 3\n", "tests/t2.rs": "#[test] fn t2() {}\n"}, "b2")
    for r in (api, a, b):
        git(r, "tag", "v2")
    wdir = data / "github_data" / "workspaces" / "cwx_v1"
    wdir.mkdir(parents=True)
    owner = {"s": "api", "a": "cl-a", "b": "cl-b"}
    (wdir / "commit_repos.json").write_text(json.dumps({v[:7]: owner[k[0]] for k, v in sha.items()}))
    res = data / "result" / "cwx_v1_t1"
    res.mkdir(parents=True)
    rows = [
        ("M01", "Contract id", "s1;a1", "api;cl-a"),
        ("M02", "Client only", "b1", "cl-b"),
        ("M03", "Contract and b", "s4;b2", "api;cl-b"),
    ]
    lines = ["id,title,category,commits,mini_srs,confidence_score,repos"]
    for mid, title, commits, repos_col in rows:
        shas = ";".join(sha[c][:7] for c in commits.split(";"))
        lines.append(f'{mid},{title},Feature,{shas},"Mini SRS for {title}.",0.9,{repos_col}')
    (res / "milestones.csv").write_text("\n".join(lines) + "\n")
    (res / "dependencies.csv").write_text("prerequisite_id,dependant_id,rationale\n")
    manifest = root / "cwx.workspace.yaml"
    manifest.write_text(CONTRACT_MANIFEST)
    (root / "sha.json").write_text(json.dumps(sha))
    return manifest


@pytest.fixture(scope="module")
def cworld(tmp_path_factory):
    root = tmp_path_factory.mktemp("cworld")
    manifest = build_contract_world(root)
    report = export_csf_task(manifest, "t1", root / "DATA", root / "out" / "fam", task_id="csf-cwx-001")
    return root, manifest, report


def test_contract_only_manifest_validation():
    import yaml

    from deepcommit.multirepo import WorkspaceError, parse_workspace

    data = yaml.safe_load(CONTRACT_MANIFEST)
    ws = parse_workspace(data)
    assert ws.get("api").contract_only and ws.get("api").to_dict()["contract_only"] is True
    assert not ws.get("cl-a").contract_only
    data["repos"][1]["contract_only"] = True  # an sdk cannot be contract-only
    with pytest.raises(WorkspaceError):
        parse_workspace(data)


def test_contract_only_steps_and_gold(cworld, tmp_path):
    root, _, report = cworld
    sha = json.loads((root / "sha.json").read_text())
    s = by_mid(report)
    assert [s[m]["status"] for m in ("M01", "M02", "M03")] == ["built"] * 3
    t = report.task_dir
    gold = {
        n: sorted(p.name for p in (t / "steps" / n / "solution/gold").iterdir())
        for n in (d.name for d in (t / "steps").iterdir())
    }
    assert gold["m1-contract-id"] == ["api.patch", "cl-a.patch"]  # cross-repo
    assert gold["m2-client-only"] == ["cl-b.patch"]  # nothing new on the contract
    assert gold["m3-contract-and-b"] == ["api.patch", "cl-b.patch"]
    for patch in t.glob("steps/*/solution/gold/api.patch"):
        text = patch.read_text()
        assert "src/server.rs" not in text and "proto/" in text
    # the commit that no milestone owns lands in the gold of the step whose prefix includes it
    assert s["M01"]["contract_background"] == []
    assert s["M03"]["contract_background"] == [{"repo": "api", "commit": sha["s3"], "files": ["proto/bg.proto"]}]
    assert "proto/bg.proto" in (t / "steps/m3-contract-and-b/solution/gold/api.patch").read_text()
    assert (
        report.background["api"]["mode"] == "contract-only"
        and report.background["api"]["contract_background_commits"] == 1
    )
    assert not list(t.glob("steps/*/tests/hidden/api"))
    # gold applies in sequence onto the sparse base (the start tree restricted to the contract paths + base.patch)
    work = tmp_path / "api"
    git(tmp_path, "clone", "-q", str(root / "DATA/github_data/repos/acme_api"), str(work))
    git(work, "checkout", "-q", "v1")
    for f in ("src/server.rs", "tests/t.rs", "LICENSE"):
        (work / f).unlink()
    git(tmp_path, "apply", "--include=api/*", str(t / "environment/base.patch"))  # rooted at /app: a/api/...
    assert (work / "proto/old.proto").read_text() == "old\n"  # older background contract change is in the base
    for n in ("m1-contract-id", "m3-contract-and-b"):
        patch = t / "steps" / n / "solution/gold/api.patch"
        git(work, "apply", "--check", str(patch))
        git(work, "apply", str(patch))
    assert (work / "proto/svc.proto").read_text() == "v3\n" and (work / "proto/bg.proto").read_text() == "bg\n"
    assert sorted(p.name for p in (work / "proto").iterdir()) == ["bg.proto", "old.proto", "svc.proto"]
    assert "old.proto" not in (t / "steps/m1-contract-id/solution/gold/api.patch").read_text()
    bg = report.background["api"]
    assert bg["contract_base_commit"] == sha["s0"] and bg["contract_base_background_commits"] == 1
    assert s["M03"]["contract_background_count"] == 1 and "contract_gold_large" not in s["M03"]


def test_large_contract_gold_is_flagged(cworld, tmp_path, monkeypatch):
    import deepcommit.export.csf_task as ct

    root, manifest, _ = cworld
    monkeypatch.setattr(ct, "CONTRACT_GOLD_FLAG", 1)
    report = export_csf_task(manifest, "t1", root / "DATA", tmp_path / "o" / "f", task_id="csf-cwx-009")
    s = by_mid(report)
    assert s["M01"]["contract_gold_large"] == {"api": s["M01"]["contract_gold_lines"]["api"]}
    assert "FLAG contract gold above 1 lines" in (report.task_dir / "README.md").read_text()


def test_contract_only_task_files_and_checker(cworld):
    _, _, report = cworld
    t = report.task_dir
    toml = tomllib.loads((t / "task.toml").read_text())
    assert toml["metadata"]["contract_only_repos"] == ["api"]
    assert toml["metadata"]["repo_roles"]["api"] == "contract" and toml["metadata"]["repos"] == ["api", "cl-a", "cl-b"]
    suites = json.loads((t / "tests/suites.json").read_text())
    assert [x["repo"] for x in suites["suites"]] == ["cl-a", "cl-b"]
    assert suites["contract_only"]["api"]["mode"] == "contract-only"
    assert "api" in (t / "environment/csf-collect").read_text().split("for repo in")[1].split(";")[0].split()
    assert "git ls-files -z -- ':(glob)proto/**/*.proto'" in (t / "environment/Dockerfile").read_text()
    inst = (t / "steps/m1-contract-id/instruction.md").read_text()
    assert "`proto/svc.proto`" in inst and "tools/generate_grpc_client.sh" in inst and "diff --git" not in inst
    assert "sparse checkout" in inst
    assert "contract-only" in (t / "README.md").read_text()
    assert report.check.ok, report.check.format()  # >= 1 cross-repo step, no suite needed for the contract repo
    env = {"CSF_TESTS_DIR": str(t / "tests"), **os.environ}
    out = subprocess.run([sys.executable, str(t / "tests/grade.py"), "repos"], env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["cl-a", "cl-b", "api"]
    out = subprocess.run([sys.executable, str(t / "tests/grade.py"), "suites"], env=env, capture_output=True, text=True)
    assert len(out.stdout.splitlines()) == 2


def test_contract_only_export_is_deterministic(cworld, tmp_path):
    root, manifest, report = cworld
    again = export_csf_task(manifest, "t1", root / "DATA", tmp_path / "out" / "fam", task_id="csf-cwx-001")
    assert tree(again.task_dir) == tree(report.task_dir) and tree(again.src_dir) == tree(report.src_dir)


# ------------------------------------------------------------------ split commits

SPLIT_MANIFEST = """\
workspace_id: swx_v1
repos:
  - name: lib
    owner: acme
    repo: lib
    role: sdk
    start: v1
    end: v2
    repo_src_dirs: [lib_/]
    test_dirs: ["tests/**"]
  - name: app
    owner: acme
    repo: app
    role: sdk
    start: v1
    end: v2
    repo_src_dirs: [app_/]
    test_dirs: ["tests/**"]
"""


def build_split_world(root: Path, mismatch: bool = False) -> tuple[Path, dict]:
    data = root / "DATA"
    repos = data / "github_data" / "repos"
    for name in ("lib", "app"):
        (repos / f"acme_{name}").mkdir(parents=True)
        git(repos / f"acme_{name}", "init", "-q", "-b", "main")
    lib, app = repos / "acme_lib", repos / "acme_app"
    commit(lib, {"pyproject.toml": "[project]\nname='l'\n", "lib_/core.py": "C = 1\n"}, "base")
    commit(app, {"pyproject.toml": "[project]\nname='p'\n", "app_/main.py": "M = 1\n"}, "base")
    for r in (lib, app):
        git(r, "tag", "v1")
    files = {"lib_/a.py": "A = 1\n", "lib_/b.py": "B = 1\n", "tests/test_ab.py": "def test_ab(): pass\n"}
    if mismatch:
        files["lib_/c.py"] = "C = 1\n"  # in the original, not in any sub-commit
    orig = commit(lib, files, "X: the original commit (replaced by its split chain)")
    m2 = commit(app, {"app_/main.py": "M = 2\n", "tests/test_m.py": "def test_m(): pass\n"}, "app change")
    for r in (lib, app):
        git(r, "tag", "v2")
    # the trial's clone of lib has the split branch: base -> sub1 -> sub2 -> test commit
    trial = data / "trial_space" / "swx_v1_t1"
    clone = trial / "repo_data" / "lib"
    clone.parent.mkdir(parents=True)
    git(clone.parent, "clone", "-q", str(lib), str(clone))
    git(clone, "checkout", "-q", "-b", f"split-{orig[:7]}", "v1")
    sub1 = commit(clone, {"lib_/a.py": "A = 1\n"}, "sub 1")
    sub2 = commit(clone, {"lib_/b.py": "B = 1\n"}, "sub 2")
    itc = commit(clone, {"tests/test_ab.py": "def test_ab(): pass\n"}, "tests")
    git(clone, "checkout", "-q", "main")
    out = trial / "stage4_decomp" / "output"
    meta = out / "commit_splits" / f"split-{orig[:7]}"
    meta.mkdir(parents=True)
    (meta / "METADATA.json").write_text(
        json.dumps(
            {
                "original_commit": {"sha": orig, "short_sha": orig[:7]},
                "split_result": {
                    "src_commits": [{"sha": sub1, "short_sha": sub1[:7]}, {"sha": sub2, "short_sha": sub2[:7]}],
                    "integration_test_commit": {"sha": itc, "short_sha": itc[:7]},
                },
            }
        )
    )
    wdir = data / "github_data" / "workspaces" / "swx_v1"
    wdir.mkdir(parents=True)
    (wdir / "commit_repos.json").write_text(json.dumps({m2[:7]: "app"}))
    lines = [
        "id,title,category,commits,mini_srs,confidence_score,repos,integration_test_commit",
        f'M01_sub-01,First half,Feature,{sub1[:7]},"Mini one.",0.9,lib,{itc[:7]}',
        f'M01_sub-02,Second half,Feature,{sub2[:7]},"Mini two.",0.9,lib,',
        f'M02,App,Feature,{m2[:7]},"Mini app.",0.9,app,',
    ]
    (out / "milestones.csv").write_text("\n".join(lines) + "\n")
    (out / "dependencies.csv").write_text("prerequisite_id,dependant_id,rationale\nM01_sub-01,M02,r\n")
    manifest = root / "swx.workspace.yaml"
    manifest.write_text(SPLIT_MANIFEST)
    return manifest, {"orig": orig, "sub1": sub1, "sub2": sub2, "itc": itc, "out": out}


def export_split(tmp_path, mismatch=False):
    manifest, info = build_split_world(tmp_path, mismatch)
    report = export_csf_task(
        manifest, "t1", tmp_path / "DATA", tmp_path / "o" / "f", task_id="csf-swx-001",
        milestones_csv=info["out"] / "milestones.csv", dependencies_csv=info["out"] / "dependencies.csv",
    )  # fmt: skip
    return report, info


def test_split_commits_are_applied_as_their_chain(tmp_path):
    report, info = export_split(tmp_path)
    s = by_mid(report)
    assert s["M01_sub-01"]["status"] == "built" and s["M02"]["status"] == "built"
    # sub-02 has no tests of its own: background, so it is in the base, not a step
    assert s["M01_sub-02"]["status"] == "background"
    assert b"lib_/b.py" in (report.task_dir / "environment/base.patch").read_bytes()
    t = report.task_dir
    gold = (t / "steps/m1-first-half/solution/gold/lib.patch").read_text()
    assert "lib_/a.py" in gold and "lib_/b.py" not in gold and "tests/" not in gold
    # the integration-test commit supplies the hidden test
    assert (t / "steps/m1-first-half/tests/hidden/lib/tests/test_ab.py").exists()
    (sp,) = report.splits
    assert sp["union_ok"] and sp["original"] == info["orig"] and len(sp["chain"]) == 3
    (rec,) = s["M01_sub-01"]["split_commits"]
    assert rec["union_ok"] and rec["original"] == info["orig"] and len(rec["sub_commits"]) == 2
    assert s["M01_sub-02"].get("split_commits") is None
    saved = json.loads((report.src_dir / "export_report.json").read_text())
    assert saved["splits"][0]["union_ok"] is True


def test_split_union_mismatch_is_reported(tmp_path):
    report, _ = export_split(tmp_path, mismatch=True)
    assert report.splits[0]["union_ok"] is False
    assert any("UNION MISMATCH" in n for n in report.notes)
