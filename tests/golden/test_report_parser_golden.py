"""Wave 2 (T2) — golden/characterization tests for the framework report parsers.

Each ``parse_<framework>_report`` reads a recorded report and normalizes it to the
standardized shape ``{tests, summary, duration, _framework, ...}``. We snapshot that
standardized projection (dropping ``_raw_data``, which just echoes the input) and pin
it as a golden. Regenerate intentionally with:

    REGEN_GOLDEN=1 uv run pytest tests/golden/test_report_parser_golden.py

Fixtures live in ``tests/fixtures/report_parser/`` and were synthesized from each
parser's documented format and verified by executing the real parser (no real
samples exist in-repo, so there is nothing to scrub). The ``tests`` list is compared
canonically (sorted by nodeid) so the snapshot is PYTHONHASHSEED-proof.
"""

import json
import os
from pathlib import Path

import pytest

from deepcommit.harness.test_runner.core import report_parser as rp

FIX = Path(__file__).parent.parent / "fixtures" / "report_parser"
VALID_OUTCOMES = {"passed", "failed", "skipped", "error", "xfailed", "xpassed"}

# (id, parser fn, fixture filename)
CASES = [
    ("pytest", rp.parse_pytest_report, "pytest.json"),
    ("go_test", rp.parse_go_test_report, "go_test.jsonl"),
    ("jest", rp.parse_jest_report, "jest.json"),
    ("vitest", rp.parse_vitest_report, "vitest.json"),
    ("mocha", rp.parse_mocha_report, "mocha.json"),
    ("playwright", rp.parse_playwright_report, "playwright.json"),
    ("ginkgo", rp.parse_ginkgo_report, "ginkgo.json"),
    ("maven", rp.parse_maven_report, "maven_default.log"),
    ("cargo", rp.parse_cargo_report, "cargo.log"),
    ("django", rp.parse_django_report, "django_default.log"),
    ("nushell", rp.parse_nushell_script_report, "nushell.log"),
    # Real-derived (harvested + scrubbed from a real scikit-learn run): exercises
    # the real pytest outcomes our synthetic fixture missed — xfailed / xpassed
    # (passed through raw here; the downstream TestClassifier remaps them).
    ("pytest_real_outcomes", rp.parse_pytest_report, "pytest_real_outcomes.json"),
]


def _project(out):
    """Standardized projection: drop _raw_data (echoes input); canonicalize test order."""
    proj = {k: v for k, v in out.items() if k != "_raw_data"}
    proj["tests"] = sorted(proj.get("tests", []), key=lambda t: (t["nodeid"], t["outcome"]))
    return proj


@pytest.mark.parametrize("name,fn,fixture", CASES, ids=[c[0] for c in CASES])
def test_parser_golden(name, fn, fixture):
    proj = _project(fn(FIX / fixture))
    gold_path = FIX / f"{fixture}.golden.json"
    if os.environ.get("REGEN_GOLDEN"):
        gold_path.write_text(json.dumps(proj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    golden = json.loads(gold_path.read_text())
    assert proj == golden

    # invariants — guard against a careless REGEN pinning garbage
    assert proj["tests"], "fixture should exercise at least one test"
    assert all(set(t) >= {"nodeid", "outcome"} for t in proj["tests"])
    assert all(t["outcome"] in VALID_OUTCOMES for t in proj["tests"])
    s = proj["summary"]
    assert set(s) >= {"passed", "failed", "skipped", "error", "total"}
    assert all(isinstance(s[k], int) for k in ("passed", "failed", "skipped", "error", "total"))


# ---- dispatcher: parse_test_report routing + short-circuit ----
def test_dispatcher_routes_to_framework_parser():
    # jest.json is NOT in standardized shape ("testResults", not "tests") -> routed to parse_jest_report.
    assert rp.parse_test_report(FIX / "jest.json", "jest") == rp.parse_jest_report(FIX / "jest.json")


def test_dispatcher_short_circuits_already_standardized_json():
    # pytest.json IS already standardized (tests:[{nodeid,outcome}]) -> returned as-is,
    # ignoring the framework arg entirely (no re-parse).
    out = rp.parse_test_report(FIX / "pytest.json", "go_test")
    fixture = json.loads((FIX / "pytest.json").read_text())
    # Full raw passthrough — assert the whole dict, not just tests, so a regression that
    # returned only {"tests": ...} (dropping summary/duration/metadata) would be caught.
    assert out == fixture


def test_dispatcher_missing_file_returns_empty():
    out = rp.parse_test_report(FIX / "does_not_exist.json", "pytest")
    assert out["tests"] == []
    assert out["summary"]["total"] == 0


# ---- merge_test_reports: cross-mode merge priority (skipped loses to a real run) ----
def _std_report(tmp_path, name, tests):
    p = tmp_path / name
    p.write_text(json.dumps({"tests": tests}))
    return p


def test_merge_prefers_non_skipped_and_unions(tmp_path):
    f1 = _std_report(
        tmp_path, "report_a.json", [{"nodeid": "t1", "outcome": "skipped"}, {"nodeid": "t2", "outcome": "passed"}]
    )
    f2 = _std_report(
        tmp_path, "report_b.json", [{"nodeid": "t1", "outcome": "passed"}, {"nodeid": "t3", "outcome": "failed"}]
    )
    out = tmp_path / "merged.json"
    assert rp.merge_test_reports([f1, f2], out) is True
    merged = json.loads(out.read_text())
    # t1 was skipped in f1 but a real run (passed) in f2 -> the non-skipped result wins.
    assert {t["nodeid"]: t["outcome"] for t in merged["tests"]} == {"t1": "passed", "t2": "passed", "t3": "failed"}
    assert merged["summary"] == {"passed": 2, "failed": 1, "skipped": 0, "error": 0, "total": 3}


def test_merge_no_valid_reports_returns_false(tmp_path):
    assert rp.merge_test_reports([tmp_path / "nope.json"], tmp_path / "out.json") is False


# ---- real-data regressions (harvested + scrubbed from real EvoClaw runs) ----
def test_jest_whole_suite_error_emits_error_entry():
    # Regression for the real element-web false-negative: a jest suite that fails
    # to COMPILE (empty assertionResults + status "failed", numRuntimeErrorTestSuites>0)
    # must surface as an `error` entry, not vanish. Before the fix this returned 0 tests.
    out = rp.parse_jest_report(FIX / "jest_suite_error.json")
    errors = [t for t in out["tests"] if t["outcome"] == "error"]
    assert len(errors) == 1
    assert errors[0]["nodeid"].endswith("RoomListStoreV3-test.ts")
    assert "Test suite failed to run" in errors[0]["message"]
    assert out["summary"]["error"] == 1
    # the sibling passing test is still present (not clobbered by the error branch)
    assert any(t["outcome"] == "passed" for t in out["tests"])


@pytest.mark.parametrize(
    "fixture,fn",
    [("cargo_build_fail.log", rp.parse_cargo_report), ("maven_build_fail.log", rp.parse_maven_report)],
    ids=["cargo", "maven"],
)
def test_build_failure_parses_to_empty(fixture, fn):
    # Real compile/build failures (no test ran) currently parse to ZERO tests.
    # Pinned as characterization: documents the known false-negative (a broken
    # build reads as "no tests" rather than "error") — the same family as the
    # jest fix above, tracked for a future cross-framework fix (tests/unit/README).
    out = fn(FIX / fixture)
    assert out["tests"] == []
    assert out["summary"]["total"] == 0
