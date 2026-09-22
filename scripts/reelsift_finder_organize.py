#!/usr/bin/env python3
# Finder 本地素材整理入口：分析视频后复制或原地重命名，不经过网页或数据库。
from __future__ import annotations

import csv
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from ai import VideoAnalysis, analyze_video
from db import ClipRecord, create_library, get_library_by_name, init_db, save_clip
from metrics import select_cover_frame
from pipeline import build_video_hash, extract_keyframes, get_keyframe_paths, scan_folder

CACHE_DIR = PROJECT_DIR / "data" / "thumbnails"
DB_PATH = PROJECT_DIR / "data" / "reelsift.db"
DIALOG_SOURCE = PROJECT_DIR / "scripts" / "reelsift_finder_dialog.swift"
DIALOG_BINARY = PROJECT_DIR / "data" / ".reelsift_finder_dialog"
LOG_PATH = PROJECT_DIR / "data" / "finder-organize.log"
MAX_NAME_LENGTH = 72


@dataclass
class OrganizeItem:
    """记录一条素材在整理流程中的分析与改名结果。"""

    source: Path
    analysis: VideoAnalysis | None = None
    target_name: str = ""
    target_path: Path | None = None
    status: str = "待处理"
    error: str = ""


def write_log(message: str) -> None:
    """记录 Finder 运行步骤，方便定位桌面环境中的错误。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as output:
        output.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def sanitize_name_part(value: str) -> str:
    """清理不能作为 macOS 文件名的字符，保留可读中文语义。"""
    cleaned = re.sub(r'[/:\\\\<>|?*"\x00-\x1f]', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:24]


def build_target_stem(index: int, analysis: VideoAnalysis) -> str:
    """优先采用视觉模型给出的具体命名描述，必要时才回退到结构化字段。"""
    rename_title = sanitize_name_part(analysis.rename_title or analysis.summary)
    if rename_title and rename_title not in {"未识别场景", "暂无摘要"}:
        return f"{index:03d}_{rename_title}"
    parts = [
        sanitize_name_part(analysis.scene),
        sanitize_name_part(analysis.subjects[0] if analysis.subjects else ""),
        sanitize_name_part(analysis.actions[0] if analysis.actions else ""),
    ]
    meaningful = [part for part in parts if part and part not in {"未识别场景", "暂无摘要"}]
    if not meaningful:
        meaningful = [sanitize_name_part(analysis.summary) or "未识别素材"]
    return f"{index:03d}_" + "_".join(meaningful)


def emit_progress(enabled: bool, stage: str, index: int, total: int, filename: str, detail: str = "") -> None:
    """以 JSON Lines 输出进度，供原生 Finder 服务窗口实时读取。"""
    if not enabled:
        return
    print(json.dumps({
        "stage": stage,
        "index": index,
        "total": total,
        "filename": filename,
        "detail": detail,
    }, ensure_ascii=False), flush=True)


def build_unique_name(stem: str, suffix: str, used_names: set[str]) -> str:
    """生成不与既有文件冲突的名字，大小写不敏感地判断冲突。"""
    safe_stem = stem[:MAX_NAME_LENGTH].rstrip(" ._") or "未识别素材"
    candidate = f"{safe_stem}{suffix}"
    number = 2
    while candidate.casefold() in used_names:
        trimmed = safe_stem[: MAX_NAME_LENGTH - len(str(number)) - 1].rstrip(" ._")
        candidate = f"{trimmed}-{number}{suffix}"
        number += 1
    used_names.add(candidate.casefold())
    return candidate


def build_output_folder(source_folder: Path) -> Path:
    """在原文件夹同级创建一个不会覆盖现有内容的整理目录。"""
    base = source_folder.parent / f"{source_folder.name}-Reelsift整理"
    candidate = base
    number = 2
    while candidate.exists():
        candidate = source_folder.parent / f"{base.name}-{number}"
        number += 1
    return candidate


def run_dialog(mode: str, *values: str) -> str:
    """调用原生 AppKit 弹窗，返回用户的选择。"""
    DIALOG_BINARY.parent.mkdir(parents=True, exist_ok=True)
    if not DIALOG_BINARY.exists() or DIALOG_SOURCE.stat().st_mtime > DIALOG_BINARY.stat().st_mtime:
        subprocess.run(
            ["swiftc", str(DIALOG_SOURCE), "-o", str(DIALOG_BINARY)],
            check=True,
            capture_output=True,
            text=True,
        )
    completed = subprocess.run(
        [str(DIALOG_BINARY), mode, *values],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def analyze_items(videos: list[Path], progress_enabled: bool = False) -> list[OrganizeItem]:
    """依次抽帧并调用视觉模型；失败条目保留在清单中但不会原地改名。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    items: list[OrganizeItem] = []
    total = len(videos)
    for index, video in enumerate(videos, 1):
        item = OrganizeItem(source=video)
        try:
            emit_progress(progress_enabled, "extracting", index, total, video.name, "正在抽取关键帧")
            video_hash = build_video_hash(video)
            _, frame_dir = extract_keyframes(video, CACHE_DIR, cache_key=video_hash)
            emit_progress(progress_enabled, "analyzing", index, total, video.name, "正在识别画面并生成命名")
            item.analysis = analyze_video(get_keyframe_paths(frame_dir))
            item.target_name = build_target_stem(index, item.analysis)
            item.status = "已分析"
            emit_progress(progress_enabled, "named", index, total, item.target_name, "已生成具体命名")
        except Exception as exc:
            item.status = "分析失败"
            item.error = str(exc)
        items.append(item)
    return items


