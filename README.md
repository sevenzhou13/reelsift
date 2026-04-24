# Reelsift

面向短视频创作者的本地素材粗筛工具。上传一批拍摄素材后，系统会自动抽帧、生成视觉摘要、可选识别口播，并在浏览器里完成浏览、筛选、对比、导出和回收站管理。

## 当前状态

项目主链路已经可用，当前重点在 Day 6 收尾和 Day 7 回归测试：

- 上传视频到素材库
- 抽取 6 张关键帧并缓存
- 生成视觉摘要、标签、结构化分析明细
- 可选口播识别，失败不阻塞主流程
- SQLite 存储素材、标签、转写、文件夹树、回收站状态
- 素材库页支持项目树、多层文件夹、选择模式、导出、删除、对比
- 详情页支持摘要编辑、标签追加、MY NOTE、口播重识别、前后切换、删除
- 相似素材对比页支持推荐最佳项和一键保留
- 项目级回收站支持恢复 7 天内删除的素材

## 当前采用的方法

### 上传主流程

1. 保存上传视频到 `data/uploads/`
2. 抽取 6 张关键帧到 `data/thumbnails/{video_hash}/`
3. 选择封面图
4. 可选执行 ASR，失败不阻塞主流程
5. 调用视觉模型生成摘要、标签和结构化描述
6. 写入 SQLite
7. 启动后台补全任务，生成浏览器预览和对比分缓存

### 视觉分析方法

- 当前实现使用火山方舟兼容接口
- 输入是 6 张关键帧，加上可选 transcript 提示
- 发送前会压缩关键帧，减小请求体，降低 `Broken pipe`
- 网络异常会自动重试，并转换成更易理解的中文错误

### 口播识别方法

- 上传时是“非阻塞可选项”
- 详情页可以单独重新触发一次口播识别
- 区分 `done / empty / failed / unavailable`

### 预览与对比分

- 原始素材保持不变
- 浏览器播放的是后台补全生成的 `mp4` 预览版
- 对比分会写入缓存，详情页和对比页优先读缓存结果
- 当前推荐逻辑已经包含清晰度、稳定度、信息量、构图完整度、口播可用性等维度

## 进度概览

- [x] Day 1: 视频扫描 + 关键帧抽取
- [x] Day 2: 视觉摘要与标签生成
- [x] Day 3: SQLite schema + 状态管理
- [x] Day 4: FastAPI 页面、上传页、详情页
- [x] Day 5: 素材网格、搜索、标签筛选
- [~] Day 6: 多选导出、对比页、项目树、回收站已可用，仍在收尾交互
- [ ] Day 7: 真实素材回归测试 + 性能与稳定性收尾

## 同伴快速上手

### 1. 克隆项目

```bash
git clone https://gitee.com/seven-circles/reelsift.git
cd reelsift
```

### 2. 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 安装 ffmpeg

```bash
brew install ffmpeg
ffmpeg -version
```

### 5. 配置环境变量

```bash
cp .env.example .env
```

`.env` 至少需要填这些：

```env
ARK_API_KEY=your_ark_api_key_here
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_API_STYLE=chat
ARK_MODEL=your_ark_endpoint_id_or_model
ARK_TIMEOUT_SECONDS=90
ARK_IMAGE_MAX_EDGE=960
ARK_IMAGE_QUALITY=78
VOLC_SPEECH_API_KEY=your_volc_speech_api_key_here
```

### 6. 启动服务

优先使用项目虚拟环境启动，不要直接用系统环境里的 `uvicorn`：

