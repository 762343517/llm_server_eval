# develop.log — 踩坑与特异点回溯

记录**不显然、反直觉、易复发**的事实，供以后回溯。常规代码改动看 git，不在这里重复。

---

## 2026-10-19 · mini-swe-agent 批量评测跑不通（0 通过 / 0 步 / 0 token）

现象：一次 run 共 2 个任务，`status=succeeded` 但两个任务全 error、耗时 8.6 分钟。
两个失败点完全不同，逐条记录其"特异点"。

### A. 网络约束（本机事实，决定了所有方案边界）

| 目标 | 结果 |
|---|---|
| `astral.sh` | 200，但**301 重定向到 `github.com/.../releases` → 超时** |
| `raw.githubusercontent.com` | 超时 |
| `github.com` | 超时 |
| `pypi.org` / `files.pythonhosted.org` | 200（约 0.9s） |
| dockerd 代理 | **未配置**，只有 registry-mirrors |

> **结论**：凡是安装步骤要访问 GitHub 的（uv 的 install.sh、python-build-standalone、litellm 价目表），必然失败；
> 能走 PyPI 的都能通。**不要把"astral.sh 可达"当成 uv 能装。**

### B. pier 的行为特异点

1. **Dockerfile 形状是固定的，不能靠换 `FROM` 预置任何东西。** 每个任务的形状都是
   `FROM <该任务镜像> → ARG PIER_AGENT_INSTALL_FINGERPRINT → USER root → RUN apt-get ... → USER <镜像用户> → RUN 安装 agent`。
   只能往中间**注入一层**（`COPY --from`）。
2. **每个任务的 `FROM` 都不同 → 后面所有层的 cache key 都不同 → buildkit 缓存永远不命中。**
   所以"反复下载 uv"不是偶发，而是任务数 ×1 的必然放大（113 个任务 = 下载 113 次）。
3. **`RUN` 用 exec form `["/bin/bash","-c",...]`**，而任务镜像默认 shell 可能是 dash。
   手写验证用 Dockerfile 时必须加 `SHELL ["/bin/bash","-c"]`，否则报
   `set: Illegal option -o pipefail`（这是验证脚本的坑，不是 pier 的坑）。
4. **`--ak key=value` 是官方注入点**，值经 `parse_kwargs` 解析（JSON / literal_eval）。
   列表**必须写成 JSON 字面量**：`--ak 'extra_python_packages=["mcp>=2.2.0,<3"]'`。
5. **`status=succeeded` 只代表 pier 进程正常退出，不代表任务成功。**
   一定要看 `out.items.jsonl` 的 `status` / `resolved` / `steps` / `tokens`。

### C. uv 的特异点

1. **PyPI 的 uv wheel 里就带预编译二进制**：`uv-0.7.13.data/scripts/uv`（43.6 MB），
   可以完全绕开 GitHub 的 install.sh。
2. **这个二进制是动态链接的**（`interpreter /lib64/ld-linux-x86-64.so.2`，依赖 `libgcc_s`、`libc`）。
   所以 `FROM scratch` 里 **跑不起来**（`exec /uv: no such file or directory`），
   `uv-base` 必须基于带 glibc 的最小镜像（用 `debian:bookworm-slim`）。
3. **`COPY` 到已存在的软链会跟随软链写入。** 实测某任务镜像里
   `/usr/local/bin/uv -> /root/.local/bin/uv`，`COPY ... /usr/local/bin/uv` 写进了**软链目标**；
   把目标文件删掉后该路径变成悬空链。
   → 改用 `/opt/uv/bin/uv`，与镜像里任何既有路径都不冲突。
4. **预置 uv 会连带打断两个隐含依赖**（原脚本靠 `install.sh` 生成的文件）：
   - `source "$HOME/.local/bin/env"`：预置时该文件不存在，`set -euo pipefail` 下直接失败 → 必须加 `[ -f ]` 保护；
   - `uv tool install` 把可执行文件装到 `$HOME/.local/bin`，该目录**不在 PATH** →
     `command -v mini-swe-agent` 为空 → `head -n 1 ""` 报错（`head: cannot open '' for reading`）→
     必须无条件 `export PATH="$HOME/.local/bin:$PATH"`。

### D. litellm 的特异点

1. **litellm 1.106.0 打包缺陷**：把 `mcp` 声明成 extra（`mcp<3,>=2.2.0; extra == "mcp"`，默认不装），
   却在 `litellm/responses/mcp/chat_completions_handler.py` 里**无条件 import**，
   触发条件是 `if not skip_mcp_handler and tools:`。
   mini-swe-agent 必然带 tools → **第一次模型调用前就 `ModuleNotFoundError: No module named 'mcp'`**。
