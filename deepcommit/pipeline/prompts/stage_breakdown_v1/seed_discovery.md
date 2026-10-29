# Milestone Seed Discovery - LLM-as-Judge with DAG Statistics

## Task Overview

Analyze commits for the workspace repositories {{repo_name}} (version {{version_range}}) and identify **milestone seeds** - commits that represent the starting points of major development milestones.

You are an LLM judge with access to rich commit data including DAG topology statistics. Use your semantic understanding combined with structural metrics to identify milestone seeds (target: about 20 per repository, soft). Ultrathink

### Workspace `{{workspace_id}}`

This run covers a workspace of one or more repositories that are developed together:

{{workspace_repos_table}}

- Inspect code with `git -C repo_data/<repo> ...` (the `repo dir` column of the table; for a one-repo workspace that is `repo_data` itself). The MCP git tools route to the correct repository automatically.
- Commit ids are bare 7-character SHAs, unique inside the workspace. A commit's repository is its `repo` field in the commit records or its entry in `commit_repos` (`commit_repos.json`). Never guess it from the SHA and never run `git` against the wrong repository.
- With more than one repository, every file path in merged data is prefixed `<repo>/<path-in-repo>`; strip the prefix before passing a path to `git -C repo_data/<repo>`.
- Edges in `commit_dag.json` come in two groups. `edges_dag` holds per-repository textual edges plus HARD cross-repo edges (`"xrepo": true`, `edge_types` such as `xrepo_contract`, `xrepo_pin`, `xrepo_pr_link`). `edges_soft` holds heuristic hints (for example time proximity): never enforced, and you may cut them.

**Seed rules**:
- The seed pool spans all repositories. Soft target: about 20 seeds **per repository**, not 20 in total.
- A contract change (for example a `.proto` or OpenAPI change) and its first consumer in another repository are normally one feature. Use the HARD `xrepo` edges in `edges_dag` to spot such pairs, and choose seeds so that both can end up in the same milestone.
- Do not treat an `edges_soft` link as evidence that two seeds belong together.
- Every seed line carries a `repo` field (the repository of the seed commit).

---
---

## Critical Data Sources

### 1. Main Commits with textual commit dependency DAG Statistics (ENTRY POINT - READ THIS COMPLETELY!)

**File**: `{{main_commits}}`

This is your PRIMARY data source. Each line contains a commit with complete information:

```json
{
  "sha": "1812eac7",
  "message": "Add native Emscripten support",
  "additions": 1100,
  "deletions": 2,
  "files_count": 9,
  "dag": {
    "layer": 0,
    "in_degree": 0,
    "out_degree": 7,
    "ancestors_count": 0,
    "descendants_count": 16
  }
}
```

**Field Explanations**:

- **sha**: Short commit hash for identification
- **message**: Commit message (semantic signal)
- **author.time**: Commit timestamp
- **merged_via_pr**: Associated PR number (can cross-reference with pull_requests.jsonl)
- **additions/deletions**: Code churn (magnitude of change)
- **files_changed**: Detailed per-file changes (reveals scope and nature)
- **dag.layer**: Topological layer within the constructed dependency DAG.
  - `0` = Foundation (no dependencies)
  - `1-2` = Early layers
  - `3-5` = Later layers
  - Layer indicates structural order only; a seed anchors a milestone phase and is not necessarily the earliest or lowest-layer commit
- **dag.descendants_count**: Transitive number of commits that depend on this commit in the analysis DAG (impact).
  - Higher count → broader impact → higher priority
- **dag.ancestors_count**: Transitive number of commits this commit depends on in the analysis DAG (dependency depth).
  - Lower count → more independent → higher priority
- **dag.in_degree/out_degree**: Direct dependency relationships in the analysis DAG.
  - Edge semantics: `source -> target` means the target depends on the source
  - `in_degree` = direct dependencies; `out_degree` = direct dependents
  - Definition & limits: dependencies are textual edit-overlap/ordering signals (commit B edits on top of A). They do not include semantic or symbolic relations (e.g., API/func call graph, type-level coupling). Inferred with `detection_fuzz=3`, scoped to `analysis_scope=["src/"]`. Only node-level stats are provided (no full DAG). Use alongside semantics and evolutionary signals—they are complementary.

**🔥 YOU MUST READ THIS FILE COMPLETELY - IT'S YOUR CORE DATA**

---

### 2. Code Maat Analysis (Pre-computed)

**File**: `{{codemaat_analysis}}`

Contains pre-computed evolutionary metrics. Structure:

```json
{
  "hotspots": { "top_20": [{"entity": "src/urllib3/response.py"}] },
  "coupling": { "threshold_40": [{"entity": "src/urllib3/connection.py", "coupled": "src/urllib3/response.py"}] },
  "entity_churn": { "top_20": [{"entity": "src/urllib3/contrib/emscripten/fetch.py", "added": "725", "deleted": "17"}] },
  "temporal_churn": { "all": [{"date": "2023-11-25", "added": "1100", "deleted": "2"}] }
}
```

**How to Use**:

