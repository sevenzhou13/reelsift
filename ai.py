# 豆包视觉 API 调用封装：输入 6 张关键帧，返回结构化视频分析
from __future__ import annotations

import base64
import json
import os
import re
import socket
from io import BytesIO
from pathlib import Path
from urllib import error, request

from dotenv import load_dotenv
from PIL import Image
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv(override=True)

_SYSTEM_PROMPT = """你是一个视频素材分析助手。用户是短视频创作者，主要拍摄生活 vlog 和旅行记录。
我会给你同一个视频中均匀抽取的 6 张关键帧。
有时还会补充这段视频的口播转写。
请综合“画面内容 + 口播内容”分析整段视频，给出适合“素材筛选”和“后续检索”的结构化 JSON 输出。

联合分析规则：
- 场景判断优先依赖关键帧，不要只根据口播脑补地点。
- summary、actions、tags 可以适度参考口播，用来补充事件主题、人物动作和说话内容。
- 如果口播与画面有轻微冲突，以画面为准；如果口播能明确说明事件主题，可让 summary 更贴近事件。
- 如果口播只有语气词、寒暄、无意义碎片，不要把这些内容机械写进 summary。

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


def _get_request_timeout_seconds() -> int:
    """读取视觉请求超时时间，默认比之前更宽松一些。"""
    raw_value = os.environ.get("ARK_TIMEOUT_SECONDS", "90").strip()
    try:
        timeout_seconds = int(raw_value)
    except ValueError:
        return 90
    return max(timeout_seconds, 30)


def _get_request_size_text(body: bytes) -> str:
    """把请求体大小转成人类可读文本，便于排查断连问题。"""
    size_mb = len(body) / (1024 * 1024)
    return f"{size_mb:.2f}MB"


def _get_image_max_edge() -> int:
    """读取关键帧压缩后的最长边，默认控制在更稳妥的范围。"""
    raw_value = os.environ.get("ARK_IMAGE_MAX_EDGE", "960").strip()
    try:
        max_edge = int(raw_value)
    except ValueError:
        return 960
    return min(max(max_edge, 480), 1600)


def _get_image_quality() -> int:
    """读取 JPEG 压缩质量，兼顾识别效果和请求体大小。"""
    raw_value = os.environ.get("ARK_IMAGE_QUALITY", "78").strip()
    try:
        quality = int(raw_value)
    except ValueError:
        return 78
    return min(max(quality, 50), 90)


def _format_url_error_message(exc: error.URLError, endpoint: str, body: bytes) -> str:
    """把 urllib 的网络错误翻译成更清晰的中文信息。"""
    reason = exc.reason
    request_size = _get_request_size_text(body)
    endpoint_label = endpoint.rsplit("/", 1)[-1]

    if isinstance(reason, (socket.timeout, TimeoutError)):
        return (
            f"视觉摘要请求超时：等待方舟接口响应超过限制（{endpoint_label}，请求体约 {request_size}）。"
            "请稍后重试，或适当减小关键帧尺寸。"
        )

    if isinstance(reason, socket.gaierror):
        return (
            f"视觉摘要请求失败：无法解析方舟接口域名（{endpoint_label}）。"
            "请检查当前网络、DNS 或代理设置。"
        )

    if isinstance(reason, BrokenPipeError) or (isinstance(reason, OSError) and getattr(reason, "errno", None) == 32):
        return (
            f"视觉摘要请求失败：连接在发送过程中被远端中断（Broken pipe，{endpoint_label}，请求体约 {request_size}）。"
            "常见原因是网络波动、请求体过大，或服务端提前断开连接。请稍后重试。"
        )

    if isinstance(reason, ConnectionResetError) or (isinstance(reason, OSError) and getattr(reason, "errno", None) in {54, 104}):
        return (
            f"视觉摘要请求失败：连接被远端重置（{endpoint_label}，请求体约 {request_size}）。"
            "常见原因是网络波动或服务端临时不可用。请稍后重试。"
        )

    if isinstance(reason, ConnectionRefusedError):
        return (
            f"视觉摘要请求失败：无法连接到方舟接口（{endpoint_label}）。"
            "请检查接口地址、代理配置或当前网络。"
        )

    return (
        f"视觉摘要请求失败：网络请求异常（{endpoint_label}，请求体约 {request_size}）：{exc}。"
        "请稍后重试。"
    )


def _extract_json_object(raw: str) -> str:
    """从模型输出里尽量截取第一个完整 JSON 对象。"""
    start = raw.find("{")
    if start == -1:
        raise ArkAPIError(f"模型返回中没有 JSON 对象：{raw}")

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

    raise ArkAPIError(f"模型返回的 JSON 对象不完整：{raw}")


def _normalize_video_analysis_dict(data: dict) -> dict:
    """对模型输出做最小修复，避免因个别字段缺失而整条失败。"""
    normalized = dict(data)

    normalized["summary"] = str(normalized.get("summary") or "暂无摘要").strip()
    normalized["scene"] = str(normalized.get("scene") or "未识别场景").strip()

    for field_name in ("subjects", "actions", "tags"):
        value = normalized.get(field_name, [])
        if isinstance(value, list):
            normalized[field_name] = [str(item).strip() for item in value if str(item).strip()]
        elif value in (None, ""):
            normalized[field_name] = []
        else:
            normalized[field_name] = [str(value).strip()]

    if "has_motion" not in normalized or normalized["has_motion"] in (None, ""):
        normalized["has_motion"] = bool(normalized["actions"])
    else:
        normalized["has_motion"] = bool(normalized["has_motion"])

    return normalized


def _repair_common_json_issues(raw: str) -> str:
    """修补模型输出里最常见的 JSON 语法小错误。"""
    repaired = raw
    # 上一个值已结束，但下一个 key 直接开始，补上逗号。
    repaired = re.sub(r'(\]|"|true|false|null|\d)\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(\]|"|true|false|null|\d)\s+(")', r'\1, \2', repaired)
    return repaired


def _parse_video_analysis(raw: str) -> VideoAnalysis:
    """尽量从模型返回里解析出结构化结果。"""
    cleaned = _strip_code_block(raw)

    try:
        return VideoAnalysis.model_validate_json(cleaned)
    except Exception:
        pass

    json_text = _extract_json_object(cleaned)
    try:
        payload = json.loads(json_text)
    except Exception as exc:
        repaired_text = _repair_common_json_issues(json_text)
        try:
            payload = json.loads(repaired_text)
        except Exception:
            raise ArkAPIError(f"JSON 文本解析失败：{exc}；原始内容：{cleaned}") from exc

    try:
        normalized = _normalize_video_analysis_dict(payload)
        return VideoAnalysis.model_validate(normalized)
    except Exception as exc:
        raise ArkAPIError(f"结构化字段校验失败：{exc}；原始内容：{cleaned}") from exc


def _build_data_url(image_path: Path) -> str:
    """把本地图片压缩后编码成 data URL，降低大请求体断连概率。"""
    max_edge = _get_image_max_edge()
    quality = _get_image_quality()

    with Image.open(image_path) as source_image:
        image = source_image.convert("RGB")
        width, height = image.size
        longest_edge = max(width, height)
        if longest_edge > max_edge:
            scale = max_edge / float(longest_edge)
            resized_size = (
                max(1, int(width * scale)),
                max(1, int(height * scale)),
            )
            image = image.resize(resized_size, Image.Resampling.LANCZOS)

        buffer = BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=quality,
            optimize=True,
        )

    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def _build_transcript_hint(transcript_text: str | None) -> str:
    """把 transcript 文本整理成适合提示词的补充说明。"""
    if not transcript_text:
        return _USER_PROMPT

    compact = " ".join(transcript_text.split())
    if len(compact) > 400:
        compact = compact[:400] + "..."
    return f"{_USER_PROMPT}\n\n补充口播转写（可用于修正摘要、动作和标签）：\n{compact}"


def _build_request_body(keyframe_paths: list[Path], transcript_text: str | None = None) -> bytes:
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
    content.append({"type": "text", "text": _build_transcript_hint(transcript_text)})

    body = {
        "model": _ARK_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _build_responses_body(keyframe_paths: list[Path], transcript_text: str | None = None) -> bytes:
    """构造方舟 Responses API 请求体。"""
    content: list[dict[str, str]] = []
    for image_path in sorted(keyframe_paths):
        content.append(
            {
                "type": "input_image",
                "image_url": _build_data_url(image_path),
            }
        )
    content.append({"type": "input_text", "text": _build_transcript_hint(transcript_text)})

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
def analyze_video(keyframe_paths: list[Path], transcript_text: str | None = None) -> VideoAnalysis:
    """把 6 张关键帧发给豆包视觉模型，返回结构化分析。"""
    # 每次调用前重新加载 .env，避免切换模型后必须重启服务进程。
    load_dotenv(override=True)
    ark_api_key = os.environ.get("ARK_API_KEY")
    ark_base_url = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    ark_model = os.environ.get("ARK_MODEL", "")
    ark_api_style = os.environ.get("ARK_API_STYLE", "chat")

    if not ark_api_key:
        raise RuntimeError("缺少 ARK_API_KEY，请在 .env 中配置火山方舟 API Key")
    if not ark_model:
        raise RuntimeError("缺少 ARK_MODEL，请在 .env 中配置可用模型名或 Endpoint ID")

    if not keyframe_paths:
        raise ValueError("没有可分析的关键帧")

    global _ARK_MODEL
    _ARK_MODEL = ark_model

    if ark_api_style == "responses":
        endpoint = f"{ark_base_url.rstrip('/')}/responses"
        body = _build_responses_body(keyframe_paths, transcript_text)
    else:
        endpoint = f"{ark_base_url.rstrip('/')}/chat/completions"
        body = _build_request_body(keyframe_paths, transcript_text)
    timeout_seconds = _get_request_timeout_seconds()

    req = request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ark_api_key}",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise ArkAPIError(f"方舟 API 返回 HTTP {exc.code}：{body}") from exc
    except error.URLError as exc:
        raise ArkAPIError(_format_url_error_message(exc, endpoint, body)) from exc
    except TimeoutError as exc:
        raise ArkAPIError(
            f"视觉摘要请求超时：等待方舟接口响应超过限制（超时 {timeout_seconds}s）。请稍后重试。"
        ) from exc

    if ark_api_style == "responses":
        raw = _strip_code_block(_extract_responses_text(payload))
    else:
        raw = _strip_code_block(_extract_text(payload))

    try:
        return _parse_video_analysis(raw)
    except ArkAPIError as exc:
        print(f"\n  [调试] 豆包原始返回：{raw}")
        raise exc
