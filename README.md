# Reelsift

本地运行的视频素材粗筛工具，服务于短视频创作者的剪辑前筛片流程。

## 项目状态

- Day 1 已完成：扫描视频、抽取 6 张关键帧
- Day 2 进行中：Gemini 分析已接入 CLI
- Day 3 之后未完成：SQLite、清晰度评分、Web 界面仍在开发

## 技术栈

- Python 3.11+
- ffmpeg-python
- google-genai（`gemini-2.5-flash`）
- sqlite3
- FastAPI + Jinja2 + HTMX

## 安装

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 安装 ffmpeg

```bash
brew install ffmpeg
```

安装完成后可验证：

```bash
ffmpeg -version
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env`，填入 Gemini API Key：

```bash
cp .env.example .env
```

`.env` 示例：

```env
GEMINI_API_KEY=your_key_here
```

## 当前可用功能

### CLI：扫描并处理视频

```bash
python reelsift.py /path/to/my/videos
```

只处理前几个视频做联调：

```bash
python reelsift.py /path/to/my/videos --limit 3
```

运行后会：

1. 递归扫描视频文件
2. 为每个视频抽取 6 张关键帧
3. 调用 Gemini 生成摘要、场景、主体、动作、标签
4. 将关键帧缓存到 `data/thumbnails/`

## 尚未完成

以下能力还没有实现完成：

- OpenCV 清晰度 / 抖动评分
- SQLite 数据库存储
- FastAPI Web 界面
- 标签筛选、搜索、多选导出

`python server.py` 目前还只是占位文件，暂时不能作为可用入口。

## 常用排查

### ffmpeg 找不到

先确认：

```bash
ffmpeg -version
```

如果命令不存在，重新安装 ffmpeg。

### Gemini API 401 / 403

检查：

- `.env` 是否存在
- `GEMINI_API_KEY` 是否填写正确
- API Key 是否在 Google AI Studio 可用

## 协作建议

- 不要提交 `.env`、`data/`、`test-video/`
- 每个功能单独开分支开发
- 提交前至少跑一次 CLI 联调
