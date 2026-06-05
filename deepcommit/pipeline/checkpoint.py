"""Simple checkpoint manager for pipeline recovery.

Tracks phase and stage completion status to enable resume from last successful point.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional


class CheckpointManager:
    """Minimal checkpoint manager for pipeline recovery.

    Features:
    - Track phase/stage completion status
    - Skip completed phases/stages on retry
    - Validate previous outputs before re-execution
    """

    def __init__(self, checkpoint_file: Path):
        self.checkpoint_file = checkpoint_file
        self.data = self._load()

    def _load(self) -> dict:
        """Load checkpoint from file, initialize if not exists."""
        if self.checkpoint_file.exists():
            with open(self.checkpoint_file) as f:
                return json.load(f)
        return {"phases": {}}

    def _save(self):
        """Save checkpoint to file."""
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        self.data["last_updated"] = datetime.now().isoformat()
        with open(self.checkpoint_file, "w") as f:
            json.dump(self.data, f, indent=2)

    # ========== Phase-level operations ==========

    def is_phase_completed(self, phase_id: str) -> bool:
        """Check if phase is already completed."""
        return self.data["phases"].get(phase_id, {}).get("status") == "completed"

    def mark_phase_completed(self, phase_id: str):
        """Mark phase as completed."""
        if phase_id not in self.data["phases"]:
            self.data["phases"][phase_id] = {}
        self.data["phases"][phase_id]["status"] = "completed"
        self._save()

    def mark_phase_failed(self, phase_id: str, error: str):
        """Mark phase as failed (non-blocking for phase 3/4/5)."""
        if phase_id not in self.data["phases"]:
            self.data["phases"][phase_id] = {}
        self.data["phases"][phase_id]["status"] = "failed"
        self.data["phases"][phase_id]["error"] = error
        self._save()

    # ========== Stage-level operations ==========

    def is_stage_completed(self, stage_id: str) -> bool:
        """Check if stage is already completed."""
        phase2 = self.data["phases"].get("phase2_multi_stage", {})
        stages = phase2.get("stages", {})
        return stages.get(stage_id, {}).get("status") == "completed"

    def get_stage_status(self, stage_id: str) -> Optional[str]:
        """Get stage status: 'completed', 'failed', or None."""
        phase2 = self.data["phases"].get("phase2_multi_stage", {})
        stages = phase2.get("stages", {})
        return stages.get(stage_id, {}).get("status")

    def mark_stage_completed(self, stage_id: str):
        """Mark stage as completed."""
        if "phase2_multi_stage" not in self.data["phases"]:
            self.data["phases"]["phase2_multi_stage"] = {"stages": {}}
        if "stages" not in self.data["phases"]["phase2_multi_stage"]:
            self.data["phases"]["phase2_multi_stage"]["stages"] = {}

        self.data["phases"]["phase2_multi_stage"]["stages"][stage_id] = {"status": "completed"}
        self._save()

    def mark_stage_failed(self, stage_id: str, error: str):
        """Mark stage as failed."""
        if "phase2_multi_stage" not in self.data["phases"]:
            self.data["phases"]["phase2_multi_stage"] = {"stages": {}}
        if "stages" not in self.data["phases"]["phase2_multi_stage"]:
            self.data["phases"]["phase2_multi_stage"]["stages"] = {}

        self.data["phases"]["phase2_multi_stage"]["stages"][stage_id] = {"status": "failed", "error": error}
        self._save()
