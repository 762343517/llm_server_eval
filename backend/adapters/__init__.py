"""数据集适配器层：业务代码读取数据集的唯一入口。

用法：
    import adapters

    adapters.catalog()                  # 数据集清单（页面下拉框）
    adapters.load(path, limit=10)       # 读成统一字段的 DataFrame (+ 图片映射)
    adapters.tasks.resolve_dir(name)    # Agent 任务集目录
    adapters.tasks.list_tasks(directory)  # 任务清单（task.toml 元信息）

具体格式的适配在子模块中：table（csv/parquet）、hle（按 row group 读图）、
tasks（Agent 任务集）。新增 reader 见 base.py 顶部说明。
"""
from .base import (NORM_COLUMNS, DatasetAdapter, adapter_for, catalog, default_path,
                   find, label, load, reader_for, readers, register)
# 导入即注册（顺序无关，reader 名唯一）
from . import hle, table, tasks  # noqa: F401,E402

__all__ = [
    "NORM_COLUMNS", "DatasetAdapter", "adapter_for", "catalog", "default_path",
    "find", "label", "load", "reader_for", "readers", "register", "tasks",
]
