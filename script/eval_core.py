"""大模型精度评测核心逻辑（SimpleQA-Verified / HLE 通用）。

CLI (simpleqa_eval.py) 与 Web 服务 (webapp.py) 共用同一套实现，
所有可变配置(端点/密钥/模型/并发/上限)均通过参数注入，不再依赖模块级全局常量。

数据集适配见 load_dataset()：
  - SimpleQA-Verified  CSV，字段 original_index/problem/answer/topic/answer_type
  - HLE                parquet，question/raw_subject/category/answer_type，含图片题
两者统一为同一套字段，题型判定与打分模板完全复用。
"""
import json
import os
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import pandas as pd
import requests

ANSWER_PROMPT = """Your response should be in the following format:
Explanation: {{your explanation for your final answer}}
Exact Answer: {{your succinct, final answer}}
Confidence: {{your confidence score between 0% and 100% for your answer}}

Question: {question}"""

GRADER_TEMPLATE = """Your job is to look at a question, a gold target, and a predicted answer, and then return a letter grade of either [Correct] or [Incorrect] or [Not_Attempted]. Before giving a grade, first write a brief explanation. Then give the grade.

Note that the predicted answer may contain additional information, and it is okay if it contains additional information as long as the information is correct and the answer is correct.

The gold target may be of the form "X, Y, or Z", in which case any of X, Y, or Z is a correct answer. The gold target may also list several acceptable answers.

Use [Not_Attempted] when the predicted answer is a refusal, an expression of uncertainty, or otherwise does not attempt to answer. Use [Incorrect] when the model attempts an answer but it is wrong.

Question: {question}
Gold target: {target}
Predicted answer: {predicted_answer}

First write your explanation, then on a final line output exactly one of: [Correct], [Incorrect], [Not_Attempted]"""

DEFAULT_DATA = "dataset/SimpleQA-Verified/simpleqa_verified.csv"
HLE_DIR = "dataset/HLE"

# 数据集统一后的字段（_NORM_COLUMNS）：
#   SimpleQA-Verified  CSV   -> 原列名直接可用
#   HLE                parquet -> question->problem, raw_subject->subject,
#                                category->topic, image 单独存放不落到逐题记录
_NORM_COLUMNS = ("original_index", "problem", "answer", "topic", "answer_type",
                 "subject")


def _find_hle_file():
    if not os.path.isdir(HLE_DIR):
        return None
    for fn in sorted(os.listdir(HLE_DIR)):
        if fn.lower().endswith((".parquet", ".pq")):
            return os.path.join(HLE_DIR, fn)
    return None


def available_datasets():
    """内置数据集列表（含文件是否存在），供 Web 下拉选择。"""
    items = [{"name": "SimpleQA-Verified", "path": DEFAULT_DATA}]
    hle = _find_hle_file()
    if hle:
        items.append({"name": "HLE (Humanity's Last Exam)", "path": hle})
    for it in items:
        it["exists"] = os.path.exists(it["path"])
    return items


def dataset_kind(path):
    """按扩展名区分数据集结构：parquet 视为 HLE，其余按 SimpleQA CSV。"""
    low = (path or "").lower()
    return "hle" if low.endswith((".parquet", ".pq")) else "simpleqa"


def _str_col(df, name, default=""):
    if name in df.columns:
        return df[name].fillna(default).astype(str)
    return pd.Series([default] * len(df), index=df.index)


_HLE_LIGHT_COLUMNS = ("question", "answer", "answer_type", "raw_subject",
                      "category")


def _row_group_bounds(pf):
    """返回每个 row group 的行号区间 [(start, end), ...]。"""
    bounds, start = [], 0
    for g in range(pf.num_row_groups):
        n = pf.metadata.row_group(g).num_rows
        bounds.append((start, start + n))
        start += n
    return bounds


