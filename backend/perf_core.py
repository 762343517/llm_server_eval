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

长序列 / Prefix Cache 场景（`prompt_mode=long`）用 evalscope 的另外两个数据集：
- `--dataset random`：按 tokenizer 精确生成 `--prefix-length` 个**所有请求共享**的前缀
  token，后缀长度由 `--min/max-prompt-length` 控制，因此
  序列长度 = 前缀长度 + 后缀长度、Prefix Cache 命中率 = 前缀长度 / 序列长度；
- `--dataset line_by_line` + `--dataset-args '{"prefix_file": ..., "target_input_len": N}'`：
  逐行读取提问文本，把 `prefix_file` 的内容（过短时重复平铺）注入为系统消息前缀，
  使每次请求的**前缀内容完全一致**、整段输入恰好 N 个 token。
两种数据集都是单轮请求，`--number` 即请求数（不按会话数 × 轮次换算）。
"""
import json
import math
import os
import random
import re
import shutil
import socket
import sqlite3
import statistics
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import consts
import utils

CLI_NAME = "evalscope"
API_NAME = "openai"                       # 被测服务统一按 OpenAI 兼容协议访问
DATASET_NAME = "custom_multi_turn"        # 自定义会话数据集（支持多轮）
DATASET_FILE = "dataset.jsonl"            # 自定义会话数据集的文件名
DEFAULT_API_PATH = "/v1/chat/completions"
RESULT_DB_NAME = "benchmark_data.db"
RUN_NAME = "perf"

# evalscope 原始产物（写在 <run_dir>/perf 下）：(文件名, 展示名, 类型)
# 类型 text 供在线文本预览，html 供内嵌渲染；三者均支持下载。
RESULT_FILES = (
    ("benchmark.log", "evalscope 运行日志", "text"),
    ("performance_summary.txt", "性能汇总报告", "text"),
    ("perf_report.html", "可视化报告", "html"),
)

# 平台自己补的两份排障产物（evalscope 自身不产出）：
#   evalscope_stderr.log —— 子进程 stderr 原样落盘（Traceback / 崩溃栈都在这里）；
#   failed_requests.log  —— 失败请求逐条明细，含重放取回的 HTTP 状态码与响应体摘要。
STDERR_FILE = "evalscope_stderr.log"
FAILED_FILE = "failed_requests.log"
RESULT_FILES = RESULT_FILES + (
    (STDERR_FILE, "evalscope stderr（异常栈）", "text"),
    (FAILED_FILE, "失败请求明细（含 HTTP 状态码）", "text"),
)

# 请求模式：chat = 自定义会话提问；long = 长序列 + 固定前缀（Prefix Cache 命中率）
MODE_CHAT = "chat"
MODE_LONG = "long"
PROMPT_MODES = (MODE_CHAT, MODE_LONG)
# long 模式的两种 evalscope 数据集：
#   random      —— 按 token 精确生成「固定共享前缀 + 变长后缀」，前缀长度可控；
#   line_by_line—— 由本地文本逐行提供提问，配合 prefix_file 注入「固定的前缀内容」。
DATASET_RANDOM = "random"
DATASET_LINE = "line_by_line"
LONG_DATASET_FILE = "prompts.txt"         # line_by_line 的逐行提问文件
PREFIX_FILE = "prefix.txt"                # 固定前缀内容落盘文件
TOKEN_SUFFIXES = (("k", 1000), ("m", 1000000))   # 序列长度写法：128K = 128000

# 分词器索引：model/ 下的扫描结果缓存（见 list_tokenizers），避免每次请求都遍历目录、
# 解析 tokenizer_config.json（分词器目录可达数十 MB，重复扫盘对接口延迟影响明显）。
TOKENIZER_INDEX_NAME = "tokenizers.json"
TOKENIZER_INDEX_VERSION = 2                    # 索引结构版本，结构变更时 +1 触发重建
GENERATED_TEMPLATE_FILE = "chat_template.jinja"    # 缺对话模板时自动补齐的文件名

# 逐行提交的缓冲行数：越小实时性越好，越大写入开销越小。
# 取 1 是为了让运行中的进度/明细能立即读到（evalscope 的写入在独立线程，开销可控）。
DB_COMMIT_INTERVAL = 1
# 结果库读取：单次最多读多少行、拿锁最多等多久。
# 一条 128K 请求的行含整段 prompt（实测约 1MB），而 evalscope 用默认 rollback
# journal 建库、默认 5s 的 busy_timeout：平台侧若用 COUNT(*) / OFFSET 做游标，
# 每次轮询都要扫全表、长时间持有 SHARED 锁，evalscope 的 `con.commit()` 等不到
# EXCLUSIVE 锁就抛 "sqlite3.OperationalError: database is locked" 并中断整轮压测。
# 因此按 rowid 做 keyset 分页、只读需要的列、单次限量，把每个读事务压到毫秒级。
#
# 批大小按「一行 ~1MB」定：`request` 是表的第 1 列，任何后续列都要顺着该行的溢出页链
# 跳过去，冷缓存下实测约 10ms/行（518 行 / 434MB 的库全读 5.2s）。批 512 行时单个读
# 事务就有 5s，正好等于 evalscope 的 busy_timeout，仍会把它的 commit 堵死；批 64 行
# 约 0.6s，留足安全边界。会话模式行很小，64 行仍是毫秒级。
READ_BATCH = 64
READ_TIMEOUT = 0.5
# 一次增量调用内最多连续读多久（秒）：读完一批就检查预算，超了留到下一轮轮询，
# 避免短上下文高 RPS 时积压的行数把单次调用拖长。
READ_BUDGET = 0.25
# unique_prefix 打开时，每条请求注入的随机前缀长度（字符）
UNIQUE_PREFIX_CHARS = 1024
# 单条 prompt 的长度上限（字符，evalscope 默认值）
MAX_PROMPT_LENGTH = 131072

# 失败诊断：evalscope 的 `status_code` / `error` 只存在内存里（result 表没有这两列，
# benchmark.log 也只记 INFO），失败原因只能靠「重放同一份请求体」取回 HTTP 状态码。
# 重放放在整轮压测结束之后、只做几次，且不进入指标统计；PERF_PROBE_LIMIT=0 可关闭。
PROBE_LIMIT = 3
PROBE_TIMEOUT = 60.0
PROBE_BODY_CHARS = 500                        # 响应体摘要保留的字符数
# 长序列 + 固定前缀内容模式下，未填写提问内容时使用的默认问题
DEFAULT_LONG_QUESTION = "请用一句话介绍你自己。"
# 启动前目标连通性预检的超时（秒）：只做一次 TCP 握手，不发 HTTP、不消耗 token。
# 用于在 evalscope 的 connect_test 之前给出结论，避免地址填错时白等 --total-timeout。
PRECHECK_TIMEOUT = 3.0

# 汇总指标口径版本，写进任务快照；版本落后时由 perf_manager 用结果库重算并回写。
# v2：分位由 quantiles 插值改为最近秩（与 benchmark.log 一致）；
#     新增 overall_tokens_per_second（含输入的总吞吐）。
STATS_VERSION = 2

_PROBE = {"at": 0.0, "info": None}
_PROBE_TTL = 60.0

_LEVEL_RE = re.compile(r"parallel_(\d+)_number_(\d+)")


# --------------------------------------------------------------------- 可执行文件
def resolve_bin():
    """定位 evalscope：环境变量 > PATH > 当前解释器同目录 > 常见安装位置。"""
    raw = (os.environ.get("EVALSCOPE_BIN") or "").strip()
    if raw:
        if os.path.isfile(raw) and os.access(raw, os.X_OK):
            return raw
        return shutil.which(raw)
    found = shutil.which(CLI_NAME)
    if found:
        return found
    # pip install 装进项目 venv 时，可执行文件与解释器同级（.venv/bin/evalscope）；
    # 服务由 start.sh 用该解释器启动，但 venv 未必在 PATH 里，因此单独看这里。
    local_bin = os.path.join(os.path.dirname(sys.executable), CLI_NAME)
    for cand in (local_bin, "~/.local/bin/evalscope", "/usr/local/bin/evalscope"):
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def command_prefix():
    """命令前缀：优先定位到可执行文件，退化为在当前解释器里调入口函数。

    evalscope 没有 `__main__.py`，`python -m evalscope` 会直接报
    "No module named evalscope.__main__"，因此兜底走 console_scripts 的入口函数。
    """
    path = resolve_bin()
    if path:
        return [path]
    return [sys.executable, "-c",
            "import sys; from evalscope.cli.cli import run_cmd; sys.exit(run_cmd())"]


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
                info["path"] = "{}（内置模块调用）".format(sys.executable)
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


class TargetUnreachable(RuntimeError):
    """压测目标不可达（连接被拒 / 超时 / 域名解析失败）。"""


def precheck_target(endpoint, timeout=PRECHECK_TIMEOUT):
    """启动前预检目标可达性，返回 (ok, message)。

    只做一次 TCP 握手：不发 HTTP 请求、不消耗 token、不在目标服务留下业务记录。
    连接被拒 / 超时 / 域名解析失败都能即刻给出结论，省掉 evalscope connect_test
    等满 `--total-timeout` 的时间（连不上时它会一直等到超时）。
    """
    try:
        url = normalize_endpoint(endpoint)
    except ValueError as exc:
        return False, str(exc)
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        return False, "端口号不合法：{}".format(endpoint)
    port = port or (443 if parts.scheme == "https" else 80)
    target = "{}:{}".format(host, port)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except socket.gaierror as exc:
        return False, "无法解析域名 {}：{}".format(host, exc)
    except socket.timeout:
        return False, ("连接 {} 超时（{} 秒）：目标服务未启动、端口不通或网络不可达"
                       .format(target, timeout))
    except ConnectionRefusedError:
        return False, ("{} 拒绝连接：该端口没有服务在监听，请确认目标服务已启动、"
                       "地址与端口填写正确".format(target))
    except ConnectionResetError:
        return False, "{} 连接被重置，可能被防火墙或代理拦截".format(target)
    except OSError as exc:
        return False, "无法连接 {}：{}".format(target, exc)


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


def numbers(cfg):
    """evalscope `--number` 列表，与 levels(cfg) 同序一一对应。

    `--number` 为本档要发出的请求总数（chat 多轮模式下为会话数），由后端按
    「并发数 × 每个并发的请求数（cfg.iterations）」换算后写入快照；快照缺少
    numbers（早期任务）时按同一公式现算，保证历史任务仍可复现。
    """
    users = levels(cfg)
    raw = cfg.get("numbers") or []
    if len(raw) == len(users):
        return [max(1, int(x)) for x in raw]
    legacy = max(1, int(cfg.get("iterations") or 1))
    return [max(1, int(u)) * legacy for u in users]


def expected_requests(cfg):
    """本次压测预计发出的请求总数，用于计算进度百分比。

    chat 模式 = 会话数 × 轮次；long 模式为单轮请求，不再乘轮次。
    """
    turns = 1 if prompt_mode(cfg) == MODE_LONG else max(1, int(cfg.get("turns") or 1))
    return sum(numbers(cfg)) * turns


# --------------------------------------------------------------------- 长序列模式
def prompt_mode(cfg):
    """归一化请求模式：chat（自定义会话）/ long（长序列 + 固定前缀）。"""
    mode = str(cfg.get("prompt_mode") or MODE_CHAT).strip().lower()
    return MODE_LONG if mode == MODE_LONG else MODE_CHAT


def parse_tokens(value, default=0):
    """解析 token 长度：支持 128000 / "128000" / "128K" / "1M"（K=1000、M=1000000）。"""
    if value is None or value == "":
        return max(0, int(default))
    if isinstance(value, bool):
        return max(0, int(default))
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().lower().replace(",", "").replace("_", "")
    if not text:
        return max(0, int(default))
    factor = 1
    for suffix, mult in TOKEN_SUFFIXES:
        if text.endswith(suffix):
            factor, text = mult, text[: -len(suffix)]
            break
    try:
        return max(0, int(float(text) * factor))
    except ValueError:
        raise ValueError("token 长度格式不合法：{}".format(value))


def seq_length(cfg):
    """序列长度（输入 tokens）。"""
    return parse_tokens(cfg.get("seq_length"), 0)


def prefix_length(cfg):
    """固定共享前缀长度（tokens）：显式值优先，否则按 Prefix Cache 命中率换算。"""
    total = seq_length(cfg)
    fixed = parse_tokens(cfg.get("prefix_length"), 0)
    if fixed > 0:
        return min(fixed, total)
    ratio = max(0.0, min(1.0, float(cfg.get("prefix_cache_ratio") or 0.0)))
    return int(total * ratio)


def suffix_length(cfg):
    """变长后缀长度（tokens）= 序列长度 - 固定前缀长度。"""
    return max(0, seq_length(cfg) - prefix_length(cfg))


def cache_hit_ratio(cfg):
    """实际 Prefix Cache 命中率（前缀长度 / 序列长度）。"""
    total = seq_length(cfg)
    return (prefix_length(cfg) / total) if total else 0.0


def output_length(cfg):
    """输出 tokens：显式配置优先，否则沿用 max_tokens。"""
    out = parse_tokens(cfg.get("output_length"), 0)
    return out if out > 0 else max(1, int(cfg.get("max_tokens") or 512))


def prefix_content(cfg):
    """用户填写的固定前缀内容（可为空）。"""
    return str(cfg.get("prefix_content") or "").strip()


def long_dataset(cfg):
    """long 模式的 evalscope 数据集名。

    填写了固定前缀内容时用 line_by_line + prefix_file（前缀内容完全一致），
    否则用 random（由 evalscope 生成所有请求共享的固定前缀，长度精确可控）。
    """
    return DATASET_LINE if prefix_content(cfg) else DATASET_RANDOM


def prefix_file_path(run_dir):
    return os.path.join(str(run_dir), PREFIX_FILE)


def dataset_file(cfg, run_dir):
    """长序列模式下载荷文件的落盘路径（chat 模式固定为 dataset.jsonl）。"""
    if prompt_mode(cfg) == MODE_LONG and long_dataset(cfg) == DATASET_LINE:
        return os.path.join(str(run_dir), LONG_DATASET_FILE)
    return os.path.join(str(run_dir), DATASET_FILE)


# ------------------------------------------------------- 对话模板与分词器索引
# 自动生成的兜底模板：只在分词器目录既没有 chat_template.jinja、tokenizer_config.json
# 也没有 chat_template 字段时按目录名族系生成，仅用于 evalscope 在客户端估算 prompt
# token 数（不改变分词表，也不改变发给服务端的请求体）。
DEEPSEEK_CHAT_TEMPLATE = (
    "{% if not add_generation_prompt is defined %}"
    "{% set add_generation_prompt = false %}{% endif %}"
    "{{ bos_token }}"
    "{% for message in messages %}"
    "{% if message['role'] == 'user' %}{{ 'User: ' + message['content'] + '\\n\\n' }}"
    "{% elif message['role'] == 'assistant' %}"
    "{{ 'Assistant: ' + message['content'] + eos_token }}"
    "{% elif message['role'] == 'system' %}{{ message['content'] + '\\n\\n' }}"
    "{% endif %}"
    "{% endfor %}"
    "{% if add_generation_prompt %}{{ 'Assistant:' }}{% endif %}"
)
CHATML_CHAT_TEMPLATE = (
    "{% for message in messages %}"
    "{{ '<|im_start|>' + message['role'] + '\\n' + message['content'] + "
    "'<|im_end|>' + '\\n' }}"
    "{% endfor %}"
    "{% if add_generation_prompt %}{{ '<|im_start|>assistant\\n' }}{% endif %}"
)
# 目录名（小写）包含关键字即视为该族系；未命中时用 ChatML 兜底
TEMPLATE_FAMILIES = (("deepseek", DEEPSEEK_CHAT_TEMPLATE),)
FALLBACK_TEMPLATE_FAMILY = "chatml"
GENERATED_TEMPLATE_MARK = "自动生成的对话模板"
GENERATED_TEMPLATE_HEADER = """{#-
""" + GENERATED_TEMPLATE_MARK + """（llm_server_eval，族系：{family}）。

本目录原本没有对话模板（tokenizer_config.json 无 chat_template 字段，也没有
chat_template.jinja），而 evalscope 按 token 生成长序列时必须在客户端套用一次模板来
估算 prompt token 数，缺失会直接报错，因此按最接近该词表的族系模板自动补齐。

该模板只影响客户端计数，不会改变分词表与请求体。若与线上服务实际使用的模板不一致，
请直接用官方模板覆盖本文件。
-#}
"""

_INDEX_LOCK = threading.Lock()
_INDEX_CACHE = {"signature": None, "items": None}
_TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def _template_text_ok(text):
    """jinja 文件是否含有效模板：去掉注释块后仍有内容才算有模板。"""
    return bool(_TEMPLATE_COMMENT.sub("", text).strip())


def _template_source(path):
    """已有对话模板的来源：tokenizer_config / file / generated；都没有时返回 None。"""
    config = os.path.join(path, "tokenizer_config.json")
    if os.path.isfile(config):
        try:
            with open(config, encoding="utf-8") as fh:
                if str(json.load(fh).get("chat_template") or "").strip():
                    return "tokenizer_config"
        except (OSError, ValueError):
            return "tokenizer_config"       # 读不出来时不误判，交给 evalscope 报错
    target = os.path.join(path, GENERATED_TEMPLATE_FILE)
    if not os.path.isfile(target):
        return None
    try:
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return "file"
    if not _template_text_ok(text):
        return None         # 空文件 / 只剩注释：视为没有模板，交给 ensure 重新生成
    return "generated" if GENERATED_TEMPLATE_MARK in text else "file"


def has_chat_template(path):
    """本地分词器目录是否带对话模板（tokenizer_config.json 字段或 chat_template.jinja）。"""
    return _template_source(path) is not None


def _family_template(name):
    """按目录名挑一个族系兜底模板，返回 (模板文本, 族系名)。"""
    lowered = name.lower()
    for family, template in TEMPLATE_FAMILIES:
        if family in lowered:
            return template, family
    return CHATML_CHAT_TEMPLATE, FALLBACK_TEMPLATE_FAMILY


def ensure_chat_template(path):
    """确保分词器目录带对话模板：缺失时按族系生成 chat_template.jinja。

    返回模板来源（tokenizer_config / file / generated）；生成失败（目录不可写）时返回
    None，由调用方决定是否拦截。evalscope 缺少模板会直接报
    `tokenizer.chat_template is not set`，所以这里主动补齐。
    """
    source = _template_source(path)
    if source:
        return source
    name = os.path.basename(os.path.normpath(path))
    template, family = _family_template(name)
    target = os.path.join(path, GENERATED_TEMPLATE_FILE)
    tmp = target + ".tmp"
    payload = GENERATED_TEMPLATE_HEADER.replace("{family}", family) + template + "\n"
    try:
        # 先写临时文件再原子替换：避免中断时留下半截模板被当成可用模板
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return "generated"


def _tokenizer_signature():
    """model/ 的轻量指纹（子目录名 + mtime），用于判断索引缓存是否失效。

    只做 scandir + stat，不打开任何分词器文件，因此即使每次调用开销也可忽略。
    """
    signature = {}
    try:
        with os.scandir(consts.MODEL_DIR) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        signature[entry.name] = entry.stat().st_mtime_ns
                except OSError:
                    continue
    except OSError:
        return {}
    return signature


def _index_file():
    return os.path.join(consts.MODEL_DIR, TOKENIZER_INDEX_NAME)


def _read_index(signature):
    """读取 model/tokenizers.json；版本或指纹不匹配时返回 None（触发重建）。"""
    try:
        with open(_index_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if data.get("version") != TOKENIZER_INDEX_VERSION:
        return None
    if data.get("signature") != signature or not isinstance(data.get("tokenizers"), list):
        return None
    return data["tokenizers"]


def _write_index(signature, items):
    """把扫描结果写回配置文件；写失败不影响功能（索引只是缓存）。"""
    payload = {
        "version": TOKENIZER_INDEX_VERSION,
        "root": consts.MODEL_DIR,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "signature": signature,
        "tokenizers": items,
    }
    try:
        with open(_index_file(), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError:
        pass


def _scan_tokenizers():
    """真正扫盘：枚举 model/ 下可用分词器，并为缺模板的目录生成对话模板。"""
    items = []
    try:
        names = sorted(os.listdir(consts.MODEL_DIR))
    except OSError:
        return items
    for name in names:
        path = os.path.join(consts.MODEL_DIR, name)
        if not os.path.isdir(path):
            continue
        if not any(os.path.isfile(os.path.join(path, f))
                   for f in ("tokenizer.json", "tokenizer_config.json")):
            continue
        source = ensure_chat_template(path)
        items.append({"name": name, "path": path,
                      "has_chat_template": source is not None,
                      "template_source": source or "missing"})
    # 带对话模板的排前面：长序列模式要求分词器能套模板估算 prompt token 数
    items.sort(key=lambda it: (not it["has_chat_template"], it["name"]))
    return items


def list_tokenizers(force=False):
    """枚举 model/ 目录下可用作分词器的模型目录（含 tokenizer.json 或 tokenizer_config.json）。

    分词器根目录固定为项目内的 model/（consts.MODEL_DIR）：页面下拉框的候选来自这里，
    提交时也只接受该目录下的本地路径，避免分词器散落各处导致压测口径不一致。

    结果走三级缓存，避免每次请求都扫盘：内存 → model/tokenizers.json（跨进程复用）→
    重新扫描并回写索引。目录新增/改名/删除（指纹变化）时自动重建；force=True 强制重扫。
    """
    signature = _tokenizer_signature()
    with _INDEX_LOCK:
        cached = _INDEX_CACHE["items"]
        if not force and cached is not None and _INDEX_CACHE["signature"] == signature:
            items = cached
        else:
            items = None if force else _read_index(signature)
            if items is None:
                items = _scan_tokenizers()
                # 生成模板会改写子目录 mtime，这里重算指纹，避免下次请求又判定失效
                signature = _tokenizer_signature()
                _write_index(signature, items)
            _INDEX_CACHE["signature"] = signature
            _INDEX_CACHE["items"] = items
        return [dict(it) for it in items]


def default_tokenizer():
    """默认分词器：model/ 下第一个带对话模板的目录，没有候选时退回常量默认值。"""
    items = list_tokenizers()
    for item in items:
        if item["has_chat_template"]:
            return item["path"]
    return items[0]["path"] if items else consts.DEFAULT_STRESS_TOKENIZER_PATH


def tokenizer_in_root(path):
    """本地路径是否位于固定的分词器根目录 model/ 之内。"""
    root = os.path.realpath(consts.MODEL_DIR)
    real = os.path.realpath(path)
    try:
        return os.path.commonpath([root, real]) == root
    except ValueError:                  # 不同盘符等极端情况
        return False


def validate_tokenizer(tokenizer):
    """提前校验分词器目录，避免 evalscope 子进程里抛出难以定位的 traceback。

    只检查绝对路径（模型 ID 形式的远程分词器无法在本地校验）。evalscope 会用
    `--tokenizer-path` 指向的分词器在客户端套用一次对话模板来估算 prompt token 数
    （random 数据集必须扣除模板开销），因此分词器目录必须带 chat template：目录里没有
    模板时由 `ensure_chat_template` 按族系自动生成 chat_template.jinja（见
    list_tokenizers 的说明），只有目录不可写才会在这里拦截。
    """
    if not os.path.isabs(tokenizer):
        return
    if not tokenizer_in_root(tokenizer):
        raise ValueError(
            "分词器只能使用项目 model/ 目录下的模型：{}（请把分词器放到该目录，"
            "或在页面上从下拉框选择）".format(consts.MODEL_DIR))
    if not os.path.isdir(tokenizer):
        raise ValueError("分词器路径不存在：{}".format(tokenizer))
    if not ensure_chat_template(tokenizer):
        raise ValueError(
            "分词器目录缺少对话模板且无法自动生成（目录不可写？）：evalscope 需要它在"
            "客户端估算 prompt token 数，请确认 {} 可写，或手动添加 "
            "chat_template.jinja（或 tokenizer_config.json 的 chat_template 字段）".format(
                tokenizer))


def validate(cfg):
    """启动前的配置校验：不合法时抛 ValueError（由调用方转 400）。"""
    if not (cfg.get("model") or "").strip():
        raise ValueError("模型名不能为空")
    if prompt_mode(cfg) != MODE_LONG:
        conversation_messages(cfg)          # 提问内容不能为空
        return
    tokenizer = str(cfg.get("tokenizer_path") or "").strip()
    if not tokenizer:
        raise ValueError("长序列模式需要填写分词器路径（tokenizer_path），"
                         "evalscope 按 token 精确生成长序列时必须提供")
    validate_tokenizer(tokenizer)

    total = seq_length(cfg)
    if total <= 0:
        raise ValueError("序列长度必须大于 0")
    fixed = parse_tokens(cfg.get("prefix_length"), 0)
    if fixed > total:
        raise ValueError("固定前缀长度不能大于序列长度")
    if prefix_content(cfg) and fixed >= total:
        raise ValueError("固定前缀长度必须小于序列长度（否则没有变长后缀）")
    if output_length(cfg) <= 0:
        raise ValueError("输出长度必须大于 0")


def prepare(cfg, run_dir):
    """落盘本模式所需的全部载荷文件（chat 会话、long 的 prefix.txt + 提问文本）。"""
    if prompt_mode(cfg) == MODE_LONG and long_dataset(cfg) == DATASET_RANDOM:
        return None                     # random 数据集由 evalscope 自行生成
    path = os.path.abspath(dataset_file(cfg, run_dir))
    build_dataset(cfg, path)
    return path


# --------------------------------------------------------------------- 数据集 / 命令行
def build_dataset(cfg, path):
    """写 evalscope 数据集（按模式选择内容）。

    - chat：JSONL，每行一条会话 messages 数组。evalscope 会循环使用数据集，因此默认
      只写一行即可；打开 unique_prefix 时按预计会话数写同样多行，让每条请求都带不同
      的随机前缀；
    - long + 固定前缀内容：同目录写 prefix.txt（固定前缀），path 逐行写提问（纯文本），
      交给 evalscope 的 line_by_line + prefix_file 注入；
    - long + 共享随机前缀：由 evalscope 自行生成，不落任何数据文件。
    """
    if prompt_mode(cfg) == MODE_LONG:
        if long_dataset(cfg) == DATASET_RANDOM:
            return path
        return _build_long_dataset(cfg, path)
    rows = max(numbers(cfg))
    if not bool(cfg.get("unique_prefix")):
        rows = 1
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(rows):
            messages = conversation_messages(cfg)
            if rows > 1:
                _inject_prefix(messages)
            f.write(json.dumps(messages, ensure_ascii=False) + "\n")
    return path


def _build_long_dataset(cfg, path):
    """long + 固定前缀内容：写 prefix.txt（固定前缀）与提问文本文件。"""
    content = prefix_content(cfg)
    if not content:
        raise ValueError("固定前缀内容不能为空")
    run_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(run_dir, exist_ok=True)
    with open(prefix_file_path(run_dir), "w", encoding="utf-8") as f:
        f.write(content if content.endswith("\n") else content + "\n")
    question = (cfg.get("prompt") or "").strip() or DEFAULT_LONG_QUESTION
    # 每条请求的提问（= 前缀之后的可变后缀）都不同，避免后缀也整体命中缓存
    rows = max(numbers(cfg))
    with open(path, "w", encoding="utf-8") as f:
        for i in range(rows):
            f.write("{}{}\n".format(question, "" if rows == 1 else "（第 {} 条请求）".format(i + 1)))
    return path


def build_command(cfg, run_dir, dataset_path=None):
    """构造 `evalscope perf` 命令行参数。"""
    model = (cfg.get("model") or "").strip()
    if not model:
        raise ValueError("模型名不能为空")
    users = levels(cfg)
    numbers_list = numbers(cfg)
    dataset, ds_path, ds_args = dataset_spec(cfg, run_dir)

    cmd = command_prefix() + [
        "perf",
        "--api", API_NAME,
        "--model", model,
        "--url", normalize_endpoint(cfg.get("endpoint")),
        "--dataset", dataset,
    ]
    if ds_path:
        cmd += ["--dataset-path", ds_path]
    if ds_args is not None:
        cmd += ["--dataset-args", json.dumps(ds_args, ensure_ascii=False)]
    if prompt_mode(cfg) == MODE_CHAT:
        cmd += [
            # 自定义会话数据集：assistant 消息仅作轮次分界
            "--multi-turn",
            "--max-turns", str(max(1, int(cfg.get("turns") or 1))),
        ]
    cmd += [
        "--number", *[str(n) for n in numbers_list],
        "--parallel", *[str(u) for u in users],
        "--sleep-interval",
        str(int(max(0.0, float(cfg.get("ramp_pause_seconds") or 0.0)))),
        "--total-timeout", str(max(1, int(float(cfg.get("timeout") or 120)))),
        "--db-commit-interval", str(DB_COMMIT_INTERVAL),
        "--outputs-dir", os.path.abspath(str(run_dir)),
        "--name", RUN_NAME,
        "--no-timestamp",
    ]
    if prompt_mode(cfg) == MODE_LONG:
        cmd += _long_arguments(cfg)
    else:
        cmd += ["--max-tokens", str(max(1, int(cfg.get("max_tokens") or 512))),
                "--max-prompt-length", str(MAX_PROMPT_LENGTH)]
    cmd += ["--temperature", str(float(cfg.get("temperature") or 0.0))]
    top_p = cfg.get("top_p")
    cmd += ["--top-p", str(float(1.0 if top_p in (None, "") else top_p))]
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        cmd += ["--api-key", api_key]
    top_k = int(cfg.get("top_k") or -1)
    if top_k >= 0:                      # -1 表示不注入该字段
        cmd += ["--top-k", str(top_k)]
    if bool(cfg.get("ignore_eos")):
        cmd += ["--extra-args", json.dumps({"ignore_eos": True})]
    return cmd


def _long_arguments(cfg):
    """长序列模式专属参数：序列长度 / 固定前缀 / 输出长度 / 预热。"""
    if long_dataset(cfg) == DATASET_RANDOM:
        return [
            "--tokenizer-path", str(cfg.get("tokenizer_path") or "").strip(),
            "--prefix-length", str(prefix_length(cfg)),
            "--min-prompt-length", str(suffix_length(cfg)),
            "--max-prompt-length", str(suffix_length(cfg)),
        ] + _long_common_arguments(cfg)
    # line_by_line + prefix_file：由 target_input_len 控制总长，前缀长度自动 = 序列长度 - 提问
    return [
        "--tokenizer-path", str(cfg.get("tokenizer_path") or "").strip(),
    ] + _long_common_arguments(cfg)


def _long_common_arguments(cfg):
    """长序列模式共用的输出长度 / 预热参数。"""
    out = output_length(cfg)
    args = ["--min-tokens", str(out), "--max-tokens", str(out)]
    warmup = max(0, int(float(cfg.get("warmup_num") or 0)))
    if warmup:
        args += ["--warmup-num", str(warmup)]
    return args


def dataset_spec(cfg, run_dir):
    """返回 (dataset 名, dataset-path, dataset-args)；供命令行与结果解析共用。"""
    if prompt_mode(cfg) == MODE_LONG and long_dataset(cfg) == DATASET_RANDOM:
        return DATASET_RANDOM, None, None
    path = os.path.abspath(dataset_file(cfg, run_dir))
    if prompt_mode(cfg) == MODE_LONG:
        return DATASET_LINE, path, {
            "prefix_file": os.path.abspath(prefix_file_path(run_dir)),
            "target_input_len": seq_length(cfg),
            "input_len_mode": "cap",
            "prefix_role": "system",
        }
    return DATASET_NAME, path, None


def dump_args(cfg, run_dir, dataset_path=None):
    """渲染本次运行参数（落盘为 args.json，供页面「查看运行参数」）。"""
    dataset, ds_path, ds_args = dataset_spec(cfg, run_dir)
    payload = {
        "command": build_command(cfg, run_dir, dataset_path),
        "endpoint": normalize_endpoint(cfg.get("endpoint")),
        "model": cfg.get("model"),
        "prompt_mode": prompt_mode(cfg),
        "dataset": dataset,
        "dataset_path": ds_path,
        "dataset_args": ds_args,
        "max_tokens": cfg.get("max_tokens"),
        "temperature": cfg.get("temperature"),
        "top_p": cfg.get("top_p"),
        "top_k": cfg.get("top_k"),
        "timeout": cfg.get("timeout"),
        "turns": max(1, int(cfg.get("turns") or 1)),
        "unique_prefix": bool(cfg.get("unique_prefix")),
        "numbers": cfg.get("numbers") or numbers(cfg),
        "levels": [{"parallel": u, "number": n}
                   for u, n in zip(levels(cfg), numbers(cfg))],
        "sleep_interval": cfg.get("ramp_pause_seconds"),
        "expected_requests": expected_requests(cfg),
        "api_key": "***" if (cfg.get("api_key") or "").strip() else "",
    }
    if prompt_mode(cfg) == MODE_LONG:
        custom_prefix = bool(prefix_content(cfg))
        payload["long"] = {
            "tokenizer_path": cfg.get("tokenizer_path"),
            "seq_length": seq_length(cfg),
            "output_length": output_length(cfg),
            # 固定前缀内容模式下前缀长度由「序列长度 − 提问长度」自动决定
            "prefix_length": None if custom_prefix else prefix_length(cfg),
            "prefix_length_mode": "auto" if custom_prefix else "explicit",
            "suffix_length": None if custom_prefix else suffix_length(cfg),
            "prefix_cache_ratio": None if custom_prefix else round(cache_hit_ratio(cfg), 4),
            "prefix_content_chars": len(prefix_content(cfg)),
            "prefix_file": (os.path.abspath(prefix_file_path(run_dir))
                            if custom_prefix else None),
            "warmup_num": max(0, int(float(cfg.get("warmup_num") or 0))),
            "ignore_eos": bool(cfg.get("ignore_eos")),
        }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------- 结果解析
def _num(value, default=0.0):
    """指标解析：非数字与 NaN 都回落到 default（NaN 口径与别处的 float_or 不同）。"""
    return utils.float_or(value, default, nan_to_default=True)


_opt_num = utils.float_or_none


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


def list_result_files(run_dir):
    """列出可在线查看/下载的 evalscope 原始产物（相对任务目录的路径）。

    evalscope 以 `--outputs-dir <run_dir> --name perf` 运行，三类报告都落在
    `<run_dir>/perf/` 下；这里只暴露白名单内的文件，避免把结果库等大文件透出去。
    """
    out = []
    for name, label, kind in RESULT_FILES:
        path = os.path.join(os.path.abspath(run_dir), RUN_NAME, name)
        if not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({
            "path": "{}/{}".format(RUN_NAME, name),
            "name": name,
            "label": label,
            "kind": kind,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    return out


def result_file(run_dir, rel_path):
    """把相对路径解析为白名单内文件的真实路径，非法或不存在时抛 ValueError。"""
    norm = os.path.normpath(str(rel_path or "").strip().lstrip("/")).replace(os.sep, "/")
    allowed = {"{}/{}".format(RUN_NAME, name) for name, _label, _kind in RESULT_FILES}
    if norm not in allowed:
        raise ValueError("不支持的文件：{}".format(rel_path or ""))
    root = os.path.realpath(os.path.join(os.path.abspath(run_dir), RUN_NAME))
    path = os.path.realpath(os.path.join(os.path.abspath(run_dir), norm))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise ValueError("文件不存在：{}".format(norm))
    return path


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
    turn = _request_turn(cell("request"))
    if turn is None and prompt_mode(cfg) == MODE_LONG and cell("success") is not None:
        turn = 1        # long 模式不读 request 列（整段 prompt），该模式恒为单轮
    return {
        "run_id": run_id,
        "timestamp_start": ts.isoformat(timespec="milliseconds") if ts else "",
        "timestamp_end": (ts + timedelta(seconds=latency)).isoformat(timespec="milliseconds")
                         if ts else "",
        "concurrent_users_level": level,
        "conversation": "conv",
        "turn": turn,
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
        # 逐请求的失败原因要等收尾阶段重放才知道（evalscope 不落盘状态码），
        # 这里先给出兜底文案，诊断完成后由 perf_manager 覆盖前几条
        "error": "" if success else "请求失败（详见 perf/{}）".format(FAILED_FILE),
        "scenario": (cfg.get("name") or "stress"),
        "target_server": _target_of(cfg),
        "target_model": (cfg.get("model") or "").strip(),
    }


def _target_of(cfg):
    try:
        return normalize_endpoint(cfg.get("endpoint"))
    except ValueError:                      # 历史任务缺少配置时不至于整条读取失败
        return (cfg.get("endpoint") or "").strip()


# 平台侧真正用到的列。`request` 是整段 prompt（长序列模式下每行数百 KB 到 1MB），
# 只用来数轮次；`inter_token_latencies` / `response_messages` 完全不用，均不读。
READ_COLUMNS = (
    "start_time", "success", "latency", "first_chunk_latency",
    "prompt_tokens", "completion_tokens", "time_per_output_token", "request_id",
)


def _read_columns(cfg):
    columns = list(READ_COLUMNS)
    if prompt_mode(cfg) != MODE_LONG:
        columns.append("request")       # 会话模式的 request 只有几百字节，用于推断轮次
    return ", ".join(columns)


def _select_batch(path, columns, last_rowid, limit):
    """按 rowid keyset 读一批新增行；库还没建好/正被写时返回 None。

    不长事务、不扫全表：只从 `last_rowid` 之后取 `limit` 行，
    避免把 evalscope 的写入（commit）堵到超时报 "database is locked"。
    """
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True,
                               timeout=READ_TIMEOUT)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout={}".format(int(READ_TIMEOUT * 1000)))
        return conn.execute(
            "SELECT rowid AS _rid, {} FROM result WHERE rowid > ? "
            "ORDER BY rowid LIMIT ?".format(columns),
            (int(last_rowid), int(limit))).fetchall()
    except sqlite3.Error:                   # 表尚未创建 / 结果库损坏
        return None
    finally:
        conn.close()


def read_records(run_dir, cfg=None, run_id="", after=None):
    """增量读取 evalscope 的 SQLite 结果库，归一化为统一记录格式。

    结果库**只追加**，因此按 rowid 游标（keyset 分页）增量读取：只读新增行、
    只读需要的列，单次最多 READ_BATCH 行。`after` 为 {db_path: 已读到的 rowid}，
    返回 (新增记录, 新游标)；`after=None` 表示整库读取（同样分批，不让单个读事务
    持续太久）。
    """
    cfg = cfg or {}
    cursor = dict(after or {})
    incremental = after is not None
    records = []
    anchor = _time_anchor()
    columns = _read_columns(cfg)
    started = time.monotonic()
    for level, path in _result_dbs(run_dir):
        last = int(cursor.get(path) or 0)
        while True:
            rows = _select_batch(path, columns, last, READ_BATCH)
            if not rows:
                break
            last = int(rows[-1]["_rid"])
            cursor[path] = last
            for row in rows:
                records.append(_record(row, level, cfg, run_id, anchor,
                                       int(row["_rid"])))
            if len(rows) < READ_BATCH:
                break
            # 增量读取按时间预算收尾：每批都是独立短事务，剩下的交给下一轮轮询
            if incremental and time.monotonic() - started >= READ_BUDGET:
                break
    return records, cursor


# ------------------------------------------------------------------- 失败诊断
def stderr_path(run_dir):
    """evalscope 子进程 stderr 的落盘路径（<run_dir>/perf/evalscope_stderr.log）。"""
    return os.path.join(os.path.abspath(str(run_dir)), RUN_NAME, STDERR_FILE)


def probe_limit():
    """失败重放次数上限（环境变量 PERF_PROBE_LIMIT 可覆盖，0 表示关闭重放）。"""
    try:
        return max(0, int(os.environ.get("PERF_PROBE_LIMIT", PROBE_LIMIT)))
    except ValueError:
        return PROBE_LIMIT


def probe_timeout(cfg):
    """重放用的超时：不短于压测本身的 total_timeout，最长 PROBE_TIMEOUT。

    重放只用于取证，跑满整个压测超时没有意义；但目标服务在长序列下可能先排队
    再报错，超时设得太短会拿到「无响应」而不是真实的 4xx。
    """
    try:
        base = float((cfg or {}).get("timeout") or 0) or PROBE_TIMEOUT
    except (TypeError, ValueError):
        base = PROBE_TIMEOUT
    return max(5.0, min(base, PROBE_TIMEOUT))


def _snippet(raw, limit=PROBE_BODY_CHARS):
    """把响应体压成一行摘要：去换行、截断，避免日志被整段 HTML 误差页刷屏。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = " ".join(str(raw or "").split())
    return text[:limit]


def failure_message(status_code, detail=""):
    """失败请求的一句话结论：优先 HTTP 状态码，其次原始异常描述。"""
    detail = (detail or "").strip()
    if not status_code:
        return "请求失败（未收到 HTTP 响应{}）".format("：" + detail if detail else "")
    if status_code < 400:
        # 重放反而成功：说明失败是偶发错误或客户端超时，不是配置/契约问题
        return "重放已成功（HTTP {}）：原失败可能是偶发错误或客户端超时".format(status_code)
    return "HTTP {}{}".format(status_code, "：" + detail if detail else "")


def failed_bodies(run_dir, limit):
    """取前 `limit` 条失败请求的原始请求体，供重放取证。

    `request` 是整段 prompt（长序列模式下每行数百 KB 到 1MB），只在**失败之后**
    读、且最多读几行；成功请求的该列一律不碰。
    """
    if limit <= 0:
        return []
    out = []
    seen = set()
    for level, path in _result_dbs(run_dir):
        try:
            conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True,
                                   timeout=READ_TIMEOUT)
        except sqlite3.Error:
            continue
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout={}".format(int(READ_TIMEOUT * 1000)))
            rows = conn.execute(
                "SELECT rowid AS _rid, request FROM result WHERE success = 0 "
                "ORDER BY rowid LIMIT ?", (int(limit) * 4,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for row in rows:
            body = row["request"]
            if not body:
                continue
            key = hash(body)
            if key in seen:             # 同一份请求体只重放一次
                continue
            seen.add(key)
            out.append({"level": level, "rowid": int(row["_rid"]), "body": body})
            if len(out) >= limit:
                return out
    return out


def probe_request(cfg, body, timeout):
    """同步重放一次请求，返回 (status_code, 响应体摘要)。

    用标准库而不是 aiohttp/requests：只在收尾阶段调用一次，不需要连接池，
    也免得多引一个第三方依赖。4xx/5xx 由 HTTPError 给出状态码与响应体。
    """
    url = normalize_endpoint((cfg or {}).get("endpoint"))
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = ((cfg or {}).get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), _snippet(resp.read(4096))
    except urllib.error.HTTPError as exc:
        return int(exc.code), _snippet(exc.read())
    except Exception as exc:            # noqa: BLE001  连接/超时/证书等
        return None, "{}: {}".format(type(exc).__name__, exc)


def diagnose_failures(run_dir, cfg, limit=None, log=None):
    """重放少量失败请求，取回 HTTP 状态码并写 <run_dir>/perf/failed_requests.log。

    返回 [{"rowid", "level", "status_code", "message"}]；重放次数受 probe_limit()
    限制，且只在压测结束后执行，不会混进被测服务的指标。
    """
    limit = probe_limit() if limit is None else int(limit)
    samples = failed_bodies(run_dir, limit)
    if not samples:
        return []
    timeout = probe_timeout(cfg)
    lines = [
        "# 失败请求诊断 · {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "# evalscope 不落盘 HTTP 状态码（result 表无该列、benchmark.log 只记 INFO），",
        "# 因此这里按原请求体重放 {} 次取回状态码；重放不计入压测指标。".format(len(samples)),
        "# 重放用的是本任务启动时记录的密钥：若已轮换，这里会得到 401，不代表当时的失败原因。",
        "",
    ]
    out = []
    for i, sample in enumerate(samples, 1):
        status, text = probe_request(cfg, sample["body"], timeout)
        message = failure_message(status, text)
        out.append({"rowid": sample["rowid"], "level": sample["level"],
                    "status_code": status, "message": message})
        lines.append("[{}] 并发={} rowid={} 状态码={} 响应={}".format(
            i, sample["level"], sample["rowid"],
            status if status is not None else "-", text or "-"))
        if log:
            log("[失败] 重放第 {}/{} 条：{}".format(i, len(samples), message[:300]))
    try:
        os.makedirs(os.path.join(str(run_dir), RUN_NAME), exist_ok=True)
        with open(os.path.join(str(run_dir), RUN_NAME, FAILED_FILE), "a",
                  encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except OSError as exc:
        if log:
            log("[失败] 写 {} 失败：{}".format(FAILED_FILE, exc))
    return out


# --------------------------------------------------------------------- 指标聚合
def _nearest_rank(values, percentile):
    """最近秩分位：与 evalscope 写进 benchmark.log 的分位表口径一致。

    evalscope 取 `ceil(n * p) - 1` 号样本（见 evalscope/perf/utils/db_util.py），
    因此分位值必然是真实样本之一；此前用 statistics.quantiles 做线性插值，
    会算出日志里根本不存在的数（例如 20 条样本里 19 条 TTFT 缺失、只有一条
    17.692s 时，插值得到 P95 = 16.807s，而日志的 TTFT 95% 分位是 0、99% 才是
    17.692s），页面与 benchmark.log 对不上。
    """
    ordered = sorted(_num(v) for v in values)
    if not ordered:
        return 0.0
    rank = math.ceil(percentile * len(ordered) / 100)
    return ordered[min(max(rank, 1), len(ordered)) - 1]


def _p95(values):
    return _nearest_rank(values, 95)


def _p50(values):
    return _nearest_rank(values, 50)


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
        # 仅输出 token，对应 benchmark.log 的 Output Throughput
        "total_tokens_per_second": (completion / duration) if duration else 0.0,
        # 输入 + 输出，对应 benchmark.log 的 Total Throughput（长序列下远大于输出吞吐）
        "overall_tokens_per_second": ((prompt_tokens + completion) / duration)
        if duration else 0.0,
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
