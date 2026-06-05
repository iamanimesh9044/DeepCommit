#!/usr/bin/env python3
"""
Extract fail-to-pass test code and analyze milestone statistics.

This script provides two independent modes:

1. EXTRACTION MODE (default):
   - Extracts fail_to_pass test code from specified milestones
   - Supports cross-file imports, inheritance, and parametrized tests
   - Saves tests to {work_dir}/test_results/{milestone_id}/benchmark/f2p_tests/
   - Automatically shows extraction statistics after completion

2. REPORT MODE (--report):
   - Analyzes ALL milestone test results (not specific ones)
   - Detects issues (empty, error, warning status)
   - Always includes f2p extraction status check
   - Generates comprehensive report

Note: These two modes are mutually exclusive. Use shell && to combine:
  ./script.py /path M001 M002 && ./script.py /path --report

Enhanced Extraction Features:
- Import resolution: Follow cross-file imports to find class definitions
- Inheritance resolution: Search parent classes for inherited methods
- Parametrized test support: Extract tests with @pytest.mark.parametrize decorators
"""

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


class TestIDParser:
    """Parse pytest test ID into components."""

    @staticmethod
    def parse(test_id: str) -> Dict[str, Optional[str]]:
        """
        Parse test ID like 'test/path/file.py::ClassName::test_method'

        Returns:
            Dict with keys: file_path, class_name, function_name
        """
        parts = test_id.split("::")

        result = {
            "file_path": parts[0],
            "class_name": None,
            "function_name": None,
        }

        if len(parts) == 2:
            # Format: file.py::test_function
            result["function_name"] = parts[1]
        elif len(parts) == 3:
            # Format: file.py::ClassName::test_method
            result["class_name"] = parts[1]
            result["function_name"] = parts[2]

        return result


