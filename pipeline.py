# 处理流水线：扫描文件夹、抽取关键帧

import hashlib
import os
from pathlib import Path

import ffmpeg

# Homebrew 安装的 ffmpeg/ffprobe 不在系统 PATH，追加进去
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"

# 支持的视频扩展名（统一转小写比较）
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}


def scan_folder(folder: Path) -> list[Path]:
    """递归扫描文件夹，返回所有视频文件路径，按文件名排序。"""
    videos = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    return sorted(videos, key=lambda p: p.name)


def _video_hash(video_path: Path) -> str:
    """取视频路径字符串的 MD5 前 12 位作为缓存 key。"""
    return hashlib.md5(str(video_path).encode()).hexdigest()[:12]


def extract_keyframes(
    video_path: Path,
    cache_dir: Path,
    count: int = 6,
) -> tuple[str, Path]:
    """
    为单个视频抽取 count 张关键帧，存到 cache_dir/{hash}/。
    已有足够帧数时直接跳过。
    返回 (video_hash, 帧目录)。
    """
    video_hash = _video_hash(video_path)
    frame_dir = cache_dir / video_hash

    # 已缓存则跳过
    if frame_dir.exists():
        existing = list(frame_dir.glob("*.jpg"))
        if len(existing) >= count:
            return video_hash, frame_dir

    frame_dir.mkdir(parents=True, exist_ok=True)

    # 获取视频时长（秒）
    probe = ffmpeg.probe(str(video_path))
    duration = float(probe["format"]["duration"])

    # 抽帧位置：均匀分布在 10%~85% 之间，避开首尾
    positions = [duration * p for p in [0.10, 0.25, 0.40, 0.55, 0.70, 0.85]][:count]

    for i, t in enumerate(positions):
        out_path = frame_dir / f"{i}.jpg"
        (
            ffmpeg
            .input(str(video_path), ss=t)
            .filter("scale", 480, -1)
            .output(str(out_path), vframes=1, **{"q:v": 3})
            .overwrite_output()
            .run(quiet=True)
        )

    return video_hash, frame_dir
