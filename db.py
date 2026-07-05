from __future__ import annotations

import json
import os
import base64
import difflib
import hashlib
import hmac
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
VENDOR_DIR = BASE_DIR / ".vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    case,
    create_engine,
    delete,
    event,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker


@dataclass
class LibraryRecord:
    id: int
    name: str
    clip_count: int = 0
    pending_note_count: int = 0
    owner_user_id: int | None = None


@dataclass
class ProjectNodeRecord:
    id: int
    library_id: int
    name: str
    parent_id: int | None = None
    depth: int = 0
    clip_count: int = 0


@dataclass
class RecycleClipRecord:
    clip_id: int
    library_id: int
    summary: str
    filename: str
    deleted_at: str
    expires_at: str
    deleted_node_ids: list[int]


@dataclass
class TranscriptRecord:
    clip_id: int
    start_ms: int
    end_ms: int
    text: str
    segment_index: int


@dataclass
class ClipCutSegmentRecord:
    id: int
    clip_id: int
    name: str
    start_ms: int
    end_ms: int
    note: str | None = None
    exported_path: Path | None = None


@dataclass
class StoryboardRecord:
    id: int
    library_id: int
    title: str
    brief_text: str
    target_duration_seconds: int
    tone_prompt: str
    selected_clip_ids: list[int]
    framework_text: str | None = None
    core_message: str | None = None
    emotional_arc_json: list[str] | None = None
    story_plan: str | None = None
    script_text: str | None = None
    revision_prompt: str | None = None
    status: str = "draft"
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class StoryboardItemRecord:
    id: int
    storyboard_id: int
    clip_id: int
    position: int
    section_name: str
    narrative_role: str
    suggested_duration_seconds: int
    script_line: str | None = None
    reason: str | None = None


@dataclass
class StoryboardMessageRecord:
    id: int
    storyboard_id: int
    role: str
    content: str
    reasoning_text: str | None = None
    action_json: dict[str, Any] | None = None
    created_at: str | None = None


@dataclass
class StoryboardRunRecord:
    id: int
    storyboard_id: int
    run_type: str
    status: str
    reasoning_text: str | None = None
    output_text: str | None = None
    error_message: str | None = None


@dataclass
class ClipRecord:
    video_hash: str
    filename: str
    filepath: Path
    library_id: int = 1
    summary: str | None = None
    scene: str | None = None
    subjects: list[str] | None = None
    actions: list[str] | None = None
    tags: list[str] | None = None
    has_motion: bool | None = None
    sharpness_score: float | None = None
    cover_path: Path | None = None
    status: str = "pending"
    error_message: str | None = None
    transcript_status: str = "pending"
    transcript_error_message: str | None = None
    preview_status: str = "pending"
    preview_path: Path | None = None
    preview_error_message: str | None = None
    comparison_status: str = "pending"
    comparison_scores_json: str | None = None
    comparison_error_message: str | None = None
    user_note: str | None = None
    source_modified_at: float | None = None
    note_status: str = "pending"


@dataclass
class UserRecord:
    id: int
    username: str
    role: str
    display_name: str
    is_active: bool = True
    phone_number: str | None = None
    phone_verified_at: datetime | None = None


@dataclass
class UserUsageRecord:
    user: UserRecord
    library_count: int
    clip_count: int
    recycled_clip_count: int
    total_storage_bytes: int
    active_session_count: int


DEFAULT_LIBRARY_NAME = "默认素材库"
ROOT_NODE_NAME = "全部素材"
_DB_PATH = BASE_DIR / "data" / "reelsift.db"
_DB_LOCK = threading.RLock()
_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker | None = None
_ENGINE_KEY: str | None = None

