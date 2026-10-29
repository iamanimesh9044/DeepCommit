"""SRS prompt preparation in workspace mode (real tmp git repos via the testbed builder) and N=1 identity."""

import json
import shutil

from deepcommit.harness.gen_srs.prepare_prompt import SRSPromptPreparer
from deepcommit.harness.prepare_repo import multirepo_testbed as mt
from tests.unit.test_multirepo_harness_testbed import ws  # noqa: F401  (fixture)


def _prepare(ws, tmp_path):  # noqa: F811
    mt.build_workspace_testbed(
        workspace_dir=ws["wsdir"], milestone_csv=ws["mcsv"], dependencies_csv=ws["dcsv"], output_dir=ws["out"]
    )
    out = ws["out"]
    shutil.copy(ws["mcsv"], out / "milestones.csv")
    cdir = out / "test_results" / "M1" / "benchmark"
    cdir.mkdir(parents=True)
    (cdir / "M1_classification.json").write_text(
        json.dumps(
            {"stable_classification": {"fail_to_pass": ["alpha::tests/test_lib.py::test_n", "beta::TestGoThing"]}}
        )
    )
    return SRSPromptPreparer(
        work_dir=str(out), milestone_id="M1", patches_dir=str(tmp_path / "patches"), milestone_container="ctr"
    )


def test_workspace_prompt(ws, tmp_path):  # noqa: F811
    prep = _prepare(ws, tmp_path)
    assert prep.workspace_mode
    prompt = prep.generate_output()
    assert "**alpha** (`/workspace/alpha`, 1 commits):" in prompt
    assert "**beta** (`/workspace/beta`, 1 commits):" in prompt
    assert "Interface contract" in prompt and "Boundary-name rule" in prompt
    assert "git -C <repo_path> diff" in prompt and "{%" not in prompt
    ids = {t["test_id"]: t for t in prep.f2p_tests}
    assert ids["alpha::tests/test_lib.py::test_n"]["repo"] == "alpha"
    assert ids["alpha::tests/test_lib.py::test_n"]["source_file"] == "tests/test_lib.py"
    assert ids["beta::TestGoThing"]["repo"] == "beta" and ids["beta::TestGoThing"]["source_file"] is None
    assert (tmp_path / "patches").is_dir() and len(list((tmp_path / "patches").glob("*.patch"))) == 2


def test_single_repo_prompt_is_one_repo_workspace(ws, tmp_path):  # noqa: F811
    prep = _prepare(ws, tmp_path)
    (prep.testbed_path / "manifest.json").unlink()  # N=1 testbeds have no manifest
    prep2 = SRSPromptPreparer(
        work_dir=str(prep.work_dir),
        milestone_id="M2",
        patches_dir=str(tmp_path / "p2"),
        milestone_container="ctr",
        git_repo=str(prep.testbed_path / "alpha"),
    )
    assert not prep2.workspace_mode
    prompt = prep2.generate_output()
    assert "**repo** (`/testbed`, " in prompt and "/workspace/" not in prompt and "{%" not in prompt


def test_extract_f2p_namespaced_ids(tmp_path):
    from deepcommit.harness.filter_tests.extract_f2p_tests import FailToPassExtractor, TestIDParser

    assert TestIDParser.parse("a/t.py::C::m")["class_name"] == "C"  # N=1 unchanged
    assert TestIDParser.parse("alpha::t.py::C::m", ["alpha"]) == {
        "repo": "alpha",
        "file_path": "t.py",
        "class_name": "C",
        "function_name": "m",
    }
    assert TestIDParser.parse("beta::TestGo", ["alpha", "beta"])["file_path"] is None

    bench = tmp_path / "test_results" / "M1" / "benchmark"
    bench.mkdir(parents=True)
    (bench / "M1_classification.json").write_text(
        json.dumps({"stable_classification": {"fail_to_pass": ["beta::TestGo", "alpha::t.py::test_x"]}})
    )
    (tmp_path / "testbed").mkdir()
    (tmp_path / "testbed" / "manifest.json").write_text(json.dumps({"repos": {"alpha": {}, "beta": {}}}))
    FailToPassExtractor(str(tmp_path), "M1", extract_code=True).extract()  # falls back to ids-only
    meta = json.loads((bench / "f2p_tests" / "metadata.json").read_text())
    assert meta["extraction_mode"] == "test_ids_only"
    assert {(t["test_id"], t["repo"]) for t in meta["tests"]} == {
        ("beta::TestGo", "beta"),
        ("alpha::t.py::test_x", "alpha"),
    }
