"""Agent 评测核心逻辑：用 pier 在 docker 沙箱里跑 SWE 类编码 agent 任务（DeepSWE）。

与「单轮作答 + LLM 裁判」的精度测试是两套执行模型：
  1. 每道题是一个任务目录（task.toml + instruction.md + environment/ + tests/），
     由外部沙箱框架逐任务起容器执行；
  2. agent 在容器内自主改代码（多轮工具调用），单任务超时以小时计；
  3. 判定由任务自带的测试驱动：应用测试补丁 → 跑测试脚本 → reward = 是否通过；
  4. 因此这里不解析数据集表，而是驱动沙箱框架并解析其任务产物。

任务集清单与格式约定（任务目录 / task.toml / instruction.md）来自 adapters.tasks；
本模块只做三件事：环境自检与适配（docker / pier / egress 代理端口）、组装 pier 命令、
执行并解析结果。
"""
import calendar
import collections
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile

import adapters
import consts
import utils

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
    raw = os.environ.get("LLM_SERVER_EVAL_IMAGE_MIRRORS")
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
# 任务集的格式约定（task.toml / instruction.md / 目录布局）统一在 adapters/tasks.py，
# 这里只保留与 pier 运行相关的包装（把本机镜像清单注入适配器）。
def agent_datasets():
    """配置里声明了 agent 段的数据集（即 Agent 任务集）。"""
    return adapters.tasks.catalog()


def task_spec(name_or_path=""):
    """按名称/路径找 Agent 任务集条目；缺省取第一个。"""
    return adapters.tasks.spec(name_or_path)


def tasks_dir(spec=None):
    """任务集目录（dataset/<数据集名称>/tasks）。"""
    return adapters.tasks.directory(spec)


def list_tasks(directory):
    """枚举任务目录（含 task.toml 元信息与本机镜像是否就绪）。"""
    return adapters.tasks.list_tasks(directory, images=known_images())


def task_names(directory):
    return adapters.tasks.names(directory, images=known_images())


def tasks_label(directory):
    """任务集标签：dataset/deepswe/tasks -> deepswe（用于日志与汇总标题）。"""
    return adapters.tasks.label(directory)


def resolve_tasks_dir(path):
    """把页面/配置里的路径解析成「任务目录集合」所在目录。"""
    return adapters.tasks.resolve_dir(path)


# 数值兜底统一收敛到 utils（原先 api / agent_core / perf_core 各写一份）
_num = utils.float_or


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
        "toml": adapters.tasks.toml_available(),
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
    if not adapters.tasks.toml_available():
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
    """按 limit/seed 挑任务（抽样策略见 adapters.tasks.sample）。"""
    return adapters.tasks.sample(catalogue, limit, seed)


_SELECTION_CACHE = {}
_SELECTION_LOCK = threading.Lock()


def resolve_selection(cfg, use_cache=False):
    """按运行配置解析本轮任务清单，返回 (任务目录, 目录清单, 选中任务)。

    与 run_agent_eval 共用同一套选任务逻辑：子任务看板据此还原“还有哪些任务在等待”，
    对没有把清单落盘的历史 run 同样有效。use_cache 仅供看板/日志这类高频只读接口使用，
    实际执行始终按最新目录与镜像状态重新解析。
    """
    key = (str(cfg.get("data") or ""), str(cfg.get("tasks") or ""),
           str(cfg.get("limit") or ""), str(cfg.get("seed") or ""))
    if use_cache:
        with _SELECTION_LOCK:
            hit = _SELECTION_CACHE.get(key)
        if hit:
            return hit
    directory = resolve_tasks_dir(cfg.get("data"))
    if not directory or not os.path.isdir(directory):
        raise RuntimeError("任务目录不存在: {}".format(directory or cfg.get("data") or "-"))
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
    result = (directory, catalogue, selected)
    if use_cache:
        with _SELECTION_LOCK:
            if len(_SELECTION_CACHE) > 16:
                _SELECTION_CACHE.clear()
            _SELECTION_CACHE[key] = result
    return result


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
    """任务说明摘要（格式约定见 adapters.tasks.instruction）。"""
    return adapters.tasks.instruction(task_dir, limit)


