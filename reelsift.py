# CLI 入口：python reelsift.py /path/to/videos [--limit N]

import argparse
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from pipeline import build_video_hash, get_keyframe_paths, scan_folder, extract_keyframes
from ai import analyze_video, VideoAnalysis
from db import ClipRecord, init_db, save_clip
from metrics import score_keyframes

console = Console()

CACHE_DIR = Path(__file__).parent / "data" / "thumbnails"
DB_PATH = Path(__file__).parent / "data" / "reelsift.db"


def print_analysis(name: str, index: int, total: int, analysis: VideoAnalysis) -> None:
    """格式化打印单个视频的 AI 分析结果。"""
    console.print(f"\n[bold][{index}/{total}] {name}[/bold]")
    console.print(f"  摘要：{analysis.summary}")
    console.print(f"  场景：{analysis.scene}")
    console.print(f"  主体：{'、'.join(analysis.subjects) if analysis.subjects else '—'}")
    console.print(f"  动作：{'、'.join(analysis.actions) if analysis.actions else '—'}")
    console.print(f"  标签：{'、'.join(analysis.tags)}")
    console.print(f"  有运动：{'是' if analysis.has_motion else '否'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reelsift — 视频素材粗筛工具")
    parser.add_argument("folder", type=Path, help="要扫描的视频文件夹路径")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个视频（用于测试）")
    args = parser.parse_args()

    folder: Path = args.folder.expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        console.print(f"[red]错误：路径不存在或不是文件夹：{folder}[/red]")
        raise SystemExit(1)

    console.print("\n[bold cyan]🎬 Reelsift[/bold cyan]")
    console.print(f"扫描文件夹：{folder}")
    init_db(DB_PATH)

    all_videos = scan_folder(folder)
    if not all_videos:
        console.print("[yellow]未找到任何视频文件。[/yellow]")
        return

    videos = all_videos[:args.limit] if args.limit else all_videos
    if args.limit and args.limit < len(all_videos):
        console.print(f"找到 [bold]{len(all_videos)}[/bold] 个视频文件，limit={args.limit}，实际处理 {len(videos)} 个")
    else:
        console.print(f"找到 [bold]{len(videos)}[/bold] 个视频文件")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    frame_failures: list[tuple[Path, str]] = []
    ai_failures: list[tuple[Path, str]] = []
    metric_failures: list[tuple[Path, str]] = []
    ai_calls = 0
    saved_count = 0
    start = time.time()

    for i, video in enumerate(videos, 1):
        console.print(f"\n[bold][{i}/{len(videos)}] {video.name}[/bold]")
        video_hash = build_video_hash(video)

        # 抽帧
        try:
            video_hash, frame_dir = extract_keyframes(video, CACHE_DIR)
            frames = get_keyframe_paths(frame_dir)
            console.print(f"  抽帧：{len(frames)} 张关键帧已保存")
        except Exception as e:
            console.print(f"  [red]❌ 抽帧失败：{e}[/red]")
            frame_failures.append((video, str(e)))
            save_clip(
                ClipRecord(
                    video_hash=video_hash,
                    filename=video.name,
                    filepath=video,
                    status="failed",
                    error_message=f"抽帧失败：{e}",
                ),
                DB_PATH,
            )
            continue

        # 清晰度评分
        try:
            metrics = score_keyframes(frames, frame_dir / "cover.jpg")
            console.print(f"  清晰度：{metrics.sharpness_score:.1f}")
        except Exception as e:
            console.print(f"  [red]❌ 清晰度评分失败：{e}[/red]")
            metric_failures.append((video, str(e)))
            save_clip(
                ClipRecord(
                    video_hash=video_hash,
                    filename=video.name,
                    filepath=video,
                    status="failed",
                    error_message=f"清晰度评分失败：{e}",
                ),
                DB_PATH,
            )
            continue

        # AI 分析
        console.print("  分析中... (Doubao)")
        try:
            analysis = analyze_video(frames)
            ai_calls += 1
            print_analysis(video.name, i, len(videos), analysis)
            save_clip(
                ClipRecord(
                    video_hash=video_hash,
                    filename=video.name,
                    filepath=video,
                    summary=analysis.summary,
                    scene=analysis.scene,
                    subjects=analysis.subjects,
                    actions=analysis.actions,
                    tags=analysis.tags,
                    has_motion=analysis.has_motion,
                    sharpness_score=metrics.sharpness_score,
                    cover_path=metrics.cover_path,
                    status="done",
                ),
                DB_PATH,
            )
            saved_count += 1
        except Exception as e:
            console.print(f"  [red]❌ AI 分析失败：{e}[/red]")
            ai_failures.append((video, str(e)))
            save_clip(
                ClipRecord(
                    video_hash=video_hash,
                    filename=video.name,
                    filepath=video,
                    sharpness_score=metrics.sharpness_score,
                    cover_path=metrics.cover_path,
                    status="failed",
                    error_message=f"AI 分析失败：{e}",
                ),
                DB_PATH,
            )

    elapsed = time.time() - start
    all_failures = frame_failures + metric_failures + ai_failures

    console.print()
    console.print("[bold green]✅ 处理完成[/bold green]")

    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_row("成功", f"[green]{len(videos) - len(all_failures)}[/green]")
    summary.add_row("失败", f"[red]{len(all_failures)}[/red]" if all_failures else "0")
    summary.add_row("AI 调用", f"{ai_calls} 次")
    summary.add_row("入库", f"{saved_count} 条")
    summary.add_row("耗时", f"{elapsed:.1f}s")
    console.print(summary)

    if all_failures:
        console.print("\n[red]失败文件：[/red]")
        for path, reason in all_failures:
            console.print(f"  • {path.name}：{reason}")

    console.print(f"\n关键帧保存到：{CACHE_DIR}")
    console.print(f"数据库保存到：{DB_PATH}\n")


if __name__ == "__main__":
    main()
