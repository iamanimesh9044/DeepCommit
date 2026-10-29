from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import yaml
from pydantic import BaseModel, Field, computed_field, model_validator

from .types import StageResult

if TYPE_CHECKING:
    from .stage_config import StageConfig


class TrialContext(BaseModel):
    """Trial context configuration for pipeline execution.

    All Path fields (_root, _path suffix) accept both str and Path inputs.
    String inputs are automatically converted to Path objects during validation.
    """

    trial_name: str
    repo_id: str
    prompt_file: str
    agents: List[str]  # Accepts CSV string or list
    code_commit_sha: str
    start_time_iso: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    max_retries: int = 2
    workspace_root: str  # absolute path
    tool_list: Optional[List[str]] = None
    repo_src_dirs: Optional[List[str]] = None  # Source directories for LoC calculation
    # Test directories and exclude patterns (used by filtering / tools)
    test_dirs: Optional[List[str]] = None  # Test file/dir patterns (e.g., ["tests/**", "**/*_test.go"])
    exclude: Optional[List[str]] = None  # Additional exclude patterns (e.g., ["**/testdata/**", "**/fixtures/**"])
    # Build environment configuration (for commit splitting verification)
    base_image_name: Optional[str] = None  # Docker base image (e.g., "navidrome-test:v0.57.0")
    build_command: Optional[str] = None  # Build command (e.g., "go build ./cmd/...", "npm run build")

    # Multi-repo workspace support (None => classic single-repo trial)
    workspace_dir: Optional[str] = None  # dir with repos.json + commit_repos.json (DEEPCOMMIT_WORKSPACE_DIR)
    workspace_id: Optional[str] = None
    # name -> {role, checkout, src_dirs, test_dirs, exclude, ...}; loaded from repos.json
    repos: Optional[Dict[str, Dict[str, Any]]] = None

    # Multi-stage pipeline support
    is_multi_stage: bool = False
    current_stage_id: Optional[str] = None
    executed_stages: List[str] = Field(default_factory=list)  # Track stage execution order

    # Agreements with the run_trial.sh script
    _result_dir: Union[Path, str] = "result"
    _proc_dir: Union[Path, str] = "proc_data"  # ✅ Fixed: was "preprocessed"
    _repo_dir: Union[Path, str] = "repo_data"  # ✅ Fixed: was "repo"
    _logs_dir: Union[Path, str] = "logs"
    _tools_dir: Union[Path, str] = "deepcommit/tools"  # ✅ Fixed: was "deepcommit/collect/tools"
    _prompt_dir: Union[Path, str] = "deepcommit/pipeline/prompts"  # ✅ Fixed: was "deepcommit/prompt"
    _configs_dir: Union[Path, str] = "deepcommit/pipeline/configs"  # ✅ New: configs directory
    _mcp_config: Union[Path, str] = "trial.mcp.json"
    _tools_config_dir: Union[Path, str] = "deepcommit/configs/mcp"
    _db_file: Union[Path, str] = "trials.sqlite"

    # Runtime stage results cache
    stage_results: Dict[str, StageResult] = Field(default_factory=dict)

    # ✅ New: Configuration management for multi-stage pipeline
    shared_config: Optional["StageConfig"] = None
    pipeline_config: Optional[Dict[str, Any]] = None
    stage_configs: Dict[str, "StageConfig"] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_inputs(cls, data: dict):
        # Allow agents as CSV string
        agents = data.get("agents")
        if isinstance(agents, str):
            data["agents"] = [a.strip() for a in agents.split(",") if a.strip()]

        def _normalize_tools(raw) -> Optional[List[str]]:
            def _clean_iter(values: Iterable[object]) -> Optional[List[str]]:
                cleaned: List[str] = []
                for item in values:
                    text = str(item).strip()
                    if not text:
                        continue
                    if text.lower() == "all":
                        return None
                    cleaned.append(text)
                return cleaned or None

            if raw is None or raw == "":
                return None
            if isinstance(raw, str):
                parts = [part for part in raw.split(",")]
                return _clean_iter(parts)
            if isinstance(raw, (list, tuple, set)):
                return _clean_iter(raw)
            return _clean_iter([raw])

        data["tool_list"] = _normalize_tools(data.get("tool_list"))

        return data

    def model_post_init(self, __context: Any) -> None:
        """Initialize configuration after model creation.

        Loads all configuration files if in multi-stage mode.
        """
        if self.workspace_dir:
            self._load_workspace()
        if self.is_multi_stage:
            self._load_all_configs()

    def _load_workspace(self) -> None:
        """Load repos.json of the workspace; in multi mode src/test/exclude become the prefixed lists."""
        import json

        if self.repos is None:
            raw = json.loads((Path(self.workspace_dir) / "repos.json").read_text(encoding="utf-8"))
            self.repos = {
                name: {
                    "role": r.get("role", "library"),
                    "owner": r.get("owner", ""),
                    "repo": r.get("repo", ""),
                    "start": r.get("start", ""),
                    "end": r.get("end", ""),
                    "checkout": r.get("checkout_path", ""),
                    "src_dirs": r.get("repo_src_dirs", []),
                    "test_dirs": r.get("test_dirs", []),
                    "exclude": r.get("exclude", []),
                    "prefixed_src_dirs": r.get("prefixed_src_dirs", []),
                    "prefixed_test_dirs": r.get("prefixed_test_dirs", []),
                    "prefixed_exclude": r.get("prefixed_exclude", []),
                }
                for name, r in raw.items()
            }
        if not self.workspace_id:
            self.workspace_id = Path(self.workspace_dir).name
        if self.is_multi_repo:
            self.repo_src_dirs = [d for r in self.repos.values() for d in r["prefixed_src_dirs"]]
            self.test_dirs = [g for r in self.repos.values() for g in r["prefixed_test_dirs"]]
            self.exclude = [g for r in self.repos.values() for g in r["prefixed_exclude"]]

    @property
    def is_multi_repo(self) -> bool:
        return bool(self.repos) and len(self.repos) > 1

    def get_workspace_dir(self) -> Optional[str]:
        return self.workspace_dir

    def get_resolver(self):
        """Commit -> repo resolver for a multi-repo trial; None for a single-repo trial."""
        if not self.is_multi_repo:
            return None
        from deepcommit.multirepo.resolver import WorkspaceResolver

        return WorkspaceResolver.from_dir(self.workspace_dir)

    def workspace_repos_table(self) -> str:
        lines = ["| name | role | repo dir | src dirs |", "| --- | --- | --- | --- |"]
        for name, r in (self.repos or {}).items():
            src = ", ".join(f"`{d}`" for d in r["prefixed_src_dirs"])
            lines.append(f"| {name} | {r['role']} | `{self._repo_dir}/{name}` | {src} |")
        if not self.repos:  # legacy single-repo trial: one-row table, checkout is repo_data itself
            name = (self.repo_id.split(":")[0] if ":" in self.repo_id else self.repo_id).replace("_", "/", 1)
            src = ", ".join(f"`{d}`" for d in self.get_repo_src_dirs())
            lines.append(f"| {name} | library | `{self._repo_dir}` | {src} |")
        return "\n".join(lines)

    def _load_all_configs(self) -> None:
        """Load all configuration files for multi-stage pipeline.

        Loads:
        - shared_config from shared.yaml
        - pipeline_config from the pipeline config file
        - stage_configs for each stage defined in pipeline config
        """
        # Avoid circular import
        from .stage_config import StageConfig

        configs_dir = Path(self.workspace_root) / self._configs_dir

        # Load shared config (using StageConfig for consistent structure)
        shared_path = configs_dir / "shared.yaml"
        if shared_path.exists():
            with open(shared_path, encoding="utf-8") as f:
                shared_data = yaml.safe_load(f)

            self.shared_config = StageConfig(stage_id="shared", config_file="shared.yaml", config_data=shared_data)

        # Load pipeline config (stored in prompt_file for multi-stage)
        if self.prompt_file:
            pipeline_path = configs_dir / self.prompt_file
            if pipeline_path.exists():
                with open(pipeline_path, encoding="utf-8") as f:
                    self.pipeline_config = yaml.safe_load(f)

                # Load each stage config
                stages = self.pipeline_config.get("stages", [])
                for stage_info in stages:
                    stage_id = stage_info["stage_id"]
                    stage_config_file = stage_info["stage_config"]

                    config_path = configs_dir / stage_config_file
                    if config_path.exists():
                        with open(config_path, encoding="utf-8") as f:
                            config_data = yaml.safe_load(f)

                        self.stage_configs[stage_id] = StageConfig(
                            stage_id=stage_id, config_file=stage_config_file, config_data=config_data
                        )

    def requested_tools(self) -> list[str]:
        """Return normalized tool requests (empty list means all)."""
        return list(self.tool_list or [])

    # All the following properties are absolute paths
    @computed_field(return_type=str)
    @property
    def result_path(self) -> str:
        return str(Path(self._result_dir))

    @computed_field(return_type=str)
    @property
    def logs_path(self) -> str:
        return str(Path(self.result_path) / Path(self._logs_dir))

    @computed_field(return_type=str)
    @property
    def tools_path(self) -> str:
        """Tools directory path (for MCP tools)."""
        return str(Path(self._tools_dir))

    @computed_field(return_type=str)
    @property
    def configs_path(self) -> str:
        """Configuration files directory path."""
        return str(Path(self._configs_dir))

    @computed_field(return_type=str)
    @property
    def mcp_config_path(self) -> str:
        return str(Path(self._mcp_config))

    @computed_field(return_type=str)
    @property
    def tools_config_path(self) -> str:
        return str(Path(self._tools_config_dir))

    @computed_field(return_type=str)
    @property
    def db_path(self) -> str:
        # Return absolute path
        real_dir = os.path.realpath(self.result_path)
        db_path = os.path.join(os.path.dirname(real_dir), self._db_file)
        return db_path

    def to_abs_path(self, rel_path: Union[Path, str]) -> str:
        return str(Path(self.workspace_root) / Path(rel_path))

    def get_info_string(self) -> str:
        """Generate formatted basic information string for logging."""
        info_items = [
            ("Trial start", self.trial_name),
            ("Repo ID", self.repo_id),
            ("Agents", ",".join(self.agents)),
            ("Workspace root", self.workspace_root),
            ("Result path", self.to_abs_path(self.result_path)),
            ("Config file", self.prompt_file),
        ]

        label_width = max(len(label) for label, _ in info_items)
        header = "Basic Trial Info"
        border = "=" * max(len(header), label_width + 20)

        lines = ["", border, header, border]
        for label, value in info_items:
            lines.append(f"{label:<{label_width}} : {value}")
        lines.append(border)

        return "\n".join(lines)

    def to_meta(self) -> dict:
        """Convert to a plain dict suitable for JSON serialization"""
        global_status, status_str = self.get_all_status()
        end_time = datetime.now(timezone.utc)

        # Parse start time and calculate duration
        start_time = datetime.fromisoformat(self.start_time_iso.replace("Z", "+00:00"))
        duration_sec = (end_time - start_time).total_seconds()

        return {
            "trial": self.trial_name,
            "workspace_root": self.workspace_root,
            "repo_id": self.repo_id,
            **({"workspace_id": self.workspace_id} if self.workspace_id else {}),
            "config_file": self.prompt_file,
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

    # -------- Stage status helpers --------
    def set_stage_results(self, stage: str, stage_result: StageResult) -> None:
        """Record the result for a pipeline stage.

        Args:
            stage: Human-readable stage name (e.g., "Stage 1 Preprocess").
            stage_result: Result object to store.
        """
        self.stage_results[stage] = stage_result

    def get_all_status(self) -> Tuple[str, str]:
        """Aggregate global status and a concatenated error/notes string.

        Returns:
            A tuple of (global_status, status_str).
            - global_status: "ok" if no stage has status "error"; otherwise "error".
            - status_str: For stages with status "error", concatenate their
              error (or notes if error is empty). Empty if no errors.
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

    # -------- Multi-stage pipeline helpers --------
    def get_stage_workspace(self, stage_id: str) -> Path:
        """Get workspace directory for a specific stage.

        Returns:
            Path to stage workspace (e.g., workspace_root/stage1_seed/)
        """
        if not self.is_multi_stage:
            return Path(self.workspace_root)
        return Path(self.workspace_root) / stage_id

    def get_stage_input_dir(self, stage_id: Optional[str] = None) -> Path:
        """Get input directory for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Path to stage input directory (e.g., workspace_root/stage1_seed/input/)
        """
        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("stage_id required in multi-stage mode")
        return self.get_stage_workspace(sid) / "input"

    def get_stage_output_dir(self, stage_id: Optional[str] = None) -> Path:
        """Get output directory for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Path to stage output directory (e.g., workspace_root/stage1_seed/output/)
        """
        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("stage_id required in multi-stage mode")
        return self.get_stage_workspace(sid) / "output"

    def get_stage_proc_dir(self, stage_id: Optional[str] = None) -> Path:
        """Get proc directory for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Path to stage proc directory (e.g., workspace_root/stage1_seed/proc/)
        """
        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("stage_id required in multi-stage mode")
        return self.get_stage_workspace(sid) / "proc"

    def get_stage_logs_dir(self, stage_id: Optional[str] = None) -> Path:
        """Get logs directory for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Path to stage logs directory (e.g., workspace_root/stage1_seed/logs/)
        """
        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("stage_id required in multi-stage mode")
        return self.get_stage_workspace(sid) / "logs"

    def get_mcp_config_path(self, stage_id: Optional[str] = None) -> str:
        """Get MCP config file path for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Absolute path to trial.mcp.json for the stage

        Example:
            >>> ctx.get_mcp_config_path()
            '/workspace/stage1_seed/trial.mcp.json'
        """
        if not self.is_multi_stage:
            # Single-stage mode: use the old path
            return str(Path(self._mcp_config))

        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("stage_id required in multi-stage mode")

        return str(self.get_stage_workspace(sid) / self._mcp_config)

    def get_prompt_rendered_path(self, stage_id: Optional[str] = None) -> str:
        """Get rendered prompt file path for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Absolute path to rendered prompt file

        Example:
            >>> # Multi-stage mode
            >>> ctx.get_prompt_rendered_path()
            '/workspace/stage1_seed_prompt_rendered.md'

            >>> # Single-stage mode
            >>> ctx.get_prompt_rendered_path()
            '/workspace/result/prompt_rendered.md'
        """
        if not self.is_multi_stage:
            # Single-stage mode: result/prompt_rendered.md
            return str(Path(self.result_path) / "prompt_rendered.md")

        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("stage_id required in multi-stage mode")

        # Multi-stage mode: {stage_id}_prompt_rendered.md in workspace_root
        return str(Path(self.workspace_root) / f"{sid}_prompt_rendered.md")

    def get_subagent_prompt_rendered_path(self, stage_id: Optional[str] = None) -> str:
        """Get rendered subagent prompt file path for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Absolute path to rendered subagent prompt file

        Example:
            >>> # Multi-stage mode
            >>> ctx.get_subagent_prompt_rendered_path()
            '/workspace/stage1_seed_subagent_prompt_rendered.md'
        """
        if not self.is_multi_stage:
            # Single-stage mode doesn't use subagent prompts
            return str(Path(self.result_path) / "subagent_prompt_rendered.md")

        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("stage_id required in multi-stage mode")

        # Multi-stage mode: {stage_id}_subagent_prompt_rendered.md in workspace_root
        return str(Path(self.workspace_root) / f"{sid}_subagent_prompt_rendered.md")

    def get_agent_output_path(self, filename: str) -> str:
        """Get agent output file path in result directory.

        Agent outputs (milestones.csv, dependencies.csv, etc.) are always
        written to result_path regardless of single/multi-stage mode.

        Args:
            filename: Output filename (e.g., "milestones.csv")

        Returns:
            Absolute path to agent output file

        Example:
            >>> ctx.get_agent_output_path("milestones.csv")
            '/workspace/result/milestones.csv'
        """
        return str(Path(self.result_path) / filename)

    # -------- Stage management and configuration access --------

    def set_current_stage(self, stage_id: str) -> None:
        """Set current stage ID and record it in execution history.

        Should be called by main_multi_stage.py before executing each stage.

        Args:
            stage_id: Stage ID to set as current

        Raises:
            KeyError: If stage_id not found in stage_configs
        """
        if stage_id not in self.stage_configs:
            raise KeyError(f"Stage config not found: {stage_id}")
        self.current_stage_id = stage_id
        # ✅ Record stage execution
        if stage_id not in self.executed_stages:
            self.executed_stages.append(stage_id)

    def get_prev_stage(self) -> Optional[str]:
        """Get previous stage ID from execution history.

        Returns:
            Previous stage ID, or None if this is the first stage
        """
        if len(self.executed_stages) < 2:
            return None
        return self.executed_stages[-2]

    def _get_stage_config(self, stage_id: Optional[str] = None) -> "StageConfig":
        """Get configuration for a stage (private method).

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            StageConfig for the stage

        Raises:
            ValueError: If current_stage_id not set
            KeyError: If stage_id not found
        """

        sid = stage_id or self.current_stage_id
        if not sid:
            raise ValueError("current_stage_id not set. Call set_current_stage() first in main_multi_stage.py")
        if sid not in self.stage_configs:
            raise KeyError(f"Stage config not found: {sid}")
        return self.stage_configs[sid]

    def get_shared_input_path(self, var_name: str) -> Optional[str]:
        """Get input path from shared config.

        Args:
            var_name: Variable name from shared config

        Returns:
            Absolute path or None if not found
        """
        if not self.shared_config:
            return None

        # ✅ Use StageConfig API
        filename = self.shared_config.get_input_filename(var_name)
        if filename:
            directory = self.shared_config.get_input_dir()
            return str(Path(self.workspace_root) / directory / filename)
        return None

    # -------- Unified path access methods (public API) --------

    def get_input(self, var_name: str) -> str:
        """Get absolute path for an input variable.

        Automatically searches in:
        1. Current stage input config
        2. Shared config (proc_data)

        Args:
            var_name: Variable name (e.g., "milestone_seeds", "commit_dag")

        Returns:
            Absolute path to input file/directory

        Raises:
            ValueError: If variable not found in any config

        Example:
            >>> ctx.get_input("milestone_seeds")  # from stage input
            '/workspace/stage2_mstone/input/milestone_seeds.jsonl'
            >>> ctx.get_input("commit_dag")       # from shared config (proc_data)
            '/workspace/proc_data/commit_dag.json'
        """
        stage_config = self._get_stage_config()

        # Try stage config first
        filename = stage_config.get_input_filename(var_name)
        if filename:
            input_dir = self.get_stage_input_dir()
            return str(input_dir / filename)

        # Fallback to shared config
        shared_path = self.get_shared_input_path(var_name)
        if shared_path:
            return shared_path

        raise ValueError(f"Input variable not found in configs: {var_name}")

    def get_output(self, var_name: str) -> str:
        """Get absolute path for an output variable.

        Args:
            var_name: Output variable name (e.g., "dependency_proposals_csv")

        Returns:
            Absolute path to output file/directory

        Raises:
            ValueError: If variable not found

        Example:
            >>> ctx.get_output("dependency_proposals_csv")
            '/workspace/stage2_mstone/output/dependency_proposals.csv'

        Note:
            - First tries to find variable in current stage config
            - Falls back to shared config if not found in stage config
            - Shared config outputs use result_path as base directory
        """
        stage_config = self._get_stage_config()
        filename = stage_config.get_output_filename(var_name)

        if filename:
            # Found in current stage config
            output_dir = self.get_stage_output_dir()
            return str(output_dir / filename)

        # Fallback to shared config
        if self.shared_config:
            filename = self.shared_config.get_output_filename(var_name)
            if filename:
                # Shared outputs go to global result directory
                output_dir = Path(self.result_path)
                return str(output_dir / filename)

        raise ValueError(f"Output variable not found: {var_name}")

    def get_repo(self) -> str:
        """Get path to repository directory.

        Returns:
            Absolute path to repo_data directory

        Example:
            >>> ctx.get_repo()
            '/workspace/repo_data'
        """
        return str(Path(self.workspace_root) / self._repo_dir)

    def get_preprocess(self) -> str:
        """Get path to preprocessed data directory.

        Returns:
            Absolute path to proc_data directory

        Example:
            >>> ctx.get_preprocess()
            '/workspace/proc_data'
        """
        return str(Path(self.workspace_root) / self._proc_dir)

    # -------- Prompt rendering methods --------

    def render_prompt(self, stage_id: Optional[str] = None, save: bool = True) -> Tuple[str, Dict[str, str]]:
        """Render prompt template for current stage and optionally save it.

        Args:
            stage_id: Stage ID, defaults to current_stage_id
            save: Whether to save rendered prompts to files (default: True)

        Returns:
            Tuple of (rendered_prompt, path_mapping)

        Note:
            In stage implementations, just call ctx.render_prompt()
            Rendered prompts are automatically saved to workspace_root by default.
        """
        stage_config = self._get_stage_config(stage_id)
        prompt_file = stage_config.get_prompt_file()

        if not prompt_file:
            raise ValueError(f"No prompt_file in stage config: {stage_config.stage_id}")

        # ✅ Check prompt file exists
        prompt_dir = Path(self.workspace_root) / self._prompt_dir
        prompt_path = prompt_dir / prompt_file
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {prompt_path}")

        # Build template variable mapping (paths + MCP tools + metadata)
        template_mapping = self._build_template_mapping(stage_config)

        # Create a clean copy for rendering (remove internal error flags)
        # Keep original mapping for return value so caller can check errors
        render_mapping = {k: v for k, v in template_mapping.items() if not k.startswith("__")}

        # Render subagent prompt if exists
        subagent_file = stage_config.get_subagent_prompt_file()
        if subagent_file:
            # ✅ Check subagent file exists
            subagent_template_path = prompt_dir / subagent_file
            if not subagent_template_path.exists():
                raise FileNotFoundError(f"Subagent prompt template not found: {subagent_template_path}")

            subagent_rendered = self._render_template(subagent_file, render_mapping)

            if save:
                # Save rendered subagent prompt
                subagent_rendered_path = (
                    Path(self.workspace_root) / f"{stage_config.stage_id}_subagent_prompt_rendered.md"
                )
                subagent_rendered_path.write_text(subagent_rendered, encoding="utf-8")
                # ✅ Add subagent_prompt_rendered_path to mapping
                template_mapping["subagent_prompt_rendered_path"] = str(subagent_rendered_path)

            # Update path mapping to point to rendered file
            template_mapping["subagent_prompt_file"] = f"{stage_config.stage_id}_subagent_prompt_rendered.md"
            render_mapping["subagent_prompt_file"] = template_mapping["subagent_prompt_file"]

        # Render deep analysis subagent prompt if exists (Stage 4 complex milestone analysis)
        deep_analysis_file = stage_config.get_deep_analysis_subagent_prompt_file()
        if deep_analysis_file:
            deep_analysis_template_path = prompt_dir / deep_analysis_file
            if not deep_analysis_template_path.exists():
                raise FileNotFoundError(f"Deep analysis subagent prompt not found: {deep_analysis_template_path}")

            deep_analysis_rendered = self._render_template(deep_analysis_file, render_mapping)

            if save:
                # Save rendered deep analysis subagent prompt
                deep_analysis_rendered_path = (
                    Path(self.workspace_root) / f"{stage_config.stage_id}_deep_analysis_subagent_rendered.md"
                )
                deep_analysis_rendered_path.write_text(deep_analysis_rendered, encoding="utf-8")
                template_mapping["deep_analysis_subagent_prompt_rendered_path"] = str(deep_analysis_rendered_path)

            # Update path mapping to point to rendered file
            template_mapping["deep_analysis_subagent_prompt"] = (
                f"{stage_config.stage_id}_deep_analysis_subagent_rendered.md"
            )
            render_mapping["deep_analysis_subagent_prompt"] = template_mapping["deep_analysis_subagent_prompt"]

        # Render main prompt (use clean mapping without internal flags)
        rendered = self._render_template(prompt_file, render_mapping)

        if save:
            # Save rendered main prompt
            rendered_path = Path(self.workspace_root) / f"{stage_config.stage_id}_prompt_rendered.md"
            rendered_path.write_text(rendered, encoding="utf-8")
            template_mapping["prompt_rendered_path"] = str(rendered_path)

        return rendered, template_mapping

    def _build_template_mapping(self, stage_config: "StageConfig") -> Dict[str, str]:
        """Build template variable mapping for template rendering.

        Constructs a complete mapping of template variables including:
        - Input/output file paths
        - Repository paths and metadata (repo_path, start_tag, end_tag, etc.)
        - MCP tool name mappings (e.g., get_commit_context → mcp__git_analysis__get_commit_context)
        - Backward compatibility aliases

        Args:
            stage_config: Stage configuration

        Returns:
            Dictionary mapping template variables to their values
        """
        mapping = {}

        # Add shared config paths (using StageConfig API)
        if self.shared_config:
            shared_input_dir = self.shared_config.get_input_dir()
            for var in self.shared_config.get_input_vars():
                filename = self.shared_config.get_input_filename(var)
                if filename:
                    mapping[var] = f"{shared_input_dir}/{filename}"

        # Add stage input paths (override shared)
        for var in stage_config.get_input_vars():
            filename = stage_config.get_input_filename(var)
            if filename:
                input_dir = stage_config.get_input_dir()
                mapping[var] = f"{stage_config.stage_id}/{input_dir}/{filename}"

        # Add stage output paths
        for var in stage_config.get_output_vars():
            filename = stage_config.get_output_filename(var)
            if filename:
                output_dir = stage_config.get_output_dir()
                mapping[var] = f"{stage_config.stage_id}/{output_dir}/{filename}"

        # Add common paths
        mapping["repo_path"] = str(self._repo_dir)
        mapping["result_path"] = f"{stage_config.stage_id}/output"

        # Add code commit SHA
        mapping["code_commit_sha"] = self.code_commit_sha

        # Add repo_src_dirs for LoC calculation (joined as comma-separated string)
        mapping["repo_src_dirs"] = ",".join(self.get_repo_src_dirs())

        # Add exclude_patterns for filtering out test/fixture files (joined as comma-separated string)
        mapping["exclude_patterns"] = ",".join(self.get_repo_exclude_patterns())

        # Add build environment configuration (for commit splitting verification)
        if self.base_image_name and not self.is_multi_repo:  # multi-repo: compile verification is skipped
            mapping["base_image_name"] = self.base_image_name
        if self.build_command and not self.is_multi_repo:
            mapping["build_command"] = self.build_command

        # Add scripts paths from stage config
        scripts = stage_config.get("scripts", {})
        if scripts and isinstance(scripts, dict):
            for script_name, script_path in scripts.items():
                mapping[script_name] = str(script_path)

        # Multi-repo workspace variables (always defined so prompts can reference them)
        mapping["is_multi_repo"] = "true" if self.is_multi_repo else "false"
        mapping["workspace_id"] = self.workspace_id or self.repo_id.replace(":", "_")
        mapping["workspace_repos_table"] = self.workspace_repos_table()

        # Add repo metadata
        if self.is_multi_repo:
            mapping["start_tag"] = ", ".join(f"{n}: {r['start']}" for n, r in self.repos.items())
            mapping["end_tag"] = ", ".join(f"{n}: {r['end']}" for n, r in self.repos.items())
            mapping["version_range"] = "; ".join(f"{n}: {r['start']} to {r['end']}" for n, r in self.repos.items())
            mapping["repo_name"] = ", ".join(f"{r['owner']}/{r['repo']}" for r in self.repos.values())
        elif ":" in self.repo_id:
            parts = self.repo_id.split(":", 1)[1].split("_")
            if len(parts) >= 2:
                mapping["start_tag"] = parts[0]
                mapping["end_tag"] = parts[1]
                mapping["version_range"] = f"{parts[0]} to {parts[1]}"

        if not self.is_multi_repo:
            repo_name = self.repo_id.split(":")[0] if ":" in self.repo_id else self.repo_id
            mapping["repo_name"] = repo_name.replace("_", "/", 1)  # Replace first underscore only

        # Add aliases for backward compatibility
        if "main_commits" in mapping:
            mapping["main_commits_with_dag"] = mapping["main_commits"]
            mapping["main_commits_refined"] = mapping["main_commits"]

        if "patches" in mapping:
            mapping["patch_dir"] = mapping["patches"]

        # Add MCP tool mappings
        try:
            mcp_tool_mappings = self.get_mcp_tool_mappings(stage_config.stage_id)
            mapping.update(mcp_tool_mappings)
        except ValueError as e:
            # If agents don't support MCP tools, store error for caller to log
            mapping["__mcp_tools_error__"] = str(e)

        return mapping

    def _render_template(self, template_file: str, path_mapping: Dict[str, str]) -> str:
        """Render a template file with path mapping.

        Args:
            template_file: Template filename (relative to prompts/)
            path_mapping: Dictionary mapping variables to paths

        Returns:
            Rendered template string
        """
        template_path = Path(self.workspace_root) / self._prompt_dir / template_file

        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        template_content = template_path.read_text(encoding="utf-8")

        from .utils.template import render_template_string

        return render_template_string(template_content, path_mapping)

    # -------- Helper methods --------

    def get_new_input_vars(self) -> Set[str]:
        """Calculate new input variables that need preprocessing.

        Returns variables in current stage input that are not in previous stage output.
        Automatically uses current_stage_id and get_prev_stage().

        Returns:
            Set of variable names that need preprocessing
        """
        current_config = self._get_stage_config()
        current_input_vars = current_config.get_input_vars()

        prev_stage = self.get_prev_stage()
        if not prev_stage:
            return current_input_vars

        prev_config = self._get_stage_config(prev_stage)
        prev_output_vars = prev_config.get_output_vars()

        return current_input_vars - prev_output_vars

    def get_mcp_tools(self, stage_id: Optional[str] = None) -> Dict[str, List[str]]:
        """Get MCP tools configuration for a stage.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Dict mapping MCP server name to list of tool names

        Example:
            >>> ctx.get_mcp_tools()
            {'git_analysis': ['get_commit_context', 'search_entities']}
        """
        stage_config = self._get_stage_config(stage_id)
        return stage_config.get_mcp_tools()

    def get_mcp_tool_mappings(self, stage_id: Optional[str] = None) -> Dict[str, str]:
        """Get MCP tool name mappings for template rendering.

        Reads MCP tools from stage_config and constructs a mapping from
        short method names to full platform-specific tool names.

        Args:
            stage_id: Stage ID, defaults to current_stage_id

        Returns:
            Dict mapping method name to full MCP tool name

        Raises:
            ValueError: If agents list does not contain claude/claude_code

        Example:
            >>> ctx.get_mcp_tool_mappings()
            {
                'get_commit_context': 'mcp__git_analysis__get_commit_context',
                'analyze_commit_coverage_tool': 'mcp__git_analysis__analyze_commit_coverage_tool'
            }
        """
        # Check if agents contains "claudecode"
        if "claudecode" not in self.agents:
            raise ValueError(f"Only claudecode agent is supported for MCP tools. Got agents={self.agents!r}")

        # Get MCP tools from stage config
        # Returns: {"git_analysis": ["get_commit_context", "search_entities", ...]}
        mcp_tools = self.get_mcp_tools(stage_id)

        # Build mapping: method_name -> mcp__server__method_name
        mapping: Dict[str, str] = {}
        for server, methods in mcp_tools.items():
            for method in methods:
                # Template format for Claude Code: mcp__<server>__<method>
                mapping[method] = f"mcp__{server}__{method}"

        return mapping

    def get_repo_src_dirs(self) -> List[str]:
        """Get repository source directories for LoC calculation.

        Returns:
            List of source directory paths (e.g., ["src", "lib"])
            Defaults to ["src"] if not specified

        Example:
            >>> ctx.get_repo_src_dirs()
            ['src/urllib3', 'lib/urllib3']
        """
        if self.repo_src_dirs and len(self.repo_src_dirs) > 0:
            return self.repo_src_dirs
        return ["src"]

    def get_repo_exclude_patterns(self) -> List[str]:
        """Get combined exclude patterns from test_dirs and exclude fields."""
        patterns: List[str] = []
        if self.test_dirs:
            patterns.extend([str(p) for p in self.test_dirs if str(p).strip()])
        if self.exclude:
            patterns.extend([str(p) for p in self.exclude if str(p).strip()])
        return patterns


# ✅ Rebuild model after StageConfig is imported to resolve forward references
from .stage_config import StageConfig  # noqa: E402

TrialContext.model_rebuild()
