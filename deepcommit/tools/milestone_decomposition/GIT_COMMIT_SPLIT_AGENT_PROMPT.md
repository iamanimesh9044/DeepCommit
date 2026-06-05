# Git Commit Split Agent Prompt

> ⚠️ **DEPRECATED / not on active pipeline path** — No Python code imports this prompt. The active pipeline (`main_multi_stage.py`) does not invoke this commit-splitting flow. Kept as a standalone design / historical reference.

## Purpose
This prompt is designed for an autonomous agent to split a large squashed git commit into multiple smaller, logically organized commits. The agent should analyze the commit content, create a splitting plan, and execute the split operation independently.

---

## Task Overview

You are tasked with splitting a large git commit into multiple smaller, logical commits that are easier to review and understand.

---

## Input Parameters

The following parameters will be provided when this prompt is invoked:

- **Repository Path**: `{repo_path}` - The git repository location
- **Target Commit**: `{target_commit}` - The commit SHA to be split
- **Focus Areas**: `{focus_areas}` - Specific directories or file patterns that require fine-grained splitting (e.g., "src/" directory)
- **Size Target**: `{size_target}` - Target maximum lines per commit (default: 300-500 lines)
- **Working Branch**: `{working_branch}` - The branch name to create for split commits (default: "split-{target_commit}")
- **Output Path**: `{output_path}` - Directory where patch files and documentation will be generated

---

## Task Requirements

### 1. Analysis Phase

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
   - Files that should be committed together for logical coherence

**e) Map test files to source code**:
   - Identify which test files correspond to which source files
   - Look for patterns: `test_foo.py` ↔ `foo.py`, `test/path/file.test.js` ↔ `src/path/file.js`
   - **Analyze test file structure**: A single test file may contain tests for multiple modules/classes
     - Example: `test_connection.py` may have `TestHTTPConnection`, `TestHTTPSConnection`, `TestProxyConnection`
     - Map which test classes/functions correspond to which implementation code
   - Note test files that cover multiple source files (integration tests)

### 2. Planning Phase

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

### 3. Execution Phase

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

### 4. Validation Phase

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

## Important Guidelines

### Technical Best Practices

- **Use `git add -p` for partial file staging**: When a single file contains multiple logical changes
- **Preserve atomic functionality**: Each commit should represent a complete, logical unit of change
- **Commit message format**: Use clear, descriptive messages following conventional commit style
- **Handle binary files carefully**: Binary files cannot be partially staged
- **Watch for ignored files**: Some files may need `git add -f` if they're in .gitignore

### Splitting Strategies

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

### Quality Checks

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

## Final Output:

After completing the split, generate patch files for all commits as the final deliverable.

### Output Directory Structure

Create the following directory structure in `{output_path}/split-{target_commit_short}/`:

```
split-{target_commit_short}/
├── METADATA.json                      # Machine-readable metadata (complete information)
├── COMMITS.txt                        # Commit list in application order (SHA + message)
└── patches/                           # Patch files directory
    ├── {commit_sha_1}.patch          # Named by 7-char commit SHA
    ├── {commit_sha_2}.patch
    └── ...
```

This is the minimal, essential output. No additional documentation or scripts needed.

### Required Steps

**a) Generate patch files**:
```bash
cd {repo_path}
git checkout {working_branch}

# Create output directory
mkdir -p {output_path}/split-{target_commit_short}/patches

# Get list of commits in order
COMMITS=($(git rev-list --reverse {parent_commit}..{working_branch}))

# Generate patches named by 7-char commit SHA
for commit in "${COMMITS[@]}"; do
    commit_short=$(git rev-parse --short "$commit")
    git format-patch -1 "$commit" \
        --stdout > {output_path}/split-{target_commit_short}/patches/${commit_short}.patch
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
    > {output_path}/split-{target_commit_short}/COMMITS.txt
```

Example output:
```
4f1ee4f emscripten: add build infrastructure and configuration
11e1157 emscripten: add test infrastructure
2da00caa emscripten: add comprehensive test suite
5098905 refactor(src): generalize connection type for pluggable backends
806c64e emscripten(src): add module structure and request types
650da9a emscripten(src): integrate platform detection and injection
7a6dcb9 emscripten(src): implement HTTP/HTTPS connection classes
cbd0702 emscripten(src): add JavaScript fetch worker for streaming
86bd9c4 emscripten(src): add fetch utilities and error handling
23f065a emscripten(src): implement streaming request handling
84c1f31 emscripten(src): add request sender and readiness checks
eb610da emscripten(src): implement response wrapper and stream handling
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
  "focus_areas": {...},
  "verification": {...}
}
```

---

## Notes

- **Safety**: Always work on a new branch, never modify the original commit directly
- **Autonomy**: The agent should make intelligent decisions based on code analysis, not just follow rigid rules
- **Flexibility**: Adjust the plan if unexpected issues arise during execution
- **Documentation**: Provide clear rationale for all major decisions

---

## Success Criteria

✅ All changes from the original commit are preserved
✅ Each new commit has a clear, focused purpose
✅ Commit sizes are appropriate (generally < {size_target} lines)
✅ Focus areas receive fine-grained attention
✅ Working tree is clean after completion
✅ Commit messages are clear and descriptive
✅ No empty commits created
✅ Related changes stay together logically