def trial_task(trial, meta_by_name=None):
    """trial 目录名（pier 会截断）-> 任务名。"""
    prefix = str(trial or "").split("__")[0]
    for name in (meta_by_name or {}):
        if name.startswith(prefix):
            return name
    return prefix


# ---------------------------------------------------------------- 失败原因分类
# 非通过的任务统一标一个 failure_kind，用来区分“预算不够”“模型/指令没跟上”
# 和“环境没起来”。只看异常类型与异常消息，不依赖任务名，也不改判分口径。
FAILURE_KIND_TEXT = {
    "timeout": "超时异常",
    "instruction": "指令异常",
    "environment": "环境异常",
    "unresolved": "判分未通过",
    "unknown": "未分类异常",
}
# 环境没起来（镜像/容器/网络/依赖）与超时反映的是评测环境与预算，不是模型能力：
# 与「判定与计分口径」里无效判定的处理一致，这两类失败不计入通过率的分母，
# 但仍逐条记录，并在失败原因分布里如实展示（不丢弃、不改判分结果）。
EXCLUDED_FAILURE_KINDS = ("timeout", "environment")
_TIMEOUT_HINTS = ("timeout", "timed out", "超时")
# agent 起不来（命令缺失、路径错、无权限）虽然可能包成 NonZeroAgentExitCodeError，
# 本质是 harness/环境问题，优先级高于指令异常。
_STARTUP_HINTS = (
    "command not found", "no such file or directory", "permission denied",
    "exec format error", "not executable",
)
# 模型输出不符合 agent 协议，或模型服务本身报错（空响应、格式错、上下文超限）。
_INSTRUCTION_HINTS = (
    "formaterror", "format error", "repeatedformat", "no tool call",
    "tool call", "toolcall", "invalid response", "empty response",
    "outputlengthexceeded", "contextwindow", "context window",
    "context length", "badrequest", "ratelimit", "authentication",
    "apierror", "api error",
)
# 镜像、容器、网络、依赖安装、判定链路。
_ENVIRONMENT_HINTS = (
    "docker", "compose", "image", "container", "healthcheck",
    "environmentstart", "agentsetup", "environment setup",
    "network", "connection", "dns", "proxy", "pull access denied",
    "manifest unknown", "could not find a version",
    "no matching distribution", "install failed", "installation failed",
    # compose/buildkit 的构建失败与端口/资源冲突：消息里不一定带 docker/container 字样
    "failed to solve", "port is already allocated", "address already in use",
    "no such image", "oci runtime",
)


def classify_failure(status, exc_type="", message=""):
    """把一条结果归到 failure_kind；通过的任务返回空串。"""
    if status == "resolved":
        return ""
    if status != "error":
        return "unresolved"
    # 只取消息前 400 字符：NonZeroAgentExitCodeError 会把整条任务指令拼进消息，
    # 全量匹配容易被指令正文里的字眼带偏。
    text = "{} {}".format(exc_type or "", (message or "")[:400]).lower()
    if any(h in text for h in _TIMEOUT_HINTS):
        return "timeout"
    if any(h in text for h in _STARTUP_HINTS):
        return "environment"
    if any(h in text for h in _INSTRUCTION_HINTS):
        return "instruction"
    if any(h in text for h in _ENVIRONMENT_HINTS):
        return "environment"
    return "unknown"


def fill_failure_kind(row):
    """旧 run 的 items 没有分类字段，读取时按同一规则补齐（只改内存对象）。"""
    if row.get("failure_kind"):
        return row
    kind = classify_failure(str(row.get("status") or ""),
                            str(row.get("failure_type") or ""),
                            str(row.get("error") or ""))
    row["failure_kind"] = kind
    row["failure_kind_text"] = FAILURE_KIND_TEXT.get(kind, "")
    return row


def failure_kind_counts(records):
    """失败原因分布，固定顺序返回，便于前端直接渲染。"""
    counts = collections.Counter(r.get("failure_kind") for r in records
                                 if r.get("failure_kind"))
    return [{"kind": kind, "text": text, "count": counts.get(kind, 0)}
            for kind, text in FAILURE_KIND_TEXT.items()]


def is_excluded(row):
    """该任务是否因环境/超时失败而不计入通过率分母。"""
    return row.get("failure_kind") in EXCLUDED_FAILURE_KINDS


