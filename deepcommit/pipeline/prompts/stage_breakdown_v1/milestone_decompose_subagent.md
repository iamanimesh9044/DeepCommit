**Start of Subagent Prompt Template**

---

#### Subagent Prompt: Git Commit Split

##### Purpose
This prompt is designed for an autonomous agent to split a large squashed git commit into multiple smaller, logically organized commits. The agent should analyze the commit content, create a splitting plan, and execute the split operation independently.

---

##### Task Overview

You are tasked with splitting a large git commit into multiple smaller, logical commits that are easier to review and understand.

### Workspace `{{workspace_id}}`

This run covers a workspace of one or more repositories that are developed together:

{{workspace_repos_table}}

- Inspect code with `git -C repo_data/<repo> ...` (the `repo dir` column of the table; for a one-repo workspace that is `repo_data` itself). The MCP git tools route to the correct repository automatically.
- Commit ids are bare 7-character SHAs, unique inside the workspace. A commit's repository is its `repo` field in the commit records or its entry in `commit_repos` (`commit_repos.json`). Never guess it from the SHA and never run `git` against the wrong repository.
- With more than one repository, every file path in merged data is prefixed `<repo>/<path-in-repo>`; strip the prefix before passing a path to `git -C repo_data/<repo>`.
- Edges in `commit_dag.json` come in two groups. `edges_dag` holds per-repository textual edges plus HARD cross-repo edges (`"xrepo": true`, `edge_types` such as `xrepo_contract`, `xrepo_pin`, `xrepo_pr_link`). `edges_soft` holds heuristic hints (for example time proximity): never enforced, and you may cut them.

**Workspace rules**:
- The commit you split belongs to exactly one repository (its `repo` field or `commit_repos`): the **split repository**, checked out at `repo_data/<repo>`. All git work (`cd`, `git -C`, `docker cp`, script `--repo-path`) happens in that directory, so run the commands below from it; `<repo dir>` below means it.
- Never split a contract change from what its first consumer uses: a commit that is the contract side of a HARD cross-repo edge must be split so that the contract change stays in one piece.
- `integration_test_commit` belongs to the same repository as the split commit; there is at most one per split commit.
- Compilation verification (Phase 5) applies only to a one-repo workspace with a configured base image and build command; with several repositories skip it and record `skipped (multi-repo workspace)` in the metadata notes.
---

##### Input Parameters

The following parameters will be provided when this prompt is invoked:

- **Repository Root**: `{{repo_path}}` - Directory holding the repository checkouts (e.g., `/path/to/trial_space/project/repo_data`); the split repository is `<repo dir>` as defined above
- **Target Commit**: `{{target_commit}}` - The commit SHA to be split (7-char or full SHA)
- **Number of Commits**: `{{num_commits}}` - Target number of feature commits to create from src/ directory (e.g., 3-5)
- **Src Directories**: `{{repo_src_dirs}}` - Comma-separated source directories to include (e.g., "src,lib"); with several repositories they are prefixed `<repo>/`, so keep only the split repository's entries and strip the prefix before using them inside `<repo dir>`
- **Exclude Patterns**: `{{exclude_patterns}}` - Comma-separated glob patterns for files to exclude (e.g., "tests/**,**/test_*.py")
- **Working Branch**: `{{working_branch}}` - The branch name for split commits (**MUST be `split-<commit_sha>`**, e.g., `split-5b9adb0`)
- **Base Image Name**: `{{base_image_name}}` (only set for a one-repo workspace) - Docker image with compilation environment (e.g., `navidrome-test:v0.57.0`)
- **Build Command**: `{{build_command}}` (only set for a one-repo workspace) - Command to compile the project (e.g., `go build ./cmd/...`, `npm run build`)

