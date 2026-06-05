# Milestone Splitting Task - Agent Prompt Template V3

> ⚠️ **DEPRECATED / not on active pipeline path** — No Python code imports this file. The active pipeline (`main_multi_stage.py`) uses `deepcommit/pipeline/prompts/stage_breakdown_v1/milestone_decompose.md` (and the subagent / deep_analysis prompts in the same dir).
> Kept as a standalone design / historical reference only. **For new work, use the `stage_breakdown_v1` prompts from the main pipeline.**

## Task Description

Your task is to split large milestones into smaller, focused sub-milestones using a two-stage approach: first split individual commits, then group them into cohesive units.

### Core Strategy: Two-Stage Split

**Stage 1: Commit-Level Split**
- Analyze each commit within a milestone to determine if splitting is appropriate
- Invoke Commit Split Subagent for commits suitable for splitting
- Obtain a set of split commit patches

**Stage 2: Milestone-Level Split**
- Integrate split commit patches with unsplit commits
- Group commits by related functionality, file associations, and dependencies
- Generate final sub-milestones and their dependency relationships

### Task Objectives

- Break down large milestones into cohesive, appropriately sized sub-milestones
- Ensure each sub-milestone is self-contained and testable
- Establish clear dependency relationships, forming a valid sub-milestone DAG
- Identify opportunities for parallel development to improve efficiency

---

## Input Data

### 1. Milestones Collection

Data file path: `{milestone_csv_path}`

**CSV Format**:
```csv
id,title,category,commits,src_loc,loc,touched_files,start_time,end_time
M2,WebAssembly/Emscripten Support,FEATURE,"1812eac;7b3f948;...",1601,2100,"src/connection.py;...",2023-11-10T...,2023-11-15T...
```

**Field Descriptions**:
- **id**: Milestone identifier (e.g., "M2")
- **title**: Milestone title
- **category**: Category (FEATURE/BUGFIX/STABILITY/REFACTOR, etc.)
- **commits**: List of commit SHAs (7-char short SHA, semicolon-separated)
- **src_loc**: Source code lines changed (under src directory)
- **loc**: Total code lines changed
- **touched_files**: List of modified files
- **start_time**, **end_time**: Time range

### 2. Dependency Relationships

File path: `{dependencies_csv_path}`

**CSV Format**:
```csv
source_id,target_id
M1,M2
M2,M3
```

`source_id → target_id` means **source must complete BEFORE target** (target depends on source). So `source_id` = **prerequisite** (upstream), `target_id` = **dependant** (downstream). Example: `M1,M2` means M2 depends on M1, M2 starts after M1 completes.

### 3. Available Tools and Data Resources

**MCP Tools**:
- `get_commit_context(sha, expand=['files', 'stats', 'diff'])` - Get commit details
- `calculate_commits_src_loc_tool(commits)` - Calculate src_loc for commits

**Git Repository**:
- Path: `{repo_path}`
- Used for commit analysis, diff retrieval, patch application, etc.

**Configuration Parameters**:
- `REPO_SRC_DIRS`: Source code directory prefix (e.g., "src/"), used for filtering src_loc calculation
- `SUB_MILESTONE_TARGET_SIZE`: Reference size range for sub-milestones (reference only, not a hard threshold)
- `COMMIT_SPLIT_SIZE_TARGET`: Reference size for each commit after splitting (reference only, not a hard threshold)

---

## Workflow

### Phase 0: Initialization and Analysis

**Objective**: Identify milestones that need splitting

**Steps**:

1. **Read milestones data**
   - Load all milestones from CSV file
   - Extract basic information for each milestone (id, commits list, src_loc, etc.)

2. **Identify milestones to split**
   - Analyze milestone size, complexity, and functional cohesion
   - Reference metrics (not hard thresholds): src_loc, number of commits, touched_files, time span, etc.
   - Sort by priority (typically process larger, more complex ones first)
   - Record list of milestones to split with reasons

3. **Initialize work environment**
   - Create output directory: `{output_dir}/milestone_splits/`
   - Create subdirectory for each milestone to split: `{output_dir}/milestone_splits/{milestone_id}/`

**Output**: List of milestones to split (sorted by priority)

---

### Phase 1: Commit-Level Analysis

**Objective**: Identify commits within milestones that need splitting

For each milestone to split, follow these steps:

#### 1.1 Retrieve Commit Details

For each commit in the milestone, collect the following information:

**a) Use the MCP tool to get commit details**
   - `get_commit_context(sha, expand=['files', 'stats', 'diff'])`
   - Obtain: message, author, timestamp, diff, files, stats