```bash
./.venv/bin/python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

浏览器打开：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

### 7. 第一次使用建议

1. 先创建一个素材库，例如 `默认素材库`
2. 上传几条测试视频
3. 等上传完成后进入素材库
4. 在左侧项目树里新建子文件夹，例如 `美食`、`景点`
5. 用“选择”模式试一次导出、删除、对比
6. 打开任意详情页，试摘要编辑、MY NOTE、口播识别

## 日常操作说明

### 素材库页

- 顶部 `上传视频`：进入上传页
- 顶部 `选择`：进入选择模式，统一执行导出、对比、删除
- 左侧项目树：
  - 根层显示“全部素材库”
  - 第二层显示各素材库
  - 素材库下支持多层文件夹
  - `未分类` 会显示，但不可拖入
  - 文件夹支持拖拽调整层级
  - 右键文件夹可新建子文件夹、重命名、删除
- 卡片点击：进入详情页
- 卡片右键：移动到某个文件夹或删除素材

### 选择模式

- `全选`：选中当前列表
- `清空`：取消当前选择
- `导出选中`：复制原始视频到导出目录
- `对比已选`：最多选 6 条进入对比页
- `删除选中`：把素材移入项目级回收站

### 详情页

- 摘要旁 `edit`：直接切换成可编辑状态
- 标签右侧 `add`：新增标签
- `MY NOTE`：保存人工批注
- transcript 区块：没有结果时可单独点“识别口播”
- 左右箭头：切换上一条或下一条素材
- 顶部 `trash`：删除当前素材，并自动跳到下一条

### 对比页

- 可从详情页进入
- 也可从素材库选择模式进入
- 页面会给出推荐保留项、完整排序和人工粗筛风格短评
- 支持“一键保留推荐项”，其他素材进入回收站

### 回收站

- 每个素材库共用一个项目级回收站
- 删除后默认保留 7 天
- 可从回收站恢复素材

## 目录结构

```text
reelsift/
├── reelsift.py
├── pipeline.py
├── ai.py
├── asr.py
├── metrics.py
├── db.py
├── server.py
├── templates/
│   ├── index.html
│   ├── clip_detail.html
│   ├── compare.html
│   ├── recycle.html
│   └── partials/
├── static/
└── data/
    ├── uploads/
    ├── thumbnails/
    ├── previews/
    ├── picks/
    └── reelsift.db
```

## 更新记录

### 2026-04-24

- 增加：素材库左侧项目树，支持多层文件夹、拖拽调整层级、右键新建/重命名/删除
- 增加：项目级回收站，素材删除后保留 7 天，可恢复
- 增加：对比页 `templates/compare.html`，支持多条相似素材综合推荐与一键保留
- 增加：素材卡片右键菜单，可移动到指定文件夹或直接删除
- 增加：详情页 `MY NOTE`、摘要行内编辑、顶部 `trash`、上一条/下一条切换
- 增加：统一的自定义删除确认弹窗，替换浏览器系统弹窗
- 增加：素材卡片路径展示，显示为“素材库 / 所属文件夹”
- 增加：`未分类` 作为系统分组显示在项目树中
- 更改：上传成功后启动后台补全任务，预览视频和对比分改为后台生成并缓存
- 更改：详情页只读缓存结果，有预览就播，没有预览显示生成中
- 更改：素材库顶部主操作改成 `上传视频 + 选择`，导出、对比、删除都内置到选择模式
- 更改：导出时优先使用摘要命名文件，同一导出目录里自动处理重名
- 更改：摘要在同一素材库内禁止重名
- 更改：口播识别空结果不再误报超时，改为 `empty`
- 更改：批量选择相关表单对空 `selected_ids` 做了前后端容错，避免全选删除时报 422
- 删除：素材库首页的“成功 / 结果 / 失败”统计卡片
- 删除：上传阶段的清晰度评分
- 删除：详情页摘要下方重复的“场景 / 主体 / 动作”行

### 2026-04-23

- 增加：详情页独立 transcript 区块，支持单条素材手动“识别口播”
- 增加：口播识别状态拆分为 `processing / done / empty / failed / unavailable`
- 增加：浏览器预览视频缓存，详情页默认播放预览版并保留原视频入口
- 增加：上传页显示已用时和预计剩余时间
- 增加：README 记录更新历史，便于后续持续追加
- 更改：上传主流程中的 ASR 改为非阻塞可选项，失败不再阻塞抽帧、摘要、入库
- 更改：错误提示拆分为“视觉摘要失败”和“口播识别失败”
- 更改：视觉请求发送前压缩关键帧，减少大请求体导致的断连
- 更改：上传阶段不再计算清晰度评分，只保留封面选择
- 更改：清晰度改为详情页相似素材区域的对比分之一
- 更改：ASR 空文本结果不再误报超时，改为“识别完成但没有可用口播”
- 删除：上传阶段“计算清晰度评分”这一步

## 后续记录规则

- 新增功能：写清楚“增加了什么”
- 行为变化：写清楚“更改了什么”
- 废弃或移除：写清楚“删除了什么”
- 每次更新按日期追加，不覆盖旧记录

## 当前技术栈

- Python 3.11+
- ffmpeg-python
- opencv-python
- pillow
- sqlite3
- FastAPI
- Jinja2
- HTMX
- Tailwind CSS（CDN）
- python-dotenv

## 常用命令

### 启动 Web 界面

```bash
./.venv/bin/python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

### 处理一个视频目录

```bash
./.venv/bin/python reelsift.py /path/to/my/videos
```

### 语法检查

```bash
PYTHONPYCACHEPREFIX=/Users/seven/reelsift/.pycache ./.venv/bin/python -m py_compile server.py db.py metrics.py
```

### 清空缓存重新处理

```bash
rm -rf data/thumbnails data/previews data/reelsift.db
```
