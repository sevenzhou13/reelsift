# 故事线 AI 封装：根据素材上下文和创作者想法生成第一人称叙事方案
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Iterator
from urllib import error, request

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ai import ArkAPIError


load_dotenv(override=True)

DEFAULT_TONE_PROMPT = "第一人称、口语化、像真实 vlog 旁白，不要鸡汤，不要广告腔。"
MAX_STORY_CLIP_SECONDS = 120

_SYSTEM_PROMPT = """你是一个短视频创作者的故事导演。
你的任务不是简单概括素材，而是把创作者的想法和一批视频素材整理成“第一人称故事”。

工作原则：
- 先理解创作者想表达的东西，再决定怎么讲故事。
- 脚本必须是第一人称，像创作者自己在讲，不要像广告文案或散文朗诵。
- 画面摘要、口播、创作备注都要用；其中创作者备注和整体想法优先级最高。
- 如果素材里有人物说话或可用口播，优先把它作为故事真实感来源。
- 不要编造素材里没有的关键事件；可以做合理的情绪串联，但不能硬造事实。
- 根据目标时长控制脚本长度和表达密度。
- 根据当前任务要求输出叙事框架、完整脚本或素材排序清单，不要把不同阶段混在一起。

时长参考：
- 15 秒：3-5 个素材，脚本 40-70 字。
- 30 秒：5-8 个素材，脚本 80-130 字。
- 60 秒：8-14 个素材，脚本 160-260 字。
- 90 秒：12-20 个素材，脚本 260-400 字。
- 120 秒：16-28 个素材，脚本 400-600 字。
- 180 秒：24-40 个素材，脚本 650-900 字。
- 300 秒：35-60 个素材，脚本 1000-1600 字。
- 600 秒：50-90 个素材，脚本 2000-3200 字。
- 1200 秒：70-140 个素材，脚本 4000-6500 字。

严格返回 JSON，不要 markdown 代码块，不要任何解释文字。"""


@dataclass
class StoryClipContext:
    clip_id: int
    filename: str
    summary: str
    scene: str
    tags: list[str]
    subjects: list[str]
    actions: list[str]
    user_note: str | None = None
    transcript_text: str | None = None
    source_time: str | None = None
    rating: int = 0
    is_favorite: bool = False


@dataclass
class StoryAgentChunk:
    chunk_type: str
    text: str


class StoryboardClipPlan(BaseModel):
    clip_id: int
    position: int
    section: str = Field(max_length=40)
    role: str
    suggested_duration_seconds: int = Field(ge=1, le=MAX_STORY_CLIP_SECONDS)
    script_line: str = ""
    reason: str = ""


class StoryboardPlan(BaseModel):
    title: str = Field(max_length=80)
    target_duration_seconds: int
    tone: str
    core_message: str
    emotional_arc: list[str]
    story_plan: str
    first_person_script: str
    clip_order: list[StoryboardClipPlan]


class StoryboardScript(BaseModel):
    title: str = Field(max_length=80)
    target_duration_seconds: int
    tone: str
    core_message: str
    emotional_arc: list[str]
    story_plan: str
    first_person_script: str


class StoryboardClipOrder(BaseModel):
    clip_order: list[StoryboardClipPlan]


class StoryboardFramework(BaseModel):
    title: str = Field(max_length=80)
    core_message: str
    emotional_arc: list[str]
    narrative_framework: str
    sections: list[str]


class StoryDirection(BaseModel):
    """用户可先比较、再确认的一个故事方向；只保留标题、风格、结构，生成更快。"""

    id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=80)
    style: str = Field(min_length=1, max_length=60)
    structure: list[str] = Field(min_length=3, max_length=4)


class StoryDirections(BaseModel):
    """一次生成恰好三个、彼此有区分的 Story Direction。"""

    directions: list[StoryDirection] = Field(min_length=3, max_length=3)


class StoryMoveInterpretation(BaseModel):
    """素材跨 Beat 移动后、等待用户确认的局部建议。"""

    meaning_changed: bool = False
    explanation: str = ""
    suggested_intent: str | None = None
    suggested_script: str | None = None
    suggested_thesis: str | None = None


@dataclass
class StoryBeatContext:
    """用于解释直接编辑动作的最小 Beat 上下文。"""

    title: str
    intent: str
    script_text: str | None = None


