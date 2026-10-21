# 开发文档

收录不显然、易复发的事实、环境约束与运维操作。常规代码改动见 git 提交历史，不在此重复。

**背景**：2026-10-19 的一次 run 含 2 个任务，`status=succeeded` 但两个任务均为 error，耗时 8.6 分钟。两个任务的失败点互不相同，根因分布在网络约束、pier 行为、uv 安装链、litellm 依赖四处，分节记录。

## 1. 环境约束

### 1.1 网络可达性（2026-10-19 实测）

| 目标 | 结果 |
|---|---|
| `astral.sh` | 200，301 重定向到 `github.com/.../releases` 后超时 |
| `raw.githubusercontent.com` | 超时 |
| `github.com` | 超时 |
| `pypi.org` / `files.pythonhosted.org` | 200（约 0.9s） |
| dockerd 代理 | 未配置，仅有 `registry-mirrors` |

安装步骤中依赖 GitHub 的环节（uv 的 `install.sh`、python-build-standalone、litellm 价目表）不可用，仅 PyPI 可达。`astral.sh` 返回 200 但重定向到 GitHub，不能作为「uv 可安装」的判据。

### 1.2 docker CLI 行为

- `docker images <repo>` 的位置参数过滤在新版 docker 中失效，返回空列表，不能据此判断镜像不存在。改用 `docker images --format '{{.Repository}}:{{.Tag}}' | grep xxx`。
- `docker rmi` 批量删除时，若前面成功、后面报错，整条命令返回非 0，不能据此判断删除失败。

## 2. pier 行为约定

1. **Dockerfile 形状固定**，无法通过替换 `FROM` 预置内容：
   `FROM <该任务镜像> → ARG PIER_AGENT_INSTALL_FINGERPRINT → USER root → RUN apt-get ... → USER <镜像用户> → RUN 安装 agent`。
   只能在中间注入一层（`COPY --from`）。
2. **每个任务的 `FROM` 都不同**，后续所有层的 cache key 随之不同，buildkit 缓存不命中。uv 下载次数与任务数相同（113 个任务 = 下载 113 次）。
3. **`RUN` 使用 exec form `["/bin/bash","-c",...]`**，任务镜像的默认 shell 可能是 dash。手写验证用的 Dockerfile 需加 `SHELL ["/bin/bash","-c"]`，否则报 `set: Illegal option -o pipefail`。
4. **`--ak key=value` 是官方注入点**，值经 `parse_kwargs` 解析（JSON / literal_eval）。列表必须写成 JSON 字面量：`--ak 'extra_python_packages=["mcp>=2.2.0,<3"]'`。
5. **`status=succeeded` 仅表示 pier 进程正常退出**，不代表任务成功。任务结果以 `out.items.jsonl` 的 `status` / `resolved` / `steps` / `tokens` 为准。

## 3. Agent 安装链

### 3.1 uv 预置（uv-base 镜像）

1. PyPI 的 uv wheel 内含预编译二进制 `uv-0.7.13.data/scripts/uv`（43.6 MB），可绕开 GitHub 上的 `install.sh`。
2. 该二进制为动态链接（`interpreter /lib64/ld-linux-x86-64.so.2`，依赖 `libgcc_s`、`libc`），在 `FROM scratch` 中无法执行（`exec /uv: no such file or directory`）。`uv-base` 必须基于带 glibc 的最小镜像，当前使用 `debian:bookworm-slim`。
3. `COPY` 到已存在的软链会跟随软链写入。实测某任务镜像内 `/usr/local/bin/uv -> /root/.local/bin/uv`，`COPY ... /usr/local/bin/uv` 写入的是软链目标；删除目标文件后该路径变为悬空链。当前统一使用 `/opt/uv/bin/uv`，与镜像内既有路径不冲突。
4. 预置 uv 后，`install.sh` 不再执行，其生成的两个文件需在构建期补偿：
   - `$HOME/.local/bin/env`：原脚本会 `source "$HOME/.local/bin/env"`，文件缺失时在 `set -euo pipefail` 下直接失败，需加 `[ -f ]` 保护；
   - `$HOME/.local/bin` 不在 PATH：`uv tool install` 将可执行文件装到该目录，`command -v mini-swe-agent` 为空，`head -n 1 ""` 报 `head: cannot open '' for reading`，需无条件 `export PATH="$HOME/.local/bin:$PATH"`。

### 3.2 运行期 PATH

构建期修复不足以让 agent 启动，运行期仍会失败：

- 现象：镜像构建正常，agent 启动即 `bash: /root/.local/bin/env: No such file or directory` 与 `mini-swe-agent: command not found`（exit 127，0 步 0 token，verifier 因未改动而全部 fail）。
- 原因：
  - 运行期命令由 pier 拼接，以 `. "$HOME/.local/bin/env"` 开头给 shell 加 PATH，预置 uv 的路径下该文件从未生成；
  - 构建期的 `export PATH` 只作用于构建 shell，运行期另起 shell 不继承；
  - `.` 失败不中断（行内以 `;` 连接），最终报错落在 `mini-swe-agent: command not found`；
  - 仅镜像内本就存在该 env 文件的任务（如 abs-* 系列）可跑通，表现为部分任务全 0 步。
