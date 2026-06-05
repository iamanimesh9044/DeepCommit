"""
Stage 0: MCP Test

This is a minimal stage designed to test MCP tool accessibility.
Input: None (uses only shared inputs)
Output: stage0_mcp_test/output/test_result.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from .base_stage import BaseStage

if TYPE_CHECKING:
    pass


class Stage0McpTest(BaseStage):
    """Stage 0: MCP Test - tests MCP tool accessibility."""

    def _setup_input_data(self) -> None:
        """Setup input data (no-op for Stage 0).

        This stage only tests MCP tools, no preprocessing required.
        """
        self.logger.info("Setting up input data...")
        self.logger.info("  No input data needed for MCP test")

    def _validate_outputs(self) -> Optional[str]:
        """Validate test_result.json output file.

        Returns:
            Error message if validation fails, None if successful
        """
        self.logger.info("Validating output files...")

        test_result_path = Path(self.ctx.get_output("test_result"))

        # Check file exists
        if not os.path.isfile(test_result_path):
            return f"Missing test_result.json at {test_result_path}"

        # Validate JSON format
        try:
            with open(test_result_path, "r", encoding="utf-8") as f:
                data = json.load(f)

                # Basic validation: check for expected keys
                if not isinstance(data, dict):
                    return "test_result.json must contain a JSON object"

                self.logger.info(f"  ✓ Test result validated: {len(data)} keys found")

        except json.JSONDecodeError as e:
            return f"Invalid JSON in test_result.json: {e}"
        except Exception as e:
            return f"Failed to read test_result.json: {e}"

        return None

    def _archive_outputs(self) -> List[str]:
        """Archive test_result.json to stage output directory.

        Returns:
            List of archived filenames
        """
        self.logger.info("Archiving output files...")
        saved_files = []

        try:
            output_path = Path(self.ctx.get_output("test_result"))

            if output_path.exists():
                self.logger.archive_file(output_path)
                saved_files.append(output_path.name)
            else:
                self.logger.warning(f"Expected output not found: {output_path}")
        except ValueError as e:
            self.logger.error(f"Failed to get output path: {e}")

        return saved_files
