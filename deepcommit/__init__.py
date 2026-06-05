"""
DeepCommit - Git Repository Analysis Framework
"""

__version__ = "0.1.0"

# Make submodules accessible
from . import pipeline, preprocess, tools, utils

__all__ = ["utils", "tools", "preprocess", "pipeline", "__version__"]
