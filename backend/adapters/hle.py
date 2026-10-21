"""HLE（Humanity's Last Exam）适配器。

与其他表格数据集的区别只有两点：
1. 图片列是内嵌 base64（整表解压后超过 1GB），必须按 row group 只读命中行，
   全量读取会既慢又吃内存；
2. 源列名与统一字段不同（question/category/raw_subject），但字段映射仍可由
   datasets.json 覆盖，这里的默认值只是 HLE 的已知布局。

因此单独成一个适配器，上层（eval_core / api）只知道「reader=hle」。
"""
import pandas as pd

from .base import (NORM_COLUMNS, DatasetAdapter, mapped, register, str_col)


def _row_group_bounds(pf):
    """返回每个 row group 的行号区间 [(start, end), ...]。"""
    bounds, start = [], 0
    for g in range(pf.num_row_groups):
        n = pf.metadata.row_group(g).num_rows
        bounds.append((start, start + n))
        start += n
    return bounds


def _read_images(pf, indexes, on_event=None, col="image"):
    """仅读取包含目标行的 row group 的图片列。"""
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


@register
class HleAdapter(DatasetAdapter):
    reader = "hle"

    def load(self, files, fields=None, limit=0, seed=0, on_event=None):
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(files[0])
        names = pf.schema_arrow.names
        cols = {name: mapped(fields, name, default) for name, default in (
            ("problem", "question"), ("answer", "answer"), ("topic", "category"),
            ("answer_type", "answer_type"), ("subject", "raw_subject"))}
        light = [c for c in dict.fromkeys(cols.values()) if c in names]

        raw = pf.read(columns=light).to_pandas()
        df = pd.DataFrame({
            "original_index": pd.Series(range(len(raw)), index=raw.index),
            "problem": str_col(raw, cols["problem"]),
            "answer": str_col(raw, cols["answer"]),
            "topic": str_col(raw, cols["topic"], "Unknown"),
            "answer_type": str_col(raw, cols["answer_type"], "exactMatch"),
            "subject": str_col(raw, cols["subject"]),
        })
        del raw

        picked = df
        if limit and limit < len(df):
            picked = df.sample(n=limit, random_state=seed).sort_index()

        image_col = mapped(fields, "image", "image")
        images = {}
        if image_col in names and len(picked):
            images = _read_images(pf, picked["original_index"].tolist(), on_event,
                                  image_col)
        return picked[list(NORM_COLUMNS)].reset_index(drop=True), images
