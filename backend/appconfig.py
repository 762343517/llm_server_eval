"""表单默认值与「上次提交配置」持久化。

历史配置保存于 runs/.last_config.json（精度测试）与 runs/.last_stress_config.json
（压力测试），两者均位于已被 .gitignore 忽略的 runs/ 目录下，权限 600。文件含明文
密钥，用途是保证页面跳转或删除任务后重新渲染时表单内容不被清空。
"""
import json
import os

import consts
import dataset_registry
import eval_core
import perf_core
import run_manager

# 端点/密钥默认值统一维护在 consts.py；此处仅保留端点的环境变量覆盖能力
DEFAULT_ENDPOINT = os.environ.get("ENDPOINT") or consts.DEFAULT_ENDPOINT
DEFAULT_API_KEY = consts.DEFAULT_API_KEY
DEFAULT_STRESS_ENDPOINT = os.environ.get("STRESS_ENDPOINT") or consts.DEFAULT_STRESS_ENDPOINT
DEFAULT_STRESS_API_KEY = consts.DEFAULT_STRESS_API_KEY

APP_TITLE = "大模型服务测试平台"

# 思考开关的三态取值（"" 表示不传该字段，由服务端默认决定）
THINKING_LABELS = {"": "服务端默认（不传该字段）", "enabled": "开启思考",
                   "disabled": "关闭思考"}
EFFORT_LABELS = {"": "默认（不传）", "low": "low", "medium": "medium",
                 "high": "high", "max": "max"}
THINKING_OPTIONS = [(v, THINKING_LABELS[v]) for v in eval_core.THINKING_CHOICES]
EFFORT_OPTIONS = [(v, EFFORT_LABELS[v]) for v in eval_core.EFFORT_CHOICES]

LAST_CFG_NAME = ".last_config.json"
LAST_STRESS_CFG_NAME = ".last_stress_config.json"
LAST_AGENT_CFG_NAME = ".last_agent_config.json"


def _last_cfg_path(name):
    return os.path.join(run_manager.RUNS_DIR, name)


