"""验证同 Beat 素材重排的 AI suggestion 与故事生成约束。"""
from __future__ import annotations

import os
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from db import (
    ClipRecord,
    StoryboardItemRecord,
    create_library,
    create_storyboard,
    init_db,
    list_story_beats,
    materialize_storyboard_beats,
    save_clip,
    update_storyboard_result,
)
from scripts import reelsift_director_bridge as bridge
from story_ai import (
    StoryBeatContext,
    StoryClipContext,
    StoryMoveInterpretation,
    _build_request_body,
    _build_user_prompt,
    _get_story_model_config,
    _parse_storyboard_plan,
    generate_beat_order_interpretation,
    generate_story_reorder_interpretation,
)


def build_clip(clip_id: int, name: str) -> StoryClipContext:
    """构造带真实依据字段的最小素材上下文。"""
    return StoryClipContext(
        clip_id=clip_id,
        filename=name,
        summary=f"{name} 的画面摘要",
        scene="北京的家",
        tags=["日常"],
        subjects=["创作者"],
        actions=["准备早餐"],
        user_note="真实记录，不要煽情",
        transcript_text="今天先把早饭做好。",
    )


def prepare_reorder_story(tmp_path: Path) -> tuple[Path, Path, int, list[int]]:
    """建立一个包含同 Beat 三条素材的临时故事。"""
    db_path = tmp_path / "reelsift.db"
    folder = tmp_path / "素材"
    folder.mkdir()
    init_db(db_path)
    library = create_library("素材", db_path)
    clip_ids: list[int] = []
    for index in range(1, 4):
        video = folder / f"IMG_{index}.mp4"
        video.touch()
        clip_ids.append(save_clip(ClipRecord(
            video_hash=f"reorder-{index}", filename=video.name, filepath=video,
            library_id=library.id, status="done", summary=f"真实画面 {index}",
            scene="家中", user_note=f"备注 {index}",
        ), db_path))
    story = create_storyboard(
        library_id=library.id, title="早餐", brief_text="用早餐开场的一天",
        target_duration_seconds=30, tone_prompt="口语", selected_clip_ids=clip_ids, db_path=db_path,
    )
    update_storyboard_result(
        storyboard_id=story.id, title="早餐", core_message="普通早晨的开始", emotional_arc=[],
        story_plan="准备到出门", script_text="先准备早饭，再出门。", db_path=db_path,
        items=[
            StoryboardItemRecord(0, story.id, clip_ids[0], 1, "在家准备", "展示早晨节奏", 3, "先从厨房开始。", None),
            StoryboardItemRecord(0, story.id, clip_ids[1], 2, "在家准备", "展示早晨节奏", 3, "然后把东西收好。", None),
            StoryboardItemRecord(0, story.id, clip_ids[2], 3, "在家准备", "展示早晨节奏", 3, "最后准备出门。", None),
        ],
    )
    materialize_storyboard_beats(story.id, db_path)
    return db_path, folder, story.id, clip_ids


def prepare_two_beat_story(tmp_path: Path) -> tuple[Path, Path, int, list[int]]:
    """建立包含两个 Beat 的临时故事，用于测试 Beat 整体排序。"""
    db_path = tmp_path / "reelsift.db"
    folder = tmp_path / "两段素材"
    folder.mkdir()
    init_db(db_path)
    library = create_library("两段素材", db_path)
    clip_ids: list[int] = []
    for index in range(1, 3):
        video = folder / f"IMG_{index}.mp4"
        video.touch()
        clip_ids.append(save_clip(ClipRecord(
            video_hash=f"twobeat-{index}", filename=video.name, filepath=video,
            library_id=library.id, status="done", summary=f"真实画面 {index}",
            scene="家中",
        ), db_path))
    story = create_storyboard(
        library_id=library.id, title="两段故事", brief_text="早晨到出门",
        target_duration_seconds=30, tone_prompt="口语", selected_clip_ids=clip_ids, db_path=db_path,
    )
    update_storyboard_result(
        storyboard_id=story.id, title="两段故事", core_message="从准备到出门", emotional_arc=[],
        story_plan="准备到出门", script_text="先准备，再出门。", db_path=db_path,
        items=[
            StoryboardItemRecord(0, story.id, clip_ids[0], 1, "在家准备", "展示早晨节奏", 3, "先从厨房开始。", None),
            StoryboardItemRecord(0, story.id, clip_ids[1], 2, "出门路上", "展示离家过程", 3, "然后走出家门。", None),
        ],
    )
    materialize_storyboard_beats(story.id, db_path)
    return db_path, folder, story.id, clip_ids


