"""Web 评测任务管理：状态跟踪、持久化、CSV 导出。

每个任务在 runs/<run_id>/ 下落盘，服务重启后历史任务仍可查看/下载。
"""
import collections
import csv
import json
import os
import threading
import time

import eval_core

RUNS_DIR = os.environ.get("RUNS_DIR", "runs")

CSV_COLUMNS = [
    ("original_index", "题号"),
    ("problem", "问题"),
    ("answer", "标准答案"),
    ("prediction", "模型回答"),
    ("exact_answer", "抽取答案"),
    ("grade", "判定"),
    ("topic", "主题"),
    ("answer_type", "答案类型"),
    ("subject", "细分学科"),
    ("has_image", "含图片"),
    ("completion_tokens", "总输出token"),
    ("content_tokens", "可见token"),
    ("reasoning_tokens", "思考token"),
    ("ttft_s", "首字延迟s"),
    ("total_s", "总耗时s"),
    ("tpot_visible_s", "可见TPOTs"),
    ("finish_reason", "结束原因"),
    ("incomplete", "是否截断"),
]


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_run_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


class RunState:
    """单个评测任务的内存状态（线程安全）。"""

    def __init__(self, run_id, cfg, out):
        self.run_id = run_id
        self.cfg = cfg
        self.out = out
        self.dir = os.path.dirname(out)
        self.meta_path = os.path.join(self.dir, "meta.json")
        # 可重入锁：on_event 持锁期间会调用 log()/snapshot()，普通 Lock 会自锁死
        self.lock = threading.RLock()
        self.status = "running"      # running | succeeded | failed | stopped
        self.phase = "init"          # init|gen|judge|summary|done
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self.gen = {"done": 0, "total": 0, "errors": 0, "tokens": 0,
                    "elapsed_s": 0.0, "tps": 0.0, "cached_valid": 0,
                    "cached_incomplete": 0, "stuck": 0, "todo": 0,
                    "max_tokens": 0}
        self.judge = {"done": 0, "total": 0, "errors": 0, "elapsed_s": 0.0,
                      "cached": 0}
        self.summary = None
        self.logs = collections.deque(maxlen=400)
        self.stop_event = threading.Event()

    # -------------------------------------------------- 事件处理
    def log(self, msg):
        with self.lock:
            self.logs.append({"t": _now(), "msg": msg})

    def on_event(self, kind, p):
        with self.lock:
            if kind == "load_start":
                self.phase = "load"
                self.load = {"loaded": 0, "total": 0, "images": 0}
                self.log("[加载] 数据集 {} [{}]".format(
                    os.path.basename(p.get("path", "")), p.get("kind", "")))
            elif kind == "load_progress":
                self.load = {"loaded": p.get("loaded", 0),
                             "total": p.get("total", 0),
                             "images": p.get("images", 0)}
            elif kind == "load_end":
                self.log("[加载] 完成 {} 题，带图 {} 题".format(
                    p.get("rows", 0), p.get("images", 0)))
            elif kind == "gen_start":
                self.phase = "gen"
                self.gen.update({
                    "total": p["total"], "cached_valid": p["cached_valid"],
                    "cached_incomplete": p["cached_incomplete"],
                    "stuck": p["stuck"], "todo": p["todo"],
                    "max_tokens": p["max_tokens"],
                })
                self.log("[gen] 数据集 {} 总题数 {} 含图 {} 缓存有效 {} 缓存截断 {} "
                         "终态 {} 待跑 {} 并发 {} max_tokens {} 思考 {}".format(
                             p.get("dataset", "-"), p["total"], p.get("images", 0),
                             p["cached_valid"], p["cached_incomplete"], p["stuck"],
                             p["todo"], p["concurrency"], p["max_tokens"],
                             p.get("thinking", "-")))
            elif kind == "gen_progress":
                self.gen.update(p)
            elif kind == "truncated":
                self.log("[截断] {} 命中 {} token，扩容到 {} 重试".format(
                    p["tag"], p["hit"], p["retry"]))
            elif kind == "gen_end":
                self.gen.update({"done": p["done"], "errors": p["errors"],
                                 "elapsed_s": p["elapsed_s"], "tps": p["tps"]})
                self.log("[gen] 完成 {} 题，失败 {}，耗时 {:.1f} 分钟，{:.1f} tps{}"
                         .format(p["done"], p["errors"], p["elapsed_s"] / 60,
                                 p["tps"], "（已停止）" if p.get("stopped") else ""))
            elif kind == "gen_summary":
                self.log("[gen] 有效 {}/{} 截断 {} 失败 {}".format(
                    p["valid"], p["expected"], p["incomplete"], p["failed"]))
            elif kind == "judge_start":
                self.phase = "judge"
                self.judge.update({"total": p["todo"], "cached": p["cached"]})
                self.log("[judge] 模型 {} 端点 {} 缓存 {} 待判 {} 并发 {} 思考 {}"
                         .format(p["model"], p.get("endpoint", "-"), p["cached"],
                                 p["todo"], p["concurrency"],
                                 p.get("thinking", "-")))
            elif kind == "judge_progress":
                self.judge.update(p)
            elif kind == "judge_end":
                self.judge.update({"done": p["done"], "errors": p["errors"],
                                   "elapsed_s": p["elapsed_s"]})
                self.log("[judge] 完成 {} 题，失败 {}，耗时 {:.1f} 分钟".format(
                    p["done"], p["errors"], p["elapsed_s"] / 60))
            elif kind == "item_failed":
                self.log("[{}-失败] 第 {} 题：{}".format(
                    p["stage"], p["index"], p["error"]))
            elif kind == "error":
                self.error = p["message"]
                self.log("[错误] " + p["message"])
            elif kind == "summary":
                self.summary = p["summary"]
                self.phase = "done"
        if kind in ("gen_progress", "judge_progress"):
            return
        self.save()

    # -------------------------------------------------- 快照与持久化
    def snapshot(self, include_logs=True):
        with self.lock:
            snap = {
                "run_id": self.run_id,
                "status": self.status,
                "phase": self.phase,
                "cfg": self.cfg,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "started_at_str": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
                "finished_at_str": (
                    time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(self.finished_at))
                    if self.finished_at else None),
                "duration_s": round(
                    (self.finished_at or time.time()) - self.started_at, 1),
                "load": dict(self.load),
                "gen": dict(self.gen),
                "judge": dict(self.judge),
                "summary": self.summary,
                "progress": self._progress(),
            }
            if include_logs:
                snap["logs"] = list(self.logs)[-120:]
            return snap

    def _progress(self):
        """归一化总体进度百分比与阶段描述。"""
        if self.phase == "gen" and self.gen["todo"]:
            return {"stage": "生成", "pct": round(
                100 * self.gen["done"] / self.gen["todo"], 1),
                "text": "{}/{} 题".format(self.gen["done"], self.gen["todo"])}
        if self.phase == "judge" and self.judge["total"]:
            return {"stage": "裁判", "pct": round(
                100 * self.judge["done"] / self.judge["total"], 1),
                "text": "{}/{} 题".format(self.judge["done"], self.judge["total"])}
        if self.phase == "done":
            return {"stage": "完成", "pct": 100.0, "text": "已完成"}
        return {"stage": "准备中", "pct": 0.0, "text": ""}

    def save(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.meta_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.snapshot(include_logs=True), f,
                          ensure_ascii=False, indent=2)
            os.replace(tmp, self.meta_path)
        except Exception:  # noqa: BLE001
            pass


