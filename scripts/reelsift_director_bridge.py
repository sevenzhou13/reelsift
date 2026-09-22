#!/usr/bin/env python3
# 原生导演台的数据桥：读取本地索引和 Story Canvas 状态，不启动 Web 服务。
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from db import (  # noqa: E402
    StoryboardItemRecord, StoryboardMessageRecord, add_storyboard_message, clips,
    create_story_suggestion, create_storyboard, get_engine,
    get_library_by_id, get_storyboard, init_db, list_story_beat_clips, list_story_beats,
    list_story_suggestions, list_storyboard_messages, list_storyboards, load_transcripts,
    materialize_storyboard_beats,
    move_story_beat_clip, reorder_story_beats, resolve_story_suggestion,
    update_clip_narrative_tags, update_clip_note, update_clip_note_status,
    update_library_memory_note, update_story_beat,
    update_storyboard_error, update_storyboard_result,
)
from story_ai import (  # noqa: E402
    ChatBeatContext, DEFAULT_TONE_PROMPT, StoryBeatContext, StoryClipContext,
    generate_beat_order_interpretation, generate_story_agent_reply,
    generate_story_move_interpretation, StoryDirection, generate_story_directions,
    generate_story_reorder_interpretation, generate_storyboard_plan,
    strip_native_agent_suggestion, _parse_native_agent_suggestion,
)

DB_PATH = Path(os.environ.get("REELSIFT_DB_PATH", PROJECT_DIR / "data" / "reelsift.db"))


def build_cover_path(value: str | None) -> str:
    """只返回存在的本地封面路径。"""
    return str(Path(value)) if value and Path(value).exists() else ""


def build_optional_path(value: str | None) -> str:
    """优先返回存在的预览文件，原生端可回退原素材。"""
    return str(Path(value)) if value and Path(value).exists() else ""


def serialize_storyboard(story: Any) -> dict[str, object]:
    """对原生端暴露稳定的 Story 元数据。"""
    return {
        "id": story.id, "library_id": story.library_id, "title": story.title,
        "brief_text": story.brief_text, "thesis": story.core_message or story.brief_text,
        "core_message": story.core_message or "", "script_text": story.script_text or "",
        "story_plan": story.story_plan or "", "status": story.status,
        "target_duration_seconds": story.target_duration_seconds, "updated_at": story.updated_at or "",
    }


def serialize_suggestion(item: Any) -> dict[str, object]:
    """暴露待确认建议；Canvas 必须经 Apply/Dismiss 才会写入 Beat。"""
    return {
        "id": item.id, "storyboard_id": item.storyboard_id, "type": item.suggestion_type,
        "status": item.status, "source_beat_id": item.source_beat_id,
        "target_beat_id": item.target_beat_id, "clip_id": item.clip_id,
        "explanation": item.explanation, "suggested_intent": item.suggested_intent or "",
        "suggested_script": item.suggested_script or "",
        "suggested_thesis": item.suggested_thesis or "", "error_message": item.error_message or "",
    }


def _load_folder_items(folder: Path, db_path: Path) -> tuple[int, list[dict[str, object]]]:
    """从已完成本地索引中筛选该 Finder 文件夹的素材。"""
    resolved = folder.resolve()
    library_id = 0
    items: list[dict[str, object]] = []
    with get_engine(db_path).connect() as conn:
        rows = conn.execute(clips.select().where(clips.c.status == "done").order_by(clips.c.filename.asc())).mappings().all()
    for row in rows:
        source = Path(str(row["filepath"]))
        try:
            source.resolve().relative_to(resolved)
        except ValueError:
            continue
        row_library_id = int(row["library_id"])
        if library_id and library_id != row_library_id:
            continue
        library_id = row_library_id
        transcript = " ".join(segment.text for segment in load_transcripts(int(row["id"]), db_path)[:3])
        items.append({
            "id": int(row["id"]), "filename": str(row["filename"]), "filepath": str(source),
            "preview_path": build_optional_path(row.get("preview_path")),
            "cover_path": build_cover_path(row.get("cover_path")),
            "summary": str(row.get("summary") or "暂无摘要"),
            "rename_title": str(row.get("rename_title") or "").strip(),
            "detail_summary": str(row.get("detail_summary") or "").strip(),
            "scene": str(row.get("scene") or ""),
            "visual_tags": [str(row.get("scene") or "")] + list(row.get("subjects_json") or []) + list(row.get("actions_json") or []),
            "narrative_tags": list(row.get("narrative_tags_json") or []),
            "user_note": str(row.get("user_note") or ""),
            "note_status": str(row.get("note_status") or "pending"), "transcript": transcript,
        })
    return library_id, sorted(items, key=lambda item: str(item["filename"]))