load_dotenv(override=True)

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(100), nullable=False, unique=True),
    Column("password_hash", String(512), nullable=False),
    Column("role", String(20), nullable=False),
    Column("display_name", String(255), nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("phone_number", String(32), nullable=True, unique=True),
    Column("phone_verified_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

user_sessions = Table(
    "user_sessions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("session_token", String(255), nullable=False, unique=True),
    Column("expires_at", DateTime, nullable=False),
    Column("created_at", DateTime, nullable=False),
)

phone_verification_codes = Table(
    "phone_verification_codes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("phone_number", String(32), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
    Column("code_hash", String(512), nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("consumed_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False),
)

libraries = Table(
    "libraries",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False, unique=True),
    Column("owner_user_id", Integer, ForeignKey("users.id", ondelete="SET NULL")),
    Column("created_at", DateTime, nullable=False),
)

clips = Table(
    "clips",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("video_hash", String(255), nullable=False, unique=True),
    Column("library_id", Integer, ForeignKey("libraries.id"), nullable=False, default=1),
    Column("filename", String(1024), nullable=False),
    Column("filepath", Text, nullable=False),
    Column("summary", Text),
    Column("scene", Text),
    Column("subjects_json", JSON, nullable=False, default=list),
    Column("actions_json", JSON, nullable=False, default=list),
    Column("has_motion", Boolean),
    Column("sharpness_score", Float),
    Column("cover_path", Text),
    Column("status", String(32), nullable=False, default="pending"),
    Column("error_message", Text),
    Column("transcript_status", String(32), nullable=False, default="pending"),
    Column("transcript_error_message", Text),
    Column("preview_status", String(32), nullable=False, default="pending"),
    Column("preview_path", Text),
    Column("preview_error_message", Text),
    Column("comparison_status", String(32), nullable=False, default="pending"),
    Column("comparison_scores_json", Text),
    Column("comparison_error_message", Text),
    Column("user_note", Text),
    Column("source_modified_at", Float),
    Column("note_status", String(32), nullable=False, default="pending"),
    Column("is_favorite", Boolean, nullable=False, default=False),
    Column("rating", Integer, nullable=False, default=0),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

clip_tags = Table(
    "clip_tags",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("clip_id", Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False),
    Column("tag", String(255), nullable=False),
    UniqueConstraint("clip_id", "tag", name="uq_clip_tags_clip_id_tag"),
)

transcripts = Table(
    "transcripts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("clip_id", Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False),
    Column("start_ms", Integer, nullable=False),
    Column("end_ms", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("segment_index", Integer, nullable=False),
)

clip_cut_segments = Table(
    "clip_cut_segments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("clip_id", Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(255), nullable=False, default="未命名片段"),
    Column("start_ms", Integer, nullable=False),
    Column("end_ms", Integer, nullable=False),
    Column("note", Text),
    Column("exported_path", Text),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

storyboards = Table(
    "storyboards",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("library_id", Integer, ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
    Column("title", String(255), nullable=False),
    Column("brief_text", Text, nullable=False),
    Column("target_duration_seconds", Integer, nullable=False),
    Column("tone_prompt", Text, nullable=False),
    Column("selected_clip_ids_json", JSON, nullable=False, default=list),
    Column("framework_text", Text),
    Column("core_message", Text),
    Column("emotional_arc_json", JSON, nullable=False, default=list),
    Column("story_plan", Text),
    Column("script_text", Text),
    Column("revision_prompt", Text),
    Column("status", String(32), nullable=False, default="draft"),
    Column("error_message", Text),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

storyboard_items = Table(
    "storyboard_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("storyboard_id", Integer, ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False),
    Column("clip_id", Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("section_name", String(100), nullable=False),
    Column("narrative_role", Text, nullable=False),
    Column("suggested_duration_seconds", Integer, nullable=False),
    Column("script_line", Text),
    Column("reason", Text),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

storyboard_messages = Table(
    "storyboard_messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("storyboard_id", Integer, ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False),
    Column("role", String(32), nullable=False),
    Column("content", Text, nullable=False),
    Column("reasoning_text", Text),
    Column("action_json", JSON),
    Column("created_at", DateTime, nullable=False),
)

storyboard_runs = Table(
    "storyboard_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("storyboard_id", Integer, ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False),
    Column("run_type", String(40), nullable=False),
    Column("status", String(32), nullable=False),
    Column("reasoning_text", Text),
    Column("output_text", Text),
    Column("error_message", Text),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

project_nodes = Table(
    "project_nodes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("library_id", Integer, ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
    Column("parent_id", Integer, ForeignKey("project_nodes.id", ondelete="CASCADE")),
    Column("name", String(255), nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("library_id", "parent_id", "name", name="uq_project_nodes_library_parent_name"),
)

clip_node_refs = Table(
    "clip_node_refs",
    metadata,
    Column("clip_id", Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False),
    Column("node_id", Integer, ForeignKey("project_nodes.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("clip_id", "node_id", name="uq_clip_node_refs_clip_id_node_id"),
)

recycled_clips = Table(
    "recycled_clips",
    metadata,
    Column("clip_id", Integer, ForeignKey("clips.id", ondelete="CASCADE"), primary_key=True),
    Column("library_id", Integer, ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
    Column("deleted_node_ids_json", JSON, nullable=False, default=list),
    Column("deleted_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


TAG_MERGE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("第一人称", ("第一人称", "第一视角", "pov", "主观视角", "手持视角")),
    ("人物口播", ("口播", "说话", "讲话", "采访", "讲解", "真人出镜", "人物出镜", "主播", "vlog")),
    ("人物特写", ("人物特写", "人像", "脸部", "表情", "眼神", "半身", "近景人物")),
    ("人物出镜", ("人物", "自拍", "合影", "合照", "互动", "人物互动", "朋友", "两人", "三人", "四人", "多人", "女生", "男生", "女孩", "男孩", "女子", "男子", "小孩", "情侣", "行人", "人群", "顾客", "游客", "服务员", "背影", "侧脸", "比耶", "笑")),
    ("城市街头", ("街景", "街头", "城市", "街道", "城市街道", "马路", "路边", "店外", "商圈", "夜景", "夜间", "建筑", "楼宇", "人行道", "斑马线", "巷子", "桥", "成都")),
    ("室内商业空间", ("室内", "咖啡店", "餐厅", "餐馆", "店铺", "商店", "室内店", "吧台", "展厅", "酒店", "商场", "办公室空间", "店内", "门店", "餐桌", "座位", "桌椅", "招牌", "理发店", "快餐店", "按摩室", "摊位", "内景", "桌前", "桌下", "宿舍", "床", "窗口", "文字墙", "灯具", "陈设", "菜单", "超市", "走廊", "舞蹈室", "露台")),
    ("自然风景", ("自然", "风景", "山", "海", "湖", "河", "溪", "天空", "云", "日落", "日出", "森林", "草地", "草坪", "公园", "植物", "绿植", "蓝天", "天气", "窗外", "露天", "伞下", "动物")),
    ("交通出行", ("车", "汽车", "地铁", "火车", "公交", "飞机", "机场", "车站", "骑行", "开车", "行驶", "通勤", "乘梯", "扶梯", "自动扶梯", "公路", "交通工具", "舷窗", "接机", "机翼", "机舱", "航空配餐", "线路图", "下行")),
    ("美食饮品", ("美食", "食物", "吃饭", "餐食", "甜品", "咖啡", "饮品", "饮食", "酒", "茶", "烹饪", "厨房", "串串", "火锅", "小吃", "用餐", "餐桌", "菜品", "饮料", "夹菜", "筷子", "粉条", "食材", "冰粉", "可乐", "吃", "就餐", "吃喝", "果盘", "汉堡", "涮菜", "烤串", "烧烤摊", "热油", "煎蛋", "煮", "薯条", "陈麻婆豆腐", "面馆")),
    ("工作学习", ("工作", "办公", "电脑", "键盘", "会议", "学习", "阅读", "书", "笔记", "写字", "创作", "毕业答辩", "校园", "操场")),
    ("居家生活", ("居家", "家里", "客厅", "卧室", "生活方式", "日常", "家居", "收纳", "清洁")),
    ("旅行记录", ("旅行", "旅游", "出游", "景点", "民宿", "酒店", "行李", "旅途", "打卡")),
    ("运动户外", ("运动", "健身", "跑步", "徒步", "爬山", "户外", "露营", "滑雪", "游泳")),
    ("人物动作", ("走动", "行走", "挥手", "拍照", "聊天", "观看", "交流", "交谈", "休息", "准备", "吐槽", "听歌", "唱歌", "手", "手势", "手持", "手舀", "手指", "手部动作", "指", "指示", "操作", "整理头发", "移动", "翻找", "选择", "姿势", "服务", "表演")),
    ("产品物件", ("产品", "商品", "物品", "物件", "设备", "手机", "相机", "包装", "开箱", "特写物", "展示", "卡片", "平板", "屏幕", "打印机", "品牌", "提示牌", "摆放", "旧物", "灯光作品", "照片", "耳机", "胶片", "鞋子", "文字")),
    ("休闲娱乐", ("ktv", "麻将", "聚餐", "综艺", "娱乐", "唱歌", "听歌", "休闲区")),
    ("情绪氛围", ("氛围", "情绪", "治愈", "松弛", "温暖", "孤独", "安静", "热闹", "高级感", "电影感", "暗环境")),
    ("构图景别", ("特写", "近景", "中景", "远景", "全景", "俯拍", "仰拍", "俯瞰", "跟拍", "慢动作", "静态")),
    ("转场空镜", ("空镜", "转场", "过渡", "环境镜头", "建立镜头", "细节镜头", "b-roll", "broll")),
]

TAG_DROP_WORDS = {"视频", "素材", "镜头", "画面", "内容", "场景", "无", "其他"}


def normalize_clip_tag(tag: str) -> str | None:
    """把细碎标签合并成更适合筛选的大类。"""
    cleaned = re.sub(r"\s+", "", str(tag).strip().lower())
    cleaned = cleaned.strip("#＃,，.。;；:：/\\|_-")
    if not cleaned or cleaned in TAG_DROP_WORDS:
        return None

    for canonical, keywords in TAG_MERGE_RULES:
        if cleaned == canonical.lower():
            return canonical
        if any(keyword.lower() in cleaned for keyword in keywords):
            return canonical

    return str(tag).strip()


def normalize_clip_tags(tags: list[str]) -> list[str]:
    """归一化并去重一组标签。"""
    normalized_tags: list[str] = []
    seen_tags: set[str] = set()
    for tag in tags:
        normalized_tag = normalize_clip_tag(tag)
        if not normalized_tag or normalized_tag in seen_tags:
            continue
        seen_tags.add(normalized_tag)
        normalized_tags.append(normalized_tag)
    return normalized_tags


def _hash_password(password: str, *, salt: str | None = None) -> str:
    resolved_salt = salt or base64.urlsafe_b64encode(secrets.token_bytes(16)).decode("ascii")
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        resolved_salt.encode("utf-8"),
        120_000,
    )
    digest = base64.urlsafe_b64encode(derived).decode("ascii")
    return f"pbkdf2_sha256${resolved_salt}${digest}"


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt, digest = password_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    recalculated = _hash_password(password, salt=salt)
    return hmac.compare_digest(recalculated, password_hash)


def _normalize_phone_number(phone_number: str) -> str:
    cleaned = re.sub(r"\D+", "", phone_number.strip())
    if len(cleaned) == 11 and cleaned.startswith("1"):
        return cleaned
    raise ValueError("请输入有效的 11 位手机号。")


def _serialize_db_path(db_path: Path) -> str:
    return str(db_path.resolve())


def get_database_url(db_path: Path = _DB_PATH) -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured:
        return configured
    return f"sqlite:///{_serialize_db_path(db_path)}"


def _is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def _create_engine(database_url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if _is_sqlite_url(database_url):
        connect_args["check_same_thread"] = False

    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 10000")
            try:
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA synchronous = NORMAL")
            except Exception:
                pass
            cursor.close()

    return engine


def get_engine(db_path: Path = _DB_PATH) -> Engine:
    global _ENGINE, _SESSION_FACTORY, _ENGINE_KEY

    database_url = get_database_url(db_path)
    if _ENGINE is None or _ENGINE_KEY != database_url:
        _ENGINE = _create_engine(database_url)
        _SESSION_FACTORY = sessionmaker(bind=_ENGINE, future=True, expire_on_commit=False)
        _ENGINE_KEY = database_url
    return _ENGINE


def _with_retry(action, *, attempts: int = 6, sleep_seconds: float = 0.25):
    last_error: OperationalError | None = None
    for index in range(attempts):
        try:
            return action()
        except OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message and "deadlock" not in message:
                raise
            last_error = exc
            if index == attempts - 1:
                break
            time.sleep(sleep_seconds * (index + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("数据库重试失败，但没有捕获到明确异常。")


def _table_exists(conn: Connection, table_name: str) -> bool:
    if conn.engine.dialect.name == "postgresql":
        return (
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = :table_name"
                ),
                {"table_name": table_name},
            ).first()
            is not None
        )
    return (
        conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name = :table_name"),
            {"table_name": table_name},
        ).first()
        is not None
    )


def _column_names(conn: Connection, table_name: str) -> set[str]:
    if conn.engine.dialect.name == "postgresql":
        rows = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).mappings()
        return {row["column_name"] for row in rows}

    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).mappings()
    return {row["name"] for row in rows}


def _ensure_legacy_columns(conn: Connection) -> None:
    if _table_exists(conn, "users"):
        user_columns = _column_names(conn, "users")
        if "phone_number" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN phone_number TEXT"))
        if "phone_verified_at" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN phone_verified_at DATETIME"))

    if not _table_exists(conn, "clips"):
        return

    existing = _column_names(conn, "clips")
    statements = {
        "library_id": "ALTER TABLE clips ADD COLUMN library_id INTEGER NOT NULL DEFAULT 1",
        "transcript_status": "ALTER TABLE clips ADD COLUMN transcript_status TEXT NOT NULL DEFAULT 'pending'",
        "transcript_error_message": "ALTER TABLE clips ADD COLUMN transcript_error_message TEXT",
        "preview_status": "ALTER TABLE clips ADD COLUMN preview_status TEXT NOT NULL DEFAULT 'pending'",
        "preview_path": "ALTER TABLE clips ADD COLUMN preview_path TEXT",
        "preview_error_message": "ALTER TABLE clips ADD COLUMN preview_error_message TEXT",
        "comparison_status": "ALTER TABLE clips ADD COLUMN comparison_status TEXT NOT NULL DEFAULT 'pending'",
        "comparison_scores_json": "ALTER TABLE clips ADD COLUMN comparison_scores_json TEXT",
        "comparison_error_message": "ALTER TABLE clips ADD COLUMN comparison_error_message TEXT",
        "user_note": "ALTER TABLE clips ADD COLUMN user_note TEXT",
        "source_modified_at": "ALTER TABLE clips ADD COLUMN source_modified_at FLOAT",
        "note_status": "ALTER TABLE clips ADD COLUMN note_status TEXT NOT NULL DEFAULT 'pending'",
        "is_favorite": "ALTER TABLE clips ADD COLUMN is_favorite BOOLEAN NOT NULL DEFAULT 0",
        "rating": "ALTER TABLE clips ADD COLUMN rating INTEGER NOT NULL DEFAULT 0",
    }
    for column_name, ddl in statements.items():
        if column_name not in existing:
            conn.execute(text(ddl))
    conn.execute(
        text(
            "UPDATE clips SET note_status = 'done' "
            "WHERE user_note IS NOT NULL "
            "AND TRIM(user_note) <> '' "
            "AND note_status = 'pending'"
        )
    )

    if _table_exists(conn, "storyboards"):
        storyboard_columns = _column_names(conn, "storyboards")
        if "framework_text" not in storyboard_columns:
            conn.execute(text("ALTER TABLE storyboards ADD COLUMN framework_text TEXT"))

    if _table_exists(conn, "libraries"):
        library_columns = _column_names(conn, "libraries")
        if "owner_user_id" not in library_columns:
            conn.execute(text("ALTER TABLE libraries ADD COLUMN owner_user_id INTEGER"))

    if _table_exists(conn, "clip_cut_segments"):
        cut_columns = _column_names(conn, "clip_cut_segments")
        if "name" not in cut_columns:
            conn.execute(text("ALTER TABLE clip_cut_segments ADD COLUMN name TEXT NOT NULL DEFAULT '未命名片段'"))


def _normalize_project_roots_locked(conn: Connection) -> None:
    library_rows = conn.execute(select(libraries.c.id).order_by(libraries.c.id)).mappings().all()
    for library_row in library_rows:
        library_id = int(library_row["id"])
        root_rows = conn.execute(
            select(project_nodes.c.id)
            .where(
                project_nodes.c.library_id == library_id,
                project_nodes.c.parent_id.is_(None),
            )
            .order_by(project_nodes.c.id)
        ).mappings().all()

        if not root_rows:
            conn.execute(
                insert(project_nodes).values(
                    library_id=library_id,
                    parent_id=None,
                    name=ROOT_NODE_NAME,
                    created_at=_utcnow(),
                )
            )
            continue

        keeper_id = int(root_rows[0]["id"])
        conn.execute(
            update(project_nodes)
            .where(project_nodes.c.id == keeper_id)
            .values(name=ROOT_NODE_NAME)
        )

        for duplicate_row in root_rows[1:]:
            duplicate_id = int(duplicate_row["id"])
            conn.execute(
                update(project_nodes)
                .where(project_nodes.c.parent_id == duplicate_id)
                .values(parent_id=keeper_id)
            )
            duplicate_refs = conn.execute(
                select(clip_node_refs.c.clip_id)
                .where(clip_node_refs.c.node_id == duplicate_id)
            ).mappings().all()
            for ref_row in duplicate_refs:
                _insert_clip_node_ref_if_missing(conn, int(ref_row["clip_id"]), keeper_id)
            conn.execute(delete(clip_node_refs).where(clip_node_refs.c.node_id == duplicate_id))
            conn.execute(delete(project_nodes).where(project_nodes.c.id == duplicate_id))


def _purge_expired_recycled_clips_locked(conn: Connection) -> None:
    expired_rows = conn.execute(
        select(recycled_clips.c.clip_id).where(recycled_clips.c.expires_at <= _utcnow())
    ).mappings().all()
    clip_ids = [int(row["clip_id"]) for row in expired_rows]
    if not clip_ids:
        return

    conn.execute(delete(recycled_clips).where(recycled_clips.c.clip_id.in_(clip_ids)))
    conn.execute(delete(clip_tags).where(clip_tags.c.clip_id.in_(clip_ids)))
    conn.execute(delete(transcripts).where(transcripts.c.clip_id.in_(clip_ids)))
    conn.execute(delete(clip_node_refs).where(clip_node_refs.c.clip_id.in_(clip_ids)))
    conn.execute(delete(clips).where(clips.c.id.in_(clip_ids)))


def _ensure_default_library_locked(conn: Connection) -> None:
    row = conn.execute(
        select(libraries.c.id).where(libraries.c.id == 1)
    ).first()
    if row is None:
        conn.execute(
            insert(libraries).values(
                id=1,
                name=DEFAULT_LIBRARY_NAME,
                created_at=_utcnow(),
            )
        )


def _ensure_default_users_locked(conn: Connection) -> None:
    user_count_row = conn.execute(select(func.count(users.c.id).label("count"))).mappings().first()
    if int(user_count_row["count"] or 0) > 0:
        return

    now = _utcnow()
    default_users = [
        {
            "username": "admin",
            "password": "admin123",
            "role": "admin",
            "display_name": "Administrator",
        },
        {
            "username": "demo",
            "password": "demo123",
            "role": "user",
            "display_name": "Demo User",
        },
    ]
    for item in default_users:
        conn.execute(
            insert(users).values(
                username=item["username"],
                password_hash=_hash_password(item["password"]),
                role=item["role"],
                display_name=item["display_name"],
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )


def _assign_unowned_libraries_to_admin_locked(conn: Connection) -> None:
    admin_row = conn.execute(
        select(users.c.id).where(users.c.role == "admin").order_by(users.c.id.asc()).limit(1)
    ).mappings().first()
    if admin_row is None:
        return
    admin_user_id = int(admin_row["id"])
    conn.execute(
        update(libraries)
        .where(libraries.c.owner_user_id.is_(None))
        .values(owner_user_id=admin_user_id)
    )


def _ensure_root_nodes_locked(conn: Connection) -> None:
    library_rows = conn.execute(select(libraries.c.id)).mappings().all()
    for library_row in library_rows:
        library_id = int(library_row["id"])
        exists_row = conn.execute(
            select(project_nodes.c.id).where(
                project_nodes.c.library_id == library_id,
                project_nodes.c.parent_id.is_(None),
            )
        ).first()
        if exists_row is None:
            conn.execute(
                insert(project_nodes).values(
                    library_id=library_id,
                    parent_id=None,
                    name=ROOT_NODE_NAME,
                    created_at=_utcnow(),
                )
            )


def _insert_clip_node_ref_if_missing(conn: Connection, clip_id: int, node_id: int) -> None:
    existing = conn.execute(
        select(clip_node_refs.c.clip_id).where(
            clip_node_refs.c.clip_id == clip_id,
            clip_node_refs.c.node_id == node_id,
        )
    ).first()
    if existing is None:
        conn.execute(
            insert(clip_node_refs).values(
                clip_id=clip_id,
                node_id=node_id,
                created_at=_utcnow(),
            )
        )


def _ensure_root_refs_locked(conn: Connection) -> None:
    root_rows = conn.execute(
        select(project_nodes.c.id, project_nodes.c.library_id).where(project_nodes.c.parent_id.is_(None))
    ).mappings().all()
    root_map = {int(row["library_id"]): int(row["id"]) for row in root_rows}

    clip_rows = conn.execute(select(clips.c.id, clips.c.library_id)).mappings().all()
    for clip_row in clip_rows:
        clip_id = int(clip_row["id"])
        library_id = int(clip_row["library_id"])
        root_id = root_map.get(library_id)
        if root_id is not None:
            _insert_clip_node_ref_if_missing(conn, clip_id, root_id)


def _normalize_existing_clip_tags_locked(conn: Connection) -> None:
    """把历史标签按当前合并规则重写一遍。"""
    rows = conn.execute(
        select(clip_tags.c.clip_id, clip_tags.c.tag).order_by(clip_tags.c.clip_id.asc(), clip_tags.c.id.asc())
    ).mappings().all()
    if not rows:
        return

    tags_by_clip: dict[int, list[str]] = {}
    for row in rows:
        tags_by_clip.setdefault(int(row["clip_id"]), []).append(str(row["tag"]))

    changed_clip_ids: list[int] = []
    for clip_id, raw_tags in tags_by_clip.items():
        normalized_tags = normalize_clip_tags(raw_tags)
        if normalized_tags == raw_tags:
            continue
        changed_clip_ids.append(clip_id)
        conn.execute(delete(clip_tags).where(clip_tags.c.clip_id == clip_id))
        for tag in normalized_tags:
            conn.execute(insert(clip_tags).values(clip_id=clip_id, tag=tag))

    if changed_clip_ids:
        conn.execute(update(clips).where(clips.c.id.in_(changed_clip_ids)).values(updated_at=_utcnow()))


def init_db(db_path: Path = _DB_PATH) -> Engine:
    engine = get_engine(db_path)

    def _init() -> Engine:
        with _DB_LOCK:
            metadata.create_all(engine)
            with engine.begin() as conn:
                _ensure_legacy_columns(conn)
                _ensure_default_users_locked(conn)
                _ensure_default_library_locked(conn)
                _assign_unowned_libraries_to_admin_locked(conn)
                _ensure_root_nodes_locked(conn)
                _normalize_project_roots_locked(conn)
                _ensure_root_refs_locked(conn)
                _normalize_existing_clip_tags_locked(conn)
                _purge_expired_recycled_clips_locked(conn)
        return engine

    return _with_retry(_init)


def _fetch_all(stmt, params: dict[str, Any] | None = None, db_path: Path = _DB_PATH) -> list[RowMapping]:
    engine = get_engine(db_path)
    with engine.connect() as conn:
        return conn.execute(stmt, params or {}).mappings().all()


def _fetch_one(stmt, params: dict[str, Any] | None = None, db_path: Path = _DB_PATH) -> RowMapping | None:
    engine = get_engine(db_path)
    with engine.connect() as conn:
        return conn.execute(stmt, params or {}).mappings().first()


def _row_to_library(row: RowMapping) -> LibraryRecord:
    return LibraryRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        clip_count=int(row.get("clip_count", 0) or 0),
        pending_note_count=int(row.get("pending_note_count", 0) or 0),
        owner_user_id=int(row["owner_user_id"]) if row.get("owner_user_id") is not None else None,
    )


def _row_to_user(row: RowMapping) -> UserRecord:
    return UserRecord(
        id=int(row["id"]),
        username=str(row["username"]),
        role=str(row["role"]),
        display_name=str(row.get("display_name") or row["username"]),
        is_active=bool(row.get("is_active", True)),
        phone_number=str(row["phone_number"]) if row.get("phone_number") else None,
        phone_verified_at=row.get("phone_verified_at"),
    )


def _project_node_depths(rows: list[RowMapping]) -> dict[int, int]:
    parent_map = {int(row["id"]): (int(row["parent_id"]) if row["parent_id"] is not None else None) for row in rows}
    cache: dict[int, int] = {}

    def _resolve(node_id: int) -> int:
        if node_id in cache:
            return cache[node_id]
        parent_id = parent_map.get(node_id)
        if parent_id is None:
            cache[node_id] = 0
            return 0
        depth = _resolve(parent_id) + 1
        cache[node_id] = depth
        return depth

    for node_id in parent_map:
        _resolve(node_id)
    return cache


def _load_clip_tags_map(clip_ids: list[int], db_path: Path = _DB_PATH) -> dict[int, list[str]]:
    if not clip_ids:
        return {}
    rows = _fetch_all(
        select(clip_tags.c.clip_id, clip_tags.c.tag)
        .where(clip_tags.c.clip_id.in_(clip_ids))
        .order_by(clip_tags.c.id.asc()),
        db_path=db_path,
    )
    tag_map: dict[int, list[str]] = {clip_id: [] for clip_id in clip_ids}
    for row in rows:
        clip_id = int(row["clip_id"])
        tag_map.setdefault(clip_id, []).append(str(row["tag"]))
    return tag_map


def _load_clip_folder_names_map(clip_ids: list[int], db_path: Path = _DB_PATH) -> dict[int, list[str]]:
    if not clip_ids:
        return {}
    rows = _fetch_all(
        select(
            clip_node_refs.c.clip_id,
            project_nodes.c.name,
        )
        .select_from(
            clip_node_refs.join(project_nodes, project_nodes.c.id == clip_node_refs.c.node_id)
        )
        .where(
            clip_node_refs.c.clip_id.in_(clip_ids),
            project_nodes.c.parent_id.is_not(None),
        )
        .order_by(project_nodes.c.id.asc()),
        db_path=db_path,
    )
    folder_map: dict[int, list[str]] = {clip_id: [] for clip_id in clip_ids}
    for row in rows:
        clip_id = int(row["clip_id"])
        folder_map.setdefault(clip_id, []).append(str(row["name"]))
    return folder_map


def list_libraries(
    db_path: Path = _DB_PATH,
    *,
    owner_user_id: int | None = None,
    include_all: bool = True,
) -> list[LibraryRecord]:
    clip_count_subquery = (
        select(
            clips.c.library_id.label("library_id"),
            func.count(func.distinct(clips.c.id)).label("clip_count"),
            func.count(
                func.distinct(
                    case(
                        (clips.c.note_status == "pending", clips.c.id),
                        else_=None,
                    )
                )
            ).label("pending_note_count"),
        )
        .select_from(
            clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id)
        )
        .where(
            clips.c.status == "done",
            recycled_clips.c.clip_id.is_(None),
        )
        .group_by(clips.c.library_id)
        .subquery()
    )

    rows = _fetch_all(
        select(
            libraries.c.id,
            libraries.c.name,
            libraries.c.owner_user_id,
            func.coalesce(clip_count_subquery.c.clip_count, 0).label("clip_count"),
            func.coalesce(clip_count_subquery.c.pending_note_count, 0).label("pending_note_count"),
        )
        .select_from(
            libraries.outerjoin(
                clip_count_subquery,
                clip_count_subquery.c.library_id == libraries.c.id,
            )
        )
        .where(
            literal(True)
            if include_all or owner_user_id is None
            else libraries.c.owner_user_id == owner_user_id
        )
        .order_by(libraries.c.created_at.asc(), libraries.c.id.asc()),
        db_path=db_path,
    )
    return [_row_to_library(row) for row in rows]


def get_library_by_id(
    library_id: int,
    db_path: Path = _DB_PATH,
    *,
    owner_user_id: int | None = None,
    include_all: bool = True,
) -> LibraryRecord | None:
    rows = [
        item
        for item in list_libraries(db_path, owner_user_id=owner_user_id, include_all=include_all)
        if item.id == library_id
    ]
    return rows[0] if rows else None


def get_library_by_name(
    name: str,
    db_path: Path = _DB_PATH,
    *,
    owner_user_id: int | None = None,
    include_all: bool = True,
) -> LibraryRecord | None:
    row = _fetch_one(
        select(libraries.c.id, libraries.c.name).where(libraries.c.name == name.strip()),
        db_path=db_path,
    )
    if row is None:
        return None
    library = get_library_by_id(
        int(row["id"]),
        db_path,
        owner_user_id=owner_user_id,
        include_all=include_all,
    )
    return library


def create_library(name: str, db_path: Path = _DB_PATH, *, owner_user_id: int | None = None) -> LibraryRecord:
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("素材库名称不能为空")

    engine = get_engine(db_path)

    def _write() -> LibraryRecord:
        with _DB_LOCK:
            now = _utcnow()
            with engine.begin() as conn:
                result = conn.execute(
                    insert(libraries).values(name=cleaned_name, owner_user_id=owner_user_id, created_at=now)
                )
                library_id = int(result.inserted_primary_key[0])
                conn.execute(
                    insert(project_nodes).values(
                        library_id=library_id,
                        parent_id=None,
                        name=ROOT_NODE_NAME,
                        created_at=now,
                    )
                )
            library = get_library_by_id(library_id, db_path, include_all=True)
            if library is None:
                raise ValueError("素材库创建失败")
            return library

    try:
        return _with_retry(_write)
    except IntegrityError as exc:
        raise ValueError("素材库名称已存在") from exc


def rename_library(library_id: int, new_name: str, db_path: Path = _DB_PATH) -> LibraryRecord:
    cleaned_name = new_name.strip()
    if not cleaned_name:
        raise ValueError("素材库名称不能为空")
    if library_id == 1:
        raise ValueError("默认素材库不支持重命名")

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    update(libraries)
                    .where(libraries.c.id == library_id)
                    .values(name=cleaned_name)
                )
                if result.rowcount == 0:
                    raise ValueError("素材库不存在")

    try:
        _with_retry(_write)
    except IntegrityError as exc:
        raise ValueError("素材库名称已存在") from exc

    library = get_library_by_id(library_id, db_path)
    if library is None:
        raise ValueError("素材库不存在")
    return library


def delete_library(library_id: int, db_path: Path = _DB_PATH) -> None:
    if library_id == 1:
        raise ValueError("默认素材库不支持删除")

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                count_row = conn.execute(
                    select(func.count(func.distinct(clips.c.id)).label("count"))
                    .select_from(
                        clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id)
                    )
                    .where(
                        clips.c.library_id == library_id,
                        recycled_clips.c.clip_id.is_(None),
                    )
                ).mappings().first()
                if count_row is None:
                    raise ValueError("素材库不存在")
                if int(count_row["count"] or 0) > 0:
                    raise ValueError("素材库里还有素材，不能直接删除")

                result = conn.execute(delete(libraries).where(libraries.c.id == library_id))
                if result.rowcount == 0:
                    raise ValueError("素材库不存在")

    _with_retry(_write)


def delete_clips(clip_ids: list[int], library_id: int, db_path: Path = _DB_PATH) -> int:
    if not clip_ids:
        return 0

    engine = get_engine(db_path)

    def _write() -> int:
        with _DB_LOCK:
            with engine.begin() as conn:
                rows = conn.execute(
                    select(clips.c.id)
                    .select_from(
                        clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id)
                    )
                    .where(
                        clips.c.library_id == library_id,
                        clips.c.id.in_(clip_ids),
                        recycled_clips.c.clip_id.is_(None),
                    )
                ).mappings().all()
                target_ids = [int(row["id"]) for row in rows]
                if not target_ids:
                    return 0

                deleted_node_rows = conn.execute(
                    select(clip_node_refs.c.clip_id, clip_node_refs.c.node_id)
                    .where(clip_node_refs.c.clip_id.in_(target_ids))
                ).mappings().all()
                deleted_node_map: dict[int, list[int]] = {}
                for row in deleted_node_rows:
                    deleted_node_map.setdefault(int(row["clip_id"]), []).append(int(row["node_id"]))

                now = _utcnow()
                expires_at = now + timedelta(days=7)
                for clip_id in target_ids:
                    existing = conn.execute(
                        select(recycled_clips.c.clip_id).where(recycled_clips.c.clip_id == clip_id)
                    ).first()
                    values = {
                        "clip_id": clip_id,
                        "library_id": library_id,
                        "deleted_node_ids_json": deleted_node_map.get(clip_id, []),
                        "deleted_at": now,
                        "expires_at": expires_at,
                    }
                    if existing is None:
                        conn.execute(insert(recycled_clips).values(**values))
                    else:
                        conn.execute(
                            update(recycled_clips)
                            .where(recycled_clips.c.clip_id == clip_id)
                            .values(**values)
                        )

                conn.execute(delete(clip_node_refs).where(clip_node_refs.c.clip_id.in_(target_ids)))
                return len(target_ids)

    return _with_retry(_write)


def clip_exists_in_library_by_hash(video_hash: str, library_id: int, db_path: Path = _DB_PATH) -> bool:
    """判断当前素材库是否已有同一内容的视频。"""
    row = _fetch_one(
        select(clips.c.id)
        .select_from(clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id))
        .where(
            clips.c.video_hash == video_hash,
            clips.c.library_id == library_id,
            recycled_clips.c.clip_id.is_(None),
        )
        .limit(1),
        db_path=db_path,
    )
    return row is not None


def clip_exists_by_hash(video_hash: str, db_path: Path = _DB_PATH) -> bool:
    """判断数据库中是否已有同一内容的视频。"""
    row = _fetch_one(
        select(clips.c.id)
        .select_from(clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id))
        .where(
            clips.c.video_hash == video_hash,
            recycled_clips.c.clip_id.is_(None),
        )
        .limit(1),
        db_path=db_path,
    )
    return row is not None


def save_clip(record: ClipRecord, db_path: Path = _DB_PATH) -> int:
    engine = get_engine(db_path)

    def _write() -> int:
        with _DB_LOCK:
            with engine.begin() as conn:
                now = _utcnow()
                existing_row = conn.execute(
                    select(clips.c.id, clips.c.user_note)
                    .where(clips.c.video_hash == record.video_hash)
                ).mappings().first()

                values = {
                    "video_hash": record.video_hash,
                    "library_id": record.library_id,
                    "filename": record.filename,
                    "filepath": str(record.filepath),
                    "summary": record.summary,
                    "scene": record.scene,
                    "subjects_json": record.subjects or [],
                    "actions_json": record.actions or [],
                    "has_motion": record.has_motion,
                    "sharpness_score": record.sharpness_score,
                    "cover_path": str(record.cover_path) if record.cover_path else None,
                    "status": record.status,
                    "error_message": record.error_message,
                    "transcript_status": record.transcript_status,
                    "transcript_error_message": record.transcript_error_message,
                    "preview_status": record.preview_status,
                    "preview_path": str(record.preview_path) if record.preview_path else None,
                    "preview_error_message": record.preview_error_message,
                    "comparison_status": record.comparison_status,
                    "comparison_scores_json": record.comparison_scores_json,
                    "comparison_error_message": record.comparison_error_message,
                    "user_note": record.user_note if record.user_note is not None else (existing_row["user_note"] if existing_row else None),
                    "source_modified_at": record.source_modified_at,
                    "note_status": record.note_status,
                    "updated_at": now,
                }

                if existing_row is None:
                    values["created_at"] = now
                    result = conn.execute(insert(clips).values(**values))
                    clip_id = int(result.inserted_primary_key[0])
                else:
                    clip_id = int(existing_row["id"])
                    conn.execute(
                        update(clips)
                        .where(clips.c.id == clip_id)
                        .values(**values)
                    )

                conn.execute(delete(clip_tags).where(clip_tags.c.clip_id == clip_id))
                seen_tags: set[str] = set()
                for tag in normalize_clip_tags(record.tags or []):
                    if tag in seen_tags:
                        continue
                    seen_tags.add(tag)
                    conn.execute(insert(clip_tags).values(clip_id=clip_id, tag=tag))

                root_row = conn.execute(
                    select(project_nodes.c.id).where(
                        project_nodes.c.library_id == record.library_id,
                        project_nodes.c.parent_id.is_(None),
                    )
                ).mappings().first()
                if root_row is not None:
                    _insert_clip_node_ref_if_missing(conn, clip_id, int(root_row["id"]))

                return clip_id

    return _with_retry(_write)


def save_transcripts(records: list[TranscriptRecord], db_path: Path = _DB_PATH) -> None:
    if not records:
        return

    engine = get_engine(db_path)
    clip_id = records[0].clip_id

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(delete(transcripts).where(transcripts.c.clip_id == clip_id))
                for record in records:
                    conn.execute(
                        insert(transcripts).values(
                            clip_id=record.clip_id,
                            start_ms=record.start_ms,
                            end_ms=record.end_ms,
                            text=record.text,
                            segment_index=record.segment_index,
                        )
                    )

    _with_retry(_write)


def load_transcripts(clip_id: int, db_path: Path = _DB_PATH) -> list[TranscriptRecord]:
    rows = _fetch_all(
        select(
            transcripts.c.clip_id,
            transcripts.c.start_ms,
            transcripts.c.end_ms,
            transcripts.c.text,
            transcripts.c.segment_index,
        )
        .where(transcripts.c.clip_id == clip_id)
        .order_by(transcripts.c.segment_index.asc(), transcripts.c.start_ms.asc()),
        db_path=db_path,
    )
    return [
        TranscriptRecord(
            clip_id=int(row["clip_id"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            text=str(row["text"]),
            segment_index=int(row["segment_index"]),
        )
        for row in rows
    ]


def update_clip_summary_and_tags(
    clip_id: int,
    library_id: int,
    summary: str,
    new_tags: list[str],
    db_path: Path = _DB_PATH,
) -> None:
    update_clip_summary(clip_id, library_id, summary, db_path)
    if new_tags:
        append_clip_tags(clip_id, new_tags, db_path)


def update_clip_summary(
    clip_id: int,
    library_id: int,
    summary: str,
    db_path: Path = _DB_PATH,
) -> None:
    cleaned_summary = summary.strip()
    if not cleaned_summary:
        raise ValueError("摘要不能为空")

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                duplicate = conn.execute(
                    select(clips.c.id)
                    .where(
                        clips.c.library_id == library_id,
                        clips.c.summary == cleaned_summary,
                        clips.c.id != clip_id,
                    )
                    .limit(1)
                ).first()
                if duplicate is not None:
                    raise ValueError("同一素材库中摘要不能重名")

                result = conn.execute(
                    update(clips)
                    .where(clips.c.id == clip_id, clips.c.library_id == library_id)
                    .values(summary=cleaned_summary, updated_at=_utcnow())
                )
                if result.rowcount == 0:
                    raise ValueError("素材不存在")

    _with_retry(_write)


def append_clip_tags(
    clip_id: int,
    new_tags: list[str],
    db_path: Path = _DB_PATH,
) -> None:
    cleaned_tags = normalize_clip_tags(new_tags)

    if not cleaned_tags:
        raise ValueError("请至少填写一个标签")

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                clip_row = conn.execute(select(clips.c.id).where(clips.c.id == clip_id)).first()
                if clip_row is None:
                    raise ValueError("素材不存在")

                existing_tags = {
                    str(row["tag"])
                    for row in conn.execute(
                        select(clip_tags.c.tag).where(clip_tags.c.clip_id == clip_id)
                    ).mappings()
                }
                for tag in cleaned_tags:
                    if tag not in existing_tags:
                        conn.execute(insert(clip_tags).values(clip_id=clip_id, tag=tag))
                conn.execute(
                    update(clips).where(clips.c.id == clip_id).values(updated_at=_utcnow())
                )

    _with_retry(_write)


def update_clip_note(
    clip_id: int,
    note: str,
    db_path: Path = _DB_PATH,
) -> None:
    cleaned_note = note.strip()
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    update(clips)
                    .where(clips.c.id == clip_id)
                    .values(
                        user_note=cleaned_note or None,
                        note_status="done" if cleaned_note else "pending",
                        updated_at=_utcnow(),
                    )
                )
                if result.rowcount == 0:
                    raise ValueError("素材不存在")

    _with_retry(_write)


def update_clip_note_status(
    clip_id: int,
    note_status: str,
    db_path: Path = _DB_PATH,
) -> None:
    """更新素材的创作备注状态。"""
    cleaned_status = note_status.strip()
    if cleaned_status not in {"pending", "done", "skipped"}:
        raise ValueError("备注状态无效")

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    update(clips)
                    .where(clips.c.id == clip_id)
                    .values(note_status=cleaned_status, updated_at=_utcnow())
                )
                if result.rowcount == 0:
                    raise ValueError("素材不存在")

    _with_retry(_write)


def update_clip_review(
    clip_id: int,
    *,
    is_favorite: bool | None = None,
    rating: int | None = None,
    db_path: Path = _DB_PATH,
) -> None:
    """更新素材的人工收藏和等级。"""
    values: dict[str, Any] = {"updated_at": _utcnow()}
    if is_favorite is not None:
        values["is_favorite"] = bool(is_favorite)
    if rating is not None:
        values["rating"] = max(0, min(int(rating), 5))
    if len(values) == 1:
        return

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(update(clips).where(clips.c.id == clip_id).values(**values))
                if result.rowcount == 0:
                    raise ValueError("素材不存在")

    _with_retry(_write)


def list_clip_cut_segments(clip_id: int, db_path: Path = _DB_PATH) -> list[ClipCutSegmentRecord]:
    rows = _fetch_all(
        select(
            clip_cut_segments.c.id,
            clip_cut_segments.c.clip_id,
            clip_cut_segments.c.name,
            clip_cut_segments.c.start_ms,
            clip_cut_segments.c.end_ms,
            clip_cut_segments.c.note,
            clip_cut_segments.c.exported_path,
        )
        .where(clip_cut_segments.c.clip_id == clip_id)
        .order_by(clip_cut_segments.c.start_ms.asc(), clip_cut_segments.c.id.asc()),
        db_path=db_path,
    )
    return [
        ClipCutSegmentRecord(
            id=int(row["id"]),
            clip_id=int(row["clip_id"]),
            name=str(row.get("name") or "未命名片段"),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            note=str(row["note"]) if row.get("note") else None,
            exported_path=Path(str(row["exported_path"])) if row.get("exported_path") else None,
        )
        for row in rows
    ]


def get_clip_cut_segment(segment_id: int, db_path: Path = _DB_PATH) -> ClipCutSegmentRecord | None:
    row = _fetch_one(
        select(
            clip_cut_segments.c.id,
            clip_cut_segments.c.clip_id,
            clip_cut_segments.c.name,
            clip_cut_segments.c.start_ms,
            clip_cut_segments.c.end_ms,
            clip_cut_segments.c.note,
            clip_cut_segments.c.exported_path,
        ).where(clip_cut_segments.c.id == segment_id),
        db_path=db_path,
    )
    if row is None:
        return None
    return ClipCutSegmentRecord(
        id=int(row["id"]),
        clip_id=int(row["clip_id"]),
        name=str(row.get("name") or "未命名片段"),
        start_ms=int(row["start_ms"]),
        end_ms=int(row["end_ms"]),
        note=str(row["note"]) if row.get("note") else None,
        exported_path=Path(str(row["exported_path"])) if row.get("exported_path") else None,
    )


def create_clip_cut_segment(
    clip_id: int,
    name: str,
    start_ms: int,
    end_ms: int,
    note: str,
    db_path: Path = _DB_PATH,
) -> ClipCutSegmentRecord:
    if start_ms < 0:
        raise ValueError("入点不能小于 0")
    if end_ms <= start_ms:
        raise ValueError("出点必须晚于入点")

    cleaned_name = name.strip() or "未命名片段"
    cleaned_note = note.strip()
    engine = get_engine(db_path)

    def _write() -> int:
        with _DB_LOCK:
            with engine.begin() as conn:
                clip_row = conn.execute(select(clips.c.id).where(clips.c.id == clip_id)).first()
                if clip_row is None:
                    raise ValueError("素材不存在")
                now = _utcnow()
                result = conn.execute(
                    insert(clip_cut_segments).values(
                        clip_id=clip_id,
                        name=cleaned_name,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        note=cleaned_note or None,
                        exported_path=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return int(result.inserted_primary_key[0])

    segment_id = _with_retry(_write)
    segment = get_clip_cut_segment(segment_id, db_path)
    if segment is None:
        raise ValueError("粗剪片段创建失败")
    return segment


def delete_clip_cut_segment(segment_id: int, db_path: Path = _DB_PATH) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(delete(clip_cut_segments).where(clip_cut_segments.c.id == segment_id))
                if result.rowcount == 0:
                    raise ValueError("粗剪片段不存在")

    _with_retry(_write)


def update_clip_cut_segment_range(
    segment_id: int,
    start_ms: int,
    end_ms: int,
    db_path: Path = _DB_PATH,
) -> None:
    """更新粗剪片段的入点和出点。"""
    if start_ms < 0:
        raise ValueError("入点不能小于 0")
    if end_ms <= start_ms:
        raise ValueError("出点必须晚于入点")

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    update(clip_cut_segments)
                    .where(clip_cut_segments.c.id == segment_id)
                    .values(start_ms=start_ms, end_ms=end_ms, updated_at=_utcnow())
                )
                if result.rowcount == 0:
                    raise ValueError("粗剪片段不存在")

    _with_retry(_write)


def update_clip_cut_segment_export_path(
    segment_id: int,
    exported_path: Path,
    db_path: Path = _DB_PATH,
) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    update(clip_cut_segments)
                    .where(clip_cut_segments.c.id == segment_id)
                    .values(exported_path=str(exported_path), updated_at=_utcnow())
                )
                if result.rowcount == 0:
                    raise ValueError("粗剪片段不存在")

    _with_retry(_write)


def _row_to_storyboard(row: RowMapping) -> StoryboardRecord:
    return StoryboardRecord(
        id=int(row["id"]),
        library_id=int(row["library_id"]),
        title=str(row["title"]),
        brief_text=str(row["brief_text"]),
        target_duration_seconds=int(row["target_duration_seconds"]),
        tone_prompt=str(row["tone_prompt"]),
        selected_clip_ids=[int(item) for item in (row["selected_clip_ids_json"] or [])],
        framework_text=str(row["framework_text"]) if row.get("framework_text") else None,
        core_message=str(row["core_message"]) if row.get("core_message") else None,
        emotional_arc_json=[str(item) for item in (row["emotional_arc_json"] or [])],
        story_plan=str(row["story_plan"]) if row.get("story_plan") else None,
        script_text=str(row["script_text"]) if row.get("script_text") else None,
        revision_prompt=str(row["revision_prompt"]) if row.get("revision_prompt") else None,
        status=str(row.get("status") or "draft"),
        error_message=str(row["error_message"]) if row.get("error_message") else None,
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
    )


def create_storyboard(
    *,
    library_id: int,
    title: str,
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str,
    selected_clip_ids: list[int],
    db_path: Path = _DB_PATH,
) -> StoryboardRecord:
    """创建一次素材库级故事线方案。"""
    cleaned_brief = brief_text.strip()
    if not cleaned_brief:
        raise ValueError("请先填写这条视频想表达的东西。")
    cleaned_title = title.strip() or "未命名故事线"
    cleaned_tone = tone_prompt.strip() or "第一人称、口语化、像真实 vlog 旁白，不要鸡汤，不要广告腔。"
    safe_duration = max(15, min(int(target_duration_seconds), 1200))
    unique_clip_ids = list(dict.fromkeys(int(item) for item in selected_clip_ids if int(item) > 0))
    if not unique_clip_ids:
        raise ValueError("请至少选择一条素材。")

    engine = get_engine(db_path)

    def _write() -> int:
        with _DB_LOCK:
            with engine.begin() as conn:
                library_row = conn.execute(select(libraries.c.id).where(libraries.c.id == library_id)).first()
                if library_row is None:
                    raise ValueError("素材库不存在。")

                valid_count_row = conn.execute(
                    select(func.count(clips.c.id).label("count"))
                    .select_from(clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id))
                    .where(
                        clips.c.library_id == library_id,
                        clips.c.id.in_(unique_clip_ids),
                        clips.c.status == "done",
                        recycled_clips.c.clip_id.is_(None),
                    )
                ).mappings().first()
                if int(valid_count_row["count"] or 0) == 0:
                    raise ValueError("选中的素材不属于当前素材库，或还不可用于生成故事线。")

                now = _utcnow()
                result = conn.execute(
                    insert(storyboards).values(
                        library_id=library_id,
                        title=cleaned_title,
                        brief_text=cleaned_brief,
                        target_duration_seconds=safe_duration,
                        tone_prompt=cleaned_tone,
                        selected_clip_ids_json=unique_clip_ids,
                        emotional_arc_json=[],
                        status="framework_pending",
                        created_at=now,
                        updated_at=now,
                    )
                )
                return int(result.inserted_primary_key[0])

    storyboard_id = _with_retry(_write)
    storyboard = get_storyboard(storyboard_id, db_path)
    if storyboard is None:
        raise ValueError("故事线创建失败。")
    return storyboard


def update_storyboard_status(
    storyboard_id: int,
    status: str,
    *,
    revision_prompt: str | None = None,
    target_duration_seconds: int | None = None,
    tone_prompt: str | None = None,
    error_message: str | None = None,
    db_path: Path = _DB_PATH,
) -> None:
    """更新故事线生成阶段。"""
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                values: dict[str, Any] = {
                    "status": status,
                    "error_message": error_message,
                    "updated_at": _utcnow(),
                }
                if revision_prompt is not None:
                    values["revision_prompt"] = revision_prompt.strip() or None
                if target_duration_seconds is not None:
                    values["target_duration_seconds"] = max(15, min(int(target_duration_seconds), 1200))
                if tone_prompt is not None:
                    values["tone_prompt"] = tone_prompt.strip() or "第一人称、口语化、像真实 vlog 旁白，不要鸡汤，不要广告腔。"
                result = conn.execute(
                    update(storyboards)
                    .where(storyboards.c.id == storyboard_id)
                    .values(**values)
                )
                if result.rowcount == 0:
                    raise ValueError("故事线不存在。")

    _with_retry(_write)


def update_storyboard_framework(
    *,
    storyboard_id: int,
    title: str,
    core_message: str,
    emotional_arc: list[str],
    framework_text: str,
    revision_prompt: str | None = None,
    db_path: Path = _DB_PATH,
) -> None:
    """保存待确认的叙事框架。"""
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    update(storyboards)
                    .where(storyboards.c.id == storyboard_id)
                    .values(
                        title=title.strip() or "未命名故事线",
                        core_message=core_message.strip(),
                        emotional_arc_json=[item.strip() for item in emotional_arc if item.strip()],
                        framework_text=framework_text.strip(),
                        revision_prompt=revision_prompt.strip() if revision_prompt else None,
                        status="framework_done",
                        error_message=None,
                        updated_at=_utcnow(),
                    )
                )
                if result.rowcount == 0:
                    raise ValueError("故事线不存在。")

    _with_retry(_write)


def update_storyboard_result(
    *,
    storyboard_id: int,
    title: str,
    core_message: str,
    emotional_arc: list[str],
    story_plan: str,
    script_text: str,
    items: list[StoryboardItemRecord],
    revision_prompt: str | None = None,
    db_path: Path = _DB_PATH,
) -> None:
    """保存 AI 生成或修改后的故事线结果。"""
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                now = _utcnow()
                result = conn.execute(
                    update(storyboards)
                    .where(storyboards.c.id == storyboard_id)
                    .values(
                        title=title.strip() or "未命名故事线",
                        core_message=core_message.strip(),
                        emotional_arc_json=[item.strip() for item in emotional_arc if item.strip()],
                        story_plan=story_plan.strip(),
                        script_text=script_text.strip(),
                        revision_prompt=revision_prompt.strip() if revision_prompt else None,
                        status="done",
                        error_message=None,
                        updated_at=now,
                    )
                )
                if result.rowcount == 0:
                    raise ValueError("故事线不存在。")

                conn.execute(delete(storyboard_items).where(storyboard_items.c.storyboard_id == storyboard_id))
                for item in sorted(items, key=lambda record: record.position):
                    conn.execute(
                        insert(storyboard_items).values(
                            storyboard_id=storyboard_id,
                            clip_id=item.clip_id,
                            position=item.position,
                            section_name=item.section_name,
                            narrative_role=item.narrative_role,
                            suggested_duration_seconds=max(1, int(item.suggested_duration_seconds)),
                            script_line=item.script_line,
                            reason=item.reason,
                            created_at=now,
                            updated_at=now,
                        )
                    )

    _with_retry(_write)


def update_storyboard_script(
    *,
    storyboard_id: int,
    script_text: str,
    db_path: Path = _DB_PATH,
) -> None:
    """保存用户手动编辑后的完整脚本。"""
    cleaned_script = script_text.strip()
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    update(storyboards)
                    .where(storyboards.c.id == storyboard_id)
                    .values(
                        script_text=cleaned_script,
                        status="done",
                        error_message=None,
                        updated_at=_utcnow(),
                    )
                )
                if result.rowcount == 0:
                    raise ValueError("故事线不存在。")

    _with_retry(_write)


def update_storyboard_error(storyboard_id: int, error_message: str, db_path: Path = _DB_PATH) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    update(storyboards)
                    .where(storyboards.c.id == storyboard_id)
                    .values(status="failed", error_message=error_message, updated_at=_utcnow())
                )

    _with_retry(_write)


def get_storyboard(storyboard_id: int, db_path: Path = _DB_PATH) -> StoryboardRecord | None:
    row = _fetch_one(
        select(
            storyboards.c.id,
            storyboards.c.library_id,
            storyboards.c.title,
            storyboards.c.brief_text,
            storyboards.c.target_duration_seconds,
            storyboards.c.tone_prompt,
            storyboards.c.selected_clip_ids_json,
            storyboards.c.framework_text,
            storyboards.c.core_message,
            storyboards.c.emotional_arc_json,
            storyboards.c.story_plan,
            storyboards.c.script_text,
            storyboards.c.revision_prompt,
            storyboards.c.status,
            storyboards.c.error_message,
            storyboards.c.created_at,
            storyboards.c.updated_at,
        ).where(storyboards.c.id == storyboard_id),
        db_path=db_path,
    )
    return _row_to_storyboard(row) if row is not None else None


def list_storyboards(
    *,
    library_ids: list[int] | None = None,
    limit: int = 20,
    db_path: Path = _DB_PATH,
) -> list[StoryboardRecord]:
    """按素材库列出故事项目。"""
    filters: list[Any] = []
    if library_ids is not None:
        safe_ids = [int(item) for item in library_ids if int(item) > 0]
        if not safe_ids:
            return []
        filters.append(storyboards.c.library_id.in_(safe_ids))
    statement = select(
        storyboards.c.id,
        storyboards.c.library_id,
        storyboards.c.title,
        storyboards.c.brief_text,
        storyboards.c.target_duration_seconds,
        storyboards.c.tone_prompt,
        storyboards.c.selected_clip_ids_json,
        storyboards.c.framework_text,
        storyboards.c.core_message,
        storyboards.c.emotional_arc_json,
        storyboards.c.story_plan,
        storyboards.c.script_text,
        storyboards.c.revision_prompt,
        storyboards.c.status,
        storyboards.c.error_message,
        storyboards.c.created_at,
        storyboards.c.updated_at,
    ).order_by(storyboards.c.updated_at.desc(), storyboards.c.id.desc())
    if filters:
        statement = statement.where(and_(*filters))
    if limit > 0:
        statement = statement.limit(limit)
    rows = _fetch_all(statement, db_path=db_path)
    return [_row_to_storyboard(row) for row in rows]


def list_storyboard_items(storyboard_id: int, db_path: Path = _DB_PATH) -> list[StoryboardItemRecord]:
    rows = _fetch_all(
        select(
            storyboard_items.c.id,
            storyboard_items.c.storyboard_id,
            storyboard_items.c.clip_id,
            storyboard_items.c.position,
            storyboard_items.c.section_name,
            storyboard_items.c.narrative_role,
            storyboard_items.c.suggested_duration_seconds,
            storyboard_items.c.script_line,
            storyboard_items.c.reason,
        )
        .where(storyboard_items.c.storyboard_id == storyboard_id)
        .order_by(storyboard_items.c.position.asc(), storyboard_items.c.id.asc()),
        db_path=db_path,
    )
    return [
        StoryboardItemRecord(
            id=int(row["id"]),
            storyboard_id=int(row["storyboard_id"]),
            clip_id=int(row["clip_id"]),
            position=int(row["position"]),
            section_name=str(row["section_name"]),
            narrative_role=str(row["narrative_role"]),
            suggested_duration_seconds=int(row["suggested_duration_seconds"]),
            script_line=str(row["script_line"]) if row.get("script_line") else None,
            reason=str(row["reason"]) if row.get("reason") else None,
        )
        for row in rows
    ]


def add_storyboard_message(
    *,
    storyboard_id: int,
    role: str,
    content: str,
    reasoning_text: str | None = None,
    action_json: dict[str, Any] | None = None,
    db_path: Path = _DB_PATH,
) -> StoryboardMessageRecord:
    """保存一条导演 Agent 对话消息。"""
    cleaned_role = role.strip()
    cleaned_content = content.strip()
    if cleaned_role not in {"user", "assistant", "system"}:
        raise ValueError("消息角色无效。")
    if not cleaned_content:
        raise ValueError("消息内容不能为空。")
    engine = get_engine(db_path)

    def _write() -> int:
        with _DB_LOCK:
            with engine.begin() as conn:
                now = _utcnow()
                result = conn.execute(
                    insert(storyboard_messages).values(
                        storyboard_id=storyboard_id,
                        role=cleaned_role,
                        content=cleaned_content,
                        reasoning_text=reasoning_text.strip() if reasoning_text else None,
                        action_json=action_json,
                        created_at=now,
                    )
                )
                return int(result.inserted_primary_key[0])

    message_id = _with_retry(_write)
    messages = list_storyboard_messages(storyboard_id, db_path)
    for message in messages:
        if message.id == message_id:
            return message
    raise ValueError("消息保存失败。")


def list_storyboard_messages(storyboard_id: int, db_path: Path = _DB_PATH) -> list[StoryboardMessageRecord]:
    """读取导演 Agent 对话历史。"""
    rows = _fetch_all(
        select(
            storyboard_messages.c.id,
            storyboard_messages.c.storyboard_id,
            storyboard_messages.c.role,
            storyboard_messages.c.content,
            storyboard_messages.c.reasoning_text,
            storyboard_messages.c.action_json,
            storyboard_messages.c.created_at,
        )
        .where(storyboard_messages.c.storyboard_id == storyboard_id)
        .order_by(storyboard_messages.c.id.asc()),
        db_path=db_path,
    )
    return [
        StoryboardMessageRecord(
            id=int(row["id"]),
            storyboard_id=int(row["storyboard_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            reasoning_text=str(row["reasoning_text"]) if row.get("reasoning_text") else None,
            action_json=dict(row["action_json"]) if row.get("action_json") else None,
            created_at=str(row["created_at"]) if row.get("created_at") else None,
        )
        for row in rows
    ]


def create_storyboard_run(
    *,
    storyboard_id: int,
    run_type: str,
    db_path: Path = _DB_PATH,
) -> int:
    """记录一次 Agent 调用。"""
    engine = get_engine(db_path)

    def _write() -> int:
        with _DB_LOCK:
            with engine.begin() as conn:
                now = _utcnow()
                result = conn.execute(
                    insert(storyboard_runs).values(
                        storyboard_id=storyboard_id,
                        run_type=run_type,
                        status="running",
                        created_at=now,
                        updated_at=now,
                    )
                )
                return int(result.inserted_primary_key[0])

    return _with_retry(_write)


def update_storyboard_run(
    run_id: int,
    *,
    status: str,
    reasoning_text: str | None = None,
    output_text: str | None = None,
    error_message: str | None = None,
    db_path: Path = _DB_PATH,
) -> None:
    """更新 Agent 调用状态。"""
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    update(storyboard_runs)
                    .where(storyboard_runs.c.id == run_id)
                    .values(
                        status=status,
                        reasoning_text=reasoning_text.strip() if reasoning_text else None,
                        output_text=output_text.strip() if output_text else None,
                        error_message=error_message.strip() if error_message else None,
                        updated_at=_utcnow(),
                    )
                )

    _with_retry(_write)


def list_project_nodes(library_id: int, db_path: Path = _DB_PATH) -> list[ProjectNodeRecord]:
    node_rows = _fetch_all(
        select(
            project_nodes.c.id,
            project_nodes.c.library_id,
            project_nodes.c.parent_id,
            project_nodes.c.name,
        )
        .where(project_nodes.c.library_id == library_id)
        .order_by(project_nodes.c.id.asc()),
        db_path=db_path,
    )
    if not node_rows:
        return []

    depth_map = _project_node_depths(node_rows)
    count_rows = _fetch_all(
        select(
            clip_node_refs.c.node_id,
            func.count(func.distinct(clip_node_refs.c.clip_id)).label("clip_count"),
        )
        .select_from(
            clip_node_refs.outerjoin(recycled_clips, recycled_clips.c.clip_id == clip_node_refs.c.clip_id)
        )
        .where(
            clip_node_refs.c.node_id.in_([int(row["id"]) for row in node_rows]),
            recycled_clips.c.clip_id.is_(None),
        )
        .group_by(clip_node_refs.c.node_id),
        db_path=db_path,
    )
    count_map = {int(row["node_id"]): int(row["clip_count"] or 0) for row in count_rows}

    ordered_rows = sorted(
        node_rows,
        key=lambda row: (depth_map[int(row["id"])], int(row["parent_id"]) if row["parent_id"] is not None else -1, int(row["id"])),
    )
    return [
        ProjectNodeRecord(
            id=int(row["id"]),
            library_id=int(row["library_id"]),
            parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
            name=str(row["name"]),
            depth=depth_map[int(row["id"])],
            clip_count=count_map.get(int(row["id"]), 0),
        )
        for row in ordered_rows
    ]


def get_project_node(node_id: int, db_path: Path = _DB_PATH) -> ProjectNodeRecord | None:
    row = _fetch_one(
        select(
            project_nodes.c.id,
            project_nodes.c.library_id,
            project_nodes.c.parent_id,
            project_nodes.c.name,
        ).where(project_nodes.c.id == node_id),
        db_path=db_path,
    )
    if row is None:
        return None

    nodes = list_project_nodes(int(row["library_id"]), db_path)
    for node in nodes:
        if node.id == node_id:
            return node
    return None


def create_project_node(
    library_id: int,
    name: str,
    parent_id: int | None = None,
    db_path: Path = _DB_PATH,
) -> ProjectNodeRecord:
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("文件夹名称不能为空")

    engine = get_engine(db_path)

    def _write() -> ProjectNodeRecord:
        with _DB_LOCK:
            with engine.begin() as conn:
                if parent_id is not None:
                    parent_row = conn.execute(
                        select(project_nodes.c.id).where(
                            project_nodes.c.id == parent_id,
                            project_nodes.c.library_id == library_id,
                        )
                    ).first()
                    if parent_row is None:
                        raise ValueError("父文件夹不存在")

                try:
                    result = conn.execute(
                        insert(project_nodes).values(
                            library_id=library_id,
                            parent_id=parent_id,
                            name=cleaned_name,
                            created_at=_utcnow(),
                        )
                    )
                except IntegrityError as exc:
                    raise ValueError("同级文件夹名称已存在") from exc

                node_id = int(result.inserted_primary_key[0])

            node = get_project_node(node_id, db_path)
            if node is None:
                raise ValueError("文件夹创建失败")
            return node

    return _with_retry(_write)


def rename_project_node(
    node_id: int,
    library_id: int,
    new_name: str,
    db_path: Path = _DB_PATH,
) -> ProjectNodeRecord:
    cleaned_name = new_name.strip()
    if not cleaned_name:
        raise ValueError("文件夹名称不能为空")

    engine = get_engine(db_path)

    def _write() -> ProjectNodeRecord:
        with _DB_LOCK:
            with engine.begin() as conn:
                row = conn.execute(
                    select(project_nodes.c.parent_id).where(
                        project_nodes.c.id == node_id,
                        project_nodes.c.library_id == library_id,
                    )
                ).mappings().first()
                if row is None:
                    raise ValueError("文件夹不存在")
                if row["parent_id"] is None:
                    raise ValueError("根节点不支持重命名")

                try:
                    conn.execute(
                        update(project_nodes)
                        .where(project_nodes.c.id == node_id, project_nodes.c.library_id == library_id)
                        .values(name=cleaned_name)
                    )
                except IntegrityError as exc:
                    raise ValueError("同级文件夹名称已存在") from exc

            node = get_project_node(node_id, db_path)
            if node is None:
                raise ValueError("文件夹不存在")
            return node

    return _with_retry(_write)


def move_project_node(
    node_id: int,
    library_id: int,
    target_parent_id: int | None,
    db_path: Path = _DB_PATH,
) -> ProjectNodeRecord:
    engine = get_engine(db_path)

    def _write() -> ProjectNodeRecord:
        with _DB_LOCK:
            with engine.begin() as conn:
                rows = conn.execute(
                    select(project_nodes.c.id, project_nodes.c.parent_id)
                    .where(project_nodes.c.library_id == library_id)
                ).mappings().all()
                row_map = {int(row["id"]): row for row in rows}
                current = row_map.get(node_id)
                if current is None:
                    raise ValueError("文件夹不存在")
                if current["parent_id"] is None:
                    raise ValueError("根节点不支持移动")

                if target_parent_id is not None and target_parent_id not in row_map:
                    raise ValueError("目标文件夹不存在")

                descendants: set[int] = set()
                stack = [node_id]
                while stack:
                    current_id = stack.pop()
                    descendants.add(current_id)
                    child_ids = [int(row["id"]) for row in rows if row["parent_id"] == current_id]
                    stack.extend(child_ids)
                if target_parent_id in descendants:
                    raise ValueError("不能把文件夹移动到自己的子节点下")

                try:
                    conn.execute(
                        update(project_nodes)
                        .where(project_nodes.c.id == node_id, project_nodes.c.library_id == library_id)
                        .values(parent_id=target_parent_id)
                    )
                except IntegrityError as exc:
                    raise ValueError("目标位置已存在同名文件夹") from exc

            node = get_project_node(node_id, db_path)
            if node is None:
                raise ValueError("文件夹不存在")
            return node

    return _with_retry(_write)


def delete_project_node(
    node_id: int,
    library_id: int,
    db_path: Path = _DB_PATH,
) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                row = conn.execute(
                    select(project_nodes.c.parent_id).where(
                        project_nodes.c.id == node_id,
                        project_nodes.c.library_id == library_id,
                    )
                ).mappings().first()
                if row is None:
                    raise ValueError("文件夹不存在")
                if row["parent_id"] is None:
                    raise ValueError("根节点不支持删除")

                child_row = conn.execute(
                    select(project_nodes.c.id).where(project_nodes.c.parent_id == node_id).limit(1)
                ).first()
                if child_row is not None:
                    raise ValueError("请先移动或删除子文件夹")

                clip_row = conn.execute(
                    select(clip_node_refs.c.clip_id).where(clip_node_refs.c.node_id == node_id).limit(1)
                ).first()
                if clip_row is not None:
                    raise ValueError("请先移走该文件夹里的素材")

                conn.execute(
                    delete(project_nodes).where(
                        project_nodes.c.id == node_id,
                        project_nodes.c.library_id == library_id,
                    )
                )

    _with_retry(_write)


def attach_clip_to_node(
    clip_id: int,
    node_id: int,
    db_path: Path = _DB_PATH,
) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                row = conn.execute(
                    select(clips.c.id)
                    .select_from(clips.join(project_nodes, project_nodes.c.library_id == clips.c.library_id))
                    .where(clips.c.id == clip_id, project_nodes.c.id == node_id)
                ).first()
                if row is None:
                    raise ValueError("素材或目标文件夹不存在")
                _insert_clip_node_ref_if_missing(conn, clip_id, node_id)
                conn.execute(delete(recycled_clips).where(recycled_clips.c.clip_id == clip_id))

    _with_retry(_write)


def move_clip_to_node(
    clip_id: int,
    library_id: int,
    target_node_id: int | None,
    db_path: Path = _DB_PATH,
) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                root_row = conn.execute(
                    select(project_nodes.c.id).where(
                        project_nodes.c.library_id == library_id,
                        project_nodes.c.parent_id.is_(None),
                    )
                ).mappings().first()
                if root_row is None:
                    raise ValueError("根文件夹不存在")
                root_id = int(root_row["id"])

                clip_row = conn.execute(
                    select(clips.c.id).where(clips.c.id == clip_id, clips.c.library_id == library_id)
                ).first()
                if clip_row is None:
                    raise ValueError("素材不存在")

                if target_node_id is not None:
                    target_row = conn.execute(
                        select(project_nodes.c.id).where(
                            project_nodes.c.id == target_node_id,
                            project_nodes.c.library_id == library_id,
                            project_nodes.c.parent_id.is_not(None),
                        )
                    ).first()
                    if target_row is None:
                        raise ValueError("目标文件夹不存在")

                child_node_ids = [
                    int(row["id"])
                    for row in conn.execute(
                        select(project_nodes.c.id).where(
                            project_nodes.c.library_id == library_id,
                            project_nodes.c.parent_id.is_not(None),
                        )
                    ).mappings()
                ]
                if child_node_ids:
                    conn.execute(
                        delete(clip_node_refs).where(
                            clip_node_refs.c.clip_id == clip_id,
                            clip_node_refs.c.node_id.in_(child_node_ids),
                        )
                    )

                _insert_clip_node_ref_if_missing(conn, clip_id, root_id)
                if target_node_id is not None:
                    _insert_clip_node_ref_if_missing(conn, clip_id, target_node_id)
                conn.execute(delete(recycled_clips).where(recycled_clips.c.clip_id == clip_id))

    _with_retry(_write)


def list_recycled_clips(library_id: int, db_path: Path = _DB_PATH) -> list[RecycleClipRecord]:
    engine = get_engine(db_path)
    with _DB_LOCK:
        with engine.begin() as conn:
            _purge_expired_recycled_clips_locked(conn)

    rows = _fetch_all(
        select(
            recycled_clips.c.clip_id,
            recycled_clips.c.library_id,
            recycled_clips.c.deleted_node_ids_json,
            recycled_clips.c.deleted_at,
            recycled_clips.c.expires_at,
            clips.c.summary,
            clips.c.filename,
        )
        .select_from(recycled_clips.join(clips, clips.c.id == recycled_clips.c.clip_id))
        .where(recycled_clips.c.library_id == library_id)
        .order_by(recycled_clips.c.deleted_at.desc()),
        db_path=db_path,
    )
    return [
        RecycleClipRecord(
            clip_id=int(row["clip_id"]),
            library_id=int(row["library_id"]),
            summary=str(row["summary"] or "暂无摘要"),
            filename=str(row["filename"]),
            deleted_at=str(row["deleted_at"]),
            expires_at=str(row["expires_at"]),
            deleted_node_ids=[int(item) for item in (row["deleted_node_ids_json"] or [])],
        )
        for row in rows
    ]


def restore_recycled_clips(
    library_id: int,
    clip_ids: list[int],
    db_path: Path = _DB_PATH,
) -> int:
    if not clip_ids:
        return 0

    engine = get_engine(db_path)

    def _write() -> int:
        with _DB_LOCK:
            with engine.begin() as conn:
                rows = conn.execute(
                    select(recycled_clips.c.clip_id, recycled_clips.c.deleted_node_ids_json)
                    .where(
                        recycled_clips.c.library_id == library_id,
                        recycled_clips.c.clip_id.in_(clip_ids),
                    )
                ).mappings().all()
                if not rows:
                    return 0

                root_row = conn.execute(
                    select(project_nodes.c.id).where(
                        project_nodes.c.library_id == library_id,
                        project_nodes.c.parent_id.is_(None),
                    )
                ).mappings().first()
                root_id = int(root_row["id"]) if root_row is not None else None

                valid_node_ids = {
                    int(row["id"])
                    for row in conn.execute(
                        select(project_nodes.c.id).where(project_nodes.c.library_id == library_id)
                    ).mappings()
                }

                restored_count = 0
                for row in rows:
                    clip_id = int(row["clip_id"])
                    node_ids = [int(item) for item in (row["deleted_node_ids_json"] or [])]
                    valid_nodes = [node_id for node_id in node_ids if node_id in valid_node_ids]
                    if not valid_nodes and root_id is not None:
                        valid_nodes = [root_id]
                    for node_id in valid_nodes:
                        _insert_clip_node_ref_if_missing(conn, clip_id, node_id)
                    conn.execute(delete(recycled_clips).where(recycled_clips.c.clip_id == clip_id))
                    restored_count += 1

                return restored_count

    return _with_retry(_write)


def count_uncategorized_clips(library_id: int, db_path: Path = _DB_PATH) -> int:
    child_nodes_subquery = (
        select(project_nodes.c.id)
        .where(
            project_nodes.c.library_id == library_id,
            project_nodes.c.parent_id.is_not(None),
        )
        .subquery()
    )
    row = _fetch_one(
        select(func.count(func.distinct(clips.c.id)).label("count"))
        .select_from(clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id))
        .where(
            clips.c.library_id == library_id,
            clips.c.status == "done",
            recycled_clips.c.clip_id.is_(None),
            ~exists(
                select(literal(1))
                .select_from(clip_node_refs)
                .where(
                    clip_node_refs.c.clip_id == clips.c.id,
                    clip_node_refs.c.node_id.in_(select(child_nodes_subquery.c.id)),
                )
            ),
        ),
        db_path=db_path,
    )
    return int(row["count"] or 0) if row is not None else 0


def _normalize_search_text(value: Any) -> str:
    """把搜索文本归一化，兼顾中英文和符号。"""
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _score_text_match(query: str, target: str) -> float:
    if not query or not target:
        return 0.0
    if query in target:
        return 1.0
    query_tokens = [item for item in re.split(r"[\s,，、/|]+", query) if item]
    if query_tokens:
        token_hits = sum(1 for item in query_tokens if item in target)
        token_score = token_hits / len(query_tokens)
    else:
        token_score = 0.0
    char_score = len(set(query) & set(target)) / max(len(set(query)), 1)
    sequence_score = difflib.SequenceMatcher(None, query, target[: max(len(query) * 4, 24)]).ratio()
    return max(token_score, char_score * 0.72, sequence_score * 0.78)


def _score_clip_search(row: RowMapping, tags: list[str], query: str) -> float:
    """按多字段综合计算本地模糊搜索分数。"""
    normalized_query = _normalize_search_text(query)
    if not normalized_query:
        return 0.0
    weighted_targets = [
        (row["summary"], 4.0),
        (" ".join(tags), 3.5),
        (row["filename"], 2.4),
        (row["scene"], 2.0),
        (" ".join(row["subjects_json"] or []), 1.8),
        (" ".join(row["actions_json"] or []), 1.8),
    ]
    score = 0.0
    for target, weight in weighted_targets:
        score += _score_text_match(normalized_query, _normalize_search_text(target)) * weight
    return score


def query_clips(
    *,
    library_id: int,
    query: str = "",
    tag: str = "",
    favorite_only: bool = False,
    min_rating: int = 0,
    include_failed: bool = False,
    node_id: int | None = None,
    uncategorized_node_id: int | None = None,
    db_path: Path = _DB_PATH,
) -> list[dict[str, Any]]:
    filters: list[Any] = [
        clips.c.library_id == library_id,
        recycled_clips.c.clip_id.is_(None),
    ]
    if not include_failed:
        filters.append(clips.c.status == "done")
    if favorite_only:
        filters.append(clips.c.is_favorite.is_(True))
    if min_rating > 0:
        filters.append(clips.c.rating >= max(1, min(int(min_rating), 5)))

    if node_id is not None:
        if uncategorized_node_id is not None and node_id == uncategorized_node_id:
            child_node_ids = select(project_nodes.c.id).where(
                project_nodes.c.library_id == library_id,
                project_nodes.c.parent_id.is_not(None),
            )
            filters.append(
                ~exists(
                    select(literal(1))
                    .select_from(clip_node_refs)
                    .where(
                        clip_node_refs.c.clip_id == clips.c.id,
                        clip_node_refs.c.node_id.in_(child_node_ids),
                    )
                )
            )
        else:
            filters.append(
                exists(
                    select(literal(1))
                    .select_from(clip_node_refs)
                    .where(
                        clip_node_refs.c.clip_id == clips.c.id,
                        clip_node_refs.c.node_id == node_id,
                    )
                )
            )

    if tag:
        filters.append(
            exists(
                select(literal(1))
                .select_from(clip_tags)
                .where(clip_tags.c.clip_id == clips.c.id, clip_tags.c.tag == tag)
            )
        )

    rows = _fetch_all(
        select(
            clips.c.id,
            clips.c.filename,
            clips.c.filepath,
            clips.c.summary,
            clips.c.scene,
            clips.c.subjects_json,
            clips.c.actions_json,
            clips.c.has_motion,
            clips.c.sharpness_score,
            clips.c.cover_path,
            clips.c.status,
            clips.c.error_message,
            clips.c.transcript_status,
            clips.c.transcript_error_message,
            clips.c.preview_status,
            clips.c.preview_path,
            clips.c.preview_error_message,
            clips.c.comparison_status,
            clips.c.comparison_scores_json,
            clips.c.comparison_error_message,
            clips.c.is_favorite,
            clips.c.rating,
            clips.c.user_note,
            clips.c.source_modified_at,
            clips.c.note_status,
        )
        .select_from(clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id))
        .where(and_(*filters))
        .order_by(clips.c.updated_at.desc(), clips.c.id.desc()),
        db_path=db_path,
    )
    clip_ids = [int(row["id"]) for row in rows]
    tag_map = _load_clip_tags_map(clip_ids, db_path)
    folder_map = _load_clip_folder_names_map(clip_ids, db_path)

    items: list[dict[str, Any]] = []
    for row in rows:
        clip_id = int(row["id"])
        row_tags = tag_map.get(clip_id, [])
        search_score = _score_clip_search(row, row_tags, query)
        if query and search_score < 0.34:
            continue
        items.append(
            {
                **dict(row),
                "subjects_json": list(row["subjects_json"] or []),
                "actions_json": list(row["actions_json"] or []),
                "tags": row_tags,
                "folder_names": folder_map.get(clip_id, []),
                "search_score": search_score,
            }
        )
    if query:
        items.sort(key=lambda item: float(item.get("search_score") or 0.0), reverse=True)
    return items


def query_clip_card(clip_id: int, db_path: Path = _DB_PATH) -> dict[str, Any] | None:
    row = _fetch_one(
        select(
            clips.c.id,
            clips.c.filename,
            clips.c.filepath,
            clips.c.summary,
            clips.c.scene,
            clips.c.subjects_json,
            clips.c.actions_json,
            clips.c.has_motion,
            clips.c.sharpness_score,
            clips.c.cover_path,
            clips.c.status,
            clips.c.error_message,
            clips.c.transcript_status,
            clips.c.transcript_error_message,
            clips.c.preview_status,
            clips.c.preview_path,
            clips.c.preview_error_message,
            clips.c.comparison_status,
            clips.c.comparison_scores_json,
            clips.c.comparison_error_message,
            clips.c.is_favorite,
            clips.c.rating,
            clips.c.user_note,
            clips.c.source_modified_at,
            clips.c.note_status,
        ).where(clips.c.id == clip_id),
        db_path=db_path,
    )
    if row is None:
        return None
    tag_map = _load_clip_tags_map([clip_id], db_path)
    folder_map = _load_clip_folder_names_map([clip_id], db_path)
    return {
        **dict(row),
        "subjects_json": list(row["subjects_json"] or []),
        "actions_json": list(row["actions_json"] or []),
        "tags": tag_map.get(clip_id, []),
        "folder_names": folder_map.get(clip_id, []),
    }


def query_tag_counts(
    library_id: int,
    node_id: int | None = None,
    uncategorized_node_id: int | None = None,
    db_path: Path = _DB_PATH,
) -> list[tuple[str, int]]:
    clip_ids = [
        int(item["id"])
        for item in query_clips(
            library_id=library_id,
            node_id=node_id,
            uncategorized_node_id=uncategorized_node_id,
            db_path=db_path,
        )
    ]
    if not clip_ids:
        return []
    rows = _fetch_all(
        select(clip_tags.c.tag, func.count(func.distinct(clip_tags.c.clip_id)).label("count"))
        .where(clip_tags.c.clip_id.in_(clip_ids))
        .group_by(clip_tags.c.tag)
        .order_by(func.count(func.distinct(clip_tags.c.clip_id)).desc(), clip_tags.c.tag.asc()),
        db_path=db_path,
    )
    return [(str(row["tag"]), int(row["count"] or 0)) for row in rows]


def query_clip_detail(clip_id: int, db_path: Path = _DB_PATH) -> dict[str, Any] | None:
    row = _fetch_one(
        select(
            clips.c.id,
            clips.c.library_id,
            clips.c.filename,
            clips.c.filepath,
            clips.c.summary,
            clips.c.scene,
            clips.c.subjects_json,
            clips.c.actions_json,
            clips.c.has_motion,
            clips.c.sharpness_score,
            clips.c.cover_path,
            clips.c.status,
            clips.c.error_message,
            clips.c.transcript_status,
            clips.c.transcript_error_message,
            clips.c.preview_status,
            clips.c.preview_path,
            clips.c.preview_error_message,
            clips.c.comparison_status,
            clips.c.comparison_scores_json,
            clips.c.comparison_error_message,
            clips.c.user_note,
            clips.c.source_modified_at,
            clips.c.note_status,
            libraries.c.name.label("library_name"),
        )
        .select_from(clips.outerjoin(libraries, libraries.c.id == clips.c.library_id))
        .where(clips.c.id == clip_id),
        db_path=db_path,
    )
    if row is None:
        return None
    tag_map = _load_clip_tags_map([clip_id], db_path)
    return {
        **dict(row),
        "subjects_json": list(row["subjects_json"] or []),
        "actions_json": list(row["actions_json"] or []),
        "tags": tag_map.get(clip_id, []),
    }


def query_similar_clips(
    current_clip_id: int,
    library_id: int,
    scene: str,
    limit: int = 3,
    db_path: Path = _DB_PATH,
) -> list[dict[str, Any]]:
    rows = _fetch_all(
        select(
            clips.c.id,
            clips.c.filename,
            clips.c.filepath,
            clips.c.summary,
            clips.c.scene,
            clips.c.subjects_json,
            clips.c.actions_json,
            clips.c.has_motion,
            clips.c.sharpness_score,
            clips.c.cover_path,
            clips.c.status,
            clips.c.error_message,
            clips.c.transcript_status,
            clips.c.transcript_error_message,
            clips.c.preview_status,
            clips.c.preview_path,
            clips.c.preview_error_message,
            clips.c.comparison_status,
            clips.c.comparison_scores_json,
            clips.c.comparison_error_message,
            clips.c.user_note,
            clips.c.source_modified_at,
            clips.c.note_status,
        )
        .select_from(clips.outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id))
        .where(
            clips.c.id != current_clip_id,
            clips.c.library_id == library_id,
            clips.c.scene == scene,
            clips.c.status == "done",
            recycled_clips.c.clip_id.is_(None),
        )
        .order_by(clips.c.updated_at.desc(), clips.c.id.desc())
        .limit(limit),
        db_path=db_path,
    )
    clip_ids = [int(row["id"]) for row in rows]
    tag_map = _load_clip_tags_map(clip_ids, db_path)
    return [
        {
            **dict(row),
            "subjects_json": list(row["subjects_json"] or []),
            "actions_json": list(row["actions_json"] or []),
            "tags": tag_map.get(int(row["id"]), []),
            "folder_names": [],
        }
        for row in rows
    ]


def query_adjacent_clip_ids(clip_id: int, library_id: int, db_path: Path = _DB_PATH) -> tuple[int | None, int | None]:
    rows = _fetch_all(
        select(clips.c.id)
        .where(clips.c.library_id == library_id)
        .order_by(clips.c.updated_at.desc(), clips.c.id.desc()),
        db_path=db_path,
    )
    ordered_ids = [int(row["id"]) for row in rows]
    try:
        current_index = ordered_ids.index(clip_id)
    except ValueError:
        return None, None
    previous_id = ordered_ids[current_index - 1] if current_index > 0 else None
    next_id = ordered_ids[current_index + 1] if current_index < len(ordered_ids) - 1 else None
    return previous_id, next_id


def update_clip_transcript_state(
    clip_id: int,
    *,
    transcript_status: str,
    transcript_error_message: str | None,
    db_path: Path = _DB_PATH,
) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    update(clips)
                    .where(clips.c.id == clip_id)
                    .values(
                        transcript_status=transcript_status,
                        transcript_error_message=transcript_error_message,
                        updated_at=_utcnow(),
                    )
                )

    _with_retry(_write)


