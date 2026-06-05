#!/usr/bin/env python3
"""
Dockerfile Linter for Milestone Images
=======================================

Deterministic validation of agent-generated Dockerfiles to catch
forbidden operations that would compromise milestone correctness.

Rules (severity=block):
  - no_checkout_file: git checkout <ref> -- <path>
  - no_delete:        rm / git rm of project files

Rules (severity=warn):
  - indirect_src_modify:  heredoc-generated Python script that writes src
  - patch_not_committed:  patches applied without git commit + tag -f

Note: command-pattern src/test modify checks (`no_src_modify`, `test_file_modify`)
were removed in favor of effect-based per-class numstat thresholds in
diagnosis.py::evaluate_per_class_thresholds. See module2_lint_class_threshold_plan.md.

Usage:
    from .dockerfile_lint import lint_dockerfile, format_violations

    violations = lint_dockerfile(
        dockerfile_path=Path("dockerfiles/M001/Dockerfile"),
        src_dirs=["src/urllib3"],
        test_dir="test",
        allowed_tags=["milestone-M001-start", "milestone-M001-end"],
    )
    blockers = [v for v in violations if v.severity == "block"]
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class LintViolation:
    """A single lint violation found in a Dockerfile."""

    rule: str  # "no_checkout_file", "no_delete", "indirect_src_modify", ...
    severity: str  # "block" or "warn"
    line: int  # Dockerfile line number (1-based)
    command: str  # The violating shell command (trimmed)
    detail: str  # Human-readable explanation


# =============================================================================
# Dockerfile parsing
# =============================================================================


def _parse_run_instructions(content: str) -> List[dict]:
    """Parse Dockerfile and extract RUN instructions with line numbers.

    Handles multi-line RUN commands (backslash continuation) and preserves
    heredoc bodies in a separate ``raw`` field for indirect modification checks.

    Returns:
        List of {"args": str, "line": int, "raw": str}
        where args is the shell command with continuations joined, and raw is
        the original RUN instruction text including heredoc bodies.
    """
    instructions = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # Only care about RUN instructions
        match = re.match(r"^RUN\s+(.*)", stripped, re.IGNORECASE)
        if not match:
            i += 1
            continue

        args = match.group(1)
        start_line = i + 1  # 1-based
        raw_lines = [lines[i]]

        heredoc_tags = _heredoc_tags(args)
        for tag in heredoc_tags:
            while i + 1 < len(lines):
                i += 1
                raw_lines.append(lines[i])
                if lines[i].strip() == tag:
                    break

        # Handle continuation lines
        while not heredoc_tags and args.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            raw_lines.append(lines[i])
            args = args.rstrip()[:-1] + " " + lines[i].strip()

        instructions.append({"args": args, "line": start_line, "raw": "\n".join(raw_lines)})
        i += 1

    return instructions


def _split_shell_commands(run_args: str) -> List[str]:
    """Split a RUN instruction by && into individual shell commands."""
    parts = re.split(r"\s*&&\s*", run_args)
    return [p.strip() for p in parts if p.strip()]


# =============================================================================
# Path matching
# =============================================================================


def _normalize_path(path: str) -> str:
    """Strip /testbed/ prefix and leading ./ for matching."""
    path = path.strip().strip("'\"")
    path = re.sub(r"^/testbed/", "", path)
    path = re.sub(r"^\./", "", path)
    return path


def _path_touches_dir(path: str, dirs: List[str]) -> Optional[str]:
    """Return normalized path if path is under any protected directory."""
    normalized = _normalize_path(path)
    for src_dir in dirs:
        d = src_dir.strip().strip("/").rstrip("/")
        if d and (normalized == d or normalized.startswith(f"{d}/")):
            return normalized
    return None


_HEREDOC_TAG_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_CAT_HEREDOC_RE = re.compile(
    r"\bcat\s*>\s*(?P<target>['\"]?[^'\"\s]+['\"]?)\s*<<-?\s*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?"
)
_PYTHON_SCRIPT_EXEC_RE = re.compile(r"\b(?:python3?|/usr/bin/python3?)\s+(?P<script>/tmp/[^\s;&|]+\.py)\b")


def _heredoc_tags(command: str) -> List[str]:
    return [m.group(1) for m in _HEREDOC_TAG_RE.finditer(command)]


def _extract_heredoc_bodies(raw_run_instruction: str) -> List[Dict[str, str]]:
    """Extract cat heredoc bodies from a raw RUN instruction."""
    lines = raw_run_instruction.splitlines()
    bodies: List[Dict[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _CAT_HEREDOC_RE.search(line)
        if not match:
            i += 1
            continue

        target = match.group("target").strip().strip("'\"")
        tag = match.group("tag")
        body_lines: List[str] = []
        i += 1
        while i < len(lines):
            if lines[i].strip() == tag:
                break
            body_lines.append(lines[i])
            i += 1
        bodies.append({"target": target, "tag": tag, "body": "\n".join(body_lines)})
        i += 1
    return bodies


def _python_executed_temp_scripts(run_instructions: List[dict]) -> Set[str]:
    scripts: Set[str] = set()
    for instr in run_instructions:
        haystack = "\n".join([str(instr.get("args", "")), str(instr.get("raw", ""))])
        for match in _PYTHON_SCRIPT_EXEC_RE.finditer(haystack):
            scripts.add(match.group("script").strip().strip("'\""))
    return scripts


def _write_mode_is_mutating(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _extract_python_src_writes(script_body: str, src_dirs: List[str]) -> List[str]:
    """Best-effort detection of Python code writing protected src paths."""
    if not src_dirs:
        return []

    variable_paths: Dict[str, str] = {}
    touched: List[str] = []

    def add_if_src(path: str) -> None:
        matched_path = _path_touches_dir(path, src_dirs)
        if matched_path and matched_path not in touched:
            touched.append(matched_path)

    for line in script_body.splitlines():
        for match in re.finditer(
            r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:Path\()?\s*['\"](?P<path>/testbed/[^'\"]+|[^'\"]+)['\"]\s*\)?",
            line,
        ):
            matched_path = _path_touches_dir(match.group("path"), src_dirs)
            if matched_path:
                variable_paths[match.group("name")] = matched_path

        for match in re.finditer(
            r"\bopen\(\s*(?P<path>['\"][^'\"]+['\"]|[A-Za-z_][A-Za-z0-9_]*)\s*,\s*['\"](?P<mode>[^'\"]*)['\"]",
            line,
        ):
            if not _write_mode_is_mutating(match.group("mode")):
                continue
            path_expr = match.group("path").strip().strip("'\"")
            add_if_src(variable_paths.get(path_expr, path_expr))

        for match in re.finditer(
            r"\bPath\(\s*['\"](?P<path>[^'\"]+)['\"]\s*\)\.write_(?:text|bytes)\(",
            line,
        ):
            add_if_src(match.group("path"))

        for match in re.finditer(
            r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)\.write_(?:text|bytes)\(",
            line,
        ):
            mapped = variable_paths.get(match.group("name"))
            if mapped and mapped not in touched:
                touched.append(mapped)

    return touched


# =============================================================================
# Individual lint rules
# =============================================================================


def _check_no_checkout_file(
    command: str,
    line: int,
) -> Optional[LintViolation]:
    """git checkout <ref> -- <path> replaces a file from another commit.

    This changes the milestone's code state and is always forbidden.
    Plain branch/tag checkout (git checkout <tag>) is fine.
    """
    match = re.search(r"git\s+checkout\s+(\S+)\s+--\s+(\S+)", command)
    if not match:
        return None

    ref = match.group(1)
    file_path = match.group(2)
    return LintViolation(
        rule="no_checkout_file",
        severity="block",
        line=line,
        command=command[:200],
        detail=(
            f"'git checkout {ref} -- {file_path}' checks out a file from "
            f"another commit, changing the milestone's code state.  "
            f"If this symbol is missing, it likely indicates a DAG dependency "
            f"issue (missing edge), not an environment problem."
        ),
    )


def _check_indirect_src_modify(
    run_instructions: List[dict],
    src_dirs: List[str],
) -> List[LintViolation]:
    """Warn when a RUN heredoc writes a temp Python script that edits src files."""
    if not src_dirs:
        return []

    executed_scripts = _python_executed_temp_scripts(run_instructions)
    violations: List[LintViolation] = []
    seen: Set[tuple[int, str, str]] = set()

    for instr in run_instructions:
        raw = str(instr.get("raw") or instr.get("args") or "")
        line = int(instr.get("line", 0) or 0)

        for heredoc in _extract_heredoc_bodies(raw):
            target = heredoc["target"]
            if not (target.startswith("/tmp/") and target.endswith(".py")):
                continue
            if target not in executed_scripts:
                continue
            for touched_path in _extract_python_src_writes(heredoc["body"], src_dirs):
                key = (line, target, touched_path)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    LintViolation(
                        rule="indirect_src_modify",
                        severity="warn",
                        line=line,
                        command=f"cat > {target} << {heredoc['tag']} ...; python {target}",
                        detail=(
                            "Indirect source modification via heredoc-generated "
                            f"Python script '{target}', which writes '{touched_path}'. "
                            "Review this as a source shim; if the missing symbol is a "
                            "DAG/root-cause issue, prefer a verified bound waiver over "
                            "implementing framework behavior."
                        ),
                    )
                )

        if re.search(r"\b(?:python3?|bash)\s+-c\s+", raw):
            for touched_path in _extract_python_src_writes(raw, src_dirs):
                key = (line, "(inline)", touched_path)
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    LintViolation(
                        rule="indirect_src_modify",
                        severity="warn",
                        line=line,
                        command=raw[:200],
                        detail=(
                            "Indirect source modification via inline interpreter command, "
                            f"which writes '{touched_path}'."
                        ),
                    )
                )

    return violations


def _check_no_delete(
    command: str,
    line: int,
) -> Optional[LintViolation]:
    """No rm / git rm of project files.

    Allowed exceptions:
      - rm -rf /testbed  (initial cleanup before COPY)
      - rm of /tmp/ or /output/ files
      - rm of __pycache__, .pyc, .egg-info
    """
    # Match rm or git rm
    if not re.search(r"\brm\s", command):
        return None

    # ---- Allowlist ----
    # rm -rf /testbed (the initial testbed replacement)
    if re.search(r"rm\s+-rf\s+/testbed\s*$", command):
        return None
    # /tmp/ or /output/ cleanup
    if re.search(r"/tmp/|/output/", command):
        return None
    # Cache/build artifact cleanup
    if re.search(r"__pycache__|\.pyc|\.pyo|\.egg-info|\.cache|node_modules", command):
        return None
    # find ... -delete (for cache cleanup) — allow if target is cache-like
    if re.search(r"find\s.*(__pycache__|\.pyc|\.cache).*(-delete|rm)", command):
        return None

    return LintViolation(
        rule="no_delete",
        severity="block",
        line=line,
        command=command[:200],
        detail=(
            "Deleting files is forbidden. File deletion can break test "
            "discovery and changes the milestone's code state. "
            "If a file causes compilation errors, comment out its contents "
            "(whole-function comments) instead."
        ),
    )


_PATCH_RE = re.compile(r"sed\s+-[a-zA-Z]*i|sed\s+--in-place|\bpatch\s+-p")
_GIT_ADD_RE = re.compile(r"git\s+add")
_GIT_COMMIT_RE = re.compile(r"git\s+commit")
_GIT_TAG_FORCE_RE = re.compile(r"git\s+tag\s+-f")


def _check_patch_committed_file_level(
    run_instructions: List[dict],
) -> Optional[LintViolation]:
    """Patches must be committed and tags moved to survive git checkout.

    File-level check (across all RUN blocks): if the Dockerfile contains any
    sed -i / patch operation, it must also contain git add + git commit +
    git tag -f somewhere. This accepts the common pattern of splitting
    patch apply vs. commit into separate RUN blocks (readable, cacheable)
    which the old per-block check spuriously flagged.

    Caveat: does not enforce ordering — a commit BEFORE a later uncommitted
    sed would pass here but is still wrong. Ordering enforcement would need
    a full shell-semantic walk which the lint layer deliberately avoids.
    Treat a "pass" as "probably fine" and rely on downstream test failures
    to catch the rare ordering bug.
    """
    # Accumulate ALL commands from ALL RUN blocks
    all_commands: List[tuple[int, str]] = []  # [(line, cmd), ...]
    for instr in run_instructions:
        line = instr["line"]
        for cmd in _split_shell_commands(instr["args"]):
            all_commands.append((line, cmd))

    patch_locations = [line for line, cmd in all_commands if _PATCH_RE.search(cmd)]
    if not patch_locations:
        return None

    has_git_add = any(_GIT_ADD_RE.search(cmd) for _, cmd in all_commands)
    has_git_commit = any(_GIT_COMMIT_RE.search(cmd) for _, cmd in all_commands)
    has_tag_force = any(_GIT_TAG_FORCE_RE.search(cmd) for _, cmd in all_commands)

    if has_git_add and has_git_commit and has_tag_force:
        return None

    missing = []
    if not has_git_add:
        missing.append("git add -A")
    if not has_git_commit:
        missing.append("git commit -m '[ENV-PATCH] ...'")
    if not has_tag_force:
        missing.append("git tag -f <TAG> HEAD")

    # Point warning at the first patch location for actionability
    return LintViolation(
        rule="patch_not_committed",
        severity="warn",
        line=patch_locations[0],
        command="(RUN block with sed/patch)",
        detail=(
            f"Dockerfile contains sed/patch at L{patch_locations[0]}"
            f"{f' (and {len(patch_locations) - 1} more)' if len(patch_locations) > 1 else ''}"
            f" but is missing {' + '.join(missing)} anywhere in the file.  "
            f"Patches will be lost when switching between START/END states.  "
            f"Add: git add -A && git commit -m '[ENV-PATCH] ...' && "
            f"git tag -f <TAG> HEAD"
        ),
    )


# =============================================================================
# Main entry points
# =============================================================================


def lint_dockerfile(
    dockerfile_path: Path,
    src_dirs: List[str],
    test_dir: str,
    allowed_tags: List[str],  # noqa: ARG001 — reserved for future tag-aware rules
) -> List[LintViolation]:
    """Run all lint rules on a Dockerfile.

    Args:
        dockerfile_path: Path to the Dockerfile to lint.
        src_dirs: Source code directories (e.g., ["src/urllib3"]).
        test_dir: Test directory (e.g., "test").
        allowed_tags: Git tags allowed in checkout commands (reserved).

    Returns:
        List of LintViolation objects, empty if clean.
    """
    if not dockerfile_path.exists():
        return [
            LintViolation(
                rule="missing_dockerfile",
                severity="block",
                line=0,
                command="",
                detail=f"Dockerfile not found: {dockerfile_path}",
            )
        ]

    content = dockerfile_path.read_text()
    run_instructions = _parse_run_instructions(content)
    violations: List[LintViolation] = []

    for instr in run_instructions:
        commands = _split_shell_commands(instr["args"])
        line = instr["line"]

        # Per-command rules. Note: src/test classification + threshold-based block
        # is handled by per-class numstat in diagnosis.py::evaluate_per_class_thresholds
        # (see module2_lint_class_threshold_plan.md), not by command-pattern regex.
        for cmd in commands:
            for check in (
                lambda c: _check_no_checkout_file(c, line),  # noqa: B023
                lambda c: _check_no_delete(c, line),  # noqa: B023
            ):
                v = check(cmd)
                if v:
                    violations.append(v)

    # File-level rules (scan across all RUN blocks so split-block patch
    # patterns don't spuriously trigger per-block checks)
    v = _check_patch_committed_file_level(run_instructions)
    if v:
        violations.append(v)

    violations.extend(_check_indirect_src_modify(run_instructions, src_dirs))

    return violations


def format_violations(violations: List[LintViolation]) -> str:
    """Format violations into a string suitable for retry prompts.

    Blockers are listed first; warnings follow.
    """
    if not violations:
        return ""

    blockers = [v for v in violations if v.severity == "block"]
    warnings = [v for v in violations if v.severity == "warn"]

    parts = [f"Dockerfile lint: {len(blockers)} error(s), {len(warnings)} warning(s)\n"]

    for label, group in [("ERROR", blockers), ("WARNING", warnings)]:
        for v in group:
            parts.append(f"  [{label}] L{v.line} {v.rule}")
            parts.append(f"    {v.detail}")
            if v.command and v.command != "(RUN block with sed/patch)":
                preview = v.command[:150]
                if len(v.command) > 150:
                    preview += "..."
                parts.append(f"    Command: {preview}")
            parts.append("")

    if blockers:
        parts.append(
            "Fix the ERROR violations and regenerate the Dockerfile. "
            "See the DOCKERFILE VERIFICATION CHECKLIST in the original prompt."
        )
    else:
        parts.append(
            "Review the WARNING violations. They do not block acceptance by "
            "themselves, but they are audit evidence for risky environment "
            "patches or test-signal loss."
        )
    return "\n".join(parts)


# =============================================================================
# CLI entry point
# =============================================================================


def _cli_main() -> int:
    """Lint a Dockerfile from the command line.

    Exit codes:
      0 — clean (no violations)
      1 — warn-level violations only (not gate-blocking)
      2 — block-level violations (will fail B_lint_block_clean)
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Lint a milestone Dockerfile for known violations.",
    )
    parser.add_argument("dockerfile", type=Path, help="Path to the Dockerfile to lint")
    parser.add_argument(
        "--repo-src-dirs",
        nargs="*",
        default=[],
        help="Source directories protected from modification (e.g., src/ django/)",
    )
    parser.add_argument(
        "--test-dir",
        default="tests",
        help="Test directory protected from modification (default: tests)",
    )
    args = parser.parse_args()

    violations = lint_dockerfile(
        dockerfile_path=args.dockerfile,
        src_dirs=args.repo_src_dirs,
        test_dir=args.test_dir,
        allowed_tags=[],
    )

    if not violations:
        print("clean: no violations.")
        return 0

    print(format_violations(violations))

    if any(v.severity == "block" for v in violations):
        return 2
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(_cli_main())