def _read_hle_images(pf, indexes, on_event=None):
    """只读取包含目标行的 row group 的 image 列。

    HLE 的 image 是内嵌 base64（整表解压后 >1GB），全量读入既慢又吃内存；
    按命中的 row group 精确读取后，限制题量时加载时间从分钟级降到秒级。
    """
    need = sorted({int(i) for i in indexes})
    bounds = _row_group_bounds(pf)
    images, done = {}, 0
    for gi, (s, e) in enumerate(bounds):
        sel = [i for i in need if s <= i < e]
        if not sel:
            continue
        col = pf.read_row_group(gi, columns=["image"]).column("image").to_pylist()
        for i in sel:
            v = col[i - s]
            if isinstance(v, str) and v.strip():
                images[i] = v
        done += len(sel)
        if on_event:
            on_event("load_progress", {
                "total": len(need), "loaded": done, "images": len(images),
                "row_group": gi + 1, "row_groups": pf.num_row_groups,
            })
    return images


def _load_hle(path, limit=0, seed=0, on_event=None):
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    names = pf.schema_arrow.names
    light = [c for c in _HLE_LIGHT_COLUMNS if c in names]

    raw = pf.read(columns=light).to_pandas()
    df = pd.DataFrame({
        "original_index": pd.Series(range(len(raw)), index=raw.index),
        "problem": _str_col(raw, "question"),
        "answer": _str_col(raw, "answer"),
        "topic": _str_col(raw, "category", "Unknown"),
        "answer_type": _str_col(raw, "answer_type", "exactMatch"),
        "subject": _str_col(raw, "raw_subject"),
    })
    del raw

    picked = df
    if limit and limit < len(df):
        picked = df.sample(n=limit, random_state=seed).sort_index()

    images = {}
    if "image" in names and len(picked):
        images = _read_hle_images(pf, picked["original_index"].tolist(), on_event)
    return picked[list(_NORM_COLUMNS)].reset_index(drop=True), images


def _load_csv(path, limit=0, seed=0, on_event=None):
    raw = pd.read_csv(path)
    if "original_index" in raw.columns:
        idx = raw["original_index"].astype(int)
    else:
        idx = pd.Series(range(len(raw)), index=raw.index)
    df = pd.DataFrame({"original_index": idx})
    for col in ("problem", "answer", "topic", "answer_type", "subject"):
        df[col] = _str_col(raw, col, "Unknown" if col == "topic" else "")
    images = ({int(i): v for i, v in _str_col(raw, "image").items() if v}
              if "image" in raw.columns else {})
    del raw

    picked = df
    if limit and limit < len(df):
        picked = df.sample(n=limit, random_state=seed).sort_index()
    keep = set(picked["original_index"].astype(int).tolist())
    images = {k: v for k, v in images.items() if k in keep}
    if on_event:
        on_event("load_progress", {"total": len(picked), "loaded": len(picked),
                                   "images": len(images)})
    return picked[list(_NORM_COLUMNS)].reset_index(drop=True), images


def load_dataset(path, limit=0, seed=0, on_event=None):
    """读取数据集并归一化为统一字段（含采样）。

    返回 (df, images)：df 含 _NORM_COLUMNS；images 为 {original_index: data-url}，
    仅当数据集带图片（HLE 多模态题）时非空，避免把 base64 复制进逐题记录。
    limit>0 时在此处完成采样，图片只加载采样命中的行。
    """
    on_event = on_event or (lambda kind, payload: None)
    kind = dataset_kind(path)
    on_event("load_start", {"path": path, "kind": kind})
    if kind == "hle":
        df, images = _load_hle(path, limit, seed, on_event)
    else:
        df, images = _load_csv(path, limit, seed, on_event)
    on_event("load_end", {"rows": len(df), "images": len(images)})
    return df, images


class HttpError(RuntimeError):
    """带状态码的 HTTP 错误，便于区分 502/429 等重试策略。"""

    def __init__(self, status, body):
        super().__init__(f"http {status}: {body}")
        self.status = status


# ---------------------------------------------------------------- token 统计
try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _ENC = None

