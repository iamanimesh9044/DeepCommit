#!/usr/bin/env python3
"""Deterministic validation gate for a Phase-1 range config.

Given a repo + a candidate config (the source-boundary fields), this classifies
the repo's files through the SINGLE canonical engine ``SrcFileFilter`` (the same
one Phase 1-3 use after the classification unification) and reports:

  * a per-FileClass count summary (incl. per-language src counts),
  * blocking ERRORs (config is unusable — exit 1),
  * advisory WARNs (likely-wrong boundary — exit 0),
  * samples of surprising classifications.

It is a pure function of (file list, candidate config), so it is cheap to unit
test and safe for an agent to call in a propose -> validate -> fix loop: the
exit code is the hard gate ("don't write the config until this is green").

Usage:
    python -m deepcommit.preprocess.prepare_range_config.validate \
        --repo DATA/github_data/repos/<owner_repo> \
        --config config/<owner_repo_start_end>.yaml [--json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from deepcommit.harness.utils.src_filter import SrcFileFilter

# --- config schema taxonomy --------------------------------------------------
# Phase-1 source-boundary fields the config-prep tool authors + validates.
BOUNDARY_KEYS = {
    "repo_src_dirs",
    "test_dirs",
    "exclude",
    "main_branch",
    "tag_prefix",
    "generated_patterns",
    "modifiable_test_patterns",
}
# Phase-3 build/runtime fields — belong in the workspace, not the hand-authored
# config (see classification_unification.md "config = boundary" section).
PHASE3_KEYS = {"base_image_name", "build_command", "language", "test_framework", "test_infra_patterns"}
# Removed entirely.
OBSOLETE_KEYS = {"struct_field_fixes"}
# Tolerated advanced flags (read by the DAG/codemaat steps).
FLAG_KEYS = {
    "exclude_test_files",
    "exclude_ci_files",
    "exclude_config_files",
    "exclude_doc_files",
    "use_semver_filter",
    "reduce_transitive",
}

GLOB_CHARS = set("*?[]")

# Source-like extensions the classifier RECOGNISES (mirrors src_filter's map).
RECOGNISED_SRC_EXT = {".py", ".java", ".kt", ".go", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rs"}
# Source-like extensions the file-level classifier does NOT recognise → if these
# live under repo_src_dirs they are silently left unchecked.
UNRECOGNISED_SRC_EXT = {
    ".groovy",
    ".scala",
    ".vue",
    ".svelte",
    ".pyi",
    ".cc",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".cs",
    ".swift",
}
# Directory segments that UNAMBIGUOUSLY imply a test tree. Deliberately excludes
# the singular "test" (a common source submodule name, e.g. django/test/, the test
# framework) and "fixtures" (test *data*, not test code) to avoid false positives.
TEST_DIR_SEGS = (
    "tests",
    "__tests__",
    "__snapshots__",
    "testdata",
    "e2e",
    "playwright",
    "cypress",
)
# Filename suffixes / patterns that imply a test file regardless of directory.
TEST_FILE_SUFFIXES = ("_test.go", "_test.py", "_test.rs", "_test.java", "test.kt", "spec.rb")


@dataclass
class Finding:
    level: str  # "error" | "warn"
    code: str
    message: str
    samples: List[str] = field(default_factory=list)


@dataclass
class Report:
    counts: Dict[str, int]
    per_language_src: Dict[str, int]
    n_files: int
    findings: List[Finding]

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _run_git(repo: Path, args: List[str]) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.returncode, r.stdout


def _list_tree(repo: Path, ref: str) -> List[str]:
    code, out = _run_git(repo, ["ls-tree", "-r", "--name-only", ref])
    if code != 0:
        return []
    return [ln for ln in out.splitlines() if ln]


def _resolve_ref(repo: Path, ref: str) -> Optional[str]:
    for candidate in (ref, f"origin/{ref}", f"refs/tags/{ref}"):
        code, _ = _run_git(repo, ["rev-parse", "--verify", "--quiet", candidate])
        if code == 0:
            return candidate
    return None


def _looks_like_test(path: str) -> bool:
    segs = path.lower().split("/")
    if any(seg in TEST_DIR_SEGS for seg in segs[:-1]):
        return True
    base = segs[-1]
    if base.startswith("test_") and base.endswith((".py", ".rs")):
        return True
    if ".test." in base or ".spec." in base:
        return True
    return base.endswith(TEST_FILE_SUFFIXES)


def classify_and_lint(
    config: dict,
    files: List[str],
    *,
    main_branch_resolves: Optional[bool] = None,
    sample_n: int = 8,
) -> Report:
    """Pure core: lint the candidate ``config`` and classify ``files`` through the
    single canonical ``SrcFileFilter``. Returns a :class:`Report` (``.ok`` is the
    gate). No git I/O — the caller enumerates ``files`` and passes
    ``main_branch_resolves`` (``None`` skips the branch check)."""
    findings: List[Finding] = []
    repo_src_dirs = config.get("repo_src_dirs") or []
    test_dirs = config.get("test_dirs") or []
    exclude = config.get("exclude") or []

    # ---- static schema lint -------------------------------------------------
    if not repo_src_dirs:
        findings.append(
            Finding("error", "E_no_src_dirs", "repo_src_dirs is missing or empty — nothing counts as source.")
        )
    for d in repo_src_dirs:
        if set(str(d)) & GLOB_CHARS:
            findings.append(
                Finding(
                    "error",
                    "E_glob_in_src_dir",
                    f"repo_src_dirs entry {d!r} contains glob chars; it is matched as a LITERAL prefix "
                    f"(str.startswith), so the glob silently matches nothing. Use a plain directory path.",
                )
            )
    for k in config:
        if k in OBSOLETE_KEYS:
            findings.append(
                Finding("error", "E_obsolete_key", f"{k!r} is obsolete (no reader in the codebase) — remove it.")
            )
        elif k in PHASE3_KEYS:
            findings.append(
                Finding(
                    "warn",
                    "W_phase3_field",
                    f"{k!r} is a Phase-3 build/runtime field; it belongs in the workspace, not the boundary config.",
                )
            )
        elif k == "exclude_patterns":
            findings.append(
                Finding(
                    "warn",
                    "W_exclude_alias",
                    "use 'exclude' (Phase-1 only honors 'exclude'; 'exclude_patterns' silently no-ops in preprocess).",
                )
            )
        elif k not in BOUNDARY_KEYS and k not in FLAG_KEYS:
            findings.append(Finding("warn", "W_unknown_key", f"unknown top-level key {k!r}."))
    # trailing-slash consistency
    if repo_src_dirs:
        with_slash = [d for d in repo_src_dirs if str(d).endswith("/")]
        if with_slash and len(with_slash) != len(repo_src_dirs):
            findings.append(
                Finding(
                    "warn",
                    "W_trailing_slash_mixed",
                    "repo_src_dirs mixes trailing-slash styles; harmless (normalized) but standardize for clarity.",
                )
            )
    # main_branch resolvable?
    mb = config.get("main_branch")
    if mb and main_branch_resolves is False:
        findings.append(
            Finding(
                "warn", "W_main_branch_unresolved", f"main_branch {mb!r} does not resolve in the repo (branch/tag/sha)."
            )
        )

    # ---- classify the file universe ----------------------------------------
    flt = SrcFileFilter(src_dirs=repo_src_dirs, test_dirs=test_dirs, exclude_patterns=exclude)
    counts: Dict[str, int] = {}
    per_lang: Dict[str, int] = {}
    test_looks_src: List[str] = []
    src_like_outside: List[str] = []
    unrecognised_in_src: List[str] = []

    for p in files:
        cls = flt.classify(p)
        counts[cls] = counts.get(cls, 0) + 1
        if cls.endswith("_src"):
            per_lang[cls] = per_lang.get(cls, 0) + 1
            if _looks_like_test(p):
                test_looks_src.append(p)
        ext = "." + p.rsplit(".", 1)[-1] if "." in p.rsplit("/", 1)[-1] else ""
        in_src = SrcFileFilter.in_src_dirs(p, repo_src_dirs)
        if cls == "non_src_non_test":
            if not in_src and ext in RECOGNISED_SRC_EXT and not _looks_like_test(p):
                src_like_outside.append(p)
            elif in_src and ext in UNRECOGNISED_SRC_EXT:
                unrecognised_in_src.append(p)

    n_src = sum(per_lang.values())
    if files and n_src == 0:
        findings.append(
            Finding(
                "error",
                "E_zero_src",
                "0 files classified as source — repo_src_dirs almost certainly wrong for this repo.",
            )
        )

    if test_looks_src:
        findings.append(
            Finding(
                "warn",
                "W_test_looks_like_src",
                f"{len(test_looks_src)} file(s) look like tests but are classified as SOURCE — "
                f"test_dirs is likely incomplete (e.g. a root-anchored 'tests/**' that should be '**/tests/**').",
                test_looks_src[:sample_n],
            )
        )
    if src_like_outside:
        by_dir = Counter(p.split("/")[0] if "/" in p else "(root)" for p in src_like_outside)
        summary = ", ".join(f"{d} ({n})" for d, n in by_dir.most_common(15))
        findings.append(
            Finding(
                "warn",
                "W_src_like_outside_boundary",
                f"{len(src_like_outside)} source-extension file(s) lie OUTSIDE repo_src_dirs, by top-level dir: "
                f"{summary}. Confirm none of these dirs belong in repo_src_dirs "
                f"(benchmarks / tools / examples / CI scripts are normal to leave out).",
            )
        )
    if unrecognised_in_src:
        findings.append(
            Finding(
                "warn",
                "W_unrecognised_src_ext",
                f"{len(unrecognised_in_src)} file(s) under repo_src_dirs have a source-like extension the classifier does NOT recognise — silently left unchecked.",
                unrecognised_in_src[:sample_n],
            )
        )

    return Report(counts=counts, per_language_src=per_lang, n_files=len(files), findings=findings)


def validate_config(repo: Path, config: dict, *, end_ref: str = "HEAD", sample_n: int = 8) -> Report:
    """Git shell around :func:`classify_and_lint`: enumerate the repo tree at
    ``end_ref`` and resolve ``main_branch``, then run the pure core."""
    files = _list_tree(repo, end_ref)
    mb = config.get("main_branch")
    mb_resolves = (_resolve_ref(repo, str(mb)) is not None) if mb else None
    return classify_and_lint(config, files, main_branch_resolves=mb_resolves, sample_n=sample_n)


def format_report(report: Report, *, repo: Path, end_ref: str) -> str:
    lines: List[str] = []
    lines.append(f"validate: {repo.name} @ {end_ref} — {report.n_files} files")
    lines.append("  classification:")
    for cls in sorted(report.counts):
        lines.append(f"    {cls:<22} {report.counts[cls]:>6}")
    if report.errors:
        lines.append(f"\n  ERRORS ({len(report.errors)}) — config is NOT usable:")
        for f in report.errors:
            lines.append(f"    ✗ [{f.code}] {f.message}")
            for s in f.samples:
                lines.append(f"        {s}")
    if report.warnings:
        lines.append(f"\n  WARNINGS ({len(report.warnings)}):")
        for f in report.warnings:
            lines.append(f"    ⚠ [{f.code}] {f.message}")
            for s in f.samples:
                lines.append(f"        {s}")
    lines.append(
        f"\n  => {'PASS' if report.ok else 'FAIL'} ({len(report.errors)} errors, {len(report.warnings)} warnings)"
    )
    return "\n".join(lines)


def _finding_dict(f: Finding) -> dict:
    return {"level": f.level, "code": f.code, "message": f.message, "samples": f.samples}


def _report_to_dict(report: Report) -> dict:
    # Explicit, automation-friendly keys (ok / passed + split errors/warnings) so a
    # consumer doesn't have to filter `findings` by level themselves.
    return {
        "ok": report.ok,
        "passed": report.ok,
        "n_files": report.n_files,
        "n_errors": len(report.errors),
        "n_warnings": len(report.warnings),
        "counts": report.counts,
        "per_language_src": report.per_language_src,
        "errors": [_finding_dict(f) for f in report.errors],
        "warnings": [_finding_dict(f) for f in report.warnings],
        "findings": [_finding_dict(f) for f in report.findings],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a Phase-1 range config against a repo.")
    ap.add_argument("--repo", type=Path, required=True, help="path to the local git repo")
    ap.add_argument("--config", type=Path, required=True, help="candidate config YAML")
    ap.add_argument(
        "--end-ref", default=None, help="ref to enumerate (default: end tag parsed from config name, else HEAD)"
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a human report")
    args = ap.parse_args()

    if not args.repo.exists():
        print(f"error: repo not found: {args.repo}", file=sys.stderr)
        return 2
    config = yaml.safe_load(args.config.read_text()) or {}

    end_ref = args.end_ref
    if end_ref is None:
        parts = args.config.stem.rsplit("_", 2)
        end_ref = parts[-1] if len(parts) == 3 and _resolve_ref(args.repo, parts[-1]) else "HEAD"

    report = validate_config(args.repo, config, end_ref=end_ref)
    if args.json:
        print(json.dumps(_report_to_dict(report), indent=2))
    else:
        print(format_report(report, repo=args.repo, end_ref=end_ref))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
