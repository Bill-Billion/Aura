#!/usr/bin/env bash
# S5-T7: 从 Git 历史中彻底清除 gamemcu GLB 资产
#
# ⚠️  此脚本会改写 Git 历史并需要 force-push
# ⚠️  运行前请确保所有协作者已推送本地更改
# ⚠️  运行后所有人需要重新 clone
#
# 用法:
#   chmod +x scripts/purge-gamemcu-glb.sh
#   ./scripts/purge-gamemcu-glb.sh
#
# 前置条件:
#   brew install git-filter-repo  # macOS
#   或 pip install git-filter-repo

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== S5-T7: 清除 gamemcu GLB 资产 ==="
echo ""
echo "将要清除以下文件的所有历史记录："
echo "  frontend/public/models/F1.glb"
echo "  frontend/public/models/F2.glb"
echo "  frontend/public/models/F3.glb"
echo "  frontend/dist/models/F1.glb"
echo "  frontend/dist/models/F2.glb"
echo "  frontend/dist/models/F3.glb"
echo ""

# 检查是否有未提交的更改
if ! git diff-index --quiet HEAD --; then
    echo "❌ 有未提交的更改，请先提交或 stash"
    exit 1
fi

# 检查 git-filter-repo 是否可用
if ! command -v git-filter-repo &>/dev/null; then
    echo "❌ git-filter-repo 未安装"
    echo "   安装: brew install git-filter-repo  (macOS)"
    echo "   或:   pip install git-filter-repo"
    exit 1
fi

# 确认
read -rp "确认要改写 Git 历史吗？这将需要 force-push。[y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "已取消"
    exit 0
fi

# 备份远程 URL
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")

echo ""
echo "=== 执行 git filter-repo ==="
git filter-repo \
    --path frontend/public/models/F1.glb \
    --path frontend/public/models/F2.glb \
    --path frontend/public/models/F3.glb \
    --path frontend/dist/models/F1.glb \
    --path frontend/dist/models/F2.glb \
    --path frontend/dist/models/F3.glb \
    --invert-paths \
    --force

if [ -n "$REMOTE_URL" ]; then
    git remote add origin "$REMOTE_URL"
    echo ""
    echo "=== 已完成 ==="
    echo ""
    echo "下一步（手动执行）："
    echo "  git push origin --force --all"
    echo "  git push origin --force --tags"
    echo ""
    echo "然后通知所有协作者重新 clone。"
else
    echo ""
    echo "=== 已完成 ==="
    echo "未检测到 remote，请手动添加后 force-push。"
fi