- 修复（pier `agents/installed/mini_swe_agent.py`）：构建期补建 `$HOME/.local/bin/env`；运行命令与 `get_version_command` 前置 `export PATH="$HOME/.local/bin:$PATH"; [ -f env ] && . env`；版本检测的 PATH 追加 `/opt/uv/bin`。安装步骤变化会改变指纹，旧镜像自动失效。

### 3.3 litellm 与 mini-swe-agent 版本

1. **litellm 1.106.0 打包缺陷**：`mcp` 声明为 extra（`mcp<3,>=2.2.0; extra == "mcp"`，默认不安装），但 `litellm/responses/mcp/chat_completions_handler.py` 中无条件 import，触发条件为 `if not skip_mcp_handler and tools:`。mini-swe-agent 必然携带 tools，首次模型调用前即 `ModuleNotFoundError: No module named 'mcp'`。
2. **mini-swe-agent 的依赖声明只有下界**（`litellm!=1.82.7,!=1.82.8,>=1.75.5`），`uv tool install` 会取最新版，行为随时间漂移（10/16 可用，三天后全部失败）。用 `--ak version=2.4.6` 锁定版本。
3. litellm 导入时会从 `raw.githubusercontent.com` 拉取价目表，超时 3 次后回退。设 `LITELLM_LOCAL_MODEL_COST_MAP=true` 可消除该请求，该开关在 1.106 实测有效。

## 4. 已落地改动（2026-10-19）

| 位置 | 内容 |
|---|---|
| `backend/agent_core.py` | 新增 `MINI_SWE_AGENT_VERSION = "2.4.6"`、`MINI_SWE_AGENT_EXTRA_PACKAGES = ["mcp>=2.2.0,<3"]`；`build_command` 对 mini-swe-agent 追加两个 `--ak` |
| pier `environments/agent_setup.py` | 新增 `DEFAULT_UV_BASE_IMAGE`；`write_agent_dockerfile` 在 `FROM` 后插入 `COPY --from=llm-server-eval/uv-base:0.7.13 /uv /opt/uv/bin/uv`；可用 `PIER_UV_BASE_IMAGE=""` 关闭 |
| pier `agents/installed/mini_swe_agent.py` | uv 安装改幂等（预置存在则跳过 curl）；`/opt/uv/bin` 加入 PATH；`$HOME/.local/bin` 无条件加入 PATH；构建期补建 `$HOME/.local/bin/env`；运行期命令与版本检测前置 `export PATH` 与 `[ -f env ] && . env` |
| 镜像 | `llm-server-eval/uv-base:0.7.13`（debian-slim + uv 0.7.13，175 MB） |

## 5. 验证记录

用 `python:3.12-slim`（自带 Python、无 uv，对应 `adaptive-rejection-sampler` 一类镜像）执行 pier 真实生成的 Dockerfile：

- 预置文件存在且可执行：`/opt/uv/bin/uv`，`uv 0.7.13`；
- 构建期不访问 GitHub（`command -v uv` 命中即跳过 curl）；
- `uv tool install mini-swe-agent==2.4.6` 产出 4 个可执行文件；
- `uv pip install mcp>=2.2.0,<3` 成功；
- `litellm 1.106.0 + mcp 2.3.0` 导入成功；
- `mini-swe-agent --help` 通过。

对比：预置前该层 `curl` 阻塞 131 秒后失败，预置后整层 25 秒完成。

运行期验证：真实任务镜像内 `. "$HOME/.local/bin/env"; mini-swe-agent --help` 输出版本 2.4.6；pier 单任务实跑，agent 首个 tool call 正常执行。

## 6. 运维

### 6.1 uv-base 重建

`uv-base` 被 prune 后 `COPY --from` 无法解析，所有任务构建失败。重建命令：

```bash
curl -sSL "https://pypi.org/pypi/uv/0.7.13/json" \
  | python3 -c "import json,sys;print([u['url'] for u in json.load(sys.stdin)['urls'] if 'manylinux' in u['filename'] and 'x86_64' in u['filename']][0])" \
  | xargs curl -sSL -o /tmp/uv.whl
unzip -o -j /tmp/uv.whl "uv-0.7.13.data/scripts/uv" -d /tmp/uvbase/
printf 'FROM debian:bookworm-slim\nCOPY uv /uv\nRUN chmod 0755 /uv\n' > /tmp/uvbase/Dockerfile
docker build -t llm-server-eval/uv-base:0.7.13 /tmp/uvbase
```

服务侧的自动回收会调用 `ensure_uv_base()` 补回该镜像，等价于以上步骤（wheel 解包用 `zipfile`，无需系统 unzip）。

### 6.2 pier 补丁维护

pier 的改动落在 site-packages，`uv tool upgrade datacurve-pier` 会覆盖；原文件备份为同目录 `.orig`。

`agent_setup.py` 另有一处 egress 端口放宽（`acl Safe_ports port 80 443` → `80 443 3000-65535`），重打 patch 时不得覆盖。

### 6.3 生效范围

| 改动位置 | 生效方式 |
|---|---|
| `backend/*.py` | 重启后端服务 |
| pier（site-packages） | 下次 `pier run` 自动生效（子进程） |
| `backend/webui/` 前端产物 | 重新构建后刷新浏览器 |
