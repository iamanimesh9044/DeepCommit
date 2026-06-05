#!/usr/bin/env python3
"""Diagnose the *real* root causes behind H_error_within_budget failures.

When a milestone fails ``H_error_within_budget`` with hundreds of persistent
``error->error`` tests, the per-test ``message`` is frequently a generic bridge
placeholder ("Django runtests.py reported error") or a bare ``Traceback`` header.
The actual exception is still in the artifacts -- either in the pytest record
(``tests[].{setup,call,teardown}.{crash.message,longrepr}``) or, for the pure
``django_runtests_bridge`` framework, in the sibling raw stdout log
``end_default.json.log``. This module extracts those true causes, buckets them
into a small taxonomy, and classifies each milestone so M3 can tell apart:

- ``ENV_FIXABLE``      -- a shared environment/config root cause (SQLite FK /
  migration, missing ``INSTALLED_APPS``) that one base-image or settings fix
  resolves across many milestones. Worth a one-time batch investment.
- ``TIMING_DAG``       -- tests reference an API absent at this milestone-end
  (attr/kwarg/import skew, "introduced post-milestone"). Fixing the env does
  nothing; this needs a DAG fix or the milestone is a non-graded candidate.
- ``HARNESS_ARTIFACT`` -- an error injected by the harness itself (e.g. the
  env-patch "subtest not picklable" guard), not a real defect.
- ``TEST_FAIL``        -- a genuine assertion failure.

No Docker rerun is needed: every cause is read from existing artifacts. This is
the offline "enrichment" step -- it recovers the cause the bridge dropped.

Usage::

    python3 -m deepcommit.harness.main_orch.diagnose_error_causes <work_dir> --all
    python3 -m deepcommit.harness.main_orch.diagnose_error_causes <work_dir> -m maint_002 maint_018
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Optional

import yaml

# An exception line in either a pytest longrepr ("E   ValueError: ...") or a raw
# unittest/runtests traceback (bare "ValueError: ..." at column 0).
_EXC_LINE = re.compile(
    r"(?m)^(?:E\s+)?\s*"
    r"([A-Za-z_][\w.]*(?:Error|Exception|Warning|NotFound))"
    r"(?::[ \t]+(.*\S))?[ \t]*$"
)

# (bucket, substrings) — first match wins, so order matters (specific first).
STARTER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("HARNESS_ARTIFACT", ("[env-patch]", "not picklable", "outcome preserved as error")),
    (
        "ENV_DB",
        ("schema editor", "operationalerror", "notsupportederror", "integrityerror", "databaseerror", "no such table"),
    ),
    ("ENV_APP", ("no installed app", "aren't loaded", "appregistrynotready", "apps aren't loaded")),
    ("TIMING_POST_MS", ("introduced post-milestone",)),
    ("IMPORT_MISSING", ("cannot import name", "no module named", "failed to import")),
    ("API_KWARG", ("unexpected keyword", "__init__()", "positional argument", "takes no arguments")),
    ("API_ATTR", ("no attribute", "has no attribute")),
    ("TEST_ASSERT", ("assertionerror",)),
)

STARTER_CATEGORY: Mapping[str, str] = {
    "ENV_DB": "ENV_FIXABLE",
    "ENV_APP": "ENV_FIXABLE",
    "IMPORT_MISSING": "TIMING_DAG",
    "API_KWARG": "TIMING_DAG",
    "API_ATTR": "TIMING_DAG",
    "TIMING_POST_MS": "TIMING_DAG",
    "HARNESS_ARTIFACT": "HARNESS_ARTIFACT",
    "TEST_ASSERT": "TEST_FAIL",
}


def normalize_cause(sig: str) -> str:
    """Collapse value-specific detail so equal root causes group together."""
    sig = re.sub(r"'[^']*'", "'X'", sig)
    sig = re.sub(r'"[^"]*"', '"X"', sig)
    sig = re.sub(r"\(/[^)]*\)", "", sig)  # drop "(/testbed/...path)"
    sig = re.sub(r"/testbed/\S+", "<path>", sig)
    sig = re.sub(r"commit \w+", "commit Z", sig)
    sig = re.sub(r"\b\d+\b", "N", sig)
    return re.sub(r"\s+", " ", sig).strip()[:110]


class Taxonomy(NamedTuple):
    rules: tuple[tuple[str, tuple[str, ...]], ...]  # (bucket, substrings), first-match-wins
    categories: Mapping[str, str]  # bucket -> coarse category
    grounded_buckets: "frozenset[str]" = frozenset()  # buckets backed by a non-empty grounded_by

    def classify(self, sig: str) -> tuple[str, str]:
        low = sig.lower()
        for bucket, keys in self.rules:
            if any(k in low for k in keys):
                return bucket, self.categories.get(bucket, "OTHER")
        return "OTHER", "OTHER"


STARTER = Taxonomy(STARTER_RULES, STARTER_CATEGORY, frozenset(STARTER_CATEGORY))


def classify_cause(sig: str, taxonomy: "Taxonomy | None" = None) -> tuple[str, str]:
    """Return ``(bucket, category)`` for a cause string under ``taxonomy`` (default: STARTER)."""
    return (taxonomy or STARTER).classify(sig)


_TAXONOMY_FILE = "_error_taxonomy.yaml"
_OTHER_PCT_FLOOR = 10.0  # report lists the unmatched causes once OTHER mass crosses this
ALLOWED_CATEGORIES = frozenset(
    {"ENV_FIXABLE", "TIMING_DAG", "HARNESS_ARTIFACT", "TEST_FAIL", "NEEDS_PROVENANCE", "OTHER"}
)


def load_taxonomy(work_dir: Path) -> Taxonomy:
    """Workspace-curated taxonomy; falls back to STARTER when absent/empty/malformed.

    Reads ``<work_dir>/_error_taxonomy.yaml`` = {inherit_starter: bool=true,
    rules: [{bucket, category, patterns, grounded_by?}]}. Workspace rules are
    checked first (first-match-wins); STARTER is appended unless ``inherit_starter``
    is false. A rule with a non-empty ``grounded_by`` is recorded as grounded.
    """
    path = work_dir / _TAXONOMY_FILE
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return STARTER
    if not isinstance(doc, Mapping):
        return STARTER
    rules: list[tuple[str, tuple[str, ...]]] = []
    cats: dict[str, str] = {}
    grounded: set[str] = set()
    for r in doc.get("rules") or []:
        if not isinstance(r, Mapping):
            continue
        bucket = str(r.get("bucket") or "").strip()
        patterns = tuple(str(p).lower() for p in (r.get("patterns") or []) if str(p).strip())
        if not bucket or not patterns:
            continue
        category = str(r.get("category") or "OTHER")
        rules.append((bucket, patterns))
        cats[bucket] = category if category in ALLOWED_CATEGORIES else "OTHER"
        if str(r.get("grounded_by") or "").strip():
            grounded.add(bucket)
    if doc.get("inherit_starter", True):
        rules.extend(STARTER_RULES)
        cats = {**STARTER_CATEGORY, **cats}  # workspace category wins on a bucket-name clash
        grounded |= set(STARTER_CATEGORY)  # starter buckets count as grounded
    return Taxonomy(tuple(rules), cats, frozenset(grounded)) if rules else STARTER


def _starter_as_yaml() -> str:
    """Emit STARTER as an adoptable _error_taxonomy.yaml (for --print-starter)."""
    rules = [{"bucket": b, "category": STARTER_CATEGORY[b], "patterns": list(p)} for b, p in STARTER_RULES]
    return str(yaml.safe_dump({"inherit_starter": True, "rules": rules}, sort_keys=False, allow_unicode=True))


def _causes_in_text(text: str) -> list[str]:
    """All exception lines in a traceback blob, in order, wrappers dropped."""
    out: list[str] = []
    for m in _EXC_LINE.finditer(text):
        typ, msg = m.group(1), (m.group(2) or "").strip()
        sig = f"{typ}: {msg}" if msg else typ
        if "Failed to import test module" in sig:
            # unittest wrapper — the real ImportError follows it
            continue
        out.append(sig)
    return out


def cause_from_record(test: Mapping[str, Any]) -> Optional[str]:
    """Extract the deepest real exception from a pytest-json test record.

    The last exception line is the real cause (pytest/unittest print the
    triggering exception last). Returns None when the record carries no
    exception text (the bridge-placeholder case -> caller falls back to .log).
    """
    blobs: list[str] = []
    for section in ("call", "setup", "teardown"):
        sec = test.get(section)
        if isinstance(sec, Mapping):
            longrepr = sec.get("longrepr")
            if longrepr:
                blobs.append(str(longrepr))
            crash = sec.get("crash")
            if isinstance(crash, Mapping) and crash.get("message"):
                blobs.append(str(crash["message"]))
    causes = _causes_in_text("\n".join(blobs))
    return causes[-1] if causes else None


def causes_from_log(log_path: Path, limit_bytes: int = 60_000_000) -> Counter:
    """Frequency of true exceptions in a raw runtests stdout log (.log).

    Used only for the pure ``django_runtests_bridge`` framework, whose JSON
    error records are all generic placeholders. The log is read defensively
    (latin-1, capped) because it can be tens of MB of mixed stdout.
    """
    counter: Counter = Counter()
    try:
        text = log_path.read_text(encoding="latin-1", errors="replace")[:limit_bytes]
    except OSError:
        return counter
    for sig in _causes_in_text(text):
        counter[normalize_cause(sig)] += 1
    return counter


def _latest_attempt(work_dir: Path, milestone_id: str) -> Optional[str]:
    bs_path = work_dir / "dockerfiles" / milestone_id / "build_status.json"
    try:
        bs = json.loads(bs_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    paths = bs.get("artifact_paths")
    if isinstance(paths, Mapping) and paths.get("latest_attempt"):
        return str(paths["latest_attempt"])
    return None


def analyze_milestone(
    work_dir: Path, milestone_id: str, taxonomy: "Taxonomy | None" = None
) -> Optional[dict[str, Any]]:
    """Per-milestone error-cause breakdown, or None if there is nothing to read."""
    attempt = _latest_attempt(work_dir, milestone_id)
    if not attempt:
        return None
    base = work_dir / "test_results" / milestone_id / "refinement" / attempt
    end = base / "end_default.json"
    if not end.exists():
        end = base / "end.json"
    if not end.exists():
        return None
    try:
        doc = json.loads(end.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    framework = doc.get("_framework", "?")
    errors = [t for t in doc.get("tests", []) if t.get("outcome") in ("error", "errored")]
    if not errors:
        return None

    raw_causes = [cause_from_record(t) for t in errors]
    covered = [c for c in raw_causes if c]
    source = "record"
    # Bridge placeholder case: records have no exception text -> mine the .log.
    if len(covered) < 0.5 * len(errors):
        log_counter = causes_from_log(base / "end_default.json.log")
        if log_counter:
            causes = log_counter
            source = "log"
        else:
            causes = Counter(normalize_cause(c) for c in covered)
    else:
        causes = Counter(normalize_cause(c) for c in covered)

    tax = taxonomy or STARTER
    cause_bucket = {sig: tax.classify(sig)[0] for sig in causes}
    buckets: Counter = Counter()
    for sig, n in causes.items():
        buckets[cause_bucket[sig]] += n
    categories: Counter = Counter()
    for bucket, n in buckets.items():
        categories[tax.categories.get(bucket, "OTHER")] += n
    total = sum(buckets.values()) or 1
    dominant_bucket, dominant_n = buckets.most_common(1)[0] if buckets else ("OTHER", 0)
    other_causes = [sig for sig, _ in causes.most_common() if cause_bucket[sig] == "OTHER"]

    return {
        "milestone_id": milestone_id,
        "framework": framework,
        "error_count": len(errors),
        "cause_source": source,
        "dominant_bucket": dominant_bucket,
        "dominant_category": categories.most_common(1)[0][0] if categories else "OTHER",
        "dominant_fraction": round(dominant_n / total, 3),
        "other_count": buckets.get("OTHER", 0),
        "buckets": dict(buckets.most_common()),
        "categories": dict(categories.most_common()),
        "top_causes": dict(causes.most_common(6)),
        "top_other_causes": other_causes[:6],
    }


def analyze_scope(work_dir: Path, milestone_ids: Iterable[str], taxonomy: "Taxonomy | None" = None) -> dict[str, Any]:
    per_ms: dict[str, Any] = {}
    global_buckets: Counter = Counter()
    global_categories: Counter = Counter()
    for mid in milestone_ids:
        res = analyze_milestone(work_dir, mid, taxonomy)
        if res is None:
            continue
        per_ms[mid] = res
        global_buckets.update(res["buckets"])
        global_categories.update(res["categories"])
    tax = taxonomy or STARTER
    declared = list(dict.fromkeys(b for b, _ in tax.rules))
    digest = hashlib.sha256(repr((tax.rules, sorted(tax.categories.items()))).encode()).hexdigest()[:12]
    global_total = sum(global_buckets.values()) or 1
    return {
        "milestones": per_ms,
        "global_buckets": dict(global_buckets.most_common()),
        "global_categories": dict(global_categories.most_common()),
        "env_fixable_milestones": sorted(m for m, r in per_ms.items() if r["dominant_category"] == "ENV_FIXABLE"),
        "timing_dag_milestones": sorted(m for m, r in per_ms.items() if r["dominant_category"] == "TIMING_DAG"),
        "other_pct": round(100 * global_buckets.get("OTHER", 0) / global_total, 1),
        "taxonomy": {
            "source": "workspace" if (work_dir / _TAXONOMY_FILE).exists() else "starter",
            "rule_count": len(tax.rules),
            "rules_digest": digest,
            "inherit_starter": set(STARTER_CATEGORY).issubset(set(declared)),
            "unused_rules": [b for b in declared if b not in global_buckets],
            "ungrounded_rules": sorted(set(declared) - set(tax.grounded_buckets)),
        },
    }


def _scope_milestone_ids(work_dir: Path) -> list[str]:
    fp = work_dir / "_frozen_prefix.json"
    if fp.exists():
        try:
            data = json.loads(fp.read_text())
            ids = data.get("scope_milestones") or data.get("frozen_milestones")
            if isinstance(ids, list) and ids:
                return [str(i) for i in ids]
        except (OSError, json.JSONDecodeError):
            pass
    return sorted(p.name for p in (work_dir / "dockerfiles").iterdir() if p.is_dir())


def _format_report(report: dict[str, Any]) -> str:
    tax = report.get("taxonomy") or {}
    lines = [
        "=== taxonomy ===",
        f"  source={tax.get('source', '?')}  rules={tax.get('rule_count', 0)}  "
        f"digest={tax.get('rules_digest', '?')}  other_pct={report.get('other_pct', 0.0)}%",
    ]
    unused = tax.get("unused_rules") or []
    ungrounded = tax.get("ungrounded_rules") or []
    if unused:
        lines.append(f"  caution: unused rules (shadowed / no matching mass this scope): {unused}")
    if ungrounded:
        lines.append(f"  caution: ungrounded rules (no grounded_by — auto-mislabels what they match): {ungrounded}")
    lines.append("")
    lines.append("=== global error-cause categories ===")
    total = sum(report["global_categories"].values()) or 1
    for cat, n in report["global_categories"].items():
        lines.append(f"  {n:>6} ({100 * n / total:4.1f}%)  {cat}")
    if report.get("other_pct", 0.0) >= _OTHER_PCT_FLOOR:
        other_causes: list[str] = []
        seen: set[str] = set()
        for r in report["milestones"].values():
            for c in r.get("top_other_causes", []):
                if c not in seen:
                    seen.add(c)
                    other_causes.append(c)
        lines.append("")
        lines.append(f"  OTHER mass is {report['other_pct']}% — unrecognized causes to curate:")
        for c in other_causes[:10]:
            lines.append(f"    {c}")
    lines.append("")
    lines.append(f"  ENV_FIXABLE milestones (batch env fix): {report['env_fixable_milestones']}")
    lines.append(f"  TIMING_DAG milestones (DAG fix / non-grade candidate): {report['timing_dag_milestones']}")
    lines.append("")
    lines.append("=== per-milestone (>=50 errors) ===")
    rows = sorted(report["milestones"].values(), key=lambda r: -r["error_count"])
    omitted = 0
    for r in rows:
        if r["error_count"] < 50:
            omitted += 1
            continue
        lines.append(
            f"  {r['milestone_id']:26} {r['error_count']:>5}  "
            f"{r['dominant_bucket']:16} -> {r['dominant_category']:16} "
            f"(src={r['cause_source']})"
        )
    if omitted:
        lines.append(f"  ({omitted} milestones with <50 errors omitted from this view)")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("work_dir", type=Path, help="harness workspace directory")
    p.add_argument("-m", "--milestones", nargs="*", help="milestone ids (default: scope)")
    p.add_argument("--all", action="store_true", help="every dockerfiles/*/ milestone")
    p.add_argument("--json", type=Path, default=None, help="write full report JSON here")
    p.add_argument(
        "--print-starter",
        action="store_true",
        help="emit the built-in starter taxonomy as adoptable YAML to stdout and exit",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.print_starter:
        print(_starter_as_yaml(), end="")
        return 0
    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"work_dir not found: {work_dir}", file=sys.stderr)
        return 2
    if args.milestones:
        mids = args.milestones
    elif args.all:
        mids = sorted(p.name for p in (work_dir / "dockerfiles").iterdir() if p.is_dir())
    else:
        mids = _scope_milestone_ids(work_dir)
    tax = load_taxonomy(work_dir)
    report = analyze_scope(work_dir, mids, tax)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"wrote {args.json}")
    print(_format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