**Output Location** (hardcoded, NOT a parameter):
- Output directory: `WORKSPACE_ROOT/stage4_decomp/output/commit_splits/{{working_branch}}/`
- Where `WORKSPACE_ROOT = $(dirname "{{repo_path}}")` (parent of repo_data)
- Example: If `{{repo_path}}` is `/path/trial_space/project/repo_data`, output goes to `/path/trial_space/project/stage4_decomp/output/commit_splits/split-xxx/`

---

##### Task Requirements

###### 1. Analysis Phase

Before splitting, thoroughly analyze the target commit:

**a) Examine commit metadata and calculate LOC breakdown**:
   ```bash
   # Overall stats
   git show --stat {{target_commit}}

   # Src directory LOC (primary splitting basis)
   # Note: Replace {{repo_src_dirs}} with actual directories; for multiple dirs, sum each separately
   git show {{target_commit}} --numstat -- {{repo_src_dirs}} | awk '{sum+=$1+$2} END {print sum}'

   # Test LOC
   git show {{target_commit}} --numstat -- test/ | awk '{sum+=$1+$2} END {print sum}'

   # Config/infrastructure LOC
   git show {{target_commit}} --numstat -- '*.yml' '*.yaml' 'noxfile.py' | awk '{sum+=$1+$2} END {print sum}'
   ```

**b) Identify file categories**:
   - **Src files** ({{repo_src_dirs}}): Primary splitting target (may span multiple directories)
   - **Test infrastructure** (conftest.py, fixtures): Shared test foundation
   - **Unit tests**: Test single modules/classes
   - **Integration tests**: Test complete feature workflows
   - **Configuration files** (CI, linting, dependencies)
   - **Documentation** (README, changelog)

**c) Calculate splitting target for src/ directory**:
   ```bash
   # Target LOC per feature commit
   target_loc_per_commit = src_total_loc / {{num_commits}}

   # Example: 1102 LOC / 3 commits ≈ 367 LOC per commit
   ```

**d) Analyze test file structure**:
   - **Identify test types**:
     - Unit tests: Test single class/function (e.g., `test_request_validation()`)
     - Integration tests: Test complete workflows (e.g., `test_http_request_response_cycle()`)
   - **Map test-to-code relationships**:
     - Example: `test_connection.py` contains:
       - `TestHTTPConnection` (unit test for `HTTPConnection`) → C2
       - `TestHTTPSConnection` (unit test for `HTTPSConnection`) → C2
       - `test_full_request()` (integration test) → CN+1 (separate test commit)
   - **Identify shared test infrastructure**:
     - `conftest.py`, pytest fixtures → Commit 1
     - Test server setup, mock objects → Commit 1

**e) Analyze src/ code structure**:
   - Use `git show {{target_commit}}:<file_path>` to examine file structure
   - Identify logical boundaries (classes, functions, modules)
   - Group related functionality for balanced commits

###### 2. Planning Phase

Create a detailed splitting plan based on src/ directory LOC balance:

**a) Standard commit structure** ({{num_commits}} feature commits + optional 1 test commit):

   **IMPORTANT**: At most ONE integration test commit allowed per original commit.

   ```
   Commit 1: Infrastructure + Test Infrastructure + First Feature Layer
     ├─ Config files (CI, build, linting)
     ├─ Test infrastructure (conftest.py, fixtures)
     ├─ Src code: ~target_loc_per_commit
     └─ Unit tests for this layer (if exist)

   Commit 2 to N: Feature Layers (balanced by src/ LOC)
     ├─ Src code: ~target_loc_per_commit each
     └─ Unit tests for this layer (if exist)

   Commit N+1: Integration Test Suite (if exists, at most ONE)
     └─ All integration tests that require complete feature stack
   ```

