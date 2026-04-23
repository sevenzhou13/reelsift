# Reelsift

面向短视频创作者的本地素材粗筛工具：上传一批拍摄素材后，自动抽取关键帧、生成视觉摘要和标签，并在浏览器里完成浏览、筛选和导出。

## 当前状态

项目已经进入可持续迭代阶段，主链路能跑通：

- 上传本地视频并写入素材库
- 抽取 6 张关键帧并缓存
- 调用视觉模型生成摘要、场景、主体、动作、标签
- SQLite 存储素材、标签、转写和处理状态
- FastAPI + Jinja2 + HTMX 浏览素材库、详情页、上传页
- 支持素材库切换、搜索、标签筛选、详情页查看
- 支持多选导出原始视频
- 支持详情页单独触发口播识别

当前实现里，上传时不再计算清晰度评分；“对比分”改为在相似素材区域按需计算，便于后续继续扩展稳定度、构图等评分项。

## 当前采用的方法

### 上传处理链路

1. 保存上传视频到 `data/uploads/`
2. 抽取 6 张关键帧到 `data/thumbnails/{video_hash}/`
3. 选取中间帧作为封面缓存
4. 可选执行 ASR，失败不阻塞主流程
5. 调用视觉模型生成结构化摘要
6. 写入 SQLite

### 视觉分析方法

- 当前实现使用火山方舟兼容接口
- 输入是 6 张关键帧，加上可选 transcript 提示
- 发送前会把关键帧压缩成较小 JPEG，再转成 `data URL`
- 网络异常会自动重试，并转换成更清晰的中文错误

### 口播识别方法

- 使用火山语音识别接口
- 上传主流程中口播识别是“非阻塞可选项”
- 详情页可单独重新触发一次口播识别
- 对“识别完成但结果为空”和“真正失败/超时”做了区分

### 详情页视频预览方法

- 原始素材文件保持不变
- 详情页会按需生成浏览器可播放的 `mp4` 预览缓存
- 浏览器默认播放预览版
- “打开原视频 / 下载原视频”仍然指向原文件

### 相似素材对比分

- 当前已接入第一个对比维度：清晰度
- 对比分只在详情页相似素材区域按需计算
- 评分结构已经预留扩展位，后续可以继续增加稳定度、构图完整度等分数

## 进度概览

- [x] Day 1: 视频扫描 + 关键帧抽取
- [x] Day 2: 视觉摘要与标签生成
- [x] Day 3: SQLite schema + 处理状态管理
- [x] Day 4: FastAPI 页面与详情页
- [x] Day 5: 素材网格、搜索、标签筛选
- [~] Day 6: 多选导出已可用，相似素材对比刚开始
- [ ] Day 7: 真实素材回归测试 + 收尾优化

## 更新记录

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

### 后续记录规则

- 新增功能：写清楚“增加了什么”
- 行为变化：写清楚“更改了什么”
- 废弃或移除：写清楚“删除了什么”
- 每次更新尽量按日期追加，不覆盖旧记录

## 已实现功能

### CLI / 后端

- 递归扫描视频文件
- 抽取 6 张关键帧
- 调用视觉模型分析视频内容
- 写入 `data/reelsift.db`
- 缓存关键帧和封面图到 `data/thumbnails/`
- 素材库管理
- 上传任务状态追踪

### 数据存储

- `clips` 保存素材主信息、摘要、状态、封面、转写状态
- `clip_tags` 保存标签
- `transcripts` 保存口播分段
- 已建立基础索引

### Web 页面

- 首页素材网格展示
- 素材库切换
- 关键词搜索
- 标签筛选
- 详情页查看
- 相似素材展示
- 口播识别区块
- 多选导出

## 还没完成的部分

1. 相似素材真正的对比面板还不完整，目前只有清晰度一个维度。
2. 对比分还没有缓存到数据库，仍然是按需计算。
3. 详情页预览视频仍然是首次访问时生成，后续可继续前移到后台补全任务。
4. 更完整的筛选条件还没做，比如按运动状态、对比分排序等。
5. 真实大批量素材的回归测试还没系统化沉淀。

## 技术栈

- Python 3.11+
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

验证：

```bash
ffmpeg -version
```

### 4. 配置环境变量

```bash
cp .env.example .env
```

示例：

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

## 快速开始

### 启动 Web 界面

```bash
uvicorn server:app --reload --port 8000
```

浏览器打开：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

### 处理一个视频目录

```bash
./.venv/bin/python reelsift.py /path/to/my/videos
```

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
├── static/
└── data/
    ├── uploads/
    ├── thumbnails/
    ├── previews/
    ├── picks/
    └── reelsift.db
```
