# FastAPI 服务器：提供上传流程、素材网格与导出页面

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import ffmpeg
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai import analyze_video
from asr import ASREmptyResult, ASRError, is_asr_configured, transcribe_video
from db import (
    ClipRecord,
    LibraryRecord,
    ProjectNodeRecord,
    RecycleClipRecord,
    TranscriptRecord,
    append_clip_tags,
    attach_clip_to_node,
    create_library,
    create_project_node,
    delete_clips,
    delete_library,
    delete_project_node,
    get_library_by_id,
    get_project_node,
    init_db,
    list_libraries,
    list_project_nodes,
    list_recycled_clips,
    load_transcripts,
    rename_library,
    restore_recycled_clips,
    rename_project_node,
    save_clip,
    save_transcripts,
    move_project_node,
    move_clip_to_node,
    update_clip_note,
    update_clip_summary,
)
from metrics import build_comparison_ranking, build_comparison_scores, select_cover_frame
from pipeline import VIDEO_EXTENSIONS, build_video_hash, extract_keyframes, get_keyframe_paths


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "reelsift.db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
PICKS_DIR = BASE_DIR / "data" / "picks"
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
CACHE_DIR = BASE_DIR / "data" / "thumbnails"
PREVIEWS_DIR = BASE_DIR / "data" / "previews"

STAGE_META = {
    "queued": {"label": "排队中", "progress": 8},
    "saved": {"label": "已接收", "progress": 20},
    "extracting": {"label": "抽帧中", "progress": 42},
    "scoring": {"label": "评分中", "progress": 58},
    "analyzing": {"label": "AI 分析中", "progress": 84},
    "done": {"label": "已完成", "progress": 100},
    "failed": {"label": "处理失败", "progress": 100},
}


@dataclass
class ClipCard:
    id: int
    filename: str
    filepath: str
    summary: str
    scene: str
    subjects: list[str]
    actions: list[str]
    tags: list[str]
    has_motion: bool
    sharpness_score: float | None
    cover_url: str | None
    status: str
    visual_error_message: str | None
    transcript_status: str
    transcript_error_message: str | None
    comparison_scores: dict[str, float] | None = None
    preview_status: str = "pending"
    comparison_status: str = "pending"
    comparison_error_message: str | None = None
    folder_label: str = "未分类"


@dataclass
class ClipDetail:
    id: int
    library_id: int
    library_name: str
    filename: str
    filepath: str
    summary: str
    scene: str
    subjects: list[str]
    actions: list[str]
    tags: list[str]
    has_motion: bool
    sharpness_score: float | None
    cover_url: str | None
    keyframe_urls: list[str]
    media_url: str | None
    media_type: str | None
    media_error_message: str | None
    preview_status: str
    status: str
    visual_error_message: str | None
    file_size_text: str
    shot_time_text: str
    transcripts: list[dict[str, Any]]
    transcript_available: bool
    transcript_status: str
    transcript_error_message: str | None
    comparison_scores: dict[str, float] | None = None
    comparison_status: str = "pending"
    comparison_error_message: str | None = None
    user_note: str | None = None


@dataclass
class UploadItemState:
    filename: str
    filepath: str
    stage: str = "queued"
    progress: int = 8
    detail: str = "等待开始"
    error_message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class UploadJobState:
    job_id: str
    batch_name: str
    batch_dir: str
    library_id: int
    library_name: str
    items: list[UploadItemState] = field(default_factory=list)
    status: str = "queued"
    message: str = "等待上传"
    redirect_url: str = "/"
    started_at: float = field(default_factory=time.time)


app = FastAPI(title="Reelsift")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/data", StaticFiles(directory=BASE_DIR / "data"), name="data")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
init_db(DB_PATH)

UPLOAD_JOBS: dict[str, UploadJobState] = {}
UPLOAD_LOCK = threading.Lock()
TRANSCRIPT_JOBS: dict[int, str] = {}
TRANSCRIPT_LOCK = threading.Lock()
PREVIEW_LOCK = threading.Lock()
ASSET_JOBS: dict[int, str] = {}
ASSET_LOCK = threading.Lock()
TREE_COLLAPSE_KEY = "reelsift-tree-collapsed"


def build_uncategorized_node_id(library_id: int) -> int:
    """返回当前素材库的未分类虚拟节点 ID。"""
    return -1000000 - library_id


def get_connection() -> sqlite3.Connection:
    """创建只读连接用于页面查询。"""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000;")
    return conn


def build_cover_url(cover_path: str | None) -> str | None:
    """把数据库里的 cover 路径转成静态资源 URL。"""
    if not cover_path:
        return None

    cover_file = Path(cover_path)
    try:
        return f"/data/{cover_file.relative_to(BASE_DIR / 'data').as_posix()}"
    except ValueError:
        return None


def build_data_url_from_path(file_path: str | None) -> str | None:
    """把 data 目录下的文件路径转成可访问 URL。"""
    if not file_path:
        return None

    target = Path(file_path)
    try:
        return f"/data/{target.relative_to(BASE_DIR / 'data').as_posix()}"
    except ValueError:
        return None


