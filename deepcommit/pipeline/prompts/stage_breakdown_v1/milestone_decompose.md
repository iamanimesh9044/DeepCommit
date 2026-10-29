# Milestone DAG Balancing

## Task Overview

Your task is to optimize the global milestone DAG by balancing milestone sizes through an iterative three-stage process. The goal is to achieve a coefficient of variation (CV) of src_loc across all milestones below 1.0.

**Core Strategy: Three-Stage Iterative Optimization**

- **Stage 1**: Global analysis and targeting (Main Agent)
- **Stage 2**: Parallel commit splitting (Subagents)
- **Stage 3**: Global reorganization and balancing (Main Agent)

**Target**: Reduce milestone `src_loc` CV; report **SUCCESS** if CV < 1.0, otherwise **WARNING** (but do not regress CV vs the input).

**Key Principles**:
- Global optimization over local perfection
- Iterative refinement with CV-guided convergence
- Intelligent LLM-based decisions (treat script thresholds as signals, not rules)
- Parallel processing where possible (Stage 2)
- Skip documentary milestones that do not contain any source code changes.

### Workspace `{{workspace_id}}`

This run covers a workspace of one or more repositories that are developed together:

{{workspace_repos_table}}

- Inspect code with `git -C repo_data/<repo> ...` (the `repo dir` column of the table; for a one-repo workspace that is `repo_data` itself). The MCP git tools route to the correct repository automatically.
- Commit ids are bare 7-character SHAs, unique inside the workspace. A commit's repository is its `repo` field in the commit records or its entry in `commit_repos` (`commit_repos.json`). Never guess it from the SHA and never run `git` against the wrong repository.
- With more than one repository, every file path in merged data is prefixed `<repo>/<path-in-repo>`; strip the prefix before passing a path to `git -C repo_data/<repo>`.
- Edges in `commit_dag.json` come in two groups. `edges_dag` holds per-repository textual edges plus HARD cross-repo edges (`"xrepo": true`, `edge_types` such as `xrepo_contract`, `xrepo_pin`, `xrepo_pr_link`). `edges_soft` holds heuristic hints (for example time proximity): never enforced, and you may cut them.

**Workspace rules**:
1. **src_loc per repository**: compute `src_loc` per repository with that repository's own source prefixes (the `src dirs` column above) and sum the per-repo values for the milestone `src_loc`. The CV is computed on the sum. The "do not regress CV vs the input" guard applies.
2. **Thresholds are per workspace**: the commit-count and src_loc thresholds below (50 commits / 5x mean / 30% of total; FORCE_SPLIT at 80 commits / 10,000 src_loc / 40% of total) are measured against the workspace totals, as signals rather than exact rules.
3. **Never split a contract change from its first consumer.** Chunk boundaries must keep HARD cross-repo edges (`"xrepo": true`) inside one sub-milestone.
4. **Cross-repo validity**: a milestone that lists 2 or more repositories in `repos` must still change at least 2 repositories after splitting, and no single repository's part may be a complete feature on its own. If a split would violate this, merge or re-cut instead.
5. **integration_test_commit**: one per repository (a SHA belongs to its own repository); never more than one per repository per milestone.
6. **`repos` column**: preserve it, and recompute it (semicolon-separated repository names) for every sub-milestone you create.
---

## Input Data

### 1. Milestones Collection

**File path**: `{{milestone_csv}}`

**CSV format**: Preserve the existing header and any extra columns. At minimum, ensure these columns exist and are populated:
- `id`, `title`, `category`, `commits`, `mini_srs`, `confidence_score`
- `src_loc` (computed and added by this stage via the analysis script; used for balancing)

