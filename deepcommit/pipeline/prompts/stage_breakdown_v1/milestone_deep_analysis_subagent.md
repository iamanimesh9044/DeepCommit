# Deep Dependency Analysis for Complex Milestone

## Task

Analyze milestone **{milestone_id}** for splitting feasibility. This milestone has **{num_commits} commits** and **{src_loc} src_loc**, making it a dominant contributor to CV imbalance.

**Your goal**: Determine if and how this milestone can be split while maintaining DAG validity.

## Input

- **Milestone ID**: `{milestone_id}`
- **Commits**: `{commits}` (semicolon-separated)
- **Current src_loc**: `{src_loc}`
- **Mean src_loc**: `{mean_src_loc}`
- **Commit DAG path**: `{commit_dag_path}`
- **Milestone CSV**: `{milestone_csv}`
- **Dependencies CSV**: `{dependencies_csv}`

### Workspace `{{workspace_id}}`

This run covers a workspace of one or more repositories that are developed together:

{{workspace_repos_table}}

- Inspect code with `git -C repo_data/<repo> ...` (the `repo dir` column of the table; for a one-repo workspace that is `repo_data` itself). The MCP git tools route to the correct repository automatically.
- Commit ids are bare 7-character SHAs, unique inside the workspace. A commit's repository is its `repo` field in the commit records or its entry in `commit_repos` (`commit_repos.json`). Never guess it from the SHA and never run `git` against the wrong repository.
- With more than one repository, every file path in merged data is prefixed `<repo>/<path-in-repo>`; strip the prefix before passing a path to `git -C repo_data/<repo>`.
- Edges in `commit_dag.json` come in two groups. `edges_dag` holds per-repository textual edges plus HARD cross-repo edges (`"xrepo": true`, `edge_types` such as `xrepo_contract`, `xrepo_pin`, `xrepo_pr_link`). `edges_soft` holds heuristic hints (for example time proximity): never enforced, and you may cut them.