def format_file_size(size_bytes: int) -> str:
    """把字节数转成更适合页面展示的文本。"""
    size = float(size_bytes)
    units = ["B", "KB", "MB", "GB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def build_keyframe_urls(cover_path: str | None) -> list[str]:
    """根据 cover 路径推断同目录下的关键帧 URL。"""
    if not cover_path:
        return []

    cover_file = Path(cover_path)
    frame_urls: list[str] = []
    for index in range(6):
        for suffix in (".png", ".jpg", ".jpeg"):
            frame_path = cover_file.parent / f"{index}{suffix}"
            frame_url = build_data_url_from_path(str(frame_path))
            if frame_url:
                frame_urls.append(frame_url)
                break
    return frame_urls


def build_preview_path(video_path: Path) -> Path:
    """根据原视频路径生成浏览器预览文件路径。"""
    video_hash = build_video_hash(video_path)
    return PREVIEWS_DIR / video_hash / "preview.mp4"


def ensure_preview_video(video_path: Path) -> Path:
    """按需生成浏览器可播放的 mp4 预览文件。"""
    preview_path = build_preview_path(video_path)
    preview_path.parent.mkdir(parents=True, exist_ok=True)

    if preview_path.exists() and preview_path.stat().st_mtime >= video_path.stat().st_mtime:
        return preview_path

    with PREVIEW_LOCK:
        if preview_path.exists() and preview_path.stat().st_mtime >= video_path.stat().st_mtime:
            return preview_path

        stream = ffmpeg.input(str(video_path))
        (
            ffmpeg
            .output(
                stream,
                str(preview_path),
                vcodec="libx264",
                acodec="aac",
                pix_fmt="yuv420p",
                movflags="+faststart",
                vf="scale='min(1280,iw)':-2",
            )
            .overwrite_output()
            .run(quiet=True)
        )
    return preview_path


def format_timestamp(ms: int) -> str:
    """把毫秒时间转成 mm:ss 文本。"""
    total_seconds = max(ms // 1000, 0)
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def build_transcript_context(segments: list[TranscriptSegment]) -> str:
    """把 transcript 分段拼成适合摘要生成的上下文文本。"""
    if not segments:
        return ""
    lines: list[str] = []
    total_length = 0
    for item in segments:
        line = f"{format_timestamp(item.start_ms)} {item.text.strip()}"
        if not item.text.strip():
            continue
        if total_length + len(line) > 500:
            break
        lines.append(line)
        total_length += len(line)
    return "\n".join(lines)


def normalize_transcript_status(status: str | None, transcripts: list[TranscriptRecord]) -> str:
    """把历史数据和当前记录映射成稳定的 transcript 状态。"""
    if transcripts:
        return "done"
    if not status:
        return "pending"
    return status


def build_transcript_panel_context(clip: ClipDetail) -> dict[str, Any]:
    """构造 transcript 局部模板上下文。"""
    return {"clip": clip}


def resolve_library_id(library_id: int | None = None) -> int:
    """返回当前要查看的素材库 ID。"""
    libraries = list_libraries(DB_PATH)
    if not libraries:
        default_library = create_library("默认素材库", DB_PATH)
        return default_library.id
    if library_id is None:
        return libraries[0].id
    library = get_library_by_id(library_id, DB_PATH)
    if library is None:
        return libraries[0].id
    return library.id


def resolve_node_id(library_id: int, node_id: int | None = None) -> int | None:
    """返回当前项目节点 ID，默认取根节点。"""
    nodes = list_project_nodes(library_id, DB_PATH)
    if not nodes:
        return None
    uncategorized_id = build_uncategorized_node_id(library_id)
    if node_id == uncategorized_id:
        return uncategorized_id
    if node_id is None:
        root_node = next((item for item in nodes if item.parent_id is None), None)
        return root_node.id if root_node is not None else nodes[0].id
    matched = next((item for item in nodes if item.id == node_id and item.library_id == library_id), None)
    if matched is None:
        root_node = next((item for item in nodes if item.parent_id is None), None)
        return root_node.id if root_node is not None else nodes[0].id
    return matched.id


def build_project_tree(
    library_id: int,
    current_node_id: int | None,
) -> list[dict[str, Any]]:
    """构造项目树视图数据。"""
    nodes = list_project_nodes(library_id, DB_PATH)
    child_counts: dict[int, int] = {}
    for node in nodes:
        if node.parent_id is not None:
            child_counts[node.parent_id] = child_counts.get(node.parent_id, 0) + 1
    tree = [
        {
            "id": node.id,
            "name": node.name,
            "depth": node.depth,
            "clip_count": node.clip_count,
            "is_active": node.id == current_node_id,
            "is_root": node.parent_id is None,
            "parent_id": node.parent_id,
            "has_children": child_counts.get(node.id, 0) > 0,
            "is_system": node.parent_id is None,
        }
        for node in nodes
    ]
    root_node = next((node for node in nodes if node.parent_id is None), None)
    if root_node is not None:
        tree.insert(
            1,
            {
                "id": build_uncategorized_node_id(library_id),
                "name": "未分类",
                "depth": 1,
                "clip_count": count_uncategorized_clips(library_id),
                "is_active": current_node_id == build_uncategorized_node_id(library_id),
                "is_root": False,
                "parent_id": root_node.id,
                "has_children": False,
                "is_system": True,
            },
        )
    return tree


def count_uncategorized_clips(library_id: int) -> int:
    """统计当前素材库未归入任何子文件夹的素材数。"""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT c.id) AS count
        FROM clips c
        LEFT JOIN recycled_clips rc ON rc.clip_id = c.id
        WHERE c.library_id = ?
          AND c.status = 'done'
          AND rc.clip_id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM clip_node_refs ref
              JOIN project_nodes n ON n.id = ref.node_id
              WHERE ref.clip_id = c.id
                AND n.library_id = c.library_id
                AND n.parent_id IS NOT NULL
          )
        """,
        (library_id,),
    ).fetchone()
    conn.close()
    return int(row["count"]) if row is not None else 0


def clamp_score(value: float, *, minimum: float = 0.0, maximum: float = 100.0) -> float:
    """把分数压到稳定区间。"""
    return max(minimum, min(maximum, value))


def build_compare_payload(clip: ClipDetail) -> dict[str, Any]:
    """构造相似素材对比页的单条评分数据。"""
    frame_dir = CACHE_DIR / build_video_hash(Path(clip.filepath))
    frame_paths = get_keyframe_paths(frame_dir)
    if not frame_paths:
        raise ValueError(f"素材缺少关键帧：{clip.filename}")

    content_score = clamp_score(
        35.0
        + len(clip.actions) * 12.0
        + len(clip.subjects) * 9.0
        + len(clip.tags) * 4.0
        + (8.0 if clip.has_motion else 0.0)
    )
    speech_score = clamp_score(
        15.0
        + (45.0 if clip.transcript_status == "done" and clip.transcripts else 0.0)
        + min(sum(len(item["text"]) for item in clip.transcripts) / 12.0, 40.0)
    )
    composition_score = clamp_score(
        30.0
        + len(clip.subjects) * 10.0
        + (12.0 if clip.cover_url else 0.0)
        + min(len(clip.summary) / 3.0, 25.0)
    )
    ranking = build_comparison_ranking(
        frame_paths,
        content_score=content_score,
        speech_score=speech_score,
        composition_score=composition_score,
    )
    return {
        "clip": clip,
        "overall_score": ranking.overall_score,
        "sharpness_score": ranking.sharpness_score,
        "stability_score": ranking.stability_score,
        "content_score": ranking.content_score,
        "speech_score": ranking.speech_score,
        "composition_score": ranking.composition_score,
    }


def load_compare_payloads(clip_ids: list[int]) -> list[dict[str, Any]]:
    """读取对比页需要的素材与评分。"""
    payloads: list[dict[str, Any]] = []
    for clip_id in clip_ids[:6]:
        clip = load_clip_detail(clip_id)
        if clip is None:
            continue
        payloads.append(build_compare_payload(clip))
    payloads.sort(key=lambda item: item["overall_score"], reverse=True)
    for index, item in enumerate(payloads, start=1):
        item["rank"] = index
        item["is_recommended"] = index == 1
    return payloads


def build_recommendation_reason(item: dict[str, Any], compare_items: list[dict[str, Any]]) -> str:
    """生成更接近人工粗筛口吻的推荐理由。"""
    reasons: list[str] = []
    if item["rank"] == 1:
        reasons.append("这一条整体最稳，适合作为这一组的优先保留项。")
    if item["sharpness_score"] >= max(other["sharpness_score"] for other in compare_items) - 0.1:
        reasons.append("画面清晰度处在这一组前列。")
    if item["stability_score"] >= max(other["stability_score"] for other in compare_items) - 0.1:
        reasons.append("镜头更稳，后期可用率更高。")
    if item["content_score"] >= max(other["content_score"] for other in compare_items) - 0.1:
        reasons.append("主体和动作信息更完整，粗筛时更容易留下。")
    if item["speech_score"] >= max(other["speech_score"] for other in compare_items) - 0.1 and item["speech_score"] > 25:
        reasons.append("口播信息更可用，适合保留作讲解或说明素材。")
    if item["composition_score"] >= max(other["composition_score"] for other in compare_items) - 0.1:
        reasons.append("构图更完整，封面和剪辑落点都更好用。")
    if not reasons:
        reasons.append("综合表现比较均衡，作为备选保留也有价值。")
    return " ".join(reasons[:3])


def load_clips(
    query: str = "",
    tag: str = "",
    include_failed: bool = False,
    library_id: int | None = None,
    node_id: int | None = None,
) -> list[ClipCard]:
    """从 SQLite 读取素材卡片数据，并支持关键词与标签过滤。"""
    conn = get_connection()
    filters: list[str] = []
    params: list[Any] = []
    current_library_id = resolve_library_id(library_id)
    current_uncategorized_id = build_uncategorized_node_id(current_library_id)

    filters.append("c.library_id = ?")
    params.append(current_library_id)
    filters.append("rc.clip_id IS NULL")

    if node_id is not None:
        if node_id == current_uncategorized_id:
            filters.append(
                """
                NOT EXISTS (
                    SELECT 1
                    FROM clip_node_refs ref2
                    JOIN project_nodes n2 ON n2.id = ref2.node_id
                    WHERE ref2.clip_id = c.id
                      AND n2.library_id = c.library_id
                      AND n2.parent_id IS NOT NULL
                )
                """
            )
        else:
            filters.append(
                """
                EXISTS (
                    SELECT 1
                    FROM clip_node_refs ref2
                    WHERE ref2.clip_id = c.id AND ref2.node_id = ?
                )
                """
            )
            params.append(node_id)

    if not include_failed:
        filters.append("c.status = 'done'")

    if query:
        filters.append(
            """
            (
                c.filename LIKE ?
                OR c.summary LIKE ?
                OR c.scene LIKE ?
                OR EXISTS (
                    SELECT 1 FROM clip_tags t2
                    WHERE t2.clip_id = c.id AND t2.tag LIKE ?
                )
            )
            """
        )
        like_query = f"%{query}%"
        params.extend([like_query, like_query, like_query, like_query])

    if tag:
        filters.append(
            """
            EXISTS (
                SELECT 1 FROM clip_tags t3
                WHERE t3.clip_id = c.id AND t3.tag = ?
            )
            """
        )
        params.append(tag)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = conn.execute(
        f"""
        SELECT
            c.id,
            c.filename,
            c.filepath,
            c.summary,
            c.scene,
            c.subjects_json,
            c.actions_json,
            c.has_motion,
            c.sharpness_score,
            c.cover_path,
            c.status,
            c.error_message,
            c.transcript_status,
            c.transcript_error_message,
            c.preview_status,
            c.preview_path,
            c.preview_error_message,
            c.comparison_status,
            c.comparison_scores_json,
            c.comparison_error_message,
            (
                SELECT GROUP_CONCAT(n3.name, '|||')
                FROM clip_node_refs ref3
                JOIN project_nodes n3 ON n3.id = ref3.node_id
                WHERE ref3.clip_id = c.id
                  AND n3.library_id = c.library_id
                  AND n3.parent_id IS NOT NULL
            ) AS folder_names_text,
            GROUP_CONCAT(t.tag, '|||') AS tags_text
        FROM clips c
        LEFT JOIN clip_tags t ON t.clip_id = c.id
        LEFT JOIN recycled_clips rc ON rc.clip_id = c.id
        {where_sql}
        GROUP BY c.id
        ORDER BY c.updated_at DESC, c.id DESC
        """,
        params,
    ).fetchall()
    conn.close()

    cards: list[ClipCard] = []
    for row in rows:
        cards.append(
            ClipCard(
                id=int(row["id"]),
                filename=row["filename"],
                filepath=row["filepath"],
                summary=row["summary"] or "暂无摘要",
                scene=row["scene"] or "未识别场景",
                subjects=json.loads(row["subjects_json"] or "[]"),
                actions=json.loads(row["actions_json"] or "[]"),
                tags=(row["tags_text"] or "").split("|||") if row["tags_text"] else [],
                has_motion=bool(row["has_motion"]) if row["has_motion"] is not None else False,
                sharpness_score=row["sharpness_score"],
                cover_url=build_cover_url(row["cover_path"]),
                status=row["status"],
                visual_error_message=row["error_message"],
                transcript_status=row["transcript_status"] or "pending",
                transcript_error_message=row["transcript_error_message"],
                preview_status=row["preview_status"] or "pending",
                comparison_status=row["comparison_status"] or "pending",
                comparison_scores=json.loads(row["comparison_scores_json"]) if row["comparison_scores_json"] else None,
                comparison_error_message=row["comparison_error_message"],
                folder_label=((row["folder_names_text"] or "").split("|||")[0] if row["folder_names_text"] else "未分类"),
            )
        )
    return cards


def load_clip_card(clip_id: int) -> ClipCard | None:
    """读取单条素材卡片数据。"""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
            c.id,
            c.filename,
            c.filepath,
            c.summary,
            c.scene,
            c.subjects_json,
            c.actions_json,
            c.has_motion,
            c.sharpness_score,
            c.cover_path,
            c.status,
            c.error_message,
            c.transcript_status,
            c.transcript_error_message,
            c.preview_status,
            c.preview_path,
            c.preview_error_message,
            c.comparison_status,
            c.comparison_scores_json,
            c.comparison_error_message,
            (
                SELECT GROUP_CONCAT(n3.name, '|||')
                FROM clip_node_refs ref3
                JOIN project_nodes n3 ON n3.id = ref3.node_id
                WHERE ref3.clip_id = c.id
                  AND n3.library_id = c.library_id
                  AND n3.parent_id IS NOT NULL
            ) AS folder_names_text,
            GROUP_CONCAT(t.tag, '|||') AS tags_text
        FROM clips c
        LEFT JOIN clip_tags t ON t.clip_id = c.id
        WHERE c.id = ?
        GROUP BY c.id
        """,
        (clip_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return ClipCard(
        id=int(row["id"]),
        filename=row["filename"],
        filepath=row["filepath"],
        summary=row["summary"] or "暂无摘要",
        scene=row["scene"] or "未识别场景",
        subjects=json.loads(row["subjects_json"] or "[]"),
        actions=json.loads(row["actions_json"] or "[]"),
        tags=(row["tags_text"] or "").split("|||") if row["tags_text"] else [],
        has_motion=bool(row["has_motion"]) if row["has_motion"] is not None else False,
        sharpness_score=row["sharpness_score"],
        cover_url=build_cover_url(row["cover_path"]),
        status=row["status"],
        visual_error_message=row["error_message"],
        transcript_status=row["transcript_status"] or "pending",
        transcript_error_message=row["transcript_error_message"],
        preview_status=row["preview_status"] or "pending",
        comparison_status=row["comparison_status"] or "pending",
        comparison_scores=json.loads(row["comparison_scores_json"]) if row["comparison_scores_json"] else None,
        comparison_error_message=row["comparison_error_message"],
        folder_label=((row["folder_names_text"] or "").split("|||")[0] if row["folder_names_text"] else "未分类"),
    )


def load_tags(library_id: int | None = None, node_id: int | None = None) -> list[tuple[str, int]]:
    """读取所有标签及其数量。"""
    conn = get_connection()
    current_library_id = resolve_library_id(library_id)
    params: list[Any] = [current_library_id]
    node_filter = ""
    current_uncategorized_id = build_uncategorized_node_id(current_library_id)
    if node_id is not None:
        if node_id == current_uncategorized_id:
            node_filter = """
            AND NOT EXISTS (
                SELECT 1
                FROM clip_node_refs ref2
                JOIN project_nodes n2 ON n2.id = ref2.node_id
                WHERE ref2.clip_id = c.id
                  AND n2.library_id = c.library_id
                  AND n2.parent_id IS NOT NULL
            )
            """
        else:
            node_filter = """
            AND EXISTS (
                SELECT 1
                FROM clip_node_refs ref2
                WHERE ref2.clip_id = c.id AND ref2.node_id = ?
            )
            """
            params.append(node_id)
    rows = conn.execute(
        f"""
        SELECT t.tag, COUNT(DISTINCT t.clip_id) AS count
        FROM clip_tags t
        JOIN clips c ON c.id = t.clip_id
        LEFT JOIN recycled_clips rc ON rc.clip_id = c.id
        WHERE c.library_id = ? AND c.status = 'done' AND rc.clip_id IS NULL {node_filter}
        GROUP BY t.tag
        ORDER BY count DESC, t.tag ASC
        """,
        params,
    ).fetchall()
    conn.close()
    return [(row["tag"], int(row["count"])) for row in rows]


def load_clip_detail(clip_id: int) -> ClipDetail | None:
    """读取单条素材详情。"""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
            c.id,
            c.library_id,
            c.filename,
            c.filepath,
            c.summary,
            c.scene,
            c.subjects_json,
            c.actions_json,
            c.has_motion,
            c.sharpness_score,
            c.cover_path,
            c.status,
            c.error_message,
            c.transcript_status,
            c.transcript_error_message,
            c.preview_status,
            c.preview_path,
            c.preview_error_message,
            c.comparison_status,
            c.comparison_scores_json,
            c.comparison_error_message,
            c.user_note,
            l.name AS library_name,
            GROUP_CONCAT(t.tag, '|||') AS tags_text
        FROM clips c
        LEFT JOIN libraries l ON l.id = c.library_id
        LEFT JOIN clip_tags t ON t.clip_id = c.id
        WHERE c.id = ?
        GROUP BY c.id
        """,
        (clip_id,),
    ).fetchone()
    conn.close()

    if row is None:
        return None

    file_path = Path(row["filepath"])
    file_size_text = "未知"
    shot_time_text = "未知"
    media_url: str | None = None
    media_type: str | None = None
    media_error_message: str | None = None
    preview_status = row["preview_status"] or "pending"
    if file_path.exists():
        stat = file_path.stat()
        file_size_text = format_file_size(stat.st_size)
        shot_time_text = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        preview_path = row["preview_path"]
        if preview_path:
            media_url = build_data_url_from_path(preview_path)
            media_type = "video/mp4"
        media_error_message = row["preview_error_message"]

    cover_path = row["cover_path"]
    transcript_records = load_transcripts(clip_id, DB_PATH)
    transcript_status = normalize_transcript_status(row["transcript_status"], transcript_records)
    transcripts = [
        {
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
            "text": item.text,
            "time_text": format_timestamp(item.start_ms),
        }
        for item in transcript_records
    ]
    return ClipDetail(
        id=int(row["id"]),
        library_id=int(row["library_id"]),
        library_name=row["library_name"] or "默认素材库",
        filename=row["filename"],
        filepath=row["filepath"],
        summary=row["summary"] or "暂无摘要",
        scene=row["scene"] or "未识别场景",
        subjects=json.loads(row["subjects_json"] or "[]"),
        actions=json.loads(row["actions_json"] or "[]"),
        tags=(row["tags_text"] or "").split("|||") if row["tags_text"] else [],
        has_motion=bool(row["has_motion"]) if row["has_motion"] is not None else False,
        sharpness_score=row["sharpness_score"],
        cover_url=build_cover_url(cover_path),
        keyframe_urls=build_keyframe_urls(cover_path),
        media_url=media_url,
        media_type=media_type,
        media_error_message=media_error_message,
        preview_status=preview_status,
        status=row["status"],
        visual_error_message=row["error_message"],
        file_size_text=file_size_text,
        shot_time_text=shot_time_text,
        transcripts=transcripts,
        transcript_available=is_asr_configured(),
        transcript_status=transcript_status,
        transcript_error_message=row["transcript_error_message"],
        comparison_scores=json.loads(row["comparison_scores_json"]) if row["comparison_scores_json"] else None,
        comparison_status=row["comparison_status"] or "pending",
        comparison_error_message=row["comparison_error_message"],
        user_note=row["user_note"],
    )


def load_similar_clips(current_clip_id: int, scene: str, limit: int = 3) -> list[ClipCard]:
    """按相同场景读取相似素材，用于详情页侧边展示。"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            c.id,
            c.filename,
            c.filepath,
            c.summary,
            c.scene,
            c.subjects_json,
            c.actions_json,
            c.has_motion,
            c.sharpness_score,
            c.cover_path,
            c.status,
            c.error_message,
            c.transcript_status,
            c.transcript_error_message,
            c.preview_status,
            c.preview_path,
            c.preview_error_message,
            c.comparison_status,
            c.comparison_scores_json,
            c.comparison_error_message,
            GROUP_CONCAT(t.tag, '|||') AS tags_text
        FROM clips c
        LEFT JOIN clip_tags t ON t.clip_id = c.id
        WHERE c.id != ? AND c.scene = ?
        GROUP BY c.id
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ?
        """,
        (current_clip_id, scene, limit),
    ).fetchall()
    conn.close()

    cards: list[ClipCard] = []
    for row in rows:
        cards.append(
            ClipCard(
                id=int(row["id"]),
                filename=row["filename"],
                filepath=row["filepath"],
                summary=row["summary"] or "暂无摘要",
                scene=row["scene"] or "未识别场景",
                subjects=json.loads(row["subjects_json"] or "[]"),
                actions=json.loads(row["actions_json"] or "[]"),
                tags=(row["tags_text"] or "").split("|||") if row["tags_text"] else [],
                has_motion=bool(row["has_motion"]) if row["has_motion"] is not None else False,
                sharpness_score=row["sharpness_score"],
                cover_url=build_cover_url(row["cover_path"]),
                status=row["status"],
                visual_error_message=row["error_message"],
                transcript_status=row["transcript_status"] or "pending",
                transcript_error_message=row["transcript_error_message"],
                preview_status=row["preview_status"] or "pending",
                comparison_status=row["comparison_status"] or "pending",
                comparison_scores=json.loads(row["comparison_scores_json"]) if row["comparison_scores_json"] else None,
                comparison_error_message=row["comparison_error_message"],
            )
        )
    return cards


def load_adjacent_clip_ids(clip_id: int, library_id: int) -> tuple[int | None, int | None]:
    """按素材库当前排序查找上一条和下一条素材。"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id
        FROM clips
        WHERE library_id = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (library_id,),
    ).fetchall()
    conn.close()

    ordered_ids = [int(row["id"]) for row in rows]
    try:
        current_index = ordered_ids.index(clip_id)
    except ValueError:
        return None, None

    previous_id = ordered_ids[current_index - 1] if current_index > 0 else None
    next_id = ordered_ids[current_index + 1] if current_index < len(ordered_ids) - 1 else None
    return previous_id, next_id