def _stringify_framework_value(value) -> str:
    """把模型返回的对象/数组转成可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(_stringify_framework_value(item) for item in value if _stringify_framework_value(item))
    if isinstance(value, dict):
        labels = {
            "beginning": "开头",
            "development": "发展",
            "turn": "转折",
            "conclusion": "收束",
            "function": "作用",
            "emotion": "情绪",
            "image_type": "画面类型",
        }
        parts = []
        for key, item in value.items():
            text = _stringify_framework_value(item)
            if text:
                parts.append(f"{labels.get(str(key), str(key))}：{text}")
        return "；".join(parts)
    return str(value).strip()


def _normalize_framework_payload(payload: dict) -> dict:
    """兼容模型把框架字段返回成对象的情况。"""
    normalized = dict(payload)
    normalized["title"] = _stringify_framework_value(normalized.get("title")) or "未命名故事线"
    normalized["core_message"] = _stringify_framework_value(normalized.get("core_message"))
    normalized["narrative_framework"] = _stringify_framework_value(normalized.get("narrative_framework"))
    emotional_arc = normalized.get("emotional_arc") or []
    if not isinstance(emotional_arc, list):
        emotional_arc = [emotional_arc]
    normalized["emotional_arc"] = [_stringify_framework_value(item) for item in emotional_arc if _stringify_framework_value(item)]
    sections = normalized.get("sections") or []
    if not isinstance(sections, list):
        sections = [sections]
    normalized["sections"] = [_stringify_framework_value(item) for item in sections if _stringify_framework_value(item)]
    return normalized


def _strip_code_block(raw: str) -> str:
    """剥离模型可能返回的 markdown 代码块。"""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
    return cleaned.strip()


def _extract_json_object(raw: str) -> str:
    """从模型输出中截取第一个完整 JSON 对象。"""
    start = raw.find("{")
    if start == -1:
        raise ArkAPIError(f"故事线模型返回中没有 JSON 对象：{raw}")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start:index + 1]
    raise ArkAPIError(f"故事线模型返回的 JSON 对象不完整：{raw}")


def _parse_storyboard_plan(raw: str, allowed_clip_ids: set[int], target_duration_seconds: int) -> StoryboardPlan:
    """解析并做最小归一化，避免无效素材 ID 混入排序清单。"""
    cleaned = _strip_code_block(raw)
    try:
        plan = StoryboardPlan.model_validate_json(cleaned)
    except Exception:
        json_text = _extract_json_object(cleaned)
        try:
            plan = StoryboardPlan.model_validate(json.loads(json_text))
        except Exception as exc:
            raise ArkAPIError(f"故事线 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc

    valid_items = [
        item
        for item in plan.clip_order
        if item.clip_id in allowed_clip_ids
    ]
    if not valid_items:
        raise ArkAPIError("故事线没有匹配到任何有效素材。")

    normalized_items: list[StoryboardClipPlan] = []
    seen_clip_ids: set[int] = set()
    for index, item in enumerate(valid_items, start=1):
        if item.clip_id in seen_clip_ids:
            continue
        seen_clip_ids.add(item.clip_id)
        normalized_items.append(
            StoryboardClipPlan(
                clip_id=item.clip_id,
                position=index,
                section=item.section.strip() or "故事段落",
                role=item.role.strip() or "承接故事情绪",
                suggested_duration_seconds=max(1, min(int(item.suggested_duration_seconds), MAX_STORY_CLIP_SECONDS)),
                script_line=item.script_line.strip(),
                reason=item.reason.strip(),
            )
        )

    section_names = {item.section for item in normalized_items if item.section}
    if len(normalized_items) >= 3 and not 3 <= len(section_names) <= 6:
        raise ArkAPIError(
            f"Story Canvas 需要 3–6 个有意义的 Beat，但模型返回了 {len(section_names)} 个 section。"
        )
    plan.clip_order = normalized_items
    plan.target_duration_seconds = target_duration_seconds
    return plan


def _parse_storyboard_script(raw: str, target_duration_seconds: int) -> StoryboardScript:
    """解析只包含脚本结果的 JSON。"""
    cleaned = _strip_code_block(raw)
    try:
        script = StoryboardScript.model_validate_json(cleaned)
    except Exception:
        json_text = _extract_json_object(cleaned)
        try:
            script = StoryboardScript.model_validate(json.loads(json_text))
        except Exception as exc:
            raise ArkAPIError(f"故事脚本 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc

    script.target_duration_seconds = target_duration_seconds
    if not script.first_person_script.strip():
        raise ArkAPIError("故事脚本返回为空。")
    return script


def _parse_storyboard_clip_order(raw: str, allowed_clip_ids: set[int]) -> StoryboardClipOrder:
    """解析脚本确认后的素材排序结果。"""
    cleaned = _strip_code_block(raw)
    try:
        payload = json.loads(cleaned)
    except Exception:
        json_text = _extract_json_object(cleaned)
        try:
            payload = json.loads(json_text)
        except Exception as exc:
            raise ArkAPIError(f"素材排序 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc

    raw_items = payload.get("clip_order") if isinstance(payload, dict) else None
    if raw_items is None and isinstance(payload, list):
        raw_items = payload
    if not isinstance(raw_items, list):
        raise ArkAPIError(f"素材排序结果缺少 clip_order：{cleaned}")

    items: list[StoryboardClipPlan] = []
    seen_clip_ids: set[int] = set()
    for index, item in enumerate(raw_items, start=1):
        try:
            plan_item = StoryboardClipPlan.model_validate(item)
        except Exception:
            continue
        if plan_item.clip_id not in allowed_clip_ids or plan_item.clip_id in seen_clip_ids:
            continue
        seen_clip_ids.add(plan_item.clip_id)
        items.append(
            StoryboardClipPlan(
                clip_id=plan_item.clip_id,
                position=len(items) + 1,
                section=plan_item.section.strip() or "脚本段落",
                role=plan_item.role.strip() or "匹配脚本画面",
                suggested_duration_seconds=max(1, min(int(plan_item.suggested_duration_seconds), MAX_STORY_CLIP_SECONDS)),
                script_line=plan_item.script_line.strip(),
                reason=plan_item.reason.strip(),
            )
        )

    if not items:
        raise ArkAPIError("素材排序没有匹配到任何有效素材。")
    return StoryboardClipOrder(clip_order=items)


def _parse_storyboard_framework(raw: str) -> StoryboardFramework:
    """解析叙事框架 JSON。"""
    cleaned = _strip_code_block(raw)
    payload: dict
    try:
        payload = json.loads(cleaned)
    except Exception:
        json_text = _extract_json_object(cleaned)
        try:
            payload = json.loads(json_text)
        except Exception as exc:
            raise ArkAPIError(f"叙事框架 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc
    try:
        return StoryboardFramework.model_validate(_normalize_framework_payload(payload))
    except Exception as exc:
        raise ArkAPIError(f"叙事框架 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc


def _parse_story_directions(raw: str) -> StoryDirections:
    """解析三个方向，并把模型不稳定的 id 归一化为稳定的 direction-1..3。"""
    cleaned = _strip_code_block(raw)
    try:
        payload = json.loads(cleaned)
    except Exception:
        try:
            payload = json.loads(_extract_json_object(cleaned))
        except Exception as exc:
            raise ArkAPIError(f"故事方向 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc
    try:
        directions = StoryDirections.model_validate(payload)
    except Exception as exc:
        raise ArkAPIError(f"故事方向 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc

    normalized: list[StoryDirection] = []
    titles: set[str] = set()
    for index, direction in enumerate(directions.directions, start=1):
        title = direction.title.strip()
        if not title or title in titles:
            raise ArkAPIError("三个故事方向需要各自有不同且明确的标题。")
        titles.add(title)
        structure = [item.strip() for item in direction.structure if item and item.strip()]
        if not 3 <= len(structure) <= 4:
            raise ArkAPIError("每个故事方向需要包含 3–4 个清晰的结构段落。")
        normalized.append(StoryDirection(
            id=f"direction-{index}", title=title, style=direction.style.strip(), structure=structure,
        ))
    return StoryDirections(directions=normalized)


def _parse_story_move_interpretation(raw: str) -> StoryMoveInterpretation:
    """解析局部编辑解释，解析失败交给调用方显示明确错误。"""
    cleaned = _strip_code_block(raw)
    try:
        payload = json.loads(cleaned)
    except Exception:
        payload = json.loads(_extract_json_object(cleaned))
    try:
        result = StoryMoveInterpretation.model_validate(payload)
    except Exception as exc:
        raise ArkAPIError(f"素材移动建议 JSON 解析失败：{exc}；原始内容：{cleaned}") from exc
    result.explanation = result.explanation.strip()
    result.suggested_intent = result.suggested_intent.strip() if result.suggested_intent else None
    result.suggested_script = result.suggested_script.strip() if result.suggested_script else None
    result.suggested_thesis = result.suggested_thesis.strip() if result.suggested_thesis else None
    return result


def format_storyboard_framework(framework: StoryboardFramework) -> str:
    """把结构化框架转成页面可读文本。"""
    parts = [
        f"标题：{framework.title.strip()}",
        f"核心表达：{framework.core_message.strip()}",
    ]
    if framework.emotional_arc:
        parts.append("情绪走向：" + " → ".join(item.strip() for item in framework.emotional_arc if item.strip()))
    parts.append("叙事框架：")
    parts.append(framework.narrative_framework.strip())
    if framework.sections:
        parts.append("段落设计：")
        parts.extend(f"{index}. {section.strip()}" for index, section in enumerate(framework.sections, start=1) if section.strip())
    return "\n".join(part for part in parts if part.strip())


def _format_clip_context(clip: StoryClipContext) -> str:
    """把单条素材整理成模型容易引用的文本。"""
    lines = [
        f"素材 ID：{clip.clip_id}",
        f"文件名：{clip.filename}",
        f"画面摘要：{clip.summary}",
        f"场景：{clip.scene}",
        f"标签：{'、'.join(clip.tags) if clip.tags else '无'}",
        f"主体：{'、'.join(clip.subjects) if clip.subjects else '无'}",
        f"动作：{'、'.join(clip.actions) if clip.actions else '无'}",
        f"收藏/评分：{'已收藏' if clip.is_favorite else '未收藏'}，{clip.rating} 星",
    ]
    if clip.user_note:
        lines.append(f"创作备注：{clip.user_note}")
    if clip.transcript_text:
        lines.append(f"口播摘录：{clip.transcript_text}")
    if clip.source_time:
        lines.append(f"原素材时间：{clip.source_time}")
    return "\n".join(lines)


def _get_story_model_config(*, use_fast_model: bool = False) -> tuple[str, str, str, int]:
    """读取 OpenAI-compatible 故事模型配置，优先使用 STORY_*。

    不需要为不同模型维护不同的调用器。若要切换供应商，只需在 .env 配置
    ``STORY_BASE_URL``、``STORY_API_KEY``、``STORY_MODEL``；也兼容 Qwen、
    DeepSeek、豆包常见的环境变量别名，方便复用已有本地凭证。密钥不会写入代码。
    """
    load_dotenv(override=True)
    provider = os.environ.get("STORY_PROVIDER", "").strip().upper()
    provider_aliases = {
        "QWEN": ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL", "QWEN_MODEL"),
        "DEEPSEEK": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"),
        "DOUBAO": ("DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL"),
    }
    aliases = provider_aliases.get(provider, ())
    api_key_names = ("STORY_API_KEY",) + tuple(name for name in aliases if name.endswith("_API_KEY")) + ("ARK_API_KEY",)
    base_url_names = ("STORY_BASE_URL",) + tuple(name for name in aliases if name.endswith("_BASE_URL")) + ("ARK_BASE_URL",)
    model_names = ("STORY_MODEL",) + tuple(name for name in aliases if name.endswith("_MODEL")) + ("ARK_MODEL",)

    def first_env(names: tuple[str, ...]) -> str:
        return next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")

    api_key = first_env(api_key_names)
    base_url = (
        first_env(base_url_names)
        or "https://ark.cn-beijing.volces.com/api/v3"
    )
    model = first_env(model_names)
    # 快速任务只在显式配置时切换模型，避免升级后意外改变现有故事生成质量。
    if use_fast_model:
        model = os.environ.get("STORY_FAST_MODEL", "").strip() or model
    timeout_seconds = int(
        os.environ.get("STORY_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("ARK_TIMEOUT_SECONDS", "90")
        or "90"
    )
    if not api_key:
        raise RuntimeError("缺少故事模型 API Key；请配置 STORY_API_KEY，或设置 STORY_PROVIDER 后配置对应供应商变量。")
    if not model:
        raise RuntimeError("缺少故事模型名；请配置 STORY_MODEL，或设置 STORY_PROVIDER 后配置对应供应商变量。")
    return api_key, base_url, model, timeout_seconds


def _build_story_chat_messages(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str,
    current_framework_text: str | None,
    current_script_text: str | None,
    history: list[dict[str, str]],
    user_message: str,
) -> list[dict[str, str]]:
    """构造导演 Agent 的多轮对话消息。"""
    clip_context = "\n\n---\n\n".join(_format_clip_context(clip) for clip in clips[:80])
    script_block = current_script_text.strip() if current_script_text else "暂无已生成脚本"
    framework_block = current_framework_text.strip() if current_framework_text else "暂无已确认叙事框架"
    system_prompt = """你是一个短视频创作者的故事导演。