class TestCodeExtractor:
    """Extract test code from Python files using AST."""

    def __init__(self, file_path: str):
        """Initialize extractor with file path."""
        self.file_path = file_path
        with open(file_path, "r", encoding="utf-8") as f:
            self.source_code = f.read()
        self.tree = ast.parse(self.source_code)

    def extract_imports(self) -> str:
        """Extract all import statements from the file."""
        imports = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_line = ast.get_source_segment(self.source_code, node)
                if import_line:
                    imports.append(import_line)
        return "\n".join(imports)

    def extract_class(self, class_name: str) -> Optional[str]:
        """Extract a class definition by name."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return ast.get_source_segment(self.source_code, node)
        return None

    def extract_function(self, function_name: str) -> Optional[str]:
        """Extract a top-level function definition by name."""
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                return ast.get_source_segment(self.source_code, node)
        return None

    def extract_method_from_class(self, class_name: str, _method_name: str) -> Optional[str]:
        """Extract a class with only the specific method (deprecated - use extract_minimal_class_with_method)."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                # Get the full class code
                class_code = ast.get_source_segment(self.source_code, node)
                return class_code
        return None

    def extract_minimal_class_with_method(self, class_name: str, method_name: str) -> Optional[str]:
        """
        Extract a minimal class containing only:
        - Class definition
        - setUp/setUpClass/tearDown/tearDownClass methods (if any)
        - The target test method

        This is more focused than extracting the entire class.
        """
        # Find the class node
        class_node = None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                class_node = node
                break

        if not class_node:
            return None

        # Define setup/teardown method names to include
        setup_teardown_methods = {
            "setUp",
            "setUpClass",
            "tearDown",
            "tearDownClass",
            "setup_method",
            "setup_class",
            "teardown_method",
            "teardown_class",
            "setup",
            "teardown",  # pytest style
        }

        # Collect methods to include
        methods_to_include = []
        target_method_found = False

        for item in class_node.body:
            if isinstance(item, ast.FunctionDef):
                if item.name == method_name:
                    methods_to_include.append(item)
                    target_method_found = True
                elif item.name in setup_teardown_methods:
                    methods_to_include.append(item)

        if not target_method_found:
            return None

        # Reconstruct the class with only selected methods
        # Get class code segment including decorators
        class_start_line = class_node.lineno - 1
        if class_node.decorator_list:
            first_decorator_line = class_node.decorator_list[0].lineno - 1
            class_start_line = first_decorator_line

        source_lines = self.source_code.splitlines()

        # Get class definition line (up to and including the colon)
        class_def_line_idx = class_node.lineno - 1
        class_header_lines = source_lines[class_start_line : class_def_line_idx + 1]

        # Build the result
        result_lines = class_header_lines[:]

        # Determine indentation level for methods (typically 4 spaces)
        # Get the base indent of the class
        class_line = source_lines[class_def_line_idx]
        base_indent = len(class_line) - len(class_line.lstrip())
        method_indent = " " * (base_indent + 4)

        # Add methods with proper indentation
        for method_node in methods_to_include:
            # Add blank line
            result_lines.append("")

            # Get method source code (ast.get_source_segment removes leading indentation)
            method_code = ast.get_source_segment(self.source_code, method_node)
            if method_code:
                # Add indentation to each line
                for line in method_code.splitlines():
                    if line.strip():  # Non-empty line
                        result_lines.append(method_indent + line)
                    else:  # Empty line
                        result_lines.append("")

        return "\n".join(result_lines)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 方案2：跨文件导入解析
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _resolve_import(self, testbed_path: Path, file_path: Path, class_name: str) -> Optional[Path]:
        """
        解析跨文件导入，返回类的实际定义文件。

        Args:
            testbed_path: testbed根目录
            file_path: 当前测试文件路径
            class_name: 要查找的类名

        Returns:
            类定义所在的文件路径，如果在当前文件则返回None
        """
        if not file_path.exists():
            return None

        with open(file_path, "r") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                return None

        # 检查类是否在当前文件定义
        class_exists = any(isinstance(node, ast.ClassDef) and node.name == class_name for node in ast.walk(tree))

        if class_exists:
            return None  # 在当前文件，无需解析

        # 查找 import 语句
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # 检查是否导入了目标类
                if any(alias.name == class_name or alias.name == "*" for alias in node.names):
                    # 解析相对导入路径
                    import_path = self._resolve_relative_import(testbed_path, file_path, node.module, node.level)
                    if import_path and import_path.exists():
                        return import_path

        return None

    def _resolve_relative_import(
        self, testbed_path: Path, current_file: Path, import_module: str, level: int
    ) -> Optional[Path]:
        """
        解析相对导入路径。

        Args:
            testbed_path: testbed根目录
            current_file: 当前文件路径
            import_module: 导入模块名 (如 "with_dummyserver.test_https")
            level: 相对层级 (0=绝对导入, 1=., 2=.., etc)

        Returns:
            解析后的文件路径

        Example:
            from ..with_dummyserver.test_https import XXX
            level=2, module="with_dummyserver.test_https"
        """
        current_dir = current_file.parent

        # 从当前目录向上level-1层 (level包含了当前目录本身)
        target_dir = current_dir
        for _ in range(level - 1):
            target_dir = target_dir.parent

        # 拼接模块路径
        if import_module:
            module_parts = import_module.split(".")
            module_path = target_dir / "/".join(module_parts)
        else:
            # from .. import XXX (level>0 but no module)
            module_path = target_dir

        return module_path.with_suffix(".py")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 策略B：参数化测试处理
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _strip_parametrize(self, method_name: str) -> str:
        """
        去掉参数化测试后缀。

        Example:
            test_func[param1-param2] → test_func
            test_func → test_func
        """
        return re.sub(r"\[.*?\]$", "", method_name)

    def _extract_decorators(self, method_node: ast.FunctionDef) -> str:
        """
        提取方法的所有装饰器代码。

        Returns:
            装饰器代码字符串，每行以@开头，用换行符连接
        """
        if not method_node.decorator_list:
            return ""

        decorators = []
        for dec in method_node.decorator_list:
            dec_code = ast.get_source_segment(self.source_code, dec)
            if dec_code:
                decorators.append(f"@{dec_code}")

        return "\n".join(decorators)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 方案1：同文件继承解析
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _find_class(self, class_name: str) -> Optional[ast.ClassDef]:
        """在当前文件的AST树中查找类定义。"""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node
        return None

    def _find_method_in_hierarchy(
        self, class_node: ast.ClassDef, method_name: str, depth: int = 0, max_depth: int = 5
    ) -> Tuple[Optional[ast.FunctionDef], Optional[str]]:
        """
        递归查找方法在继承链中的定义。

        Args:
            class_node: 当前类节点
            method_name: 方法名
            depth: 当前递归深度
            max_depth: 最大递归深度

        Returns:
            (method_node, defining_class_name) 元组
            如果未找到则返回 (None, None)
        """
        # 检查当前类中是否有该方法
        for item in class_node.body:
            if isinstance(item, ast.FunctionDef) and item.name == method_name:
                return item, class_node.name

        # 达到最大深度
        if depth >= max_depth:
            return None, None

        # 递归查找父类
        for base in class_node.bases:
            # 获取父类名（处理简单情况）
            parent_name = ast.unparse(base).split("(")[0].strip()
            parent_class = self._find_class(parent_name)

            if parent_class:
                result = self._find_method_in_hierarchy(parent_class, method_name, depth + 1, max_depth)
                if result[0]:  # 找到了
                    return result

        return None, None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 增强版提取方法（组合三个方案）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def extract_with_enhancements(
        self,
        testbed_path: Path,
        original_file_path: Path,
        class_name: str,
        method_name: str,
    ) -> Tuple[Optional[str], Dict[str, any]]:
        """
        增强版提取方法，组合三个方案：
        1. 方案2：跨文件导入解析
        2. 策略B：参数化测试处理
        3. 方案1：同文件继承解析

        Args:
            testbed_path: testbed根目录
            original_file_path: 原始测试文件路径
            class_name: 类名
            method_name: 方法名（可能包含参数化后缀）

        Returns:
            (extracted_code, metadata) 元组
            metadata包含提取过程的信息
        """
        metadata = {
            "original_file": str(original_file_path.relative_to(testbed_path)),
            "resolved_file": None,
            "original_method": method_name,
            "resolved_method": None,
            "defining_class": None,
            "has_inheritance": False,
            "has_parametrize": False,
            "has_cross_file_import": False,
        }

        # Step 1: 方案2 - 跨文件导入解析
        resolved_file = self._resolve_import(testbed_path, original_file_path, class_name)
        if resolved_file:
            metadata["resolved_file"] = str(resolved_file.relative_to(testbed_path))
            metadata["has_cross_file_import"] = True

            # 重新初始化提取器使用正确的文件
            self.__init__(str(resolved_file))

        # Step 2: 策略B - 参数化测试处理
        base_method_name = self._strip_parametrize(method_name)
        if base_method_name != method_name:
            metadata["resolved_method"] = base_method_name
            metadata["has_parametrize"] = True
            method_name = base_method_name

        # Step 3: 方案1 - 同文件继承解析
        target_class = self._find_class(class_name)
        if not target_class:
            return None, metadata

        # 在继承链中查找方法
        method_node, defining_class = self._find_method_in_hierarchy(target_class, method_name)

        if not method_node:
            return None, metadata

        metadata["defining_class"] = defining_class
        if defining_class != class_name:
            metadata["has_inheritance"] = True

        # 提取代码：导入 + 类定义 + 方法（含装饰器）
        imports = self.extract_imports()
        decorators_code = self._extract_decorators(method_node)

        # 获取setup/teardown方法
        setup_teardown_methods = {
            "setUp",
            "setUpClass",
            "tearDown",
            "tearDownClass",
            "setup_method",
            "setup_class",
            "teardown_method",
            "teardown_class",
            "setup",
            "teardown",
        }

        setup_methods = []
        for item in target_class.body:
            if isinstance(item, ast.FunctionDef) and item.name in setup_teardown_methods:
                setup_methods.append(item)

        # 组合代码
        result_lines = []
        result_lines.append(imports)
        result_lines.append("")
        result_lines.append("")

        # 添加注释说明
        if metadata["has_cross_file_import"] or metadata["has_inheritance"]:
            result_lines.append("# NOTE: Test extracted with enhancements")
            if metadata["has_cross_file_import"]:
                result_lines.append(f"#   - Cross-file import resolved: {metadata['resolved_file']}")
            if metadata["has_inheritance"]:
                result_lines.append(f"#   - Inherited from: {defining_class}")
            if metadata["has_parametrize"]:
                result_lines.append(f"#   - Parametrized test: {metadata['original_method']}")
            result_lines.append("")

        # 获取类定义行（包括装饰器）
        class_start_line = target_class.lineno - 1
        if target_class.decorator_list:
            first_decorator_line = target_class.decorator_list[0].lineno - 1
            class_start_line = first_decorator_line

        source_lines = self.source_code.splitlines()
        class_def_line_idx = target_class.lineno - 1
        class_header_lines = source_lines[class_start_line : class_def_line_idx + 1]

        result_lines.extend(class_header_lines)

        # 添加setup/teardown方法
        for setup_method in setup_methods:
            result_lines.append("")
            method_code = ast.get_source_segment(self.source_code, setup_method)
            if method_code:
                for line in method_code.splitlines():
                    if line.strip():
                        result_lines.append("    " + line)
                    else:
                        result_lines.append("")

        # 添加目标方法（含装饰器）
        result_lines.append("")
        if decorators_code:
            for dec_line in decorators_code.splitlines():
                result_lines.append("    " + dec_line)

        method_code = ast.get_source_segment(self.source_code, method_node)
        if method_code:
            for line in method_code.splitlines():
                if line.strip():
                    result_lines.append("    " + line)
                else:
                    result_lines.append("")

        return "\n".join(result_lines), metadata