def build_stats(clips: list[ClipCard]) -> dict[str, int]:
    """计算卡片统计信息。"""
    return {
        "total": len(clips),
        "done": sum(1 for clip in clips if clip.status == "done"),
        "failed": sum(1 for clip in clips if clip.status == "failed"),
    }


def save_uploaded_files(files: list[UploadFile]) -> list[Path]:
    """把网页上传的视频保存到本地批次目录。"""
    valid_files = [file for file in files if file.filename]
    if not valid_files:
        raise HTTPException(status_code=400, detail="请先选择要上传的视频文件。")
    if len(valid_files) > 50:
        raise HTTPException(status_code=400, detail="一次最多上传 50 条视频。")

    batch_dir = UPLOADS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    for file in valid_files:
        filename = Path(file.filename).name
        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"不支持的文件类型：{filename}")

        target = batch_dir / filename
        with target.open("wb") as output:
            shutil.copyfileobj(file.file, output)
        saved_paths.append(target)

    return saved_paths


def resolve_upload_library(library_id_input: int | None, new_library_name: str) -> LibraryRecord:
    """根据上传页输入，决定这批视频要归属的素材库。"""
    cleaned_name = new_library_name.strip()
    if cleaned_name:
        try:
            return create_library(cleaned_name, DB_PATH)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except sqlite3.IntegrityError:
            conn = get_connection()
            row = conn.execute(
                "SELECT id, name FROM libraries WHERE name = ?",
                (cleaned_name,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=400, detail="素材库创建失败，请换一个名称再试。")
            return LibraryRecord(id=int(row["id"]), name=row["name"], clip_count=0)

    current_library_id = resolve_library_id(library_id_input)
    library = get_library_by_id(current_library_id, DB_PATH)
    if library is None:
        raise HTTPException(status_code=400, detail="选中的素材库不存在。")
    return library


def build_upload_page_context(
    upload_job: UploadJobState | None = None,
    error_message: str | None = None,
    selected_library_id: int | None = None,
    pending_library_name: str = "",
) -> dict[str, Any]:
    """构造上传页上下文。"""
    libraries = list_libraries(DB_PATH)
    current_library_id = resolve_library_id(selected_library_id)
    return {
        "upload_job": upload_job,
        "upload_error": error_message,
        "libraries": libraries,
        "selected_library_id": current_library_id,
        "pending_library_name": pending_library_name,
    }


def build_upload_summary(job: UploadJobState) -> dict[str, int]:
    """统计上传任务的完成情况。"""
    total = len(job.items)
    done = sum(1 for item in job.items if item.stage == "done")
    failed = sum(1 for item in job.items if item.stage == "failed")
    progress = int(sum(item.progress for item in job.items) / total) if total else 0
    completed_items = [
        item for item in job.items
        if item.finished_at is not None and item.started_at is not None and item.finished_at >= item.started_at
    ]
    elapsed_seconds = max(int(time.time() - job.started_at), 0)
    average_seconds = (
        sum((item.finished_at or 0) - (item.started_at or 0) for item in completed_items) / len(completed_items)
        if completed_items else None
    )
    remaining_items = total - done - failed
    eta_seconds = int(average_seconds * remaining_items) if average_seconds is not None and remaining_items > 0 else 0
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "progress": progress,
        "elapsed_seconds": elapsed_seconds,
        "eta_seconds": eta_seconds,
        "has_eta": average_seconds is not None and remaining_items > 0,
    }


