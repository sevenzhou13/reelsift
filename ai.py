# 豆包视觉 API 调用封装：输入 6 张关键帧，返回结构化视频分析

import base64
import json
import os
from pathlib import Path
from urllib import error, request

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv(override=True)

_ARK_API_KEY = os.environ.get("ARK_API_KEY")
_ARK_BASE_URL = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
_ARK_MODEL = os.environ.get("ARK_MODEL", "")
_ARK_API_STYLE = os.environ.get("ARK_API_STYLE", "chat")

_SYSTEM_PROMPT = """你是一个视频素材分析助手。用户是短视频创作者，主要拍摄生活 vlog 和旅行记录。
我会给你同一个视频中均匀抽取的 6 张关键帧。
请分析整段视频的内容，给出适合“素材筛选”和“后续检索”的结构化 JSON 输出。

输出要求：
- summary 字段：一句话客观描述，尽量 10-16 个字，优先写成“谁在什么场景做什么”的筛片用语。
  目标是让创作者一眼知道“这条视频素材的主事件是什么”，而不是单纯描述局部画面。
  先概括整条视频，再考虑细节。只有当局部特写本身就是主体时，才写“脚部特写”“手部特写”这类词。
  如果能判断出人物关系或人数，优先保留，例如“两人自拍”“情侣自拍”“两位女生互动”“旅客排队通行”。
  好例子："机场内行人匆匆行走" "电梯内两人自拍，男子低头" "地铁车厢情侣自拍，背景线路图" "冰面滑行第一视角，人群玩耍"
  坏例子："人物脚部特写" "手部与鞋子局部画面" "画面中有几个人在移动"
  不要用"一段"、"这是"开头，不要用抒情形容词，不要写成完整解说句。
- scene 字段：具体场景名词。如"咖啡店"、"街道"、"室内"、"地铁"。
  不要用"户外"、"公共场所"这种太宽泛的词。
  scene 必须基于多张关键帧里稳定出现的环境证据判断，不能只根据单张局部近景脑补。
  如果环境线索不足，但能确定是桌下、近景、室内局部，可以用更保守的场景词，如"餐桌下"、"室内桌下"、"室内近景"。
  不要为了追求具体，硬猜成"电梯"、"地铁车厢"、"机场"等高置信度场景，除非 6 张图里都有足够证据支持。
  特别注意：如果主要画面是脚部、手部、桌下、座位旁、腿部、手机近景等局部视角，而缺少明确环境特征，优先判断为"餐桌下"、"桌下近景"、"室内近景"、"局部特写"；不要脑补成电梯或地铁。
  只有当多张图反复出现电梯门、镜面轿厢、楼层门缝，才能判断为电梯；只有当多张图反复出现车厢扶手、线路图、车窗、长条座椅，才能判断为地铁车厢。
- subjects 字段：数组，视频里的主要主体（最多 3 个）。
  选项：人物、食物、建筑、风景、物品、动物、交通工具、文字/招牌
- actions 字段：数组，视频里发生的动作（最多 3 个）。
  如：倒奶、拉花、走路、说话、吃、笑、看、指、拿起
  如果是静态画面（空镜），actions 为空数组 []
- tags 字段：数组，3-5 个适合筛片的检索标签。
  优先给“具体、可筛选”的词，不要堆泛词。
  标签优先级：
  1. 具体场景：机场、值机区、地铁、电梯、冰场、校园
  2. 主体或元素：行李箱、校服、自拍、招牌、滑冰、线路图
  3. 人物关系或镜头类型：情侣、两人、第一视角、自拍
  4. 景别：特写、中景、远景
  5. 少量动作或状态：走动、互动、微笑、排队
  只有在镜头移动特别明显时才用"运镜"；不要轻易给"室内"、"人物"这类过泛标签，除非没有更具体的词。
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


class ArkAPIError(RuntimeError):
    """方舟 API 调用错误。"""


def _build_data_url(image_path: Path) -> str:
    """把本地图片编码成 data URL。"""
    encoded = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def _build_request_body(keyframe_paths: list[Path]) -> bytes:
    """构造方舟 Chat Completions 请求体。"""
    content: list[dict[str, object]] = []
    for image_path in sorted(keyframe_paths):
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _build_data_url(image_path),
                },
            }
        )
    content.append({"type": "text", "text": _USER_PROMPT})

    body = {
        "model": _ARK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _build_responses_body(keyframe_paths: list[Path]) -> bytes:
    """构造方舟 Responses API 请求体。"""
    content: list[dict[str, str]] = []
    for image_path in sorted(keyframe_paths):
        content.append(
            {
                "type": "input_image",
                "image_url": _build_data_url(image_path),
            }
        )
    content.append({"type": "input_text", "text": _USER_PROMPT})

    body = {
        "model": _ARK_MODEL,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "temperature": 0.3,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _extract_text(payload: dict) -> str:
    """从方舟响应中提取文本。"""
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ArkAPIError(f"响应结构异常：{payload}") from exc

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        return "".join(text_parts).strip()

    raise ArkAPIError(f"无法解析响应内容：{content}")


def _extract_responses_text(payload: dict) -> str:
    """从方舟 Responses API 响应中提取文本。"""
    output = payload.get("output", [])
    for item in output:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                return str(content.get("text", "")).strip()
    raise ArkAPIError(f"无法解析 Responses 响应：{payload}")


def _strip_code_block(raw: str) -> str:
    """兜底剥离 markdown 代码块。"""
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((error.URLError, error.HTTPError, TimeoutError, ArkAPIError)),
    reraise=True,
)
def analyze_video(keyframe_paths: list[Path]) -> VideoAnalysis:
    """把 6 张关键帧发给豆包视觉模型，返回结构化分析。"""
    if not _ARK_API_KEY:
        raise RuntimeError("缺少 ARK_API_KEY，请在 .env 中配置火山方舟 API Key")
    if not _ARK_MODEL:
        raise RuntimeError("缺少 ARK_MODEL，请在 .env 中配置火山方舟 Endpoint ID")

    if not keyframe_paths:
        raise ValueError("没有可分析的关键帧")

    if _ARK_API_STYLE == "responses":
        endpoint = f"{_ARK_BASE_URL.rstrip('/')}/responses"
        body = _build_responses_body(keyframe_paths)
    else:
        endpoint = f"{_ARK_BASE_URL.rstrip('/')}/chat/completions"
        body = _build_request_body(keyframe_paths)

    req = request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_ARK_API_KEY}",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise ArkAPIError(f"方舟 API 返回 HTTP {exc.code}：{body}") from exc

    if _ARK_API_STYLE == "responses":
        raw = _strip_code_block(_extract_responses_text(payload))
    else:
        raw = _strip_code_block(_extract_text(payload))

    try:
        return VideoAnalysis.model_validate_json(raw)
    except Exception as exc:
        print(f"\n  [调试] 豆包原始返回：{raw}")
        raise ValueError(f"JSON 解析失败：{exc}") from exc