def assign_target_names(items: list[OrganizeItem], existing_names: set[str]) -> None:
    """为分析成功的条目分配无冲突的完整文件名。"""
    used_names = {name.casefold() for name in existing_names}
    for item in items:
        if item.analysis is None:
            continue
        item.target_name = build_unique_name(item.target_name, item.source.suffix.lower(), used_names)


def copy_items(items: list[OrganizeItem], output_folder: Path) -> None:
    """把成功项复制到新目录；分析失败项保留原名复制，避免遗漏素材。"""
    output_folder.mkdir(parents=True, exist_ok=False)
    assign_target_names(items, set())
    used_names: set[str] = set()
    for index, item in enumerate(items, 1):
        if not item.target_name:
            item.target_name = build_unique_name(f"{index:03d}_{item.source.stem}", item.source.suffix.lower(), used_names)
        item.target_path = output_folder / item.target_name
        try:
            shutil.copy2(item.source, item.target_path)
            item.status = "已复制" if item.analysis else "已复制（未分析）"
        except Exception as exc:
            item.status = "复制失败"
            item.error = str(exc)


def rename_items(items: list[OrganizeItem], source_folder: Path) -> None:
    """对成功项两阶段改名，避免 A/B 文件名互换时发生碰撞。"""
    successful = [item for item in items if item.analysis is not None]
    selected_names = {item.source.name.casefold() for item in successful}
    existing_names = {path.name for path in source_folder.iterdir() if path.name.casefold() not in selected_names}
    assign_target_names(successful, existing_names)

    temporary: list[tuple[OrganizeItem, Path]] = []
    for item in successful:
        temporary_path = item.source.with_name(f".reelsift-renaming-{uuid.uuid4().hex}{item.source.suffix}")
        try:
            item.source.rename(temporary_path)
            temporary.append((item, temporary_path))
        except Exception as exc:
            item.status = "重命名失败"
            item.error = str(exc)

    for item, temporary_path in temporary:
        item.target_path = source_folder / item.target_name
        try:
            temporary_path.rename(item.target_path)
            item.status = "已重命名"
        except Exception as exc:
            item.status = "重命名失败"
            item.error = str(exc)
            try:
                temporary_path.rename(item.source)
            except Exception:
                item.error += "；恢复原文件名也失败，请检查以 .reelsift-renaming- 开头的文件"


def write_manifest(items: list[OrganizeItem], manifest_path: Path) -> None:
    """输出兼容表格软件的 CSV 清单，记录每条素材的处理结果。"""
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=["原始路径", "整理后路径", "原文件名", "新文件名", "AI命名描述", "AI摘要", "场景", "主体", "动作", "状态", "错误"],
        )
        writer.writeheader()
        for item in items:
            analysis = item.analysis
            writer.writerow({
                "原始路径": str(item.source),
                "整理后路径": str(item.target_path or ""),
                "原文件名": item.source.name,
                "新文件名": item.target_name,
                "AI命名描述": analysis.rename_title if analysis else "",
                "AI摘要": analysis.summary if analysis else "",
                "场景": analysis.scene if analysis else "",
                "主体": "、".join(analysis.subjects) if analysis else "",
                "动作": "、".join(analysis.actions) if analysis else "",
                "状态": item.status,
                "错误": item.error,
            })


def index_organized_items(items: list[OrganizeItem], output_folder: Path, progress_enabled: bool) -> None:
    """把整理后的本地文件及本次识别结果直接写入导演台索引。"""
    init_db(DB_PATH)
    library = get_library_by_name(output_folder.name, DB_PATH)
    if library is None:
        library = create_library(output_folder.name, DB_PATH)
    total = len(items)
    for index, item in enumerate(items, 1):
        if item.target_path is None or not item.target_path.exists() or item.analysis is None:
            continue
        try:
            video_hash = build_video_hash(item.target_path)
            _, frame_dir = extract_keyframes(item.target_path, CACHE_DIR, cache_key=video_hash)
            frame_paths = get_keyframe_paths(frame_dir)
            cover_path = select_cover_frame(frame_paths, frame_dir / "cover.jpg") if frame_paths else None
            stat = item.target_path.stat()
            save_clip(
                ClipRecord(
                    video_hash=video_hash,
                    filename=item.target_path.name,
                    filepath=item.target_path,
                    library_id=library.id,
                    summary=item.analysis.summary,
                    scene=item.analysis.scene,
                    subjects=item.analysis.subjects,
                    actions=item.analysis.actions,
                    tags=item.analysis.tags,
                    has_motion=item.analysis.has_motion,
                    cover_path=cover_path,
                    status="done",
                    source_modified_at=stat.st_mtime,
                ),
                DB_PATH,
            )
            emit_progress(progress_enabled, "indexing", index, total, item.target_path.name, "正在写入导演台索引")
        except Exception as exc:
            item.error = f"{item.error}；索引失败：{exc}".strip("；")
            write_log(f"索引失败：{item.target_path}：{exc}")


