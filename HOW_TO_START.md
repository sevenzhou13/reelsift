# 如何使用这些文档启动 Claude Code

> 这是给我自己看的操作说明。把三份文档放进项目后，按这个流程和 Claude Code 对话。

---

## 第一次启动流程

### 1. 准备好项目目录

```bash
mkdir -p ~/reelsift
cd ~/reelsift
```

### 2. 把三份文档放进去

把以下三个文件放到 `~/reelsift/`：
- `PROJECT_BRIEF.md`
- `CLAUDE.md`
- `DAY1_TASK.md`

### 3. 准备测试素材

找 5-10 个真实拍过的视频，放到一个文件夹（比如 `~/test-videos/`）。
**一定要用真实素材**，不要下载随便的测试视频——你用自己的素材测，发现的问题才是真问题。

### 4. 去拿 Gemini API Key（Day 2 要用，Day 1 不用也可以）

1. 打开 https://aistudio.google.com/
2. 用 Google 账号登录
3. 左上角点 "Get API key" → "Create API key"
4. 复制这串 key，先记在备忘录里，Day 2 会用到

### 5. 启动 Claude Code

```bash
cd ~/reelsift
claude
```

### 6. 第一句话这样说

复制粘贴以下内容给 Claude Code：

```
先读项目根目录的三份文档：PROJECT_BRIEF.md、CLAUDE.md、DAY1_TASK.md。

读完之后，按 CLAUDE.md 里"与 Claude Code 协作的规则"的第 1 条：
先告诉我你打算怎么做 Day 1 的任务，列出 3-5 条 bullet 计划。
我同意后再开始写代码。

开始吧。
```

### 7. 检查它的计划

Claude Code 应该会说一个类似这样的计划：

> 1. 创建项目骨架（各 py 文件、templates、static、.gitignore 等）
> 2. 写 requirements.txt，只包含 Day 1 依赖
> 3. 在 pipeline.py 实现 scan_folder 和 extract_keyframes
> 4. 在 reelsift.py 实现 CLI 入口
> 5. 测试：用一个视频试一下
>
> 确认后开始？

你看一下有没有问题，没问题就回复"好，开始"。

---

## 每天的启动流程（Day 2 及以后）

### 1. 启动

```bash
cd ~/reelsift
claude
```

### 2. 开场白

```
先读 CLAUDE.md 确认当前进度，然后读 DAY{N}_TASK.md。
按协作规则：先告诉我计划，确认后再写。
```

注意：每天开始前，我（你自己）要提前写好 `DAY{N}_TASK.md`，就像我给你的 `DAY1_TASK.md` 那样。
如果懒得自己写，可以跟我（Claude）再聊一次，让我帮你生成下一天的任务文档。

---

## Claude Code 跑偏时怎么办

### 场景 1：它开始用不认识的库/技术栈

"停。CLAUDE.md 里已经锁定了技术栈。为什么要用 XXX？先说理由，我同意才能换。"

### 场景 2：它写了一堆代码但没测试

"停下来。按协作规则第 3 条，每个模块写完要立刻运行测试。
现在给我一个命令，让我能验证你刚写的代码真的能跑。"

### 场景 3：它顺手改了其他文件

"回滚。我只让你改 X，你为什么动了 Y？除非有必要，不要擅自修改范围外的代码。"

### 场景 4：它遇到错误但"绕过"而不是修

"不要跳过错误。告诉我具体是什么错误，完整的 traceback 贴出来。我们一起修。"

### 场景 5：上下文太多它开始变笨

用 `/compact` 压缩对话，或者直接 `/clear` 重开。关键信息都在 CLAUDE.md 里，它重启后会读。

---

## 每天结束前

1. 让 Claude Code 更新 CLAUDE.md 的进度清单
2. 让它用一句话总结"今天做了什么、遇到什么坑"
3. 如果项目发生了技术决策变化，让它更新 CLAUDE.md 的"关键决策记录"
4. `git commit`：哪怕只有 2 行代码也要 commit，方便回滚

---

## 如果卡住了

- 回 Claude.ai 和我（Claude）继续聊，描述你卡在哪里
- 把 Claude Code 的完整错误输出贴给我
- 我可以帮你判断是要改代码、改任务、还是改项目方向

加油。