def update_clip_asset_state(
    clip_id: int,
    *,
    preview_status: str | None = None,
    preview_path: Path | None = None,
    preview_error_message: str | None | object = None,
    comparison_status: str | None = None,
    comparison_scores: dict[str, float] | None = None,
    comparison_error_message: str | None | object = None,
    db_path: Path = _DB_PATH,
) -> None:
    values: dict[str, Any] = {"updated_at": _utcnow()}
    if preview_status is not None:
        values["preview_status"] = preview_status
    if preview_path is not None:
        values["preview_path"] = str(preview_path)
    if preview_error_message is not None:
        values["preview_error_message"] = preview_error_message
    if comparison_status is not None:
        values["comparison_status"] = comparison_status
    if comparison_scores is not None:
        values["comparison_scores_json"] = json.dumps(comparison_scores, ensure_ascii=False)
    if comparison_error_message is not None:
        values["comparison_error_message"] = comparison_error_message

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(update(clips).where(clips.c.id == clip_id).values(**values))

    _with_retry(_write)


def query_export_clips(clip_ids: list[int], db_path: Path = _DB_PATH) -> list[dict[str, Any]]:
    if not clip_ids:
        return []
    rows = _fetch_all(
        select(clips.c.id, clips.c.filename, clips.c.filepath, clips.c.summary)
        .where(clips.c.id.in_(clip_ids))
        .order_by(clips.c.id.asc()),
        db_path=db_path,
    )
    return [dict(row) for row in rows]


