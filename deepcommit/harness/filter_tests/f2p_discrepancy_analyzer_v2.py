#!/usr/bin/env python3
"""
F2P Discrepancy Analyzer

This script analyzes the discrepancy between "Commit-Level" Fail-to-Pass (F2P) counts and
"Milestone-Level" F2P counts.

It categorizes differences into two main groups:
1. Commit Overcounts (Tests in Commit F2P but NOT in Milestone F2P)
   - Iterative: Redundant intermediate fixes.
   - Reverted: Fixed then broken again.
   - Skipped/Removed: Fixed then ended up skipped or removed at milestone.
   - Baseline Mismatch: Commit says F2P, but Milestone Start was already Pass.
   - Other: Catch-all when final outcome is unrecognized.

2. Milestone Unique (Tests in Milestone F2P but NOT in Commit F2P)
   - Other: Milestone-only F2P (e.g., env/hidden fixes, ID mismatch, missing commit data).

中文分类说明：
- Iterative：重复修复（同一测试在多个 commit 中被多次“修好”）。
- Reverted：commit 修好但里程碑最终又失败。
- Skipped/Removed：commit 修好但里程碑最终被标为 skipped 或缺失/移除。
- Baseline Mismatch：commit 认为是 Fail->Pass，但里程碑起始状态已经是 Pass。
- Other：无法识别或未覆盖的情况（commit 侧或里程碑侧的兜底）。

Usage:
    python f2p_discrepancy_analyzer.py \
        --milestones-csv /path/to/milestones.csv \
        --commit-data /path/to/commit_level_test_data \
        --milestone-data /path/to/test_data
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


class F2PAnalyzer:
    def __init__(self, milestones_csv, commit_data_dir, milestone_data_dir, use_stable=False):
        self.milestones_csv = Path(milestones_csv)
        self.commit_data_dir = Path(commit_data_dir)
        self.milestone_data_dir = Path(milestone_data_dir)
        self.use_stable = use_stable

        if not self.milestones_csv.exists():
            raise FileNotFoundError(f"Milestones CSV not found: {self.milestones_csv}")
        if not self.commit_data_dir.exists():
            raise FileNotFoundError(f"Commit data directory not found: {self.commit_data_dir}")
        if not self.milestone_data_dir.exists():
            raise FileNotFoundError(f"Milestone data directory not found: {self.milestone_data_dir}")

    def load_milestone_commits(self):
        """Load mapping of milestone_id -> list of commit_shas (ordered)."""
        milestones = {}
        with open(self.milestones_csv, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                m_id = row["id"]
                if row["commits"]:
                    commits = [c.strip() for c in row["commits"].split(";") if c.strip()]
                    milestones[m_id] = commits
        return milestones

    def load_commit_f2p(self, commit_sha):
        """Load F2P test IDs for a specific commit.

        F2P includes:
        - fail_to_pass: Tests that went from fail to pass
        - env_patch_skip_to_pass: Tests that went from [ENV-PATCH] skip to pass
        - new_tests with final_outcome="passed": New tests that pass
        """
        short_sha = commit_sha[:7]
        path = self.commit_data_dir / short_sha / "classification.json"

        if not path.exists():
            path = self.commit_data_dir / commit_sha / "classification.json"

        if not path.exists():
            return set()

        def get_ids(lst):
            """Extract test IDs from a list that may contain strings or dicts."""
            ids = set()
            for item in lst:
                if isinstance(item, dict):
                    ids.add(item.get("test_id"))
                else:
                    ids.add(item)
            return ids

        def get_passed_new_tests(lst):
            """Extract test IDs from new_tests where final_outcome is 'passed'."""
            ids = set()
            for item in lst:
                if isinstance(item, dict):
                    if item.get("final_outcome") == "passed":
                        ids.add(item.get("test_id"))
                # String items don't have outcome info, skip them
            return ids

        try:
            with open(path, "r") as f:
                data = json.load(f)
                # Use stable_classification if requested and available
                if self.use_stable:
                    cls = data.get("stable_classification", data.get("classification", {}))
                else:
                    cls = data.get("classification", {})

                f2p = set()
                # F2P in commit context means Parent(Fail/Skip) -> Commit(Pass)
                # 1. fail_to_pass
                f2p.update(get_ids(cls.get("fail_to_pass", [])))
                # 2. env_patch_skip_to_pass
                f2p.update(get_ids(cls.get("env_patch_skip_to_pass", [])))
                # 3. new_tests with final_outcome="passed"
                f2p.update(get_passed_new_tests(cls.get("new_tests", [])))

                return f2p
        except Exception:
            # print(f"Warning: Error loading {path}: {e}", file=sys.stderr)
            return set()

    def load_milestone_results(self, milestone_id):
        """
        Load final milestone status for tests.
        """
        path = self.milestone_data_dir / milestone_id / "benchmark" / f"{milestone_id}_classification.json"

        if not path.exists():
            path = self.milestone_data_dir / milestone_id / "benchmark" / "classification.json"

        if not path.exists():
            return set(), {}, set()

        try:
            with open(path, "r") as f:
                data = json.load(f)
                # Use stable_classification if requested and available
                if self.use_stable:
                    cls = data.get("stable_classification", data.get("classification", {}))
                else:
                    cls = data.get("classification", {})

                f2p_tests = set()
                final_outcomes = {}
                new_test_ids = set()

                def get_ids(lst):
                    ids = set()
                    for item in lst:
                        if isinstance(item, dict):
                            ids.add(item.get("test_id"))
                        else:
                            ids.add(item)
                    return ids

                # Milestone F2P = fail_to_pass + env_patch_skip_to_pass + new_tests(passed)
                f2p_tests.update(get_ids(cls.get("fail_to_pass", [])))
                f2p_tests.update(get_ids(cls.get("env_patch_skip_to_pass", [])))

                # Build outcome map
                for t in get_ids(cls.get("pass_to_pass", [])):
                    final_outcomes[t] = "passed"
                for t in get_ids(cls.get("fail_to_pass", [])):
                    final_outcomes[t] = "passed"
                for t in get_ids(cls.get("env_patch_skip_to_pass", [])):
                    final_outcomes[t] = "passed"

                for item in cls.get("new_tests", []):
                    if isinstance(item, dict):
                        tid = item["test_id"]
                        outcome = item.get("final_outcome", "unknown")
                        final_outcomes[tid] = outcome
                        new_test_ids.add(tid)
                        # Add new_tests with passed outcome to F2P
                        if outcome == "passed":
                            f2p_tests.add(tid)
                    else:
                        final_outcomes[item] = "unknown"
                        new_test_ids.add(item)

                for t in get_ids(cls.get("pass_to_fail", [])):
                    final_outcomes[t] = "failed"
                for t in get_ids(cls.get("fail_to_fail", [])):
                    final_outcomes[t] = "failed"
                for item in get_ids(cls.get("env_patch_skip_to_fail", [])):
                    final_outcomes[item] = "failed"

                for t in get_ids(cls.get("skipped", [])):
                    final_outcomes[t] = "skipped"
                for item in get_ids(cls.get("env_patch_skip_to_skip", [])):
                    final_outcomes[item] = "skipped"

                for t in get_ids(cls.get("removed_tests", [])):
                    final_outcomes[t] = "missing"

                return f2p_tests, final_outcomes, new_test_ids

        except Exception:
            # print(f"Error loading {path}: {e}", file=sys.stderr)
            return set(), {}, set()

    def analyze_milestone(self, m_id, commits):
        # 1. Gather all commit F2P events
        commit_f2p_events = []
        commit_f2p_unique_tests = set()

        for idx, commit in enumerate(commits):
            f2p_tests = self.load_commit_f2p(commit)
            for t in f2p_tests:
                commit_f2p_events.append((t, commit, idx))
                commit_f2p_unique_tests.add(t)

        # 2. Get Milestone outcomes
        milestone_f2p_set, final_outcomes, _new_test_set = self.load_milestone_results(m_id)

        categories = defaultdict(int)

        # --- Part A: Analyze Commit Overcounts (Commit F2P - Milestone F2P) ---

        # Group events by test_id
        events_by_test = defaultdict(list)
        for t, c, idx in commit_f2p_events:
            events_by_test[t].append((c, idx))

        for test_id, events in events_by_test.items():
            events.sort(key=lambda x: x[1])
            count = len(events)

            # Check if this test is ultimately a Milestone F2P
            is_milestone_f2p = test_id in milestone_f2p_set

            for i, (_commit, _idx) in enumerate(events):
                is_last = i == count - 1

                # Category 1: Iterative Fix (Redundant)
                # Any fix that is NOT the last one for this test is Iterative
                if not is_last:
                    categories["Iterative"] += 1
                    continue

                # Now consider the last fix for this test
                if is_milestone_f2p:
                    # It matches!
                    categories["Matched"] += 1
                else:
                    # It is NOT in Milestone F2P. Why?
                    outcome = final_outcomes.get(test_id, "missing")

                    if outcome == "passed":
                        # Category 3: Baseline Mismatch (Start was already Pass in Milestone env)
                        categories["BaseMismatch"] += 1
                    elif outcome == "failed":
                        # Category 4: Reverted (Fixed in commit, but Failed at End)
                        categories["Reverted"] += 1
                    elif outcome == "skipped":
                        # Category 5: Skipped or removed downstream
                        categories["SkippedRemoved"] += 1
                    elif outcome == "missing":
                        # Category 5: Skipped or removed downstream
                        categories["SkippedRemoved"] += 1
                    else:
                        categories["OtherCommit"] += 1

        # --- Part B: Analyze Milestone Unique (Milestone F2P - Commit F2P) ---
        milestone_unique_count = 0
        for t in milestone_f2p_set:
            if t not in commit_f2p_unique_tests:
                milestone_unique_count += 1

        categories["OtherMilestone"] = milestone_unique_count

        return {"commit_total": len(commit_f2p_events), "milestone_net": len(milestone_f2p_set), "cats": categories}

    def run(self):
        milestones = self.load_milestone_commits()

        # Print mode indicator
        if self.use_stable:
            print("📊 Using stable_classification (excluding flaky tests)")
        else:
            print("📊 Using full classification (including flaky tests)")
        print()

        # Header
        # Comm = Commit Total F2P Events
        # Mile = Milestone Net F2P Count
        # Iterative = Redundant fixes in commit stream
        # Reverted = Fixed in commit, broken in milestone
        # BaseMis = Baseline Mismatch (Start Pass vs Parent Fail)
        # SkipRem = Skipped/Removed
        # OtherC = Catch-all for commit F2P not mapping cleanly to milestone outcome
        # OtherM = Milestone F2P NOT in Commit F2P (env/hidden/ID mismatch/unknown)

        print(
            f"{'ID':<5} | {'Comm':<5} | {'Mile':<5} || {'Iterative':<9} | {'Reverted':<8} | {'BaseMis':<7} | {'SkipRem':<7} | {'OtherC':<7} || {'Matched':<7} | {'OtherM':<6}"
        )
        print("-" * 112)

        for m_id, commits in sorted(milestones.items()):
            if not commits:
                continue

            res = self.analyze_milestone(m_id, commits)
            c = res["cats"]

            print(
                f"{m_id:<5} | {res['commit_total']:<5} | {res['milestone_net']:<5} || {c['Iterative']:<9} | {c['Reverted']:<8} | {c['BaseMismatch']:<7} | {c['SkippedRemoved']:<7} | {c['OtherCommit']:<7} || {c['Matched']:<7} | {c['OtherMilestone']:<6}"
            )


def main():
    parser = argparse.ArgumentParser(description="Analyze discrepancy between Commit F2P and Milestone F2P")
    parser.add_argument("--milestones-csv", required=True, help="Path to milestones.csv")
    parser.add_argument(
        "--commit-data", required=True, help="Directory containing commit-level classification.json files"
    )
    parser.add_argument("--milestone-data", required=True, help="Directory containing milestone-level test results")
    parser.add_argument(
        "--use-stable",
        action="store_true",
        help="Use stable_classification (excluding flaky tests) instead of full classification",
    )

    args = parser.parse_args()

    analyzer = F2PAnalyzer(args.milestones_csv, args.commit_data, args.milestone_data, use_stable=args.use_stable)
    analyzer.run()


if __name__ == "__main__":
    main()