def load_last_cfg(name):
    """读取上次提交的配置；文件缺失/损坏时返回空 dict。"""
    try:
        with open(_last_cfg_path(name), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_last_cfg(name, cfg):
    """落盘上次配置（含密钥，权限 600），失败不影响任务启动。"""
    path = _last_cfg_path(name)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def accuracy_defaults():
    """精度测试表单默认值：静态默认 + 上次提交的配置。"""
    d = {
        "endpoint": DEFAULT_ENDPOINT,
        "api_key": DEFAULT_API_KEY,
        "judge_endpoint": consts.DEFAULT_JUDGE_ENDPOINT,
        "judge_api_key": consts.DEFAULT_JUDGE_API_KEY,
        "model": consts.DEFAULT_MODEL,
        "judge_model": consts.DEFAULT_JUDGE_MODEL,
        # 数据集来自固定的 dataset/ 目录，默认取 dataset/datasets.json 配置里的 default
        "data": dataset_registry.default_path() or eval_core.DEFAULT_DATA,
        "thinking": consts.DEFAULT_THINKING,
        "reasoning_effort": consts.DEFAULT_REASONING_EFFORT,
        "judge_thinking": consts.DEFAULT_THINKING,
        "judge_reasoning_effort": consts.DEFAULT_REASONING_EFFORT,
        "concurrency": consts.DEFAULT_CONCURRENCY,
        "judge_concurrency": consts.DEFAULT_JUDGE_CONCURRENCY,
        "limit": consts.DEFAULT_LIMIT,
        "seed": consts.DEFAULT_SEED,
        "max_tokens": consts.DEFAULT_MAX_TOKENS,
        "max_tokens_cap": consts.DEFAULT_MAX_TOKENS_CAP,
        "max_incomplete_attempts": consts.DEFAULT_MAX_INCOMPLETE_ATTEMPTS,
    }
    saved = load_last_cfg(LAST_CFG_NAME)
    for k, v in saved.items():
        if k in d and v is not None:
            d[k] = v
    # 历史配置里的数据集可能已被删除/改名，这里静默回落到配置指定的默认数据集
    d["data"] = _valid_dataset(d["data"])
    return d


def _valid_dataset(path):
    """数据集路径存在则保留（页面上也允许自定义路径），否则回落到配置里的默认数据集。"""
    path = str(path or "").strip()
    if path and os.path.exists(path):
        # 历史配置里可能存的是相对路径，统一规范化为绝对路径以便与下拉框候选匹配
        return os.path.abspath(path)
    return dataset_registry.default_path() or eval_core.DEFAULT_DATA


def _default_agent_data():
    """Agent 任务集目录：取配置里第一个声明了 agent 段的数据集。"""
    for item in dataset_registry.datasets():
        tasks_dir = (item.get("agent") or {}).get("tasks_dir")
        if tasks_dir:
            return tasks_dir
    return ""


def _valid_agent_data(path):
    """历史配置里的任务集可能已被删除/改名，这里静默回落到默认任务集。"""
    path = str(path or "").strip()
    if path and os.path.isdir(path):
        return os.path.abspath(path)
    return _default_agent_data()


def agent_defaults():
    """Agent 评测表单默认值：静态默认 + 上次提交的配置。"""
    d = {
        "endpoint": consts.DEFAULT_AGENT_ENDPOINT,
        "api_key": consts.DEFAULT_AGENT_API_KEY,
        "model": consts.DEFAULT_AGENT_MODEL,
        "model_class": consts.DEFAULT_AGENT_MODEL_CLASS,
        "agent": consts.DEFAULT_AGENT_NAME,
        # 任务集来自 dataset/<数据集名称>/tasks，默认取配置中声明 agent 段的数据集
        "data": _default_agent_data(),
        "limit": consts.DEFAULT_AGENT_N_TASKS,
        "concurrency": consts.DEFAULT_AGENT_CONCURRENCY,
        "seed": consts.DEFAULT_AGENT_SEED,
        "timeout_multiplier": consts.DEFAULT_AGENT_TIMEOUT_MULTIPLIER,
        "verifier_timeout_multiplier": consts.DEFAULT_AGENT_VERIFIER_TIMEOUT_MULTIPLIER,
    }
    saved = load_last_cfg(LAST_AGENT_CFG_NAME)
    for k, v in saved.items():
        if k in d and v is not None:
            d[k] = v
    d["data"] = _valid_agent_data(d["data"])
    return d


def _legacy_iterations(saved):
    """旧版表单直接填逐档 --number（请求总数），换算回「每个并发的请求数」用于回填。

    返回 None 表示无从换算（快照里没有请求数信息），此时沿用静态默认值。
    """
    try:
        levels = parse_levels(saved.get("ramp_levels_text"))
        counts = parse_int_list(saved.get("ramp_numbers_text"))
    except ValueError:
        levels, counts = [], []
    if levels and counts:
        return max(1, counts[0] // levels[0])
    try:
        total = int(saved.get("numbers") or 0)
        users = int(saved.get("concurrent_users") or 0)
    except (TypeError, ValueError):
        return None
    if total > 0 and users > 0:
        return max(1, total // users)
    return None


def _valid_tokenizer(path):
    """分词器路径必须落在固定的 model/ 目录内且真实存在，否则回落到当前可用默认值。"""
    path = str(path or "").strip()
    if path and perf_core.tokenizer_in_root(path) and os.path.isdir(path):
        return path
    return perf_core.default_tokenizer()


def stress_defaults():
    """压力测试表单默认值：静态默认 + 上次提交的配置。"""
    d = {
        "name": consts.DEFAULT_STRESS_NAME,
        "description": "",
        "endpoint": DEFAULT_STRESS_ENDPOINT,
        "api_key": DEFAULT_STRESS_API_KEY,
        "model": consts.DEFAULT_STRESS_MODEL,
        "max_tokens": consts.DEFAULT_STRESS_MAX_TOKENS,
        "temperature": consts.DEFAULT_STRESS_TEMPERATURE,
        "top_p": consts.DEFAULT_STRESS_TOP_P,
        "top_k": consts.DEFAULT_STRESS_TOP_K,
        "timeout": consts.DEFAULT_STRESS_TIMEOUT,
        "system_prompt": "",
        "prompt": consts.DEFAULT_STRESS_PROMPT,
        "turns": consts.DEFAULT_STRESS_TURNS,
        "unique_prefix": False,
        "concurrency_mode": consts.DEFAULT_STRESS_CONCURRENCY_MODE,
        "concurrent_users": consts.DEFAULT_STRESS_CONCURRENT_USERS,
        # 表单只填「每个并发的请求数」，最终 --number 由后端按并发数换算
        "iterations": consts.DEFAULT_STRESS_ITERATIONS,
        "ramp_levels_text": consts.DEFAULT_STRESS_RAMP_LEVELS_TEXT,
        "ramp_pause_seconds": consts.DEFAULT_STRESS_RAMP_PAUSE_SECONDS,
        "prompt_mode": consts.DEFAULT_STRESS_PROMPT_MODE,
        # 分词器来自固定目录 model/，默认取其中第一个带对话模板的目录
        "tokenizer_path": perf_core.default_tokenizer(),
        "seq_length": consts.DEFAULT_STRESS_SEQ_LENGTH,
        "output_length": consts.DEFAULT_STRESS_OUTPUT_LENGTH,
        "prefix_cache_ratio": consts.DEFAULT_STRESS_PREFIX_CACHE_RATIO,
        "prefix_length": consts.DEFAULT_STRESS_PREFIX_LENGTH,
        "prefix_content": consts.DEFAULT_STRESS_PREFIX_CONTENT,
        "warmup_num": consts.DEFAULT_STRESS_WARMUP_NUM,
        "ignore_eos": consts.DEFAULT_STRESS_IGNORE_EOS,
    }
    saved = load_last_cfg(LAST_STRESS_CFG_NAME)
    if saved and "iterations" not in saved:
        legacy = _legacy_iterations(saved)
        if legacy:
            d["iterations"] = legacy
    for k, v in saved.items():
        if k in d and v is not None:
            d[k] = v
    # 历史配置里的分词器可能已被移出 model/ 目录或删除，这里静默回落到可用值
    d["tokenizer_path"] = _valid_tokenizer(d["tokenizer_path"])
    return d


def parse_int_list(text, label="数值"):
    """解析逗号分隔的正整数列表（如 "2,4,8,16" / "6, 12, 24, 48"），保序不去重。"""
    if isinstance(text, (list, tuple)):
        raw = list(text)
    else:
        raw = str(text or "").replace("，", ",").split(",")
    values = []
    for item in raw:
        item = str(item).strip()
        if not item:
            continue
        try:
            value = int(float(item))
        except ValueError:
            raise ValueError("{}必须是逗号分隔的整数: {}".format(label, text))
        if value < 1:
            raise ValueError("{}必须大于 0: {}".format(label, text))
        values.append(value)
    return values


def parse_levels(text):
    """解析阶梯并发的档位文本（如 "2,4,8,16"）-> 升序去重的整数列表。"""
    return sorted(set(parse_int_list(text, "并发档位")))