def get_user_by_id(user_id: int, db_path: Path = _DB_PATH) -> UserRecord | None:
    row = _fetch_one(
        select(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.display_name,
            users.c.is_active,
            users.c.phone_number,
            users.c.phone_verified_at,
        ).where(users.c.id == user_id),
        db_path=db_path,
    )
    return _row_to_user(row) if row is not None else None


def get_user_by_username(username: str, db_path: Path = _DB_PATH) -> UserRecord | None:
    row = _fetch_one(
        select(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.display_name,
            users.c.is_active,
            users.c.phone_number,
            users.c.phone_verified_at,
        ).where(users.c.username == username.strip()),
        db_path=db_path,
    )
    return _row_to_user(row) if row is not None else None


def authenticate_user(username: str, password: str, db_path: Path = _DB_PATH) -> UserRecord | None:
    row = _fetch_one(
        select(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.display_name,
            users.c.is_active,
            users.c.phone_number,
            users.c.phone_verified_at,
            users.c.password_hash,
        ).where(users.c.username == username.strip()),
        db_path=db_path,
    )
    if row is None:
        return None
    if not bool(row["is_active"]):
        return None
    if not _verify_password(password, str(row["password_hash"])):
        return None
    return _row_to_user(row)


def _validate_password(password: str) -> str:
    cleaned = password.strip()
    if len(cleaned) < 6:
        raise ValueError("密码至少需要 6 位。")
    return cleaned