**b) Calculate commit's src_loc**
   - `calculate_commits_src_loc_tool([sha])`
   - Obtain: code lines changed under src directory

**c) Record complete commit metadata**
   - SHA, message, src_loc, additions, deletions, files, timestamp, etc.

#### 1.2 Commit Split Decision

**Core Principle**: Prioritize functional independence and reviewability over size alone.

**Evaluation factors**:
- ⭐⭐⭐ **Functional independence**: Multiple independent functions that can be understood/tested separately
- ⭐⭐ **Module independence**: Low coupling between modules; scattered vs. concentrated changes
- ⭐⭐ **Split feasibility**: Clear boundaries (by file/module/function); facilitates code review
- ⭐ **Size signal**: Reference only; focus on relative size vs. milestone average

**Decision criteria**:

**Split if**:
- Squash merge with large size (compressed from multiple commits)
- Multiple independent modules with low coupling
- Multi-theme commit message (e.g., "add X, implement Y, fix Z")
- Significantly exceeds milestone average size

**Keep intact if**:
- Single function with small size
- Single-file config/docs modification
- Tightly coupled refactoring
- Atomic hotfix/bug fix
- Binary/auto-generated files

**Default**: When uncertain, keep the commit intact.

---

#### 1.3 Decision Output

For each commit, record:
- **should_split**: true/false
- **confidence**: 0-1 (based on signal strength: strong signals 0.9+, borderline cases 0.6-0.8)
- **reason**: Brief reason (1-2 sentences)
- **split_strategy_hint**: If splitting, suggest strategy (e.g., "group by module", "by functional theme", "by file groups")

**Phase 1 Final Output**:

Based on decision results, classify commits into two categories:
- **commits_to_keep**: Commits to keep unchanged (with decision reasons)
- **commits_to_split**: Commits to split (with decision information)
  - Each commit includes: SHA, reason, confidence, split_strategy_hint, analysis_hints

---

### Phase 2: Commit-Level Split

**Objective**: Decompose identified commits into smaller patches by invoking Commit Split Subagent

#### 2.1 Invoke Commit Split Subagent

**⚠️ Important Note**:
This step requires starting an independent Subagent for each commit to be split to perform the splitting task. The complete prompt template for the Subagent is provided in Phase 2.4 below.

**Invocation Steps**:

For each commit to split (from Phase 1's `commits_to_split` list), follow these steps:

**a) Prepare Subagent input parameters**

Assemble the following parameters:

**From global configuration**:
- `repo_path`: Git repository path (input parameter)
- `focus_areas`: Use `REPO_SRC_DIRS` (e.g., "src/")
- `size_target`: Reference `COMMIT_SPLIT_SIZE_TARGET`, can fine-tune based on commit scale

**From Phase 1 decision results**:
- `target_commit`: commit SHA
- `split_reason`: Split reason
- `split_strategy_hint`: Split strategy hint
- `analysis_hints`: Auxiliary information (is_squash_merge, module_count, functional_themes)

**Generate runtime parameters**:
- `working_branch`: Working branch name (e.g., "split-{commit_sha[:7]}")
- `output_path`: Output directory (e.g., "{output_dir}/milestone_splits/{milestone_id}/commit_splits/{commit_sha[:7]}/")

**b) Invoke Subagent**

Start a new agent instance using the prompt template defined in Phase 2.4 below, and fill the above parameters into the template placeholders.

**c) Important notes**

- ⚠️ Must wait for Subagent to complete before continuing (sequential execution of commit splits)
- ⚠️ Verify output file integrity (METADATA.json, COMMITS.txt, patches/ directory)
- ⚠️ If Subagent execution fails, log error and mark the commit as unsplittable

#### 2.2 Collect Subagent Output

**Output Directory Structure**:
```
{output_dir}/milestone_splits/M2/commit_splits/1812eac/
├── METADATA.json          # Split metadata
├── COMMITS.txt           # List of split commits (SHA + message, in chronological order)
└── patches/              # Patch files directory
    ├── abc123d.patch
    ├── def456e.patch
    └── ...
```

**Build Split Commit Objects**
   - Each split commit is recorded as a "virtual commit"
   - Contains: patch_file path, src_loc, files, message
   - Mark as split from original commit: `base_commit: "1812eac"`

#### 2.3 Integrate Commits List

**Integration Logic**:

Integrate unchanged commits and split commits into a unified list:

**a) Add unsplit commits**
   - Type marked as "original"
   - Retain complete metadata: sha, message, src_loc, files, timestamp, etc.

**b) Add split commits**
   - Read split results from each original commit's METADATA.json
   - Type marked as "split"
   - Record: base_commit (original SHA), split_sha, patch_file path, complete metadata
   - Inherit original commit's timestamp
   - Verify: sum of all splits' src_loc should be close to original commit's src_loc