你的任务是把创作者的想法和一批视频素材整理成更好讲的第一人称故事。
工作原则：
- 先理解创作者想表达的东西，再决定怎么讲故事。
- 不要像广告文案或散文朗诵，要像真实 vlog 创作者在和观众说话。
- 画面摘要、口播、创作备注都要用；其中创作者备注和整体想法优先级最高。
- 如果素材里有人物说话或可用口播，优先把它作为故事真实感来源。
- 不要编造素材里没有的关键事件；可以做合理的情绪串联，但不能硬造事实。

现在你不是直接生成最终 JSON，而是在“导演 Agent 工作台”里和创作者对话。
你可以帮用户分析素材、寻找故事灵感、比较叙事方向、提出开头和结构建议。
不要默认修改脚本；如果用户表达的是讨论、发散、询问灵感，只给建议。
如果用户明显想修改现有脚本，你必须在回复里给出一版完整脚本候选稿，并用下面两个独占标记包起来：
【脚本候选稿开始】
这里放完整脚本文字
【脚本候选稿结束】
标记内的文字会被系统直接应用到脚本框，所以不要写省略号、解释、标题或 markdown。
只要用户用了“改、修改、重写、压缩、扩写、换一种说法、调整时长、不要这样写”之类的表达，就视为想修改现有脚本，必须输出完整候选稿。
候选稿必须是改完后的完整脚本，不是局部片段；没有改到的段落也要保留在候选稿里。
标记外可以简短说明你改了什么。
回复要自然、具体、可执行，优先引用素材事实和创作者备注。"""
    context_prompt = f"""当前故事线背景：
创作者整体想法：{brief_text.strip()}
目标时长：{target_duration_seconds} 秒
口吻要求：{tone_prompt.strip() or DEFAULT_TONE_PROMPT}

当前叙事框架：
{framework_block}

当前脚本：
{script_block}

