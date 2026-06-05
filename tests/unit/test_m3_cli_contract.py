"""Exit-code contract: a missing required input is exit 1 (input invalid), never
exit 2 (which is reserved for a verdict violation/fail and drives the agent's
re-plan). Mirrors frozen_check_gate.py, which treats missing input as exit 1.
"""

import importlib

import pytest

from deepcommit.harness.main_orch import context as ctx

# (module, entry-point attr). freeze-apply + unfreeze share one module (verbs.freeze)
# with a dedicated *_main per verb; every other verb is one module exposing ``main``.
VERB_ENTRYPOINTS = [
    ("dashboard", "main"),
    ("verify_dag", "main"),
    ("plan_validate", "main"),
    ("freeze", "freeze_apply_main"),
    ("freeze", "unfreeze_main"),
    ("preflight", "main"),
    ("snapshot", "main"),
]


@pytest.mark.parametrize("modname, entry", VERB_ENTRYPOINTS)
def test_missing_required_input_returns_input_invalid_not_violation(modname, entry):
    mod = importlib.import_module(f"deepcommit.harness.main_orch.verbs.{modname}")
    main = getattr(mod, entry)
    # no args at all → must return exit 1 (input invalid), not raise SystemExit / return 2
    assert main([]) == ctx.INPUT_INVALID
