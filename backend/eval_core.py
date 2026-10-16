"""大模型精度评测核心逻辑（数据集无关）。

CLI (simpleqa_eval.py) 与 Web 服务 (api.py) 共用同一套实现，
所有可变配置（端点/密钥/模型/并发/上限）均通过参数注入，不使用模块级全局常量。

数据集不再硬编码在代码里：清单与字段映射见 dataset/datasets.json（dataset_registry），
每个数据集以名称作为顶级目录放在 dataset/ 下。加载时按配置里的 reader/fields 把源表
映射为统一字段，题型判定与打分模板完全复用。
"""
import json
import logging
import os
import random
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import dataset_registry

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

# 裁判判定：前三种为模型作答结果，后两种为裁判未给出可用判定（无效作答）
GRADE_CORRECT = "CORRECT"
GRADE_INCORRECT = "INCORRECT"
GRADE_NOT_ATTEMPTED = "NOT_ATTEMPTED"
GRADE_JUDGE_EMPTY = "JUDGE_EMPTY"       # 裁判返回内容为空
GRADE_JUDGE_INVALID = "JUDGE_INVALID"   # 裁判有输出但不含可识别的判定标签
INVALID_GRADES = (GRADE_JUDGE_EMPTY, GRADE_JUDGE_INVALID)

GRADE_LABELS = {
    GRADE_CORRECT: "正确",
    GRADE_INCORRECT: "错误",
    GRADE_NOT_ATTEMPTED: "未作答",
    GRADE_JUDGE_EMPTY: "裁判空输出",
    GRADE_JUDGE_INVALID: "裁判无法判断",
}

# 默认数据集：来自 dataset/datasets.json 的 default（配置缺失时为空，由调用方兜底）
DEFAULT_DATA = dataset_registry.default_path()

# 数据集统一后的字段（_NORM_COLUMNS）：由配置里的 fields 映射而来，
# 未声明映射的字段按同名列读取，列不存在时填空值。
_NORM_COLUMNS = ("original_index", "problem", "answer", "topic", "answer_type",
                 "subject")


def available_datasets():
    """数据集候选（含文件是否存在、是否可用当前流程评测），供 Web 端下拉选择。

    清单、文件名通配符与字段映射均来自 dataset/datasets.json（见 dataset_registry），
    新增数据集不需要改代码。
    """
    return dataset_registry.datasets()


def default_data_path():
    """当前默认数据集路径（实时读取配置，供表单默认值与兜底使用）。"""
    return dataset_registry.default_path() or DEFAULT_DATA


def dataset_reader(path, spec=None):
    """返回 (读取方式, 字段映射)：优先用配置，自定义路径按扩展名兜底。"""
    if spec is None:
        spec = dataset_registry.spec_for_path(path)
    if spec:
        return (spec.get("reader") or "csv"), (spec.get("fields") or {})
    return ("parquet" if (path or "").lower().endswith((".parquet", ".pq"))
            else "csv"), {}


def dataset_label(path, spec=None):
    """数据集展示名：配置里的名称，自定义路径取所在目录名。"""
    if spec is None:
        spec = dataset_registry.spec_for_path(path)
    if spec and spec.get("name"):
        return spec["name"]
    return os.path.dirname(path or "").rstrip("/").split("/")[-1] or (path or "")


def _str_col(df, name, default=""):
    if name in df.columns:
        return df[name].fillna(default).astype(str)
    return pd.Series([default] * len(df), index=df.index)


def _mapped(fields, name, default):
    """统一字段对应的源列名：配置里声明过则用映射，否则按同名/默认列名。"""
    return (fields or {}).get(name) or default


def _row_group_bounds(pf):
    """返回每个 row group 的行号区间 [(start, end), ...]。"""
    bounds, start = [], 0
    for g in range(pf.num_row_groups):
        n = pf.metadata.row_group(g).num_rows
        bounds.append((start, start + n))
        start += n
    return bounds


