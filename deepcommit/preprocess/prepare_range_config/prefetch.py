#!/usr/bin/env python3
"""Context prefetch for the Phase-1 config-prep agent.

Emits a compact markdown brief the agent reads before drafting the config:
  * detected languages + test-dir suggestions (via scripts/detect_test_dirs.py),
  * a per-top-level-directory histogram (source-ext / test-looking / other file
    counts) at the END ref — the single most useful signal for picking
    ``repo_src_dirs`` (source lives where the source-ext counts cluster),
  * the changed-files-in-range top-dir breakdown (what the milestone DAG sees).

This is deterministic, read-only, and cheap; the agent then proposes the 4
boundary fields and calls ``validate`` to check them.

Usage:
    python -m deepcommit.preprocess.prepare_range_config.prefetch \
        --repo DATA/github_data/repos/<owner_repo> \
        [--start-tag X --end-tag Y] [--config config/<...>.yaml]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional

from deepcommit.preprocess.prepare_range_config.validate import (
    RECOGNISED_SRC_EXT,
    UNRECOGNISED_SRC_EXT,
    _looks_like_test,
    _resolve_ref,
    _run_git,
)

SRC_EXT = RECOGNISED_SRC_EXT | UNRECOGNISED_SRC_EXT
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1] if "." in base else ""


def _top(path: str) -> str:
    return path.split("/")[0] if "/" in path else "(root)"


def _ls_tree(repo: Path, ref: str) -> List[str]:
    code, out = _run_git(repo, ["ls-tree", "-r", "--name-only", ref])
    return [ln for ln in out.splitlines() if ln] if code == 0 else []


def _changed_files(repo: Path, start: str, end: str) -> List[str]:
    code, out = _run_git(repo, ["diff", "--name-only", f"{start}..{end}"])
    return [ln for ln in out.splitlines() if ln] if code == 0 else []


def _detect(repo: Path, config: Optional[Path]) -> str:
    script = _REPO_ROOT / "scripts" / "detect_test_dirs.py"
    if not script.exists():
        return "(detect_test_dirs.py not found)"
    cmd = [sys.executable, str(script), str(repo)]
    if config and config.exists():
        cmd += ["--config", str(config)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def _histogram(files: List[str]) -> List[tuple]:
    """Per-top-dir (src-ext, test-looking, other) counts, sorted by src desc."""
    src: Counter = Counter()
    test: Counter = Counter()
    other: Counter = Counter()
    for f in files:
        top = _top(f)
        if _looks_like_test(f):
            test[top] += 1
        elif _ext(f) in SRC_EXT:
            src[top] += 1
        else:
            other[top] += 1
    tops = set(src) | set(test) | set(other)
    rows = [(t, src[t], test[t], other[t]) for t in tops]
    rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
    return rows


def build_brief(repo: Path, start: Optional[str], end: str, config: Optional[Path]) -> str:
    files = _ls_tree(repo, end)
    out: List[str] = []
    out.append(f"# Config prefetch — {repo.name}")
    out.append(f"- repo: `{repo}`")
    out.append(f"- range: `{start or '?'}` .. `{end}`  ({len(files)} files at end ref)\n")

    out.append("## Detected languages + test-dir suggestions\n```")
    out.append(_detect(repo, config))
    out.append("```\n")

    out.append("## Per-top-level-dir file histogram @ end ref")
    out.append("Source lives where `src` clusters. `test` = test-looking files; `other` = non-source.\n")
    out.append("| top-level dir | src-ext | test-like | other |")
    out.append("|---------------|--------:|----------:|------:|")
    for top, s, t, o in _histogram(files)[:30]:
        out.append(f"| `{top}` | {s} | {t} | {o} |")
    out.append("")

    if start:
        changed = _changed_files(repo, start, end)
        out.append(f"## Changed files in range ({len(changed)}) — top-dir breakdown")
        ch = Counter(_top(f) for f in changed)
        out.append("```")
        out.append(", ".join(f"{d} ({n})" for d, n in ch.most_common(20)) or "(none)")
        out.append("```\n")

    out.append("## Reminders (matching contract)")
    out.append("- `repo_src_dirs`: LITERAL path prefixes (NOT globs); no `*`. S-norm anchored.")
    out.append(
        "- `test_dirs` / `exclude`: gitwildmatch globs, ROOT-anchored — `tests/**` matches root only; "
        "use `**/tests/**` (or `pkg/*/tests/**`) for nested tests."
    )
    out.append("- `exclude` only needs to target files INSIDE `repo_src_dirs`.")
    out.append("- Author ONLY the boundary fields; run `validate` until it's green.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit a config-prep context brief for a repo + range.")
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--start-tag", default=None)
    ap.add_argument("--end-tag", default=None)
    ap.add_argument("--config", type=Path, default=None, help="optional existing config (for tag parsing)")
    ap.add_argument("--out", type=Path, default=None, help="write brief here (default: stdout)")
    args = ap.parse_args()

    if not args.repo.exists():
        print(f"error: repo not found: {args.repo}", file=sys.stderr)
        return 2

    start, end = args.start_tag, args.end_tag
    if (not start or not end) and args.config:
        parts = args.config.stem.rsplit("_", 2)
        if len(parts) == 3:
            start, end = start or parts[1], end or parts[2]
    if not end:
        end = "HEAD"
    elif _resolve_ref(args.repo, end) is None:
        print(f"warning: end ref {end!r} does not resolve; falling back to HEAD", file=sys.stderr)
        end = "HEAD"

    brief = build_brief(args.repo, start, end, args.config)
    if args.out:
        args.out.write_text(brief)
        print(f"brief written: {args.out}", file=sys.stderr)
    else:
        print(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