**b) Test file allocation strategy**:

   **CRITICAL PRINCIPLE**: Tests should follow the code they test whenever possible. A pure test commit should NEVER become a standalone milestone.

   | Test Type | Allocation | Condition | Example |
   |-----------|-----------|-----------|---------|
   | **Test infrastructure** | Commit 1 | Always with first feature | `conftest.py`, fixtures |
   | **Unit tests** | Same commit as src | Tests compile & run with that commit's code | `test_request.py` → with `request.py` |
   | **Feature tests** | Same commit as src | Tests only need code from this commit | `test_connection.py` → with `connection.py` |
   | **Integration tests** | `integration_test_commit` | Tests require code from multiple commits | Cross-module workflow tests |

   **Decision flowchart for each test file**:
   ```
   For each test file:
   1. Does it ONLY test code from a single feature commit?
      YES → Include in that feature commit
      NO → Continue to step 2

   2. Can it compile and run with just one feature commit's code?
      YES → Include in that feature commit
      NO → Goes to integration_test_commit

   3. Does it test cross-cutting functionality across multiple commits?
      YES → Goes to integration_test_commit
   ```

   **How to verify test allocation**:
   ```bash
   # After creating feature commit N, verify its tests can run:
   docker exec verify-split bash -c "cd /testbed && \
     go test ./path/to/module/... -run TestXxx"  # or pytest for Python

   # If tests pass → correct allocation
   # If tests fail due to missing code → move to integration_test_commit
   ```

   **How to identify test type**:
   - Unit/Feature test (include with code): Tests single module, can run independently
     ```python
     from src.foo import FooClass
     def test_foo_method(): ...  # Only tests FooClass, include with foo.py
     ```
   - Integration test (separate commit): Tests workflow across multiple modules
     ```python
     from src.connection import HTTPConnection  # From commit 2
     from src.response import Response           # From commit 1
     from src.fetch import fetch                 # From commit 3
     def test_full_http_request(): ...  # Needs all commits, → integration_test_commit
     ```

**c) Src/ directory splitting plan**:
   - Target: ~<target_loc_per_commit> LOC per commit
   - Group by logical layers or features
   - Example for 1102 LOC / 3 commits:
     - Commit 1: Infrastructure + Core types + Response (330 LOC)
     - Commit 2: Connection layer (359 LOC)
     - Commit 3: Fetch layer (413 LOC)
     - Commit 4: Integration tests (948 LOC)

**d) Document your plan** including:
   - Total commits to create (feature + test)
   - Each commit's purpose and message
   - Src LOC distribution
   - Test allocation (unit vs integration)
   - Which files need partial staging (`git add -p`)

###### 3. Execution Phase

Execute the split operation carefully:

**a) Setup**:
   ```bash
   cd <repo dir>
   git checkout {{target_commit}}
   git checkout -b {{working_branch}}
   ```

**b) Prepare for splitting**:
   ```bash
   # Soft reset to parent (keeps all changes staged)
   git reset --soft HEAD^

   # Unstage all files
   git reset
   ```

**c) Create Commit 1 (Infrastructure + Test Infrastructure + First Feature)**:
   ```bash
   # Add config/infrastructure files
   git add .github/workflows/*.yml noxfile.py *.txt .eslintrc.yml

   # Add test infrastructure
   git add test/**/conftest.py

   # Add first src/ layer
   git add src/module/__init__.py src/module/core.py

   # Add unit tests for first layer (if exist)
   git add test/test_core.py

   # Commit with detailed message
   git commit -m "$(cat <<'EOF'
feat: add infrastructure, test setup, and core module

Src changes:
- Core module initialization and base classes (XXX LOC)
- Response handling infrastructure (YYY LOC)

Tests:
- Test infrastructure (conftest.py with fixtures)
- Unit tests for core module (if applicable)
EOF
)"
   ```

**d) Create Commits 2-N (Feature Layers)**:
   ```bash
   # For each subsequent feature layer

   # Add src/ files for this layer (complete or partial)
   git add src/module/feature.py
   # OR use git add -p for large files
   git add -p src/module/large_file.py

   # Add unit tests for this layer (if exist)
   # If test file only contains tests for this layer:
   git add test/test_feature.py

   # If test file contains multiple test classes, use git add -p:
   git add -p test/test_multiple.py  # Select only relevant test class

   # Commit with detailed message
   git commit -m "$(cat <<'EOF'
feat: implement feature layer X

Src changes:
- Feature X implementation (XXX LOC)
- Helper utilities for feature X (YYY LOC)

Tests:
- Unit tests for feature X
EOF
)"
   ```

