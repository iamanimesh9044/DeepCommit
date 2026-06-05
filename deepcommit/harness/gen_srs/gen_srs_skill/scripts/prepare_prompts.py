#!/usr/bin/env python3
"""
Prepare all prompts for SRS generation stages.

This script automates Phase 1.2 of the SRS generation workflow:
1. Generate Stage 1 prompt (initial SRS generation)
2. Generate Stage 2 prompt (acceptance criteria refinement)
3. Generate Stage 3 prompt (environment dependencies)
4. Create initial workflow state file

All prompts are saved to the output directory for agent consumption.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from jinja2 import Template

# Import the existing SRSPromptPreparer
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from prepare_prompt import SRSPromptPreparer


class PromptPreparer:
    """Prepare all SRS generation prompts."""

    def __init__(
        self,
        repo_name: str,
        milestone_version: str,
        milestone_id: str,
        trial_name: str,
        git_repo: str,
    ):
        self.repo_name = repo_name
        self.milestone_version = milestone_version
        self.milestone_id = milestone_id
        self.trial_name = trial_name
        self.git_repo = Path(git_repo).resolve()  # Convert to absolute path

        # Setup paths
        # Go up to the real project root (5 levels up from this file)
        self.project_root = Path(__file__).parent.parent.parent.parent.parent
        self.workspace_root = self.project_root / "srs_workspace"
        self.work_dir = self.workspace_root / repo_name / milestone_version
        self.env_data_dir = self.work_dir / "env_data"
        self.test_data_dir = self.work_dir / "test_data"
        self.output_dir = self.work_dir / "srs" / trial_name / milestone_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Template paths
        self.template_dir = Path(__file__).parent.parent.parent
        self.srs_gen_tmpl = self.template_dir / "srs_gen_v1.tmpl"
        self.acceptance_tmpl = self.template_dir / "acceptance_test_align.tmpl"
        self.env_spec_tmpl = self.template_dir / "env_spec.tmpl"

        # Container names (for env_spec prompt)
        base_image = f"{repo_name}/{milestone_version.lower()}/base"
        milestone_image = f"{repo_name}/{milestone_version.lower()}/{milestone_id.lower()}"
        self.base_container = f"{base_image.replace('/', '-')}-container"
        self.milestone_container = f"{milestone_image.replace('/', '-')}-container"

        # Setup logger
        self.logger = self._setup_logger()

        # Validate paths
        self._validate()

    def _setup_logger(self) -> logging.Logger:
        """Setup logger."""
        logger = logging.getLogger(f"prompt_prep.{self.milestone_id}")
        logger.setLevel(logging.INFO)

        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter("[%(name)s] %(levelname)s: %(message)s")
        console.setFormatter(formatter)
        logger.addHandler(console)

        return logger

    def _validate(self):
        """Validate required paths and files."""
        if not self.work_dir.exists():
            raise FileNotFoundError(f"Work directory not found: {self.work_dir}")

        if not self.git_repo.exists():
            raise FileNotFoundError(f"Git repository not found: {self.git_repo}")

        for tmpl in [self.srs_gen_tmpl, self.acceptance_tmpl, self.env_spec_tmpl]:
            if not tmpl.exists():
                raise FileNotFoundError(f"Template not found: {tmpl}")

    def prepare_all(self):
        """Prepare all prompts."""
        self.logger.info("=" * 70)
        self.logger.info("Prompt Preparation for SRS Generation")
        self.logger.info("=" * 70)
        self.logger.info(f"Repository: {self.repo_name}")
        self.logger.info(f"Milestone: {self.milestone_id}")
        self.logger.info(f"Trial: {self.trial_name}")
        self.logger.info(f"Output: {self.output_dir}")
        self.logger.info("=" * 70)

        # Prepare Stage 1: SRS Generation prompt
        self.logger.info("\n[1/3] Preparing Stage 1 prompt (SRS Generation)...")
        stage1_prompt = self._prepare_srs_gen_prompt()
        stage1_path = self.output_dir / "prompt_srs_gen.md"
        stage1_path.write_text(stage1_prompt, encoding="utf-8")
        self.logger.info(f"✓ Saved to: {stage1_path}")

        # Prepare Stage 2: Acceptance Alignment prompt
        self.logger.info("\n[2/3] Preparing Stage 2 prompt (Acceptance Criteria)...")
        stage2_prompt = self._prepare_acceptance_prompt()
        stage2_path = self.output_dir / "prompt_acceptance_align.md"
        stage2_path.write_text(stage2_prompt, encoding="utf-8")
        self.logger.info(f"✓ Saved to: {stage2_path}")

        # Prepare Stage 3: Environment Spec prompt
        self.logger.info("\n[3/3] Preparing Stage 3 prompt (Environment Dependencies)...")
        stage3_prompt = self._prepare_env_spec_prompt()
        stage3_path = self.output_dir / "prompt_env_spec.md"
        stage3_path.write_text(stage3_prompt, encoding="utf-8")
        self.logger.info(f"✓ Saved to: {stage3_path}")

        # Create initial state file
        self.logger.info("\n[4/4] Creating workflow state file...")
        self._create_state_file()
        self.logger.info(f"✓ State file created: {self.output_dir / 'state.json'}")

        # Summary
        self.logger.info("\n" + "=" * 70)
        self.logger.info("✓ PROMPT PREPARATION COMPLETED")
        self.logger.info("=" * 70)
        self.logger.info(f"Output directory: {self.output_dir}")
        self.logger.info("  - prompt_srs_gen.md (Stage 1)")
        self.logger.info("  - prompt_acceptance_align.md (Stage 2)")
        self.logger.info("  - prompt_env_spec.md (Stage 3)")
        self.logger.info("  - state.json (workflow state)")
        self.logger.info("\nNext step: Launch agents to execute each stage sequentially")

    def _prepare_srs_gen_prompt(self) -> str:
        """Prepare Stage 1 SRS generation prompt using existing SRSPromptPreparer."""
        # Construct patches_dir
        data_root = self.project_root / "DATA"
        patches_dir_path = data_root / "github_data" / "preprocessed" / self.repo_name / "patches"
        patches_dir = str(patches_dir_path) if patches_dir_path.exists() else None

        preparer = SRSPromptPreparer(
            work_dir=str(self.work_dir),
            milestone_id=self.milestone_id,
            filter_src_only=False,
            patches_dir=patches_dir,
            output_dir=str(self.output_dir),
            project_root=str(self.project_root),
            git_repo=str(self.git_repo),
            milestone_container=self.milestone_container,
        )

        return preparer.generate_output()

    def _prepare_acceptance_prompt(self) -> str:
        """Prepare Stage 2 acceptance criteria refinement prompt."""
        template = Template(self.acceptance_tmpl.read_text(encoding="utf-8"))

        # Use relative paths from project root
        srs_path_rel = (self.output_dir / "SRS.md").relative_to(self.project_root)
        f2p_test_dir = self.test_data_dir / self.milestone_id / "benchmark" / "f2p_tests"
        f2p_test_dir_rel = f2p_test_dir.relative_to(self.project_root)
        test_metadata_rel = (f2p_test_dir / "metadata.json").relative_to(self.project_root)

        return template.render(
            srs_path=str(srs_path_rel),
            f2p_test_dir=str(f2p_test_dir_rel),
            start_commit=f"milestone-{self.milestone_id}-start",
            end_commit=f"milestone-{self.milestone_id}-end",
            test_metadata=str(test_metadata_rel),
            milestone_container=self.milestone_container,
        )

    def _prepare_env_spec_prompt(self) -> str:
        """Prepare Stage 3 environment dependencies prompt."""
        base_dockerfile = self.env_data_dir / "Base" / "Dockerfile"
        milestone_dockerfile = self.env_data_dir / self.milestone_id / "Dockerfile"

        template = Template(self.env_spec_tmpl.read_text(encoding="utf-8"))

        return template.render(
            base_dockerfile=str(base_dockerfile),
            milestone_dockerfile=str(milestone_dockerfile),
            base_container=self.base_container,
            milestone_container=self.milestone_container,
            git_repo=str(self.git_repo),
            output_file=str(self.output_dir / "env_dependencies.md"),
        )

    def _create_state_file(self):
        """Create initial workflow state file."""
        state = {
            "workflow": "srs_generation",
            "repo_name": self.repo_name,
            "milestone_version": self.milestone_version,
            "milestone_id": self.milestone_id,
            "trial_name": self.trial_name,
            "created_at": datetime.now().isoformat(),
            "stages": {
                "stage1_srs_gen": {
                    "status": "pending",
                    "prompt_file": "prompt_srs_gen.md",
                    "expected_output": "SRS.md",
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                },
                "stage2_acceptance": {
                    "status": "pending",
                    "prompt_file": "prompt_acceptance_align.md",
                    "expected_output": "SRS.md (updated)",
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                },
                "stage3_env_spec": {
                    "status": "pending",
                    "prompt_file": "prompt_env_spec.md",
                    "expected_output": "env_dependencies.md",
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                },
            },
            "output_dir": str(self.output_dir),
        }

        state_file = self.output_dir / "state.json"
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Prepare all prompts for SRS generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--repo-name", required=True, help="Repository name (e.g., urllib3_urllib3_2.0.6_2.3.0)")
    parser.add_argument("--milestone-version", required=True, help="Milestone version (e.g., test_multi_stage_V2)")
    parser.add_argument("--milestone-id", required=True, help="Milestone ID (e.g., M001)")
    parser.add_argument("--trial-name", default="v1", help="Trial version (default: v1)")
    parser.add_argument(
        "--git-repo",
        help="Path to git repository (auto-inferred if not specified)",
    )

    args = parser.parse_args()

    # Auto-infer git_repo
    if not args.git_repo:
        project_root = Path(__file__).parent.parent.parent.parent
        data_root = project_root / "DATA" / "harness_workspace"
        args.git_repo = str(data_root / args.repo_name / args.milestone_version / "testbed")
        print(f"Auto-inferred git-repo: {args.git_repo}")

    try:
        preparer = PromptPreparer(
            repo_name=args.repo_name,
            milestone_version=args.milestone_version,
            milestone_id=args.milestone_id,
            trial_name=args.trial_name,
            git_repo=args.git_repo,
        )
        preparer.prepare_all()
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Preparation failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
