# FastAPI 服务器：提供上传流程、素材网格与导出页面

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import ffmpeg
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai import analyze_video
from asr import ASREmptyResult, ASRError, is_asr_configured, transcribe_video
from db import (
    ClipCutSegmentRecord,
    ClipRecord,
    LibraryRecord,
    ProjectNodeRecord,
    RecycleClipRecord,
    TranscriptRecord,
    UserRecord,
    append_clip_tags,
    authenticate_user,
    attach_clip_to_node,
    bind_user_phone,
    change_user_password,
    create_session,
    create_user,
    count_uncategorized_clips as db_count_uncategorized_clips,
    create_clip_cut_segment,
    create_library,
    create_project_node,
    delete_session,
    delete_clip_cut_segment,
    delete_clips,
    delete_library,
    delete_project_node,
    get_admin_dashboard_stats,
    get_clip_cut_segment,
    get_library_by_id,
    get_library_by_name,
    get_project_node,
    get_user_by_id,
    get_user_by_session_token,
    issue_phone_verification_code,
    init_db,
    list_libraries,
    list_clip_cut_segments,
    list_project_nodes,
    list_recycled_clips,
    list_users,
    load_transcripts,
    query_adjacent_clip_ids,
    query_clip_card,
    query_clip_detail,
    query_clips,
    query_export_clips,
    query_similar_clips,
    query_tag_counts,
    rename_library,
    restore_recycled_clips,
    reset_password_by_phone,
    revoke_user_sessions,
    rename_project_node,
    save_clip,
    save_transcripts,
    update_user_password,
    update_user_status,
    move_project_node,
    move_clip_to_node,
    update_clip_asset_state as db_update_clip_asset_state,
    update_clip_cut_segment_export_path,
    update_clip_cut_segment_range,
    update_clip_note,
    update_clip_review,
    update_clip_transcript_state as db_update_clip_transcript_state,
    update_clip_summary,
)
from metrics import build_comparison_ranking, build_comparison_scores, select_cover_frame
from pipeline import VIDEO_EXTENSIONS, build_video_hash, extract_keyframes, get_keyframe_paths


BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "reelsift.db"

# Swift 文件夹选择器：编译一次后缓存二进制，避免每次点击都重新编译
_PICKER_BIN  = BASE_DIR / "data" / ".pick_folder_bin"
_PICKER_LOCK = threading.Lock()
_PICKER_MODULE_CACHE = BASE_DIR / "data" / ".swift_module_cache"
_SWIFT_PICKER_SRC = """\
import AppKit
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.activate(ignoringOtherApps: true)
let panel = NSOpenPanel()
panel.canChooseFiles = false
panel.canChooseDirectories = true
panel.allowsMultipleSelection = false
panel.title = "选择导出目录"
panel.prompt = "选择此文件夹"
let resp = panel.runModal()
if resp == .OK, let url = panel.url { print(url.path, terminator: "") }
"""

def _ensure_picker_binary() -> tuple[Path | None, str | None]:
    """编译 Swift 文件夹选择器，编译成功后缓存二进制。"""
    if _PICKER_BIN.exists():
        return _PICKER_BIN, None
    with _PICKER_LOCK:
        if _PICKER_BIN.exists():
            return _PICKER_BIN, None
        _PICKER_BIN.parent.mkdir(parents=True, exist_ok=True)
        _PICKER_MODULE_CACHE.mkdir(parents=True, exist_ok=True)
        src = _PICKER_BIN.with_suffix(".swift")
        try:
            src.write_text(_SWIFT_PICKER_SRC)
            r = subprocess.run(
                [
                    "swiftc",
                    "-module-cache-path",
                    str(_PICKER_MODULE_CACHE),
                    str(src),
                    "-o",
                    str(_PICKER_BIN),
                ],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0 or not _PICKER_BIN.exists():
                return None, (r.stderr or r.stdout or "Swift 文件夹选择器编译失败").strip()
            return _PICKER_BIN, None
        except Exception as exc:
            return None, str(exc)
        finally:
            src.unlink(missing_ok=True)
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
PICKS_DIR = BASE_DIR / "data" / "picks"
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
CACHE_DIR = BASE_DIR / "data" / "thumbnails"
PREVIEWS_DIR = BASE_DIR / "data" / "previews"
SESSION_COOKIE_NAME = "reelsift_session"

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
    is_favorite: bool = False
    rating: int = 0
    search_score: float = 0.0


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

# 服务启动时在后台预编译 Swift 文件夹选择器，第一次点击"浏览"时就不需要等待编译
if sys.platform == "darwin":
    threading.Thread(target=_ensure_picker_binary, daemon=True).start()

UPLOAD_JOBS: dict[str, UploadJobState] = {}
UPLOAD_LOCK = threading.Lock()
TRANSCRIPT_JOBS: dict[int, str] = {}
TRANSCRIPT_LOCK = threading.Lock()
PREVIEW_LOCK = threading.Lock()
ASSET_JOBS: dict[int, str] = {}
ASSET_LOCK = threading.Lock()
TREE_COLLAPSE_KEY = "reelsift-tree-collapsed"


def is_admin(user: UserRecord | None) -> bool:
    return user is not None and user.role == "admin"


def is_default_account(user: UserRecord | None) -> bool:
    return user is not None and user.username in {"admin", "demo"}


def get_current_user(request: Request) -> UserRecord | None:
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    user = get_user_by_session_token(session_token, DB_PATH) if session_token else None
    request.state.current_user = user
    return user


def get_current_session_token(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE_NAME, "").strip()


def require_user(request: Request) -> UserRecord:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=303, detail="请先登录。")
    return user


def require_admin(request: Request) -> UserRecord:
    user = require_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可访问。")
    return user


def get_visible_libraries(request: Request) -> list[LibraryRecord]:
    user = require_user(request)
    if is_admin(user):
        return list_libraries(DB_PATH, include_all=True)
    return list_libraries(DB_PATH, owner_user_id=user.id, include_all=False)


def get_accessible_library(request: Request, library_id: int | None = None) -> LibraryRecord:
    user = require_user(request)
    if is_admin(user):
        library = get_library_by_id(library_id, DB_PATH, include_all=True) if library_id is not None else None
    else:
        library = (
            get_library_by_id(library_id, DB_PATH, owner_user_id=user.id, include_all=False)
            if library_id is not None else None
        )
    if library is None:
        libraries = get_visible_libraries(request)
        if not libraries:
            default_name = f"{user.display_name} 的素材库" if not is_admin(user) else "默认素材库"
            created_library = create_library(default_name, DB_PATH, owner_user_id=user.id)
            return created_library
        return libraries[0]
    return library


