# Reelwave 作品案例页规格文档

## 1. 项目定位

### 产品名称
**Reelwave**

### 产品副标题
**面向原始素材的可视化故事编辑器**

### 一句话定位
**把原始素材，变成你能亲手编辑的故事。**

产品解释：

> Reelwave 是一个面向视频创作者的本地可视化故事编辑工具。
> 用户不需要反复通过 Prompt 告诉 AI"故事应该怎么改"，而是直接移动真实视频素材和 Story Beat 来重组叙事，AI 理解这些编辑动作并提供局部辅助。

### 核心产品理念
**从"基于 Prompt 的叙事" 转向 "直接操作式叙事"**

AI 是 reasoning layer（推理层），不是产品主体。

---

## 2. 作品页目标

这个页面不是：

- SaaS 营销官网
- 开发文档
- 产品下载站
- AI 模型展示页

而是：

> **AI 产品经理作品案例页**

主要面向：

- HR
- 产品负责人
- AI 产品经理面试官
- 对项目实现感兴趣的技术面试官

访问者应该能够：

- **30 秒内理解：** Reelwave 是什么
- **2–3 分钟内理解：** 为什么这个产品经历了多次 Pivot，以及最终产品判断是什么
- **5 分钟内理解：** 我在需求、AI 能力判断、人机交互和产品实现中的作用

---

## 3. 页面核心叙事

整个 Case Study 围绕一条主线展开：

**管理素材**
↓
**生成故事**
↓
**用素材直接编辑故事**

产品迭代经历：

### V1
AI 素材库

### V2
本地故事 Agent

### V3
可视化故事编辑器

真正的 PM Case 不是：

> "我做了一个 AI App。"

而是：

> "我不断验证最初的产品假设，并在通用 Agent 已经能够替代原方案后重新设计 Human-AI Interaction。"

---

## 4. SECTION 1 — HERO

### Eyebrow
`REELWAVE · 可视化故事编辑器`

### Headline
**把原始素材，变成你能亲手编辑的故事。**

### Subtitle
> Reelwave 是一个面向视频创作者的本地优先可视化故事编辑器。
> 你不需要反复用 Prompt 让 AI 重写故事，而是直接移动真实素材来构建故事——AI 会理解并辅助你的每一次编辑决定。

辅助表达：

> 从零散视频素材到可编辑故事，而不是再多一个帮你写文案的 AI 聊天框。

### CTA

Primary:

**观看60秒演示**

Secondary:

**了解产品**

Tertiary:

`查看 GitHub ↗`

不要把 Download 作为主要 CTA。

### Hero Visual

直接使用真实 Reelwave Story Canvas。

优先使用一个 8–12 秒自动循环、静音的视频：

Story Canvas
→
拖动素材
→
Inspector 出现 Story Change
→
Apply
→
Beat / Script 更新

不要使用 AI 生成的概念图。

---

## 5. SECTION 2 — PROBLEM

### Headline
**真正难的不是剪，而是不知道这些素材到底能讲什么。**

### Copy
> 拍完一段 vlog 之后，创作者常常面对几十甚至上百个片段、一堆零散的想法，却没有清晰的叙事线。
>
> 常见的流程是：
>
> 看完所有素材 → 找主题 → 写文案 → 再去找素材 → 重写 → 再找一遍。
>
> 认知瓶颈发生在动手剪辑之前。

### Visual Flow

原始素材
87 个片段

↓

"发生了什么？"

↓

"这段视频到底在讲什么？"

↓

"该怎么讲这个故事？"

↓

"哪些素材能支撑每一部分？"

### Closing
**Reelwave 专注于素材和故事之间的这段空白。**

---

## 6. SECTION 3 — PRODUCT EVOLUTION

### Headline
**我一开始做的并不是对的产品。**

### Subtitle
> 在验证产品背后的假设过程中，Reelwave 经历了两次重大转向。

### V1 — AI 素材库