**Key fields**:
- **id**: Milestone identifier (parent like `M003`, sub-milestone like `M003_sub-01`)
- **commits**: Semicolon-separated commit SHAs (short or full; keep the input style consistent)
- **mini_srs**: Short functional description (keep the input style consistent)
- **src_loc** (used for balancing): Total additions + deletions for files under each repository's source dirs (`REPO_SRC_DIRS`) only, summed over repositories (respecting `exclude_patterns`)
- **repos**: Semicolon-separated repository names of the milestone's commits
- **integration_test_commit** (optional, if present): Empty or one SHA per repository (semicolon-separated); never more than one per repository per milestone

### 2. Dependencies

**File path**: `{{dependencies_csv}}`

**CSV format**:
```csv
prerequisite_id,dependant_id,type,strength,rationale,confidence_score
M2,M1,FUNC,Strong,"M1 depends on M2's infrastructure",0.9
```

**Semantics**: 
- Direction: Edge `[prerequisite_id, dependant_id]` means `dependant_id` **depends on** `prerequisite_id`
- Execution order: source should complete before target starts (Weak = recommended, Strong = required)

**Dependency fields**:
- **type** (MUST be one of):
  - `FUNC`: Functional dependency - code/API dependency between milestones
  - `NFR`: Non-functional requirement - performance, security, reliability dependency
  - `TEXT`: Textual/documentation dependency
- **strength** (MUST be exactly `Strong` or `Weak` - NO other values allowed):
  - `Weak`: Downstream milestone can still build/test without upstream, but with degraded accuracy/robustness/experience or higher failure rate
  - `Strong`: Upstream absence blocks build/test/pass entirely

**⚠️ IMPORTANT**: The `strength` field accepts ONLY two values: `Strong` or `Weak`.

### 3. Original Repositories

**Repository root**: `{{repo_path}}` (each repository is checked out at `repo_data/<repo>`, see the table above)

The original Git repositories containing all commits and source code for analysis and commit splitting operations.

### 4. Main Branch Commits Metadata

**File path**: `{{main_commits}}`

JSONL file containing metadata for all commits on the main branch, including:
- Commit SHA, message, author, timestamp
- File changes and statistics
- Source code modifications

Used for commit analysis and understanding development history.

### 5. Available Tools

**Statistical analysis script**:
```bash
python {{identify_imbalances}} \
  --repo-path {{repo_path}} \
  --milestone-dir {{result_path}} \
  --repo-src-dirs {{repo_src_dirs}} \
  --exclude-patterns "{{exclude_patterns}}" \
  [--small]  # For undersized milestones
  [--k-value K]  # Std deviation multiplier
  [--output-json PATH]  # Optional JSON output for machine parsing
```

**Output**:
- Updates `src_loc` in the active `milestones.csv` (in-place)
- Prints global stats and candidates to stdout
- If `--output-json` is provided, writes `{"statistics": ..., "extreme_milestones": [...]}` for machine parsing

**MCP tools**:
- `{{get_commit_context}}(sha, expand=['files', 'stats', 'diff'])` - Get commit details
- `{{calculate_commits_src_loc_tool}}(commits)` - Calculate src_loc for commits (additions + deletions in REPO_SRC_DIRS path only)
- `{{validate_milestone_partitioning}}(milestone_csv, split_metadata_dir=None)` - Check milestone-level cycles; pass `split_metadata_dir` to include split commit dependencies
- `{{analyze_dag_conflicts}}(milestone_csv_path, dependencies_csv_path, split_metadata_dir=None)` - Check DAG validity; pass `split_metadata_dir` to include split commit dependencies

**Configuration**:
- `REPO_SRC_DIRS`: Source directory prefixes (e.g., "src/"; repo-prefixed when the workspace has several repositories) for src_loc calculation
- `MAX_ITERATIONS`: Maximum iteration cycles (default: 5)

---

## Stage 1: Global Analysis and Targeting

**Objective**: Identify which milestones and commits need splitting to improve global CV

### 1.1 Identify Oversized Milestones and Large Commits

**Call the statistical analysis script** `{{identify_imbalances}}` to obtain candidate oversized milestones and commits:

**Script output** (from stdout or `--output-json`):
- **Global statistics**: mean/std and milestone-level CV (`statistics.cv_milestones`)
- **Candidate milestones**: `extreme_milestones` (oversized if `--small` not set; undersized if `--small` set)
- **Commit-level details**: For each milestone, `commit_src_locs` mapping commit SHA → `src_loc`

**Check termination**:
- If CV < 1.0: Output "Already balanced" and exit
- If CV >= 1.0: Continue to intelligent filtering

**Step 1: LLM filter oversized milestones**

Review script output and select milestones to split:

**Selection criteria**:
- ⭐⭐⭐ **Size signal**: Significantly exceeds mean (not barely over threshold)
- ⭐⭐ **Functional complexity**: Multiple independent feature themes
- ⭐ **CV impact**: Splitting would meaningfully reduce variance

**Selection process**:
- Analyze each candidate milestone
- Prioritize clear functional boundaries
- Select top candidates (typically 3-5, not all candidates)

**Step 1.5: Deep Dependency Analysis (Required for Complex Milestones)**

**Trigger condition** - Launch subagent analysis for ANY milestone with:
- \> 50 commits, OR
- \> 5x mean src_loc, OR
- \> 30% of total workspace src_loc

**MANDATORY**: Do NOT skip this step for complex milestones. The subagent provides critical dependency analysis that determines splitting feasibility.

**Subagent prompt**: `{{deep_analysis_subagent_prompt}}`

**Subagent parameters**:
- `milestone_id`: Target milestone ID
- `commits`: Semicolon-separated commit list
- `src_loc`: Milestone's total src_loc
- `mean_src_loc`: Global mean from statistics
- `commit_dag_path`: `{{commit_dag}}`
- `milestone_csv`: `{{milestone_csv}}`
- `dependencies_csv`: `{{dependencies_csv}}`
- `get_commit_context`: `{{get_commit_context}}`
- `analyze_dag_conflicts`: `{{analyze_dag_conflicts}}`

**Subagent output**: JSON with recommendation:
- `SPLIT_RECOMMENDED`: Proceed with suggested grouping strategy
- `PARTIAL_SPLIT`: Extract what's possible, keep coupled core together
- `SPLIT_NOT_RECOMMENDED`: With specific blocking analysis (not just "cycles exist")

**Key principle**: Do NOT conclude "cannot split" without:
1. Identifying SPECIFIC commits causing cycles
2. Evaluating if moving 1-3 boundary commits would break cycles
3. Considering PARTIAL splits (extract what you can)

**Use subagent result** to inform Step 2 decisions:
- If `SPLIT_RECOMMENDED`: Use suggested grouping for commit splitting
- If `PARTIAL_SPLIT`: Only process extractable commits in Stage 2
- If `SPLIT_NOT_RECOMMENDED`: Check FORCE_SPLIT thresholds below

**⚠️ FORCE_SPLIT (Mandatory for Extreme Milestones)**:

If milestone exceeds ANY threshold: **>80 commits OR >10,000 src_loc OR >40% total src_loc** → splitting is MANDATORY regardless of subagent recommendation.

**Fallback strategies** when functional splitting fails (try in order):
1. **Topological/Dependency-based**: Group commits by code dependency relationships (imports, function calls, shared data structures). This preserves semantic coherence - related commits stay together.
2. **Directory clustering**: Group by primary module/directory touched
3. **Hybrid topological + size**: Topological grouping with size-balancing adjustments
4. **Chronological** (LAST RESORT): Split into ~25-commit chunks by timestamp. ⚠️ WARNING: This often fragments semantically related commits across time slices, producing sub-milestones with generic descriptions like "maint, fix, enh changes" instead of clear functional themes. Only use when other strategies fail.

Cycles from FORCE_SPLIT must be resolved by adjusting chunk boundaries.

**Step 2: LLM identify large commits within selected milestones**

For each selected milestone, analyze commits using:
- Script's commit_src_locs data
- Commit details from `{{get_commit_context}}` (message, diff, files)

