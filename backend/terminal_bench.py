"""Terminal-Bench 2.0 数据集：任务发现与 task.toml 解析。

该数据集不是单张表（csv/parquet），而是「一个任务一个子目录」：

    <数据集目录>/<task_id>/task.toml       任务元信息（镜像、超时、类别、难度）
    <数据集目录>/<task_id>/instruction.md  交给被测 agent 的任务描述（problem）
    <数据集/<task_id>/tests/test.sh        评测脚本，通过时写入 /logs/verifier/reward.txt
    <数据集目录>/<task_id>/solution/       参考解（本平台不使用）
    <数据集目录>/<task_id>/environment/    构建镜像用的 Dockerfile（本平台不重新构建，
                                          直接用 task.toml 里声明的 docker_image）

本模块只负责把任务目录解析成统一记录列表（problem/topic/subject + 执行所需的镜像与
各项超时），真正的容器执行、agent 循环与判分见 agent_core。
"""
import os
import random

import dataset_registry

# 任务元信息文件名（存在于子目录中即视为一个任务）
TASK_FILE = "task.toml"
INSTRUCTION_FILE = "instruction.md"
TESTS_DIR = "tests"
TESTS_SCRIPT = "test.sh"

# 兜底默认值：task.toml 缺字段时使用
DEFAULT_AGENT_TIMEOUT = 900.0
DEFAULT_VERIFIER_TIMEOUT = 900.0
DEFAULT_BUILD_TIMEOUT = 600.0

try:                                    # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:             # Python 3.10 及以下
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None


class TaskError(RuntimeError):
    """数据集/任务本身不可用（目录缺失、缺少必要文件等）。"""


def dataset_root(path):
    """把数据集路径解析为任务所在的数据集根目录。

    path 可能是配置里的数据文件（<数据集目录>/*/task.toml）、数据集目录本身，
    或页面上手填的目录；优先按 dataset_registry 的登记反查。
    """
    spec = dataset_registry.spec_for_path(path)
    if spec:
        return spec["dir"]
    raw = str(path or "").strip()
    if os.path.isdir(raw):
        return raw
    if raw and os.path.basename(raw) == TASK_FILE:
        # <root>/<task_id>/task.toml -> <root>
        return os.path.dirname(os.path.dirname(os.path.abspath(raw)))
    return os.path.dirname(os.path.abspath(raw)) if raw else ""


def task_dirs(root):
    """数据集根目录下的全部任务目录（含 task.toml 的子目录），按名称排序。"""
    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if not os.path.isdir(directory):
            continue
        if os.path.isfile(os.path.join(directory, TASK_FILE)):
            found.append(directory)
    return found


def _read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def parse_task(directory):
    """解析单个任务目录，返回执行所需的全部信息（缺 task.toml 时抛 TaskError）。"""
    toml_path = os.path.join(directory, TASK_FILE)
    if not os.path.isfile(toml_path):
        raise TaskError("缺少 {}".format(TASK_FILE))
    if _toml is None:
        raise TaskError("缺少 TOML 解析库，请执行: pip install tomli")
    with open(toml_path, "rb") as fh:
        try:
            cfg = _toml.load(fh)
        except Exception as exc:  # noqa: BLE001 单个任务配置损坏不应中断整轮评测
            raise TaskError("{} 解析失败: {}".format(TASK_FILE, exc))
    meta = cfg.get("metadata") or {}
    agent = cfg.get("agent") or {}
    verifier = cfg.get("verifier") or {}
    environ = cfg.get("environment") or {}
    tests_dir = os.path.join(directory, TESTS_DIR)
    task_id = os.path.basename(directory.rstrip(os.sep))
    image = str(environ.get("docker_image") or "").strip()
    return {
        "task_id": task_id,
        "task_dir": directory,
        "tests_dir": tests_dir,
        "tests_script": os.path.join(tests_dir, TESTS_SCRIPT),
        "has_tests": os.path.isfile(os.path.join(tests_dir, TESTS_SCRIPT)),
        "problem": _read_text(os.path.join(directory, INSTRUCTION_FILE)).strip(),
        "topic": str(meta.get("category") or "Unknown").strip() or "Unknown",
        "difficulty": str(meta.get("difficulty") or "").strip(),
        "tags": [str(t) for t in (meta.get("tags") or [])],
        "expert_time_estimate_min": meta.get("expert_time_estimate_min"),
        "human_minutes": _float(meta.get("junior_time_estimate_min"), 0.0),
        "docker_image": image,
        "agent_timeout": _float(agent.get("timeout_sec"), DEFAULT_AGENT_TIMEOUT),
        "verifier_timeout": _float(verifier.get("timeout_sec"),
                                   DEFAULT_VERIFIER_TIMEOUT),
        "build_timeout": _float(environ.get("build_timeout_sec"),
                                DEFAULT_BUILD_TIMEOUT),
        "cpus": _float(environ.get("cpus"), 1.0),
        "memory": str(environ.get("memory") or "").strip(),
        "storage": str(environ.get("storage") or "").strip(),
        "metadata": meta,
    }