#### Hypothesis
> 创作者的困境在于，无法高效理解和检索大量素材。

#### What I built
- 画面理解
- 语音识别
- 标签
- 搜索
- 评分
- 相似素材
- 导出
- 素材组织

#### What I learned

两个问题逐渐暴露：

1. 大体积视频上传到 Web 产品本身形成新的 friction；
2. 在真实 vlog 创作中，"找到素材"并不是最核心的创作难点。

#### Product Decision
**把产品搬到素材所在的地方。**

### V2 — 本地故事 Agent

产品改为 Local-first：

Finder
→
分析
→
故事
→
文案

#### Hypothesis
> 如果 AI 能理解所有素材，就能帮创作者发现故事并写出文案。

#### The Codex Test

我将一组真实 vlog 原始素材直接交给 Codex，在没有人工整理故事结构的情况下进行测试。

Codex 已能够完成：

- 视频内容理解
- 跨素材关系判断
- Story Angle 生成
- 主线提炼
- 信息缺口识别
- 主动追问用户
- Script 生成
- 素材匹配建议

实际生成过类似：

> "北京实习的一个月，我慢慢学会把陌生城市过成自己的日常。"

#### Key Realization
**这暴露了我自己产品的问题。**

如果通用 Agent 已经能够完成 Story Discovery，那么继续做一个：

> 聊天式 + 故事 Agent

没有足够长期价值。

### V3 — 可视化故事编辑器

#### Key Decision
**不在智能程度上与通用 Agent 竞争，而是改变交互方式。**

Reelwave 从：

**基于 Prompt 的叙事方式**

转为：

**直接操作式叙事**

#### Before

用户：

> 把做饭那段素材挪到前面，重写开头让它更像是在讲一个人独自生活的感觉。

AI：

返回一段修改后的内容。

#### After

用户直接拖动"做饭"素材：

从：

`下班后的生活`

到：

`刚到北京`

Reelwave 理解：

> 用户正在重新定义这条素材的叙事意义。

然后提供局部 Story / Script 修改建议。

---

## 7. SECTION 4 — CORE INTERACTION

### Headline
**直接移动素材本身，来构建故事。**

### Subtitle
> Story Beat 把叙事意图、文案和真实素材，连接成一个可编辑的结构。

### Example

#### 01 — 刚到北京

Intent

> 开始学着一个人在陌生城市生活。

Script

> 刚到北京的时候，我先学会的不是工作……

Footage

[入住自拍] [做饭] [街景]

#### 02 — 找到生活节奏

Intent

> 工作开始塑造日常生活。

Footage

[办公室] [晚高峰] [下班自拍]

#### 03 — 把城市过成自己的日常

Intent

> 空闲时间渐渐融入这座城市的生活。

Footage

[公园] [咖啡] [朋友]

### Interaction Principle
**素材可以在 Beat 之间移动，Beat 也可以在故事里重新排序。**

---

## 8. SECTION 5 — THE WOW MOMENT

这是整个作品页最重要的交互展示。

### Headline
**一次拖动，不只是排版变化，它承载着叙事意图。**

### Before

Cooking clip 当前属于：

`03 · 下班后的生活`

用户拖动到：

`01 · 刚到北京`

### Reelwave Inspector

#### STORY CHANGE
> 把这条做饭素材移到开场，它的角色从"下班后享受生活"变成了"学会在陌生城市照顾自己"。

#### Suggested Intent
> 从一个人照顾自己的日常开始适应北京。

#### Suggested Script
> 刚到北京的时候，我先学会的不是工作，而是一个人把日子过起来。

Buttons:

**应用**

**忽略**

### Core Principle
**AI 负责建议，创作者负责决定。**

---

## 9. SECTION 6 — HUMAN × AI

### Headline
**AI 是推理层，而不是产品本身。**

### Creator owns
- 故事方向
- 素材摆放
- Story Beat 结构
- 个人意义
- 最终编辑决定