候选素材：
{clip_context}"""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_prompt},
    ]
    messages.extend(history[-12:])
    messages.append({"role": "user", "content": user_message.strip()})
    return messages


def _build_user_prompt(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str,
    framework_text: str | None = None,
    previous_plan_text: str | None = None,
    revision_prompt: str | None = None,
) -> str:
    """构造故事线生成提示词。"""
    # 完整 Story 生成只保留每条素材最有用的事实；长口播逐字送入会明显拖慢模型首响。
    clip_context = "\n\n---\n\n".join(_format_story_plan_clip_context(clip) for clip in clips[:48])
    previous_block = ""
    if previous_plan_text:
        previous_block = f"\n\n上一版故事方案：\n{previous_plan_text.strip()}"
    revision_block = ""
    if revision_prompt:
        revision_block = f"\n\n本次修改要求：\n{revision_prompt.strip()}"
    framework_block = ""
    if framework_text:
        framework_block = f"\n\n已确认叙事框架，必须按这个框架填充完整内容：\n{framework_text.strip()}"

    return f"""创作者整体想法：
{brief_text.strip()}

目标时长：{target_duration_seconds} 秒
口吻要求：{tone_prompt.strip() or DEFAULT_TONE_PROMPT}
{framework_block}{previous_block}{revision_block}

候选素材：
{clip_context}

请输出 JSON，字段必须包含：
- title：故事标题
- target_duration_seconds：目标时长秒数
- tone：实际采用的口吻
- core_message：这个视频真正想表达的核心意思
- emotional_arc：情绪变化数组
- story_plan：故事讲述方案，说明怎么从开头讲到结尾
- first_person_script：完整第一人称叙事脚本
- clip_order：素材排序清单，每项包含 clip_id、position、section、role、suggested_duration_seconds、script_line、reason

注意：
- clip_order 只能使用候选素材中真实存在的素材 ID。
- 将 clip_order 划分为 3–6 个清晰且不重复的 section；同一 section 名称对应一个可直接编辑的 Story Beat。
- 每个 section 都必须有明确的叙事任务（建立场景、推进动作、转折或收束），不能只按地点或文件名机械分组。
- section 内的素材 position 必须服务于可见动作和情绪推进；同一画面信息不要在相邻 Beat 重复表达。
- script_line 必须能被对应素材的画面、口播、备注或标签支持；如果事实不足，写克制的观察，不要补造事件、关系或动机。
- first_person_script 要自然、具体、口语化，避免“治愈、成长、仪式感、刚好”等泛化套话，也不要重复解释同一个意思。
- 素材数量要匹配目标时长，不要把所有素材机械塞进去。
- 如果创作者有修改要求，以修改要求为准，但仍要尊重素材内容。"""


def _format_story_plan_clip_context(clip: StoryClipContext) -> str:
    """为完整 Story 规划压缩单条素材上下文，控制模型输入长度。"""
    lines = [
        f"素材 ID：{clip.clip_id}",
        f"文件名：{clip.filename}",
        f"画面摘要：{clip.summary[:220]}",
        f"场景/标签：{clip.scene}；{'、'.join(clip.tags[:10]) or '无'}",
        f"主体/动作：{'、'.join(clip.subjects[:6]) or '无'}；{'、'.join(clip.actions[:6]) or '无'}",
    ]
    if clip.user_note:
        lines.append(f"创作备注：{clip.user_note[:160]}")
    if clip.transcript_text:
        lines.append(f"口播摘录：{clip.transcript_text[:240]}")
    return "\n".join(lines)


def _build_directions_prompt(
    *, clips: list[StoryClipContext], brief_text: str,
    target_duration_seconds: int, tone_prompt: str,
) -> str:
    """构造在生成 Canvas 前供创作者选择的三个叙事方向提示词。"""
    clip_context = "\n\n---\n\n".join(_format_clip_context(clip) for clip in clips[:80])
    return f"""创作者给出的主题、随手记与要求：
{brief_text.strip()}

目标时长：{target_duration_seconds} 秒
口吻偏好：{tone_prompt.strip() or DEFAULT_TONE_PROMPT}

候选素材事实（其中 Creator Note 优先级最高）：
{clip_context}

请先帮助创作者比较三个真正不同、都能由现有素材支撑的 Story Direction。只需要标题、风格、结构三项，不需要展开解释，严格输出 JSON：
{{
  "directions": [
    {{
      "id": "direction-1",
      "title": "方向标题，本身要能传达这个方向的核心角度，不超过 20 字",
      "style": "口吻/情绪风格，例如“克制、真实、不煽情”，不超过 15 字",
      "structure": ["第一个 Beat 的叙事任务", "第二个 Beat 的叙事任务", "第三个 Beat 的叙事任务"]
    }}
  ]
}}

要求：
- 必须恰好输出 3 个方向；不要用同一套故事只换标题。
- 每个方向的 structure 只给 3–4 个词组式的阶段标签（如"开场自拍"、"出门"、"收束"），不要写成完整句子。
- title 要让人一眼看出这个方向和另外两个的区别，不需要额外解释。
- 不要虚构素材中不存在的人、事件或情绪转折。
- 主题、Creator Notes、文件夹随手记和额外要求优先于泛化 vlog 套话。
- 这里只生成方向的标题、风格、结构，不生成完整脚本、素材排序或 Markdown，也不要输出任何字段以外的解释文字，保持输出尽量简短。"""


def _build_script_prompt(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str,
    framework_text: str | None = None,
    previous_plan_text: str | None = None,
    revision_prompt: str | None = None,
) -> str:
    """构造只生成或改写脚本的提示词。"""
    clip_context = "\n\n---\n\n".join(_format_clip_context(clip) for clip in clips[:80])
    framework_block = f"\n\n已确认叙事框架：\n{framework_text.strip()}" if framework_text else ""
    previous_block = f"\n\n当前脚本和方案：\n{previous_plan_text.strip()}" if previous_plan_text else ""
    revision_block = f"\n\n本次修改要求，优先级最高：\n{revision_prompt.strip()}" if revision_prompt else ""
    return f"""创作者整体想法：
{brief_text.strip()}

目标时长：{target_duration_seconds} 秒
口吻要求：{tone_prompt.strip() or DEFAULT_TONE_PROMPT}
{framework_block}{previous_block}{revision_block}

候选素材事实：
{clip_context}

请只生成或改写“故事方案 + 完整第一人称脚本”，不要输出素材排序清单。
JSON 字段必须包含：
- title：故事标题
- target_duration_seconds：目标时长秒数
- tone：实际采用的口吻
- core_message：这个视频真正想表达的核心意思
- emotional_arc：情绪变化数组
- story_plan：故事讲述方案，说明怎么从开头讲到结尾
- first_person_script：完整第一人称叙事脚本

