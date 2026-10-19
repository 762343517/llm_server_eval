"""Agent 评测核心逻辑：用 pier 在 docker 沙箱里跑 SWE 类编码 agent 任务（DeepSWE）。

与「单轮作答 + LLM 裁判」的精度测试是两套执行模型：
  1. 每道题是一个任务目录（task.toml + instruction.md + environment/ + tests/），
     由外部沙箱框架逐任务起容器执行；
  2. agent 在容器内自主改代码（多轮工具调用），单任务超时以小时计；
  3. 判定由任务自带的测试驱动：应用测试补丁 → 跑测试脚本 → reward = 是否通过；
  4. 因此这里不解析数据集表，而是驱动沙箱框架并解析其任务产物。

任务目录来自 dataset/datasets.json 的 agent 段（见 dataset_registry）；本模块做三件事：
环境自检与适配（docker / pier / egress 代理端口）、组装 pier 命令、执行并解析结果。
"""
import calendar
import collections
import glob
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time

import consts
import dataset_registry
import utils

try:                                    # Python >= 3.11 自带 tomllib，3.10 用 tomli
    import tomllib as _toml
except ImportError:                     # pragma: no cover
    try:
        import tomli as _toml
    except ImportError:
        _toml = None

FRAMEWORK = "pier"
PIER_COMMAND = "pier"
PIER_FALLBACK_BIN = os.path.expanduser("~/.local/bin/pier")   # uv tool 安装的默认位置

# pier CLI 一启动就会 import LiteLLM，而 LiteLLM 默认去 GitHub 拉 model cost map 做定价；
# 内网拉不通时它要重试 3 次并退避，于是单次 `pier --version` 要 22.7s（实测），每次
# `pier run` 也要先白付这笔时间。置为 True 表示只用随包分发的本地价目表（不影响模型调用）；
# 子进程继承本进程的环境变量，因此 uvicorn 与 CLI 两条入口都从这里生效。
# 用 setdefault：外部显式设置过就不覆盖。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# pier 在 allow_internet=false 的任务里起 squid 做 egress 代理，配置把「安全端口」写死为
# 80/443，自部署模型端点（如 http://10.174.0.70:34000/v1）会被 `deny !Safe_ports` 拒绝，
# agent 因此拿不到模型响应（表现为 agent 空转、超时）。这里把安全端口放宽到非特权端口：
# 目标主机仍受 allowed_domains 白名单限制（只放行配置的模型端点），隔离语义不变。
EGRESS_SAFE_PORTS_ORIG = "acl Safe_ports port 80 443"
EGRESS_SAFE_PORTS_PATCH = "acl Safe_ports port 80 443 3000-65535"
# 视为「已放宽」的写法：3000-65535 覆盖自部署模型端点常用端口
EGRESS_SAFE_PORTS_OK = ("acl Safe_ports port 80 443 3000-65535",
                        "acl Safe_ports port 80 443 1024-65535")

_IMAGE_CACHE = {"at": 0.0, "tags": set()}
_IMAGE_TTL = 60.0
# docker daemon 状态变化快（用户可能刚起停 docker），缓存只用来压掉同一批请求里的重复探测
_DOCKER_CACHE = {"at": 0.0, "data": None}
_DOCKER_TTL = 10.0
LOG_TAIL_LINES = 200          # pier 原始输出的回看行数（失败时给出更多现场）


