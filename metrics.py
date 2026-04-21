# OpenCV 清晰度 / 抖动评分：基于关键帧计算清晰度并选封面

import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2


@dataclass
class FrameScore:
    path: Path
    sharpness: float


@dataclass
class VideoMetrics:
    sharpness_score: float
    cover_path: Path
    best_frame_path: Path
    frame_scores: list[FrameScore]


def _calculate_frame_sharpness(frame_path: Path) -> float:
    """用拉普拉斯方差估算单帧清晰度。"""
    image = cv2.imread(str(frame_path))
    if image is None:
        raise ValueError(f"无法读取关键帧：{frame_path}")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def score_keyframes(frame_paths: list[Path], cover_path: Path) -> VideoMetrics:
    """计算关键帧清晰度分，并把最清晰的一帧复制为封面。"""
    if not frame_paths:
        raise ValueError("没有可评分的关键帧")

    frame_scores = [
        FrameScore(path=frame_path, sharpness=_calculate_frame_sharpness(frame_path))
        for frame_path in sorted(frame_paths)
    ]
    best_frame = max(frame_scores, key=lambda item: item.sharpness)

    cover_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best_frame.path, cover_path)

    average_score = sum(item.sharpness for item in frame_scores) / len(frame_scores)
    return VideoMetrics(
        sharpness_score=average_score,
        cover_path=cover_path,
        best_frame_path=best_frame.path,
        frame_scores=frame_scores,
    )
