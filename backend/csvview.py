"""工作区内 CSV 的读取与预览（供数据集预览、结果 CSV 预览共用）。

只读取工作区（项目根）内的文件，防路径穿越；单次预览行数有上限，超出提示下载。
"""
import csv
import os

from fastapi import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.realpath(os.path.join(BASE_DIR, os.pardir))
CSV_DEFAULT_LIMIT = 500
CSV_MAX_LIMIT = 5000


def resolve_path(raw):
    """把用户传入的路径限制在工作区内，返回真实绝对路径。"""
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(400, "缺少文件路径")
    target = raw if os.path.isabs(raw) else os.path.join(WORKSPACE_ROOT, raw)
    real = os.path.realpath(target)
    if real != WORKSPACE_ROOT and not real.startswith(WORKSPACE_ROOT + os.sep):
        raise HTTPException(403, "只允许查看工作区内的文件")
    if not os.path.isfile(real):
        raise HTTPException(404, "文件不存在: " + raw)
    return real


def read_csv(path, limit):
    """读取 CSV 表头与前 limit 行，返回 (header, rows, total, encoding)。

    编码依次尝试 utf-8-sig / gbk / latin-1；分隔符由 Sniffer 嗅探，失败回退逗号。
    total 是文件的数据行总数，用于提示「仅展示前 N 行」。
    """
    err = None
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            with open(path, "r", newline="", encoding=enc) as f:
                sample = f.read(8192)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.reader(f, dialect)
                header = next(reader, None)
                if header is None:
                    return [], [], 0, enc
                rows, total = [], 0
                for row in reader:
                    total += 1
                    if len(rows) < limit:
                        rows.append(row)
            return header, rows, total, enc
        except UnicodeDecodeError as e:      # 换编码重试
            err = e
    raise HTTPException(400, "无法解码 CSV 文件: {}".format(err))


def numeric_columns(header, rows, sample=200):
    """逐列判断是否全为数字，供前端右对齐（题号 / token / 耗时等）。"""
    flags = []
    for i in range(len(header)):
        vals = [str(r[i]).strip() for r in rows[:sample]
                if i < len(r) and str(r[i]).strip()]
        ok = bool(vals)
        for v in vals:
            try:
                float(v.replace(",", ""))
            except ValueError:
                ok = False
                break
        flags.append(ok)
    return flags


def preview(path, limit, title=""):
    """组装 CSV 表格所需的全部字段（前端表格直接渲染）。"""
    limit = max(1, min(int(limit or CSV_DEFAULT_LIMIT), CSV_MAX_LIMIT))
    header, rows, total, enc = read_csv(path, limit)
    width = max([len(header)] + [len(r) for r in rows]) if header else 0
    header = (header + [""] * width)[:width]
    rows = [(r + [""] * width)[:width] for r in rows]
    return {
        "title": title or os.path.basename(path),
        "path": os.path.relpath(path, WORKSPACE_ROOT),
        "header": header,
        "rows": rows,
        "numeric": numeric_columns(header, rows),
        "total": total,
        "shown": len(rows),
        "width": width,
        "encoding": enc,
        "limit": limit,
        "max_limit": CSV_MAX_LIMIT,
    }