def require_accessible_library(request: Request, library_id: int) -> LibraryRecord:
    user = require_user(request)
    library = (
        get_library_by_id(library_id, DB_PATH, include_all=True)
        if is_admin(user)
        else get_library_by_id(library_id, DB_PATH, owner_user_id=user.id, include_all=False)
    )
    if library is None:
        raise HTTPException(status_code=404, detail="素材库不存在或无权访问。")
    return library


def require_accessible_clip(request: Request, clip_id: int) -> ClipDetail:
    clip = load_clip_detail(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    require_accessible_library(request, clip.library_id)
    return clip


def build_common_context(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    return {
        "current_user": user,
        "is_admin": is_admin(user),
        "is_default_account": is_default_account(user),
    }


@app.middleware("http")
async def require_login_middleware(request: Request, call_next):
    path = request.url.path
    public_prefixes = ("/static", "/login", "/admin/login", "/register", "/forgot-password")
    if path.startswith(public_prefixes) or path == "/favicon.ico":
        return await call_next(request)
    if get_current_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


def build_uncategorized_node_id(library_id: int) -> int:
    """返回当前素材库的未分类虚拟节点 ID。"""
    return -1000000 - library_id


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


def format_duration_ms(ms: int) -> str:
    """把毫秒时长转成适合粗剪面板展示的文本。"""
    total_seconds = max(ms // 1000, 0)
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"


def parse_timecode_ms(value: str) -> int:
    """解析 mm:ss、hh:mm:ss 或纯秒数为毫秒。"""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("请填写时间点")
    if re.fullmatch(r"\d+(\.\d+)?", cleaned):
        return int(float(cleaned) * 1000)

    parts = cleaned.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("时间格式请使用 mm:ss、hh:mm:ss 或秒数")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError("时间格式请使用数字") from exc
    if any(number < 0 for number in numbers):
        raise ValueError("时间点不能为负数")
    if len(numbers) == 2:
        minutes, seconds = numbers
        total_seconds = minutes * 60 + seconds
    else:
        hours, minutes, seconds = numbers
        total_seconds = hours * 3600 + minutes * 60 + seconds
    return int(total_seconds * 1000)


def build_cut_segment_view(segment: ClipCutSegmentRecord) -> dict[str, Any]:
    """构造详情页粗剪片段展示数据。"""
    exported_url = build_data_url_from_path(str(segment.exported_path)) if segment.exported_path else None
    return {
        "id": segment.id,
        "clip_id": segment.clip_id,
        "name": segment.name,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "start_text": format_timestamp(segment.start_ms),
        "end_text": format_timestamp(segment.end_ms),
        "duration_text": format_duration_ms(segment.end_ms - segment.start_ms),
        "note": segment.note,
        "exported_path": str(segment.exported_path) if segment.exported_path else None,
        "exported_url": exported_url,
    }


def load_cut_segments(clip_id: int) -> list[dict[str, Any]]:
    """读取详情页粗剪片段列表。"""
    return [build_cut_segment_view(segment) for segment in list_clip_cut_segments(clip_id, DB_PATH)]


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


def build_saved_transcript_context(transcripts: list[dict[str, Any]]) -> str:
    """把已入库 transcript 拼成适合重新生成摘要的上下文。"""
    if not transcripts:
        return ""
    lines: list[str] = []
    total_length = 0
    for item in transcripts:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        line = f"{format_timestamp(int(item.get('start_ms') or 0))} {text}"
        if total_length + len(line) > 900:
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


def resolve_library_id(request: Request, library_id: int | None = None) -> int:
    """返回当前要查看的素材库 ID。"""
    if library_id is not None:
        return require_accessible_library(request, library_id).id
    return get_accessible_library(request, library_id).id


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
    return db_count_uncategorized_clips(library_id, DB_PATH)


def clamp_score(value: float, *, minimum: float = 0.0, maximum: float = 100.0) -> float:
    """把分数压到稳定区间。"""
    return max(minimum, min(maximum, value))


def build_compare_payload(request: Request, clip: ClipDetail) -> dict[str, Any]:
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
        **build_common_context(request),
        "clip": clip,
        "overall_score": ranking.overall_score,
        "sharpness_score": ranking.sharpness_score,
        "stability_score": ranking.stability_score,
        "content_score": ranking.content_score,
        "speech_score": ranking.speech_score,
        "composition_score": ranking.composition_score,
    }


def load_compare_payloads(request: Request, clip_ids: list[int]) -> list[dict[str, Any]]:
    """读取对比页需要的素材与评分。"""
    payloads: list[dict[str, Any]] = []
    for clip_id in clip_ids[:6]:
        clip = load_clip_detail(clip_id)
        if clip is None:
            continue
        payloads.append(build_compare_payload(request, clip))
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


def build_clip_card_from_row(row: dict[str, Any]) -> ClipCard:
    """把数据库查询结果转换成页面卡片对象。"""
    folder_names = row.get("folder_names") or []
    comparison_scores_json = row.get("comparison_scores_json")
    return ClipCard(
        id=int(row["id"]),
        filename=row["filename"],
        filepath=row["filepath"],
        summary=row.get("summary") or "暂无摘要",
        scene=row.get("scene") or "未识别场景",
        subjects=list(row.get("subjects_json") or []),
        actions=list(row.get("actions_json") or []),
        tags=list(row.get("tags") or []),
        has_motion=bool(row.get("has_motion")) if row.get("has_motion") is not None else False,
        sharpness_score=row.get("sharpness_score"),
        cover_url=build_cover_url(row.get("cover_path")),
        status=row.get("status") or "pending",
        visual_error_message=row.get("error_message"),
        transcript_status=row.get("transcript_status") or "pending",
        transcript_error_message=row.get("transcript_error_message"),
        preview_status=row.get("preview_status") or "pending",
        comparison_status=row.get("comparison_status") or "pending",
        comparison_scores=json.loads(comparison_scores_json) if comparison_scores_json else None,
        comparison_error_message=row.get("comparison_error_message"),
        folder_label=folder_names[0] if folder_names else "未分类",
        is_favorite=bool(row.get("is_favorite")),
        rating=int(row.get("rating") or 0),
        search_score=float(row.get("search_score") or 0.0),
    )


def load_clips(
    request: Request,
    query: str = "",
    tag: str = "",
    favorite_only: bool = False,
    min_rating: int = 0,
    include_failed: bool = False,
    library_id: int | None = None,
    node_id: int | None = None,
) -> list[ClipCard]:
    """读取素材卡片数据，并支持关键词与标签过滤。"""
    current_library_id = resolve_library_id(request, library_id)
    current_uncategorized_id = build_uncategorized_node_id(current_library_id)
    rows = query_clips(
        library_id=current_library_id,
        query=query.strip(),
        tag=tag.strip(),
        favorite_only=favorite_only,
        min_rating=min_rating,
        include_failed=include_failed,
        node_id=node_id,
        uncategorized_node_id=current_uncategorized_id,
        db_path=DB_PATH,
    )
    return [build_clip_card_from_row(row) for row in rows]


def load_clip_card(clip_id: int) -> ClipCard | None:
    """读取单条素材卡片数据。"""
    row = query_clip_card(clip_id, DB_PATH)
    if row is None:
        return None
    return build_clip_card_from_row(row)


def load_tags(request: Request, library_id: int | None = None, node_id: int | None = None) -> list[tuple[str, int]]:
    """读取所有标签及其数量。"""
    current_library_id = resolve_library_id(request, library_id)
    current_uncategorized_id = build_uncategorized_node_id(current_library_id)
    return query_tag_counts(
        current_library_id,
        node_id=node_id,
        uncategorized_node_id=current_uncategorized_id,
        db_path=DB_PATH,
    )


def load_clip_detail(clip_id: int) -> ClipDetail | None:
    """读取单条素材详情。"""
    row = query_clip_detail(clip_id, DB_PATH)
    if row is None:
        return None

    file_path = Path(row["filepath"])
    file_size_text = "未知"
    shot_time_text = "未知"
    media_url: str | None = None
    media_type: str | None = None
    media_error_message: str | None = None
    preview_status = row.get("preview_status") or "pending"
    if file_path.exists():
        stat = file_path.stat()
        file_size_text = format_file_size(stat.st_size)
        shot_time_text = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        preview_path = row.get("preview_path")
        if preview_path:
            media_url = build_data_url_from_path(preview_path)
            media_type = "video/mp4"
        media_error_message = row.get("preview_error_message")

    cover_path = row.get("cover_path")
    transcript_records = load_transcripts(clip_id, DB_PATH)
    transcript_status = normalize_transcript_status(row.get("transcript_status"), transcript_records)
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
        library_name=row.get("library_name") or "默认素材库",
        filename=row["filename"],
        filepath=row["filepath"],
        summary=row.get("summary") or "暂无摘要",
        scene=row.get("scene") or "未识别场景",
        subjects=list(row.get("subjects_json") or []),
        actions=list(row.get("actions_json") or []),
        tags=list(row.get("tags") or []),
        has_motion=bool(row.get("has_motion")) if row.get("has_motion") is not None else False,
        sharpness_score=row.get("sharpness_score"),
        cover_url=build_cover_url(cover_path),
        keyframe_urls=build_keyframe_urls(cover_path),
        media_url=media_url,
        media_type=media_type,
        media_error_message=media_error_message,
        preview_status=preview_status,
        status=row.get("status") or "pending",
        visual_error_message=row.get("error_message"),
        file_size_text=file_size_text,
        shot_time_text=shot_time_text,
        transcripts=transcripts,
        transcript_available=is_asr_configured(),
        transcript_status=transcript_status,
        transcript_error_message=row.get("transcript_error_message"),
        comparison_scores=json.loads(row["comparison_scores_json"]) if row.get("comparison_scores_json") else None,
        comparison_status=row.get("comparison_status") or "pending",
        comparison_error_message=row.get("comparison_error_message"),
        user_note=row.get("user_note"),
    )


def load_similar_clips(current_clip_id: int, library_id: int, scene: str, limit: int = 3) -> list[ClipCard]:
    """按相同场景读取相似素材，用于详情页侧边展示。"""
    rows = query_similar_clips(current_clip_id, library_id, scene, limit=limit, db_path=DB_PATH)
    return [build_clip_card_from_row(row) for row in rows]


def load_adjacent_clip_ids(clip_id: int, library_id: int) -> tuple[int | None, int | None]:
    """按素材库当前排序查找上一条和下一条素材。"""
    return query_adjacent_clip_ids(clip_id, library_id, DB_PATH)


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


def resolve_upload_library(request: Request, library_id_input: int | None, new_library_name: str) -> LibraryRecord:
    """根据上传页输入，决定这批视频要归属的素材库。"""
    user = require_user(request)
    cleaned_name = new_library_name.strip()
    if cleaned_name:
        try:
            return create_library(cleaned_name, DB_PATH, owner_user_id=user.id)
        except ValueError as exc:
            existing_library = get_library_by_name(
                cleaned_name,
                DB_PATH,
                owner_user_id=None if is_admin(user) else user.id,
                include_all=is_admin(user),
            )
            if existing_library is not None:
                return existing_library
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    current_library_id = resolve_library_id(request, library_id_input)
    library = get_accessible_library(request, current_library_id)
    if library is None:
        raise HTTPException(status_code=400, detail="选中的素材库不存在。")
    return library


def build_upload_page_context(
    request: Request,
    upload_job: UploadJobState | None = None,
    error_message: str | None = None,
    selected_library_id: int | None = None,
    pending_library_name: str = "",
) -> dict[str, Any]:
    """构造上传页上下文。"""
    libraries = get_visible_libraries(request)
    current_library_id = resolve_library_id(request, selected_library_id)
    return {
        **build_common_context(request),
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
    db_update_clip_transcript_state(
        clip.id,
        transcript_status=transcript_status,
        transcript_error_message=transcript_error_message,
        db_path=DB_PATH,
    )


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
    db_update_clip_asset_state(
        clip_id,
        preview_status=preview_status,
        preview_path=preview_path,
        preview_error_message=preview_error_message,
        comparison_status=comparison_status,
        comparison_scores=comparison_scores,
        comparison_error_message=comparison_error_message,
        db_path=DB_PATH,
    )


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

    rows = query_export_clips(clip_ids, DB_PATH)

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


def build_cut_segment_export_path(clip: ClipDetail, segment: ClipCutSegmentRecord, export_dir: str = "") -> Path:
    """生成粗剪片段导出路径。export_dir 非空时使用自定义目录。"""
    if export_dir.strip():
        destination_dir = Path(export_dir.strip()).expanduser().resolve()
    else:
        destination_dir = PICKS_DIR / "clips" / str(clip.id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    base_name = sanitize_export_filename(segment.name or segment.note or clip.summary or Path(clip.filename).stem)
    destination = destination_dir / f"{base_name}.mp4"
    sequence = 2
    while destination.exists():
        destination = destination_dir / f"{base_name}-{sequence}.mp4"
        sequence += 1
    return destination


def export_cut_segment(clip: ClipDetail, segment: ClipCutSegmentRecord, export_dir: str = "") -> Path:
    """用 ffmpeg 导出粗剪片段，不覆盖原视频。"""
    source = Path(clip.filepath)
    if not source.exists():
        raise ValueError("原始视频不存在，无法导出片段")

    duration_seconds = max((segment.end_ms - segment.start_ms) / 1000.0, 0.01)
    destination = build_cut_segment_export_path(clip, segment, export_dir=export_dir)
    stream = ffmpeg.input(str(source), ss=segment.start_ms / 1000.0)
    (
        ffmpeg
        .output(
            stream,
            str(destination),
            t=duration_seconds,
            vcodec="libx264",
            acodec="aac",
            pix_fmt="yuv420p",
            movflags="+faststart",
        )
        .overwrite_output()
        .run(quiet=True)
    )
    update_clip_cut_segment_export_path(segment.id, destination, DB_PATH)
    return destination


def build_cut_export_result(exported_path: Path) -> dict[str, Any]:
    """构造粗剪导出完成面板需要的数据。"""
    return {
        "copied": 1,
        "files": [exported_path.name],
        "skipped": [],
        "destination": str(exported_path.parent),
        "message": "已导出 1 个粗剪片段",
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


@app.get("/api/pick-folder", include_in_schema=False)
def pick_folder_dialog(request: Request):
    """调起当前系统的原生文件夹选择对话框，返回用户选择的路径。"""
    require_user(request)
    if sys.platform.startswith("win"):
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected_path = filedialog.askdirectory(title="选择导出目录", mustexist=True)
            root.destroy()
            return {
                "path": selected_path.rstrip("\\/"),
                "cancelled": not bool(selected_path),
                "error": "",
            }
        except Exception as exc:
            return {
                "path": "",
                "cancelled": True,
                "error": f"Windows 文件夹选择器启动失败：{exc}",
            }

    if sys.platform != "darwin":
        return {"path": "", "cancelled": True, "error": "当前系统暂不支持原生文件夹选择，请手动输入路径。"}

    try:
        picker, picker_error = _ensure_picker_binary()
        if picker is None:
            return {"path": "", "cancelled": True, "error": picker_error or "文件夹选择器不可用。"}
        result = subprocess.run(
            [str(picker)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return {"path": "", "cancelled": True, "error": (result.stderr or "Finder 文件夹选择器启动失败。").strip()}
        path = result.stdout.strip().rstrip("/")
        return {"path": path, "cancelled": not bool(path), "error": ""}
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": True, "error": "文件夹选择超时。"}
    except Exception as exc:
        return {"path": "", "cancelled": True, "error": str(exc)}


@app.get("/login", include_in_schema=False)
def login_page(request: Request, next_url: str = Query(default="/"), error: str = Query(default="")):
    user = get_current_user(request)
    if user is not None:
        return RedirectResponse(url=next_url or "/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            **build_common_context(request),
            "login_title": "用户登录",
            "login_action": "/login",
            "next_url": next_url or "/",
            "error_message": error.strip(),
            "admin_only": False,
        },
    )


@app.post("/login", include_in_schema=False)
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form(default="/"),
):
    user = authenticate_user(username, password, DB_PATH)
    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                **build_common_context(request),
                "login_title": "用户登录",
                "login_action": "/login",
                "next_url": next_url or "/",
                "error_message": "用户名或密码不正确。",
                "admin_only": False,
            },
            status_code=400,
        )
    response = RedirectResponse(url=next_url or "/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session(user.id, DB_PATH),
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
    )
    return response


@app.get("/admin/login", include_in_schema=False)
def admin_login_page(request: Request, error: str = Query(default="")):
    user = get_current_user(request)
    if is_admin(user):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            **build_common_context(request),
            "login_title": "管理员登录",
            "login_action": "/admin/login",
            "next_url": "/admin",
            "error_message": error.strip(),
            "admin_only": True,
        },
    )


