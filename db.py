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


_DB_PATH = Path(__file__).parent / "data" / "reelsift.db"
_CONNECTION: sqlite3.Connection | None = None
_DB_LOCK = threading.RLock()
DEFAULT_LIBRARY_NAME = "默认素材库"
ROOT_NODE_NAME = "全部素材"


def _normalize_project_roots_locked(conn: sqlite3.Connection) -> None:
    """清理重复根节点，并确保每个素材库只有一个根节点。"""
    library_rows = conn.execute("SELECT id FROM libraries ORDER BY id ASC").fetchall()
    for library_row in library_rows:
        library_id = int(library_row["id"])
        root_rows = conn.execute(
            """
            SELECT id
            FROM project_nodes
            WHERE library_id = ? AND parent_id IS NULL
            ORDER BY id ASC
            """,
            (library_id,),
        ).fetchall()

        if not root_rows:
            conn.execute(
                "INSERT INTO project_nodes (library_id, parent_id, name) VALUES (?, NULL, ?)",
                (library_id, ROOT_NODE_NAME),
            )
            continue

        keeper_id = int(root_rows[0]["id"])
        conn.execute("UPDATE project_nodes SET name = ? WHERE id = ?", (ROOT_NODE_NAME, keeper_id))

        for duplicate_row in root_rows[1:]:
            duplicate_id = int(duplicate_row["id"])
            conn.execute(
                "UPDATE project_nodes SET parent_id = ? WHERE parent_id = ?",
                (keeper_id, duplicate_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO clip_node_refs (clip_id, node_id)
                SELECT clip_id, ?
                FROM clip_node_refs
                WHERE node_id = ?
                """,
                (keeper_id, duplicate_id),
            )
            conn.execute("DELETE FROM clip_node_refs WHERE node_id = ?", (duplicate_id,))
            conn.execute("DELETE FROM project_nodes WHERE id = ?", (duplicate_id,))


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


def _purge_expired_recycled_clips_locked(conn: sqlite3.Connection) -> None:
    """清理超过保留期的回收站素材。"""
    expired_rows = conn.execute(
        """
        SELECT clip_id
        FROM recycled_clips
        WHERE datetime(expires_at) <= datetime('now')
        """
    ).fetchall()
    clip_ids = [int(row["clip_id"]) for row in expired_rows]
    if not clip_ids:
        return

    placeholders = ",".join("?" for _ in clip_ids)
    conn.execute(f"DELETE FROM recycled_clips WHERE clip_id IN ({placeholders})", clip_ids)
    conn.execute(f"DELETE FROM clip_tags WHERE clip_id IN ({placeholders})", clip_ids)
    conn.execute(f"DELETE FROM transcripts WHERE clip_id IN ({placeholders})", clip_ids)
    conn.execute(f"DELETE FROM clip_node_refs WHERE clip_id IN ({placeholders})", clip_ids)
    conn.execute(f"DELETE FROM clips WHERE id IN ({placeholders})", clip_ids)


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
                    preview_status TEXT NOT NULL DEFAULT 'pending',
                    preview_path TEXT,
                    preview_error_message TEXT,
                    comparison_status TEXT NOT NULL DEFAULT 'pending',
                    comparison_scores_json TEXT,
                    comparison_error_message TEXT,
                    user_note TEXT,
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

                CREATE TABLE IF NOT EXISTS project_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_id INTEGER NOT NULL,
                    parent_id INTEGER,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE,
                    FOREIGN KEY (parent_id) REFERENCES project_nodes(id) ON DELETE CASCADE,
                    UNIQUE (library_id, parent_id, name)
                );

                CREATE TABLE IF NOT EXISTS clip_node_refs (
                    clip_id INTEGER NOT NULL,
                    node_id INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE,
                    FOREIGN KEY (node_id) REFERENCES project_nodes(id) ON DELETE CASCADE,
                    UNIQUE (clip_id, node_id)
                );

                CREATE TABLE IF NOT EXISTS recycled_clips (
                    clip_id INTEGER PRIMARY KEY,
                    library_id INTEGER NOT NULL,
                    deleted_node_ids_json TEXT NOT NULL DEFAULT '[]',
                    deleted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE,
                    FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_project_nodes_library_id ON project_nodes(library_id);
                CREATE INDEX IF NOT EXISTS idx_project_nodes_parent_id ON project_nodes(parent_id);
                CREATE INDEX IF NOT EXISTS idx_clip_node_refs_node_id ON clip_node_refs(node_id);
                CREATE INDEX IF NOT EXISTS idx_clip_node_refs_clip_id ON clip_node_refs(clip_id);
                CREATE INDEX IF NOT EXISTS idx_recycled_clips_library_id ON recycled_clips(library_id);
                CREATE INDEX IF NOT EXISTS idx_recycled_clips_expires_at ON recycled_clips(expires_at);
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
            if "preview_status" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN preview_status TEXT NOT NULL DEFAULT 'pending'")
            if "preview_path" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN preview_path TEXT")
            if "preview_error_message" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN preview_error_message TEXT")
            if "comparison_status" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN comparison_status TEXT NOT NULL DEFAULT 'pending'")
            if "comparison_scores_json" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN comparison_scores_json TEXT")
            if "comparison_error_message" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN comparison_error_message TEXT")
            if "user_note" not in columns:
                conn.execute("ALTER TABLE clips ADD COLUMN user_note TEXT")
            conn.execute("UPDATE clips SET library_id = 1 WHERE library_id IS NULL")
            conn.execute(
                "UPDATE clips SET transcript_status = 'pending' "
                "WHERE transcript_status IS NULL OR transcript_status = ''"
            )
            conn.execute(
                "UPDATE clips SET preview_status = 'pending' "
                "WHERE preview_status IS NULL OR preview_status = ''"
            )
            conn.execute(
                "UPDATE clips SET comparison_status = 'pending' "
                "WHERE comparison_status IS NULL OR comparison_status = ''"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_library_id ON clips(library_id)")
            library_rows = conn.execute("SELECT id FROM libraries").fetchall()
            for library_row in library_rows:
                existing_root = conn.execute(
                    """
                    SELECT id
                    FROM project_nodes
                    WHERE library_id = ? AND parent_id IS NULL
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (int(library_row["id"]),),
                ).fetchone()
                if existing_root is None:
                    conn.execute(
                        """
                        INSERT INTO project_nodes (library_id, parent_id, name)
                        VALUES (?, NULL, ?)
                        """,
                        (int(library_row["id"]), ROOT_NODE_NAME),
                    )
            _normalize_project_roots_locked(conn)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_project_nodes_root_unique
                ON project_nodes(library_id) WHERE parent_id IS NULL
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO clip_node_refs (clip_id, node_id)
                SELECT c.id, n.id
                FROM clips c
                JOIN project_nodes n
                  ON n.library_id = c.library_id
                 AND n.parent_id IS NULL
                 AND n.name = ?
                LEFT JOIN clip_node_refs r ON r.clip_id = c.id
                WHERE r.clip_id IS NULL
                """,
                (ROOT_NODE_NAME,),
            )
            _purge_expired_recycled_clips_locked(conn)
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
                COUNT(DISTINCT CASE WHEN rc.clip_id IS NULL THEN c.id END) AS clip_count
            FROM libraries l
            LEFT JOIN clips c ON c.library_id = l.id AND c.status = 'done'
            LEFT JOIN recycled_clips rc ON rc.clip_id = c.id
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
                COUNT(DISTINCT CASE WHEN rc.clip_id IS NULL THEN c.id END) AS clip_count
            FROM libraries l
            LEFT JOIN clips c ON c.library_id = l.id AND c.status = 'done'
            LEFT JOIN recycled_clips rc ON rc.clip_id = c.id
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
                """
                SELECT COUNT(DISTINCT c.id) AS count
                FROM clips c
                LEFT JOIN recycled_clips rc ON rc.clip_id = c.id
                WHERE c.library_id = ? AND rc.clip_id IS NULL
                """,
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
    """批量把素材移入项目回收站。"""
    if not clip_ids:
        return 0

    conn = get_connection(db_path)
    placeholders = ",".join("?" for _ in clip_ids)
    params = [library_id, *clip_ids]

    def _write() -> int:
        with _DB_LOCK:
            rows = conn.execute(
                f"""
                SELECT c.id
                FROM clips c
                LEFT JOIN recycled_clips rc ON rc.clip_id = c.id
                WHERE c.library_id = ? AND c.id IN ({placeholders}) AND rc.clip_id IS NULL
                """,
                params,
            ).fetchall()
            target_ids = [int(row["id"]) for row in rows]
            if not target_ids:
                return 0

            deleted_node_rows = conn.execute(
                f"""
                SELECT clip_id, GROUP_CONCAT(node_id) AS node_ids_text
                FROM clip_node_refs
                WHERE clip_id IN ({",".join("?" for _ in target_ids)})
                GROUP BY clip_id
                """,
                target_ids,
            ).fetchall()
            deleted_node_map = {
                int(row["clip_id"]): [
                    int(item)
                    for item in (row["node_ids_text"] or "").split(",")
                    if item.strip()
                ]
                for row in deleted_node_rows
            }
            conn.executemany(
                """
                INSERT OR REPLACE INTO recycled_clips (
                    clip_id,
                    library_id,
                    deleted_node_ids_json,
                    deleted_at,
                    expires_at
                ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, datetime('now', '+7 days'))
                """,
                [
                    (
                        clip_id,
                        library_id,
                        json.dumps(deleted_node_map.get(clip_id, []), ensure_ascii=False),
                    )
                    for clip_id in target_ids
                ],
            )
            delete_placeholders = ",".join("?" for _ in target_ids)
            conn.execute(f"DELETE FROM clip_node_refs WHERE clip_id IN ({delete_placeholders})", target_ids)
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
                    preview_status,
                    preview_path,
                    preview_error_message,
                    comparison_status,
                    comparison_scores_json,
                    comparison_error_message,
                    user_note,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
                    preview_status=excluded.preview_status,
                    preview_path=excluded.preview_path,
                    preview_error_message=excluded.preview_error_message,
                    comparison_status=excluded.comparison_status,
                    comparison_scores_json=excluded.comparison_scores_json,
                    comparison_error_message=excluded.comparison_error_message,
                    user_note=COALESCE(excluded.user_note, clips.user_note),
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
                    record.preview_status,
                    str(record.preview_path) if record.preview_path else None,
                    record.preview_error_message,
                    record.comparison_status,
                    record.comparison_scores_json,
                    record.comparison_error_message,
                    record.user_note,
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
            root_row = conn.execute(
                """
                SELECT id
                FROM project_nodes
                WHERE library_id = ? AND parent_id IS NULL
                LIMIT 1
                """,
                (record.library_id,),
            ).fetchone()
            if root_row is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO clip_node_refs (clip_id, node_id) VALUES (?, ?)",
                    (clip_id, int(root_row["id"])),
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


def update_clip_summary_and_tags(
    clip_id: int,
    library_id: int,
    summary: str,
    new_tags: list[str],
    db_path: Path = _DB_PATH,
) -> None:
    """更新单条素材的摘要，并追加新标签。"""
    cleaned_summary = summary.strip()
    if not cleaned_summary:
        raise ValueError("摘要不能为空")

    cleaned_tags: list[str] = []
    seen_tags: set[str] = set()
    for tag in new_tags:
        cleaned_tag = tag.strip()
        if not cleaned_tag:
            continue
        if cleaned_tag in seen_tags:
            continue
        seen_tags.add(cleaned_tag)
        cleaned_tags.append(cleaned_tag)

    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            duplicate = conn.execute(
                """
                SELECT id
                FROM clips
                WHERE library_id = ? AND summary = ? AND id != ?
                LIMIT 1
                """,
                (library_id, cleaned_summary, clip_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("同一素材库中摘要不能重名")

            conn.execute(
                """
                UPDATE clips
                SET summary = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND library_id = ?
                """,
                (cleaned_summary, clip_id, library_id),
            )
            if conn.total_changes == 0:
                raise ValueError("素材不存在")

            if cleaned_tags:
                conn.executemany(
                    "INSERT OR IGNORE INTO clip_tags (clip_id, tag) VALUES (?, ?)",
                    [(clip_id, tag) for tag in cleaned_tags],
                )
            conn.commit()

    _with_locked_retry(_write)


def update_clip_summary(
    clip_id: int,
    library_id: int,
    summary: str,
    db_path: Path = _DB_PATH,
) -> None:
    """更新单条素材摘要，并校验同素材库内唯一。"""
    cleaned_summary = summary.strip()
    if not cleaned_summary:
        raise ValueError("摘要不能为空")

    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            duplicate = conn.execute(
                """
                SELECT id
                FROM clips
                WHERE library_id = ? AND summary = ? AND id != ?
                LIMIT 1
                """,
                (library_id, cleaned_summary, clip_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("同一素材库中摘要不能重名")

            cursor = conn.execute(
                """
                UPDATE clips
                SET summary = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND library_id = ?
                """,
                (cleaned_summary, clip_id, library_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("素材不存在")
            conn.commit()

    _with_locked_retry(_write)


def append_clip_tags(
    clip_id: int,
    new_tags: list[str],
    db_path: Path = _DB_PATH,
) -> None:
    """为单条素材追加新标签，并自动去重。"""
    cleaned_tags: list[str] = []
    seen_tags: set[str] = set()
    for tag in new_tags:
        cleaned_tag = tag.strip()
        if not cleaned_tag:
            continue
        if cleaned_tag in seen_tags:
            continue
        seen_tags.add(cleaned_tag)
        cleaned_tags.append(cleaned_tag)

    if not cleaned_tags:
        raise ValueError("请至少填写一个标签")

    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            row = conn.execute("SELECT id FROM clips WHERE id = ?", (clip_id,)).fetchone()
            if row is None:
                raise ValueError("素材不存在")
            conn.executemany(
                "INSERT OR IGNORE INTO clip_tags (clip_id, tag) VALUES (?, ?)",
                [(clip_id, tag) for tag in cleaned_tags],
            )
            conn.execute(
                "UPDATE clips SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (clip_id,),
            )
            conn.commit()

    _with_locked_retry(_write)


def update_clip_note(
    clip_id: int,
    note: str,
    db_path: Path = _DB_PATH,
) -> None:
    """更新单条素材的用户批注。"""
    cleaned_note = note.strip()
    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            cursor = conn.execute(
                """
                UPDATE clips
                SET user_note = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cleaned_note or None, clip_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("素材不存在")
            conn.commit()

    _with_locked_retry(_write)


def list_project_nodes(library_id: int, db_path: Path = _DB_PATH) -> list[ProjectNodeRecord]:
    """读取项目内的文件夹树节点。"""
    conn = get_connection(db_path)
    with _DB_LOCK:
        rows = conn.execute(
            """
            WITH RECURSIVE node_tree AS (
                SELECT id, library_id, parent_id, name, 0 AS depth
                FROM project_nodes
                WHERE library_id = ? AND parent_id IS NULL
                UNION ALL
                SELECT n.id, n.library_id, n.parent_id, n.name, node_tree.depth + 1 AS depth
                FROM project_nodes n
                JOIN node_tree ON n.parent_id = node_tree.id
            )
            SELECT
                node_tree.id,
                node_tree.library_id,
                node_tree.parent_id,
                node_tree.name,
                node_tree.depth,
                COUNT(DISTINCT CASE WHEN rc.clip_id IS NULL THEN r.clip_id END) AS clip_count
            FROM node_tree
            LEFT JOIN clip_node_refs r ON r.node_id = node_tree.id
            LEFT JOIN recycled_clips rc ON rc.clip_id = r.clip_id
            GROUP BY node_tree.id, node_tree.library_id, node_tree.parent_id, node_tree.name, node_tree.depth
            ORDER BY node_tree.depth ASC, node_tree.parent_id ASC, node_tree.id ASC
            """,
            (library_id,),
        ).fetchall()
    return [
        ProjectNodeRecord(
            id=int(row["id"]),
            library_id=int(row["library_id"]),
            parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
            name=row["name"],
            depth=int(row["depth"]),
            clip_count=int(row["clip_count"]),
        )
        for row in rows
    ]


def get_project_node(node_id: int, db_path: Path = _DB_PATH) -> ProjectNodeRecord | None:
    """按 ID 读取单个项目节点。"""
    conn = get_connection(db_path)
    with _DB_LOCK:
        row = conn.execute(
            """
            SELECT
                n.id,
                n.library_id,
                n.parent_id,
                n.name,
                COUNT(DISTINCT CASE WHEN rc.clip_id IS NULL THEN r.clip_id END) AS clip_count
            FROM project_nodes n
            LEFT JOIN clip_node_refs r ON r.node_id = n.id
            LEFT JOIN recycled_clips rc ON rc.clip_id = r.clip_id
            WHERE n.id = ?
            GROUP BY n.id, n.library_id, n.parent_id, n.name
            """,
            (node_id,),
        ).fetchone()
    if row is None:
        return None
    depth = 0
    parent_id = row["parent_id"]
    while parent_id is not None:
        parent_row = conn.execute("SELECT parent_id FROM project_nodes WHERE id = ?", (parent_id,)).fetchone()
        if parent_row is None:
            break
        depth += 1
        parent_id = parent_row["parent_id"]
    return ProjectNodeRecord(
        id=int(row["id"]),
        library_id=int(row["library_id"]),
        parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
        name=row["name"],
        depth=depth,
        clip_count=int(row["clip_count"]),
    )


def create_project_node(
    library_id: int,
    name: str,
    parent_id: int | None = None,
    db_path: Path = _DB_PATH,
) -> ProjectNodeRecord:
    """在项目内创建文件夹节点。"""
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("文件夹名称不能为空")
    conn = get_connection(db_path)

    def _write() -> ProjectNodeRecord:
        with _DB_LOCK:
            if parent_id is not None:
                parent_row = conn.execute(
                    "SELECT id FROM project_nodes WHERE id = ? AND library_id = ?",
                    (parent_id, library_id),
                ).fetchone()
                if parent_row is None:
                    raise ValueError("父文件夹不存在")
            cursor = conn.execute(
                "INSERT INTO project_nodes (library_id, parent_id, name) VALUES (?, ?, ?)",
                (library_id, parent_id, cleaned_name),
            )
            conn.commit()
            node = get_project_node(int(cursor.lastrowid), db_path)
            if node is None:
                raise ValueError("文件夹创建失败")
            return node

    return _with_locked_retry(_write)


def rename_project_node(
    node_id: int,
    library_id: int,
    new_name: str,
    db_path: Path = _DB_PATH,
) -> ProjectNodeRecord:
    """重命名项目节点。"""
    cleaned_name = new_name.strip()
    if not cleaned_name:
        raise ValueError("文件夹名称不能为空")
    conn = get_connection(db_path)

    def _write() -> ProjectNodeRecord:
        with _DB_LOCK:
            row = conn.execute(
                "SELECT parent_id FROM project_nodes WHERE id = ? AND library_id = ?",
                (node_id, library_id),
            ).fetchone()
            if row is None:
                raise ValueError("文件夹不存在")
            if row["parent_id"] is None:
                raise ValueError("根节点不支持重命名")
            conn.execute(
                "UPDATE project_nodes SET name = ? WHERE id = ? AND library_id = ?",
                (cleaned_name, node_id, library_id),
            )
            conn.commit()
            node = get_project_node(node_id, db_path)
            if node is None:
                raise ValueError("文件夹不存在")
            return node

    return _with_locked_retry(_write)


def move_project_node(
    node_id: int,
    library_id: int,
    target_parent_id: int | None,
    db_path: Path = _DB_PATH,
) -> ProjectNodeRecord:
    """移动项目节点到新的父节点下。"""
    conn = get_connection(db_path)

    def _write() -> ProjectNodeRecord:
        with _DB_LOCK:
            row = conn.execute(
                "SELECT parent_id FROM project_nodes WHERE id = ? AND library_id = ?",
                (node_id, library_id),
            ).fetchone()
            if row is None:
                raise ValueError("文件夹不存在")
            if row["parent_id"] is None:
                raise ValueError("根节点不支持移动")

            if target_parent_id is not None:
                target_row = conn.execute(
                    "SELECT id FROM project_nodes WHERE id = ? AND library_id = ?",
                    (target_parent_id, library_id),
                ).fetchone()
                if target_row is None:
                    raise ValueError("目标文件夹不存在")

                descendants = conn.execute(
                    """
                    WITH RECURSIVE descendants AS (
                        SELECT id FROM project_nodes WHERE id = ?
                        UNION ALL
                        SELECT n.id
                        FROM project_nodes n
                        JOIN descendants d ON n.parent_id = d.id
                    )
                    SELECT id FROM descendants
                    """,
                    (node_id,),
                ).fetchall()
                descendant_ids = {int(item["id"]) for item in descendants}
                if target_parent_id in descendant_ids:
                    raise ValueError("不能把文件夹移动到自己的子节点下")

            conn.execute(
                "UPDATE project_nodes SET parent_id = ? WHERE id = ? AND library_id = ?",
                (target_parent_id, node_id, library_id),
            )
            conn.commit()
            node = get_project_node(node_id, db_path)
            if node is None:
                raise ValueError("文件夹不存在")
            return node

    return _with_locked_retry(_write)


def delete_project_node(
    node_id: int,
    library_id: int,
    db_path: Path = _DB_PATH,
) -> None:
    """删除空的项目节点。"""
    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            row = conn.execute(
                "SELECT parent_id FROM project_nodes WHERE id = ? AND library_id = ?",
                (node_id, library_id),
            ).fetchone()
            if row is None:
                raise ValueError("文件夹不存在")
            if row["parent_id"] is None:
                raise ValueError("根节点不支持删除")

            child_row = conn.execute(
                "SELECT 1 FROM project_nodes WHERE parent_id = ? LIMIT 1",
                (node_id,),
            ).fetchone()
            if child_row is not None:
                raise ValueError("请先移动或删除子文件夹")

            clip_row = conn.execute(
                "SELECT 1 FROM clip_node_refs WHERE node_id = ? LIMIT 1",
                (node_id,),
            ).fetchone()
            if clip_row is not None:
                raise ValueError("请先移走该文件夹里的素材")

            conn.execute(
                "DELETE FROM project_nodes WHERE id = ? AND library_id = ?",
                (node_id, library_id),
            )
            conn.commit()

    _with_locked_retry(_write)


def attach_clip_to_node(
    clip_id: int,
    node_id: int,
    db_path: Path = _DB_PATH,
) -> None:
    """把素材引用到指定节点。"""
    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            row = conn.execute(
                """
                SELECT c.id
                FROM clips c
                JOIN project_nodes n ON n.library_id = c.library_id
                WHERE c.id = ? AND n.id = ?
                """,
                (clip_id, node_id),
            ).fetchone()
            if row is None:
                raise ValueError("素材或目标文件夹不存在")
            conn.execute(
                "INSERT OR IGNORE INTO clip_node_refs (clip_id, node_id) VALUES (?, ?)",
                (clip_id, node_id),
            )
            conn.execute("DELETE FROM recycled_clips WHERE clip_id = ?", (clip_id,))
            conn.commit()

    _with_locked_retry(_write)


def move_clip_to_node(
    clip_id: int,
    library_id: int,
    target_node_id: int | None,
    db_path: Path = _DB_PATH,
) -> None:
    """把素材移动到指定文件夹；None 表示移回未分类。"""
    conn = get_connection(db_path)

    def _write() -> None:
        with _DB_LOCK:
            root_row = conn.execute(
                """
                SELECT id
                FROM project_nodes
                WHERE library_id = ? AND parent_id IS NULL
                LIMIT 1
                """,
                (library_id,),
            ).fetchone()
            if root_row is None:
                raise ValueError("根文件夹不存在")
            root_id = int(root_row["id"])

            clip_row = conn.execute(
                "SELECT id FROM clips WHERE id = ? AND library_id = ?",
                (clip_id, library_id),
            ).fetchone()
            if clip_row is None:
                raise ValueError("素材不存在")

            if target_node_id is not None:
                target_row = conn.execute(
                    """
                    SELECT id
                    FROM project_nodes
                    WHERE id = ? AND library_id = ? AND parent_id IS NOT NULL
                    """,
                    (target_node_id, library_id),
                ).fetchone()
                if target_row is None:
                    raise ValueError("目标文件夹不存在")

            conn.execute(
                """
                DELETE FROM clip_node_refs
                WHERE clip_id = ? AND node_id IN (
                    SELECT id FROM project_nodes WHERE library_id = ? AND parent_id IS NOT NULL
                )
                """,
                (clip_id, library_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO clip_node_refs (clip_id, node_id) VALUES (?, ?)",
                (clip_id, root_id),
            )
            if target_node_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO clip_node_refs (clip_id, node_id) VALUES (?, ?)",
                    (clip_id, target_node_id),
                )
            conn.execute("DELETE FROM recycled_clips WHERE clip_id = ?", (clip_id,))
            conn.commit()

    _with_locked_retry(_write)


def list_recycled_clips(library_id: int, db_path: Path = _DB_PATH) -> list[RecycleClipRecord]:
    """读取项目级回收站素材。"""
    conn = get_connection(db_path)
    with _DB_LOCK:
        _purge_expired_recycled_clips_locked(conn)
        rows = conn.execute(
            """
            SELECT
                rc.clip_id,
                rc.library_id,
                rc.deleted_node_ids_json,
                rc.deleted_at,
                rc.expires_at,
                c.summary,
                c.filename
            FROM recycled_clips rc
            JOIN clips c ON c.id = rc.clip_id
            WHERE rc.library_id = ?
            ORDER BY rc.deleted_at DESC
            """,
            (library_id,),
        ).fetchall()
        conn.commit()
    return [
        RecycleClipRecord(
            clip_id=int(row["clip_id"]),
            library_id=int(row["library_id"]),
            summary=row["summary"] or "暂无摘要",
            filename=row["filename"],
            deleted_at=row["deleted_at"],
            expires_at=row["expires_at"],
            deleted_node_ids=json.loads(row["deleted_node_ids_json"] or "[]"),
        )
        for row in rows
    ]


def restore_recycled_clips(
    library_id: int,
    clip_ids: list[int],
    db_path: Path = _DB_PATH,
) -> int:
    """从项目回收站恢复素材。"""
    if not clip_ids:
        return 0
    conn = get_connection(db_path)

    def _write() -> int:
        with _DB_LOCK:
            rows = conn.execute(
                f"""
                SELECT clip_id, deleted_node_ids_json
                FROM recycled_clips
                WHERE library_id = ? AND clip_id IN ({",".join("?" for _ in clip_ids)})
                """,
                [library_id, *clip_ids],
            ).fetchall()
            if not rows:
                return 0
            restored_count = 0
            root_row = conn.execute(
                "SELECT id FROM project_nodes WHERE library_id = ? AND parent_id IS NULL LIMIT 1",
                (library_id,),
            ).fetchone()
            root_id = int(root_row["id"]) if root_row is not None else None
            for row in rows:
                clip_id = int(row["clip_id"])
                node_ids = json.loads(row["deleted_node_ids_json"] or "[]")
                valid_nodes = [
                    int(node_id)
                    for node_id in node_ids
                    if conn.execute(
                        "SELECT 1 FROM project_nodes WHERE id = ? AND library_id = ?",
                        (int(node_id), library_id),
                    ).fetchone()
                    is not None
                ]
                if not valid_nodes and root_id is not None:
                    valid_nodes = [root_id]
                conn.executemany(
                    "INSERT OR IGNORE INTO clip_node_refs (clip_id, node_id) VALUES (?, ?)",
                    [(clip_id, node_id) for node_id in valid_nodes],
                )
                conn.execute("DELETE FROM recycled_clips WHERE clip_id = ?", (clip_id,))
                restored_count += 1
            conn.commit()
        return restored_count

    return _with_locked_retry(_write)