**c) Sort and deduplicate**
   - Sort by timestamp (for original commits)
   - For split commits from the same original commit, maintain the order from COMMITS.txt
   - Ensure no duplicate commits

**Output**:
- Complete list of commits (original commits + split commits)
- Each commit marked with type (original or split) and complete metadata

---

### Phase 2.4: Commit Split Subagent Prompt Template

**Important**: This section contains the complete prompt template used by the Commit Split Subagent. When invoking a Subagent in Phase 2.1, this prompt should be used with parameters filled in from the parent agent's context.

---

#### Subagent Prompt: Git Commit Split

##### Purpose
This prompt is designed for an autonomous agent to split a large squashed git commit into multiple smaller, logically organized commits. The agent should analyze the commit content, create a splitting plan, and execute the split operation independently.

---

##### Task Overview

You are tasked with splitting a large git commit into multiple smaller, logical commits that are easier to review and understand.

---

##### Input Parameters

The following parameters will be provided when this prompt is invoked:

- **Repository Path**: `{repo_path}` - The git repository location
- **Target Commit**: `{target_commit}` - The commit SHA to be split
- **Focus Areas**: `{focus_areas}` - Specific directories or file patterns that require fine-grained splitting (e.g., "src/" directory)
- **Size Target**: `{size_target}` - Target maximum lines per commit (default: 300-500 lines)
- **Working Branch**: `{working_branch}` - The branch name to create for split commits (default: "split-{target_commit}")
- **Output Path**: `{output_path}` - Directory where patch files and documentation will be generated

---

##### Task Requirements

###### 1. Analysis Phase

Before splitting, thoroughly analyze the target commit:

**a) Examine commit metadata**:
   ```bash
   git show --stat {target_commit}
   git show {target_commit}
   ```

**b) Identify file categories**:
   - Configuration files (CI, linting, dependencies)
   - Documentation (README, changelog, comments)
   - Source code files (implementation)
   - Test files
   - Build/infrastructure files

**c) Analyze large files changes**:
   - Use `git show {target_commit}:{file_path}` to examine structure
   - Identify logical boundaries (classes, functions, modules)
   - Look for opportunities to split by functionality

**d) Identify cross-file dependencies**:
   - Related changes across multiple files
   - Files that should be committed together to maintain logical coherence

**e) Map test files to source code**:
   - Identify which test files correspond to which source files
   - Look for patterns: `test_foo.py` ↔ `foo.py`, `test/path/file.test.js` ↔ `src/path/file.js`
   - **Analyze test file structure**: A single test file may contain tests for multiple modules/classes
     - Example: `test_connection.py` may have `TestHTTPConnection`, `TestHTTPSConnection`, `TestProxyConnection`
     - Map which test classes/functions correspond to which implementation code
   - Note test files that cover multiple source files (integration tests)

###### 2. Planning Phase

Create a detailed splitting plan based on your analysis:

**a) Define commit groups** with clear criteria:
   - Group by functionality (e.g., "Configuration setup", "Core implementation", "Tests")
   - Consider natural development flow
   - Keep related changes together
   - **IMPORTANT**: Include corresponding test code with implementation in the same commit
     - If committing `src/foo.py`, include related tests from `test/test_foo.py`
     - **Tests may need partial staging**: Use `git add -p` if test file contains tests for multiple modules
     - Example: Committing `HTTPConnection` class → include only `TestHTTPConnection` from `test_connection.py`
     - This maintains test-code synchronization and makes each commit self-contained

**b) For focus areas** (e.g., src/ directory):
   - Plan fine-grained splits
   - Large files changes should be split using `git add -p`

**c) For non-focus areas**:
   - Can merge multiple files into larger logical commits
   - Still maintain clear functional boundaries

**d) Document your plan** including:
   - Number of commits to create
   - Each commit's purpose and message
   - Files included in each commit
   - Which files need partial staging (`git add -p`)

###### 3. Execution Phase

Execute the split operation carefully:

**a) Setup**:
   ```bash
   cd {repo_path}
   git checkout {target_commit}
   git checkout -b {working_branch}
   ```

**b) Prepare for splitting**:
   ```bash
   # Soft reset to parent (keeps all changes staged)
   git reset --soft HEAD^

   # Unstage all files
   git reset
   ```