**Split criteria**:
- ⭐⭐⭐ **Functional independence**: Multiple independent features/modules
- ⭐⭐ **Size anomaly**: Significantly larger than milestone's other commits
- ⭐⭐ **Split feasibility**: Clear boundaries (module/feature/file groups)
- ⭐ **Squash merge signal**: Originally comessed from multiple commits

**Do NOT split**:
- Small single-function commits
- Tightly coupled refactoring
- Atomic bug fixes
- Binary/auto-generated files

**Determine `num_commits`**:
- Based on src_loc and functional complexity
- Typical: 3-5 feature commits + optional 1 test commit
- Aim for balanced splits

**Final output**: `large_commits_to_split` list:
```
{
  "milestone_id": "M2",
  "commit_sha": "1812eac",
  "src_loc": 1601,
  "num_commits": 4,
  "split_reason": "Squash merge with multiple independent modules",
  "functional_themes": ["infrastructure", "connection", "fetch"]
}
```

---

## Stage 2: Parallel Commit Splitting

**Objective**: Split identified large commits into smaller logical commits using Subagents

### 2.1 Invoke Subagents in Parallel

**Subagent prompt**: `{{subagent_prompt_file}}`

**Batch processing** (max 5 subagents concurrently):

Process `large_commits_to_split` in batches of up to 5 commits:
- For each batch, launch subagents in parallel
- Each subagent receives a single commit to split

**Subagent parameters** (dynamic, to be filled by Main Agent):
- `target_commit`: Commit SHA to split
- `num_commits`: Target number of feature commits (from Stage 1.2)
- `working_branch`: "split-{commit_sha[:7]}"

**Note**: The following are pre-injected into subagent prompt (no need to pass):
- `repo_path`, `repo_src_dirs`, `exclude_patterns`, `base_image_name`, `build_command`

**Note**: Output path and metadata script are hardcoded in the subagent prompt:
- Output: `WORKSPACE_ROOT/stage4_decomp/output/commit_splits/{working_branch}/`
- Script: `WORKSPACE_ROOT/deepcommit/tools/milestone_balancing/generate_split_metadata.py`

**Execution requirements**:
- Wait for all subagents in a batch to complete before processing next batch
- Validate subagent output integrity (METADATA.json exists and valid)
- If a subagent fails, mark the commit as "split failed, keep original"

### 2.2 Collect and Parse Subagent Output

**For each successfully split commit**, collect:

**Output directory structure**:
```
{output_base_path}/split-{commit_sha}/
├── METADATA.json
└── patches/
    ├── {split_sha_1}.patch
    ├── {split_sha_2}.patch
    └── ...
```

**Parse METADATA.json** to extract:

⚠️ **IMPORTANT**: METADATA.json MUST use `split_result.src_commits` format, NOT top-level `commits` or `split_commits`.

```json
{
  "original_commit": {
    "sha": "1812eac36019be19cb02f181aa21d133ba9fe219",
    "short_sha": "1812eac",
    "message": "Original commit message"
  },
  "split_result": {
    "src_commits": [
      {
        "sha": "abc123d...",
        "short_sha": "abc123d",
        "message": "feat: ...",
        "src_stats": {"total_loc": 401},
        "patch_file": "patches/abc123d.patch",
        "files": ["src/module/file1.py"]
      },
      {
        "sha": "def456e...",
        "short_sha": "def456e",
        "message": "feat: ...",
        "src_stats": {"total_loc": 389},
        "patch_file": "patches/def456e.patch",
        "files": ["src/module/file2.py"]
      }
    ],
    "integration_test_commit": {
      "short_sha": "test123",
      "message": "test: ...",
      "src_stats": {"total_loc": 0},
      "patch_file": "patches/test123.patch"
    },
    "dependency_edges": []
  }
}
```

