from pathlib import Path
from typing import Optional

from ...utils.data.data_extractor import DataExtractor
from ...utils.data.index_tool import IndexTool
from ...utils.git.local_git_repo import LocalGitRepo


class RepositoryAnalyzer:
    """Base class for repository analysis operations"""

    def __init__(self, repo_path: str, data_dir: Optional[str] = None):
        """
        Initialize repository analyzer

        Args:
            repo_path: Path to the git repository
            data_dir: Optional path to data directory for this repository
        """
        self.repo_path = repo_path
        self.git_repo = LocalGitRepo(self.repo_path)

        if data_dir and Path(data_dir).exists():
            self.index_tool = IndexTool(data_dir)
            try:
                self.index_tool.load_index()
            except:  # noqa: E722
                # TODO: Build index if not available
                pass

            # Initialize data extractor for release, PR, and issue information
            try:
                self.data_extractor = DataExtractor(data_dir)
            except ValueError:
                self.data_extractor = None
        else:
            self.index_tool = None
            self.data_extractor = None
