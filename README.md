# Reelsift

面向短视频创作者的本地素材粗筛工具：抽帧、AI 理解、标签摘要，帮助在剪辑前快速筛片。

上传一批拍摄素材后，Reelsift 会自动抽取关键帧，调用 Gemini 理解视频内容，并生成适合剪辑前浏览的摘要与标签，帮助用户更快完成“先筛片、再剪辑”这一步。

## 项目定位

Reelsift 当前是一个个人使用优先的本地工具，目标非常明确：

- 输入：一次拍摄产生的 30-50 条本地视频素材
- 处理：抽帧、AI 分析、清晰度评分、结果存储
- 输出：可在浏览器里快速筛选和挑选的素材列表

这个项目不做云端上传、不做登录、不做剪辑功能，只专注于“粗筛素材”。

## 项目状态

- Day 1 已完成：扫描视频、抽取 6 张关键帧
- Day 2 进行中：Gemini 分析已接入 CLI
- Day 3 之后未完成：SQLite、清晰度评分、Web 界面仍在开发

## 核心工作流

1. 用户把一批视频素材放进本地文件夹
2. 运行 CLI 扫描目录并抽取每条视频的 6 张关键帧
3. 调用 Gemini 生成摘要、场景、主体、动作、标签
4. 后续会把分析结果与评分写入 SQLite
5. 最终在 Web 页面中按标签和关键词筛选，并导出选中的原始视频

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

## 快速开始

处理一个本地视频目录：

```bash
python reelsift.py /path/to/my/videos
```

只处理前 3 个视频做联调：

```bash
python reelsift.py /path/to/my/videos --limit 3
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

## 目录结构

```text
reelsift/
├── reelsift.py            # CLI 入口
├── pipeline.py            # 扫描与抽帧流程
├── ai.py                  # Gemini 调用封装
├── metrics.py             # 清晰度 / 抖动评分（待实现）
├── db.py                  # SQLite 读写（待实现）
├── server.py              # FastAPI 服务（待实现）
├── templates/             # Jinja2 模板
├── static/                # 自定义样式
└── data/                  # 本地运行数据，不提交到 Git
```

## 尚未完成

以下能力还没有实现完成：

- OpenCV 清晰度 / 抖动评分
- SQLite 数据库存储
- FastAPI Web 界面
- 标签筛选、搜索、多选导出

`python server.py` 目前还只是占位文件，暂时不能作为可用入口。

## 团队协作说明

- 当前主分支为 `main`
- 本地运行数据不进入版本控制：`.env`、`data/`、`test-video/`
- 开发时优先按模块拆分：`pipeline.py`、`ai.py`、`db.py`、`server.py`
- 每次改动后至少跑一次 CLI 联调，避免只看代码不跑流程

## 下一步任务

- 完成 `metrics.py`，补上 OpenCV 清晰度 / 抖动评分
- 完成 `db.py`，建立 SQLite schema 与索引
- 把 CLI 分析结果写入数据库
- 实现 FastAPI + Jinja2 + HTMX 首页
- 支持标签筛选、关键词搜索、多选导出

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
