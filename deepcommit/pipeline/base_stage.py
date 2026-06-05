"""
Base Stage Template Class

Provides a template for all pipeline stages with unified error handling and flow control.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional, Union

from .context import TrialContext
from .stages.agent import run_agent
from .stages.tools import configure_tools
from .types import StageResult

if TYPE_CHECKING:
    from .harness_context import HarnessContext
    from .utils.pipeline_logger import PipelineLoggerAdapter


class BaseStage(ABC):
    """Abstract base class for all pipeline stages.

    Implements the Template Method pattern:
    - run_stage() defines the fixed workflow
    - Subclasses implement specific hook methods

    All error handling is centralized in run_stage().

    Compatible with both TrialContext (multi-stage) and HarnessContext (simplified).
    """

    def __init__(self, ctx: Union[TrialContext, "HarnessContext"], logger: "PipelineLoggerAdapter"):
        """Initialize stage with context and logger.

        Args:
            ctx: Trial context with stage configuration
            logger: Pipeline logger instance
        """
        self.ctx = ctx
        self.logger = logger

    # ═══════════════════════════════════════════════════════════════
    # Concrete Properties - Available to all subclasses
    # ═══════════════════════════════════════════════════════════════

    @property
    def stage_id(self) -> str:
        """Return the stage ID from context (e.g., 'stage1_seed')."""
        return self.ctx.current_stage_id or "unknown"

    # ═══════════════════════════════════════════════════════════════
    # Template Method - Fixed workflow with centralized error handling
    # ═══════════════════════════════════════════════════════════════

    def run_stage(self) -> StageResult:
        """Execute the stage workflow (Template Method).

        This method defines the fixed execution flow:
        1. Setup input data
        2. Render prompt
        3. Configure tools
        4. Run agent
        5. Validate outputs
        6. Archive outputs

        Exceptions are propagated to the caller for centralized error handling.

        Returns:
            StageResult with status and artifacts

        Raises:
            Exception: Any exception from sub-steps is propagated upward
        """
        import uuid

        self.logger.info(f"Stage: {self.stage_id}")

        # Step 1: Setup input data
        self._setup_input_data()

        # Step 2: Render prompt
        self._render_prompt()

        # Step 3: Configure tools
        res_tools = self._configure_tools()
        if res_tools.status == "error":
            return res_tools

        # Step 4: Run agent (first time - new session)
        session_id = str(uuid.uuid4())
        self.logger.info(f"Created session ID: {session_id}")
        res_agent = self._run_agent(session_id=session_id, resume=False)
        if res_agent.status == "error":
            self.logger.error(f"Agent execution failed: {res_agent.error}")
            return res_agent

        # Step 5: Validate outputs with auto-retry
        max_retries = self.ctx.max_retries
        retry_count = 0
        agent_cli_status = res_agent.artifacts.get("agent_cli_status", "ok")

        while retry_count <= max_retries:
            # Check if timeout occurred
            if agent_cli_status == "timeout":
                if retry_count == max_retries:
                    self.logger.error(f"✗ Agent timed out after {max_retries} retry(ies)")
                    return StageResult(status="error", error="Agent execution timed out after max retries")

                # Handle timeout: retry with simple "continue" prompt
                retry_count += 1
                self.logger.warning(
                    f"⚠ Agent timed out (attempt {retry_count}/{max_retries}), retrying with 'continue'..."
                )

                # Simple continue prompt for timeout
                continue_prompt = "continue"

                # Re-run agent in resume mode with continue prompt
                res_agent = self._run_agent(session_id=session_id, resume=True, override_prompt=continue_prompt)

                if res_agent.status == "error":
                    self.logger.error("Retry after timeout failed: agent returned error")
                    return res_agent

                # Update agent status for next iteration
                agent_cli_status = res_agent.artifacts.get("agent_cli_status", "ok")
                continue  # Skip validation, go to next iteration

            # Validate outputs
            validation_error = self._validate_outputs()

            if not validation_error:
                # Validation passed
                if retry_count > 0:
                    self.logger.info(f"✓ Validation passed after {retry_count} retry(ies)")
                break

            # Validation failed
            if retry_count == max_retries:
                # Max retries reached
                self.logger.error(f"✗ Validation failed after {max_retries} retry(ies)")
                return StageResult(status="error", error=f"Output validation failed: {validation_error}")

            # Retry with error feedback
            retry_count += 1
            self.logger.warning(f"⚠ Validation failed (attempt {retry_count}/{max_retries}): {validation_error}")
            self.logger.info(f"Retrying with error feedback (session: {session_id[:8]}...)")

            # Build fix prompt
            fix_prompt = self._build_fix_prompt(validation_error)

            # Re-run agent in resume mode with fix prompt
            res_agent = self._run_agent(session_id=session_id, resume=True, override_prompt=fix_prompt)

            if res_agent.status == "error":
                self.logger.error("Retry failed: agent returned error")
                return res_agent

            # Update agent status for next iteration
            agent_cli_status = res_agent.artifacts.get("agent_cli_status", "ok")

        # Step 6: Archive outputs
        saved_files = self._archive_outputs()

        # Success
        return StageResult(
            status="ok",
            artifacts={
                "output_dir": str(self.ctx.get_stage_output_dir()),
                "output_files": saved_files,
                "session_id": session_id,
            },
            notes=f"Stage completed, {len(saved_files)} output file(s) saved",
        )

    # ═══════════════════════════════════════════════════════════════
    # Abstract Hook Methods - Subclass must implement
    # ═══════════════════════════════════════════════════════════════

    @abstractmethod
    def _setup_input_data(self) -> None:
        """Setup input data (preprocessing, generation, etc.).

        This method should:
        - Check for new input variables via ctx.get_new_input_vars()
        - Generate or preprocess required input files
        - Raise exception on failure (handled by template method)
        """
        pass

    @abstractmethod
    def _validate_outputs(self) -> Optional[str]:
        """Validate agent-generated output files.

        This method should:
        - Check output files exist and have correct format
        - Validate data integrity (unique IDs, references, etc.)
        - Return error message string if validation fails
        - Return None if validation succeeds
        - Raise exception for unexpected errors (handled by template method)

        Returns:
            Error message if validation fails, None otherwise
        """
        pass

    @abstractmethod
    def _archive_outputs(self) -> List[str]:
        """Archive output files to the stage output directory.

        This method should:
        - Move/copy output files to stage output directory
        - Return list of successfully archived filenames
        - Raise exception on failure (handled by template method)

        Returns:
            List of archived filenames
        """
        pass

    # ═══════════════════════════════════════════════════════════════
    # Concrete Helper Methods - Subclass can use directly
    # ═══════════════════════════════════════════════════════════════

    def _render_prompt(self) -> None:
        """Render prompt template (common implementation).

        Raises:
            Exception if rendering fails
        """
        self.logger.info("Rendering prompt template...")
        rendered_prompt, path_mapping = self.ctx.render_prompt()

        # Check if MCP tool mapping failed - abort if so
        if "__mcp_tools_error__" in path_mapping:
            error_msg = path_mapping["__mcp_tools_error__"]
            self.logger.error(f"Failed to load MCP tool mappings: {error_msg}")
            raise RuntimeError(f"MCP tool mapping failed: {error_msg}. Cannot proceed without MCP tools.")

        self.logger.info(f"Rendered prompt saved to: {path_mapping.get('prompt_rendered_path', 'N/A')}")

        # Check if subagent prompt was also rendered
        if "subagent_prompt_rendered_path" in path_mapping:
            self.logger.info(f"Rendered subagent prompt saved to: {path_mapping['subagent_prompt_rendered_path']}")

        # Check if deep analysis subagent prompt was also rendered (Stage 4)
        if "deep_analysis_subagent_prompt_rendered_path" in path_mapping:
            self.logger.info(
                f"Rendered deep analysis subagent prompt saved to: "
                f"{path_mapping['deep_analysis_subagent_prompt_rendered_path']}"
            )

    def _configure_tools(self) -> StageResult:
        """Configure MCP tools (common implementation).

        Returns:
            StageResult from tool configuration
        """
        self.logger.info("Configuring MCP tools...")
        return configure_tools(self.ctx, self.logger)

    def _run_agent(
        self,
        session_id: str,
        resume: bool = False,
        override_prompt: Optional[str] = None,
    ) -> StageResult:
        """Run agent with optional fix/retry mode.

        Args:
            session_id: Session ID (UUID) - REQUIRED
            resume: If True, resume existing session; if False, create new session
            override_prompt: If provided, use this instead of rendered prompt (for fix/retry mode)

        Returns:
            StageResult from agent execution
        """
        if resume:
            self.logger.info(f"Running agent in RESUME MODE (session: {session_id[:8]}...)")
        else:
            self.logger.info(f"Running agent with new session (session: {session_id[:8]}...)")
        return run_agent(
            self.ctx,
            self.logger,
            session_id=session_id,
            resume=resume,
            override_prompt=override_prompt,
        )

    def _build_fix_prompt(self, validation_error: str) -> str:
        """Build fix prompt from validation error.

        Args:
            validation_error: Error message from _validate_outputs()

        Returns:
            Fix prompt for agent
        """
        return f"""❌ **Output Validation Failed**

The outputs you generated failed validation with the following error:

```
{validation_error}
```

**Your Task**:
1. Review the error message carefully
2. Fix the issues in the output files
3. Self-validate your fixes against the requirements
4. Ensure all outputs pass validation
"""

    def validate(self) -> Optional[str]:
        """Validate stage outputs (reusable for recovery).

        This method can be called independently to check if previous
        stage execution produced valid outputs, enabling checkpoint recovery.

        Returns:
            Error message if validation fails, None if valid
        """
        return self._validate_outputs()
