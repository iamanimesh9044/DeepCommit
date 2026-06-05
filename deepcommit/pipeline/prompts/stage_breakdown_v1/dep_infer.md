# DAG Dependency Validation with LLM-as-Judge

## Overview

This workflow infers the milestone dependency using LLM-based analysis. The goal is to identify valid **dependencies** through code-level analysis.

**IMPORTANT - Dependency Direction Convention**:
**`source → target`** means **target depends on source**.

In other words, `source` must be completed before `target` can start. Target builds upon source's work.

**Core Principle**: `inferred_dependencies` (from commit DAG) are assumed correct.

**This workflow performs two tasks**:
1. **Validate candidate dependencies**: Potentially missing edges → Should they be added? (Strong/Weak/INVALID)
2. **Classify inferred dependencies**: Already valid edges → What's their strength? (Strong/Weak)

---

## Input Data

### Required Files

1. **Milestone Assignments**: `{{milestone_csv}}`
    - Each milestone constructed by a set of non-overlapped **commits**

2. **Inferred Dependencies**: `{{inferred_dependencies}}`
   - Code-inferred edges (ASSUMED CORRECT)

4. **Pre-Selected Candidates**: `{{pre_selected_candidates}}`
   - Pre-ranked based on heuristics
   - Selectively read into context from top to down.

5. **Commit Data**: `{{main_commits}}`
   - Commit SHA (7 chars), message, author, timestamp, etc.

6. **Commit DAG**: `{{commit_dag}}`
   - `nodes`: Commit metadata (in/out degree, ancestors/descendants count)
   - `edges_dag`: Exact commit-level dependencies with edge types

7. **Git Repository**: `{{repo_path}}`
   - For code inspection at specific commits

### Optional Resources (for Semantic/Logical Dependencies)

8. **PR Metadata**: `{{pull_requests}}`
   - PR titles, descriptions, labels
   - Query selectively by PR number from commits

9. **Issue Context**: `{{issues}}`
   - Issue descriptions and discussion
   - Query if PR references specific issues

10. **PR Conversations**: `{{pr_conversations}}`
    - Full discussion threads (can be large)
    - Use sparingly, only when PR/Issue metadata insufficient

---

## Stage 1: Finalize Candidate Pool from Pre-selected Candidates

**Goal**: Select candidate edges for in-depth LLM validation.

### Input

**Pre-selected candidates**: `{{pre_selected_candidates}}`

### Instruction

- Recommend candidate pool size: 20
- The input is a large file. First read `metadata`, then read top `candidates[:N]` into context
- Perform a brief scan to finalize candidate pool 

---

## Stage 2: Edge Validation (Unified Subagents)

**Goal**: Dispatch subagents to validate edges through code-level and logical analysis in parallel

### Task Distribution

**Two types of validation tasks**:

1. **Candidate Edge Validation**: Validate if Strong/Weak/INVALID
   - Create subagents to handle candidate edges
   - Each subagent validates a subset of candidates

2. **Inferred Edge Classification**: Classify strength as Strong/Weak
   - Create separate subagents to handle inferred edges
   - Each subagent classifies a subset of inferred dependencies
   - Note: These edges are already valid (from commit DAG), only need strength classification

**Execution**: Run both types of subagents in parallel

**IMPORTANT - Parallel Execution**: You MUST launch all subagents in a single message with multiple Task tool calls to maximize performance. Do NOT launch subagents sequentially.

### Subagent Instructions

**CRITICAL**:
- **DO NOT generate summary documents, markdown files, or comprehensive reports**
- **DO NOT create additional documentation beyond the required JSON output**
- **ONLY output the structured JSON validation result** (see Output Format below)
- Focus solely on code analysis and decision-making

