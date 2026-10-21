"""Web 评测任务管理：状态跟踪、持久化、CSV 导出。

每个任务落盘于 runs/<run_id>/，服务重启后历史任务仍可查看与下载。

同一套状态机被两类任务复用（构造时用 kind/worker/csv_columns 区分）：
  accuracy  单轮作答 + 裁判判分（eval_core.run_eval），进度看 gen/judge；
  agent     Agent 评测（agent_core.run_agent_eval：pier + docker 逐任务沙箱判定），
            进度看 agent。
"""
import calendar
import collections
import csv
import json
import os
import re
import shutil
import threading
import time

import eval_core
import runtime_paths
import task_queue
import utils

# 默认落在数据根下（开发态为项目根，打包后为可执行文件旁），避免依赖启动时的 CWD
RUNS_DIR = os.environ.get("RUNS_DIR") or os.path.join(runtime_paths.app_root(), "runs")

# run_id 直接参与路径拼接（删除时会执行 rmtree），必须拦截 ".." 等越权名称
_RUN_ID_RE = re.compile(r"^(?!.*\.\.)[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")

# 展示时区与时间格式化统一在 utils（原先与 eval_core 各写一份）
TIME_FMT = utils.TIME_FMT
TZ_NAME = utils.tz_name()
DISPLAY_TZ = utils.display_tz(TZ_NAME)


def _fmt(ts, fmt=TIME_FMT):
    """按展示时区格式化时间戳；ts 为 None 时返回 None。"""
    return utils.format_ts(ts, fmt, DISPLAY_TZ)


def _now():
    return _fmt(time.time())


