"""SimpleQA-Verified 评测 Web 服务（FastAPI + Jinja2 服务端渲染）。

启动:
  python3 script/webapp.py --host 0.0.0.0 --port 8000

页面:
  /                  配置表单 + 历史任务列表（SSR）
  /runs/<id>         任务详情：进度、指标、逐题结果（SSR + 轮询增量刷新）

接口:
  GET  /api/runs/<id>/status    进度快照
  GET  /api/runs/<id>/items     逐题结果 JSON
  GET  /runs/<id>/download.csv  下载 CSV
  POST /api/runs/<id>/stop      停止任务
  POST /runs/<id>/delete        删除任务
"""
import argparse
import os
import sys

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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


def defaults():
    return {
        "endpoint": DEFAULT_ENDPOINT,
        "api_key": DEFAULT_API_KEY,
        "model": "GLM-5.2-zhipu",
        "judge_model": "DeepSeek-V4-Flash-0731",
        "data": eval_core.DEFAULT_DATA,
        "concurrency": 8,
        "judge_concurrency": 8,
        "limit": 0,
        "seed": 0,
        "max_tokens": 32768,
        "max_tokens_cap": 65536,
        "max_incomplete_attempts": 2,
    }


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
    return templates.TemplateResponse(request, "index.html", {
        "defaults": defaults(),
        "runs": runs,
        "datasets": datasets,
        "data_exists": any(d["exists"] for d in datasets),
    })


@app.post("/start")
def start(
    endpoint: str = Form(...),
    api_key: str = Form(""),
    model: str = Form(...),
    judge_model: str = Form(...),
    data: str = Form(eval_core.DEFAULT_DATA),
    data_custom: str = Form(""),
    concurrency: int = Form(8),
    judge_concurrency: int = Form(8),
    limit: int = Form(0),
    seed: int = Form(0),
    max_tokens: int = Form(32768),
    max_tokens_cap: int = Form(65536),
    max_incomplete_attempts: int = Form(2),
):
    data = data_custom.strip() or data.strip()
    if not model.strip() or not judge_model.strip():
        raise HTTPException(400, "模型与裁判模型不能为空")
    if not endpoint.startswith("http"):
        raise HTTPException(400, "API 端点必须以 http 开头")
    if not os.path.exists(data):
        raise HTTPException(400, f"数据集不存在: {data}")
    if max_tokens_cap < max_tokens:
        max_tokens_cap = max_tokens
    cfg = {
        "endpoint": endpoint.strip(), "api_key": api_key.strip(),
        "model": model.strip(), "judge_model": judge_model.strip(),
        "data": data.strip(), "concurrency": max(1, concurrency),
        "judge_concurrency": max(1, judge_concurrency),
        "limit": max(0, limit), "seed": seed,
        "max_tokens": max_tokens, "max_tokens_cap": max_tokens_cap,
        "max_incomplete_attempts": max(1, max_incomplete_attempts),
    }
    run_id = manager.start(cfg)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}")
def run_page(request: Request, run_id: str):
    snap = manager.get(run_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    items = manager.read_items(run_id, limit=300)
    for it in items:
        pred = it.get("prediction") or ""
        it["prediction_short"] = pred[:300]
    snap["status_text"] = STATUS_TEXT.get(snap.get("status"), snap.get("status"))
    return templates.TemplateResponse(request, "run.html", {
        "run": snap, "items": items,
        "total_items": len(manager.read_items(run_id)) if items else 0,
    })


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


@app.get("/runs/{run_id}/download.csv")
def download_csv(run_id: str):
    if not manager.get(run_id):
        raise HTTPException(404, "任务不存在")
    path, n = manager.export_csv(run_id)
    if not path:
        raise HTTPException(404, "暂无可导出的结果")
    return FileResponse(path, media_type="text/csv",
                        filename=f"simpleqa_{run_id}.csv")


@app.get("/runs/{run_id}/download.json")
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