def _serialize_canvas(story_id: int, items: list[dict[str, object]], db_path: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """将 Beat 的关联素材和 Unused 素材从同一真实索引构造出来。"""
    clips_by_id = {int(item["id"]): dict(item) for item in items}
    clips_by_beat: dict[int, list[dict[str, object]]] = {}
    used_ids: set[int] = set()
    for relation in list_story_beat_clips(story_id, db_path):
        clip = clips_by_id.get(relation.clip_id)
        if clip is None:
            continue
        linked = dict(clip)
        linked.update({
            "beat_clip_id": relation.id, "position": relation.position, "in_ms": relation.in_ms,
            "out_ms": relation.out_ms, "locked": relation.locked, "must_use": relation.must_use,
        })
        clips_by_beat.setdefault(relation.beat_id, []).append(linked)
        used_ids.add(relation.clip_id)
    beats = [{
        "id": beat.id, "storyboard_id": beat.storyboard_id, "title": beat.title,
        "position": beat.position, "intent": beat.intent, "script": beat.script_text or "",
        "note": beat.user_note or "", "locked": beat.locked, "must_use": beat.must_use,
        "ai_generated": beat.ai_generated, "user_edited": beat.user_edited,
        "clips": clips_by_beat.get(beat.id, []),
    } for beat in list_story_beats(story_id, db_path)]
    return beats, [item for item in items if int(item["id"]) not in used_ids]


def load_folder(folder: Path, db_path: Path = DB_PATH, *, allow_restore: bool = True) -> dict[str, object]:
    """加载已有索引；无条目时仅从已有 CSV 恢复，绝不重新视觉识别。"""
    init_db(db_path)
    resolved = folder.resolve()
    library_id, items = _load_folder_items(resolved, db_path)
    restore_warning = ""
    restored_count = 0
    if not items and allow_restore and (resolved / "Reelsift整理清单.csv").exists() and db_path == DB_PATH:
        try:
            from scripts.reelsift_finder_organize import restore_manifest_index
            restored_count = restore_manifest_index(resolved)
            library_id, items = _load_folder_items(resolved, db_path)
        except Exception as exc:
            restore_warning = f"从 Reelsift整理清单.csv 恢复索引失败：{exc}"
    library = get_library_by_id(library_id, db_path) if library_id else None
    story_payload: dict[str, object] | None = None
    beats: list[dict[str, object]] = []
    unused = list(items)
    suggestions: list[dict[str, object]] = []
    if library_id:
        stories = list_storyboards(library_ids=[library_id], limit=20, db_path=db_path)
        if stories:
            story = stories[0]
            for candidate in stories:
                materialize_storyboard_beats(candidate.id, db_path)
                if list_story_beats(candidate.id, db_path):
                    story = candidate
                    break
            if story.id != stories[0].id and stories[0].status == "failed":
                restore_warning = f"最近一次 Story 生成失败，已恢复上一版 Canvas：{stories[0].error_message or '可重试生成'}"
            story_payload = serialize_storyboard(story)
            beats, unused = _serialize_canvas(story.id, items, db_path)
            suggestions = [serialize_suggestion(item) for item in list_story_suggestions(story.id, status="pending", db_path=db_path)]
    return {
        "folder": str(resolved), "library_id": library_id, "memory_note": (library.memory_note or "") if library else "",
        "items": items, "unused": unused, "story": story_payload, "beats": beats,
        "pending_suggestions": suggestions, "restored_count": restored_count, "restore_warning": restore_warning,
    }


def _build_clip_context(clip_id: int, db_path: Path) -> StoryClipContext:
    """加载一次 AI 调用所需的真实素材事实。"""
    with get_engine(db_path).connect() as conn:
        row = conn.execute(clips.select().where(clips.c.id == clip_id)).mappings().first()
    if row is None:
        raise ValueError("素材不存在。")
    transcript = " ".join(item.text for item in load_transcripts(clip_id, db_path)[:6])
    return StoryClipContext(
        clip_id=clip_id, filename=str(row["filename"]), summary=str(row.get("summary") or "暂无摘要"),
        scene=str(row.get("scene") or ""), tags=[str(item) for item in (row.get("narrative_tags_json") or [])],
        subjects=[str(item) for item in (row.get("subjects_json") or [])],
        actions=[str(item) for item in (row.get("actions_json") or [])],
        user_note=str(row.get("user_note") or "") or None, transcript_text=transcript or None,
        source_time=(datetime.fromtimestamp(float(row["source_modified_at"])).isoformat(timespec="minutes") if row.get("source_modified_at") else None),
        rating=int(row.get("rating") or 0), is_favorite=bool(row.get("is_favorite")),
    )


def _ordered_beat_clip_ids(storyboard_id: int, beat_id: int, db_path: Path) -> list[int]:
    """读取一个 Beat 内当前的真实素材顺序，用于直接编辑 suggestion 的失效判断。"""
    return [
        relation.clip_id
        for relation in list_story_beat_clips(storyboard_id, db_path)
        if relation.beat_id == beat_id
    ]


def _build_creator_brief(
    *, memory_note: str, theme: str = "", requirements: str = "",
    direction: StoryDirection | None = None,
) -> str:
    """把用户 Brief、文件夹随手记与选中方向收敛为现有 Story 生成可复用的文本。"""
    parts: list[str] = []
    if theme.strip():
        parts.append(f"创作者主题：{theme.strip()}")
    if memory_note.strip():
        parts.append(f"文件夹随手记：{memory_note.strip()}")
    if requirements.strip():
        parts.append(f"额外要求：{requirements.strip()}")
    if direction is not None:
        parts.extend([
            f"已确认故事方向：{direction.title}",
            f"风格：{direction.style}",
            "方向结构：" + " → ".join(direction.structure),
        ])
    return "\n".join(parts) or "把这批真实素材组织成一段自然、具体的第一人称记录。"


def serialize_direction(direction: StoryDirection) -> dict[str, object]:
    """以原生端可直接显示的稳定 JSON 输出一个方向。"""
    return {
        "id": direction.id, "title": direction.title, "style": direction.style,
        "structure": direction.structure,
    }


def generate_directions(
    folder: Path, *, theme: str = "", tone: str = "", requirements: str = "",
    duration: int = 60, db_path: Path = DB_PATH,
) -> dict[str, object]:
    """生成三个供创作者挑选的故事方向；不写入任何 Story Canvas。"""
    payload = load_folder(folder, db_path)
    library_id = int(payload["library_id"])
    items = list(payload["items"])
    if not library_id or not items:
        raise ValueError("当前 Finder 文件夹没有可用的已识别素材。")
    target_duration = max(15, min(int(duration), 1200))
    effective_tone = tone.strip() or DEFAULT_TONE_PROMPT
    brief = _build_creator_brief(
        memory_note=str(payload.get("memory_note") or ""), theme=theme, requirements=requirements,
    )
    contexts = [_build_clip_context(int(item["id"]), db_path) for item in items]
    directions = generate_story_directions(
        clips=contexts, brief_text=brief, target_duration_seconds=target_duration,
        tone_prompt=effective_tone,
    )
    return {
        "ok": True, "directions": [serialize_direction(item) for item in directions.directions],
        "brief": brief, "tone": effective_tone, "duration": target_duration,
        "context": {
            "clip_count": len(items),
            "creator_note_count": sum(1 for item in items if str(item.get("user_note") or "").strip()),
            "has_library_note": bool(str(payload.get("memory_note") or "").strip()),
        },
    }


def generate_story(folder: Path, *, title: str = "", brief: str = "", duration: int = 60,
                   tone_prompt: str = DEFAULT_TONE_PROMPT, db_path: Path = DB_PATH,
                   payload: dict[str, object] | None = None,
                   contexts: list[StoryClipContext] | None = None) -> dict[str, object]:
    """复用 Web 的完整 Storyboard 生成，再物化为原生 Canvas Beat。"""
    payload = payload or load_folder(folder, db_path)
    library_id = int(payload["library_id"])
    items = list(payload["items"])
    if not library_id or not items:
        raise ValueError("当前 Finder 文件夹没有可用的已识别素材。")
    clean_brief = brief.strip() or str(payload.get("memory_note") or "") or "把这批真实素材组织成一段自然、具体的第一人称记录。"
    contexts = contexts or [_build_clip_context(int(item["id"]), db_path) for item in items]
    story = create_storyboard(
        library_id=library_id, title=title.strip() or folder.resolve().name, brief_text=clean_brief,
        target_duration_seconds=max(15, min(int(duration), 1200)), tone_prompt=tone_prompt,
        selected_clip_ids=[item.clip_id for item in contexts], db_path=db_path,
    )
    try:
        plan = generate_storyboard_plan(clips=contexts, brief_text=clean_brief, target_duration_seconds=story.target_duration_seconds, tone_prompt=story.tone_prompt)
        plan_items = [StoryboardItemRecord(
            id=0, storyboard_id=story.id, clip_id=item.clip_id, position=item.position,
            section_name=item.section, narrative_role=item.role, suggested_duration_seconds=item.suggested_duration_seconds,
            script_line=item.script_line or None, reason=item.reason or None,
        ) for item in plan.clip_order]
        update_storyboard_result(
            storyboard_id=story.id, title=plan.title, core_message=plan.core_message, emotional_arc=plan.emotional_arc,
            story_plan=plan.story_plan, script_text=plan.first_person_script, items=plan_items, db_path=db_path,
        )
        materialize_storyboard_beats(story.id, db_path)
    except Exception as exc:
        update_storyboard_error(story.id, str(exc), db_path)
        return {"ok": False, "storyboard_id": story.id, "error": f"Story 生成失败：{exc}"}
    return {"ok": True, "storyboard_id": story.id, "payload": load_folder(folder, db_path)}


def choose_direction(
    folder: Path, *, direction_json: str, theme: str = "", tone: str = "",
    requirements: str = "", duration: int = 60, db_path: Path = DB_PATH,
) -> dict[str, object]:
    """确认一个方向后才生成并持久化 Canvas，避免把方向卡当成一次性文案。"""
    try:
        direction = StoryDirection.model_validate(json.loads(direction_json))
    except Exception as exc:
        raise ValueError(f"选择的故事方向无效：{exc}") from exc
    payload = load_folder(folder, db_path)
    if not int(payload["library_id"]) or not list(payload["items"]):
        raise ValueError("当前 Finder 文件夹没有可用的已识别素材。")
    brief = _build_creator_brief(
        memory_note=str(payload.get("memory_note") or ""), theme=theme,
        requirements=requirements, direction=direction,
    )
    contexts = [_build_clip_context(int(item["id"]), db_path) for item in list(payload["items"])]
    result = generate_story(
        folder, title=direction.title, brief=brief, duration=duration,
        tone_prompt=tone.strip() or DEFAULT_TONE_PROMPT, db_path=db_path,
        payload=payload, contexts=contexts,
    )
    result["direction"] = serialize_direction(direction)
    return result


def _serialize_move_interpretation_request(
    *, kind: str, storyboard_id: int, clip_id: int, source_beat_id: int | None,
    target_beat_id: int, before_clip_ids: list[int], base_clip_ids: list[int],
    target: Any,
) -> dict[str, object]:
    """记录 AI 建议对应的 Canvas 快照，供后台完成前校验是否已过期。"""
    return {
        "kind": kind,
        "storyboard_id": storyboard_id,
        "clip_id": clip_id,
        "source_beat_id": source_beat_id,
        "target_beat_id": target_beat_id,
        "before_clip_ids": before_clip_ids,
        "base_clip_ids": base_clip_ids,
        "base_intent": target.intent,
        "base_script": target.script_text or "",
    }


def move_clip(*, folder: Path, storyboard_id: int, clip_id: int, target_beat_id: int | None, position: int | None = None, in_ms: int | None = None, out_ms: int | None = None, db_path: Path = DB_PATH) -> dict[str, object]:
    """立即持久化用户拖动，并返回后续后台 AI 建议所需的状态快照。"""
    story = get_storyboard(storyboard_id, db_path)
    if story is None:
        raise ValueError("故事线不存在。")
    beats_before = {beat.id: beat for beat in list_story_beats(storyboard_id, db_path)}
    source_id = next((item.beat_id for item in list_story_beat_clips(storyboard_id, db_path) if item.clip_id == clip_id), None)
    source = beats_before.get(source_id) if source_id else None
    before_clip_ids = _ordered_beat_clip_ids(storyboard_id, source_id, db_path) if source_id else []
    moved_source, moved_target = move_story_beat_clip(
        storyboard_id=storyboard_id, clip_id=clip_id, target_beat_id=target_beat_id, position=position,
        in_ms=in_ms, out_ms=out_ms, db_path=db_path,
    )
    result: dict[str, object] = {
        "ok": True, "moved": True, "source_beat_id": moved_source,
        "target_beat_id": moved_target, "suggestion": None,
    }

    def finish() -> dict[str, object]:
        result["payload"] = load_folder(folder, db_path)
        return result

    if target_beat_id is None:
        return finish()
    target = next((beat for beat in list_story_beats(storyboard_id, db_path) if beat.id == target_beat_id), None)
    if target is None:
        return finish()
    after_clip_ids = _ordered_beat_clip_ids(storyboard_id, target_beat_id, db_path)
    if source_id == target_beat_id and before_clip_ids == after_clip_ids:
        result["interpretation"] = "素材顺序没有变化。"
        return finish()
    result["interpretation_request"] = _serialize_move_interpretation_request(
        kind="reorder_within_beat" if source_id == target_beat_id else "move_clip",
        storyboard_id=storyboard_id,
        clip_id=clip_id,
        source_beat_id=source_id,
        target_beat_id=target_beat_id,
        before_clip_ids=before_clip_ids if source_id == target_beat_id else [],
        base_clip_ids=after_clip_ids,
        target=target,
    )
    return finish()


def reorder_beats(*, folder: Path, storyboard_id: int, beat_ids: list[int], db_path: Path = DB_PATH) -> dict[str, object]:
    """立即持久化 Beat 排序，并返回后续后台 AI 建议所需的状态快照。"""
    story = get_storyboard(storyboard_id, db_path)
    if story is None:
        raise ValueError("故事线不存在。")
    before_beats = list_story_beats(storyboard_id, db_path)
    reordered = reorder_story_beats(storyboard_id=storyboard_id, beat_ids=beat_ids, db_path=db_path)
    after_beats = list_story_beats(storyboard_id, db_path)
    return {
        "ok": True,
        "beats": [{"id": item.id, "position": item.position} for item in reordered],
        "suggestion": None,
        "interpretation_request": {
            "kind": "reorder_beats",
            "storyboard_id": storyboard_id,
            "before_beat_ids": [item.id for item in before_beats],
            "base_beat_ids": [item.id for item in after_beats],
        },
        "payload": load_folder(folder, db_path),
    }


def _is_move_interpretation_current(*, request_payload: dict[str, object], db_path: Path) -> bool:
    """确认后台任务完成时，用户仍停留在它最初观察到的 Canvas 状态。"""
    storyboard_id = int(request_payload["storyboard_id"])
    kind = str(request_payload["kind"])
    if kind == "reorder_beats":
        expected = [int(item) for item in list(request_payload["base_beat_ids"])]
        return [item.id for item in list_story_beats(storyboard_id, db_path)] == expected
    target_beat_id = int(request_payload["target_beat_id"])
    clip_id = int(request_payload["clip_id"])
    target = next((item for item in list_story_beats(storyboard_id, db_path) if item.id == target_beat_id), None)
    if target is None:
        return False
    current_target_id = next(
        (item.beat_id for item in list_story_beat_clips(storyboard_id, db_path) if item.clip_id == clip_id),
        None,
    )
    return (
        current_target_id == target_beat_id
        and _ordered_beat_clip_ids(storyboard_id, target_beat_id, db_path)
        == [int(item) for item in list(request_payload["base_clip_ids"])]
        and target.intent == str(request_payload["base_intent"])
        and (target.script_text or "") == str(request_payload["base_script"])
    )


def interpret_move(*, folder: Path, request_payload: dict[str, object], db_path: Path = DB_PATH) -> dict[str, object]:
    """在后台生成拖动建议；开始与结束各校验一次，避免旧建议写回新 Canvas。"""
    required = {"kind", "storyboard_id"}
    if not required.issubset(request_payload):
        raise ValueError("故事建议请求缺少必要状态。")
    storyboard_id = int(request_payload["storyboard_id"])
    story = get_storyboard(storyboard_id, db_path)
    if story is None:
        raise ValueError("故事线不存在。")
    if not _is_move_interpretation_current(request_payload=request_payload, db_path=db_path):
        return {"ok": True, "stale": True, "interpretation": "素材位置已再次变化，本次较早的 AI 建议已丢弃。"}

    kind = str(request_payload["kind"])
    try:
        if kind == "reorder_beats":
            before_ids = [int(item) for item in list(request_payload["before_beat_ids"])]
            before_by_id = {item.id: item for item in list_story_beats(storyboard_id, db_path)}
            before_beats = [before_by_id[item_id] for item_id in before_ids if item_id in before_by_id]
            after_beats = list_story_beats(storyboard_id, db_path)
            interpretation = generate_beat_order_interpretation(
                thesis=story.core_message or story.brief_text,
                before_beats=[StoryBeatContext(item.title, item.intent, item.script_text) for item in before_beats],
                after_beats=[StoryBeatContext(item.title, item.intent, item.script_text) for item in after_beats],
            )
            suggestion_type = "beat_order_interpretation"
            default_explanation = "这次 Beat 顺序调整可能改变了整段故事的节奏。"
            suggestion_kwargs: dict[str, object] = {
                "payload_json": {"move": "reorder_beats", "base_beat_ids": request_payload["base_beat_ids"]},
            }
        elif kind in {"move_clip", "reorder_within_beat"}:
            target_beat_id = int(request_payload["target_beat_id"])
            source_id = int(request_payload["source_beat_id"]) if request_payload.get("source_beat_id") is not None else None
            clip_id = int(request_payload["clip_id"])
            beats_by_id = {item.id: item for item in list_story_beats(storyboard_id, db_path)}
            target = beats_by_id.get(target_beat_id)
            if target is None:
                return {"ok": True, "stale": True, "interpretation": "目标 Beat 已变化，本次 AI 建议已丢弃。"}
            clip = _build_clip_context(clip_id, db_path)
            if kind == "reorder_within_beat":
                before_ids = [int(item) for item in list(request_payload["before_clip_ids"])]
                after_ids = [int(item) for item in list(request_payload["base_clip_ids"])]
                interpretation = generate_story_reorder_interpretation(
                    thesis=story.core_message or story.brief_text,
                    beat=StoryBeatContext(target.title, target.intent, target.script_text),
                    moved_clip=clip,
                    before_clips=[_build_clip_context(item, db_path) for item in before_ids],
                    after_clips=[_build_clip_context(item, db_path) for item in after_ids],
                )
                suggestion_type = "reorder_interpretation"
                default_explanation = "这次素材重排可能改变了该 Beat 的观看节奏。"
            else:
                source = beats_by_id.get(source_id) if source_id else None
                interpretation = generate_story_move_interpretation(
                    thesis=story.core_message or story.brief_text,
                    source_beat=StoryBeatContext(source.title, source.intent, source.script_text) if source else None,
                    target_beat=StoryBeatContext(target.title, target.intent, target.script_text),
                    clip=clip,
                    move_description=f"将素材 {clip_id} 从「{source.title if source else 'Unused'}」移动到「{target.title}」",
                )
                suggestion_type = "move_interpretation"
                default_explanation = "这次素材移动可能改变了该 Beat 的叙事含义。"
            suggestion_kwargs = {
                "source_beat_id": source_id,
                "target_beat_id": target_beat_id,
                "clip_id": clip_id,
                "payload_json": {
                    "move": kind,
                    "clip_id": clip_id,
                    "base_clip_ids": request_payload["base_clip_ids"],
                    "base_intent": request_payload["base_intent"],
                    "base_script": request_payload["base_script"],
                },
            }
        else:
            raise ValueError("未知的故事建议请求类型。")

        if not _is_move_interpretation_current(request_payload=request_payload, db_path=db_path):
            return {"ok": True, "stale": True, "interpretation": "素材位置已再次变化，本次较早的 AI 建议已丢弃。"}
        if not (interpretation.meaning_changed or interpretation.suggested_intent or interpretation.suggested_script or interpretation.suggested_thesis):
            return {"ok": True, "interpretation": interpretation.explanation or "这次调整没有产生需要修改的故事建议。"}
        suggestion = create_story_suggestion(
            storyboard_id=storyboard_id,
            suggestion_type=suggestion_type,
            explanation=interpretation.explanation or default_explanation,
            suggested_intent=interpretation.suggested_intent,
            suggested_script=interpretation.suggested_script,
            suggested_thesis=interpretation.suggested_thesis,
            db_path=db_path,
            **suggestion_kwargs,
        )
        return {"ok": True, "suggestion": serialize_suggestion(suggestion)}
    except Exception as exc:
        return {"ok": True, "ai_error": f"素材顺序已保存，但故事建议生成失败：{exc}"}


def _story_clip_contexts(folder: Path, db_path: Path) -> list[StoryClipContext]:
    """收集这个 Finder 文件夹下全部素材的事实，供对话 Agent 参考。"""
    _, items = _load_folder_items(folder, db_path)
    return [_build_clip_context(int(item["id"]), db_path) for item in items]


def serialize_story_message(message: StoryboardMessageRecord) -> dict[str, object]:
    """以原生端可直接渲染的稳定 JSON 输出一条导演 Agent 对话消息。"""
    return {
        "id": message.id, "role": message.role, "content": message.content,
        "reasoning_text": message.reasoning_text or "", "action": message.action_json,
        "created_at": message.created_at or "",
    }


def list_story_chat_messages(storyboard_id: int, db_path: Path = DB_PATH) -> dict[str, object]:
    """读取导演 Agent 对话历史，供原生端进入 Shape Story 时恢复。"""
    messages = list_storyboard_messages(storyboard_id, db_path)
    return {"ok": True, "messages": [serialize_story_message(item) for item in messages]}


def agent_chat(
    *, folder: Path, storyboard_id: int, message: str, focused_beat_id: int | None = None,
    db_path: Path = DB_PATH,
) -> dict[str, object]:
    """在 Shape Story 里和导演 Agent 对话；只在明确提出修改时生成待确认 suggestion。"""
    cleaned_message = message.strip()
    if not cleaned_message:
        raise ValueError("请先输入想和导演 Agent 讨论的内容。")
    story = get_storyboard(storyboard_id, db_path)
    if story is None:
        raise ValueError("故事线不存在。")
    beats = list_story_beats(storyboard_id, db_path)
    chat_beats = [
        ChatBeatContext(id=beat.id, title=beat.title, intent=beat.intent, script_text=beat.script_text)
        for beat in beats
    ]
    existing_messages = list_storyboard_messages(storyboard_id, db_path)
    add_storyboard_message(storyboard_id=storyboard_id, role="user", content=cleaned_message, db_path=db_path)
    history = [
        {"role": item.role, "content": item.content}
        for item in existing_messages
        if item.role in {"user", "assistant"}
    ]
    # 聊天完成后原生端已持有当前 Story；不要在模型调用前后重复加载整个文件夹，
    # 否则素材较多时会拖慢首个可见反馈，并覆盖本地刚插入的聊天气泡。
    result: dict[str, object] = {"ok": True, "message": None, "suggestion": None, "payload": None}
    try:
        clips_context = _story_clip_contexts(folder, db_path)
        reasoning_text, assistant_text = generate_story_agent_reply(
            thesis=story.core_message or story.brief_text,
            beats=chat_beats, focused_beat_id=focused_beat_id,
            clips=clips_context, history=history, user_message=cleaned_message,
        )
        if not assistant_text:
            raise ValueError("导演 Agent 没有返回内容。")
        suggestion_obj = _parse_native_agent_suggestion(assistant_text, chat_beats, focused_beat_id)
        display_text = strip_native_agent_suggestion(assistant_text) if suggestion_obj else assistant_text
        action_json: dict[str, object] | None = None
        if suggestion_obj is not None:
            suggestion = create_story_suggestion(
                storyboard_id=storyboard_id, suggestion_type="agent_chat",
                target_beat_id=suggestion_obj.beat_id,
                explanation=suggestion_obj.explanation,
                suggested_intent=suggestion_obj.suggested_intent,
                suggested_script=suggestion_obj.suggested_script,
                suggested_thesis=suggestion_obj.suggested_thesis,
                payload_json={"move": "agent_chat", "beat_id": suggestion_obj.beat_id},
                db_path=db_path,
            )
            target_beat = next((beat for beat in chat_beats if beat.id == suggestion_obj.beat_id), None)
            action_json = {
                "type": "beat_suggestion", "suggestion_id": suggestion.id, "beat_id": suggestion_obj.beat_id,
                "beat_title": target_beat.title if target_beat else "",
                "explanation": suggestion_obj.explanation,
                "suggested_intent": suggestion_obj.suggested_intent or "",
                "suggested_script": suggestion_obj.suggested_script or "",
                "suggested_thesis": suggestion_obj.suggested_thesis or "",
            }
            result["suggestion"] = serialize_suggestion(suggestion)
        saved = add_storyboard_message(
            storyboard_id=storyboard_id, role="assistant", content=display_text,
            reasoning_text=reasoning_text or None, action_json=action_json, db_path=db_path,
        )
        result["message"] = serialize_story_message(saved)
    except Exception as exc:
        result["ai_error"] = f"对话失败：{exc}"
    return result


def emit(payload: dict[str, object]) -> None:
    """新 bridge 命令仅输出一个 JSON，供 Swift 的 Process 解码。"""
    print(json.dumps(payload, ensure_ascii=False))


def main() -> None:
    """处理原生导演台传来的桥接命令。"""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load"); load.add_argument("folder", type=Path)
    clip_note = sub.add_parser("save-clip-note"); clip_note.add_argument("clip_id", type=int); clip_note.add_argument("note")
    clip_note_status = sub.add_parser("save-clip-note-status"); clip_note_status.add_argument("clip_id", type=int); clip_note_status.add_argument("status", choices=("pending", "skipped", "done"))
    tags = sub.add_parser("save-narrative-tags"); tags.add_argument("clip_id", type=int); tags.add_argument("tags_json")
    library_note = sub.add_parser("save-library-note"); library_note.add_argument("library_id", type=int); library_note.add_argument("note")
    generate = sub.add_parser("generate-story"); generate.add_argument("folder", type=Path); generate.add_argument("--title", default=""); generate.add_argument("--brief", default=""); generate.add_argument("--duration", type=int, default=60)
    directions = sub.add_parser("generate-directions"); directions.add_argument("folder", type=Path); directions.add_argument("--theme", default=""); directions.add_argument("--tone", default=""); directions.add_argument("--requirements", default=""); directions.add_argument("--duration", type=int, default=60)
    choose = sub.add_parser("choose-direction"); choose.add_argument("folder", type=Path); choose.add_argument("direction_json"); choose.add_argument("--theme", default=""); choose.add_argument("--tone", default=""); choose.add_argument("--requirements", default=""); choose.add_argument("--duration", type=int, default=60)
    move = sub.add_parser("move-clip"); move.add_argument("folder", type=Path); move.add_argument("storyboard_id", type=int); move.add_argument("clip_id", type=int); move.add_argument("target_beat_id", type=int, help="传 0 表示移回 Unused"); move.add_argument("--position", type=int); move.add_argument("--in-ms", type=int); move.add_argument("--out-ms", type=int)
    reorder = sub.add_parser("reorder-beats"); reorder.add_argument("folder", type=Path); reorder.add_argument("storyboard_id", type=int); reorder.add_argument("beat_ids_json")
    interpret = sub.add_parser("interpret-move"); interpret.add_argument("folder", type=Path); interpret.add_argument("request_json")
    beat = sub.add_parser("update-beat"); beat.add_argument("beat_id", type=int); beat.add_argument("--title"); beat.add_argument("--intent"); beat.add_argument("--script"); beat.add_argument("--note"); beat.add_argument("--locked", type=lambda value: value.lower() == "true"); beat.add_argument("--must-use", type=lambda value: value.lower() == "true")
    apply = sub.add_parser("apply-suggestion"); apply.add_argument("suggestion_id", type=int)
    dismiss = sub.add_parser("dismiss-suggestion"); dismiss.add_argument("suggestion_id", type=int)
    chat = sub.add_parser("agent-chat"); chat.add_argument("folder", type=Path); chat.add_argument("storyboard_id", type=int); chat.add_argument("message"); chat.add_argument("--focused-beat-id", type=int, default=None)
    messages = sub.add_parser("list-story-messages"); messages.add_argument("storyboard_id", type=int)
    args = parser.parse_args()
    try:
        if args.command == "load": emit({"ok": True, "payload": load_folder(args.folder)})
        elif args.command == "save-clip-note": update_clip_note(args.clip_id, args.note, DB_PATH)
        elif args.command == "save-clip-note-status": update_clip_note_status(args.clip_id, args.status, DB_PATH)
        elif args.command == "save-narrative-tags": update_clip_narrative_tags(args.clip_id, json.loads(args.tags_json), DB_PATH)
        elif args.command == "save-library-note": update_library_memory_note(args.library_id, args.note, DB_PATH)
        elif args.command == "generate-story": emit(generate_story(args.folder, title=args.title, brief=args.brief, duration=args.duration))
        elif args.command == "generate-directions": emit(generate_directions(args.folder, theme=args.theme, tone=args.tone, requirements=args.requirements, duration=args.duration))
        elif args.command == "choose-direction": emit(choose_direction(args.folder, direction_json=args.direction_json, theme=args.theme, tone=args.tone, requirements=args.requirements, duration=args.duration))
        elif args.command == "move-clip": emit(move_clip(folder=args.folder, storyboard_id=args.storyboard_id, clip_id=args.clip_id, target_beat_id=args.target_beat_id or None, position=args.position, in_ms=args.in_ms, out_ms=args.out_ms))
        elif args.command == "reorder-beats":
            ids = json.loads(args.beat_ids_json)
            if not isinstance(ids, list): raise ValueError("Beat 排序必须是 JSON 数组。")
            emit(reorder_beats(folder=args.folder, storyboard_id=args.storyboard_id, beat_ids=ids))
        elif args.command == "interpret-move":
            request_payload = json.loads(args.request_json)
            if not isinstance(request_payload, dict): raise ValueError("故事建议请求必须是 JSON 对象。")
            emit(interpret_move(folder=args.folder, request_payload=request_payload))
        elif args.command == "update-beat":
            record = update_story_beat(beat_id=args.beat_id, title=args.title, intent=args.intent, script_text=args.script, user_note=args.note, locked=args.locked, must_use=args.must_use, db_path=DB_PATH)
            emit({"ok": True, "beat": {"id": record.id, "title": record.title, "intent": record.intent, "script": record.script_text or "", "note": record.user_note or "", "locked": record.locked, "must_use": record.must_use, "user_edited": record.user_edited}})
        elif args.command == "apply-suggestion": emit({"ok": True, "suggestion": serialize_suggestion(resolve_story_suggestion(suggestion_id=args.suggestion_id, apply=True, db_path=DB_PATH))})
        elif args.command == "dismiss-suggestion": emit({"ok": True, "suggestion": serialize_suggestion(resolve_story_suggestion(suggestion_id=args.suggestion_id, apply=False, db_path=DB_PATH))})
        elif args.command == "agent-chat": emit(agent_chat(folder=args.folder, storyboard_id=args.storyboard_id, message=args.message, focused_beat_id=args.focused_beat_id, db_path=DB_PATH))
        elif args.command == "list-story-messages": emit(list_story_chat_messages(args.storyboard_id, DB_PATH))
    except Exception as exc:
        emit({"ok": False, "error": str(exc)})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
