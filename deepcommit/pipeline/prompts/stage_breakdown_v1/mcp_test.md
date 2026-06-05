# MCP Tool Test

## Objective

Test that MCP tools are accessible and functional by making simple tool calls.

## Task 1: Test Repository Entity Tools

1. Use `mcp__git_analysis__search_entities` to find the 2 most recent commits
   - Parameters: `types: ["commit"]`, `limit: 2`, `order_by: "created_at"`

2. Use `mcp__git_analysis__get_commit_context` on the first commit to get detailed information
   - Parameters: `sha: <commit_sha>`, `expand: ["stats", "files"]`

## Task 2: Test Commit DAG Dependency Tool

3. Create a minimal test CSV file at `stage0_mcp_test/input/test_milestones.csv` with just headers:
   ```csv
   id,title,commits
   ```

4. Use `mcp__git_analysis__check_dependency` to check dependency between the 2 commits
   - Parameters:
     - `milestone_csv_path: "stage0_mcp_test/input/test_milestones.csv"`
     - `commits_a: [<first_commit_sha>]`
     - `commits_b: [<second_commit_sha>]`

   Expected result: The tool should tell you if commit A depends on B, B depends on A, both, or neither.

## Output Requirements

Save the combined test results as JSON to `{{test_result}}` with the following structure:

```json
{
  "test_status": "success",
  "tools_tested": ["search_entities", "get_commit_context", "check_dependency"],
  "results": {
    "search_entities": {
      "success": true,
      "commits_found": 2,
      "commit_shas": ["sha1", "sha2"]
    },
    "get_commit_context": {
      "success": true,
      "commit_sha": "...",
      "commit_message": "...",
      "files_changed": 123,
      "additions": 10,
      "deletions": 5
    },
    "check_dependency": {
      "success": true,
      "dependency_type": "unidirectional|bidirectional|none",
      "direction": "A → B | B → A | A ↔ B | A ∥ B",
      "a_depends_on_b": true,
      "b_depends_on_a": false
    }
  }
}
```

## Success Criteria

- All 3 MCP tools executed successfully
- Output JSON file created with correct structure
- All required fields populated
- Dependency relationship correctly identified

## Notes

- This is a simple test stage, keep it minimal
- If any tool call fails, mark that tool as failed but continue with others
- Do not spend time on deep analysis, just verify tool accessibility and basic functionality
