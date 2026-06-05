"""
Build milestone Docker images using a CLI coding agent.

This module provides tools to:
1. Generate environment setup prompts for milestones
2. Run Codex or Claude agent to create Dockerfiles
3. Validate built images with test runner
"""

from .render_prompt import render_env_setup_prompt

__all__ = ["render_env_setup_prompt"]