def scoring(records):
    """按「剔除环境/超时」的口径统计通过率，返回分数字段。

    分母是「计分任务数」（num_scored）= 有结果的任务 - 环境/超时失败任务；
    没有任何计分任务时 pass_at_1 记 0（避免除零），num_excluded 仍如实给出。
    """
    resolved = sum(1 for r in records if r.get("status") == "resolved")
    excluded = sum(1 for r in records if is_excluded(r))
    scored = len(records) - excluded
    return {
        "resolved": resolved,
        "num_scored": scored,
        "num_excluded": excluded,
        "pass_at_1": round(resolved / scored, 4) if scored else 0.0,
    }


def apply_scoring(summary, records):
    """按当前口径补齐汇总里的通过率字段（只改内存对象，不落盘）。

    旧 run 的 pass_at_1 把环境/超时失败也算进分母，读取时用逐任务记录重算；
    分组通过率同一口径，一并重算。
    """
    if not summary or not records:
        return summary
    rows = [fill_failure_kind(r) for r in records]
    summary.update(scoring(rows))
    summary["by_category"] = _group(rows, "category")
    summary["by_language"] = _group(rows, "language")
    return summary


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
    exc_type = str(exc.get("exception_type") or "").strip()
    error = str(exc.get("exception_message") or exc.get("exception_type") or "").strip()
    agent = data.get("agent_result") or {}
    started, finished = _epoch(data.get("started_at")), _epoch(data.get("finished_at"))
    if error:
        status = "error"
    elif reward is not None and reward >= 1 - 1e-9:
        status = "resolved"
    else:
        status = "unresolved"
    failure_kind = classify_failure(status, exc_type, error)
    return {
        "task": name,
        "title": info.get("title") or name,
        "category": info.get("category", ""),
        "language": info.get("language", ""),
        "status": status,
        "failure_kind": failure_kind,
        "failure_kind_text": FAILURE_KIND_TEXT.get(failure_kind, ""),
        "failure_type": exc_type[:120],
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


# ---------------------------------------------------------------- 子任务看板
# 逐任务结果表格要覆盖“还没跑完”的任务，因此状态只有四种：
#   等待（清单里有、trial 目录还没建）
#   运行中（trial 目录已建、没有 result.json）
#   完成（正常结束，含判分未通过）
#   异常退出（有 exception_info，额外带 failure_kind 分类）
SUBTASK_STATE_TEXT = {
    "waiting": "等待",
    "running": "运行中",
    "done": "完成",
    "error": "异常退出",
}
# 运行中只有 agent 输出在持续增长，结束后才有 pier 自己写的 trial.log
SUBTASK_LOG_CANDIDATES = ("agent/mini-swe-agent.txt", "trial.log")


def trial_log_rel(trial_dir):
    """trial 目录里最适合实时查看的日志（相对路径，取不到返回空串）。"""
    for rel in SUBTASK_LOG_CANDIDATES:
        if os.path.isfile(os.path.join(trial_dir, rel)):
            return rel
    return ""


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _trial_started(trial_dir):
    """trial 开始时间：优先 config.json 的 mtime（pier 建目录时写一次就不再动）。

    目录 ctime/mtime 会随容器写入产物不断刷新，不能用来算运行时长。
    """
    stamp = os.path.join(trial_dir, "config.json")
    if os.path.isfile(stamp):
        try:
            return os.path.getmtime(stamp)
        except OSError:
            pass
    try:
        return os.path.getctime(trial_dir)
    except OSError:
        return None


def find_trial_dir(cfg, out, task):
    """按任务名定位 trial 目录（不构建整张看板，供日志轮询使用）。"""
    task = str(task or "").strip()
    if not task or "/" in task or "\\" in task or task.startswith("."):
        return ""
    job_dir = _job_dir(out)
    try:
        entries = os.listdir(job_dir)
    except OSError:
        return ""
    for entry in entries:
        path = os.path.join(job_dir, entry)
        if not os.path.isdir(path):
            continue
        prefix = entry.split("__")[0]
        if (entry == task or entry.startswith(task + "__") or prefix == task
                or (prefix and task.startswith(prefix))):
            return path
    return ""


def _job_dir(out):
    """pier 的 job 目录：<run 目录>/pier/<run 目录名>。"""
    run_root = os.path.dirname(os.path.abspath(out))
    return os.path.join(run_root, "pier", os.path.basename(run_root))


def read_log_slice(path, offset=-1, max_lines=200, max_bytes=256 * 1024):
    """读一段日志：offset<0（或文件回退）从尾部回看，否则按 offset 增量读。

    返回 {text, next, reset, size}；reset=True 表示调用方应先清空已有内容。
    """
    if not path or not os.path.isfile(path):
        return {"text": "", "next": 0, "reset": True, "size": 0}
    size = _file_size(path)
    reset = offset is None or offset < 0 or offset > size
    if reset:
        # 首次/回退：只回看尾部 max_bytes，再取其中最后 max_lines 行
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fh.seek(max(0, size - max_bytes))
                if size > max_bytes:
                    fh.readline()        # 丢掉被截断的半行
                lines = [clean_log_line(line) for line in fh]
        except OSError:
            return {"text": "", "next": 0, "reset": True, "size": size}
        lines = [line for line in lines if line][-max_lines:]
        return {"text": "\n".join(lines), "next": size, "reset": True, "size": size}
    lines = []
    next_offset = offset
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            while len(lines) < max_lines:
                line = fh.readline()
                if not line:
                    break
                next_offset = fh.tell()
                text = clean_log_line(line)
                if text:
                    lines.append(text)
    except OSError:
        return {"text": "", "next": 0, "reset": True, "size": size}
    return {"text": "\n".join(lines), "next": next_offset, "reset": False, "size": size}


def list_subtasks(cfg, out):
    """子任务看板：计划清单 × pier 目录的实时状态。"""
    _, catalogue, selected = resolve_selection(cfg, use_cache=True)
    meta_by_name = {t["name"]: t for t in catalogue}
    job_dir = _job_dir(out)
    finished, running = scan_trials(job_dir)

    done_by_task = {}
    for trial, path in finished:
        name = trial_task(trial, meta_by_name)
        record = trial_record(path, _read_json(os.path.join(path, "result.json")) or {},
                              meta_by_name.get(name))
        record["verifier_tail"] = verifier_tail(path)
        record["task_id"] = record["task"]
        record.pop("problem", None)      # 任务全文不进列表，详情里另看
        done_by_task[name] = record
    run_by_task = {trial_task(trial, meta_by_name): os.path.join(job_dir, trial)
                   for trial in running}

    now = time.time()
    rows = []
    for index, task in enumerate(selected, 1):
        name = task["name"]
        row = {
            "index": index, "original_index": index,
            "task": name, "task_id": name,
            "title": task.get("title") or name,
            "category": task.get("category", ""),
            "language": task.get("language", ""),
            "state": "waiting", "state_text": SUBTASK_STATE_TEXT["waiting"],
            "status": "waiting", "result_text": "",
            "reward": None, "steps": None,
            "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0,
            "elapsed_s": None, "started_at": None, "finished": False,
            "error": "", "verifier_tail": "", "trial": "", "trial_dir": "",
            "has_patch": False, "log_rel": "", "log_size": 0,
            "failure_kind": "", "failure_kind_text": "", "failure_type": "",
        }
        record = done_by_task.get(name)
        path = run_by_task.get(name)
        if record:
            row.update({key: record.get(key) for key in (
                "status", "reward", "steps", "input_tokens", "output_tokens",
                "cache_tokens", "peak_context_tokens", "elapsed_s", "error",
                "trial", "trial_dir", "has_patch", "verifier_tail",
                "failure_kind", "failure_kind_text", "failure_type")})
            row["finished"] = True
            row["state"] = "error" if record["status"] == "error" else "done"
            row["state_text"] = SUBTASK_STATE_TEXT[row["state"]]
            row["result_text"] = {"resolved": "通过",
                                  "unresolved": "未通过"}.get(record["status"], "")
        elif path:
            started = _trial_started(path)
            row.update({
                "state": "running", "state_text": SUBTASK_STATE_TEXT["running"],
                "status": "running", "trial_dir": path,
                "trial": os.path.basename(path), "started_at": started,
                "elapsed_s": round(max(0.0, now - started), 1) if started else None,
            })
        if row["trial_dir"]:
            row["log_rel"] = trial_log_rel(row["trial_dir"])
            if row["log_rel"]:
                row["log_size"] = _file_size(os.path.join(row["trial_dir"], row["log_rel"]))
        rows.append(row)
    return rows


def _group(records, key):
    """按字段分组统计（通过率），供汇总展示。

    与整体通过率同一口径：环境/超时失败不计入分组分母。
    """
    out = {}
    for row in records:
        if is_excluded(row):
            continue
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


# ---------------------------------------------------------------- 容器回收
# pier 正常跑完会自己 `docker compose down --rmi all`；但被中断（点停止、SIGKILL、
# pier 崩溃、后端重启）时来不及清理，任务容器与 egress 代理会一直 Up，白占 CPU/内存/
# 网络。这里按 compose 的 project label 兜底回收：不依赖 compose 文件，也不再撤镜像
# tag（保留 tag 反而省掉下一轮的重新 pull）。
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_PROJECT_FMT = '|{{.Label "com.docker.compose.project"}}'

# 删除顺序固定为 容器 -> 网络 -> 卷：网络/卷被容器占用时删不掉。
# docker volume ls 没有 .ID 字段，卷要用 .Name（两者同值，但模板用错会渲染成空串）
_RECLAIM_STEPS = (
    ("container", ["docker", "ps", "-a"], ["docker", "rm", "-f"], "{{.ID}}"),
    ("network", ["docker", "network", "ls"], ["docker", "network", "rm"], "{{.ID}}"),
    ("volume", ["docker", "volume", "ls"], ["docker", "volume", "rm", "-f"], "{{.Name}}"),
)


def _docker(argv, timeout=120):
    """按当前 docker 权限模式（直连 / sg docker）执行命令，返回 (rc, 输出)。"""
    return _run(wrap_argv(argv, docker_state()["mode"]), timeout=timeout)


def compose_projects(job_dir):
    """pier job 目录下每个 trial 对应的 compose project 名（compose 会小写化名字）。"""
    try:
        names = os.listdir(job_dir)
    except OSError:
        return []
    return sorted({n.lower() for n in names if os.path.isdir(os.path.join(job_dir, n))})


def cleanup_projects(projects, on_event=None):
    """回收指定 compose project 的容器/网络/卷，返回各类删除数量。

    一次列出所有带 compose project label 的资源再按归属过滤，避免逐 project 反复调
    docker；正常跑完的 run 没有残留，调用退化为 3 次 list。
    """
    targets = {p for p in projects if p}
    counts = {"container": 0, "network": 0, "volume": 0}
    if not targets:
        return counts
    for kind, list_argv, remove_argv, fmt in _RECLAIM_STEPS:
        rc, out = _docker(list_argv + ["--filter", "label=" + _COMPOSE_PROJECT_LABEL,
                                       "--format", fmt + _PROJECT_FMT])
        if rc != 0:
            continue
        ids = []
        for line in out.splitlines():
            name, _, project = line.partition("|")
            if name.strip() and project.strip() in targets:
                ids.append(name.strip())
        if not ids:
            continue
        rc, err = _docker(remove_argv + ids)
        if rc == 0:
            counts[kind] = len(ids)
        elif on_event:
            on_event("task_log", {"msg": "[回收] {} 删除失败：{}".format(
                kind, _last_line(err))})
    if any(counts.values()) and on_event:
        on_event("task_log", {"msg": "[回收] 清理残留容器 {} 个、网络 {} 个、卷 {} 个".format(
            counts["container"], counts["network"], counts["volume"])})
    return counts


def reclaim_job_containers(job_dir, on_event=None):
    """回收本轮 run 的容器残留；pier 已自行 down 时是空操作。"""
    return cleanup_projects(compose_projects(job_dir), on_event)


def active_pier_jobs():
    """当前机器上正在运行的 pier job 名集合（pier 的 --job-name 即 run_id）。"""
    rc, out = _run(["ps", "-eo", "args"], timeout=10)
    if rc != 0:
        return set()
    jobs = set()
    for line in out.splitlines():
        if "pier" not in line or "--job-name" not in line:
            continue
        parts = line.split()
        for i, part in enumerate(parts):
            if part == "--job-name" and i + 1 < len(parts):
                jobs.add(parts[i + 1])
    return jobs


def reclaim_orphans(runs_dir, on_event=None):
    """回收历史 run 遗留的容器（服务启动时兜底）。

    判据是「机器上没有对应的 pier 进程」而不是快照状态：进程被 SIGKILL 时 meta.json
    可能仍写着 running，只看快照会永远跳过这些残留；反之正在跑的 run 在 ps 里一定能
    看到 --job-name，据此避免误清别的部署正在执行的任务。
    """
    counts = {"container": 0, "network": 0, "volume": 0}
    try:
        run_ids = os.listdir(runs_dir)
    except OSError:
        return counts
    running = active_pier_jobs()
    projects = []
    for run_id in run_ids:
        if run_id in running:
            continue
        pier_root = os.path.join(runs_dir, run_id, "pier")
        try:
            jobs = os.listdir(pier_root)
        except OSError:
            continue
        for job in jobs:
            projects.extend(compose_projects(os.path.join(pier_root, job)))
    return cleanup_projects(projects, on_event)


# ------------------------------------------------------------------ 磁盘回收
# 与上面容器回收的分工：reclaim_orphans 只管容器/网络/卷（pier 被中断时的残留），
# 这里管镜像层与 buildkit 缓存——那部分 pier 的 `compose down --rmi all` 只撤 tag，
# 层仍留在 /var/lib/containerd；而容器回收又刻意保留 tag 省 pull（见 clean_up 处注释），
# 于是 docker 占用只进不出。必须按占用阈值主动回收，否则系统盘会被批量评测占满。
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
# `image prune` 输出 "Total reclaimed space: 223.1GB"，`builder prune` 输出 "Total:\t241.9GB"，
# 两种尾部汇总都要能抓到，否则回收量恒为 0
_RECLAIMED_RE = re.compile(r"Total(?:\s+reclaimed\s+space)?:?\s*([0-9.]+[KMGT]?B)", re.I)
# docker 用 SI 单位显示（1GB=1000^3），这里按 1024 换算，对阈值判断只差几个百分点
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGT]?B)?\s*$", re.I)