@app.post("/admin/login", include_in_schema=False)
def admin_login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user = authenticate_user(username, password, DB_PATH)
    if user is None or not is_admin(user):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                **build_common_context(request),
                "login_title": "管理员登录",
                "login_action": "/admin/login",
                "next_url": "/admin",
                "error_message": "管理员账号或密码不正确。",
                "admin_only": True,
            },
            status_code=400,
        )
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session(user.id, DB_PATH),
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
    )
    return response


@app.post("/logout", include_in_schema=False)
def logout_action(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    with suppress(Exception):
        delete_session(session_token, DB_PATH)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/admin", include_in_schema=False)
def admin_dashboard_page(
    request: Request,
    error: str = Query(default=""),
    success: str = Query(default=""),
):
    require_admin(request)
    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            **build_common_context(request),
            "dashboard": get_admin_dashboard_stats(DB_PATH),
            "users": list_users(DB_PATH),
            "user_create_error": "",
            "user_create_success": "",
            "user_action_error": error.strip(),
            "user_action_success": success.strip(),
        },
    )


@app.get("/register", include_in_schema=False)
def register_page(request: Request, error: str = Query(default=""), success: str = Query(default="")):
    user = get_current_user(request)
    if user is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            **build_common_context(request),
            "error_message": error.strip(),
            "success_message": success.strip(),
        },
    )


