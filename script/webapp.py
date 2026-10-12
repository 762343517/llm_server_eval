"""SimpleQA-Verified 评测 Web 服务（FastAPI + Jinja2 服务端渲染）。

启动:
  python3 script/webapp.py --host 0.0.0.0 --port 8000

页面:
  /                  配置表单 + 历史任务列表（SSR）
  /runs/<id>         任务详情：进度、指标、逐题结果（SSR + 轮询增量刷新）
  /csv?path=<p>      把工作区内的 CSV 文件渲染成 HTML 表格

接口:
  GET  /api/runs/<id>/status    进度快照
  GET  /api/runs/<id>/items     逐题结果 JSON
  GET  /runs/<id>/download.csv  下载 CSV（同时接受 HEAD 探测）
  GET  /runs/<id>/download.json 下载汇总 JSON（同时接受 HEAD）
  GET  /runs/<id>/view.csv      以表格形式查看任务结果 CSV
  POST /api/runs/<id>/stop      停止任务
  POST /runs/<id>/delete        删除任务
"""
import argparse
import csv
import json
import os
import sys
from urllib.parse import quote as urlquote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# CSV 预览只允许读取工作区内的文件，防止路径穿越
WORKSPACE_ROOT = os.path.realpath(os.path.join(BASE_DIR, os.pardir))
# 单次渲染行数上限（预览用，超出部分提示下载）
CSV_DEFAULT_LIMIT = 500
CSV_MAX_LIMIT = 5000
sys.path.insert(0, BASE_DIR)

import eval_core  # noqa: E402
import run_manager  # noqa: E402

DEFAULT_ENDPOINT = os.environ.get(
    "ENDPOINT", "http://10.174.0.70:34000/v1/chat/completions"
)
DEFAULT_API_KEY = os.environ.get(
    "API_KEY", "sk-vgeocsgvmkutmzivcrkaiyxxjygwcazjozlgkpyqmamovclv"
)

app = FastAPI(title="SimpleQA-Verified 评测平台")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
          name="static")
manager = run_manager.RunManager()

STATUS_TEXT = {
    "running": "运行中", "succeeded": "已完成",
    "failed": "失败", "stopped": "已停止",
    "interrupted": "已中断",
}

# 思考开关的三态取值（"" = 不传字段，交给服务端默认），标签仅供模板渲染
THINKING_LABELS = {"": "服务端默认（不传该字段）", "enabled": "开启思考",
                   "disabled": "关闭思考"}
EFFORT_LABELS = {"": "默认（不传）", "low": "low", "medium": "medium",
                 "high": "high", "max": "max"}
THINKING_OPTIONS = [(v, THINKING_LABELS[v]) for v in eval_core.THINKING_CHOICES]
EFFORT_OPTIONS = [(v, EFFORT_LABELS[v]) for v in eval_core.EFFORT_CHOICES]


def defaults():
    """表单默认值：本进程首次启动用静态默认，之后沿用上次提交的配置。

    历史配置落在 runs/.last_config.json（runs/ 已在 .gitignore 中），
    因此从任务详情页返回首页、或在首页删除任务后重新渲染，表单内容都不会被清空。
    """
    d = {
        "endpoint": DEFAULT_ENDPOINT,
        "api_key": DEFAULT_API_KEY,
        "judge_endpoint": "",
        "judge_api_key": "",
        "model": "GLM-5.2-zhipu",
        "judge_model": "DeepSeek-V4-Flash-0731",
        "data": eval_core.DEFAULT_DATA,
        "thinking": "",
        "reasoning_effort": "",
        "judge_thinking": "",
        "judge_reasoning_effort": "",
        "concurrency": 8,
        "judge_concurrency": 8,
        "limit": 0,
        "seed": 0,
        "max_tokens": 32768,
        "max_tokens_cap": 65536,
        "max_incomplete_attempts": 2,
    }
    saved = load_last_cfg()
    for k, v in saved.items():
        if k in d and v is not None:
            d[k] = v
    return d


LAST_CFG_NAME = ".last_config.json"