def build_comparison_scores_for_clip(clip: ClipCard | ClipDetail) -> dict[str, float] | None:
    """从缓存字段中读取相似素材对比分。"""
    return clip.comparison_scores


def format_eta_text(total_seconds: int) -> str:
    """把秒数转成上传页里更易读的剩余时间文本。"""
    seconds = max(total_seconds, 0)
    minutes, remain_seconds = divmod(seconds, 60)
    hours, remain_minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours} 小时 {remain_minutes} 分"
    if minutes > 0:
        return f"{minutes} 分 {remain_seconds} 秒"
    return f"{remain_seconds} 秒"


def sanitize_export_filename(name: str) -> str:
    """把摘要转成适合导出文件名的文本。"""
    sanitized = re.sub(r'[\\/:*?"<>|]+', "_", name).strip().strip(".")
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized or "未命名素材"


def get_upload_job(job_id: str) -> UploadJobState | None:
    """读取当前上传任务。"""
    with UPLOAD_LOCK:
        return UPLOAD_JOBS.get(job_id)


def update_upload_item(
    job_id: str,
    filepath: Path,
    *,
    stage: str,
    detail: str,
    error_message: str | None = None,
) -> None:
    """更新单条视频的上传处理状态。"""
    with UPLOAD_LOCK:
        job = UPLOAD_JOBS.get(job_id)
        if job is None:
            return
        for item in job.items:
            if item.filepath == str(filepath):
                if item.started_at is None and stage not in {"queued", "saved"}:
                    item.started_at = time.time()
                item.stage = stage
                item.progress = STAGE_META[stage]["progress"]
                item.detail = detail
                item.error_message = error_message
                if stage in {"done", "failed"}:
                    item.finished_at = time.time()
                break

        summary = build_upload_summary(job)
        if summary["done"] + summary["failed"] == summary["total"]:
            job.status = "completed"
            job.message = f"已完成 {summary['done']} 条，失败 {summary['failed']} 条"
        elif stage == "failed":
            job.status = "processing"
            job.message = "部分视频处理失败，其他任务继续进行"
        else:
            job.status = "processing"
            job.message = f"正在处理 {summary['done'] + summary['failed']} / {summary['total']} 条视频"


