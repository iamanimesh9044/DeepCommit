# SRS Generation Skill - Agent Workflow

This skill enables Claude Code agents to autonomously generate Software Requirements Specification (SRS) documents for milestones through a multi-stage process.

## Overview

The SRS generation process consists of **automated setup/cleanup** stages and **manual agent-driven** content generation stages:

1. **Automated**: Container setup and prompt preparation
2. **Manual**: Agent executes 3-stage SRS generation process
3. **Automated**: Cleanup containers

## Quick Start

To generate an SRS for a milestone, provide this README to a Claude Code agent with the following parameters:

```
# Example
Generate SRS for:
- Repository: urllib3_urllib3_2.0.6_2.3.0
- Milestone Version: test_multi_stage_V2
- Milestone ID: M001
- Trial Name: v1
```

The agent will autonomously:
1. Run setup scripts to prepare containers and prompts
2. Launch subagents for each SRS generation stage
3. Run cleanup scripts

## Detailed Workflow

### Phase 1: Automated Setup

**Step 1.1: Container Setup**

Run the container setup script to create Docker environments for environment comparison:

```bash
python harness/gen_srs/gen_srs_skill/scripts/setup_containers.py \
  --repo-name urllib3_urllib3_2.0.6_2.3.0 \
  --milestone-version test_multi_stage_V2 \
  --milestone-id M001 \
  --base-image urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/base \
  --milestone-image urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/m001 \
  [--force]
```

**Outputs:**
- Base container: Running container from base environment
- Milestone container: Created container from milestone environment
- Validation: Verifies both images exist and containers are ready

**Step 1.2: Prompt Preparation**

Run the prompt preparation script to generate all required prompts:

```bash
python harness/gen_srs/gen_srs_skill/scripts/prepare_prompts.py \
  --repo-name urllib3_urllib3_2.0.6_2.3.0 \
  --milestone-version test_multi_stage_V2 \
  --milestone-id M001 \
  --trial-name v1 \
  --git-repo DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/testbed
```

**Outputs:** (saved to `srs_workspace/{repo_name}/{milestone_version}/srs/{trial_name}/{milestone_id}/`)
- `prompt_srs_gen.md` - Initial SRS generation prompt
- `prompt_acceptance_align.md` - Acceptance criteria refinement prompt
- `prompt_env_spec.md` - Environment dependencies generation prompt
- `state.json` - Initial workflow state

### Phase 2: Manual Agent-Driven SRS Generation

The agent should launch 3 sequential subagents using the Task tool, each working from the project root directory.

**Stage 1: Generate Initial SRS**

Launch a subagent to generate the initial SRS document:

```python
Task(
    subagent_type="general-purpose",
    description="Generate initial SRS",
    prompt=<content of prompt_srs_gen.md>
)
```

**Expected Output:**
- Creates `SRS.md` in the output directory
- Contains: Feature overview, implementation requirements, API changes, test scenarios

**Stage 2: Refine Acceptance Criteria**

Launch a subagent to align acceptance criteria with test data:

```python
Task(
    subagent_type="general-purpose",
    description="Refine acceptance criteria",
    prompt=<content of prompt_acceptance_align.md>
)
```

**Expected Output:**
- Updates `SRS.md` with refined acceptance criteria
- Aligns with test metadata from f2p_tests

**Stage 3: Generate Environment Dependencies**

Launch a subagent to generate environment specification:

```python
Task(
    subagent_type="general-purpose",
    description="Generate env dependencies",
    prompt=<content of prompt_env_spec.md>
)
```

**Expected Output:**
- Creates `env_dependencies.md`

**Post-Processing (Manual Step):**

After Stage 3 completes, append the environment dependencies to the SRS:

```bash
cat srs_workspace/{repo_name}/{milestone_version}/srs/{trial_name}/{milestone_id}/env_dependencies.md >> srs_workspace/{repo_name}/{milestone_version}/srs/{trial_name}/{milestone_id}/SRS.md
```