def create_user(
    username: str,
    password: str,
    *,
    display_name: str = "",
    role: str = "user",
    is_active: bool = True,
    db_path: Path = _DB_PATH,
) -> UserRecord:
    cleaned_username = username.strip().lower()
    cleaned_display_name = display_name.strip() or cleaned_username
    cleaned_role = role.strip().lower()
    if not cleaned_username:
        raise ValueError("用户名不能为空。")
    if cleaned_role not in {"user", "admin"}:
        raise ValueError("用户角色无效。")
    validated_password = _validate_password(password)
    if get_user_by_username(cleaned_username, db_path) is not None:
        raise ValueError("用户名已存在。")

    engine = get_engine(db_path)

    def _write() -> UserRecord:
        with _DB_LOCK:
            with engine.begin() as conn:
                result = conn.execute(
                    insert(users).values(
                        username=cleaned_username,
                        password_hash=_hash_password(validated_password),
                        role=cleaned_role,
                        display_name=cleaned_display_name,
                        is_active=bool(is_active),
                        created_at=_utcnow(),
                        updated_at=_utcnow(),
                    )
                )
                user_id = int(result.inserted_primary_key[0])
                row = conn.execute(
                    select(
                        users.c.id,
                        users.c.username,
                        users.c.role,
                        users.c.display_name,
                        users.c.is_active,
                        users.c.phone_number,
                        users.c.phone_verified_at,
                    ).where(users.c.id == user_id)
                ).mappings().first()
        if row is None:
            raise RuntimeError("创建用户后未找到记录。")
        return _row_to_user(row)

    try:
        return _with_retry(_write)
    except IntegrityError as exc:
        raise ValueError("用户名已存在。") from exc