# ------------------------------------------------------------------ 命令执行与探测
def _run(argv, timeout=30):
    """执行命令并返回 (returncode, 合并后的输出)；命令不存在或超时返回 rc=-1。"""
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "命令超时（{}s）：{}".format(timeout, " ".join(argv[:3]))
    except OSError as exc:
        return -1, "命令不可用：{}".format(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _kill_group(proc):
    """强杀子进程及其整个进程组：只杀父进程会留下 docker pull 这类子进程。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _run_interruptible(argv, timeout, should_stop, poll=0.5):
    """执行命令，期间可被 should_stop 打断；返回 (returncode, 输出尾部)。

    _run 是阻塞的 subprocess.run，而 docker pull 单个镜像可能持续几分钟（数 GB），这段
    时间里「停止」会完全失效。这里改为轮询：收到停止请求或超时就杀掉整个进程组。
    输出落到临时文件而不是管道，避免无人读取时管道被写满、把子进程卡死。
    """
    tmp = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(argv, stdout=tmp, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as exc:
        tmp.close()
        return -1, "命令不可用：{}".format(exc)
    deadline = time.time() + timeout
    note = ""
    while proc.poll() is None:
        if should_stop():
            note = "已按停止请求中止"
            _kill_group(proc)
            break
        if time.time() > deadline:
            note = "命令超时（{}s）".format(timeout)
            _kill_group(proc)
            break
        time.sleep(poll)
    try:
        tmp.seek(0, os.SEEK_END)
        size = tmp.tell()
        tmp.seek(max(0, size - 8192))        # 只取尾部：失败原因总在最后几行
        out = tmp.read().decode("utf-8", "replace")
    finally:
        tmp.close()
    if note:
        return -1, (out.rstrip() + "\n" + note) if out.strip() else note
    return proc.returncode, out


def _last_line(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def pier_bin():
    """pier 可执行文件：优先 PATH，其次 uv tool 的默认安装位置。"""
    found = shutil.which(PIER_COMMAND)
    if found:
        return found
    return PIER_FALLBACK_BIN if os.path.isfile(PIER_FALLBACK_BIN) else ""


def pier_version():
    """pier 版本号（未安装时返回空串）。

    优先读安装目录里的 dist-info 元数据（零子进程）：`pier --version` 要起一次 CLI，
    导入开销以秒计（见 LITELLM_LOCAL_MODEL_COST_MAP 处的说明），而环境自检在页面
    每次加载时都可能问一次版本。读不到（非 uv tool 安装等）再回退到 CLI。
    """
    binary = pier_bin()
    if not binary:
        return ""
    version = _pier_version_from_metadata(binary)
    if version:
        return version
    rc, out = _run([binary, "--version"], timeout=60)
    match = re.search(r"(\d+\.\d+\.\d+\S*)", out)
    return match.group(1) if rc == 0 and match else ""


def _pier_version_from_metadata(binary):
    """从 <venv>/lib/python3.*/site-packages/datacurve_pier-*.dist-info/METADATA 取版本。"""
    venv = os.path.dirname(os.path.dirname(os.path.realpath(binary)))
    pattern = os.path.join(venv, "lib", "python3.*", "site-packages",
                           "datacurve_pier-*.dist-info", "METADATA")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
                    if not line.strip():        # 头部结束，后面不再是键值对
                        break
        except OSError:
            continue
    return ""


def docker_state():
    """docker 可用性：先直连，失败再用 `sg docker`（用户已在 docker 组但会话未刷新）。"""
    rc, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if rc == 0:
        return {"ok": True, "mode": "direct", "version": out.strip(), "detail": ""}
    direct_error = _last_line(out)
    rc2, out2 = _run(["sg", "docker", "-c", "docker info --format '{{.ServerVersion}}'"],
                     timeout=20)
    if rc2 == 0:
        return {"ok": True, "mode": "sg", "version": out2.strip(),
                "detail": "当前进程没有 docker 组权限，已改用 sg docker 执行"}
    return {"ok": False, "mode": "none", "version": "",
            "detail": direct_error or _last_line(out2) or "docker 不可用"}


def docker_state_cached(use_cache=True):
    """docker 可用性（短缓存）：只压掉同一批页面请求里的重复探测。

    docker daemon 可能刚被起停，TTL 远短于 pier/egress 这类静态项（见 _static_env）。
    """
    now = time.time()
    if use_cache and _DOCKER_CACHE["data"] and now - _DOCKER_CACHE["at"] < _DOCKER_TTL:
        return _DOCKER_CACHE["data"]
    state = docker_state()
    _DOCKER_CACHE.update({"at": now, "data": state})
    return state


def wrap_argv(argv, mode):
    """按 docker 权限模式包装命令：sg 模式下整条命令交给 `sg docker -c` 执行。"""
    if mode != "sg":
        return list(argv)
    return ["sg", "docker", "-c", " ".join(shlex.quote(str(a)) for a in argv)]


def known_images():
    """本机 docker 已有镜像的「仓库:标签」集合（带缓存；docker 不可用时返回空集）。"""
    now = time.time()
    if now - _IMAGE_CACHE["at"] < _IMAGE_TTL:
        return _IMAGE_CACHE["tags"]
    tags = set()
    docker = docker_state()
    if docker["ok"]:
        argv = wrap_argv(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                         docker["mode"])
        rc, out = _run(argv, timeout=30)
        if rc == 0:
            tags = {line.strip() for line in out.splitlines() if line.strip()}
    _IMAGE_CACHE.update({"at": now, "tags": tags})
    return tags


def refresh_images():
    """作废镜像清单缓存：pier 跑完会撤掉镜像 tag，run 开始时必须拿到实时快照。"""
    _IMAGE_CACHE.update({"at": 0.0, "tags": set()})


def image_mirrors():
    """镜像加速器前缀列表：环境变量可覆盖，空串表示禁用（只用本机已有镜像）。"""
    raw = os.environ.get("ZHIPU_EVAL_IMAGE_MIRRORS")
    if raw is None:
        return list(consts.DEFAULT_IMAGE_MIRRORS)
    return [p.strip().strip("/") for p in raw.split(",") if p.strip()]


def is_docker_hub(image):
    """镜像是否归 Docker Hub —— 只有 Docker Hub 镜像才能套加速器前缀。

    加速器（docker.1ms.run / docker.m.daocloud.io 等）只代理 Docker Hub。
    带显式仓库主机的镜像（public.ecr.aws/…、ghcr.io/…、quay.io/…、localhost:5000/…）
    套上前缀会拼成加速器根本不认识的引用，实测报 failed to resolve reference、
    invalid repository、要求付费开通，把本来直连就能拉到的镜像变成失败。
    """
    parts = image.split("/")
    if len(parts) < 2:                                  # 裸名（ubuntu:24.04）就是官方 library
        return True
    first = parts[0]
    return not ("." in first or ":" in first or first == "localhost")


_HUB_DIRECT = {"ok": None}          # Docker Hub 直连是否可用：None 未探、True 可用、False 不通


def _hub_direct_worth_trying():
    """本进程内是否还值得先直连 Docker Hub：探到一次不通后不再重复吃 6s 超时。"""
    return _HUB_DIRECT["ok"] is not False


def _pull_candidates(image):
    """给出一条镜像的拉取候选引用：[直连原名, 加速器前缀…]。

    两类仓库在这里的可达性正好相反，所以候选顺序不同：
      * ECR / ghcr / quay 等：直连正常，加速器前缀一律不适用，只有原名一个候选；
      * Docker Hub：直连在内网超时，需经加速器拉取后 tag 回原名。daemon.json 的
        registry-mirrors 生效时，第一个候选（原名）也会由 daemon 自动送到加速器。
    """
    if not is_docker_hub(image):
        return [image]
    candidates = [image] if _hub_direct_worth_trying() else []
    candidates += ["{}/{}".format(prefix, image) for prefix in image_mirrors()]
    return candidates


def ensure_images_local(images, on_event=None, should_stop=None):
    """把本机缺失的任务镜像拉取并 tag 回原名，返回 (ready, failed)。

    为什么要主动做：pier 每次跑完会 `docker compose down --rmi all`，把任务镜像的 tag 撤掉
    （镜像 ID 若还有别的 tag 则不会真删）；下一次运行于是必须重新拉取，拉不到整个 run 会被
    记成「环境错误」而不是「判分不通过」。这里在起 pier 之前把镜像补齐，补不齐就提前报错并
    指明缺哪个。

    should_stop 为真时立即中止：已拉取的保留，未处理的既不记入 ready 也不记入 failed，
    调用方应据此区分「被用户停止」与「网络拉取失败」。
    """
    emit = on_event or (lambda kind, payload=None: None)
    should_stop = should_stop or (lambda: False)
    refresh_images()
    missing = [img for img in dict.fromkeys(images) if img and img not in known_images()]
    if not missing:
        return [], []
    docker = docker_state()
    if not docker["ok"]:
        return [], missing
    ready, failed = [], []
    stopped = False
    for image in missing:
        if should_stop():
            stopped = True
            break
        hub = is_docker_hub(image)
        ok = False
        for ref in _pull_candidates(image):
            if ref != image:
                emit("task_log", {"msg": "[镜像] 经 {} 拉取 {} …".format(
                    ref.split("/", 1)[0], image)})
            rc, out = _run_interruptible(wrap_argv(["docker", "pull", ref], docker["mode"]),
                                         consts.DEFAULT_IMAGE_PULL_TIMEOUT, should_stop)
            if rc != 0:
                if should_stop():               # 是被停止打断的，不当作拉取失败
                    stopped = True
                    break
                if hub and ref == image:
                    _HUB_DIRECT["ok"] = False            # 记住直连不通，后续镜像不再重试
                emit("task_log", {"msg": "[镜像] {} 拉取失败：{}".format(
                    ref, _last_line(out) or "未知原因")})
                continue
            if hub and ref == image:
                _HUB_DIRECT["ok"] = True
            if ref != image:
                rc2, out2 = _run(wrap_argv(["docker", "tag", ref, image], docker["mode"]),
                                 timeout=60)
                if rc2 != 0:
                    emit("task_log", {"msg": "[镜像] tag 失败：{}".format(_last_line(out2))})
                    continue
            ok = True
            emit("task_log", {"msg": "[镜像] 已就绪：{}".format(image)})
            break
        if stopped:
            break
        (ready if ok else failed).append(image)
    if stopped:
        emit("task_log", {"msg": "[停止] 已中止镜像拉取：已就绪 {} 个，未处理 {} 个".format(
            len(ready), len(missing) - len(ready) - len(failed))})
    refresh_images()                                     # 让 known_images 下次重新枚举
    return ready, failed


# --------------------------------------------------------------- egress 代理端口适配
def _pier_setup_path():
    """定位 pier 包内的 environments/agent_setup.py（通过 pier 所在 venv 的 python）。"""
    binary = pier_bin()
    if not binary:
        return ""
    python = os.path.join(os.path.dirname(os.path.realpath(binary)), "python")
    if not os.path.isfile(python):
        return ""
    rc, out = _run([python, "-c",
                    "import pier.environments.agent_setup as m; print(m.__file__)"],
                   timeout=60)
    return _last_line(out) if rc == 0 else ""


def egress_state():
    """egress 代理端口适配状态：patched / original / unknown / missing。"""
    path = _pier_setup_path()
    if not path or not os.path.isfile(path):
        return {"state": "missing", "path": "", "detail": "未找到 pier 的安装目录"}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"state": "unknown", "path": path, "detail": "读取失败：{}".format(exc)}
    if any(ok in text for ok in EGRESS_SAFE_PORTS_OK):
        return {"state": "patched", "path": path, "detail": "已放行非特权端口"}
    if EGRESS_SAFE_PORTS_ORIG in text:
        return {"state": "original", "path": path,
                "detail": "仅放行 80/443，自部署模型端点会被代理拒绝"}
    return {"state": "unknown", "path": path,
            "detail": "未找到预期的端口配置行（pier 版本可能已变化）"}


def ensure_egress_patch():
    """幂等适配：把 squid 的安全端口放宽到非特权端口，返回 (是否就绪, 说明)。

    pier 由 uv tool 安装，升级后补丁会丢失，因此每次发起任务前都检查一次；
    只在命中原始配置行时改写，并保留一份 .orig 备份。
    """
    info = egress_state()
    if info["state"] == "patched":
        return True, info["detail"]
    if info["state"] != "original":
        return False, info["detail"]
    path = info["path"]
    tmp = path + ".tmp"
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if not os.path.isfile(path + ".orig"):
            with open(path + ".orig", "w", encoding="utf-8") as fh:
                fh.write(text)
        patched = text.replace(EGRESS_SAFE_PORTS_ORIG + "\n",
                               EGRESS_SAFE_PORTS_PATCH + "\n")
        if patched == text:
            return False, "未找到可替换的端口配置行"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(patched)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, "写入失败：{}".format(exc)
    return True, "已放行非特权端口（原文件备份为 agent_setup.py.orig）"


# ------------------------------------------------------------------ 数据集与任务清单
def agent_datasets():
    """配置里声明了 agent 段的数据集（即 Agent 任务集）。"""
    return [it for it in dataset_registry.datasets() if it.get("agent")]


def task_spec(name_or_path=""):
    """按名称/路径找 Agent 任务集条目；缺省取第一个（当前即 DeepSWE）。"""
    items = agent_datasets()
    key = str(name_or_path or "").strip()
    if key:
        for it in items:
            if key in (it["name"], it["dir"], it["path"]):
                return it
    return items[0] if items else None


def tasks_dir(spec=None):
    """任务集目录（dataset/<数据集名称>/tasks）。"""
    item = spec if spec is not None else task_spec()
    return ((item or {}).get("agent") or {}).get("tasks_dir", "")


def _read_toml(path):
    """解析 task.toml（缺 tomllib/tomli 或文件损坏时返回空 dict）。"""
    if _toml is None:
        return {}
    try:
        with open(path, "rb") as fh:
            data = _toml.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# 数值兜底统一收敛到 utils（原先 api / agent_core / perf_core 各写一份）
_num = utils.float_or


def list_tasks(directory):
    """枚举任务目录：每个任务含 task.toml（名称取目录名，元信息取自 toml）。"""
    items = []
    if not directory or not os.path.isdir(directory):
        return items
    ready = known_images()
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        task_file = os.path.join(path, "task.toml")
        if not os.path.isfile(task_file):
            continue
        meta = _read_toml(task_file)
        info = meta.get("metadata") or {}
        env = meta.get("environment") or {}
        image = str(env.get("docker_image") or "").strip()
        items.append({
            "name": name,
            "dir": path,
            "title": str(info.get("display_title") or "").strip() or name,
            "category": str(info.get("category") or "").strip(),
            "language": str(info.get("language") or "").strip(),
            "agent_timeout_sec": _num((meta.get("agent") or {}).get("timeout_sec")),
            "verifier_timeout_sec": _num((meta.get("verifier") or {}).get("timeout_sec")),
            "docker_image": image,
            # 镜像是否已在本机（未拉取时首次运行要等下载）
            "image_ready": image in ready if image else False,
        })
    return items


def task_names(directory):
    return [it["name"] for it in list_tasks(directory)]


def tasks_label(directory):
    """任务集标签：dataset/deepswe/tasks -> deepswe（用于日志与汇总标题）。"""
    path = os.path.abspath(str(directory or "."))
    base = os.path.basename(path)
    return os.path.basename(os.path.dirname(path)) if base == "tasks" else base


def resolve_tasks_dir(path):
    """把页面/配置里的路径解析成「任务目录集合」所在目录。

    支持三种写法：数据集名称（如 deepswe）、数据集目录、tasks 目录本身
    （后者用于临时指向别处的任务集，只要子目录里有 task.toml 即可）。
    """
    key = str(path or "").strip()
    spec = dataset_registry.find(key)
    if spec and spec.get("agent"):
        return spec["agent"]["tasks_dir"]
    if key and os.path.isdir(key) and os.path.isdir(os.path.join(key, "tasks")):
        return os.path.join(key, "tasks")
    return key


# ------------------------------------------------------------------ 环境自检
_ENV_CACHE = {"at": 0.0, "data": None}
_ENV_TTL = 300.0              # 静态项（pier / egress / TOML）5 分钟复检一次


def env_report(use_cache=True):
    """环境自检：docker / pier / egress 端口适配 / TOML 解析库。

    返回的 notes 直接作为「发起前提示」写进任务日志；ok=false 时不允许发起。
    自检要起子进程（docker info / pier 探测），而页面每次加载都会调用，因此分两层缓存：
    静态项（pier 路径与版本、egress 补丁状态、TOML 库）5 分钟复检一次，
    docker daemon 状态 10 秒（用户可能刚起停 docker，需要及时反映）；
    真正发起任务时用 use_cache=False 全部复核。
    """
    return _env_report(use_cache)


def _static_env(use_cache=True):
    """自检里「慢、且只在重装 pier 或改配置时才变」的部分。"""
    now = time.time()
    if use_cache and _ENV_CACHE["data"] and now - _ENV_CACHE["at"] < _ENV_TTL:
        return _ENV_CACHE["data"]
    binary = pier_bin()
    data = {
        "pier": {"available": bool(binary), "path": binary,
                 "version": pier_version() if binary else ""},
        "egress": egress_state(),
        "toml": _toml is not None,
    }
    _ENV_CACHE.update({"at": now, "data": data})
    return data


def _env_report(use_cache=True):
    docker = docker_state_cached(use_cache)
    static = _static_env(use_cache)
    binary = static["pier"]["path"]
    version = static["pier"]["version"]
    egress = static["egress"]
    notes = []
    if not binary:
        notes.append("未找到 pier 命令：请先 uv tool install datacurve-pier")
    else:
        notes.append("pier {}（{}）".format(version or "版本未知", binary))
    if not docker["ok"]:
        notes.append("docker 不可用：{}".format(docker["detail"]))
    else:
        notes.append("docker {}（权限模式 {}）".format(docker["version"], docker["mode"]))
        if docker["mode"] == "sg":
            notes.append("当前进程没有 docker 组权限，pier 命令将通过 sg docker 执行")
    if egress["state"] == "patched":
        notes.append("egress 代理已放行非特权端口（自部署端点可直连）")
    elif egress["state"] == "original":
        notes.append("egress 代理默认只放行 80/443，发起时自动放宽到非特权端口")
    else:
        notes.append("egress 代理端口适配状态未知：{}".format(egress["detail"]))
    if _toml is None:
        notes.append("缺少 TOML 解析库（Python < 3.11 需 pip install tomli）")
    return {
        "docker": docker,
        "pier": dict(static["pier"]),
        "egress": dict(egress),
        "toml": static["toml"],
        "notes": notes,
        "ok": bool(binary and docker["ok"] and static["toml"]),
    }


# ------------------------------------------------------------------ 计划与命令
AGENT_CHOICES = ("mini-swe-agent", "oracle", "nop")
# 这些 agent 是评测链路的自检工具（不调用模型），用于验证镜像与判定是否正常
MODEL_FREE_AGENTS = ("oracle", "nop")
ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/responses")

# mini-swe-agent 的依赖声明没有上界（litellm>=1.75.5），uv tool install 会拉到最新版；
# 而 litellm 1.106.0 起把 mcp 声明成 extra，却在 responses/mcp 里无条件硬导入：
#   if not skip_mcp_handler and tools: from litellm.responses.mcp... import ...
# mini-swe-agent 必然带 tools，于是首次调用前就 ModuleNotFoundError（steps/tokens 全 0）。
# 这里锁定已验证的 agent 版本，并用 extra_python_packages 补装 mcp（pier 会执行
# `uv pip install --python "$python_bin" ...`，装进同一个 tool venv）。
MINI_SWE_AGENT_VERSION = "2.4.6"
MINI_SWE_AGENT_EXTRA_PACKAGES = ["mcp>=2.2.0,<3"]

# 这里刻意不覆盖 mini-swe-agent 的 max_consecutive_format_errors / format_error_template：
# 模型返回空响应（content 与 tool_calls 均为 null）属于模型服务质量问题，按 agent 默认值
# （连续 3 次格式错误即 RepeatedFormatError 退出）如实暴露，不做针对性放宽或改写提示。
# 需要排查时看 trajectory 里的空响应次数即可，不要通过调大阈值把问题掩盖掉。


_int = utils.int_or
_opt_float = utils.float_or_none


def _num_text(value, default):
    return "{:g}".format(_num(value, default))


def endpoint_base(endpoint):
    """chat/completions 地址 -> OpenAI 兼容 base_url（litellm 要的是服务根地址）。"""
    text = str(endpoint or "").strip().rstrip("/")
    for suffix in ENDPOINT_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def sample_tasks(catalogue, limit=0, seed=0):
    """按 limit/seed 挑任务：limit<=0 取全部；seed=0 按目录顺序取前 N 个（可复现）。"""
    if limit and limit < len(catalogue):
        if seed:
            return sorted(random.Random(seed).sample(catalogue, limit),
                          key=lambda t: t["name"])
        return catalogue[:limit]
    return list(catalogue)


def _mask_argv(argv):
    """日志里遮盖密钥。"""
    return ["OPENAI_API_KEY=***" if arg.startswith("OPENAI_API_KEY=") else arg
            for arg in argv]


def build_command(cfg, directory, names, jobs_dir, job_name):
    """组装 pier run 命令，返回 (argv, 可展示的命令文本)。"""
    agent = str(cfg.get("agent") or consts.DEFAULT_AGENT_NAME).strip()
    concurrency = max(1, _int(cfg.get("concurrency"), consts.DEFAULT_AGENT_CONCURRENCY))
    argv = [
        pier_bin(), "run", "-p", directory, "-a", agent,
        "-o", jobs_dir, "--job-name", job_name,
        "-n", str(concurrency),
        "--agent-timeout-multiplier",
        _num_text(cfg.get("timeout_multiplier"), consts.DEFAULT_AGENT_TIMEOUT_MULTIPLIER),
        "--verifier-timeout-multiplier", _num_text(
            cfg.get("verifier_timeout_multiplier"),
            consts.DEFAULT_AGENT_VERIFIER_TIMEOUT_MULTIPLIER),
        "-y",
    ]
    model = str(cfg.get("model") or "").strip()
    if model and agent not in MODEL_FREE_AGENTS:
        # litellm 通过前缀判断服务商，自部署 OpenAI 兼容服务统一用 openai/ 前缀
        argv += ["-m", model if "/" in model else "openai/" + model]
        model_class = str(cfg.get("model_class") or "").strip()
        if model_class:
            argv += ["--ak", "model_class=" + model_class]
        base = endpoint_base(cfg.get("endpoint"))
        if base:
            argv += ["--ae", "OPENAI_API_BASE=" + base]
        key = str(cfg.get("api_key") or "").strip()
        if key:
            argv += ["--ae", "OPENAI_API_KEY=" + key]
    if agent == "mini-swe-agent":
        # --ak 的 value 由 pier 用 literal_eval/JSON 解析，列表要写成 JSON 字面量
        argv += ["--ak", "version=" + MINI_SWE_AGENT_VERSION]
        argv += ["--ak", "extra_python_packages="
                 + json.dumps(MINI_SWE_AGENT_EXTRA_PACKAGES)]
        # 不注入 config_yaml：格式错误阈值与提示模板一律用 mini-swe-agent 默认值，
        # 模型空响应等服务质量问题如实暴露，见文件上方说明。
    for name in names:
        argv += ["-i", name]
    return argv, " ".join(shlex.quote(a) for a in _mask_argv(argv))


# ------------------------------------------------------------------ 结果解析
def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, (dict, list)) else None


def _epoch(text):
    """解析 pier 的 ISO 时间戳（UTC）为 epoch 秒；失败返回 None。"""
    raw = str(text or "").strip().replace("Z", "").split(".")[0]
    if not raw:
        return None
    try:
        return calendar.timegm(time.strptime(raw, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _instruction(task_dir, limit=600):
    """任务说明摘要（instruction.md），用于结果表展示与检索。"""
    for name in ("instruction.md", "README.md"):
        path = os.path.join(str(task_dir or ""), name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return " ".join(fh.read().split())[:limit]
            except OSError:
                return ""
    return ""


def trial_task(trial, meta_by_name=None):
    """trial 目录名（pier 会截断）-> 任务名。"""
    prefix = str(trial or "").split("__")[0]
    for name in (meta_by_name or {}):
        if name.startswith(prefix):
            return name
    return prefix


def trial_record(trial_dir, data, meta=None):
    """把 pier 的 trial result.json 归一化成一条结果记录。"""
    task_dir = str((data.get("task_id") or {}).get("path") or "")
    info = meta or {}
    # 优先用任务清单里的名字（与用户选择、任务清单表格一致），pier 的 task_name 带组织前缀
    name = (str(info.get("name") or "").strip()
            or str(data.get("task_name") or "").strip()
            or os.path.basename(task_dir.rstrip(os.sep)) or os.path.basename(trial_dir))
    rewards = ((data.get("verifier_result") or {}).get("rewards")) or {}
    reward = _opt_float(rewards.get("reward"))
    exc = data.get("exception_info") or {}
    error = str(exc.get("exception_message") or exc.get("exception_type") or "").strip()
    agent = data.get("agent_result") or {}
    started, finished = _epoch(data.get("started_at")), _epoch(data.get("finished_at"))
    if error:
        status = "error"
    elif reward is not None and reward >= 1 - 1e-9:
        status = "resolved"
    else:
        status = "unresolved"
    return {
        "task": name,
        "title": info.get("title") or name,
        "category": info.get("category", ""),
        "language": info.get("language", ""),
        "status": status,
        "reward": reward,
        "steps": _int(agent.get("n_agent_steps") or data.get("n_agent_steps")),
        "input_tokens": _int(agent.get("n_input_tokens")),
        "output_tokens": _int(agent.get("n_output_tokens")),
        "cache_tokens": _int(agent.get("n_cache_tokens")),
        "peak_context_tokens": _int(agent.get("peak_context_tokens")),
        "elapsed_s": round(finished - started, 1) if started and finished else None,
        "error": error[:400],
        "problem": _instruction(task_dir),
        "trial": os.path.basename(trial_dir),
        "trial_dir": trial_dir,
        "has_patch": os.path.isfile(os.path.join(trial_dir, "artifacts", "model.patch")),
    }


def scan_trials(jobs_dir):
    """扫描 job 目录：返回 (已完成的 trial [(目录名, 路径)], 仍在跑的 trial 目录名)。"""
    done, running = [], []
    if not os.path.isdir(jobs_dir):
        return done, running
    for entry in sorted(os.listdir(jobs_dir)):
        path = os.path.join(jobs_dir, entry)
        if not os.path.isdir(path):
            continue
        if os.path.isfile(os.path.join(path, "result.json")):
            done.append((entry, path))
        else:
            running.append(entry)
    return done, running


def _group(records, key):
    """按字段分组统计（通过率），供汇总展示。"""
    out = {}
    for row in records:
        name = row.get(key) or "未分类"
        item = out.setdefault(name, {"total": 0, "resolved": 0})
        item["total"] += 1
        if row["status"] == "resolved":
            item["resolved"] += 1
    for item in out.values():
        item["pass_rate"] = round(item["resolved"] / item["total"], 3) if item["total"] else 0.0
    return out


def _tail_text(path, lines=6):
    """读取文件末尾若干行（用于失败时给出 pier 的现场信息）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tail = collections.deque(fh, maxlen=lines)
    except OSError:
        return ""
    return " | ".join(line.strip() for line in tail if line.strip())[:600]


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# rich 进度条重绘留下的「纯装饰」行（进度条、框线、spinner）直接丢弃
# \u2800-\u28ff 是 rich 默认 spinner 用的盲文点阵字符
NOISE_RE = re.compile(r"^[\s\d%·•\u2800-\u28ff▏▎▍▌▋▊▉█░▒▓╭╮╯╰│─━┃▶◀←→↑↓]+$")


def clean_log_line(line):
    """把 pier 的一行输出清理成可展示日志；纯进度条重绘返回空串。"""
    text = line.replace("\r", "\n")
    segments = [seg for seg in ANSI_RE.sub("", text).split("\n") if seg.strip()]
    if not segments:
        return ""
    last = segments[-1].strip()
    return "" if NOISE_RE.match(last) else last[:400]


def verifier_tail(trial_dir, lines=6):
    """判定输出（verifier/test-stdout.txt）末尾若干行，作为任务结果的一句话说明。"""
    return _tail_text(os.path.join(trial_dir, "verifier", "test-stdout.txt"), lines)


def _pump_log(path, offset, on_event, max_lines=40):
    """把单个日志文件的新增内容转成 task_log 事件，返回新的读取偏移。

    每轮最多转发 max_lines 行，避免长日志与高频进度输出把内存和前端拖慢。
    """
    if not path or not os.path.isfile(path):
        return offset
    size = os.path.getsize(path)
    if size <= offset:
        return offset
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for count, line in enumerate(fh):
                if count >= max_lines:
                    break
                text = clean_log_line(line)
                if text:
                    on_event("task_log", {"msg": text})
            return fh.tell()
    except OSError:
        return offset


def _pump_logs(job_dir, offsets, on_event, max_lines=40):
    """轮询 job 目录：pier 自身的 job.log 与各 trial 的 trial.log。"""
    paths = [os.path.join(job_dir, "job.log")]
    try:
        entries = os.listdir(job_dir)
    except OSError:
        entries = []
    for entry in entries:
        candidate = os.path.join(job_dir, entry, "trial.log")
        if os.path.isfile(candidate):
            paths.append(candidate)
    for path in paths:
        offsets[path] = _pump_log(path, offsets.get(path, 0), on_event, max_lines)


def _terminate(proc, grace=25.0):
    """停止 pier：先发 SIGINT（让它自己清理容器），超时再 SIGTERM/SIGKILL。"""
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, 10.0),
                      (signal.SIGKILL, 10.0)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


