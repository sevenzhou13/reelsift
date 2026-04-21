# FastAPI 服务器：读取 SQLite 结果并渲染素材列表页

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "reelsift.db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


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
    error_message: str | None


app = FastAPI(title="Reelsift")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/data", StaticFiles(directory=BASE_DIR / "data"), name="data")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_connection() -> sqlite3.Connection:
    """创建只读连接用于页面查询。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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


def load_clips(query: str = "", tag: str = "") -> list[ClipCard]:
    """从 SQLite 读取素材卡片数据，并支持关键词与标签过滤。"""
    conn = get_connection()
    filters: list[str] = []
    params: list[Any] = []

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
            GROUP_CONCAT(t.tag, '|||') AS tags_text
        FROM clips c
        LEFT JOIN clip_tags t ON t.clip_id = c.id
        {where_sql}
        GROUP BY c.id
        ORDER BY c.updated_at DESC, c.id DESC
        """,
        params,
    ).fetchall()

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
                error_message=row["error_message"],
            )
        )
    conn.close()
    return cards


def load_tags() -> list[tuple[str, int]]:
    """读取所有标签及其数量。"""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT tag, COUNT(*) AS count
        FROM clip_tags
        GROUP BY tag
        ORDER BY count DESC, tag ASC
        """
    ).fetchall()
    conn.close()
    return [(row["tag"], int(row["count"])) for row in rows]


def build_stats(clips: list[ClipCard]) -> dict[str, int]:
    """计算卡片统计信息。"""
    return {
        "total": len(clips),
        "done": sum(1 for clip in clips if clip.status == "done"),
        "failed": sum(1 for clip in clips if clip.status == "failed"),
    }


@app.get("/", include_in_schema=False)
def index(
    request: Request,
    q: str = Query(default=""),
    tag: str = Query(default=""),
):
    """渲染素材网格首页。"""
    clips = load_clips(query=q.strip(), tag=tag.strip())
    stats = {
        "total": len(clips),
        "done": sum(1 for clip in clips if clip.status == "done"),
        "failed": sum(1 for clip in clips if clip.status == "failed"),
    }
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "clips": clips,
            "stats": stats,
            "all_tags": load_tags(),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
        },
    )


@app.get("/clips", include_in_schema=False)
def clips_partial(
    request: Request,
    q: str = Query(default=""),
    tag: str = Query(default=""),
):
    """返回素材网格局部 HTML，供 HTMX 刷新。"""
    clips = load_clips(query=q.strip(), tag=tag.strip())
    return templates.TemplateResponse(
        request=request,
        name="partials/clip_grid.html",
        context={
            "clips": clips,
            "stats": build_stats(clips),
            "current_query": q.strip(),
            "current_tag": tag.strip(),
        },
    )
