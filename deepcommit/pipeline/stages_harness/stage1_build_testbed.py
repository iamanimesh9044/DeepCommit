"""
Stage 1: Build Testbed

Agent that builds the milestone testbed using build_milestone_testbed.py.
The agent can intelligently modify milestones.csv and dependencies.csv
to fix build errors.

Expected inputs in workspace:
- milestones.csv: Milestone definitions
- dependencies.csv: Dependency graph
- config/{repo_range}.yaml: Repository configuration
- source_repo/: Git repository (processed repo)

Expected outputs:
- testbed/: Git repository with milestone tags
- metadata.json: Build metadata
"""

from __future__ import annotations

import csv
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..base_stage import BaseStage

if TYPE_CHECKING:
    pass


class Stage1BuildTestbed(BaseStage):
    """Stage 1: Build Testbed - creates milestone testbed via agent."""

    def _setup_input_data(self) -> None:
        """Setup input data for build testbed stage.

        Verifies that required input files exist:
        - milestones.csv
        - dependencies.csv
        - config file
        - source repository
        """
        self.logger.info("Setting up input data for build testbed stage...")

        workspace = Path(self.ctx.workspace_root)

        # Check milestones.csv
        milestones_path = workspace / "milestones.csv"
        if milestones_path.exists():
            self.logger.info("  ✓ Found: milestones.csv")
        else:
            self.logger.warning("  ✗ Missing: milestones.csv")

        # Check dependencies.csv
        dependencies_path = workspace / "dependencies.csv"
        if dependencies_path.exists():
            self.logger.info("  ✓ Found: dependencies.csv")
        else:
            self.logger.warning("  ✗ Missing: dependencies.csv")

        # Check config file
        config_path = workspace / "config" / f"{self.ctx.repo_range}.yaml"
        if config_path.exists():
            self.logger.info(f"  ✓ Found: config/{self.ctx.repo_range}.yaml")
        else:
            # Try alternative config naming
            alt_config = list((workspace / "config").glob("*.yaml")) if (workspace / "config").exists() else []
            if alt_config:
                self.logger.info(f"  ✓ Found config: {alt_config[0].name}")
            else:
                self.logger.warning("  ✗ Missing: config directory or config file")

        # Check source repo
        source_repo_path = workspace / "source_repo"
        if source_repo_path.exists():
            self.logger.info("  ✓ Found: source_repo/")
        else:
            self.logger.warning("  ✗ Missing: source_repo/")

    def _validate_outputs(self) -> Optional[str]:
        """Validate build testbed stage outputs.

        Checks that:
        - testbed/ directory exists with .git
        - metadata.json was created
        - milestones.csv and dependencies.csv are valid (not corrupted)

        Returns:
            Error message if validation fails, None if success
        """
        self.logger.info("Validating build testbed outputs...")

        workspace = Path(self.ctx.workspace_root)
        errors = []

        # Check testbed directory
        testbed_path = workspace / "testbed"
        if not testbed_path.exists():
            errors.append("testbed/ directory not found")
        elif not (testbed_path / ".git").exists():
            errors.append("testbed/ is not a git repository")
        else:
            self.logger.info("  ✓ testbed/ directory exists with .git")

        # Check metadata.json
        metadata_path = workspace / "metadata.json"
        if not metadata_path.exists():
            errors.append("metadata.json not found")
        else:
            self.logger.info("  ✓ metadata.json exists")

        # Check that milestones.csv is valid CSV and collect milestone IDs
        milestones_path = workspace / "milestones.csv"
        milestone_ids = []
        if milestones_path.exists():
            try:
                with open(milestones_path, "r") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                if not rows:
                    errors.append("milestones.csv is empty")
                elif "id" not in reader.fieldnames:
                    errors.append("milestones.csv missing 'id' column")
                else:
                    milestone_ids = [row["id"] for row in rows]
                    self.logger.info(f"  ✓ milestones.csv valid ({len(rows)} milestones)")
            except Exception as e:
                errors.append(f"milestones.csv parse error: {e}")
        else:
            errors.append("milestones.csv not found")

        # Check that dependencies.csv is valid CSV
        dependencies_path = workspace / "dependencies.csv"
        if dependencies_path.exists():
            try:
                with open(dependencies_path, "r") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                # Empty dependencies is OK (no dependencies)
                self.logger.info(f"  ✓ dependencies.csv valid ({len(rows)} dependencies)")
            except Exception as e:
                errors.append(f"dependencies.csv parse error: {e}")
        else:
            errors.append("dependencies.csv not found")

        # Validate milestone tags in testbed repository
        if testbed_path.exists() and (testbed_path / ".git").exists() and milestone_ids:
            try:
                # Get all tags from testbed repo
                result = subprocess.run(
                    ["git", "tag"],
                    cwd=testbed_path,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                existing_tags = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()

                # Check each milestone has both start and end tags
                missing_tags = []
                for mid in milestone_ids:
                    start_tag = f"milestone-{mid}-start"
                    end_tag = f"milestone-{mid}-end"
                    if start_tag not in existing_tags:
                        missing_tags.append(start_tag)
                    if end_tag not in existing_tags:
                        missing_tags.append(end_tag)

                if missing_tags:
                    errors.append(
                        f"Missing {len(missing_tags)} milestone tag(s): {', '.join(missing_tags[:5])}"
                        + (f" ... and {len(missing_tags) - 5} more" if len(missing_tags) > 5 else "")
                    )
                else:
                    self.logger.info(f"  ✓ All milestone tags present ({len(milestone_ids)} milestones × 2 tags)")
            except subprocess.CalledProcessError as e:
                errors.append(f"Failed to list git tags: {e}")
            except Exception as e:
                errors.append(f"Tag validation error: {e}")

        if errors:
            return "; ".join(errors)

        self.logger.info("  ✓ All validation checks passed")
        return None

    def _archive_outputs(self) -> List[str]:
        """Archive build testbed stage outputs.

        For harness pipeline, outputs are already in workspace root.
        Just return list of output files for verification.

        Returns:
            List of output filenames
        """
        self.logger.info("Checking output files...")

        workspace = Path(self.ctx.workspace_root)
        expected_outputs = [
            "testbed",
            "metadata.json",
            "milestones.csv",
            "dependencies.csv",
        ]

        saved_files = []
        for filename in expected_outputs:
            path = workspace / filename
            if path.exists():
                saved_files.append(filename)
                if path.is_dir():
                    self.logger.info(f"  ✓ Found directory: {filename}/")
                else:
                    self.logger.info(f"  ✓ Found file: {filename}")

        self.logger.info(f"  ✓ {len(saved_files)}/{len(expected_outputs)} output(s) present")
        return saved_files
