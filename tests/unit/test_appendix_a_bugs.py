"""Appendix-A latent-bug pins for pure functions in repo_entity_retriever.

Wave 1 is test-only: we do NOT touch runtime source. The pure functions that
Appendix A flags as buggy are pinned here by asserting the *desired* behavior
and marking the assertion ``xfail(strict=True)``. Today it xfails (the bug is
present); the day someone fixes the source the test flips to XPASS and strict
turns that into a hard failure -- a self-cleaning reminder to delete the marker
and update Appendix A.

Target under test: ``deepcommit.tools.repo_entity.repo_entity_retriever._is_prerelease``
(a pure regex predicate, no I/O). Every expected value below was ground-truthed
by executing the real function, not traced statically.

The bug: ``_is_prerelease`` matches each family with an unanchored ``.*token.*``
pattern via ``re.match``. Because ``re.match`` only anchors at the *start* and
``.*`` is greedy, ``r".*token.*"`` degenerates into a plain substring search --
so any final tag whose name merely *contains* ``rc`` / ``test`` / ``pre`` / etc.
is mis-classified as a prerelease.
"""

import pytest

# _is_prerelease is pure, but the module imports pygit2 at top level. Gate the
# import so a missing heavy dep skips rather than errors -- without weakening any
# assertion below.
repo_entity_retriever = pytest.importorskip("deepcommit.tools.repo_entity.repo_entity_retriever")

_is_prerelease = repo_entity_retriever._is_prerelease


# --- Real prereleases: these positives MUST keep working regardless of the
# over-match bug pinned further down. One representative per pattern family.
REAL_PRERELEASES = [
    "v2.0.0-alpha.1",  # .*alpha.*
    "2.0-beta",  # .*beta.*
    "1.4.0rc2",  # .*rc.*
    "v3.0.0-rc.1",  # .*rc.*
    "1.0.0-pre",  # .*pre.*
    "1.5.0-dev",  # .*dev.*
    "nightly",  # .*nightly.*
    "snapshot-1",  # .*snapshot.*
    "v1.0.0-preview",  # .*preview.*
]


@pytest.mark.parametrize("tag", REAL_PRERELEASES)
def test_is_prerelease_flags_real_prereleases(tag):
    """Genuine prerelease tags are detected, one per pattern family."""
    result = _is_prerelease(tag)
    assert result is True
    # Exact-shape guard: a real ``bool``, never a truthy match object / 1.
    assert type(result) is bool


def test_is_prerelease_is_case_insensitive():
    """The ``tag.lower()`` normalization means upper-case families still match."""
    assert _is_prerelease("V2.0.0-ALPHA.1") is True
    assert _is_prerelease("1.0.0-RC1") is True
    assert _is_prerelease("NIGHTLY") is True
    assert _is_prerelease("AlPhA") is True  # mixed case exercises the same branch


# --- Final releases that genuinely contain NO prerelease marker. These already
# return False today (no pattern matches), so this is a regular always-passing
# guard, not an xfail.
TRUE_FINAL_RELEASES = [
    "v1.0.0",
    "1.2.3",
    "v2.4.0",
    "release-2024",
    "stable",
]


@pytest.mark.parametrize("tag", TRUE_FINAL_RELEASES)
def test_is_prerelease_does_not_flag_clean_final_tags(tag):
    """Plain final tags with no prerelease substring are not flagged."""
    result = _is_prerelease(tag)
    assert result is False
    assert type(result) is bool


def test_is_prerelease_empty_string_is_not_prerelease():
    """Degenerate input: the empty tag matches no pattern -> False (a real bool)."""
    result = _is_prerelease("")
    assert result is False
    assert type(result) is bool


# --- Characterization of the CURRENT (buggy) over-match behavior. These are NOT
# desired -- they are the present reality, locked in as a regression tripwire so
# that a future regex fix must consciously change them. They live OUTSIDE the
# xfail block because they describe what the function does *today* (returns
# True). The strict-xfail below asserts the inverse *desired* values; keeping
# both makes the contradiction (today True, should be False) explicit and
# machine-checked from two directions.
CURRENTLY_OVERMATCHED_FINALS = [
    "v1.0.0-latest",  # 'la(test)' caught by .*test.*
    "release-search",  # 'sea(rc)h' caught by .*rc.*
    "v2.0-arch",  # 'a(rc)h' caught by .*rc.*
    "compress-2.0",  # 'com(pre)ss' caught by .*pre.*
    "deprecated",  # 'de(pre)cated' caught by .*pre.*
    "source-1.0",  # 'sou(rc)e' caught by .*rc.*
    "march-1.0",  # 'ma(rc)h' caught by .*rc.*
    "greatest",  # 'grea(test)' caught by .*test.*
    "attestation",  # 'at(test)ation' caught by .*test.*
    "developer-build",  # 'de(dev)eloper' caught by .*dev.*
]


