"""EvalScope 压测任务管理：状态跟踪、实时进度、持久化、结果导出。

任务目录 runs/stress/<run_id>/：
  meta.json     任务快照（状态 / 实时指标 / 汇总 / 日志），api_key 已脱敏
  dataset.jsonl 交给 evalscope 的自定义会话数据集（每行一条会话 messages）
  prompts.txt   长序列 + 固定前缀内容模式下的逐行提问（line_by_line 数据集）
  prefix.txt    长序列 + 固定前缀内容模式下的固定前缀正文
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
MAX_LOGS = 200
MAX_RECORDS = 200000            # 内存中保留的明细上限，防止超长压测占满内存
STATUS_TEXT = {"running": "压测中", "succeeded": "已完成", "failed": "失败",
               "stopped": "已停止", "interrupted": "已中断"}
MASK = "***"
PROC_NAME = "proc.json"          # 运行中的进程组信息，后端重启后据此接管残留进程
TERM_STEPS = ((signal.SIGTERM, 2.0), (signal.SIGKILL, 3.0))
POLL_STEP = 0.1


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


def _proc_cmdline(pid):
    """读取进程命令行：用于确认待终止的进程确实属于本任务（防进程组号被复用）。"""
    try:
        with open("/proc/{}/cmdline".format(int(pid)), "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    return raw.replace("\0", " ").strip()


def _pid_alive(pid):
    """进程是否存在。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                     # 权限不足等：视为存活，避免漏杀
        return True
    return True


def _group_pids(pgid):
    """扫描 /proc，返回进程组内仍在运行的进程号（僵尸进程不算存活）。

    不能直接用 os.killpg(pgid, 0) 判断：已退出但尚未回收的僵尸仍会让它成功，
    导致终止流程误判为"没杀掉"而白等到超时。
    """
    pids = []
    if not pgid or pgid <= 1:
        return pids
    try:
        names = os.listdir("/proc")
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/{}/stat".format(name), "rb") as f:
                data = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        try:
            tail = data.rsplit(") ", 1)[1].split()
            state, pgrp = tail[0], int(tail[2])
        except (IndexError, ValueError):
            continue
        if pgrp == pgid and state not in ("Z", "X", "x"):
            pids.append(int(name))
    return pids


def _group_alive(pgid):
    """进程组内是否仍有存活进程。"""
    return bool(_group_pids(pgid))


def kill_group(pgid, hint=""):
    """终止整个进程组：SIGTERM → 等待 → SIGKILL → 等待，返回是否已确认全部退出。

    evalscope 以独立会话（start_new_session）启动，因此组号即其主进程号；
    hint 取任务目录名（evalscope 命令行里必然包含它），只有当 /proc/<pgid>/cmdline
    命中时才动手，避免组号被复用后误杀无关进程。
    """
    if not pgid or pgid <= 1:
        return True
    cmdline = _proc_cmdline(pgid)
    if cmdline and hint and hint not in cmdline:
        return True                     # 组号已被别的进程占用，不能杀
    for sig, wait in TERM_STEPS:
        if not _group_alive(pgid):
            return True
        try:
            os.killpg(pgid, sig)
        except OSError:
            return not _group_alive(pgid)
        deadline = time.time() + wait
        while time.time() < deadline:
            if not _group_alive(pgid):
                return True
            time.sleep(POLL_STEP)
    return not _group_alive(pgid)


def proc_path(run_dir):
    return os.path.join(run_dir, PROC_NAME)