要求：
- 如果有本次修改要求，必须优先满足；不要只是轻微换词。
- 保留当前脚本里与修改要求无关、仍然成立的部分。
- 不要编造素材里没有的关键事实。
- 不要输出 clip_order、素材排序、解释文字或 markdown。"""


def _build_clip_order_prompt(
    *,
    clips: list[StoryClipContext],
    script_text: str,
    target_duration_seconds: int,
    tone_prompt: str,
) -> str:
    """构造脚本确认后的素材匹配提示词。"""
    clip_context = "\n\n---\n\n".join(_format_clip_context(clip) for clip in clips)
    return f"""已确认的完整脚本：
{script_text.strip()}

目标时长：{target_duration_seconds} 秒
口吻要求：{tone_prompt.strip() or DEFAULT_TONE_PROMPT}

候选素材：
{clip_context}

现在只做素材匹配和排序，不要改写脚本。
请输出 JSON，字段必须包含：
- clip_order：素材排序清单，每项包含 clip_id、position、section、role、suggested_duration_seconds、script_line、reason

要求：
- clip_order 只能使用候选素材中真实存在的素材 ID。
- 根据脚本段落匹配画面，不要机械使用所有素材。
- 同一条素材最多使用一次。
- script_line 写它对应的脚本文字或段落摘要。
- reason 说明为什么这条素材适合这一段。
- 严格返回 JSON，不要 markdown。"""


def _build_framework_prompt(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str,
    revision_prompt: str | None = None,
) -> str:
    """构造只生成叙事框架的提示词。"""
    clip_context = "\n\n---\n\n".join(_format_clip_context(clip) for clip in clips[:40])
    revision_block = f"\n\n修改要求：\n{revision_prompt.strip()}" if revision_prompt else ""
    return f"""创作者整体想法：
{brief_text.strip()}

目标时长：{target_duration_seconds} 秒
口吻要求：{tone_prompt.strip() or DEFAULT_TONE_PROMPT}
{revision_block}

候选素材：
{clip_context}

先不要写完整脚本，也不要输出素材排序清单。
请只输出一个可供创作者确认的叙事框架 JSON，字段必须包含：
- title：故事标题
- core_message：核心表达
- emotional_arc：情绪变化数组
- narrative_framework：叙事框架，说明开头、发展、转折、收束怎么讲
- sections：段落数组，每项说明该段的作用、情绪和需要的画面类型

要求：
- 框架要短，方便用户判断方向。
- 不要填充具体旁白全文。
- 不要编造素材里没有的关键事件。
- 严格返回 JSON，不要 markdown。"""


def _build_move_interpretation_prompt(
    *, thesis: str, source_beat: StoryBeatContext | None, target_beat: StoryBeatContext,
    clip: StoryClipContext, move_description: str,
) -> str:
    """构造一次直接编辑的最小 grounding context，不要求 AI 重写整个故事。"""
    source_text = "无（该素材原本未被使用）"
    if source_beat is not None:
        source_text = (
            f"标题：{source_beat.title}\n意图：{source_beat.intent}\n"
            f"当前旁白：{source_beat.script_text or '无'}"
        )
    target_text = (
        f"标题：{target_beat.title}\n意图：{target_beat.intent}\n"
        f"当前旁白：{target_beat.script_text or '无'}"
    )
    return f"""你正在协助创作者直接拖动真实素材来编辑 Story Canvas。不要自动修改任何故事内容，只提出可选的局部建议。

故事 thesis：{thesis or '尚未明确'}
用户动作：{move_description}

原 Beat：
{source_text}

新 Beat：
{target_text}

被移动素材：
{_format_clip_context(clip)}

判断这个动作是否改变了新 Beat 的叙事含义。只在确有帮助时建议修改新 Beat 的 intent 或局部第一人称旁白；不要重写整篇脚本，不要编造事实。thesis 只有确实受影响时才建议调整。

严格返回 JSON：
- meaning_changed：boolean
- explanation：一句具体、面向创作者的解释
- suggested_intent：可选的新 Beat intent，没有就 null
- suggested_script：可选的新 Beat 局部旁白，没有就 null
- suggested_thesis：可选的新 thesis，没有就 null"""


def _format_ordered_clip_sequence(clips: list[StoryClipContext]) -> str:
    """将一个 Beat 的当前素材顺序与事实一起交给模型，避免只凭文件名猜测。"""
    if not clips:
        return "（此 Beat 暂无素材）"
    return "\n\n---\n\n".join(
        f"顺序 {index}\n{_format_clip_context(clip)}"
        for index, clip in enumerate(clips, start=1)
    )


def _format_ordered_beat_sequence(beats: list[StoryBeatContext]) -> str:
    """把 Beat 顺序交给模型，避免只凭标题猜测结构变化。"""
    if not beats:
        return "（暂无 Beat）"
    return "\n\n---\n\n".join(
        f"顺序 {index}\n标题：{beat.title}\n意图：{beat.intent}\n当前旁白：{beat.script_text or '无'}"
        for index, beat in enumerate(beats, start=1)
    )


def _build_beat_order_interpretation_prompt(
    *, thesis: str, before_beats: list[StoryBeatContext], after_beats: list[StoryBeatContext],
) -> str:
    """构造 Story Beat 整体顺序调整的局部解释，只关心 Beat 之间的先后关系。"""
    return f"""你正在协助创作者直接拖动 Story Beat 来调整整段故事的讲述顺序。不要自动修改故事内容，只提出可选的局部建议。

故事 thesis：{thesis or '尚未明确'}

调整前 Beat 顺序：
{_format_ordered_beat_sequence(before_beats)}

调整后 Beat 顺序：
{_format_ordered_beat_sequence(after_beats)}

判断“调整后”的 Beat 顺序是否改变了整段故事的叙事节奏、逻辑或 thesis 表达。只在确有帮助时建议更新某个 Beat 的 intent 或整体 thesis：
- 建议必须以调整后的顺序为准，不要编造调整前后不存在的 Beat 或内容。
- 如果顺序调整只是轻微变化、故事逻辑依然成立，meaning_changed 应为 false，suggested_intent、suggested_script、suggested_thesis 均为 null。
- suggested_script 通常不适用于整体排序调整，除非某个 Beat 的旁白明显需要因顺序变化而调整开头衔接，否则填 null。

严格返回 JSON：
- meaning_changed：boolean
- explanation：一句具体、面向创作者的解释
- suggested_intent：可选，如果有某个 Beat 的 intent 需要因顺序变化调整，用"标题：新 intent"的格式描述，没有就 null
- suggested_script：可选的局部旁白调整建议，没有就 null
- suggested_thesis：可选的新 thesis，没有就 null"""


def _build_reorder_interpretation_prompt(
    *, thesis: str, beat: StoryBeatContext, moved_clip: StoryClipContext,
    before_clips: list[StoryClipContext], after_clips: list[StoryClipContext],
) -> str:
    """构造同 Beat 排序变更的局部解释，不把排序误当成跨 Beat 移动。"""
    return f"""你正在协助创作者直接重排同一个 Story Beat 内的真实素材。不要自动修改故事内容，只提出可选的局部建议。