def _last_cfg_path():
    return os.path.join(run_manager.RUNS_DIR, LAST_CFG_NAME)


def last_cfg_mtime():
    """上次提交配置的写入时间；没有配置文件时返回 0。"""
    try:
        return os.path.getmtime(_last_cfg_path())
    except OSError:
        return 0.0


def load_last_cfg():
    """读取上次提交的配置；文件缺失/损坏时返回空 dict。"""
    try:
        with open(_last_cfg_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_last_cfg(cfg):
    """落盘上次配置（含密钥，权限 600），失败不影响评测启动。"""
    path = _last_cfg_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def _resolve_path(raw):
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


def _read_csv(path, limit):
    """读取 CSV 表头与前 limit 行，返回 (header, rows, total, encoding)。

    编码依次尝试 utf-8-sig / gbk / latin-1；分隔符由 Sniffer 嗅探，失败回退逗号。
    total 是文件的数据行总数，用于提示"仅展示前 N 行"。
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
        except UnicodeDecodeError as e:  # 换编码重试
            err = e
    raise HTTPException(400, "无法解码 CSV 文件: {}".format(err))


def _numeric_columns(header, rows, sample=200):
    """逐列判断是否全为数字，供模板右对齐（题号 / token / 耗时等）。"""
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


def _csv_context(path, limit, title="", back_url="/", download_url=""):
    """组装 CSV 表格页所需的全部模板变量。"""
    limit = max(1, min(int(limit or CSV_DEFAULT_LIMIT), CSV_MAX_LIMIT))
    header, rows, total, enc = _read_csv(path, limit)
    width = max([len(header)] + [len(r) for r in rows]) if header else 0
    header = (header + [""] * width)[:width]
    rows = [(r + [""] * width)[:width] for r in rows]
    return {
        "title": title or os.path.basename(path),
        "path": os.path.relpath(path, WORKSPACE_ROOT),
        "header": header,
        "rows": rows,
        "numeric": _numeric_columns(header, rows),
        "total": total,
        "shown": len(rows),
        "width": width,
        "encoding": enc,
        "limit": limit,
        "max_limit": CSV_MAX_LIMIT,
        "back_url": back_url,
        "download_url": download_url,
        "error": None,
    }


def _csv_error(request, path, title, detail, back_url, download_url, status=404):
    return templates.TemplateResponse(request, "csv.html", {
        "title": title or path or "CSV",
        "path": path, "header": [], "rows": [], "numeric": [],
        "total": 0, "shown": 0, "width": 0, "encoding": "",
        "limit": CSV_DEFAULT_LIMIT, "max_limit": CSV_MAX_LIMIT,
        "back_url": back_url, "download_url": download_url,
        "error": detail,
    }, status_code=status)


def _decorate(runs):
    for r in runs:
        r["status_text"] = STATUS_TEXT.get(r.get("status"), r.get("status"))
        s = r.get("summary") or {}
        r["pass_at_1"] = s.get("pass_at_1")
        r["n_done"] = s.get("num_questions")
        r["expected"] = s.get("expected_questions")
        prog = r.get("progress") or {}
        r["stage"] = prog.get("stage", "")
        r["pct"] = prog.get("pct", 0.0)
    return runs


# ---------------------------------------------------------------- 页面
@app.get("/")
def index(request: Request):
    runs = _decorate(manager.list_runs())
    datasets = eval_core.available_datasets()
    for d in datasets:
        d["is_csv"] = d["path"].lower().endswith(".csv")
    data_defaults = defaults()
    # 上次用的是内置列表之外的路径时，回填到「自定义数据集路径」而不是丢掉
    known = {d["path"] for d in datasets}
    custom_data = "" if data_defaults["data"] in known else data_defaults["data"]
    return templates.TemplateResponse(request, "index.html", {
        "defaults": data_defaults,
        "custom_data": custom_data,
        "saved_cfg_at": last_cfg_mtime(),
        "runs": runs,
        "datasets": datasets,
        "data_exists": any(d["exists"] for d in datasets),
        "thinking_options": THINKING_OPTIONS,
        "effort_options": EFFORT_OPTIONS,
    })


@app.post("/start")
def start(
    endpoint: str = Form(...),
    api_key: str = Form(""),
    judge_endpoint: str = Form(""),
    judge_api_key: str = Form(""),
    model: str = Form(...),
    judge_model: str = Form(...),
    data: str = Form(eval_core.DEFAULT_DATA),
    data_custom: str = Form(""),
    thinking: str = Form(""),
    reasoning_effort: str = Form(""),
    judge_thinking: str = Form(""),
    judge_reasoning_effort: str = Form(""),
    concurrency: int = Form(8),
    judge_concurrency: int = Form(8),
    limit: int = Form(0),
    seed: int = Form(0),
    max_tokens: int = Form(32768),
    max_tokens_cap: int = Form(65536),
    max_incomplete_attempts: int = Form(2),
):
    data = data_custom.strip() or data.strip()
    endpoint = endpoint.strip()
    if not model.strip() or not judge_model.strip():
        raise HTTPException(400, "模型与裁判模型不能为空")
    if not endpoint.startswith("http"):
        raise HTTPException(400, "被测模型 API 端点必须以 http 开头")
    # 裁判端点/密钥留空时复用被测模型的，二者可指向不同服务
    judge_endpoint_raw = judge_endpoint.strip()
    judge_api_key_raw = judge_api_key.strip()
    judge_endpoint = judge_endpoint_raw or endpoint
    if not judge_endpoint.startswith("http"):
        raise HTTPException(400, "裁判模型 API 端点必须以 http 开头")
    judge_api_key = judge_api_key_raw or api_key.strip()
    if not os.path.exists(data):
        raise HTTPException(400, f"数据集不存在: {data}")
    if max_tokens_cap < max_tokens:
        max_tokens_cap = max_tokens
    for label, value in (("思考模式", thinking), ("裁判思考模式", judge_thinking)):
        if value not in eval_core.THINKING_CHOICES:
            raise HTTPException(400, f"{label}取值非法: {value}")
    for label, value in (("思考强度", reasoning_effort),
                         ("裁判思考强度", judge_reasoning_effort)):
        if value not in eval_core.EFFORT_CHOICES:
            raise HTTPException(400, f"{label}取值非法: {value}")
    cfg = {
        "endpoint": endpoint, "api_key": api_key.strip(),
        "judge_endpoint": judge_endpoint, "judge_api_key": judge_api_key,
        "model": model.strip(), "judge_model": judge_model.strip(),
        "data": data.strip(),
        "thinking": thinking, "reasoning_effort": reasoning_effort,
        "judge_thinking": judge_thinking,
        "judge_reasoning_effort": judge_reasoning_effort,
        "concurrency": max(1, concurrency),
        "judge_concurrency": max(1, judge_concurrency),
        "limit": max(0, limit), "seed": seed,
        "max_tokens": max_tokens, "max_tokens_cap": max_tokens_cap,
        "max_incomplete_attempts": max(1, max_incomplete_attempts),
    }
    # 表单默认值沿用「原样填写」的裁判端点/密钥，保留“留空=复用被测模型”的状态
    saved = {k: cfg[k] for k in defaults() if k in cfg}
    saved["judge_endpoint"] = judge_endpoint_raw
    saved["judge_api_key"] = judge_api_key_raw
    save_last_cfg(saved)
    run_id = manager.start(cfg)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}")
def run_page(request: Request, run_id: str):
    snap = manager.get(run_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    # 逐题明细整段下发，页面用可滚动单元格完整展示，不再截断文本
    items = manager.read_items(run_id, limit=300)
    snap["status_text"] = STATUS_TEXT.get(snap.get("status"), snap.get("status"))
    cfg = snap.get("cfg") or {}
    snap["thinking_text"] = eval_core.thinking_label(
        cfg.get("thinking"), cfg.get("reasoning_effort"))
    snap["judge_thinking_text"] = eval_core.thinking_label(
        cfg.get("judge_thinking"), cfg.get("judge_reasoning_effort"))
    return templates.TemplateResponse(request, "run.html", {
        "run": snap, "items": items,
        "total_items": len(manager.read_items(run_id)) if items else 0,
    })


@app.get("/runs/{run_id}/view.csv")
def run_csv_view(request: Request, run_id: str, limit: int = CSV_DEFAULT_LIMIT):
    """把任务结果 CSV（导出用的同一份文件）渲染成 HTML 表格。"""
    if not manager.get(run_id):
        raise HTTPException(404, "任务不存在")
    back_url = "/runs/" + run_id
    download_url = "/runs/{}/download.csv".format(run_id)
    title = "{} 结果 CSV".format(run_id)
    path, _ = manager.export_csv(run_id)
    if not path:
        return _csv_error(request, "", title, "该任务暂无可导出的结果（逐题记录为空）。",
                          back_url, download_url)
    try:
        ctx = _csv_context(path, limit, title, back_url, download_url)
    except HTTPException as e:
        return _csv_error(request, path, title, e.detail, back_url, download_url,
                          status=e.status_code)
    return templates.TemplateResponse(request, "csv.html", ctx)


@app.get("/csv")
def csv_view(request: Request, path: str = "", limit: int = CSV_DEFAULT_LIMIT,
             download: int = 0, back: str = "", title: str = ""):
    """查看工作区内任意 CSV：默认渲染表格，download=1 时直接下载原文件。"""
    real = _resolve_path(path)
    if download:
        return FileResponse(real, media_type="text/csv",
                            filename=os.path.basename(real))
    back_url = back or "/"
    download_url = "/csv?path={}&download=1".format(urlquote(path.strip()))
    try:
        ctx = _csv_context(real, limit, title, back_url, download_url)
    except HTTPException as e:
        return _csv_error(request, path, title, e.detail, back_url, download_url,
                          status=e.status_code)
    return templates.TemplateResponse(request, "csv.html", ctx)


# ---------------------------------------------------------------- 接口
@app.get("/api/runs")
def api_runs():
    return JSONResponse({"runs": _decorate(manager.list_runs())})


@app.get("/api/runs/{run_id}/status")
def api_status(run_id: str):
    snap = manager.get(run_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    snap["status_text"] = STATUS_TEXT.get(snap.get("status"), snap.get("status"))
    return JSONResponse(snap)


@app.get("/api/runs/{run_id}/items")
def api_items(run_id: str, limit: int = 500, grade: str = "", q: str = ""):
    rows = manager.read_items(run_id)
    if grade:
        rows = [r for r in rows if r.get("grade") == grade]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in str(r.get("problem", "")).lower()
                or ql in str(r.get("prediction", "")).lower()]
    return JSONResponse({"total": len(rows), "items": rows[:limit]})


@app.post("/api/runs/{run_id}/stop")
def api_stop(run_id: str):
    if not manager.stop(run_id):
        raise HTTPException(404, "任务不存在或未在运行")
    return JSONResponse({"ok": True})


@app.post("/runs/{run_id}/delete")
def delete_run(run_id: str):
    manager.delete(run_id)
    return RedirectResponse("/", status_code=303)


@app.api_route("/runs/{run_id}/download.csv", methods=["GET", "HEAD"])
def download_csv(run_id: str):
    if not manager.get(run_id):
        raise HTTPException(404, "任务不存在")
    path, n = manager.export_csv(run_id)
    if not path:
        raise HTTPException(404, "暂无可导出的结果")
    return FileResponse(path, media_type="text/csv",
                        filename=f"simpleqa_{run_id}.csv")


@app.api_route("/runs/{run_id}/download.json", methods=["GET", "HEAD"])
def download_json(run_id: str):
    snap = manager.get(run_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    path = manager.out_base(run_id) + ".json"
    if not os.path.exists(path):
        raise HTTPException(404, "暂无可导出的汇总")
    return FileResponse(path, media_type="application/json",
                        filename=f"simpleqa_{run_id}.json")


@app.get("/health")
def health():
    return {"ok": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
