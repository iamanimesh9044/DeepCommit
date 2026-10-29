from __future__ import annotations

from typing import Union

from pydantic import BaseModel, Field, model_validator


class MilestoneSeedModel(BaseModel):
    """Milestone seed model for Stage 1 output (milestone_seeds.jsonl).

    JSONL format - each line is a JSON object with these fields.
    """

    sha: str = Field(min_length=1, description="Short SHA (7 chars)")
    confidence_score: Union[str, float] = Field(default="", description="Confidence score (0.0-1.0)")
    milestone_type: str = Field(default="", description="Type of milestone")
    title: str = Field(min_length=1, description="Milestone title")
    churn_context: str = Field(default="", description="Churn pattern context")
    rationale: str = Field(default="", description="Reasoning for seed selection")


class MilestoneModel(BaseModel):
    """Milestone model for Stage 2 output (milestone.csv).

    CSV columns: id,title,commits,mini_srs,integration_test_commit
    """

    id: str = Field(min_length=1, description="Milestone unique identifier (e.g., M1, M2)")
    title: str = Field(min_length=1, description="Milestone title")
    commits: str = Field(default="", description="Semicolon-separated commit SHA7 list")
    mini_srs: str = Field(default="", description="Mini-SRS requirement specification")
    integration_test_commit: str = Field(
        default="", description="SHA of integration test commit (only for first sub-milestone after split)"
    )
    repos: str = Field(default="", description="Semicolon-separated repo names touched (multi-repo workspaces only)")


class DependencyEdgeModel(BaseModel):
    """Dependency edge model for Stage 3 output (dependencies.csv).

    CSV columns: prerequisite_id,dependant_id,type,strength,rationale,confidence_score
    (also accepts legacy source_id/target_id via field aliases for backward compat).

    Direction: prerequisite must complete before dependant (dependant depends on prerequisite).
    Based on dep_infer.md prompt specification.
    """

    model_config = {"populate_by_name": True}

    prerequisite_id: str = Field(
        min_length=1,
        alias="source_id",  # accept legacy column name in CSV / JSON input
        description="Prerequisite milestone ID (upstream, must complete first)",
    )
    dependant_id: str = Field(
        min_length=1,
        alias="target_id",  # accept legacy column name in CSV / JSON input
        description="Dependant milestone ID (downstream, depends on prerequisite)",
    )
    type: str = Field(default="", description="Dependency type: FUNC/NFR/TEXT/XREPO")
    strength: str = Field(default="", description="Dependency strength: Strong/Weak")
    rationale: str = Field(default="", description="Reasoning for the dependency")
    confidence_score: Union[str, float] = Field(default="", description="Confidence score (0.0-1.0)")

    @model_validator(mode="after")
    def _no_self_edge(self) -> "DependencyEdgeModel":
        if self.prerequisite_id == self.dependant_id:
            raise ValueError("prerequisite_id and dependant_id must differ")
        return self

    @model_validator(mode="after")
    def _validate_type(self) -> "DependencyEdgeModel":
        if self.type and self.type not in ["FUNC", "NFR", "TEXT", "XREPO", ""]:
            raise ValueError(f"type must be FUNC, NFR, TEXT, or XREPO, got: {self.type}")
        return self

    @model_validator(mode="after")
    def _validate_strength(self) -> "DependencyEdgeModel":
        if self.strength and self.strength not in ["Strong", "Weak", ""]:
            raise ValueError(f"strength must be Strong or Weak, got: {self.strength}")
        return self