故事 thesis：{thesis or '尚未明确'}

当前 Beat：
标题：{beat.title}
意图：{beat.intent}
当前旁白：{beat.script_text or '无'}

用户刚刚把「{moved_clip.filename}」（素材 ID：{moved_clip.clip_id}）在同一 Beat 内改了顺序。

调整前素材顺序：
{_format_ordered_clip_sequence(before_clips)}

调整后素材顺序：
{_format_ordered_clip_sequence(after_clips)}

判断“调整后”的画面顺序是否改变了这个 Beat 的观看节奏或叙事重点。只在确有帮助时建议更新 intent 或局部第一人称旁白：
- 建议必须以调整后的顺序为准，并只引用上面真实存在的画面、口播、备注或标签。
- 不要把同 Beat 重排说成素材移动到另一个 Beat；不要重写整篇脚本；不要编造事实。
- 如果顺序只是轻微调整且旁白仍然成立，meaning_changed 应为 false，suggested_intent 和 suggested_script 均为 null。

严格返回 JSON：
- meaning_changed：boolean
- explanation：一句具体、面向创作者的解释
- suggested_intent：可选的新 Beat intent，没有就 null
- suggested_script：可选的局部第一人称旁白，没有就 null
- suggested_thesis：同 Beat 内排序通常为 null；只有确实影响全片表达时才填写"""


def _is_deepseek_fast_request(base_url: str, model: str) -> bool:
    """判断快速请求是否应附带 DeepSeek 专属参数。"""
    normalized_url = base_url.strip().lower()
    normalized_model = model.strip().lower()
    return (
        "deepseek" in normalized_url
        or normalized_model == "deepseek"
        or normalized_model.startswith(("deepseek-", "deepseek_"))
    )


def _build_request_body(prompt: str, *, use_fast_model: bool = False) -> bytes:
    """构造故事线 Chat Completions 请求体。"""
    _, base_url, model, _ = _get_story_model_config(use_fast_model=use_fast_model)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.45,
    }
    # 只有显式配置快速模型且目标是 DeepSeek 时，才发送 DeepSeek 专属字段。
    # 未配置 STORY_FAST_MODEL 时，快速任务回退到 STORY_MODEL，保持原有请求行为。
    fast_model_configured = bool(os.environ.get("STORY_FAST_MODEL", "").strip())
    if use_fast_model and fast_model_configured and _is_deepseek_fast_request(base_url, model):
        body.update({
            "thinking": {"type": "disabled"},
            "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
            "max_tokens": 1024,
        })
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _call_story_model(prompt: str, *, use_fast_model: bool = False) -> str:
    """调用故事线聊天接口并返回文本。"""
    api_key, base_url, _, timeout_seconds = _get_story_model_config(use_fast_model=use_fast_model)
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    body = _build_request_body(prompt, use_fast_model=use_fast_model)
    req = request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=max(timeout_seconds, 30)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="ignore")
        raise ArkAPIError(f"故事线 API 返回 HTTP {exc.code}：{body_text}") from exc
    return _extract_text(payload)


def rewrite_script_selection(
    *,
    clips: list[StoryClipContext],
    full_script_text: str,
    selected_text: str,
    correction_reason: str,
    tone_prompt: str = DEFAULT_TONE_PROMPT,
) -> str:
    """根据用户指出的问题，只改写脚本中被选中的一小段文字。"""
    api_key, base_url, model, timeout_seconds = _get_story_model_config()
    clip_context = "\n\n---\n\n".join(_format_clip_context(clip) for clip in clips[:60])
    messages = [
        {
            "role": "system",
            "content": """你是短视频脚本编辑。你的任务是修正用户选中的一小段脚本文字。
必须遵守：
- 只返回替换后的这一小段文字，不要解释，不要 markdown，不要引号。
- 根据用户指出的错误原因修正，避免编造素材里没有的事实。
- 保持第一人称、口语化、真实 vlog 旁白。
- 保持和上下文语气一致。""",
        },
        {
            "role": "user",
            "content": f"""完整脚本：
{full_script_text.strip()}

需要修改的原文：
{selected_text.strip()}

用户指出的问题：
{correction_reason.strip()}

口吻要求：
{tone_prompt.strip() or DEFAULT_TONE_PROMPT}

素材上下文：
{clip_context}

