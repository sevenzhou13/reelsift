# AGENTS.md — Reelsift 项目工作手册

> 这份文档会被 Codex 每次启动时自动读取。它是你的"工作记忆"。
> 做任何决定前，先看这里。

---

## 项目一句话

本地运行的视频素材粗筛工具，个人使用。技术栈已锁定，不要提议替换方案。

详见同目录的 `PROJECT_BRIEF.md`。

---

## 技术栈（已锁定，禁止替换）

| 用途 | 技术 | 备注 |
|---|---|---|
| 语言 | Python 3.11+ | 不用 TypeScript、不用 Go |
| 视频处理 | ffmpeg-python | 不要换成 moviepy |
| 图像分析 | opencv-python | 算清晰度/抖动 |
| AI 模型 | 火山方舟兼容接口 | 通过 `.env` 配置 `ARK_*`，上传摘要可结合口播文本 |
| 数据库 | SQLAlchemy Core + `DATABASE_URL` | 默认 SQLite，准备上云时可切 PostgreSQL |
| Web 后端 | FastAPI | uvicorn 跑 |
| 模板 | Jinja2 | FastAPI 自带支持 |
| 前端交互 | HTMX | 不要用 React、Vue、Alpine |
| 前端样式 | Tailwind CSS（CDN 版本即可） | 不要配置 PostCSS |
| 配置 | python-dotenv | 读 .env 文件 |

**如果你觉得某个技术选择有问题，先告诉我原因，我同意后才能改。**

---

## 项目结构

```
reelsift/
├── PROJECT_BRIEF.md         # 项目总纲（只读，不要改）
├── AGENTS.md                # 本文件（工作手册）
├── README.md                # 使用说明
├── requirements.txt         # Python 依赖
├── .env.example             # 环境变量模板
├── .env                     # 实际环境变量（gitignore）
├── .gitignore
│
├── reelsift.py              # CLI 入口：python reelsift.py /path/to/videos
├── pipeline.py              # 处理流水线：扫描 → 抽帧 → AI 分析 → 存库
├── ai.py                    # 视觉摘要模型调用封装
├── metrics.py               # OpenCV 清晰度/抖动评分
├── db.py                    # SQLAlchemy Core 数据层
├── server.py                # FastAPI 服务器
│
├── templates/
│   ├── base.html            # 基础布局
│   ├── index.html           # 主页（素材网格）
│   ├── clip_detail.html     # 详情页
│   ├── clip_cut.html        # 粗剪页
│   └── partials/            # HTMX 局部刷新用
│
├── static/
│   └── style.css            # 自定义样式（Tailwind 不够用时）
│
└── data/                    # 运行时数据（gitignore）
    ├── reelsift.db          # SQLite 数据库
    ├── uploads/             # 上传原始视频
    ├── previews/            # 浏览器预览版视频
    ├── thumbnails/          # 抽帧缓存
    │   └── {video_hash}/
    │       ├── 0.jpg ~ 5.jpg  # 6 张关键帧
    │       └── cover.jpg      # 智能封面（第 3 张或清晰度最高的）
    └── picks/               # 导出选中视频的目标文件夹
```

---

## 编码规范

### 风格
- **注释用中文**。变量名、函数名用英文。
- **函数名动词开头**：`extract_keyframes`, `scan_folder`, `analyze_video`
- **类名用 PascalCase**：`Clip`, `Pipeline`
- **文件顶部写一句话说明**：该文件的职责

### 类型
- **所有函数参数和返回值加 type hint**
- **数据结构用 Pydantic 或 dataclass**，不要裸 dict 到处传
- **路径用 `pathlib.Path`**，不要用字符串拼路径

### 错误处理
- **清晰的错误信息**，告诉用户是哪个视频、哪一步出了问题
- **不要 silent fail**。处理失败的视频要记录下来，在界面上显示"处理失败"
- **网络错误（视觉摘要 API）要重试**：指数退避，最多 3 次

### 数据库
- 当前数据层是 **SQLAlchemy Core + DATABASE_URL**，不是 ORM 模型层
- 默认使用 `sqlite:///./data/reelsift.db`
- 准备上云时可通过 `DATABASE_URL` 切 PostgreSQL
- schema 变更要同时考虑 SQLite 和 PostgreSQL 可移植性
- 常用索引仍要保留：`clips(filename)`, `clip_tags(tag)`, `clip_tags(clip_id)`

### FastAPI
- **路由按功能分组**：`/clips/*`, `/tags/*`, `/export/*`
- **返回 HTML 片段（HTMX 风格）**，不是 JSON
- **错误用 HTTPException**，错误信息对用户友好

---

## 关键决策记录

### 为什么抽 6 张关键帧而不是更多
- 再多视觉模型成本线性上升
- 6 张足够让 AI 理解一段 10-30 秒视频的内容
- UI 上也刚好能排成一行展示

### 为什么用 video_hash 做缓存目录
- MD5 前 12 位作为目录名
- 文件路径变了（重命名、移动）也不会重复抽帧
- 相同内容但不同路径会浪费——这个目前可以接受

### 为什么视觉摘要可结合口播
- 画面摘要擅长识别场景、主体、动作、构图和可用性
- 口播文本能补充人物意图、观点、品牌词和无法从画面看出的信息
- 当前上传流程中 ASR 是非阻塞可选项，失败不影响抽帧、视觉摘要和入库
- 有 transcript 时，摘要提示词会把口播作为参考，而不是完全依赖口播

### 为什么引入 DATABASE_URL
- 本地单人使用仍默认 SQLite，启动和备份简单
- 登录、管理员后台和后续上云需要更清晰的数据访问边界
- `DATABASE_URL` 允许同一套数据层在 SQLite 和 PostgreSQL 间切换
- 当前使用 SQLAlchemy Core 做 SQL 可移植封装，不使用 ORM 模型层