**e) Create Commit N+1 (Integration Tests, if exist)**:

   **IMPORTANT RULES**:
   - ✅ **ONLY ONE** integration test commit allowed per original commit - this is a critical constraint
   - ✅ **MUST be the LAST commit** on the branch
   - ✅ Message must be self-contained (see format below)
   - ✅ Integration test commit typically has **src_loc=0** (tests are outside source directories)

   ```bash
   # Add all integration test files
   git add test/test_integration.py test/test_e2e.py

   # Commit with detailed message
   git commit -m "$(cat <<'EOF'
test: add comprehensive integration test suite

Tests:
- 29 integration tests covering complete workflows
- HTTP/HTTPS requests, streaming, timeouts, error handling
- Requires all feature commits (1-3) to run
EOF
)"
   ```

**f) Tips for partial file staging**:
   - Use `git add -p` to interactively select hunks
   - Split at logical boundaries (function/class boundaries)
   - When `git add -p` doesn't show good boundaries, use `git add -e` to manually edit
   - Example: Split `test_connection.py` by test classes
     ```bash
     git add -p test/test_connection.py
     # Select: TestHTTPConnection class → Commit 2
     # Skip: TestHTTPSConnection class → Commit 3
     ```

**g) Commit Message Format Requirements**:

   **CRITICAL**: Each commit message MUST be self-contained and follow this format:

   **For feature commits**:
   ```
   feat: <concise summary>

   Src changes:
   - <Key feature 1> (<LOC count>)
   - <Key feature 2> (<LOC count>)
   - <Key classes/functions added>

   Tests:
   - <Unit tests included, if any>
   - OR "No unit tests (<reason>)"
   ```

   **For integration test commit**:
   ```
   test: add comprehensive integration test suite

   Tests:
   - <Number> integration tests covering <scope>
   - <Key test scenarios>
   - Requires all feature commits (<range>) to run
   ```

**h) Verification after each commit**:
   ```bash
   git show --stat
   git log --oneline -5

   # Verify message format
   git log -1 --format=%B
   ```

###### 4. Validation Phase

After completing all commits, validate the results:

**a) Verify tree SHA match (MOST IMPORTANT)**:

   This is the **strongest validation** - if tree SHAs match, the splits are 100% identical to the original.

   ```bash
   # Compare tree objects
   ORIG_TREE=$(git rev-parse {{target_commit}}^{tree})
   SPLIT_TREE=$(git rev-parse {{working_branch}}^{tree})

   if [ "$ORIG_TREE" = "$SPLIT_TREE" ]; then
       echo "✅ PERFECT MATCH - Tree SHA is identical"
   else
       echo "❌ Trees differ - investigation needed"
   fi
   ```

   **Note**: Tree SHA represents the entire working directory state. If they match, every file has exactly the same content, permissions, and path.

**b) Check working tree is clean**:
   ```bash
   git status
   ```

###### 5. Compilation Verification Phase

Skipped when the workspace has several repositories (see the workspace rules). Otherwise, after validating tree SHA match, verify that each split commit s successfully when patches are applied sequentially.

**a) Setup container (once)**:
   ```bash
   WORKSPACE_ROOT="$(dirname "{{repo_path}}")"
   OUTPUT_DIR="$WORKSPACE_ROOT/stage4_decomp/output/commit_splits/{{working_branch}}"

   # Start container
   docker run -d --name verify-split-{{target_commit}} {{base_image_name}} tail -f /dev/null

   # Copy repository
   docker exec verify-split-{{target_commit}} rm -rf /testbed
   docker cp <repo dir> verify-split-{{target_commit}}:/testbed
   docker exec verify-split-{{target_commit}} git config --global --add safe.directory /testbed
   docker exec verify-split-{{target_commit}} git config --global user.email "verify@test.local"
   docker exec verify-split-{{target_commit}} git config --global user.name "Verify Bot"

   # Verify parent compiles (environment check)
   docker exec verify-split-{{target_commit}} bash -c "cd /testbed && \
     git checkout {{target_commit}}^ && \
     {{build_command}}"
   ```
   If parent fails to compile, the environment is misconfigured - fix before proceeding.

