#!/bin/zsh
# 构建并安装原生 Reelsift Finder 服务应用。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_PATH="$PROJECT_DIR/macos/ReelsiftFinderService.swift"
APP_SOURCE="$PROJECT_DIR/macos/ReelsiftFinderService.app"
APP_TARGET="$HOME/Applications/Reelsift Finder 服务.app"
EXECUTABLE="$APP_SOURCE/Contents/MacOS/ReelsiftFinderService"
MODULE_CACHE="$PROJECT_DIR/data/.swift-module-cache"

mkdir -p "$HOME/Applications"
mkdir -p "$MODULE_CACHE"
swiftc -module-cache-path "$MODULE_CACHE" "$SOURCE_PATH" -o "$EXECUTABLE"
killall ReelsiftFinderService 2>/dev/null || true
if [[ -e "$APP_TARGET" ]]; then
  BACKUP_TARGET="$PROJECT_DIR/data/Reelsift Finder 服务.$(date +%Y%m%d-%H%M%S).backup.app"
  mv "$APP_TARGET" "$BACKUP_TARGET"
  print "已备份旧原生服务：$BACKUP_TARGET"
fi
ditto "$APP_SOURCE" "$APP_TARGET"
"/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister" -f "$APP_TARGET"
open "$APP_TARGET"
"/System/Library/CoreServices/pbs" -update || true
killall Finder || true
print "原生 Finder 服务已安装：Reelsift 原生整理素材"
