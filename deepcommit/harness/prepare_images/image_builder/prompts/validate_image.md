# Docker Image Validation

Validate that Docker image `{image_name}` is correctly configured for running {test_framework} tests.

## Target

- **Repository**: `{repo_path}`
- **Language**: {language}
- **Test Framework**: {test_framework}
- **Test Directory**: `{test_dir}`
- **Test Pattern**: `{test_file_pattern}`

## Execution

Run commands inside the container:
```bash
docker run --rm -w /testbed {image_name} bash -c "<command>"
```

## Validation Steps

### 1. Environment Check

Verify the runtime environment in a single container run:
- Language runtime version and availability
- Test framework installed and importable
- Test directory exists

**Early exit**: If test framework is not installed or test directory doesn't exist, report FAIL immediately.

### 2. Test Collection & Coverage

**Step 2a: Discover all test files**

Use `find` with pattern `{test_file_pattern}` in `{test_dir}` to get the complete list of test files on filesystem.

**Step 2b: Run test collection**

Run the test collection command for {test_framework} and capture:
- Which test files were successfully collected
- Any import errors or collection failures
- Warning messages

**Step 2c: Compare and analyze gaps**

Compare filesystem test files vs collected test files:
- Identify test files that exist but were NOT collected
- For each uncollected file, determine the reason:
  - Import error (missing dependency) → fixable via environment
  - Syntax error → code issue, not environment
  - Skipped by config (e.g., `--ignore`) → intentional
  - Other errors

For each uncollected file, try to collect it individually to get the specific error message, then categorize and determine if fixable.

**Criteria**:
| Condition | Status |
|-----------|--------|
| All test files collected, `test_count > 0` | PASS |
| Some files uncollected due to **fixable** env issues (missing deps) | FAIL |
| Some files uncollected due to **intentional** skip or non-env issues | PASS |
| `test_count == 0` | FAIL |

### 3. Test Execution (if collection passed)

Run a subset of tests to verify the environment can actually execute tests.

**Execute tests using SingleStateTestRunner CLI:**

```bash
python -m harness.test_runner.single_state_runner \
    --image {image_name} \
    --output-dir {test_output_dir} \
    --language {language} \
    --test-framework {test_framework} \
    --test-dir {test_dir} \
    --timeout 300 \
    --json
```

**Analyze the result for environment issues:**

Check the output JSON and identify tests that failed/errored/skipped due to **fixable environment issues**:

| Category | How to Identify | Fixable? |
|----------|-----------------|----------|
| **Import Error** | `ModuleNotFoundError`, `ImportError` in error message | Yes - install package |
| **Missing Binary** | `FileNotFoundError: [Errno 2]`, `command not found` | Yes - install tool |
| **Permission Issue** | `PermissionError`, `Permission denied` | Yes - fix permissions |
| **Config Missing** | `ConfigError`, missing env vars, missing config files | Yes - add config |
| **Assertion Failure** | `AssertionError`, expected vs actual mismatch | No - code issue |
| **Timeout** | `TimeoutError`, test exceeded time limit | Maybe - increase timeout |
| **Skip (missing dep)** | `pytest.skip` with "requires X" message | Yes - install X |
| **Skip (platform)** | `pytest.skip` with "only on windows/linux" | No - intentional |

**For each error/failed/skipped test:**
1. Read the error message from `failed_tests` or test output
2. Categorize the root cause
3. Determine if it's fixable via environment changes
4. If fixable, add to recommendations

**Criteria**:
| Condition | Status |
|-----------|--------|
| Tests run, no env-related errors/skips | PASS |
| Tests run, some errors/skips due to **fixable** env issues | FAIL |
| Tests run, some errors/skips due to **non-fixable** issues (code bugs, platform) | PASS |
| Tests cannot run at all | FAIL |

Key distinction:
- `error` usually = environment issue → investigate
- `failed` usually = assertion failure → code issue, not env
- `skipped` = check skip reason → may be missing optional dependency

## Output

Return a JSON object:

```json
{{
    "status": "PASS | FAIL | WARNING",
    "checks": [
        {{
            "name": "environment",
            "status": "PASS | FAIL",
            "message": "Brief description",
            "details": {{
                "language_version": "...",
                "test_framework_version": "...",
                "test_dir_exists": true
            }}
        }},
        {{
            "name": "test_collection",
            "status": "PASS | FAIL | WARNING",
            "message": "Collected N/M test files (K uncollected due to env issues)",
            "details": {{
                "fs_test_files": ["list of all test files found via find"],
                "collected_test_files": ["list of successfully collected files"],
                "uncollected_files": [
                    {{
                        "file": "test/test_foo.py",
                        "reason": "import_error | syntax_error | config_skip | other",
                        "error_message": "ModuleNotFoundError: No module named 'xyz'",
                        "fixable": true
                    }}
                ],
                "test_count": N,
                "collection_command": "...",
                "missing_packages": ["xyz", "abc"]
            }}
        }},
        {{
            "name": "test_execution",
            "status": "PASS | FAIL | WARNING | SKIPPED",
            "message": "Tests executed: X passed, Y failed, Z errors (N fixable env issues)",
            "details": {{
                "success": true,
                "summary": {{"passed": X, "failed": Y, "skipped": S, "error": E, "total": T}},
                "env_issues": [
                    {{
                        "test": "test/test_foo.py::test_bar",
                        "outcome": "error | failed | skipped",
                        "category": "import_error | missing_binary | permission | config | skip_missing_dep",
                        "error_message": "ModuleNotFoundError: No module named 'xyz'",
                        "fixable": true,
                        "fix": "pip install xyz"
                    }}
                ],
                "fixable_count": N,
                "non_fixable_count": M
            }}
        }}
    ],
    "summary": {{
        "collection": {{
            "fs_test_files": M,
            "collected": N,
            "uncollected": K,
            "uncollected_fixable": F
        }},
        "execution": {{
            "passed": X,
            "failed": Y,
            "skipped": S,
            "error": E,
            "total": T,
            "env_issues_fixable": N,
            "env_issues_non_fixable": M
        }}
    }},
    "recommendations": ["pip install xyz", "other actionable fixes"]
}}
```

## Decision Criteria

| Status | Condition |
|--------|-----------|
| **PASS** | Tests executed, `env_issues_fixable == 0` (non-fixable issues are OK) |
| **FAIL** | Any **fixable** environment issue exists (collection or execution) |

**FAIL conditions (any one triggers FAIL):**
- `uncollected_fixable > 0` - some test files couldn't be collected due to missing deps
- `env_issues_fixable > 0` - some tests error/skip due to fixable env issues
- `test_count == 0` - no tests collected
- Test framework not installed

**Key principle:** If installing a package, adding a config, or fixing permissions would allow more tests to run successfully, the image is NOT ready → **FAIL**.

Execute validation now.
