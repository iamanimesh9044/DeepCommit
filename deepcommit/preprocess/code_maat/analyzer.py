"""Code Maat analyzer for evolutionary coupling and complexity analysis."""

import csv
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adaptive_params import AdaptiveParamsCalculator


class CodeMaatAnalyzer:
    """Wrapper for Code Maat analysis tool."""

    DEFAULT_CODEMAAT_JAR = "bin/code-maat-1.0.4-standalone.jar"

    # Default parameters based on empirical analysis
    DEFAULT_HOTSPOTS_ROWS = 50  # Top 50 files
    DEFAULT_COUPLING_ROWS = 30  # Top 30 coupling pairs
    DEFAULT_COUPLING_MIN = 40  # Minimum 40% coupling

    def __init__(
        self,
        log_file: Path,
        output_dir: Path,
        codemaat_jar: Optional[Path] = None,
        verbose: bool = False,
        use_adaptive_params: bool = True,
    ):
        """
        Initialize Code Maat analyzer.

        Args:
            log_file: Path to git log file (git2 format)
            output_dir: Directory to save analysis results
            codemaat_jar: Path to Code Maat JAR file (default: bin/code-maat-1.0.4-standalone.jar)
            verbose: Print detailed output
            use_adaptive_params: Automatically calculate optimal parameters based on repository size
        """
        self.log_file = Path(log_file)
        self.output_dir = Path(output_dir)
        self.codemaat_jar = Path(codemaat_jar or self.DEFAULT_CODEMAAT_JAR)
        self.verbose = verbose
        self.use_adaptive_params = use_adaptive_params

        if not self.log_file.exists():
            raise FileNotFoundError(f"Log file not found: {self.log_file}")

        if not self.codemaat_jar.exists():
            raise FileNotFoundError(f"Code Maat JAR not found: {self.codemaat_jar}")

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Calculate adaptive parameters if enabled
        self.adaptive_params = None
        if self.use_adaptive_params:
            self._calculate_adaptive_params()

    def _calculate_adaptive_params(self):
        """Calculate optimal parameters based on repository characteristics."""
        calculator = AdaptiveParamsCalculator(self.log_file)
        self.adaptive_params = calculator.calculate_all_params()

        if self.verbose:
            print(calculator.explain_params())

    def _run_analysis(
        self,
        analysis_type: str,
        output_file: Path,
        extra_args: Optional[List[str]] = None,
    ) -> bool:
        """
        Run Code Maat analysis.

        Args:
            analysis_type: Type of analysis (revisions, coupling, abs-churn, etc.)
            output_file: Path to save output CSV
            extra_args: Additional command line arguments

        Returns:
            True if successful, False otherwise
        """
        cmd = [
            "java",
            "-jar",
            str(self.codemaat_jar),
            "-l",
            str(self.log_file),
            "-c",
            "git2",
            "-a",
            analysis_type,
        ]

        if extra_args:
            cmd.extend(extra_args)

        if self.verbose:
            print(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )

            # Write output to file
            output_file.write_text(result.stdout)

            if self.verbose:
                print(f"✓ Generated: {output_file}")

            return True

        except subprocess.CalledProcessError as e:
            print(f"Error running {analysis_type} analysis:")
            print(f"  {e.stderr}")
            return False

    def run_hotspots(self, rows: Optional[int] = None) -> Optional[Path]:
        """
        Run hotspots (revisions) analysis.

        Args:
            rows: Number of top files to return (default: adaptive or 50)

        Returns:
            Path to output file if successful, None otherwise
        """
        # Use adaptive params if available and no explicit rows specified
        if rows is None and self.adaptive_params:
            rows = self.adaptive_params["hotspots"]["rows"]
        else:
            rows = rows or self.DEFAULT_HOTSPOTS_ROWS

        output_file = self.output_dir / "hotspots.csv"

        extra_args = ["-r", str(rows)]
        success = self._run_analysis("revisions", output_file, extra_args)

        return output_file if success else None

    def run_coupling(
        self,
        rows: Optional[int] = None,
        min_coupling: Optional[int] = None,
    ) -> Optional[Path]:
        """
        Run coupling analysis.

        Args:
            rows: Number of top coupling pairs to return (default: adaptive or 30)
            min_coupling: Minimum coupling percentage (default: adaptive or 40)

        Returns:
            Path to output file if successful, None otherwise
        """
        # Use adaptive params if available and no explicit values specified
        if rows is None and self.adaptive_params:
            rows = self.adaptive_params["coupling"]["rows"]
        else:
            rows = rows or self.DEFAULT_COUPLING_ROWS

        if min_coupling is None and self.adaptive_params:
            min_coupling = self.adaptive_params["coupling"]["min_coupling"]
        else:
            min_coupling = min_coupling or self.DEFAULT_COUPLING_MIN

        output_file = self.output_dir / "coupling.csv"

        extra_args = ["-r", str(rows), "-i", str(min_coupling)]
        success = self._run_analysis("coupling", output_file, extra_args)

        return output_file if success else None

    def run_temporal_churn(self) -> Optional[Path]:
        """
        Run temporal churn (abs-churn) analysis.

        Returns:
            Path to output file if successful, None otherwise
        """
        output_file = self.output_dir / "temporal_churn.csv"
        success = self._run_analysis("abs-churn", output_file)

        return output_file if success else None

    def run_entity_churn(self, rows: Optional[int] = None) -> Optional[Path]:
        """
        Run entity churn analysis.

        Args:
            rows: Number of top files to return (default: adaptive or 50)

        Returns:
            Path to output file if successful, None otherwise
        """
        # Use adaptive params if available and no explicit rows specified
        if rows is None and self.adaptive_params:
            rows = self.adaptive_params["entity_churn"]["rows"]
        else:
            rows = rows or self.DEFAULT_HOTSPOTS_ROWS

        output_file = self.output_dir / "entity_churn.csv"

        extra_args = ["-r", str(rows)]
        success = self._run_analysis("entity-churn", output_file, extra_args)

        return output_file if success else None

    def _csv_to_dict_list(self, csv_file: Path) -> List[Dict[str, Any]]:
        """Convert CSV file to list of dictionaries."""
        if not csv_file.exists():
            return []

        with open(csv_file, "r") as f:
            reader = csv.DictReader(f)
            return list(reader)

    def run_all_analyses(
        self,
        generate_json: bool = True,
    ) -> Dict[str, Any]:
        """
        Run all standard Code Maat analyses.

        Args:
            generate_json: Generate codemaat_analysis.json summary

        Returns:
            Dictionary with analysis results and file paths
        """
        results = {
            "hotspots": None,
            "coupling": None,
            "temporal_churn": None,
            "entity_churn": None,
            "json_summary": None,
        }

        print("Running Code Maat analyses...")

        # Run analyses
        hotspots_file = self.run_hotspots()
        if hotspots_file:
            results["hotspots"] = hotspots_file

        coupling_file = self.run_coupling()
        if coupling_file:
            results["coupling"] = coupling_file

        temporal_file = self.run_temporal_churn()
        if temporal_file:
            results["temporal_churn"] = temporal_file

        entity_file = self.run_entity_churn()
        if entity_file:
            results["entity_churn"] = entity_file

        # Generate JSON summary
        if generate_json:
            json_file = self._generate_json_summary(results)
            results["json_summary"] = json_file

        return results

    def _generate_json_summary(self, results: Dict[str, Any]) -> Optional[Path]:
        """
        Generate JSON summary of all analyses.

        Args:
            results: Dictionary with analysis file paths

        Returns:
            Path to JSON file if successful, None otherwise
        """
        summary = {}

        # Load hotspots
        if results.get("hotspots"):
            hotspots_data = self._csv_to_dict_list(results["hotspots"])
            summary["hotspots"] = {
                "top_20": hotspots_data[:20],
                "top_50": hotspots_data[:50],
                "all": hotspots_data,
            }

        # Load coupling
        if results.get("coupling"):
            coupling_data = self._csv_to_dict_list(results["coupling"])
            summary["coupling"] = {
                "threshold_40": coupling_data,
            }

        # Load temporal churn
        if results.get("temporal_churn"):
            temporal_data = self._csv_to_dict_list(results["temporal_churn"])
            summary["temporal_churn"] = {
                "all": temporal_data,
            }

        # Load entity churn
        if results.get("entity_churn"):
            entity_data = self._csv_to_dict_list(results["entity_churn"])
            summary["entity_churn"] = {
                "top_20": entity_data[:20],
                "top_50": entity_data[:50],
                "all": entity_data,
            }

        # Add adaptive parameters metadata if available
        if self.adaptive_params:
            summary["_metadata"] = {
                "adaptive_params_used": True,
                "parameters": self.adaptive_params,
            }
        else:
            summary["_metadata"] = {
                "adaptive_params_used": False,
            }

        # Save JSON
        json_file = self.output_dir / "codemaat_analysis.json"
        with open(json_file, "w") as f:
            json.dump(summary, f, indent=2)

        if self.verbose:
            print(f"✓ Generated JSON summary: {json_file}")

        return json_file