### 为什么 HTMX 而不是 React
- 单人项目，不需要复杂状态管理
- 避免前端构建工具链（webpack/vite）的配置地狱
- HTMX 让 Python 后端直接驱动 UI 更新
- 原型设计的风格用 Tailwind 能还原 80%

### 为什么不用 Docker
- 本地工具，没必要额外一层抽象
- ffmpeg 在 Docker 里跑 GPU 加速会很麻烦
- 开发调试更直接

---

## 与 Codex 协作的规则

1. **先说计划**：新任务开始前，先用 3-5 条 bullet 告诉我你打算做什么，等我确认
2. **一次一事**：只做当前任务要求的，别顺手"优化"其他文件
3. **立即测试**：每个模块写完，给我一个可以直接运行的命令去测试
4. **提问优先**：任何设计决策不确定，停下来问我
5. **简洁至上**：能 50 行解决的不写 100 行。能用标准库不引入第三方
6. **不要道歉**：出错就直接说哪里错了怎么改，不用"抱歉让您困扰"之类的废话
7. **中文交流**：和我对话用中文，代码注释用中文

---

## 当前进度

- [x] Day 1: 视频扫描 + 关键帧抽取
- [x] Day 2: 视觉摘要与标签生成
- [x] Day 3: SQLite schema + 状态管理
- [x] Day 4: FastAPI 页面、上传页、详情页
- [x] Day 5: 素材网格、搜索、标签筛选
- [x] Day 6: 多选导出、项目树、回收站、对比页、收藏评分、粗剪页已可用
- [~] Day 7: 真实素材测试、上传稳定性、故事项目与导演 Agent 工作台持续打磨
- [ ] Day 8: 故事项目真实剪辑流测试、脚本-素材匹配质量优化、导出剪辑方案

**今天做到哪一天**：Day 7（真实素材测试中；故事项目、导演 Agent、脚本编辑与素材推荐工作台已进入可用原型）

### 当前实现方法

- 上传主流程：保存视频 → 抽 6 张关键帧 → 选封面 → 可选 ASR → 视觉摘要 → 入库 → 后台补全预览和对比分缓存
- 口播识别：上传时非阻塞；详情页可单独重新识别
- 素材库组织：左侧项目树 + 多层文件夹 + 未分类分组 + 项目级回收站
- 详情页播放：默认播放浏览器预览版，原视频入口保留
- 对比分：支持从详情页和素材库进入；当前已有综合推荐和一键保留
- 清晰度：不再在上传时评分，改为对比分维度之一
- 收藏与等级：素材卡片可收藏、设置 1-5 级评分，素材库页可筛选
- 搜索增强：关键词会匹配文件名、摘要、标签和人工备注
- 粗剪：详情页进入独立粗剪页，支持命名片段、预览、修边、删除确认和按名称导出
- 导出目录：素材批量导出和粗剪片段导出可手动输入路径，也可调用本地文件夹选择弹窗
- 文件夹弹窗：macOS 使用 `NSOpenPanel`，Windows 使用 `tkinter.filedialog.askdirectory()`；WSL、Docker、无 GUI 环境请手动输入路径
- 文件夹上传：支持上传整个文件夹，文件夹名会作为新素材库名称；上传进度可在首页持续查看
- 上传去重：同一素材库会按内容 hash 跳过重复视频，避免重复入库
- 素材记录：支持 MY NOTE 状态（待记录、已记录、无需记录），首页按状态和原文件修改时间辅助处理素材
- 标签治理：上传和历史标签会做激进归一化，减少重复、近义和低频噪声标签
- 故事项目：从素材库多选后会创建可回访的故事项目，首页展示当前素材库的故事项目入口
- 导演 Agent：故事项目支持 DeepSeek/`STORY_*` 配置优先的对话式 Agent，保留对话历史、流式输出和默认折叠的思考内容
- 脚本工作台：支持先生成叙事框架，确认后填充完整脚本；目标时长滑杆最长 20 分钟，调整后自动重生成脚本
- 脚本编辑：完整脚本可手动编辑；选中脚本局部并说明错误原因后，AI 只改写选中片段
- 素材匹配：完整脚本段落 hover 时显示对应素材缩略图，辅助把故事脚本落到素材排序

---

## 常用命令速查

```bash
# 安装依赖
pip install -r requirements.txt

# 处理一个文件夹的视频
python reelsift.py /path/to/my/videos

# 启动 Web 界面
./.venv/bin/python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000

# Windows PowerShell
.\.venv\Scripts\python.exe -m uvicorn server:app --reload --host 127.0.0.1 --port 8000

# 清空缓存重新处理
rm -rf data/thumbnails data/previews data/reelsift.db
```

---

## 遇到问题时

**ffmpeg 找不到**：
- Mac：`brew install ffmpeg`
- Windows: WSL 里 `sudo apt install ffmpeg`

**视觉摘要 API 认证失败**：
- 检查 `.env` 里的 `ARK_API_KEY`、`ARK_BASE_URL`、`ARK_MODEL` 是否正确
- 确认当前模型或 endpoint 已启用

**处理速度太慢**：
- 检查上传阶段抽帧是否正常走 ffmpeg
- 关键帧分辨率不要过高，当前流程会压缩后再送视觉模型

**导出文件夹弹窗不出现**：
- macOS 确认本机有 Swift 工具链，首次会编译一个本地选择器到 `data/.pick_folder_bin`
- Windows 确认服务运行在正常桌面会话里，不要在 WSL、Docker 或无 GUI 服务中期待系统弹窗
- 弹窗不可用时可以直接在导出目录输入框里手动填写路径