**b) Generate patches and verify** (repeat until success):
   ```bash
   # Define paths (in case running separately from step a)
   WORKSPACE_ROOT="$(dirname "{{repo_path}}")"
   OUTPUT_DIR="$WORKSPACE_ROOT/stage4_decomp/output/commit_splits/{{working_branch}}"

   # Generate patches from host
   mkdir -p "$OUTPUT_DIR/patches"
   cd "<repo dir>"
   COMMITS=($(git rev-list --reverse {{target_commit}}^..{{working_branch}}))
   for commit in "${COMMITS[@]}"; do
       commit_short=$(git rev-parse --short "$commit")
       git format-patch -1 "$commit" --stdout > "$OUTPUT_DIR/patches/${commit_short}.patch"
   done

   # Reset container state and copy new patches
   docker exec verify-split-{{target_commit}} bash -c "cd /testbed && \
     git checkout {{target_commit}}^ && \
     git clean -fd && \
     git checkout -B verify-split"
   docker exec verify-split-{{target_commit}} rm -rf /tmp/patches
   docker cp "$OUTPUT_DIR/patches" verify-split-{{target_commit}}:/tmp/patches

   # Apply each patch in commit order (IMPORTANT: not alphabetical!)
   for commit in "${COMMITS[@]}"; do
     sha=$(git rev-parse --short "$commit")
     patch_name="${sha}.patch"

     echo "Testing patch $sha..."
     docker exec verify-split-{{target_commit}} bash -c "cd /testbed && \
       git apply /tmp/patches/$patch_name && \
       {{build_command}} && \
       git add -A && git commit -m 'Applied $sha'"

     if [ $? -ne 0 ]; then
       echo "❌ Patch $sha failed - see 'Handling Failures' below"
       break
     fi
   done
   ```

**c) Verify final result**:
   ```bash
   docker exec verify-split-{{target_commit}} bash -c "cd /testbed && git diff {{target_commit}} ."
   # Should produce empty output
   ```

**d) Cleanup container** (only after all verification passes):
   ```bash
   docker stop verify-split-{{target_commit}} && docker rm verify-split-{{target_commit}}
   ```

**Handling Compilation Failures**:

Container stays running for fast iteration. When a patch fails:

1. **Note the error** - record which patch failed and the compilation error
2. **Fix on host** - return to `<repo dir>` and modify commits on `{{working_branch}}`:
   - **Re-partition code** between commits to resolve dependencies
   - **Merge commits** that have tightly coupled changes (`git rebase -i`)
   - **Reorder commits** to ensure definitions appear before usages
3. **Re-run Phase 4** - verify tree SHA still matches
4. **Re-run step (b)** - container resets to parent and applies new patches

The container is reused across iterations - only `git reset` + copy new patches, no container recreation.

---

##### Important Guidelines

###### Technical Best Practices

- **Use `git add -p` for partial file staging**: When a single file contains multiple logical changes
- **Preserve atomic functionality**: Each commit should represent a complete, logical unit of change
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
- **Commit message requirements** (CRITICAL):
  - ✅ Self-contained with "Src changes" and "Tests" sections
  - ✅ Include LOC counts for major features
  - ✅ Explain test status (unit tests included or not, and why)
  - ✅ For test commit: state dependencies on feature commits
  - ❌ No vague messages like "feat: implement feature"
