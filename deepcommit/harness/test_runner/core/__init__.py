"""
Core components for test runner framework.
"""

from .classifier import TestClassifier
from .docker import (
    DockerExecutor,
    DockerRunner,
    build_docker_image,
    check_image_exists,
    cleanup_docker_image,
)
from .merger import ResultMerger, is_flaky, merge_outcome
from .types import (
    CommitTestConfig,
    MilestoneTestConfig,
    MilestoneTestMode,
    TestMode,
)

__all__ = [
    # Commit-level types
    "TestMode",
    "CommitTestConfig",
    # Milestone-level types
    "MilestoneTestMode",
    "MilestoneTestConfig",
    # Docker utilities
    "DockerExecutor",
    "DockerRunner",
    "check_image_exists",
    "build_docker_image",
    "cleanup_docker_image",
    # Merger utilities
    "ResultMerger",
    "merge_outcome",
    "is_flaky",
    # Classifier
    "TestClassifier",
]
