"""Unit tests for the m3 verb-suite CLI dispatch.

``python -m deepcommit.harness.main_orch <verb>`` dispatches to one module per
verb (lazy import), plus thin aliases for the existing frozen_check_gate /
diagnose_error_causes scripts.
"""

from deepcommit.harness.main_orch import cli


def test_cli_registers_all_seven_verbs_and_two_aliases():
    expected = {
        "dashboard",
        "verify-dag",
        "plan-validate",
        "freeze-apply",
        "unfreeze",
        "preflight",
        "snapshot",
        # aliases to existing scripts
        "frozen-check",
        "diagnose-errors",
    }
    assert expected.issubset(set(cli.VERBS))
    # guard + audit-artifacts were removed (folded into preflight / dashboard)
    assert "guard" not in cli.VERBS
    assert "audit-artifacts" not in cli.VERBS


def test_cli_unknown_verb_returns_input_invalid(capsys):
    code = cli.main(["no-such-verb"])
    assert code == 1
    err = capsys.readouterr().err
    assert "no-such-verb" in err


def test_cli_no_verb_prints_usage_and_returns_nonzero(capsys):
    code = cli.main([])
    assert code != 0
    combined = capsys.readouterr()
    assert "verb" in (combined.out + combined.err).lower()


def test_cli_help_lists_verbs(capsys):
    code = cli.main(["--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dashboard" in out
    assert "verify-dag" in out


def test_cli_dispatches_to_real_alias_module(capsys):
    # frozen-check with no workspace exercises the dispatch path end-to-end:
    # the alias resolves, imports, and the underlying script returns 1 (input invalid).
    code = cli.main(["frozen-check"])
    assert code == 1
