"""Stage configuration encapsulation.

Provides a clean interface to access stage-specific configuration.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from pydantic import BaseModel


class StageConfig(BaseModel):
    """Single stage configuration encapsulation.

    Provides convenient methods to access configuration values
    without hardcoding configuration keys.
    """

    stage_id: str
    config_file: str
    config_data: Dict[str, Any]

    # Configuration keys as class constants
    CFG_INPUT: str = "input"
    CFG_OUTPUT: str = "output"
    CFG_CHILDREN: str = "children"
    CFG_DIRECTORY: str = "directory"

    def get_input_vars(self) -> Set[str]:
        """Get all input variable names.

        Returns:
            Set of input variable names from config
        """
        input_section = self.config_data.get(self.CFG_INPUT, {})
        children = input_section.get(self.CFG_CHILDREN, {})
        return set(children.keys()) if children else set()

    def get_output_vars(self) -> Set[str]:
        """Get all output variable names.

        Returns:
            Set of output variable names from config
        """
        output_section = self.config_data.get(self.CFG_OUTPUT, {})
        children = output_section.get(self.CFG_CHILDREN, {})
        return set(children.keys()) if children else set()

    def get_input_filename(self, var_name: str) -> Optional[str]:
        """Get input filename for a variable.

        Args:
            var_name: Variable name from config (e.g., "milestone_seeds")

        Returns:
            Filename or None if not found
        """
        input_section = self.config_data.get(self.CFG_INPUT, {})
        children = input_section.get(self.CFG_CHILDREN, {})
        return children.get(var_name) if children else None

    def get_output_filename(self, var_name: str) -> Optional[str]:
        """Get output filename for a variable.

        Args:
            var_name: Variable name from config (e.g., "dependency_proposals_csv")

        Returns:
            Filename or None if not found
        """
        output_section = self.config_data.get(self.CFG_OUTPUT, {})
        children = output_section.get(self.CFG_CHILDREN, {})
        return children.get(var_name) if children else None

    def get_input_dir(self) -> str:
        """Get input directory name.

        Returns:
            Directory name (default: "input")
        """
        input_section = self.config_data.get(self.CFG_INPUT, {})
        result = input_section.get(self.CFG_DIRECTORY, "input")
        return str(result)

    def get_output_dir(self) -> str:
        """Get output directory name.

        Returns:
            Directory name (default: "output")
        """
        output_section = self.config_data.get(self.CFG_OUTPUT, {})
        result = output_section.get(self.CFG_DIRECTORY, "output")
        return str(result)

    def get_prompt_file(self) -> Optional[str]:
        """Get prompt template file.

        Returns:
            Prompt template filename or None
        """
        return self.config_data.get("prompt_file")

    def get_subagent_prompt_file(self) -> Optional[str]:
        """Get subagent prompt template file.

        Returns:
            Subagent prompt template filename or None
        """
        return self.config_data.get("subagent_prompt_file")

    def get_deep_analysis_subagent_prompt_file(self) -> Optional[str]:
        """Get deep analysis subagent prompt template file.

        Used in Stage 4 for complex milestone analysis before splitting.

        Returns:
            Deep analysis subagent prompt template filename or None
        """
        return self.config_data.get("deep_analysis_subagent_prompt_file")

    def get_mcp_tools(self) -> Dict[str, list[str]]:
        """Get MCP tools configuration for this stage.

        Returns:
            Dict mapping MCP server name to list of tool names
            Example: {"git_analysis": ["get_commit_context", "search_entities"]}
        """
        tools = self.config_data.get("mcp_tools", {})
        if isinstance(tools, dict):
            return tools
        # Backward compatibility: if it's a list, assume git_analysis
        elif isinstance(tools, list):
            return {"git_analysis": tools} if tools else {}
        return {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get arbitrary configuration value.

        Args:
            key: Configuration key
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        return self.config_data.get(key, default)
