"""
HarnessContext - Simplified context for harness pipeline.

Provides a flat directory structure without multi-stage complexity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field, computed_field, model_validator

from .types import StageResult


class HarnessContext(BaseModel):
    """Simplified context for harness pipeline execution.

    Uses a flat directory structure:
    - workspace_root/
      ├── deepcommit/         # Source code
      ├── proc_data/          # Symlink to preprocessed data
      ├── logs/               # Logs directory
      └── ... (flat outputs)

    No input/output subdirectories, all stages share the same workspace.
    """

    trial_name: str
    repo_id: str
    config_file: str  # Pipeline config file name
    agents: List[str]  # Comma-separated agent list -> list
    code_commit_sha: str
    start_time_iso: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    max_retries: int = 2
    workspace_root: str  # Absolute path

    # Optional repo configuration
    repo_src_dirs: Optional[List[str]] = None
    test_dirs: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    base_image_name: Optional[str] = None
    build_command: Optional[str] = None

    # Compatibility with BaseStage (simulated multi-stage properties)
    current_stage_id: str = "harness"  # Fixed stage ID for harness pipeline
    is_multi_stage: bool = True  # Set to True to use get_mcp_tools() for MCP config
    use_flat_structure: bool = True  # No result/ directory, logs directly in logs/

    # Fixed directory names (relative to workspace_root)
    _proc_dir: Union[Path, str] = "proc_data"
    _logs_dir: Union[Path, str] = "logs"
    _tools_dir: Union[Path, str] = "deepcommit/tools"
    _prompt_dir: Union[Path, str] = "deepcommit/pipeline/prompts"
    _configs_dir: Union[Path, str] = "deepcommit/pipeline/configs_harness"
    _mcp_config: Union[Path, str] = "trial.mcp.json"
    _tools_config_dir: Union[Path, str] = "deepcommit/configs/mcp"

    # Runtime stage results cache
    stage_results: Dict[str, StageResult] = Field(default_factory=dict)

    # Model configuration
    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="before")
    @classmethod
    def _normalize_inputs(cls, data: dict) -> dict:
        """Normalize agents from CSV string to list."""
        agents = data.get("agents")
        if isinstance(agents, str):
            data["agents"] = [a.strip() for a in agents.split(",") if a.strip()]
        return data

    # -------- Computed paths --------

    @computed_field(return_type=str)
    @property
    def logs_path(self) -> str:
        """Absolute path to logs directory."""
        return str(Path(self.workspace_root) / self._logs_dir)

    @computed_field(return_type=str)
    @property
    def tools_path(self) -> str:
        """Absolute path to MCP tools directory."""
        return str(Path(self.workspace_root) / self._tools_dir)

    @computed_field(return_type=str)
    @property
    def configs_path(self) -> str:
        """Absolute path to config directory."""
        return str(Path(self.workspace_root) / self._configs_dir)

    @computed_field(return_type=str)
    @property
    def mcp_config_path(self) -> str:
        """Absolute path to MCP config file."""
        return str(Path(self.workspace_root) / self._mcp_config)

    @computed_field(return_type=str)
    @property
    def tools_config_path(self) -> str:
        """Absolute path to MCP tools config directory."""
        return str(Path(self.workspace_root) / self._tools_config_dir)

    @computed_field(return_type=str)
    @property
    def prompt_dir(self) -> str:
        """Absolute path to prompts directory."""
        return str(Path(self.workspace_root) / self._prompt_dir)

    @computed_field(return_type=str)
    @property
    def proc_data_path(self) -> str:
        """Absolute path to proc_data (preprocessed data)."""
        return str(Path(self.workspace_root) / self._proc_dir)

    @computed_field(return_type=str)
    @property
    def repo_range(self) -> str:
        """Get repo_range from repo_id (e.g., 'apache_dubbo_dubbo-3.3.3_dubbo-3.3.6').

        Parses repo_id format: owner_repo:start_end -> owner_repo_start_end
        """
        if ":" in self.repo_id:
            repo_part, time_span = self.repo_id.split(":", 1)
            return f"{repo_part}_{time_span}"
        return self.repo_id

    # -------- Helper methods --------

    def to_abs_path(self, rel_path: Union[Path, str]) -> str:
        """Convert relative path to absolute path within workspace."""
        return str(Path(self.workspace_root) / Path(rel_path))

    def get_output_path(self, filename: str) -> str:
        """Get absolute path for an output file (flat structure)."""
        return str(Path(self.workspace_root) / filename)

    def get_input_path(self, filename: str) -> str:
        """Get absolute path for an input file in proc_data."""
        return str(Path(self.proc_data_path) / filename)

    def requested_tools(self) -> list[str]:
        """Return empty list (all tools enabled by default)."""
        return []

    def get_repo_src_dirs(self) -> List[str]:
        """Get source directories for LoC calculation."""
        return self.repo_src_dirs or ["src"]

    def get_repo_exclude_patterns(self) -> List[str]:
        """Get exclude patterns for filtering."""
        return self.exclude or []

    # -------- Stage status helpers --------

    def set_stage_results(self, stage: str, stage_result: StageResult) -> None:
        """Record result for a pipeline stage."""
        self.stage_results[stage] = stage_result

    def get_all_status(self) -> Tuple[str, str]:
        """Get aggregated status.

        Returns:
            (global_status, status_str) where:
            - global_status: "ok" or "error"
            - status_str: error messages concatenated
        """
        if not self.stage_results:
            return "ok", ""

        errors: List[str] = []
        for stage, res in self.stage_results.items():
            if res.status != "ok":
                msg = (res.error or res.notes or "").strip()
                if msg:
                    errors.append(f"{stage}: {msg}")
                else:
                    errors.append(f"{stage}: error")

        global_status = "ok" if not errors else "error"
        status_str = "\n".join(errors)
        return global_status, status_str

    # -------- BaseStage compatibility methods --------

    def get_stage_output_dir(self, stage_id: Optional[str] = None) -> Path:
        """Get output directory for stage (flat structure, returns workspace root)."""
        return Path(self.workspace_root)

    def get_stage_logs_dir(self, stage_id: Optional[str] = None) -> Path:
        """Get logs directory for stage."""
        return Path(self.logs_path)

    def get_mcp_config_path(self, stage_id: Optional[str] = None) -> str:
        """Get MCP config file path."""
        return self.mcp_config_path

    def get_prompt_rendered_path(self) -> str:
        """Get path to rendered prompt file in logs directory.

        Returns path like: logs/{current_stage_id}_prompt.md
        """
        return str(Path(self.logs_path) / f"{self.current_stage_id}_prompt.md")

    def get_repo(self) -> str:
        """Get repository path for MCP tools.

        Returns source_repo symlink which points to DATA/github_data/repos/{owner_repo}.
        This is needed for MCP tools like validate_milestone_partitioning
        and analyze_dag_conflicts that require git operations.
        """
        return str(Path(self.workspace_root) / "source_repo")

    def get_preprocess(self) -> str:
        """Get preprocessed data path."""
        return self.proc_data_path

    def get_input(self, var_name: str) -> str:
        """Get input file path from proc_data.

        Args:
            var_name: Variable name (e.g., "commit_dag", "main_commits")

        Returns:
            Absolute path to input file in proc_data
        """
        # Simple mapping: convert var_name to filename
        # This can be enhanced by loading from shared config
        filename_mapping = {
            "commit_dag": "commit_dag.json",
            "main_commits": "main_commits.jsonl",
            "patches": "patches",
            "pull_requests": "pull_requests.jsonl",
            "releases": "releases.jsonl",
            "pr_conversations": "pr_conversations.jsonl",
            "issues": "issues.jsonl",
            "codemaat_analysis": "codemaat_analysis.json",
            "symbol": "symbol/main_commits_symbol.jsonl",
        }
        filename = filename_mapping.get(var_name, f"{var_name}.jsonl")
        return str(Path(self.proc_data_path) / filename)

    def get_output(self, var_name: str) -> str:
        """Get output file path (flat structure).

        Args:
            var_name: Variable name (e.g., "milestones", "dependencies")

        Returns:
            Absolute path to output file in workspace root
        """
        # Simple mapping: convert var_name to filename
        filename_mapping = {
            "milestones": "milestones.csv",
            "milestone_csv": "milestones.csv",
            "dependencies": "dependencies.csv",
            "dependencies_csv": "dependencies.csv",
            "metadata": "metadata.json",
            "result_summary": "result_summary.json",
        }
        filename = filename_mapping.get(var_name, f"{var_name}.json")
        return str(Path(self.workspace_root) / filename)

    def get_mcp_tools(self) -> Dict[str, List[str]]:
        """Get MCP tools configuration from stage config.

        Returns:
            Dict mapping server names to tool lists
            e.g., {"git_analysis": ["get_commit_context", "search_entities"]}
        """
        # Load stage config to get MCP tools
        # For now, return a default configuration
        # This should be loaded from stage1_harness_main.yaml
        return {
            "git_analysis": [
                "get_commit_context",
                "search_entities",
                "analyze_commit_coverage",
                "check_dependency",
            ]
        }

    def get_new_input_vars(self) -> set:
        """Get new input variables (harness has no stage inheritance).

        Returns:
            Empty set (no multi-stage inheritance in harness)
        """
        return set()

    def render_prompt(self) -> tuple[str, dict]:
        """Render prompt template with context variables.

        Loads prompt template from stage config and renders with Jinja2.

        Returns:
            (rendered_prompt_text, path_mapping) tuple
        """
        import yaml
        from jinja2 import Template

        rendered_path = self.get_prompt_rendered_path()
        path_mapping = {
            "prompt_rendered_path": rendered_path,
        }

        # Load stage config to get prompt file
        stage_config_path = Path(self.configs_path) / f"{self.current_stage_id}.yaml"
        prompt_file = None

        if stage_config_path.exists():
            with open(stage_config_path, "r") as f:
                stage_config = yaml.safe_load(f) or {}
            prompt_file = stage_config.get("prompt_file")

        if not prompt_file:
            # Fallback: use default prompt
            rendered_text = f"""# Harness Pipeline Prompt

