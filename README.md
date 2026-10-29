# DeepCommit

> Automated agentic pipeline for synthesizing long-horizon coding tasks from the
> commit histories of open-source repositories.

DeepCommit powers the **SWE-Milestone** benchmark for evaluating AI agents on continuous
software evolution ([paper](https://arxiv.org/abs/2603.13428)).

[![CI](https://github.com/DeepCommit-ai/DeepCommit/actions/workflows/ci.yml/badge.svg)](https://github.com/DeepCommit-ai/DeepCommit/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python ≥3.10](https://img.shields.io/badge/python-%E2%89%A53.10-blue.svg)](pyproject.toml)
[![arXiv](https://img.shields.io/badge/arXiv-2603.13428-b31b1b.svg)](https://arxiv.org/abs/2603.13428)

> ⚠️ **Status:** early research preview — expect bugs, and layout/APIs/workflow may still change.
>
> - **Scope:** best results for release ranges within **~20,000 source LoC**.
> - **Languages:** only repositories written in **Python, JS/TS, Go, Java, or Rust** are supported.
> - **Phase 1 (Preprocess)** and **Phase 2 (DAG construction)** are relatively stable and run end-to-end without human intervention.
> - **Phase 3 (Env Resolution)** is experimental — it can be costly and often produces unsatisfactory one-shot results that require several human-in-the-loop iterations to refine.
> - **Model:** recommended to run with **Opus 4.8** (max).

## Installation

DeepCommit targets Python ≥ 3.10. Run `./setup-dev.sh` to set everything up
(`uv`, virtualenv, dependencies, and direnv).

## Configuration

Settings live in `.envrc` at the project root. Edit it, then run `direnv allow`
once to activate ([direnv](https://direnv.net/) auto-loads it on entry):

```bash
# Required. GitHub API tokens for the crawler. It is rate-limited, so pass
# several comma-separated tokens to avoid throttling.
export GITHUB_CRAWLER_TOKENS="ghp_xxx,ghp_yyy,ghp_zzz"

# Optional. Where all data is stored — pick a disk with plenty of space.
# Defaults to $PWD/DATA if unset.
export DEEPCOMMIT_DATA_ROOT="$PWD/DATA"
```

## Usage & Pipeline Overview

DeepCommit runs as a four-phase pipeline. See
[`docs/execution_guide.md`](docs/execution_guide.md) for the end-to-end walkthrough
and how to run each phase.

### Multi-repo workspaces

Milestones can span several repositories (for example an API contract repo plus its SDKs) and end as a Harbor multi-step
CSF task, where one agent keeps one environment across all steps and hidden tests grade every repo. Describe the repos in a
workspace manifest and run `deepcommit workspace <validate|preproc|merge|run|bundle|export-task|finalize-task|check-task>`.

```bash
uv run deepcommit workspace validate config/qdrant_clients_v1.workspace.yaml
uv run deepcommit workspace preproc  config/qdrant_clients_v1.workspace.yaml
uv run deepcommit workspace merge    config/qdrant_clients_v1.workspace.yaml
uv run deepcommit workspace run      config/qdrant_clients_v1.workspace.yaml --trial-name <trial>
uv run deepcommit workspace export-task   config/qdrant_clients_v1.workspace.yaml --trial-name <trial>
uv run deepcommit workspace finalize-task <task folder>
```

The full sequence, the milestone rules and the grading are in [docs/multirepo.md](docs/multirepo.md).

#### What is in the multi-repo code

| Path | What it holds |
| --- | --- |
| [`deepcommit/multirepo/`](deepcommit/multirepo/) | Workspace manifest (`workspace.py`), per-repo Phase 1 merge (`merge.py`), cross-repo edge detection (`xrepo.py`), SHA to repo resolver, and `bundle.py` which joins single-repo milestones into cross-repo ones |
| [`deepcommit/export/`](deepcommit/export/) | CSF task export: `csf_task.py` (task folder from milestones), `finalize.py` (measure, curate, fold, instructions, README), `curate.py` (F2P/P2P ids), `fold.py` (merge ungradable steps), `gen_instructions.py`, `csf_check.py` (format checker) |
| [`deepcommit/export/templates/`](deepcommit/export/templates/) | Files copied into every task: `grade.py`, `test.sh`, `measure.py`, and `viewer_gen.py` (HTML viewer of a task) |
| [`config/`](config/) and [`config/examples/`](config/examples/) | Workspace manifests: the Qdrant pilot, plus Qdrant and Temporal examples |
| [`docs/multirepo.md`](docs/multirepo.md) | How it works and the commands to generate a task |
| [`examples/qdrant-clients-v1/`](examples/qdrant-clients-v1/) | A finished run with every artifact (see below) |

#### Example: Qdrant + two SDKs

[`examples/qdrant-clients-v1/`](examples/qdrant-clients-v1/) is a complete pilot: Qdrant (contract only) + `rust-client` + `qdrant-client`,
v1.18.0 to v1.20.0. 35 milestones were found, 19 became steps, and measuring the real tests folded them into 11 steps. The oracle scores 1
on all 11 and the no-op scores 0 on all 11.

| Look at | To see |
| --- | --- |
| [`export/task_viewer.html`](examples/qdrant-clients-v1/export/task_viewer.html) | Each step's instruction, gold code and hidden tests (download and open in a browser) |
| [`export/graph.html`](examples/qdrant-clients-v1/export/graph.html) | The commit and milestone graph |
| [`phase2_milestones/`](examples/qdrant-clients-v1/phase2_milestones/) | Milestone proposals, consolidation report and final `milestones.csv` |
| [`task/`](examples/qdrant-clients-v1/task/) | The task folder: `task.toml`, instructions, gold patches, test ids, grader |
| [`gates/`](examples/qdrant-clients-v1/gates/) | Oracle and no-op results per step |
| [`ARTIFACTS.md`](examples/qdrant-clients-v1/ARTIFACTS.md) | Where the complete task zip (69 MB) and a copy of the artifacts are stored |

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). For security issues, see [SECURITY.md](SECURITY.md).

## Citation

If you enjoy DeepCommit or SWE-Milestone, please cite our paper:

```bibtex
@misc{deng2026swemilestoneevaluatingaiagents,
      title={SWE-Milestone: Evaluating AI Agents on Continuous Software Evolution},
      author={Gangda Deng and Zhaoling Chen and Zhongming Yu and Haoyang Fan and Yuhong Liu and Yuxin Yang and Dhruv Parikh and Rajgopal Kannan and Le Cong and Mengdi Wang and Qian Zhang and Viktor Prasanna and Xiangru Tang and Xingyao Wang},
      year={2026},
      eprint={2603.13428},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2603.13428},
}
```

## License

Licensed under the [Apache License 2.0](LICENSE). © The DeepCommit Authors.