def start_upload_job(saved_paths: list[Path], library: LibraryRecord) -> UploadJobState:
    """创建后台处理任务。"""
    job_id = uuid.uuid4().hex[:12]
    batch_dir = saved_paths[0].parent if saved_paths else UPLOADS_DIR
    job = UploadJobState(
        job_id=job_id,
        batch_name=batch_dir.name,
        batch_dir=str(batch_dir),
        library_id=library.id,
        library_name=library.name,
        items=[
            UploadItemState(
                filename=path.name,
                filepath=str(path),
                stage="saved",
                progress=STAGE_META["saved"]["progress"],
                detail="已保存到本地工作目录",
            )
            for path in saved_paths
        ],
        status="processing",
        message=f"文件上传完成，准备写入素材库「{library.name}」",
        redirect_url=f"/?library_id={library.id}",
    )

    with UPLOAD_LOCK:
        UPLOAD_JOBS[job_id] = job

    thread = threading.Thread(
        target=process_upload_job,
        args=(job_id, saved_paths, library.id),
        daemon=True,
    )
    thread.start()
    return job


def update_clip_transcript_state(
    clip: ClipDetail,
    *,
    transcript_status: str,
    transcript_error_message: str | None,
) -> None:
    """只更新 transcript 相关状态，避免覆盖视觉分析结果。"""
    conn = get_connection()
    conn.execute(
        """
        UPDATE clips
        SET transcript_status = ?, transcript_error_message = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (transcript_status, transcript_error_message, clip.id),
    )
    conn.commit()
    conn.close()


def update_clip_asset_state(
    clip_id: int,
    *,
    preview_status: str | None = None,
    preview_path: Path | None = None,
    preview_error_message: str | None | object = None,
    comparison_status: str | None = None,
    comparison_scores: dict[str, float] | None = None,
    comparison_error_message: str | None | object = None,
) -> None:
    """更新预览和对比分缓存状态。"""
    conn = get_connection()
    updates: list[str] = []
    params: list[Any] = []

    if preview_status is not None:
        updates.append("preview_status = ?")
        params.append(preview_status)
    if preview_path is not None:
        updates.append("preview_path = ?")
        params.append(str(preview_path))
    if preview_error_message is not None:
        updates.append("preview_error_message = ?")
        params.append(preview_error_message)
    if comparison_status is not None:
        updates.append("comparison_status = ?")
        params.append(comparison_status)
    if comparison_scores is not None:
        updates.append("comparison_scores_json = ?")
        params.append(json.dumps(comparison_scores, ensure_ascii=False))
    if comparison_error_message is not None:
        updates.append("comparison_error_message = ?")
        params.append(comparison_error_message)

    if not updates:
        conn.close()
        return

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(clip_id)
    conn.execute(
        f"UPDATE clips SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    conn.commit()
    conn.close()


def start_clip_asset_job(clip_id: int) -> None:
    """启动单条素材的后台补全任务。"""
    with ASSET_LOCK:
        if clip_id in ASSET_JOBS:
            return
        ASSET_JOBS[clip_id] = "processing"

    update_clip_asset_state(
        clip_id,
        preview_status="processing",
        preview_error_message="",
        comparison_status="processing",
        comparison_error_message="",
    )
    thread = threading.Thread(target=run_clip_asset_job, args=(clip_id,), daemon=True)
    thread.start()


def run_clip_asset_job(clip_id: int) -> None:
    """生成预览视频并计算对比分缓存。"""
    try:
        clip = load_clip_detail(clip_id)
        if clip is None:
            return

        video_path = Path(clip.filepath)
        if not video_path.exists():
            update_clip_asset_state(
                clip_id,
                preview_status="failed",
                preview_error_message="原始视频不存在，无法生成预览。",
                comparison_status="failed",
                comparison_error_message="原始视频不存在，无法生成对比分。",
            )
            return

        try:
            preview_path = ensure_preview_video(video_path)
            update_clip_asset_state(
                clip_id,
                preview_status="done",
                preview_path=preview_path,
                preview_error_message="",
            )
        except Exception as exc:
            update_clip_asset_state(
                clip_id,
                preview_status="failed",
                preview_error_message=f"预览生成失败：{exc}",
            )

        try:
            frame_dir = CACHE_DIR / build_video_hash(video_path)
            frame_paths = get_keyframe_paths(frame_dir)
            if not frame_paths:
                raise RuntimeError("没有可用于对比分析的关键帧")
            scores = build_comparison_scores(frame_paths)
            update_clip_asset_state(
                clip_id,
                comparison_status="done",
                comparison_scores={"sharpness_score": scores.sharpness_score},
                comparison_error_message="",
            )
        except Exception as exc:
            update_clip_asset_state(
                clip_id,
                comparison_status="failed",
                comparison_error_message=f"对比分生成失败：{exc}",
            )
    finally:
        with ASSET_LOCK:
            ASSET_JOBS.pop(clip_id, None)


def process_upload_job(job_id: str, video_paths: list[Path], library_id: int) -> None:
    """后台处理上传的视频。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for video in video_paths:
        video_hash = ""
        try:
            update_upload_item(job_id, video, stage="extracting", detail="正在抽取关键帧")
            video_hash, frame_dir = extract_keyframes(video, CACHE_DIR)
            frames = get_keyframe_paths(frame_dir)
            if not frames:
                raise RuntimeError("没有成功抽取关键帧")

            cover_path = select_cover_frame(frames, frame_dir / "cover.jpg")

            transcript_segments: list[TranscriptSegment] = []
            transcript_status = "pending"
            transcript_error_message: str | None = None
            try:
                transcript_segments = transcribe_video(video, frame_dir)
                transcript_status = "done" if transcript_segments else "empty"
            except ASREmptyResult:
                transcript_segments = []
                transcript_status = "empty"
            except ASRError as exc:
                # 口播识别失败不影响主流程，只是不参与摘要增强。
                transcript_segments = []
                transcript_status = "failed"
                transcript_error_message = str(exc)

            update_upload_item(job_id, video, stage="analyzing", detail="正在调用 AI 分析视频")
            analysis = analyze_video(frames, build_transcript_context(transcript_segments))
            clip_id = save_clip(
                ClipRecord(
                    video_hash=video_hash,
                    filename=video.name,
                    filepath=video,
                    library_id=library_id,
                    summary=analysis.summary,
                    scene=analysis.scene,
                    subjects=analysis.subjects,
                    actions=analysis.actions,
                    tags=analysis.tags,
                    has_motion=analysis.has_motion,
                    sharpness_score=None,
                    cover_path=cover_path,
                    status="done",
                    transcript_status=transcript_status,
                    transcript_error_message=transcript_error_message,
                    preview_status="pending",
                    comparison_status="pending",
                ),
                DB_PATH,
            )

            if transcript_segments:
                save_transcripts(
                    [
                        TranscriptRecord(
                            clip_id=clip_id,
                            start_ms=segment.start_ms,
                            end_ms=segment.end_ms,
                            text=segment.text,
                            segment_index=segment.segment_index,
                        )
                        for segment in transcript_segments
                    ],
                    DB_PATH,
                )

            start_clip_asset_job(clip_id)
            update_upload_item(job_id, video, stage="done", detail="已完成分析并写入素材库")
        except Exception as exc:
            error_text = str(exc)
            if "InvalidEndpointOrModel.NotFound" in error_text:
                error_text = (
                    "当前 ARK_MODEL 不可用或你没有访问权限。"
                    "请把 .env 里的 ARK_MODEL 改成你账号下可访问的模型名或 Endpoint ID。"
                )
            try:
                save_clip(
                    ClipRecord(
                        video_hash=video_hash or video.name,
                        filename=video.name,
                        filepath=video,
                        library_id=library_id,
                        status="failed",
                        error_message=error_text,
                        transcript_status="pending",
                    ),
                    DB_PATH,
                )
            except Exception as save_exc:
                exc = RuntimeError(f"{exc}；写入失败记录时又出错：{save_exc}")
                error_text = str(exc)
            update_upload_item(job_id, video, stage="failed", detail="处理失败", error_message=error_text)


def run_clip_transcript_job(clip_id: int) -> None:
    """单独执行一条素材的 transcript 识别。"""
    try:
        clip = load_clip_detail(clip_id)
        if clip is None:
            return
        if not is_asr_configured():
            update_clip_transcript_state(
                clip,
                transcript_status="unavailable",
                transcript_error_message=None,
            )
            return

        video_path = Path(clip.filepath)
        if not video_path.exists():
            update_clip_transcript_state(
                clip,
                transcript_status="failed",
                transcript_error_message="原始视频不存在，无法识别口播。",
            )
            return

        frame_dir = CACHE_DIR / build_video_hash(video_path)
        frame_dir.mkdir(parents=True, exist_ok=True)
        segments = transcribe_video(video_path, frame_dir)

        if segments:
            save_transcripts(
                [
                    TranscriptRecord(
                        clip_id=clip.id,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        text=segment.text,
                        segment_index=segment.segment_index,
                    )
                    for segment in segments
                ],
                DB_PATH,
            )
            update_clip_transcript_state(
                clip,
                transcript_status="done",
                transcript_error_message=None,
            )
        else:
            update_clip_transcript_state(
                clip,
                transcript_status="empty",
                transcript_error_message=None,
            )
    except ASREmptyResult:
        clip = load_clip_detail(clip_id)
        if clip is not None:
            update_clip_transcript_state(
                clip,
                transcript_status="empty",
                transcript_error_message=None,
            )
    except ASRError as exc:
        clip = load_clip_detail(clip_id)
        if clip is not None:
            update_clip_transcript_state(
                clip,
                transcript_status="failed",
                transcript_error_message=str(exc),
            )
    finally:
        with TRANSCRIPT_LOCK:
            TRANSCRIPT_JOBS.pop(clip_id, None)


def export_clips_by_ids(clip_ids: list[int], destination_input: str = "") -> dict[str, Any]:
    """把选中的原始视频复制到指定目录。"""
    destination_dir = Path(destination_input).expanduser() if destination_input.strip() else PICKS_DIR
    if not clip_ids:
        return {
            "copied": 0,
            "files": [],
            "skipped": [],
            "destination": str(destination_dir),
            "message": "请先选择要导出的素材。",
        }

    conn = get_connection()
    placeholders = ",".join("?" for _ in clip_ids)
    rows = conn.execute(
        f"""
        SELECT id, filename, filepath, summary
        FROM clips
        WHERE id IN ({placeholders})
        ORDER BY id ASC
        """,
        clip_ids,
    ).fetchall()

    destination_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    skipped: list[str] = []
    for row in rows:
        source = Path(row["filepath"])
        if not source.exists():
            skipped.append(f"{row['filename']}（源文件不存在）")
            continue

        base_name = sanitize_export_filename((row["summary"] or "").strip() or source.stem)
        destination = destination_dir / f"{base_name}{source.suffix}"
        sequence = 2
        while destination.exists():
            destination = destination_dir / f"{base_name}-{sequence}{source.suffix}"
            sequence += 1

        shutil.copy2(source, destination)
        copied.append(destination.name)

    message = f"已导出 {len(copied)} 条素材" if copied else "没有导出新文件，请检查是否都已存在或源文件缺失。"
    return {
        "copied": len(copied),
        "files": copied,
        "skipped": skipped,
        "destination": str(destination_dir),
        "message": message,
    }


def parse_selected_ids(raw_selected_ids: list[str]) -> list[int]:
    """过滤表单里的空值，并把素材 ID 转成整数列表。"""
    selected_ids: list[int] = []
    for raw_value in raw_selected_ids:
        value = raw_value.strip()
        if not value:
            continue
        try:
            selected_ids.append(int(value))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"素材 ID 无效：{raw_value}") from exc
    return selected_ids