Trial: {self.trial_name}
Repo: {self.repo_id}
Agents: {", ".join(self.agents)}

No prompt_file configured in stage config.
"""
            with open(rendered_path, "w") as f:
                f.write(rendered_text)
            return rendered_text, path_mapping

        # Load and render template
        template_path = Path(self.prompt_dir) / prompt_file
        if not template_path.exists():
            rendered_text = f"# Error: Template not found: {template_path}"
            with open(rendered_path, "w") as f:
                f.write(rendered_text)
            return rendered_text, path_mapping

        with open(template_path, "r") as f:
            template_content = f.read()

        # Build template variables
        template_vars = {
            "trial_name": self.trial_name,
            "repo_id": self.repo_id,
            "repo_range": self.repo_range,
            "workspace_root": self.workspace_root,
            "agents": ", ".join(self.agents),
            "milestone_csv_path": str(Path(self.workspace_root) / "milestones.csv"),
            "dependencies_csv_path": str(Path(self.workspace_root) / "dependencies.csv"),
            "config_path": str(Path(self.workspace_root) / "config" / f"{self.repo_range}.yaml"),
        }

        # Render template
        template = Template(template_content)
        rendered_text = template.render(**template_vars)

        # Save to file
        with open(rendered_path, "w") as f:
            f.write(rendered_text)

        return rendered_text, path_mapping

    def get_info_string(self) -> str:
        """Generate formatted basic information string for logging."""
        info_items = [
            ("Trial name", self.trial_name),
            ("Repo ID", self.repo_id),
            ("Agents", ",".join(self.agents)),
            ("Workspace root", self.workspace_root),
            ("Config file", self.config_file),
        ]

        label_width = max(len(label) for label, _ in info_items)
        header = "Harness Trial Info"
        border = "=" * max(len(header), label_width + 20)

        lines = ["", border, header, border]
        for label, value in info_items:
            lines.append(f"{label:<{label_width}} : {value}")
        lines.append(border)

        return "\n".join(lines)

    def to_meta(self) -> dict:
        """Convert to plain dict for JSON serialization."""
        global_status, status_str = self.get_all_status()
        end_time = datetime.now(timezone.utc)

        start_time = datetime.fromisoformat(self.start_time_iso.replace("Z", "+00:00"))
        duration_sec = (end_time - start_time).total_seconds()

        return {
            "trial": self.trial_name,
            "workspace_root": self.workspace_root,
            "repo_id": self.repo_id,
            "config_file": self.config_file,
            "agents": ",".join(self.agents),
            "code_commit_sha": self.code_commit_sha,
            "max_retries": self.max_retries,
            "start_ts": self.start_time_iso,
            "end_ts": end_time.isoformat(),
            "duration_sec": duration_sec,
            "status": global_status,
            "status_str": status_str,
            "log_dir": Path(self.logs_path).resolve(),
        }
