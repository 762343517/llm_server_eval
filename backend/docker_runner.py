"""Docker 执行封装：Terminal-Bench 任务在容器内执行命令与验证脚本。

刻意通过 docker CLI（子进程）而不是 Python SDK 调用，与项目里压测走 evalscope CLI
的做法保持一致，避免引入额外依赖，也让「页面看到的行为」和「命令行手动复现」完全一致。

容器生命周期（每个任务一个容器）：
    1. 镜像不存在时 docker pull（走 /etc/docker/daemon.json 里的国内加速器）；
    2. docker create/run 起一个 sleep infinity 的常驻容器，绑定 cpus/memory；
    3. 把 tests/ 目录拷进容器的 /tests，并建好 /logs/verifier；
    4. 反复 docker exec 执行 agent 给出的命令；
    5. 执行 /tests/test.sh，读取 /logs/verifier/reward.txt 得到 0/1 判定；
    6. docker rm -f 清理容器。

所有对外函数在 docker 不可用/容器不存在时抛 DockerError，由上层收敛为任务失败。
"""
import logging
import os
import shutil
import subprocess
import threading
import time

logger = logging.getLogger("zhipu_eval")

# docker 可执行文件：非标准安装位置（或使用 podman 别名）时用 DOCKER_BIN 指定
DOCKER_BIN = (os.environ.get("DOCKER_BIN") or "docker").strip()

# 容器内约定的路径（与 Terminal-Bench 官方 harness 一致）
TESTS_MOUNT = "/tests"
REWARD_PATH = "/logs/verifier/reward.txt"
LOGS_DIR = "/logs/verifier"

# 单条命令的默认执行上限（秒），由上层按任务配置覆盖
DEFAULT_EXEC_TIMEOUT = 180.0
# docker pull 的上限（秒）：单个镜像可达数 GB
DEFAULT_PULL_TIMEOUT = 3600.0

_CONTAINER_PREFIX = "zpeval"
_live = set()                       # 本进程创建且尚未清理的容器名
_live_lock = threading.Lock()
_probe_cache = {"at": 0.0, "info": None}
_PROBE_TTL = 30.0


class DockerError(RuntimeError):
    """docker 不可用、容器操作失败或超时。"""


def _run(args, timeout=None, input_text=None):
    try:
        return subprocess.run([DOCKER_BIN, *args], capture_output=True, text=True,
                              timeout=timeout, input=input_text)
    except FileNotFoundError:
        raise DockerError("找不到 docker 可执行文件（可用 DOCKER_BIN 指定路径）")
    except subprocess.TimeoutExpired:
        raise DockerError("docker {} 超时（{}s）".format(" ".join(args[:2]), timeout))


def probe(force=False):
    """探测 docker 可用性（带短缓存），供页面顶部的状态提示使用。"""
    now = time.time()
    if not force and _probe_cache["info"] and now - _probe_cache["at"] < _PROBE_TTL:
        return dict(_probe_cache["info"])
    info = {"available": False, "path": shutil.which(DOCKER_BIN) or "",
            "version": "", "error": ""}
    try:
        r = _run(["version", "--format", "{{.Server.Version}}"], timeout=15)
        if r.returncode == 0:
            info["available"] = True
            info["version"] = (r.stdout or "").strip()
        else:
            info["error"] = (r.stderr or r.stdout or "").strip()[:300]
    except DockerError as exc:
        info["error"] = str(exc)[:300]
    _probe_cache.update({"at": now, "info": dict(info)})
    return info


def _check():
    info = probe()
    if not info["available"]:
        raise DockerError("docker 不可用：{}".format(
            info["error"] or "请确认服务进程有权访问 /var/run/docker.sock"))
    return info


def image_present(image):
    r = _run(["image", "inspect", image], timeout=60)
    return r.returncode == 0


