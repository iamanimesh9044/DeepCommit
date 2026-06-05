#!/usr/bin/env python3
"""
Analyze test changes in each milestone commit.

This tool analyzes each commit in milestones to identify:
- Added test functions/methods (with full pytest-style IDs)
- Modified test functions/methods
- Deleted test functions/methods

Output format includes test IDs like:
  test/test_poolmanager.py::TestPoolManager::test_pools_keyed_with_from_host
"""

import argparse
import csv
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set


@dataclass
class TestChange:
    """Represents a test function/method change."""

    test_id: str
    file_path: str
    class_name: Optional[str]
    function_name: str
    change_type: str  # 'added', 'modified', 'deleted'


@dataclass
class CommitTestChanges:
    """Test changes for a single commit."""

    sha: str  # 7-char SHA
    test_changes: Dict[str, List[str]]  # {change_type: [test_ids]}


@dataclass
class MilestoneTestChanges:
    """Test changes for a milestone."""

    milestone_id: str
    title: str
    commits: List[CommitTestChanges]
    summary: Dict[str, any]


class TestPatternExtractor:
    """Extract test patterns from git diffs."""

    # Patterns to identify test files
    TEST_FILE_PATTERNS = [
        r"^test/.*\.py$",
        r"^tests/.*\.py$",
        r".*test_.*\.py$",
    ]

    # Pattern to match test function/method definitions
    TEST_FUNC_PATTERN = re.compile(r"^[+\- ](?:    |\t)*(async\s+)?def\s+(test_\w+)\s*\(", re.MULTILINE)

    # Pattern to match class definitions
    CLASS_PATTERN = re.compile(r"^[+\- ]class\s+(Test\w*)\s*[:(]", re.MULTILINE)

    @classmethod
    def is_test_file(cls, file_path: str) -> bool:
        """Check if a file is a test file."""
        return any(re.match(pattern, file_path) for pattern in cls.TEST_FILE_PATTERNS)

    @classmethod
    def extract_tests_from_diff(cls, diff_text: str, file_path: str) -> Dict[str, Set[str]]:
        """
        Extract added/modified/deleted test IDs from diff.

        Returns:
            Dict with keys: 'added', 'modified', 'deleted'
            Values are sets of test IDs
        """
        added_tests = set()
        deleted_tests = set()
        modified_tests = set()

        # Split diff into hunks
        hunks = diff_text.split("\n@@")

        for hunk in hunks:
            # Parse the current class context
            current_class = cls._extract_class_from_hunk(hunk)

            # Find all test function definitions
            for match in cls.TEST_FUNC_PATTERN.finditer(hunk):
                line_prefix = match.group(0)[0]  # '+', '-', or ' '
                test_name = match.group(2)

                # Generate test ID
                test_id = cls._generate_test_id(file_path, current_class, test_name)

                if line_prefix == "+":
                    added_tests.add(test_id)
                elif line_prefix == "-":
                    deleted_tests.add(test_id)
                else:
                    # Context line - might be modified
                    pass

        # A test is "modified" if it appears in both added and deleted
        # (This happens when the function body changes but signature stays)
        # For simplicity, we'll mark as modified if it's in the diff context

        return {"added": added_tests, "modified": modified_tests, "deleted": deleted_tests}

    @classmethod
    def _extract_class_from_hunk(cls, hunk_text: str) -> Optional[str]:
        """Extract the class name from hunk context."""
        # Look for class definition in the hunk
        lines = hunk_text.split("\n")
        current_class = None

        for line in lines:
            # Skip diff markers
            if line.startswith("+++") or line.startswith("---"):
                continue

            class_match = cls.CLASS_PATTERN.search(line)
            if class_match:
                current_class = class_match.group(1)

        return current_class

    @classmethod
    def extract_tests_from_file(cls, repo_path: str, commit_sha: str, file_path: str) -> Set[str]:
        """
        Extract all test IDs from a file at a specific commit.
        Uses AST parsing for accuracy.
        """
        try:
            # Get file content at commit
            result = subprocess.run(
                ["git", "show", f"{commit_sha}:{file_path}"], cwd=repo_path, capture_output=True, text=True, check=True
            )
            content = result.stdout

            # Parse Python AST
            import ast

            tree = ast.parse(content, filename=file_path)

            test_ids = set()

            # Only iterate over module-level nodes (not recursive walk)
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    if node.name.startswith("Test"):
                        # Find test methods in class
                        for item in node.body:
                            if isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                                if item.name.startswith("test_"):
                                    test_id = cls._generate_test_id(file_path, node.name, item.name)
                                    test_ids.add(test_id)

                elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                    # Top-level test function
                    if node.name.startswith("test_"):
                        test_id = cls._generate_test_id(file_path, None, node.name)
                        test_ids.add(test_id)

            return test_ids

        except Exception as e:
            print(f"Warning: Failed to parse {file_path} at {commit_sha}: {e}")
            return set()

    @classmethod
    def _generate_test_id(cls, file_path: str, class_name: Optional[str], func_name: str) -> str:
        """Generate pytest-style test ID."""
        if class_name:
            return f"{file_path}::{class_name}::{func_name}"
        else:
            return f"{file_path}::{func_name}"


