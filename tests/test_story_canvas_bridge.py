"""验证原生导演台 Story Canvas bridge 在临时 SQLite 中的核心持久化闭环。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from db import (
    ClipRecord,
    StoryboardItemRecord,
    create_library,
    create_story_suggestion,
    create_storyboard,
    init_db,
    list_story_beats,
    save_clip,
    update_story_beat,
    update_storyboard_error,
    update_storyboard_result,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
BRIDGE_PATH = PROJECT_DIR / "scripts" / "reelsift_director_bridge.py"


def run_bridge(db_path: Path, *args: str) -> dict[str, object]:
    """以独立进程模拟原生 Process 调用，并读取唯一 JSON 输出。"""
    environment = dict(os.environ)
    environment["REELSIFT_DB_PATH"] = str(db_path)
    completed = subprocess.run(
        [sys.executable, str(BRIDGE_PATH), *args],
        cwd=PROJECT_DIR,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def prepare_story(tmp_path: Path) -> tuple[Path, Path, int, list[int]]:
    """建立包含两个 legacy storyboard section 的最小真实素材库。"""
    db_path = tmp_path / "reelsift.db"
    folder = tmp_path / "北京实习"
    folder.mkdir()
    init_db(db_path)
    library = create_library(folder.name, db_path)
    clip_ids: list[int] = []
    for index in range(1, 4):
        video_path = folder / f"IMG_{index}.mp4"
        video_path.touch()
        clip_ids.append(save_clip(ClipRecord(
            video_hash=f"hash-{index}", filename=video_path.name, filepath=video_path,
            library_id=library.id, summary=f"素材摘要 {index}", scene="北京",
            status="done",
        ), db_path))
    story = create_storyboard(
        library_id=library.id, title="北京实习", brief_text="一个人适应北京",
        target_duration_seconds=60, tone_prompt="口语", selected_clip_ids=clip_ids, db_path=db_path,
    )
    update_storyboard_result(
        storyboard_id=story.id, title="北京实习", core_message="适应新生活", emotional_arc=[],
        story_plan="开头到收束", script_text="完整旁白", db_path=db_path,
        items=[
            StoryboardItemRecord(0, story.id, clip_ids[0], 1, "刚到北京", "开始适应", 3, "刚来的时候", None),
            StoryboardItemRecord(0, story.id, clip_ids[1], 2, "进入节奏", "建立日常", 3, "后来慢慢", None),
        ],
    )
    return db_path, folder, story.id, clip_ids


class StoryCanvasBridgeTest(unittest.TestCase):
    """临时 SQLite 的 bridge 回归测试。"""

    def test_load_move_reorder_update_and_resolve(self) -> None:
        """素材拖进、跨 Beat 移动、排序、编辑、Apply/Dismiss 都可跨进程恢复。"""
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, story_id, clip_ids = prepare_story(Path(temporary))
            loaded = run_bridge(db_path, "load", str(folder))
            self.assertTrue(loaded["ok"])
            payload = loaded["payload"]
            self.assertEqual(len(payload["beats"]), 2)
            self.assertEqual([item["id"] for item in payload["unused"]], [clip_ids[2]])
            first_beat, second_beat = payload["beats"]

            moved = run_bridge(db_path, "move-clip", str(folder), str(story_id), str(clip_ids[2]), str(first_beat["id"]))
            self.assertTrue(moved["ok"] and moved["moved"])
            # 未配置模型时，AI 建议失败不能撤销已保存的拖动。
            reloaded = run_bridge(db_path, "load", str(folder))["payload"]
            self.assertNotIn(clip_ids[2], [item["id"] for item in reloaded["unused"]])

            trimmed = run_bridge(
                db_path,
                "move-clip",
                str(folder),
                str(story_id),
                str(clip_ids[2]),
                str(second_beat["id"]),
                "--in-ms",
                "1200",
                "--out-ms",
                "4200",
            )
            self.assertTrue(trimmed["ok"])
            run_bridge(db_path, "move-clip", str(folder), str(story_id), str(clip_ids[2]), str(first_beat["id"]))
            preserved = run_bridge(db_path, "load", str(folder))["payload"]
            preserved_link = next(
                clip
                for beat in preserved["beats"]
                if beat["id"] == first_beat["id"]
                for clip in beat["clips"]
                if clip["id"] == clip_ids[2]
            )
            self.assertEqual((preserved_link["in_ms"], preserved_link["out_ms"]), (1200, 4200))

            run_bridge(
                db_path,
                "move-clip",
                str(folder),
                str(story_id),
                str(clip_ids[0]),
                str(first_beat["id"]),
                "--position",
                "3",
            )
            same_beat = run_bridge(db_path, "load", str(folder))["payload"]
            first_beat_clips = next(
                beat["clips"] for beat in same_beat["beats"] if beat["id"] == first_beat["id"]
            )
            self.assertEqual([clip["id"] for clip in first_beat_clips], [clip_ids[2], clip_ids[0]])

            moved_back = run_bridge(db_path, "move-clip", str(folder), str(story_id), str(clip_ids[2]), "0")
            self.assertTrue(moved_back["ok"])
            self.assertIsNone(moved_back["target_beat_id"])
            reordered = run_bridge(db_path, "reorder-beats", str(folder), str(story_id), json.dumps([second_beat["id"], first_beat["id"]]))
            self.assertTrue(reordered["ok"])
            updated = run_bridge(db_path, "update-beat", str(first_beat["id"]), "--intent", "新的开场意图", "--script", "新的局部旁白")
            self.assertTrue(updated["beat"]["user_edited"])

            suggestion = create_story_suggestion(
                storyboard_id=story_id, suggestion_type="move_interpretation", target_beat_id=first_beat["id"],
                explanation="建议", suggested_intent="应用后的意图", suggested_script="应用后的旁白", db_path=db_path,
            )
            applied = run_bridge(db_path, "apply-suggestion", str(suggestion.id))
            self.assertEqual(applied["suggestion"]["status"], "applied")
            self.assertEqual(next(item for item in list_story_beats(story_id, db_path) if item.id == first_beat["id"]).intent, "应用后的意图")

            dismissed = create_story_suggestion(
                storyboard_id=story_id, suggestion_type="move_interpretation", target_beat_id=first_beat["id"],
                explanation="不应用", suggested_intent="不应写入", db_path=db_path,
            )
            self.assertEqual(run_bridge(db_path, "dismiss-suggestion", str(dismissed.id))["suggestion"]["status"], "dismissed")

            stale = create_story_suggestion(
                storyboard_id=story_id,
                suggestion_type="move_interpretation",
                target_beat_id=first_beat["id"],
                clip_id=clip_ids[0],
                explanation="旧建议",
                suggested_intent="不应覆盖人工编辑",
                payload_json={"base_intent": "应用后的意图", "base_script": "应用后的旁白"},
                db_path=db_path,
            )
            update_story_beat(beat_id=first_beat["id"], intent="刚刚人工修改", db_path=db_path)
            expired = run_bridge(db_path, "apply-suggestion", str(stale.id))
            self.assertEqual(expired["suggestion"]["status"], "expired")
            self.assertEqual(
                next(item for item in list_story_beats(story_id, db_path) if item.id == first_beat["id"]).intent,
                "刚刚人工修改",
            )

    def test_load_recovers_previous_canvas_after_failed_regeneration(self) -> None:
        """最近一次重新生成失败时，保留并恢复上一版可用 Canvas。"""
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, good_story_id, clip_ids = prepare_story(Path(temporary))
            loaded = run_bridge(db_path, "load", str(folder))["payload"]
            self.assertEqual(loaded["story"]["id"], good_story_id)

            failed_story = create_storyboard(
                library_id=loaded["library_id"],
                title="失败的重新生成",
                brief_text="测试降级",
                target_duration_seconds=60,
                tone_prompt="口语",
                selected_clip_ids=clip_ids,
                db_path=db_path,
            )
            update_storyboard_error(failed_story.id, "模型暂时不可用", db_path)

            recovered = run_bridge(db_path, "load", str(folder))["payload"]
            self.assertEqual(recovered["story"]["id"], good_story_id)
            self.assertEqual(len(recovered["beats"]), 2)
            self.assertIn("已恢复上一版 Canvas", recovered["restore_warning"])
