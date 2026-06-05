# Contributing to DeepCommit

Thanks for your interest in contributing! DeepCommit is an automated pipeline for
reconstructing verifiable software evolution itineraries from raw git histories. This
guide covers how to set up your environment, the standards we follow, and how to get a
change merged.

## Code of Conduct

This project adheres to the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating
you agree to uphold it. Report unacceptable behavior to <gangdade@usc.edu>.

## Development setup

DeepCommit uses [`uv`](https://docs.astral.sh/uv/) for environment and dependency
management and targets **Python ≥ 3.10**.

```bash
git clone https://github.com/DeepCommit-ai/DeepCommit.git
cd DeepCommit
uv sync --dev
```

If you use [direnv](https://direnv.net/), the checked-in `.envrc` activates the
virtualenv automatically. Export any tokens you need (e.g. `GITHUB_CRAWLER_TOKENS`) in
your shell — **never commit secrets** (see [Secrets and credentials](#secrets-and-credentials)).

## Before you open a pull request

Run the same checks CI runs:

```bash
uv run ruff check deepcommit/              # lint
uv run ruff format --check deepcommit/     # formatting
uv run pytest tests/ -m "not integration"  # unit tests
```

- `uv run ruff format deepcommit/` auto-formats your changes.
- Tests that need Docker or real git repositories are marked `@pytest.mark.integration`
  and are skipped by the default run.

## Pull request workflow

1. Create a topic branch off `main` (`git switch -c feat/my-change`).
2. Make focused commits following [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`). This keeps history
   readable and enables changelog generation.
3. Push and open a PR against `main`, filling in the PR template.
4. CI (lint + tests) must pass and at least one maintainer must approve before merge.
5. Keep your branch up to date with `main`.

## Licensing of contributions

DeepCommit is licensed under the [Apache License 2.0](LICENSE). Per Section 5 of that
license, any contribution you intentionally submit for inclusion is licensed under the
same terms, without any additional conditions. You retain copyright to your work.

## Secrets and credentials

Never commit API keys, tokens, or credentials. Use environment variables (see `.envrc`)
or a local, untracked config file. If you commit a secret by accident, **rotate it
immediately** and notify a maintainer — deleting it in a later commit is *not* enough,
because it remains in git history.

## Reporting bugs and requesting features

Use the [issue templates](https://github.com/DeepCommit-ai/DeepCommit/issues/new/choose).
For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.
