"""llm-grill 压测任务管理：状态跟踪、实时进度、持久化、结果导出。

任务目录 runs/stress/<run_id>/：
  meta.json      任务快照（状态 / 实时指标 / 汇总 / 日志），api_key 已脱敏
  scenario.yaml  交给 llm-grill 的场景文件（含密钥，权限 600）
  results.jsonl  llm-grill 的逐请求结果，边跑边落盘，可 tail 出实时进度

与精度测试的 RunManager 保持同一套约定（快照字段、中断标记、删除语义），
前端两个 Tab 因此能复用同一批组件。
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque

import grill_core
import run_manager as rm

STRESS_SUBDIR = "stress"
LAST_CFG = ".last_stress_config.json"
MAX_LOGS = 200
MAX_RECORDS = 200000            # 内存里保留的明细上限，避免超长压测把内存吃满
STATUS_TEXT = {"running": "压测中", "succeeded": "已完成", "failed": "失败",
               "stopped": "已停止", "interrupted": "已中断"}


class StressRun:
    """一次压测任务的全部状态（内存 + 磁盘）。"""

    def __init__(self, run_id, cfg, base_dir):
        self.run_id = run_id
        self.cfg = dict(cfg)
        self.dir = os.path.join(base_dir, run_id)
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.scenario_path = os.path.join(self.dir, "scenario.yaml")
        self.result_path = os.path.join(self.dir, "results.jsonl")
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
        self.progress = {"stage": "准备中", "text": "正在生成场景文件", "pct": 0.0}
        self.records = []
        self.stats = grill_core.aggregate([])
        self.summary = None
        self.conv = None
        self.by_level = []
        self.scenario_text = None
        self.expected = grill_core.expected_requests(cfg)
        self._last_agg = 0.0
        self._last_save = 0.0

    # ---------------------------------------------------------------- 基础
    def log(self, msg):
        with self.lock:
            self.logs.append({"t": rm.fmt_time(time.time()),
                              "msg": "{}".format(msg)[:600]})

    def _safe_cfg(self):
        """快照里的配置：密钥脱敏（原文只留在 scenario.yaml 与 last_config）。"""
        safe = dict(self.cfg)
        if safe.get("api_key"):
            safe["api_key"] = rm.mask_secret(safe["api_key"])
        safe.pop("ramp_levels_text", None)
        return safe

    def save(self):
        """落盘 meta.json；已删除的任务直接跳过，避免把目录写回来。"""
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
                "scenario_text": self.scenario_text,
                "result_exists": os.path.isfile(self.result_path),
            }
            if include_logs:
                snap["logs"] = list(self.logs)
            return snap

    # ---------------------------------------------------------------- 进度
    def _recompute(self, force=False):
        """按已完成请求刷新实时指标与进度（节流，避免大任务反复全量聚合）。"""
        now = time.time()
        if not force and now - self._last_agg < 0.5:
            return False
        self._last_agg = now
        self.stats = grill_core.aggregate(self.records)
        self.by_level = grill_core.by_level(self.records)
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

    def _read_new(self, pos):
        """从 results.jsonl 的 pos 处读增量（只消费完整行），返回新位置。"""
        if not os.path.isfile(self.result_path):
            return pos
        try:
            with open(self.result_path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            return pos
        if not chunk:
            return pos
        lines = chunk.split(b"\n")
        tail = lines.pop()                       # 最后一段可能还没写完
        consumed = len(chunk) - len(tail)
        parsed = [rec for rec in (grill_core.parse_line(x) for x in lines) if rec]
        if parsed:
            with self.lock:
                if len(self.records) < MAX_RECORDS:
                    self.records.extend(parsed)
            if self._recompute():
                self._maybe_save()
        return pos + consumed

    def _pump_stdout(self, proc):
        """把 llm-grill 的输出转进任务日志。"""
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self.log("[llm-grill] " + line[:400])
        except Exception:  # noqa: BLE001
            pass

    def _terminate(self, proc):
        """终止 llm-grill 子进程（含其进程组）。"""
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
        """后台线程：写场景 -> 起 llm-grill -> 边跑边读结果 -> 收尾定状态。"""
        rc = None
        error = None
        try:
            scenario = grill_core.build_scenario(self.cfg)
            text = grill_core.dump_scenario(scenario)
            os.makedirs(self.dir, exist_ok=True)
            with open(self.scenario_path, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(self.scenario_path, 0o600)
            self.scenario_text = text
            self.save()

            binary = grill_core.resolve_bin()
            if not binary:
                raise RuntimeError(grill_core.probe()["error"] or "未找到 llm-grill 可执行文件")
            cmd = [binary, "run", self.scenario_path, "-o", self.result_path, "-q"]
            self.log("[启动] " + " ".join(cmd))
            proc = subprocess.Popen(cmd, cwd=self.dir, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    start_new_session=True)
            self.proc = proc
            threading.Thread(target=self._pump_stdout, args=(proc,), daemon=True).start()
            self.phase = "run"
            self.progress = {"stage": "压测中",
                             "text": "0/{} 请求".format(self.expected), "pct": 0.0}
            self.save()

            pos = 0
            while True:
                pos = self._read_new(pos)
                if proc.poll() is not None:
                    self._read_new(pos)
                    break
                if self.stop_event.is_set():
                    self.log("[停止] 收到停止指令，正在终止 llm-grill")
                    self._terminate(proc)
                    break
                time.sleep(0.3)
            rc = proc.wait()
        except Exception as exc:  # noqa: BLE001
            error = "{}: {}".format(type(exc).__name__, exc)
            self.log("[异常] " + error)
        finally:
            self._finalize(rc, error)

    def _finalize(self, rc, error):
        """定状态、出汇总；即使失败也保留已完成的部分结果。"""
        with self.lock:
            self.summary = grill_core.aggregate(self.records)
            self.conv = grill_core.conversation_metrics(self.records)
            self.by_level = grill_core.by_level(self.records)
            if error:
                self.status, self.error = "failed", error
            elif self.stop_event.is_set():
                self.status = "stopped"
                self.error = "已手动停止"
            elif rc not in (0, None):
                self.status = "failed"
                self.error = "llm-grill 退出码 {}，请查看日志".format(rc)
            elif not self.records:
                self.status = "failed"
                self.error = "llm-grill 未产出任何请求结果，请检查场景与端点"
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
        grill_core.build_scenario(cfg)              # 校验失败 -> ValueError -> 400
        info = grill_core.probe()
        if not info["available"]:
            raise RuntimeError(info["error"] or "llm-grill 不可用")
        run_id = rm.new_run_id()
        cfg["run_id"] = run_id              # 让 llm-grill 结果里带上本平台的任务号
        state = StressRun(run_id, cfg, self.base)
        state.log("[准备] 正在生成 llm-grill 场景文件")
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
        """内存中的任务 + 磁盘上的历史任务，按开始时间倒序。"""
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
        """列表用精简快照：去掉大字段。"""
        for key in ("scenario_text", "logs", "by_level", "conv"):
            snap.pop(key, None)
        return snap

    def records(self, run_id):
        """读磁盘上的逐请求结果（运行中也安全，llm-grill 每条即 flush）。"""
        if not rm.RUN_ID_RE.match(run_id or ""):
            return []
        return grill_core.load_records(os.path.join(self.base, run_id, "results.jsonl"))

    def scenario_path(self, run_id):
        if not rm.RUN_ID_RE.match(run_id or ""):
            return None
        path = os.path.join(self.base, run_id, "scenario.yaml")
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
        """删除任务：先停子进程，再连同目录一起清掉。"""
        if not rm.RUN_ID_RE.match(run_id or ""):
            return False                     # 名字不合法就不碰文件系统
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
        """服务退出时终止所有在跑的压测（llm-grill 是独立进程，必须显式收尾）。"""
        with self._lock:
            states = list(self._states.values())
        for state in states:
            if state.status == "running":
                state.stop_event.set()
                state._terminate(state.proc)
                state.save()
