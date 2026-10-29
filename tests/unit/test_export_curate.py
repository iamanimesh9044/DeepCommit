"""Curate / fold / id-level grading of the measured CSF task pipeline."""

import importlib.util
import json
from pathlib import Path

from deepcommit.export import curate, fold

GRADE = Path(curate.__file__).parent / "templates" / "grade.py"


def _grade_module():
    spec = importlib.util.spec_from_file_location("csf_grade", GRADE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _step(task: Path, name: str, f2p_files: dict):
    d = task / "steps" / name / "tests"
    d.mkdir(parents=True)
    lines = [f"STEP={name}"] + [f"F2P_{r.upper().replace('-', '_')}='{' '.join(fs)}'" for r, fs in f2p_files.items()]
    (d / "step.env").write_text("\n".join(lines) + "\n")


def test_f2p_is_own_failing_then_passing_and_p2p_is_earlier(tmp_path):
    _step(tmp_path, "m1-a", {"qdrant-client": ["tests/test_a.py"]})
    _step(tmp_path, "m2-b", {"qdrant-client": ["tests/test_b.py"]})
    a, b = "qdrant-client::tests/test_a.py::t", "qdrant-client::tests/test_b.py::t"
    other = "qdrant-client::tests/test_c.py::t"
    measure = {
        "steps": [
            {"step": "m1-a", "pre": {a: "failed"}, "post": [{a: "passed", other: "passed"}] * 2},
            {"step": "m2-b", "pre": {b: "failed"}, "post": [{a: "passed", b: "passed", other: "passed"}] * 2},
        ]
    }
    res = curate.curate(measure, tmp_path)
    assert res[0].f2p == [a] and res[0].p2p == []
    assert res[1].f2p == [b] and res[1].p2p == [a]  # test_c belongs to no step's test files


def test_flaky_test_is_excluded(tmp_path):
    _step(tmp_path, "m1-a", {"qdrant-client": ["tests/test_a.py"]})
    a = "qdrant-client::tests/test_a.py::t"
    b = "qdrant-client::tests/test_a.py::flaky"
    measure = {"steps": [{"step": "m1-a", "pre": {}, "post": [{a: "passed", b: "passed"}, {a: "passed", b: "failed"}]}]}
    assert curate.curate(measure, tmp_path)[0].f2p == [a]


def test_cargo_ids_are_owned_by_module_path(tmp_path):
    _step(tmp_path, "m1-a", {"rust-client": ["tests/snippet_tests/test_x.rs"]})
    own = "rust-client::test_snippets::snippet_tests::test_x::test_x"
    lib = "rust-client::lib::util::tests::unrelated"
    measure = {"steps": [{"step": "m1-a", "pre": {}, "post": [{own: "passed", lib: "passed"}] * 2}]}
    assert curate.curate(measure, tmp_path)[0].f2p == [own]


def test_ungradable_step_folds_into_successor():
    cur = [
        curate.StepCuration("m1-a", f2p=[]),
        curate.StepCuration("m2-b", f2p=["x"]),
        curate.StepCuration("m3-c", f2p=[]),
    ]
    assert fold.plan(cur) == [["m1-a", "m2-b", "m3-c"]]
    cur[0].f2p = ["y"]
    assert fold.plan(cur) == [["m1-a"], ["m2-b", "m3-c"]]


def test_grade_uses_only_listed_ids(tmp_path, monkeypatch):
    tests, log = tmp_path / "tests", tmp_path / "log"
    tests.mkdir()
    log.mkdir()
    (tests / "suites.json").write_text(json.dumps({"suites": [{"repo": "r", "parser": "pytest", "run_cmd": "x"}]}))
    (tests / "ids.json").write_text(json.dumps({"f2p": ["r::t::new"], "p2p": ["r::t::old"]}))
    (log / "r.f2p.log").write_text("PASSED t::new\nFAILED t::unrelated\n")
    (log / "r.f2p.rc").write_text("1")
    (log / "r.p2p.log").write_text("PASSED t::old\n")
    (log / "r.p2p.rc").write_text("0")
    monkeypatch.setenv("CSF_TESTS_DIR", str(tests))
    monkeypatch.setenv("CSF_LOG_DIR", str(log))
    g = _grade_module()
    g.grade()
    rewards = json.loads((log / "reward.json").read_text())
    assert rewards["reward"] == 1 and rewards["f2p_total"] == 1 and rewards["p2p_total"] == 1
    (log / "r.p2p.log").write_text("FAILED t::old\n")
    g.grade()
    assert json.loads((log / "reward.json").read_text())["reward"] == 0