def new_run_id():
    return _fmt(time.time(), "%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


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

# Agent 评测（kind=agent）的逐任务结果表：一个任务一行
AGENT_CSV_COLUMNS = [
    ("original_index", "序号"),
    ("task", "任务"),
    ("title", "标题"),
    ("category", "类别"),
    ("language", "语言"),
    ("status", "结果"),
    ("failure_kind_text", "失败原因"),
    ("failure_type", "异常类型"),
    ("reward", "reward"),
    ("steps", "步数"),
    ("input_tokens", "输入token"),
    ("output_tokens", "输出token"),
    ("cache_tokens", "缓存token"),
    ("elapsed_s", "耗时s"),
    ("error", "异常"),
    ("verifier_tail", "判定输出"),
    ("trial_dir", "产物目录"),
]


class RunState:
    """单个评测任务的内存状态（线程安全）。"""

    def __init__(self, run_id, cfg, out, kind="accuracy"):
        self.run_id = run_id
        self.cfg = cfg
        self.out = out
        self.kind = kind
        self.dir = os.path.dirname(out)
        self.meta_path = os.path.join(self.dir, "meta.json")
        # 可重入锁：on_event 持锁期间会调用 log()/snapshot()，普通 Lock 会导致自锁
        self.lock = threading.RLock()
        # queued（排队中）| running | succeeded | failed | stopped | interrupted
        self.status = "running"
        self.phase = "init"          # init|load|gen|judge|agent|done
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        # 删除标记：置位后 save() 不再落盘，避免已删除的目录被重新写回
        self.deleted = False
        # load 在 load_start 事件中才赋值，此处提前初始化，避免早期失败的任务导致快照抛错
        self.load = {"loaded": 0, "total": 0, "images": 0}
        self.gen = {"done": 0, "total": 0, "errors": 0, "tokens": 0,
                    "elapsed_s": 0.0, "tps": 0.0, "cached_valid": 0,
                    "cached_incomplete": 0, "stuck": 0, "todo": 0,
                    "max_tokens": 0}
        self.judge = {"done": 0, "total": 0, "errors": 0, "elapsed_s": 0.0,
                      "cached": 0}
        # 终端 Agent 评测进度（kind=terminal 时使用；其余任务类型保持全 0）
        self.agent = {"done": 0, "total": 0, "errors": 0, "resolved": 0,
                      "excluded": 0, "steps": 0, "tokens": 0, "elapsed_s": 0.0,
                      "tps": 0.0, "cached": 0, "todo": 0, "max_steps": 0,
                      "max_tokens": 0}
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
                if self.kind == "agent":
                    self.log("[加载] 完成 {} 个任务，跳过 {} 个".format(
                        p.get("rows", 0), p.get("skipped", 0)))
                else:
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
                self.log("[gen] 完成 {} 题，失败 {}，耗时 {:.1f} 分钟，单请求 {:.1f} tps{}"
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
            elif kind == "task_start":
                # 容器类任务带镜像信息；Agent 评测的任务由 pier 自行准备环境
                self.log("[任务] #{} {} 启动{}".format(
                    p.get("index"), p.get("task_id"),
                    "，镜像 " + str(p["image"]) if p.get("image") else ""))
            elif kind == "task_log":
                self.log(p.get("msg", ""))
            elif kind == "step":
                if p.get("command"):
                    self.log("[步骤] #{i} 第 {n} 步 退出码 {c} 耗时 {s}s $ {cmd}"
                             .format(i=p.get("index"), n=p.get("step"),
                                     c=p.get("exit_code"), s=p.get("elapsed_s", 0),
                                     cmd=p["command"]))
                else:
                    self.log("[步骤] #{i} 第 {n} 步 模型未给出命令：{a}".format(
                        i=p.get("index"), n=p.get("step"),
                        a=(p.get("assistant") or "").replace("\n", " ")[:200]))
            elif kind == "task_done":
                # elapsed_s 可能缺失（例如容器异常退出），此时不硬凑数字
                elapsed = p.get("elapsed_s")
                self.log("[任务] #{} {} {}，步数 {}，耗时 {}{}".format(
                    p.get("index", "-"), p.get("task_id"),
                    {"resolved": "通过", "unresolved": "未通过",
                     "error": "异常"}.get(p.get("status"), p.get("status")),
                    p.get("steps", 0),
                    "{:.1f}s".format(elapsed) if elapsed else "—",
                    ("，错误：" + str(p.get("error"))[:200])
                    if p.get("error") else ""))
            elif kind == "agent_start":
                self.phase = "agent"
                self.agent.update({"total": p["total"], "cached": p["cached"],
                                   "todo": p["todo"], "max_steps": 0,
                                   "max_tokens": 0})
                self.log("[agent] 任务集 {} 总任务 {} 待跑 {} 并发 {} agent {} 模型 {}".format(
                    p.get("dataset", "-"), p["total"], p["todo"],
                    p.get("concurrency"), p.get("agent", "-"), p.get("model", "-")))
            elif kind == "agent_progress":
                self.agent.update(p)
            elif kind == "agent_end":
                self.agent.update({"done": p["done"], "errors": p["errors"],
                                   "resolved": p["resolved"],
                                   "excluded": p.get("excluded", 0),
                                   "elapsed_s": p["elapsed_s"]})
                excluded = p.get("excluded") or 0
                self.log("[agent] 完成 {} 个任务，通过 {}，异常 {}，耗时 {:.1f} 分钟{}{}"
                         .format(p["done"], p["resolved"], p["errors"],
                                 p["elapsed_s"] / 60,
                                 "，其中环境/超时 {} 个不计入通过率".format(excluded)
                                 if excluded else "",
                                 "（已停止）" if p.get("stopped") else ""))
            elif kind == "item_failed":
                self.log("[{}-失败] 第 {} 题：{}".format(
                    p["stage"], p["index"], p["error"]))
            elif kind == "error":
                self.error = p["message"]
                self.log("[错误] " + p["message"])
            elif kind == "summary":
                self.summary = p["summary"]
                self.phase = "done"
        # 高频事件只更新内存进度，不落盘（否则每题一次 fsync 会拖慢评测）
        if kind in ("gen_progress", "judge_progress", "agent_progress", "step"):
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
                "started_at_str": _fmt(self.started_at),
                "finished_at_str": _fmt(self.finished_at),
                # 排队中尚未开始执行，耗时不进入统计（前端显示为 —）
                "duration_s": 0.0 if self.status == "queued" else round(
                    (self.finished_at or time.time()) - self.started_at, 1),
                "kind": self.kind,
                "load": dict(self.load),
                "gen": dict(self.gen),
                "judge": dict(self.judge),
                "agent": dict(self.agent),
                "summary": self.summary,
                "progress": self._progress(),
            }
            if include_logs:
                snap["logs"] = list(self.logs)[-120:]
            return snap

    def _progress(self):
        """归一化总体进度百分比与阶段描述。"""
        if self.status == "queued":
            return {"stage": "排队中", "pct": 0.0, "text": "等待前序任务完成"}
        if self.phase == "agent" and self.agent["todo"]:
            return {"stage": "容器执行", "pct": round(
                100 * self.agent["done"] / self.agent["todo"], 1),
                "text": "{}/{} 个任务".format(self.agent["done"],
                                              self.agent["todo"])}
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
        """将快照写入 meta.json；任务已删除时直接跳过。

        写盘全程持锁，与 delete() 的删除操作互斥：否则删除瞬间仍在执行的
        save() 会把 meta.json 重新写回，任务会再次出现在列表中。
        """
        try:
            with self.lock:
                if self.deleted:
                    return
                os.makedirs(self.dir, exist_ok=True)
                tmp = self.meta_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.snapshot(include_logs=True), f,
                              ensure_ascii=False, indent=2)
                os.replace(tmp, self.meta_path)
        except Exception:  # noqa: BLE001
            pass