**Field explanations**:
- Use `sha` for full hash, `short_sha` for 7-char hash (consistent naming)
- `src_commits` MUST be inside `split_result`, never at top level
- `src_loc` is `src_stats.total_loc` (additions + deletions in `REPO_SRC_DIRS` only)
- `integration_test_commit`: May be null; at most ONE per original commit (a commit belongs to one repository)
- `dependency_edges`: Array of `[source, target]` pairs where target depends on source. May be empty if commits are independent.

**Build split commits mapping**:
```
split_commits_map = {
  "1812eac": {
    "src_commits": ["abc123d", "def456e"],
    "integration_test_commit": "test123",  // or null
    "dependencies": [...]  // from dependency_edges, may be empty
  },
  ...
}
```

**Verification**:
- Confirm all splits' total src_loc ≈ original commit's src_loc
- Validate patch files exist for all split commits
- If dependency_edges is non-empty, ensure edges form a valid DAG (no cycles within split commits)

**Output**: Complete `split_commits_map` for all successfully split commits

---

## Stage 3: Global Reorganization and Balancing

**Objective**: Split oversized milestones, merge undersized milestones, and achieve CV < 1.0

### 3.1 Split Oversized Milestones

**⚠️ MANDATORY SPLIT PRINCIPLE**: Every oversized milestone MUST be split into at least 2 sub-milestones (if find necessary). Do NOT give up easily.

**Iterative Approach**: If one grouping strategy causes cycles, try alternative approaches:
- If cycles detected, reorder commits between groups to break cycles rather than abandoning the split
- Continue iterating until a valid split is found

**Process each oversized milestone sequentially**:

#### Step 1: Prepare Updated Commit List

For milestone M1 with commits [A (split), B (kept), C (split)]:
- Replace split commits with their split results from `split_commits_map`
- Keep unsplit commits as-is
- Result: new_commits = [A.1, A.2, A.3, B, C.1, C.2]

#### Step 2: Group Commits by Functional Coherence

**⚠️ BALANCE CV AND SEMANTIC COHERENCE**: Both metrics matter equally. Each sub-milestone should represent a clear functional theme (e.g., "Tags System Revamp", "Testing Framework Modernization") rather than a generic time-based slice (e.g., "maint, fix, enh changes May-Jun"). Aim for CV < 1.0 while maintaining meaningful functional groupings—avoid sacrificing one for the other.

