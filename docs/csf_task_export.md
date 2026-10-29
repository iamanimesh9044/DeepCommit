# From milestones to a runnable CSF task

Input: a finished workspace trial (Phase 2 `milestones.csv` + `dependencies.csv`). Output: a Harbor multi-step task
(`schema_version = "1.4"`, family `cross-repo-change`) whose oracle scores 1 on every step and whose no-op scores 0.

```bash
# 1. export: gold patches and hidden test files per milestone, Dockerfile skeleton
uv run deepcommit workspace export-task config/qdrant_clients_v1.workspace.yaml --trial-name <trial> \
    --milestones-csv <stage4>/milestones.csv --dependencies-csv <stage4>/dependencies.csv

# 2. complete environment/Dockerfile by hand once per repo set (toolchains, dependency prefetch, services the tests need,
#    editable install of Python packages), and fill tests/suites.json (run_cmd, parser, services, whole_suite for cargo)

# 3. measure, curate, fold, instructions, README (needs Docker and the `claude` CLI)
uv run deepcommit workspace finalize-task <task folder>

# 4. gates (Harbor >= 0.25; the 0.20 on many machines cannot run separate-verifier multi-step tasks)
harbor run -p <task folder> -a oracle        # every step reward 1
harbor run -p <task folder> -a nop           # every step reward 0
uv run deepcommit workspace check-task <task folder>
```

What `finalize-task` does:

| Stage | Module | Effect |
| --- | --- | --- |
| measure | `export/templates/measure.py` | In the task image, apply gold step by step; run the step's own test files before the gold patch and the full suites after it, `--runs` times |
| curate | `export/curate.py` | F2P = the step's own tests that fail before and pass in every run after; P2P = earlier steps' tests passing in the previous and the current state; flaky tests drop out |
| fold | `export/fold.py` | A step with no F2P test is folded into its successor (gold patches concatenated, steps renumbered, DAG rewritten) |
| instructions | `export/gen_instructions.py` | Behaviour-level statement from the gold patch + hidden tests (no hashes, names every public name the tests use) |
| README | `export/readme.py` | Status table, grading, known limits |

Grading is by id: `steps/<step>/tests/ids.json` lists `<repo>::<id>` for `f2p` and `p2p`; `tests/grade.py` counts only those.
`tests/suites.json` may declare `services` (started by `test.sh` before the suites, e.g. a Qdrant server) and
`whole_suite: true` for runners that cannot take a file list (cargo).

Why a step can be ungradable: background-first export puts every unowned commit into the base state, so a base can be
inconsistent (here: Python code using generated stubs that a later milestone regenerates, so the package does not import
until then). The first step that makes it work gets all the tests; earlier steps fold into it.