def revoke_user_sessions(
    user_id: int,
    db_path: Path = _DB_PATH,
    *,
    except_session_token: str | None = None,
) -> None:
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                stmt = delete(user_sessions).where(user_sessions.c.user_id == user_id)
                if except_session_token:
                    stmt = stmt.where(user_sessions.c.session_token != except_session_token)
                conn.execute(stmt)

    _with_retry(_write)


def update_user_password(user_id: int, new_password: str, db_path: Path = _DB_PATH) -> None:
    validated_password = _validate_password(new_password)
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    update(users)
                    .where(users.c.id == user_id)
                    .values(
                        password_hash=_hash_password(validated_password),
                        updated_at=_utcnow(),
                    )
                )

    _with_retry(_write)


def change_user_password(
    user_id: int,
    current_password: str,
    new_password: str,
    db_path: Path = _DB_PATH,
) -> None:
    row = _fetch_one(
        select(users.c.password_hash).where(users.c.id == user_id),
        db_path=db_path,
    )
    if row is None:
        raise ValueError("用户不存在。")
    if not _verify_password(current_password, str(row["password_hash"])):
        raise ValueError("当前密码不正确。")
    update_user_password(user_id, new_password, db_path)


def issue_phone_verification_code(
    phone_number: str,
    purpose: str,
    db_path: Path = _DB_PATH,
    *,
    user_id: int | None = None,
    ttl_minutes: int = 10,
) -> str:
    normalized_phone = _normalize_phone_number(phone_number)
    cleaned_purpose = purpose.strip().lower()
    if cleaned_purpose not in {"bind", "reset"}:
        raise ValueError("验证码用途无效。")
    if cleaned_purpose == "reset" and get_user_by_phone(normalized_phone, db_path) is None:
        raise ValueError("这个手机号尚未绑定已验证账号。")
    if cleaned_purpose == "bind":
        existing_user = get_user_by_phone(normalized_phone, db_path)
        if existing_user is not None and existing_user.id != user_id:
            raise ValueError("这个手机号已被其他账号绑定。")

    code = f"{secrets.randbelow(1_000_000):06d}"
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    update(phone_verification_codes)
                    .where(
                        phone_verification_codes.c.phone_number == normalized_phone,
                        phone_verification_codes.c.purpose == cleaned_purpose,
                        phone_verification_codes.c.consumed_at.is_(None),
                    )
                    .values(consumed_at=_utcnow())
                )
                conn.execute(
                    insert(phone_verification_codes).values(
                        phone_number=normalized_phone,
                        purpose=cleaned_purpose,
                        user_id=user_id,
                        code_hash=_hash_password(code),
                        expires_at=_utcnow() + timedelta(minutes=ttl_minutes),
                        consumed_at=None,
                        created_at=_utcnow(),
                    )
                )

    _with_retry(_write)
    return code


