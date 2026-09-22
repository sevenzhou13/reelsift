#!/bin/zsh
# 安装 Reelsift 的 Finder 快速操作到当前用户的服务目录。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICES_DIR="$HOME/Library/Services"
BACKUP_DIR="$PROJECT_DIR/data/finder-workflow-backups"

WORKFLOW_NAMES=("Reelsift 扫描素材" "Reelsift AI 整理并命名素材")

mkdir -p "$SERVICES_DIR" "$BACKUP_DIR"
for WORKFLOW_NAME in "${WORKFLOW_NAMES[@]}"; do
  WORKFLOW_SOURCE="$PROJECT_DIR/finder/$WORKFLOW_NAME.workflow"
  WORKFLOW_TARGET="$SERVICES_DIR/$WORKFLOW_NAME.workflow"
  if [[ ! -d "$WORKFLOW_SOURCE" ]]; then
    print -u2 "找不到 Finder 快速操作：$WORKFLOW_SOURCE"
    exit 1
  fi
  if [[ -e "$WORKFLOW_TARGET" ]]; then
    BACKUP_TARGET="$BACKUP_DIR/$WORKFLOW_NAME.$(date +%Y%m%d-%H%M%S).backup.workflow"
    mv "$WORKFLOW_TARGET" "$BACKUP_TARGET"
    print "已备份旧快速操作：$BACKUP_TARGET"
  fi
  ditto "$WORKFLOW_SOURCE" "$WORKFLOW_TARGET"
done
chmod +x "$PROJECT_DIR/scripts/reelsift_finder_scan.py"
chmod +x "$PROJECT_DIR/scripts/reelsift_finder_organize.py"
chmod +x "$PROJECT_DIR/scripts/reelsift_finder_organize_launcher.sh"
"/System/Library/CoreServices/pbs" -update || true
killall Finder || true
print "Finder 快速操作已安装：Reelsift 扫描素材、Reelsift AI 整理并命名素材"
