# Gemini API 调用封装：输入 6 张关键帧，返回结构化视频分析

import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

_SYSTEM_PROMPT = """你是一个视频素材分析助手。用户是短视频创作者，主要拍摄生活 vlog 和旅行记录。
我会给你同一个视频中均匀抽取的 6 张关键帧。
请分析整段视频的内容，给出结构化的 JSON 输出。

输出要求：
- summary 字段：一句话客观描述，15 字以内，名词短语为主。
  好例子："咖啡拉花特写，慢速倒奶" "外滩傍晚远景，行人经过"
  坏例子："令人心动的咖啡时光" "这是一段拍摄于咖啡店的美好画面"
  不要用"一段"、"这是"开头，不要用抒情形容词。
- scene 字段：具体场景名词。如"咖啡店"、"街道"、"室内"、"地铁"。
  不要用"户外"、"公共场所"这种太宽泛的词。
- subjects 字段：数组，视频里的主要主体（最多 3 个）。
  选项：人物、食物、建筑、风景、物品、动物、交通工具、文字/招牌
- actions 字段：数组，视频里发生的动作（最多 3 个）。
  如：倒奶、拉花、走路、说话、吃、笑、看、指、拿起
  如果是静态画面（空镜），actions 为空数组 []
- tags 字段：数组，3-5 个通用标签。
  可以是：特写、中景、远景、运镜、空镜、黄昏、阴天、人物、食物等
- has_motion 字段：布尔，画面主体是否有明显运动

严格返回 JSON，不要 markdown 代码块，不要任何解释文字。"""

_USER_PROMPT = "请分析这 6 张关键帧，它们来自同一段视频，按时间顺序排列。"


class VideoAnalysis(BaseModel):
    summary: str = Field(max_length=40)
    scene: str
    subjects: list[str]
    actions: list[str]
    tags: list[str]
    has_motion: bool


# 不重试配额/鉴权/地区错误
_NON_RETRYABLE = ("quota", "api_key_invalid", "permission_denied", "403", "401", "location")


def _is_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return not any(kw in msg for kw in _NON_RETRYABLE)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def analyze_video(keyframe_paths: list[Path]) -> VideoAnalysis:
    """把 6 张关键帧以字节内嵌方式发给 Gemini，返回结构化分析。失败时抛异常。"""
    # 直接内嵌图片字节，避免使用 Files API（有地区限制）
    parts: list = []
    for p in sorted(keyframe_paths):
        parts.append(types.Part.from_bytes(
            data=p.read_bytes(),
            mime_type="image/jpeg",
        ))
    parts.append(_USER_PROMPT)

    response = _client.models.generate_content(
        model="gemini-2.5-flash",
        contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )

    raw = response.text.strip()

    # 兜底：剥掉 markdown 代码块
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return VideoAnalysis.model_validate_json(raw)
    except Exception as e:
        print(f"\n  [调试] Gemini 原始返回：{raw}")
        raise ValueError(f"JSON 解析失败：{e}") from e
