"""数据集注册表：数据集清单全部来自 dataset/datasets.json，代码里不再硬编码。

目录约定：每个数据集以「数据集名称」作为顶级目录，放在 consts.DATASET_DIR（dataset/）
之下，数据文件放在该目录内（可再分子目录）。新增数据集只需两步，无需改代码：

1. 把数据集目录拷进 dataset/（目录名即数据集名称）；
2. 在 dataset/datasets.json 的 datasets 里加一条：name + file（支持 * 通配符），
   必要时用 reader 指定读取方式、用 fields 把源字段映射到统一字段。

统一字段（NORM_FIELDS）：problem / answer / topic / answer_type / subject / image；
未在 fields 中声明的字段按同名列读取，列不存在时填空值。
"""
import glob
import json
import os
import threading

import consts

CONFIG_NAME = "datasets.json"
CONFIG_VERSION = 1
# 读取方式（reader）：
#   csv     CSV 表；
#   parquet 通用 parquet（多分片自动拼接）；
#   hle     HLE 专用：按 row group 只读命中行的 image 列（整表含 base64 图片时快很多）
READERS = ("csv", "parquet", "hle")
NORM_FIELDS = ("problem", "answer", "topic", "answer_type", "subject", "image")

_CACHE = {"key": None, "state": None}
_LOCK = threading.Lock()


def config_path():
    return os.path.join(consts.DATASET_DIR, CONFIG_NAME)


def _load_config():
    """读取配置文件；文件缺失或损坏时返回空配置（页面会提示没有可用数据集）。"""
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dataset_dir(spec):
    """数据集目录：dataset/<数据集名称>。"""
    return os.path.join(consts.DATASET_DIR, str(spec.get("name") or "").strip())


def _data_files(spec):
    """按 file 通配符匹配数据集目录内的数据文件（相对路径，支持子目录）。"""
    pattern = str(spec.get("file") or "").strip()
    if not pattern:
        return []
    return sorted(p for p in glob.glob(os.path.join(dataset_dir(spec), pattern))
                  if os.path.isfile(p))


def _norm_fields(raw):
    """字段映射只保留统一字段，忽略其它键，避免配置写错时静默影响结果。"""
    fields = {}
    if isinstance(raw, dict):
        for key, col in raw.items():
            key, col = str(key).strip(), str(col or "").strip()
            if key in NORM_FIELDS and col:
                fields[key] = col
    return fields


def _norm_reader(raw, files):
    """reader 缺省时按数据文件扩展名推断。"""
    reader = str(raw or "").strip().lower()
    if reader in READERS:
        return reader
    return "csv" if any(f.lower().endswith(".csv") for f in files) else "parquet"


def _norm_agent(spec):
    """Agent 任务集描述（配置里的 agent 段）；不是 agent 数据集时返回 None。

    这类数据集的「题目」不是一道道问答题，而是任务目录（每个任务自带 task.toml、
    环境 Dockerfile 与测试脚本），由 Agent 评测页交给外部沙箱框架逐任务执行判定，
    因此与 reader/fields 无关。

    tasks 是相对数据集目录的路径；留空或写 "." 表示「任务目录就是数据集根目录」
    （如 terminal-bench-2.0：任务目录直接放在数据集目录下，没有多余的 tasks/ 层）。
    """
    raw = spec.get("agent")
    if not isinstance(raw, dict):
        return None
    tasks = str(raw.get("tasks") or "").strip()
    root = dataset_dir(spec)
    at_root = tasks in ("", ".", "./")
    return {
        "framework": str(raw.get("framework") or "pier").strip() or "pier",
        # normpath 抹掉 "dataset/xxx/." 里的尾随点，路径比较/展示才干净
        "tasks_dir": root if at_root else os.path.normpath(os.path.join(root, tasks)),
        "tasks": tasks,
        # 下拉框展示用：任务在数据集根目录时给个明确的说法，避免显示 "."
        "tasks_text": "数据集根目录" if at_root else tasks,
    }


def _build_state():
    cfg = _load_config()
    raw_items = cfg.get("datasets")
    entries = []
    for raw in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        spec = dict(raw, name=name)
        directory = dataset_dir(spec)
        files = _data_files(spec)
        reader = _norm_reader(spec.get("reader"), files)
        entries.append({
            "name": name,
            "title": str(spec.get("title") or "").strip() or name,
            "dir": directory,
            "path": files[0] if files else os.path.join(
                directory, str(spec.get("file") or "").strip()),
            "files": files,
            "reader": reader,
            "is_csv": reader == "csv",
            "fields": _norm_fields(spec.get("fields")),
            # runnable=false 表示该数据集不适合当前「单轮作答 + 裁判判分」流程
            "runnable": spec.get("runnable") is not False,
            "note": str(spec.get("note") or "").strip(),
            # agent 段：该数据集是 Agent 任务集（如 DeepSWE）时为 dict，否则为 None
            "agent": _norm_agent(spec),
            "exists": bool(files) and os.path.isdir(directory),
        })
    return {"default": str(cfg.get("default") or "").strip(), "entries": entries}


def _signature(names):
    """缓存指纹：配置文件 + 各数据集目录的 mtime，变化时自动重建。"""
    key = []
    try:
        st = os.stat(config_path())
        key.append((st.st_mtime_ns, st.st_size))
    except OSError:
        key.append(None)
    for name in sorted(names):
        try:
            key.append((name, os.stat(os.path.join(consts.DATASET_DIR, name)).st_mtime_ns))
        except OSError:
            key.append((name, None))
    return tuple(key)


def _state():
    """数据集配置状态（带缓存）：首次解析后仅用几次 stat 判断是否失效。"""
    with _LOCK:
        cached = _CACHE["state"]
        names = [it["name"] for it in (cached or {}).get("entries", [])]
        key = _signature(names)
        if cached is not None and _CACHE["key"] == key:
            return cached
        state = _build_state()
        # 首次解析时缓存里还没有数据集名，用真实名称重算指纹
        _CACHE["key"] = _signature([it["name"] for it in state["entries"]])
        _CACHE["state"] = state
        return state


def datasets():
    """全部数据集条目（供 /api/meta 与页面下拉框使用，返回拷贝避免改动缓存）。"""
    return [dict(it) for it in _state()["entries"]]


def default_path():
    """默认数据集路径：优先取配置里的 default，其次第一个数据文件存在的数据集。"""
    state = _state()
    entries = state["entries"]
    for it in entries:
        if it["name"] == state["default"]:
            return it["path"]
    for it in entries:
        if it["exists"]:
            return it["path"]
    return entries[0]["path"] if entries else ""


def spec_for_path(path):
    """按数据文件路径反查数据集条目（把 reader/fields 交给加载器使用）。

    自定义路径（不在配置的某个数据集目录内）返回 None，加载器按扩展名兜底。
    """
    real = os.path.realpath(str(path or ""))
    if not real:
        return None
    for it in _state()["entries"]:
        if not os.path.isdir(it["dir"]):
            continue
        root = os.path.realpath(it["dir"])
        if real == root or real.startswith(root + os.sep):
            return dict(it)
    return None


def find(name_or_path):
    """按数据集名称 / 数据文件路径 / 数据集目录查找条目，找不到时返回 None。"""
    key = str(name_or_path or "").strip()
    if not key:
        return None
    for it in _state()["entries"]:
        if key in (it["name"], it["path"], it["dir"]):
            return dict(it)
    return spec_for_path(key)
