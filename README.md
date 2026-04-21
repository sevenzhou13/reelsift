# Reelsift

面向短视频创作者的本地素材粗筛工具：抽帧、AI 理解、标签摘要，帮助在剪辑前快速筛片。

上传一批拍摄素材后，Reelsift 会自动抽取关键帧，调用豆包视觉模型理解视频内容，并生成适合剪辑前浏览的摘要、场景、标签和清晰度结果，帮助用户更快完成“先筛片、再剪辑”这一步。

## 当前状态

项目已经能跑通一条完整链路：

- 扫描本地视频目录
- 抽取 6 张关键帧并缓存
- 调用豆包视觉生成摘要、场景、主体、动作、标签
- 计算清晰度分并生成封面图
- 将结果写入 SQLite
- 用 FastAPI + Jinja2 + HTMX 在浏览器中展示素材列表
- 支持关键词搜索和标签筛选

当前主模型配置：

- `ARK_API_STYLE=chat`
- `ARK_MODEL=doubao-1.5-vision-pro-250328`

## 已实现功能

### CLI 处理流程

- 递归扫描视频文件
- 抽取 6 张关键帧
- 调用豆包视觉分析视频内容
- 计算清晰度评分
- 将结果写入 `data/reelsift.db`
- 缓存关键帧和封面图到 `data/thumbnails/`

### 数据存储

- `clips` 表保存摘要、场景、动作、主体、清晰度、状态等信息
- `clip_tags` 表保存标签
- 已建立基础索引

### Web 页面

- 首页素材网格展示
- 封面、摘要、场景、标签、清晰度显示
- 关键词搜索
- 标签筛选
- HTMX 局部刷新

## 还没实现的功能

这是当前还没做完、或者还没进入主线的部分：

1. 多选素材
目前页面只能浏览和筛选，还不能勾选多条素材。

2. 导出选中视频到 `data/picks/`
数据库和页面已经准备到这一步，但导出动作还没接。

3. 更完整的筛选条件
现在只有关键词和标签，还没有：
- 按场景筛选
- 按清晰度排序/过滤
- 按是否有运动过滤

4. 相似素材 / 推荐排序
你之前提到的 embedding、相似素材对比、推荐评分还没做。

5. 重复素材或近重复素材聚类
这类高级筛片功能还没有实现。

6. 更稳的标签与摘要后处理
目前主模型已经可用，但某些局部镜头仍可能需要后处理规则进一步修正。

7. Web 端多路由功能拆分
目前页面已经能用，但服务端还没有完全按 `/clips/*`、`/tags/*`、`/export/*` 这种形态展开。

## 技术栈

- Python 3.11+（你当前本机虚拟环境暂时是 3.9，建议后续升级）
- ffmpeg-python
- opencv-python
- sqlite3
- FastAPI
- Jinja2
- HTMX
- Tailwind CSS（CDN）
- python-dotenv

## 安装

### 1. 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 安装 ffmpeg

```bash
brew install ffmpeg
```

安装完成后可验证：

```bash
ffmpeg -version
```

### 4. 配置环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

示例：

```env
ARK_API_KEY=your_ark_api_key_here
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_API_STYLE=chat
ARK_MODEL=doubao-1.5-vision-pro-250328
```

## 快速开始

### 处理一个视频目录

```bash
./.venv/bin/python reelsift.py /path/to/my/videos
```

只处理前 3 条视频做联调：

```bash
./.venv/bin/python reelsift.py /path/to/my/videos --limit 3
```

### 启动 Web 界面

```bash
source .venv/bin/activate
uvicorn server:app --reload --port 8000
```

浏览器打开：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

## 当前目录结构

```text
reelsift/
├── reelsift.py                    # CLI 入口
├── pipeline.py                    # 扫描与抽帧流程
├── ai.py                          # 豆包视觉调用封装
├── metrics.py                     # 清晰度评分
├── db.py                          # SQLite 写入
├── server.py                      # FastAPI 页面服务
├── templates/
│   ├── base.html
│   ├── index.html
│   └── partials/clip_grid.html
├── static/
│   └── style.css
└── data/
    ├── reelsift.db
    └── thumbnails/
```

## 常用排查

### ffmpeg 找不到

先确认：

```bash
ffmpeg -version
```

如果命令不存在，重新安装 ffmpeg。

### 方舟 API 401 / 403

检查：

- `.env` 是否存在
- `ARK_API_KEY` 是否填写正确
- `ARK_MODEL` 是否为可用模型名或可用 Endpoint ID
- 火山方舟控制台里是否已开通对应模型

### 页面能开但没有数据

检查：

- 是否已经先跑过 `reelsift.py`
- `data/reelsift.db` 是否存在
- `clips` 表中是否已有记录

## 协作说明

- 当前主分支为 `main`
- 不要提交 `.env`、`data/`、`test-video/`、`.venv/`
- 每个功能尽量单独开分支开发
- 提交前至少跑一次 CLI 或页面联调