Or in Python:
```python
# Append env_dependencies.md to SRS.md
env_deps_file = output_dir / "env_dependencies.md"
srs_file = output_dir / "SRS.md"

if env_deps_file.exists():
    env_content = env_deps_file.read_text(encoding="utf-8")
    with open(srs_file, "a", encoding="utf-8") as f:
        f.write("\n\n---\n\n")
        f.write(env_content)
```

### Phase 3: Automated Cleanup

**Step 3.1: Cleanup Containers**

Run the cleanup script to remove temporary containers:

```bash
python harness/gen_srs/gen_srs_skill/scripts/cleanup.py \
  --milestone-id M001 \
  --milestone-image urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/m001
```

**Actions:**
- Removes milestone container (temporary)
- Keeps base container (reusable across milestones)

### Phase 4: Update Workflow State

After each stage completes, update the state file:

```python
import json
from pathlib import Path

state_file = Path("srs_workspace/.../state.json")
state = json.loads(state_file.read_text())

# Update after Stage 1
state["stages"]["stage1_srs_gen"]["status"] = "completed"
state["stages"]["stage1_srs_gen"]["completed_at"] = datetime.now().isoformat()

# Save
state_file.write_text(json.dumps(state, indent=2))
```

## Output Structure

```
srs_workspace/{repo_name}/{milestone_version}/srs/{trial_name}/{milestone_id}/
├── SRS.md                          # Final SRS document
├── env_dependencies.md             # Environment specification (intermediate)
├── prompt_srs_gen.md              # Stage 1 prompt (for reference)
├── prompt_acceptance_align.md     # Stage 2 prompt (for reference)
├── prompt_env_spec.md             # Stage 3 prompt (for reference)
├── state.json                      # Workflow execution state
└── logs/                           # Execution logs (if generated)
```

## Important Notes for Agents

1. **Sequential Execution**: Each stage must complete before starting the next
2. **Working Directory**: All subagents should execute from the project root
3. **Prompt Injection**: Read prompt files and pass their full content to subagents
4. **Error Handling**: Log errors to state.json but continue with next stages
5. **Containers**: Setup script handles all Docker operations automatically
6. **Paths**: All paths in prompts use relative paths from project root

## Auto-Derivation Rules

The agent can auto-derive missing parameters:

1. **Base Image**: `{repo_name}/{milestone_version_lower}/base`
2. **Milestone Image**: `{repo_name}/{milestone_version_lower}/{milestone_id_lower}`
3. **Git Repo**: `DATA/harness_workspace/{repo_name}/{milestone_version}/testbed`
4. **Output Dir**: `srs_workspace/{repo_name}/{milestone_version}/srs/{trial_name}/{milestone_id}`

## Example Agent Instructions

When asked to generate SRS for M001 of urllib3_urllib3_2.0.6_2.3.0:

1. Run `setup_containers.py` with auto-derived image names
2. Run `prepare_prompts.py` to generate all 3 prompts
3. Read `prompt_srs_gen.md` and launch Stage 1 subagent
4. Wait for completion, verify `SRS.md` exists
5. Read `prompt_acceptance_align.md` and launch Stage 2 subagent
6. Wait for completion, verify `SRS.md` updated
7. Read `prompt_env_spec.md` and launch Stage 3 subagent
8. Wait for completion, verify `env_dependencies.md` created
9. **Post-process**: Append `env_dependencies.md` to `SRS.md`
10. Run `cleanup.py` to remove containers
11. Report final output location

## Troubleshooting

- **Container already exists**: Use `--force` flag in setup script
- **Image not found**: Check that Dockerfile exists in `env_data/{milestone_id}/`
- **Prompt file missing**: Verify prepare_prompts.py completed successfully
- **SRS.md not created**: Check Stage 1 subagent logs and retry
- **Stage hangs**: Check container status with `docker ps -a`

## Related Files

- Original implementation: `harness/gen_srs/generate_srs.py`
- Prompt preparer: `harness/gen_srs/prepare_prompt.py`
- Templates: `harness/gen_srs/*.tmpl`
