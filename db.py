# SQLite 读写：初始化 schema，并负责 clip 结果存取

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LibraryRecord:
    id: int
    name: str
    clip_count: int = 0


@dataclass
class TranscriptRecord:
    clip_id: int
    start_ms: int
    end_ms: int
    text: str
    segment_index: int


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


_DB_PATH = Path(__file__).parent / "data" / "reelsift.db"
_CONNECTION: sqlite3.Connection | None = None
_DB_LOCK = threading.Lock()
DEFAULT_LIBRARY_NAME = "默认素材库"


def _with_locked_retry(action, *, attempts: int = 6, sleep_seconds: float = 0.25):
    """遇到 database is locked 时做有限重试。"""
    last_error: sqlite3.OperationalError | None = None
    for index in range(attempts):
        try:
            return action()
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise
            last_error = exc
            if index == attempts - 1:
                break
            time.sleep(sleep_seconds * (index + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("数据库重试失败，但没有捕获到明确异常。")


def get_connection(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    """返回全局 SQLite 连接，避免每次查询重复打开。"""
    global _CONNECTION

    if _CONNECTION is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _CONNECTION = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        _CONNECTION.row_factory = sqlite3.Row
        _CONNECTION.execute("PRAGMA busy_timeout = 10000;")
        _CONNECTION.execute("PRAGMA foreign_keys = ON;")
        try:
            _CONNECTION.execute("PRAGMA journal_mode=WAL;")
            _CONNECTION.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.OperationalError:
            # 如果当前已有别的连接占用数据库，先退回默认模式，避免启动直接失败。
            pass
    return _CONNECTION


def init_db(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    """初始化数据库表和索引。"""
    conn = get_connection(db_path)
    try:
        with _DB_LOCK:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS libraries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS clips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_hash TEXT NOT NULL UNIQUE,
                    library_id INTEGER NOT NULL DEFAULT 1,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    summary TEXT,
                    scene TEXT,
                    subjects_json TEXT NOT NULL DEFAULT '[]',
                    actions_json TEXT NOT NULL DEFAULT '[]',
                    has_motion INTEGER,
                    sharpness_score REAL,
                    cover_path TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    transcript_status TEXT NOT NULL DEFAULT 'pending',
                    transcript_error_message TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (library_id) REFERENCES libraries(id)
                );

                CREATE TABLE IF NOT EXISTS clip_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clip_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE,
                    UNIQUE (clip_id, tag)
                );

                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clip_id INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_clips_filename ON clips(filename);
                CREATE INDEX IF NOT EXISTS idx_clip_tags_tag ON clip_tags(tag);
                CREATE INDEX IF NOT EXISTS idx_clip_tags_clip_id ON clip_tags(clip_id);
                CREATE INDEX IF NOT EXISTS idx_transcripts_clip_id ON transcripts(clip_id);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO libraries (id, name) VALUES (1, ?)",
                (DEFAULT_LIBRARY_NAME,),
            )

            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(clips)").fetchall()
            }
            if "library_id" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN library_id INTEGER NOT NULL DEFAULT 1")
            if "transcript_status" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN transcript_status TEXT NOT NULL DEFAULT 'pending'")
            if "transcript_error_message" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN transcript_error_message TEXT")
            conn.execute("UPDATE clips SET library_id = 1 WHERE library_id IS NULL")
            conn.execute(
                "UPDATE clips SET transcript_status = 'pending' "
                "WHERE transcript_status IS NULL OR transcript_status = ''"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_library_id ON clips(library_id)")
            conn.commit()
    except sqlite3.OperationalError as exc:
        if "database is locked" not in str(exc).lower():
            raise
    return conn


def list_libraries(db_path: Path = _DB_PATH) -> list[LibraryRecord]:
    """读取所有素材库及其素材数量。"""
    conn = get_connection(db_path)
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT
                l.id,
                l.name,
                COUNT(c.id) AS clip_count
            FROM libraries l
            LEFT JOIN clips c ON c.library_id = l.id AND c.status = 'done'
            GROUP BY l.id
            ORDER BY l.created_at ASC, l.id ASC
            """
        ).fetchall()
    return [
        LibraryRecord(
            id=int(row["id"]),
            name=row["name"],
            clip_count=int(row["clip_count"]),
        )
        for row in rows
    ]


def create_library(name: str, db_path: Path = _DB_PATH) -> LibraryRecord:
    """创建素材库。"""
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("素材库名称不能为空")

    conn = get_connection(db_path)

    def _write() -> LibraryRecord:
        with _DB_LOCK:
            cursor = conn.execute(
                "INSERT INTO libraries (name) VALUES (?)",
                (cleaned_name,),
            )
            conn.commit()
            return LibraryRecord(id=int(cursor.lastrowid), name=cleaned_name, clip_count=0)

    return _with_locked_retry(_write)


def get_library_by_id(library_id: int, db_path: Path = _DB_PATH) -> LibraryRecord | None:
    """按 ID 读取素材库。"""
    conn = get_connection(db_path)
    with _DB_LOCK:
        row = conn.execute(
            """
            SELECT
                l.id,
                l.name,
                COUNT(c.id) AS clip_count
            FROM libraries l
            LEFT JOIN clips c ON c.library_id = l.id AND c.status = 'done'
            WHERE l.id = ?
            GROUP BY l.id
            """,
            (library_id,),
        ).fetchone()
    if row is None:
        return None
    return LibraryRecord(
        id=int(row["id"]),
        name=row["name"],
        clip_count=int(row["clip_count"]),
    )


def rename_library(library_id: int, new_name: str, db_path: Path = _DB_PATH) -> LibraryRecord:
    """重命名素材库。"""
    cleaned_name = new_name.strip()
    if not cleaned_name:
        raise ValueError("素材库名称不能为空")
    if library_id == 1:
        raise ValueError("默认素材库不支持重命名")

    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            conn.execute(
                "UPDATE libraries SET name = ? WHERE id = ?",
                (cleaned_name, library_id),
            )
            if conn.total_changes == 0:
                raise ValueError("素材库不存在")
            conn.commit()

    _with_locked_retry(_write)
    library = get_library_by_id(library_id, db_path)
    if library is None:
        raise ValueError("素材库不存在")
    return library


def delete_library(library_id: int, db_path: Path = _DB_PATH) -> None:
    """删除空素材库。"""
    if library_id == 1:
        raise ValueError("默认素材库不支持删除")

    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM clips WHERE library_id = ?",
                (library_id,),
            ).fetchone()
            if row is None:
                raise ValueError("素材库不存在")
            if int(row["count"]) > 0:
                raise ValueError("素材库里还有素材，不能直接删除")
            conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
            if conn.total_changes == 0:
                raise ValueError("素材库不存在")
            conn.commit()

    _with_locked_retry(_write)


def delete_clips(clip_ids: list[int], library_id: int, db_path: Path = _DB_PATH) -> int:
    """批量删除素材和标签记录。"""
    if not clip_ids:
        return 0

    conn = get_connection(db_path)
    placeholders = ",".join("?" for _ in clip_ids)
    params = [library_id, *clip_ids]

    def _write() -> int:
        with _DB_LOCK:
            rows = conn.execute(
                f"""
                SELECT id FROM clips
                WHERE library_id = ? AND id IN ({placeholders})
                """,
                params,
            ).fetchall()
            target_ids = [int(row["id"]) for row in rows]
            if not target_ids:
                return 0

            delete_placeholders = ",".join("?" for _ in target_ids)
            conn.execute(
                f"DELETE FROM clip_tags WHERE clip_id IN ({delete_placeholders})",
                target_ids,
            )
            conn.execute(
                f"DELETE FROM clips WHERE id IN ({delete_placeholders})",
                target_ids,
            )
            conn.commit()
        return len(target_ids)

    return _with_locked_retry(_write)


def save_clip(record: ClipRecord, db_path: Path = _DB_PATH) -> int:
    """插入或更新 clip，并同步 tags。"""
    conn = get_connection(db_path)

    def _write() -> int:
        with _DB_LOCK:
            conn.execute(
                """
                INSERT INTO clips (
                    video_hash,
                    library_id,
                    filename,
                    filepath,
                    summary,
                    scene,
                    subjects_json,
                    actions_json,
                    has_motion,
                    sharpness_score,
                    cover_path,
                    status,
                    error_message,
                    transcript_status,
                    transcript_error_message,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(video_hash) DO UPDATE SET
                    library_id=excluded.library_id,
                    filename=excluded.filename,
                    filepath=excluded.filepath,
                    summary=excluded.summary,
                    scene=excluded.scene,
                    subjects_json=excluded.subjects_json,
                    actions_json=excluded.actions_json,
                    has_motion=excluded.has_motion,
                    sharpness_score=excluded.sharpness_score,
                    cover_path=excluded.cover_path,
                    status=excluded.status,
                    error_message=excluded.error_message,
                    transcript_status=excluded.transcript_status,
                    transcript_error_message=excluded.transcript_error_message,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    record.video_hash,
                    record.library_id,
                    record.filename,
                    str(record.filepath),
                    record.summary,
                    record.scene,
                    json.dumps(record.subjects or [], ensure_ascii=False),
                    json.dumps(record.actions or [], ensure_ascii=False),
                    None if record.has_motion is None else int(record.has_motion),
                    record.sharpness_score,
                    str(record.cover_path) if record.cover_path else None,
                    record.status,
                    record.error_message,
                    record.transcript_status,
                    record.transcript_error_message,
                ),
            )

            row = conn.execute(
                "SELECT id FROM clips WHERE video_hash = ?",
                (record.video_hash,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"未找到已保存的 clip：{record.video_hash}")
            clip_id = int(row["id"])

            conn.execute("DELETE FROM clip_tags WHERE clip_id = ?", (clip_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO clip_tags (clip_id, tag) VALUES (?, ?)",
                [(clip_id, tag) for tag in (record.tags or [])],
            )
            conn.commit()
        return clip_id

    return _with_locked_retry(_write)


def save_transcripts(records: list[TranscriptRecord], db_path: Path = _DB_PATH) -> None:
    """覆盖保存某条素材的 transcript 分段。"""
    if not records:
        return

    conn = get_connection(db_path)
    clip_id = records[0].clip_id

    def _write() -> None:
        with _DB_LOCK:
            conn.execute("DELETE FROM transcripts WHERE clip_id = ?", (clip_id,))
            conn.executemany(
                """
                INSERT INTO transcripts (
                    clip_id,
                    start_ms,
                    end_ms,
                    text,
                    segment_index
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.clip_id,
                        record.start_ms,
                        record.end_ms,
                        record.text,
                        record.segment_index,
                    )
                    for record in records
                ],
            )
            conn.commit()

    _with_locked_retry(_write)


def load_transcripts(clip_id: int, db_path: Path = _DB_PATH) -> list[TranscriptRecord]:
    """读取单条素材的 transcript 分段。"""
    conn = get_connection(db_path)
    with _DB_LOCK:
        rows = conn.execute(
            """
            SELECT clip_id, start_ms, end_ms, text, segment_index
            FROM transcripts
            WHERE clip_id = ?
            ORDER BY segment_index ASC, start_ms ASC
            """,
            (clip_id,),
        ).fetchall()
    return [
        TranscriptRecord(
            clip_id=int(row["clip_id"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            text=row["text"],
            segment_index=int(row["segment_index"]),
        )
        for row in rows
    ]