Group commits based on (priority order):
1. **Functional relevance**: Related features belong together (highest priority)
2. **Code dependencies**: Commits with import/call relationships stay together
3. **Module structure**: Group commits touching same modules/files
4. **Size balance**: Aim for balanced groups (lowest priority - don't sacrifice coherence for balance)

**Example**:
```
M1 commits: [A.1, A.2, A.3, B, C.1, C.2]
Dependencies (from METADATA.json): A.1 → A.3, C.1 → C.2  // may be partial or empty

Groups:
- Group 1: [A.1, A.2] - "Infrastructure foundation"
- Group 2: [A.3, C.1] - "Connection layer"
- Group 3: [B, C.2] - "Fetch layer and utilities"
```

#### Step 3: Generate Sub-Milestones and Update milestones.csv

**For each group**, create a sub-milestone:
- **ID**: `{parent_id}_sub-{seq:02d}` (e.g., M1_sub-01, M1_sub-02, M1_sub-03). **No nested splits allowed** - sub-milestones cannot be split further.
- **Fields**: Generate `title`, `mini_srs`; preserve other columns

**⚠️ CRITICAL: integration_test_commit Column Handling**:

When a commit was split and METADATA.json contains `integration_test_commit`:

1. **Read** the `integration_test_commit.short_sha` from METADATA.json (e.g., `4b2505f`)
2. **Write** this SHA to the `integration_test_commit` column of the **FIRST sub-milestone ONLY** (e.g., M1_sub-01)
3. **Do NOT** include this SHA in the `commits` column of any milestone
4. **Leave empty** the `integration_test_commit` column for all other sub-milestones (M1_sub-02, M1_sub-03, ...)

**Example**:
```
METADATA.json: {"split_result": {"integration_test_commit": {"short_sha": "4b2505f"}}}

milestones.csv:
| id    | commits           | integration_test_commit |
|-------|-------------------|-------------------------|
| M1_sub-01 | 5d488;0e2dc   | 4b2505f                 |  ← FIRST sub-milestone gets the SHA
| M1_sub-02 | 24901;6f503   |                         |  ← Empty
| M1_sub-03 | a5eb7         |                         |  ← Empty
```

**Common mistakes to avoid**:
- ❌ Putting integration_test_commit SHA in `commits` column
- ❌ Assigning integration_test_commit to multiple sub-milestones
- ❌ Forgetting to populate the `integration_test_commit` column entirely

**Write to milestones.csv**: Insert sub-milestones, delete parent M1

**Validate and iterate**:
```
{{validate_milestone_partitioning}}({{milestone_csv}}, split_metadata_dir={{commit_splits}})
```

Key outputs: `inferred_dependencies`, `cycles`, `cycle_edge_details`

**If cycles detected**:
- Use `cycle_edge_details` to identify which commits cause the cycle
- Reorder commits: move boundary commits between groups to break cycles
- Try different grouping strategies if reordering doesn't work
- Re-validate after each adjustment

**Output**: Valid sub-milestones + `inferred_dependencies` for Step 4

#### Step 4: Update Dependencies

**Internal dependencies**: Add edges from `inferred_dependencies` between sub-milestones (default strength: Strong)

**External dependencies**: Map original edges involving M1 to sub-milestones:
- Incoming (X → M1): Map to M1_sub-01 (first)
- Outgoing (M1 → Y): Map to M1_sub-{n} (last)

**Write to dependencies.csv**:
- PRESERVE edges not involving M1
- REMOVE edges with M1
- ADD internal + mapped external edges

**Validate and iterate**:
```
{{analyze_dag_conflicts}}({{milestone_csv}}, {{dependencies_csv}}, split_metadata_dir={{commit_splits}})
```

**If cycles detected**:
- For `existing_cycles`: remove or weaken the problematic edges
- For `enriched_cycles`: adjust commit assignments between sub-milestones
- Continue iterating until cycles are resolved

**If current grouping strategy fails to produce a valid DAG**: Do NOT give up. Switch to a different grouping strategy (topological/dependency-based, directory-based, or size-based) and retry. Chronological splitting should only be used as a last resort due to poor semantic coherence. Only after exhausting all reasonable strategies should you consider keeping parent unsplit, and this must be documented with detailed justification.

### **3.2 Merge Undersized Milestones**

**After completing all splits**, identify and merge small milestones.

#### Step 1: Identify Undersized Milestones

Run `{{identify_imbalances}}` with `--small --k-value 0.5` to get candidates (src_loc < mean - 0.5*std)

#### Step 2: Evaluate and Execute Merge

**For each undersized milestone U**, find merge candidates from neighboring milestones (predecessors/successors in dependency graph).

**Merge criteria**:
- Functional relevance: Similar category or related functionality
- Size compatibility: merged_src_loc < mean + std
- No cycles introduced

**If merging U into N**:
- Combine commits (maintain temporal order)
- Update `title`, `mini_srs` to reflect merged content
- Merge other fields as appropriate (union for lists, min/max for timestamps)

**If no good candidate found**: Keep U as-is

#### Step 3: Update CSV Files

**milestones.csv**: Modify N's row with merged data, delete U's row

**dependencies.csv**:
- Remove edges between N and U
- Redirect edges referencing U to N
- Deduplicate (prefer Strong over Weak)

Validate with `{{analyze_dag_conflicts}}` to ensure no cycles.

### 3.3 Validate CV and Decide Iteration

**After completing all splits and merges**:

#### Step 1: Final DAG Validation

Run both validations and ensure they pass before proceeding:

```
{{validate_milestone_partitioning}}({{milestone_csv}}, split_metadata_dir={{commit_splits}})
{{analyze_dag_conflicts}}({{milestone_csv}}, {{dependencies_csv}}, split_metadata_dir={{commit_splits}})
```

- Required: `cycles_count = 0`, `existing_cycles = []`, `enriched_cycles = []`
- If fails: Fix issues before continuing; do not proceed with invalid DAG

#### Step 2: Recalculate Global CV

- Read updated milestones.csv
- Calculate new CV = std(src_loc) / mean(src_loc)

#### Step 3: Check Termination Conditions

**Evaluate in order**:

1. **Target achieved**: CV < 1.0 → Output "SUCCESS", exit

2. **Maximum iterations reached**: iteration_count >= MAX_ITERATIONS → Output "WARNING", exit

3. **No improvement**: new_cv >= old_cv → Output "WARNING", exit

4. **Continue iterating**: CV improved but not < 1.0 → Return to Stage 1

#### Step 4: Iteration State Management

**Track across iterations**:
- Iteration count
- CV history: [initial_cv, cv_after_iter1, cv_after_iter2, ...]
- Milestones split/merged per iteration
- Best CV achieved (for potential rollback): If an iteration regresses CV or creates invalid output, rollback to the best-known state before exiting

---

## Output Format

### Updated CSV Files

**milestones.csv**: Update in-place (add sub-milestones, remove split parents, modify merged rows)

**dependencies.csv**: Update in-place

### Optimization Report

**File**: `{{balancing_report}}`

**Structure**:
```json
{
  "task": "milestone_dag_balancing",
  "status": "SUCCESS" | "WARNING",
  "initial_cv": 1.45,
  "final_cv": 0.87,
  "target_cv": 1.0,
  "total_iterations": 3,
  "iteration_history": [
    {
      "iteration": 1,
      "cv_before": 1.45,
      "cv_after": 1.12,
      "milestones_split": ["M2", "M5"],
      "milestones_merged": [],
      "commits_split": ["1812eac", "abc123d"]
    },
    ...
  ],
  "final_statistics": {
    "total_milestones": 45,
    "mean_src_loc": 423.5,
    "std_src_loc": 368.2,
    "cv": 0.87
  },
  "warnings": [],
  "notes": "Successfully balanced milestone DAG in 3 iterations"
}
```

---

## Important Notes

- **PRIORITIZE SPLITTING over MERGING**: The primary goal is to split oversized milestones into smaller sub-milestones. Merging undersized milestones is OPTIONAL and secondary. Focus your effort on splitting large milestones first.
- **PERSIST UNTIL SUCCESS**: If a split attempt fails (due to cycles or other issues), do NOT give up. Try different grouping strategies (functional → topological/dependency-based → directory-based → size-based → chronological as last resort), reorder commits between groups, or adjust boundaries. Keep iterating until you find a valid split. Only mark as failed after exhausting ALL reasonable approaches.
- **DAG Integrity**: Validate acyclic after each modification; abort operation if cycle detected
- **Balance Both Goals**: Achieve CV < 1.0 while maintaining semantic coherence—both are important targets, not just one or the other
- **Test Commits**: At most ONE `integration_test_commit` per repository per milestone. When splitting: (1) write SHA to `integration_test_commit` column of FIRST sub-milestone only, (2) do NOT put this SHA in any `commits` column, (3) leave other sub-milestones' `integration_test_commit` empty
- **External Dependency Mapping**: Do NOT blindly inherit to all entry/exit; analyze patches to find true relationships (typically 1-2 sub-milestones per edge)
- **FORCE_SPLIT is MANDATORY**: Milestones exceeding force thresholds (>80 commits, >10K src_loc, >40% total) MUST be decomposed using fallback strategies if needed.
- **Dependency Preservation**: When updating dependencies.csv, only modify edges directly involving the milestone(s) being split or merged. All other existing dependency edges MUST be preserved exactly as-is. This is an incremental update, NOT a reconstruction—the output should contain all original edges minus those explicitly replaced/removed, plus any new edges added.