@app.post("/register", include_in_schema=False)
def register_action(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(default=""),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                **build_common_context(request),
                "error_message": "两次输入的密码不一致。",
                "success_message": "",
            },
            status_code=400,
        )
    try:
        create_user(
            username=username,
            password=password,
            display_name=display_name,
            role="user",
            db_path=DB_PATH,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                **build_common_context(request),
                "error_message": str(exc),
                "success_message": "",
            },
            status_code=400,
        )
    return RedirectResponse(url="/register?success=注册成功，请登录。", status_code=303)


@app.get("/account", include_in_schema=False)
def account_page(
    request: Request,
    error: str = Query(default=""),
    success: str = Query(default=""),
    phone_code: str = Query(default=""),
    pending_phone: str = Query(default=""),
):
    user = require_user(request)
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            **build_common_context(request),
            "account_user": user,
            "error_message": error.strip(),
            "success_message": success.strip(),
            "phone_code": phone_code.strip(),
            "pending_phone": pending_phone.strip(),
        },
    )


@app.post("/account/password", include_in_schema=False)
def change_password_action(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = require_user(request)
    session_token = get_current_session_token(request)
    if new_password != confirm_password:
        return RedirectResponse(url="/account?error=两次输入的新密码不一致。", status_code=303)
    try:
        change_user_password(user.id, current_password, new_password, DB_PATH)
        revoke_user_sessions(user.id, DB_PATH)
    except ValueError as exc:
        return RedirectResponse(url=f"/account?error={exc}", status_code=303)

    response = RedirectResponse(url="/account?success=密码已更新，其他设备已退出登录。", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session(user.id, DB_PATH),
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
    )
    return response


@app.post("/account/phone/code", include_in_schema=False)
def send_account_phone_code_action(
    request: Request,
    phone_number: str = Form(...),
):
    user = require_user(request)
    try:
        code = issue_phone_verification_code(phone_number, "bind", DB_PATH, user_id=user.id)
    except ValueError as exc:
        return RedirectResponse(url=f"/account?error={exc}", status_code=303)
    return RedirectResponse(
        url=f"/account?success=验证码已生成。&phone_code={code}&pending_phone={phone_number.strip()}",
        status_code=303,
    )


@app.post("/account/phone/verify", include_in_schema=False)
def verify_account_phone_action(
    request: Request,
    phone_number: str = Form(...),
    code: str = Form(...),
):
    user = require_user(request)
    try:
        bind_user_phone(user.id, phone_number, code, DB_PATH)
    except ValueError as exc:
        return RedirectResponse(url=f"/account?error={exc}", status_code=303)
    return RedirectResponse(url="/account?success=手机号已验证并绑定。", status_code=303)


@app.post("/account/logout-all", include_in_schema=False)
def logout_all_sessions_action(request: Request):
    user = require_user(request)
    session_token = get_current_session_token(request)
    revoke_user_sessions(user.id, DB_PATH)
    with suppress(Exception):
        delete_session(session_token, DB_PATH)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/forgot-password", include_in_schema=False)
def forgot_password_page(
    request: Request,
    error: str = Query(default=""),
    success: str = Query(default=""),
    phone_code: str = Query(default=""),
    pending_phone: str = Query(default=""),
):
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={
            **build_common_context(request),
            "error_message": error.strip(),
            "success_message": success.strip(),
            "phone_code": phone_code.strip(),
            "pending_phone": pending_phone.strip(),
        },
    )