_TOKEN_CACHE = {}
_TOKEN_CACHE_LOCK = threading.Lock()


def count_tokens(text):
    """估算可见回答的 token 数。思考内容不流式返回，只能用可见文本反推。"""
    if not text:
        return 0
    if _ENC is None:
        return max(1, len(text) // 4)
    with _TOKEN_CACHE_LOCK:
        hit = _TOKEN_CACHE.get(text)
    if hit is not None:
        return hit
    n = len(_ENC.encode(text))
    with _TOKEN_CACHE_LOCK:
        if len(_TOKEN_CACHE) < 20000:
            _TOKEN_CACHE[text] = n
    return n


# ---------------------------------------------------------------- 结果解析
def parse_grade(text):
    low = (text or "").lower()
    pos, grade = -1, "NOT_ATTEMPTED"
    for label, name in (
        ("[correct]", "CORRECT"),
        ("[incorrect]", "INCORRECT"),
        ("[not_attempted]", "NOT_ATTEMPTED"),
    ):
        idx = low.rfind(label)
        if idx > pos:
            pos, grade = idx, name
    return grade


def extract_exact_answer(text):
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        if ln.lower().startswith("exact answer"):
            return ln.split(":", 1)[-1].strip()
    return lines[-1] if lines else ""


# ---------------------------------------------------------------- 落盘工具
def load_jsonl(path, key):
    done = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    done[rec[key]] = rec
                except json.JSONDecodeError:
                    continue
    return done


def stuck_indexes(path, limit):
    """统计被标记 incomplete 的次数，达到上限视为终态，不再无限重跑。"""
    counts = {}
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("incomplete"):
                idx = rec["original_index"]
                counts[idx] = counts.get(idx, 0) + 1
    return {k for k, v in counts.items() if v >= limit}


_IO_LOCK = threading.Lock()


def append_jsonl(path, rec):
    with _IO_LOCK:
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


# ---------------------------------------------------------------- 指标汇总
def summarise(records, model, judge_model, data, total_questions,
              incomplete_n, failed_n):
    """把逐题记录汇总成指标字典。"""
    dfr = pd.DataFrame(records)
    n = len(records)
    c = int((dfr.grade == "CORRECT").sum())
    ic = int((dfr.grade == "INCORRECT").sum())
    na = int((dfr.grade == "NOT_ATTEMPTED").sum())
    toks = dfr.completion_tokens.tolist()
    ttfts = dfr.ttft_s.tolist()
    tpots = (dfr.tpot_visible_s.dropna().tolist()
             if "tpot_visible_s" in dfr else [])
    walls = dfr.total_s.tolist()

    def pct(v, p):
        v = sorted(v)
        return v[min(int(len(v) * p), len(v) - 1)]

    summary = {
        "model": model,
        "judge_model": judge_model,
        "dataset": os.path.basename(data),
        "num_questions": n,
        "expected_questions": total_questions,
        "coverage": round(n / total_questions, 4) if total_questions else 0.0,
        "num_incomplete": incomplete_n,
        "num_failed": failed_n,
        "temperature": 0.0,
        "counts": {"CORRECT": c, "INCORRECT": ic, "NOT_ATTEMPTED": na},
        "pass_at_1": round(c / n, 4),
        "pass_at_1_ci95": [
            round(c / n - 1.96 * ((c / n) * (1 - c / n) / n) ** 0.5, 4),
            round(c / n + 1.96 * ((c / n) * (1 - c / n) / n) ** 0.5, 4),
        ],
        "accuracy_attempted": round(c / (c + ic), 4) if (c + ic) else None,
        "not_attempted_rate": round(na / n, 4),
        "output_tokens_avg": round(sum(toks) / n, 1),
        "output_tokens_p50": round(pct(toks, 0.5)),
        "visible_tokens_avg": (
            round(float(dfr.content_tokens.mean()), 1)
            if "content_tokens" in dfr else None
        ),
        "reasoning_tokens_avg": (
            round(float(dfr.reasoning_tokens.dropna().mean()), 1)
            if "reasoning_tokens" in dfr and dfr.reasoning_tokens.notna().any()
            else None
        ),
        "ttft_visible_p50_s": round(pct(ttfts, 0.5), 2),
        "ttft_visible_p90_s": round(pct(ttfts, 0.9), 2),
        "tpot_visible_p50_s": round(pct(tpots, 0.5), 4) if tpots else None,
        "e2e_latency_p50_s": round(pct(walls, 0.5), 2),
        "e2e_latency_p90_s": round(pct(walls, 0.9), 2),
    }
    summary["by_topic"] = {
        t: {"n": int(len(g)), "pass_at_1": round(float((g.grade == "CORRECT").mean()), 4)}
        for t, g in dfr.groupby("topic")
    }
    summary["by_answer_type"] = {
        t: {"n": int(len(g)), "pass_at_1": round(float((g.grade == "CORRECT").mean()), 4)}
        for t, g in dfr.groupby("answer_type")
    }
    return summary


# ---------------------------------------------------------------- 思考开关
# 三态："" 表示不传该字段（由服务端默认决定），其余为显式开关。
THINKING_CHOICES = ("", "enabled", "disabled")
EFFORT_CHOICES = ("", "low", "medium", "high", "max")


def build_think_extra(thinking, reasoning_effort=""):
    """把思考开关/强度转成请求体附加字段。

    沿用 simpleqa_verified_deepseekv4.py 里验证过的约定：
    {"thinking": {"type": "enabled"|"disabled"}, "reasoning_effort": "low|medium|high|max"}。
    thinking 为空时不注入任何字段，保持服务端默认行为。
    """
    extra = {}
    if thinking:
        extra["thinking"] = {"type": thinking}
    if thinking == "enabled" and reasoning_effort:
        extra["reasoning_effort"] = reasoning_effort
    return extra


def thinking_label(thinking, reasoning_effort=""):
    """人类可读的思考配置描述，用于日志与汇总。"""
    if not thinking:
        return "服务端默认"
    if thinking == "disabled":
        return "关闭"
    return "开启（强度 {}）".format(reasoning_effort or "默认")


# ---------------------------------------------------------------- HTTP 客户端
class HttpClient:
    """按 endpoint/api_key 参数化的流式 + 非流式调用封装。"""

    def __init__(self, endpoint, api_key, timeout=1800, should_stop=None,
                 extra=None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.should_stop = should_stop or (lambda: False)
        self.extra = dict(extra or {})
        self._local = threading.local()

    def _session(self):
        if not hasattr(self._local, "s"):
            s = requests.Session()
            s.headers.update({
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            })
            self._local.s = s
        return self._local.s

    @staticmethod
    def backoff(k, status=None):
        """402/429 限流退避要长；5xx 属上游抖动，短退避快速重试。"""
        if status in (429, 402):
            return min(5.0 * (k + 1), 60.0) + random.random()
        if status in (500, 502, 503, 504):
            return min(1.0 + 0.7 * k, 15.0) + random.random()
        return min(1.5 + 0.8 * k, 25.0) + random.random()

    @staticmethod
    def _content(prompt, image=None):
        """带图题目走多模态消息体（data-url），纯文本题保持字符串以兼容旧服务。"""
        if not image:
            return prompt
        return [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image}},
        ]

    def _post_stream(self, model, prompt, max_tokens, temperature=0.0, image=None):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": self._content(prompt, image)}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self.extra,
        }
        t0 = time.perf_counter()
        ttft = None
        parts, usage, finish = [], {}, None
        with self._session().post(
            self.endpoint, json=payload, timeout=self.timeout, stream=True
        ) as r:
            if r.status_code != 200:
                raise HttpError(r.status_code, r.text[:200])
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                chunk = json.loads(body)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        if ttft is None:
                            ttft = time.perf_counter() - t0
                        parts.append(piece)
        total = time.perf_counter() - t0
        return "".join(parts), usage, (ttft if ttft is not None else total), total, finish

    def stream(self, model, prompt, max_tokens=32768, retries=20, tag="",
               on_retry=None, image=None):
        last = None
        for k in range(retries):
            if self.should_stop():
                raise RuntimeError("stopped by user")
            try:
                return self._post_stream(model, prompt, max_tokens, image=image)
            except Exception as e:  # noqa: BLE001
                last = e
                wait_s = self.backoff(k, getattr(e, "status", None))
                if on_retry:
                    on_retry(k, e, wait_s)
                time.sleep(wait_s)
        raise RuntimeError(f"stream failed after {retries}: {last} {tag}")

    def chat_json(self, model, prompt, max_tokens=2048, retries=8, temperature=0.0):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last = None
        for k in range(retries):
            if self.should_stop():
                raise RuntimeError("stopped by user")
            try:
                r = self._session().post(
                    self.endpoint, json=payload, timeout=self.timeout
                )
                if r.status_code != 200:
                    raise HttpError(r.status_code, r.text[:200])
                return r.json()["choices"][0]["message"]["content"] or ""
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(self.backoff(k, getattr(e, "status", None)))
        raise RuntimeError(f"chat failed after {retries}: {last}")