请只返回“需要修改的原文”的替换文本。""",
        },
    ]
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.35,
    }
    req = request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=max(timeout_seconds, 30)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="ignore")
        raise ArkAPIError(f"局部脚本修改 API 返回 HTTP {exc.code}：{body_text}") from exc
    rewritten = _strip_code_block(_extract_text(payload)).strip().strip('"“”')
    if not rewritten:
        raise ArkAPIError("局部脚本修改返回为空。")
    return rewritten


def stream_story_agent_reply(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str,
    current_framework_text: str | None,
    current_script_text: str | None,
    history: list[dict[str, str]],
    user_message: str,
) -> Iterator[StoryAgentChunk]:
    """流式调用导演 Agent，对外输出思考和正文增量。"""
    api_key, base_url, model, timeout_seconds = _get_story_model_config()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": _build_story_chat_messages(
            clips=clips,
            brief_text=brief_text,
            target_duration_seconds=target_duration_seconds,
            tone_prompt=tone_prompt,
            current_framework_text=current_framework_text,
            current_script_text=current_script_text,
            history=history,
            user_message=user_message,
        ),
        "temperature": 0.65,
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "enabled"},
        "reasoning_effort": os.environ.get("STORY_REASONING_EFFORT", "high").strip() or "high",
    }
    req = request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=max(timeout_seconds, 30)) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = payload.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning_content")
                content = delta.get("content")
                if reasoning:
                    yield StoryAgentChunk("reasoning", str(reasoning))
                if content:
                    yield StoryAgentChunk("content", str(content))
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="ignore")
        raise ArkAPIError(f"导演 Agent API 返回 HTTP {exc.code}：{body_text}") from exc


def _extract_text(payload: dict) -> str:
    """从方舟 chat 响应里提取文本。"""
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ArkAPIError(f"故事线响应结构异常：{payload}") from exc
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict)).strip()
    raise ArkAPIError(f"无法解析故事线响应内容：{content}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_storyboard_framework(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str = DEFAULT_TONE_PROMPT,
    revision_prompt: str | None = None,
) -> StoryboardFramework:
    """先生成供用户确认的叙事框架。"""
    if not clips:
        raise ValueError("没有可用于生成故事线的素材。")
    prompt = _build_framework_prompt(
        clips=clips,
        brief_text=brief_text,
        target_duration_seconds=target_duration_seconds,
        tone_prompt=tone_prompt,
        revision_prompt=revision_prompt,
    )
    return _parse_storyboard_framework(_call_story_model(prompt))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_story_directions(
    *, clips: list[StoryClipContext], brief_text: str,
    target_duration_seconds: int, tone_prompt: str = DEFAULT_TONE_PROMPT,
) -> StoryDirections:
    """基于真实素材和随手记生成三个待确认的 Story Direction。"""
    if not clips:
        raise ValueError("没有可用于生成故事方向的素材。")
    prompt = _build_directions_prompt(
        clips=clips, brief_text=brief_text,
        target_duration_seconds=target_duration_seconds, tone_prompt=tone_prompt,
    )
    return _parse_story_directions(_call_story_model(prompt, use_fast_model=True))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_storyboard_plan(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str = DEFAULT_TONE_PROMPT,
    framework_text: str | None = None,
    previous_plan_text: str | None = None,
    revision_prompt: str | None = None,
) -> StoryboardPlan:
    """根据候选素材和创作者想法生成故事线。"""
    if not clips:
        raise ValueError("没有可用于生成故事线的素材。")

    prompt = _build_user_prompt(
        clips=clips,
        brief_text=brief_text,
        target_duration_seconds=target_duration_seconds,
        tone_prompt=tone_prompt,
        framework_text=framework_text,
        previous_plan_text=previous_plan_text,
        revision_prompt=revision_prompt,
    )
    raw = _call_story_model(prompt)
    return _parse_storyboard_plan(
        raw,
        allowed_clip_ids={clip.clip_id for clip in clips},
        target_duration_seconds=target_duration_seconds,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_story_move_interpretation(
    *, thesis: str, source_beat: StoryBeatContext | None, target_beat: StoryBeatContext,
    clip: StoryClipContext, move_description: str,
) -> StoryMoveInterpretation:
    """解释一次素材拖动，只返回 suggestion，不会写入数据库或自动改稿。"""
    prompt = _build_move_interpretation_prompt(
        thesis=thesis,
        source_beat=source_beat,
        target_beat=target_beat,
        clip=clip,
        move_description=move_description,
    )
    return _parse_story_move_interpretation(_call_story_model(prompt, use_fast_model=True))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_story_reorder_interpretation(
    *, thesis: str, beat: StoryBeatContext, moved_clip: StoryClipContext,
    before_clips: list[StoryClipContext], after_clips: list[StoryClipContext],
) -> StoryMoveInterpretation:
    """解释一个 Beat 内的素材重排，只返回待确认的局部修改建议。"""
    if [clip.clip_id for clip in before_clips] == [clip.clip_id for clip in after_clips]:
        return StoryMoveInterpretation(meaning_changed=False, explanation="素材顺序没有变化。")
    prompt = _build_reorder_interpretation_prompt(
        thesis=thesis,
        beat=beat,
        moved_clip=moved_clip,
        before_clips=before_clips,
        after_clips=after_clips,
    )
    return _parse_story_move_interpretation(_call_story_model(prompt, use_fast_model=True))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_beat_order_interpretation(
    *, thesis: str, before_beats: list[StoryBeatContext], after_beats: list[StoryBeatContext],
) -> StoryMoveInterpretation:
    """解释一次 Story Beat 整体顺序调整，只返回待确认的局部修改建议。"""
    if [beat.title for beat in before_beats] == [beat.title for beat in after_beats]:
        return StoryMoveInterpretation(meaning_changed=False, explanation="Beat 顺序没有变化。")
    prompt = _build_beat_order_interpretation_prompt(
        thesis=thesis,
        before_beats=before_beats,
        after_beats=after_beats,
    )
    return _parse_story_move_interpretation(_call_story_model(prompt, use_fast_model=True))


@dataclass
class ChatBeatContext:
    """带 id 的 Beat 上下文，供原生导演 Agent 对话引用真实 Beat。"""

    id: int
    title: str
    intent: str
    script_text: str | None = None


@dataclass
class NativeAgentSuggestion:
    """原生导演 Agent 对话解析出的、针对单个 Beat 的待确认建议。"""

    beat_id: int
    meaning_changed: bool
    explanation: str
    suggested_intent: str | None = None
    suggested_script: str | None = None
    suggested_thesis: str | None = None


_NATIVE_CHAT_SUGGESTION_START = "【BEAT_SUGGESTION_START】"
_NATIVE_CHAT_SUGGESTION_END = "【BEAT_SUGGESTION_END】"


def _format_chat_beats(beats: list[ChatBeatContext], focused_beat_id: int | None) -> str:
    """把当前 Beat 列表交给模型，标出创作者当前聚焦的 Beat。"""
    if not beats:
        return "（这个故事还没有 Beat）"
    lines = []
    for beat in beats:
        marker = "（当前聚焦）" if beat.id == focused_beat_id else ""
        lines.append(f"Beat「{beat.title}」{marker}\n意图：{beat.intent}\n当前旁白：{beat.script_text or '无'}")
    return "\n\n---\n\n".join(lines)


def _build_native_story_chat_messages(
    *, thesis: str, beats: list[ChatBeatContext], focused_beat_id: int | None,
    clips: list[StoryClipContext], history: list[dict[str, str]], user_message: str,
) -> list[dict[str, str]]:
    """构造原生 Shape Story 里导演 Agent 对话的多轮消息。"""
    # 原生导演台的追问优先需要当前故事附近的可用事实；长口播和过多素材会让
    # 每轮对话上下文不断膨胀，直接拉长模型首字时间。
    clip_context = "\n\n---\n\n".join(_format_native_chat_clip_context(clip) for clip in clips[:24])
    system_prompt = """你是一个短视频创作者的故事导演，正在 Reelsift 的 Shape Story 工作台里和创作者聊天。
这个故事已经拆成若干 Story Beat（每个 Beat 有标题、意图 intent、旁白 script_text），这是 Beat 粒度的 Canvas，不是一整段脚本。
工作原则：
- 先理解创作者想表达的东西，再决定怎么讲故事；不要写成广告文案，要像真实 vlog 创作者说话。
- 只引用下面给出的真实 Beat 和素材事实，不要编造不存在的 Beat、素材或事件。
- 如果用户只是讨论、发散、问灵感，就正常聊，不要给修改建议。
- 如果用户明显想修改某个 Beat 的台词或意图（用了"改、换、重写、更幽默、更克制、压缩、扩写"之类的表达），必须在回复最后用下面两个独占标记包一段 JSON：
【BEAT_SUGGESTION_START】
{"beat_title": "要修改的 Beat 标题，必须是下面列表里真实存在的标题", "meaning_changed": true, "explanation": "一句话说明这次修改改变了什么", "suggested_intent": "新的 intent，不需要改就填 null", "suggested_script": "新的旁白，不需要改就填 null", "suggested_thesis": "只有确实影响整体表达才填，否则 null"}
【BEAT_SUGGESTION_END】
- 标记内必须是合法 JSON，不要写省略号或注释；标记外可以简短说明你的想法。
- 如果不确定改哪个 Beat，优先默认"当前聚焦"的 Beat；如果连当前聚焦的 Beat 都判断不出来改什么，就不要输出标记，先反问创作者。
- 你不能提议增加、删除或整体重排 Beat；这类大改动要请创作者去用 Regenerate Story，不要在这里假装做到。"""
    context_prompt = f"""故事 thesis：{thesis or '尚未明确'}