def _parse_size(text):
    """把 docker 的 '223.2GB' / '175.2MB' / '0B' 解析成字节；解析不出返回 0。"""
    match = _SIZE_RE.match(text or "")
    if not match:
        return 0
    unit = (match.group(2) or "B").upper()
    return int(float(match.group(1)) * _SIZE_UNITS.get(unit, 1))


def _fmt_bytes(size):
    """人类可读体积：>=1GiB 用 GB，否则用 MB。"""
    size = max(0, int(size or 0))
    if size >= 1024 ** 3:
        return "{:.1f}GB".format(size / 1024 ** 3)
    return "{:.0f}MB".format(size / 1024 ** 2)


def docker_disk_usage():
    """docker 当前占用字节数（镜像 + 构建缓存）；docker 不可用时返回 0。

    只统计这两类：容器/卷由 reclaim_orphans 负责，且本地卷默认不占镜像层。
    """
    rc, out = _docker(["docker", "system", "df", "--format", "{{.Type}}|{{.Size}}"],
                      timeout=60)
    if rc != 0:
        return 0
    total = 0
    for line in out.splitlines():
        kind, _, size = line.partition("|")
        if kind.strip() in ("Images", "Build Cache"):
            total += _parse_size(size)
    return total


def docker_disk_limit():
    """回收阈值（字节）：环境变量可覆盖，0 表示关闭自动回收。"""
    raw = os.environ.get("LLM_SERVER_EVAL_DOCKER_DISK_LIMIT_GB")
    value = utils.float_or(raw, consts.DEFAULT_DOCKER_DISK_LIMIT_GB) \
        if raw is not None else consts.DEFAULT_DOCKER_DISK_LIMIT_GB
    return int(max(0.0, value) * 1024 ** 3)