def _read_hle_images(pf, indexes, on_event=None, col="image"):
    """仅读取包含目标行的 row group 的 image 列。

    HLE 的 image 为内嵌 base64（整表解压后超过 1GB），全量读取既慢且占用大量内存；
    按命中的 row group 精确读取后，限制题量时加载耗时从分钟级降至秒级。
    """
    need = sorted({int(i) for i in indexes})
    bounds = _row_group_bounds(pf)
    images, done = {}, 0
    for gi, (s, e) in enumerate(bounds):
        sel = [i for i in need if s <= i < e]
        if not sel:
            continue
        values = pf.read_row_group(gi, columns=[col]).column(col).to_pylist()
        for i in sel:
            v = values[i - s]
            if isinstance(v, str) and v.strip():
                images[i] = v
        done += len(sel)
        if on_event:
            on_event("load_progress", {
                "total": len(need), "loaded": done, "images": len(images),
                "row_group": gi + 1, "row_groups": pf.num_row_groups,
            })
    return images


def _load_hle(files, fields=None, limit=0, seed=0, on_event=None):
    """HLE 专用加载（reader="hle"）：按 row group 只读采样命中行的 image 列。"""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(files[0])
    names = pf.schema_arrow.names
    cols = {name: _mapped(fields, name, default) for name, default in (
        ("problem", "question"), ("answer", "answer"), ("topic", "category"),
        ("answer_type", "answer_type"), ("subject", "raw_subject"))}
    light = [c for c in dict.fromkeys(cols.values()) if c in names]

    raw = pf.read(columns=light).to_pandas()
    df = pd.DataFrame({
        "original_index": pd.Series(range(len(raw)), index=raw.index),
        "problem": _str_col(raw, cols["problem"]),
        "answer": _str_col(raw, cols["answer"]),
        "topic": _str_col(raw, cols["topic"], "Unknown"),
        "answer_type": _str_col(raw, cols["answer_type"], "exactMatch"),
        "subject": _str_col(raw, cols["subject"]),
    })
    del raw

    picked = df
    if limit and limit < len(df):
        picked = df.sample(n=limit, random_state=seed).sort_index()

    image_col = _mapped(fields, "image", "image")
    images = {}
    if image_col in names and len(picked):
        images = _read_hle_images(pf, picked["original_index"].tolist(), on_event,
                                  image_col)
    return picked[list(_NORM_COLUMNS)].reset_index(drop=True), images


def _normalise(raw, fields=None, limit=0, seed=0, on_event=None):
    """把原始表映射为统一字段（含采样与图片抽取），各 reader 共用。"""
    if "original_index" in raw.columns:
        idx = raw["original_index"].astype(int)
    else:
        idx = pd.Series(range(len(raw)), index=raw.index)
    df = pd.DataFrame({"original_index": idx})
    for col in ("problem", "answer", "topic", "answer_type", "subject"):
        df[col] = _str_col(raw, _mapped(fields, col, col),
                           "Unknown" if col == "topic" else "")
    image_col = _mapped(fields, "image", "image")
    images = ({int(i): v for i, v in _str_col(raw, image_col).items() if v}
              if image_col in raw.columns else {})
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


def _load_csv(files, fields=None, limit=0, seed=0, on_event=None):
    """CSV 数据集（多分片按文件名顺序拼接）。"""
    if len(files) == 1:
        raw = pd.read_csv(files[0])
    else:
        raw = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    return _normalise(raw, fields, limit, seed, on_event)


def _load_parquet(files, fields=None, limit=0, seed=0, on_event=None):
    """通用 parquet 数据集（多分片交由 pandas 拼接）。"""
    raw = pd.read_parquet(files if len(files) > 1 else files[0])
    return _normalise(raw, fields, limit, seed, on_event)


