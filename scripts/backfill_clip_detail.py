#!/usr/bin/env python3
"""一次性回填脚本：给已分析完成但缺少 rename_title/detail_summary 的素材补上这两个新字段。

复用已经抽好的关键帧（data/thumbnails/{hash}/）重新调用一次视觉模型，
不重新做 ASR，也不改动 summary/scene/tags 等既有字段。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from ai import analyze_video  # noqa: E402
from db import clips, get_engine, init_db, load_transcripts, update_clip_detail  # noqa: E402
from pipeline import get_keyframe_paths  # noqa: E402

DB_PATH = PROJECT_DIR / "data" / "reelsift.db"
CACHE_DIR = PROJECT_DIR / "data" / "thumbnails"


def _pending_clips(db_path: Path, limit: int | None) -> list[dict]:
    """找出已完成分析、但 rename_title 或 detail_summary 还是空的素材。"""
    with get_engine(db_path).connect() as conn:
        rows = (
            conn.execute(
                clips.select()
                .where(clips.c.status == "done")
                .order_by(clips.c.id.asc())
            )
            .mappings()
            .all()
        )
    pending = [
        dict(row)
        for row in rows
        if not str(row.get("rename_title") or "").strip()
        or not str(row.get("detail_summary") or "").strip()
    ]
    return pending[:limit] if limit else pending


def _build_transcript_text(clip_id: int, db_path: Path) -> str:
    segments = load_transcripts(clip_id, db_path)[:6]
    return " ".join(segment.text for segment in segments)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条，默认不限制")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写数据库")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--sleep", type=float, default=0.5, help="每条之间的等待秒数，避免打满 API")
    args = parser.parse_args()

    init_db(args.db_path)
    pending = _pending_clips(args.db_path, args.limit)
    if not pending:
        print("没有需要回填的素材。")
        return

    print(f"共 {len(pending)} 条待回填素材（{'dry-run，不写库' if args.dry_run else '将写入数据库'}）。")

    updated = 0
    skipped = 0
    failed = 0

    for index, row in enumerate(pending, start=1):
        clip_id = int(row["id"])
        filename = str(row["filename"])
        video_hash = str(row["video_hash"])
        frame_dir = CACHE_DIR / video_hash
        frame_paths = get_keyframe_paths(frame_dir)
        if not frame_paths:
            print(f"[{index}/{len(pending)}] #{clip_id} {filename}：找不到缓存关键帧，跳过。")
            skipped += 1
            continue

        transcript_text = _build_transcript_text(clip_id, args.db_path)
        try:
            analysis = analyze_video(frame_paths, transcript_text or None)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整体
            print(f"[{index}/{len(pending)}] #{clip_id} {filename}：AI 调用失败：{exc}")
            failed += 1
            continue

        print(
            f"[{index}/{len(pending)}] #{clip_id} {filename}\n"
            f"    标题：{analysis.rename_title}\n"
            f"    详情：{analysis.detail}"
        )

        if not args.dry_run:
            update_clip_detail(
                clip_id=clip_id,
                rename_title=analysis.rename_title,
                detail_summary=analysis.detail,
                db_path=args.db_path,
            )
        updated += 1

        if args.sleep > 0 and index < len(pending):
            time.sleep(args.sleep)

    verb = "生成（dry-run，未写库）" if args.dry_run else "更新"
    print(f"完成：{verb} {updated} 条，跳过（无关键帧）{skipped} 条，失败 {failed} 条。")


if __name__ == "__main__":
    main()
