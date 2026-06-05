#!/usr/bin/env python3
"""Compute the scenario-level base baseline for Module 2 hard gates."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from deepcommit.harness.test_runner.core.report_parser import parse_test_report
from deepcommit.harness.test_runner.core.types import BaseValidationConfig
from deepcommit.harness.test_runner.single_state_runner import SingleStateTestRunner


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _failure_reason(test: Dict[str, Any]) -> str:
    for key in ("message", "error", "failure", "longrepr", "stderr", "stdout"):
        value = test.get(key)
        if value:
            text = str(value).strip().splitlines()[0]
            return text[:300]
    return str(test.get("nodeid") or test.get("name") or "unknown failure")[:300]


def _fail_signatures(output_file: Optional[Path], framework: str, limit: int = 20) -> List[Dict[str, Any]]:
    if not output_file or not output_file.exists():
        return []
    try:
        parsed = parse_test_report(output_file, framework)
    except Exception:
        return []
    counter: Counter[str] = Counter()
    for test in parsed.get("tests", []):
        if test.get("outcome") in ("failed", "error"):
            counter[_failure_reason(test)] += 1
    return [{"reason": reason, "count": count} for reason, count in counter.most_common(limit)]


def _infer_framework_from_base_config(path: Path, fallback: str) -> str:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return fallback
    modes = data if isinstance(data, list) else data.get("modes", []) if isinstance(data, dict) else []
    joined = " ".join(str(mode.get("test_cmd", "")) for mode in modes if isinstance(mode, dict)).lower()
    if "runtests.py" in joined:
        return "django_runtests"
    if "go test" in joined:
        return "go_test"
    if "cargo test" in joined:
        return "cargo"
    if "mvn " in joined:
        return "maven"
    if "gradle" in joined:
        return "gradle"
    if "jest" in joined:
        return "jest"
    if "mocha" in joined:
        return "mocha"
    if "pytest" in joined:
        return "pytest"
    return fallback


def _load_baseline_config(work_dir: Path, language: str, test_framework: str) -> BaseValidationConfig:
    base_config = work_dir.parent / "base" / "dockerfiles" / "test_config.json"
    if base_config.exists():
        framework = _infer_framework_from_base_config(base_config, test_framework)
        return BaseValidationConfig.from_file(base_config, language=language, test_framework=framework)
    return BaseValidationConfig.from_defaults(language, test_framework)


def compute_base_baseline(
    *,
    work_dir: Path,
    base_image: str,
    language: str = "python",
    test_framework: str = "pytest",
    test_dir: str = "test/",
    timeout: int = 1800,
    workers: int = 1,
    force: bool = False,
) -> Dict[str, Any]:
    """Run the base image once and write `{work_dir.parent}/base/baseline_metrics.json`."""
    work_dir = work_dir.resolve()
    base_dir = work_dir.parent / "base"
    output_path = base_dir / "baseline_metrics.json"
    if output_path.exists() and not force:
        return json.loads(output_path.read_text())

    result_dir = base_dir / "baseline_results"
    result_dir.mkdir(parents=True, exist_ok=True)

    config = _load_baseline_config(work_dir, language, test_framework)
    runner = SingleStateTestRunner(base_image, config)

    start = time.time()
    result = runner.run_tests(
        test_dir=test_dir,
        output_dir=result_dir,
        timeout=timeout,
        workers=workers,
        verbose=True,
    )
    duration = time.time() - start

    summary = result.summary or {}
    passed = int(summary.get("passed", 0) or 0)
    failed = int(summary.get("failed", 0) or 0)
    skipped = int(summary.get("skipped", 0) or 0)
    error = int(summary.get("error", summary.get("errors", 0)) or 0)
    xfailed = int(summary.get("xfailed", 0) or 0)
    total = int(summary.get("total", 0) or 0)
    if total == 0:
        total = passed + failed + skipped + error + xfailed

    metrics = {
        "schema_version": 1,
        "scenario": f"{work_dir.parent.name}/{work_dir.name}",
        "base_image": base_image,
        "computed_at": _utc_now_iso(),
        "compute_duration_s": round(duration, 2),
        "collection": {
            "total_collected": total,
            "collection_errors_count": int(summary.get("collection_errors", 0) or 0),
            "collection_error_signatures": [],
        },
        "exec_results": {
            "total_run": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "error": error,
            "xfailed": xfailed,
        },
        "fail_signatures": _fail_signatures(result.output_file, result.framework),
        "tests_run_command": config.get_run_cmd("default"),
        "tests_run_at_state": "base_image_default",
        "runner_success": result.success,
        "runner_message": result.message,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(metrics, indent=2))
    return metrics


def ensure_base_baseline(
    *,
    work_dir: Path,
    base_image: str,
    language: str = "python",
    test_framework: str = "pytest",
    test_dir: str = "test/",
    timeout: int = 1800,
    workers: int = 1,
) -> Path:
    """Ensure baseline_metrics.json exists and return its path."""
    path = work_dir.resolve().parent / "base" / "baseline_metrics.json"
    if not path.exists():
        compute_base_baseline(
            work_dir=work_dir,
            base_image=base_image,
            language=language,
            test_framework=test_framework,
            test_dir=test_dir,
            timeout=timeout,
            workers=workers,
        )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute Module 2 base baseline metrics")
    parser.add_argument("--work-dir", type=Path, required=True, help="Harness scenario work dir")
    parser.add_argument("--base-image", required=True, help="Base Docker image")
    parser.add_argument("--language", default="python")
    parser.add_argument("--test-framework", default="pytest")
    parser.add_argument("--test-dir", default="test/")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    metrics = compute_base_baseline(
        work_dir=args.work_dir,
        base_image=args.base_image,
        language=args.language,
        test_framework=args.test_framework,
        test_dir=args.test_dir,
        timeout=args.timeout,
        workers=args.workers,
        force=args.force,
    )
    path = args.work_dir.resolve().parent / "base" / "baseline_metrics.json"
    print(f"Wrote {path}")
    print(json.dumps({"total_collected": metrics["collection"]["total_collected"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
