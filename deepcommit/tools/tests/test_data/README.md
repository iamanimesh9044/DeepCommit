# Test Data for milestone_internal_conflict_analyzer

This directory contains test data for unit tests.

## Files

- **commit_dag.json**: Commit dependency graph for urllib3 (v2.0.6 to v2.3.0)
  - Source: `DATA/github_data/archive/urllib3_urllib3_2.0.6_2.3.0/commit_dag/commit_dag/commit_dag.json`
  - Contains commit nodes and their dependency edges

- **milestones.csv**: 15 milestones (M1-M15) for urllib3 development
  - Source: `DATA/result/test_full_dag_v2_004_anlaysis/milestones.csv`
  - Contains milestone metadata, commits, and metrics

- **dependencies.csv**: Milestone dependency graph
  - Source: `DATA/result/test_full_dag_v2_004_anlaysis/dependencies.csv`
  - Contains inter-milestone dependencies with metadata

## Usage

These files are used by `test_milestone_internal_conflict_analyzer.py` to run tests with real data scenarios.

## Update

To update test data, copy the latest files from the source directories listed above.