def pull_image(image, timeout=DEFAULT_PULL_TIMEOUT, on_line=None):
    """拉取镜像；输出按行回调（供页面日志展示）。返回最后若干行文本。"""
    if not image:
        raise DockerError("任务未声明 docker_image")
    logger.info("[docker pull] %s", image)
    proc = subprocess.Popen([DOCKER_BIN, "pull", image], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace")
    tail, started = [], time.time()
    last_emit = 0.0
    try:
        for line in proc.stdout:
            line = (line or "").strip()
            if not line:
                continue
            tail.append(line)
            del tail[:-20]
            now = time.time()
            if on_line and now - last_emit > 2.0:      # 拉取进度行很密，限流回调
                on_line(line[:200])
                last_emit = now
            if now - started > timeout:
                proc.kill()
                raise DockerError("docker pull 超时（{}s）：{}".format(timeout, image))
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    if proc.returncode != 0:
        raise DockerError("docker pull 失败({})：{}｜{}".format(
            proc.returncode, image, (tail[-1] if tail else "")[:300]))
    return "\n".join(tail[-5:])


def ensure_image(image, timeout=DEFAULT_PULL_TIMEOUT, on_line=None):
    """确保镜像就绪：已在本地直接返回 False（未拉取），否则拉取并返回 True。"""
    _check()
    if image_present(image):
        return False
    pull_image(image, timeout=timeout, on_line=on_line)
    return True


def _register(name):
    with _live_lock:
        _live.add(name)


def _unregister(name):
    with _live_lock:
        _live.discard(name)


def live_containers():
    with _live_lock:
        return sorted(_live)


def start_container(name, image, workdir=None, cpus=None, memory=None, env=None,
                    gpus=False):
    """启动常驻容器（sleep infinity），返回容器名。

    镜像自带 ENTRYPOINT 时会吞掉 sleep infinity 导致容器立刻退出，
    此时自动改用 --entrypoint "" 重试一次。
    """
    _check()
    base = ["run", "-d", "--name", name]
    if workdir:
        base += ["-w", workdir]
    if cpus:
        base += ["--cpus", str(cpus)]
    if memory:
        base += ["--memory", str(memory)]
    for key, value in (env or {}).items():
        base += ["-e", "{}={}".format(key, value)]
    if gpus:
        base += ["--gpus", "all"]
    for extra in ([], ["--entrypoint", ""]):
        remove_container(name)              # 残留同名容器先清掉
        r = _run(base + extra + [image, "sleep", "infinity"], timeout=300)
        if r.returncode != 0:
            continue
        _register(name)
        if _wait_running(name, 5.0):
            return name
        remove_container(name)
    raise DockerError("容器启动失败：{}".format(image))


def _wait_running(name, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _run(["inspect", "-f", "{{.State.Running}}", name], timeout=30)
        if r.returncode == 0 and (r.stdout or "").strip() == "true":
            return True
        time.sleep(0.3)
    return False


def copy_in(name, src, dest):
    """把宿主机文件/目录拷进容器（容器未运行时也可以）。"""
    r = _run(["cp", src, "{}:{}".format(name, dest)], timeout=1800)
    if r.returncode != 0:
        raise DockerError("docker cp 失败：{} -> {}:{}（{}）".format(
            src, name, dest, (r.stderr or "").strip()[:200]))


def mkdir(name, path):
    script = "mkdir -p {}".format(_sh_quote(path))
    r = exec_script(name, script, timeout=60)
    if r.exit_code != 0:
        raise DockerError("创建容器目录失败：{}".format(path))


class ExecResult:
    """一次容器内命令的执行结果。"""

    def __init__(self, exit_code, output, timed_out=False, elapsed_s=0.0):
        self.exit_code = exit_code
        self.output = output
        self.timed_out = timed_out
        self.elapsed_s = elapsed_s

    @property
    def ok(self):
        return self.exit_code == 0 and not self.timed_out

    def tail(self, limit=4000):
        """末尾 limit 个字符（回灌给模型时避免超长输出撑爆上下文）。"""
        text = self.output or ""
        return text if len(text) <= limit else "...(已截断)...\n" + text[-limit:]


def exec_script(name, script, workdir=None, timeout=DEFAULT_EXEC_TIMEOUT,
                on_line=None, env=None):
    """在容器内以 bash 执行一段脚本（经 stdin 传入，避免引号转义问题）。

    超时只杀掉 docker exec 客户端，容器内的进程可能仍在运行；调用方在任务结束后
    统一 docker rm -f，因此不会泄漏。
    """
    args = [DOCKER_BIN, "exec", "-i"]
    if workdir:
        args += ["-w", workdir]
    for key, value in (env or {}).items():
        args += ["-e", "{}={}".format(key, value)]
    args += [name, "bash", "-s"]
    started = time.time()
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DockerError("docker exec 启动失败：{}".format(exc))
    lines = []

    def reader():
        for line in proc.stdout:
            text = line.rstrip("\n")
            lines.append(text)
            if on_line:
                on_line(text)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        proc.stdin.write(script)
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    thread.join(timeout=10)
    return ExecResult(proc.returncode, "\n".join(lines), timed_out,
                      time.time() - started)


def read_text(name, path, timeout=60):
    """读取容器内文本文件；不存在或读取失败返回 None。"""
    r = exec_script(name, "cat {}".format(_sh_quote(path)), timeout=timeout)
    if r.exit_code != 0:
        return None
    return r.output


def read_reward(name, timeout=60):
    """读取验证脚本写出的 reward（0/1）；文件缺失返回 None。"""
    text = read_text(name, REWARD_PATH, timeout=timeout)
    if text is None:
        return None
    for token in (text or "").strip().split():
        try:
            return float(token)
        except ValueError:
            continue
    return None


def remove_container(name, timeout=180):
    """强制删除容器（不存在时静默返回）。"""
    if not name:
        return
    try:
        _run(["rm", "-f", name], timeout=timeout)
    except DockerError as exc:
        logger.warning("[docker rm] %s 失败：%s", name, exc)
    finally:
        _unregister(name)


def remove_all():
    """清理本进程创建的全部容器（服务退出或任务停止时调用）。"""
    for name in live_containers():
        remove_container(name)


def container_name(run_id, index):
    """容器名：run_id 已限定为 [0-9A-Za-z._-]，这里再拼一个自增序号。"""
    safe = "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in str(run_id))
    return "{}-{}-{}".format(_CONTAINER_PREFIX, safe, index)[:60]


def _sh_quote(text):
    return "'" + str(text).replace("'", "'\\''") + "'"
