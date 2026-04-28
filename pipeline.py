# 处理流水线：扫描文件夹、抽取关键帧

import hashlib
import os
from pathlib import Path

import ffmpeg

# Homebrew 安装的 ffmpeg/ffprobe 不在系统 PATH，追加进去
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"

# 支持的视频扩展名（统一转小写比较）
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
FRAME_EXTENSIONS = (".png", ".jpg", ".jpeg")
KEYFRAME_POSITIONS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85)
KEYFRAME_MAX_WIDTH = 480


def scan_folder(folder: Path) -> list[Path]:
    """递归扫描文件夹，返回所有视频文件路径，按文件名排序。"""
    videos = [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    return sorted(videos, key=lambda p: p.name)


def build_video_hash(video_path: Path) -> str:
    """取视频路径字符串的 MD5 前 12 位作为缓存 key。"""
    return hashlib.md5(str(video_path).encode()).hexdigest()[:12]


def get_keyframe_paths(frame_dir: Path, count: int = 6) -> list[Path]:
    """只返回 0.jpg ~ N.jpg，避免把 cover.jpg 混进关键帧列表。"""
    frame_paths: list[Path] = []
    for index in range(count):
        for suffix in FRAME_EXTENSIONS:
            candidate = frame_dir / f"{index}{suffix}"
            if candidate.exists():
                frame_paths.append(candidate)
                break
    return frame_paths


def get_video_duration(video_path: Path) -> float:
    """读取视频时长，返回秒数。"""
    probe = ffmpeg.probe(str(video_path))
    return float(probe["format"]["duration"])


def clear_numbered_keyframes(frame_dir: Path, count: int) -> None:
    """清理编号关键帧，避免失败重试时混入旧文件。"""
    for index in range(count):
        for suffix in FRAME_EXTENSIONS:
            candidate = frame_dir / f"{index}{suffix}"
            if candidate.exists():
                candidate.unlink()


def extract_keyframes_fast(video_path: Path, frame_dir: Path, duration: float, count: int) -> None:
    """用单个 ffmpeg 进程按多个位置抽取关键帧。"""
    positions = [duration * percent for percent in KEYFRAME_POSITIONS[:count]]
    outputs = []
    for index, position in enumerate(positions):
        stream = ffmpeg.input(str(video_path), ss=position)
        stream = stream.filter("scale", f"min({KEYFRAME_MAX_WIDTH},iw)", -2)
        outputs.append(
            stream.output(
                str(frame_dir / f"{index}.png"),
                vframes=1,
            )
        )

    ffmpeg.merge_outputs(*outputs).overwrite_output().run(quiet=True)

    if len(get_keyframe_paths(frame_dir, count)) < count:
        raise RuntimeError("快速抽帧没有生成足够关键帧")


def extract_keyframes_slow(video_path: Path, frame_dir: Path, duration: float, count: int) -> None:
    """逐帧 seek 抽取关键帧，作为兼容回退。"""
    positions = [duration * percent for percent in KEYFRAME_POSITIONS[:count]]
    for index, position in enumerate(positions):
        out_path = frame_dir / f"{index}.png"
        stream = ffmpeg.input(str(video_path), ss=position)
        stream = stream.filter("scale", f"min({KEYFRAME_MAX_WIDTH},iw)", -2)
        (
            stream
            .output(
                str(out_path),
                vframes=1,
            )
            .overwrite_output()
            .run(quiet=True)
        )


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
    video_hash = build_video_hash(video_path)
    frame_dir = cache_dir / video_hash

    # 已缓存则跳过
    if frame_dir.exists():
        existing = get_keyframe_paths(frame_dir, count)
        if len(existing) >= count:
            return video_hash, frame_dir

    frame_dir.mkdir(parents=True, exist_ok=True)

    duration = get_video_duration(video_path)
    clear_numbered_keyframes(frame_dir, count)
    try:
        extract_keyframes_fast(video_path, frame_dir, duration, count)
    except Exception:
        clear_numbered_keyframes(frame_dir, count)
        extract_keyframes_slow(video_path, frame_dir, duration, count)

    return video_hash, frame_dir
