"""EvalScope 压测集成核心：数据集生成、命令行构造、结果解析、指标聚合。

集成方式：仅调用 evalscope 的**命令行入口**（`evalscope perf ...`），不 import 其 Python
包。evalscope 依赖较重（modelscope / transformers 等），子进程方式可完全隔离依赖。

evalscope 1.13 关键行为（本项目依赖的部分）：
- 自定义提问用 `--dataset custom_multi_turn --dataset-path <jsonl> --multi-turn`；
  数据集每行是一条 OpenAI messages JSON 数组，其中 assistant 消息仅作**轮次分界**
  （内容被丢弃，真实回答由 runner 追加），因此把同一段提问重复 N 轮即等价于多轮追问；
- `--parallel` / `--number` 支持列表：一个进程即可扫描多个并发档位（阶梯压测）。
  多轮模式下 `--number` 表示**会话数**，`--parallel` 表示并发会话数，
  单档请求数 = 会话数 × 轮次；
- 结果落盘为 SQLite：每个档位目录下 `benchmark_data.db`，表名 `result`，逐行提交
  （`--db-commit-interval` 控制缓冲行数），因此可边跑边读做实时进度；
  `--enable-progress-tracker` 另写 progress.json（processed/total 计数）。
"""
import json
import os
import random
import re
import shutil
import sqlite3
import statistics
import string
import subprocess
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

CLI_NAME = "evalscope"
API_NAME = "openai"                       # 被测服务统一按 OpenAI 兼容协议访问
DATASET_NAME = "custom_multi_turn"        # 自定义会话数据集（支持多轮）
DEFAULT_API_PATH = "/v1/chat/completions"
RESULT_DB_NAME = "benchmark_data.db"
RUN_NAME = "perf"
# 逐行提交的缓冲行数：越小实时性越好，越大写入开销越小。
# 取 1 是为了让运行中的进度/明细能立即读到（evalscope 的写入在独立线程，开销可控）。
DB_COMMIT_INTERVAL = 1
# unique_prefix 打开时，每条请求注入的随机前缀长度（字符）
UNIQUE_PREFIX_CHARS = 1024
# 单条 prompt 的长度上限（字符，evalscope 默认值）
MAX_PROMPT_LENGTH = 131072

_PROBE = {"at": 0.0, "info": None}
_PROBE_TTL = 60.0

_LEVEL_RE = re.compile(r"parallel_(\d+)_number_(\d+)")


# --------------------------------------------------------------------- 可执行文件
def resolve_bin():
    """定位 evalscope：环境变量 > PATH > 常见安装位置。"""
    raw = (os.environ.get("EVALSCOPE_BIN") or "").strip()
    if raw:
        if os.path.isfile(raw) and os.access(raw, os.X_OK):
            return raw
        return shutil.which(raw)
    found = shutil.which(CLI_NAME)
    if found:
        return found
    for cand in ("~/.local/bin/evalscope", "/usr/local/bin/evalscope"):
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def command_prefix():
    """命令前缀：优先 PATH 中的 evalscope，退化为 `python -m evalscope`。"""
    path = resolve_bin()
    if path:
        return [path]
    return [sys.executable, "-m", CLI_NAME]


def probe(force=False):
    """探测 evalscope 是否可用（60s 缓存，供 /api/meta 用）。"""
    now = time.time()
    if not force and _PROBE["info"] and now - _PROBE["at"] < _PROBE_TTL:
        return _PROBE["info"]
    path = resolve_bin()
    info = {"available": False, "path": path, "version": None, "error": None}
    try:
        proc = subprocess.run(command_prefix() + ["--version"], capture_output=True,
                              text=True, timeout=30)
        out = ((proc.stdout or "").strip() or (proc.stderr or "").strip())
        if proc.returncode == 0:
            info["available"] = True
            info["version"] = out.replace(CLI_NAME, "").strip() or out
            if not path:
                info["path"] = "{} -m {}".format(sys.executable, CLI_NAME)
        else:
            info["error"] = "evalscope --version 退出码 {}: {}".format(proc.returncode, out[:200])
    except FileNotFoundError:
        info["error"] = ("未找到 evalscope；请先安装（pip install evalscope），"
                         "或用环境变量 EVALSCOPE_BIN 指定完整路径")
    except Exception as e:  # noqa: BLE001
        info["error"] = "{}: {}".format(type(e).__name__, str(e)[:200])
    _PROBE.update({"at": now, "info": info})
    return info


