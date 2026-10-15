"""全局测试任务队列：精度测试与压力测试共用一条串行队列。

并行执行测试会互相争抢被测服务的算力、连接与 KV 缓存，指标随之失真，因此所有
任务统一入队，由单一调度线程顺序执行：

- 队列空闲时立刻开跑；
- 否则等前序任务完全结束（压测子进程退出 / 精度任务写完汇总）再开跑；
- 相邻任务之间留 SETTLE_SECONDS 静默间隔，让服务释放连接、显存与缓存，
  使每次测试都从相同的初始状态起跑。

队列本身只保存内存中的调度信息；排队状态同时写入任务快照（status=queued）。
服务重启后内存队列不复存在，这些任务无法恢复执行，由 mark_stale_queued()
在启动时收敛为「已中断」。
"""
import json
import os
import threading
import time

KIND_TEXT = {"accuracy": "精度测试", "stress": "压力测试"}

# 相邻任务之间的静默间隔（秒）：给被测服务留出释放连接/显存的时间
SETTLE_SECONDS = 5.0
POLL_STEP = 0.2


class TaskQueue:
    """单 worker 的 FIFO 队列：starter 返回即视为任务完全结束。"""

    def __init__(self):
        # 可重入锁：submit()/snapshot() 内部会调用同样加锁的 position()
        self._lock = threading.RLock()
        self._pending = []
        self._running = None
        self._thread = None
        self._seq = 0

    # ---------------------------------------------------------------- 入队
    def submit(self, kind, run_id, starter):
        """入队并返回排队位置（1-based，正在运行的任务占第 1 位）。

        starter 由队列线程调用，必须阻塞到任务完全结束，队列据此串行调度。
        """
        item = {"kind": kind, "run_id": run_id, "starter": starter,
                "enqueued_at": time.time()}
        with self._lock:
            self._seq += 1
            item["seq"] = self._seq
            self._pending.append(item)
            position = self.position(run_id)
        self._ensure_worker()
        return position

    def cancel(self, run_id):
        """移出尚未开跑的任务；正在运行或已结束的任务返回 False。"""
        with self._lock:
            for index, item in enumerate(self._pending):
                if item["run_id"] == run_id:
                    del self._pending[index]
                    return True
        return False

    # ---------------------------------------------------------------- 查询
    def position(self, run_id):
        """排队位置（1-based）；未在排队（运行中/已结束）时返回 0。"""
        with self._lock:
            base = 1 if self._running else 0
            for index, item in enumerate(self._pending):
                if item["run_id"] == run_id:
                    return base + index + 1
        return 0

    def snapshot(self):
        with self._lock:
            running = dict(self._running) if self._running else None
            base = 1 if self._running else 0
            pending = [
                {"kind": item["kind"],
                 "kind_text": KIND_TEXT.get(item["kind"], item["kind"]),
                 "run_id": item["run_id"],
                 "position": base + index + 1,
                 "enqueued_at": item["enqueued_at"]}
                for index, item in enumerate(self._pending)
            ]
            return {"running": running, "pending": pending,
                    "pending_count": len(pending),
                    "settle_seconds": SETTLE_SECONDS}

    # ---------------------------------------------------------------- 调度
    def _ensure_worker(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="task-queue",
                                            daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                item = self._pending.pop(0) if self._pending else None
                if item is not None:
                    self._running = {
                        "kind": item["kind"],
                        "kind_text": KIND_TEXT.get(item["kind"], item["kind"]),
                        "run_id": item["run_id"],
                        "started_at": time.time(),
                        "enqueued_at": item["enqueued_at"],
                    }
                else:
                    self._running = None
            if item is None:
                time.sleep(POLL_STEP)
                continue
            try:
                item["starter"]()
            except Exception:  # noqa: BLE001
                # 任务自身的异常由各自的 worker 收敛为 failed，这里只保证队列继续
                pass
            finally:
                with self._lock:
                    self._running = None
            with self._lock:
                has_more = bool(self._pending)
            if has_more:
                time.sleep(SETTLE_SECONDS)      # 留出释放资源的时间再开下一个


QUEUE = TaskQueue()


def _write_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def mark_stale_queued(runs_dir):
    """把磁盘上仍为 queued 的任务收敛为 interrupted（服务重启，队列已丢失）。

    返回被收敛的任务号列表，供日志记录。
    """
    if not os.path.isdir(runs_dir):
        return []
    changed = []
    for name in os.listdir(runs_dir):
        path = os.path.join(runs_dir, name, "meta.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(snap, dict) or snap.get("status") != "queued":
            continue
        snap["status"] = "interrupted"
        snap["error"] = "服务重启，排队中的任务未被执行"
        snap["finished_at"] = snap.get("finished_at") or time.time()
        snap["progress"] = {"stage": "已中断", "text": "服务重启，排队任务未执行",
                            "pct": 0.0}
        _write_json(path, snap)
        changed.append(name)
    return changed
