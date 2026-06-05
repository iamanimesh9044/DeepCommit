"""
Stage 1: Seed Discovery

Input: proc_data (via shared config)
Output: stage1_seed/output/milestone_seeds.jsonl
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..base_stage import BaseStage
from ..models import MilestoneSeedModel

if TYPE_CHECKING:
    pass


class Stage1SeedDiscovery(BaseStage):
    """Stage 1: Seed Discovery - identifies milestone seed commits."""

    def _setup_input_data(self) -> None:
        """Setup input data (no-op for Stage 1).

        Stage 1 uses only shared inputs (commit_dag, main_commits, etc.).
        No preprocessing required.
        """
        self.logger.info("Setting up input data...")
        self.logger.info("  No new input variables to preprocess")

    def _validate_outputs(self) -> Optional[str]:
        """Validate milestone_seeds.jsonl output file.

        Returns:
            Error message if validation fails, None if successful
        """
        self.logger.info("Validating output files...")

        milestone_seeds_path = Path(self.ctx.get_output("milestone_seeds"))
        error_messages: List[str] = []

        # Check file exists
        if not os.path.isfile(milestone_seeds_path):
            return f"Missing milestone_seeds.jsonl at {milestone_seeds_path}"

        # Validate JSONL format and content
        try:
            with open(milestone_seeds_path, "r", encoding="utf-8") as f:
                seen_shas = set()
                seed_count = 0

                for idx, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue

                    # Parse JSON
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as e:
                        error_messages.append(f"Line {idx} invalid JSON: {e}")
                        continue

                    # Validate against model
                    payload = {k: data.get(k, "") for k in MilestoneSeedModel.model_fields.keys()}
                    try:
                        seed = MilestoneSeedModel.model_validate(payload)
                    except Exception as e:
                        error_messages.append(f"Line {idx} invalid: {e}")
                        continue

                    # Check for duplicate SHAs
                    if seed.sha in seen_shas:
                        error_messages.append(f"Duplicate seed SHA: {seed.sha}")
                        continue

                    seen_shas.add(seed.sha)
                    seed_count += 1

                if seed_count == 0:
                    error_messages.append("No valid data rows")
        except Exception as e:
            return f"Failed to read milestone_seeds.jsonl: {e}"

        if error_messages:
            return "; ".join(error_messages)

        self.logger.info(f"  ✓ Validated {seed_count} milestone seeds")
        return None

    def _archive_outputs(self) -> List[str]:
        """Archive milestone_seeds.jsonl to stage output directory.

        Returns:
            List of archived filenames
        """
        self.logger.info("Archiving output files...")
        saved_files = []

        try:
            output_path = Path(self.ctx.get_output("milestone_seeds"))

            if output_path.exists():
                self.logger.archive_file(output_path)
                saved_files.append(output_path.name)
            else:
                self.logger.warning(f"Expected output not found: {output_path}")
        except ValueError as e:
            self.logger.error(f"Failed to get output path: {e}")

        return saved_files