**c) Create commits according to your plan**:
   - For complete files: `git add file1 file2 ...`
   - For partial files: `git add -p file` (interactive selection)
   - For precise control: `git add -e file` (manual patch editing)
   - **Include corresponding test code**: When adding implementation, add its tests too
     - If test file only covers this implementation: `git add src/foo.py test/test_foo.py`
     - If test file covers multiple modules: `git add src/foo.py && git add -p test/test_multi.py`
       - Use `git add -p` to select only the test classes/functions related to current implementation
     - Example: Committing `HTTPConnection` implementation → use `git add -p test/test_connection.py` to select only `TestHTTPConnection`
   - Commit with clear message: `git commit -m "descriptive message"`

**d) For large file splitting**:
   - Use `git add -p` to interactively select hunks
   - Split at logical boundaries (function/class boundaries)
   - Ensure each part makes sense independently
   - When `git add -p` doesn't show good boundaries, use `git add -e` to manually edit the patch
   - **Apply same principle to test files**: If implementation file is split, split its test file too
     - Example: `src/connection.py` split into 3 commits → `test/test_connection.py` also split into 3 commits
     - Commit 1: `HTTPConnection` class + `TestHTTPConnection` class
     - Commit 2: `HTTPSConnection` class + `TestHTTPSConnection` class
     - Commit 3: `ProxyConnection` class + `TestProxyConnection` class

**e) Verification after each commit**:
   ```bash
   git show --stat
   git log --oneline -5
   ```

###### 4. Validation Phase

After completing all commits, validate the results:

**a) Verify commit count and structure**:
   ```bash
   git log --oneline {working_branch} ^{parent_commit}
   ```

**b) Verify total changes match original**:
   ```bash
   git diff {parent_commit}..{target_commit} --stat
   git diff {parent_commit}..{working_branch} --stat
   # These should be identical (or nearly identical)
   ```

**c) Check working tree is clean**:
   ```bash
   git status
   ```

**d) Verify no commits are empty**:
   ```bash
   git log --oneline {working_branch} ^{parent_commit} --format="%h %s" --numstat
   ```

---

##### Important Guidelines

###### Technical Best Practices

- **Use `git add -p` for partial file staging**: When a single file contains multiple logical changes
- **Preserve atomic functionality**: Each commit should represent a complete, logical unit of change
- **Commit message format**: Use clear, descriptive messages following conventional commit style
- **Handle binary files carefully**: Binary files cannot be partially staged
- **Watch for ignored files**: Some files may need `git add -f` if they're in .gitignore

###### Splitting Strategies

**a) By layer/responsibility**:
   - Infrastructure/configuration
   - Type definitions/interfaces
   - Core implementation
   - Utilities/helpers
   - Tests
   - Documentation

**b) By dependency order**:
   - Base/foundation code first (with its tests)
   - Dependent code later (with its tests)
   - Test infrastructure (fixtures, conftest) can be separate if shared across modules

**c) By feature components**:
   - Each major feature or module as separate commits
   - Shared/common code first

###### Quality Checks

- Each commit should be understandable independently
- Commit messages should explain "why" not just "what"
- Related file changes should stay together
- Avoid arbitrary splits that break logical coherence
- **Test code should be committed together with their corresponding implementation**
  - If you split `src/module/feature.py` into multiple commits, split the corresponding tests accordingly
  - **Use `git add -p` for test files containing multiple test suites**
    - Only stage test classes/functions related to current implementation
    - Example: When committing `ClassA` implementation, stage only `TestClassA` from `test_module.py`
  - Tests for a feature should be in the same commit as the feature implementation
  - Exception: Shared test infrastructure (fixtures, conftest, test utilities) can be committed separately

---

##### Final Output:

After completing the split, generate patch files for all commits as the final deliverable.

###### Output Directory Structure

Create the following directory structure in `{output_path}`:

```
{output_path}/
├── METADATA.json                      # Machine-readable metadata (complete information)
├── COMMITS.txt                        # Commit list in application order (SHA + message)
└── patches/                           # Patch files directory
    ├── {commit_sha_1}.patch          # Named by 7-char commit SHA
    ├── {commit_sha_2}.patch
    └── ...
```

This is the minimal, essential output. No additional documentation or scripts needed.

###### Required Steps

**a) Generate patch files**:
```bash
cd {repo_path}
git checkout {working_branch}

# Create output directory
mkdir -p {output_path}/patches

# Get list of commits in order
COMMITS=($(git rev-list --reverse {parent_commit}..{working_branch}))

# Generate patches named by 7-char commit SHA
for commit in "${COMMITS[@]}"; do
    commit_short=$(git rev-parse --short "$commit")
    git format-patch -1 "$commit" \
        --stdout > {output_path}/patches/${commit_short}.patch
done

# Result: patches/abc123d.patch, patches/def456e.patch, ...
```

**b) Generate COMMITS.txt**:

