# 云端语音识别：抽取音频并调用火山语音新版 submit/query 接口，返回带时间戳的口播分段

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

import ffmpeg
from dotenv import load_dotenv


load_dotenv(override=True)

ASR_SUBMIT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
ASR_QUERY_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
ASR_RESOURCE_ID = "volc.seedasr.auc"


@dataclass
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    segment_index: int


class ASRError(RuntimeError):
    """语音识别调用错误。"""


class ASREmptyResult(RuntimeError):
    """语音识别已完成，但没有识别到可用文本。"""


def is_asr_configured() -> bool:
    """判断是否已经配置火山语音识别新版 API Key。"""
    return bool(os.environ.get("VOLC_SPEECH_API_KEY"))


def extract_audio_for_asr(video_path: Path, output_path: Path) -> Path:
    """从视频中抽取单声道 16k wav，供 ASR 使用。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    (
        ffmpeg
        .input(str(video_path))
        .output(
            str(output_path),
            acodec="pcm_s16le",
            ac=1,
            ar=16000,
            vn=None,
        )
        .overwrite_output()
        .run(quiet=True)
    )
    return output_path


def _build_submit_headers(request_id: str) -> dict[str, str]:
    """构造提交任务请求头。"""
    api_key = os.environ.get("VOLC_SPEECH_API_KEY")
    if not api_key:
        raise ASRError("未配置 VOLC_SPEECH_API_KEY，无法执行口播识别。")

    return {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": os.environ.get("VOLC_SPEECH_RESOURCE_ID", ASR_RESOURCE_ID),
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }


def _build_query_headers(request_id: str) -> dict[str, str]:
    """构造查询结果请求头。"""
    api_key = os.environ.get("VOLC_SPEECH_API_KEY")
    if not api_key:
        raise ASRError("未配置 VOLC_SPEECH_API_KEY，无法查询口播识别结果。")

    return {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": os.environ.get("VOLC_SPEECH_RESOURCE_ID", ASR_RESOURCE_ID),
        "X-Api-Request-Id": request_id,
    }


def _request_json(url: str, payload: dict, headers: dict[str, str], timeout: int = 120) -> dict:
    """发送 JSON 请求并返回解析后的响应。"""
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise ASRError(f"ASR 返回 HTTP {exc.code}：{body}") from exc
    except Exception as exc:
        raise ASRError(f"ASR 请求失败：{exc}") from exc

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ASRError(f"ASR 返回了无法解析的 JSON：{raw}") from exc


def _build_submit_body(audio_path: Path) -> dict:
    """构造提交任务请求体。"""
    uid = os.environ.get("VOLC_SPEECH_UID", "reelsift")
    audio_base64 = base64.b64encode(audio_path.read_bytes()).decode("utf-8")
    return {
        "user": {
            "uid": uid,
        },
        "audio": {
            "data": audio_base64,
            "format": "wav",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": False,
            "enable_ddc": False,
            "enable_speaker_info": False,
            "enable_channel_split": False,
            "show_utterances": True,
            "vad_segment": True,
            "sensitive_words_filter": "",
        },
    }


def _extract_segments(payload: dict) -> list[TranscriptSegment]:
    """从 query 返回中提取 transcript 分段。"""
    utterances = payload.get("result", {}).get("utterances") or []
    segments: list[TranscriptSegment] = []
    for index, item in enumerate(utterances):
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start_ms=int(item.get("start_time", 0)),
                end_ms=int(item.get("end_time", 0)),
                text=text,
                segment_index=index,
            )
        )

    if segments:
        return segments

    text = str(payload.get("result", {}).get("text", "")).strip()
    duration = int(payload.get("audio_info", {}).get("duration", 0))
    if text:
        return [
            TranscriptSegment(
                start_ms=0,
                end_ms=duration,
                text=text,
                segment_index=0,
            )
        ]
    return []


def _is_query_finished_without_text(payload: dict) -> bool:
    """判断 query 是否已经完成，但结果为空。"""
    result = payload.get("result", {})
    audio_info = payload.get("audio_info", {})

    duration = int(audio_info.get("duration", 0) or 0)
    additions_duration = str(result.get("additions", {}).get("duration", "")).strip()
    text = str(result.get("text", "")).strip()
    utterances = result.get("utterances") or []

    if text or utterances:
        return False

    if duration > 0:
        return True
    if additions_duration.isdigit() and int(additions_duration) > 0:
        return True
    return False


def transcribe_video(video_path: Path, working_dir: Path) -> list[TranscriptSegment]:
    """转写单条视频，返回带时间戳的口播分段。"""
    if not is_asr_configured():
        return []

    audio_path = extract_audio_for_asr(video_path, working_dir / "audio_for_asr.wav")
    request_id = str(uuid.uuid4())

    _request_json(
        ASR_SUBMIT_ENDPOINT,
        _build_submit_body(audio_path),
        _build_submit_headers(request_id),
    )

    last_payload: dict | None = None
    for _ in range(20):
        payload = _request_json(
            ASR_QUERY_ENDPOINT,
            {},
            _build_query_headers(request_id),
            timeout=60,
        )
        last_payload = payload
        segments = _extract_segments(payload)
        if segments:
            return segments
        if _is_query_finished_without_text(payload):
            raise ASREmptyResult("ASR 已完成识别，但这条视频没有识别到可用口播内容。")
        time.sleep(1)

    if last_payload:
        raise ASRError(f"ASR 查询超时，最后一次返回：{json.dumps(last_payload, ensure_ascii=False)}")
    raise ASRError("ASR 查询超时，未收到任何结果。")
