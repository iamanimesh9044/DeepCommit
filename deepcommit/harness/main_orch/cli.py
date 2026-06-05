"""Verb dispatch for the m3 orchestration-support suite.

Usage:
    python -m deepcommit.harness.main_orch <verb> [args...]

Each verb is one module exposing ``main(argv) -> int`` (the frozen_check_gate
exit-code convention: 0 ok/pass · 1 input invalid · 2 violation/fail). Verbs are
imported lazily so a single invocation only loads the module it runs.
"""

from __future__ import annotations

import importlib
import sys
from typing import Sequence

# verb name -> (module path, callable name, one-line help)
VERBS: dict[str, tuple[str, str, str]] = {
    "dashboard": (
        "deepcommit.harness.main_orch.verbs.dashboard",
        "main",
        "survey each milestone's latest state (read-only)",
    ),
    "verify-dag": (
        "deepcommit.harness.main_orch.verbs.verify_dag",
        "main",
        "per dag_update event: graph invariants + commit conservation",
    ),
    "plan-validate": (
        "deepcommit.harness.main_orch.verbs.plan_validate",
        "main",
        "validate the round ledger (full schema) + rejected_suggestions",
    ),
    "freeze-apply": (
        "deepcommit.harness.main_orch.verbs.freeze",
        "freeze_apply_main",
        "atomic FREEZE write to _frozen_prefix.json + invariants",
    ),
    "unfreeze": (
        "deepcommit.harness.main_orch.verbs.freeze",
        "unfreeze_main",
        "remove frozen MIDs (still closed) + append UNFREEZE history",
    ),
    "preflight": (
        "deepcommit.harness.main_orch.verbs.preflight",
        "main",
        "pre-operation go/no-go: deliverables clean / fresh / self-consistent",
    ),
    "snapshot": (
        "deepcommit.harness.main_orch.verbs.snapshot",
        "main",
        "snapshot recipe files before surgical; --restore reverses it",
    ),
    # thin aliases to existing scripts in this package
    "frozen-check": (
        "deepcommit.harness.main_orch.frozen_check_gate",
        "main",
        "alias: frozen-prefix hard gate aggregation",
    ),
    "diagnose-errors": (
        "deepcommit.harness.main_orch.diagnose_error_causes",
        "main",
        "alias: offline H_error root-cause triage (workspace-curated taxonomy)",
    ),
}


def _usage() -> str:
    lines = ["usage: python -m deepcommit.harness.main_orch <verb> [args...]", "", "verbs:"]
    width = max(len(v) for v in VERBS)
    for verb, (_mod, _func, help_text) in VERBS.items():
        lines.append(f"  {verb.ljust(width)}  {help_text}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        print(_usage(), file=sys.stderr)
        return 1
    if argv[0] in ("-h", "--help"):
        print(_usage())
        return 0

    verb = argv[0]
    entry = VERBS.get(verb)
    if entry is None:
        print(f"unknown verb: {verb}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 1

    module_path, func_name, _help = entry
    module = importlib.import_module(module_path)
    func = getattr(module, func_name)
    return int(func(argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