def _consume_phone_verification_code(
    phone_number: str,
    code: str,
    purpose: str,
    db_path: Path = _DB_PATH,
) -> str:
    normalized_phone = _normalize_phone_number(phone_number)
    cleaned_code = code.strip()
    cleaned_purpose = purpose.strip().lower()
    if not cleaned_code:
        raise ValueError("请输入验证码。")

    engine = get_engine(db_path)

    def _write() -> str:
        with _DB_LOCK:
            with engine.begin() as conn:
                rows = conn.execute(
                    select(
                        phone_verification_codes.c.id,
                        phone_verification_codes.c.code_hash,
                        phone_verification_codes.c.expires_at,
                    )
                    .where(
                        phone_verification_codes.c.phone_number == normalized_phone,
                        phone_verification_codes.c.purpose == cleaned_purpose,
                        phone_verification_codes.c.consumed_at.is_(None),
                    )
                    .order_by(phone_verification_codes.c.id.desc())
                ).mappings().all()
                for row in rows:
                    expires_at = row["expires_at"]
                    if expires_at <= _utcnow():
                        continue
                    if _verify_password(cleaned_code, str(row["code_hash"])):
                        conn.execute(
                            update(phone_verification_codes)
                            .where(phone_verification_codes.c.id == int(row["id"]))
                            .values(consumed_at=_utcnow())
                        )
                        return normalized_phone
        raise ValueError("验证码无效或已过期。")

    return _with_retry(_write)


