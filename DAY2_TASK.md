# Day 2 任务：Gemini AI 集成 —— 生成视频摘要和标签

> 今天是整个项目最见效的一天。跑通后，视频从"一堆缩略图"变成"能看懂的素材库"。

---

## 今日目标

**用 Day 1 抽出的 6 张关键帧调用 Gemini API，为每个视频生成一句话摘要和标签，打印到终端。**

当我运行 `python reelsift.py ~/test-videos` 时，在 Day 1 的抽帧之后，新增：

1. 对每个视频，把 6 张关键帧上传给 Gemini
2. Gemini 返回结构化 JSON：摘要、标签、场景、主体、动作
3. 结果打印到终端（Day 3 再存数据库）
4. 终端最终输出类似：

```
[1/15] IMG_2847.mp4
  摘要：咖啡拉花特写，慢速倒奶
  场景：咖啡店
  主体：食物
  动作：倒奶、拉花
  标签：拉花、特写、室内

[2/15] IMG_2848.mp4
  摘要：傍晚外滩，行人和建筑远景
  ...
```

**这一天不做**：
- 不存数据库（Day 3）
- 不做 Web 界面（Day 4-5）
- 不做并发优化（先串行跑通再说）
- 不做错误重试（先让它跑出来，记录失败即可）

---

## 任务拆解

### 1. 依赖和环境

添加到 `requirements.txt`：
```
google-generativeai>=0.8.0
pillow
tenacity
```

`.env.example` 添加：
```
GEMINI_API_KEY=your_key_here
```

让我（用户）自己去 https://aistudio.google.com/ 拿 key，你不要假设我已经有。

### 2. 实现 `ai.py`

职责：封装 Gemini API 调用，输入 6 张关键帧路径，返回结构化数据。

**Prompt 设计要求（严格按这个来，不要自己发挥）**：

System prompt：
```
你是一个视频素材分析助手。用户是短视频创作者，主要拍摄生活 vlog 和旅行记录。
我会给你同一个视频中均匀抽取的 6 张关键帧。
请分析整段视频的内容，给出结构化的 JSON 输出。

输出要求：
- summary 字段：一句话客观描述，15 字以内，名词短语为主。
  好例子："咖啡拉花特写，慢速倒奶" "外滩傍晚远景，行人经过"
  坏例子："令人心动的咖啡时光" "这是一段拍摄于咖啡店的美好画面"
  不要用"一段"、"这是"开头，不要用抒情形容词。
- scene 字段：具体场景名词。如"咖啡店"、"街道"、"室内"、"地铁"。
  不要用"户外"、"公共场所"这种太宽泛的词。
- subjects 字段：数组，视频里的主要主体（最多 3 个）。
  选项：人物、食物、建筑、风景、物品、动物、交通工具、文字/招牌
- actions 字段：数组，视频里发生的动作（最多 3 个）。
  如：倒奶、拉花、走路、说话、吃、笑、看、指、拿起
  如果是静态画面（空镜），actions 为空数组 []
- tags 字段：数组，3-5 个通用标签。
  可以是：特写、中景、远景、运镜、空镜、黄昏、阴天、人物、食物等
- has_motion 字段：布尔，画面主体是否有明显运动

严格返回 JSON，不要 markdown 代码块，不要任何解释文字。
```

User prompt（和 6 张图一起发送）：
```
请分析这 6 张关键帧，它们来自同一段视频，按时间顺序排列。
```

**返回格式（严格 Pydantic 校验）**：

```python
from pydantic import BaseModel, Field

class VideoAnalysis(BaseModel):
    summary: str = Field(max_length=40)  # 中文 15 字约等于 40 个字符上限
    scene: str
    subjects: list[str]
    actions: list[str]
    tags: list[str]
    has_motion: bool
```

### 3. 函数签名

```python
def analyze_video(keyframe_paths: list[Path]) -> VideoAnalysis:
    """
    把 6 张关键帧发给 Gemini，返回结构化分析。
    失败时抛异常，由上层决定是跳过还是重试。
    """
```

要求：
- 用 `gemini-2.0-flash-exp` 模型
- 用 `tenacity` 库做重试：最多 3 次，指数退避（1s, 2s, 4s）
- 只重试网络错误和 JSON 解析错误，不重试 API 配额错误
- 如果 Gemini 返回的 JSON 被 markdown 代码块包裹（```json ... ```），要剥掉
- Pydantic 校验失败时打印原始返回内容，方便我调试 prompt

### 4. 集成到 `reelsift.py`

