"""EvalScope 压测任务管理：状态跟踪、实时进度、持久化、结果导出。

任务目录 runs/stress/<run_id>/：
  meta.json     任务快照（状态 / 实时指标 / 汇总 / 日志），api_key 已脱敏
  dataset.jsonl 交给 evalscope 的自定义数据集（每行一条会话 messages）
  args.json     本次运行参数与完整命令（含密钥，权限 600，展示时已脱敏）
  perf/         evalscope 的产物目录：各并发档位下 benchmark_data.db（逐行提交，可边跑边读）

与精度测试的 RunManager 保持同一套约定（快照字段、中断标记、删除语义），
前端两个 Tab 因此可复用同一批组件。
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque

import perf_core
import run_manager as rm

STRESS_SUBDIR = "stress"
LAST_CFG = ".last_stress_config.json"
ARGS_NAME = "args.json"
DATASET_NAME = "dataset.jsonl"
MAX_LOGS = 200
MAX_RECORDS = 200000            # 内存中保留的明细上限，防止超长压测占满内存
STATUS_TEXT = {"running": "压测中", "succeeded": "已完成", "failed": "失败",
               "stopped": "已停止", "interrupted": "已中断"}
MASK = "***"


def _redact(cmd):
    """命令行中的 api_key 脱敏后再写日志。"""
    out = list(cmd)
    for i, item in enumerate(out):
        if item == "--api-key" and i + 1 < len(out):
            out[i + 1] = MASK
    return out


def _mask_text(text):
    """args.json 中的密钥脱敏（展示用）。"""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if data.get("api_key"):
        data["api_key"] = MASK
    cmd = data.get("command")
    if isinstance(cmd, list):
        data["command"] = _redact(cmd)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


class StressRun:
    """一次压测任务的全部状态（内存 + 磁盘）。"""

    def __init__(self, run_id, cfg, base_dir):
        self.run_id = run_id
        self.cfg = dict(cfg)
        self.dir = os.path.join(base_dir, run_id)
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.args_path = os.path.join(self.dir, ARGS_NAME)
        self.dataset_path = os.path.join(self.dir, DATASET_NAME)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.deleted = False
        self.proc = None
        self.status = "running"
        self.phase = "init"
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self.logs = deque(maxlen=MAX_LOGS)
        self.progress = {"stage": "准备中", "text": "正在生成数据集", "pct": 0.0}
        self.records = []
        self.stats = perf_core.aggregate([])
        self.summary = None
        self.conv = None
        self.by_level = []
        self.args_text = None
        self.expected = perf_core.expected_requests(cfg)
        self._cursor = {}                # {db_path: 已读取行数}，增量读取用
        self._last_agg = 0.0
        self._last_read = 0.0
        self._last_save = 0.0

    # ---------------------------------------------------------------- 基础
    def log(self, msg):
        with self.lock:
            self.logs.append({"t": rm.fmt_time(time.time()),
                              "msg": "{}".format(msg)[:600]})

    def _safe_cfg(self):
        """快照中的配置：密钥脱敏（原文仅保留在 args.json 与 last_config 中）。"""
        safe = dict(self.cfg)
        if safe.get("api_key"):
            safe["api_key"] = rm.mask_secret(safe["api_key"])
        safe.pop("ramp_levels_text", None)
        return safe

    def save(self):
        """落盘 meta.json；任务已删除时直接跳过，避免目录被重新写回。"""
        try:
            with self.lock:
                if self.deleted:
                    return
                os.makedirs(self.dir, exist_ok=True)
                tmp = self.meta_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.snapshot(include_logs=True), f,
                              ensure_ascii=False, indent=2)
                os.replace(tmp, self.meta_path)
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self, include_logs=True):
        with self.lock:
            snap = {
                "run_id": self.run_id,
                "kind": "stress",
                "engine": "evalscope",
                "status": self.status,
                "status_text": STATUS_TEXT.get(self.status, self.status),
                "phase": self.phase,
                "error": self.error,
                "started_at": self.started_at,
                "started_at_str": rm.fmt_time(self.started_at),
                "finished_at": self.finished_at,
                "finished_at_str": rm.fmt_time(self.finished_at),
                "duration_s": round((self.finished_at or time.time()) - self.started_at, 1),
                "cfg": self._safe_cfg(),
                "target": "{} · {}".format(self.cfg.get("endpoint", ""),
                                           self.cfg.get("model", "")),
                "progress": dict(self.progress),
                "expected": self.expected,
                "stats": self.stats,
                "summary": self.summary,
                "conv": self.conv,
                "by_level": self.by_level,
                "args_text": _mask_text(self.args_text) if self.args_text else None,
                "result_exists": os.path.isdir(self.dir),
            }
            if include_logs:
                snap["logs"] = list(self.logs)
            return snap

    # ---------------------------------------------------------------- 进度
    def _recompute(self, force=False):
        """按已完成请求刷新实时指标与进度（节流，避免大任务频繁全量聚合）。"""
        now = time.time()
        if not force and now - self._last_agg < 0.5:
            return False
        self._last_agg = now
        self.stats = perf_core.aggregate(self.records)
        self.by_level = perf_core.by_level(self.records)
        done = len(self.records)
        pct = min(100.0, done / self.expected * 100.0) if self.expected else 0.0
        level = int(self.stats.get("concurrent_level") or 0)
        if self.status == "running":
            self.phase = "run"
            text = "{}/{} 请求".format(done, self.expected)
            if level:
                text += " · 当前并发 {}".format(level)
            if self.stats["error_count"]:
                text += " · 失败 {}".format(self.stats["error_count"])
            self.progress = {"stage": "压测中", "text": text, "pct": round(pct, 1)}
        return True

    def _maybe_save(self):
        now = time.time()
        if now - self._last_save >= 0.8:
            self._last_save = now
            self.save()

    def _read_new(self, force=False):
        """从 evalscope 的 SQLite 结果库增量读取新完成的请求。"""
        now = time.time()
        if not force and now - self._last_read < 0.5:
            return
        self._last_read = now
        try:
            parsed, self._cursor = perf_core.read_records(
                self.dir, self.cfg, self.run_id, after=self._cursor)
        except Exception as exc:  # noqa: BLE001
            self.log("[读取] 结果库暂不可读：{}".format(exc))
            return
        if not parsed:
            return
        with self.lock:
            room = MAX_RECORDS - len(self.records)
            if room > 0:
                self.records.extend(parsed[:room])
        if self._recompute():
            self._maybe_save()

    def _pump_stdout(self, proc):
        """将 evalscope 的输出写入任务日志。"""
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self.log("[evalscope] " + line[:400])
        except Exception:  # noqa: BLE001
            pass

    def _terminate(self, proc):
        """终止 evalscope 子进程（含其进程组）。"""
        if proc is None or proc.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    # ---------------------------------------------------------------- 主流程
    def worker(self):
        """后台线程：生成数据集 → 启动 evalscope → 边跑边读结果 → 收尾并确定状态。"""
        rc = None
        error = None
        try:
            os.makedirs(self.dir, exist_ok=True)
            perf_core.build_dataset(self.cfg, self.dataset_path)
            text = perf_core.dump_args(self.cfg, self.dir, self.dataset_path)
            with open(self.args_path, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(self.args_path, 0o600)
            self.args_text = text
            self.save()

            cmd = perf_core.build_command(self.cfg, self.dir, self.dataset_path)
            self.log("[启动] " + " ".join(_redact(cmd)))
            proc = subprocess.Popen(cmd, cwd=self.dir, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    start_new_session=True)
            self.proc = proc
            threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True).start()
            self.phase = "run"
            self.progress = {"stage": "压测中",
                             "text": "0/{} 请求".format(self.expected), "pct": 0.0}
            self.save()

            while True:
                self._read_new()
                if proc.poll() is not None:
                    self._read_new(force=True)
                    break
                if self.stop_event.is_set():
                    self.log("[停止] 收到停止指令，正在终止 evalscope")
                    self._terminate(proc)
                    break
                time.sleep(0.5)
            rc = proc.wait()
        except Exception as exc:  # noqa: BLE001
            error = "{}: {}".format(type(exc).__name__, exc)
            self.log("[异常] " + error)
        finally:
            self._finalize(rc, error)

    def _finalize(self, rc, error):
        """确定最终状态并生成汇总；即使失败也保留已完成的部分结果。"""
        with self.lock:
            self.summary = perf_core.aggregate(self.records)
            self.conv = perf_core.conversation_metrics(self.records)
            self.by_level = perf_core.by_level(self.records)
            if error:
                self.status, self.error = "failed", error
            elif self.stop_event.is_set():
                self.status = "stopped"
                self.error = "已手动停止"
            elif rc not in (0, None):
                self.status = "failed"
                self.error = "evalscope 退出码 {}，请查看日志".format(rc)
            elif not self.records:
                self.status = "failed"
                self.error = "evalscope 未产出任何请求结果，请检查端点与模型名"
            elif self.summary["success_count"] == 0:
                self.status = "failed"
                first = (self.summary["errors"] or [{}])[0].get("message", "")
                self.error = "全部 {} 个请求失败：{}".format(
                    self.summary["total_requests"], first)
            else:
                self.status = "succeeded"
            self.phase = "done"
            self.finished_at = time.time()
            self.progress = {
                "stage": STATUS_TEXT.get(self.status, self.status),
                "text": "{}/{} 请求完成".format(len(self.records), self.expected),
                "pct": 100.0,
            }
            self.log("[完成] 状态={} 请求={} 成功={} 失败={} RPS={:.2f} P95={:.2f}s".format(
                self.status, self.summary["total_requests"], self.summary["success_count"],
                self.summary["error_count"], self.summary["requests_per_second"],
                self.summary["e2e_p95_s"]))
        self._last_save = 0.0
        self.save()


class StressManager:
    """压测任务池：启动、查询、停止、删除。"""

    def __init__(self, runs_dir=None):
        self.base = os.path.join(runs_dir or rm.RUNS_DIR, STRESS_SUBDIR)
        self._states = {}
        self._threads = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 启动
    def start(self, cfg):
        """校验配置并后台启动一次压测，返回 run_id。"""
        cfg = dict(cfg)
        perf_core.build_dataset(cfg, os.devnull)  # 校验失败时抛 ValueError，由调用方转 400
        info = perf_core.probe()
        if not info["available"]:
            raise RuntimeError(info["error"] or "evalscope 不可用")
        run_id = rm.new_run_id()
        cfg["run_id"] = run_id              # 使结果中带上本平台的任务号
        state = StressRun(run_id, cfg, self.base)
        state.log("[准备] 正在生成 evalscope 数据集")
        with self._lock:
            self._states[run_id] = state
        state.save()
        thread = threading.Thread(target=state.worker, name="stress-" + run_id, daemon=True)
        with self._lock:
            self._threads[run_id] = thread
        thread.start()
        return run_id

    # ---------------------------------------------------------------- 查询
    def get(self, run_id):
        if not rm.RUN_ID_RE.match(run_id or ""):
            return None
        with self._lock:
            state = self._states.get(run_id)
        if state is not None and not state.deleted:
            return state.snapshot()
        return self._from_disk(run_id)

    def _from_disk(self, run_id):
        path = os.path.join(self.base, run_id, "meta.json")
        if not os.path.isfile(path):
            return None
        try:
            return rm.load_snapshot(path)
        except (OSError, ValueError):
            return None

    def list_runs(self, limit=200):
        """合并内存中的任务与磁盘上的历史任务，按开始时间倒序排列。"""
        with self._lock:
            items = {rid: self._brief(st.snapshot(include_logs=False))
                     for rid, st in self._states.items() if not st.deleted}
        if os.path.isdir(self.base):
            for name in os.listdir(self.base):
                if name in items or not rm.RUN_ID_RE.match(name):
                    continue
                snap = self._from_disk(name)
                if snap is not None:
                    items[name] = self._brief(snap)
        runs = sorted(items.values(), key=lambda s: -(s.get("started_at") or 0))
        return runs[:limit]

    @staticmethod
    def _brief(snap):
        """列表使用精简快照：移除大字段。"""
        for key in ("args_text", "logs", "by_level", "conv"):
            snap.pop(key, None)
        return snap

    def records(self, run_id):
        """读取磁盘上的逐请求结果（运行中也可安全读取，evalscope 逐行提交结果库）。"""
        if not rm.RUN_ID_RE.match(run_id or ""):
            return []
        run_dir = os.path.join(self.base, run_id)
        with self._lock:
            state = self._states.get(run_id)
        cfg = state.cfg if state is not None else {}
        records, _ = perf_core.read_records(run_dir, cfg, run_id)
        return records

    def args_path(self, run_id):
        if not rm.RUN_ID_RE.match(run_id or ""):
            return None
        path = os.path.join(self.base, run_id, ARGS_NAME)
        return path if os.path.isfile(path) else None

    # ---------------------------------------------------------------- 控制
    def stop(self, run_id):
        with self._lock:
            state = self._states.get(run_id)
        if state is None or state.deleted or state.status != "running":
            return False
        state.log("[停止] 收到停止请求")
        state.stop_event.set()
        state._terminate(state.proc)
        state.save()
        return True

    def delete(self, run_id):
        """删除任务：先停止子进程，再连同目录一并清除。"""
        if not rm.RUN_ID_RE.match(run_id or ""):
            return False                     # 名称不合法时不访问文件系统
        with self._lock:
            state = self._states.pop(run_id, None)
        if state is not None:
            state.deleted = True
            state.stop_event.set()
            state._terminate(state.proc)
        path = os.path.join(self.base, run_id)
        existed = state is not None or os.path.isdir(path)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        with self._lock:
            self._threads.pop(run_id, None)
        return existed

    def cancel_all(self):
        """服务退出时终止所有运行中的压测（evalscope 为独立进程，必须显式收尾）。"""
        with self._lock:
            states = list(self._states.values())
        for state in states:
            if state.status == "running":
                state.stop_event.set()
                state._terminate(state.proc)
                state.save()