@app.post("/forgot-password/code", include_in_schema=False)
def forgot_password_code_action(
    request: Request,
    phone_number: str = Form(...),
):
    try:
        code = issue_phone_verification_code(phone_number, "reset", DB_PATH)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="forgot_password.html",
            context={
                **build_common_context(request),
                "error_message": str(exc),
                "success_message": "",
                "phone_code": "",
                "pending_phone": phone_number.strip(),
            },
            status_code=400,
        )
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={
            **build_common_context(request),
            "error_message": "",
            "success_message": "验证码已生成。",
            "phone_code": code,
            "pending_phone": phone_number.strip(),
        },
    )


@app.post("/forgot-password/reset", include_in_schema=False)
def forgot_password_reset_action(
    request: Request,
    phone_number: str = Form(...),
    code: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="forgot_password.html",
            context={
                **build_common_context(request),
                "error_message": "两次输入的新密码不一致。",
                "success_message": "",
                "phone_code": "",
                "pending_phone": phone_number.strip(),
            },
            status_code=400,
        )
    try:
        reset_password_by_phone(phone_number, code, new_password, DB_PATH)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="forgot_password.html",
            context={
                **build_common_context(request),
                "error_message": str(exc),
                "success_message": "",
                "phone_code": "",
                "pending_phone": phone_number.strip(),
            },
            status_code=400,
        )
    return RedirectResponse(url="/login?error=密码已重置，请使用新密码登录。", status_code=303)


