# SQLite 读写：初始化 schema，并负责 clip 结果存取

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClipRecord:
    video_hash: str
    filename: str
    filepath: Path
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


_DB_PATH = Path(__file__).parent / "data" / "reelsift.db"
_CONNECTION: sqlite3.Connection | None = None


def get_connection(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    """返回全局 SQLite 连接，避免每次查询重复打开。"""
    global _CONNECTION

    if _CONNECTION is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _CONNECTION = sqlite3.connect(db_path)
        _CONNECTION.row_factory = sqlite3.Row
    return _CONNECTION


def init_db(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    """初始化数据库表和索引。"""
    conn = get_connection(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_hash TEXT NOT NULL UNIQUE,
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
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS clip_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE,
            UNIQUE (clip_id, tag)
        );

        CREATE INDEX IF NOT EXISTS idx_clips_filename ON clips(filename);
        CREATE INDEX IF NOT EXISTS idx_clip_tags_tag ON clip_tags(tag);
        CREATE INDEX IF NOT EXISTS idx_clip_tags_clip_id ON clip_tags(clip_id);
        """
    )
    conn.commit()
    return conn


def save_clip(record: ClipRecord, db_path: Path = _DB_PATH) -> int:
    """插入或更新 clip，并同步 tags。"""
    conn = get_connection(db_path)
    cursor = conn.execute(
        """
        INSERT INTO clips (
            video_hash,
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
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(video_hash) DO UPDATE SET
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
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            record.video_hash,
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
        ),
    )

    clip_id = cursor.lastrowid
    if clip_id == 0:
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
