# 故事线 AI 封装：根据素材上下文和创作者想法生成第一人称叙事方案
from __future__ import annotations

import json
import os
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
- 根据目标时长控制脚本长度、素材数量和每条素材建议时长。
- 需要输出“故事讲述方案 + 素材排序清单”，帮助创作者去剪辑软件里操作。

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


class StoryboardFramework(BaseModel):
    title: str = Field(max_length=80)
    core_message: str
    emotional_arc: list[str]
    narrative_framework: str
    sections: list[str]


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

    plan.clip_order = normalized_items
    plan.target_duration_seconds = target_duration_seconds
    return plan


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
    return "\n".join(lines)


def _get_story_model_config() -> tuple[str, str, str, int]:
    """读取故事线模型配置，优先使用 STORY_*。"""
    load_dotenv(override=True)
    api_key = (os.environ.get("STORY_API_KEY", "").strip() or os.environ.get("ARK_API_KEY", "").strip())
    base_url = (
        os.environ.get("STORY_BASE_URL", "").strip()
        or os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").strip()
    )
    model = (os.environ.get("STORY_MODEL", "").strip() or os.environ.get("ARK_MODEL", "").strip())
    timeout_seconds = int(
        os.environ.get("STORY_TIMEOUT_SECONDS", "").strip()
        or os.environ.get("ARK_TIMEOUT_SECONDS", "90")
        or "90"
    )
    if not api_key:
        raise RuntimeError("缺少 STORY_API_KEY 或 ARK_API_KEY，请在 .env 中配置故事线 API Key")
    if not model:
        raise RuntimeError("缺少 STORY_MODEL 或 ARK_MODEL，请在 .env 中配置故事线模型名")
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
如果用户明显想修改现有脚本，你可以说明你建议如何改，并等待用户点击“应用到脚本”。
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
    clip_context = "\n\n---\n\n".join(_format_clip_context(clip) for clip in clips)
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
- 素材数量要匹配目标时长，不要把所有素材机械塞进去。
- 如果创作者有修改要求，以修改要求为准，但仍要尊重素材内容。"""


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


def _build_request_body(prompt: str) -> bytes:
    """构造故事线 Chat Completions 请求体。"""
    _, _, model, _ = _get_story_model_config()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.45,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _call_story_model(prompt: str) -> str:
    """调用故事线聊天接口并返回文本。"""
    api_key, base_url, _, timeout_seconds = _get_story_model_config()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    body = _build_request_body(prompt)
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
