"""表单默认值与「上次提交配置」持久化。

历史配置保存于 runs/.last_config.json（精度测试）与 runs/.last_stress_config.json
（压力测试），两者均位于已被 .gitignore 忽略的 runs/ 目录下，权限 600。文件含明文
密钥，用途是保证页面跳转或删除任务后重新渲染时表单内容不被清空。
"""
import json
import os

import consts
import eval_core
import run_manager

# 端点/密钥默认值统一维护在 consts.py；此处仅保留端点的环境变量覆盖能力
DEFAULT_ENDPOINT = os.environ.get("ENDPOINT") or consts.DEFAULT_ENDPOINT
DEFAULT_API_KEY = consts.DEFAULT_API_KEY
DEFAULT_STRESS_ENDPOINT = os.environ.get("STRESS_ENDPOINT") or consts.DEFAULT_STRESS_ENDPOINT
DEFAULT_STRESS_API_KEY = consts.DEFAULT_STRESS_API_KEY

APP_TITLE = "SimpleQA-Verified 评测平台"

# 思考开关的三态取值（"" 表示不传该字段，由服务端默认决定）
THINKING_LABELS = {"": "服务端默认（不传该字段）", "enabled": "开启思考",
                   "disabled": "关闭思考"}
EFFORT_LABELS = {"": "默认（不传）", "low": "low", "medium": "medium",
                 "high": "high", "max": "max"}
THINKING_OPTIONS = [(v, THINKING_LABELS[v]) for v in eval_core.THINKING_CHOICES]
EFFORT_OPTIONS = [(v, EFFORT_LABELS[v]) for v in eval_core.EFFORT_CHOICES]

LAST_CFG_NAME = ".last_config.json"
LAST_STRESS_CFG_NAME = ".last_stress_config.json"


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
        "data": eval_core.DEFAULT_DATA,
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
    return d


def stress_defaults():
    """压力测试表单默认值：静态默认 + 上次提交的配置。"""
    d = {
        "name": consts.DEFAULT_STRESS_NAME,
        "description": "",
        "backend_name": consts.DEFAULT_STRESS_BACKEND_NAME,
        "backend_type": consts.DEFAULT_STRESS_BACKEND_TYPE,
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
        "iterations": consts.DEFAULT_STRESS_ITERATIONS,
        "ramp_up_seconds": 0,
        "think_time_seconds": 0,
        "ramp_levels_text": consts.DEFAULT_STRESS_RAMP_LEVELS_TEXT,
        "ramp_pause_seconds": consts.DEFAULT_STRESS_RAMP_PAUSE_SECONDS,
    }
    saved = load_last_cfg(LAST_STRESS_CFG_NAME)
    for k, v in saved.items():
        if k in d and v is not None:
            d[k] = v
    return d


def parse_levels(text):
    """解析阶梯并发的档位文本（如 "2,4,8,16"）-> 升序去重的整数列表。"""
    if isinstance(text, (list, tuple)):
        raw = list(text)
    else:
        raw = str(text or "").replace("，", ",").split(",")
    levels = []
    for item in raw:
        item = str(item).strip()
        if not item:
            continue
        try:
            value = int(float(item))
        except ValueError:
            raise ValueError("并发档位必须是逗号分隔的整数: " + text)
        if value < 1:
            raise ValueError("并发档位必须大于 0: " + text)
        levels.append(value)
    return sorted(set(levels))
