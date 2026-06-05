# SRS Generation Pipeline

This directory contains tools for generating Software Requirements Specifications (SRS) from milestone data and executing agent-based code generation.

## Core Scripts

### SRS Generation
- **`generate_srs.py`** - Complete SRS generation pipeline. Orchestrates container setup, prompt rendering, and 3-stage Claude Code execution (initial SRS → acceptance criteria refinement → environment dependencies).

- **`prepare_prompt.py`** - Prepares SRS generation prompts by combining milestone metadata, fail-to-pass tests, and commit patches. Used as a module by `generate_srs.py`.

### Code Generation
- **`run_srs_code_gen.py`** - Runs Claude Code agent inside Docker container to generate code from SRS. Enforces constraints (no git history/tests access), extracts patches and traces.

- **`generate_prompt_wrapper.py`** - Prompt renderer for cloud-based Docker agents that clone testbed repositories. Generates code-gen prompts from existing SRS files without local Docker operations.

### Testbed Management
- **`auto_upload_testbed.py`** - Automated GitHub repository uploader for testbed repositories. Creates organization repositories, pushes main branch and milestone tags, disables GitHub Actions, and verifies upload integrity. Supports both HTTPS and SSH authentication.

## Prompt Templates

- **`srs_gen_v1.tmpl`** - Main template for initial SRS generation. Defines task structure, inputs (mini SRS, commits, tests), and output format.

- **`acceptance_test_align.tmpl`** - Template for Stage 2: refining SRS acceptance criteria to align with fail-to-pass tests while avoiding implementation leakage.

- **`env_spec.tmpl`** - Template for Stage 3: comparing Docker environments to extract dependency changes (packages, env vars) while excluding code-level differences.

## Supporting Files

- **`test_generate_srs.py`** - Unit tests for SRS generation pipeline.

- **`gen_srs_skill/`** - Claude Code skill package for SRS generation workflows.

- **`QUICKSTART.md`** - Quick reference guide for common operations.

- **`__init__.py`** - Package initialization.

## Pipeline Flow

```
generate_srs.py
├─ Phase 1: Setup Docker containers (base + milestone)
├─ Phase 2: Render prompts (using prepare_prompt.py + templates)
├─ Phase 3: Execute 3-stage SRS generation
│  ├─ Stage 1: Initial SRS (srs_gen_v1.tmpl)
│  ├─ Stage 2: Acceptance alignment (acceptance_test_align.tmpl)
│  └─ Stage 3: Environment deps (env_spec.tmpl)
└─ Phase 4: Cleanup containers

run_srs_code_gen.py
├─ Start Docker container from milestone image
├─ Generate code-gen prompt from SRS
├─ Execute Claude Code agent (with constraints)
├─ Extract patch + trace
└─ Cleanup container
```

## Output Structure

**SRS Generation** (`generate_srs.py`):
```
srs_workspace/{repo_name}/{milestone_version}/srs/{trial_name}/{milestone_id}/
├── SRS.md                          # Final SRS document
├── env_dependencies.md             # Environment comparison
├── prompt_srs_gen.md              # Stage 1 prompt
├── prompt_acceptance_align.md     # Stage 2 prompt
├── prompt_env_spec.md             # Stage 3 prompt
└── logs/                          # Execution logs
```

**Code Generation** (`run_srs_code_gen.py`):
```
{output_dir}/
├── agent_changes.patch            # Generated code diff
└── log/
    ├── pipeline.log              # Pipeline execution log
    ├── agent_prompt.txt          # Prompt sent to agent
    ├── agent_stdout.txt          # Agent stdout
    ├── agent_stderr.txt          # Agent stderr
    ├── agent_trace.jsonl         # Agent trace (claude-extract)
    └── session_id.txt            # Claude session ID
```

## Usage Examples

### Upload Testbed to GitHub

Upload a testbed repository with milestone tags to GitHub:

```bash
# Using environment variable
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx
python harness/gen_srs/auto_upload_testbed.py

# Or pass token as argument
python harness/gen_srs/auto_upload_testbed.py --token ghp_xxxxxxxxxxxx

# Custom repository name and organization
python harness/gen_srs/auto_upload_testbed.py \
  --org YourOrg \
  --repo-name custom-testbed-name \
  --testbed-path path/to/testbed

# Create as public repository (default is private)
python harness/gen_srs/auto_upload_testbed.py --no-private
```

The script will:
1. Create a GitHub repository in the specified organization
2. Configure Git remote with authentication
3. Push the main branch
4. Push all milestone tags
5. Disable GitHub Actions
6. Verify the upload and display repository URLs
