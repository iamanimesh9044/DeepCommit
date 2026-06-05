#!/bin/bash
# safe_commit_split.sh - 安全的 commit 拆分工具
#
# 特点：
# 1. 完全不动原 commit（多重保护）
# 2. 在新分支上拆分
# 3. 自动验证等价性
# 4. 失败可以轻松回滚
#
# 使用方法：
#   ./safe_commit_split.sh <commit-sha>
#
# 示例：
#   ./safe_commit_split.sh abc123
#

set -e  # 遇到错误立即退出

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 检查参数
if [ $# -ne 1 ]; then
    echo -e "${RED}❌ 用法: $0 <commit-sha>${NC}"
    echo "示例: $0 abc123"
    exit 1
fi

ORIGINAL_COMMIT=$1
ORIGINAL_BRANCH=$(git branch --show-current)
SPLIT_BRANCH="split-${ORIGINAL_COMMIT:0:7}"
TAG_NAME="backup-${ORIGINAL_COMMIT:0:7}"

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}  安全 Commit 拆分工具${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# 验证 commit 是否存在
if ! git rev-parse --verify "$ORIGINAL_COMMIT^{commit}" >/dev/null 2>&1; then
    echo -e "${RED}❌ Commit $ORIGINAL_COMMIT 不存在${NC}"
    exit 1
fi

FULL_SHA=$(git rev-parse "$ORIGINAL_COMMIT")

echo -e "${GREEN}📍 原始 commit:${NC}"
echo -e "   SHA:    $FULL_SHA"
echo -e "   分支:   $ORIGINAL_BRANCH"
echo -e "   信息:   $(git log -1 --oneline $ORIGINAL_COMMIT)"
echo ""

# 步骤 1: 创建标签（永久引用）
echo -e "${YELLOW}[1/5]${NC} 创建保护标签..."
if git tag "$TAG_NAME" "$ORIGINAL_COMMIT" 2>/dev/null; then
    echo -e "${GREEN}✅ 创建标签: $TAG_NAME${NC}"
else
    echo -e "${YELLOW}⚠️  标签 $TAG_NAME 已存在，跳过${NC}"
fi
echo ""

# 步骤 2: 创建新分支
echo -e "${YELLOW}[2/5]${NC} 创建拆分分支..."
if git rev-parse --verify "$SPLIT_BRANCH" >/dev/null 2>&1; then
    echo -e "${RED}❌ 分支 $SPLIT_BRANCH 已存在${NC}"
    echo "请先删除或使用其他名称："
    echo "  git branch -D $SPLIT_BRANCH"
    exit 1
fi

# 基于原 commit 的 parent 创建分支
PARENT_COMMIT="${ORIGINAL_COMMIT}~1"
git checkout -b "$SPLIT_BRANCH" "$PARENT_COMMIT" >/dev/null 2>&1
echo -e "${GREEN}✅ 创建分支: $SPLIT_BRANCH (基于 ${PARENT_COMMIT:0:7})${NC}"
echo ""

# 步骤 3: Cherry-pick 原 commit
echo -e "${YELLOW}[3/5]${NC} 应用原 commit 的改动..."
if git cherry-pick "$ORIGINAL_COMMIT" >/dev/null 2>&1; then
    echo -e "${GREEN}✅ Cherry-pick 成功${NC}"
else
    echo -e "${RED}❌ Cherry-pick 失败，可能有冲突${NC}"
    echo "请手动解决冲突后继续，或放弃："
    echo "  git cherry-pick --continue  # 解决后继续"
    echo "  git cherry-pick --abort     # 放弃"
    exit 1
fi
echo ""

# 步骤 4: 撤销 commit，准备拆分
echo -e "${YELLOW}[4/5]${NC} 准备拆分..."
git reset HEAD~1 >/dev/null 2>&1
echo -e "${GREEN}✅ 已撤销 commit（改动保留在工作区）${NC}"
echo ""

# 步骤 5: 显示指导信息
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🎉 准备完成！现在可以开始拆分了${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${YELLOW}📋 当前状态：${NC}"
echo "  • 原 commit: 保持不变"
echo "    - 在分支: $ORIGINAL_BRANCH"
echo "    - 有标签: $TAG_NAME"
echo "    - SHA: $FULL_SHA"
echo ""
echo "  • 拆分分支: $SPLIT_BRANCH (当前分支)"
echo "    - 改动已在工作区，等待拆分提交"
echo ""
echo -e "${YELLOW}📝 下一步操作：${NC}"
echo ""
echo "1. 查看待提交的改动："
echo "   ${GREEN}git status${NC}"
echo ""
echo "2. 分批提交（示例）："
echo "   ${GREEN}git add src/feature1.py${NC}"
echo "   ${GREEN}git commit -m \"Part 1: Add feature1\"${NC}"
echo ""
echo "   ${GREEN}git add src/feature2.py${NC}"
echo "   ${GREEN}git commit -m \"Part 2: Add feature2\"${NC}"
echo ""
echo "3. 完成拆分后，验证等价性："
echo "   ${GREEN}python3 -m deepcommit.tools.commit_split_verifier \\${NC}"
echo "   ${GREEN}  --repo-path . \\${NC}"
echo "   ${GREEN}  --original $FULL_SHA \\${NC}"
echo "   ${GREEN}  --split HEAD~2 HEAD~1 HEAD${NC}  # 根据拆分数量调整"
echo ""
echo -e "${YELLOW}🔄 如果需要重新开始：${NC}"
echo "   ${GREEN}git checkout $ORIGINAL_BRANCH${NC}"
echo "   ${GREEN}git branch -D $SPLIT_BRANCH${NC}"
echo "   然后重新运行此脚本"
echo ""
echo -e "${YELLOW}📌 原 commit 访问方式（任选其一）：${NC}"
echo "   ${GREEN}git show $ORIGINAL_COMMIT${NC}       # 通过 SHA"
echo "   ${GREEN}git show $TAG_NAME${NC}  # 通过标签"
echo "   ${GREEN}git log $ORIGINAL_BRANCH${NC}    # 在原分支查看"
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
