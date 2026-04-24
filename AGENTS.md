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
| AI 模型 | google-genai | 用 gemini-2.5-flash 模型 |
| 数据库 | sqlite3（Python 内置） | 不要引入 SQLAlchemy、不要换成 PostgreSQL |
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
├── ai.py                    # Gemini API 调用封装
├── metrics.py               # OpenCV 清晰度/抖动评分
├── db.py                    # SQLite 读写
├── server.py                # FastAPI 服务器
│
├── templates/
│   ├── base.html            # 基础布局
│   ├── index.html           # 主页（素材网格）
│   └── partials/            # HTMX 局部刷新用
│
├── static/
│   └── style.css            # 自定义样式（Tailwind 不够用时）
│
└── data/                    # 运行时数据（gitignore）
    ├── reelsift.db          # SQLite 数据库
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
- **网络错误（Gemini API）要重试**：指数退避，最多 3 次

### SQLite
- **不要每次查询都打开连接**。用一个全局连接或连接池
- **必须加索引**：`clips(filename)`, `clip_tags(tag)`, `clip_tags(clip_id)`
- **不要用 ORM**，写原生 SQL

### FastAPI
- **路由按功能分组**：`/clips/*`, `/tags/*`, `/export/*`
- **返回 HTML 片段（HTMX 风格）**，不是 JSON
- **错误用 HTTPException**，错误信息对用户友好

---

## 关键决策记录

### 为什么抽 6 张关键帧而不是更多
- 再多 Gemini 成本线性上升
- 6 张足够让 AI 理解一段 10-30 秒视频的内容
- UI 上也刚好能排成一行展示

### 为什么用 video_hash 做缓存目录
- MD5 前 12 位作为目录名
- 文件路径变了（重命名、移动）也不会重复抽帧
- 相同内容但不同路径会浪费——这个目前可以接受

### 为什么 Gemini 2.5 Flash
- 支持原生视频/图片输入
- 中文描述效果好
- 便宜：处理 6 张图 + 文字输出约 ¥0.01，可忽略
- 速度快：3-5 秒一次调用
- 视觉理解比 2.0 升级一代
- 2.0 Flash 将于 2026-06-01 下线，选稳定版避免再次迁移

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
- [~] Day 6: 多选导出、项目树、回收站、对比页已可用；仍在收尾交互
- [ ] Day 7: 真实素材测试 + bug 修复

**今天做到哪一天**：Day 6（项目树、回收站、对比页与选择模式收尾中）

### 当前实现方法

- 上传主流程：保存视频 → 抽 6 张关键帧 → 选封面 → 可选 ASR → 视觉摘要 → 入库 → 后台补全预览和对比分缓存
- 口播识别：上传时非阻塞；详情页可单独重新识别
- 素材库组织：左侧项目树 + 多层文件夹 + 未分类分组 + 项目级回收站
- 详情页播放：默认播放浏览器预览版，原视频入口保留
- 对比分：支持从详情页和素材库进入；当前已有综合推荐和一键保留
- 清晰度：不再在上传时评分，改为对比分维度之一

---

## 常用命令速查

```bash
# 安装依赖
pip install -r requirements.txt

# 处理一个文件夹的视频
python reelsift.py /path/to/my/videos

# 启动 Web 界面
python server.py
# 或
uvicorn server:app --reload --port 8000

# 清空缓存重新处理
rm -rf data/thumbnails data/reelsift.db
```

---

## 遇到问题时

**ffmpeg 找不到**：
- Mac：`brew install ffmpeg`
- Windows: WSL 里 `sudo apt install ffmpeg`

**Gemini API 401**：
- 检查 .env 里 GEMINI_API_KEY 是否正确
- 去 https://aistudio.google.com/ 重新生成

**处理速度太慢**：
- 检查是否并行处理（Day 2 之后应该并发调用 Gemini）
- 关键帧分辨率是否过高（应该 480p 够了）
