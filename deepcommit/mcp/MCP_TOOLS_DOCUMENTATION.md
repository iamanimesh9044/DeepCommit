# MCP Tools Documentation

## Overview

This document provides comprehensive descriptions of the MCP (Model Context Protocol) analysis tools available in the AgentBench repository analysis system. These tools are organized into three main categories:

1. **DAG Analysis Tools** - Graph structure validation and parallelism analysis
2. **Milestone Analysis Tools** - Coverage and balance metrics for development milestones
3. **Conflict Analysis Tools** - Detection and analysis of code conflicts

---

## 1. DAG Analysis Tools

### 1.1 `validate_dag`

#### Function Description

Validates whether a dependency graph is a proper Directed Acyclic Graph (DAG) by detecting cycles and computing topological ordering.

#### Purpose

- Ensure that milestone dependencies form a valid execution order
- Detect circular dependencies that would prevent sequential execution
- Provide topological sort for valid execution ordering

#### Input Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `csv_path` | string | Yes | - | Path to CSV file with `prerequisite_id` and `dependant_id` columns |
| `save_results` | boolean | No | False | Whether to save results to JSON file |

#### Implementation Logic

**Step 1: Load Graph from CSV**
- Reads dependency CSV file with columns: `prerequisite_id`, `dependant_id`
- Builds adjacency list representation: `graph[source] = {target1, target2, ...}`
- Collects all unique node IDs

**Step 2: Cycle Detection (Three-Color DFS)**
- Uses depth-first search with three states:
  - White (0): Unvisited
  - Gray (1): Currently visiting (in recursion stack)
  - Black (2): Fully processed
- Algorithm:
  ```
  For each unvisited node:
    Mark as Gray (visiting)
    For each neighbor:
      If neighbor is Gray → Cycle detected!
      If neighbor is White → Recursively visit
    Mark as Black (processed)
  ```
- If cycles detected, performs second pass to extract all cycle paths

