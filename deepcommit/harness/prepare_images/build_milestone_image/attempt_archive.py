"""Append-only archive for milestone agent attempts."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

AGENT_INPUT_FILES = (
    "Dockerfile",
    "test_config.json",
    "module2_waivers.json",
    "module2_signals.json",
    "retry_context.md",
)
OPTIONAL_AGENT_INPUT_FILES = ("module2_waivers.json", "module2_signals.json", "retry_context.md")
AGENT_OUTPUT_FILES = (
    "Dockerfile",
    "test_config.json",
    "module2_waivers.json",
    "module2_signals.json",
)
OPTIONAL_AGENT_OUTPUT_FILES = ("module2_waivers.json", "module2_signals.json")
VALIDATION_DIAGNOSTIC_FILES = ("runner_diagnostic.json",)
VALIDATION_REPORT_FILES = ("start.json", "end.json", "classification.json")
VALIDATION_ATTEMPT_SUMMARY_FILES = ("start_summary.json", "end_summary.json", "original_summary.json")


def attempt_number(path: Path) -> int:
    try:
        return int(path.name.split("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def validation_attempt_is_complete(attempt_dir: Path) -> bool:
    if not attempt_dir.is_dir():
        return False
    for name in VALIDATION_REPORT_FILES:
        path = attempt_dir / name
        if not path.exists():
            return False
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
    return True


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _same_path(left: Optional[Path], right: Optional[Path]) -> bool:
    if left is None or right is None:
        return False
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def runner_diagnostic_matches_attempt(
    diagnostic_path: Path,
    latest_attempt_dir: Optional[Path],
    *,
    pipeline_status: str,
) -> bool:
    """Return true iff a runner diagnostic belongs to this validation.

    ``runner_diagnostic.json`` lives under
    ``test_results/{mid}/refinement/`` and can survive across later successful
    runs. Copying it blindly into a build-run archive makes the archive look
    like the current validation crashed when the file is actually from an
    older attempt.
    """
    diagnostic = _read_json(diagnostic_path)
    if diagnostic is None:
        return False

    try:
        exit_code = int(diagnostic.get("exit_code", 0) or 0)
    except (TypeError, ValueError):
        exit_code = 0
    if pipeline_status == "success" and exit_code != 0:
        return False

    attempt_refs = [
        diagnostic.get("attempt_dir"),
        diagnostic.get("requested_attempt_dir"),
        diagnostic.get("latest_attempt_dir"),
    ]
    attempt_paths = [Path(str(ref)) for ref in attempt_refs if ref]
    if latest_attempt_dir is None:
        return bool(attempt_paths) or diagnostic.get("exit_code") is not None
    return any(_same_path(path, latest_attempt_dir) for path in attempt_paths)


def next_aligned_attempt_number(work_dir: Path, milestone_id: str) -> int:
    """Return the next attempt number shared by build_runs and test_results."""
    candidates: List[int] = []
    for parent in (
        work_dir / "_build_runs" / milestone_id,
        work_dir / "test_results" / milestone_id / "refinement",
    ):
        if parent.exists():
            candidates.extend(n for n in (attempt_number(path) for path in parent.glob("attempt_*")) if n > 0)
    return (max(candidates) + 1) if candidates else 1


def file_sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def agent_input_hashes(docker_dir: Path) -> Dict[str, Optional[str]]:
    return {name: file_sha256(docker_dir / name) for name in AGENT_OUTPUT_FILES}


def find_official_agent_logs(
    agent: str,
    provider_session_id: Optional[str],
    *,
    home: Optional[Path] = None,
) -> List[Path]:
    """Find provider-native JSONL logs for a Codex/Claude session id."""
    if not provider_session_id:
        return []
    home_dir = Path(home) if home else Path.home()
    patterns: List[Path] = []
    if agent == "codex":
        patterns = [
            home_dir / ".codex" / "sessions",
            home_dir / ".codex" / "archived_sessions",
        ]
    elif agent == "claude":
        patterns = [home_dir / ".claude" / "projects"]
    else:
        return []

    matches: List[Path] = []
    for root in patterns:
        if not root.exists():
            continue
        matches.extend(sorted(root.rglob(f"*{provider_session_id}*.jsonl")))
    return matches


class MilestoneAttemptArchive:
    """Archive one outer agent retry attempt without affecting hard-gate state."""

    def __init__(
        self,
        *,
        work_dir: Path,
        milestone_id: str,
        attempt_number: int,
        repo_id: str,
        test_scenario: str,
        agent: str,
        model: str,
        reasoning_effort: Optional[str],
        agent_retry_attempt: int,
    ):
        self.work_dir = Path(work_dir)
        self.milestone_id = milestone_id
        self.attempt_number = attempt_number
        self.root = self.work_dir / "_build_runs" / milestone_id / f"attempt_{attempt_number}"
        self.index_path = self.work_dir / "_build_runs" / "index.jsonl"
        self.meta_path = self.root / "meta.json"
        self.meta: Dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "repo_id": repo_id,
            "test_scenario": test_scenario,
            "milestone_id": milestone_id,
            "attempt": attempt_number,
            "agent_retry_attempt": agent_retry_attempt,
            "validation_attempt": None,
            "validation_ran": False,
            "agent": agent,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "session_id": None,
            "provider_session_id": None,
            "resume_attempts": 0,
            "agent_success": None,
            "pipeline_status": None,
            "failed_gates": [],
            "source_paths": {},
            "official_log_paths": [],
            "official_log_missing": False,
            "missing_files": [],
            "missing_files_by_stage": {},
            "optional_missing_files": [],
            "skipped_artifacts": [],
            "copy_errors": [],
        }
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def start(
        cls,
        *,
        work_dir: Path,
        milestone_id: str,
        attempt_number: int,
        repo_id: str,
        test_scenario: str,
        agent: str,
        model: str,
        reasoning_effort: Optional[str],
        agent_retry_attempt: int,
        docker_dir: Path,
        prompt_file: Path,
        prompt_text: str,
    ) -> "MilestoneAttemptArchive":
        archive = cls(
            work_dir=work_dir,
            milestone_id=milestone_id,
            attempt_number=attempt_number,
            repo_id=repo_id,
            test_scenario=test_scenario,
            agent=agent,
            model=model,
            reasoning_effort=reasoning_effort,
            agent_retry_attempt=agent_retry_attempt,
        )
        archive.meta["source_paths"].update(
            {
                "docker_dir": str(docker_dir),
                "prompt_file": str(prompt_file),
            }
        )
        archive.copy_file(prompt_file, archive.root / "env_setup_prompt.md")
        archive.write_text(archive.root / "prompt_sent.md", prompt_text)
        archive.snapshot_files(
            docker_dir,
            archive.root / "before_agent",
            AGENT_INPUT_FILES,
            optional_names=OPTIONAL_AGENT_INPUT_FILES,
            stage="before_agent",
        )
        archive.write_meta()
        return archive

    @classmethod
    def open_existing(
        cls,
        *,
        work_dir: Path,
        milestone_id: str,
        attempt_number: int,
    ) -> Optional["MilestoneAttemptArchive"]:
        """Open an existing archive attempt for a validation rerun."""
        meta_path = Path(work_dir) / "_build_runs" / milestone_id / f"attempt_{attempt_number}" / "meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(meta, dict):
            return None
        archive = cls(
            work_dir=Path(work_dir),
            milestone_id=milestone_id,
            attempt_number=attempt_number,
            repo_id=str(meta.get("repo_id") or ""),
            test_scenario=str(meta.get("test_scenario") or ""),
            agent=str(meta.get("agent") or ""),
            model=str(meta.get("model") or ""),
            reasoning_effort=meta.get("reasoning_effort"),
            agent_retry_attempt=int(meta.get("agent_retry_attempt") or 0),
        )
        archive.meta = meta
        archive.write_meta()
        return archive

    def _record_missing(self, src: Path, dst: Path, *, stage: Optional[str], required: bool) -> None:
        entry = {"src": str(src), "dst": str(dst), "required": required}
        if stage:
            entry["stage"] = stage
        if required:
            self.meta["missing_files"].append(str(src))
        else:
            self.meta.setdefault("optional_missing_files", []).append(entry)
        if stage:
            by_stage = self.meta.setdefault("missing_files_by_stage", {})
            if isinstance(by_stage, dict):
                by_stage.setdefault(stage, []).append(entry)

    def copy_file(
        self,
        src: Path,
        dst: Path,
        *,
        stage: Optional[str] = None,
        required: bool = True,
    ) -> Optional[Path]:
        src = Path(src)
        dst = Path(dst)
        if not src.exists():
            self._record_missing(src, dst, stage=stage, required=required)
            return None
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return dst
        except OSError as e:
            self.meta["copy_errors"].append({"src": str(src), "dst": str(dst), "error": str(e)})
            return None

    def write_text(self, dst: Path, text: str) -> None:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        except OSError as e:
            self.meta["copy_errors"].append({"dst": str(dst), "error": str(e)})

    def snapshot_files(
        self,
        src_dir: Path,
        dst_dir: Path,
        names: Iterable[str],
        *,
        optional_names: Iterable[str] = (),
        stage: Optional[str] = None,
    ) -> None:
        optional = set(optional_names)
        for name in names:
            self.copy_file(
                Path(src_dir) / name,
                Path(dst_dir) / name,
                stage=stage,
                required=name not in optional,
            )

    def record_agent_result(
        self,
        *,
        agent_result: Any,
    ) -> None:
        self.meta.update(
            {
                "session_id": getattr(agent_result, "session_id", None),
                "provider_session_id": getattr(agent_result, "provider_session_id", None),
                "resume_attempts": getattr(agent_result, "resume_attempts", 0),
                "agent_success": getattr(agent_result, "success", None),
            }
        )
        wrapper_log = self.root / "agent" / "wrapper.log"
        if not wrapper_log.exists():
            self.meta["copy_errors"].append(
                {"dst": str(wrapper_log), "error": "wrapper.log missing — runner did not write log_path"}
            )
        official_dir = self.root / "agent" / "official"
        official_logs = find_official_agent_logs(
            self.meta["agent"],
            self.meta.get("provider_session_id"),
        )
        copied: List[str] = []
        for path in official_logs:
            target_name = f"{self.meta['agent']}_{path.name}"
            copied_path = self.copy_file(path, official_dir / target_name)
            if copied_path:
                copied.append(str(copied_path))
        self.meta["official_log_paths"] = copied
        self.meta["official_log_missing"] = not bool(copied)
        self.write_meta()

    def snapshot_after_agent(self, docker_dir: Path) -> None:
        self.snapshot_files(
            docker_dir,
            self.root / "after_agent",
            AGENT_OUTPUT_FILES,
            optional_names=OPTIONAL_AGENT_OUTPUT_FILES,
            stage="after_agent",
        )
        self.meta["after_agent_input_hashes"] = agent_input_hashes(docker_dir)
        self.write_meta()

    def _copy_latest_attempt_artifacts(self, latest_attempt_dir: Path, validation_dir: Path) -> None:
        target_dir = validation_dir / "latest_attempt"
        names = set(VALIDATION_REPORT_FILES) | set(VALIDATION_ATTEMPT_SUMMARY_FILES) | {"attempt_manifest.json"}
        for path in sorted(Path(latest_attempt_dir).glob("*.json")):
            names.add(path.name)
        for name in sorted(names):
            self.copy_file(
                Path(latest_attempt_dir) / name,
                target_dir / name,
                stage="after_validation.latest_attempt",
                required=name in VALIDATION_REPORT_FILES,
            )
        manifest = Path(latest_attempt_dir) / "attempt_manifest.json"
        if manifest.exists():
            self.copy_file(
                manifest,
                validation_dir / "latest_attempt_manifest.json",
                stage="after_validation",
                required=False,
            )

    def snapshot_after_validation(
        self,
        *,
        docker_dir: Path,
        test_results_dir: Path,
        status: Optional[Dict[str, Any]],
        latest_attempt_dir: Optional[Path],
        pipeline_status: str,
        validation_ran: bool,
        validation_timeout_seconds: Optional[int] = None,
        validation_run_kind: str = "pipeline",
    ) -> None:
        validation_dir = self.root / "after_validation"
        self.meta["pipeline_status"] = pipeline_status
        self.meta["validation_ran"] = bool(validation_ran)
        self.meta["validation_attempt"] = attempt_number(latest_attempt_dir) if latest_attempt_dir else None
        validation_runs = self.meta.setdefault("validation_runs", [])
        if isinstance(validation_runs, list):
            validation_runs.append(
                {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "kind": validation_run_kind,
                    "validation_attempt": self.meta["validation_attempt"],
                    "validation_ran": bool(validation_ran),
                    "pipeline_status": pipeline_status,
                    "failed_gates": status.get("failed_gates", []) if status else [],
                    "timeout_seconds": validation_timeout_seconds,
                }
            )
        if status:
            self.meta["failed_gates"] = status.get("failed_gates", [])
        self.copy_file(Path(docker_dir) / "build_status.json", validation_dir / "build_status.json")

        if validation_ran:
            milestone_results_dir = Path(test_results_dir) / self.milestone_id
            refinement_results_dir = milestone_results_dir / "refinement"
            for name in VALIDATION_DIAGNOSTIC_FILES:
                diagnostic_path = refinement_results_dir / name
                if diagnostic_path.exists() and runner_diagnostic_matches_attempt(
                    diagnostic_path,
                    Path(latest_attempt_dir) if latest_attempt_dir else None,
                    pipeline_status=pipeline_status,
                ):
                    self.copy_file(
                        diagnostic_path,
                        validation_dir / name,
                        stage="after_validation",
                        required=False,
                    )
                elif diagnostic_path.exists():
                    self.meta.setdefault("skipped_artifacts", []).append(
                        {
                            "path": str(diagnostic_path),
                            "reason": "runner diagnostic does not match current validation attempt",
                        }
                    )
            latest_complete = latest_attempt_dir and validation_attempt_is_complete(Path(latest_attempt_dir))
            if latest_complete:
                self._copy_latest_attempt_artifacts(Path(latest_attempt_dir), validation_dir)
            else:
                self.meta["missing_files"].append("validation summary not copied: latest attempt is incomplete")

        after_validation_hashes = agent_input_hashes(docker_dir)
        self.write_text(
            validation_dir / "input_hashes.json",
            json.dumps(after_validation_hashes, indent=2, sort_keys=True),
        )
        after_agent_hashes = self.meta.get("after_agent_input_hashes") or {}
        changed_dir = validation_dir / "changed_inputs"
        for name, digest in after_validation_hashes.items():
            if after_agent_hashes.get(name) != digest:
                self.copy_file(Path(docker_dir) / name, changed_dir / name)
        self.write_meta()

    def write_meta(self) -> None:
        self.write_text(self.meta_path, json.dumps(self.meta, indent=2, sort_keys=True))

    def finish(self) -> None:
        self.write_meta()
        summary = {
            "created_at": self.meta.get("created_at"),
            "milestone_id": self.milestone_id,
            "attempt": self.attempt_number,
            "agent_retry_attempt": self.meta.get("agent_retry_attempt"),
            "agent": self.meta.get("agent"),
            "model": self.meta.get("model"),
            "reasoning_effort": self.meta.get("reasoning_effort"),
            "session_id": self.meta.get("session_id"),
            "provider_session_id": self.meta.get("provider_session_id"),
            "resume_attempts": self.meta.get("resume_attempts"),
            "agent_success": self.meta.get("agent_success"),
            "validation_ran": self.meta.get("validation_ran"),
            "validation_attempt": self.meta.get("validation_attempt"),
            "pipeline_status": self.meta.get("pipeline_status"),
            "failed_gates": self.meta.get("failed_gates", []),
            "archive_path": str(self.root),
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, sort_keys=True) + "\n")
