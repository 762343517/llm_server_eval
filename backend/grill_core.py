"""llm-grill 集成核心：场景生成、结果解析、指标聚合。

集成方式：只调用 llm-grill 的**命令行可执行文件**，不 import 它的 Python 包。
llm-grill 由 `uv tool` 装在自己的虚拟环境里，直接 import 会把它那套
pydantic/anyio 依赖混进本项目的解释器，有版本冲突风险；子进程方式完全隔离。

llm-grill 0.1.4 关键行为：
- `llm-grill run <scenario.yaml> -o <results.jsonl> [-q]`，结果逐条 flush 落盘，
  所以可以边跑边 tail 该文件做实时进度；
- 客户端固定 POST `/v1/chat/completions`，base URL 里带路径会被覆盖；
- 每行结果是一条 RequestMetrics（ttft_s / tpot_s / e2e_latency_s / success ...）。
"""
import json
import os
import shutil
import statistics
import subprocess
import time
from urllib.parse import urlsplit

try:
    import yaml
except ImportError:  # pragma: no cover - requirements.txt 已声明 pyyaml
    yaml = None

BACKEND_TYPES = ("openai", "vllm", "sglang", "llamacpp", "litellm")
BACKEND_LABELS = {
    "openai": "OpenAI 兼容（vLLM / SGLang / LiteLLM 均可）",
    "vllm": "vLLM（附带 /metrics 采集）",
    "sglang": "SGLang（附带 /metrics 采集）",
    "llamacpp": "llama.cpp",
    "litellm": "LiteLLM 代理",
}

_PROBE = {"at": 0.0, "info": None}
_PROBE_TTL = 60.0


# --------------------------------------------------------------------- 可执行文件
def resolve_bin():
    """定位 llm-grill：环境变量 > PATH > 常见安装位置。"""
    raw = (os.environ.get("LLM_GRILL_BIN") or "").strip()
    if raw:
        if os.path.isfile(raw) and os.access(raw, os.X_OK):
            return raw
        return shutil.which(raw)
    found = shutil.which("llm-grill")
    if found:
        return found
    for cand in ("~/.local/bin/llm-grill", "/usr/local/bin/llm-grill"):
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def probe(force=False):
    """探测 llm-grill 是否可用（60s 缓存，供 /api/meta 用）。"""
    now = time.time()
    if not force and _PROBE["info"] and now - _PROBE["at"] < _PROBE_TTL:
        return _PROBE["info"]
    path = resolve_bin()
    info = {"available": False, "path": path, "version": None, "error": None}
    if not path:
        info["error"] = ("未找到 llm-grill；请先安装（如 uv tool install llm-grill），"
                         "或用环境变量 LLM_GRILL_BIN 指定完整路径")
    else:
        try:
            proc = subprocess.run([path, "--version"], capture_output=True,
                                  text=True, timeout=20)
            out = ((proc.stdout or "").strip() or (proc.stderr or "").strip())
            if proc.returncode == 0:
                info["available"] = True
                info["version"] = out.replace("llm-grill", "").strip() or out
            else:
                info["error"] = "llm-grill --version 退出码 {}: {}".format(
                    proc.returncode, out[:200])
        except Exception as e:  # noqa: BLE001
            info["error"] = "{}: {}".format(type(e).__name__, str(e)[:200])
    _PROBE.update({"at": now, "info": info})
    return info


# --------------------------------------------------------------------- 场景生成
def normalize_base_url(endpoint):
    """归一成 llm-grill 需要的 base URL（scheme://host:port）。"""
    raw = (endpoint or "").strip()
    if not raw:
        raise ValueError("API 端点不能为空")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("API 端点格式不合法: " + endpoint)
    return "{}://{}".format(parts.scheme, parts.netloc)


