# DAG Dependency Validation with LLM-as-Judge

## Overview

This workflow validates the quality of milestone dependency DAG using LLM-based analysis. The goal is to identify **missing dependencies** and validate **extra dependencies** through code-level analysis.

**IMPORTANT - Dependency Direction Convention**:
**`source → target`** means **target depends on source**.

In other words, `source` must be completed before `target` can start. Target builds upon source's work.

**Core Principle**: `inferred_dependencies` (from commit DAG) are assumed correct. We validate:
1. **Extra dependencies**: Human-declared but not code-inferred → Should they be kept?
2. **Candidate missing dependencies**: Potentially missing edges → Should they be added?

---

## Input Data

### Required Files

1. **Milestone DAG Analysis**: `trial/evaluate_dependency/milestone_dag/final_result/dag_dependency_analysis.json`
   - `inferred_dependencies`: Code-inferred edges (ASSUMED CORRECT)
   - `extra_dependencies`: Declared but not inferred (need validation)
   - `metadata`: Paths to milestones.csv, dependencies.csv

2. **Pre-Selected Candidates**: `trial/evaluate_dependency/data/stage1_top20.json`
   - 20 candidate edges for validation
   - Each edge includes: milestones, commits (8-digit SHAs)

3. **Commit Data**: `trial/evaluate_dependency/data/main_commits_refined.jsonl`
   - Commit SHA (7 chars), message, author, timestamp, etc.

4. **Commit DAG**: `trial/evaluate_dependency/data/main_commit_dag_with_type.json`
   - `nodes`: Commit metadata (in/out degree, ancestors/descendants count)
   - `edges_dag`: Exact commit-level dependencies with edge types

5. **Git Repository**: `DATA/github_data/repos/urllib3_urllib3`
   - For code inspection at specific commits

---

## Overall Architecture

```
Stage 0: Data Preparation
    ↓
Stage 1: Load Pre-Selected Candidates
    → Load 20 candidates from stage1_top20.json
    ↓
Stage 2-3: Edge Validation (Unified Subagents)
    → Validate declared_extra edges (human-declared, code-unconfirmed)
    → Validate potential_new edges (20 pre-selected candidates)
    → All validated via code analysis in parallel
    ↓
Generate validation_report.json
```

---

## Stage 0: Data Preparation

**Your Task**: Load and structure data for analysis.

### Load Data
1. Read `dag_dependency_analysis.json`
2. **Read `stage1_top20.json`** (pre-selected 20 candidate edges)
3. Read `main_commits_refined.jsonl`
4. **Read `main_commit_dag_with_type.json`** (commit-level DAG with edge types)
5. Load milestones.csv and dependencies.csv (paths in metadata)
6. Build mappings:
   - `milestone_id → [commit_shas]`
   - `commit_sha → milestone_id`
   - `commit_sha → commit_data` (from jsonl)
   - **`sha_prefix_map`: 7-char SHA → 8-char SHA** (for milestone commits, if needed)
   - **`commit_dag_edges`: Dict mapping (source_sha, target_sha) → edge_info**

---

## Stage 1: Load Pre-Selected Candidates

**Goal**: Load the **20 candidate edges** for validation.

### Input

**Pre-selected candidates**: `trial/evaluate_dependency/data/stage1_top20.json`

This file contains 20 candidate edges, each with:
- Source and target milestones
- Source and target commits (8-digit SHAs)

### Implementation

```python
import json

# Load top-20 candidates
with open('trial/evaluate_dependency/data/stage1_top20.json') as f:
    candidates = json.load(f)

# Pass to Stage 2-3 for validation
```

**Note**: Simply load the candidates and pass them to Stage 2-3 for validation

---

## Stage 2-3: Edge Validation (Unified Subagents)

**Goal**: Validate edges through code-level analysis (all declared_extra + 20 potential_new)

**Reminder**: `source → target` means target depends on source

### Task Distribution

- Create subagents: Each validates 2-3 edges in parallel

### Subagent Instructions

**⚠️ CRITICAL**:
- **DO NOT generate summary documents, markdown files, or comprehensive reports**
- **DO NOT create additional documentation beyond the required JSON output**
- **ONLY output the structured JSON validation result** (see Output Format below)
- Focus solely on code analysis and decision-making
- **`source → target`** means **target depends on source**. In other words, `source` must be completed before `target` can start. Target builds upon source's work.

For each edge `source → target` conduct the following steps:

#### Step 1: Load Commit Data

Extract commits and patches for both source and target milestones from milestones.csv and main_commits_refined.jsonl

#### Step 2: Analyze Code Dependencies

**Analysis Method** (CODE ONLY):
- ONLY analyze actual code changes (not commit DAG or semantic reasoning)
- **Note**: Git ancestry ≠ code dependency

Check if target depends on source:
1. **Direct imports**: Target imports source's modules/functions
2. **Code modification**: Target modifies/extends source's code
3. **API usage**: Target uses source's APIs or fixes its bugs

#### Step 3: Git Repository Access

Use git commands to inspect code at specific commits:

```bash
# View file at commit
git -C DATA/github_data/repos/urllib3_urllib3 show <sha>:<filepath>

# Search for definitions
git -C DATA/github_data/repos/urllib3_urllib3 grep -n "def function_name" <sha>

# Compare between commits
git -C DATA/github_data/repos/urllib3_urllib3 diff <sha1> <sha2> -- <filepath>
```

#### Step 4: Make Decision

Classify the dependency based on code evidence:

**Dependency Strength Categories**:
- **Weak dependency (Weak)**: Downstream still builds/tests without upstream, but with degraded accuracy/robustness/experience/higher failure rate.
- **Strong dependency (Strong)**: Selected subset of Weak edges where upstream absence blocks build/test/pass.
- **INVALID**: No code dependency, only semantic relationship, or transitive

**When uncertain, prefer INVALID** (safer to have fewer dependencies).

#### Output Format

**IMPORTANT**: Return ONLY this JSON structure. Do NOT create markdown files, summary documents, or any additional outputs.

```json
{
  "subagent_id": "validation_subagent_1",
  "validated_edges": [
    {
      "source": "M006",
      "target": "M015",
      "edge_type": "declared_extra",
      "decision": "Strong" | "Weak" | "INVALID",
      "confidence": 0.9,
      "evidence": {
        "dependency_type": "import" | "modify" | "extend" | "bugfix" | "none",
        "code_examples": [
          "Specific code evidence with commit SHAs and file names"
        ]
      },
      "reasoning": "Brief code-level explanation (1-2 sentences)"
    }
  ]
}
```

**Remember**: Keep `reasoning` concise (1-2 sentences). Focus on facts, not verbose explanations.

---

## Final Report Generation

**Your Task**: Aggregate all validation results into a final summary report.

### Collect Results

1. Combine all validated edges from Stage 2-3
2. Separate results by edge type (`declared_extra` vs `potential_new`)
3. Summarize decisions

### Output Format

```json
{
  "metadata": {
    "analysis_time": "2025-10-26T01:00:00",
    "repo": "urllib3/urllib3",
    "validation_method": "llm_as_judge_code_analysis"
  },
  "stage1_candidate_ranking": {
    "total_candidates": 196,
    "selected_for_validation": 20
  },
  "edge_validation": {
    "declared_extra": {
      "strong": [],
      "weak": [],
      "invalid": ["M006→M015"]
    },
    "potential_new": {
      "strong": ["M006→M001"],
      "weak": ["M013→M006"],
      "invalid": ["M007→M002"]
    }
  },
  "summary": {
    "recommended_actions": [
      "REMOVE: M006→M015 (no code evidence)",
      "ADD (Strong): M006→M001 (blocks build)",
      "ADD (Weak): M013→M006 (degrades robustness)"
    ]
  }
}
```

Save to: `trial/evaluate_dependency/output4/validation_report.json`

---

## Key Principles

### 1. Code-First Analysis
- All decisions must be based on **actual code inspection**
- Use git commands to examine code at specific commits
- Provide concrete code examples as evidence

### 2. Conservative Validation
- When uncertain, prefer **INVALID** over Strong/Weak
- Strong dependencies require high confidence (>0.8) with clear blocking evidence
- Weak dependencies require moderate confidence (>0.6) with degradation evidence
- When evidence is ambiguous, mark as INVALID

### 3. Direct Dependencies Only
- Focus on **direct** dependencies
- Avoid adding transitive dependencies
- Check if dependency is already covered by existing paths

### 4. Concurrency Safety
- All git commands are read-only (safe for parallel execution)
- Each subagent works independently on 2-3 edges
- No file write conflicts (different output files)
- 8-10 subagents can run in parallel safely

---

## Success Criteria

Your validation is successful if:
1. ✅ All extra dependencies have clear Strong/Weak/INVALID decisions with code evidence
2. ✅ All 20 candidates have clear Strong/Weak/INVALID decisions with code evidence
3. ✅ Each decision is supported by specific code examples or git command outputs
4. ✅ Final report provides actionable recommendations
5. ✅ High confidence decisions (>0.8) are backed by strong code evidence
6. ✅ Subagents ran in parallel and produced consistent outputs
7. ✅ **All validated Strong/Weak edges are from the provided candidate set** (declared_extra + stage1_top20.json)

---

## Notes

- **Commit SHA format**: All commit SHAs in outputs must be **7 characters** (first 7 chars of full SHA)
- **Subagent coordination**: Each subagent validates 2-3 edges independently
- **Parallel execution**: All validation subagents run concurrently (8-10 subagents)
- **Unified validation**: Both edge types use the same validation logic
- **Git access**: All commands use `-C DATA/github_data/repos/urllib3_urllib3` for repo path
- **Commit data**: Use `main_commits_refined.jsonl` for patch information
- **Milestone data**: Load from paths in `dag_dependency_analysis.json` metadata
- **Edge types explained**:
  - `declared_extra`: Human-declared in dependencies.csv but not found in commit DAG
  - `potential_new`: Candidate edges identified by Stage 1 ranking
- **Decision mapping**:
  - For declared_extra: Strong/Weak = keep, INVALID = remove
  - For potential_new: Strong/Weak = add, INVALID = skip
- **Dependency strength**:
  - Strong: Upstream absence blocks build/test/pass
  - Weak: Upstream absence causes degraded accuracy/robustness/experience