This file lists all commits in application order (SHA + message, space-separated).

```bash
cd {repo_path}
git checkout {working_branch}

# Generate commit list: SHA + message
git log --reverse --format='%h %s' {parent_commit}..{working_branch} \
    > {output_path}/COMMITS.txt
```

Example output:
```
4f1ee4f emscripten: add build infrastructure and configuration
11e1157 emscripten: add test infrastructure
2da00caa emscripten: add comprehensive test suite
...
```

**c) Generate METADATA.json** with structured data:
```json
{
  "version": "1.0",
  "generated_at": "<ISO8601 timestamp>",
  "original_commit": {
    "hash": "<full hash>",
    "short_hash": "<7-char hash>",
    "parent_hash": "<parent hash>",
    "message": "<commit message>",
    "stats": {
      "files_changed": N,
      "insertions": N,
      "deletions": N
    }
  },
  "split_result": {
    "total_commits": N,
    "working_branch": "<branch name>",
    "commits": [
      {
        "number": 1,
        "hash": "<full commit hash>",
        "short_hash": "<7-char hash>",
        "message": "<message>",
        "patch_file": "patches/<short_hash>.patch",
        "stats": {...},
        "files": [...]
      }
    ]
  },
  "focus_areas": {...}
}
```

---

##### Notes

- **Safety**: Always work on a new branch, never modify the original commit directly
- **Autonomy**: The agent should make intelligent decisions based on code analysis, not just follow rigid rules
- **Flexibility**: Adjust the plan if unexpected issues arise during execution
- **Documentation**: Provide clear rationale for all major decisions

---

##### Success Criteria

✅ All changes from the original commit are preserved
✅ Each new commit has a clear, focused purpose
✅ Commit sizes are appropriate (generally < {size_target} lines)
✅ Focus areas receive fine-grained attention
✅ Working tree is clean after completion
✅ Commit messages are clear and descriptive
✅ No empty commits created
✅ Related changes stay together logically

---

**End of Subagent Prompt Template**

---

### Phase 3: Sub-Milestone Generation

**Objective**: Group related commits into cohesive, appropriately sized sub-milestones and export to CSV

**Core Tasks**:
- Analyze semantic and functional relationships between commits
- Group commits by functional themes
- Define sub-milestones for each group (ID, Title, description, statistics)
- Output structured sub_milestones.csv file

#### 3.1 Grouping Strategy

**Core Objective**: Combine commits into cohesive, self-contained sub-milestones

**Grouping considerations**:

- **a) Functional relevance** ⭐ Most important - Identify functional themes, group commits implementing related functions
- **b) Temporal order** - Consider development sequence and evolutionary stages
- **c) File/module structure** - Highly modular code can be grouped by modules
- **d) Size balance** - Reference target size, but prioritize functional cohesion (allow reasonable exceptions)
- **e) Dependencies** - Identify parallelization opportunities, avoid excessive serialization

**Core Principles**:
- ✅ Prioritize functional cohesion and understandability
- ✅ Based on semantic understanding, not mechanical rules
- ✅ Split commits can be distributed to different sub-milestones (based on functional relevance)

---

#### 3.2 Execute Grouping

**Grouping Steps**:

**a) Understand commit semantics**
   - Read all commit messages to understand the purpose and function of each commit
   - Analyze modified files and code to understand technical implementation
   - Identify functional themes (e.g., "basic infrastructure", "connection management", "test infrastructure")

**b) Group by semantic relevance**
   - Group functionally related commits
   - Each group should have a clear functional theme and purpose
   - Splits from the same original commit can be distributed to different groups (based on functional relevance)

**c) Adjust grouping**
   - Merge overly small groups (functionally related and small in size)
   - Split overly large groups (further divide by sub-functions)
   - Reference target size, but prioritize functional cohesion

---

#### 3.3 Define Sub-Milestones

Define sub-milestones for each group.

**a) Generate Sub-Milestone ID**

Format: `{parent_id}.{sequence_number}`

**Numbering Rules**:
- Number by temporal order or dependency order
- Example: M2.1, M2.2, M2.3, ...

**b) Generate Title and Description**

**Title** (6-15 characters):
- Based on the group's theme and primary purpose
- Examples:
  - "Emscripten infrastructure"
  - "Streaming request implementation"
  - "Error handling enhancement"

**mini_srs** (100-200 characters):
- Template: Implement [function]: [method]; Modules involved: [files]; Role: [importance]
- Example:
  - "Establish initial architecture and base classes for Emscripten module: connection.py(186 lines), fetch.py(376 lines); This is the foundation of entire Emscripten support; Main modules: src/urllib3/contrib/emscripten/"

