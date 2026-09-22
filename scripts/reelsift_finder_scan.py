#!/usr/bin/env python3
"""Finder 快速操作入口：启动本地 Reelsift 并打开指定文件夹的扫描页。"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PYTHON_BIN = PROJECT_DIR / ".venv" / "bin" / "python"
SERVER_URL = "http://127.0.0.1:8000"
FINDER_LOG_PATH = PROJECT_DIR / "data" / "finder-scan.log"


def write_finder_log(message: str) -> None:
    """记录 Finder 快速操作的关键步骤，便于定位桌面环境故障。"""
    FINDER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with FINDER_LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")


def is_server_running() -> bool:
    """检查本机 Reelsift 服务是否已可访问。"""
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/api/health", timeout=1) as response:
            return response.status < 500
    except (urllib.error.URLError, TimeoutError):
        return False


def start_server() -> None:
    """使用项目现有虚拟环境启动本地服务。"""
    if not PYTHON_BIN.exists():
        raise RuntimeError(f"找不到 Reelsift 虚拟环境：{PYTHON_BIN}")
    log_path = PROJECT_DIR / "data" / "reelsift-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            [
                str(PYTHON_BIN),
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            cwd=PROJECT_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    for _ in range(30):
        if is_server_running():
            return
        time.sleep(0.5)
    raise RuntimeError(f"Reelsift 服务启动失败，请查看日志：{log_path}")


def open_folder_scan(folder: Path) -> None:
    """在默认浏览器打开带有本地素材目录的扫描页。"""
    if not folder.is_dir():
        raise RuntimeError(f"Finder 选中的项目不是文件夹：{folder}")
    write_finder_log(f"收到 Finder 文件夹：{folder}")
    if not is_server_running():
        write_finder_log("本地服务未运行，开始启动。")
        start_server()
    else:
        write_finder_log("本地服务已运行。")
    query = urllib.parse.urlencode({"scan_folder_path": str(folder.resolve())})
    scan_url = f"{SERVER_URL}/upload?{query}"
    chrome_path = Path("/Applications/Google Chrome.app")
    if chrome_path.exists():
        subprocess.Popen(["open", "-a", str(chrome_path), scan_url], start_new_session=True)
        write_finder_log("已请求前台 Chrome 打开扫描页。")
    else:
        subprocess.Popen(["open", scan_url], start_new_session=True)
        write_finder_log("已请求默认浏览器打开扫描页。")


def main() -> None:
    """读取 Finder 传入的文件夹参数。"""
    folders = [Path(value).expanduser() for value in sys.argv[1:]]
    if not folders:
        raise RuntimeError("请先在 Finder 中选中一个素材文件夹。")
    open_folder_scan(folders[0])


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        write_finder_log(f"失败：{exc}")
        print(f"Reelsift Finder 操作失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
