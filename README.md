# DeepCommit

> Automated agentic pipeline for synthesizing long-horizon coding tasks from the
> commit histories of open-source repositories.

DeepCommit powers the **EvoClaw** benchmark for evaluating AI agents on continuous
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

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md). For security issues, see [SECURITY.md](SECURITY.md).

## Citation

If you use DeepCommit or the EvoClaw benchmark, please cite the paper (see
[CITATION.cff](CITATION.cff)):

```bibtex
@misc{deng2026evoclaw,
  title         = {EvoClaw: Evaluating AI Agents on Continuous Software Evolution},
  author        = {Deng, Gangda and Chen, Zhaoling and Yu, Zhongming and Fan, Haoyang
                   and Liu, Yuhong and Yang, Yuxin and Parikh, Dhruv and Kannan, Rajgopal
                   and Cong, Le and Wang, Mengdi and Zhang, Qian and Prasanna, Viktor
                   and Tang, Xiangru and Wang, Xingyao},
  year          = {2026},
  eprint        = {2603.13428},
  archivePrefix = {arXiv},
  primaryClass  = {cs.SE},
  url           = {https://arxiv.org/abs/2603.13428}
}
```

## License

Licensed under the [Apache License 2.0](LICENSE). © The DeepCommit Authors.