- Related file changes should stay together
- Src/ LOC should be relatively balanced across feature commits
- **Test commit rules** (CRITICAL):
  - ✅ Maximum ONE integration test commit per original commit - strict constraint
  - ✅ MUST be the LAST commit on branch
  - ✅ Message must explain test coverage and dependencies
  - ✅ Integration test commit typically has src_loc=0 (tests outside source directories)
  - ❌ No multiple test commits
  - ❌ No test commit in the middle of feature commits

---

##### Final Output:

After completing the split and compilation verification, finalize the output artifacts.

**Note**: Patches were already generated during Phase 5 (Compilation Verification). This section focuses on generating METADATA.json and analyzing dependencies.

###### Output Directory Structure

**IMPORTANT**: Output MUST be created at `WORKSPACE_ROOT/stage4_decomp/output/commit_splits/`, NOT inside `repo_data/`.

- **Working Branch**: `{{working_branch}}` (e.g., `split-1812eac`)
- **Output Directory**: `WORKSPACE_ROOT/stage4_decomp/output/commit_splits/{{working_branch}}/`

Create the following directory structure:

```
WORKSPACE_ROOT/
├── repo_data/                         # Git repository (DO NOT create output here!)
└── stage4_decomp/
    └── output/
        └── commit_splits/
            └── {{working_branch}}/    # e.g., split-1812eac/
                ├── METADATA.json      # Machine-readable metadata
                └── patches/           # Patch files directory
                    ├── <sha_1>.patch  # Named by 7-char commit SHA
                    ├── <sha_2>.patch
                    └── ...
```

**Example**: For commit `1812eac` with `repo_path=/data/trial/project/repo_data`:
- WORKSPACE_ROOT: `/data/trial/project`
- Branch name: `split-1812eac`
- Output path: `/data/trial/project/stage4_decomp/output/commit_splits/split-1812eac/`

This is the minimal, essential output. No additional documentation or scripts needed.

###### Required Steps

**a) Verify patches exist** (generated in Phase 5):

```bash
WORKSPACE_ROOT="$(dirname "{{repo_path}}")"
OUTPUT_DIR="$WORKSPACE_ROOT/stage4_decomp/output/commit_splits/{{working_branch}}"

ls -la "$OUTPUT_DIR/patches/"
# Should show all .patch files generated during Phase 5
```

**b) Generate METADATA.json**:

Use the automated script to generate structured metadata:

```bash
# Use the same absolute path approach
WORKSPACE_ROOT="$(dirname "{{repo_path}}")"
REPO_PATH="<repo dir>"   # the split repository
OUTPUT_DIR="$WORKSPACE_ROOT/stage4_decomp/output/commit_splits/{{working_branch}}"

cd "$REPO_PATH"
git checkout {{working_branch}}

# Run the metadata generation script with absolute paths
python "$WORKSPACE_ROOT/deepcommit/tools/milestone_balancing/generate_split_metadata.py" \
    --repo-path "$REPO_PATH" \
    --branch-name {{working_branch}} \
    --repo-src-dirs {{repo_src_dirs}} \
    --exclude-patterns "{{exclude_patterns}}" \
    --output-dir "$OUTPUT_DIR"

# Verify output was created in correct location
if [[ -f "$OUTPUT_DIR/METADATA.json" ]]; then
    echo "✅ METADATA.json created at: $OUTPUT_DIR/METADATA.json"
else
    echo "❌ ERROR: METADATA.json not found at expected location!"
    exit 1
fi
```

The script will automatically:
- Analyze all commits on the branch
- Separate src commits from integration test commit (at most ONE)
- Calculate source directory LOC statistics for each commit (respecting exclude patterns)
- Generate METADATA.json in the format expected by Main Agent (see Main Agent prompt for complete schema)

**METADATA.json format** (MUST follow this exact structure - Main Agent expects `split_result.src_commits`):

⚠️ **IMPORTANT**: Always use `split_result.src_commits`, NOT top-level `commits` or `split_commits`. This is the standardized format.