**Dependency Direction** - `source → target`:
- Means: **target depends on source** (target builds upon source's work)
- **Commit-level rule**: If target's commit C2 depends on source's commit C1, then **C1 must execute before C2**
- **Important**: Milestones can have time overlap, but commit dependencies must follow strict execution order

**Edge Type Handling**:

| Aspect | Candidate Edges | Inferred Edges |
|--------|----------------|----------------|
| **Validity** | Unknown (need to validate) | Already valid (from commit DAG) |
| **Decision Options** | Strong / Weak / INVALID | Strong / Weak (no INVALID) |
| **Step 3.5** | Optional (if code shows INVALID) | Optional (to determine strength) |
| **Stage 3 Verification** | Required (if code-based) | Not required (already validated) |

For each edge `source → target` conduct the following steps:

#### Step 1: Load Commit Data

Extract commits and patches for both source and target milestones from `{{milestone_csv}}` and `{{main_commits}}`

#### Step 2: Analyze Code Dependencies

**Analysis Method** (CODE ONLY):
- ONLY analyze actual code changes (not commit DAG or semantic reasoning)
- **Note**: Git ancestry ≠ code dependency

**Access Commit Patches**:
- **Option 1 (Recommended)**: Read preprocessed patches from `{{patches}}/<sha>.patch`
- **Option 2**: Use git commands (see Step 3) for deeper inspection if needed

**Two aspects to check**:

1. **Code Modification**: Does target modify source's code?
2. **Functional Usage** (recommended to check): Does target use source's functionality?
   - Imports, API calls, class inheritance, etc.

**Note**: Checking functional usage helps distinguish TEXT type (textual code only) from FUNC type (has functional coupling)

#### Step 3: Git Repository Access (Optional - For Deeper Inspection)

Use git commands when preprocessed patches are insufficient:

```bash
# View file at commit
git -C {{repo_path}} show <sha>:<filepath>

# Search for definitions
git -C {{repo_path}} grep -n "def function_name" <sha>

# Compare between commits
git -C {{repo_path}} diff <sha1> <sha2> -- <filepath>
```

**Note**: Most analyses can be completed using preprocessed patches from Step 2.

#### Step 3.5: Analyze Semantic & Logical Dependencies (Optional)

**Goal**: Capture important logical relationships beyond pure code dependencies.

**When to use**: After code analysis shows INVALID, but milestone categories (Breaking, Platform, Security) suggest potential logical relationships.

**What to search**:
1. **PR Metadata** (primary source)
   - Extract PR numbers from milestone commits (via `{{main_commits}}`)
   - Query `{{pull_requests}}` for PR titles, descriptions, labels
   - Look for: "depends on", "builds on", "requires", version progression mentions

2. **Issue Context** (if PR references issues)
   - Query `{{issues}}` for issue descriptions and key discussion
   - Focus on: architectural decisions, dependency mentions, deprecation plans

3. **PR Conversations** (last resort)
   - Query `{{pr_conversations}}` only when PR/Issue metadata insufficient
   - Extract: design decisions explicitly mentioning dependencies

**Constraints**:
- **DO NOT read entire files** - use selective queries (jq/grep by PR/issue number)
- **Query order**: commit → PR metadata → issue (optional) → conversation (rare)
- **Time limit**: Max 2-3 minutes per edge for semantic analysis

#### Step 4: Make Decision

Classify the dependency based on **code evidence** and **optional semantic/logical evidence**:

**Dependency Strength Categories**:
- **Strong**: Upstream absence blocks downstream execution
  - Code-level: Blocks build/test/pass, OR target must modify/extend source's code to implement its functionality
  - Logic-level: Logically blocks execution (e.g., cannot drop Python 3.8 before establishing Python 3.8 as minimum)
- **Weak**: Downstream can execute without upstream
  - Code-level: Target incidentally modifies source's code but doesn't depend on it (e.g., code style cleanup, version bumps)
  - Logic-level: Non-blocking logical relationships (e.g., quality improvements before features)
- **INVALID**: No actual code or logical dependency (only for candidate edges)

**Dependency Type Categories**:
- **FUNC**: Functional dependency (target uses source's functionality via imports, API calls, inheritance, etc.)
- **NFR**: Non-functional requirement dependency (version progression, security requirements, logical constraints)
- **TEXT**: Textual-only dependency (target modifies source's code but does NOT use source's functionality)

**Decision Logic**:
1. Determine type based on Step 2 analysis:
   - Has functional usage → **FUNC**
   - Only logical constraints (Step 3.5) → **NFR**
   - Only code modification, no functional usage → **TEXT**
2. Determine strength based on blocking severity:
   - Blocks execution → **Strong**
   - Non-blocking → **Weak**
3. **When uncertain, prefer INVALID** (safer to have fewer dependencies, only for candidates)

#### Output Format （`{{result_path}}/validation_subagent_i.json` for i-th subagent）

**IMPORTANT**: Return ONLY this JSON structure. Do NOT create markdown files, summary documents, or any additional outputs.

```json
{
  "subagent_id": "validation_subagent_1",
  "edge_type": "candidate" | "inferred",
  "validated_edges": [
    {
      "source": "M006",
      "target": "M010",
      "decision": "Strong" | "Weak" | "INVALID",
      "dependency_type": "FUNC" | "NFR" | "TEXT",
      "confidence": 0.95,
      "evidence": {
        "code_modification": [
          "Commit bd71bcb modifies util/ssl_.py from source ae06688"
        ],
        "functional_usage": [
          "Target imports typing.Literal from source (Python 3.8+)",
          "Target uses HTTPConnection.read1() API from source"
        ],
        "logical_reasoning": [
          "PR #3143: Establishes Python 3.8 as minimum version",
          "PR #3492: Requires 3.8 as minimum before dropping it"
        ]
      },
      "reasoning": "Strong FUNC: Target uses source's APIs. Blocks build without source.",
      "pr_references": [3143, 3492]
    }
  ]
}
```

**Remember**: Keep `reasoning` concise (1-2 sentences). Focus on facts, not verbose explanations.

#### Example Classifications

**Example 1: FUNC type (has functional usage)**
```
Source: HTTP/2 Foundation
Target: HTTP/2 Production

Analysis:
- Code modification: None
- Functional usage: Target imports HTTP2Connection API from source

Decision: Strong FUNC
→ Target uses source's APIs without modifying source code
```

**Example 2: TEXT type - Weak (incidental code modification)**
```
Source: Add HTTPResponse.read1() API
Target: Upgrade pre-commit tools (linting/formatting)

Analysis:
- Code modification: Target's tool upgrade reformats source's code (spacing, imports order)
- Functional usage: None (linting tools don't call read1() API)

Decision: Weak TEXT
→ Code style changes only, target doesn't depend on source's functionality
```

---

## Stage 3: Verification of Valid Dependencies (Main Agent)

**Goal**: Verify that code-based Strong/Weak dependencies identified by subagents are truly cross-milestone dependencies.

**Critical Check**: Prevent false positives where milestone-internal bug fixes are mistaken for cross-milestone dependencies.

**Note**: Only code-based dependencies (with code_examples) require verification. Logic-based dependencies (with logical_evidence only) do not need commit verification, regardless of Strong or Weak classification.

### Verification Steps

For **each Strong or Weak edge** (`source → target`) from Stage 2:

#### 1. Verify Evidence Commits

Check **all commits mentioned in `evidence.code_examples`**:

- **VALID**: Evidence commits belong to their claimed milestones
  - If citing source's commit → must be in `source_commits`
  - If citing target's commit → must be in `target_commits`

- **INVALID**: Evidence commits don't belong to claimed milestone
  - Example: "M013's commit d7bb83b fixes M006's bug" but d7bb83b ∈ M006
  - This indicates milestone-internal dependency, not cross-milestone

#### 2. Decision

- **Keep as Strong/Weak**: All evidence commits verified correctly
- **Reclassify as INVALID**: Any evidence commit belongs to wrong milestone

**Note**: Verification results will be integrated into Stage 4 final report. No separate output file needed.

---

## Stage 4: Final Report Generation

**Your Task**: Aggregate all validation and verification results into a final summary report.

### Collect Results

1. Combine all validated edges from Stage 2
2. Apply verification results from Stage 3 (reclassify edges if needed)
3. Summarize final decisions

### Output Format

#### 1. JSON Report (`{{result_path}}/validation_report.json`)

```json
{
  "metadata": {
    "analysis_time": "2025-11-02T00:00:00",
    "repo": "urllib3/urllib3",
    "total_candidates_analyzed": 20,
    "total_inferred_classified": 10,
    "total_dependencies": 11,
    "candidate_strong": 1,
    "inferred_strong": 10,
    "add_to_dag": ["M006→M010"]
  },
  "candidate_dependencies": {
    "strong": [
      {
        "edge": "M006→M010",
        "type": "NFR",
        "basis": "logical",
        "confidence": 0.95,
        "rationale": "Python version progression: 3.7→3.8 must precede 3.8→3.9"
      }
    ],
    "weak": []
  },
  "inferred_dependencies": {
    "strong": [
      {
        "edge": "M004→M001",
        "type": "FUNC",
        "confidence": 0.90,
        "rationale": "HTTP/2 implementation provides foundation for HTTP/2 support"
      }
    ],
    "weak": []
  },
  "verification_notes": {
    "code_based_verified": [],
    "reclassified_to_invalid": []
  }
}
```

#### 2. CSV Export (`{{dependencies_csv}}`)

**Columns**: `prerequisite_id`, `dependant_id`, `type` (FUNC/NFR/TEXT), `strength` (Strong/Weak), `rationale`, `confidence_score`

**Include**: All validated Strong/Weak dependencies (both candidate and inferred)

### Final DAG Validation

After generating `{{dependencies_csv}}`, run final validation to ensure the dependency graph is complete and correct:

**CRITICAL - Tool Usage**: You MUST use the MCP tool below. DO NOT write custom Python scripts as replacement.

**Tool**: `{{analyze_dag_conflicts}}(milestone_csv_path={{milestone_csv}}, dependencies_csv_path={{dependencies_csv}})` (switch params to absolute paths)

**Required Results & Fixes**:

| Check | Required Result | If Fails, Fix By |
|-------|----------------|------------------|
| **Missing dependencies** | `missing_dependencies = []` (all inferred dependencies included) | Add missing inferred edges to `{{dependencies_csv}}` with appropriate type/strength classification |
| **Cycles** | `enriched_cycles = []`, `existing_cycles = []` (DAG is acyclic) | Review cycle details, remove edges causing cycles |
| **Redundant edges** | `redundant_dependencies = []` (transitive reduction done) | Remove transitive dependencies already covered by existing paths |

---

## Key Principles

### 1. Code-First Analysis
- All decisions must be based on **actual code inspection** first
- Provide concrete code examples as evidence for code-based dependencies
- Logical evidence can elevate INVALID to Strong or Weak, depending on blocking severity

### 2. Conservative Validation
- When uncertain, prefer **INVALID** over Strong/Weak
- Strong dependencies require high confidence (>0.8) with clear blocking evidence:
  - Code-level: Blocks build/test/pass, OR
  - Logic-level: Logically blocks execution (e.g., version progression)
- Weak dependencies require either:
  - Moderate confidence (>0.6) with code degradation evidence, OR
  - Clear PR/Issue evidence of non-blocking logical relationships

### 3. Direct Dependencies Only
- Focus on **direct** dependencies
- Avoid adding transitive dependencies
- Check if dependency is already covered by existing paths

### 4. Concurrency Safety
- All git commands are read-only (safe for parallel execution)
- 5-10 subagents can run in parallel safely

---

## Success Criteria

Your validation is successful if:
1. All finalized candidates have clear Strong/Weak/INVALID decisions
2. Each Strong decision is supported by:
   - Code examples showing blocking dependencies, OR
   - PR/Issue evidence showing logically blocking relationships (e.g., version progression)
3. Each Weak decision is supported by:
   - Code examples showing degradation, OR
   - PR/Issue evidence showing non-blocking logical relationships
4. **All validated Strong/Weak edges are from the provided pre-selected candidate set**
5. **Stage 3 verification completed: All code-based Strong/Weak edges confirmed as cross-milestone dependencies** (not milestone-internal)
6. **Final DAG validation passed**: All inferred dependencies included, no cycles, no redundant edges (using `{{analyze_dag_conflicts}}`)