# ---------------------------------------------------------------- 主流程
def run_eval(cfg, on_event=None, should_stop=None):
    """执行一次完整评测（生成 -> 裁判 -> 汇总）。

    cfg 必填: endpoint, api_key, model, judge_model, out
    cfg 可选: data, concurrency, judge_concurrency, limit, seed,
             max_tokens, max_tokens_cap, max_incomplete_attempts
    返回 summary dict；无可用记录时返回 None。
    """
    on_event = on_event or (lambda kind, payload: None)
    should_stop = should_stop or (lambda: False)

    model = cfg["model"]
    judge_model = cfg["judge_model"]
    data = cfg.get("data") or DEFAULT_DATA
    out = cfg["out"]
    concurrency = int(cfg.get("concurrency", 8))
    judge_concurrency = int(cfg.get("judge_concurrency", 8))
    limit = int(cfg.get("limit", 0) or 0)
    seed = int(cfg.get("seed", 0) or 0)
    max_tokens = int(cfg.get("max_tokens", 32768))
    max_tokens_cap = int(cfg.get("max_tokens_cap", 65536))
    max_incomplete_attempts = int(cfg.get("max_incomplete_attempts", 2))
    thinking = str(cfg.get("thinking") or "")
    reasoning_effort = str(cfg.get("reasoning_effort") or "")
    judge_thinking = str(cfg.get("judge_thinking") or "")
    judge_reasoning_effort = str(cfg.get("judge_reasoning_effort") or "")

    client = HttpClient(cfg["endpoint"], cfg["api_key"], should_stop=should_stop,
                        extra=build_think_extra(thinking, reasoning_effort))
    judge_client = HttpClient(
        cfg["endpoint"], cfg["api_key"], should_stop=should_stop,
        extra=build_think_extra(judge_thinking, judge_reasoning_effort))

    df, images = load_dataset(data, limit=limit, seed=seed, on_event=on_event)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    gen_path, judge_path = out + ".gen.jsonl", out + ".judge.jsonl"

    # ---------- 阶段一: 生成 ----------
    cached = load_jsonl(gen_path, "original_index")
    done = {k: v for k, v in cached.items() if not v.get("incomplete")}
    stuck = stuck_indexes(gen_path, max_incomplete_attempts)
    todo = [
        r for r in df.to_dict("records")
        if int(r["original_index"]) not in done
        and int(r["original_index"]) not in stuck
    ]
    on_event("gen_start", {
        "model": model, "total": len(df), "cached_valid": len(done),
        "cached_incomplete": len(cached) - len(done), "stuck": len(stuck),
        "todo": len(todo), "concurrency": concurrency, "max_tokens": max_tokens,
        "dataset": os.path.dirname(data).rstrip("/").split("/")[-1] or data,
        "images": len(images),
        "thinking": thinking_label(thinking, reasoning_effort),
    })

    def gen(row):
        idx = int(row["original_index"])
        image = images.get(idx)
        prompt = ANSWER_PROMPT.format(question=row["problem"])
        tag = f"q{idx}"
        mt = max_tokens
        while True:
            text, usage, ttft, total, finish = client.stream(
                model, prompt, max_tokens=mt, tag=tag, image=image
            )
            out_tok = usage.get("completion_tokens") or 0
            # 思考占满上限 => 答案还没写出来，翻倍上限重试一轮
            if finish == "length" and mt < max_tokens_cap:
                on_event("truncated", {"tag": tag, "hit": mt, "retry": mt * 2})
                mt = min(mt * 2, max_tokens_cap)
                continue
            break
        # 思考 token 不流式输出，需用可见文本 token 数做分母
        content_tokens = count_tokens(text)
        tpot = (total - ttft) / (content_tokens - 1) if content_tokens > 1 else None
        detail = usage.get("completion_tokens_details") or {}
        return {
            "original_index": idx,
            "problem": row["problem"],
            "answer": row["answer"],
            "topic": row["topic"],
            "answer_type": row["answer_type"],
            "subject": row.get("subject", ""),
            "has_image": bool(image),
            "prediction": text,
            "exact_answer": extract_exact_answer(text),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": out_tok,
            "content_tokens": content_tokens,
            "reasoning_tokens": detail.get("reasoning_tokens")
            or max(out_tok - content_tokens, 0),
            "finish_reason": finish,
            "max_tokens_used": mt,
            "incomplete": finish == "length" or not text.strip(),
            "ttft_s": round(ttft, 4),
            "total_s": round(total, 4),
            "tpot_visible_s": round(tpot, 5) if tpot else None,
        }

    gen_errors, gen_tokens, gen_done_n, gen_stopped = [], 0, 0, False
    if todo:
        t0 = time.perf_counter()

        def on_gen_done(row, rec):
            append_jsonl(gen_path, rec)
            nonlocal gen_tokens
            gen_tokens += rec["completion_tokens"]

        def on_gen_fail(row, exc):
            gen_errors.append((int(row["original_index"]), str(exc)[:200]))
            on_event("item_failed", {"stage": "gen",
                                     "index": int(row["original_index"]),
                                     "error": str(exc)[:200]})

        with ThreadPoolExecutor(concurrency) as ex:
            futs = {ex.submit(gen, r): r for r in todo}
            pending = set(futs)
            last_emit = 0.0
            while pending:
                if should_stop():
                    gen_stopped = True
                    for f in pending:
                        f.cancel()
                    break
                finished, pending = wait(pending, timeout=1.0,
                                         return_when=FIRST_COMPLETED)
                for f in finished:
                    row = futs[f]
                    try:
                        rec = f.result()
                    except Exception as e:  # noqa: BLE001
                        on_gen_fail(row, e)
                        continue
                    on_gen_done(row, rec)
                    gen_done_n += 1
                now = time.perf_counter()
                if now - last_emit > 0.5 or not pending:
                    el = now - t0
                    on_event("gen_progress", {
                        "done": gen_done_n, "total": len(todo),
                        "errors": len(gen_errors), "tokens": gen_tokens,
                        "elapsed_s": round(el, 1),
                        "tps": round(gen_tokens / el, 1) if el else 0.0,
                    })
                    last_emit = now
            for f in futs:
                f.cancel()
        el = time.perf_counter() - t0
        on_event("gen_end", {
            "done": gen_done_n, "errors": len(gen_errors),
            "elapsed_s": round(el, 1), "tokens": gen_tokens,
            "tps": round(gen_tokens / max(el, 1), 1), "stopped": gen_stopped,
        })
        if gen_errors:
            with open(out + ".gen_errors.json", "w") as f:
                json.dump(gen_errors, f, ensure_ascii=False, indent=2)

    gen_all = load_jsonl(gen_path, "original_index")
    gen_recs = {k: v for k, v in gen_all.items() if not v.get("incomplete")}
    incomplete_n = len(gen_all) - len(gen_recs)
    failed_n = max(len(df) - len(gen_all), 0)
    on_event("gen_summary", {
        "valid": len(gen_recs), "expected": len(df),
        "incomplete": incomplete_n, "failed": failed_n,
    })

    # ---------- 阶段二: 裁判 ----------
    judged = load_jsonl(judge_path, "original_index")
    to_judge = [r for r in gen_recs.values() if r["original_index"] not in judged]
    on_event("judge_start", {
        "model": judge_model, "cached": len(judged), "todo": len(to_judge),
        "concurrency": judge_concurrency,
        "thinking": thinking_label(judge_thinking, judge_reasoning_effort),
    })

    def judge(rec):
        out_text = judge_client.chat_json(
            judge_model,
            GRADER_TEMPLATE.format(
                question=rec["problem"],
                target=rec["answer"],
                predicted_answer=rec["prediction"] or "(empty)",
            ),
        )
        return {"original_index": rec["original_index"], "judge_output": out_text,
                "grade": parse_grade(out_text)}

    judge_errors, judge_done_n, judge_stopped = [], 0, False
    if to_judge:
        t0 = time.perf_counter()

        def on_judge_done(rec, jr):
            append_jsonl(judge_path, jr)

        def on_judge_fail(rec, exc):
            judge_errors.append((rec["original_index"], str(exc)[:200]))
            on_event("item_failed", {"stage": "judge",
                                     "index": rec["original_index"],
                                     "error": str(exc)[:200]})

        with ThreadPoolExecutor(judge_concurrency) as ex:
            futs = {ex.submit(judge, r): r for r in to_judge}
            pending = set(futs)
            last_emit = 0.0
            while pending:
                if should_stop():
                    judge_stopped = True
                    for f in pending:
                        f.cancel()
                    break
                finished, pending = wait(pending, timeout=1.0,
                                         return_when=FIRST_COMPLETED)
                for f in finished:
                    rec = futs[f]
                    try:
                        jr = f.result()
                    except Exception as e:  # noqa: BLE001
                        on_judge_fail(rec, e)
                        continue
                    on_judge_done(rec, jr)
                    judge_done_n += 1
                now = time.perf_counter()
                if now - last_emit > 0.5 or not pending:
                    on_event("judge_progress", {
                        "done": judge_done_n, "total": len(to_judge),
                        "errors": len(judge_errors),
                        "elapsed_s": round(now - t0, 1),
                    })
                    last_emit = now
            for f in futs:
                f.cancel()
        on_event("judge_end", {
            "done": judge_done_n, "errors": len(judge_errors),
            "elapsed_s": round(time.perf_counter() - t0, 1),
            "stopped": judge_stopped,
        })

    judged = load_jsonl(judge_path, "original_index")

    # ---------- 汇总 ----------
    records = []
    for idx, rec in gen_recs.items():
        j = judged.get(idx)
        if not j:
            continue
        rec = dict(rec)
        rec["grade"] = j["grade"]
        rec["judge_output"] = j.get("judge_output", "")
        records.append(rec)

    if not records:
        on_event("error", {"message": "no records to summarise"})
        return None

    summary = summarise(records, model, judge_model, data, len(df),
                        incomplete_n, failed_n)
    summary.update({
        "thinking": thinking or "server_default",
        "reasoning_effort": reasoning_effort or None,
        "judge_thinking": judge_thinking or "server_default",
        "judge_reasoning_effort": judge_reasoning_effort or None,
    })
    with open(out + ".json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(out + ".items.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary["_stopped"] = gen_stopped or judge_stopped
    on_event("summary", {"summary": summary, "records": len(records)})
    return summary