**Step 3: Topological Sort (Kahn's Algorithm)**
- Only performed if graph is acyclic
- Computes in-degree for each node (number of incoming edges)
- Algorithm:
  ```
  Initialize queue with all nodes having in-degree = 0
  While queue not empty:
    Pop node from queue
    Add to result
    For each neighbor of node:
      Decrease neighbor's in-degree
      If in-degree becomes 0, add to queue
  ```
- Returns None if cycle exists

**Step 4: Graph Statistics**
- Counts total nodes and edges
- Packages results with metadata

#### Return Value

```json
{
  "is_valid_dag": true/false,
  "has_cycle": true/false,
  "node_count": int,
  "edge_count": int,
  // If has_cycle = true:
  "cycles": [["node1", "node2", "node3", "node1"], ...],
  "cycle_count": int,
  // If has_cycle = false:
  "topological_order": ["node1", "node2", ...],
  // If save_results = true:
  "output_file": "path/to/file.json",
  "output_message": "Results saved to ..."
}
```

#### Key Algorithms

- **Cycle Detection**: DFS with three-color marking (O(V + E))
- **Topological Sort**: Kahn's algorithm with in-degree tracking (O(V + E))

#### Use Cases

- Validate dependency declarations before execution
- Detect circular dependencies in milestone planning
- Generate valid execution order for parallel processing

---

### 1.2 `analyze_dag_parallelism`

#### Function Description

Analyzes the parallelism characteristics and performance potential of a DAG, including critical path analysis, maximum parallelism width, and speedup potential.

#### Purpose

- Identify execution bottlenecks (critical path)
- Measure parallelization opportunities
- Estimate speedup with different processor counts
- Find bottleneck nodes that limit parallelism

#### Input Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `csv_path` | string | Yes | - | Path to dependency CSV file |
| `include_bottlenecks` | boolean | No | True | Whether to identify bottleneck nodes |
| `save_results` | boolean | No | False | Whether to save results to JSON |

#### Implementation Logic

**Step 1: Compute Parallel Levels**
- Organizes nodes into levels where each level can execute in parallel
- Algorithm (Level-by-level BFS):
  ```
  While nodes remain:
    Find all nodes with in-degree = 0 → Current level
    Remove current level nodes
    Update in-degrees of their neighbors
    Add current level to levels list
  ```
- Each level represents tasks that can run concurrently

**Step 2: Find Critical Path (Longest Path)**
- Uses dynamic programming on topological order
- Algorithm:
  ```
  dist[node] = 0 for all nodes
  parent[node] = None for all nodes

  For each node in topological order:
    For each neighbor of node:
      if dist[neighbor] < dist[node] + 1:
        dist[neighbor] = dist[node] + 1
        parent[neighbor] = node

  Find node with maximum dist → End of critical path
  Backtrack using parent pointers to reconstruct path
  ```
- Critical path length = Maximum distance + 1 (counting nodes, not edges)
- Critical path determines minimum execution time

**Step 3: Calculate Parallelism Metrics**

- **Maximum Width**: Max nodes across all levels
  - Represents peak parallelism opportunity
  - `max_width = max(len(level) for level in levels)`

- **Average Parallelism**: Total work / Critical path length
  - Measures overall parallelization efficiency
  - `avg_parallelism = total_nodes / critical_path_length`

- **Parallelism Ratio**: Max width / Critical path length
  - Higher ratio = more opportunities for parallelism
  - `ratio = maximum_width / critical_path_length`

- **Parallel Efficiency**: How well-balanced the levels are
  - Measures if parallelism is evenly distributed
  - `efficiency = sum(level_widths) / (num_levels × max_width)`
  - Values closer to 1.0 indicate better balance

#### Return Value

```json
{
  "critical_path_length": int,
  "critical_path_nodes": ["node1", "node2", ...],
  "maximum_width": int,
  "total_nodes": int,
  "total_edges": int,
  "levels": [["node1", "node2"], ["node3"], ...],
  "level_widths": [2, 1, ...],
  "level_widths_stats": {
    "mean": float,
    "median": float,
    "std_dev": float,
    "min": int,
    "max": int,
    "variance_coefficient": float
  }
}
```

**level_widths_stats explanation:**
- `mean`: Average parallelism across all levels
- `median`: Median parallelism (more robust than mean for skewed distributions)
- `std_dev`: Standard deviation (measures how much level widths vary)
- `min`/`max`: Minimum and maximum level widths
- `variance_coefficient`: Coefficient of variation (std_dev/mean)
  - Low value (< 0.5): Balanced parallelism across levels
  - High value (> 1.0): Highly variable parallelism, some levels are bottlenecks

#### Key Algorithms

- **Level Computation**: Modified BFS with in-degree tracking (O(V + E))
- **Critical Path**: Dynamic programming on topological order (O(V + E))
- **Speedup Calculation**: Level-wise time simulation (O(V + E))

#### Use Cases

- Optimize milestone execution planning
- Identify which milestones can run in parallel
- Estimate time savings with parallel execution
- Find bottlenecks that should be split or optimized

---

## 2. Milestone Analysis Tools

### 2.1 `analyze_commit_coverage`

#### Function Description

Analyzes how well milestones cover the commits in the repository, measuring coverage rates against main branch commits and PR commits.

#### Purpose

- Assess completeness of milestone decomposition
- Identify gaps in milestone coverage
- Validate that major changes are captured in milestones
- Analyze coverage distribution across categories

#### Input Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `csv_file` | string | Yes | - | Path to milestone CSV |
| `main_commits_file` | string | No | Auto | Path to main_commits.jsonl |
| `pr_file` | string | No | Auto | Path to pull_requests.jsonl |
| `save_results` | boolean | No | False | Whether to save results |

#### Implementation Logic

**Step 1: Load Milestone Data**
- Reads milestone CSV with columns:
  - `id`: Milestone identifier
  - `title`, `category`: Metadata
  - `core_pr_commits`: Semicolon-separated commit SHAs (or legacy `commits` column)
  - `main_commits`: Semicolon-separated main branch commits
- Normalizes all commit SHAs to 7 characters for comparison
- Builds two sets:
  - `milestone_core_commits`: All core_pr_commits across milestones
  - `milestone_main_commits`: All main_commits across milestones

**Step 2: Load External Data Sources**
- **Main Commits**: Loads from JSONL file
  - Extracts commit SHAs from main branch
  - Normalizes to 7-char SHAs

- **PR Commits**: Loads from pull_requests JSONL
  - Extracts all commit SHAs from PRs
  - Builds mapping: PR → commit list
  - Normalizes SHAs

**Step 3: Calculate Coverage Rates**

- **Core PR Commits → Main Branch Coverage**:
  ```
  covered_by_core = milestone_core_commits ∩ all_main_commits
  coverage_rate = len(covered_by_core) / len(all_main_commits) × 100%
  ```
  - Measures: What % of main branch is covered by milestone core commits?

- **Main Commits Field Coverage**:
  ```
  covered_by_main = milestone_main_commits ∩ all_main_commits
  coverage_rate = len(covered_by_main) / len(all_main_commits) × 100%
  ```
  - Measures: What % is covered by milestone main_commits field?

- **PR Commits Coverage** (optional):
  ```
  covered_pr = milestone_core_commits ∩ all_pr_commits
  coverage_rate = len(covered_pr) / len(all_pr_commits) × 100%
  ```
  - Measures: What % of PR commits are in milestones?

**Step 4: Category-wise Statistics**
- Groups milestones by category (feature, bugfix, etc.)
- For each category calculates:
  - Number of milestones
  - Total commits (with duplicates)
  - Unique commits (deduplicated)
  - Average commits per milestone

**Step 5: Data Consistency Check**
- Identifies commits in milestones but NOT in data sources:
  ```
  extra = milestone_core_commits - all_main_commits - all_pr_commits
  ```
- These may indicate:
  - Data collection issues
  - Commits from other branches
  - Merge commits not in PR data

#### Return Value

```json
{
  "milestones": [
    {
      "id": "milestone_001",
      "title": "Add feature X",
      "category": "feature",
      "core_commits": ["abc1234", ...],
      "main_commits": ["def5678", ...],
      "commit_count": int
    },
    ...
  ],
  "milestone_core_commits": ["commit1", "commit2", ...],
  "milestone_main_commits": ["commit3", ...],
  "main_coverage": 85.5,  // Percentage
  "pr_coverage": 78.2,    // Percentage
  "total_main_commits": 1250,
  "total_pr_commits": 980,
  "category_stats": {
    "feature": {
      "count": 15,
      "commits": ["...", ...]
    },
    ...
  }
}
```

#### Key Metrics

- **Main Coverage**: Critical metric - should be >80% for good decomposition
- **PR Coverage**: Helps validate milestone completeness
- **Category Distribution**: Shows balance across work types

#### Use Cases

- Validate milestone extraction quality
- Identify missing commits that should be in milestones
- Assess balance of work across categories
- Quality control for automated milestone generation

---

### 2.2 `analyze_milestone_size_balance`

#### Function Description

Analyzes milestone size distribution and balance, including LoC-weighted metrics to account for actual code change magnitude.

#### Purpose

- Assess if work is evenly distributed across milestones
- Identify oversized or undersized milestones
- Use LoC weighting to measure "true" work complexity
- Validate milestone decomposition quality

#### Input Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `csv_file` | string | Yes | - | Path to milestone CSV |
| `repo_path` | string | No | Auto | Path to bare git repository |
| `save_results` | boolean | No | False | Whether to save results |

#### Implementation Logic

**Step 1: Load Milestone Data**
- Reads milestone CSV
- Extracts `core_pr_commits` (or legacy `commits` column)
- Normalizes commit SHAs to 7 characters
- Records milestone metadata (id, title, category)

**Step 2: Basic Size Statistics**
- Calculates for milestone sizes (commit counts):
  - **Mean**: Average milestone size
  - **Median**: Middle value (robust to outliers)
  - **Standard Deviation**: Spread of sizes
  - **Min/Max**: Range of sizes
  - **Coefficient of Variation (CV)**: `std_dev / mean`
    - CV < 0.3: Well-balanced
    - CV 0.3-0.6: Moderate variation
    - CV > 0.6: High variation (imbalance)

**Step 3: LoC-Weighted Analysis** (if repo_path provided)

This is the key innovation - weights milestones by actual code changes.

**3a. Calculate LoC per Milestone**
```python
For each milestone:
  total_loc = 0
  valid_commits = 0

  For each commit in milestone:
    loc_info = get_commit_loc(commit)  # Uses git diff
    if commit exists:
      total_loc += loc_info['total']  # additions + deletions
      valid_commits += 1

  milestone_locs.append(total_loc)
  milestone_sizes.append(valid_commits)
```

**3b. Filter Valid Data**
- Remove milestones with LoC = 0 (empty or failed)
- Need at least 2 milestones for statistics

**3c. Calculate LoC-Weighted Standard Deviation**

Standard deviation doesn't account for milestone importance. A 100-line milestone and a 10,000-line milestone shouldn't be weighted equally.

- **Weighted Mean Size**:
  ```
  weighted_mean = Σ(size_i × loc_i) / Σ(loc_i)
  ```
  - Milestones with more code changes contribute more to the mean

- **Weighted Variance**:
  ```
  weighted_variance = Σ(loc_i × (size_i - weighted_mean)²) / Σ(loc_i)
  ```
  - Penalizes deviations proportional to milestone importance

- **Weighted Standard Deviation**:
  ```
  weighted_std = √(weighted_variance)
  ```

- **LoC Coefficient of Variation** (直接衡量工作量平衡性):
  ```
  loc_cv = loc_std / loc_mean
  ```
  - This directly measures the balance of workload (LoC) distribution
  - Low loc_cv (< 0.5): Well balanced
  - High loc_cv (> 1.0): Severely imbalanced

- **Weighted Commit CV** (由大milestone主导，不反映整体平衡性):
  ```
  weighted_commit_cv = weighted_std / weighted_mean
  ```
  - This measures the CV of commit counts weighted by LoC
  - **Caveat**: Large milestones dominate this metric, may be misleading
  - Use `loc_cv` for true workload balance assessment

**3d. Calculate Correlation**
- Pearson correlation between LoC and commit count
- Formula:
  ```
  correlation = Σ((loc_i - mean_loc) × (size_i - mean_size)) /
                (√Σ(loc_i - mean_loc)² × √Σ(size_i - mean_size)²)
  ```
- Interpretation:
  - |r| > 0.7: Strong correlation (commit count is good proxy for work)
  - 0.4 < |r| < 0.7: Moderate correlation
  - |r| < 0.4: Weak correlation (commit count misleading)

**Step 4: Comparison Analysis**
- Compares standard std vs. weighted std
- Large difference indicates:
  - Some milestones have very large/small commits
  - Commit count alone is misleading
  - Should use LoC-weighted metrics for planning

#### Return Value

```json
{
  "milestones": [...],
  "total_count": int,
  "size_stats": {
    "mean": float,
    "median": float,
    "std_dev": float,
    "min": int,
    "max": int,
    "cv": float
  },
  "loc_stats": {
    "loc_mean": float,
    "loc_median": float,
    "loc_std": float,
    "loc_min": int,
    "loc_max": int,
    "loc_cv": float,  // LoC CV - TRUE workload balance metric
    "weighted_mean_size": float,
    "weighted_std": float,
    "weighted_commit_cv": float,  // Weighted commit CV - dominated by large milestones
    "loc_size_correlation": float,
    "valid_milestone_count": int,
    "total_milestone_count": int
  }
}
```

#### Key Insights

- **High CV** (>0.6): Milestones are unbalanced, some too large/small
- **Low Correlation**: Commit count doesn't reflect work complexity
- **Large std difference**: LoC weighting reveals hidden imbalances

#### Use Cases

- Validate milestone decomposition quality
- Identify milestones that should be split or merged
- Plan resource allocation based on actual work complexity
- Quality gate for automated milestone generation

---

## 3. Conflict Analysis Tools

### 3.1 `analyze_milestone_conflicts`

#### Function Description

Performs comprehensive conflict analysis for milestones by detecting sequential cherry-pick conflicts and analyzing their root causes with precise dependency tracking.

#### Purpose

- Detect conflicts when applying milestone commits sequentially
- Identify specific commits that cause each conflict
- Determine if conflicts are resolved in subsequent commits
- Provide detailed conflict reasoning for resolution planning

#### Input Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `milestone_csv_path` | string | Yes | - | Path to milestone CSV |
| `milestone_ids` | list[string] | No | None | Specific milestones to analyze (None = all) |
| `main_branch` | string | No | "main" | Main branch name |

#### Implementation Logic

**Step 1: Sequential Conflict Detection**

Uses `MilestoneConflictDetector`:

```
For each milestone:
  base_commit = None

  For each commit in milestone (in order):
    if base_commit is None:
      base_commit = commit
      continue

    # Try to cherry-pick next_commit onto base_commit
    result = git cherry-pick --no-commit next_commit

    if result contains "conflict":
      conflicts.append({
        base: base_commit,
        target: next_commit,
        position: index,
        files: extract_conflict_files()
      })

    base_commit = next_commit
```

**Step 2: Precise Conflict Analysis**

For each detected conflict, uses `MilestoneConflictDetector.analyze_conflict()`:

**2a. Set up Test Environment**
```
1. Create temporary directory
2. Clone repository
3. Checkout base_commit
4. Create test branch
```

**2b. Attempt Cherry-pick**
```
result = git cherry-pick --no-commit target_commit
if conflict detected:
  conflict_files = git diff --name-only --diff-filter=U
```

**2c. Analyze Dependencies**

For each conflict file:
```
1. Get list of commits between base_commit and target_commit
   that modified the conflict file

2. For each intermediate commit:
   a. Reset to base_commit
   b. Try cherry-pick intermediate_commit
   c. If succeeds, then cherry-pick target_commit
   d. If no conflict → intermediate_commit is a dependency!

3. Dependencies are commits that must be applied before
   target_commit to avoid conflict
```

**2d. Classify Conflict Type**
- **Simple**: 0-1 dependencies
- **Complex**: 2-5 dependencies
- **Very Complex**: >5 dependencies

**Step 3: Resolution Detection**

Checks if conflict is resolved in milestone:

```
For commits after conflict position:
  if commit modifies any conflict_file:
    return (True, "Resolved in commit {sha}")

return (False, "Unresolved")
```

**Step 4: Enhanced Result Packaging**

Combines information:
```json
{
  "base_commit": "abc1234",
  "next_commit": "def5678",
  "position": 5,
  "files": ["file1.py", "file2.py"],
  "conflict_type": "complex",
  "dependencies": [
    {"commit": "xyz0123", "files": ["file1.py"]},
    ...
  ],
  "causing_commits": ["xyz0123", ...],
  "is_resolved": true,
  "resolution_reason": "Resolved in commit 890abcd"
}
```

**Step 5: Statistics Aggregation**
- Count resolved vs. unresolved conflicts
- Calculate resolution rate
- Group by milestone and category

#### Return Value

```json
{
  "metadata": {
    "repo_path": "...",
    "milestone_csv": "...",
    "analysis_time": "2024-01-01T12:00:00",
    "main_branch": "main"
  },
  "milestones": {
    "milestone_001": {
      "title": "Add feature X",
      "category": "feature",
      "conflict_count": 3,
      "enhanced_conflicts": [
        {
          "base_commit": "abc1234",
          "next_commit": "def5678",
          "position": 5,
          "files": ["file1.py"],
          "conflict_type": "simple",
          "dependencies": [],
          "is_resolved": true,
          "resolution_reason": "Resolved in commit xyz"
        },
        ...
      ],
      "resolved_count": 2,
      "unresolved_count": 1,
      "resolution_rate": 0.67
    },
    ...
  },
  "summary": {
    "total_milestones_analyzed": 25,
    "milestones_with_conflicts": 8,
    "total_conflicts": 45,
    "total_resolved": 32,
    "total_unresolved": 13,
    "overall_resolution_rate": 0.71
  }
}
```

#### Key Algorithms

- **Sequential Cherry-pick**: Simulates milestone application (O(n) per milestone)
- **Dependency Analysis**: Binary search for causing commits (O(k log k) per conflict)
- **Resolution Detection**: File-based tracking in commit sequence

#### Use Cases

- **Before Execution**: Identify conflicts that need resolution
- **Quality Control**: Validate milestone coherence
- **Planning**: Estimate difficulty of milestone application
- **Root Cause**: Find exact commits causing conflicts

#### Important Notes

- Should be run BEFORE `analyze_dag_conflicts`
- Requires working tree (not bare repo) for conflict analysis
- Can be resource-intensive for large milestones
- Results used as input for DAG conflict validation

---

### 3.2 `analyze_dag_conflicts`

#### Function Description

Validates declared DAG dependencies using topology analysis and discovers hidden dependencies through snapshot-based conflict detection between supposedly parallel milestones.

#### Purpose

- Validate that declared dependencies match DAG topology
- Identify invalid dependencies (between parallel milestones)
- Discover hidden dependencies via code conflicts
- Optimize dependency graph by removing redundant edges

#### Prerequisites

**IMPORTANT**: Milestones must have no internal conflicts before running this tool. Run `analyze_milestone_conflicts` first to resolve intra-milestone conflicts.

#### Input Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `milestone_csv_path` | string | Yes | - | Path to milestone CSV |
| `dependencies_csv_path` | string | Yes | - | Path to dependencies CSV |
| `main_branch` | string | No | "main" | Main branch name |
| `max_hidden_pairs` | int | No | 50 | Max parallel pairs to check |

#### Implementation Logic

**Phase 1: Topology-Based Validation**

**1a. Build DAG Structure**
- Loads dependencies: prerequisite_id → dependant_id
- Builds adjacency list: `dependencies[source] = {target1, target2, ...}`
- Identifies all nodes (milestone IDs)

**1b. Compute Transitive Closure (Floyd-Warshall)**

This is critical for understanding ALL dependency relationships:

```python
closure[i] = {} for all nodes

# Initialize direct dependencies
For source → target in dependencies:
  closure[source].add(target)

# Compute transitive closure
For each intermediate k:
  For each source i:
    For each target j:
      if k in closure[i] AND j in closure[k]:
        closure[i].add(j)  # i can reach j via k
```

Result: `closure[A]` contains ALL nodes reachable from A (direct + indirect).

**1c. Classify Each Declared Dependency**

For each declared dependency source → target:

```python
if target in dependencies[source]:
  # Direct dependency in DAG
  type = "valid_direct"

elif target in closure[source]:
  # Can reach target via other nodes
  type = "valid_transitive" (REDUNDANT!)
  path = find_path(source, target)

elif source in closure[target]:
  # Reversed! target actually depends on source
  type = "invalid_reversed"
  correct = f"{target} → {source}"

else:
  # No path in either direction
  type = "invalid_parallel"
  note = "These milestones can run in parallel"
```

**Phase 2: Discover Hidden Dependencies**

**2a. Find Truly Parallel Pairs**

```python
parallel_pairs = []

For each pair (m1, m2):
  # Check both directions
  has_path_12 = m2 in closure[m1]
  has_path_21 = m1 in closure[m2]

  if NOT has_path_12 AND NOT has_path_21:
    # Neither depends on the other → Parallel!
    parallel_pairs.append((m1, m2))
```

**2b. Get Milestone Snapshots**

Instead of testing n×m commits pairwise (slow!), we use snapshots:

```python
For milestone:
  commits = get_all_commits()

  # Sort by commit time
  first_commit = earliest commit
  last_commit = latest commit
  base_commit = first_commit.parent

  snapshot = (first_commit, last_commit, base_commit)
```

This represents the milestone's CUMULATIVE effect.

**2c. Snapshot-Based Conflict Detection**

For each parallel pair (m1, m2):

**Test Direction 1: m1 → m2**
```
1. Clone repository to temp directory
2. Checkout m1_last_commit (final state of m1)
3. Try to apply ALL of m2 as a single range:
   git cherry-pick --no-commit m2_base..m2_last
4. If conflict → m2 depends on m1
```

**Test Direction 2: m2 → m1**
```
(Same process, swapped)
```

**2d. Interpret Results**

```python
if conflict_m1_to_m2 AND NOT conflict_m2_to_m1:
  dependency = "m2 → m1"  # m1 must come first

elif conflict_m2_to_m1 AND NOT conflict_m1_to_m2:
  dependency = "m1 → m2"  # m2 must come first

elif conflict_m1_to_m2 AND conflict_m2_to_m1:
  dependency = "m1 ↔ m2 (bidirectional)"  # CYCLIC!

else:
  # No conflicts → Truly parallel
  pass
```

**2e. Backup Method: Patch-Based Detection**

If cherry-pick fails (e.g., complex history):

```
1. Generate patch: git diff m2_base..m2_last > milestone.patch
2. Reset to m1_last
3. Try apply: git apply --check milestone.patch
4. If fails → Conflict exists
```

**Phase 3: Statistics and Analysis**

**3a. Dependency Quality Metrics**
```python
false_dependency_ratio = invalid_declared / total_declared
# High ratio = Poor dependency quality

redundancy_ratio = valid_transitive / total_declared
# High ratio = DAG could be simplified
```

**3b. Generate Recommendations**

- **Invalid Dependencies**: Remove from graph
- **Transitive Dependencies**: Can be removed (implied by transitivity)
- **Hidden Dependencies**: Must be added to graph

#### Return Value

```json
{
  "declared_dependencies": [
    {
      "source_milestone": "m001",
      "target_milestone": "m002",
      "dependency_type": "valid_direct",
      "dependency_valid": true,
      "status": "valid_direct"
    },
    {
      "source_milestone": "m003",
      "target_milestone": "m005",
      "dependency_type": "valid_transitive",
      "dependency_redundant": true,
      "path": "m003 → m004 → m005",
      "status": "valid_transitive"
    },
    {
      "source_milestone": "m006",
      "target_milestone": "m007",
      "dependency_type": "invalid_parallel",
      "dependency_valid": false,
      "note": "These milestones are on different branches",
      "status": "invalid"
    }
  ],
  "hidden_dependencies": [
    {
      "milestone_1": "m010",
      "milestone_2": "m012",
      "dependency_direction": "m010 → m012",
      "conflict_details": "Snapshot conflict detected",
      "conflict_files": ["file1.py", "file2.py"],
      "hidden_dependency_detected": true
    }
  ],
  "parallel_pairs_count": 45,
  "summary": {
    "total_milestones": 30,
    "total_declared": 25,
    "valid_direct": 18,
    "valid_transitive": 5,
    "invalid_declared": 2,
    "total_hidden": 3,
    "parallel_pairs": 45,
    "false_dependency_ratio": 0.08,
    "detection_mode": "topology_and_conflict_based"
  }
}
```

#### Performance Optimization

**Snapshot Method Benefits**:
- Traditional: O(n × m) where n, m = commits per milestone
- Snapshot: O(1) per milestone pair
- For 100 commits per milestone: 10,000× → 1× = **10,000× speedup**!

**Why It Works**:
- Testing cumulative effect (base → last) captures all intermediate changes
- When milestones have no internal conflicts, snapshot is 100% accurate
- Conflicts in snapshot ⟺ conflicts in sequential application

#### Key Algorithms

- **Transitive Closure**: Floyd-Warshall (O(V³))
- **Conflict Detection**: Snapshot-based cherry-pick (O(1) per pair)
- **Path Finding**: BFS for finding dependency paths (O(V + E))

#### Use Cases

1. **Validation**: Before execution, validate dependency graph
2. **Optimization**: Remove redundant transitive dependencies
3. **Discovery**: Find missing dependencies that would cause conflicts
4. **Quality Control**: Measure false dependency ratio

#### Interpretation Guide

**Good DAG**:
- `false_dependency_ratio` < 0.1
- `total_hidden` = 0
- `valid_transitive` / `total_declared` < 0.2

**Poor DAG**:
- `false_dependency_ratio` > 0.2 → Many invalid dependencies
- `total_hidden` > 5 → Missing critical dependencies
- `valid_transitive` / `total_declared` > 0.5 → Over-specified

#### Error Scenarios

- **Bidirectional Conflicts**: Indicates cyclic dependency in code
- **High Hidden Count**: Milestone decomposition is flawed
- **Many Invalid**: Dependency declaration doesn't match code reality

---

## Tool Workflow Recommendations

### Standard Analysis Pipeline

```
1. validate_dag
   ↓ (if valid)
2. analyze_dag_parallelism
   ↓
3. analyze_commit_coverage
   ↓
4. analyze_milestone_size_balance
   ↓
5. analyze_milestone_conflicts  ← Fix conflicts here!
   ↓ (after resolving conflicts)
6. analyze_dag_conflicts
```

### Decision Points

**After validate_dag**:
- ❌ Invalid → Fix dependency cycles before proceeding
- ✅ Valid → Continue to parallelism analysis

**After analyze_milestone_conflicts**:
- If `unresolved_count` > 0 → Resolve conflicts before DAG analysis
- If `resolution_rate` < 0.8 → Review milestone decomposition
- Only proceed to DAG analysis when clean

**After analyze_dag_conflicts**:
- If `false_dependency_ratio` > 0.2 → Review dependency declarations
- If `total_hidden` > 0 → Add missing dependencies
- Iterate until graph is clean

---

## Performance Considerations

### Large Repository Handling

| Tool | Complexity | Bottleneck | Optimization |
|------|------------|------------|--------------|
| validate_dag | O(V + E) | Graph traversal | None needed |
| analyze_dag_parallelism | O(V + E) | Critical path DP | None needed |
| analyze_commit_coverage | O(N) | File I/O | Use JSONL streaming |
| analyze_milestone_size_balance | O(N × C) | Git LoC calculation | Cache results |
| analyze_milestone_conflicts | O(M × C²) | Cherry-pick testing | Limit milestone selection |
| analyze_dag_conflicts | O(P) | Conflict detection | Use `max_hidden_pairs` |

Where:
- V = nodes (milestones)
- E = edges (dependencies)
- N = total commits
- C = commits per milestone
- M = number of milestones
- P = parallel pairs to test

### Memory Usage

- **validate_dag**: Low (graph structure only)
- **analyze_dag_parallelism**: Low (graph + metrics)
- **analyze_commit_coverage**: Medium (commit lists)
- **analyze_milestone_size_balance**: Medium-High (LoC cache)
- **analyze_milestone_conflicts**: High (git operations)
- **analyze_dag_conflicts**: Very High (temporary repos)

### Recommended Limits

```python
# For production use:
max_milestones_for_conflict_analysis = 50
max_commits_per_milestone = 200
max_hidden_pairs = 50

# For full validation:
max_milestones_for_conflict_analysis = 100
max_commits_per_milestone = 500
max_hidden_pairs = 100
```

---

## Common Error Patterns and Solutions

### 1. High False Dependency Ratio

**Symptom**: `false_dependency_ratio > 0.3`

**Causes**:
- Dependencies declared based on time order, not code dependencies
- Copy-paste errors in dependency CSV
- Misunderstanding of dependency semantics

**Solution**:
```
1. Run analyze_dag_conflicts with save_results=true
2. Review "invalid_declared" entries
3. Remove dependencies between parallel milestones
4. Re-validate with validate_dag
```

### 2. Many Hidden Dependencies

**Symptom**: `total_hidden > 10`

**Causes**:
- Incomplete dependency analysis during milestone creation
- Shared file modifications not tracked
- Refactoring that affects multiple milestones

**Solution**:
```
1. Review hidden_dependencies in results
2. Add missing dependencies to dependencies.csv
3. Consider splitting milestones with many hidden deps
4. Re-run analyze_dag_conflicts
```

### 3. Unresolved Conflicts in Milestones

**Symptom**: `unresolved_count > 5` in analyze_milestone_conflicts

**Causes**:
- Incomplete milestone commit sequences
- Missing intermediate commits
- Commits applied in wrong order

**Solution**:
```
1. Review "enhanced_conflicts" with is_resolved=false
2. Check if "dependencies" commits are in milestone
3. Add missing commits or reorder sequence
4. Re-run analyze_milestone_conflicts
```

### 4. Low Coverage Rates

**Symptom**: `main_coverage < 70%`

**Causes**:
- Milestone extraction incomplete
- Important commits missed
- Wrong commit range used

**Solution**:
```
1. Identify uncovered commits:
   uncovered = all_main_commits - milestone_core_commits
2. Analyze uncovered commits (git log)
3. Add missing commits to milestones
4. Re-run analyze_commit_coverage
```

### 5. High Size Imbalance

**Symptom**: `cv > 0.8` or `loc_cv > 1.0`

**Causes**:
- Some milestones too large (should be split)
- Some milestones too small (should be merged)
- Uneven feature complexity

**Solution**:
```
1. Sort milestones by size (ascending)
2. Split milestones with size > 2×mean
3. Merge milestones with size < 0.5×mean
4. Re-run analyze_milestone_size_balance
```

---

## Integration with LLM Agents

These tools are designed for LLM agent consumption via MCP:

### Usage Pattern

```python
# Agent workflow:
1. Call validate_dag(csv_path)
   if not is_valid_dag:
     analyze cycles and suggest fixes

2. Call analyze_dag_parallelism(csv_path)
   report: "Critical path: {critical_path_length} levels"
   report: "Maximum parallelism: {maximum_width} nodes"
   report: "Median level width: {level_widths_stats['median']} nodes"

3. Call analyze_commit_coverage(csv_file)
   if main_coverage < 80:
     warn: "Coverage is low, may miss important changes"

4. Call analyze_milestone_size_balance(csv_file)
   if cv > 0.6:
     suggest: "Consider rebalancing milestones"

5. Call analyze_milestone_conflicts(milestone_csv, milestone_ids)
   if unresolved_count > 0:
     stop: "Resolve conflicts before proceeding"

6. Call analyze_dag_conflicts(milestone_csv, dependencies_csv)
   if false_dependency_ratio > 0.1:
     suggest: "Review and fix invalid dependencies"
   if total_hidden > 0:
     action: "Add missing dependencies to CSV"
```

### Agent Decision Logic

```python
def should_proceed_with_execution(analysis_results):
    """LLM agent decision function"""

    # Critical gates
    if not analysis_results['dag_validation']['is_valid_dag']:
        return False, "DAG has cycles, cannot execute"

    if analysis_results['milestone_conflicts']['summary']['total_unresolved'] > 0:
        return False, "Milestones have unresolved conflicts"

    if analysis_results['dag_conflicts']['summary']['total_hidden'] > 5:
        return False, "Too many hidden dependencies, graph is incomplete"

    # Warning gates
    warnings = []
    if analysis_results['coverage']['main_coverage'] < 70:
        warnings.append("Low commit coverage")

    if analysis_results['balance']['size_stats']['cv'] > 0.8:
        warnings.append("High size imbalance")

    if analysis_results['dag_conflicts']['summary']['false_dependency_ratio'] > 0.2:
        warnings.append("Many invalid dependencies")

    if warnings:
        return True, f"Can proceed with warnings: {', '.join(warnings)}"

    return True, "All checks passed"
```

---

## Appendix: Mathematical Foundations

### A. Coefficient of Variation (CV)

**Formula**: `CV = σ / μ`

**Interpretation**:
- CV < 0.3: Low variability (well-balanced)
- 0.3 ≤ CV < 0.6: Moderate variability
- CV ≥ 0.6: High variability (imbalanced)

**Why it matters**: CV is scale-independent, so it works across projects of different sizes.

### B. Weighted Standard Deviation

**Formula**:
```
weighted_mean = Σ(w_i × x_i) / Σ(w_i)
weighted_var = Σ(w_i × (x_i - weighted_mean)²) / Σ(w_i)
weighted_std = √(weighted_var)
```

**Why it matters**: Gives more weight to important observations (milestones with more LoC).

### C. Pearson Correlation

**Formula**:
```
r = Σ((x_i - x̄)(y_i - ȳ)) / (√Σ(x_i - x̄)² × √Σ(y_i - ȳ)²)
```

**Interpretation**:
- r > 0.7: Strong positive correlation
- 0.3 < r < 0.7: Moderate correlation
- r < 0.3: Weak correlation

**Why it matters**: Tells us if commit count is a good proxy for work complexity.

### D. Amdahl's Law (Speedup)

**Formula**:
```
Speedup = T_sequential / T_parallel
```

**Factors**:
- Sequential work (critical path): Cannot be parallelized
- Parallel work (wide levels): Limited by processor count
- Communication overhead: Not modeled (assumed negligible)

**Why it matters**: Provides realistic speedup estimates accounting for parallelism limits.

---

## Version History

- v1.0 (2024-01): Initial documentation
- Tools support milestone-based repository analysis pipeline
- Optimized for LLM agent consumption via MCP protocol