def restore_manifest_index(output_folder: Path) -> int:
    """从已有整理清单恢复本地导演台索引，不再次调用视觉模型。"""
    manifest_path = output_folder / "Reelsift整理清单.csv"
    if not manifest_path.exists():
        raise ValueError(f"找不到整理清单：{manifest_path}")
    init_db(DB_PATH)
    library = get_library_by_name(output_folder.name, DB_PATH) or create_library(output_folder.name, DB_PATH)
    restored_count = 0
    with manifest_path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            video_path = Path(str(row.get("整理后路径") or ""))
            if not video_path.exists():
                continue
            video_hash = build_video_hash(video_path)
            _, frame_dir = extract_keyframes(video_path, CACHE_DIR, cache_key=video_hash)
            frame_paths = get_keyframe_paths(frame_dir)
            cover_path = select_cover_frame(frame_paths, frame_dir / "cover.jpg") if frame_paths else None
            stat = video_path.stat()
            split_values = lambda key: [item.strip() for item in str(row.get(key) or "").split("、") if item.strip()]
            save_clip(
                ClipRecord(
                    video_hash=video_hash,
                    filename=video_path.name,
                    filepath=video_path,
                    library_id=library.id,
                    summary=str(row.get("AI摘要") or "暂无摘要"),
                    scene=str(row.get("场景") or "未识别场景"),
                    subjects=split_values("主体"),
                    actions=split_values("动作"),
                    tags=split_values("主体") + split_values("动作") + [str(row.get("场景") or "").strip()],
                    cover_path=cover_path,
                    status="done",
                    source_modified_at=stat.st_mtime,
                ),
                DB_PATH,
            )
            restored_count += 1
    write_log(f"已从整理清单恢复 {restored_count} 条导演台索引：{output_folder}")
    return restored_count


def main() -> None:
    """处理 Finder 传入的一个文件夹。"""
    parser = argparse.ArgumentParser(description="Reelsift Finder 本地素材整理")
    parser.add_argument("folder", type=Path, help="要整理的素材文件夹")
    parser.add_argument("--mode", choices=("copy", "rename"), help="由原生 Finder 服务传入的整理模式")
    parser.add_argument("--progress-json", action="store_true", help="向原生 Finder 服务输出实时进度")
    parser.add_argument("--restore-manifest", action="store_true", help="从已有整理清单恢复导演台索引")
    args = parser.parse_args()
    source_folder = args.folder.expanduser().resolve()
    if not source_folder.is_dir():
        raise SystemExit(f"所选路径不是文件夹：{source_folder}")
    if args.restore_manifest:
        restored_count = restore_manifest_index(source_folder)
        print(f"已恢复 {restored_count} 条素材索引")
        return

    videos = scan_folder(source_folder)
    write_log(f"收到 Finder 文件夹：{source_folder}，找到 {len(videos)} 个视频")
    if not videos:
        run_dialog("message", "没有找到视频", "该文件夹及其子文件夹中没有 MP4、MOV、MKV 或 WebM 视频。")
        return

    choice = args.mode or run_dialog("choose", source_folder.name, str(len(videos)))
    if choice == "cancel":
        return
    if choice == "rename" and args.mode is None and run_dialog("confirm_rename", source_folder.name) != "rename":
        return

    emit_progress(args.progress_json, "started", 0, len(videos), source_folder.name, "正在准备素材")
    items = analyze_items(videos, progress_enabled=args.progress_json)
    emit_progress(args.progress_json, "organizing", len(videos), len(videos), source_folder.name, "正在整理文件")
    if choice == "copy":
        output_folder = build_output_folder(source_folder)
        copy_items(items, output_folder)
        manifest_path = output_folder / "Reelsift整理清单.csv"
    else:
        rename_items(items, source_folder)
        output_folder = source_folder
        manifest_path = source_folder / "Reelsift重命名清单.csv"
    index_organized_items(items, output_folder, args.progress_json)
    write_manifest(items, manifest_path)
    success_count = sum(item.status in {"已复制", "已重命名", "已复制（未分析）"} for item in items)
    write_log(f"整理完成：{success_count}/{len(items)}，结果目录：{output_folder}")
    emit_progress(args.progress_json, "completed", success_count, len(items), str(output_folder), "整理完成")
    if args.mode is None:
        run_dialog("finish", str(output_folder), str(success_count), str(len(items)))
    elif not args.progress_json:
        subprocess.Popen(["open", str(output_folder)], start_new_session=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write_log(f"失败：{exc}")
        print(f"Reelsift Finder 整理失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
