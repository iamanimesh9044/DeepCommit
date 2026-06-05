"""T0 hygiene gate: no machine-absolute paths / secret assignments leak into active tests/config.

This module is the SINGLE SOURCE OF TRUTH for the path/secret ban: CI runs *this
test* (see .github/workflows/ci.yml) instead of re-implementing the grep, so the
two can never drift. gitleaks (CI) does the heavy secret-value scanning; this
catches machine-absolute path leaks and the named-token assignment pattern
cheaply, with no external deps.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# NOTE: single backslash — `\s` must mean "whitespace", not a literal backslash.
# Project-relative `DATA/...` paths are intentionally allowed (spec §T0).
BANNED = re.compile(
    r"/home/[a-z][a-z0-9_-]*"  # /home/<user>
    r"|/data2/"  # known NFS mount on the lab box
    r"|/Users/"  # macOS home
    r"|/mnt/[a-z]"  # generic mount
    r"|(?:GITHUB_CRAWLER_TOKENS|GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=\s*\S"
)

ACTIVE_GLOBS = (
    "tests/unit/**/*.py",
    "tests/golden/**/*.py",
    "pyproject.toml",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".pre-commit-config.yaml",
)

# This file legitimately contains the BANNED literals (the regex itself + the
# self-test strings below), so it excludes itself from the tree scan — the same
# rationale as its entry in .gitleaks.toml's allowlist.
SELF = Path(__file__).resolve()


def _tracked(*globs):
    out = subprocess.run(
        ["git", "ls-files", *globs], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return [REPO / p for p in out]


def test_banned_regex_is_live_not_a_literal_backslash():
    # Guards the exact bug the spec calls out: the pattern must MATCH a real assignment.
    assert BANNED.search("GITHUB_CRAWLER_TOKENS = ghp_realtokenvalue")
    assert BANNED.search('GITHUB_TOKEN="abc123"')
    assert BANNED.search("OPENAI_API_KEY=sk-abc")
    assert BANNED.search("home dir is /home/alice/data")
    assert BANNED.search("config at /Users/bob/x")
    # ...and must NOT fire on an allowed project-relative path or a bare mention.
    assert not BANNED.search("path = DATA/github_data/repos/foo")
    assert not BANNED.search("# document GITHUB_CRAWLER_TOKENS here")  # no `=<value>`
    assert not BANNED.search("artifacts under /tmp/build")  # /tmp is allowed
    assert not BANNED.search("nodeid /testbed/test/foo.py::t")  # /testbed is allowed


def test_no_machine_absolute_paths():
    offenders = []
    for path in _tracked(*ACTIVE_GLOBS):
        if "tests/legacy/" in str(path) or path.resolve() == SELF:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if BANNED.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()[:100]}")
    assert not offenders, "machine-absolute paths / secrets found:\n" + "\n".join(offenders)
