# Commit Analysis Tools

## calculate_commits_src_loc

Calculate source lines of code (src_loc) for given commits, with intelligent filtering to distinguish between source code and other files (tests, configs, docs, etc.).

### Purpose

Solves the problem of accurately estimating the actual source code changes in commits by filtering out:
- Test files (test/, spec/, __tests__/)
- Configuration files (.eslintrc, package.json, etc.)
- Documentation files (README.md, CHANGELOG, docs/)
- Build artifacts and scripts (noxfile.py, Makefile, etc.)

### Usage

#### Python API

```python
from deepcommit.tools.commit_analysis import calculate_commits_src_loc

# Calculate src_loc for specific commits
result = calculate_commits_src_loc(
    commits=["1812eac", "abc123"],
    repo_path="/path/to/repo",
    path_prefix="src/"  # Only count files under src/
)

print(f"Total src_loc: {result['total_src_loc']}")
print(f"Total additions: {result['total_additions']}")
print(f"Total deletions: {result['total_deletions']}")
```

#### MCP Tool (via Claude Code or other MCP clients)

```python
# The tool is available as 'calculate_commits_src_loc_tool' in the MCP server
# It automatically uses the REPO_SRC_DIRS environment variable for path filtering

# Example: Calculate src_loc for commits
result = calculate_commits_src_loc_tool(
    commits=["1812eac", "abc123"]
)
# This will use REPO_SRC_DIRS (e.g., "src/urllib3") to filter source files
```

### Parameters

#### MCP Tool Parameters
- **commits** (List[str]): List of commit SHAs to analyze (can be full or 7-char SHAs)

#### Python API Parameters
- **commits** (List[str]): List of commit SHAs to analyze (can be full or 7-char SHAs)
- **repo_path** (str): Path to the git repository (bare or regular)
- **path_prefix** (Optional[str]): Path prefix to filter files (e.g., "src/", "lib/")
  - Default: `None` (counts all files)
  - Set to a specific path to filter by directory

### Return Value

```python
{
    "total_src_loc": int,        # Total source code lines changed
    "total_additions": int,      # Total lines added
    "total_deletions": int,      # Total lines deleted
    "valid_commits": int,        # Number of valid commits
    "invalid_commits": int,      # Number of commits that don't exist
    "path_prefix": str,          # Path prefix used for filtering
    "commits": [                 # Per-commit details
        {
            "sha": str,          # Short SHA (7 chars)
            "full_sha": str,     # Full SHA
            "exists": bool,      # Whether commit exists
            "additions": int,    # Lines added
            "deletions": int,    # Lines deleted
            "total": int         # Total lines changed
        }
    ]
}
```

### Real-World Example

For commit `1812eac` (Add native Emscripten support in urllib3):

```python
# With src/ filter
result = calculate_commits_src_loc(
    commits=["1812eac"],
    repo_path="DATA/github_data/repos/urllib3_urllib3",
    path_prefix="src/"
)
# Result: 1102 LOC (source code only)

# Without filter
result = calculate_commits_src_loc(
    commits=["1812eac"],
    repo_path="DATA/github_data/repos/urllib3_urllib3",
    path_prefix=None
)
# Result: 2458 LOC (all files)

# Analysis:
# - Total changes: 2458 LOC
# - Source code: 1102 LOC (44.8%)
# - Excluded: 1356 LOC (55.2%)
#   - Test files: ~948 LOC
#   - Config files: ~128 LOC
#   - Build scripts: ~106 LOC
#   - Documentation: ~174 LOC
```

### Benefits

1. **Accurate estimation**: Get the true size of source code changes without noise from tests, configs, and docs
2. **Flexible filtering**: Choose what to include based on your needs
3. **Batch processing**: Analyze multiple commits at once
4. **Error handling**: Gracefully handles non-existent commits

### Environment Variables

- `DEEPCOMMIT_REPO_DIR`: Default repository path (used by MCP tool)
- `REPO_SRC_DIRS`: Default path prefix for filtering (default: "src/")

### Implementation Details

This tool uses `LocalGitRepo.get_commit_loc()` which:
- Uses pygit2 for efficient git operations
- Calculates line-level diffs (additions and deletions)
- Supports path prefix filtering at the file level
- Works with both bare and regular repositories