2. **mini-swe-agent 的依赖声明只有下界**（`litellm!=1.82.7,!=1.82.8,>=1.75.5`），
   `uv tool install` 会拉到最新版 → **行为随时间漂移**（10/16 还能跑，三天后全崩）。
   → 必须用 `--ak version=2.4.6` 锚定。
3. litellm import 时会去 `raw.githubusercontent.com` 拉价目表，超时 3 次后回退；
   设 `LITELLM_LOCAL_MODEL_COST_MAP=true` 可消除，**该开关在 1.106 仍然有效**（已实测）。

### E. 工具链小坑

- 新版 docker 的 `docker images <repo>` 位置参数过滤**失效**（返回空，容易误判成镜像被删）。
  用 `docker images --format '{{.Repository}}:{{.Tag}}' | grep xxx`。
- `docker rmi` 一个不存在的 tag 时，若前面几个删成功、后面报错，整条命令返回非 0，不要据此判断"没删掉"。

### F. 已落地的改动（2026-10-19）

| 位置 | 内容 |
|---|---|
| `backend/agent_core.py` | 新增 `MINI_SWE_AGENT_VERSION = "2.4.6"`、`MINI_SWE_AGENT_EXTRA_PACKAGES = ["mcp>=2.2.0,<3"]`；`build_command` 对 mini-swe-agent 追加两个 `--ak` |
| pier `environments/agent_setup.py` | 新增 `DEFAULT_UV_BASE_IMAGE`；`write_agent_dockerfile` 在 `FROM` 后插入 `COPY --from=zhipu-eval/uv-base:0.7.13 /uv /opt/uv/bin/uv`；可用 `PIER_UV_BASE_IMAGE=""` 关闭 |
| pier `agents/installed/mini_swe_agent.py` | uv 安装改幂等（预置存在则跳过 curl）；`/opt/uv/bin` 加 PATH；`$HOME/.local/bin` 无条件加 PATH；`source env` 加 `[ -f ]` 保护 |
| 镜像 | `zhipu-eval/uv-base:0.7.13`（debian-slim + uv 0.7.13，175 MB） |

### G. 验证记录

- 用 `python:3.12-slim`（自带 Python、**没有 uv**，即 `adaptive-rejection-sampler` 那类镜像）跑 **pier 真实生成**的 Dockerfile：
  - 预置文件存在且可执行：`/opt/uv/bin/uv`，`uv 0.7.13`；
  - 构建期**不访问 GitHub**（`command -v uv` 命中即跳过 curl）；
  - `uv tool install mini-swe-agent==2.4.6` → 4 个可执行文件；
  - `uv pip install mcp>=2.2.0,<3` → 成功；
  - `litellm 1.106.0 + mcp 2.3.0` 导入成功；
  - `mini-swe-agent --help` 通过。
- 对比：预置前该层 `curl` 卡 131 秒后失败；预置后整层 25 秒完成。

### H. 风险与待办

1. **`uv-base` 被 prune 掉会导致构建直接失败**（`COPY --from` 拉不到）。重建命令见下。
2. **`agent_core.py` 的改动要重启后端才生效**；pier 是子进程，改动下次 `pier run` 自动生效。
3. **pier 改动在 site-packages**，`uv tool upgrade datacurve-pier` 会覆盖；原文件已备份为同目录 `.orig`。
   注意 `agent_setup.py` 原本还有一处 egress 端口放宽（`acl Safe_ports port 80 443` → `80 443 3000-65535`），
   重打 patch 时**不要覆盖**它。

重建 uv-base：

```bash
curl -sSL "https://pypi.org/pypi/uv/0.7.13/json" \
  | python3 -c "import json,sys;print([u['url'] for u in json.load(sys.stdin)['urls'] if 'manylinux' in u['filename'] and 'x86_64' in u['filename']][0])" \
  | xargs curl -sSL -o /tmp/uv.whl
unzip -o -j /tmp/uv.whl "uv-0.7.13.data/scripts/uv" -d /tmp/uvbase/
printf 'FROM debian:bookworm-slim\nCOPY uv /uv\nRUN chmod 0755 /uv\n' > /tmp/uvbase/Dockerfile
docker build -t zhipu-eval/uv-base:0.7.13 /tmp/uvbase
```
