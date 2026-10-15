"""FastAPI 后端：精度测试 + 压力测试的统一 JSON 接口，并托管 Vue 前端产物。

路由约定：
  /api/meta                 应用元信息（表单默认值、选项、evalscope 状态）
  /api/accuracy/*           精度测试：任务列表 / 启动 / 详情 / 逐题 / 停止 / 删除 / CSV
  /api/stress/*             压力测试：任务列表 / 启动 / 详情 / 明细 / 停止 / 删除 / CSV /
                            原始产物（benchmark.log、performance_summary.txt、perf_report.html）
  /api/csv                  工作区内 CSV 文件预览
其余路径交给 SPA（产物在 backend/webui/），未命中的静态文件回落到 index.html。
"""
import argparse
import csv
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import appconfig      # noqa: E402
import consts         # noqa: E402
import csvview        # noqa: E402
import eval_core      # noqa: E402
import perf_core      # noqa: E402
import perf_manager   # noqa: E402
import run_manager    # noqa: E402

WEBUI_DIR = os.path.join(BASE_DIR, "webui")

accuracy = run_manager.RunManager()
stress = perf_manager.StressManager()


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        # 关闭时终止所有压测子进程（替代已移除的 shutdown 事件钩子）
        stress.cancel_all()


app = FastAPI(title=appconfig.APP_TITLE, lifespan=lifespan)

ACC_STATUS_TEXT = {
    "running": "运行中", "succeeded": "已完成",
    "failed": "失败", "stopped": "已停止", "interrupted": "已中断",
}


# ---------------------------------------------------------------- 小工具
def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _valid_run_id(run_id):
    """校验 run_id：该值会参与路径拼接，需拦截 ".." 等越权名称。"""
    if not run_manager.RUN_ID_RE.match(run_id or ""):
        raise HTTPException(400, "任务 ID 不合法")
    return run_id


def _decorate(runs):
    """为精度任务列表补充前端渲染所需的派生字段。"""
    for r in runs:
        r["status_text"] = ACC_STATUS_TEXT.get(r.get("status"), r.get("status"))
        s = r.get("summary") or {}
        r["pass_at_1"] = s.get("pass_at_1")
        r["n_done"] = s.get("num_questions")
        r["expected"] = s.get("expected_questions")
        prog = r.get("progress") or {}
        r["stage"] = prog.get("stage", "")
        r["pct"] = prog.get("pct", 0.0)
    return runs


# ---------------------------------------------------------------- 元信息
@app.get("/api/meta")
def api_meta():
    datasets = eval_core.available_datasets()
    for d in datasets:
        d["is_csv"] = d["path"].lower().endswith(".csv")
    defaults = appconfig.accuracy_defaults()
    # 上次使用的路径不在内置数据集列表中时，回填到「自定义数据集路径」
    known = {d["path"] for d in datasets}
    custom_data = "" if defaults["data"] in known else defaults["data"]
    return JSONResponse({
        "title": appconfig.APP_TITLE,
        "accuracy": {
            "defaults": defaults,
            "custom_data": custom_data,
            "datasets": datasets,
            "thinking_options": appconfig.THINKING_OPTIONS,
            "effort_options": appconfig.EFFORT_OPTIONS,
            "saved_cfg_at": _mtime(os.path.join(run_manager.RUNS_DIR,
                                                appconfig.LAST_CFG_NAME)),
            "has_data": any(d["exists"] for d in datasets),
        },
        "stress": {
            "defaults": appconfig.stress_defaults(),
            "evalscope": perf_core.probe(),
        },
        "csv": {"default_limit": csvview.CSV_DEFAULT_LIMIT,
                "max_limit": csvview.CSV_MAX_LIMIT},
    })


@app.get("/api/csv")
def api_csv(path: str = "", limit: int = csvview.CSV_DEFAULT_LIMIT):
    """工作区内 CSV 预览（数据集、结果文件通用）。"""
    return JSONResponse(csvview.preview(csvview.resolve_path(path), limit,
                                        title=os.path.basename(path)))


@app.api_route("/api/csv/download", methods=["GET", "HEAD"])
def api_csv_download(path: str = ""):
    real = csvview.resolve_path(path)
    return FileResponse(real, media_type="text/csv", filename=os.path.basename(real))