# --------------------------------------------------------------------- 配置换算
def normalize_endpoint(endpoint):
    """归一化为完整的 chat/completions 地址（evalscope 不会自动补全路径）。"""
    raw = (endpoint or "").strip()
    if not raw:
        raise ValueError("API 端点不能为空")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("API 端点格式不合法: " + endpoint)
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        pass
    elif not path:
        path = DEFAULT_API_PATH                   # 只填端口：补 /v1/chat/completions
    else:
        path = path + "/chat/completions"         # /v1、/api/xxx/v4 等前缀：只补后缀
    return "{}://{}{}".format(parts.scheme, parts.netloc, path)


def conversation_messages(cfg):
    """构造一条会话（数据集的一行）：system + 重复 turns 次的同一段提问。

    末尾的 assistant 占位消息只用于闭合最后一轮（evalscope 会把 assistant 消息当作
    轮次分界并丢弃其内容）。
    """
    turns = max(1, int(cfg.get("turns") or 1))
    prompt = (cfg.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提问内容不能为空")
    messages = []
    system = (cfg.get("system_prompt") or "").strip()
    if system:
        messages.append({"role": "system", "content": system})
    for _ in range(turns):
        messages.append({"role": "user", "content": prompt})
        messages.append({"role": "assistant", "content": ""})
    return messages


def _prefix_text(chars):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(max(1, int(chars))))


def _inject_prefix(messages):
    """把随机前缀插到第一条 user 消息前，避免完全相同的 prompt 命中 KV cache。"""
    for msg in messages:
        if msg.get("role") == "user":
            msg["content"] = _prefix_text(UNIQUE_PREFIX_CHARS) + "\n" + msg["content"]
            return


def levels(cfg):
    """并发档位列表；未启用阶梯时返回单档。"""
    raw = cfg.get("ramp_levels") or []
    if raw:
        return sorted({max(1, int(x)) for x in raw})
    return [max(1, int(cfg.get("concurrent_users") or 1))]


def conversation_count(cfg, users):
    """单个并发档位的会话数（evalscope 多轮模式下 --number 表示会话数）。"""
    return max(1, int(users)) * max(1, int(cfg.get("iterations") or 1))


def expected_requests(cfg):
    """本次压测预计发出的请求总数（= 会话数 × 轮次），用于计算进度百分比。"""
    turns = max(1, int(cfg.get("turns") or 1))
    return sum(conversation_count(cfg, u) for u in levels(cfg)) * turns


# --------------------------------------------------------------------- 数据集 / 命令行
def build_dataset(cfg, path):
    """写 evalscope 数据集（JSONL，每行一条会话）。

    evalscope 会循环使用数据集，因此默认只写一行即可；打开 unique_prefix 时按预计
    会话数写同样多行，让每条请求都带不同的随机前缀。
    """
    rows = max(conversation_count(cfg, u) for u in levels(cfg))
    if not bool(cfg.get("unique_prefix")):
        rows = 1
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(rows):
            messages = conversation_messages(cfg)
            if rows > 1:
                _inject_prefix(messages)
            f.write(json.dumps(messages, ensure_ascii=False) + "\n")
    return path


def build_command(cfg, run_dir, dataset_path):
    """构造 `evalscope perf` 命令行参数。"""
    model = (cfg.get("model") or "").strip()
    if not model:
        raise ValueError("模型名不能为空")
    users = levels(cfg)
    numbers = [conversation_count(cfg, u) for u in users]

    cmd = command_prefix() + [
        "perf",
        "--api", API_NAME,
        "--model", model,
        "--url", normalize_endpoint(cfg.get("endpoint")),
        "--dataset", DATASET_NAME,
        "--dataset-path", str(dataset_path),
        "--multi-turn",
        "--max-turns", str(max(1, int(cfg.get("turns") or 1))),
        "--number", *[str(n) for n in numbers],
        "--parallel", *[str(u) for u in users],
        "--sleep-interval",
        str(int(max(0.0, float(cfg.get("ramp_pause_seconds") or 0.0)))),
        "--max-tokens", str(max(1, int(cfg.get("max_tokens") or 512))),
        "--temperature", str(float(cfg.get("temperature") or 0.0)),
        "--top-p", str(float(cfg.get("top_p")
                             if cfg.get("top_p") not in (None, "") else 1.0)),
        "--total-timeout", str(max(1, int(float(cfg.get("timeout") or 120)))),
        "--max-prompt-length", str(MAX_PROMPT_LENGTH),
        "--db-commit-interval", str(DB_COMMIT_INTERVAL),
        "--outputs-dir", str(run_dir),
        "--name", RUN_NAME,
        "--no-timestamp",
    ]
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        cmd += ["--api-key", api_key]
    top_k = int(cfg.get("top_k") or -1)
    if top_k >= 0:                      # -1 表示不注入该字段
        cmd += ["--top-k", str(top_k)]
    return cmd