@app.post("/admin/users", include_in_schema=False)
def admin_create_user_action(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(default=""),
    role: str = Form(default="user"),
    password: str = Form(...),
):
    require_admin(request)
    try:
        create_user(
            username=username,
            password=password,
            display_name=display_name,
            role=role,
            db_path=DB_PATH,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="admin_dashboard.html",
            context={
                **build_common_context(request),
                "dashboard": get_admin_dashboard_stats(DB_PATH),
                "users": list_users(DB_PATH),
                "user_create_error": str(exc),
                "user_create_success": "",
                "user_action_error": "",
                "user_action_success": "",
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin?success=用户已创建。", status_code=303)


@app.post("/admin/users/{user_id}/status", include_in_schema=False)
def admin_update_user_status_action(
    request: Request,
    user_id: int,
    is_active: str = Form(...),
):
    require_admin(request)
    try:
        update_user_status(user_id, is_active == "true", DB_PATH)
    except ValueError as exc:
        return RedirectResponse(url=f"/admin?error={exc}", status_code=303)
    return RedirectResponse(url="/admin?success=用户状态已更新。", status_code=303)


@app.post("/admin/users/{user_id}/password", include_in_schema=False)
def admin_reset_user_password_action(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
):
    require_admin(request)
    try:
        update_user_password(user_id, new_password, DB_PATH)
        revoke_user_sessions(user_id, DB_PATH)
    except ValueError as exc:
        return RedirectResponse(url=f"/admin?error={exc}", status_code=303)
    return RedirectResponse(url="/admin?success=密码已重置，旧会话已失效。", status_code=303)


@app.post("/admin/users/{user_id}/logout-all", include_in_schema=False)
def admin_logout_user_sessions_action(request: Request, user_id: int):
    require_admin(request)
    user = get_user_by_id(user_id, DB_PATH)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    revoke_user_sessions(user_id, DB_PATH)
    return RedirectResponse(url="/admin?success=该用户已被强制下线。", status_code=303)


@app.get("/", include_in_schema=False)
def grid_page(
    request: Request,
    q: str = Query(default=""),
    tag: str = Query(default=""),
    favorite: bool = Query(default=False),
    rating: int = Query(default=0),
    library_id: Optional[int] = Query(default=None),
    node_id: Optional[int] = Query(default=None),
):
    """渲染素材网格页。"""
    current_library_id = resolve_library_id(request, library_id)
    current_node_id = resolve_node_id(current_library_id, node_id)
    clips = load_clips(
        request,
        query=q.strip(),
        tag=tag.strip(),
        favorite_only=favorite,
        min_rating=rating,
        library_id=current_library_id,
        node_id=current_node_id,
    )
    libraries = get_visible_libraries(request)
    current_library = require_accessible_library(request, current_library_id)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            **build_common_context(request),
            "clips": clips,
            "stats": build_stats(clips),
            "all_tags": load_tags(request, current_library_id, current_node_id),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "current_favorite": favorite,
            "current_rating": max(0, min(int(rating), 5)),
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
        context=build_upload_page_context(request, selected_library_id=library_id),
    )


def build_clip_detail_context(
    request: Request,
    clip_id: int,
    current_library_id: int,
    edit_error: str | None = None,
    cut_error: str | None = None,
) -> dict[str, Any]:
    """构造详情页模板上下文。"""
    clip = require_accessible_clip(request, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    previous_clip_id, next_clip_id = load_adjacent_clip_ids(clip_id, current_library_id)
    return {
        **build_common_context(request),
        "clip": clip,
        "similar_clips": load_similar_clips(clip.id, current_library_id, clip.scene),
        "cut_segments": load_cut_segments(clip.id),
        "return_url": f"/?library_id={current_library_id}",
        "current_library_id": current_library_id,
        "edit_error": edit_error,
        "cut_error": cut_error,
        "previous_clip_id": previous_clip_id,
        "next_clip_id": next_clip_id,
    }


def build_clip_cut_context(
    request: Request,
    clip_id: int,
    current_library_id: int,
    cut_error: str | None = None,
) -> dict[str, Any]:
    """构造独立粗剪页模板上下文。"""
    clip = require_accessible_clip(request, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    return {
        **build_common_context(request),
        "clip": clip,
        "cut_segments": load_cut_segments(clip.id),
        "return_url": f"/clips/{clip.id}?library_id={current_library_id}",
        "library_url": f"/?library_id={current_library_id}",
        "current_library_id": current_library_id,
        "cut_error": cut_error,
    }


@app.get("/clips/{clip_id}", include_in_schema=False)
def clip_detail_page(request: Request, clip_id: int, library_id: Optional[int] = Query(default=None)):
    """渲染单条素材详情页。"""
    clip = require_accessible_clip(request, clip_id)
    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    return templates.TemplateResponse(
        request=request,
        name="clip_detail.html",
        context=build_clip_detail_context(request, clip_id, current_library_id),
    )


@app.get("/clips/{clip_id}/cut", include_in_schema=False)
def clip_cut_page(request: Request, clip_id: int, library_id: Optional[int] = Query(default=None)):
    """渲染单条素材的独立粗剪页。"""
    clip = require_accessible_clip(request, clip_id)
    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    return templates.TemplateResponse(
        request=request,
        name="clip_cut.html",
        context=build_clip_cut_context(request, clip_id, current_library_id),
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

    current_library_id = resolve_library_id(request, library_id)
    payloads = [item for item in load_compare_payloads(request, clip_ids) if item["clip"].library_id == current_library_id]
    if len(payloads) < 2:
        raise HTTPException(status_code=400, detail="没有足够素材可用于对比。")
    for item in payloads:
        item["recommendation_reason"] = build_recommendation_reason(item, payloads)
    return templates.TemplateResponse(
        request=request,
        name="compare.html",
        context={
            **build_common_context(request),
            "compare_items": payloads,
            "recommended_item": payloads[0],
            "current_library_id": current_library_id,
            "return_url": f"/?library_id={current_library_id}",
        },
    )


@app.post("/compare/selection", include_in_schema=False)
def compare_clips_from_selection(
    request: Request,
    selected_ids: list[str] = Form(default_factory=list),
    library_id: Optional[int] = Form(default=None),
):
    """从素材库多选进入对比页。"""
    current_library_id = resolve_library_id(request, library_id)
    clip_ids = parse_selected_ids(selected_ids)[:6]
    if len(clip_ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择 2 条素材才能对比。")
    joined_ids = ",".join(str(item) for item in clip_ids)
    return RedirectResponse(url=f"/compare?library_id={current_library_id}&ids={joined_ids}", status_code=303)


@app.post("/compare/keep", include_in_schema=False)
def keep_recommended_clip(
    request: Request,
    keep_clip_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
    library_id: int = Form(...),
):
    """保留推荐素材，并把其余素材移入回收站。"""
    require_accessible_library(request, library_id)
    require_accessible_clip(request, keep_clip_id)
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
    clip = require_accessible_clip(request, clip_id)
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
        current_library_id = resolve_library_id(request, library_id)
        return templates.TemplateResponse(
            request=request,
            name="clip_detail.html",
            context=build_clip_detail_context(request, clip_id, current_library_id, str(exc)),
            status_code=400,
        )

    current_library_id = resolve_library_id(request, library_id)
    return RedirectResponse(url=f"/clips/{clip_id}?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/summary/from-transcript", include_in_schema=False)
def regenerate_clip_summary_from_transcript(
    request: Request,
    clip_id: int,
    library_id: Optional[int] = Form(default=None),
):
    """结合已保存的口播转写重新生成摘要。"""
    clip = require_accessible_clip(request, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    if not clip.transcripts:
        raise HTTPException(status_code=400, detail="这条素材还没有可用口播。")

    frame_dir = CACHE_DIR / build_video_hash(Path(clip.filepath))
    frame_paths = get_keyframe_paths(frame_dir)
    if not frame_paths:
        raise HTTPException(status_code=400, detail="这条素材缺少关键帧，无法重新生成摘要。")

    transcript_context = build_saved_transcript_context(clip.transcripts)
    analysis = analyze_video(frame_paths, transcript_context)
    try:
        update_clip_summary(
            clip_id=clip.id,
            library_id=clip.library_id,
            summary=analysis.summary,
            db_path=DB_PATH,
        )
    except ValueError as exc:
        current_library_id = resolve_library_id(request, library_id or clip.library_id)
        return templates.TemplateResponse(
            request=request,
            name="clip_detail.html",
            context=build_clip_detail_context(request, clip_id, current_library_id, str(exc)),
            status_code=400,
        )

    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    return RedirectResponse(url=f"/clips/{clip_id}?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/card-summary", include_in_schema=False)
def update_clip_card_summary_action(
    request: Request,
    clip_id: int,
    summary: str = Form(...),
    library_id: Optional[int] = Form(default=None),
):
    """更新素材库卡片里的摘要名称，并返回局部卡片 HTML。"""
    clip_detail = require_accessible_clip(request, clip_id)
    clip = load_clip_card(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    current_library_id = resolve_library_id(request, library_id or clip_detail.library_id)
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


@app.post("/clips/{clip_id}/review", include_in_schema=False)
def update_clip_review_action(
    request: Request,
    clip_id: int,
    is_favorite: Optional[bool] = Form(default=None),
    rating: Optional[int] = Form(default=None),
    filter_favorite: bool = Form(default=False),
    filter_rating: int = Form(default=0),
    library_id: Optional[int] = Form(default=None),
):
    """更新素材卡片的收藏与人工等级，并返回卡片局部 HTML。"""
    clip_detail = require_accessible_clip(request, clip_id)
    if clip_detail is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    update_clip_review(
        clip_id=clip_detail.id,
        is_favorite=is_favorite,
        rating=rating,
        db_path=DB_PATH,
    )
    clip = load_clip_card(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")
    current_library_id = resolve_library_id(request, library_id or clip_detail.library_id)
    current_filter_rating = max(0, min(int(filter_rating), 5))
    if filter_favorite and not clip.is_favorite:
        return Response(content="", media_type="text/html")
    if current_filter_rating > 0 and clip.rating < current_filter_rating:
        return Response(content="", media_type="text/html")
    return templates.TemplateResponse(
        request=request,
        name="partials/clip_card.html",
        context={
            "clip": clip,
            "current_library_id": current_library_id,
            "current_library_name": get_library_by_id(current_library_id, DB_PATH).name if get_library_by_id(current_library_id, DB_PATH) else "素材库",
            "project_tree": build_project_tree(current_library_id, resolve_node_id(current_library_id)),
            "current_favorite": filter_favorite,
            "current_rating": current_filter_rating,
            "card_error": None,
            "card_edit_open": False,
        },
    )


@app.post("/clips/{clip_id}/move", include_in_schema=False)
def move_clip_to_folder_action(
    request: Request,
    clip_id: int,
    library_id: int = Form(...),
    target_node_id: int = Form(...),
    return_node_id: Optional[int] = Form(default=None),
):
    """把单条素材移动到指定文件夹，或移回未分类。"""
    clip = require_accessible_clip(request, clip_id)
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
    clip = require_accessible_clip(request, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    tag_candidates = re.split(r"[\n,，]+", new_tags)
    try:
        append_clip_tags(clip_id=clip.id, new_tags=tag_candidates, db_path=DB_PATH)
    except ValueError as exc:
        current_library_id = resolve_library_id(request, library_id or clip.library_id)
        return templates.TemplateResponse(
            request=request,
            name="clip_detail.html",
            context=build_clip_detail_context(request, clip_id, current_library_id, str(exc)),
            status_code=400,
        )

    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    return RedirectResponse(url=f"/clips/{clip_id}?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/note", include_in_schema=False)
def update_clip_note_action(
    request: Request,
    clip_id: int,
    note: str = Form(default=""),
    library_id: Optional[int] = Form(default=None),
):
    """更新单条素材的用户批注。"""
    clip = require_accessible_clip(request, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    update_clip_note(clip_id=clip.id, note=note, db_path=DB_PATH)

    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    return RedirectResponse(url=f"/clips/{clip_id}?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/cut-segments", include_in_schema=False)
def create_clip_cut_segment_action(
    request: Request,
    clip_id: int,
    segment_name: str = Form(default=""),
    start_time: str = Form(...),
    end_time: str = Form(...),
    note: str = Form(default=""),
    library_id: Optional[int] = Form(default=None),
):
    """保存一段非破坏性粗剪片段。"""
    clip = require_accessible_clip(request, clip_id)
    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    try:
        create_clip_cut_segment(
            clip_id=clip.id,
            name=segment_name,
            start_ms=parse_timecode_ms(start_time),
            end_ms=parse_timecode_ms(end_time),
            note=note,
            db_path=DB_PATH,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="clip_cut.html",
            context=build_clip_cut_context(request, clip_id, current_library_id, cut_error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(url=f"/clips/{clip_id}/cut?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/cut-segments/{segment_id}/delete", include_in_schema=False)
def delete_clip_cut_segment_action(
    request: Request,
    clip_id: int,
    segment_id: int,
    library_id: Optional[int] = Form(default=None),
):
    """删除一条粗剪片段标记，不删除原视频。"""
    clip = require_accessible_clip(request, clip_id)
    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    segment = get_clip_cut_segment(segment_id, DB_PATH)
    if segment is None or segment.clip_id != clip.id:
        raise HTTPException(status_code=404, detail="粗剪片段不存在。")
    delete_clip_cut_segment(segment_id, DB_PATH)
    return RedirectResponse(url=f"/clips/{clip_id}/cut?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/cut-segments/{segment_id}/range", include_in_schema=False)
def update_clip_cut_segment_range_action(
    request: Request,
    clip_id: int,
    segment_id: int,
    start_ms: int = Form(...),
    end_ms: int = Form(...),
    library_id: Optional[int] = Form(default=None),
):
    """更新粗剪片段的入点和出点。"""
    clip = require_accessible_clip(request, clip_id)
    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    segment = get_clip_cut_segment(segment_id, DB_PATH)
    if segment is None or segment.clip_id != clip.id:
        raise HTTPException(status_code=404, detail="粗剪片段不存在。")
    try:
        update_clip_cut_segment_range(segment_id, start_ms, end_ms, DB_PATH)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="clip_cut.html",
            context=build_clip_cut_context(request, clip_id, current_library_id, cut_error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(url=f"/clips/{clip_id}/cut?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/cut-segments/{segment_id}/export", include_in_schema=False)
def export_clip_cut_segment_action(
    request: Request,
    clip_id: int,
    segment_id: int,
    library_id: Optional[int] = Form(default=None),
    export_dir: str = Form(default=""),
):
    """导出一条粗剪片段，支持自定义目标目录。"""
    clip = require_accessible_clip(request, clip_id)
    current_library_id = resolve_library_id(request, library_id or clip.library_id)
    segment = get_clip_cut_segment(segment_id, DB_PATH)
    if segment is None or segment.clip_id != clip.id:
        raise HTTPException(status_code=404, detail="粗剪片段不存在。")
    try:
        exported_path = export_cut_segment(clip, segment, export_dir=export_dir.strip())
    except Exception as exc:
        return templates.TemplateResponse(
            request=request,
            name="clip_cut.html",
            context=build_clip_cut_context(request, clip_id, current_library_id, cut_error=f"片段导出失败：{exc}"),
            status_code=400,
        )
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return templates.TemplateResponse(
            request=request,
            name="partials/export_panel.html",
            context={"export_result": build_cut_export_result(exported_path)},
        )
    return RedirectResponse(url=f"/clips/{clip_id}/cut?library_id={current_library_id}", status_code=303)


@app.post("/clips/{clip_id}/delete", include_in_schema=False)
def delete_single_clip(
    request: Request,
    clip_id: int,
    library_id: Optional[int] = Form(default=None),
):
    """删除当前素材，并跳转到相邻详情页或素材库。"""
    clip = require_accessible_clip(request, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="素材不存在。")

    current_library_id = resolve_library_id(request, library_id or clip.library_id)
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
    request: Request,
    library_id: int = Form(...),
    name: str = Form(...),
    parent_id: Optional[int] = Form(default=None),
):
    """在项目内创建文件夹。"""
    require_accessible_library(request, library_id)
    create_project_node(library_id=library_id, name=name, parent_id=parent_id, db_path=DB_PATH)
    target_url = f"/?library_id={library_id}"
    if parent_id is not None:
        target_url += f"&node_id={parent_id}"
    return RedirectResponse(url=target_url, status_code=303)


@app.post("/projects/nodes/{node_id}/rename", include_in_schema=False)
def rename_project_node_action(
    request: Request,
    node_id: int,
    library_id: int = Form(...),
    new_name: str = Form(...),
):
    """重命名项目节点。"""
    require_accessible_library(request, library_id)
    rename_project_node(node_id=node_id, library_id=library_id, new_name=new_name, db_path=DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}&node_id={node_id}", status_code=303)


@app.post("/projects/nodes/{node_id}/move", include_in_schema=False)
def move_project_node_action(
    request: Request,
    node_id: int,
    library_id: int = Form(...),
    target_parent_id: Optional[int] = Form(default=None),
):
    """移动项目节点。"""
    require_accessible_library(request, library_id)
    move_project_node(node_id=node_id, library_id=library_id, target_parent_id=target_parent_id, db_path=DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}&node_id={node_id}", status_code=303)


@app.post("/projects/nodes/{node_id}/delete", include_in_schema=False)
def delete_project_node_action(
    request: Request,
    node_id: int,
    library_id: int = Form(...),
):
    """删除项目节点。"""
    require_accessible_library(request, library_id)
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
    request: Request,
    library_id: int = Form(...),
    target_node_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
):
    """把选中的素材引用到指定项目节点。"""
    require_accessible_library(request, library_id)
    for clip_id in parse_selected_ids(selected_ids):
        attach_clip_to_node(clip_id=clip_id, node_id=target_node_id, db_path=DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}&node_id={target_node_id}", status_code=303)


@app.get("/recycle", include_in_schema=False)
def recycle_page(
    request: Request,
    library_id: Optional[int] = Query(default=None),
):
    """渲染项目回收站页面。"""
    current_library_id = resolve_library_id(request, library_id)
    current_library = require_accessible_library(request, current_library_id)
    return templates.TemplateResponse(
        request=request,
        name="recycle.html",
        context={
            **build_common_context(request),
            "current_library_id": current_library_id,
            "current_library": current_library,
            "recycle_items": list_recycled_clips(current_library_id, DB_PATH),
            "return_url": f"/?library_id={current_library_id}",
        },
    )


@app.post("/recycle/restore", include_in_schema=False)
def restore_recycled_clips_action(
    request: Request,
    library_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
):
    """从项目回收站恢复素材。"""
    require_accessible_library(request, library_id)
    restore_recycled_clips(library_id, parse_selected_ids(selected_ids), DB_PATH)
    return RedirectResponse(url=f"/?library_id={library_id}", status_code=303)


@app.get("/clips/{clip_id}/transcript", include_in_schema=False)
def clip_transcript_partial(request: Request, clip_id: int):
    """返回详情页 transcript 局部 HTML。"""
    clip = require_accessible_clip(request, clip_id)
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
    clip = require_accessible_clip(request, clip_id)
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
def clip_media(request: Request, clip_id: int):
    """按素材 ID 返回原始视频文件，供详情页播放。"""
    clip = require_accessible_clip(request, clip_id)
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
    favorite: bool = Query(default=False),
    rating: int = Query(default=0),
    library_id: Optional[int] = Query(default=None),
    node_id: Optional[int] = Query(default=None),
):
    """返回素材网格局部 HTML，供 HTMX 刷新。"""
    current_library_id = resolve_library_id(request, library_id)
    current_node_id = resolve_node_id(current_library_id, node_id)
    clips = load_clips(
        request,
        query=q.strip(),
        tag=tag.strip(),
        favorite_only=favorite,
        min_rating=rating,
        library_id=current_library_id,
        node_id=current_node_id,
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/clip_grid.html",
        context={
            "clips": clips,
            "stats": build_stats(clips),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "current_favorite": favorite,
            "current_rating": max(0, min(int(rating), 5)),
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
        library = resolve_upload_library(request, library_id, new_library_name)
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
                request,
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
    favorite: bool = Form(default=False),
    rating: int = Form(default=0),
    library_id: Optional[int] = Form(default=None),
    node_id: Optional[int] = Form(default=None),
):
    """导出选中的视频到 data/picks/。"""
    export_result = export_clips_by_ids(parse_selected_ids(selected_ids), destination_input=export_dir)
    current_library_id = resolve_library_id(request, library_id)
    current_node_id = resolve_node_id(current_library_id, node_id)
    clips = load_clips(
        request,
        query=q.strip(),
        tag=tag.strip(),
        favorite_only=favorite,
        min_rating=rating,
        library_id=current_library_id,
        node_id=current_node_id,
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/export_panel.html",
        context={
            "current_query": q.strip(),
            "current_tag": tag.strip(),
            "current_favorite": favorite,
            "current_rating": max(0, min(int(rating), 5)),
            "current_library_id": current_library_id,
            "current_node_id": current_node_id,
            "stats": build_stats(clips),
            "export_result": export_result,
            "current_export_dir": export_dir.strip(),
        },
    )


@app.post("/libraries/{library_id}/rename", include_in_schema=False)
def rename_library_action(
    request: Request,
    library_id: int,
    new_name: str = Form(...),
):
    """重命名素材库。"""
    require_accessible_library(request, library_id)
    try:
        rename_library(library_id, new_name, DB_PATH)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/?library_id={library_id}", status_code=303)


@app.post("/libraries/{library_id}/delete", include_in_schema=False)
def delete_library_action(request: Request, library_id: int):
    """删除空素材库。"""
    require_accessible_library(request, library_id)
    try:
        delete_library(library_id, DB_PATH)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


@app.post("/clips/delete", include_in_schema=False)
def delete_selected_clips(
    request: Request,
    library_id: int = Form(...),
    selected_ids: list[str] = Form(default_factory=list),
    node_id: Optional[int] = Form(default=None),
):
    """批量删除当前素材库中的素材。"""
    require_accessible_library(request, library_id)
    delete_clips(parse_selected_ids(selected_ids), library_id, DB_PATH)
    target_url = f"/?library_id={library_id}"
    if node_id is not None:
        target_url += f"&node_id={node_id}"
    return RedirectResponse(url=target_url, status_code=303)
