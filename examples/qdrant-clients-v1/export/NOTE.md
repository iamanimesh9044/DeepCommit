# Authoring sources

`render.py`, `steps.json`, `gold/`, `hidden/` and `instructions/` are the exporter's output and are **outdated**: after
`deepcommit workspace finalize-task` the task folder's `steps/` is the source of truth (steps were folded and renumbered, ids.json
added, instructions rewritten). Do not re-run `render.py`.

- `task_pre_finalize/`: the exported task before finalize (19 steps, file-level tests)
- `measure.json`: per-test outcomes of the 19 original steps (before/after each gold patch, 2 runs)
- `measure_folded.json`: the same merged into the 11 final steps (`members` lists the original steps)
- `export_report.json`: exporter report (milestone of each original step, conflicts, background commits)
- `graph.html`: commit and milestone graph viewer
