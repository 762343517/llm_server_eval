"""通用表格适配器：CSV 与 parquet。

两者只差「怎么把源文件读成一张表」，归一化（字段映射/采样/图片抽取）完全共用，
新增表格类容器（例如 JSONL 表格）时照着这里再实现一个 load() 即可。
"""
import pandas as pd

from .base import DatasetAdapter, normalise, register


@register
class CsvAdapter(DatasetAdapter):
    reader = "csv"

    def load(self, files, fields=None, limit=0, seed=0, on_event=None):
        """CSV 数据集（多分片按文件名顺序拼接）。"""
        if len(files) == 1:
            raw = pd.read_csv(files[0])
        else:
            raw = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
        return normalise(raw, fields, limit, seed, on_event)


@register
class ParquetAdapter(DatasetAdapter):
    reader = "parquet"

    def load(self, files, fields=None, limit=0, seed=0, on_event=None):
        """通用 parquet 数据集（多分片交由 pandas 拼接）。"""
        raw = pd.read_parquet(files if len(files) > 1 else files[0])
        return normalise(raw, fields, limit, seed, on_event)
