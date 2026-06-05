# Code Maat Adaptive Parameters

This module provides adaptive parameter calculation for Code Maat analyses based on repository characteristics.

## How It Works

The `AdaptiveParamsCalculator` analyzes the Code Maat log file to determine:
- Total number of commits
- Number of files modified
- Frequency distribution of file changes
- Total change volume

Based on these metrics, it calculates optimal parameters for each analysis type.

## Adaptive Algorithms

### Hotspots (Revisions) - `-r` parameter

**Goal**: Cover 70-90% of modified files depending on repository size

**Algorithm**:
```
if commits < 50:
    coverage = 90%  # Small repos: cover more files
elif commits < 150:
    coverage = 80%  # Medium repos: balanced coverage
else:
    coverage = 70%  # Large repos: focus on top files

rows = min(max(files_count × coverage, 10), 100)
```

**Example**:
- Small repo (30 commits, 20 files) → `-r 18` (90% coverage)
- Medium repo (69 commits, 36 files) → `-r 28` (78% coverage)
- Large repo (500 commits, 200 files) → `-r 100` (50% coverage, capped)

### Coupling - `-r` and `-i` parameters

**Goal**: Find significant coupling relationships while reducing noise

**Algorithm**:
```
active_files = count(files with >2 modifications)

if commits < 50:
    rows_ratio = 80%
    threshold = 50%  # Strict: reduce noise
elif commits < 150:
    rows_ratio = 70%
    threshold = 40%  # Balanced
else:
    rows_ratio = 50%
    threshold = 30%  # Permissive: find more relationships

rows = min(max(active_files × rows_ratio, 10), 50)
```

**Example**:
- Small repo (30 commits) → `-r 12 -i 50` (strict coupling)
- Medium repo (69 commits) → `-r 14 -i 40` (balanced)
- Large repo (500 commits) → `-r 40 -i 30` (permissive)

### Entity Churn - `-r` parameter

**Goal**: Focus on files with significant activity

**Algorithm**:
```
significant_files = count(files with >3 modifications)

# If too few, relax to >1 modifications
if significant_files < 10:
    significant_files = count(files with >1 modifications)

rows = min(max(significant_files × 75%, 10), 100)
```

**Example**:
- 16 significant files → `-r 12` (75% coverage)
- 80 significant files → `-r 60` (75% coverage)
- 200 significant files → `-r 100` (50% coverage, capped)

## Usage

### Automatic (Default)

```python
from deepcommit.preprocess.code_maat import CodeMaatAnalyzer

analyzer = CodeMaatAnalyzer(
    log_file="codemaat_src.log",
    output_dir="analysis/",
    use_adaptive_params=True,  # Default
    verbose=True,
)

# Parameters are calculated automatically
results = analyzer.run_all_analyses()
```

### Manual Override

```python
analyzer = CodeMaatAnalyzer(
    log_file="codemaat_src.log",
    output_dir="analysis/",
    use_adaptive_params=False,  # Disable adaptive params
)

# Specify parameters manually
analyzer.run_hotspots(rows=50)
analyzer.run_coupling(rows=30, min_coupling=40)
analyzer.run_entity_churn(rows=50)
```

### Standalone Calculator

```python
from deepcommit.preprocess.code_maat import AdaptiveParamsCalculator

calc = AdaptiveParamsCalculator("codemaat_src.log")

# Get all parameters
params = calc.calculate_all_params()
print(params)
# {
#   'hotspots': {'rows': 28},
#   'coupling': {'rows': 14, 'min_coupling': 40},
#   'entity_churn': {'rows': 12},
#   'stats': {...}
# }

# Get explanation
print(calc.explain_params())
```

## CLI Usage

The adaptive parameters are used by default in the preprocessing command:

```bash
# Adaptive parameters calculated automatically
python -m deepcommit.cli preproc codemaat urllib3_urllib3:2.0.6_2.3.0 \
  --repo-src-dirs src/ \
  --verbose

# Output shows calculated parameters:
# ======================================================================
# Adaptive Code Maat Parameters
# ======================================================================
# Repository Statistics:
#   Commits: 69
#   Files modified: 36
#   Total file changes: 157
#   Avg files per commit: 2.3
#
# Calculated Parameters:
#   Hotspots (-r): 28
#     → Covers ~78% of modified files
#   Coupling (-r): 14
#   Coupling (-i): 40%
#     → Minimum 40% coupling threshold
#   Entity Churn (-r): 12
#     → Focuses on high-churn files
# ======================================================================
```

## Output

The calculated parameters are saved in the JSON summary:

```json
{
  "hotspots": {...},
  "coupling": {...},
  "temporal_churn": {...},
  "entity_churn": {...},
  "_metadata": {
    "adaptive_params_used": true,
    "parameters": {
      "hotspots": {"rows": 28},
      "coupling": {"rows": 14, "min_coupling": 40},
      "entity_churn": {"rows": 12},
      "stats": {
        "commits_count": 69,
        "files_count": 36,
        "total_changes": 157,
        "avg_files_per_commit": 2.28
      }
    }
  }
}
```

## Benefits

1. **Optimal Coverage**: Automatically adjusts to repository size
2. **Noise Reduction**: Higher thresholds for small repos, lower for large repos
3. **Consistent Quality**: 70-90% coverage target across all repository sizes
4. **Reproducible**: Same repository → same parameters
5. **Transparent**: Parameters and rationale saved in metadata

## Design Principles

- **Conservative for small repos**: Avoid noise by requiring higher coupling thresholds
- **Comprehensive for medium repos**: Balance between coverage and focus
- **Focused for large repos**: Concentrate on top contributors to avoid information overload
- **Bounded**: Never return extreme values (min 10, max 100 for most parameters)
- **Coverage-based**: Target percentage of active files rather than absolute numbers