def load_dataset(path, limit=0, seed=0, on_event=None, spec=None):
    """读取数据集并归一化为统一字段（含采样）。

    path 为数据文件路径（页面上也可以填任意 csv/parquet 路径）；spec 为
    dataset_registry 里的数据集条目，缺省时按路径反查配置、再按扩展名兜底，
    读取方式与字段映射都来自配置，因此新增数据集不需要改代码。

    返回 (df, images)：df 含 _NORM_COLUMNS；images 为 {original_index: data-url}，
    仅当数据集含图片（HLE 多模态题）时非空，避免将 base64 复制进逐题记录。
    limit>0 时在此处完成采样，图片仅加载采样命中的行。
    """
    on_event = on_event or (lambda kind, payload: None)
    if spec is None:
        spec = dataset_registry.spec_for_path(path)
    reader, fields = dataset_reader(path, spec)
    files = [f for f in (spec or {}).get("files") or [] if os.path.isfile(f)]
    if not files:
        files = [path]
    on_event("load_start", {"path": path, "kind": reader,
                            "dataset": dataset_label(path, spec)})
    if reader == "hle":
        df, images = _load_hle(files, fields, limit, seed, on_event)
    elif reader == "parquet":
        df, images = _load_parquet(files, fields, limit, seed, on_event)
    else:
        df, images = _load_csv(files, fields, limit, seed, on_event)
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
    """估算可见回答的 token 数。思考内容不流式返回，只能依据可见文本反推。"""
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
    """将裁判输出解析为判定结果；无法解析时返回无效态，不归类为拒答。

    取最后一次出现的标签作为最终判定（模板要求末行给出结论）。
    """
    low = (text or "").strip().lower()
    if not low:
        return GRADE_JUDGE_EMPTY
    pos, grade = -1, None
    for label, name in (
        ("[correct]", GRADE_CORRECT),
        ("[incorrect]", GRADE_INCORRECT),
        ("[not_attempted]", GRADE_NOT_ATTEMPTED),
    ):
        idx = low.rfind(label)
        if idx > pos:
            pos, grade = idx, name
    return grade or GRADE_JUDGE_INVALID


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
    """统计被标记 incomplete 的次数，达到上限即视为终态，避免无限重跑。"""
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
              incomplete_n, failed_n, dataset_name=""):
    """将逐题记录汇总为指标字典。

    裁判空输出 / 裁判无法判断均为「无效作答」，不计入 Pass@1、拒答率等
    正确率类指标的分母；性能类指标仍按全部记录统计。
    """
    dfr = pd.DataFrame(records)
    n_records = len(records)
    je = int((dfr.grade == GRADE_JUDGE_EMPTY).sum())
    ji = int((dfr.grade == GRADE_JUDGE_INVALID).sum())
    unscored = je + ji
    # 有效计分集合：剔除两类裁判无效判定
    dfv = dfr[~dfr.grade.isin(INVALID_GRADES)]
    n = len(dfv)
    c = int((dfv.grade == GRADE_CORRECT).sum())
    ic = int((dfv.grade == GRADE_INCORRECT).sum())
    na = int((dfv.grade == GRADE_NOT_ATTEMPTED).sum())
    toks = dfr.completion_tokens.tolist()
    ttfts = dfr.ttft_s.tolist()
    tpots = (dfr.tpot_visible_s.dropna().tolist()
             if "tpot_visible_s" in dfr else [])
    walls = dfr.total_s.tolist()

    def pct(v, p):
        v = sorted(v)
        return v[min(int(len(v) * p), len(v) - 1)]

    p1 = (c / n) if n else 0.0
    half = 1.96 * ((p1 * (1 - p1) / n) ** 0.5) if n else 0.0
    summary = {
        "model": model,
        "judge_model": judge_model,
        "dataset": dataset_name or os.path.basename(data),
        "num_questions": n,
        "expected_questions": total_questions,
        "num_records": n_records,
        "coverage": (round(n_records / total_questions, 4)
                     if total_questions else 0.0),
        "num_incomplete": incomplete_n,
        "num_failed": failed_n,
        "num_unscored": unscored,
        "judge_empty": je,
        "judge_invalid": ji,
        "temperature": 0.0,
        "counts": {GRADE_CORRECT: c, GRADE_INCORRECT: ic,
                   GRADE_NOT_ATTEMPTED: na, GRADE_JUDGE_EMPTY: je,
                   GRADE_JUDGE_INVALID: ji},
        "pass_at_1": round(p1, 4),
        "pass_at_1_ci95": [round(p1 - half, 4), round(p1 + half, 4)],
        "accuracy_attempted": round(c / (c + ic), 4) if (c + ic) else None,
        "not_attempted_rate": round(na / n, 4) if n else 0.0,
        "judge_invalid_rate": (round(unscored / n_records, 4)
                               if n_records else 0.0),
        "output_tokens_avg": round(sum(toks) / n_records, 1),
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
        t: {"n": int(len(g)),
            "pass_at_1": round(float((g.grade == GRADE_CORRECT).mean()), 4)}
        for t, g in dfv.groupby("topic")
    }
    summary["by_answer_type"] = {
        t: {"n": int(len(g)),
            "pass_at_1": round(float((g.grade == GRADE_CORRECT).mean()), 4)}
        for t, g in dfv.groupby("answer_type")
    }
    return summary


