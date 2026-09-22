"""验证三方向生成与确认后复用现有 Canvas 持久化链路。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db import ClipRecord, create_library, get_storyboard, init_db, save_clip
from scripts import reelsift_director_bridge as bridge
from story_ai import StoryboardClipPlan, StoryboardPlan, _parse_story_directions


def prepare_folder(tmp_path: Path) -> tuple[Path, Path, list[int]]:
    """建立已有识别结果的最小 Finder 文件夹。"""
    db_path = tmp_path / "reelsift.db"
    folder = tmp_path / "morning"
    folder.mkdir()
    init_db(db_path)
    library = create_library(folder.name, db_path)
    clip_ids: list[int] = []
    for index in range(1, 4):
        video_path = folder / f"IMG_{index}.mov"
        video_path.touch()
        clip_ids.append(save_clip(ClipRecord(
            video_hash=f"direction-{index}", filename=video_path.name, filepath=video_path,
            library_id=library.id, summary=f"早餐素材 {index}", scene="厨房", status="done",
            user_note="这是我开始一天的方式" if index == 1 else None,
        ), db_path))
    return db_path, folder, clip_ids


class StoryDirectionsTest(unittest.TestCase):
    """方向生成的 JSON 契约与确认行为。"""

    def test_parse_directions_normalizes_ids_and_requires_three_choices(self) -> None:
        """模型任意 id 不会泄漏到原生选择状态。"""
        raw = json.dumps({"directions": [
            {"id": "a", "title": "慢慢开始", "style": "克制、真实", "structure": ["开场", "准备", "出门"]},
            {"id": "b", "title": "给自己留白", "style": "安静、留白", "structure": ["独处", "行动", "收束"]},
            {"id": "c", "title": "一顿饭的时间", "style": "松弛、生活感", "structure": ["起锅", "出门", "结尾"]},
        ]}, ensure_ascii=False)
        directions = _parse_story_directions(raw)
        self.assertEqual([item.id for item in directions.directions], ["direction-1", "direction-2", "direction-3"])

    def test_generate_directions_includes_notes_and_choose_persists_canvas(self) -> None:
        """确认方向会把用户 Brief 和选中方向带入既有 Storyboard 写入。"""
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, clip_ids = prepare_folder(Path(temporary))
            generated = _parse_story_directions(json.dumps({"directions": [
                {"id": "x", "title": "早晨启动", "style": "克制、真实", "structure": ["醒来", "准备", "出门"]},
                {"id": "y", "title": "一个人的节奏", "style": "安静、留白", "structure": ["独处", "动作", "继续"]},
                {"id": "z", "title": "平常的一天", "style": "松弛、生活感", "structure": ["开始", "路上", "收束"]},
            ]}, ensure_ascii=False))
            plan = StoryboardPlan(
                title="早晨启动", target_duration_seconds=60, tone="口语", core_message="从照顾自己开始",
                emotional_arc=["平静", "进入状态"], story_plan="厨房到出门", first_person_script="我先把早饭准备好，再慢慢出门。",
                clip_order=[
                    StoryboardClipPlan(clip_id=clip_ids[0], position=1, section="醒来", role="开场", suggested_duration_seconds=3),
                    StoryboardClipPlan(clip_id=clip_ids[1], position=2, section="准备", role="推进", suggested_duration_seconds=3),
                    StoryboardClipPlan(clip_id=clip_ids[2], position=3, section="出门", role="收束", suggested_duration_seconds=3),
                ],
            )
            with patch.object(bridge, "generate_story_directions", return_value=generated):
                result = bridge.generate_directions(folder, theme="记录早晨", tone="自然口语", requirements="不要煽情", db_path=db_path)
            self.assertTrue(result["ok"])
            self.assertEqual(result["context"]["creator_note_count"], 1)
            self.assertIn("创作者主题：记录早晨", str(result["brief"]))

            selected = json.dumps(result["directions"][0], ensure_ascii=False)
            with patch.object(bridge, "generate_storyboard_plan", return_value=plan) as generate_plan:
                chosen = bridge.choose_direction(
                    folder, direction_json=selected, theme="记录早晨", tone="自然口语",
                    requirements="不要煽情", db_path=db_path,
                )
            self.assertTrue(chosen["ok"])
            story = get_storyboard(int(chosen["storyboard_id"]), db_path)
            self.assertIsNotNone(story)
            self.assertIn("已确认故事方向：早晨启动", story.brief_text)
            self.assertIn("创作者主题：记录早晨", story.brief_text)
            self.assertEqual(story.tone_prompt, "自然口语")
            self.assertEqual(generate_plan.call_args.kwargs["brief_text"], story.brief_text)