**Workspace rules**:
- `src_loc` values are per-repository `src_loc` (that repository's source prefixes) summed over the milestone; give per-group `src_loc` the same way and judge the CV on the sum.
- Group candidates must keep HARD cross-repo edges (`"xrepo": true`) inside one group; never split a contract change from its first consumer. Soft edges (`edges_soft`) are hints and may be cut.
- A group or milestone that spans repositories must change at least 2 repositories, and no single repository's part alone may be a complete feature; report the `repos` of every proposed group.
- Cycle checks use textual edges and hard xrepo edges only.

---

## Analysis Steps

### Step 1: Build Internal Dependency Graph

Use the commit DAG to map all dependencies WITHIN this milestone:

```
For each commit pair (A, B) in milestone:
  - Check if A is ancestor of B in commit DAG
  - Check if A and B modify overlapping files
  - Record: A -> B if dependency exists
```

**Output**: Internal dependency edges list

### Step 2: Classify Commits by Functional Theme

For each commit, determine its primary function:

1. Read commit message and changed files via `{get_commit_context}(sha, expand=['files', 'diff'])`
2. Classify into themes based on:
   - **Module/directory**: Which source directories are touched
   - **Feature type**: Infrastructure, API, UI, tests, refactoring, bugfix
   - **Semantic purpose**: What the commit accomplishes

**Output**: `commit_classifications` mapping each commit to theme(s)

### Step 3: Identify Candidate Groupings

Based on classifications, propose 2-4 grouping strategies:

**Strategy A**: Group by primary module/directory
**Strategy B**: Group by feature theme (PREFERRED - produces semantically coherent sub-milestones with clear functional descriptions like "Tags System Revamp" rather than generic "maint, fix, enh changes")
**Strategy C**: Group by code dependency relationships (topological ordering - commits that share imports, function calls, or data structures stay together)
**Strategy D**: Extract independent commits, keep coupled core

⚠️ **AVOID chronological grouping** (splitting by timestamp) unless all other strategies fail. Chronological splitting fragments semantically related commits across time slices, producing sub-milestones with vague descriptions.

For each strategy, define:
```json
{
  "strategy": "A",
  "groups": [
    {"name": "group_1", "commits": ["sha1", "sha2"], "src_loc": 5000, "repos": ["contract", "sdk"]},
    {"name": "group_2", "commits": ["sha3", "sha4"], "src_loc": 4500, "repos": ["sdk"]}
  ]
}
```

### Step 4: Validate Each Grouping for DAG Cycles

For each proposed grouping, simulate the split and check for cycles:

1. **Internal cycles**: Would sub-milestones have circular dependencies?
   - If group_1 commit depends on group_2 commit AND group_2 commit depends on group_1 commit → CYCLE

2. **External cycles (enriched)**: Would sub-milestones create cycles with OTHER milestones?
   - Use `{analyze_dag_conflicts}` with simulated milestone/dependency CSVs
   - Check `enriched_cycles` in result

**For each grouping**, record:
```json
{
  "strategy": "A",
  "internal_cycles": [],
  "external_cycles": ["M1_sub-01 -> M2 -> M1_sub-02 -> M1_sub-01"],
  "blocking_commits": ["sha5 in group_1 depends on sha6 in group_2"],
  "is_valid": false
}
```

### Step 5: Attempt Cycle Resolution

If a promising grouping has cycles, try to resolve:

1. **Identify boundary commits**: Commits that cause cross-group dependencies
2. **Evaluate moving boundary commits**:
   - What if commit X moves from group_1 to group_2?
   - Would this break the cycle without creating new ones?
3. **Consider merging small groups**: If two groups are tightly coupled, merge them

**Key insight**: Often 1-3 commits are the "linchpin" causing cycles. Moving them can unlock the split.

### Step 6: Generate Recommendation

Based on analysis, output ONE of:

**SPLIT_RECOMMENDED**:
```json
{
  "recommendation": "SPLIT_RECOMMENDED",
  "strategy": "A (modified)",
  "sub_milestones": [
    {"id": "{milestone_id}_sub-01", "commits": [...], "src_loc": 5000, "theme": "..."},
    {"id": "{milestone_id}_sub-02", "commits": [...], "src_loc": 4500, "theme": "..."}
  ],
  "internal_dependencies": ["{milestone_id}_sub-01 -> {milestone_id}_sub-02"],
  "confidence": 0.85,
  "notes": "Clean split by module boundaries"
}
```

**PARTIAL_SPLIT**:
```json
{
  "recommendation": "PARTIAL_SPLIT",
  "extractable_group": {"commits": [...], "src_loc": 3000, "theme": "..."},
  "remaining_core": {"commits": [...], "src_loc": 12000},
  "reason": "Core commits too coupled, but infrastructure can be extracted",
  "confidence": 0.7
}
```

**SPLIT_NOT_RECOMMENDED**:
```json
{
  "recommendation": "SPLIT_NOT_RECOMMENDED",
  "reason": "All 122 commits form a single tightly-coupled development chain",
  "blocking_analysis": {
    "primary_coupling": "Commits modify same 5 core files throughout",
    "cycle_sources": ["Every grouping creates enriched cycles via commit DAG"],
    "attempted_resolutions": ["Tried moving boundary commits - creates new cycles"]
  },
  "alternative_suggestion": "Consider merging with related small milestones instead"
}
```

## Output Format

Return a single JSON object with your complete analysis:

```json
{
  "milestone_id": "{milestone_id}",
  "analysis_summary": {
    "total_commits": 122,
    "total_src_loc": 17702,
    "internal_dependencies_count": 45,
    "functional_themes_identified": ["core", "api", "utils"]
  },
  "grouping_evaluations": [
    {"strategy": "A", "is_valid": false, "blocking_reason": "..."},
    {"strategy": "B", "is_valid": true, "groups": [...]}
  ],
  "recommendation": { ... },
  "confidence": 0.85
}
```

## Important Notes

- **Do NOT give up early**: Explore at least 3 grouping strategies before concluding "cannot split"
- **Be specific**: If cycles exist, name the exact commits and edges causing them
- **Consider partial wins**: Extracting even 20-30% of src_loc helps CV
- **Document attempts**: Show what you tried, even if it failed