在 Day 1 的流程后面加上：

```python
# 伪代码
for video_path in videos:
    hash, frame_dir = extract_keyframes(...)  # Day 1
    try:
        analysis = analyze_video(sorted(frame_dir.glob("*.jpg")))
        print_analysis(video_path.name, analysis)  # 格式化打印
    except Exception as e:
        print(f"  ❌ AI 分析失败: {e}")
        failed_analyses.append((video_path, str(e)))
```

### 5. 成本保护：必须加的"冒烟测试"模式

**这是硬要求**：在 `reelsift.py` 添加一个 `--limit` 参数，默认不限制。

```bash
# 只处理前 3 个视频，用来测试 prompt 和 API
python reelsift.py ~/test-videos --limit 3

# 处理全部
python reelsift.py ~/test-videos
```

**原因**：Gemini API 一次 ¥0.02-0.05，跑 50 个视频发现 prompt 不对，就浪费 50 次钱。**加了 limit 参数，让我先用 3 个视频验证 prompt 效果，再跑全量**。

### 6. 不要做的事

- **不要批量调用 / 不要并发**。先串行跑通。并发优化放到以后。
- **不要缓存 AI 结果**。Day 3 用 SQLite 做缓存，今天直接每次重新调用。
- **不要做 streaming 输出**。用普通的同步 API。
- **不要自己加重试逻辑**。用 `tenacity` 库。

---

## 验收标准

### 基础测试

```bash
cd ~/reelsift

# 先用 1 个视频试水
python reelsift.py ~/test-videos --limit 1
```

看到类似这样的输出：

```
🎬 Reelsift
扫描文件夹：/Users/me/test-videos
找到 15 个视频文件，limit=1，实际处理 1 个

[1/1] IMG_2847.mp4
  抽帧: 6 张关键帧已保存
  分析中... (Gemini)
  摘要：咖啡拉花特写，慢速倒奶
  场景：咖啡店
  主体：食物、人物
  动作：倒奶、拉花
  标签：特写、室内、拉花
  有运动：是

✅ 处理完成
成功：1  失败：0
AI 调用：1 次
耗时：8.3s
```

### Prompt 效果检验（最关键的一步）

**我（用户）会用 3 个不同类型的视频测试**：
1. 一个有明显主体和动作的（比如吃东西、走路）
2. 一个空镜/风景（没有人物和动作）
3. 一个有口播的（需要看 AI 能不能识别"说话"动作）

**每个视频的 AI 输出，我要真的读一下**：
- summary 是不是真的 15 字以内、符合"简洁客观"风格？
- scene 是不是具体的地点（"咖啡店"而不是"室内"）？
- subjects 和 actions 数组有没有被错误填充（比如空镜里塞进了"人物"）？
- 有没有 AI 自己发挥、违反 prompt 要求的情况？

**如果效果不理想，不要急着跑全量。告诉我（Claude）具体问题，我们调 prompt。**

---

## 关键调试点

### Gemini 返回的 JSON 被包裹

Gemini 有时候不听话，会把 JSON 包在 ```json ... ``` 里。在解析前做这个清理：

```python
text = response.text.strip()
if text.startswith("```"):
    text = text.split("```")[1]  # 取中间部分
    if text.startswith("json"):
        text = text[4:]
text = text.strip()
```

更稳的做法是用 Gemini 的 JSON mode：

```python
generation_config = {
    "response_mime_type": "application/json",
    "temperature": 0.3,  # 低温度让输出更稳定
}
```

**如果 Gemini SDK 支持 JSON mode，用 JSON mode。** 不支持的话用清理逻辑兜底。

### 图片太大导致慢

关键帧是 480px 宽，应该不大。如果发现某个视频处理特别慢（超过 30 秒），打印出每张图的文件大小，看是不是某张超大。

### API 配额错误

免费账户有 RPM（每分钟请求数）限制。如果一次跑多个视频，可能会被限流。今天先串行跑，不会遇到；Day 3 之后再处理。

---

## 今日结束前

更新 `CLAUDE.md`：
- 进度清单 Day 2 打勾
- 在"关键决策记录"加一条：Prompt 的设计原则
- 如果你调整了 prompt，把最终版本保存到 `prompts/video_analysis_v1.txt`，方便以后对比

告诉我：
1. 跑通了没有
2. 用几个视频测试的，摘要质量怎么样
3. 标签准不准，有没有离谱的输出
4. 耗时大概多少、花了多少钱（Gemini Studio 后台能看）
5. 遇到什么问题

然后等我的 Day 3 指令。