class RunManager:
    def __init__(self, runs_dir=RUNS_DIR, kind="accuracy", worker=None,
                 csv_columns=None):
        """kind 决定队列里的任务名与日志措辞；worker 缺省时走精度测试的评测核心。"""
        self.runs_dir = runs_dir
        self.kind = kind
        self.worker = worker
        self.csv_columns = csv_columns or CSV_COLUMNS
        self._states = {}
        self._lock = threading.Lock()
        os.makedirs(runs_dir, exist_ok=True)
        # 上次服务退出时排在队列里的任务无法恢复执行，收敛为「已中断」
        task_queue.mark_stale_queued(runs_dir)

    # -------------------------------------------------- 启动/停止
    def start(self, cfg):
        """创建任务并加入全局队列，返回 run_id（轮到本任务时才开始跑）。"""
        run_id = new_run_id()
        out = os.path.join(self.runs_dir, run_id, "out")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        safe_cfg = dict(cfg)
        for k in ("api_key", "judge_api_key"):
            if safe_cfg.get(k):
                safe_cfg[k] = _mask(safe_cfg[k])
        state = RunState(run_id, safe_cfg, out, kind=self.kind)
        state.status = "queued"
        with self._lock:
            self._states[run_id] = state
        state.log("[排队] 已加入全局测试队列，等待前序任务完成")
        state.save()
        task_queue.QUEUE.submit(self.kind, run_id,
                                lambda: self._run_queued(state, cfg))
        return run_id

    def _run_queued(self, state, cfg):
        """队列轮到本任务：正式开跑并阻塞到任务完全结束（供队列串行调度）。"""
        if state.deleted:
            return
        if state.stop_event.is_set():
            state.status = "stopped"
            state.error = "排队期间已被取消"
            state.finished_at = time.time()
            state.save()
            return
        state.status = "running"
        state.started_at = time.time()       # 时长只统计真正执行的时间
        if self.kind == "agent":
            state.log("[开始] 轮到本任务，agent {} 模型 {} 端点 {} 并发 {} 任务数 {}".format(
                state.cfg.get("agent"), state.cfg.get("model"),
                state.cfg.get("endpoint"), state.cfg.get("concurrency"),
                state.cfg.get("limit")))
        else:
            state.log("[开始] 轮到本任务，模型 {} 端点 {} 裁判模型 {} 裁判端点 {}"
                      .format(state.cfg.get("model"), state.cfg.get("endpoint"),
                              state.cfg.get("judge_model"),
                              state.cfg.get("judge_endpoint")
                              or state.cfg.get("endpoint")))
        state.save()
        self._worker(state, cfg)

    def _worker(self, state, cfg):
        cfg = dict(cfg)
        cfg["out"] = state.out
        run = self.worker or eval_core.run_eval
        try:
            summary = run(
                cfg, on_event=state.on_event,
                should_stop=state.stop_event.is_set,
            )
            if state.stop_event.is_set():
                # 停止优先于「无结果」：停止可能发生在还没有任何结果的阶段（如镜像拉取），
                # 此时不返回 summary，若先判 None 会把「用户停止」误记成「任务失败」。
                state.status = "stopped"
                if summary is None:
                    state.error = "任务在产出结果前被手动停止"
            elif summary is None:
                state.status = "failed"
                state.error = "没有可用结果可汇总"
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
        if st.status == "queued":
            # 排队中的任务尚未开跑：直接从队列摘除并收敛为「已停止」
            task_queue.QUEUE.cancel(run_id)
            st.stop_event.set()
            st.status = "stopped"
            st.error = "排队期间被手动取消"
            st.finished_at = time.time()
            st.log("[停止] 排队中的任务已取消")
            st.save()
            return True
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
                return _load_snapshot(path)
            except Exception:  # noqa: BLE001
                return None
        return None

    def get_state(self, run_id):
        return self._states.get(run_id)

    def list_runs(self):
        """合并内存中的任务与磁盘上的历史任务，按开始时间倒序排列。"""
        items = {}
        with self._lock:
            for rid, st in self._states.items():
                if st.deleted:
                    continue
                items[rid] = st.snapshot(include_logs=False)
        if os.path.isdir(self.runs_dir):
            for rid in os.listdir(self.runs_dir):
                if rid in items:
                    continue
                path = os.path.join(self.runs_dir, rid, "meta.json")
                if os.path.exists(path):
                    try:
                        items[rid] = _load_snapshot(path)
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

    def export_csv(self, run_id, dest=None, rows=None):
        """导出逐任务 CSV；rows 可由调用方传入（用于补齐旧记录缺的字段）。"""
        rows = self.read_items(run_id) if rows is None else rows
        if not rows:
            return None, 0
        dest = dest or os.path.join(self.out_base(run_id) + ".csv")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        columns = self.csv_columns or CSV_COLUMNS
        with open(dest, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([c[1] for c in columns])
            for r in rows:
                w.writerow([_cell(r.get(k)) for k, _ in columns])
        return dest, len(rows)

    def delete(self, run_id):
        """删除任务目录并移出内存；返回是否确实删除了内容。

        运行中的任务被删除后 worker 仍在运行，因此这里同时置位 deleted 标记并触发
        stop_event：既使 save() 不再写回 meta.json，也让它尽快停止。
        """
        if not _RUN_ID_RE.match(run_id or ""):
            return False
        with self._lock:
            st = self._states.pop(run_id, None)
        target = os.path.join(self.runs_dir, run_id)
        root = os.path.realpath(self.runs_dir)
        if os.path.dirname(os.path.realpath(target)) != root:
            return False                    # 兜底：删除目标必须落在 runs 目录内
        if st is None:
            existed = os.path.isdir(target)
            shutil.rmtree(target, ignore_errors=True)
            return existed
        task_queue.QUEUE.cancel(run_id)     # 排队中的任务直接摘除，不再等待执行
        with st.lock:                       # 与 save() 互斥，避免任务被重新写回
            st.deleted = True
            st.stop_event.set()
            shutil.rmtree(target, ignore_errors=True)
        st.log("[删除] 任务已删除，结果文件已清理")
        return True


def _legacy_offset(snap):
    """从旧快照反推写入进程相对 UTC 的偏移（秒）。

    旧版本使用写入进程的系统时区（多为 UTC）渲染 started_at_str，
    与同一文件中的 started_at（epoch）相比即可还原当时的偏移，用于平移日志时间。
    """
    try:
        written = calendar.timegm(time.strptime(snap["started_at_str"], TIME_FMT))
        return written - float(snap["started_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0


def _restamp(snap):
    """将磁盘快照中的时间字符串改为按展示时区渲染。

    起止时间直接按 epoch 重算；日志中只有字符串（写入进程的本地时间），
    先用 _legacy_offset 推导偏移并还原为 epoch，再按展示时区渲染。
    """
    legacy = _legacy_offset(snap)
    if snap.get("started_at"):
        snap["started_at_str"] = _fmt(snap["started_at"])
    if snap.get("finished_at"):
        snap["finished_at_str"] = _fmt(snap["finished_at"])
    if legacy and isinstance(snap.get("logs"), list):
        for entry in snap["logs"]:
            try:
                naive = time.strptime(entry["t"], TIME_FMT)
            except (KeyError, TypeError, ValueError):
                continue
            entry["t"] = _fmt(calendar.timegm(naive) - legacy)
    return snap


def _load_snapshot(path):
    """读取磁盘快照：修正展示时区，再补充「已中断」标记。"""
    with open(path) as f:
        return _mark_interrupted(_restamp(json.load(f)), path)


def _mark_interrupted(snap, path=None):
    """磁盘快照仍为 running，说明该任务所属进程已退出，标记为「已中断」。

    真实的中断时刻无法得知，使用 meta.json 的最后写入时间（即最后一次落盘）近似，
    这样任务列表反复刷新时耗时不会持续增长。仅在 status 为 running 时改写，
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
    snap["finished_at_str"] = _fmt(ended)
    if ended:
        snap["duration_s"] = round(ended - started, 1)
    if isinstance(snap.get("logs"), list):
        snap["logs"].append({
            "t": _now(),
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


# 压测任务管理复用同一套命名/时间/脱敏/快照恢复逻辑，此处提供公开别名
RUN_ID_RE = _RUN_ID_RE
fmt_time = _fmt
mask_secret = _mask
mark_interrupted = _mark_interrupted
load_snapshot = _load_snapshot