- **hotspots**: Frequently modified files indicate core modules. Seeds touching hotspots are often architectural.
- **coupling**: Files changing together suggest related functionality. Seeds affecting coupled files may represent cross-cutting changes.
- **entity_churn**:
  - `additions >> deletions` → New feature/module
  - `additions ≈ deletions` → Refactoring
  - High churn in few commits → Concentrated development (likely seed)
- **temporal_churn**: Spikes in activity indicate major development periods

**Use this to validate and enrich your judgment, but don't rely on it exclusively.**

---

### 3. Additional Reference Data (Query as Needed)

- **Pull Requests**: `{{pull_requests}}`
  - Cross-reference `merged_via_pr` to get PR title/body for semantic context
  - Look for labels like "breaking", "security", "enhancement"
  - Potentially large, do not read the entire file, query as needed

- **Releases**: `{{releases}}`
  - Understand release milestones
  - Commits near releases may be important

- **Original Repositories** (via git CLI): one checkout per row of the repository table above
  - You can use git commands to explore each repository
  - Example: `git -C repo_data/<repo> show <sha>`

---

## LLM-as-Judge Analysis Process

### Step 1: Load and Explore Data

- Load `{{main_commits}}` and `{{codemaat_analysis}}` into memory.
- Prepare helper sets/maps if useful (e.g., hotspots, high-churn files).

### Step 2: Use Multi-Dimensional Signals

You have access to rich signals across **3 dimensions**. Use your judgment to identify commits that represent milestone seeds.

#### Available Signals

**DAG Topology** (Structural Importance):
- `layer`: Topological layer (lower = foundation)
- `descendants_count`: How many commits depend on this
- `ancestors_count`: How many commits this depends on
- `in_degree`, `out_degree`: Direct dependency relationships

**Semantics** (What Changed & Why):
- Commit message (breaking changes, new features, platform support, security)
- PR title/body (if available via `merged_via_pr`)
- File changes (new directories, file status, paths)
- Code churn (additions, deletions)

**Evolutionary Patterns** (How It Evolved):
- Entity churn (hotspots, cumulative file changes)
- Coupling (files changing together)
- Temporal patterns (activity spikes)

Use these signals together—no single metric decides a seed.

### Step 3: Select Seeds

**Target**: about 20 seeds per repository (soft). Choose commits that open a new development theme for a phase (not incremental work). Aim for diversity across milestone types.

---

## Milestone Types

Classify each seed into one of these types:

- **breaking_change**: Removes support, breaks compatibility
- **major_feature**: Adds significant new functionality
- **platform_support**: Adds support for new platform/runtime
- **security_fix**: Addresses security vulnerabilities
- **architectural_refactor**: Major internal restructuring
- **dependency_update**: Significant dependency changes

---

## Output Format

**IMPORTANT**: Save all generated files to the `{{result_path}}` directory.

**Primary Output**: `{{milestone_seeds}}`

Each line should contain:

```json
{ "sha": "1812eac7", "repo": "urllib3", "confidence_score": 0.95, "milestone_type": "platform_support", "title": "Add Native Emscripten/WebAssembly Support", "churn_context": "+1100 lines, multiple new files in contrib/emscripten/. High entity churn concentrated in few commits.", "rationale": "Anchors the Emscripten milestone phase. Layer 0, 16 descendants; strong impact despite node-level DAG scope." }
```

**Required Fields**:
- **sha**: Short SHA (7 chars)
- **repo**: Repository name (the `name` column of the repository table) that the seed commit belongs to
- **confidence_score**: 0.0-1.0 (how confident you are this is a seed)
- **milestone_type**: One of the types listed above
- **title**: Concise milestone title (what this milestone delivers)
- **churn_context**: 1-2 sentences describing relevant churn patterns
- **rationale**: 2-3 sentences explaining why this is a seed, mentioning DAG metrics

---

## Success Criteria

Your analysis should:

1. Read all commits from `{{main_commits}}`
2. Use DAG stats, semantics, and evolutionary patterns together
3. Identify about 20 seeds per repository; provide clear rationales
4. Cover diverse milestone types
5. Save outputs to `{{result_path}}` (primary: `{{milestone_seeds}}`)

---

## Key Principles

1) Use signals, not rules. Combine DAG, semantics, and churn; avoid rigid thresholds.
2) Quality over quantity: a seed opens a phase/theme; incremental commits are not seeds.
3) Signals are complementary, not decisive in isolation.

---

## Example Decision Process

**Commit**: `1812eac7` "Add native Emscripten support"

Data: additions=1100, files=9, layer=0, descendants=16, ancestors=0. Decision: high-confidence seed for the Emscripten phase.

---

## Final Note

This is **true LLM-as-Judge enhanced with structural data**. You're not running an algorithm - you're making informed judgments by combining:
- **Your semantic understanding** (what changed, why, what it means)
- **DAG topology** (structural importance, impact, dependencies)
- **Evolutionary patterns** (churn, hotspots, coupling)

Trust your judgment, but ground it in data. Good luck!
