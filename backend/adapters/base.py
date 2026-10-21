"""数据集适配器基类、注册表与统一入口。

分层约定（上层业务只认本包，不认具体格式）：

    dataset/datasets.json
        └─ dataset_registry   清单：文件名通配符、字段映射、reader 名
            └─ adapters       本包：按 reader 名选择读取实现
                └─ eval_core / agent_core / api   业务层

新增一种数据集格式只需要三步，业务层零改动：
1. 在 adapters/ 下新增模块，继承 DatasetAdapter 实现 load()；
2. 用 @register 登记 reader 名；
3. 在 datasets.json 的条目里写 "reader": "<名字>"。

表格类适配器统一产出 NORM_COLUMNS 列，上层拿到的永远是同一张表；
Agent 任务集（task.toml 目录约定）的适配见 adapters/tasks.py。
"""
import os

import pandas as pd

import dataset_registry

# 所有表格类数据集归一化后的列（original_index 用于回取原表行，图片按它关联）
NORM_COLUMNS = ("original_index", "problem", "answer", "topic", "answer_type", "subject")


class DatasetAdapter:
    """一种数据格式一个实现：把源文件读成统一字段的 DataFrame。"""

    reader = ""

    def load(self, files, fields=None, limit=0, seed=0, on_event=None):
        """读取数据文件并归一化，返回 (df, images)。

        files  按配置通配符匹配到的文件列表（已排序，可能多个分片）；
        fields 统一字段 -> 源列名的映射（配置里没声明的按同名列读）；
        limit>0 时在本层完成采样，图片只加载采样命中的行；
        images 为 {original_index: data-url}，无图片的数据集返回空 dict。
        """
        raise NotImplementedError


_ADAPTERS = {}


def register(adapter_cls):
    """登记适配器（reader 名为索引），返回类本身以便装饰器写法。"""
    reader = str(getattr(adapter_cls, "reader", "") or "").strip()
    if not reader:
        raise ValueError("适配器必须声明非空的 reader 名")
    _ADAPTERS[reader] = adapter_cls()
    return adapter_cls


def adapter_for(reader):
    """按 reader 名取适配器实例；未知 reader 退回 csv（reader_for 已按扩展名兜底）。"""
    return _ADAPTERS.get(str(reader or "").strip()) or _ADAPTERS["csv"]


def readers():
    """已注册的 reader 名（供文档/自检展示）。"""
    return sorted(_ADAPTERS)


# ------------------------------------------------------------------ 清单门面
def catalog():
    """全部数据集条目（清单来自 dataset/datasets.json，带缓存与失效检测）。"""
    return dataset_registry.datasets()


def find(name_or_path):
    """按数据集名称 / 数据文件路径 / 数据集目录反查条目。"""
    return dataset_registry.find(name_or_path)


def default_path():
    """默认数据文件路径。"""
    return dataset_registry.default_path()


def reader_for(path, spec=None):
    """数据集用哪种 reader：配置优先，自定义路径按扩展名兜底。"""
    if spec is None:
        spec = dataset_registry.spec_for_path(path)
    if spec and spec.get("reader"):
        return spec["reader"]
    return "parquet" if str(path or "").lower().endswith((".parquet", ".pq")) else "csv"


def label(path, spec=None):
    """数据集展示名：配置里的名称，自定义路径取所在目录名。"""
    if spec is None:
        spec = dataset_registry.spec_for_path(path)
    if spec and spec.get("name"):
        return spec["name"]
    return os.path.dirname(path or "").rstrip("/").split("/")[-1] or (path or "")


def load(path, limit=0, seed=0, on_event=None, spec=None):
    """读取任意数据集并归一化为统一字段（业务层唯一入口）。

    path 为数据文件路径（也允许页面上填任意 csv/parquet 路径）；spec 为清单条目，
    缺省时按路径反查配置。返回 (df, images)，df 含 NORM_COLUMNS。
    """
    on_event = on_event or (lambda kind, payload: None)
    if spec is None:
        spec = dataset_registry.spec_for_path(path)
    adapter = adapter_for(reader_for(path, spec))
    fields = (spec or {}).get("fields") or {}
    files = [f for f in (spec or {}).get("files") or [] if os.path.isfile(f)]
    if not files:
        files = [path]
    on_event("load_start", {"path": path, "kind": adapter.reader,
                            "dataset": label(path, spec)})
    df, images = adapter.load(files, fields, limit, seed, on_event)
    on_event("load_end", {"rows": len(df), "images": len(images)})
    return df, images


# ------------------------------------------------------------------ 表格共用
def mapped(fields, name, default):
    """统一字段对应的源列名：配置里声明过则用映射，否则按同名/默认列名。"""
    return (fields or {}).get(name) or default


def str_col(df, name, default=""):
    if name in df.columns:
        return df[name].fillna(default).astype(str)
    return pd.Series([default] * len(df), index=df.index)


def normalise(raw, fields=None, limit=0, seed=0, on_event=None):
    """把原始表映射为统一字段（含采样与图片抽取），表格类适配器共用。"""
    if "original_index" in raw.columns:
        idx = raw["original_index"].astype(int)
    else:
        idx = pd.Series(range(len(raw)), index=raw.index)
    df = pd.DataFrame({"original_index": idx})
    for col in ("problem", "answer", "topic", "answer_type", "subject"):
        df[col] = str_col(raw, mapped(fields, col, col),
                          "Unknown" if col == "topic" else "")
    image_col = mapped(fields, "image", "image")
    images = ({int(i): v for i, v in str_col(raw, image_col).items() if v}
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
    return picked[list(NORM_COLUMNS)].reset_index(drop=True), images