**rationale** (50-100 characters):
- Explain why these commits belong to the same group
- Example:
  - "These commits jointly establish the basic protocol support for HTTP/2, which is the prerequisite for subsequent features"

**c) Calculate Statistics**

Calculate for each sub-milestone:
- **total_src_loc**: Sum of src_loc for all commits
- **loc**: Total code lines changed for all commits (additions + deletions)
- **touched_files**: Set of all files involved by all commits (deduplicated)
- **start_time**: Earliest commit time
- **end_time**: Latest commit time
- **category**: Inherit parent milestone's category

---

#### 3.4 Generate Sub-Milestones CSV

**Output File**: `{output_dir}/milestone_splits/{milestone_id}/sub_milestones.csv`

**CSV Format**:
```csv
id,parent_id,title,category,commits,total_src_loc,loc,touched_files,start_time,end_time,mini_srs,rationale
M2.1,M2,Emscripten infrastructure,FEATURE,"7b3f948;abc123d;def456e",401,550,"pyproject.toml;src/urllib3/contrib/emscripten/connection.py;...",2023-11-10T19:32:03+00:00,2023-11-10T19:32:03+00:00,"Establish initial architecture and base classes for Emscripten module: connection.py(186 lines), ...","These commits jointly establish the basic infrastructure for Emscripten support"
```

**CSV Fields**:
- **id**: Sub-milestone identifier (e.g., "M2.1")
- **parent_id**: Parent milestone's id
- **title**: Sub-milestone title
- **category**: Inherit parent milestone's category
- **commits**: Commits SHA list (7-char short SHA, including original and split, semicolon-separated)
- **total_src_loc**: Sum of src_loc for all commits
- **loc**: Total code lines changed for all commits (additions + deletions)
- **touched_files**: Set of all files involved by all commits (semicolon-separated)
- **start_time**: Earliest commit time
- **end_time**: Latest commit time
- **mini_srs**: Sub-milestone function description (100-200 characters)
- **rationale**: Grouping reason (50-100 characters)

**Phase 3 Output**:
- ✅ `sub_milestones.csv` - Sub-milestone definition file
- ✅ In-memory complete sub-milestone object list (with detailed commit metadata, for subsequent phases)

---

### Phase 4: Dependency Inference

**Objective**: Determine dependency relationships among sub-milestones within the same milestone

**⚠️ Important Scope Note**:
- **Only infer dependencies among sub-milestones within the same milestone**
  - Example: Dependencies among M2.1, M2.2, M2.3
- **Do not infer cross-milestone dependencies**
  - Example: Do not infer dependencies like M2.1 depends on M3.1
  - Cross-milestone dependencies are already defined in the original milestone dependency relationships

---

#### 4.1 Dependency Inference Methods

Identify dependency relationships among sub-milestones by checking for three types of dependencies:

---

**Dependency Type Summary**:

| Type | When to Identify | Severity | Detection Method | Tag |
|------|------------------|----------|------------------|-----|
| **Code Conflict** | Sub-milestones modify overlapping code regions in the same file | ⚠️ Critical<br>(must respect temporal order) | Compare modified files and line ranges;<br>If regions overlap → dependency exists | `CODE_CONFLICT` |
| **Functional Dependency** | M2.1 defines functionality that M2.2 uses | ⭐ Strong<br>(def-before-use) | Check if M2.2 imports/calls/references entities from M2.1;<br>Analyze commit messages for semantic clues | `FUNCTIONAL_DEPENDENCY` |
| **Evolution-Based** | Natural development progression suggests ordering | 💡 Suggested<br>(logical ordering) | Identify common patterns:<br>• Infrastructure → Features<br>• Sync → Async<br>• Core → Error handling | `EVOLUTION_DEPENDENCY` |

---

**Detailed Detection Methods**:

**Type 1: Code Conflict** ⚠️
- Analyze files and code line ranges modified by commits in each sub-milestone
- Check for overlapping code regions (same or adjacent lines in the same file)
- If M2.2 modifies lines that M2.1 modified → M2.2 depends on M2.1

*Example*:
```
M2.1: Modified connection.py lines 100-150 (added basic connection logic)
M2.2: Modified connection.py lines 120-140 (enhanced connection logic)
→ M2.2 depends on M2.1 (overlapping regions, must apply in order)
```

---

**Type 2: Functional Dependency** ⭐
- Identify entities (classes, functions, modules) added by M2.1
- Check if M2.2's commits import, reference, or call these entities
- Use semantic analysis when code-level detection is unclear