### AI assists
- 理解素材
- 发现叙事模式
- 文案起草
- 素材匹配溯源
- 解读编辑改动
- 给出局部修改建议

### Principle
> Reelwave 的设计核心是"直接操作 + AI 辅助"，而不是以对话为先的创作方式。

---

## 10. SECTION 7 — LOCAL-FIRST

### Headline
**把产品搬到素材身边，而不是把素材搬到产品里。**

### Workflow

Finder
↓
用 Reelwave 打开
↓
本地分析
↓
Story Canvas
↓
预览

### Copy
> 视频文件体积大、存在本地，并且已经按文件夹组织好了。
> Reelwave 直接进入这套已有的工作流，而不是要求创作者把所有素材迁移到另一个云端素材库。

### Key Benefits
**无需上传**

**原始素材不被改动**

**项目状态本地保存**

这里使用真实 Finder 右键截图。

---

## 11. SECTION 8 — WORKING ALPHA

### Headline
**这是一个真正可运行的本地 Alpha 版本，而不只是原型。**

### Footage Layer
- 视频扫描
- 关键帧提取
- 画面理解
- 可选语音识别
- 备注与标签

### Story Layer
- Story Beat
- 文案生成
- 素材匹配溯源
- AI 故事建议
- 应用 / 忽略
- 故事预览

### Native Workflow
- Finder 集成
- 原生 macOS 窗口
- SQLite 本地状态
- 项目状态持久化恢复

### Built With
Swift / AppKit
Python
SQLite
多模态大模型

### Product Status
**本地 Alpha 版本 / 面试演示**

不要写成：

- 正式上线
- 商业化
- 大规模用户使用
- Production ready

除非之后真实发生。

---

## 12. SECTION 9 — MY ROLE

### Headline
**我的角色**

### Copy
> 从问题发现到可运行的 Alpha 版本，我独立负责了整个产品。

### Product
- 问题定义
- 工作流研究
- 产品定位
- 功能优先级
- 交互设计
- 验收标准

### AI
- 能力评估
- Prompt / 上下文设计
- Codex 替代性测试
- 人机交互设计

### Execution
- 架构决策
- 与 AI 编程 Agent 协作实现
- 测试
- 迭代

### AI Collaboration Statement
> Codex 作为工程协作者参与了代码分析、实现和调试；产品方向和验收决定始终由我主导。

---

## 13. SECTION 10 — PRODUCT LESSONS

### Headline
**这个项目改变了我看待 AI 产品的方式**

### 01 — AI 能力本身不是护城河。
> 如果通用 Agent 一句提示词就能复现同样的智能，产品的价值就必须来自别处。

### 02 — 工作流有时比模型能力更重要。
> 当我不再试图在智能上超越通用 Agent，转而围绕真实的编辑动作设计产品时，Reelwave 才真正有了意义。

### 03 — AI 应该理解用户的意图，而不是替用户做决定。
> 故事的改动始终由用户掌控；AI 只给出局部修改建议，而不会悄悄重写整部作品。

---

## 14. SECTION 11 — NEXT

### Headline
**接下来我会往哪个方向做**

只展示三个方向。

### Rough Cut Preview
把 Story Beat 变成一个可播放的粗剪版本。

### NLE Export
把编辑决定直接导出到 Final Cut / Premiere，而不只是停留在一份文字方案。

### Creator Memory
学习创作者在不同项目中反复出现的编辑偏好，同时不干预创作主导权。

---

## 15. FOOTER

### Closing Question
**Reelwave 是在探索一个问题：**

**如果 AI 已经能理解素材，创作者又该如何与它交互？**

### CTA
- 观看演示
- 查看 GitHub
- 返回简历

---

## 16. NAVIGATION

顶部导航保持极简。

左侧：

**Reelwave**

右侧：

- 演示
- 演进
- 产品
- 心得
- GitHub ↗

不要出现：

- Pricing
- Customers
- Contact Sales
- Features
- Solutions