# ---------------------------------------------------------------- 思考开关
# 三态："" 表示不传该字段（由服务端默认决定），其余为显式开关。
THINKING_CHOICES = ("", "enabled", "disabled")
EFFORT_CHOICES = ("", "low", "medium", "high", "max")


def build_think_extra(thinking, reasoning_effort=""):
    """将思考开关与强度转换为请求体附加字段。

    沿用 simpleqa_verified_deepseekv4.py 中已验证的约定：
    {"thinking": {"type": "enabled"|"disabled"}, "reasoning_effort": "low|medium|high|max"}。
    思考取值为空时不注入任何字段，保持服务端默认行为。
    """
    extra = {}
    if thinking:
        extra["thinking"] = {"type": thinking}
    if thinking == "enabled" and reasoning_effort:
        extra["reasoning_effort"] = reasoning_effort
    return extra


def thinking_label(thinking, reasoning_effort=""):
    """思考配置的可读描述，用于日志与汇总。"""
    if not thinking:
        return "服务端默认"
    if thinking == "disabled":
        return "关闭"
    return "开启（强度 {}）".format(reasoning_effort or "默认")


# ---------------------------------------------------------------- 请求日志
# 每次调用 LLM 都按行记录实际请求参数，便于排查各厂商字段/取值差异；
# 提示词正文、图片 data-url、API Key 一律不落日志，只保留长度摘要。
# ZHIPU_EVAL_LOG_REQ=0 可整体关闭。
_LOG_REQ = (os.environ.get("ZHIPU_EVAL_LOG_REQ") or "1").strip().lower() not in (
    "0", "false", "no", "off")

_TZ_LOCAL_NAMES = ("", "local", "system", "localtime")
_TZ_PLUS8_NAMES = ("asia/shanghai", "asia/chongqing", "asia/harbin", "asia/urumqi",
                   "prc")


def _display_tz():
    """日志时间按展示时区渲染（默认东八区），避免容器 UTC 环境下相差 8 小时。"""
    name = (os.environ.get("ZHIPU_EVAL_TZ") or "Asia/Shanghai").strip()
    if name.lower() in _TZ_LOCAL_NAMES:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 镜像缺 tzdata 时东八区退回固定 +08:00
        return timezone(timedelta(hours=8)) if name.lower() in _TZ_PLUS8_NAMES else None


class _TZFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s [%(levelname)s] %(message)s")
        self._tz = _display_tz()

    def formatTime(self, record, datefmt=None):
        dt = (datetime.fromtimestamp(record.created) if self._tz is None
              else datetime.fromtimestamp(record.created, self._tz))
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


