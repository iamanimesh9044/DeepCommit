"""Adaptive parameter calculation for Code Maat analyses."""

from collections import Counter
from pathlib import Path
from typing import Dict, Tuple


class AdaptiveParamsCalculator:
    """Calculate optimal Code Maat parameters based on repository characteristics."""

    # Coverage targets (what percentage of active files/commits to include)
    # NOTE: Higher coverage for LLM agents - they can filter, but can't add missing info
    HOTSPOTS_COVERAGE_TARGET = 0.90  # 90% of modified files (increased for agent input)
    COUPLING_COVERAGE_TARGET = 0.80  # 80% of potential coupling pairs (increased)
    ENTITY_CHURN_COVERAGE_TARGET = 0.85  # 85% of churned files (increased)

    # Absolute bounds
    MIN_HOTSPOTS_ROWS = 20  # Increased from 10 - ensure sufficient context
    MAX_HOTSPOTS_ROWS = 100
    MIN_COUPLING_ROWS = 15  # Increased from 10
    MAX_COUPLING_ROWS = 50
    MIN_COUPLING_THRESHOLD = 30  # Minimum coupling percentage
    MAX_COUPLING_THRESHOLD = 60  # Maximum coupling percentage

    def __init__(self, log_file: Path):
        """
        Initialize calculator with Code Maat log file.

        Args:
            log_file: Path to git2 format log file
        """
        self.log_file = Path(log_file)
        self._stats = None

    def _parse_log(self) -> Dict:
        """
        Parse Code Maat log to extract statistics.

        Returns:
            Dictionary with statistics:
            - commits_count: Total number of commits
            - files_modified: Set of all modified files
            - file_change_counts: Counter of how many times each file was modified
            - total_changes: Total number of file changes
        """
        if not self.log_file.exists():
            raise FileNotFoundError(f"Log file not found: {self.log_file}")

        commits_count = 0
        files_modified = set()
        file_change_counts = Counter()
        total_changes = 0

        current_commit = None

        with open(self.log_file, "r") as f:
            for line in f:
                line = line.strip()

                # Commit header: --hash--date--author
                if line.startswith("--"):
                    commits_count += 1
                    current_commit = line
                    continue

                # Empty line (commit separator)
                if not line:
                    current_commit = None
                    continue

                # File change line: additions\tdeletions\tfilepath
                if current_commit and "\t" in line:
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        filepath = parts[2]
                        files_modified.add(filepath)
                        file_change_counts[filepath] += 1
                        total_changes += 1

        return {
            "commits_count": commits_count,
            "files_modified": files_modified,
            "file_change_counts": file_change_counts,
            "total_changes": total_changes,
        }

    def get_stats(self) -> Dict:
        """Get repository statistics (cached)."""
        if self._stats is None:
            self._stats = self._parse_log()
        return self._stats

    def calculate_hotspots_rows(self) -> int:
        """
        Calculate optimal number of rows for hotspots analysis.

        Strategy:
        - Small repos (<50 commits): Cover 95% of files
        - Medium repos (50-150 commits): Cover 90% of files
        - Large repos (>150 commits): Cover 80% of files
        - Never exceed 100 or go below 20

        Returns:
            Optimal number of rows for -r parameter
        """
        stats = self.get_stats()
        commits_count = stats["commits_count"]
        files_count = len(stats["files_modified"])

        # Adjust coverage based on repo size (higher for agent consumption)
        if commits_count < 50:
            coverage = 0.95
        elif commits_count < 150:
            coverage = self.HOTSPOTS_COVERAGE_TARGET  # 0.90
        else:
            coverage = 0.80

        # Calculate target based on coverage
        target = int(files_count * coverage)

        # Apply bounds
        result = max(self.MIN_HOTSPOTS_ROWS, min(target, self.MAX_HOTSPOTS_ROWS))

        # Ensure we don't exceed total file count
        result = min(result, files_count)

        return result

    def calculate_coupling_params(self) -> Tuple[int, int]:
        """
        Calculate optimal parameters for coupling analysis.

        Strategy:
        - Rows: Based on number of highly-modified files (>2 changes)
        - Threshold: Higher for small repos (less noise), lower for large repos

        Returns:
            Tuple of (rows, min_coupling_percentage)
        """
        stats = self.get_stats()
        commits_count = stats["commits_count"]
        file_change_counts = stats["file_change_counts"]

        # Count "active" files (modified more than twice)
        active_files = sum(1 for count in file_change_counts.values() if count > 2)

        # Rows: Proportional to active files (higher ratios for agent consumption)
        if commits_count < 50:
            # Small repos: Cover more coupling pairs
            rows_ratio = 0.90
        elif commits_count < 150:
            rows_ratio = self.COUPLING_COVERAGE_TARGET  # 0.80
        else:
            # Large repos: Still focus on more couplings
            rows_ratio = 0.70

        rows = int(active_files * rows_ratio)
        rows = max(self.MIN_COUPLING_ROWS, min(rows, self.MAX_COUPLING_ROWS))

        # Threshold: Higher for small repos to reduce noise
        if commits_count < 50:
            threshold = 50  # 50% minimum coupling
        elif commits_count < 150:
            threshold = 40  # 40% minimum coupling
        else:
            threshold = 30  # 30% minimum coupling (more permissive)

        threshold = max(self.MIN_COUPLING_THRESHOLD, min(threshold, self.MAX_COUPLING_THRESHOLD))

        return (rows, threshold)

    def calculate_entity_churn_rows(self) -> int:
        """
        Calculate optimal number of rows for entity churn analysis.

        Strategy:
        - Focus on files with changes (>2 modifications, relaxed from >3)
        - Cover 85% of frequently-changed files (increased from 75%)

        Returns:
            Optimal number of rows for -r parameter
        """
        stats = self.get_stats()
        file_change_counts = stats["file_change_counts"]

        # Count files with significant activity (>2 changes, relaxed threshold)
        significant_files = sum(1 for count in file_change_counts.values() if count > 2)

        # If few files with significant activity, use broader threshold
        if significant_files < 15:
            significant_files = sum(1 for count in file_change_counts.values() if count > 1)

        # Target 85% coverage of significant files (increased for agent)
        target = int(significant_files * self.ENTITY_CHURN_COVERAGE_TARGET)

        # Apply bounds
        result = max(self.MIN_HOTSPOTS_ROWS, min(target, self.MAX_HOTSPOTS_ROWS))

        # Ensure we don't exceed total file count
        result = min(result, len(stats["files_modified"]))

        return result

    def calculate_all_params(self) -> Dict[str, Dict]:
        """
        Calculate all optimal parameters for Code Maat analyses.

        Returns:
            Dictionary with parameters for each analysis type:
            {
                'hotspots': {'rows': int},
                'coupling': {'rows': int, 'min_coupling': int},
                'entity_churn': {'rows': int},
                'stats': {...}  # Repository statistics
            }
        """
        stats = self.get_stats()

        hotspots_rows = self.calculate_hotspots_rows()
        coupling_rows, coupling_threshold = self.calculate_coupling_params()
        entity_churn_rows = self.calculate_entity_churn_rows()

        return {
            "hotspots": {
                "rows": hotspots_rows,
            },
            "coupling": {
                "rows": coupling_rows,
                "min_coupling": coupling_threshold,
            },
            "entity_churn": {
                "rows": entity_churn_rows,
            },
            "stats": {
                "commits_count": stats["commits_count"],
                "files_count": len(stats["files_modified"]),
                "total_changes": stats["total_changes"],
                "avg_files_per_commit": (
                    stats["total_changes"] / stats["commits_count"] if stats["commits_count"] > 0 else 0
                ),
            },
        }

    def explain_params(self) -> str:
        """
        Generate human-readable explanation of parameter choices.

        Returns:
            Explanation string
        """
        params = self.calculate_all_params()
        stats = params["stats"]

        explanation = []
        explanation.append("=" * 70)
        explanation.append("Adaptive Code Maat Parameters (Agent-Optimized)")
        explanation.append("=" * 70)
        explanation.append("")
        explanation.append("Repository Statistics:")
        explanation.append(f"  Commits: {stats['commits_count']}")
        explanation.append(f"  Files modified: {stats['files_count']}")
        explanation.append(f"  Total file changes: {stats['total_changes']}")
        explanation.append(f"  Avg files per commit: {stats['avg_files_per_commit']:.1f}")
        explanation.append("")
        explanation.append("Calculated Parameters (Higher coverage for LLM agents):")
        explanation.append(f"  Hotspots (-r): {params['hotspots']['rows']}")
        explanation.append(
            f"    → Covers ~{params['hotspots']['rows'] / stats['files_count'] * 100:.0f}% of modified files"
        )
        explanation.append("")
        explanation.append(f"  Coupling (-r): {params['coupling']['rows']}")
        explanation.append(f"  Coupling (-i): {params['coupling']['min_coupling']}%")
        explanation.append(f"    → Minimum {params['coupling']['min_coupling']}% coupling threshold")
        explanation.append("")
        explanation.append(f"  Entity Churn (-r): {params['entity_churn']['rows']}")
        explanation.append("    → Covers high-activity files (>2 modifications)")
        explanation.append("")
        explanation.append("Note: Higher coverage ensures agents have sufficient context")
        explanation.append("=" * 70)

        return "\n".join(explanation)