因为这是 Portfolio Case，而不是 SaaS 官网。

---

## 17. VISUAL DIRECTION

整体视觉参考：

- Apple
- Linear
- Notion
- 高质量 editorial portfolio

视觉原则：

- 白 / 极浅灰背景
- 深灰正文
- 单一强调色
- 高信息密度
- 大量真实产品截图
- 克制圆角
- 少 icon
- 不使用 AI 紫色渐变
- 不使用玻璃拟态
- 不使用无意义 3D 插画
- 不使用 AI 生成产品概念图

动画只用于：

- Drag interaction
- Story Change
- Apply 后更新
- 页面轻微 reveal

---

## 18. REQUIRED ASSETS

推荐目录：

```text
portfolio/
└── assets/
    ├── hero-story-canvas.png
    ├── finder-open-reelwave.png
    ├── codex-story-test.png
    ├── story-before.png
    ├── story-after.png
    ├── drag-story-change.mp4
    ├── reelwave-demo-40s.mp4
    └── reelwave-demo-90s.mp4
```

如果某项素材暂时不存在：

- 使用明确 placeholder；
- 不生成假的产品截图；
- 在实现中留下清楚 TODO。

---

## 19. HERO DEMO SCRIPT — 40s

### 0–4s

Finder 中显示真实 vlog 文件夹。

字幕：

**87 个片段，还没有清晰的故事。**

右键：

用 Reelwave 打开

### 4–9s

Reelwave Story Canvas 打开。

跳过漫长 loading。

字幕：

**Reelwave 理解素材内容，并搭出一个初始结构。**

### 9–15s

展示 Story Beats：

01 刚到北京
02 进入工作节奏
03 下班后的生活
04 城市成为日常

字幕：

**但故事不由 AI 决定。**

### 15–23s

拖动"做饭"素材：

03 → 01

字幕：

**直接移动素材，改变故事。**

### 23–30s

Inspector 出现：

故事的含义可能变了。

展示 Suggested Intent / Script。

点击 Apply。

字幕：

**AI 理解编辑意图，并给出一个局部更新建议。**

### 30–36s

拖动 Story Beat。

Canvas 重排。

字幕：

**素材在动，Beat 在动，故事也在演进。**

### 36–42s

点击 Preview。

素材开始连续播放。

Logo：

**Reelwave**

Subtitle：

**把原始素材，变成你能亲手编辑的故事。**

---

## 20. FULL DEMO SCRIPT — 90s

### 0–8s — Problem

展示大量原始视频文件。

字幕：

**我有几十条 vlog 素材，却没有清晰的故事。**

### 8–16s — Local First

Finder：

用 Reelwave 打开

字幕：

**Reelwave 在素材已经存在的地方直接工作。**

### 16–28s — Understanding

展示：

- Thumbnail
- 画面摘要
- 文字转录
- 备注

字幕：

**AI 理解素材内容，但理解只是起点。**

### 28–40s — Story

Story Canvas 出现：

刚到北京
→ 工作节奏
→ 下班生活
→ 城市成为日常

字幕：

**它基于真实素材，提出一个初始故事。**

### 40–55s — Direct Manipulation

用户拖动素材跨 Beat。

字幕：

**我没有靠 Prompt 让 AI 重写故事，而是直接编辑故事本身。**

### 55–68s — AI Interpretation

Inspector：

- Story Change
- Suggested Intent
- Suggested Script

点击 Apply。

字幕：

**AI 解读这次编辑——但从不在没有确认的情况下改变故事。**

### 68–77s — Story Structure

拖动整个 Beat。

字幕：

**故事结构本身也是可编辑的。**

### 77–84s — Preview

点击 Preview。

字幕：

**画布变成了一份可执行的剪辑方案。**

### 84–90s — End

Logo：

**Reelwave**

Subtitle：

**面向原始素材的可视化故事编辑器**

Small text:

直接操作式叙事
本地优先
人类掌控 AI