@app.get("/", include_in_schema=False)
def grid_page(
    request: Request,
    q: str = Query(default=""),
    tag: str = Query(default=""),
    library_id: Optional[int] = Query(default=None),
    node_id: Optional[int] = Query(default=None),
):
    """渲染素材网格页。"""
    current_library_id = resolve_library_id(library_id)
    current_node_id = resolve_node_id(current_library_id, node_id)
    clips = load_clips(
        query=q.strip(),
        tag=tag.strip(),
        library_id=current_library_id,
        node_id=current_node_id,
    )
    libraries = list_libraries(DB_PATH)
    current_library = get_library_by_id(current_library_id, DB_PATH)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "clips": clips,
            "stats": build_stats(clips),
            "all_tags": load_tags(current_library_id, current_node_id),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "libraries": libraries,
            "current_library": current_library,
            "current_library_name": current_library.name if current_library else "素材库",
            "current_library_id": current_library_id,
            "current_node_id": current_node_id,
            "project_tree": build_project_tree(current_library_id, current_node_id),
            "current_node": get_project_node(current_node_id, DB_PATH) if current_node_id is not None else None,
            "recycle_items": list_recycled_clips(current_library_id, DB_PATH),
            "export_result": None,
        },
    )


@app.get("/upload", include_in_schema=False)
def upload_page(request: Request, library_id: Optional[int] = Query(default=None)):
    """渲染独立上传页。"""
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context=build_upload_page_context(selected_library_id=library_id),
    )