def bind_user_phone(
    user_id: int,
    phone_number: str,
    code: str,
    db_path: Path = _DB_PATH,
) -> UserRecord:
    normalized_phone = _consume_phone_verification_code(phone_number, code, "bind", db_path)
    existing_user = get_user_by_phone(normalized_phone, db_path)
    if existing_user is not None and existing_user.id != user_id:
        raise ValueError("这个手机号已被其他账号绑定。")

    engine = get_engine(db_path)

    def _write() -> UserRecord:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    update(users)
                    .where(users.c.id == user_id)
                    .values(
                        phone_number=normalized_phone,
                        phone_verified_at=_utcnow(),
                        updated_at=_utcnow(),
                    )
                )
                row = conn.execute(
                    select(
                        users.c.id,
                        users.c.username,
                        users.c.role,
                        users.c.display_name,
                        users.c.is_active,
                        users.c.phone_number,
                        users.c.phone_verified_at,
                    ).where(users.c.id == user_id)
                ).mappings().first()
        if row is None:
            raise ValueError("用户不存在。")
        return _row_to_user(row)

    return _with_retry(_write)


def get_user_by_phone(phone_number: str, db_path: Path = _DB_PATH) -> UserRecord | None:
    normalized_phone = _normalize_phone_number(phone_number)
    row = _fetch_one(
        select(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.display_name,
            users.c.is_active,
            users.c.phone_number,
            users.c.phone_verified_at,
        ).where(
            users.c.phone_number == normalized_phone,
            users.c.phone_verified_at.is_not(None),
        ),
        db_path=db_path,
    )
    return _row_to_user(row) if row is not None else None


def reset_password_by_phone(
    phone_number: str,
    code: str,
    new_password: str,
    db_path: Path = _DB_PATH,
) -> UserRecord:
    normalized_phone = _consume_phone_verification_code(phone_number, code, "reset", db_path)
    user = get_user_by_phone(normalized_phone, db_path)
    if user is None:
        raise ValueError("这个手机号尚未绑定已验证账号。")
    update_user_password(user.id, new_password, db_path)
    revoke_user_sessions(user.id, db_path)
    return user


def update_user_status(user_id: int, is_active: bool, db_path: Path = _DB_PATH) -> None:
    target_user = get_user_by_id(user_id, db_path)
    if target_user is None:
        raise ValueError("用户不存在。")
    if target_user.role == "admin" and not is_active:
        admin_count = int(
            _fetch_one(
                select(func.count()).select_from(users).where(
                    users.c.role == "admin",
                    users.c.is_active.is_(True),
                ),
                db_path=db_path,
            )["count_1"]
        )
        if admin_count <= 1:
            raise ValueError("至少需要保留一个启用中的管理员。")

    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    update(users)
                    .where(users.c.id == user_id)
                    .values(
                        is_active=bool(is_active),
                        updated_at=_utcnow(),
                    )
                )

    _with_retry(_write)
    if not is_active:
        revoke_user_sessions(user_id, db_path)


def create_session(user_id: int, db_path: Path = _DB_PATH, *, ttl_days: int = 7) -> str:
    engine = get_engine(db_path)
    session_token = secrets.token_urlsafe(32)

    def _write() -> str:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(
                    insert(user_sessions).values(
                        user_id=user_id,
                        session_token=session_token,
                        expires_at=_utcnow() + timedelta(days=ttl_days),
                        created_at=_utcnow(),
                    )
                )
        return session_token

    return _with_retry(_write)


def delete_session(session_token: str, db_path: Path = _DB_PATH) -> None:
    if not session_token:
        return
    engine = get_engine(db_path)

    def _write() -> None:
        with _DB_LOCK:
            with engine.begin() as conn:
                conn.execute(delete(user_sessions).where(user_sessions.c.session_token == session_token))

    _with_retry(_write)


def get_user_by_session_token(session_token: str, db_path: Path = _DB_PATH) -> UserRecord | None:
    if not session_token:
        return None
    engine = get_engine(db_path)
    with _DB_LOCK:
        with engine.begin() as conn:
            conn.execute(delete(user_sessions).where(user_sessions.c.expires_at <= _utcnow()))
            row = conn.execute(
                select(
                    users.c.id,
                    users.c.username,
                    users.c.role,
                    users.c.display_name,
                    users.c.is_active,
                    users.c.phone_number,
                    users.c.phone_verified_at,
                )
                .select_from(user_sessions.join(users, users.c.id == user_sessions.c.user_id))
                .where(
                    user_sessions.c.session_token == session_token,
                    user_sessions.c.expires_at > _utcnow(),
                    users.c.is_active.is_(True),
                )
            ).mappings().first()
    return _row_to_user(row) if row is not None else None


def list_users(db_path: Path = _DB_PATH) -> list[UserRecord]:
    rows = _fetch_all(
        select(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.display_name,
            users.c.is_active,
            users.c.phone_number,
            users.c.phone_verified_at,
        ).order_by(users.c.role.desc(), users.c.created_at.asc(), users.c.id.asc()),
        db_path=db_path,
    )
    return [_row_to_user(row) for row in rows]


def list_user_usage_stats(db_path: Path = _DB_PATH) -> list[UserUsageRecord]:
    rows = _fetch_all(
        select(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.display_name,
            users.c.is_active,
            users.c.phone_number,
            users.c.phone_verified_at,
            func.count(func.distinct(libraries.c.id)).label("library_count"),
            func.count(func.distinct(clips.c.id)).label("clip_count"),
            func.count(func.distinct(recycled_clips.c.clip_id)).label("recycled_clip_count"),
            func.coalesce(func.sum(func.length(clips.c.filepath)), 0).label("path_length_sum"),
            func.count(func.distinct(user_sessions.c.id)).label("active_session_count"),
        )
        .select_from(
            users.outerjoin(libraries, libraries.c.owner_user_id == users.c.id)
            .outerjoin(clips, clips.c.library_id == libraries.c.id)
            .outerjoin(recycled_clips, recycled_clips.c.clip_id == clips.c.id)
            .outerjoin(
                user_sessions,
                and_(
                    user_sessions.c.user_id == users.c.id,
                    user_sessions.c.expires_at > _utcnow(),
                ),
            )
        )
        .group_by(
            users.c.id,
            users.c.username,
            users.c.role,
            users.c.display_name,
            users.c.is_active,
            users.c.phone_number,
            users.c.phone_verified_at,
        )
        .order_by(users.c.role.desc(), users.c.id.asc()),
        db_path=db_path,
    )
    usage_records: list[UserUsageRecord] = []
    for row in rows:
        user = _row_to_user(row)
        usage_records.append(
            UserUsageRecord(
                user=user,
                library_count=int(row["library_count"] or 0),
                clip_count=int(row["clip_count"] or 0),
                recycled_clip_count=int(row["recycled_clip_count"] or 0),
                total_storage_bytes=0,
                active_session_count=int(row["active_session_count"] or 0),
            )
        )
    return usage_records


def get_admin_dashboard_stats(db_path: Path = _DB_PATH) -> dict[str, Any]:
    usage_records = list_user_usage_stats(db_path)
    total_users = len(usage_records)
    total_admins = sum(1 for item in usage_records if item.user.role == "admin")
    total_sessions = sum(item.active_session_count for item in usage_records)
    total_libraries = sum(item.library_count for item in usage_records)
    total_clips = sum(item.clip_count for item in usage_records)
    total_recycled = sum(item.recycled_clip_count for item in usage_records)
    return {
        "total_users": total_users,
        "total_admins": total_admins,
        "active_sessions": total_sessions,
        "total_libraries": total_libraries,
        "total_clips": total_clips,
        "total_recycled_clips": total_recycled,
        "user_usage": usage_records,
    }
