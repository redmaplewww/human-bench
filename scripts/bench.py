#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
human-benchmark (纯娱乐)
把历史 session 里的"人类"当成一个模型来跑分:
  - tok/s  打字输出速度 (inputLength 字符 ÷ 距上轮回复的间隔)
  - pp     阅读速度 (pages per minute, 1 页 = 500 字)
  - TTFT   首字时延 (看完模型回复到手指落下第一键之间的"思考时间")
数据源 (互相补充):
  1. ~/.zcode/cli/log/zcode-*.jsonl  按天保留的 CLI 结构化日志 —— 时间线主干:
     turn.started(t0, inputLength) / turn.completed(t1) /
     model.response.diagnostics(每次请求的 responseLength)。正文不含敏感内容,
     只有长度, 但足够算全部速度指标, 且覆盖所有历史 session。
  2. ~/.zcode/cli/rollout/*.jsonl    滚动日志(会被轮转删除) —— 取消息正文,
     用于八维能力评估的消息摘要。拿不到正文的轮次只丢摘要, 不丢指标。
  3. ~/.zcode/v2/tasks-index.sqlite  任务标题列表, 能力评估的补充证据。
所有指标口径都极不严谨, 这本来就是玩具.

用法:
  python bench.py analyze [--log DIR] [--rollout DIR] [--out DIR]
  python bench.py report  [--out DIR]   # 需先按 rubric 写好 out/scores.json
"""
import argparse
import glob
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = os.path.expanduser("~")
DEFAULT_LOG = os.path.join(HOME, ".zcode", "cli", "log")
DEFAULT_ROLLOUT = os.path.join(HOME, ".zcode", "cli", "rollout")
DEFAULT_TASKS_DB = os.path.join(HOME, ".zcode", "v2", "tasks-index.sqlite")

CHARS_PER_PAGE = 500          # 1 页 = 500 字 (中文出版粗略标准)
MIN_GAP_S = 0.5               # 更短的间隔视为连击/系统行为
MAX_GAP_S = 1800              # 超过 30 分钟算挂机, 不计入思考
SPEED_CAP = 40.0              # tok/s 截断: 粘贴不算手速
PLAUSIBLE_RATE = 10.0         # 人类持续输入上限(字/秒, 含语音); 更快 = 指令排队
MAX_MSG_TOKENS = 800          # 超长消息按粘贴处理, 不进速度样本
MIN_ASST_CHARS = 100          # 上一轮回复太短不足以构成"阅读事件"

DIMENSIONS = [
    ("reasoning",   "逻辑推理"),
    ("coding",      "代码能力"),
    ("concurrency", "并发能力"),
    ("knowledge",   "知识广度"),
    ("prompting",   "需求表达"),
    ("creativity",  "创造力"),
    ("context",     "上下文记忆"),
    ("humor",       "幽默情商"),
]

# ---------------------------------------------------------------- 段位表

LADDER = [
    (0,   9,  "0.3B · 随机鹦鹉级",       "会说人话，但不知道自己在说什么"),
    (10,  19, "1.8B · 电子宠物级",       "能陪聊两句，一问深就宕机"),
    (20,  29, "4B · 端侧小钢炮级",       "手机就能跑，离线也能浪"),
    (30,  39, "7B/8B · 开源性价比级",    "单卡平民旗舰，人人可用"),
    (40,  49, "14B · 本地部署骄傲级",    "不用 API key 也硬气"),
    (50,  59, "32B · 单卡旗舰级",        "量化一下能塞进消费级显卡"),
    (60,  69, "70B · 老牌贵族级",        "开源时代的巨人，跑分依旧能打"),
    (70,  79, "100B+ MoE · 国产之光级",  "大厂旗舰，基准屠榜"),
    (80,  86, "GPT-4o · 六边形战士级",   "全能无明显短板"),
    (87,  91, "DeepSeek-R1 · 深度思考级", "先想一想，再说话"),
    (92,  95, "Claude Sonnet 4.5 · 代码艺术家级", "写代码像在写诗"),
    (96,  98, "GPT-5 · 前沿旗舰级",      "第一梯队，贵有贵的道理"),
    (99,  99, "GPT-6 · 传说前沿级",      "只在发布会和财报里见过"),
    (100, 100, "Claude Code Fable 5.1 · 神话级", "图灵看了都想复测"),
]


def tier_of(score):
    score = max(0, min(100, round(score)))
    for lo, hi, name, blurb in LADDER:
        if lo <= score <= hi:
            return {"score": score, "name": name, "blurb": blurb}
    return {"score": score, "name": "?", "blurb": ""}


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- 数据源 1: CLI 日志 (时间线主干)

def load_log_turns(log_dir):
    """返回 {sid: [turn,...]} (按 t0 排序), turn 含 t0/t1/input_chars/asst_chars."""
    files = sorted(glob.glob(os.path.join(log_dir, "zcode-*.jsonl")))
    if not files:
        return {}, 0, set()
    turns = {}                 # (sid,tid) -> turn
    agent_sids = set()
    for path in files:
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = d.get("event") or ""
                if "persistence.started" in ev:
                    if "agentId" in (d.get("context") or {}):
                        sid = d.get("sessionId")
                        if sid:
                            agent_sids.add(sid)   # subagent 会话, 不是人类
                    continue
                if ev not in ("turn.started", "turn.completed",
                              "model.response.diagnostics"):
                    continue
                sid = d.get("sessionId")
                tid = d.get("turnId") or (d.get("context") or {}).get("turnId")
                if not sid or not tid:
                    continue
                key = (sid, tid)
                if ev == "turn.started":
                    il = (d.get("context") or {}).get("inputLength")
                    t = turns.setdefault(key, {"sid": sid, "tid": tid,
                                               "t0": None, "t1": None,
                                               "input_chars": None,
                                               "asst_chars": 0,
                                               "last_ts": None})
                    if t["t0"] is None:
                        t["t0"] = parse_ts(d["timestamp"])
                    if il is not None and t["input_chars"] is None:
                        t["input_chars"] = il
                else:
                    t = turns.get(key)
                    if t is None:
                        continue
                    ts = parse_ts(d["timestamp"])
                    t["last_ts"] = max(t["last_ts"] or ts, ts)
                    if ev == "turn.completed" and t["t1"] is None:
                        t["t1"] = ts
                    else:      # diagnostics: 只累计 main_turn 的回复长度
                        ctx = d.get("context") or {}
                        if ctx.get("querySource") == "main_turn":
                            t["asst_chars"] += ctx.get("responseLength") or 0
    sessions = {}
    for (sid, tid), t in turns.items():
        if sid in agent_sids or t["t0"] is None or not t["input_chars"]:
            continue                      # 非人类触发的轮次
        if t["t1"] is None:
            t["t1"] = t["last_ts"]        # 崩溃/未完结轮次, 用最后事件兜底
        if t["t1"] is None:
            continue
        sessions.setdefault(sid, []).append(t)
    for tl in sessions.values():
        tl.sort(key=lambda x: x["t0"])
    return sessions, len(files), agent_sids


# ---------------------------------------------------------------- 数据源 2: rollout (消息正文)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SKIP_PREFIX = ("<system", "<command", "<local", "[Request interrupted",
               "<task-notification")


def count_tokens(text):
    cjk = len(CJK_RE.findall(text))
    return cjk + (len(text) - cjk) / 4.0


def human_text_of(msg):
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    c = msg.get("content")
    if isinstance(c, str):
        txt = c
    elif isinstance(c, list):
        parts = [b.get("text", "") for b in c
                 if isinstance(b, dict) and b.get("type") == "text"]
        txt = "\n".join(parts)
    else:
        return None
    if "tool_result" in txt[:300]:
        return None
    txt = re.sub(r"<system-reminder>.*?</system-reminder>", "", txt, flags=re.S)
    txt = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "",
                 txt, flags=re.S)
    txt = re.sub(r"<task-notification>.*?</task-notification>", "", txt,
                 flags=re.S)
    lines = [ln for ln in txt.splitlines()
             if not ln.strip().startswith("Attached ")]
    txt = "\n".join(lines).strip()
    if not txt:
        return None
    head = txt.lstrip()
    if any(head.startswith(p) for p in SKIP_PREFIX):
        return None
    return txt


def load_rollout(rollout_dir):
    """返回 {sid: [turn,...]} + 模型侧统计. rollout 会轮转, 尽力而为."""
    files = sorted(glob.glob(os.path.join(rollout_dir,
                                          "model-io-sess_*.jsonl")))
    by_session = {}
    model_stats = {"call_speeds": [], "burned": 0, "tools": Counter(),
                   "models": set(), "model_counts": Counter()}
    for path in files:
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("querySource") != "main_turn":
                    continue
                sid = d.get("sessionId") or "?"
                tid = d.get("turnId") or path
                try:
                    t0 = parse_ts(d["startedAt"])
                    t1 = parse_ts(d["completedAt"])
                    dur = float(d.get("durationMs") or 0) / 1000.0
                except (KeyError, ValueError):
                    continue
                turns = by_session.setdefault(sid, {})
                if tid not in turns:
                    # 触发消息: 从消息列表末尾往前找第一条真人文本
                    msgs = (d.get("request") or {}).get("messages") or []
                    text = None
                    for m in reversed(msgs):
                        text = human_text_of(m)
                        if text:
                            break
                    turns[tid] = {"sid": sid, "tid": tid, "t0": t0,
                                  "t1": t1, "text": text, "asst_text": "",
                                  "out_tokens": 0}
                t = turns[tid]
                t["t1"] = max(t["t1"], t1)
                resp = d.get("response") or {}
                t["asst_text"] += resp.get("text") or ""
                usage = resp.get("usage") or {}
                ot = usage.get("outputTokens") or 0
                t["out_tokens"] += ot
                model_stats["burned"] += ot + (usage.get("inputTokens") or 0)
                if ot > 0 and dur > 0.3:
                    model_stats["call_speeds"].append(ot / dur)
                for tc in resp.get("toolCalls") or []:
                    if isinstance(tc, dict) and tc.get("name"):
                        model_stats["tools"][tc["name"]] += 1
                mid = ((d.get("model") or {}).get("modelId"))
                if mid:
                    model_stats["models"].add(mid)
    sessions = {}
    for sid, turns in by_session.items():
        tl = sorted(turns.values(), key=lambda x: x["t0"])
        deduped = []
        for t in tl:
            if deduped and t["text"] and t["text"] == deduped[-1]["text"]:
                continue
            deduped.append(t)
        if deduped:
            sessions[sid] = deduped
    return sessions, len(files), model_stats


# ---------------------------------------------------------------- 数据源 3: 任务标题

def load_task_titles(db_path):
    """返回 (任务标题列表, {task_id: workspace_path}) ."""
    if not os.path.isfile(db_path):
        return [], {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT task_id, title, task_status, workspace_path FROM tasks"
            " ORDER BY created_at"
        ).fetchall()
        con.close()
        tasks = [{"title": r[1], "status": r[2]} for r in rows if r[1]]
        workspaces = {r[0]: r[3] for r in rows if r[0] and r[3]}
        return tasks, workspaces
    except sqlite3.Error:
        return [], {}


# ---------------------------------------------------------------- 数据源 4: Codex sessions

CODEX_SKIP_PREFIX = (
    "<app-context", "<user_instructions", "<environment_context",
    "<turn_context", "<recommended_plugins", "<permissions", "<app-server",
    "<system", "<task", "<goal", "<approval", "<ambient", "<skills",
    "[Request interrupted", "<command", "<local", "<user_memory",
    "<in-app-browser-context", "<turn_aborted", "<sandbox", "<pipeline",
    "<codex_internal_context",
    "# Files mentioned by the user:", "# AGENTS.md instructions",
    "# Codex desktop context",
)


def codex_user_text(payload):
    if not isinstance(payload, dict) or payload.get("role") != "user":
        return None
    parts = [b.get("text") or "" for b in payload.get("content") or []
             if isinstance(b, dict) and b.get("type") == "input_text"]
    txt = "\n".join(parts).strip()
    if not txt:
        return None
    if any(txt.lstrip().startswith(p) for p in CODEX_SKIP_PREFIX):
        return None
    return txt[:2000]


def load_codex(codex_home):
    """解析 ~/.codex/sessions/**/rollout-*.jsonl (持久保存, 含正文).
    大文件动辄上百 MB, 先做子串预筛, 只对 user/assistant/token_count 行做
    完整 json 解析, 其余行只廉价截取时间戳."""
    sessions_dir = os.path.join(codex_home, "sessions")
    files = sorted(glob.glob(os.path.join(sessions_dir, "**", "*.jsonl"),
                             recursive=True))
    empty_stats = {"call_speeds": [], "burned": 0,
                   "tools": Counter(), "models": set(),
                   "model_counts": Counter()}
    if not files:
        return {}, Counter(), empty_stats
    sessions, cwd_counter = {}, Counter()
    tools, burned, models = Counter(), 0, Counter()
    for idx, path in enumerate(files, 1):
        if idx % 50 == 0:
            print(f"     ... 已读 {idx}/{len(files)} 个 session 文件")
        sid = os.path.basename(path)
        turns, cur, last_ts, last_total = [], None, None, None
        pending_prev = None   # 新一轮提交(turn_context)之前最后活动的时间
        file_cwd = None
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                i = line.find('"timestamp":"')
                ts = line[i + 13:i + 37] if i >= 0 else last_ts
                if '"role":"user"' in line:
                    try:
                        txt = codex_user_text(
                            json.loads(line).get("payload") or {})
                    except json.JSONDecodeError:
                        txt = None
                    if txt:
                        if cur is not None and cur["t1"] is None:
                            # 上轮真正的结束 = 本次提交批(turn_context)之前
                            cur["t1"] = parse_ts(pending_prev
                                                 or last_ts or ts)
                        cur = {"sid": sid, "tid": f"t{len(turns)}",
                               "t0": parse_ts(ts), "t1": None,
                               "input_chars": len(txt), "asst_chars": 0,
                               "text": txt, "cwd": file_cwd}
                        turns.append(cur)
                        pending_prev = None
                        last_ts = ts
                        continue
                if cur is not None:
                    if '"role":"assistant"' in line and len(line) < 300000:
                        # turn_context 之后还有输出 → 那是轮内请求, 不是新提交
                        pending_prev = None
                        try:
                            p = json.loads(line).get("payload") or {}
                        except json.JSONDecodeError:
                            p = {}
                        if p.get("role") == "assistant":
                            for b in p.get("content") or []:
                                if isinstance(b, dict) \
                                        and b.get("type") == "output_text":
                                    cur["asst_chars"] += len(b.get("text")
                                                             or "")
                    elif '"token_count"' in line:
                        try:
                            info = (json.loads(line)
                                    .get("payload", {}).get("info") or {})
                            tot = (info.get("total_token_usage")
                                   or {}).get("total_tokens")
                            if tot:
                                last_total = max(last_total or 0, tot)
                        except json.JSONDecodeError:
                            pass
                    elif '"function_call"' in line:
                        m = re.search(r'"name":"([^"\\]{1,60})"', line)
                        if m:
                            tools[m.group(1)] += 1
                    elif '"turn_context"' in line:
                        if pending_prev is None:
                            pending_prev = last_ts or ts
                        m = re.search(r'"model":"([^"]+)"', line)
                        if m:
                            models[m.group(1)] += 1
                elif '"session_meta"' in line and len(line) < 200000:
                    try:
                        cwd = json.loads(line).get("payload", {}).get("cwd")
                        if cwd:
                            cwd_counter[cwd] += 1
                            file_cwd = cwd
                    except json.JSONDecodeError:
                        pass
                last_ts = ts
        if cur is not None and cur["t1"] is None:
            cur["t1"] = parse_ts(pending_prev or last_ts) \
                if (pending_prev or last_ts) else cur["t0"]
        dedup = []
        for t in turns:            # 合并相邻重复文本(重试产物)
            if dedup and dedup[-1]["text"] == t["text"]:
                dedup[-1]["t1"] = max(dedup[-1]["t1"], t["t1"])
                dedup[-1]["asst_chars"] += t["asst_chars"]
                continue
            dedup.append(t)
        dedup = [t for t in dedup if t["t1"] >= t["t0"]] or dedup
        if dedup:
            sessions[sid] = dedup
        burned += last_total or 0
    return sessions, cwd_counter, {"call_speeds": [], "burned": burned,
                                   "tools": tools, "models": set(models),
                                   "model_counts": models}


# ---------------------------------------------------------------- 数据源 5: Claude Code

CLAUDE_SKIP_PREFIX = (
    "<command", "<local-command", "[Request interrupted", "Caveat:",
    "<system-reminder", "<task-notification",
)


def _ms_dt(v):
    """epoch 秒/毫秒 -> aware datetime"""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v < 1e12:            # 秒
        v *= 1000.0
    from datetime import timezone
    return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)


def _blocks_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def load_claude(claude_home):
    """~/.claude/projects/<dir>/<sid>.jsonl — Claude Code 标准会话记录."""
    files = sorted(glob.glob(os.path.join(claude_home, "projects", "**",
                                          "*.jsonl"), recursive=True))
    if not files:
        return None
    sessions, projects = {}, Counter()
    stats = {"call_speeds": [], "burned": 0, "tools": Counter(),
             "models": set(), "model_counts": Counter()}
    for path in files:
        sid = os.path.basename(path)[:-6]
        turns, cur, last_dt, cwd = [], None, None, None
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if len(line) > 2_000_000:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = d.get("type")
                try:
                    dt = parse_ts(d["timestamp"])
                except (KeyError, ValueError):
                    dt = last_dt
                if d.get("isSidechain") or d.get("isMeta"):
                    last_dt = dt or last_dt
                    continue
                if d.get("cwd"):
                    cwd = d["cwd"]
                msg = d.get("message") or {}
                if typ == "user" and msg.get("role") == "user":
                    txt = _blocks_text(msg.get("content")).strip()
                    txt = re.sub(r"<system-reminder>.*?</system-reminder>",
                                 "", txt, flags=re.S).strip()
                    ok = bool(txt) and not any(
                        txt.startswith(p) for p in CLAUDE_SKIP_PREFIX)
                    if ok:
                        if cur is not None and cur["t1"] is None:
                            cur["t1"] = last_dt or dt
                        cur = {"sid": sid, "tid": "t%d" % len(turns),
                               "t0": dt, "t1": None,
                               "input_chars": len(txt), "asst_chars": 0,
                               "text": txt[:2000], "cwd": cwd}
                        turns.append(cur)
                elif typ == "assistant" and cur is not None:
                    cur["asst_chars"] += len(_blocks_text(msg.get("content")))
                    m = msg.get("model")
                    if m:
                        stats["models"].add(m)
                        stats["model_counts"][m] += 1
                    for b in msg.get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            stats["tools"][b.get("name") or "?"] += 1
                last_dt = dt or last_dt
        if cur is not None and cur["t1"] is None:
            cur["t1"] = last_dt or cur["t0"]
        dedup = []
        for t in turns:
            if dedup and dedup[-1]["text"] == t["text"]:
                dedup[-1]["t1"] = max(dedup[-1]["t1"], t["t1"])
                continue
            dedup.append(t)
        dedup = [t for t in dedup if t["t1"] >= t["t0"]] or dedup
        if dedup:
            sessions[sid] = dedup
            if cwd:
                projects[cwd] += 1
    return sessions, stats, projects


# ---------------------------------------------------------------- 数据源 6: OpenCode

OPENCODE_HOMES = [
    os.path.join(HOME, ".local", "share", "opencode"),
    os.path.join(HOME, "AppData", "Roaming", "opencode"),
    os.path.join(HOME, ".opencode"),
]


def load_opencode():
    """opencode 消息存储, 多种历史布局都探测一遍."""
    base = next((b for b in OPENCODE_HOMES if os.path.isdir(b)), None)
    if base is None:
        return None
    pats = [os.path.join(base, "storage", "message", "**", "*.json"),
            os.path.join(base, "project", "*", "session", "*", "message",
                         "*.json"),
            os.path.join(base, "session", "**", "message", "*.json")]
    msg_files = sorted(set(f for pat in pats for f in glob.glob(
        pat, recursive=True)))
    if not msg_files:
        return "no-storage"          # 装了但没找到会话数据
    sess_meta = {}
    for meta_pat in (os.path.join(base, "storage", "session", "*.json"),
                     os.path.join(base, "project", "*", "session", "*.json")):
        for f in glob.glob(meta_pat):
            try:
                o = json.load(open(f, encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sid = o.get("sessionID") or o.get("id") or \
                os.path.basename(f)[:-5]
            sess_meta[sid] = {"cwd": o.get("directory"),
                              "title": o.get("title")}
    by_sid = {}
    for f in msg_files:
        parts = f.replace("\\", "/").split("/")
        sid = None
        if "message" in parts:
            i = parts.index("message")
            if i >= 1:
                sid = parts[i - 1]
        if sid:
            by_sid.setdefault(sid, []).append(f)
    sessions, projects = {}, Counter()
    stats = {"call_speeds": [], "burned": 0, "tools": Counter(),
             "models": set(), "model_counts": Counter()}
    for sid, files in by_sid.items():
        msgs = []
        for f in files:
            try:
                o = json.load(open(f, encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            dt = _ms_dt((o.get("time") or {}).get("created"))
            if dt is None:
                continue
            role = o.get("role")
            txt = ""
            for part in o.get("parts") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt += part.get("text") or ""
            if role in ("user", "assistant"):
                msgs.append((dt, role, txt))
        msgs.sort(key=lambda x: x[0])
        turns, cur, meta = [], None, sess_meta.get(sid, {})
        for dt, role, txt in msgs:
            if role == "user":
                txt = txt.strip()
                low = txt[:60].lower()
                ok = bool(txt) and not txt.startswith("<") and \
                    "runtime context" not in low
                if ok:
                    if cur is not None and cur["t1"] is None:
                        cur["t1"] = dt
                    cur = {"sid": sid, "tid": "t%d" % len(turns), "t0": dt,
                           "t1": None, "input_chars": len(txt),
                           "asst_chars": 0, "text": txt[:2000],
                           "cwd": meta.get("cwd")}
                    turns.append(cur)
            elif role == "assistant" and cur is not None:
                cur["asst_chars"] += len(txt)
        if cur is not None and cur["t1"] is None:
            cur["t1"] = msgs[-1][0] if msgs else cur["t0"]
        dedup = []
        for t in turns:
            if dedup and dedup[-1]["text"] == t["text"]:
                continue
            dedup.append(t)
        dedup = [t for t in dedup if t["t1"] >= t["t0"]] or dedup
        if dedup:
            sessions[sid] = dedup
            if meta.get("cwd"):
                projects[meta["cwd"]] += 1
    return sessions, stats, projects


# ---------------------------------------------------------------- 数据源 7: DSH

def load_dsh(dsh_home):
    """~/.dsh/sessions/<enc-cwd>/<sid>/session.jsonl.zstd — 事件流式会话."""
    if not os.path.isdir(os.path.join(dsh_home, "sessions")):
        return None
    try:
        import zstandard
    except ImportError:
        return "no-zstd"
    files = sorted(glob.glob(os.path.join(dsh_home, "sessions", "*", "*",
                                          "session.jsonl.zstd")))
    if not files:
        return None
    dec = zstandard.ZstdDecompressor()
    sessions, projects = {}, Counter()
    stats = {"call_speeds": [], "burned": 0, "tools": Counter(),
             "models": set(), "model_counts": Counter()}
    tasks = []
    for path in files:
        sid = os.path.basename(os.path.dirname(path))
        try:
            with open(path, "rb") as fh:
                raw = dec.stream_reader(fh).read()
        except Exception:
            continue
        turns, cur, last_dt = [], None, None
        cwd = title = None
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = o.get("type")
            dt = _ms_dt(o.get("time"))
            if typ == "session":
                cwd = o.get("cwd")
                dt = _ms_dt(o.get("createdAt")) or dt
            elif typ == "session/title":
                title = (o.get("data") or {}).get("title") or title
            elif typ == "request/header":
                m = re.search(r'"model"\s*:\s*"([^"]+)"', line)
                if m:
                    stats["models"].add(m.group(1))
                    stats["model_counts"][m.group(1)] += 1
            elif typ == "user/message":
                txt = _blocks_text((o.get("data") or {}).get("content")).strip()
                low = txt[:60].lower()
                ok = bool(txt) and not txt.startswith("<") and \
                    "runtime context" not in low and \
                    not low.startswith("current dsh")
                if ok:
                    if cur is not None and cur["t1"] is None:
                        cur["t1"] = last_dt or dt
                    cur = {"sid": sid, "tid": "t%d" % len(turns),
                           "t0": dt, "t1": None, "input_chars": len(txt),
                           "asst_chars": 0, "text": txt[:2000], "cwd": cwd}
                    turns.append(cur)
            elif typ == "assistant/message" and cur is not None:
                cur["asst_chars"] += len(_blocks_text(
                    (o.get("data") or {}).get("content")))
            elif typ == "tool/call":
                data = o.get("data") or {}
                nm = data.get("name") or (data.get("tool") or {}).get("name")
                if nm:
                    stats["tools"][nm] += 1
            elif typ == "turn/end":
                if cur is not None and cur["t1"] is None:
                    cur["t1"] = dt or last_dt
            last_dt = dt or last_dt
        if cur is not None and cur["t1"] is None:
            cur["t1"] = last_dt or cur["t0"]
        dedup = []
        for t in turns:
            if dedup and dedup[-1]["text"] == t["text"]:
                dedup[-1]["t1"] = max(dedup[-1]["t1"], t["t1"])
                continue
            dedup.append(t)
        dedup = [t for t in dedup if t["t1"] >= t["t0"]] or dedup
        if dedup:
            sessions[sid] = dedup
            if cwd:
                projects[cwd] += 1
            if title:
                tasks.append({"title": title, "status": "dsh"})
    return sessions, stats, projects, tasks


# ---------------------------------------------------------------- 并发数

def compute_concurrency(log_sessions):
    """并发数: 时间窗重叠的 session 数 (扫一下线) + 按天统计 session/项目数."""
    events = []
    day_sessions, day_projects = {}, {}
    for sid, tl in log_sessions.items():
        if not tl:
            continue
        events.append((tl[0]["t0"], 1))
        events.append((tl[-1]["t1"], -1))
        cwd = tl[0].get("cwd")
        for t in tl:
            d = t["t0"].astimezone().date()
            day_sessions.setdefault(d, set()).add(sid)
            if cwd:
                day_projects.setdefault(d, set()).add(cwd)
    events.sort(key=lambda x: (x[0], x[1]))   # 同刻先结束再开始, 避免贴边误判
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    day_msgs = {}
    for sid, tl in log_sessions.items():
        for t in tl:
            d = t["t0"].astimezone().date()
            day_msgs[d] = day_msgs.get(d, 0) + 1
    daily = [len(v) for v in day_sessions.values()]
    daily_p = [len(v) for v in day_projects.values()]
    top_days = sorted(day_msgs, key=lambda d: -day_msgs[d])[:5]
    return {
        "peak_sessions": peak,
        "median_daily_sessions": statistics.median(daily) if daily else None,
        "max_daily_sessions": max(daily, default=None),
        "median_daily_projects": statistics.median(daily_p) if daily_p else None,
        "max_daily_projects": max(daily_p, default=None),
        "active_days": len(day_sessions),
        "top_days": [{"date": d.strftime("%m-%d"),
                      "msgs": day_msgs.get(d, 0),
                      "sessions": len(day_sessions.get(d, ())),
                      "projects": len(day_projects.get(d, ()))}
                     for d in top_days],
    }


# ---------------------------------------------------------------- 指标

def pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
    return vals[k]


def compute_metrics(log_sessions, rollout_sessions, model_stats, tasks):
    turns_all = [t for tl in log_sessions.values() for t in tl]
    # rollout 正文按 (sid, 近邻时间) 挂到 CLI 轮次上;
    # codex 模式下两个结构是同一批对象, 直接按 id 命中, 避免全量扫描
    text_of = {}
    shared_text = {}
    for sid, tl in rollout_sessions.items():
        for t in tl:
            if t.get("text"):
                text_of[(sid, t["t0"])] = t["text"]
                shared_text[id(t)] = t["text"]

    def find_text(turn):
        if id(turn) in shared_text:
            return shared_text[id(turn)]
        for k, v in text_of.items():
            if k[0] == turn["sid"] \
                    and abs((k[1] - turn["t0"]).total_seconds()) < 5:
                return v
        return None

    typing, thinking, reading_ppm, afk_gaps = [], [], [], []
    digest_rows = []
    for tl in log_sessions.values():
        prev = None
        for cur in tl:
            gap = None
            if prev is not None:
                raw_gap = (cur["t0"] - prev["t1"]).total_seconds()
                if raw_gap > MAX_GAP_S:
                    afk_gaps.append(raw_gap)
                elif raw_gap >= MIN_GAP_S:
                    gap = round(raw_gap, 1)
                    if cur["input_chars"] <= MAX_MSG_TOKENS \
                            and cur["input_chars"] / raw_gap <= PLAUSIBLE_RATE:
                        typing.append(min(cur["input_chars"] / raw_gap,
                                          SPEED_CAP))
            digest_rows.append({
                "sid": cur["sid"],
                "src": cur["sid"].split(":", 1)[0] if ":" in cur["sid"] else "?",
                "time": cur["t0"].astimezone().strftime("%m-%d %H:%M"),
                "gap_s": gap,
                "tokens": cur["input_chars"],
                "text": find_text(cur),
            })
            prev = cur
    digest_rows.sort(key=lambda r: r["time"])
    # 峰值手速用于"打字/思考"时间分解, 下限防止小样本时打字时间吞掉整个间隔
    peak = max(pct(typing, 90) or 0.0, 4.0)

    for tl in log_sessions.values():
        for prev, cur in zip(tl, tl[1:]):
            gap = (cur["t0"] - prev["t1"]).total_seconds()
            if not (MIN_GAP_S < gap <= MAX_GAP_S):
                continue
            if cur["input_chars"] > MAX_MSG_TOKENS:
                continue
            if cur["input_chars"] / gap > PLAUSIBLE_RATE:
                continue          # 排队发送的消息, 打字发生在上轮执行期间
            typing_est = min(cur["input_chars"] / peak, gap)
            think = gap - typing_est
            thinking.append(think)
            if prev["asst_chars"] >= MIN_ASST_CHARS and think > 2.0:
                ppm = prev["asst_chars"] / CHARS_PER_PAGE * 60.0 / think
                if 0.05 < ppm < 600:
                    reading_ppm.append(ppm)

    hours = Counter(t["t0"].astimezone().hour for t in turns_all)
    owl = sum(v for h, v in hours.items() if h < 6) / max(len(turns_all), 1)
    peak_hour = hours.most_common(1)[0][0] if hours else None

    texts = [t["text"] for tl in rollout_sessions.values() for t in tl
             if t.get("text")]
    starters, catch, emoji = Counter(), Counter(), Counter()
    CATCHPHRASES = ["谢谢", "好的", "继续", "不对", "哈哈", "牛逼", "帮我",
                    "为什么", "可以", "直接", "还是", "再"]
    EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    for t in texts:
        s = re.sub(r"[\s\W_]+", "", t)[:4]
        if len(s) >= 2:
            starters[s] += 1
        for w in CATCHPHRASES:
            if w in t:
                catch[w] += 1
        for e in EMOJI_RE.findall(t):
            emoji[e] += 1
    user_tok = sum(r["tokens"] for r in digest_rows)

    days = sorted({t["t0"].astimezone().date() for t in turns_all})

    def r(x, nd=1):
        return None if x is None else round(x, nd)

    return {
        "meta": {
            "sessions": len(log_sessions),
            "turns": len(turns_all),
            "human_turns": len(turns_all),
            "days": len(days),
            "first": min(days).strftime("%Y-%m-%d") if days else None,
            "last": max(days).strftime("%Y-%m-%d") if days else None,
            "tasks": len(tasks),
        },
        "typing": {
            "median_tok_s": r(statistics.median(typing)) if typing else None,
            "p90_tok_s": r(pct(typing, 90)),
            "peak_assumed": r(peak),
            "samples": len(typing),
        },
        "reading": {
            "median_ppm": r(statistics.median(reading_ppm))
                          if reading_ppm else None,
            "p90_ppm": r(pct(reading_ppm, 90)),
            "median_tok_s": r(statistics.median(
                [p * CHARS_PER_PAGE / 60.0 for p in reading_ppm]))
                if reading_ppm else None,
            "samples": len(reading_ppm),
        },
        "ttft": {
            "median_s": r(statistics.median(thinking)) if thinking else None,
            "p90_s": r(pct(thinking, 90)),
            "samples": len(thinking),
            "longest_afk_min": r(max(afk_gaps) / 60.0, 0) if afk_gaps else None,
        },
        "habits": {
            "owl_index": r(owl * 100, 0),
            "peak_hour": peak_hour,
            "hour_hist": {str(h): hours.get(h, 0) for h in range(24)},
            "avg_msg_tokens": r(user_tok / len(turns_all)) if turns_all else None,
            "longest_msg_tokens": max((t["input_chars"] for t in turns_all),
                                      default=0),
            "total_user_tokens": r(user_tok, 0),
            "top_starters": starters.most_common(6),
            "catchphrases": catch.most_common(8),
            "top_emoji": emoji.most_common(5),
        },
        "model_side": {
            "model_tok_s": r(statistics.median(model_stats["call_speeds"]))
                           if model_stats["call_speeds"] else None,
            "tokens_burned": r(model_stats["burned"], 0),
            "tools_top": model_stats["tools"].most_common(8),
            "models": sorted(model_stats["models"]),
            "model_top": (model_stats.get("model_counts")
                          or Counter()).most_common(8),
        },
        "digest": digest_rows,
        "tasks": tasks,
    }


def _new_stats():
    return {"call_speeds": [], "burned": 0, "tools": Counter(),
            "models": set(), "model_counts": Counter()}


def _merge_stats(a, b):
    a["call_speeds"] += b.get("call_speeds") or []
    a["burned"] += b.get("burned") or 0
    a["tools"] += b.get("tools") or Counter()
    a["models"] |= set(b.get("models") or ())
    a["model_counts"] += b.get("model_counts") or Counter()


def probe_harnesses(args, harnesses):
    """逐个 harness 探测会话数据, 统一成 {前缀:sid: [turn,...]} 后合并."""
    merged, stats = {}, _new_stats()
    projects, tasks = Counter(), []
    found, notes = {}, []

    def absorb(name, sessions, mstat, projs, tsks=None):
        for sid, tl in sessions.items():
            merged[f"{name}:{sid}"] = tl
        _merge_stats(stats, mstat)
        projects.update(projs)
        tasks.extend(tsks or [])
        found[name] = len(sessions)

    for h in harnesses:
        try:
            if h == "zcode":
                log_sessions, n_log, agent_sids = load_log_turns(args.log)
                if not log_sessions:
                    continue
                rollout_sessions, n_roll, mstat = load_rollout(args.rollout)
                # 把 rollout 里抢救到的正文挂回 log 轮次 (近邻 5s)
                for sid, tl in rollout_sessions.items():
                    for rt in tl:
                        if not rt.get("text"):
                            continue
                        for t in log_sessions.get(sid, []):
                            if abs((t["t0"] - rt["t0"]).total_seconds()) < 5:
                                t["text"] = rt["text"]
                                break
                ztasks, workspaces = load_task_titles(args.tasks_db)
                for sid, tl in log_sessions.items():
                    for t in tl:
                        t["cwd"] = workspaces.get(sid)
                absorb("zcode", log_sessions, mstat,
                       Counter(w for w in workspaces.values()), ztasks)
            elif h == "codex":
                r = load_codex(args.codex_home)
                if not r or not r[0]:
                    continue
                sess, cwds, mstat = r
                absorb("codex", sess, mstat, cwds,
                       [{"title": c, "status": "%d session" % n}
                        for c, n in cwds.most_common(30)])
            elif h == "claude":
                r = load_claude(os.path.join(HOME, ".claude"))
                if not r:
                    continue
                sess, mstat, projs = r
                absorb("claude", sess, mstat, projs,
                       [{"title": c, "status": "%d session" % n}
                        for c, n in projs.most_common(30)])
            elif h == "opencode":
                r = load_opencode()
                if r is None:
                    continue
                if r == "no-storage":
                    notes.append("opencode: 检测到安装, 但没找到会话存储")
                    continue
                sess, mstat, projs = r
                absorb("opencode", sess, mstat, projs)
            elif h == "dsh":
                r = load_dsh(os.path.join(HOME, ".dsh"))
                if r is None:
                    continue
                if r == "no-zstd":
                    notes.append("dsh: 会话为 zstd 压缩, 请 pip install zstandard")
                    continue
                sess, mstat, projs, dtasks = r
                absorb("dsh", sess, mstat, projs, dtasks)
        except Exception as e:                       # 单个坏了不拖累整体
            notes.append("%s: 解析失败 %s: %s" % (h, type(e).__name__, e))
    return merged, stats, projects, tasks, found, notes


def cmd_analyze(args):
    harnesses = ([args.source] if args.source != "auto"
                 else ["zcode", "codex", "claude", "opencode", "dsh"])
    merged, stats, projects, tasks, found, notes = probe_harnesses(
        args, harnesses)
    if not merged:
        print("[!] 没有在任何 harness (%s) 下解析到人类轮次" % ",".join(harnesses))
        for n in notes:
            print("    - " + n)
        sys.exit(1)
    m = compute_metrics(merged, merged, stats, tasks[:60])
    m["concurrency"] = compute_concurrency(merged)
    m["projects_top"] = projects.most_common(12)
    m["meta"]["sources"] = found
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "metrics.json"), "w",
              encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)

    rows = m["digest"][-150:]
    with open(os.path.join(args.out, "digest.md"), "w", encoding="utf-8") as f:
        f.write("# 用户消息摘要 (供能力评估, 最近 150 条)\n\n")
        f.write("harness: %s | session 数: %d | 人类消息: %d | 跨度: %s ~ %s | 任务/项目: %d\n\n" % (
            " + ".join("%s %d" % (k, v) for k, v in found.items()),
            m["meta"]["sessions"], m["meta"]["human_turns"],
            m["meta"]["first"], m["meta"]["last"], len(tasks)))
        if tasks:
            f.write("## 任务/工作目录 (能力评估证据)\n\n")
            for t in tasks[:40]:
                f.write("- [%s] %s\n" % (t["status"], t["title"]))
            f.write("\n")
        f.write("## 消息流 (gap=距上轮回复间隔)\n\n")
        for row in rows:
            gap = "%ss" % row["gap_s"] if row["gap_s"] else "首发/挂机后"
            text = (row["text"] or "(正文已随日志轮转丢失)").replace("\n", " ")
            f.write("- `[%s] %s` (间隔 %s, %d 字) %s\n" % (
                row["src"], row["time"], gap, row["tokens"], text[:240]))
        f.write("\n## 自动统计\n\n```json\n")
        fun = {k: v for k, v in m.items() if k != "digest"}
        f.write(json.dumps(fun, ensure_ascii=False, indent=1))
        f.write("\n```\n")

    print("[ok] metrics.json + digest.md 已写入 %s" % os.path.abspath(args.out))
    print("     harness 探测: %s" %
          " + ".join("%s %d session" % (k, v) for k, v in found.items()))
    for n in notes:
        print("     [i] " + n)
    print("     人类消息 %d 条 / %d 个 session / 跨 %d 天" % (
        m["meta"]["human_turns"], m["meta"]["sessions"], m["meta"]["days"]))
    print("     打字 %s tok/s (中位, %d 样本) | 阅读 %s pp (%d 样本) | "
          "TTFT %ss (%d 样本)" % (
              m["typing"]["median_tok_s"], m["typing"]["samples"],
              m["reading"]["median_ppm"], m["reading"]["samples"],
              m["ttft"]["median_s"], m["ttft"]["samples"]))


# ---------------------------------------------------------------- 报告

def radar_svg(scores):
    cx = cy = 300
    R = 195
    n = len(DIMENSIONS)
    pts = []
    for i, (key, label) in enumerate(DIMENSIONS):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        x = cx + R * math.cos(ang)
        y = cy + R * math.sin(ang)
        pts.append((x, y, label, scores.get(key, 0)))
    svg = [f'<svg viewBox="0 0 600 600" '
           f'xmlns="http://www.w3.org/2000/svg" role="img">']
    for frac in (0.25, 0.5, 0.75, 1.0):
        ring = " ".join(f"{cx + R * frac * math.cos(-math.pi/2 + 2*math.pi*i/n)},"
                        f"{cy + R * frac * math.sin(-math.pi/2 + 2*math.pi*i/n)}"
                        for i in range(n))
        strong = ' stroke="#b7c2dc" stroke-width="1.5"' if frac == 1.0 \
            else ' stroke="#dbe2f0" stroke-width="1"'
        svg.append(f'<polygon points="{ring}" fill="none"{strong}/>')
    for x, y, label, _ in pts:
        svg.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                   f'stroke="#e2e8f4" stroke-width="1"/>')
    poly, dots = [], []
    for i, (x, y, label, score) in enumerate(pts):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        rr = R * max(3, min(100, score)) / 100.0
        px = cx + rr * math.cos(ang)
        py = cy + rr * math.sin(ang)
        poly.append(f"{px:.1f},{py:.1f}")
        dots.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="#0c5f78"/>')
        lx = cx + (R + 30) * math.cos(ang)
        ly = cy + (R + 30) * math.sin(ang)
        anchor = "middle"
        svg.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
                   f'fill="#3b4a6b" font-size="19">{label}</text>')
        svg.append(f'<text x="{lx:.1f}" y="{ly + 21:.1f}" text-anchor="{anchor}" '
                   f'fill="#0c5f78" font-size="17" font-weight="bold">'
                   f'{int(score)}</text>')
    svg.append(f'<polygon points="{" ".join(poly)}" fill="rgba(12,95,120,0.16)" '
               f'stroke="#0c5f78" stroke-width="2.5"/>')
    svg.extend(dots)
    svg.append(f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" '
               f'fill="#8b97b5" font-size="13">0.3B</text>')
    svg.append("</svg>")
    return "\n".join(svg)


def verdict(key, val):
    if val is None:
        return "样本不足，拒绝评价 (模型保守行为)"
    if key == "tok_s":
        if val < 2: return "低延迟模式关闭 —— 你打字像在写毛笔字"
        if val < 5: return "稳态输出，胜在持久，能耗比极佳"
        if val < 8: return "人形流式输出，前端不用转圈等你"
        return "键盘冒烟，建议自查是否有粘贴作弊"
    if key == "ppm":
        if val < 1: return "逐字精读派，LLM 的 prefill 看了摇头"
        if val < 3: return "正常人类阅读速度，符合出厂设定"
        if val < 10: return "速读忍者，或者只是挑着看"
        return "检测到幻觉性跳读：你根本没读完对吧"
    if key == "ttft":
        if val < 10: return "反射弧极短，金鱼看了都点赞"
        if val < 60: return "正常思考延迟，温度适中"
        if val < 300: return "R1 级深度思考，请等待思维链展开"
        return "Ultra-Thinking 模式：离线推理中，请勿打扰"
    return ""


def bar(pct_fill, color, label_right=""):
    w = max(1.5, min(100, pct_fill))
    return (f'<div class="bar"><div class="bar-fill" style="width:{w}%;'
            f'background:{color}"></div><span>{label_right}</span></div>')


def cmd_report(args):
    out = args.out
    mpath = os.path.join(out, "metrics.json")
    spath = os.path.join(out, "scores.json")
    for p, hint in ((mpath, "先运行 bench.py analyze"), (spath, None)):
        if not os.path.isfile(p):
            print(f"[!] 缺少 {p}" + (f" ({hint})" if hint else
                  " (按 references/rubric.md 打分后写入)"))
            sys.exit(1)
    m = json.load(open(mpath, encoding="utf-8"))
    s = json.load(open(spath, encoding="utf-8"))

    dims = s["dimensions"]
    conc = m.get("concurrency") or {}
    overall = statistics.mean(
        dims.get(k, {}).get("score", 50) for k, _ in DIMENSIONS)
    tier = tier_of(overall)

    def dim_row(key, label):
        d = dims.get(key) or {"score": 50, "comment": "未评分"}
        t = tier_of(d["score"])
        return (f'<tr><td class="dim">{label}</td>'
                f'<td class="score">{int(d["score"])}</td>'
                f'<td>{t["name"]}</td>'
                f'<td class="cmt">{d.get("comment","")}</td></tr>')

    t_med = m["typing"]["median_tok_s"]
    r_med = m["reading"]["median_ppm"]
    f_med = m["ttft"]["median_s"]
    model_tok = m["model_side"]["model_tok_s"]
    ref_decode = model_tok or 60.0
    hb = m["habits"]
    msd = m["model_side"]
    srcs = m.get("meta", {}).get("sources") or {}
    src_str = " + ".join("%s %d session" % (k, v) for k, v in srcs.items()) \
        or "本地 session 记录"

    def fmt_cn(n):
        if n is None:
            return "—"
        if n >= 1e8:
            return f"{n/1e8:.1f} 亿"
        if n >= 1e4:
            return f"{n/1e4:.1f} 万"
        return f"{n:,}"

    def base(p):
        return str(p).rstrip("\\/").replace("\\", "/").split("/")[-1] or p

    def tile(v, k):
        return f'<div class="tile"><div class="v">{v}</div><div class="k">{k}</div></div>'

    days = m["meta"]["days"] or 1
    daily_msgs = m["meta"]["human_turns"] / days
    tiles = "".join([
        tile(m["meta"]["human_turns"], "人类消息总数"),
        tile(m["meta"]["sessions"], "session 总数"),
        tile(f"{m['meta']['days']} 天", f"活跃天数 ({m['meta']['first']} 起)"),
        tile(f"{daily_msgs:.0f} 条/天", "日均消息量"),
        tile(fmt_cn(hb.get("total_user_tokens")), "你的累计输出(字)"),
        tile(fmt_cn(msd.get("tokens_burned")), "烧掉的 tokens"),
        tile(conc.get("peak_sessions") or "—", "峰值并发 session"),
        tile(f"{m['ttft'].get('longest_afk_min') or '—'} 分钟", "最长挂机"),
    ])

    # 24 小时活跃分布
    hist = hb.get("hour_hist") or {}
    hmax = max(hist.values(), default=0) or 1
    hcols, hlabels = [], []
    for h in range(24):
        v = hist.get(str(h), 0)
        cls = "hcol peak" if h == hb.get("peak_hour") else \
              ("hcol owl" if h < 6 and v else "hcol")
        hcols.append(f'<div class="{cls}" style="height:{max(v/hmax*100,2):.0f}%"'
                     f' title="{h}点: {v} 条"></div>')
        hlabels.append(f"<span>{h if h % 3 == 0 else ''}</span>")
    hours_html = (f'<div class="hours">{"".join(hcols)}</div>'
                  f'<div class="hlabels">{"".join(hlabels)}</div>')

    # 条形榜 (项目 / 工具 / 口头禅)
    def hbars(pairs, unit="", color="#0c5f78"):
        mx = max((c for _, c in pairs), default=0) or 1
        return "".join(
            f'<div class="hrow"><div class="hname" title="{n}">{base(n) if "/" in str(n) or chr(92) in str(n) else n}</div>'
            f'<div class="htrack"><div class="hfill" style="width:{c/mx*100:.0f}%;background:{color}"></div></div>'
            f'<div class="hval">{c}{unit}</div></div>'
            for n, c in pairs)

    projects = hbars((m.get("projects_top") or [])[:10], " session")
    tools_bars = hbars((msd.get("tools_top") or [])[:8], " 次", "#8a5cf5")
    words_bars = hbars((hb.get("catchphrases") or [])[:8], " 次", "#db2777")
    model_line = "、".join(f"{n} ×{c}" for n, c in (msd.get("model_top") or [])[:6])
    busy_rows = "".join(
        f'<tr><td>{d["date"]}</td><td class="score">{d["msgs"]}</td>'
        f'<td>{d["sessions"]}</td><td>{d["projects"]}</td></tr>'
        for d in (conc.get("top_days") or []))

    tools = "".join(f'<div class="chip">🔧 {n} × {c}</div>'
                    for n, c in (msd.get("tools_top") or [])[:6]) or \
        '<div class="chip">无 (你居然没使唤过工具?)</div>'
    starters = "、".join(f"「{w}」×{c}" for w, c in hb.get("top_starters", [])[:5])

    dims_rows = "".join(dim_row(k, lbl) for k, lbl in DIMENSIONS)

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>碳基模型跑分报告 · {s.get("model_name","Homo-?B")}</title>
<style>
 :root {{ --bg:#f4f6fb; --card:#ffffff; --line:#e3e8f2; --tx:#1e2638;
          --sub:#51618a; --dim:#8b97b5; --acc:#0c5f78; --hot:#db2777; }}
 * {{ box-sizing:border-box; }}
 html, body {{ margin:0; height:100%; }}
 body {{ background:var(--bg); color:var(--tx);
        font-family:"Microsoft YaHei","PingFang SC",sans-serif; font-size:13px; }}
 .wrap {{ max-width:1920px; margin:0 auto; padding:12px 16px 8px;
          height:100vh; min-height:720px; display:flex; flex-direction:column; gap:10px; }}
 .head {{ display:flex; align-items:center; gap:16px; flex-wrap:wrap; }}
 h1 {{ font-size:19px; margin:0; }}
 .model {{ color:var(--sub); font-size:13.5px; }}
 .model b {{ color:var(--tx); }}
 .badge {{ background:linear-gradient(90deg,#06b6d4,#ec4899); color:#fff;
           font-weight:700; border-radius:8px; padding:3px 12px; font-size:13px; }}
 .bento {{ flex:1; min-height:0; display:grid; gap:10px;
   grid-template-columns:repeat(12,1fr);
   grid-template-rows:auto auto minmax(0,1.05fr) minmax(0,0.95fr); }}
 .tiles {{ grid-column:1/-1; display:grid; grid-template-columns:repeat(8,1fr); gap:10px; }}
 .tile {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:12px 14px; box-shadow:0 1px 3px rgba(24,34,61,0.05); }}
 .tile .v {{ font-size:24px; font-weight:700; color:var(--acc); }}
 .tile .k {{ font-size:11.5px; color:var(--dim); margin-top:3px; }}
 .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:12px 14px; box-shadow:0 1px 3px rgba(24,34,61,0.05);
          overflow:auto; min-height:0; }}
 .c-name  {{ grid-column:1/6;  grid-row:2; border-top:3px solid #0c5f78; }}
 .c-perf  {{ grid-column:6/-1; grid-row:2; border-top:3px solid #db2777;
             display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
 .c-radar {{ grid-column:1/4;  grid-row:3/5; border-top:3px solid #8a5cf5;
             display:flex; flex-direction:column; }}
 .c-table {{ grid-column:4/10; grid-row:3; border-top:3px solid #0c5f78; }}
 .c-clock {{ grid-column:10/-1; grid-row:3; border-top:3px solid #d97706; }}
 .c-final {{ grid-column:4/7;  grid-row:4; border-top:3px solid #db2777; }}
 .c-port  {{ grid-column:7/10; grid-row:4; border-top:3px solid #6b84b8; }}
 .c-proj  {{ grid-column:10/-1; grid-row:4; border-top:3px solid #059669; }}
 .pt {{ font-size:13px; font-weight:700; color:var(--tx); margin:0 0 8px; }}
 .np-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:2px 40px; }}
 .np-row {{ display:flex; align-items:baseline; gap:14px; padding:8px 0;
            border-bottom:1px dashed var(--line); font-size:13.5px; }}
 .np-k {{ color:var(--acc); font-weight:700; min-width:120px; flex:none; }}
 .np-v {{ color:var(--sub); }}
 .metric .v {{ font-size:26px; font-weight:700; color:var(--acc); }}
 .metric .u {{ color:var(--dim); font-size:11px; margin-top:1px; }}
 .metric .vd {{ color:var(--sub); font-size:12px; margin-top:4px; }}
 .bar {{ position:relative; background:#e9edf6; border-radius:6px;
         height:12px; margin:6px 0 4px; }}
 .bar-fill {{ height:100%; border-radius:6px; }}
 .bar span {{ position:absolute; right:6px; top:-3px; font-size:10.5px;
              color:var(--sub); }}
 .mini {{ font-size:11.5px; color:var(--sub); margin:4px 0; line-height:1.7; }}
 .mini b {{ color:var(--tx); }}
 table {{ width:100%; border-collapse:collapse; font-size:13px; }}
 th,td {{ padding:6px 9px; border-bottom:1px solid var(--line); text-align:left; }}
 th {{ color:var(--sub); font-weight:600; font-size:12px; }}
 td.score {{ color:var(--acc); font-weight:700; }}
 td.dim {{ font-weight:600; white-space:nowrap; }}
 td.cmt {{ color:var(--sub); }}
 tr.total td {{ border-top:2px solid var(--acc); color:var(--hot); font-weight:700; }}
 .hours {{ display:flex; align-items:flex-end; gap:3px; height:68px; }}
 .hcol {{ flex:1; background:#a8cfe0; border-radius:2px 2px 0 0; min-height:2px; }}
 .hcol.owl {{ background:#6b84b8; }}
 .hcol.peak {{ background:linear-gradient(180deg,#ec4899,#db2777); }}
 .hlabels {{ display:flex; gap:3px; margin-top:3px; }}
 .hlabels span {{ flex:1; text-align:center; font-size:9px; color:var(--dim); }}
 .hrow {{ display:grid; grid-template-columns:minmax(80px,42%) 1fr 54px; gap:8px;
          align-items:center; font-size:12.5px; margin:5px 0; }}
 .hname {{ color:var(--sub); overflow:hidden; text-overflow:ellipsis;
           white-space:nowrap; }}
 .htrack {{ background:#e9edf6; height:12px; border-radius:5px; overflow:hidden; }}
 .hfill {{ height:100%; border-radius:5px; }}
 .hval {{ color:var(--sub); font-size:11px; text-align:right; }}
 .comment {{ font-size:13.5px; line-height:1.95; }}
 details {{ margin-top:8px; color:var(--dim); font-size:11px; }}
 .radle {{ flex:1; display:flex; justify-content:center; align-items:center;
           min-height:0; }}
 .radle svg {{ height:100%; width:auto; max-width:100%; }}
 .foot {{ color:var(--dim); font-size:10.5px; line-height:1.6; }}
 @media (max-width:1500px) {{
   .wrap {{ height:auto; }}
   .bento {{ grid-template-columns:repeat(6,1fr); grid-template-rows:none; }}
   .tiles {{ grid-column:1/-1; grid-template-columns:repeat(4,1fr); }}
   .c-name,.c-perf,.c-radar,.c-table,.c-clock,.c-final,.c-port,.c-proj
     {{ grid-column:auto; grid-row:auto; }}
   .c-name {{ grid-column:1/-1; }}
   .c-perf {{ grid-column:1/-1; }}
   .c-radar {{ grid-column:span 2; min-height:420px; }}
   .c-table {{ grid-column:span 4; }}
   .c-clock,.c-final,.c-port,.c-proj {{ grid-column:span 3; }}
   .radle svg {{ height:auto; width:100%; }}
 }}
</style></head><body><div class="wrap">
<div class="head">
 <h1>🧬 碳基模型跑分报告</h1>
 <span class="model">被评测对象: 你 · 型号 <b>{s.get("model_name","?")}</b></span>
 <span class="badge">{tier["name"]}</span>
 <span class="model">综合 <b>{tier["score"]}</b>/100 · {tier["blurb"]}</span>
</div>

<div class="bento">
 <div class="tiles">{tiles}</div>

 <div class="card c-name">
  <div class="pt">🔩 设备铭牌（出厂规格，不可退换）</div>
  <div class="np-grid">
   <div class="np-row"><span class="np-k">参数量</span>
    <span class="np-v">≈ {int(overall)}B（按突触随便换算的）</span></div>
   <div class="np-row"><span class="np-k">并发数</span>
    <span class="np-v">峰值 {conc.get('peak_sessions') or '—'} session · 日均 {conc.get('median_daily_sessions') or '—'} 个 · 单日最多 {conc.get('max_daily_projects') or '—'} 项目</span></div>
   <div class="np-row"><span class="np-k">训练数据</span>
    <span class="np-v">前半生</span></div>
   <div class="np-row"><span class="np-k">知识截止</span>
    <span class="np-v">昨晚睡前</span></div>
   <div class="np-row"><span class="np-k">推理温度</span>
    <span class="np-v">由奶茶摄入量动态调节</span></div>
   <div class="np-row"><span class="np-k">部署形态</span>
    <span class="np-v">单机单卡（就是你这台身子）</span></div>
  </div>
 </div>

 <div class="card c-perf">
  <div class="metric">
   <div class="v">{t_med if t_med is not None else "—"}</div>
   <div class="u">tok/s · 打字输出速度（中位, {m['typing']['samples']} 样本, P90 {m['typing']['p90_tok_s'] or '—'}）</div>
   {bar(t_med / ref_decode * 100 if t_med else 2, '#0c5f78', f'vs {ref_decode:.0f} tok/s 解码')}
   <div class="vd">{verdict('tok_s', t_med)}</div></div>
  <div class="metric">
   <div class="v">{r_med if r_med is not None else "—"}</div>
   <div class="u">pp · 阅读速度 页/分（{m['reading']['samples']} 样本, 1页=500字）</div>
   {bar(min(r_med / 60 * 100, 100) if r_med else 2, '#db2777', 'vs 模型 prefill')}
   <div class="vd">{verdict('ppm', r_med)}</div></div>
  <div class="metric">
   <div class="v">{f_med if f_med is not None else "—"}</div>
   <div class="u">s · 首字时延（{m['ttft']['samples']} 样本, P90 {m['ttft']['p90_s'] or '—'}s）</div>
   {bar(min(f_med / 300 * 100, 100) if f_med else 2, '#d97706', 'vs 模型 0.5s TTFT')}
   <div class="vd">{verdict('ttft', f_med)}</div></div>
 </div>

 <div class="card c-radar">
  <div class="pt">🕸️ 八维能力图</div>
  <div class="radle">{radar_svg({k: dims.get(k, {}).get("score", 50) for k, _ in DIMENSIONS})}</div>
  <div class="mini" style="text-align:center">中心 0 = 0.3B 随机鹦鹉 → 满环 100 = Claude Code Fable 5.1 神话级</div>
 </div>

 <div class="card c-table">
  <div class="pt">📊 评价表 · 每项 ≈ 哪个模型</div>
  <table><tr><th>维度</th><th>得分</th><th>≈ 模型段位</th><th>锐评</th></tr>
  {dims_rows}
  <tr class="total"><td>综合</td><td class="score">{tier["score"]}</td>
  <td>{tier["name"]}</td><td>{tier["blurb"]}</td></tr></table>
 </div>

 <div class="card c-clock">
  <div class="pt">🕐 昼夜节律 & 🔥 忙日</div>
  {hours_html}
  <div class="mini"><b>夜猫子指数</b> {m['habits']['owl_index']}%（0-6点, 蓝柱） ·
   高峰 <b>{m['habits']['peak_hour']} 点</b>（粉柱） ·
   每条 <b>{m['habits']['avg_msg_tokens'] or '—'}</b> 字 · 开口禅: {starters or "无"}</div>
  <table><tr><th>最忙日期</th><th>消息</th><th>sess</th><th>项目</th></tr>
  {busy_rows or '<tr><td colspan="4" class="cmt">无数据</td></tr>'}</table>
  <div class="mini"><b>模型</b>: {model_line or '—'}</div>
 </div>

 <div class="card c-final">
  <div class="pt">💬 总评</div>
  <div class="comment">{s.get("overall_comment","")}
   <details><summary>指标口径（点了会更不信）</summary>
    tok/s = 字数 ÷ 距上轮间隔（含思考下界, 粘贴截断 {int(SPEED_CAP)}）·
    pp = 上轮字数 ÷ (间隔-打字), 500字=1页 · TTFT = 间隔 - 打字时间 ·
    &gt;30min 按挂机剔除 · 并发 = 时间窗重叠 session 峰值 ·
    能力分 AI 主观打分, 有幻觉, 不可复现。
   </details></div>
 </div>

 <div class="card c-port">
  <div class="pt">🎨 行为画像</div>
  <div class="mini"><b>高频指令词</b></div>
  {words_bars or '<div class="mini">无</div>'}
  <div class="mini" style="margin-top:8px"><b>最爱使唤的工具</b></div>
  {tools_bars or '<div class="mini">无</div>'}
 </div>

 <div class="card c-proj">
  <div class="pt">🗂️ 项目全景</div>
  {hbars((m.get('projects_top') or [])[:6], ' 个', '#059669') or '<div class="mini">无项目数据</div>'}
  <div class="mini" style="margin-top:6px">session 投入 Top6 · 悬停看完整路径</div>
 </div>
</div>

<div class="foot">⚠️ 本报告 100% 娱乐向: 指标口径极不严谨, 能力评分是 AI 的主观印象, 段位对照纯属玩梗, 不构成任何消费/招聘/婚恋建议 ·
 数据来源: {src_str}（{m['meta']['human_turns']} 条消息 / {m['meta']['sessions']} 个 session / {m['meta']['days']} 个活跃日） ·
 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</div></body></html>"""

    path = os.path.join(out, "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 报告已生成: {os.path.abspath(path)}")


def main():
    ap = argparse.ArgumentParser(description="human-benchmark (纯娱乐)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="解析 session 日志, 产出 metrics + digest")
    a.add_argument("--source",
                   choices=["auto", "zcode", "codex", "claude", "opencode",
                            "dsh"], default="auto")
    a.add_argument("--codex-home", default=os.path.join(HOME, ".codex"))
    a.add_argument("--log", default=DEFAULT_LOG)
    a.add_argument("--rollout", default=DEFAULT_ROLLOUT)
    a.add_argument("--tasks-db", default=DEFAULT_TASKS_DB)
    a.add_argument("--out", default="bench-out")
    a.set_defaults(func=cmd_analyze)
    r = sub.add_parser("report", help="合成 HTML 报告")
    r.add_argument("--out", default="bench-out")
    r.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
