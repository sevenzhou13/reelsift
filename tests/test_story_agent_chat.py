"""验证原生 Shape Story 导演 Agent 对话：Beat 建议解析与 bridge 持久化闭环。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db import (
    ClipRecord,
    StoryboardItemRecord,
    create_library,
    create_storyboard,
    init_db,
    list_story_beats,
    list_storyboard_messages,
    materialize_storyboard_beats,
    save_clip,
    update_storyboard_result,
)
from scripts import reelsift_director_bridge as bridge
from story_ai import ChatBeatContext, _parse_native_agent_suggestion


def prepare_chat_story(tmp_path: Path) -> tuple[Path, Path, int, list[int]]:
    """建立一个包含两个 Beat 的临时故事，用于测试导演 Agent 对话。"""
    db_path = tmp_path / "reelsift.db"
    folder = tmp_path / "素材"
    folder.mkdir()
    init_db(db_path)
    library = create_library("素材", db_path)
    clip_ids: list[int] = []
    for index in range(1, 3):
        video = folder / f"IMG_{index}.mp4"
        video.touch()
        clip_ids.append(save_clip(ClipRecord(
            video_hash=f"chat-{index}", filename=video.name, filepath=video,
            library_id=library.id, status="done", summary=f"真实画面 {index}", scene="家中",
        ), db_path))
    story = create_storyboard(
        library_id=library.id, title="早餐", brief_text="早晨到出门",
        target_duration_seconds=30, tone_prompt="口语", selected_clip_ids=clip_ids, db_path=db_path,
    )
    update_storyboard_result(
        storyboard_id=story.id, title="早餐", core_message="普通早晨的开始", emotional_arc=[],
        story_plan="准备到出门", script_text="先准备早饭，再出门。", db_path=db_path,
        items=[
            StoryboardItemRecord(0, story.id, clip_ids[0], 1, "开场自拍", "建立第一人称记录感", 3, "先对镜拍一下。", None),
            StoryboardItemRecord(0, story.id, clip_ids[1], 2, "出门路上", "展示离家过程", 3, "然后走出家门。", None),
        ],
    )
    materialize_storyboard_beats(story.id, db_path)
    return db_path, folder, story.id, clip_ids


class NativeAgentSuggestionParsingTest(unittest.TestCase):
    """覆盖 Beat 建议 JSON 标记的解析健壮性。"""

    def setUp(self) -> None:
        self.beats = [
            ChatBeatContext(id=25, title="开场自拍", intent="建立第一人称记录感", script_text="先对镜拍一下。"),
            ChatBeatContext(id=26, title="出门路上", intent="展示离家过程", script_text="然后走出家门。"),
        ]

    def test_parses_valid_suggestion_and_resolves_beat_by_title(self) -> None:
        text = (
            "开场那段可以更幽默一点。\n"
            "【BEAT_SUGGESTION_START】\n"
            '{"beat_title": "开场自拍", "meaning_changed": true, '
            '"explanation": "语气更轻松", "suggested_intent": "用调侃的口吻开场", '
            '"suggested_script": "先对着镜子做个鬼脸。", "suggested_thesis": null}\n'
            "【BEAT_SUGGESTION_END】"
        )
        result = _parse_native_agent_suggestion(text, self.beats, focused_beat_id=None)
        self.assertIsNotNone(result)
        self.assertEqual(result.beat_id, 25)
        self.assertTrue(result.meaning_changed)
        self.assertEqual(result.suggested_script, "先对着镜子做个鬼脸。")

    def test_pure_discussion_without_markers_returns_none(self) -> None:
        text = "这几段素材其实也可以从出门那段开始讲，你想试试倒叙吗？"
        result = _parse_native_agent_suggestion(text, self.beats, focused_beat_id=None)
        self.assertIsNone(result)

    def test_malformed_json_inside_markers_returns_none_gracefully(self) -> None:
        text = "【BEAT_SUGGESTION_START】这不是合法 JSON{{{【BEAT_SUGGESTION_END】"
        result = _parse_native_agent_suggestion(text, self.beats, focused_beat_id=None)
        self.assertIsNone(result)

    def test_falls_back_to_focused_beat_when_title_missing(self) -> None:
        text = (
            "【BEAT_SUGGESTION_START】\n"
            '{"meaning_changed": true, "explanation": "调整节奏", '
            '"suggested_intent": null, "suggested_script": "走出门的时候多停顿一下。", "suggested_thesis": null}\n'
            "【BEAT_SUGGESTION_END】"
        )
        result = _parse_native_agent_suggestion(text, self.beats, focused_beat_id=26)
        self.assertIsNotNone(result)
        self.assertEqual(result.beat_id, 26)


class AgentChatBridgeTest(unittest.TestCase):
    """覆盖 bridge.agent_chat 的持久化与 suggestion 落库闭环。"""

    def test_discussion_reply_persists_messages_without_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, story_id, _clip_ids = prepare_chat_story(Path(temporary))
            with patch.object(
                bridge, "generate_story_agent_reply",
                return_value=("我想了想…", "这几段素材也可以从出门那段开始讲。"),
            ) as generate:
                result = bridge.agent_chat(
                    folder=folder, storyboard_id=story_id, message="还有别的讲法吗？",
                    focused_beat_id=None, db_path=db_path,
                )
            generate.assert_called_once()
            self.assertTrue(result["ok"])
            self.assertIsNone(result["suggestion"])
            self.assertIsNotNone(result["message"])
            self.assertEqual(result["message"]["content"], "这几段素材也可以从出门那段开始讲。")
            self.assertIn("payload", result)

            messages = list_storyboard_messages(story_id, db_path)
            self.assertEqual([m.role for m in messages], ["user", "assistant"])
            self.assertEqual(messages[0].content, "还有别的讲法吗？")

    def test_edit_request_creates_pending_suggestion_for_named_beat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, story_id, _clip_ids = prepare_chat_story(Path(temporary))
            beat_id = next(beat.id for beat in list_story_beats(story_id, db_path) if beat.title == "开场自拍")
            assistant_text = (
                "好的，我把开场改得更轻松一点。\n"
                "【BEAT_SUGGESTION_START】\n"
                '{"beat_title": "开场自拍", "meaning_changed": true, '
                '"explanation": "语气更轻松", "suggested_intent": "用调侃的口吻开场", '
                '"suggested_script": "先对着镜子做个鬼脸。", "suggested_thesis": null}\n'
                "【BEAT_SUGGESTION_END】"
            )
            with patch.object(bridge, "generate_story_agent_reply", return_value=("", assistant_text)):
                result = bridge.agent_chat(
                    folder=folder, storyboard_id=story_id, message="把开场那段的旁白写得更幽默一点",
                    focused_beat_id=None, db_path=db_path,
                )
            self.assertTrue(result["ok"])
            self.assertIsNotNone(result["suggestion"])
            self.assertEqual(result["suggestion"]["type"], "agent_chat")
            self.assertEqual(result["suggestion"]["target_beat_id"], beat_id)
            self.assertNotIn("BEAT_SUGGESTION_START", result["message"]["content"])

            messages = list_storyboard_messages(story_id, db_path)
            assistant_message = messages[-1]
            self.assertEqual(assistant_message.role, "assistant")
            self.assertIsNotNone(assistant_message.action_json)
            action = assistant_message.action_json
            self.assertEqual(action["type"], "beat_suggestion")
            self.assertEqual(action["beat_title"], "开场自拍")
            self.assertEqual(action["explanation"], "语气更轻松")
            self.assertEqual(action["suggested_script"], "先对着镜子做个鬼脸。")
            self.assertEqual(action["suggested_intent"], "用调侃的口吻开场")


if __name__ == "__main__":
    unittest.main()
