#!/usr/bin/env python3
"""
Prepare SRS (Software Requirements Specification) generation prompt.

This script generates a complete prompt for SRS generation by combining:
1. SRS generation guidelines (from srs_gen_v1.tmpl)
2. Input data including:
   - Mini SRS from milestones.csv
   - Fail-to-Pass test references from f2p_tests/
   - Commit list from git repository

Output: A complete prompt file ready for LLM-based SRS generation
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Template


class SRSPromptPreparer:
    """Prepare complete prompt for SRS generation."""

    def __init__(
        self,
        work_dir: str,
        milestone_id: str,
        filter_src_only: bool = False,
        patches_dir: Optional[str] = None,
        output_dir: Optional[str] = None,
        project_root: Optional[str] = None,
        git_repo: Optional[str] = None,
        milestone_container: Optional[str] = None,
        include_new_tests: bool = True,
        suffix: Optional[str] = None,
    ):
        """
        Initialize SRS prompt preparer.

        Args:
            work_dir: Work directory (e.g., .../test_multi_stage_advanced)
            milestone_id: Milestone ID (e.g., M001)
            filter_src_only: If True, only include patches for src/ directory files
            patches_dir: Directory containing patch files (default: auto-detect from work_dir)
            output_dir: Custom output directory for SRS.md (default: work_dir/test_results/milestone_id/srs)
            project_root: Project root directory for relative path conversion (default: auto-detect)
            git_repo: Path to git repository (testbed). If not provided, defaults to work_dir/testbed
            milestone_container: Docker container name for milestone environment
            include_new_tests: If True, treat new_tests from stable_classification as fail_to_pass
            suffix: Suffix for milestones.csv (e.g., "_filtered" -> "milestones_filtered.csv")
        """
        self.work_dir = Path(work_dir)
        self.milestone_id = milestone_id
        self.filter_src_only = filter_src_only
        self.milestone_container = milestone_container
        self.include_new_tests = include_new_tests
        self.suffix = suffix or ""

        # Project root for relative path conversion
        if project_root:
            self.project_root = Path(project_root)
        else:
            # Auto-detect: assume script is in harness/gen_srs, so go up 2 levels
            self.project_root = Path(__file__).parent.parent.parent

        # Define paths (apply suffix to milestones.csv)
        self.milestones_csv = self.work_dir / f"milestones{self.suffix}.csv"
        # Use provided git_repo or fallback to work_dir/testbed
        self.git_repo = Path(git_repo) if git_repo else (self.work_dir / "testbed")
        self.testbed_path = self.git_repo  # Alias for backward compatibility
        self.f2p_tests_dir = self.work_dir / "test_data" / milestone_id / "benchmark" / "f2p_tests"
        self.output_dir = Path(output_dir) if output_dir else self.work_dir / "test_results" / milestone_id / "srs"
        self.template_path = Path(__file__).parent / "srs_gen_v1.tmpl"

        # Patches directory (auto-detect if not provided)
        if patches_dir:
            self.patches_dir = Path(patches_dir)
        else:
            # Auto-detect: assume DATA directory is 3 levels up from work_dir
            # work_dir: DATA/harness_workspace/<repo>/<test_name>
            # patches_dir: DATA/github_data/preprocessed/.../patches
            data_dir = self.work_dir.parent.parent.parent
            # Get repo name from work_dir (e.g., urllib3-docker)
            repo_dir_name = self.work_dir.parent.name.replace("-docker", "")
            # Try to find preprocessed directory
            preprocessed_base = data_dir / "github_data" / "preprocessed"
            if preprocessed_base.exists():
                # Find the matching preprocessed directory
                matching_dirs = list(preprocessed_base.glob(f"{repo_dir_name}_*"))
                if matching_dirs:
                    self.patches_dir = matching_dirs[0] / "patches"
                else:
                    self.patches_dir = None
            else:
                self.patches_dir = None

        # Validate paths
        self._validate_paths()

        # Workspace (multi-repo) mode: testbed/manifest.json exists -> testbed/<repo>/ are the git repos
        self.manifest: Optional[Dict] = None
        self._sha_repo: Dict[str, str] = {}
        manifest_path = self.testbed_path / "manifest.json"
        if manifest_path.exists():
            self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for repo, entry in self.manifest.get("milestones", {}).get(milestone_id, {}).items():
                for sha in entry.get("commits", []):
                    self._sha_repo[sha[:7]] = repo

        # Initialize f2p data attributes (populated by generate_output)
        self.f2p_tests: List[Dict] = []
        self.f2p_tests_list_file: Optional[Path] = None
        self.show_all_tests: bool = False

    def _validate_paths(self):
        """Validate that required paths exist."""
        if not self.milestones_csv.exists():
            raise FileNotFoundError(f"Milestones CSV not found: {self.milestones_csv}")
        if not self.testbed_path.exists():
            raise FileNotFoundError(f"Testbed directory not found: {self.testbed_path}")
        if not self.template_path.exists():
            raise FileNotFoundError(f"Template file not found: {self.template_path}")

    @property
    def workspace_mode(self) -> bool:
        return self.manifest is not None

    def _repo_for_commit(self, commit_sha: str) -> Optional[str]:
        return self._sha_repo.get(commit_sha[:7]) if self.workspace_mode else None

    def _git_cwd(self, commit_sha: str) -> Path:
        """Directory to run git in: the commit's repo checkout in workspace mode, else the testbed."""
        repo = self._repo_for_commit(commit_sha)
        return self.testbed_path / repo if repo else self.testbed_path

    def _read_milestone_from_csv(self) -> Dict:
        """Read milestone information from milestones.csv."""
        with open(self.milestones_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["id"] == self.milestone_id:
                    return row

        raise ValueError(f"Milestone {self.milestone_id} not found in {self.milestones_csv}")

    def get_mini_srs(self) -> str:
        """Get mini SRS from milestones.csv."""
        milestone_data = self._read_milestone_from_csv()
        mini_srs = milestone_data.get("mini_srs", "").strip()

        if not mini_srs:
            mini_srs = "(No mini SRS available for this milestone)"

        return mini_srs

    def get_milestone_metadata(self) -> Dict:
        """Get milestone metadata (title, commits range, etc.)."""
        milestone_data = self._read_milestone_from_csv()

        return {
            "title": milestone_data.get("title", "No title available"),
            "base_commit": milestone_data.get("base_commit", "N/A")[:7],
            "end_commit": milestone_data.get("end_commit", "N/A")[:7],
        }

    def get_milestone_commits(self) -> List[str]:
        """Get list of commits for this milestone from milestones.csv."""
        milestone_data = self._read_milestone_from_csv()

        # Try core_pr_commits first, then commits field
        commits_str = milestone_data.get("core_pr_commits") or milestone_data.get("commits", "")

        if not commits_str:
            print(f"Warning: No commits found for milestone {self.milestone_id}", file=sys.stderr)
            return []

        # Parse semicolon-separated commit list
        commits = [c.strip() for c in commits_str.split(";") if c.strip()]

        return commits

    def get_commit_info(self, commit_sha: str) -> Optional[Dict]:
        """
        Get detailed information for a single commit.

        Args:
            commit_sha: Commit SHA (full or short)

        Returns:
            Dict with commit info and patch, or None if commit not found
        """
        try:
            # Get commit metadata
            result = subprocess.run(
                ["git", "show", "--no-patch", "--format=%H%n%an <%ae>%n%ai%n%s%n%b", commit_sha],
                cwd=self._git_cwd(commit_sha),
                capture_output=True,
                text=True,
                check=True,
            )

            lines = result.stdout.strip().split("\n")
            if len(lines) < 4:
                print(f"Warning: Invalid git show output for {commit_sha}", file=sys.stderr)
                return None

            full_sha = lines[0]
            author = lines[1]
            date = lines[2]
            subject = lines[3]

            # Message body (everything after subject)
            message_body = "\n".join(lines[4:]).strip() if len(lines) > 4 else ""
            full_message = f"{subject}\n\n{message_body}" if message_body else subject

            # Check if patch exists (but don't include content)
            patch = self._get_commit_patch(commit_sha)
            has_patch = patch is not None and len(patch) > 0

            # Save patch to file if patches_dir is configured
            if has_patch and self.patches_dir:
                self._save_patch_to_file(full_sha[:7], patch)

            return {
                "sha": full_sha[:7],  # Short SHA
                "full_sha": full_sha,
                "author": author,
                "date": date,
                "subject": subject,
                "message": full_message,
                "has_patch": has_patch,
                "repo": self._repo_for_commit(commit_sha) or "",
            }

        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to get info for commit {commit_sha}: {e}", file=sys.stderr)
            return None

    def _get_commit_patch(self, commit_sha: str) -> str:
        """Get patch for a commit, optionally filtered by src/ directory."""
        try:
            cmd = ["git", "show", "--format=", commit_sha]

            # Add path filter if requested
            if self.filter_src_only:
                cmd.extend(["--", "src/"])

            result = subprocess.run(cmd, cwd=self._git_cwd(commit_sha), capture_output=True, text=True, check=True)

            patch = result.stdout.strip()
            return patch if patch else None

        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to get patch for commit {commit_sha}: {e}", file=sys.stderr)
            return None

    def _save_patch_to_file(self, short_sha: str, patch_content: str):
        """Save patch content to a file in patches directory."""
        if not self.patches_dir:
            return

        # Create patches directory if it doesn't exist
        self.patches_dir.mkdir(parents=True, exist_ok=True)

        # Save patch to file
        patch_file = self.patches_dir / f"{short_sha}.patch"
        with open(patch_file, "w", encoding="utf-8") as f:
            f.write(patch_content)

    def get_f2p_tests(self) -> List[Dict]:
        """Get fail-to-pass test data from stable_classification.

        If include_new_tests is True, also includes new_tests from stable_classification
        as fail_to_pass tests.
        """
        # Use classification file instead of f2p_tests metadata
        classification_path = (
            self.work_dir
            / "test_results"
            / self.milestone_id
            / "benchmark"
            / f"{self.milestone_id}_classification.json"
        )

        if not classification_path.exists():
            print(f"Warning: Classification file not found: {classification_path}", file=sys.stderr)
            return []

        # Read classification file
        with open(classification_path, "r", encoding="utf-8") as f:
            classification_data = json.load(f)

        # Get stable fail-to-pass tests
        stable_classification = classification_data.get("stable_classification", {})
        stable_f2p_tests = stable_classification.get("fail_to_pass", [])

        # Include none_to_pass (or new_tests with passed outcome) as fail_to_pass if flag is set
        if self.include_new_tests:
            # First try none_to_pass (preferred field)
            none_to_pass = stable_classification.get("none_to_pass", [])
            if none_to_pass:
                # none_to_pass is a list of test IDs (strings)
                print(
                    f"  Including {len(none_to_pass)} none_to_pass tests as fail_to_pass",
                    file=sys.stderr,
                )
                stable_f2p_tests = list(stable_f2p_tests) + none_to_pass
            else:
                # Fallback to new_tests if none_to_pass doesn't exist
                new_tests = stable_classification.get("new_tests", [])
                if new_tests:
                    # new_tests is a list of dicts: [{"test_id": "...", "end_outcome": "..."}]
                    # Only include tests where end_outcome is "passed"
                    new_test_ids = []
                    for item in new_tests:
                        if isinstance(item, dict):
                            test_id = item.get("test_id")
                            end_outcome = item.get("end_outcome", "")
                            if test_id and end_outcome == "passed":
                                new_test_ids.append(test_id)
                        elif isinstance(item, str):
                            # In case the format is just a list of strings
                            new_test_ids.append(item)
                    if new_test_ids:
                        print(
                            f"  Including {len(new_test_ids)} new_tests (passed) as fail_to_pass",
                            file=sys.stderr,
                        )
                        stable_f2p_tests = list(stable_f2p_tests) + new_test_ids

        if not stable_f2p_tests:
            print(f"Warning: No stable fail-to-pass tests found in {classification_path}", file=sys.stderr)
            return []

        # Convert test IDs to structured format
        tests = []
        repo_names = list(self.manifest.get("repos", {})) if self.manifest else []
        for test_id in stable_f2p_tests:
            # Parse test ID format: file_path::ClassName::method_name or file_path::function_name
            parts = test_id.split("::")

            if repo_names:
                # Workspace mode: ids are `{repo}::{native id}`; native ids may be non-pytest (e.g. Go `TestX`)
                repo = parts[0] if parts[0] in repo_names else ""
                rest = parts[1:] if repo else parts
                tests.append(
                    {
                        "test_id": test_id,
                        "repo": repo,
                        "source_file": rest[0] if len(rest) > 1 else None,
                        "class_name": rest[1] if len(rest) == 3 else None,
                        "function_name": rest[-1],
                        "extracted_file": None,
                    }
                )
                continue

            if len(parts) == 3:
                # Class-based test: file::Class::method
                source_file = parts[0]
                class_name = parts[1]
                function_name = parts[2]
            elif len(parts) == 2:
                # Function-based test: file::function
                source_file = parts[0]
                class_name = None
                function_name = parts[1]
            else:
                print(f"Warning: Could not parse test ID: {test_id}", file=sys.stderr)
                continue

            tests.append(
                {
                    "test_id": test_id,
                    "source_file": source_file,
                    "class_name": class_name,
                    "function_name": function_name,
                    "extracted_file": None,  # Not using extracted files anymore
                }
            )

        return tests

    def generate_output(self) -> str:
        """Generate the complete SRS generation prompt."""
        print(f"Preparing prompt for milestone {self.milestone_id}...")

        # Collect all data
        print("  - Reading mini SRS...")
        mini_srs = self.get_mini_srs()

        print("  - Reading milestone metadata...")
        metadata = self.get_milestone_metadata()

        print("  - Reading fail-to-pass tests...")
        f2p_tests = self.get_f2p_tests()

        # Save complete test list to file
        self.output_dir.mkdir(parents=True, exist_ok=True)
        f2p_tests_list_file = self.output_dir / "f2p_tests_list.txt"
        with open(f2p_tests_list_file, "w", encoding="utf-8") as f:
            for test in f2p_tests:
                f.write(test["test_id"] + "\n")
        print(f"  - Saved {len(f2p_tests)} tests to {f2p_tests_list_file}")

        # Store f2p data in instance attributes for external access
        self.f2p_tests = f2p_tests
        self.f2p_tests_list_file = f2p_tests_list_file

        # Smart display logic: show all tests in prompt if <= 10, otherwise just reference file
        if len(f2p_tests) <= 10:
            f2p_tests_preview = f2p_tests  # Show all in prompt
            self.show_all_tests = True
        else:
            f2p_tests_preview = []  # Don't show in prompt, reference file instead
            self.show_all_tests = False

        print("  - Reading commit list...")
        commit_shas = self.get_milestone_commits()

        print(f"  - Fetching {len(commit_shas)} commit patches...")
        commits = []
        for i, sha in enumerate(commit_shas, 1):
            print(f"    [{i}/{len(commit_shas)}] {sha[:7]}...", end=" ")
            commit_info = self.get_commit_info(sha)
            if commit_info:
                commits.append(commit_info)
                print("✓")
            else:
                print("✗ (skipped)")

        # Load unified template
        print("  - Rendering prompt template...")
        with open(self.template_path, "r", encoding="utf-8") as f:
            template_str = f.read()

        template = Template(template_str)

        # Define output file path for the SRS document
        output_file = self.output_dir / "SRS.md"

        # Convert paths to relative from project root (or use absolute if not possible)
        try:
            output_file_rel = output_file.relative_to(self.project_root)
        except ValueError:
            output_file_rel = output_file

        try:
            f2p_tests_dir_rel = self.f2p_tests_dir.relative_to(self.project_root)
        except ValueError:
            f2p_tests_dir_rel = self.f2p_tests_dir

        try:
            git_repo_rel = self.git_repo.relative_to(self.project_root)
        except ValueError:
            git_repo_rel = self.git_repo

        if self.patches_dir:
            try:
                patches_dir_rel = self.patches_dir.relative_to(self.project_root)
            except ValueError:
                patches_dir_rel = self.patches_dir
        else:
            patches_dir_rel = None

        # Convert f2p_tests_list_file to relative path
        try:
            f2p_tests_list_file_rel = f2p_tests_list_file.relative_to(self.project_root)
        except ValueError:
            f2p_tests_list_file_rel = f2p_tests_list_file

        # One entry per repo of the workspace (a legacy testbed without manifest is a one-repo workspace at /testbed)
        repos: List[Dict] = []
        if self.workspace_mode:
            for repo in self.manifest.get("repos", {}):
                in_repo = [c for c in commits if c["repo"] == repo]
                if in_repo:
                    repos.append({"name": repo, "path": f"/workspace/{repo}", "commits": in_repo})
        else:
            repos.append({"name": "repo", "path": "/testbed", "commits": commits})

        # Render complete prompt (data + guidelines in one template)
        prompt = template.render(
            repos=repos,
            milestone_id=self.milestone_id,
            title=metadata["title"],
            mini_srs=mini_srs,
            git_repo=str(git_repo_rel),
            start_commit=f"milestone-{self.milestone_id}-start",
            end_commit=f"milestone-{self.milestone_id}-end",
            f2p_tests=f2p_tests,
            f2p_tests_preview=f2p_tests_preview,
            f2p_test_count=len(f2p_tests),
            f2p_tests_dir=str(f2p_tests_dir_rel),
            f2p_tests_list_file=str(f2p_tests_list_file_rel),
            show_all_tests=self.show_all_tests,
            commits=commits,
            commit_count=len(commits),
            patches_dir=str(patches_dir_rel) if patches_dir_rel else "N/A",
            filter_src_only=self.filter_src_only,
            output_file=str(output_file_rel),
            milestone_container=self.milestone_container,
        )

        return prompt

    def prepare(self):
        """Main preparation process."""
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Generate output
        output_content = self.generate_output()

        # Save to file
        output_file = self.output_dir / "prompt.txt"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(output_content)

        print("\n✓ Prompt prepared successfully!")
        print(f"  Output: {output_file}")
        print(f"  Size: {len(output_content)} characters ({len(output_content.splitlines())} lines)")


def get_available_milestones(work_dir: Path) -> List[str]:
    """Get list of available milestones from configs directory."""
    configs_dir = work_dir / "configs"
    if not configs_dir.exists():
        return []

    milestones = []
    for item in sorted(configs_dir.iterdir()):
        if item.is_dir() and item.name.startswith("M"):
            milestones.append(item.name)

    return milestones


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Prepare SRS generation prompt by combining guidelines and input data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Prepare prompt for single milestone
  %(prog)s /path/to/work_dir M001

  # Prepare prompts for multiple milestones
  %(prog)s /path/to/work_dir M001 M002 M003

  # Prepare prompts for all milestones
  %(prog)s /path/to/work_dir --all

  # Filter patches to only include src/ directory
  %(prog)s /path/to/work_dir M001 --filter-src
        """,
    )
    parser.add_argument("work_dir", type=str, help="Work directory (e.g., .../test_multi_stage_advanced)")
    parser.add_argument("milestone_ids", nargs="*", help="Milestone ID(s) (e.g., M001 M002 M003)")
    parser.add_argument("--all", action="store_true", help="Prepare prompts for all available milestones")
    parser.add_argument("--filter-src", action="store_true", help="Only include patches for files under src/ directory")
    parser.add_argument(
        "--patches-dir", type=str, default=None, help="Directory containing patch files (default: auto-detect)"
    )

    args = parser.parse_args()

    work_dir = Path(args.work_dir)

    # Determine which milestones to process
    if args.all:
        milestone_ids = get_available_milestones(work_dir)
        if not milestone_ids:
            print(f"Error: No milestones found in {work_dir / 'configs'}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(milestone_ids)} milestones: {', '.join(milestone_ids)}")
    elif args.milestone_ids:
        milestone_ids = args.milestone_ids
    else:
        parser.error("Please specify milestone ID(s) or use --all")

    # Process each milestone
    total_milestones = len(milestone_ids)
    successful = 0
    failed = []

    for i, milestone_id in enumerate(milestone_ids, 1):
        print(f"\n{'=' * 70}")
        print(f"Processing milestone {i}/{total_milestones}: {milestone_id}")
        print(f"{'=' * 70}")

        try:
            preparer = SRSPromptPreparer(
                str(work_dir), milestone_id, filter_src_only=args.filter_src, patches_dir=args.patches_dir
            )
            preparer.prepare()
            successful += 1
        except FileNotFoundError as e:
            print(f"⚠ Warning: Skipping {milestone_id} - {e}", file=sys.stderr)
            failed.append((milestone_id, str(e)))
        except Exception as e:
            print(f"✗ Error processing {milestone_id}: {e}", file=sys.stderr)
            failed.append((milestone_id, str(e)))

    # Final summary
    print(f"\n{'=' * 70}")
    print("Batch preparation complete:")
    print(f"  Total milestones: {total_milestones}")
    print(f"  Successful: {successful}")
    print(f"  Failed: {len(failed)}")
    if failed:
        print("\nFailed milestones:")
        for mid, error in failed:
            print(f"  - {mid}: {error}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