def _turns(cfg):
    """多轮：同一段提问重复 N 轮，历史中带上模型上一轮回答。

    llm-grill 的会话指标（首字延迟轮次比、上下文增长系数、KV cache 命中率）
    正是为长上下文/缓存复用场景设计的。
    """
    turns = max(1, int(cfg.get("turns") or 1))
    prompt = (cfg.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提问内容不能为空")
    messages = []
    system = (cfg.get("system_prompt") or "").strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend({"role": "user", "content": prompt} for _ in range(turns))
    return messages


def build_scenario(cfg):
    """Web 表单配置 -> llm-grill 场景（dict）。"""
    backend_type = (cfg.get("backend_type") or "openai").strip()
    if backend_type not in BACKEND_TYPES:
        raise ValueError("后端类型不支持: " + backend_type)
    model = (cfg.get("model") or "").strip()
    if not model:
        raise ValueError("模型名不能为空")

    backend = {
        "name": (cfg.get("backend_name") or "target").strip() or "target",
        "url": normalize_base_url(cfg.get("endpoint")),
        "type": backend_type,
        "timeout": float(cfg.get("timeout") or 120),
    }
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        backend["api_key"] = api_key

    model_cfg = {
        "name": model,
        "max_tokens": max(1, int(cfg.get("max_tokens") or 512)),
        "temperature": float(cfg.get("temperature") or 0.0),
        "top_p": float(cfg.get("top_p") if cfg.get("top_p") not in (None, "") else 1.0),
    }
    top_k = int(cfg.get("top_k") or -1)
    if top_k >= 0:                      # -1 表示不注入该字段
        model_cfg["top_k"] = top_k

    load = {
        "concurrent_users": max(1, int(cfg.get("concurrent_users") or 1)),
        "iterations": max(1, int(cfg.get("iterations") or 1)),
        "ramp_up_seconds": max(0.0, float(cfg.get("ramp_up_seconds") or 0.0)),
        "think_time_seconds": max(0.0, float(cfg.get("think_time_seconds") or 0.0)),
    }
    levels = cfg.get("ramp_levels") or []
    if levels:
        load["ramp_levels"] = sorted({max(1, int(x)) for x in levels})
        load["ramp_pause_seconds"] = max(0.0, float(cfg.get("ramp_pause_seconds") or 0.0))

    scenario = {
        "name": (cfg.get("name") or "stress").strip() or "stress",
        "description": (cfg.get("description") or "").strip(),
        "backends": [backend],
        "models": [model_cfg],
        "conversations": [{
            "name": "conv",
            "description": "{} 轮对话".format(max(1, int(cfg.get("turns") or 1))),
            "unique_prefix": bool(cfg.get("unique_prefix")),
            "turns": _turns(cfg),
        }],
        "targets": [{"backend": backend["name"], "model": model_cfg["name"],
                     "conversation": "conv"}],
        "load": load,
    }
    run_id = (cfg.get("run_id") or "").strip()
    if run_id:
        scenario["run_id"] = run_id
    return scenario


def expected_requests(cfg):
    """本次压测预计发出多少请求（进度百分比用）。"""
    turns = max(1, int(cfg.get("turns") or 1))
    iterations = max(1, int(cfg.get("iterations") or 1))
    levels = cfg.get("ramp_levels") or []
    if levels:
        users = sum(max(1, int(x)) for x in levels)
    else:
        users = max(1, int(cfg.get("concurrent_users") or 1))
    return users * iterations * turns


def dump_scenario(scenario):
    """场景 -> YAML 文本。"""
    if yaml is not None:
        return yaml.safe_dump(scenario, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)
    return _dump_yaml(scenario) + "\n"


def _yaml_scalar(value):
    """标量 -> YAML。字符串统一走 json.dumps：JSON 字符串是合法 YAML 双引号标量，
    提问文本里的引号/换行/中文都不会写坏场景文件。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        return "null"
    return json.dumps(str(value), ensure_ascii=False)


def _dump_yaml(node, indent=0):
    """兜底 YAML 输出（pyyaml 缺失时用），只覆盖场景文件的结构。"""
    pad = "  " * indent
    if isinstance(node, dict):
        lines = []
        for k, v in node.items():
            if isinstance(v, (dict, list)) and v:
                lines.append("{}{}:".format(pad, k))
                lines.append(_dump_yaml(v, indent + 1))
            else:
                lines.append("{}{}: {}".format(pad, k, _yaml_scalar(v)))
        return "\n".join(lines)
    if isinstance(node, list):
        lines = []
        for item in node:
            if isinstance(item, dict):
                body = _dump_yaml(item, indent + 1).split("\n")
                lines.append("{}- {}".format(pad, body[0].strip()))
                lines.extend(body[1:])
            else:
                lines.append("{}- {}".format(pad, _yaml_scalar(item)))
        return "\n".join(lines)
    return "{}{}".format(pad, _yaml_scalar(node))


# --------------------------------------------------------------------- 结果解析
def parse_line(raw):
    """解析一行 JSONL；空行/坏行返回 None（跑挂了也保留已完成的结果）。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def load_records(path):
    """读取整个结果文件。"""
    records = []
    if not os.path.isfile(path):
        return records
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = parse_line(line)
            if rec:
                records.append(rec)
    return records


def _num(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default          # NaN -> default


def _p95(values):
    """与 llm-grill 的 quantiles(n=20)[18] 保持一致。"""
    if not values:
        return 0.0
    if len(values) < 2:
        return values[0]
    return statistics.quantiles(values, n=20)[18]


def _p50(values):
    return statistics.median(values) if values else 0.0


def estimate_duration(records):
    """总时长估计（对齐 llm-grill）：最后一条结束 - 最早一条开始。"""
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
    """聚合指标，字段与 llm-grill 的 AggregatedMetrics 对齐，另加 p50 与错误样本。

    RPS 按总请求数算（含失败），反映压测期间目标服务实际承受的请求速率。
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
    """多轮会话指标（KV cache / 上下文增长），对齐 llm-grill 的 ConversationMetrics。"""
    first = {}
    turns = {}
    kv_hit, kv_use, ratios = [], [], {}
    for r in records:
        if not r.get("success") or r.get("turn") is None:
            continue
        turn = int(r.get("turn") or 0)
        first.setdefault(turn, []).append(_num(r.get("ttft_s")))
        turns.setdefault(turn, []).append(_num(r.get("e2e_latency_s")))
        if r.get("cache_hit_rate") is not None:      # vLLM 前缀缓存命中率
            kv_hit.append(_num(r.get("cache_hit_rate")))
        if r.get("kv_cache_usage") is not None:
            kv_use.append(_num(r.get("kv_cache_usage")))
    if not first:
        return None
    ttft_by_turn = [statistics.mean(v) for _, v in sorted(first.items())]
    e2e_by_turn = [statistics.mean(v) for _, v in sorted(turns.items())]
    for i in range(1, len(ttft_by_turn)):
        if ttft_by_turn[0]:
            ratios[str(i + 1)] = ttft_by_turn[i] / ttft_by_turn[0]
    growth = (e2e_by_turn[-1] / e2e_by_turn[0]) if len(e2e_by_turn) > 1 and e2e_by_turn[0] else 1.0
    return {
        "ttft_by_turn": ttft_by_turn,
        "e2e_by_turn": e2e_by_turn,
        "turn_to_turn_ratio": ratios,
        "context_growth_factor": growth,
        "kv_cache_hit_rate_mean": statistics.mean(kv_hit) if kv_hit else None,
        "kv_cache_usage_mean": statistics.mean(kv_use) if kv_use else None,
        "has_kv_cache": bool(kv_hit),
    }


def series(records, buckets=60):
    """按时间分桶的时序数据，给前端画实时曲线用。"""
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
    """压测结果为 CSV 导出的列（顺序与 CSV_HEADER 一致）。"""
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
