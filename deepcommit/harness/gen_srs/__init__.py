"""
SRS (Software Requirements Specification) Generation Module

This module contains tools for generating requirement documents from
fail-to-pass test code and milestone commit patches.

Tools:
- ../filter_tests/extract_f2p_tests.py: Extract fail-to-pass test code from classification results (moved to filter_tests)
- prepare_prompt.py: Prepare complete SRS generation prompts
- srs_gen_v1.tmpl: Unified template for SRS generation (includes data inputs and guidelines)
"""

__version__ = "1.0.0"