# ------------------------------------------------------------------ 执行入口
def run_agent_eval(cfg, on_event=None, should_stop=None):
    """执行一轮 Agent 评测（pier 逐任务起容器跑 agent，再跑测试判定 reward）。

    事件与 run_manager 的 agent 进度对齐：
      agent_start / agent_progress / agent_end / task_start / task_done / task_log /
      summary / error
    结果落盘为 <out>.items.jsonl（逐任务）与 <out>.json（汇总），返回汇总 dict；
    没有任何任务产出结果时返回 None（错误信息通过 error 事件给出）。
    """
    on_event = on_event or (lambda kind, payload=None: None)
    should_stop = should_stop or (lambda: False)
    out = str(cfg.get("out") or "").strip()
    if not out:
        raise ValueError("缺少 out 参数（结果输出前缀）")

    env = env_report(use_cache=False)
    for note in env["notes"]:
        on_event("task_log", {"msg": "[环境] " + note})
    if not env["pier"]["available"]:
        raise RuntimeError("未找到 pier 命令：请先 uv tool install datacurve-pier")
    if not env["docker"]["ok"]:
        raise RuntimeError("docker 不可用：{}".format(env["docker"]["detail"]))
    if not env["toml"]:
        raise RuntimeError("缺少 TOML 解析库：请 pip install tomli")
    patched, note = ensure_egress_patch()
    if not patched:
        on_event("task_log", {"msg": "[警告] egress 代理端口未适配：{}".format(note)})

    directory = resolve_tasks_dir(cfg.get("data"))
    if not directory or not os.path.isdir(directory):
        raise RuntimeError("任务目录不存在: {}".format(directory or cfg.get("data") or "-"))
    refresh_images()                  # 上一轮 run 可能刚把镜像 tag 撤掉，这里取实时快照
    catalogue = list_tasks(directory)
    if not catalogue:
        raise RuntimeError("{} 下没有找到任务（任务目录需含 task.toml）".format(directory))
    limit = _int(cfg.get("limit"), consts.DEFAULT_AGENT_N_TASKS)
    wanted = str(cfg.get("tasks") or "").strip()
    if wanted:
        # 指定任务时按清单顺序执行，便于复现单个任务的问题
        picked = {n.strip() for n in wanted.replace("\n", ",").split(",") if n.strip()}
        selected = [t for t in catalogue if t["name"] in picked]
        missing = sorted(picked - {t["name"] for t in catalogue})
        if missing:
            raise RuntimeError("任务不存在: {}".format(", ".join(missing[:5])))
    else:
        selected = sample_tasks(catalogue, limit,
                                _int(cfg.get("seed"), consts.DEFAULT_AGENT_SEED))
    meta_by_name = {t["name"]: t for t in catalogue}

    run_root = os.path.dirname(os.path.abspath(out))
    job_name = os.path.basename(run_root) or "job"
    jobs_dir = os.path.join(run_root, "pier")
    # pier 的目录约定：-o <jobs_dir> + --job-name <name> -> job 目录，trial 是它的直接子目录
    job_dir = os.path.join(jobs_dir, job_name)
    job_log = os.path.join(job_dir, "job.log")
    os.makedirs(job_dir, exist_ok=True)

    agent_name = str(cfg.get("agent") or consts.DEFAULT_AGENT_NAME).strip()
    model = str(cfg.get("model") or "").strip()
    concurrency = max(1, _int(cfg.get("concurrency"), consts.DEFAULT_AGENT_CONCURRENCY))
    argv, shown = build_command(cfg, directory, [t["name"] for t in selected],
                                jobs_dir, job_name)
    on_event("task_log", {"msg": "[命令] " + shown})
    pending_images = [t for t in selected if not t["image_ready"] and t["docker_image"]]
    if pending_images:
        on_event("task_log", {"msg": "[镜像] {} 个任务的镜像不在本机，尝试拉取"
                                      "（pier 每次跑完会撤掉镜像 tag）".format(
                                          len(pending_images))})
        _, failed = ensure_images_local([t["docker_image"] for t in pending_images],
                                        on_event, should_stop=should_stop)
        if should_stop():
            # 停止可能发生在镜像拉取阶段：此时还没起 pier，直接收敛为「已停止」
            on_event("task_log", {"msg": "[停止] 镜像拉取阶段收到停止请求，本轮不启动 pier"})
            return None
        if failed:
            raise RuntimeError("以下任务镜像本机缺失且拉取失败：{}。请检查网络，或用 "
                               "ZHIPU_EVAL_IMAGE_MIRRORS 指定可用的镜像加速器。".format(
                                   "、".join(failed[:5])))

    started = time.time()
    on_event("agent_start", {
        "dataset": tasks_label(directory),
        "total": len(selected), "cached": 0, "todo": len(selected),
        "concurrency": concurrency, "agent": agent_name, "model": model or "-",
    })

    docker = env["docker"]
    poll = max(0.5, _num(cfg.get("poll_seconds"), consts.DEFAULT_AGENT_POLL_SECONDS))
    proc = subprocess.Popen(wrap_argv(argv, docker["mode"]), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, start_new_session=True)
    records = {}
    seen_running = set()
    log_offsets = {}
    stdout_tail = collections.deque(maxlen=LOG_TAIL_LINES)
    done = errors = resolved = steps = tokens = 0
    stopped = False
    last_emit = 0.0

    def read_stdout():
        """pier 的实时输出必须持续读走，否则管道写满会把子进程卡死。"""
        try:
            for line in proc.stdout:
                stdout_tail.append(line.rstrip())
                text = clean_log_line(line)
                if text:
                    on_event("task_log", {"msg": text})
        except (OSError, ValueError):        # 进程被强杀时管道可能已关闭
            return

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()

    def emit_progress(force=False):
        nonlocal last_emit
        now = time.time()
        if not force and now - last_emit < 2.0:
            return
        last_emit = now
        span = max(now - started, 1e-6)
        on_event("agent_progress", {
            "done": done, "total": len(selected), "errors": errors,
            "resolved": resolved, "steps": steps, "tokens": tokens,
            "elapsed_s": round(span, 1), "tps": round(tokens / span, 1),
            "cached": 0, "todo": len(selected),
        })

    def collect():
        """扫出新完成的 trial；首次见到 trial 目录时补一条启动日志。"""
        nonlocal done, errors, resolved, steps, tokens
        finished, running = scan_trials(job_dir)
        for trial in running:
            if trial in seen_running:
                continue
            seen_running.add(trial)
            name = trial_task(trial, meta_by_name)
            on_event("task_start", {"index": len(seen_running), "task_id": name,
                                    "title": (meta_by_name.get(name) or {}).get("title", "")})
        fresh = []
        for trial, path in finished:
            if trial in records:
                continue
            name = trial_task(trial, meta_by_name)
            record = trial_record(path, _read_json(os.path.join(path, "result.json")) or {},
                                  meta_by_name.get(name))
            record["task_id"] = record["task"]
            record["original_index"] = len(records) + 1
            record["index"] = record["original_index"]
            record["verifier_tail"] = verifier_tail(path)
            records[trial] = record
            fresh.append(record)
            done += 1
            steps += record["steps"]
            tokens += (record["input_tokens"] + record["output_tokens"]
                       + record["cache_tokens"])
            if record["status"] == "error":
                errors += 1
            elif record["status"] == "resolved":
                resolved += 1
            if record["verifier_tail"]:
                on_event("task_log", {"msg": "[判定] {}：{}".format(
                    record["task"], record["verifier_tail"])})
        return fresh

    try:
        while True:
            _pump_logs(job_dir, log_offsets, on_event)
            fresh = collect()
            for record in fresh:
                on_event("task_done", record)
            emit_progress(force=bool(fresh))
            if proc.poll() is not None:
                break
            if should_stop():
                stopped = True
                on_event("task_log", {"msg": "[停止] 收到停止请求，正在结束 pier 与其容器…"})
                _terminate(proc)
                break
            time.sleep(poll)
    finally:
        if proc.poll() is None:
            _terminate(proc)
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        reader.join(timeout=5)
        _pump_logs(job_dir, log_offsets, on_event)
        for record in collect():
            on_event("task_done", record)
        emit_progress(force=True)

    elapsed = time.time() - started
    rc = proc.returncode
    items = sorted(records.values(), key=lambda r: r["original_index"])
    if not items:
        scene = _tail_text(job_log) or " ".join(list(stdout_tail)[-4:]).strip()
        on_event("error", {"message": "没有任何任务产出结果（pier 退出码 {}）：{}".format(
            rc, scene[:600] or "详见任务目录 {} 下的 pier 产物".format(run_root))})
        return None
    if stopped or should_stop():
        stopped = True

    resolved_n = sum(1 for r in items if r["status"] == "resolved")
    error_n = sum(1 for r in items if r["status"] == "error")
    unfinished = len(items) - resolved_n - error_n
    in_tok = sum(r["input_tokens"] for r in items)
    out_tok = sum(r["output_tokens"] for r in items)
    cache_tok = sum(r["cache_tokens"] for r in items)
    durations = [r["elapsed_s"] for r in items if r["elapsed_s"]]
    summary = {
        "kind": "agent",
        "framework": FRAMEWORK,
        "agent": agent_name,
        "model": model,
        "dataset": tasks_label(directory),
        "tasks_dir": directory,
        "pier_version": env["pier"]["version"],
        "pier_jobs_dir": jobs_dir,
        "docker_mode": docker["mode"],
        "egress": env["egress"]["state"],
        "num_tasks": len(selected),
        "num_records": len(items),
        "num_missing": max(0, len(selected) - len(items)),
        "resolved": resolved_n,
        "unresolved": unfinished,
        "errors": error_n,
        "pass_at_1": round(resolved_n / len(items), 4),
        "steps": steps,
        "avg_steps": round(steps / len(items), 1),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_tokens": cache_tok,
        "tokens": in_tok + out_tok + cache_tok,
        "elapsed_s": round(elapsed, 1),
        "avg_elapsed_s": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "concurrency": concurrency,
        "tasks": [t["name"] for t in selected],
        "by_category": _group(items, "category"),
        "by_language": _group(items, "language"),
        "stopped": stopped,
        "raw": os.path.relpath(jobs_dir, consts.PROJECT_ROOT),
    }
    with open(out + ".items.jsonl", "w", encoding="utf-8") as fh:
        for row in items:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(out + ".json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    on_event("agent_end", {"done": done, "errors": error_n, "resolved": resolved_n,
                           "elapsed_s": round(elapsed, 1), "stopped": stopped})
    on_event("summary", {"summary": summary, "records": len(items)})
    return summary