def dump_args(cfg, run_dir, dataset_path):
    """渲染本次运行参数（落盘为 args.json，供页面「查看运行参数」）。"""
    argv = build_command(cfg, run_dir, dataset_path)
    return json.dumps({
        "command": argv,
        "endpoint": normalize_endpoint(cfg.get("endpoint")),
        "model": cfg.get("model"),
        "max_tokens": cfg.get("max_tokens"),
        "temperature": cfg.get("temperature"),
        "top_p": cfg.get("top_p"),
        "top_k": cfg.get("top_k"),
        "timeout": cfg.get("timeout"),
        "turns": max(1, int(cfg.get("turns") or 1)),
        "unique_prefix": bool(cfg.get("unique_prefix")),
        "iterations": cfg.get("iterations"),
        "levels": [{"parallel": u, "number": conversation_count(cfg, u)}
                   for u in levels(cfg)],
        "sleep_interval": cfg.get("ramp_pause_seconds"),
        "expected_requests": expected_requests(cfg),
        "dataset_path": str(dataset_path),
        "api_key": "***" if (cfg.get("api_key") or "").strip() else "",
    }, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------- 结果解析
def _num(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default          # NaN -> default


def _opt_num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _level_of_dir(path):
    """档位目录形如 parallel_8_number_24，从中解析并发数。"""
    m = _LEVEL_RE.search(os.path.basename(path.rstrip("/")))
    return int(m.group(1)) if m else 0


def _result_dbs(run_dir):
    """返回 [(level, db_path)]，按档位升序。"""
    out = []
    for root, _dirs, files in os.walk(run_dir):
        if RESULT_DB_NAME in files:
            out.append((_level_of_dir(root), os.path.join(root, RESULT_DB_NAME)))
    out.sort(key=lambda item: (item[0], item[1]))
    return out


def _request_turn(raw):
    """从落盘的请求体中推断当前轮次（user 消息个数）。"""
    if not raw:
        return None
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        return None
    turns = sum(1 for m in messages
                if isinstance(m, dict) and m.get("role") == "user")
    return turns or None


def _time_anchor():
    """perf_counter 与墙上时钟的换算锚点（evalscope 的 start_time 是单调时钟）。"""
    return time.perf_counter(), datetime.now()


def _record(row, level, cfg, run_id, anchor, index):
    keys = set(row.keys())

    def cell(name):
        return row[name] if name in keys else None

    start = _num(cell("start_time"))
    latency = _num(cell("latency"))
    ttft = _opt_num(cell("first_chunk_latency"))
    tpot = _opt_num(cell("time_per_output_token"))
    prompt_tokens = _opt_num(cell("prompt_tokens"))
    completion_tokens = _opt_num(cell("completion_tokens"))
    success = bool(cell("success"))
    mono, wall = anchor
    ts = wall - timedelta(seconds=max(0.0, mono - start)) if start else None
    tps = None
    if completion_tokens is not None and latency > 0:
        tps = completion_tokens / latency
    return {
        "run_id": run_id,
        "timestamp_start": ts.isoformat(timespec="milliseconds") if ts else "",
        "timestamp_end": (ts + timedelta(seconds=latency)).isoformat(timespec="milliseconds")
                         if ts else "",
        "concurrent_users_level": level,
        "conversation": "conv",
        "turn": _request_turn(cell("request")),
        "iteration": index,
        "user_id": cell("request_id") or "",
        "success": success,
        "ttft_s": ttft,
        "tpot_s": tpot,
        "e2e_latency_s": latency,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_per_second": tps,
        "kv_cache_usage": None,
        "cache_hit_rate": None,
        "error": "" if success else "请求失败（详见 benchmark.log）",
        "scenario": (cfg.get("name") or "stress"),
        "target_server": _target_of(cfg),
        "target_model": (cfg.get("model") or "").strip(),
    }


def _target_of(cfg):
    try:
        return normalize_endpoint(cfg.get("endpoint"))
    except ValueError:                      # 历史任务缺少配置时不至于整条读取失败
        return (cfg.get("endpoint") or "").strip()


def read_records(run_dir, cfg=None, run_id="", after=None):
    """增量读取 evalscope 的 SQLite 结果库，归一化为统一记录格式。

    结果库**只追加**，因此按「已读行数」做游标即可安全增量读取（运行中也能读）。
    `after` 为 {db_path: 已读行数}，返回 (新增记录, 新游标)。
    """
    cfg = cfg or {}
    cursor = dict(after or {})
    records = []
    anchor = _time_anchor()
    for level, path in _result_dbs(run_dir):
        done = int(cursor.get(path) or 0)
        try:
            conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True, timeout=3.0)
        except sqlite3.Error:
            continue
        try:
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM result").fetchone()[0]
            if total <= done:
                continue
            rows = conn.execute("SELECT * FROM result LIMIT ? OFFSET ?",
                                (total - done, done)).fetchall()
        except sqlite3.Error:               # 表尚未创建 / 正被写入进程持有
            continue
        finally:
            conn.close()
        cursor[path] = total
        for i, row in enumerate(rows, start=done + 1):
            records.append(_record(row, level, cfg, run_id, anchor, i))
    return records, cursor


# --------------------------------------------------------------------- 指标聚合
def _p95(values):
    """P95：quantiles(n=20) 的第 18 个分位（沿用升级前的口径，便于历史对比）。"""
    if not values:
        return 0.0
    if len(values) < 2:
        return values[0]
    return statistics.quantiles(values, n=20)[18]


def _p50(values):
    return statistics.median(values) if values else 0.0


def estimate_duration(records):
    """总时长估计：最后一条结束时间减去最早一条开始时间。"""
    spans = []
    for r in records:
        ts = r.get("timestamp_start")
        if not ts:
            continue
        try:
            start = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        spans.append((start, start + _num(r.get("e2e_latency_s"))))
    if not spans:
        return 1.0
    return max(e for _, e in spans) - min(s for s, _ in spans) or 1.0


def aggregate(records):
    """聚合指标，字段与页面展示对齐，另加 p50 与错误样本。

    RPS 按总请求数计算（含失败请求），反映压测期间目标服务实际承受的请求速率。
    """
    total = len(records)
    ok = [r for r in records if r.get("success")]
    ttft = [_num(r.get("ttft_s")) for r in ok]
    e2e = [_num(r.get("e2e_latency_s")) for r in ok]
    tpot = [_num(r.get("tpot_s")) for r in ok]
    tps = [_num(r.get("tokens_per_second")) for r in ok]
    duration = estimate_duration(records)
    completion = sum(_num(r.get("completion_tokens")) for r in ok)
    prompt_tokens = sum(_num(r.get("prompt_tokens")) for r in ok)

    errors = {}
    for r in records:
        if r.get("success"):
            continue
        msg = str(r.get("error") or "未知错误")[:160]
        errors[msg] = errors.get(msg, 0) + 1
    error_list = sorted(({"message": k, "count": v} for k, v in errors.items()),
                        key=lambda x: -x["count"])[:8]

    first = records[0] if records else {}
    return {
        "total_requests": total,
        "success_count": len(ok),
        "error_count": total - len(ok),
        "success_rate": (len(ok) / total) if total else 0.0,
        "ttft_mean_s": statistics.mean(ttft) if ttft else 0.0,
        "ttft_median_s": _p50(ttft),
        "ttft_p95_s": _p95(ttft),
        "tpot_mean_s": statistics.mean(tpot) if tpot else 0.0,
        "e2e_mean_s": statistics.mean(e2e) if e2e else 0.0,
        "e2e_p95_s": _p95(e2e),
        "tokens_per_second_mean": statistics.mean(tps) if tps else 0.0,
        "total_tokens_per_second": (completion / duration) if duration else 0.0,
        "requests_per_second": (total / duration) if duration else 0.0,
        "total_duration_s": duration,
        "prompt_tokens_total": prompt_tokens,
        "completion_tokens_total": completion,
        "scenario": first.get("scenario", ""),
        "target_server": first.get("target_server", ""),
        "target_model": first.get("target_model", ""),
        "concurrent_level": max((_num(r.get("concurrent_users_level")) for r in records),
                                default=0.0),
        "errors": error_list,
    }


def by_level(records):
    """按并发档位分组聚合（阶梯压测）。非阶梯场景（level=0）返回空列表。"""
    groups = {}
    for r in records:
        lvl = int(_num(r.get("concurrent_users_level")))
        groups.setdefault(lvl, []).append(r)
    out = []
    for lvl in sorted(groups):
        if not lvl:
            continue
        agg = aggregate(groups[lvl])
        agg["level"] = lvl
        out.append(agg)
    return out


def conversation_metrics(records):
    """多轮会话指标（各轮首字延迟 / 上下文增长）。"""
    first = {}
    turns = {}
    for r in records:
        if not r.get("success") or r.get("turn") is None:
            continue
        turn = int(r.get("turn") or 0)
        first.setdefault(turn, []).append(_num(r.get("ttft_s")))
        turns.setdefault(turn, []).append(_num(r.get("e2e_latency_s")))
    if not first:
        return None
    ttft_by_turn = [statistics.mean(v) for _, v in sorted(first.items())]
    e2e_by_turn = [statistics.mean(v) for _, v in sorted(turns.items())]
    ratios = {}
    for i in range(1, len(ttft_by_turn)):
        if ttft_by_turn[0]:
            ratios[str(i + 1)] = ttft_by_turn[i] / ttft_by_turn[0]
    growth = (e2e_by_turn[-1] / e2e_by_turn[0]) if len(e2e_by_turn) > 1 and e2e_by_turn[0] else 1.0
    return {
        "ttft_by_turn": ttft_by_turn,
        "e2e_by_turn": e2e_by_turn,
        "turn_to_turn_ratio": ratios,
        "context_growth_factor": growth,
        "kv_cache_hit_rate_mean": None,      # evalscope 不采集目标侧 KV cache 指标
        "kv_cache_usage_mean": None,
        "has_kv_cache": False,
    }


def series(records, buckets=60):
    """按时间分桶的时序数据，供前端绘制实时曲线。"""
    points = []
    for r in records:
        ts = r.get("timestamp_start")
        if not ts:
            continue
        try:
            start = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        points.append((start, r))
    if not points:
        return []
    points.sort(key=lambda x: x[0])
    t0 = points[0][0]
    span = max(points[-1][0] - t0, 0.001)
    size = max(span / max(1, buckets), 0.5)
    buckets_map = {}
    for start, r in points:
        idx = int((start - t0) / size)
        buckets_map.setdefault(idx, []).append(r)
    out = []
    for idx in sorted(buckets_map):
        chunk = buckets_map[idx]
        ok = [r for r in chunk if r.get("success")]
        out.append({
            "t": round(idx * size, 2),
            "requests": len(chunk),
            "rps": round(len(chunk) / size, 3),
            "success_rate": (len(ok) / len(chunk)) if chunk else 0.0,
            "ttft_mean_s": round(statistics.mean([_num(r.get("ttft_s")) for r in ok]), 4) if ok else 0.0,
            "e2e_mean_s": round(statistics.mean([_num(r.get("e2e_latency_s")) for r in ok]), 4) if ok else 0.0,
            "tps_mean": round(statistics.mean([_num(r.get("tokens_per_second")) for r in ok]), 3) if ok else 0.0,
            "level": max((int(_num(r.get("concurrent_users_level"))) for r in chunk), default=0),
        })
    return out


def row_for_csv(record):
    """压测结果导出 CSV 时的列顺序（与 CSV_HEADER 一致）。"""
    return [
        record.get("run_id", ""),
        record.get("timestamp_start", ""),
        record.get("concurrent_users_level", ""),
        record.get("conversation", ""),
        record.get("turn", ""),
        record.get("iteration", ""),
        record.get("user_id", ""),
        record.get("success", ""),
        record.get("ttft_s", ""),
        record.get("tpot_s", ""),
        record.get("e2e_latency_s", ""),
        record.get("prompt_tokens", ""),
        record.get("completion_tokens", ""),
        record.get("tokens_per_second", ""),
        record.get("kv_cache_usage", ""),
        record.get("cache_hit_rate", ""),
        record.get("error", ""),
    ]


CSV_HEADER = ["run_id", "timestamp_start", "concurrent_users_level", "conversation",
              "turn", "iteration", "user_id", "success", "ttft_s", "tpot_s",
              "e2e_latency_s", "prompt_tokens", "completion_tokens",
              "tokens_per_second", "kv_cache_usage", "cache_hit_rate", "error"]