def build_clip_detail_context(clip_id: int, current_library_id: int, edit_error: str | None = None) -> dict[str, Any]:
    """构造详情页模板上下文。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    previous_clip_id, next_clip_id = load_adjacent_clip_ids(clip_id, current_library_id)
    return {
        "clip": clip,
        "similar_clips": load_similar_clips(clip.id, clip.scene),
        "return_url": f"/?library_id={current_library_id}",
        "current_library_id": current_library_id,
        "edit_error": edit_error,
        "previous_clip_id": previous_clip_id,
        "next_clip_id": next_clip_id,
    }


@app.get("/clips/{clip_id}", include_in_schema=False)
def clip_detail_page(request: Request, clip_id: int, library_id: Optional[int] = Query(default=None)):
    """渲染单条素材详情页。"""
    current_library_id = resolve_library_id(library_id)
    return templates.TemplateResponse(
        request=request,
        name="clip_detail.html",
        context=build_clip_detail_context(clip_id, current_library_id),
    )


@app.get("/compare", include_in_schema=False)
def compare_clips_page(
    request: Request,
    ids: str = Query(default=""),
    library_id: Optional[int] = Query(default=None),
):
    """渲染相似素材对比页。"""
    clip_ids = [int(item) for item in ids.split(",") if item.strip().isdigit()]
    if len(clip_ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择 2 条素材才能对比。")
    if len(clip_ids) > 6:
        raise HTTPException(status_code=400, detail="一次最多对比 6 条素材。")

    current_library_id = resolve_library_id(library_id)
    payloads = load_compare_payloads(clip_ids)
    if len(payloads) < 2:
        raise HTTPException(status_code=400, detail="没有足够素材可用于对比。")
    for item in payloads:
        item["recommendation_reason"] = build_recommendation_reason(item, payloads)
    return templates.TemplateResponse(
        request=request,
        name="compare.html",
        context={
            "compare_items": payloads,
            "recommended_item": payloads[0],
            "current_library_id": current_library_id,
            "return_url": f"/?library_id={current_library_id}",
        },
    )


@app.post("/compare/selection", include_in_schema=False)
def compare_clips_from_selection(
    selected_ids: list[str] = Form(default_factory=list),
    library_id: Optional[int] = Form(default=None),
):
    """从素材库多选进入对比页。"""
    current_library_id = resolve_library_id(library_id)
    clip_ids = parse_selected_ids(selected_ids)[:6]
    if len(clip_ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择 2 条素材才能对比。")
    joined_ids = ",".join(str(item) for item in clip_ids)
    return RedirectResponse(url=f"/compare?library_id={current_library_id}&ids={joined_ids}", status_code=303)


@app.post("/compare/keep", include_in_schema=False)
def keep_recommended_clip(
    keep_clip_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
    library_id: int = Form(...),
):
    """保留推荐素材，并把其余素材移入回收站。"""
    delete_targets = [clip_id for clip_id in parse_selected_ids(selected_ids) if clip_id != keep_clip_id]
    if delete_targets:
        delete_clips(delete_targets, library_id, DB_PATH)
    return RedirectResponse(url=f"/clips/{keep_clip_id}?library_id={library_id}", status_code=303)


@app.post("/clips/{clip_id}/summary", include_in_schema=False)
def update_clip_summary_action(
    request: Request,
    clip_id: int,
    summary: str = Form(...),
    library_id: Optional[int] = Form(default=None),
):
    """更新单条素材的摘要。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    try:
        update_clip_summary(
            clip_id=clip.id,
            library_id=clip.library_id,
            summary=summary,
            db_path=DB_PATH,
        )
    except ValueError as exc:
        current_library_id = resolve_library_id(library_id)
        return templates.TemplateResponse(
            request=request,
            name="clip_detail.html",
            context=build_clip_detail_context(clip_id, current_library_id, str(exc)),
            status_code=400,
        )

    current_library_id = resolve_library_id(library_id)
    return RedirectResponse(url=f"/clips/{clip_id}?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/card-summary", include_in_schema=False)
