#!/bin/zsh
# Finder 服务启动器：先记录调用，再用项目虚拟环境运行整理脚本。

set -u

PROJECT_DIR="/Users/seven/AI-agent/reelsift"
LOG_PATH="$PROJECT_DIR/data/finder-organize.log"
mkdir -p "$PROJECT_DIR/data"
print "[$(date '+%Y-%m-%d %H:%M:%S')] Finder 服务启动，参数数量：$#" >> "$LOG_PATH"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/reelsift_finder_organize.py" "$@"