当前 Beat 列表：
{_format_chat_beats(beats, focused_beat_id)}

候选素材事实：
{clip_context}"""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_prompt},
    ]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": user_message.strip()})
    return messages


def _format_native_chat_clip_context(clip: StoryClipContext) -> str:
    """压缩原生 Agent 的素材事实，保留叙事判断所需信息。"""
    fields = [
        f"素材 {clip.clip_id}：{clip.filename}",
        f"摘要：{clip.summary[:180]}",
        f"场景/标签：{clip.scene}；{'、'.join(clip.tags[:8]) or '无'}",
    ]
    if clip.user_note:
        fields.append(f"备注：{clip.user_note[:120]}")
    if clip.transcript_text:
        fields.append(f"口播摘录：{clip.transcript_text[:180]}")
    return "\n".join(fields)


def _call_story_chat_model(messages: list[dict[str, str]], *, temperature: float = 0.6) -> dict[str, str]:
    """非流式调用故事线聊天接口，返回正文与思考过程。"""
    api_key, base_url, model, timeout_seconds = _get_story_model_config()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    body = {"model": model, "messages": messages, "temperature": temperature}
    req = request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=max(timeout_seconds, 30)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="ignore")
        raise ArkAPIError(f"导演 Agent 对话 API 返回 HTTP {exc.code}：{body_text}") from exc
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ArkAPIError(f"导演 Agent 对话响应结构异常：{payload}") from exc
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    return {"content": str(content).strip(), "reasoning": str(reasoning).strip()}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_story_agent_reply(
    *, thesis: str, beats: list[ChatBeatContext], focused_beat_id: int | None,
    clips: list[StoryClipContext], history: list[dict[str, str]], user_message: str,
) -> tuple[str, str]:
    """调用原生导演 Agent 对话，返回 (reasoning_text, assistant_text)。"""
    messages = _build_native_story_chat_messages(
        thesis=thesis, beats=beats, focused_beat_id=focused_beat_id,
        clips=clips, history=history, user_message=user_message,
    )
    result = _call_story_chat_model(messages)
    return result["reasoning"], result["content"]


def strip_native_agent_suggestion(assistant_text: str) -> str:
    """聊天区只展示说明文字，不展示原始 JSON 标记。"""
    pattern = re.escape(_NATIVE_CHAT_SUGGESTION_START) + r"\s*(.*?)\s*" + re.escape(_NATIVE_CHAT_SUGGESTION_END)
    stripped = re.sub(pattern, "", assistant_text, flags=re.S).strip()
    return stripped or "我已经给出一版修改建议，可以在下面的 AI 建议卡片里查看并确认。"


def _parse_native_agent_suggestion(
    assistant_text: str, beats: list[ChatBeatContext], focused_beat_id: int | None,
) -> NativeAgentSuggestion | None:
    """从对话回复里解析出针对单个 Beat 的建议；解析不出就当作纯讨论。"""
    match = re.search(
        re.escape(_NATIVE_CHAT_SUGGESTION_START) + r"\s*(.*?)\s*" + re.escape(_NATIVE_CHAT_SUGGESTION_END),
        assistant_text,
        re.S,
    )
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        payload = json.loads(_strip_code_block(raw))
    except Exception:
        try:
            payload = json.loads(_extract_json_object(raw))
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None

    beat_title = str(payload.get("beat_title") or "").strip()
    target_beat: ChatBeatContext | None = None
    if beat_title:
        target_beat = next((beat for beat in beats if beat.title == beat_title), None)
        if target_beat is None:
            target_beat = next(
                (beat for beat in beats if beat_title in beat.title or beat.title in beat_title), None,
            )
    if target_beat is None and focused_beat_id is not None:
        target_beat = next((beat for beat in beats if beat.id == focused_beat_id), None)
    if target_beat is None:
        return None

    def _clean(value: object) -> str | None:
        text = str(value).strip() if value else ""
        return text or None

    return NativeAgentSuggestion(
        beat_id=target_beat.id,
        meaning_changed=bool(payload.get("meaning_changed", True)),
        explanation=str(payload.get("explanation") or "").strip() or "AI 在对话中提出了一版修改建议。",
        suggested_intent=_clean(payload.get("suggested_intent")),
        suggested_script=_clean(payload.get("suggested_script")),
        suggested_thesis=_clean(payload.get("suggested_thesis")),
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_storyboard_script(
    *,
    clips: list[StoryClipContext],
    brief_text: str,
    target_duration_seconds: int,
    tone_prompt: str = DEFAULT_TONE_PROMPT,
    framework_text: str | None = None,
    previous_plan_text: str | None = None,
    revision_prompt: str | None = None,
) -> StoryboardScript:
    """只生成或改写脚本，不生成素材排序。"""
    if not clips:
        raise ValueError("没有可用于生成故事线的素材。")
    prompt = _build_script_prompt(
        clips=clips,
        brief_text=brief_text,
        target_duration_seconds=target_duration_seconds,
        tone_prompt=tone_prompt,
        framework_text=framework_text,
        previous_plan_text=previous_plan_text,
        revision_prompt=revision_prompt,
    )
    return _parse_storyboard_script(_call_story_model(prompt), target_duration_seconds)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def generate_storyboard_clip_order(
    *,
    clips: list[StoryClipContext],
    script_text: str,
    target_duration_seconds: int,
    tone_prompt: str = DEFAULT_TONE_PROMPT,
) -> StoryboardClipOrder:
    """脚本确认后再生成素材排序。"""
    if not clips:
        raise ValueError("没有可用于匹配的素材。")
    if not script_text.strip():
        raise ValueError("请先确认脚本，再匹配素材。")
    prompt = _build_clip_order_prompt(
        clips=clips,
        script_text=script_text,
        target_duration_seconds=target_duration_seconds,
        tone_prompt=tone_prompt,
    )
    return _parse_storyboard_clip_order(
        _call_story_model(prompt),
        allowed_clip_ids={clip.clip_id for clip in clips},
    )
