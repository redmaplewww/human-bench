---
name: human-bench
description: >-
  娱乐向「人类跑分 / 碳基模型评测」。当用户说 给我跑个分、测测我相当于什么模型、
  评测我的水平、我的 tok/s / 打字速度 / 阅读速度 / 首字时延、人类基准测试、
  碳基模型评测、八维图、锐评我 等时使用。纯本地解析 ZCode 历史 session，
  计算打字速度(tok/s)、阅读速度(pp)、首字时延(TTFT)，结合对话质量主观评估八项能力，
  生成八维雷达图 + 「每项≈哪个模型」评价表的自包含 HTML 报告。指标口径刻意不严谨，纯玩。
---

# 碳基模型跑分 (human-benchmark)

把用户当成一个模型，用历史 session 数据给它出一份跑分报告。**全程本地、纯娱乐、
口径极不严谨**——这正是它的卖点，不要试图把指标做严谨。

## 流程

### 1. 解析历史 session（自动探测全部 harness）

```bash
# 默认 auto: 自动探测本机所有 harness 的会话数据并合并
python "<本 skill 目录>/scripts/bench.py" analyze --out ./bench-out
# 只跑某一个 harness: --source zcode|codex|claude|opencode|dsh
```

auto 模式会依次探测并合并（哪个有数据吃哪个，互不拖累）：

| harness | 数据位置 | 说明 |
|---|---|---|
| zcode | `~/.zcode/cli/log` + `rollout` + 任务库 | 时间线主干=CLI 日志；正文从 rollout 抢救 |
| codex | `~/.codex/sessions/**` | 持久含正文，子串预筛流式解析 |
| claude | `~/.claude/projects/**/*.jsonl` | Claude Code 标准会话 |
| opencode | `~/.local/share/opencode` 等多布局 | 按多种历史存储布局探测 |
| dsh | `~/.dsh/sessions/**/session.jsonl.zstd` | 需 `pip install zstandard` |

脚本会过滤各 harness 注入的假"用户消息"（system-reminder、runtime context、
browser-context、goal 重注入、附件清单等），剔除物理上不可能的排队消息样本
（>10 字/秒），并在输出里如实报告"检测到但没数据/解析失败"的 harness。

产出 `<out>/metrics.json`（tok/s、pp、TTFT、并发数、行为画像、跨 harness 来源）
和 `<out>/digest.md`（消息摘要带 `[harness]` 标签 + 任务/工作目录 + 自动统计）。
并发数是跨 harness 统计的（同一时间窗内所有 harness 的活跃 session 重叠数）。

速度类指标样本 < 5 时照常出报告，但评语里必须吐槽样本量（"本评测的置信度约等于
掷硬币"）。

### 2. 阅读摘要，给八维打分

通读 `digest.md`，按 `references/rubric.md` 的锚点标准给八个维度打 0-100 分。
正文丢失是常态——此时以**任务标题**（体现任务类型与领域广度）、幸存正文、
消息长度节奏（如 200+ 字的长指令 vs 10 字的简短指令）为证据；没有证据的维度给
50 左右并注明"证据不足"。

### 3. 写 scores.json

在 `bench-out/` 下写 `scores.json`，结构必须如下（comment 是每维一句锐评，
总评 150-250 字，风格见 rubric）：

```json
{
  "model_name": "Homo-72B-Thinking-Preview",
  "overall_comment": "……",
  "dimensions": {
    "reasoning":  {"score": 62, "comment": "……"},
    "coding":     {"score": 70, "comment": "……"},
    "math":       {"score": 40, "comment": "……"},
    "knowledge":  {"score": 66, "comment": "……"},
    "prompting":  {"score": 75, "comment": "……"},
    "creativity": {"score": 55, "comment": "……"},
    "context":    {"score": 48, "comment": "……"},
    "humor":      {"score": 58, "comment": "……"}
  }
}
```

`model_name` 按 rubric 的命名公式起，一定要好笑。

### 4. 生成报告并交付

```bash
python "<本 skill 目录>/scripts/bench.py" report --out ./bench-out
```

得到 `bench-out/report.html`。用系统默认浏览器打开它
（Windows Git Bash: `cmd //c start "" "<绝对路径>"`），然后在聊天里给出：
文件链接、综合段位、三大指标 + 一句 verdict、总评全文。聊天摘要要短，
报告里什么都有。

## 注意

- 隐私：所有数据只在本地解析，绝不外传，不要把用户消息原文贴到报告以外的地方。
- 语气：报告和评语都是综艺感锐评（先夸后损、玩 LLM 梗），但不得真的贬低用户；
  自嘲对象应该是"评测方法"而不是用户本人。
- 段位对照表（0.3B → GPT-6 → Claude Code Fable 5.1）内置于脚本，会自动按分数映射，
  不要在 scores.json 里手写段位。
