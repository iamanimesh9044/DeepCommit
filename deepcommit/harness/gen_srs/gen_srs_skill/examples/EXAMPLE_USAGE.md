# Example: Generate SRS for urllib3 M001

This example demonstrates how to use the SRS Generation Skill to generate an SRS document for urllib3's M001 milestone.

## Prerequisites

- Docker installed and running
- Claude Code CLI configured
- Project workspace set up with env_data and test_data for M001

## Step-by-Step Guide

### Step 1: Setup Containers

Run the container setup script:

```bash
python harness/gen_srs/gen_srs_skill/scripts/setup_containers.py \
  --repo-name urllib3_urllib3_2.0.6_2.3.0 \
  --milestone-version test_multi_stage_V2 \
  --milestone-id M001 \
  --force
```

**Expected Output:**
```
Auto-derived base-image: urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/base
Auto-derived milestone-image: urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/m001
Auto-inferred git-repo: DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/testbed
======================================================================
Container Setup for SRS Generation
======================================================================
✓ Base container: urllib3_urllib3_2.0.6_2.3.0-test_multi_stage_v2-base-container (running)
✓ Milestone container: urllib3_urllib3_2.0.6_2.3.0-test_multi_stage_v2-m001-container (created)
```

### Step 2: Prepare Prompts

Run the prompt preparation script:

```bash
python harness/gen_srs/gen_srs_skill/scripts/prepare_prompts.py \
  --repo-name urllib3_urllib3_2.0.6_2.3.0 \
  --milestone-version test_multi_stage_V2 \
  --milestone-id M001 \
  --trial-name v1
```

**Expected Output:**
```
Auto-inferred git-repo: DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/testbed
======================================================================
Prompt Preparation for SRS Generation
======================================================================
✓ prompt_srs_gen.md (Stage 1)
✓ prompt_acceptance_align.md (Stage 2)
✓ prompt_env_spec.md (Stage 3)
✓ state.json (workflow state)
```

**Files Created:**
- `srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/prompt_srs_gen.md`
- `srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/prompt_acceptance_align.md`
- `srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/prompt_env_spec.md`
- `srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/state.json`

### Step 3: Agent-Driven SRS Generation

Now you can either:

#### Option A: Manual Execution (Recommended for learning/debugging)

Read and execute each prompt manually to understand the process:

```bash
# Stage 1: Generate initial SRS
cat srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/prompt_srs_gen.md

# (Review the prompt, then provide it to an agent)
# The agent will create SRS.md

# Stage 2: Refine acceptance criteria
cat srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/prompt_acceptance_align.md

# (Provide to agent)
# The agent will update SRS.md

# Stage 3: Generate environment dependencies
cat srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/prompt_env_spec.md

# (Provide to agent)
# The agent will create env_dependencies.md and append to SRS.md
```

#### Option B: Agent Orchestration (Recommended for production)

Provide the main README to a Claude Code agent:

```
I need to generate SRS for:
- Repository: urllib3_urllib3_2.0.6_2.3.0
- Milestone Version: test_multi_stage_V2
- Milestone ID: M001
- Trial Name: v1

Please follow the workflow in harness/gen_srs/gen_srs_skill/README.md
```

The agent will:
1. Already have containers set up (from Step 1)
2. Already have prompts prepared (from Step 2)
3. Read each prompt file
4. Launch subagents for each stage
5. Verify outputs after each stage
6. Run cleanup script when done

### Step 4: Cleanup

Run the cleanup script:

```bash
python harness/gen_srs/gen_srs_skill/scripts/cleanup.py \
  --repo-name urllib3_urllib3_2.0.6_2.3.0 \
  --milestone-version test_multi_stage_V2 \
  --milestone-id M001
```

**Expected Output:**
```
Auto-derived milestone-image: urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2/m001
======================================================================
Container Cleanup for SRS Generation
======================================================================
✓ Milestone container removed
Base container kept for reuse across milestones
```

### Step 5: Verify Output

Check the generated SRS:

```bash
ls -lh srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/

# Should show:
# - SRS.md (final output)
# - env_dependencies.md (intermediate)
# - prompt_srs_gen.md (Stage 1 prompt)
# - prompt_acceptance_align.md (Stage 2 prompt)
# - prompt_env_spec.md (Stage 3 prompt)
# - state.json (workflow state)
```

Review the final SRS:

```bash
cat srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/srs/v1/M001/SRS.md
```

## Agent Prompt Template

If you want an agent to run the entire workflow, use this prompt:

```
Generate SRS for urllib3 M001:

Repository: urllib3_urllib3_2.0.6_2.3.0
Milestone Version: test_multi_stage_V2
Milestone ID: M001
Trial Name: v1

Please follow these steps:

1. Run setup_containers.py with --force flag
2. Run prepare_prompts.py
3. For each stage (1, 2, 3):
   - Read the corresponding prompt_*.md file
   - Launch a Task subagent with that prompt
   - Wait for completion
   - Verify expected output exists
4. Run cleanup.py
5. Report the location of final SRS.md

Use the workflow documented in harness/gen_srs/gen_srs_skill/README.md
```

## Troubleshooting

### Container Already Exists

If you see "Container already exists", use the `--force` flag:

```bash
python harness/gen_srs/gen_srs_skill/scripts/setup_containers.py \
  --repo-name urllib3_urllib3_2.0.6_2.3.0 \
  --milestone-version test_multi_stage_V2 \
  --milestone-id M001 \
  --force
```

### Image Not Found

Ensure the Dockerfile exists:

```bash
ls srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/env_data/Base/Dockerfile
ls srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/env_data/M001/Dockerfile
```

### Prompt Not Generated

Check that the work directory structure exists:

```bash
ls srs_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_V2/
# Should show: env_data/, test_data/
```

### SRS.md Not Created

After Stage 1 execution:
1. Check if the agent received the correct prompt
2. Verify the agent is working from the project root directory
3. Check logs for any errors
4. Retry the stage manually

## Next Steps

- Compare with reference SRS (if available)
- Validate acceptance criteria against test data
- Review environment dependencies
- Use generated SRS for code generation tasks