class StoryAIReorderTest(unittest.TestCase):
    """覆盖模型 prompt、供应商别名与 bridge 持久化闭环。"""

    def test_reorder_prompt_grounds_before_and_after_sequences(self) -> None:
        """模型必须能看到调整前后顺序以及每条素材事实。"""
        clips = [build_clip(1, "早餐.mov"), build_clip(2, "出门.mov")]
        with patch("story_ai._call_story_model", return_value=(
            '{"meaning_changed":true,"explanation":"开场先出现出门画面，节奏更直接。",'
            '"suggested_intent":"先给出出门的动机，再回到准备。",'
            '"suggested_script":"我先站到门口，才想起早饭还没准备好。","suggested_thesis":null}'
        )) as call_model:
            result = generate_story_reorder_interpretation(
                thesis="记录普通一天", beat=StoryBeatContext("在家准备", "展示早晨节奏", "先做早饭。"),
                moved_clip=clips[1], before_clips=clips, after_clips=list(reversed(clips)),
            )
        self.assertTrue(result.meaning_changed)
        prompt = call_model.call_args.args[0]
        self.assertIn("调整前素材顺序", prompt)
        self.assertIn("调整后素材顺序", prompt)
        self.assertIn("早餐.mov 的画面摘要", prompt)
        self.assertIn("出门.mov 的画面摘要", prompt)

    def test_same_beat_reorder_saves_before_background_interpretation(self) -> None:
        """同 Beat 改序先返回 Canvas，再由后台任务写入 suggestion。"""
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, story_id, clip_ids = prepare_reorder_story(Path(temporary))
            beat_id = list_story_beats(story_id, db_path)[0].id
            interpretation = StoryMoveInterpretation(
                meaning_changed=True,
                explanation="把出门镜头提前，开场更有行动感。",
                suggested_intent="从准备出门的动作切入早晨。",
                suggested_script="我先站到门口，才想起早饭还没准备好。",
            )
            with patch.object(bridge, "generate_story_reorder_interpretation", return_value=interpretation) as generate:
                result = bridge.move_clip(
                    folder=folder, storyboard_id=story_id, clip_id=clip_ids[2], target_beat_id=beat_id,
                    position=1, db_path=db_path,
                )
            self.assertTrue(result["moved"])
            self.assertIsNone(result["suggestion"])
            self.assertIn("interpretation_request", result)
            generate.assert_not_called()
            current_order = bridge._ordered_beat_clip_ids(story_id, beat_id, db_path)
            self.assertEqual(current_order, [clip_ids[2], clip_ids[0], clip_ids[1]])
            with patch.object(bridge, "generate_story_reorder_interpretation", return_value=interpretation) as generate_after:
                interpreted = bridge.interpret_move(
                    folder=folder, request_payload=result["interpretation_request"], db_path=db_path,
                )
            generate_after.assert_called_once()
            self.assertEqual(interpreted["suggestion"]["type"], "reorder_interpretation")
            suggestion_id = interpreted["suggestion"]["id"]
            applied = bridge.resolve_story_suggestion(suggestion_id=suggestion_id, apply=True, db_path=db_path)
            self.assertEqual(applied.status, "applied")
            self.assertEqual(list_story_beats(story_id, db_path)[0].script_text, interpretation.suggested_script)

    def test_reorder_suggestion_expires_after_a_new_order_change(self) -> None:
        """用户再次排序后，较早的 AI 建议不能覆盖新的 Canvas 状态。"""
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, story_id, clip_ids = prepare_reorder_story(Path(temporary))
            beat_id = list_story_beats(story_id, db_path)[0].id
            first_interpretation = StoryMoveInterpretation(
                meaning_changed=True,
                explanation="建议跟随第一次新顺序。",
                suggested_script="这是第一次排序对应的旁白。",
            )
            first = bridge.move_clip(
                folder=folder, storyboard_id=story_id, clip_id=clip_ids[2], target_beat_id=beat_id,
                position=1, db_path=db_path,
            )
            bridge.move_clip(
                folder=folder, storyboard_id=story_id, clip_id=clip_ids[1], target_beat_id=beat_id,
                position=1, db_path=db_path,
            )
            with patch.object(bridge, "generate_story_reorder_interpretation", return_value=first_interpretation) as generate:
                stale = bridge.interpret_move(
                    folder=folder, request_payload=first["interpretation_request"], db_path=db_path,
                )
            self.assertTrue(stale["stale"])
            generate.assert_not_called()
            self.assertNotIn("suggestion", stale)

    def test_beat_order_prompt_grounds_before_and_after_sequences(self) -> None:
        """模型必须能看到调整前后的 Beat 顺序与各自的 intent。"""
        before = [StoryBeatContext("在家准备", "展示早晨节奏"), StoryBeatContext("出门路上", "展示离家过程")]
        after = [StoryBeatContext("出门路上", "展示离家过程"), StoryBeatContext("在家准备", "展示早晨节奏")]
        with patch("story_ai._call_story_model", return_value=(
            '{"meaning_changed":true,"explanation":"先出门再回忆准备过程，节奏更有悬念。",'
            '"suggested_intent":null,"suggested_script":null,"suggested_thesis":null}'
        )) as call_model:
            result = generate_beat_order_interpretation(thesis="记录普通一天", before_beats=before, after_beats=after)
        self.assertTrue(result.meaning_changed)
        prompt = call_model.call_args.args[0]
        self.assertIn("调整前 Beat 顺序", prompt)
        self.assertIn("调整后 Beat 顺序", prompt)
        self.assertIn("在家准备", prompt)
        self.assertIn("出门路上", prompt)

    def test_beat_reorder_saves_before_background_interpretation(self) -> None:
        """整体调整 Beat 顺序不等待模型，后台任务再生成待确认 suggestion。"""
        with tempfile.TemporaryDirectory() as temporary:
            db_path, folder, story_id, _clip_ids = prepare_two_beat_story(Path(temporary))
            beat_ids = [beat.id for beat in list_story_beats(story_id, db_path)]
            reversed_ids = list(reversed(beat_ids))
            interpretation = StoryMoveInterpretation(
                meaning_changed=True,
                explanation="调换开场顺序后，故事从出门写起，更有悬念。",
                suggested_thesis="从出门那一刻写起，再回忆准备过程。",
            )
            with patch.object(bridge, "generate_beat_order_interpretation", return_value=interpretation) as generate:
                result = bridge.reorder_beats(folder=folder, storyboard_id=story_id, beat_ids=reversed_ids, db_path=db_path)
            generate.assert_not_called()
            self.assertTrue(result["ok"])
            self.assertIsNone(result["suggestion"])
            self.assertIn("interpretation_request", result)
            self.assertIn("payload", result)
            self.assertIsNotNone(result["payload"]["story"])
            self.assertEqual(
                [beat.id for beat in sorted(list_story_beats(story_id, db_path), key=lambda item: item.position)],
                reversed_ids,
            )
            with patch.object(bridge, "generate_beat_order_interpretation", return_value=interpretation) as generate_after:
                interpreted = bridge.interpret_move(
                    folder=folder, request_payload=result["interpretation_request"], db_path=db_path,
                )
            generate_after.assert_called_once()
            self.assertEqual(interpreted["suggestion"]["type"], "beat_order_interpretation")
            self.assertEqual(interpreted["suggestion"]["suggested_thesis"], interpretation.suggested_thesis)

    def test_generation_prompt_requires_canvas_ready_sections(self) -> None:
        """初始生成 prompt 明确限制 Beat 数量、语义边界与基于素材的脚本。"""
        prompt = _build_user_prompt(
            clips=[build_clip(1, "A.mov"), build_clip(2, "B.mov"), build_clip(3, "C.mov")],
            brief_text="普通的一天", target_duration_seconds=30, tone_prompt="口语",
        )
        self.assertIn("3–6 个清晰且不重复的 section", prompt)
        self.assertIn("画面、口播、备注或标签支持", prompt)
        self.assertIn("避免“治愈、成长、仪式感、刚好”等泛化套话", prompt)

    def test_plan_output_requires_three_to_six_beats_when_material_allows(self) -> None:
        """结构化输出不能退化成单一 Beat，且 section 会保留为 Canvas 分段。"""
        payload = {
            "title": "早晨", "target_duration_seconds": 30, "tone": "口语",
            "core_message": "普通早晨", "emotional_arc": ["安静", "出门"],
            "story_plan": "厨房到门口", "first_person_script": "我先做早饭，再出门。",
            "clip_order": [
                {"clip_id": 1, "position": 1, "section": "准备", "role": "开场", "suggested_duration_seconds": 3, "script_line": "先做早饭。"},
                {"clip_id": 2, "position": 2, "section": "收拾", "role": "推进", "suggested_duration_seconds": 3, "script_line": "把东西收好。"},
                {"clip_id": 3, "position": 3, "section": "出门", "role": "收束", "suggested_duration_seconds": 3, "script_line": "最后出门。"},
            ],
        }
        plan = _parse_storyboard_plan(json.dumps(payload), {1, 2, 3}, 30)
        self.assertEqual([item.section for item in plan.clip_order], ["准备", "收拾", "出门"])
        payload["clip_order"][1]["section"] = "准备"
        payload["clip_order"][2]["section"] = "准备"
        with self.assertRaises(Exception):
            _parse_storyboard_plan(json.dumps(payload), {1, 2, 3}, 30)

    def test_provider_aliases_work_without_a_ui_selector(self) -> None:
        """Qwen、DeepSeek、豆包可通过环境变量切换到同一 OpenAI-compatible adapter。"""
        base_environment = {key: value for key, value in os.environ.items() if not key.startswith(("STORY_", "ARK_", "QWEN_", "DASHSCOPE_", "DEEPSEEK_", "DOUBAO_"))}
        cases = [
            ("QWEN", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL"),
            ("DEEPSEEK", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"),
            ("DOUBAO", "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL"),
        ]
        for provider, key_name, url_name, model_name in cases:
            with self.subTest(provider=provider), patch.dict(os.environ, {
                **base_environment,
                "STORY_PROVIDER": provider,
                key_name: "test-key",
                url_name: "https://example.test/v1",
                model_name: "test-model",
            }, clear=True), patch("story_ai.load_dotenv"):
                api_key, base_url, model, _ = _get_story_model_config()
                self.assertEqual((api_key, base_url, model), ("test-key", "https://example.test/v1", "test-model"))

    def test_fast_model_only_overrides_explicit_fast_tasks(self) -> None:
        """方向与拖动建议可单独路由到快模型，完整 Story 默认不受影响。"""
        with patch.dict(os.environ, {
            "STORY_API_KEY": "test-key",
            "STORY_BASE_URL": "https://example.test/v1",
            "STORY_MODEL": "story-pro",
            "STORY_FAST_MODEL": "story-fast",
        }, clear=True), patch("story_ai.load_dotenv"):
            self.assertEqual(_get_story_model_config()[2], "story-pro")
            self.assertEqual(_get_story_model_config(use_fast_model=True)[2], "story-fast")

    def test_deepseek_fast_request_disables_thinking_and_uses_json_budget(self) -> None:
        """DeepSeek 快速任务关闭思考，并限制结构化输出长度。"""
        with patch.dict(os.environ, {
            "STORY_API_KEY": "test-key",
            "STORY_BASE_URL": "https://api.deepseek.com",
            "STORY_MODEL": "deepseek-v4-pro",
            "STORY_FAST_MODEL": "deepseek-flash",
        }, clear=True), patch("story_ai.load_dotenv"):
            body = json.loads(_build_request_body("返回方向", use_fast_model=True).decode("utf-8"))

        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_tokens"], 1024)

    def test_non_deepseek_fast_request_keeps_provider_compatible_body(self) -> None:
        """非 DeepSeek 快速模型不接收 DeepSeek 专属参数。"""
        with patch.dict(os.environ, {
            "STORY_API_KEY": "test-key",
            "STORY_BASE_URL": "https://example.test/v1",
            "STORY_MODEL": "story-pro",
            "STORY_FAST_MODEL": "story-fast",
        }, clear=True), patch("story_ai.load_dotenv"):
            body = json.loads(_build_request_body("返回方向", use_fast_model=True).decode("utf-8"))

        self.assertEqual(body["model"], "story-fast")
        for key in ("thinking", "reasoning_effort", "response_format", "max_tokens"):
            self.assertNotIn(key, body)

    def test_fast_request_falls_back_to_story_model_without_fast_configuration(self) -> None:
        """未配置快模型时回退到主模型，且不误加快速请求参数。"""
        with patch.dict(os.environ, {
            "STORY_API_KEY": "test-key",
            "STORY_BASE_URL": "https://api.deepseek.com",
            "STORY_MODEL": "deepseek-v4-pro",
        }, clear=True), patch("story_ai.load_dotenv"):
            body = json.loads(_build_request_body("返回方向", use_fast_model=True).decode("utf-8"))

        self.assertEqual(body["model"], "deepseek-v4-pro")
        for key in ("thinking", "reasoning_effort", "response_format", "max_tokens"):
            self.assertNotIn(key, body)

    def test_full_story_request_is_unchanged_when_fast_model_is_configured(self) -> None:
        """完整故事请求不因快速模型配置而改变。"""
        with patch.dict(os.environ, {
            "STORY_API_KEY": "test-key",
            "STORY_BASE_URL": "https://api.deepseek.com",
            "STORY_MODEL": "deepseek-v4-pro",
            "STORY_FAST_MODEL": "deepseek-flash",
        }, clear=True), patch("story_ai.load_dotenv"):
            body = json.loads(_build_request_body("生成完整故事", use_fast_model=False).decode("utf-8"))

        self.assertEqual(body["model"], "deepseek-v4-pro")
        self.assertEqual(body["temperature"], 0.45)
        for key in ("thinking", "reasoning_effort", "response_format", "max_tokens"):
            self.assertNotIn(key, body)
