"""
Stage 2: Collect Test

Agent that collects test information after testbed is built.
Accesses:
- Main loop prompt (collect_test/main_loop.tmpl)
- Evaluation checklist (checklist.tmpl - inserted into main prompt)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..base_stage import BaseStage

if TYPE_CHECKING:
    pass


class Stage2CollectTest(BaseStage):
    """Stage 2: Collect Test - collects test information after testbed build."""

    def _setup_input_data(self) -> None:
        """Setup input data for collect test stage.

        Assumes testbed has been built in stage 1.
        """
        self.logger.info("Setting up input data for collect test stage...")
        self.logger.info("  Testbed should be available from stage 1")

    def _validate_outputs(self) -> Optional[str]:
        """Validate collect test stage outputs.

        Placeholder - 实际验证逻辑取决于输出格式。

        Returns:
            错误信息 (如果验证失败), None (如果成功)
        """
        self.logger.info("Validating output files...")

        # TODO: 实现实际的验证逻辑
        # 示例:
        # output_path = Path(self.ctx.get_output("result_summary"))
        # if not output_path.exists():
        #     return f"Missing output file: {output_path}"

        self.logger.info("  ✓ Validation passed (placeholder)")
        return None

    def _archive_outputs(self) -> List[str]:
        """Archive collect test stage outputs.

        Harness pipeline uses flat structure - outputs are already in workspace root.
        No archiving needed, just return list of expected output files.

        Returns:
            List of output filenames (for verification)
        """
        self.logger.info("Checking output files...")

        # List expected output files (these should be in workspace root already)
        expected_outputs = [
            "result_summary.json",
            "agent_output.txt",
        ]

        # Check which files actually exist
        workspace = Path(self.ctx.workspace_root)
        saved_files = []
        for filename in expected_outputs:
            if (workspace / filename).exists():
                saved_files.append(filename)
                self.logger.info(f"  ✓ Found: {filename}")

        self.logger.info(f"  ✓ {len(saved_files)}/{len(expected_outputs)} output file(s) present")
        return saved_files