def pier_uv_base_image():
    """pier 实际会注入的 uv-base 镜像名；pier 不注入时返回空串。

    「要不要维护 uv-base」必须由 pier 自己说了算，不能按本项目的偏好硬建：
      * 全新安装的官方 pier，其 agent_setup.py 里没有 uv-base，Dockerfile 不会有
        `COPY --from=<uv-base>`，它走自带的 curl 装 uv——此时维护 uv-base 只是白占
        175MB，并在网络不通时打出一条与评测无关的「重建失败」；
      * 打过本地补丁的 pier 才会注入，镜像被 prune 掉会让所有任务构建失败，只有这种
        情况才必须重建（补丁不随 pier 安装分发，见 install.sh 的提示）。
    判据直接读 pier 源码里有没有 uv-base，而不是记一个「我们打过」的标记：
    重装/升级 pier 后标记会失真，读源码不会。
    """
    binary = pier_bin()
    if not binary:
        return ""
    venv = os.path.dirname(os.path.dirname(os.path.realpath(binary)))
    pattern = os.path.join(venv, "lib", "python3.*", "site-packages",
                           "pier", "environments", "agent_setup.py")
    patched = False
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if "uv-base" in fh.read():
                    patched = True
                    break
        except OSError:
            continue
    if not patched:
        return ""                      # 官方 pier 不读这个环境变量，返回空即不维护
    # PIER_UV_BASE_IMAGE="" 是 pier 侧的关闭开关（回退到 curl 安装），这里同样认
    return os.environ.get("PIER_UV_BASE_IMAGE", consts.UV_BASE_IMAGE).strip()