# ---------------------------------------------------------------- 精度测试
def _accuracy_cfg(payload):
    """校验并归一化精度测试配置（报错文案与原 webapp 保持一致）。"""
    data = ((payload.get("data_custom") or "").strip()
            or (payload.get("data") or "").strip())
    endpoint = (payload.get("endpoint") or "").strip()
    model = (payload.get("model") or "").strip()
    judge_model = (payload.get("judge_model") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    if not model or not judge_model:
        raise HTTPException(400, "模型与裁判模型不能为空")
    if not endpoint.startswith("http"):
        raise HTTPException(400, "被测模型 API 端点必须以 http 开头")
    # 裁判端点/密钥留空时复用被测模型配置，二者可指向不同服务
    judge_endpoint_raw = (payload.get("judge_endpoint") or "").strip()
    judge_api_key_raw = (payload.get("judge_api_key") or "").strip()
    judge_endpoint = judge_endpoint_raw or endpoint
    if not judge_endpoint.startswith("http"):
        raise HTTPException(400, "裁判模型 API 端点必须以 http 开头")
    judge_api_key = judge_api_key_raw or api_key
    if not os.path.exists(data):
        raise HTTPException(400, "数据集不存在: {}".format(data))
    max_tokens = _int(payload.get("max_tokens"), consts.DEFAULT_MAX_TOKENS)
    max_tokens_cap = max(_int(payload.get("max_tokens_cap"),
                              consts.DEFAULT_MAX_TOKENS_CAP), max_tokens)
    thinking = payload.get("thinking") or ""
    reasoning_effort = payload.get("reasoning_effort") or ""
    judge_thinking = payload.get("judge_thinking") or ""
    judge_reasoning_effort = payload.get("judge_reasoning_effort") or ""
    for label, value in (("思考模式", thinking), ("裁判思考模式", judge_thinking)):
        if value not in eval_core.THINKING_CHOICES:
            raise HTTPException(400, "{}取值非法: {}".format(label, value))
    for label, value in (("思考强度", reasoning_effort),
                         ("裁判思考强度", judge_reasoning_effort)):
        if value not in eval_core.EFFORT_CHOICES:
            raise HTTPException(400, "{}取值非法: {}".format(label, value))

    cfg = {
        "endpoint": endpoint, "api_key": api_key,
        "judge_endpoint": judge_endpoint, "judge_api_key": judge_api_key,
        "model": model, "judge_model": judge_model, "data": data.strip(),
        "thinking": thinking, "reasoning_effort": reasoning_effort,
        "judge_thinking": judge_thinking,
        "judge_reasoning_effort": judge_reasoning_effort,
        "concurrency": max(1, _int(payload.get("concurrency"),
                                   consts.DEFAULT_CONCURRENCY)),
        "judge_concurrency": max(1, _int(payload.get("judge_concurrency"),
                                         consts.DEFAULT_JUDGE_CONCURRENCY)),
        "limit": max(0, _int(payload.get("limit"), consts.DEFAULT_LIMIT)),
        "seed": _int(payload.get("seed"), consts.DEFAULT_SEED),
        "max_tokens": max_tokens, "max_tokens_cap": max_tokens_cap,
        "max_incomplete_attempts": max(
            1, _int(payload.get("max_incomplete_attempts"),
                    consts.DEFAULT_MAX_INCOMPLETE_ATTEMPTS)),
    }
    # 持久化表单默认值：裁判端点/密钥按原始填写保存，以保留「留空即复用被测模型」的语义
    saved = {k: cfg[k] for k in appconfig.accuracy_defaults() if k in cfg}
    saved["judge_endpoint"] = judge_endpoint_raw
    saved["judge_api_key"] = judge_api_key_raw
    appconfig.save_last_cfg(appconfig.LAST_CFG_NAME, saved)
    return cfg


@app.get("/api/accuracy/runs")
def accuracy_runs():
    return JSONResponse({"runs": _decorate(accuracy.list_runs())})


@app.post("/api/accuracy/runs")
def accuracy_start(payload: dict):
    cfg = _accuracy_cfg(payload or {})
    return JSONResponse({"run_id": accuracy.start(cfg)})


@app.get("/api/accuracy/runs/{run_id}")
def accuracy_status(run_id: str):
    snap = accuracy.get(_valid_run_id(run_id))
    if not snap:
        raise HTTPException(404, "任务不存在")
    snap["status_text"] = ACC_STATUS_TEXT.get(snap.get("status"), snap.get("status"))
    prog = snap.get("progress") or {}
    snap["stage"] = prog.get("stage", "")
    snap["pct"] = prog.get("pct", 0.0)
    return JSONResponse(snap)


@app.get("/api/accuracy/runs/{run_id}/items")
def accuracy_items(run_id: str, limit: int = 500, grade: str = "", q: str = ""):
    rows = accuracy.read_items(_valid_run_id(run_id))
    if grade:
        rows = [r for r in rows if r.get("grade") == grade]
    if q:
        needle = q.lower()
        rows = [r for r in rows
                if needle in str(r.get("problem", "")).lower()
                or needle in str(r.get("prediction", "")).lower()]
    return JSONResponse({"total": len(rows), "items": rows[:limit]})


@app.post("/api/accuracy/runs/{run_id}/stop")
def accuracy_stop(run_id: str):
    if not accuracy.stop(_valid_run_id(run_id)):
        raise HTTPException(404, "任务不存在或未在运行")
    return JSONResponse({"ok": True})


@app.delete("/api/accuracy/runs/{run_id}")
def accuracy_delete(run_id: str):
    if not accuracy.delete(_valid_run_id(run_id)):
        raise HTTPException(404, "任务不存在")
    return JSONResponse({"ok": True, "run_id": run_id})


@app.get("/api/accuracy/runs/{run_id}/csv")
def accuracy_csv(run_id: str, limit: int = csvview.CSV_DEFAULT_LIMIT):
    """结果 CSV 预览。"""
    if not accuracy.get(_valid_run_id(run_id)):
        raise HTTPException(404, "任务不存在")
    path, _ = accuracy.export_csv(run_id)
    if not path:
        raise HTTPException(404, "该任务暂无可导出的结果（逐题记录为空）")
    return JSONResponse(csvview.preview(path, limit, title="{} 结果".format(run_id)))


@app.api_route("/api/accuracy/runs/{run_id}/download.csv", methods=["GET", "HEAD"])
def accuracy_download_csv(run_id: str):
    if not accuracy.get(_valid_run_id(run_id)):
        raise HTTPException(404, "任务不存在")
    path, _ = accuracy.export_csv(run_id)
    if not path:
        raise HTTPException(404, "暂无可导出的结果")
    return FileResponse(path, media_type="text/csv",
                        filename="simpleqa_{}.csv".format(run_id))


# ---------------------------------------------------------------- 压力测试
# 参数上限保护：防止误操作压垮目标服务，或使单轮压测长时间无法结束
STRESS_CAPS = {"max_tokens": 131072, "turns": 100, "concurrent_users": 1024,
               "numbers": 100000, "timeout": 3600, "ramp_pause_seconds": 3600,
               "level": 1024, "levels": 20, "seq_length": 2097152,
               "output_length": 131072, "prefix_length": 2097152,
               "warmup_num": 100000}


def _capped(value, key, label):
    value = _int(value, 0)
    limit = STRESS_CAPS[key]
    if value > limit:
        raise ValueError("{} 不能超过 {}".format(label, limit))
    return value


def _tokens(value, key, label, default=0):
    """解析 token 长度（支持 128K 这类写法）并套用上限保护。"""
    tokens = perf_core.parse_tokens(value, default)
    if tokens > STRESS_CAPS[key]:
        raise ValueError("{} 不能超过 {}".format(label, STRESS_CAPS[key]))
    return tokens


def _ramp_pairs(payload):
    """解析阶梯档位：--parallel 与 --number 两个列表按位置一一对应。

    档位升序去重后返回 (并发数, 请求数) 列表；只填一个请求数时视为各档相同。
    """
    level_items = appconfig.parse_int_list(payload.get("ramp_levels_text"), "并发档位")
    if not level_items:
        raise ValueError("阶梯模式至少需要一个并发档位，例如 2,4,8,16")
    number_items = appconfig.parse_int_list(payload.get("ramp_numbers_text"), "各档请求数")
    if not number_items:
        raise ValueError("阶梯模式需要各档请求数（--number），例如 6,12,24,48")
    if len(number_items) == 1:
        number_items = number_items * len(level_items)
    if len(number_items) != len(level_items):
        raise ValueError("各档请求数需与并发档位一一对应（数量一致，或只填一个值）")
    pairs = {}
    for level, number in zip(level_items, number_items):
        pairs.setdefault(level, number)          # 档位重复时取首次出现的请求数
    ordered = sorted(pairs.items())
    for level, _ in ordered:
        if level > STRESS_CAPS["level"]:
            raise ValueError("并发档位不能超过 {}".format(STRESS_CAPS["level"]))
    if len(ordered) > STRESS_CAPS["levels"]:
        raise ValueError("并发档位最多 {} 个".format(STRESS_CAPS["levels"]))
    for _, number in ordered:
        if number > STRESS_CAPS["numbers"]:
            raise ValueError("各档请求数不能超过 {}".format(STRESS_CAPS["numbers"]))
    return ordered


def _stress_cfg(payload):
    """校验并归一化压测配置，供 perf_manager 生成 evalscope 数据集与命令行。

    请求数口径与 evalscope 一致：`--parallel` 是并发数（同时发起的请求数），
    `--number` 是本次（本档）要发出的请求总数，二者不做相互换算。
    """
    mode = "ramp" if (payload.get("concurrency_mode") or "fixed") == "ramp" else "fixed"
    if mode == "ramp":
        ordered = _ramp_pairs(payload)
        levels = [level for level, _ in ordered]
        numbers = [number for _, number in ordered]
        # 阶梯模式下固定并发的输入框不参与本次压测，快照回填最大档位，
        # 避免残留的默认值（如 8）在结果页被误读成本次生效的并发
        concurrent_users = max(levels)
    else:
        levels = []
        numbers = [max(1, _capped(payload.get("numbers"), "numbers", "请求数"))]
        concurrent_users = max(
            1, _capped(payload.get("concurrent_users"), "concurrent_users", "并发数"))

    prompt_mode = "long" if (payload.get("prompt_mode") or "") == "long" else "chat"
    cfg = {
        "name": (payload.get("name") or "stress").strip() or "stress",
        "description": (payload.get("description") or "").strip(),
        "endpoint": (payload.get("endpoint") or "").strip(),
        "api_key": (payload.get("api_key") or "").strip(),
        "model": (payload.get("model") or "").strip(),
        "max_tokens": max(1, _capped(payload.get("max_tokens"), "max_tokens", "max_tokens")),
        "temperature": _float(payload.get("temperature"),
                              consts.DEFAULT_STRESS_TEMPERATURE),
        "top_p": _float(payload.get("top_p"), consts.DEFAULT_STRESS_TOP_P),
        "top_k": _int(payload.get("top_k"), consts.DEFAULT_STRESS_TOP_K),
        "timeout": max(1.0, min(_float(payload.get("timeout"),
                                      consts.DEFAULT_STRESS_TIMEOUT),
                                STRESS_CAPS["timeout"])),
        "system_prompt": (payload.get("system_prompt") or "").strip(),
        "prompt": (payload.get("prompt") or "").strip(),
        "turns": max(1, _capped(payload.get("turns"), "turns", "对话轮数")),
        "unique_prefix": bool(payload.get("unique_prefix")),
        "concurrency_mode": mode,
        "concurrent_users": concurrent_users,
        # evalscope --number：固定并发 = [请求总数]；阶梯 = 与 ramp_levels 同序的各档请求数
        "numbers": numbers,
        "ramp_levels": levels,
        "ramp_pause_seconds": max(0.0, min(
            _float(payload.get("ramp_pause_seconds"),
                   consts.DEFAULT_STRESS_RAMP_PAUSE_SECONDS),
            STRESS_CAPS["ramp_pause_seconds"])),
        # 长序列 + 固定前缀（Prefix Cache 命中率）场景
        "prompt_mode": prompt_mode,
        "tokenizer_path": (payload.get("tokenizer_path") or "").strip(),
        "seq_length": _tokens(payload.get("seq_length"), "seq_length", "序列长度",
                              consts.DEFAULT_STRESS_SEQ_LENGTH),
        "output_length": _tokens(payload.get("output_length"), "output_length",
                                 "输出长度", consts.DEFAULT_STRESS_OUTPUT_LENGTH),
        "prefix_cache_ratio": max(0.0, min(1.0, _float(
            payload.get("prefix_cache_ratio"), consts.DEFAULT_STRESS_PREFIX_CACHE_RATIO))),
        "prefix_length": _tokens(payload.get("prefix_length"), "prefix_length",
                                 "固定前缀长度", consts.DEFAULT_STRESS_PREFIX_LENGTH),
        "prefix_content": str(payload.get("prefix_content") or ""),
        "warmup_num": _capped(payload.get("warmup_num"), "warmup_num", "预热请求数"),
        "ignore_eos": bool(payload.get("ignore_eos")),
    }
    if cfg["temperature"] < 0 or cfg["temperature"] > 2:
        raise ValueError("temperature 需在 0~2 之间")
    if cfg["top_p"] <= 0 or cfg["top_p"] > 1:
        raise ValueError("top_p 需在 0~1 之间")
    perf_core.validate(cfg)             # 提问内容 / 长序列参数校验
    return cfg


def _save_stress_last_cfg(cfg, raw_payload):
    saved = {k: cfg[k] for k in appconfig.stress_defaults() if k in cfg}
    if cfg.get("concurrency_mode") == "ramp":
        # 阶梯模式下 numbers 是与档位对应的列表，表单回填用 ramp_numbers_text
        saved.pop("numbers", None)
    else:
        saved["numbers"] = cfg["numbers"][0]
    saved["ramp_levels_text"] = (raw_payload.get("ramp_levels_text") or "").strip()
    saved["ramp_numbers_text"] = (raw_payload.get("ramp_numbers_text") or "").strip()
    appconfig.save_last_cfg(appconfig.LAST_STRESS_CFG_NAME, saved)


@app.get("/api/stress/runs")
def stress_runs():
    return JSONResponse({"runs": stress.list_runs()})


@app.post("/api/stress/runs")
def stress_start(payload: dict):
    payload = payload or {}
    try:
        cfg = _stress_cfg(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        run_id = stress.start(cfg)
    except perf_core.TargetUnreachable as exc:  # 启动前预检：目标不可达
        raise HTTPException(400, str(exc))
    except ValueError as exc:            # 数据集/参数校验失败
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:          # evalscope 不可用
        raise HTTPException(503, str(exc))
    _save_stress_last_cfg(cfg, payload)
    return JSONResponse({"run_id": run_id})


@app.get("/api/stress/runs/{run_id}")
def stress_status(run_id: str):
    snap = stress.get(run_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    return JSONResponse(snap)


@app.get("/api/stress/runs/{run_id}/records")
def stress_records(run_id: str, limit: int = 200, only_errors: bool = False):
    if not stress.get(run_id):
        raise HTTPException(404, "任务不存在")
    records = stress.records(run_id)
    if only_errors:
        records = [r for r in records if not r.get("success")]
    return JSONResponse({"total": len(records), "records": records[:limit]})


@app.get("/api/stress/runs/{run_id}/series")
def stress_series(run_id: str, buckets: int = 60):
    if not stress.get(run_id):
        raise HTTPException(404, "任务不存在")
    return JSONResponse({"points": perf_core.series(stress.records(run_id), buckets)})


@app.get("/api/stress/runs/{run_id}/args")
def stress_args(run_id: str):
    """返回本次运行参数与 evalscope 命令（api_key 已脱敏；原文见任务目录 args.json）。"""
    snap = stress.get(run_id)
    if not snap:
        raise HTTPException(404, "任务不存在")
    if not snap.get("args_text"):
        raise HTTPException(404, "运行参数不存在")
    return JSONResponse({"text": snap["args_text"],
                         "path": stress.args_path(run_id) or ""})


@app.get("/api/stress/runs/{run_id}/files")
def stress_files(run_id: str):
    """列出该任务可在线查看/下载的 evalscope 原始产物。"""
    if not stress.get(run_id):
        raise HTTPException(404, "任务不存在")
    return JSONResponse({"run_id": run_id,
                         "files": perf_core.list_result_files(stress.run_dir(run_id))})


@app.api_route("/api/stress/runs/{run_id}/file", methods=["GET", "HEAD"])
def stress_file(run_id: str, path: str = "", download: bool = False):
    """查看（默认内联，日志走文本、报告走 text/html）或下载原始产物。

    路径只接受 perf_core 白名单内的相对路径，避免任意文件读取。
    """
    if not stress.get(run_id):
        raise HTTPException(404, "任务不存在")
    try:
        real = perf_core.result_file(stress.run_dir(run_id), path)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    name = os.path.basename(real)
    media_type = ("text/html; charset=utf-8" if name.endswith(".html")
                  else "text/plain; charset=utf-8")
    if download:
        return FileResponse(real, media_type=media_type,
                            filename="{}_{}".format(run_id, name))
    return FileResponse(real, media_type=media_type,
                        headers={"Content-Disposition": 'inline; filename="{}"'.format(name)})


@app.post("/api/stress/runs/{run_id}/stop")
def stress_stop(run_id: str):
    try:
        stopped = stress.stop(run_id)
    except RuntimeError as exc:          # 残留进程未能终止
        raise HTTPException(500, str(exc))
    if not stopped:
        raise HTTPException(404, "任务不存在或未在运行")
    return JSONResponse({"ok": True})


@app.delete("/api/stress/runs/{run_id}")
def stress_delete(run_id: str):
    _valid_run_id(run_id)
    if not stress.delete(run_id):
        raise HTTPException(404, "任务不存在")
    return JSONResponse({"ok": True, "run_id": run_id})


def _stress_csv_path(run_id):
    """按需把逐请求结果导出成 CSV，返回 (路径, 行数)。"""
    records = stress.records(run_id)
    if not records:
        return None, 0
    path = os.path.join(stress.base, run_id, "results.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(perf_core.CSV_HEADER)
        for rec in records:
            writer.writerow(["" if v is None else v
                             for v in perf_core.row_for_csv(rec)])
    return path, len(records)


@app.get("/api/stress/runs/{run_id}/csv")
def stress_csv(run_id: str, limit: int = csvview.CSV_DEFAULT_LIMIT):
    if not stress.get(run_id):
        raise HTTPException(404, "任务不存在")
    path, _ = _stress_csv_path(run_id)
    if not path:
        raise HTTPException(404, "该任务暂无可导出的结果（逐请求记录为空）")
    return JSONResponse(csvview.preview(path, limit, title="{} 逐请求结果".format(run_id)))


@app.api_route("/api/stress/runs/{run_id}/download.csv", methods=["GET", "HEAD"])
def stress_download_csv(run_id: str):
    if not stress.get(run_id):
        raise HTTPException(404, "任务不存在")
    path, _ = _stress_csv_path(run_id)
    if not path:
        raise HTTPException(404, "暂无可导出的结果")
    return FileResponse(path, media_type="text/csv",
                        filename="stress_{}.csv".format(run_id))


# ---------------------------------------------------------------- 前端 SPA
def _placeholder():
    """前端产物未构建时的占位页面。"""
    return HTMLResponse(
        "<meta charset='utf-8'>"
        "<h2>前端尚未构建</h2>"
        "<p>请在项目根目录执行 <code>npm --prefix frontend install</code>，"
        "再执行 <code>npm --prefix frontend run build</code>（产物输出到 "
        "<code>backend/webui/</code>），然后刷新本页。</p>"
        "<p>接口本身已可用：<a href='/docs'>/docs</a> 查看 API 文档。</p>",
        status_code=200)


if os.path.isdir(os.path.join(WEBUI_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(WEBUI_DIR, "assets")),
              name="assets")


@app.get("/{path:path}")
def spa(path: str):
    """SPA 回落：命中静态文件则直接返回，否则交由前端路由处理。"""
    if path.startswith("api/"):
        raise HTTPException(404, "接口不存在")
    if path:
        candidate = os.path.realpath(os.path.join(WEBUI_DIR, path))
        if candidate.startswith(WEBUI_DIR + os.sep) and os.path.isfile(candidate):
            return FileResponse(candidate)
    index = os.path.join(WEBUI_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return _placeholder()


# ---------------------------------------------------------------- 启动
def main():
    parser = argparse.ArgumentParser(
        description="{}：精度测试 + evalscope 压力测试 Web 服务".format(appconfig.APP_TITLE))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    import uvicorn
    print("前端产物: {} ({})".format(WEBUI_DIR,
                                  "已构建" if os.path.isfile(
                                      os.path.join(WEBUI_DIR, "index.html")) else "未构建"))
    info = perf_core.probe(force=True)
    print("evalscope: {} ({})".format(
        "可用 " + str(info["version"]) if info["available"] else "不可用",
        info["path"] or info["error"]))
    print("服务地址: http://{}:{}".format(args.host if args.host != "0.0.0.0" else "127.0.0.1",
                                       args.port))
    target = "api:app" if args.reload else app
    try:
        uvicorn.run(target, host=args.host, port=args.port, reload=args.reload)
    except KeyboardInterrupt:
        pass
    finally:
        stress.cancel_all()


if __name__ == "__main__":
    main()