def load_tasks(path, limit=0, seed=0, on_event=None, should_stop=None):
    """加载数据集中的任务列表。

    返回 list[dict]，每条含 original_index（用于断点续跑与展示）、problem（任务描述）
    与执行所需的镜像/超时等字段；加载过程中跳过配置损坏的任务并在事件里报告。
    limit>0 时按 seed 采样（只用固定 seed 保证可复现，seed=0 表示按名称顺序取前 N 个）。
    """
    on_event = on_event or (lambda kind, payload: None)
    should_stop = should_stop or (lambda: False)
    root = dataset_root(path)
    on_event("load_start", {"path": root, "kind": "terminal_bench",
                            "dataset": os.path.basename(root) or root})
    if not os.path.isdir(root):
        raise TaskError("数据集目录不存在: {}".format(root))

    directories = task_dirs(root)
    if not directories:
        raise TaskError("{} 下没有找到任何任务目录（需包含 {}）".format(root, TASK_FILE))

    records, skipped = [], []
    for index, directory in enumerate(directories):
        if should_stop():
            break
        try:
            task = parse_task(directory)
        except TaskError as exc:
            skipped.append((os.path.basename(directory), str(exc)))
            continue
        if not task["problem"]:
            skipped.append((task["task_id"], "缺少 {}".format(INSTRUCTION_FILE)))
            continue
        task["original_index"] = index
        task["answer_type"] = task["difficulty"]
        task["subject"] = task["difficulty"]
        records.append(task)
        if len(records) % 20 == 0:
            on_event("load_progress", {"loaded": len(records),
                                       "total": len(directories), "images": 0})

    if limit and limit < len(records):
        if seed:
            picked = random.Random(seed).sample(records, limit)
            records = sorted(picked, key=lambda r: r["original_index"])
        else:
            records = records[:limit]

    on_event("load_progress", {"loaded": len(records), "total": len(directories),
                               "images": 0})
    on_event("load_end", {"rows": len(records), "images": 0,
                          "skipped": len(skipped)})
    for task_id, reason in skipped:
        on_event("task_log", {"msg": "[跳过] 任务 {}：{}".format(task_id, reason)})
    return records


def stats(path):
    """数据集概览（供页面展示）：任务数、镜像数、缺失项统计。"""
    root = dataset_root(path)
    directories = task_dirs(root) if os.path.isdir(root) else []
    images, broken, no_tests, missing_instruction = set(), 0, 0, 0
    for directory in directories:
        try:
            task = parse_task(directory)
        except TaskError:
            broken += 1
            continue
        images.add(task["docker_image"] or "?")
        if not task["has_tests"]:
            no_tests += 1
        if not task["problem"]:
            missing_instruction += 1
    return {
        "root": root,
        "tasks": len(directories),
        "images": len(images),
        "broken": broken,
        "no_tests": no_tests,
        "missing_instruction": missing_instruction,
    }