logger = logging.getLogger("zhipu_eval")
if not logger.handlers:  # 自带 stderr handler，保证 INFO 在 uvicorn/CLI 下都可见
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(_TZFormatter())
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _brief(content):
    """请求日志不记录提示词正文，仅保留长度与结构摘要。"""
    if isinstance(content, str):
        return "<prompt omitted: {} chars>".format(len(content))
    if isinstance(content, list):
        parts = []
        for item in content:
            kind = (item or {}).get("type")
            if kind == "text":
                text = item.get("text") or ""
                parts.append({"type": "text",
                              "text": "<prompt omitted: {} chars>".format(len(text))})
            elif kind == "image_url":
                raw = item.get("image_url")
                url = raw.get("url", "") if isinstance(raw, dict) else (raw or "")
                parts.append({"type": "image_url",
                              "image_url": "<image omitted: {} chars>".format(len(url))})
            else:
                parts.append({"type": kind or "unknown"})
        return parts
    return "<empty>"


def redact_payload(payload):
    """复制请求体，仅将 messages 正文替换为摘要，其余参数保持原样。"""
    out = dict(payload)
    out["messages"] = [dict(m, content=_brief(m.get("content")))
                       for m in payload.get("messages") or []]
    return out


# ---------------------------------------------------------------- HTTP 客户端
class HttpClient:
    """按 endpoint/api_key 参数化的流式与非流式调用封装。"""

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
        """402/429 采用较长退避；5xx 属上游抖动，采用短退避快速重试。"""
        if status in (429, 402):
            return min(5.0 * (k + 1), 60.0) + random.random()
        if status in (500, 502, 503, 504):
            return min(1.0 + 0.7 * k, 15.0) + random.random()
        return min(1.5 + 0.8 * k, 25.0) + random.random()

    # 402/408/425/429 仍属可重试：欠费限流、请求超时、过早请求、限速
    RETRYABLE_4XX = (402, 408, 425, 429)

    @classmethod
    def is_fatal(cls, e):
        """4xx 客户端错误（限流/超时类除外）属永久失败，重试无意义。"""
        status = getattr(e, "status", None)
        return (status is not None and 400 <= status < 500
                and status not in cls.RETRYABLE_4XX)

    @staticmethod
    def _content(prompt, image=None):
        """含图题目使用多模态消息体（data-url），纯文本题目保持字符串以兼容旧服务。"""
        if not image:
            return prompt
        return [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image}},
        ]

    def _log_request(self, kind, payload, tag=""):
        """记录一次实际发出的请求参数（提示词仅记录长度）。"""
        if not _LOG_REQ:
            return
        try:
            body = json.dumps(redact_payload(payload), ensure_ascii=False)
        except Exception:  # noqa: BLE001 日志失败不影响调用
            body = "<payload not serialisable>"
        logger.info("[LLM请求]%s %s %s %s", " " + tag if tag else "", kind,
                    self.endpoint, body)

    def _post_stream(self, model, prompt, max_tokens, temperature=0.0, image=None,
                     tag=""):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": self._content(prompt, image)}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self.extra,
        }
        self._log_request("stream", payload, tag)
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
                return self._post_stream(model, prompt, max_tokens, image=image,
                                         tag=tag)
            except Exception as e:  # noqa: BLE001
                last = e
                if self.is_fatal(e):
                    raise RuntimeError(
                        f"stream failed (fatal {getattr(e, 'status', None)}, "
                        f"no retry): {e} {tag}") from e
                wait_s = self.backoff(k, getattr(e, "status", None))
                if on_retry:
                    on_retry(k, e, wait_s)
                time.sleep(wait_s)
        raise RuntimeError(f"stream failed after {retries}: {last} {tag}")

    def chat_json(self, model, prompt, max_tokens=None, retries=8, temperature=0.0,
                  tag=""):
        """裁判（非流式）调用。max_tokens 为 None 时不传该字段，使用服务端默认值。"""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        self._log_request("json", payload, tag)
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
                if self.is_fatal(e):
                    raise RuntimeError(
                        f"chat failed (fatal {getattr(e, 'status', None)}, "
                        f"no retry): {e}") from e
                time.sleep(self.backoff(k, getattr(e, "status", None)))
        raise RuntimeError(f"chat failed after {retries}: {last}")


