# FastAPI 服务器：提供上传流程、素材网格与导出页面

from __future__ import annotations

import json
import mimetypes
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
    TranscriptRecord,
    create_library,
    delete_clips,
    delete_library,
    get_library_by_id,
    init_db,
    list_libraries,
    load_transcripts,
    rename_library,
    save_clip,
    save_transcripts,
)
from metrics import build_comparison_scores, select_cover_frame
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
    status: str
    visual_error_message: str | None
    file_size_text: str
    shot_time_text: str
    transcripts: list[dict[str, Any]]
    transcript_available: bool
    transcript_status: str
    transcript_error_message: str | None
    comparison_scores: dict[str, float] | None = None


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


def load_clips(
    query: str = "",
    tag: str = "",
    include_failed: bool = False,
    library_id: int | None = None,
) -> list[ClipCard]:
    """从 SQLite 读取素材卡片数据，并支持关键词与标签过滤。"""
    conn = get_connection()
    filters: list[str] = []
    params: list[Any] = []
    current_library_id = resolve_library_id(library_id)

    filters.append("c.library_id = ?")
    params.append(current_library_id)

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
            GROUP_CONCAT(t.tag, '|||') AS tags_text
        FROM clips c
        LEFT JOIN clip_tags t ON t.clip_id = c.id
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
            )
        )
    return cards


def load_tags(library_id: int | None = None) -> list[tuple[str, int]]:
    """读取所有标签及其数量。"""
    conn = get_connection()
    current_library_id = resolve_library_id(library_id)
    rows = conn.execute(
        """
        SELECT tag, COUNT(*) AS count
        FROM clip_tags
        WHERE clip_id IN (
            SELECT id FROM clips
            WHERE library_id = ? AND status = 'done'
        )
        GROUP BY tag
        ORDER BY count DESC, tag ASC
        """,
        (current_library_id,),
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
    if file_path.exists():
        stat = file_path.stat()
        file_size_text = format_file_size(stat.st_size)
        shot_time_text = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        try:
            preview_path = ensure_preview_video(file_path)
            media_url = build_data_url_from_path(str(preview_path))
            media_type = "video/mp4"
        except Exception as exc:
            media_error_message = f"预览视频生成失败：{exc}"

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
        status=row["status"],
        visual_error_message=row["error_message"],
        file_size_text=file_size_text,
        shot_time_text=shot_time_text,
        transcripts=transcripts,
        transcript_available=is_asr_configured(),
        transcript_status=transcript_status,
        transcript_error_message=row["transcript_error_message"],
        comparison_scores=build_comparison_scores_for_clip(
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
                cover_url=build_cover_url(cover_path),
                status=row["status"],
                visual_error_message=row["error_message"],
                transcript_status=transcript_status,
                transcript_error_message=row["transcript_error_message"],
            )
        ),
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
            )
        )
        cards[-1].comparison_scores = build_comparison_scores_for_clip(cards[-1])
    return cards


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
    """按需计算相似素材对比分数。"""
    video_path = Path(clip.filepath)
    if not video_path.exists():
        return None

    frame_dir = CACHE_DIR / build_video_hash(video_path)
    frame_paths = get_keyframe_paths(frame_dir)
    if not frame_paths:
        return None

    scores = build_comparison_scores(frame_paths)
    return {
        "sharpness_score": scores.sharpness_score,
    }


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
        SELECT id, filename, filepath
        FROM clips
        WHERE id IN ({placeholders})
        ORDER BY id ASC
        """,
        clip_ids,
    ).fetchall()
    conn.close()

    destination_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    skipped: list[str] = []
    for row in rows:
        source = Path(row["filepath"])
        if not source.exists():
            skipped.append(f"{row['filename']}（源文件不存在）")
            continue

        destination = destination_dir / source.name
        if destination.exists():
            skipped.append(f"{row['filename']}（已存在，跳过）")
            continue

        shutil.copy2(source, destination)
        copied.append(row["filename"])

    message = f"已导出 {len(copied)} 条素材" if copied else "没有导出新文件，请检查是否都已存在或源文件缺失。"
    return {
        "copied": len(copied),
        "files": copied,
        "skipped": skipped,
        "destination": str(destination_dir),
        "message": message,
    }


@app.get("/", include_in_schema=False)
def grid_page(
    request: Request,
    q: str = Query(default=""),
    tag: str = Query(default=""),
    library_id: Optional[int] = Query(default=None),
):
    """渲染素材网格页。"""
    current_library_id = resolve_library_id(library_id)
    clips = load_clips(query=q.strip(), tag=tag.strip(), library_id=current_library_id)
    libraries = list_libraries(DB_PATH)
    current_library = get_library_by_id(current_library_id, DB_PATH)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "clips": clips,
            "stats": build_stats(clips),
            "all_tags": load_tags(current_library_id),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "libraries": libraries,
            "current_library": current_library,
            "current_library_id": current_library_id,
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


@app.get("/clips/{clip_id}", include_in_schema=False)
def clip_detail_page(request: Request, clip_id: int, library_id: Optional[int] = Query(default=None)):
    """渲染单条素材详情页。"""
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    current_library_id = resolve_library_id(library_id)

    return templates.TemplateResponse(
        request=request,
        name="clip_detail.html",
        context={
            "clip": clip,
            "similar_clips": load_similar_clips(clip.id, clip.scene),
            "return_url": f"/?library_id={current_library_id}",
            "current_library_id": current_library_id,
        },
    )


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
):
    """返回素材网格局部 HTML，供 HTMX 刷新。"""
    current_library_id = resolve_library_id(library_id)
    clips = load_clips(query=q.strip(), tag=tag.strip(), library_id=current_library_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/clip_grid.html",
        context={
            "clips": clips,
            "stats": build_stats(clips),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "current_library_id": current_library_id,
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
    selected_ids: list[int] = Form(default_factory=list),
    export_dir: str = Form(default=""),
    q: str = Form(default=""),
    tag: str = Form(default=""),
    library_id: Optional[int] = Form(default=None),
):
    """导出选中的视频到 data/picks/。"""
    export_result = export_clips_by_ids(selected_ids, destination_input=export_dir)
    current_library_id = resolve_library_id(library_id)
    clips = load_clips(query=q.strip(), tag=tag.strip(), library_id=current_library_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/export_panel.html",
        context={
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "current_library_id": current_library_id,
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
    selected_ids: list[int] = Form(default_factory=list),
):
    """批量删除当前素材库中的素材。"""
    delete_clips(selected_ids, library_id, DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}", status_code=303)