class FailToPassExtractor:
    """Main extractor for fail-to-pass tests."""

    def __init__(self, work_dir: str, milestone_id: str, extract_code: bool = False):
        """Initialize extractor with work directory and milestone ID.

        Args:
            work_dir: Work directory path
            milestone_id: Milestone ID
            extract_code: Whether to extract actual test code (default: False, only extract test IDs)
        """
        self.work_dir = Path(work_dir)
        self.milestone_id = milestone_id
        self.extract_code = extract_code

        # Define paths
        self.config_path = self.work_dir / "configs" / milestone_id / "config.yaml"
        self.metadata_path = self.work_dir / "metadata.json"
        self.benchmark_dir = self.work_dir / "test_results" / milestone_id / "benchmark"
        self.classification_path = self.benchmark_dir / f"{milestone_id}_classification.json"
        self.testbed_path = self.work_dir / "testbed"
        self.output_dir = self.benchmark_dir / "f2p_tests"

        # Validate paths
        self._validate_paths()

    def _validate_paths(self):
        """Validate that required paths exist."""
        # Classification file is always required
        if not self.classification_path.exists():
            raise FileNotFoundError(f"Classification file not found: {self.classification_path}")

        # Config/metadata and testbed only required for code extraction
        if self.extract_code:
            if not self.config_path.exists() and not self.metadata_path.exists():
                raise FileNotFoundError(
                    f"Neither config file ({self.config_path}) nor metadata file ({self.metadata_path}) found"
                )
            if not self.testbed_path.exists():
                raise FileNotFoundError(f"Testbed directory not found: {self.testbed_path}")

    def _get_env_setup_commit(self) -> str:
        """Read env_setup_commit from config.yaml or metadata.json."""
        # Try config.yaml first (for backward compatibility)
        if self.config_path.exists():
            with open(self.config_path, "r") as f:
                config = yaml.safe_load(f)

            env_setup_commit = config.get("source", {}).get("env_setup_commit")
            if not env_setup_commit:
                raise ValueError(f"env_setup_commit not found in {self.config_path}")

            return env_setup_commit

        # Fallback to metadata.json
        if self.metadata_path.exists():
            with open(self.metadata_path, "r") as f:
                metadata = json.load(f)

            # Find the milestone in metadata
            for milestone in metadata.get("milestones", []):
                if milestone["id"] == self.milestone_id:
                    commit_sha_start = milestone.get("commit_sha_start")
                    if not commit_sha_start:
                        raise ValueError(f"commit_sha_start not found for {self.milestone_id} in {self.metadata_path}")
                    print(f"Using commit_sha_start from metadata.json: {commit_sha_start}")
                    return commit_sha_start

            raise ValueError(f"Milestone {self.milestone_id} not found in {self.metadata_path}")

        raise FileNotFoundError("No valid config source found")

    def _get_fail_to_pass_tests(self) -> List[Dict]:
        """Read stable fail_to_pass and env_patch_skip_to_pass tests from final classification."""
        with open(self.classification_path, "r") as f:
            data = json.load(f)

        classification = data.get("stable_classification")
        if not isinstance(classification, dict):
            classification = data.get("classification", {})
            print(
                f"Warning: stable_classification missing in {self.classification_path}; falling back to classification",
                file=sys.stderr,
            )
        fail_to_pass = classification.get("fail_to_pass", [])
        env_patch_skip_to_pass = classification.get("env_patch_skip_to_pass", [])

        # Combine both types of tests
        all_f2p_tests = fail_to_pass + env_patch_skip_to_pass

        if not all_f2p_tests:
            print(f"Warning: No fail_to_pass or env_patch_skip_to_pass tests found in {self.classification_path}")
        else:
            if fail_to_pass:
                print(f"Found {len(fail_to_pass)} fail_to_pass tests")
            if env_patch_skip_to_pass:
                print(f"Found {len(env_patch_skip_to_pass)} env_patch_skip_to_pass tests")

        return all_f2p_tests

    def _checkout_commit(self, commit_sha: str):
        """Checkout to specific commit in testbed."""
        print(f"Checking out to commit {commit_sha} in testbed...")

        # Save current branch/commit
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.testbed_path,
            capture_output=True,
            text=True,
            check=True,
        )
        current_branch = result.stdout.strip()

        # Checkout to target commit
        subprocess.run(["git", "checkout", commit_sha], cwd=self.testbed_path, capture_output=True, check=True)

        print(f"Checked out to {commit_sha} (was on {current_branch})")
        return current_branch

    def _generate_output_filename(self, test_id: str) -> str:
        """Generate output filename from test ID."""
        # Parse test ID
        parsed = TestIDParser.parse(test_id)

        # Extract filename without path and extension
        file_name = Path(parsed["file_path"]).stem

        # Build filename components
        parts = [file_name]
        if parsed["class_name"]:
            parts.append(parsed["class_name"])
        if parsed["function_name"]:
            parts.append(parsed["function_name"])

        return "__".join(parts) + ".py"

    def _extract_test_code(self, test_id: str) -> Tuple[Optional[str], Dict[str, any]]:
        """
        Extract test code for a given test ID using enhanced extraction.

        Returns:
            (extracted_code, metadata) tuple
        """
        parsed = TestIDParser.parse(test_id)

        # Get full file path in testbed
        test_file_path = self.testbed_path / parsed["file_path"]

        if not test_file_path.exists():
            print(f"Warning: Test file not found: {test_file_path}")
            return None, {"error": "file_not_found"}

        metadata = {
            "extraction_method": None,
            "extraction_success": False,
        }

        # Extract code
        try:
            extractor = TestCodeExtractor(str(test_file_path))

            # Try enhanced extraction for class methods
            if parsed["class_name"]:
                try:
                    # Attempt enhanced extraction (with import/inheritance/parametrize support)
                    full_code, enhanced_metadata = extractor.extract_with_enhancements(
                        self.testbed_path, test_file_path, parsed["class_name"], parsed["function_name"]
                    )

                    if full_code:
                        metadata["extraction_method"] = "enhanced"
                        metadata["extraction_success"] = True
                        metadata.update(enhanced_metadata)
                        return full_code, metadata

                except Exception as e:
                    # Enhanced extraction failed, will try fallback
                    print(f"  Enhanced extraction failed, trying fallback: {e}")

                # Fallback to original extraction method
                imports = extractor.extract_imports()
                test_code = extractor.extract_minimal_class_with_method(parsed["class_name"], parsed["function_name"])

                if test_code:
                    full_code = f"{imports}\n\n\n{test_code}\n"
                    metadata["extraction_method"] = "original"
                    metadata["extraction_success"] = True
                    return full_code, metadata
            else:
                # Extract standalone function (no enhancement needed)
                imports = extractor.extract_imports()
                test_code = extractor.extract_function(parsed["function_name"])

                if test_code:
                    full_code = f"{imports}\n\n\n{test_code}\n"
                    metadata["extraction_method"] = "original_function"
                    metadata["extraction_success"] = True
                    return full_code, metadata

            # Extraction failed
            print(f"Warning: Could not extract code for {test_id}")
            return None, metadata

        except Exception as e:
            print(f"Error extracting code for {test_id}: {e}")
            metadata["error"] = str(e)
            return None, metadata

    def extract_test_ids_only(self):
        """Extract only test IDs without extracting actual code.

        This is the default behavior - fast and doesn't require git checkout.
        """
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Get fail_to_pass tests
        fail_to_pass_tests = self._get_fail_to_pass_tests()
        print(f"Found {len(fail_to_pass_tests)} fail-to-pass tests")

        if not fail_to_pass_tests:
            print("No tests to extract. Exiting.")
            return

        # Prepare metadata with test IDs only
        metadata = {
            "milestone_id": self.milestone_id,
            "extraction_mode": "test_ids_only",
            "total_tests": len(fail_to_pass_tests),
            "tests": [],
        }

        print("\nExtracting test IDs (no code extraction)...")

        for i, test_info in enumerate(fail_to_pass_tests, 1):
            # Handle both dict and string formats
            if isinstance(test_info, dict):
                test_id = test_info["test_id"]
                baseline_outcome = test_info.get("baseline_outcome", "failed")
                final_outcome = test_info.get("final_outcome", "passed")
            else:
                # test_info is just a string (test_id)
                test_id = test_info
                baseline_outcome = "failed"
                final_outcome = "passed"

            # Parse test ID
            parsed = TestIDParser.parse(test_id)

            # Prepare test metadata entry (IDs only)
            test_metadata = {
                "test_id": test_id,
                "baseline_outcome": baseline_outcome,
                "final_outcome": final_outcome,
                "source_file": parsed["file_path"],
                "class_name": parsed["class_name"],
                "function_name": parsed["function_name"],
            }

            metadata["tests"].append(test_metadata)

            # Print progress every 10 tests or on last test
            if i % 10 == 0 or i == len(fail_to_pass_tests):
                print(f"  Progress: {i}/{len(fail_to_pass_tests)} tests")

        # Save metadata
        metadata_path = self.output_dir / "metadata.json"
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        print(f"\n✓ Test IDs saved to: {metadata_path}")

        # Summary
        print(f"\n{'=' * 60}")
        print("Test ID extraction complete:")
        print(f"  Total tests: {len(fail_to_pass_tests)}")
        print(f"  Output directory: {self.output_dir}")
        print("  Mode: Test IDs only (no code extraction)")
        print(f"{'=' * 60}")

    def extract_with_code(self):
        """Extract tests with actual code (requires git checkout)."""
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Get env_setup_commit
        env_setup_commit = self._get_env_setup_commit()
        print(f"Environment setup commit: {env_setup_commit}")

        # Get fail_to_pass tests
        fail_to_pass_tests = self._get_fail_to_pass_tests()
        print(f"Found {len(fail_to_pass_tests)} fail-to-pass tests")

        if not fail_to_pass_tests:
            print("No tests to extract. Exiting.")
            return

        # Checkout to env_setup_commit
        original_ref = self._checkout_commit(env_setup_commit)

        try:
            # Extract each test
            metadata = {
                "milestone_id": self.milestone_id,
                "env_setup_commit": env_setup_commit,
                "total_tests": len(fail_to_pass_tests),
                "tests": [],
            }

            for i, test_info in enumerate(fail_to_pass_tests, 1):
                # Handle both dict and string formats
                if isinstance(test_info, dict):
                    test_id = test_info["test_id"]
                    baseline_outcome = test_info.get("baseline_outcome", "failed")
                    final_outcome = test_info.get("final_outcome", "passed")
                else:
                    # test_info is just a string (test_id)
                    test_id = test_info
                    baseline_outcome = "failed"
                    final_outcome = "passed"

                print(f"\n[{i}/{len(fail_to_pass_tests)}] Processing: {test_id}")

                # Parse test ID
                parsed = TestIDParser.parse(test_id)

                # Generate output filename
                output_filename = self._generate_output_filename(test_id)
                output_path = self.output_dir / output_filename

                # Extract code (now returns tuple: code, extraction_metadata)
                test_code, extraction_metadata = self._extract_test_code(test_id)

                # Prepare test metadata entry
                test_metadata = {
                    "test_id": test_id,
                    "baseline_outcome": baseline_outcome,
                    "final_outcome": final_outcome,
                    "source_file": parsed["file_path"],
                    "class_name": parsed["class_name"],
                    "function_name": parsed["function_name"],
                }

                # Merge extraction metadata
                test_metadata.update(extraction_metadata)

                if test_code:
                    # Save to file
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(test_code)

                    # Print status with enhancement indicators
                    status_msg = f"  ✓ Saved to: {output_filename}"
                    if extraction_metadata.get("extraction_method") == "enhanced":
                        enhancements = []
                        if extraction_metadata.get("has_cross_file_import"):
                            enhancements.append("import")
                        if extraction_metadata.get("has_inheritance"):
                            enhancements.append("inherit")
                        if extraction_metadata.get("has_parametrize"):
                            enhancements.append("param")
                        if enhancements:
                            status_msg += f" [+{'+'.join(enhancements)}]"
                    print(status_msg)

                    test_metadata["extracted_file"] = output_filename
                    metadata["tests"].append(test_metadata)
                else:
                    print("  ✗ Failed to extract")
                    test_metadata["extracted_file"] = None
                    metadata["tests"].append(test_metadata)

            # Save metadata
            metadata_path = self.output_dir / "metadata.json"
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
            print(f"\n✓ Metadata saved to: {metadata_path}")

            # Summary
            successful = sum(1 for t in metadata["tests"] if t["extraction_success"])
            print(f"\n{'=' * 60}")
            print("Extraction complete:")
            print(f"  Total tests: {len(fail_to_pass_tests)}")
            print(f"  Successful: {successful}")
            print(f"  Failed: {len(fail_to_pass_tests) - successful}")
            print(f"  Output directory: {self.output_dir}")
            print(f"{'=' * 60}")

        finally:
            # Restore original git state
            print(f"\nRestoring git state to {original_ref}...")
            subprocess.run(["git", "checkout", original_ref], cwd=self.testbed_path, capture_output=True, check=True)

    def extract(self):
        """Main extraction entry point.

        Routes to appropriate extraction method based on extract_code flag.
        """
        if self.extract_code:
            print("Mode: Extracting test code (requires git checkout)")
            self.extract_with_code()
        else:
            print("Mode: Extracting test IDs only (fast, no git checkout)")
            self.extract_test_ids_only()


