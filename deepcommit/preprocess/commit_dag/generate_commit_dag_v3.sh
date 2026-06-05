#!/usr/bin/env bash

# Commit DAG v3 Generation Script
# 使用说明：
#   1. 直接编辑下面的配置参数（REPO_PATH, COMMIT_FILE, REPO_NAME 等）
#   2. 运行脚本: ./generate_commit_dag_v3.sh
#   3. 输出文件会保存在 DATA/github_data/archive/commit_dag_v3.1/<REPO_NAME>/ 目录下
#   4. 脚本会记录执行时间

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../../" && pwd)"

# Change to project root for relative paths to work
cd "$PROJECT_ROOT"

# ============================================================================
# 配置区域 - 根据实际情况修改以下参数
# ============================================================================
REPO_NAME="urllib3_urllib3"                                                    # 仓库名称（用于生成输出目录）
REPO_PATH="DATA/github_data/repos/${REPO_NAME}"                                # 仓库路径
# COMMIT_FILE="DATA/github_data/preprocessed/${REPO_NAME}_2.0.6_2.3.0/main_commits.jsonl"  # commit 列表文件
COMMIT_FILE="DATA/github_data/preprocessed/urllib3_urllib3_2.0.6_2.3.0/main_commits.jsonl"  # commit 列表文件
SRC_DIR="src/"                                                                 # 源码目录（只分析此目录下的文件）
OUTPUT_BASE_DIR="$PROJECT_ROOT/DATA/github_data/archive/commit_dag_v3.1"            # 输出基础目录

# v3 参数
CONTEXT_LINES=3           # Git blame 上下文行数
PATCH_CONTEXT_LINES=3     # Git apply 验证时的 patch 上下文行数

# ============================================================================
# v3 参数说明：
# --repo                   仓库路径（bare repo 或普通 repo）
# --commit-file            commit 列表文件（文本或 JSONL）
# --outdir                 输出目录（自动生成文件名）
# --context-lines          Git blame 上下文行数（默认: 5）
# --patch-context-lines    Git apply 验证时的 patch 上下文行数（默认: 5）
# --verify-with-patch      启用 git apply 验证（默认: True）
# --no-verify-with-patch   禁用 git apply 验证（更快但可能有误报）
# --reduce-transitive      移除传递冗余边（A→B→C 且 A→C 时，移除 A→C）
# --limit                  限制处理的 commit 数量（留空=全部，常用: 50, 100, 200）

# 可选参数：
# --include-external        包含外部依赖
# --exclude-test-files      排除测试文件
# --exclude-ci-files        排除 CI/CD 文件
# --exclude-config-files    排除配置文件
# --exclude-doc-files       排除文档文件
# --exclude-patterns A B    排除自定义模式（如 "docs/**/*" "*.md"）
# --repo-src-dir DIR        只分析指定目录下的文件（如 "src/" "lib/"）
# --print-levels            打印并行执行层级
# ============================================================================

# 记录开始时间
START_TIME=$(date +%s)
START_TIME_HUMAN=$(date '+%Y-%m-%d %H:%M:%S')

echo "============================================================================"
echo "Commit DAG v3 构建开始"
echo "开始时间: $START_TIME_HUMAN"
echo "============================================================================"
echo "仓库: $REPO_NAME"
echo "输出目录: $OUTPUT_BASE_DIR/$REPO_NAME/"
echo "上下文行数: $CONTEXT_LINES"
echo "Patch 上下文行数: $PATCH_CONTEXT_LINES"
echo "============================================================================"
echo ""

# ============================================================================
# 执行 DAG 构建
# ============================================================================
python "$SCRIPT_DIR/commit_dag_v3.py" \
    --repo "$REPO_PATH" \
    --commit-file "$COMMIT_FILE" \
    --outdir "$OUTPUT_BASE_DIR" \
    --context-lines "$CONTEXT_LINES" \
    --patch-context-lines "$PATCH_CONTEXT_LINES" \
    --verify-with-patch \
    --repo-src-dir "$SRC_DIR" \
    --reduce-transitive \
    --use-pygit2
    # --exclude-test-files --exclude-ci-files --exclude-config-files --exclude-doc-files \
    # --no-verify-with-patch \
    # --limit 100

# 捕获命令执行结果
RESULT=$?

# 记录 DAG 构建结束时间
DAG_BUILD_END_TIME=$(date +%s)
DAG_BUILD_DURATION=$((DAG_BUILD_END_TIME - START_TIME))

echo ""
echo "============================================================================"
if [ $RESULT -eq 0 ]; then
    echo "✓ DAG 构建完成！"
else
    echo "✗ DAG 构建失败（退出码: $RESULT）"
fi
echo "============================================================================"
echo "输出目录: $OUTPUT_BASE_DIR/$REPO_NAME/"
echo "DAG 构建耗时: ${DAG_BUILD_DURATION}s ($(printf '%02d:%02d:%02d' $((DAG_BUILD_DURATION/3600)) $((DAG_BUILD_DURATION%3600/60)) $((DAG_BUILD_DURATION%60))))"
echo "============================================================================"

# 如果构建失败，退出
if [ $RESULT -ne 0 ]; then
    exit $RESULT
fi

# ============================================================================
# 转换 DOT 文件为 PNG 图片（需要安装 graphviz）
# ============================================================================
# 查找生成的 DOT 文件（文件名包含上下文参数）
DOT_FILE=$(ls "$OUTPUT_BASE_DIR/$REPO_NAME"/commit_dag_v3_ctx_*.dot 2>/dev/null | head -n 1)

if [ -f "$DOT_FILE" ]; then
    PNG_FILE="${DOT_FILE%.dot}.png"

    if command -v dot &> /dev/null; then
        echo ""
        echo "正在转换 DOT 文件为 PNG..."
        PNG_START_TIME=$(date +%s)
        dot -Tpng "$DOT_FILE" -o "$PNG_FILE"
        PNG_END_TIME=$(date +%s)
        PNG_DURATION=$((PNG_END_TIME - PNG_START_TIME))
        echo "✓ PNG 图片已生成: $PNG_FILE"
        echo "PNG 转换耗时: ${PNG_DURATION}s"
    else
        echo ""
        echo "警告: 未找到 graphviz 命令 'dot'，跳过 PNG 转换"
        echo "安装方法: sudo apt-get install graphviz  或  brew install graphviz"
    fi
else
    echo ""
    echo "警告: 未找到 DOT 文件在目录: $OUTPUT_BASE_DIR/$REPO_NAME/"
fi

# ============================================================================
# 记录总结束时间
# ============================================================================
END_TIME=$(date +%s)
END_TIME_HUMAN=$(date '+%Y-%m-%d %H:%M:%S')
TOTAL_DURATION=$((END_TIME - START_TIME))

echo ""
echo "============================================================================"
echo "全部完成！"
echo "结束时间: $END_TIME_HUMAN"
echo "总运行时间: ${TOTAL_DURATION}s ($(printf '%02d:%02d:%02d' $((TOTAL_DURATION/3600)) $((TOTAL_DURATION%3600/60)) $((TOTAL_DURATION%60))))"
echo "============================================================================"
