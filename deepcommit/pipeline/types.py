from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class StageResult(BaseModel):
    status: Literal["ok", "error", "skipped"] = "ok"
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    error: Optional[str] = None