def ensure_uv_base(on_event=None):
    """确认 pier 依赖的 uv-base 基础镜像存在，缺失就地重建（约 175MB，只走 PyPI）。

    打过补丁的 pier 生成的 Dockerfile 固定 `COPY --from=<uv-base>`，该镜像被 prune 掉
    会让所有任务构建失败。而 docker 的 prune 没有「排除指定镜像」的过滤器，所以回收后
    必须补回来。重建配方见 develop.md 2026-10-19 §F/§I。
    """
    emit = on_event or (lambda kind, payload=None: None)
    image = pier_uv_base_image()
    if not image:
        return True                    # pier 不注入 uv-base，无需维护
    refresh_images()
    if image in known_images():
        return True
    emit("task_log", {"msg": "[回收] 重建基础镜像 {} …".format(image)})
    # 镜像 tag 即 uv 版本（llm-server-eval/uv-base:0.7.13），自定义镜像也能对上
    version = image.rsplit(":", 1)[-1] if ":" in image else consts.UV_BASE_UV_VERSION
    tmp = tempfile.mkdtemp(prefix="uvbase-")
    try:
        rc, out = _run(["curl", "-sSL",
                        "https://pypi.org/pypi/uv/{}/json".format(version)], timeout=120)
        if rc != 0:
            emit("task_log", {"msg": "[回收] 重建失败：取不到 uv {} 版本清单".format(version)})
            return False
        try:
            urls = json.loads(out)["urls"]
            url = next(u["url"] for u in urls
                       if "manylinux" in u["filename"] and "x86_64" in u["filename"])
        except (ValueError, KeyError, StopIteration):
            emit("task_log", {"msg": "[回收] 重建失败：找不到 manylinux x86_64 的 uv wheel"})
            return False

        wheel = os.path.join(tmp, "uv.whl")
        rc, _ = _run(["curl", "-sSL", "-o", wheel, url], timeout=300)
        if rc != 0:
            emit("task_log", {"msg": "[回收] 重建失败：uv wheel 下载失败"})
            return False

        # 本机不一定有 unzip，直接用 zipfile 解出 uv 二进制
        binary = os.path.join(tmp, "uv")
        try:
            with zipfile.ZipFile(wheel) as zf:
                member = next(n for n in zf.namelist() if n.endswith("data/scripts/uv"))
                with zf.open(member) as src, open(binary, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.chmod(binary, 0o755)
        except (zipfile.BadZipFile, OSError, StopIteration) as exc:
            emit("task_log", {"msg": "[回收] 重建失败：解包 uv 失败 {}".format(exc)})
            return False

        with open(os.path.join(tmp, "Dockerfile"), "w") as fh:
            fh.write("FROM debian:bookworm-slim\nCOPY uv /uv\nRUN chmod 0755 /uv\n")
        rc, out = _docker(["docker", "build", "-t", image, tmp], timeout=900)
        if rc != 0:
            emit("task_log", {"msg": "[回收] 重建失败：{}".format(_last_line(out))})
            return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    refresh_images()
    emit("task_log", {"msg": "[回收] 基础镜像已就绪：{}".format(image)})
    return True


def reclaim_disk(on_event=None, force=False):
    """按占用阈值回收 docker 镜像层与 buildkit 缓存，返回回收摘要 dict。

    两个前置条件（都不满足就不动手）：
      * 占用超阈值才清——不然用户自己拉的镜像会被无故清空，阈值见
        consts.DEFAULT_DOCKER_DISK_LIMIT_GB / LLM_SERVER_EVAL_DOCKER_DISK_LIMIT_GB；
      * 没有 pier 任务在跑才清——prune -a 会删掉正在跑的任务镜像，等于把 run 打断。

    顺序固定为 构建缓存 -> 镜像：前者不动任何镜像，先清它往往就够了，还能在磁盘
    接近写满时先腾出重建 uv-base 的空间（磁盘满时连 docker build 都会失败）。
    """
    emit = on_event or (lambda kind, payload=None: None)
    result = {"ran": False, "reason": "", "before": 0, "after": 0,
              "builder": 0, "images": 0, "uv_base": None}
    docker = docker_state()
    if not docker["ok"]:
        result["reason"] = "docker 不可用"
        return result
    limit = docker_disk_limit()
    if limit <= 0:
        result["reason"] = "自动回收已关闭（阈值 0）"
        return result

    before = docker_disk_usage()
    result["before"] = before
    if not force and before <= limit:
        result["after"] = before
        result["reason"] = "占用 {} 未超阈值 {}".format(_fmt_bytes(before), _fmt_bytes(limit))
        return result
    if active_pier_jobs():
        result["after"] = before
        result["reason"] = "有 pier 任务在跑，跳过以免误删在用镜像"
        return result

    result["ran"] = True
    rc, out = _docker(["docker", "builder", "prune", "-a", "-f"], timeout=600)
    match = _RECLAIMED_RE.search(out) if rc == 0 else None
    result["builder"] = _parse_size(match.group(1)) if match else 0
    if rc == 0:
        emit("task_log", {"msg": "[回收] 清理构建缓存 {}".format(_fmt_bytes(result["builder"]))})

    if docker_disk_usage() > limit:
        rc, out = _docker(["docker", "image", "prune", "-a", "-f"], timeout=900)
        match = _RECLAIMED_RE.search(out) if rc == 0 else None
        result["images"] = _parse_size(match.group(1)) if match else 0
        if rc == 0:
            emit("task_log", {"msg": "[回收] 清理未使用镜像 {}".format(
                _fmt_bytes(result["images"]))})
        # uv-base 没有 prune 排除过滤器，只能清完补回来（pier 不注入时是空操作）
        result["uv_base"] = ensure_uv_base(on_event)

    result["after"] = docker_disk_usage()
    emit("task_log", {"msg": "[回收] docker 占用 {} → {}".format(
        _fmt_bytes(before), _fmt_bytes(result["after"]))})
    return result


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

    refresh_images()                  # 上一轮 run 可能刚把镜像 tag 撤掉，这里取实时快照
    directory, catalogue, selected = resolve_selection(cfg)
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
    # 拉镜像前先按阈值回收：批量评测会把镜像层与 buildkit 缓存堆到占满系统盘，
    # 满了以后 docker pull / build 都会失败，整轮 run 会退化成「环境错误」
    reclaim_disk(on_event=on_event)
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
                               "LLM_SERVER_EVAL_IMAGE_MIRRORS 指定可用的镜像加速器。".format(
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
        # 兜底回收：pier 被中断/崩溃时不会自己 down，容器与网络会一直残留
        try:
            reclaim_job_containers(job_dir, on_event)
        except Exception:            # 回收失败不能影响任务收尾
            pass

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
    # 通过率只按「计分任务」算：环境/超时失败的题目不进分母（见 EXCLUDED_FAILURE_KINDS）
    score = scoring(items)
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
        "num_scored": score["num_scored"],
        "num_excluded": score["num_excluded"],
        "pass_at_1": score["pass_at_1"],
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
        "by_failure_kind": failure_kind_counts(items),
        "stopped": stopped,
        "raw": os.path.relpath(jobs_dir, consts.PROJECT_ROOT),
    }
    with open(out + ".items.jsonl", "w", encoding="utf-8") as fh:
        for row in items:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(out + ".json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    on_event("agent_end", {"done": done, "errors": error_n, "resolved": resolved_n,
                           "excluded": score["num_excluded"],
                           "elapsed_s": round(elapsed, 1), "stopped": stopped})
    on_event("summary", {"summary": summary, "records": len(items)})
    return summary