*Detection approaches*:
- **File-level**: M2.2 imports modules added by M2.1
- **Semantic**: Commit messages indicate usage (e.g., "Implement requests using HTTPConnection")
- **Cross-file**: M2.1 adds file A, M2.2 modifies file B that references A

*Example*:
```
M2.1: Add src/connection.py (define HTTPConnection class)
M2.2: Add src/request.py (use HTTPConnection to send requests)
→ M2.2 depends on M2.1 (def-before-use)
```

---

**Type 3: Evolution-Based Dependency** 💡
- Identify natural development progression patterns
- Apply only when there's clear architectural/design dependency

*Common patterns*:
- **Layered**: Infrastructure → Core features → Enhancements → Optimization
- **Progressive**: Interface definition → Implementation → Testing
- **Derivative**: Synchronous implementation → Asynchronous version

*Important*:
- Not mandatory - apply only when B truly builds upon A's design/architecture
- Avoid mechanical pattern matching
- When uncertain, skip to preserve parallelization opportunities

*Example*:
```
M2.1: Emscripten basic architecture (define overall design)
M2.2: Streaming request implementation (builds on M2.1's architecture)
→ M2.2 may depend on M2.1 (architectural understanding needed)
```

---

**Decision Priority**:

1. **Code conflicts** (Type 1): Always mark - required for correctness
2. **Functional dependencies** (Type 2): Mark when clear def-use relationship exists
3. **Evolution-based** (Type 3): Use sparingly - avoid over-constraining the DAG
4. **Default**: When uncertain, prefer not marking to preserve parallel opportunities

---

#### 4.2 Dependency Types and Tags

**`CODE_CONFLICT`** (Code Conflict Dependency):
- Sub-milestones modify the same code region of the same file (overlapping hunks)
- Severity: ⚠️ Critical - Must execute in temporal order

**`FUNCTIONAL_DEPENDENCY`** (Functional Definition-Usage Dependency):
- Def-before-use relationship, inferred based on functional logic and call relationships
- Severity: ⭐ Strong - Core dependency, should be followed

**`EVOLUTION_DEPENDENCY`** (Evolution-Based Dependency):
- Dependency based on natural development progression patterns
- Severity: 💡 Suggested - Logical ordering, can be adjusted based on context

#### 4.3 Handle Split Commits Dependencies

**Special Case**:
- Split commits from the same original commit usually have no dependency relationship
- But if they are assigned to different sub-milestones, need to check:
  - Do they modify different parts of the same file?
  - Are there function call relationships?

**Example**:
```
Original commit 1812eac split into 12 commits:
- abc123d (basic infrastructure) → M2.1
- def456e (connection implementation) → M2.1
- 5098905 (fetch implementation) → M2.2
- ...

Dependency relationships:
M2.2 depends on M2.1 (because fetch uses connection)
But abc123d and def456e have no dependency (both in M2.1)
```

#### 4.4 Generate Dependencies CSV

**Output File**: `{output_dir}/milestone_splits/{milestone_id}/sub_dependencies.csv`

**Important**: This CSV only contains dependency relationships among sub-milestones within the same milestone (e.g., M2.1→M2.2)

**CSV Format**:
```csv
source_id,target_id,dependency_tags,dependency_notes
M2.1,M2.2,FUNCTIONAL_DEPENDENCY,"M2.2's streaming request implementation depends on connection and fetch basic interfaces provided by M2.1"
M2.1,M2.3,FUNCTIONAL_DEPENDENCY,"M2.3's error handling depends on M2.1's basic infrastructure"
M2.2,M2.4,FUNCTIONAL_DEPENDENCY,"M2.4's integration testing depends on M2.2's streaming request implementation"
```

**Field Descriptions** (canonical convention — `source` is prerequisite, `target` is dependant):
- `source_id`: **prerequisite** (the upstream sub-milestone that must complete first)
- `target_id`: **dependant** (the downstream sub-milestone that depends on source)
- `dependency_tags`: Dependency type tags (can be multiple, semicolon-separated; use CODE_CONFLICT, FUNCTIONAL_DEPENDENCY, or EVOLUTION_DEPENDENCY)
- `dependency_notes`: Dependency relationship description

**Phase 4 Output**:
- ✅ `sub_dependencies.csv` - Sub-milestone dependency relationship file
- ✅ In-memory dependency graph data structure (for Phase 5 DAG validity validation)

---

### Phase 5: Validation

**Objective**: Validate split quality

#### 5.1 Completeness Check

**Src_loc Preservation**:
- Calculate original milestone's src_loc
- Calculate sum of all sub-milestones' src_loc
- Calculate preservation rate = sum after split / original value
- Check if preservation rate is within reasonable range (typically 0.95-1.05)