class RunManager:
    def __init__(self, runs_dir=RUNS_DIR):
        self.runs_dir = runs_dir
        self._states = {}
        self._lock = threading.Lock()
        os.makedirs(runs_dir, exist_ok=True)

    # -------------------------------------------------- 启动/停止
    def start(self, cfg):
        run_id = new_run_id()
        out = os.path.join(self.runs_dir, run_id, "out")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        safe_cfg = dict(cfg)
        for k in ("api_key", "judge_api_key"):
            if safe_cfg.get(k):
                safe_cfg[k] = _mask(safe_cfg[k])
        state = RunState(run_id, safe_cfg, out)
        with self._lock:
            self._states[run_id] = state
        state.log("[启动] 模型 {} 端点 {} 裁判模型 {} 裁判端点 {}".format(
            cfg.get("model"), cfg.get("endpoint"), cfg.get("judge_model"),
            cfg.get("judge_endpoint") or cfg.get("endpoint")))
        state.save()
        t = threading.Thread(target=self._worker, args=(state, cfg),
                             daemon=True, name=f"run-{run_id}")
        t.start()
        return run_id

    def _worker(self, state, cfg):
        cfg = dict(cfg)
        cfg["out"] = state.out
        try:
            summary = eval_core.run_eval(
                cfg, on_event=state.on_event,
                should_stop=state.stop_event.is_set,
            )
            if summary is None:
                state.status = "failed"
                state.error = "没有可用记录可汇总"
            elif state.stop_event.is_set():
                state.status = "stopped"
            else:
                state.status = "succeeded"
        except Exception as e:  # noqa: BLE001
            state.status = "failed"
            state.error = "{}: {}".format(type(e).__name__, str(e)[:300])
            state.log("[异常] " + state.error)
        finally:
            state.finished_at = time.time()
            state.phase = "done" if state.status == "succeeded" else state.phase
            state.log("[结束] 状态 {} 耗时 {:.1f} 分钟".format(
                state.status, (state.finished_at - state.started_at) / 60))
            state.save()

    def stop(self, run_id):
        st = self._states.get(run_id)
        if not st:
            return False
        st.stop_event.set()
        st.log("[停止] 收到停止请求，等待在飞请求返回…")
        return True

    # -------------------------------------------------- 查询
    def get(self, run_id):
        st = self._states.get(run_id)
        if st:
            return st.snapshot()
        path = os.path.join(self.runs_dir, run_id, "meta.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return _mark_interrupted(json.load(f), path)
            except Exception:  # noqa: BLE001
                return None
        return None

    def get_state(self, run_id):
        return self._states.get(run_id)

    def list_runs(self):
        """内存中的任务 + 磁盘上的历史任务，按开始时间倒序。"""
        items = {}
        with self._lock:
            for rid, st in self._states.items():
                items[rid] = st.snapshot(include_logs=False)
        if os.path.isdir(self.runs_dir):
            for rid in os.listdir(self.runs_dir):
                if rid in items:
                    continue
                path = os.path.join(self.runs_dir, rid, "meta.json")
                if os.path.exists(path):
                    try:
                        with open(path) as f:
                            items[rid] = _mark_interrupted(json.load(f), path)
                    except Exception:  # noqa: BLE001
                        continue
        out = sorted(items.values(), key=lambda x: x.get("started_at", 0),
                     reverse=True)
        return out

    # -------------------------------------------------- 结果
    def out_base(self, run_id):
        st = self._states.get(run_id)
        if st:
            return st.out
        return os.path.join(self.runs_dir, run_id, "out")

    def read_items(self, run_id, limit=None):
        path = self.out_base(run_id) + ".items.jsonl"
        rows = []
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        if limit:
            return rows[:limit]
        return rows

    def export_csv(self, run_id, dest=None):
        rows = self.read_items(run_id)
        if not rows:
            return None, 0
        dest = dest or os.path.join(self.out_base(run_id) + ".csv")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([c[1] for c in CSV_COLUMNS])
            for r in rows:
                w.writerow([_cell(r.get(k)) for k, _ in CSV_COLUMNS])
        return dest, len(rows)

    def delete(self, run_id):
        import shutil
        shutil.rmtree(os.path.join(self.runs_dir, run_id), ignore_errors=True)
        self._states.pop(run_id, None)
        return True


def _mark_interrupted(snap, path=None):
    """磁盘快照仍停留在 running，说明该任务所属进程已退出，标记为「已中断」。

    真实的中断时刻无法得知，用 meta.json 的最后写入时间（即最后一次落盘）近似，
    这样任务列表反复刷新时耗时不会一直增长。仅在 status 为 running 时改写，
    已完成/失败/已停止的任务原样返回。
    """
    if snap.get("status") != "running":
        return snap
    snap["status"] = "interrupted"
    started = snap.get("started_at") or 0
    ended = None
    if path:
        try:
            ended = os.path.getmtime(path)
        except OSError:
            ended = None
    if not ended or ended < started:
        ended = None
    snap["finished_at"] = ended
    snap["finished_at_str"] = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended))
        if ended else None)
    if ended:
        snap["duration_s"] = round(ended - started, 1)
    if isinstance(snap.get("logs"), list):
        snap["logs"].append({
            "t": time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(ended or time.time())),
            "msg": "[中断] 服务重启或进程退出，任务未能跑完",
        })
    return snap


def _cell(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if v is None:
        return ""
    return v


def _mask(key):
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return key[:6] + "***" + key[-4:]