# ---------------------------------------------------------------- 主流程
def run_eval(cfg, on_event=None, should_stop=None):
    """执行一次完整评测（生成 -> 裁判 -> 汇总）。

    cfg 必填：endpoint, api_key, model, judge_model, out
    cfg 可选: judge_endpoint, judge_api_key（留空则复用被测模型的端点/密钥）、
             data, concurrency, judge_concurrency, limit, seed,
             max_tokens, max_tokens_cap, max_incomplete_attempts
    返回 summary dict；无可用记录时返回 None。
    """
    on_event = on_event or (lambda kind, payload: None)
    should_stop = should_stop or (lambda: False)

    model = cfg["model"]
    judge_model = cfg["judge_model"]
    data = cfg.get("data") or default_data_path()
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
    # 裁判模型可使用独立端点/密钥，留空时回退到被测模型配置
    judge_endpoint = str(cfg.get("judge_endpoint") or cfg["endpoint"])
    judge_api_key = str(cfg.get("judge_api_key") or cfg["api_key"])
    judge_client = HttpClient(
        judge_endpoint, judge_api_key, should_stop=should_stop,
        extra=build_think_extra(judge_thinking, judge_reasoning_effort))

    df, images = load_dataset(data, limit=limit, seed=seed, on_event=on_event)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    gen_path, judge_path = out + ".gen.jsonl", out + ".judge.jsonl"

    # ---------- 阶段一：生成 ----------
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
        "dataset": dataset_label(data),
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
            # 思考占满上限说明答案未写出，翻倍上限后重试一轮
            if finish == "length" and mt < max_tokens_cap:
                on_event("truncated", {"tag": tag, "hit": mt, "retry": mt * 2})
                mt = min(mt * 2, max_tokens_cap)
                continue
            break
        # 思考 token 不流式输出，分母需使用可见文本的 token 数
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
    gen_tps_sum, gen_tps_n = 0.0, 0

    def gen_tps():
        """单请求平均 TPS：逐题 completion_tokens / total_s 的均值。"""
        return round(gen_tps_sum / gen_tps_n, 1) if gen_tps_n else 0.0

    if todo:
        t0 = time.perf_counter()

        def on_gen_done(row, rec):
            append_jsonl(gen_path, rec)
            nonlocal gen_tokens, gen_tps_sum, gen_tps_n
            gen_tokens += rec["completion_tokens"]
            if rec["total_s"]:
                gen_tps_sum += rec["completion_tokens"] / rec["total_s"]
                gen_tps_n += 1

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
                        "tps": gen_tps(),
                    })
                    last_emit = now
            for f in futs:
                f.cancel()
        el = time.perf_counter() - t0
        on_event("gen_end", {
            "done": gen_done_n, "errors": len(gen_errors),
            "elapsed_s": round(el, 1), "tokens": gen_tokens,
            "tps": gen_tps(), "stopped": gen_stopped,
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

    # ---------- 阶段二：裁判 ----------
    judged = load_jsonl(judge_path, "original_index")
    to_judge = [r for r in gen_recs.values() if r["original_index"] not in judged]
    on_event("judge_start", {
        "model": judge_model, "cached": len(judged), "todo": len(to_judge),
        "concurrency": judge_concurrency, "endpoint": judge_endpoint,
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
            tag="j{}".format(rec["original_index"]),
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
                        incomplete_n, failed_n, dataset_label(data))
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
