#!/usr/bin/env python3
"""
动态检测仓库的测试目录配置。

用法:
    # 基础检测（仅静态分析）
    python detect_test_dirs.py /path/to/repo

    # 结合 Git 历史分析（更准确, 优先使用这个版本）
    python detect_test_dirs.py /path/to/repo --config config/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml

    # 直接指定 tag 范围
    python detect_test_dirs.py /path/to/repo --start-tag dubbo-3.3.3 --end-tag dubbo-3.3.6 --main-branch 3.3
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# 各语言的标准测试模式（宁可宽泛，不可遗漏）
LANG_PATTERNS = {
    "java": [
        "**/src/test/**",
        "**/test/**",
        "**/*Test.java",
        "**/*Tests.java",
        "**/*IT.java",
        "**/*TestCase.java",
        "**/*Test.groovy",
        "**/*Spec.groovy",
    ],
    "python": [
        "**/test/**",
        "**/tests/**",
        "**/test_*.py",
        "**/*_test.py",
        "**/conftest.py",
    ],
    "go": [
        "**/*_test.go",
        "**/testdata/**",
    ],
    "rust": [
        "**/tests/**",
        "**/test/**",
        "**/benches/**",
    ],
    "javascript": [
        "**/test/**",
        "**/tests/**",
        "**/__tests__/**",
        "**/*.test.*",
        "**/*-test.*",
        "**/*.spec.*",
        "**/__snapshots__/**",
    ],
}

# 语言检测标记
LANG_MARKERS = {
    "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "python": ["pyproject.toml", "setup.py", "requirements.txt"],
    "go": ["go.mod"],
    "rust": ["Cargo.toml"],
    "javascript": ["package.json"],
}

# 特殊目录关键词
SPECIAL_DIR_KEYWORDS = ["test", "demo", "sample", "example", "playwright", "cypress", "e2e"]

# 测试文件模式（用于 Git 历史分析）
# 注：使用 (?:^|.*/) 前缀支持顶层目录和嵌套目录
TEST_FILE_PATTERNS = [
    # === 目录模式 ===
    # 标准测试目录
    r"(?:^|.*/)src/test/.*",
    r"(?:^|.*/)tests?/.*",
    r"(?:^|.*/)__tests__/.*",
    r"(?:^|.*/)testdata/.*",
    r"(?:^|.*/)fixtures?/.*",
    # 组合词测试目录 (如 configtest, js_tests, unit_tests)
    r"(?:^|.*/)[a-z_]*tests?[a-z_]*/.*",
    r"(?:^|.*/)[a-z_]*test[a-z_]+/.*",
    # 性能/基准测试
    r"(?:^|.*/)benches?/.*",
    r"(?:^|.*/)benchmarks?/.*",
    r"(?:^|.*/)benchsuite/.*",
    r"(?:^|.*/)perf/.*",
    # 模糊测试
    r"(?:^|.*/)fuzz/.*",
    r"(?:^|.*/)fuzzing/.*",
    r"(?:^|.*/)fuzzer/.*",
    # 集成/E2E 测试
    r"(?:^|.*/)integration/.*",
    r"(?:^|.*/)e2e/.*",
    r"(?:^|.*/)end-to-end/.*",
    r"(?:^|.*/)acceptance/.*",
    # E2E 测试框架
    r"(?:^|.*/)cypress/.*",
    r"(?:^|.*/)playwright/.*",
    # Mock/Stub 目录
    r"(?:^|.*/)mocks?/.*",
    r"(?:^|.*/)__mocks__/.*",
    r"(?:^|.*/)stubs?/.*",
    # 快照测试
    r"(?:^|.*/)__snapshots__/.*",
    r"(?:^|.*/)snapshots?/.*",
    r"(?:^|.*/)golden/.*",
    # === 文件模式 ===
    # Go
    r".*_test\.go$",
    # Python
    r".*_test\.py$",
    r".*test_.*\.py$",
    r"(?:^|.*/)conftest\.py$",
    # Rust
    r".*_test\.rs$",
    # Java/Kotlin/Groovy
    r".*Test\.(java|groovy|kt)$",
    r".*Tests\.(java|groovy|kt)$",
    r".*IT\.(java|groovy|kt)$",
    r".*TestCase\.(java|groovy|kt)$",
    r".*Spec\.(java|groovy|scala)$",
    # JavaScript/TypeScript
    r".*\.test\.(js|ts|jsx|tsx|mjs|cjs)$",
    r".*\.spec\.(js|ts|jsx|tsx|mjs|cjs)$",
    r".*-test\.(js|ts|jsx|tsx)$",
    r".*-spec\.(js|ts|jsx|tsx)$",
]


def detect_languages(repo: Path) -> list[str]:
    """检测仓库使用的语言（支持 monorepo 结构）。"""
    langs = []
    for lang, markers in LANG_MARKERS.items():
        if any((repo / m).exists() for m in markers):
            langs.append(lang)
    for subdir_pattern in ["packages/*", "apps/*", "crates/*", "libs/*"]:
        for subdir in repo.glob(subdir_pattern):
            if not subdir.is_dir():
                continue
            for lang, markers in LANG_MARKERS.items():
                if lang not in langs and any((subdir / m).exists() for m in markers):
                    langs.append(lang)
    return langs or ["java"]


def find_special_test_dirs(repo: Path) -> list[str]:
    """查找特殊的测试/demo 目录。"""
    special = []
    skip = {".git", "target", "build", "node_modules", "dist", "__pycache__", ".venv", "venv"}
    for d in repo.iterdir():
        if not d.is_dir() or d.name in skip:
            continue
        name_lower = d.name.lower()
        if name_lower == "examples":
            continue
        if any(kw in name_lower for kw in SPECIAL_DIR_KEYWORDS):
            special.append(f"{d.name}/**")
    return special


def detect_src_modules(repo: Path) -> list[str]:
    """检测源代码模块。"""
    modules = set()
    skip_kw = ["test", "demo", "sample", "example", "distribution", "bom"]
    for match in repo.glob("**/src/main"):
        if "target" in str(match):
            continue
        rel = match.relative_to(repo)
        top = str(rel).split("/")[0]
        if not any(kw in top.lower() for kw in skip_kw):
            modules.add(top)
    return sorted(modules)


def run_git(cmd: str, cwd: Path) -> tuple[int, str]:
    """运行 git 命令。"""
    result = subprocess.run(f"git {cmd}", shell=True, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip()


def get_merge_base(repo: Path, tag: str, main_branch: str) -> str:
    """获取 tag 和 main branch 的 merge-base。"""
    # 先检查 origin/main_branch 是否存在
    code, _ = run_git(f"rev-parse --verify origin/{main_branch}", repo)
    main_ref = f"origin/{main_branch}" if code == 0 else main_branch

    code, sha = run_git(f"merge-base {tag} {main_ref}", repo)
    if code != 0:
        print(f"Warning: Failed to find merge-base for {tag}", file=sys.stderr)
        return tag
    return sha


def get_changed_files_in_range(
    repo: Path,
    start_tag: str,
    end_tag: str,
    main_branch: str = "main",
) -> list[str]:
    """获取两个 tag 之间变更的文件列表。"""
    range_spec = f"{start_tag}..{end_tag}"

    code, output = run_git(f"diff --name-only {range_spec}", repo)
    if code != 0:
        print(f"Warning: Failed to get diff for {range_spec}", file=sys.stderr)
        return []

    return output.split("\n") if output else []


# 用于从文件路径中提取测试目录的关键词
TEST_DIR_KEYWORDS = [
    "test",
    "tests",
    "spec",
    "specs",
    "bench",
    "benches",
    "benchmark",
    "benchmarks",
    "benchsuite",
    "fuzz",
    "fuzzing",
    "fuzzer",
    "mock",
    "mocks",
    "stub",
    "stubs",
    "fixture",
    "fixtures",
    "snapshot",
    "snapshots",
    "__snapshots__",
    "golden",
    "e2e",
    "integration",
    "acceptance",
    "cypress",
    "playwright",
    "testdata",
    "configtest",
]


def analyze_git_history(changed_files: list[str]) -> set[str]:
    """分析 Git 历史中的文件变更，发现测试目录模式。"""
    discovered = set()

    for file_path in changed_files:
        if not file_path:
            continue
        for pattern in TEST_FILE_PATTERNS:
            if re.match(pattern, file_path, re.IGNORECASE):
                # 提取测试目录模式
                parts = file_path.split("/")
                for i, part in enumerate(parts):
                    part_lower = part.lower()
                    # 检查是否匹配任何测试目录关键词
                    if any(kw == part_lower or kw in part_lower for kw in TEST_DIR_KEYWORDS):
                        if i > 0:
                            discovered.add("/".join(parts[: i + 1]) + "/**")
                        else:
                            discovered.add(part + "/**")
                        break
                break

    return discovered


def load_config(config_path: Path) -> dict:
    """加载 YAML 配置文件。"""
    import yaml

    with open(config_path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(
        description="检测仓库测试目录配置",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 基础检测
    python detect_test_dirs.py DATA/github_data/repos/apache_dubbo

    # 使用配置文件（推荐，优先使用这个版本）
    python detect_test_dirs.py DATA/github_data/repos/apache_dubbo \\
        --config config/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6.yaml

    # 直接指定参数
    python detect_test_dirs.py DATA/github_data/repos/apache_dubbo \\
        --start-tag dubbo-3.3.3 --end-tag dubbo-3.3.6 \\
        --main-branch 3.3
        """,
    )
    parser.add_argument("repo_path", type=Path, help="仓库路径")
    parser.add_argument("--config", type=Path, help="配置文件路径 (YAML)")
    parser.add_argument("--start-tag", type=str, help="起始 tag")
    parser.add_argument("--end-tag", type=str, help="结束 tag")
    parser.add_argument("--main-branch", type=str, default="main", help="主分支名称")
    parser.add_argument("--format", choices=["yaml", "json"], default="yaml")
    args = parser.parse_args()

    repo = args.repo_path.resolve()
    if not repo.exists():
        print(f"Error: {repo} not found", file=sys.stderr)
        return 1

    # 从配置文件加载参数
    start_tag = args.start_tag
    end_tag = args.end_tag
    main_branch = args.main_branch
    if args.config and args.config.exists():
        config = load_config(args.config)
        if not start_tag and not end_tag:
            # 从配置文件名解析 tag（格式: owner_repo_start_end.yaml）
            name = args.config.stem
            parts = name.rsplit("_", 2)
            if len(parts) >= 3:
                start_tag = parts[-2]
                end_tag = parts[-1]
        main_branch = config.get("main_branch", main_branch)

    # 1. 检测语言
    langs = detect_languages(repo)

    # 2. 收集测试模式
    patterns = set()
    for lang in langs:
        patterns.update(LANG_PATTERNS.get(lang, []))

    # 3. 添加特殊目录
    patterns.update(find_special_test_dirs(repo))

    # 4. Git 历史分析（如果提供了 tag 范围）
    git_discovered = set()
    if start_tag and end_tag:
        print(f"Analyzing Git history: {start_tag}..{end_tag}")
        changed_files = get_changed_files_in_range(repo, start_tag, end_tag, main_branch)
        print(f"  Found {len(changed_files)} changed files")
        git_discovered = analyze_git_history(changed_files)
        if git_discovered:
            print(f"  Discovered {len(git_discovered)} test patterns from Git history")
            patterns.update(git_discovered)
        print()

    # 5. 检测源码模块
    src_modules = detect_src_modules(repo)

    # 输出
    if args.format == "yaml":
        print("# Auto-detected test configuration")
        print(f"# Languages: {', '.join(langs)}")
        if start_tag and end_tag:
            print(f"# Git range: {start_tag}..{end_tag}")
        print()
        print("test_dirs:")
        for p in sorted(patterns):
            marker = " # from git history" if p in git_discovered else ""
            print(f'  - "{p}"{marker}')
        if src_modules:
            print()
            print("repo_src_dirs:")
            for m in src_modules:
                print(f"  - {m}")
    else:
        import json

        print(
            json.dumps(
                {
                    "languages": langs,
                    "test_dirs": sorted(patterns),
                    "git_discovered": sorted(git_discovered),
                    "repo_src_dirs": src_modules,
                },
                indent=2,
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