@pytest.mark.parametrize("tag", CURRENTLY_OVERMATCHED_FINALS)
def test_is_prerelease_currently_overmatches_incidental_substrings(tag):
    """Tripwire: the buggy predicate returns True today for incidental substrings.

    This pins the present (wrong) behavior. The companion strict-xfail test
    asserts the desired ``False`` for the same class of inputs. When the source
    is fixed, *this* test starts failing (forcing it to be updated to ``False``)
    AND the strict-xfail XPASSes -- two independent signals that the marker must
    be removed and Appendix A updated.
    """
    result = _is_prerelease(tag)
    assert result is True
    assert type(result) is bool


def test_is_prerelease_match_is_anchored_at_start_not_substring_with_newline():
    """Characterize the ``re.match`` + ``.`` (no DOTALL) artifact.

    The bug is *substring-like* but not a true substring search: ``re.match``
    anchors at the start and ``.`` does not span newlines, so a token sitting
    *after* a newline is NOT reached by the leading ``.*``. This pins that
    asymmetry precisely (token-before-newline matches, token-after-newline does
    not), which a naive 'switch to ``in``' fix would silently change.
    """
    # Token at the start of a line that begins the string -> matched.
    assert _is_prerelease("alpha\nfoo") is True
    # Same token, but now only reachable across a newline -> NOT matched, because
    # the greedy ``.*`` cannot cross the ``\n``.
    assert _is_prerelease("foo\nalpha") is False
    assert _is_prerelease("v1.0\nrc") is False


def test_is_prerelease_leading_whitespace_before_token_still_matches():
    """``.*`` happily consumes leading whitespace/tabs before the token."""
    assert _is_prerelease("  alpha") is True
    assert _is_prerelease("\talpha") is True


def test_is_prerelease_rejects_non_string_via_lower():
    """Adversarial type guard: non-str input hits ``tag.lower()`` -> AttributeError.

    Pins the exact failure mode (not a silent False / TypeError) so a future
    refactor that adds input coercion is an intentional, visible change.
    """
    for bad in (None, 123, ["alpha"]):
        with pytest.raises(AttributeError):
            _is_prerelease(bad)


@pytest.mark.xfail(
    strict=True,
    reason="Appendix A: _is_prerelease patterns '.*test.*'/'.*rc.*'/'.*pre.*'/'.*dev.*' "
    "are over-broad (unanchored .* + re.match == substring search) and mis-flag "
    "final tags whose names merely *contain* those substrings.",
)
def test_is_prerelease_does_not_flag_final_tags_with_substrings():
    """DESIRED behavior pin (currently XFAILs because the bug is present).

    Empirically verified failing inputs (real output today is True, desired False):

      * 'v1.0.0-latest'  -- 'la(test)'  is caught by r'.*test.*'
      * 'release-search' -- 'sea(rc)h'  is caught by r'.*rc.*'
      * 'v2.0-arch'      -- 'a(rc)h'    is caught by r'.*rc.*'
      * 'compress-2.0'   -- 'com(pre)ss' is caught by r'.*pre.*'
      * 'deprecated'     -- 'de(pre)cated' is caught by r'.*pre.*'
      * 'march-1.0'      -- 'ma(rc)h'   is caught by r'.*rc.*'
      * 'greatest'       -- 'grea(test)' is caught by r'.*test.*'

    None of these are prereleases; the substrings are incidental. When the source
    tightens the regexes (e.g. word-boundary / token anchors) this test XPASSes,
    strict-xfail turns that into a failure, and the marker must be removed.

    A single ``assert ... is False`` would XPASS the moment *any* one of these is
    fixed; listing the whole family makes the pin survive a partial fix and only
    flip green when every incidental-substring case is genuinely repaired.
    """
    assert _is_prerelease("v1.0.0-latest") is False  # 'latest' contains 'test'
    assert _is_prerelease("release-search") is False  # 'search' contains 'rc'
    assert _is_prerelease("v2.0-arch") is False  # 'arch' contains 'rc'
    assert _is_prerelease("compress-2.0") is False  # 'compress' contains 'pre'
    assert _is_prerelease("deprecated") is False  # 'deprecated' contains 'pre'
    assert _is_prerelease("march-1.0") is False  # 'march' contains 'rc'
    assert _is_prerelease("greatest") is False  # 'greatest' contains 'test'