def get_available_milestones(work_dir: Path) -> List[str]:
    """Get list of available milestones from configs/dockerfiles/test_results directory."""
    # Try multiple directory names
    for dir_name in ["configs", "dockerfiles", "env_data"]:
        milestone_dir = work_dir / dir_name
        if milestone_dir.exists():
            milestones = []
            for item in sorted(milestone_dir.iterdir()):
                # Support both M* (e.g., M001) and milestone_* naming patterns
                if item.is_dir() and (item.name.startswith("M") or item.name.startswith("milestone_")):
                    milestones.append(item.name)
            if milestones:
                return milestones

    # Fallback: check test_results directory
    test_results_dir = work_dir / "test_results"
    if test_results_dir.exists():
        milestones = []
        for item in sorted(test_results_dir.iterdir()):
            if item.is_dir() and (item.name.startswith("M") or item.name.startswith("milestone_")):
                milestones.append(item.name)
        return milestones

    return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Statistics and Analysis Functions (integrated from milestone_stats.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def load_classification_file(milestone_dir: Path) -> Optional[Dict[str, Any]]:
    """Load classification JSON file for a milestone.

    Args:
        milestone_dir: Path to milestone directory (e.g., M001/)

    Returns:
        Parsed JSON dict or None if file not found/invalid
    """
    milestone_id = milestone_dir.name
    classification_file = milestone_dir / "benchmark" / f"{milestone_id}_classification.json"

    if not classification_file.exists():
        return None

    try:
        with open(classification_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: Failed to load {classification_file}: {e}", file=sys.stderr)
        return None


def load_baseline_final(milestone_dir: Path) -> tuple:
    """Load baseline.json and final.json files.

    Args:
        milestone_dir: Path to milestone directory

    Returns:
        Tuple of (baseline_dict, final_dict), either can be None if not found
    """
    baseline_file = milestone_dir / "baseline.json"
    final_file = milestone_dir / "final.json"

    baseline = None
    final = None

    if baseline_file.exists():
        try:
            with open(baseline_file, "r", encoding="utf-8") as f:
                baseline = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if final_file.exists():
        try:
            with open(final_file, "r", encoding="utf-8") as f:
                final = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    return baseline, final


def detect_status(stats: Dict[str, Any], baseline: Optional[Dict], final: Optional[Dict]) -> str:
    """Detect milestone test status.

    Args:
        stats: Statistics dict from classification.json
        baseline: Parsed baseline.json
        final: Parsed final.json

    Returns:
        Status string: "ok", "empty", "error", "warning"

    Exit code meanings (pytest):
        0: All tests passed
        1: Tests were collected and run but some failed (NORMAL)
        2: Test execution was interrupted by the user
        3: Internal error happened while executing tests
        4: pytest command line usage error
        5: No tests were collected
    """
    # Check if tests were collected
    if stats.get("total_tests", 0) == 0:
        return "empty"

    # Check for real errors (not normal test failures)
    # exitcode=1 means "tests failed" which is NORMAL, not an error
    # Real errors are: 2 (interrupted), 3 (internal error), 4 (usage error), 5 (no tests)
    baseline_exitcode = baseline.get("exitcode", 0) if baseline else 0
    final_exitcode = final.get("exitcode", 0) if final else 0

    # exitcode >= 2 indicates a real error (not just test failures)
    if baseline_exitcode >= 2 or final_exitcode >= 2:
        return "error"

    # Check for high regression rate (>5% of tests regressed)
    pass_to_fail = stats.get("pass_to_fail", 0)
    total = stats.get("total_tests", 1)
    if pass_to_fail > 0 and (pass_to_fail / total) > 0.05:
        return "warning"

    return "ok"


def detect_issues(stats: Dict[str, Any], status: str) -> List[str]:
    """Detect and list issues with the milestone.

    Args:
        stats: Statistics dict
        status: Status string from detect_status

    Returns:
        List of issue descriptions
    """
    issues = []

    if status == "empty":
        issues.append("No tests collected (dependency missing or exitcode=5)")
    elif status == "error":
        issues.append("Test execution error (exitcode >= 2: interrupted/internal error/usage error)")

    if stats.get("pass_to_fail", 0) > 5:
        issues.append(f"High regression: {stats['pass_to_fail']} tests now failing")

    if stats.get("total_tests", 0) > 0 and stats.get("total_tests", 0) < 100:
        issues.append("Low test coverage (<100 tests)")

    return issues


def check_extraction_status(
    milestone_dir: Path, fail_to_pass_count: int, env_patch_skip_to_pass_count: int = 0
) -> Dict[str, Any]:
    """Check if f2p tests are properly extracted.

    Args:
        milestone_dir: Path to milestone directory
        fail_to_pass_count: Number of fail_to_pass tests
        env_patch_skip_to_pass_count: Number of env_patch_skip_to_pass tests

    Returns:
        Dict containing extraction status information
    """
    f2p_dir = milestone_dir / "benchmark" / "f2p_tests"

    # Total expected includes both fail_to_pass and env_patch_skip_to_pass
    total_expected = fail_to_pass_count + env_patch_skip_to_pass_count

    result = {
        "expected": total_expected,
        "fail_to_pass": fail_to_pass_count,
        "env_patch_skip_to_pass": env_patch_skip_to_pass_count,
        "extracted": 0,
        "rate": 0.0,
        "status": "na",  # na | complete | partial | missing
    }

    # If no f2p tests expected, mark as not applicable
    if total_expected == 0:
        return result

    # Count extracted test files (*.py files in f2p_tests/)
    if f2p_dir.exists():
        test_files = list(f2p_dir.glob("*.py"))
        result["extracted"] = len(test_files)

    # Calculate rate and determine status
    if result["extracted"] > 0:
        result["rate"] = round(result["extracted"] / total_expected, 2)

        if result["extracted"] == total_expected:
            result["status"] = "complete"
        else:
            result["status"] = "partial"
    else:
        result["status"] = "missing"

    return result


def extract_milestone_stats(
    milestone_dir: Path, include_tests: bool = False, check_extraction: bool = False
) -> Dict[str, Any]:
    """Extract statistics for a single milestone.

    Args:
        milestone_dir: Path to milestone directory
        include_tests: Whether to include detailed test lists
        check_extraction: Whether to check f2p test extraction status

    Returns:
        Dict containing milestone statistics
    """
    milestone_id = milestone_dir.name
    classification = load_classification_file(milestone_dir)
    baseline, final = load_baseline_final(milestone_dir)

    if classification is None:
        return {
            "milestone_id": milestone_id,
            "status": "missing",
            "error": "Classification file not found",
            "statistics": {},
        }

    stats = classification.get("statistics", {})
    if not isinstance(stats, dict) or not stats:
        stats = classification.get("summary", {})
    status = detect_status(stats, baseline, final)
    issues = detect_issues(stats, status)

    result = {
        "milestone_id": milestone_id,
        "statistics": stats,
        "status": status,
    }
    stable_classification = classification.get("stable_classification")
    if isinstance(stable_classification, dict):
        result["stable_fail_to_pass"] = len(stable_classification.get("fail_to_pass", []))

    if issues:
        result["issues"] = issues

    # Add baseline/final info
    if baseline:
        result["baseline"] = {
            "collected": baseline.get("summary", {}).get("collected", 0),
            "passed": baseline.get("summary", {}).get("passed", 0),
            "failed": baseline.get("summary", {}).get("failed", 0),
            "exitcode": baseline.get("exitcode", 0),
        }

    if final:
        result["final"] = {
            "collected": final.get("summary", {}).get("collected", 0),
            "passed": final.get("summary", {}).get("passed", 0),
            "failed": final.get("summary", {}).get("failed", 0),
            "exitcode": final.get("exitcode", 0),
        }

    # Check extraction status if requested
    if check_extraction:
        fail_to_pass_count = stats.get("fail_to_pass", 0)
        env_patch_skip_to_pass_count = stats.get("env_patch_skip_to_pass", 0)
        result["extraction"] = check_extraction_status(milestone_dir, fail_to_pass_count, env_patch_skip_to_pass_count)

    # Optionally include test lists
    if include_tests:
        classification_data = classification.get("classification", {})
        result["test_lists"] = {
            "fail_to_pass": classification_data.get("fail_to_pass", []),
            "env_patch_skip_to_pass": classification_data.get("env_patch_skip_to_pass", []),
            "pass_to_fail": classification_data.get("pass_to_fail", []),
            "fail_to_fail": classification_data.get("fail_to_fail", []),
        }

    return result


def analyze_test_results(
    test_results_dir: Path, include_tests: bool = False, check_extraction: bool = False
) -> Dict[str, Any]:
    """Analyze all milestones in the test results directory.

    Args:
        test_results_dir: Path to directory containing milestone subdirectories
        include_tests: Whether to include detailed test lists
        check_extraction: Whether to check f2p test extraction status

    Returns:
        Dict containing summary and per-milestone statistics
    """
    if not test_results_dir.exists():
        raise FileNotFoundError(f"Test results directory not found: {test_results_dir}")

    # Find all milestone directories (M001, M002, milestone_xxx, etc.)
    milestone_dirs = sorted(
        [
            d
            for d in test_results_dir.iterdir()
            if d.is_dir()
            and ((d.name.startswith("M") and d.name[1:].split("_")[0].isdigit()) or d.name.startswith("milestone_"))
        ],
        key=lambda d: d.name,
    )

    if not milestone_dirs:
        raise ValueError(f"No milestone directories found in {test_results_dir}")

    # Extract stats for each milestone
    milestones = []
    for milestone_dir in milestone_dirs:
        stats = extract_milestone_stats(milestone_dir, include_tests, check_extraction)
        milestones.append(stats)

    # Calculate summary statistics
    summary = {
        "total_milestones": len(milestones),
        "status_breakdown": {},
        "total_tests": 0,
        "total_fail_to_pass": 0,
        "total_env_patch_skip_to_pass": 0,
        "total_pass_to_fail": 0,
        "empty_milestones": [],
        "error_milestones": [],
        "warning_milestones": [],
    }

    # Extraction statistics (only if check_extraction is enabled)
    if check_extraction:
        extracted_tests = 0
        extraction_issues = []

        for m in milestones:
            extraction = m.get("extraction")
            if extraction:
                extracted_tests += extraction.get("extracted", 0)

                # Report issues (partial or missing extraction)
                if extraction["status"] in ("partial", "missing"):
                    milestone_id = m["milestone_id"]
                    expected = extraction["expected"]
                    extracted = extraction["extracted"]
                    rate = int(extraction["rate"] * 100)
                    extraction_issues.append(f"{milestone_id}: {extracted}/{expected} extracted ({rate}%)")

        summary["extracted_tests"] = extracted_tests
        if extraction_issues:
            summary["extraction_issues"] = extraction_issues

    for m in milestones:
        status = m.get("status", "unknown")
        summary["status_breakdown"][status] = summary["status_breakdown"].get(status, 0) + 1

        stats = m.get("statistics", {})
        summary["total_tests"] += stats.get("total_tests", 0)
        summary["total_fail_to_pass"] += stats.get("fail_to_pass", 0)
        summary["total_env_patch_skip_to_pass"] += stats.get("env_patch_skip_to_pass", 0)
        summary["total_pass_to_fail"] += stats.get("pass_to_fail", 0)

        if status == "empty":
            summary["empty_milestones"].append(m["milestone_id"])
        elif status == "error":
            summary["error_milestones"].append(m["milestone_id"])
        elif status == "warning":
            summary["warning_milestones"].append(m["milestone_id"])

    return {
        "summary": summary,
        "milestones": milestones,
        "metadata": {
            "test_results_dir": str(test_results_dir.absolute()),
            "total_milestone_directories": len(milestone_dirs),
        },
    }


def verify_extraction(work_dir: Path, milestone_id: str) -> Dict[str, Any]:
    """Verify f2p test extraction for a single milestone.

    Args:
        work_dir: Work directory path
        milestone_id: Milestone ID

    Returns:
        Dict with verification results
    """
    milestone_dir = work_dir / "test_results" / milestone_id
    stats = extract_milestone_stats(milestone_dir, include_tests=False, check_extraction=True)

    extraction = stats.get("extraction", {})
    expected = extraction.get("expected", 0)
    extracted = extraction.get("extracted", 0)
    status = extraction.get("status", "na")

    return {
        "milestone_id": milestone_id,
        "expected": expected,
        "extracted": extracted,
        "rate": extraction.get("rate", 0.0),
        "status": status,
        "complete": status == "complete",
    }


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Extract fail-to-pass tests and analyze milestone statistics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  EXTRACTION MODE (extract specified milestones):
    # Extract test IDs only (default, fast, no git checkout required)
    %(prog)s /path/to/work_dir M001

    # Extract test IDs for multiple milestones
    %(prog)s /path/to/work_dir M001 M002 M003

    # Extract all milestones (IDs only)
    %(prog)s /path/to/work_dir --all

    # Extract with actual test code (requires git checkout, slower)
    %(prog)s /path/to/work_dir M001 --extract-code

    # Extract all milestones with code
    %(prog)s /path/to/work_dir --all --extract-code

  REPORT MODE (analyze ALL existing milestones):
    # Generate report for all milestones (extraction status always included)
    %(prog)s /path/to/work_dir --report

    # Show only problematic milestones
    %(prog)s /path/to/work_dir --report --filter-issues

    # Include detailed test lists
    %(prog)s /path/to/work_dir --report --include-tests

    # Custom output file
    %(prog)s /path/to/work_dir --report -o custom_report.json

  COMBINED WORKFLOW (extract then report):
    # Extract specific milestones, then generate report for all
    %(prog)s /path/to/work_dir M001 M002 && \\
    %(prog)s /path/to/work_dir --report
        """,
    )
    parser.add_argument(
        "positional",
        nargs="*",
        help=(
            "Either positional work_dir followed by milestone IDs, or just milestone IDs when --work-dir is supplied"
        ),
    )
    parser.add_argument(
        "--work-dir",
        dest="work_dir_opt",
        type=str,
        help="Work directory (alias for the positional work_dir, useful for post-loop docs)",
    )

    # Mode selection (mutually exclusive)
    parser.add_argument(
        "--report",
        action="store_true",
        help="Report mode: analyze all milestone test results and generate statistics report (always includes extraction status check)",
    )

    # Extraction mode options
    parser.add_argument("--all", action="store_true", help="Extract all available milestones (extraction mode only)")
    parser.add_argument(
        "--extract-code",
        action="store_true",
        help="Extract actual test code (requires git checkout). Default: only extract test IDs (extraction mode only)",
    )

    # Report mode options
    parser.add_argument("--include-tests", action="store_true", help="Include detailed test lists (report mode only)")
    parser.add_argument(
        "--filter-issues", action="store_true", help="Show only problematic milestones (report mode only)"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file for statistics (report mode, default: test_results/milestone_stats.json)",
    )

    args = parser.parse_args()

    if args.work_dir_opt:
        work_dir = Path(args.work_dir_opt)
        milestone_ids = list(args.positional)
    else:
        if not args.positional:
            parser.error("work_dir is required (positional or --work-dir)")
        work_dir = Path(args.positional[0])
        milestone_ids = list(args.positional[1:])

    args.milestone_ids = milestone_ids

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # MODE 1: REPORT MODE - Analyze all existing milestone results
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if args.report:
        # Validate: cannot specify milestone_ids in report mode
        if args.milestone_ids or args.all:
            parser.error("--report mode analyzes ALL milestones. Do not specify milestone IDs or --all.")

        print(f"{'=' * 70}")
        print("Report Mode: Analyzing all milestone test results...")
        print(f"{'=' * 70}")

        test_results_dir = work_dir / "test_results"

        try:
            # Analyze test results (always check extraction status)
            results = analyze_test_results(test_results_dir, args.include_tests, check_extraction=True)

            # Filter if requested
            if args.filter_issues:
                results["milestones"] = [
                    m for m in results["milestones"] if m.get("status") in ("empty", "error", "warning")
                ]
                results["summary"]["total_milestones"] = len(results["milestones"])

            # Determine output path
            if args.output:
                output_path = args.output
            else:
                output_path = test_results_dir / "milestone_stats.json"

            # Write to file
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"\n✅ Statistics written to: {output_path}", file=sys.stderr)

            # Print summary
            summary = results["summary"]
            print("\n=== Summary ===", file=sys.stderr)
            print(f"Total Milestones: {summary['total_milestones']}", file=sys.stderr)
            print(f"Total Tests: {summary['total_tests']}", file=sys.stderr)
            print(f"Total fail→pass: {summary['total_fail_to_pass']}", file=sys.stderr)
            print(f"Total env_patch_skip→pass: {summary['total_env_patch_skip_to_pass']}", file=sys.stderr)
            print(f"Total pass→fail: {summary['total_pass_to_fail']}", file=sys.stderr)

            # Show extraction info (always included)
            print(f"Extracted f2p tests: {summary.get('extracted_tests', 0)}", file=sys.stderr)
            if "extraction_issues" in summary:
                print("\n⚠️  Extraction Issues:", file=sys.stderr)
                for issue in summary["extraction_issues"]:
                    print(f"  - {issue}", file=sys.stderr)

            if summary["empty_milestones"]:
                print(f"\nEmpty Results: {', '.join(summary['empty_milestones'])}", file=sys.stderr)
            if summary["error_milestones"]:
                print(f"Errors: {', '.join(summary['error_milestones'])}", file=sys.stderr)
            if summary["warning_milestones"]:
                print(f"Warnings: {', '.join(summary['warning_milestones'])}", file=sys.stderr)

            sys.exit(0)

        except Exception as e:
            print(f"Error during report generation: {e}", file=sys.stderr)
            sys.exit(1)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # MODE 2: EXTRACTION MODE - Extract specified milestones
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # Validate: report-mode-only options
    if args.include_tests or args.filter_issues or args.output:
        parser.error("--include-tests, --filter-issues, and -o are for --report mode only.")

    # Validate: extraction-mode-only options (for clarity)
    if args.extract_code:
        # Make sure we're not in report mode
        if args.report:
            parser.error("--extract-code is for extraction mode only, not compatible with --report.")

    # Determine which milestones to process
    if args.all:
        milestone_ids = get_available_milestones(work_dir)
        if not milestone_ids:
            print(
                f"Error: No milestones found in {work_dir} (checked configs/, dockerfiles/, env_data/, test_results/)",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Found {len(milestone_ids)} milestones: {', '.join(milestone_ids)}")
    elif args.milestone_ids:
        milestone_ids = args.milestone_ids
    else:
        parser.error("Please specify milestone ID(s), use --all for extraction mode, or use --report for report mode.")

    # Process each milestone
    total_milestones = len(milestone_ids)
    successful = 0
    failed = []
    verification_results = []

    for i, milestone_id in enumerate(milestone_ids, 1):
        print(f"\n{'=' * 70}")
        print(f"Processing milestone {i}/{total_milestones}: {milestone_id}")
        print(f"{'=' * 70}")

        try:
            # Extract tests
            extractor = FailToPassExtractor(str(work_dir), milestone_id, extract_code=args.extract_code)
            extractor.extract()
            successful += 1

            # Always verify extraction and show results
            print(f"\n{'─' * 70}")
            print(f"Extraction summary for {milestone_id}:")
            print(f"{'─' * 70}")

            verification = verify_extraction(work_dir, milestone_id)
            verification_results.append(verification)

            expected = verification["expected"]
            extracted = verification["extracted"]
            rate = int(verification["rate"] * 100)
            status = verification["status"]

            if status == "complete":
                print(f"✅ Extraction complete: {extracted}/{expected} tests ({rate}%)")
            elif status == "partial":
                print(f"⚠️  Partial extraction: {extracted}/{expected} tests ({rate}%)")
            elif status == "missing":
                print(f"❌ No tests extracted: 0/{expected} tests (0%)")
            else:
                print("ℹ️  No fail-to-pass tests for this milestone")

        except FileNotFoundError as e:
            print(f"⚠️  Warning: Skipping {milestone_id} - {e}", file=sys.stderr)
            failed.append((milestone_id, str(e)))
        except Exception as e:
            print(f"✗ Error processing {milestone_id}: {e}", file=sys.stderr)
            failed.append((milestone_id, str(e)))

    # Final summary
    print(f"\n{'=' * 70}")
    print("Batch extraction complete:")
    print(f"  Total milestones: {total_milestones}")
    print(f"  Successful: {successful}")
    print(f"  Failed: {len(failed)}")

    if failed:
        print("\nFailed milestones:")
        for mid, error in failed:
            print(f"  - {mid}: {error}")

    # Extraction verification summary (always shown)
    if verification_results:
        print("\nExtraction Statistics:")
        complete = sum(1 for v in verification_results if v["status"] == "complete")
        partial = sum(1 for v in verification_results if v["status"] == "partial")
        missing = sum(1 for v in verification_results if v["status"] == "missing")

        print(f"  Complete: {complete}")
        print(f"  Partial: {partial}")
        print(f"  Missing: {missing}")

        if partial > 0 or missing > 0:
            print("\nIncomplete extractions:")
            for v in verification_results:
                if v["status"] in ("partial", "missing"):
                    rate = int(v["rate"] * 100)
                    print(f"  - {v['milestone_id']}: {v['extracted']}/{v['expected']} ({rate}%)")

    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