class MilestoneTestAnalyzer:
    """Analyze test changes across milestones."""

    def __init__(self, repo_path: str, milestone_csv_path: str):
        self.repo_path = Path(repo_path)
        self.milestone_csv_path = Path(milestone_csv_path)
        self.extractor = TestPatternExtractor()

    def analyze_all_milestones(self, output_dir: Optional[str] = None) -> List[MilestoneTestChanges]:
        """Analyze all milestones and return results."""
        milestones = self._read_milestone_csv()
        results = []

        for milestone in milestones:
            print(f"\nAnalyzing {milestone['id']}: {milestone['title']}")
            result = self.analyze_milestone(milestone)
            results.append(result)

            # Save individual milestone result
            if output_dir:
                self._save_milestone_result(result, output_dir)

        return results

    def analyze_milestone(self, milestone: Dict) -> MilestoneTestChanges:
        """Analyze test changes for a single milestone."""
        milestone_id = milestone["id"]
        title = milestone["title"]
        commits = milestone["commits"].split(";") if milestone["commits"] else []

        commit_results = []
        all_test_ids_added = set()
        all_test_ids_modified = set()
        all_test_ids_deleted = set()

        for commit_sha in commits:
            commit_sha = commit_sha.strip()
            if not commit_sha:
                continue

            print(f"  Analyzing commit {commit_sha[:7]}...")
            commit_result = self._analyze_commit(commit_sha)
            commit_results.append(commit_result)

            # Aggregate stats
            all_test_ids_added.update(commit_result.test_changes.get("added", []))
            all_test_ids_modified.update(commit_result.test_changes.get("modified", []))
            all_test_ids_deleted.update(commit_result.test_changes.get("deleted", []))

        # Create summary
        summary = {
            "total_commits": len(commit_results),
            "total_tests_added": len(all_test_ids_added),
            "total_tests_modified": len(all_test_ids_modified),
            "total_tests_deleted": len(all_test_ids_deleted),
            "all_tests_added": sorted(all_test_ids_added),
            "all_tests_modified": sorted(all_test_ids_modified),
            "all_tests_deleted": sorted(all_test_ids_deleted),
        }

        return MilestoneTestChanges(milestone_id=milestone_id, title=title, commits=commit_results, summary=summary)

    def _analyze_commit(self, commit_sha: str) -> CommitTestChanges:
        """Analyze test changes in a single commit."""
        # Get changed files
        changed_files = self._get_changed_files(commit_sha)
        test_files = [f for f in changed_files if self.extractor.is_test_file(f["path"])]

        # Analyze each test file
        added_tests = set()
        modified_tests = set()
        deleted_tests = set()

        for file_info in test_files:
            file_path = file_info["path"]
            status = file_info["status"]

            if status == "A":
                # File added - all tests are new
                tests = self.extractor.extract_tests_from_file(str(self.repo_path), commit_sha, file_path)
                added_tests.update(tests)

            elif status == "D":
                # File deleted - all tests are deleted
                # Get tests from parent commit
                parent_sha = self._get_parent_commit(commit_sha)
                if parent_sha:
                    tests = self.extractor.extract_tests_from_file(str(self.repo_path), parent_sha, file_path)
                    deleted_tests.update(tests)

            elif status == "R":
                # File renamed - compare old and new paths
                result = self._compare_renamed_file(commit_sha, file_info)
                added_tests.update(result["added"])
                modified_tests.update(result["modified"])
                deleted_tests.update(result["deleted"])

            elif status in ("M", "C"):
                # File modified or copied - compare before and after
                result = self._compare_test_file(commit_sha, file_path)
                added_tests.update(result["added"])
                modified_tests.update(result["modified"])
                deleted_tests.update(result["deleted"])

        return CommitTestChanges(
            sha=commit_sha[:7],  # Use 7-char SHA
            test_changes={
                "added": sorted(added_tests),
                "modified": sorted(modified_tests),
                "deleted": sorted(deleted_tests),
            },
        )

    def _compare_renamed_file(self, commit_sha: str, file_info: Dict[str, str]) -> Dict[str, Set[str]]:
        """Compare renamed test file, handling both old and new paths."""
        parent_sha = self._get_parent_commit(commit_sha)
        if not parent_sha:
            return {"added": set(), "modified": set(), "deleted": set()}

        old_path = file_info.get("old_path")
        new_path = file_info["path"]

        if not old_path:
            # Fallback to regular comparison if old_path is missing
            return self._compare_test_file(commit_sha, new_path)

        # Get tests from old path (before) and new path (after)
        tests_before = self.extractor.extract_tests_from_file(str(self.repo_path), parent_sha, old_path)
        tests_after = self.extractor.extract_tests_from_file(str(self.repo_path), commit_sha, new_path)

        # For renamed files, we need to compare test function names, not full IDs
        # because the file path changed
        def get_test_func_name(test_id: str) -> str:
            """Extract function name from test ID (last component after ::)"""
            return test_id.split("::")[-1]

        tests_before_names = {get_test_func_name(t): t for t in tests_before}
        tests_after_names = {get_test_func_name(t): t for t in tests_after}

        # Tests added in new file
        added_func_names = set(tests_after_names.keys()) - set(tests_before_names.keys())
        added = {tests_after_names[name] for name in added_func_names}

        # Tests deleted from old file
        deleted_func_names = set(tests_before_names.keys()) - set(tests_after_names.keys())
        deleted = {tests_before_names[name] for name in deleted_func_names}

        # Tests that exist in both (by function name) but might be modified
        common_func_names = set(tests_before_names.keys()) & set(tests_after_names.keys())
        common_tests_new_ids = {tests_after_names[name] for name in common_func_names}
        modified = self._identify_modified_tests(commit_sha, new_path, common_tests_new_ids)

        return {"added": added, "modified": modified, "deleted": deleted}

    def _compare_test_file(self, commit_sha: str, file_path: str) -> Dict[str, Set[str]]:
        """Compare test file before and after commit."""
        parent_sha = self._get_parent_commit(commit_sha)
        if not parent_sha:
            return {"added": set(), "modified": set(), "deleted": set()}

        # Get tests before and after
        tests_before = self.extractor.extract_tests_from_file(str(self.repo_path), parent_sha, file_path)
        tests_after = self.extractor.extract_tests_from_file(str(self.repo_path), commit_sha, file_path)

        added = tests_after - tests_before
        deleted = tests_before - tests_after

        # Tests that exist in both but might be modified
        # We need to check the diff to see if they were actually changed
        common_tests = tests_before & tests_after
        modified = self._identify_modified_tests(commit_sha, file_path, common_tests)

        return {"added": added, "modified": modified, "deleted": deleted}

    def _identify_modified_tests(self, commit_sha: str, file_path: str, common_tests: Set[str]) -> Set[str]:
        """Identify which common tests were actually modified."""
        try:
            # Get the diff for this file
            result = subprocess.run(
                ["git", "show", commit_sha, "--", file_path],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                check=True,
            )
            diff_text = result.stdout

            # Simple heuristic: if a test function name appears in the diff
            # with changes (not just context), consider it modified
            modified = set()
            for test_id in common_tests:
                # Extract function name from test ID
                func_name = test_id.split("::")[-1]

                # Check if this function appears in added/removed lines
                pattern = rf"^[+-].*\bdef\s+{re.escape(func_name)}\b|^[+-].*\b{re.escape(func_name)}\("
                if re.search(pattern, diff_text, re.MULTILINE):
                    modified.add(test_id)

            return modified

        except Exception as e:
            print(f"Warning: Failed to check modifications in {file_path}: {e}")
            return set()

    def _get_changed_files(self, commit_sha: str) -> List[Dict[str, str]]:
        """Get list of changed files in a commit."""
        try:
            result = subprocess.run(
                ["git", "show", "--name-status", "--format=", commit_sha],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                check=True,
            )

            files = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    status_code = parts[0]
                    status = status_code[0]  # A, M, D, R, C, etc.

                    # Handle renamed/copied files: R095 old_path new_path
                    if status in ("R", "C") and len(parts) >= 3:
                        old_path = parts[1]
                        new_path = parts[2]
                        # For renamed files, we track both old and new paths
                        files.append(
                            {
                                "status": status,
                                "path": new_path,
                                "old_path": old_path,
                                "similarity": status_code[1:] if len(status_code) > 1 else "100",
                            }
                        )
                    else:
                        # Regular files: M/A/D/T
                        path = parts[1]
                        files.append({"status": status, "path": path})

            return files

        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to get changed files for {commit_sha}: {e}")
            return []

    def _get_parent_commit(self, commit_sha: str) -> Optional[str]:
        """Get parent commit SHA."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", f"{commit_sha}^"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def _read_milestone_csv(self) -> List[Dict]:
        """Read milestone CSV file."""
        milestones = []
        with open(self.milestone_csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                milestones.append(row)
        return milestones

    def _save_milestone_result(self, result: MilestoneTestChanges, output_dir: str):
        """Save milestone result to JSON file."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        filename = f"{result.milestone_id}_test_changes.json"
        filepath = output_path / filename

        # Convert to dict
        data = {
            "milestone_id": result.milestone_id,
            "title": result.title,
            "commits": [{"sha": c.sha, "test_changes": c.test_changes} for c in result.commits],
            "summary": result.summary,
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        print(f"  Saved: {filepath}")


def main():
    parser = argparse.ArgumentParser(description="Analyze test changes in milestone commits")
    parser.add_argument("--repo-path", required=True, help="Path to git repository")
    parser.add_argument("--milestone-csv", required=True, help="Path to milestone CSV file")
    parser.add_argument("--output-dir", required=True, help="Output directory for results")
    parser.add_argument("--milestone-id", help="Analyze specific milestone only")

    args = parser.parse_args()

    analyzer = MilestoneTestAnalyzer(args.repo_path, args.milestone_csv)

    if args.milestone_id:
        # Analyze single milestone
        milestones = analyzer._read_milestone_csv()
        milestone = next((m for m in milestones if m["id"] == args.milestone_id), None)
        if not milestone:
            print(f"Error: Milestone {args.milestone_id} not found")
            return 1

        result = analyzer.analyze_milestone(milestone)
        analyzer._save_milestone_result(result, args.output_dir)
    else:
        # Analyze all milestones
        results = analyzer.analyze_all_milestones(args.output_dir)

        # Create summary report
        summary_path = Path(args.output_dir) / "summary.json"
        summary_data = {
            "total_milestones": len(results),
            "milestones": [
                {
                    "id": r.milestone_id,
                    "title": r.title,
                    "commits": len(r.commits),
                    "tests_added": r.summary["total_tests_added"],
                    "tests_modified": r.summary["total_tests_modified"],
                    "tests_deleted": r.summary["total_tests_deleted"],
                }
                for r in results
            ],
        }

        with open(summary_path, "w") as f:
            json.dump(summary_data, f, indent=2)

        print(f"\n✅ Summary saved: {summary_path}")

    return 0


if __name__ == "__main__":
    exit(main())
