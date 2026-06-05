"""
Test Runner Module

A unified framework for running tests at different levels:
- Single state: Use SingleStateTestRunner for base/final image validation
- Commit level: Use run_commit_tests.py CLI
- Milestone level: Use run_milestone_tests.py CLI
"""

from .core.classifier import TestClassifier
from .core.docker import DockerExecutor, DockerRunner
from .core.merger import ResultMerger
from .core.report_parser import merge_test_reports, parse_test_report
from .core.types import BaseValidationConfig, CommitTestConfig, TestMode
from .single_state_runner import SingleStateTestRunner, TestRunResult

__all__ = [
    # Types
    "TestMode",
    "CommitTestConfig",
    "BaseValidationConfig",
    # Docker utilities
    "DockerRunner",
    "DockerExecutor",
    # Test execution
    "SingleStateTestRunner",
    "TestRunResult",
    # Result processing
    "ResultMerger",
    "TestClassifier",
    "parse_test_report",
    "merge_test_reports",
]