**Commits Coverage**:
- All original commits are included (as original or base_commit)
- No duplicate commits

**Split Integrity**:
- For all split commits, the sum of splits' src_loc should be close to original commit's src_loc
- Verify patch files exist and are readable

#### 5.2 Balance Check

Check if the size distribution of sub-milestones is reasonable:

- Collect src_loc for all sub-milestones
- Calculate mean, standard deviation, CV (coefficient of variation = standard deviation / mean)
- Check if CV is reasonable (typically < 1.0 indicates relatively balanced distribution)
- Check for extremely sized sub-milestones
- Reference range but allow reasonable exceptions (e.g., core architecture can be larger)

#### 5.3 DAG Validity Check

Verify whether the dependency graph is a valid DAG (acyclic graph):

- Build dependency relationship graph
- Validate whether cycles exist using topological sort
- If cycles exist, mark as validation failed

#### 5.4 Error Recovery Strategy

**Retry Approach** (sequential attempts):

1. **Auto-adjust**: Automatic corrections
   - Merge overly small sub-milestones
   - Reassign commits causing circular dependencies

2. **Alternative strategy**: Try different grouping approaches
   - Switch grouping methods (functional semantic → file module → temporal order)
   - Adjust grouping granularity (finer or coarser)

3. **Fail gracefully**: Generate report with warnings
   - Create validation report marking issues
   - Continue generating output but flag for manual review

#### 5.5 Success Criteria

Split success must meet the following criteria:

- ✅ **Commit split complete**: All commits to split successfully split, patch files generated correctly
- ✅ **Src_loc preservation**: Total src_loc after split is consistent with original milestone (typically 95%-105% range)
- ✅ **Reasonable size**: Sub-milestones are appropriately sized, cohesive, and self-contained
- ✅ **Balance**: Sub-milestone size distribution is relatively balanced (CV typically < 1.0)
- ✅ **Valid DAG**: Dependency graph is acyclic
- ✅ **Clear functions**: Each sub-milestone has clear functional description
- ✅ **Split integrity**: All split commits have corresponding patch files and metadata

**Output**: Validation report (pass/fail + detailed information)

---

### Phase 6: Report Generation

**Objective**: Generate split report and validation report

#### 6.1 Split Report (`{milestone_id}_split_report.json`)

**Contains**:
- **original_milestone**: Original milestone basic information (id, title, src_loc, number of commits, etc.)
- **commit_split_summary**: Commit-level split summary
  - Total analyzed, kept/split counts
  - Split details for each split commit (original SHA, number of splits, src_loc matching, output directory)
- **split_summary**: Split summary statistics
  - Number of sub-milestones
  - Original/after-split src_loc and conservation rate
  - Size distribution statistics (max, min, mean, median, std_dev, CV)
  - Number of dependencies
- **warnings**: List of warning messages
- **notes**: Split explanations and notes

#### 6.2 Validation Report (`{milestone_id}_verification.json`)

**Contains**:
- **completeness_checks**: Completeness checks (src_loc preservation, commits coverage, split integrity)
- **balance_checks**: Balance checks (size distribution, CV value)
- **dependency_checks**: Dependency checks (DAG validity, cycle detection)
- **split_integrity_checks**: Split commits integrity (patch files exist, metadata complete, loc matching)
- **overall_status**: Overall status (PASSED/FAILED/WARNING)
- **warnings**: Warning list
- **errors**: Error list

**Output**:
- ✅ `{milestone_id}_split_report.json` - Split report
- ✅ `{milestone_id}_verification.json` - Validation report

---

## Output Checklist

The following files should be generated in the end:

```
{output_dir}/milestone_splits/{milestone_id}/
├── commit_splits/                              # Phase 2: Commit-level split results
│   ├── {commit_sha_1}/
│   │   ├── METADATA.json
│   │   ├── COMMITS.txt
│   │   └── patches/*.patch
│   └── {commit_sha_2}/
│       └── ...
├── sub_milestones.csv                          # Phase 3: Sub-milestone definitions
├── sub_dependencies.csv                        # Phase 4: Sub-milestone dependency relationships
├── {milestone_id}_split_report.json           # Phase 6: Final split report
└── {milestone_id}_verification.json           # Phase 5+6: Validation report
```

**File Descriptions**:
- **commit_splits/**: Patch files and metadata for each split commit
- **sub_milestones.csv**: Sub-milestone basic information (id, title, commits, src_loc, etc.)
- **sub_dependencies.csv**: Dependency relationships among sub-milestones within the same milestone (e.g., M2.1→M2.2)
- **split_report.json**: Complete split report (with detailed commit metadata)
- **verification.json**: Split quality validation report