def update_clip_card_summary_action(
    request: Request,
    clip_id: int,
    summary: str = Form(...),
    library_id: Optional[int] = Form(default=None),
):
    """更新素材库卡片里的摘要名称，并返回局部卡片 HTML。"""
    clip = load_clip_card(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    current_library_id = resolve_library_id(library_id)
    card_error: str | None = None
    edit_open = False
    try:
        update_clip_summary(
            clip_id=clip.id,
            library_id=current_library_id,
            summary=summary,
            db_path=DB_PATH,
        )
        clip = load_clip_card(clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="素材不存在。")
    except ValueError as exc:
        card_error = str(exc)
        edit_open = True

    return templates.TemplateResponse(
        request=request,
        name="partials/clip_card.html",
        context={
            "clip": clip,
            "current_library_id": current_library_id,
            "current_library_name": get_library_by_id(current_library_id, DB_PATH).name if get_library_by_id(current_library_id, DB_PATH) else "素材库",
            "project_tree": build_project_tree(current_library_id, resolve_node_id(current_library_id)),
            "card_error": card_error,
            "card_edit_open": edit_open,
        },
    )


@app.post("/clips/{clip_id}/move", include_in_schema=False)
def move_clip_to_folder_action(
    clip_id: int,
    library_id: int = Form(...),
    target_node_id: int = Form(...),
    return_node_id: Optional[int] = Form(default=None),
):
    """把单条素材移动到指定文件夹，或移回未分类。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    current_library_id = clip.library_id
    current_uncategorized_id = build_uncategorized_node_id(current_library_id)
    move_clip_to_node(
        clip_id=clip_id,
        library_id=current_library_id,
        target_node_id=None if target_node_id == current_uncategorized_id else target_node_id,
        db_path=DB_PATH,
    )
    target_url = f"/?library_id={current_library_id}"
    if return_node_id is not None:
        target_url += f"&node_id={return_node_id}"
    return RedirectResponse(url=target_url, status_code=303)


@app.post("/clips/{clip_id}/tags", include_in_schema=False)
def append_clip_tags_action(
    request: Request,
    clip_id: int,
    new_tags: str = Form(default=""),
    library_id: Optional[int] = Form(default=None),
):
    """为单条素材追加标签。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    tag_candidates = re.split(r"[\n,，]+", new_tags)
    try:
        append_clip_tags(clip_id=clip.id, new_tags=tag_candidates, db_path=DB_PATH)
    except ValueError as exc:
        current_library_id = resolve_library_id(library_id)
        return templates.TemplateResponse(
            request=request,
            name="clip_detail.html",
            context=build_clip_detail_context(clip_id, current_library_id, str(exc)),
            status_code=400,
        )

    current_library_id = resolve_library_id(library_id)
    return RedirectResponse(url=f"/clips/{clip_id}?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/note", include_in_schema=False)
def update_clip_note_action(
    clip_id: int,
    note: str = Form(default=""),
    library_id: Optional[int] = Form(default=None),
):
    """更新单条素材的用户批注。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    update_clip_note(clip_id=clip.id, note=note, db_path=DB_PATH)

    current_library_id = resolve_library_id(library_id)
    return RedirectResponse(url=f"/clips/{clip_id}?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/delete", include_in_schema=False)
def delete_single_clip(
    clip_id: int,
    library_id: Optional[int] = Form(default=None),
):
    """删除当前素材，并跳转到相邻详情页或素材库。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    current_library_id = resolve_library_id(library_id or clip.library_id)
    previous_clip_id, next_clip_id = load_adjacent_clip_ids(clip_id, current_library_id)
    delete_clips([clip_id], current_library_id, DB_PATH)

    target_clip_id = next_clip_id or previous_clip_id
    if target_clip_id is not None:
        return RedirectResponse(
            url=f"/clips/{target_clip_id}?library_id={current_library_id}",
            status_code=303,
        )
    return RedirectResponse(url=f"/?library_id={current_library_id}", status_code=303)


@app.post("/projects/nodes", include_in_schema=False)
def create_project_node_action(
    library_id: int = Form(...),
    name: str = Form(...),
    parent_id: Optional[int] = Form(default=None),
):
    """在项目内创建文件夹。"""
    create_project_node(library_id=library_id, name=name, parent_id=parent_id, db_path=DB_PATH)
    target_url = f"/?library_id={library_id}"
    if parent_id is not None:
        target_url += f"&node_id={parent_id}"
    return RedirectResponse(url=target_url, status_code=303)


@app.post("/projects/nodes/{node_id}/rename", include_in_schema=False)
def rename_project_node_action(
    node_id: int,
    library_id: int = Form(...),
    new_name: str = Form(...),
):
    """重命名项目节点。"""
    rename_project_node(node_id=node_id, library_id=library_id, new_name=new_name, db_path=DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}&node_id={node_id}", status_code=303)


@app.post("/projects/nodes/{node_id}/move", include_in_schema=False)
def move_project_node_action(
    node_id: int,
    library_id: int = Form(...),
    target_parent_id: Optional[int] = Form(default=None),
):
    """移动项目节点。"""
    move_project_node(node_id=node_id, library_id=library_id, target_parent_id=target_parent_id, db_path=DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}&node_id={node_id}", status_code=303)


@app.post("/projects/nodes/{node_id}/delete", include_in_schema=False)
def delete_project_node_action(
    node_id: int,
    library_id: int = Form(...),
):
    """删除项目节点。"""
    node = get_project_node(node_id, DB_PATH)
    if node is None:
        raise HTTPException(status_code=404, detail="文件夹不存在。")
    fallback_node_id = node.parent_id
    delete_project_node(node_id=node_id, library_id=library_id, db_path=DB_PATH)
    target_url = f"/?library_id={library_id}"
    if fallback_node_id is not None:
        target_url += f"&node_id={fallback_node_id}"
    return RedirectResponse(url=target_url, status_code=303)


@app.post("/projects/nodes/attach", include_in_schema=False)
def attach_clips_to_node_action(
    library_id: int = Form(...),
    target_node_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
):
    """把选中的素材引用到指定项目节点。"""
    for clip_id in parse_selected_ids(selected_ids):
        attach_clip_to_node(clip_id=clip_id, node_id=target_node_id, db_path=DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}&node_id={target_node_id}", status_code=303)


@app.get("/recycle", include_in_schema=False)
def recycle_page(
    request: Request,
    library_id: Optional[int] = Query(default=None),
):
    """渲染项目回收站页面。"""
    current_library_id = resolve_library_id(library_id)
    current_library = get_library_by_id(current_library_id, DB_PATH)
    return templates.TemplateResponse(
        request=request,
        name="recycle.html",
        context={
            "current_library_id": current_library_id,
            "current_library": current_library,
            "recycle_items": list_recycled_clips(current_library_id, DB_PATH),
            "return_url": f"/?library_id={current_library_id}",
        },
    )


@app.post("/recycle/restore", include_in_schema=False)
def restore_recycled_clips_action(
    library_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
):
    """从项目回收站恢复素材。"""
    restore_recycled_clips(library_id, parse_selected_ids(selected_ids), DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}", status_code=303)


@app.get("/clips/{clip_id}/transcript", include_in_schema=False)
def clip_transcript_partial(request: Request, clip_id: int):
    """返回详情页 transcript 局部 HTML。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    return templates.TemplateResponse(
        request=request,
        name="partials/clip_transcript.html",
        context=build_transcript_panel_context(clip),
    )


@app.post("/clips/{clip_id}/transcript/run", include_in_schema=False)
def run_clip_transcript(request: Request, clip_id: int):
    """手动触发单条素材的口播识别。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    if clip.transcripts:
        return templates.TemplateResponse(
            request=request,
            name="partials/clip_transcript.html",
            context=build_transcript_panel_context(clip),
        )

    if not is_asr_configured():
        update_clip_transcript_state(
            clip,
            transcript_status="unavailable",
            transcript_error_message=None,
        )
        refreshed_clip = load_clip_detail(clip_id)
        return templates.TemplateResponse(
            request=request,
            name="partials/clip_transcript.html",
            context=build_transcript_panel_context(refreshed_clip),
        )

    with TRANSCRIPT_LOCK:
        is_running = clip_id in TRANSCRIPT_JOBS
        if not is_running:
            TRANSCRIPT_JOBS[clip_id] = "processing"

    if not is_running:
        update_clip_transcript_state(
            clip,
            transcript_status="processing",
            transcript_error_message=None,
        )
        thread = threading.Thread(target=run_clip_transcript_job, args=(clip_id,), daemon=True)
        thread.start()

    refreshed_clip = load_clip_detail(clip_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/clip_transcript.html",
        context=build_transcript_panel_context(refreshed_clip),
    )


@app.get("/media/{clip_id}", include_in_schema=False)
def clip_media(clip_id: int):
    """按素材 ID 返回原始视频文件，供详情页播放。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    file_path = Path(clip.filepath)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="原始视频不存在。")

    media_type = clip.media_type or "video/mp4"
    return FileResponse(path=file_path, media_type=media_type, filename=clip.filename)


@app.get("/clips", include_in_schema=False)
def clips_partial(
    request: Request,
    q: str = Query(default=""),
    tag: str = Query(default=""),
    library_id: Optional[int] = Query(default=None),
    node_id: Optional[int] = Query(default=None),
):
    """返回素材网格局部 HTML，供 HTMX 刷新。"""
    current_library_id = resolve_library_id(library_id)
    current_node_id = resolve_node_id(current_library_id, node_id)
    clips = load_clips(query=q.strip(), tag=tag.strip(), library_id=current_library_id, node_id=current_node_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/clip_grid.html",
        context={
            "clips": clips,
            "stats": build_stats(clips),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "current_library_name": get_library_by_id(current_library_id, DB_PATH).name if get_library_by_id(current_library_id, DB_PATH) else "素材库",
            "current_library_id": current_library_id,
            "current_node_id": current_node_id,
            "project_tree": build_project_tree(current_library_id, current_node_id),
        },
    )


@app.post("/upload/jobs", include_in_schema=False)
def create_upload_job(
    request: Request,
    files: list[UploadFile] = File(...),
    library_id: Optional[int] = Form(default=None),
    new_library_name: str = Form(default=""),
):
    """接收本地上传的视频并启动后台处理。"""
    try:
        library = resolve_upload_library(library_id, new_library_name)
        saved_paths = save_uploaded_files(files)
        upload_job = start_upload_job(saved_paths, library)
        return templates.TemplateResponse(
            request=request,
            name="partials/upload_job.html",
            context={
                "upload_job": upload_job,
                "upload_summary": build_upload_summary(upload_job),
                "format_eta_text": format_eta_text,
            },
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            request=request,
            name="partials/upload_panel.html",
            context=build_upload_page_context(
                error_message=exc.detail,
                selected_library_id=library_id,
                pending_library_name=new_library_name,
            ),
        )


@app.get("/upload/jobs/{job_id}", include_in_schema=False)
def upload_job_partial(request: Request, job_id: str):
    """返回上传任务的实时状态。"""
    upload_job = get_upload_job(job_id)
    if upload_job is None:
        raise HTTPException(status_code=404, detail="上传任务不存在。")

    return templates.TemplateResponse(
        request=request,
        name="partials/upload_job.html",
        context={
            "upload_job": upload_job,
            "upload_summary": build_upload_summary(upload_job),
            "format_eta_text": format_eta_text,
        },
    )


@app.post("/export/clips", include_in_schema=False)
def export_selected_clips(
    request: Request,
    selected_ids: list[str] = Form(default_factory=list),
    export_dir: str = Form(default=""),
    q: str = Form(default=""),
    tag: str = Form(default=""),
    library_id: Optional[int] = Form(default=None),
    node_id: Optional[int] = Form(default=None),
):
    """导出选中的视频到 data/picks/。"""
    export_result = export_clips_by_ids(parse_selected_ids(selected_ids), destination_input=export_dir)
    current_library_id = resolve_library_id(library_id)
    current_node_id = resolve_node_id(current_library_id, node_id)
    clips = load_clips(query=q.strip(), tag=tag.strip(), library_id=current_library_id, node_id=current_node_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/export_panel.html",
        context={
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "current_library_id": current_library_id,
            "current_node_id": current_node_id,
            "stats": build_stats(clips),
            "export_result": export_result,
            "current_export_dir": export_dir.strip(),
        },
    )


@app.post("/libraries/{library_id}/rename", include_in_schema=False)
def rename_library_action(
    library_id: int,
    new_name: str = Form(...),
):
    """重命名素材库。"""
    try:
        rename_library(library_id, new_name, DB_PATH)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/?library_id={library_id}", status_code=303)


@app.post("/libraries/{library_id}/delete", include_in_schema=False)
def delete_library_action(library_id: int):
    """删除空素材库。"""
    try:
        delete_library(library_id, DB_PATH)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


@app.post("/clips/delete", include_in_schema=False)
def delete_selected_clips(
    library_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
    node_id: Optional[int] = Form(default=None),
):
    """批量删除当前素材库中的素材。"""
    delete_clips(parse_selected_ids(selected_ids), library_id, DB_PATH)
    target_url = f"/?library_id={library_id}"
    if node_id is not None:
        target_url += f"&node_id={node_id}"
    return RedirectResponse(url=target_url, status_code=303)
