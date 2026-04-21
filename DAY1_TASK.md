# Day 1 任务：视频扫描与关键帧抽取

> 给 Claude Code 的今日作业。完成一个功能就划掉一个。

---

## 今日目标

**能用一个命令扫描某个文件夹里所有视频，为每个视频抽出 6 张关键帧。**

当我运行 `python reelsift.py ~/test-videos` 时：

1. 扫描该文件夹下所有视频文件（mp4, mov, MP4, MOV, mkv, webm）
2. 为每个视频抽 6 张均匀分布的关键帧，保存到 `data/thumbnails/{hash}/` 下
3. 终端打印清晰的进度："[3/15] 正在处理 IMG_2847.mp4..."
4. 已处理过的视频跳过（通过文件路径的 MD5 判断）
5. 完成后打印总结："✅ 处理完成 15 个视频，耗时 42 秒"

**这一天不做**：
- 不调用 Gemini（那是 Day 2）
- 不存 SQLite（那是 Day 3）
- 不启动 Web 服务器（那是 Day 4）
- 不做任何界面

---

## 任务拆解

### 1. 初始化项目骨架
- 创建所有文件和目录（见 CLAUDE.md 的"项目结构"）
- `requirements.txt` 只写 Day 1 需要的：`ffmpeg-python`, `python-dotenv`
- `.gitignore` 要忽略 `.env`, `data/`, `__pycache__/`, `*.pyc`
- `README.md` 写最小的 "如何运行"
- 初始化 git 仓库

### 2. 实现 `pipeline.py`

要求实现两个函数：

**`scan_folder(folder: Path) -> list[Path]`**
- 递归扫描文件夹，找所有视频文件
- 支持的扩展名：`.mp4`, `.mov`, `.mkv`, `.webm`（大小写不敏感）
- 按文件名排序返回

**`extract_keyframes(video_path: Path, cache_dir: Path, count: int = 6) -> tuple[str, Path]`**
- 计算 `video_hash`：video_path 字符串的 MD5 前 12 位
- 输出目录：`cache_dir / video_hash`
- 已存在且有 count 张 jpg 的话直接返回（跳过）
- 用 `ffmpeg.probe` 获取视频时长
- 按时长均匀分布抽 count 张（避开首尾，比如 6 张就在 10%, 25%, 40%, 55%, 70%, 85% 这些位置）
- 每张宽度 480px（`scale=480:-1`），保持宽高比
- JPEG 质量 q:v=3（高质量但文件不大）
- 文件名 `0.jpg` 到 `{count-1}.jpg`
- 返回 `(video_hash, 帧目录)`

### 3. 实现 `reelsift.py`（入口）

```python
# 伪代码，具体实现你来
import sys
from pathlib import Path
from pipeline import scan_folder, extract_keyframes

def main():
    # 从命令行拿文件夹路径
    # 校验路径存在
    # 扫描所有视频
    # 循环处理，带进度显示
    # 最后打印总结
    pass

if __name__ == "__main__":
    main()
```

要求：
- 用标准库的 `argparse` 处理参数
- 进度用 `rich` 库的 progress bar（加到 requirements.txt）
- 错误清晰：某个视频处理失败不能让整个程序崩，记录下来继续处理下一个
- 最后打印表格：成功数、失败数、总耗时、失败的文件列表

---

## 验收标准

你写完后，我要能做这件事：

```bash
cd ~/reelsift
python reelsift.py ~/test-videos
```

然后看到类似输出：

```
🎬 Reelsift
扫描文件夹：/Users/me/test-videos
找到 15 个视频文件

处理中 ━━━━━━━━━━━━━━━━━━━━ 100% 15/15

✅ 处理完成
成功：15  失败：0
耗时：42.3s

关键帧保存到：data/thumbnails/
```

然后我去看 `data/thumbnails/` 下应该有 15 个文件夹，每个文件夹里有 6 张 jpg。

---

## 关键帧质量测试（Day 1 结束前必做）

找一个你觉得"有代表性"的视频（比如有开头空镜、中间主体、结尾环境变化的），跑完之后打开 `data/thumbnails/{hash}/` 看 6 张图：

- 这 6 张能不能让我一眼看出视频拍了什么？
- 有没有全是一样的画面（说明抽帧位置需要调整）？
- 有没有黑屏/转场的废帧？

**如果这 6 张图不够代表视频内容，说明抽帧策略要优化**——可能要加"场景变化检测"，但这是 Day 1 之后的事，今天先跑通。

---

## 今日结束前

更新 `CLAUDE.md` 的进度清单，把 Day 1 打勾。

告诉我：
1. 跑通了没有
2. 用多少视频测试的
3. 遇到什么问题
4. 关键帧质量怎么样

然后等我的下一个指令（应该是 Day 2：Gemini 集成）。
