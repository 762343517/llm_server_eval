"""Agent 任务集适配器（任务目录 + task.toml 约定）。

Agent 类数据集的「一行」不是问答题，而是一个任务目录：目录里带 task.toml
（元信息与镜像）、instruction.md（题面）、environment/ 与 tests/（沙箱与判分）。
这里集中这层格式约定，agent_core 只负责 pier 环境、命令与结果解析，
新增一种任务集布局时也只改本模块，不碰业务代码。

任务目录约定：
    <任务集目录>/<任务名>/task.toml     必须有（没有则不算任务）
    task.toml 里的 metadata.display_title / category / language
                environment.docker_image / agent.timeout_sec / verifier.timeout_sec
    instruction.md 或 README.md          题面（可选）
"""
import os
import random

import consts
import dataset_registry
import utils

try:                                    # Python 3.11+
    import tomllib as _toml
except ImportError:                     # pragma: no cover
    try:
        import tomli as _toml
    except ImportError:
        _toml = None

_num = utils.float_or                   # 数值兜底统一收敛到 utils


def toml_available():
    """任务集元信息解析库是否可用（Python < 3.11 需要 tomli）。"""
    return _toml is not None


# ------------------------------------------------------------------ 任务集清单
def catalog():
    """配置里声明了 agent 段的数据集（即 Agent 任务集）。"""
    return [it for it in dataset_registry.datasets() if it.get("agent")]


def spec(name_or_path=""):
    """按名称/路径找 Agent 任务集条目；缺省取第一个。"""
    items = catalog()
    key = str(name_or_path or "").strip()
    if key:
        for it in items:
            if key in (it["name"], it["dir"], it["path"]):
                return it
    return items[0] if items else None


def directory(item=None):
    """任务集目录（清单里 agent.tasks_dir 的绝对路径）。"""
    if item is None:
        item = spec()
    return ((item or {}).get("agent") or {}).get("tasks_dir", "")


def rebase_dataset_path(path):
    """把旧工作区遗留的绝对数据集路径改指到当前 dataset 目录。

    历史 run 的 cfg.data 落盘的是发起时的**绝对路径**（例如项目目录还叫 zhipuEval
    时的 /home/<user>/prj/zhipuEval/dataset/deepswe/tasks）。项目改名或搬到别的机器
    后该路径失效，子任务看板这类只读接口会直接报「任务目录不存在」。这里取路径里
    dataset 之后的相对部分，按当前 DATASET_DIR 重新拼接，且只在新路径确实存在时采用。
    """
    parts = [p for p in str(path or "").split(os.sep) if p]
    if "dataset" not in parts:
        return ""
    rel = parts[len(parts) - 1 - parts[::-1].index("dataset") + 1:]
    if not rel:
        return ""
    candidate = os.path.join(consts.DATASET_DIR, *rel)
    nested = os.path.join(candidate, "tasks")
    if os.path.isdir(nested):
        return nested
    return candidate if os.path.isdir(candidate) else ""


def resolve_dir(path):
    """把页面/配置里的路径解析成「任务目录集合」所在目录。

    支持三种写法：数据集名称（如 deepswe）、数据集目录、tasks 目录本身
    （后者用于临时指向别处的任务集，只要子目录里有 task.toml 即可）。
    """
    key = str(path or "").strip()
    item = dataset_registry.find(key)
    if item and item.get("agent"):
        return item["agent"]["tasks_dir"]
    if key and os.path.isdir(key):
        nested = os.path.join(key, "tasks")
        return nested if os.path.isdir(nested) else key
    return rebase_dataset_path(key) or key


def key_for(path="", directory=""):
    """把任务集标识归一化成「可落盘的 key」。

    登记在 datasets.json 里的任务集优先记**名称**（如 deepswe）：这样 run 配置与
    工作区位置解耦，项目改名或换机器后历史任务仍能解析（曾经直接落绝对路径，
    目录一变子任务看板就整页 500）。未登记的自定义路径只能原样记绝对路径。
    """
    item = dataset_registry.find(path)
    if item and item.get("agent"):
        return item["name"]
    target = str(directory or "").strip() or str(resolve_dir(path) or "").strip()
    for it in catalog():
        if (it.get("agent") or {}).get("tasks_dir") == target:
            return it["name"]
    return os.path.abspath(target) if target else ""


def label(directory_path):
    """任务集标签：dataset/deepswe/tasks -> deepswe（用于日志与汇总标题）。"""
    path = os.path.abspath(str(directory_path or "."))
    base = os.path.basename(path)
    return os.path.basename(os.path.dirname(path)) if base == "tasks" else base


# ------------------------------------------------------------------ 任务清单
def read_toml(path):
    """解析 task.toml（缺解析库或文件损坏时返回空 dict）。"""
    if _toml is None:
        return {}
    try:
        with open(path, "rb") as fh:
            data = _toml.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def list_tasks(directory_path, images=None):
    """枚举任务目录：每个任务含 task.toml（名称取目录名，元信息取自 toml）。

    images 为「本机已有的镜像集合」，用于标记 image_ready；由调用方注入，
    避免适配器层去起 docker 子进程。
    """
    items = []
    if not directory_path or not os.path.isdir(directory_path):
        return items
    ready = images or set()
    for name in sorted(os.listdir(directory_path)):
        path = os.path.join(directory_path, name)
        task_file = os.path.join(path, "task.toml")
        if not os.path.isfile(task_file):
            continue
        meta = read_toml(task_file)
        info = meta.get("metadata") or {}
        env = meta.get("environment") or {}
        image = str(env.get("docker_image") or "").strip()
        items.append({
            "name": name,
            "dir": path,
            "title": str(info.get("display_title") or "").strip() or name,
            "category": str(info.get("category") or "").strip(),
            "language": str(info.get("language") or "").strip(),
            "agent_timeout_sec": _num((meta.get("agent") or {}).get("timeout_sec")),
            "verifier_timeout_sec": _num((meta.get("verifier") or {}).get("timeout_sec")),
            "docker_image": image,
            # 镜像是否已在本机（未拉取时首次运行要等下载）
            "image_ready": image in ready if image else False,
        })
    return items


def names(directory_path, images=None):
    return [it["name"] for it in list_tasks(directory_path, images)]


def instruction(task_dir, limit=600):
    """任务说明摘要（instruction.md / README.md），用于结果表展示与检索。"""
    for name in ("instruction.md", "README.md"):
        path = os.path.join(str(task_dir or ""), name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return " ".join(fh.read().split())[:limit]
            except OSError:
                return ""
    return ""


def sample(catalogue, limit=0, seed=0):
    """按 limit/seed 挑任务：limit<=0 取全部；seed=0 按目录顺序取前 N 个（可复现）。"""
    if limit and limit < len(catalogue):
        if seed:
            return sorted(random.Random(seed).sample(catalogue, limit),
                          key=lambda t: t["name"])
        return catalogue[:limit]
    return list(catalogue)