def load_proc(run_dir):
    """读取任务落盘的进程组信息；不可用时返回 None。"""
    try:
        with open(proc_path(run_dir), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    for key in ("pgid", "pid", "server_pid"):
        try:
            info[key] = int(info.get(key) or 0)
        except (TypeError, ValueError):
            return None
    return info


def save_proc(run_dir, info):
    try:
        os.makedirs(run_dir, exist_ok=True)
        tmp = proc_path(run_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, proc_path(run_dir))
    except OSError:
        pass


def remove_proc(run_dir):
    try:
        os.remove(proc_path(run_dir))
    except OSError:
        pass


def patch_meta(run_dir, status, error):
    """把磁盘快照收敛到终态（服务重启后接管残留任务时使用）。"""
    path = os.path.join(run_dir, "meta.json")
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return False
    if snap.get("status") != "running":
        return False
    now = time.time()
    total = (snap.get("stats") or {}).get("total_requests") or 0
    snap["status"] = status
    snap["status_text"] = STATUS_TEXT.get(status, status)
    snap["phase"] = "done"
    snap["error"] = error
    snap["finished_at"] = now
    snap["finished_at_str"] = rm.fmt_time(now)
    snap["duration_s"] = round(now - (snap.get("started_at") or now), 1)
    snap["progress"] = {"stage": STATUS_TEXT.get(status, status),
                        "text": "{} 请求完成".format(total), "pct": 100.0}
    logs = snap.get("logs")
    if isinstance(logs, list):
        logs.append({"t": rm.fmt_time(now), "msg": "[停止] " + error})
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


class StressRun:
    """一次压测任务的全部状态（内存 + 磁盘）。"""

    def __init__(self, run_id, cfg, base_dir):
        self.run_id = run_id
        self.cfg = dict(cfg)
        self.dir = os.path.join(base_dir, run_id)
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.args_path = os.path.join(self.dir, ARGS_NAME)
        # 数据集文件名随模式变化（长序列 + 固定前缀内容时是纯文本 prompts.txt）
        self.dataset_path = perf_core.dataset_file(cfg, self.dir)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.deleted = False
        self.proc = None
        self.pgid = None                 # 子进程所属进程组（独立会话，组号=子进程号）
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
        # 固定前缀内容可能是整篇文档，快照只保留长度，正文落在任务目录 prefix.txt
        content = safe.pop("prefix_content", "")
        if content:
            safe["prefix_content_chars"] = len(content)
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
                "stats_version": perf_core.STATS_VERSION,
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

    def _terminate(self, proc=None):
        """终止 evalscope 进程组，返回是否已确认全部退出。

        按组号收尾而不是看 proc.poll()：主进程退出后，组内仍可能有派生进程
        继续打流量；组号取自句柄，句柄丢失时退回落盘的 pgid。
        """
        proc = proc if proc is not None else self.proc
        pgid = proc.pid if proc is not None else self.pgid
        if not pgid:
            return True
        ok = kill_group(pgid, self.run_id)
        if proc is not None:
            try:
                proc.wait(timeout=5)     # 回收僵尸进程
            except Exception:  # noqa: BLE001
                pass
        return ok

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
            self.pgid = proc.pid         # start_new_session=True：组号即子进程号
            save_proc(self.dir, {"pid": proc.pid, "pgid": proc.pid,
                                 "server_pid": os.getpid(),
                                 "started_at": time.time()})
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
        remove_proc(self.dir)            # 进程已收尾，不再需要被接管


class StressManager:
    """压测任务池：启动、查询、停止、删除。"""

    def __init__(self, runs_dir=None):
        # 统一转绝对路径：evalscope 以任务目录为 cwd 启动，命令行里的相对路径会被
        # 该 cwd 二次拼接（曾导致 --dataset-path 找不到文件、产物多套一层同名目录）
        self.base = os.path.abspath(os.path.join(runs_dir or rm.RUNS_DIR,
                                                 STRESS_SUBDIR))
        self._states = {}
        self._threads = {}
        self._lock = threading.Lock()
        self._reap_orphans()

    # ---------------------------------------------------------------- 启动
    def start(self, cfg):
        """校验配置并后台启动一次压测，返回 run_id。"""
        cfg = dict(cfg)
        perf_core.validate(cfg)             # 校验失败时抛 ValueError，由调用方转 400
        # 预检目标可达性：地址/端口填错时立即失败，不必等 evalscope 的 connect_test 超时
        ok, why = perf_core.precheck_target(cfg.get("endpoint"))
        if not ok:
            raise perf_core.TargetUnreachable(why)
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
            snap = rm.load_snapshot(path)
        except (OSError, ValueError):
            return None
        return self._refresh_stats(run_id, snap, path)

    def _refresh_stats(self, run_id, snap, path):
        """按当前口径重算历史快照的汇总指标并回写。

        meta.json 里的 stats/summary/by_level 只是结果库的缓存，口径升级（分位算法、
        吞吐字段）后旧缓存会与 benchmark.log 对不上。这里按 `stats_version` 判断，
        落后就用结果库重算一次并写回，之后读盘不再重算；重算失败时保留原值。
        """
        if snap.get("stats_version") == perf_core.STATS_VERSION:
            return snap
        run_dir = os.path.join(self.base, run_id)
        try:
            records, _ = perf_core.read_records(run_dir, snap.get("cfg") or {}, run_id)
        except Exception:                   # noqa: BLE001 结果库损坏/权限问题不该影响读快照
            return snap
        if not records:
            return snap
        stats = perf_core.aggregate(records)
        snap["stats"] = stats
        snap["summary"] = stats
        snap["by_level"] = perf_core.by_level(records)
        snap["stats_version"] = perf_core.STATS_VERSION
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass
        return snap

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

    def run_dir(self, run_id):
        """任务目录；run_id 非法时返回 None（调用方据此返回 404）。"""
        if not rm.RUN_ID_RE.match(run_id or ""):
            return None
        return os.path.join(self.base, run_id)

    # ---------------------------------------------------------------- 控制
    def stop(self, run_id):
        """停止任务，返回是否已停止；内存句柄丢失时按落盘的进程组接管。

        服务重启后内存里不再有句柄，此时若只认内存，残留的 evalscope 进程就无人
        能停（会一直对目标服务打流量），因此这里对磁盘上的运行中任务同样接管。
        """
        if not rm.RUN_ID_RE.match(run_id or ""):
            return False
        with self._lock:
            state = self._states.get(run_id)
        if state is not None and not state.deleted:
            if state.status != "running":
                return False
            state.log("[停止] 收到停止请求")
            state.progress = {"stage": "停止中", "text": "正在终止 evalscope 进程",
                              "pct": state.progress.get("pct") or 0.0}
            state.stop_event.set()
            state.save()
            state._terminate(state.proc)
            return True
        return self._stop_from_disk(run_id)

    def _stop_from_disk(self, run_id):
        """接管磁盘上的运行中任务：终止 proc.json 中记录的进程组。"""
        run_dir = os.path.join(self.base, run_id)
        if not os.path.isfile(os.path.join(run_dir, "meta.json")):
            return False
        info = load_proc(run_dir) or {}
        pgid = info.get("pgid") or 0
        if not _group_alive(pgid):
            # 进程已不在，任务只留下了 running 快照，收敛为「已中断」
            return patch_meta(run_dir, "interrupted", "服务重启，压测进程已不存在")
        if not kill_group(pgid, run_id):
            raise RuntimeError(
                "无法终止残留的 evalscope 进程（进程组 {}），请手动 kill".format(pgid))
        remove_proc(run_dir)
        patch_meta(run_dir, "stopped", "已手动停止（服务重启后接管残留进程）")
        return True

    def _reap_orphans(self):
        """启动时收尾上次服务遗留的压测进程。

        evalscope 以独立会话启动，服务进程被强杀时会变成孤儿继续打流量，表现为
        「点了停止仍在跑」。这里按任务目录里的 proc.json 找到它们并终止，同时把
        快照收敛为「已中断」；若进程属于另一个仍在运行的后端实例，则不动。
        """
        if not os.path.isdir(self.base):
            return
        for name in os.listdir(self.base):
            if not rm.RUN_ID_RE.match(name):
                continue
            run_dir = os.path.join(self.base, name)
            info = load_proc(run_dir)
            try:
                with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as f:
                    status = (json.load(f) or {}).get("status")
            except (OSError, ValueError):
                status = None
            owner = (info or {}).get("server_pid") or 0
            if owner and owner != os.getpid() and _pid_alive(owner):
                continue                 # 属于其它仍在运行的后端实例
            pgid = (info or {}).get("pgid") or 0
            if _group_alive(pgid) and not kill_group(pgid, name):
                continue                 # 未能终止：保留 proc.json，留待下次再试
            if status == "running":
                patch_meta(run_dir, "interrupted", "服务重启导致压测中断")
            if info is not None:
                remove_proc(run_dir)

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
            if state.status != "running":
                continue
            state.stop_event.set()
            state._terminate(state.proc)
            state.save()
            # 进程已终止，磁盘快照直接收敛为「已中断」，避免遗留一份"运行中"
            patch_meta(state.dir, "interrupted", "服务退出导致压测中断")
            remove_proc(state.dir)
