# 模型横向测试：对同一条视频比较多个方舟视觉模型的效果与耗时

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

from dotenv import load_dotenv

from pipeline import extract_keyframes, get_keyframe_paths


load_dotenv(override=True)

BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "data" / "thumbnails"

DEFAULT_MODELS = [
    "doubao-1-5-vision-pro-32k-250115",
    "doubao-1-5-vision-lite-250315",
    "doubao-seed-1-6-vision-250815",
    "doubao-seed-1-6-flash-250828",
    "doubao-seed-1-6-251015",
    "doubao-seed-2-0-lite-260215",
]

SYSTEM_PROMPT = """你是一个视频素材分析助手。
我会给你同一个视频中均匀抽取的 6 张关键帧。
请输出严格 JSON，字段如下：
- summary: 一句话摘要，尽量 10-18 个字
- scene: 场景名词
- tags: 3-5 个检索标签数组
- has_motion: 布尔

不要输出 markdown，不要解释。"""

USER_PROMPT = "请分析这 6 张关键帧，它们来自同一段视频，按时间顺序排列。"


@dataclass
class ModelTestResult:
    model: str
    ok: bool
    elapsed_seconds: float
    summary: str | None = None
    scene: str | None = None
    tags: list[str] | None = None
    raw_text: str | None = None
    error_message: str | None = None


def build_data_url(image_path: Path) -> str:
    """把本地关键帧编码为 data URL。"""
    encoded = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def strip_code_block(text: str) -> str:
    """兜底去掉 markdown 代码块包裹。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
    return cleaned.strip()


def extract_response_text(payload: dict) -> str:
    """从 chat/completions 返回里提取文本。"""
    content = payload["choices"][0]["message"]["content"]
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        text_parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "".join(text_parts).strip()

    raise ValueError(f"无法识别的响应内容：{content}")


def build_request_body(model: str, keyframe_paths: list[Path]) -> bytes:
    """构造方舟 chat/completions 请求体。"""
    content: list[dict[str, object]] = []
    for image_path in sorted(keyframe_paths):
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": build_data_url(image_path),
                },
            }
        )
    content.append({"type": "text", "text": USER_PROMPT})

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def test_model(
    *,
    api_key: str,
    base_url: str,
    model: str,
    keyframe_paths: list[Path],
) -> ModelTestResult:
    """测试单个模型。"""
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    body = build_request_body(model, keyframe_paths)
    req = request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    started_at = time.perf_counter()
    try:
        with request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        elapsed = time.perf_counter() - started_at
        raw_text = strip_code_block(extract_response_text(payload))
        try:
            parsed = json.loads(raw_text)
            return ModelTestResult(
                model=model,
                ok=True,
                elapsed_seconds=elapsed,
                summary=parsed.get("summary"),
                scene=parsed.get("scene"),
                tags=parsed.get("tags") or [],
                raw_text=raw_text,
            )
        except Exception:
            return ModelTestResult(
                model=model,
                ok=False,
                elapsed_seconds=elapsed,
                raw_text=raw_text,
                error_message="模型返回了文本，但不是合法 JSON",
            )
    except error.HTTPError as exc:
        elapsed = time.perf_counter() - started_at
        body_text = exc.read().decode("utf-8", errors="ignore")
        return ModelTestResult(
            model=model,
            ok=False,
            elapsed_seconds=elapsed,
            error_message=f"HTTP {exc.code}: {body_text}",
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started_at
        return ModelTestResult(
            model=model,
            ok=False,
            elapsed_seconds=elapsed,
            error_message=str(exc),
        )


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="对比多个火山视觉模型的效果")
    parser.add_argument("video", type=Path, help="要测试的视频路径")
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        help="要测试的模型列表，默认使用内置候选模型",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        help="方舟 API Base URL，默认取 .env 里的 ARK_BASE_URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ARK_API_KEY", ""),
        help="方舟 API Key，默认取 .env 里的 ARK_API_KEY",
    )
    return parser.parse_args()


def main() -> None:
    """执行模型对比测试。"""
    args = parse_args()
    if not args.api_key:
        raise RuntimeError("缺少 ARK_API_KEY，请先在 .env 中配置")
    if not args.video.exists():
        raise FileNotFoundError(f"视频不存在：{args.video}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _, frame_dir = extract_keyframes(args.video, CACHE_DIR)
    keyframe_paths = get_keyframe_paths(frame_dir)
    if not keyframe_paths:
        raise RuntimeError("抽帧失败，没有可测试的关键帧")

    print(f"测试视频：{args.video}")
    print(f"关键帧目录：{frame_dir}")
    print(f"测试模型数：{len(args.models)}")
    print("-" * 80)

    results: list[ModelTestResult] = []
    for model in args.models:
        print(f"[测试中] {model}")
        result = test_model(
            api_key=args.api_key,
            base_url=args.base_url,
            model=model,
            keyframe_paths=keyframe_paths,
        )
        results.append(result)
        if result.ok:
            print(f"  成功，耗时 {result.elapsed_seconds:.2f}s")
            print(f"  摘要：{result.summary}")
            print(f"  场景：{result.scene}")
            print(f"  标签：{', '.join(result.tags or [])}")
        else:
            print(f"  失败，耗时 {result.elapsed_seconds:.2f}s")
            print(f"  错误：{result.error_message}")
            if result.raw_text:
                print(f"  原始返回：{result.raw_text}")
        print("-" * 80)

    print("测试汇总：")
    for result in results:
        status = "OK" if result.ok else "FAIL"
        summary = result.summary or result.error_message or "-"
        print(f"- {status:4} | {result.elapsed_seconds:6.2f}s | {result.model} | {summary}")


if __name__ == "__main__":
    main()