```json
{
  "original_commit": {
    "sha": "1812eac36019be19cb02f181aa21d133ba9fe219",
    "short_sha": "1812eac",
    "message": "Original commit message",
    "...": "..."
  },
  "split_result": {
    "src_commits": [
      {
        "sha": "abc123d...",
        "short_sha": "abc123d",
        "message": "feat: ...",
        "src_stats": {"total_loc": 401},
        "patch_file": "patches/abc123d.patch",
        "files": ["src/module/file1.py", "src/module/file2.py"]
      }
    ],
    "integration_test_commit": {
      "sha": "test123...",
      "short_sha": "test123",
      "message": "test: ...",
      "src_stats": {"total_loc": 0},
      "patch_file": "patches/test123.patch"
    },
    "dependency_edges": [
      ["abc123d", "def456e"]
    ]
  }
}
```

**Field naming conventions** (use consistently):
- Use `sha` for full commit hash, `short_sha` for 7-char hash
- `src_commits` MUST be inside `split_result`, never at top level
- `integration_test_commit`: May be null; at most ONE per original commit
- `dependency_edges`: Format [source, target] means target depends on source

**Field explanations**:
- Commit `src_loc` is `src_stats.total_loc` (additions + deletions in source directories only), excluding files matching exclude patterns. Calculated by the metadata script respecting `{{repo_src_dirs}}` and `{{exclude_patterns}}` parameters.
- Integration test commit typically has `src_loc=0` since tests are outside source directories
- `dependency_edges`: **Format [source, target] means target depends on source**. Edge direction: source → target (target depends on source).

Output:
```
✅ Generated metadata: $OUTPUT_DIR/METADATA.json
   - Src commits: N
   - Integration test commit: Yes/No
```

The script generates METADATA.json with an empty `dependency_edges` array.

**c) Analyze and populate dependency_edges**:

After generating METADATA.json, analyze actual code dependencies between split commits and populate `split_result.dependency_edges[]`:

**Add a dependency edge [A, B] (B depends on A) ONLY when**:
- Commit B imports/calls symbols (classes, functions, constants) newly defined in commit A
- Commit B modifies code regions that commit A also modifies (overlapping hunks)
- Commit B's functionality logically requires commit A's changes to exist first

**Do NOT add edge when**:
- Commits modify different files with no code relationship
- The only reason is "A was created before B" (generation order ≠ code dependency)
- Commits are functionally independent (e.g., A adds `auth.py`, B adds `cache.py` with no shared code)

```bash
# Edit METADATA.json to add actual dependencies
vim "$OUTPUT_DIR/METADATA.json"

# Format: [source_sha, target_sha] means target depends on source
# Example: ["abc123d", "def456e"] means commit def456e depends on commit abc123d
# Edge direction: source -> target (target depends on source)
```

**Analysis steps**:
1. For each pair of commits, check if later commit imports/uses symbols from earlier commit
2. Check for overlapping file modifications
3. Only add edges where real code dependency exists
4. Result may be: linear chain, partial dependencies, or no dependencies (all independent)

---

##### Notes

- **Safety**: Always work on a new branch, never modify the original commit directly
- **Autonomy**: The agent should make intelligent decisions based on code analysis, not just follow rigid rules
- **Flexibility**: Adjust the plan if unexpected issues arise during execution
- **Documentation**: Provide clear rationale for all major decisions

---

##### Success Criteria

✅ All changes from the original commit are preserved (tree SHA match)
✅ All patches compile successfully when applied sequentially (when compilation verification applies)
✅ Created {{num_commits}} feature commits with balanced src/ LOC
✅ Each commit has clear, focused purpose (indicated in message)
✅ Src/ directory LOC is relatively balanced across feature commits
✅ Test infrastructure in Commit 1
✅ Unit tests paired with corresponding src code
✅ At most ONE integration test commit (if exists, must be last commit)
✅ Working tree is clean after completion
✅ No empty commits created

---

**End of Subagent Prompt Template**
