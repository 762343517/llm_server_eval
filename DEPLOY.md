# 部署手册与故障排查

面向运维/交付：从零把「大模型服务精度与性能测试框架」部署到一台 Linux 机器并通过验收；
出问题按第 8 章的症状表定位。

- 功能与计分口径见 `README.md`；
- 不显然的环境约束、pier/uv 链路的坑与已落地改动见 `develop.md`（下文标「见 develop」）；
- 密钥不在此重复，默认端点/密钥集中在 `backend/consts.py`，分发前务必替换。

## 目录

1. [部署形态与组成](#1-部署形态与组成)
2. [环境要求](#2-环境要求)
3. [方式 A：源码部署](#3-方式-a源码部署)
4. [方式 B：单文件免安装包](#4-方式-b单文件免安装包)
5. [可选组件：压测与 Agent 评测](#5-可选组件压测与-agent-评测)
6. [运行配置](#6-运行配置)
7. [上线验收清单](#7-上线验收清单)
8. [故障排查](#8-故障排查)
9. [磁盘规划与容量治理](#9-磁盘规划与容量治理)
10. [日常运维](#10-日常运维)
11. [附录：命令与环境变量](#11-附录命令与环境变量)

---

## 1. 部署形态与组成

```
浏览器 ──HTTP──> backend/api.py (FastAPI, 默认 0.0.0.0:8000)
                   ├── 托管前端产物 backend/webui/（Vue3 + Vite SPA）
                   ├── /api/*  REST 接口
                   ├── 精度测试：直连被测/裁判模型的 chat/completions
                   ├── 压测：子进程调用 evalscope perf（不 import 其包）
                   └── Agent 评测：子进程调用 pier，pier 再拉 docker 沙箱
```

| 组成 | 必需 | 缺失时表现 |
| --- | --- | --- |
| Python 3.10+ 后端 + `backend/webui/` | 是 | 服务起不来 / 页面 404 |
| `evalscope[perf]` | 否 | 压测页提示不可用，启动按钮禁用 |
| `pier` + `docker` + `uv-base` 镜像 | 否 | Agent 评测页提示未就绪 |
| `dataset/` + `dataset/datasets.json` | 精度/Agent | 下拉框为空，CLI 报数据集不存在 |
| `model/` 分词器目录 | 长序列压测 | 长序列模式无法选分词器 |

三类任务共用**一条串行队列**（`backend/task_queue.py`）：同一时刻只有一个任务真正执行，
排队中状态为「排队中」（快照里是 `queued`），前序任务结束后间隔 5 秒启动下一个。
队列只在内存：服务重启后排队任务统一收敛为「已中断」。

## 2. 环境要求

### 2.1 硬件

| 场景 | 最小 | 建议 |
| --- | --- | --- |
| 精度测试（几百题） | 2 核 4G | 4 核 8G |
| 压力测试 | 2 核 4G、与被测服务同网段 | 4 核 8G，不与被测服务抢 CPU |
| Agent 评测 | 4 核 8G + 100G 空闲盘 | **1T 以上独立数据盘**（见第 9 章） |

### 2.2 软件

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| OS | Linux x86_64 | 已在 Ubuntu 22.04 / glibc 2.35 验证 |
| Python | 3.10+ | Ubuntu 另需 `python3-venv` |
| Node.js | 18+（Vite 6） | 仅构建前端时需要 |
| Docker | daemon 可用 | 仅 Agent 评测；用户在 `docker` 组或可 `sudo sg docker` |

依赖版本已锁：`fastapi==0.143.0`、`uvicorn[standard]==0.54.0`、`pandas==2.3.3`、
`pyarrow==25.0.1`、`tiktoken==0.14.0`、`pyyaml>=6.0`、`requests>=2.25.1`。

### 2.3 网络约束（实测，见 develop §1.1）

| 目标 | 可达性 |
| --- | --- |
| `pypi.org` / `files.pythonhosted.org` | 通（约 0.9s） |
| `github.com` / `raw.githubusercontent.com` | **超时** |
| `astral.sh` | 200，但 301 到 GitHub 后超时——**不能**当「uv 可装」依据 |
| Docker Hub | 直连不通，靠 `registry-mirrors` / `LLM_SERVER_EVAL_IMAGE_MIRRORS` |

所以所有依赖 GitHub 的安装路径都要绕开：uv 走 PyPI wheel、litellm 关远程价目表、
任务镜像走加速器。`install.sh` / `start.sh` 已默认 `LITELLM_LOCAL_MODEL_COST_MAP=true`。

## 3. 方式 A：源码部署

### 3.1 代码与数据

```bash
git clone <repo> llm_server_eval && cd llm_server_eval
```

`dataset/` 与 `model/` 的数据文件不入版本库，需单独投放：

- 数据集：每个数据集一个顶级目录，**目录名 = `dataset/datasets.json` 里的 `name`**；
- 分词器：整个模型目录放进 `model/`（含 `tokenizer.json` 或 `tokenizer_config.json`）。

缺数据时服务能启动，只是下拉框为空 / 长序列模式不可用。

### 3.2 安装

```bash
./install.sh                 # 建 .venv + 后端依赖 + 构建前端
./install.sh --all           # 再加 evalscope[perf] 与 Agent 依赖
./install.sh --dry-run       # 只打印命令，适合先在客户机评估
```

| 参数 | 作用 |
| --- | --- |
| `--all` / `--perf` / `--agent` | 追加压测 / Agent 依赖 |
| `--no-frontend` | 跳过前端构建（已有 `backend/webui/index.html`） |
| `--venv PATH` / `--python PATH` | 自定义虚拟环境与解释器 |
| `--dry-run` | 只打印将执行的命令 |

流程：校验 Python/Node → 建 `.venv`（复用已有则跳过）→ `pip install -r requirements.txt`
→（可选）装 evalscope、uv、pier、检查 docker 与 `uv-base` → `npm ci && npm run build`
输出到 `backend/webui/` → 自检。常见失败：缺 `python3-venv`、Node < 18、
`backend/webui` 无权限清空（脚本会退回覆盖构建）。

### 3.3 启动

```bash
./start.sh                     # 前台，0.0.0.0:8000
PORT=8010 ./start.sh           # 或 ./start.sh --port 8010
./start.sh --daemon            # 后台：PID runs/server.pid，日志 runs/server.log
./start.sh --status            # 状态 + /api/meta 探活
./start.sh --stop              # 停止后台服务
./start.sh --check             # 只做启动前检查
./start.sh --reload            # 开发态热重载
```

启动前检查：Python 依赖、`backend/webui/index.html`、端口占用——任一 ✗ 拒绝启动；
pier/docker/evalscope 缺失只告警。`--daemon` 最多等 30s 探活 `/api/meta`，失败打印日志尾部并清理进程。

后端启动会打印三行自检，是第一手排障依据：

```
前端产物: /path/backend/webui (已构建)
evalscope: 可用 0.x.x (/path/.venv/bin/evalscope)
agent 评测: pier 1.x.x docker 29.9.1 egress patched
服务地址: http://127.0.0.1:8000
```

### 3.4 systemd 托管（长期部署推荐）

```ini
# /etc/systemd/system/llm-server-eval.service
[Unit]
Description=LLM Server Eval
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=sre
WorkingDirectory=/opt/llm_server_eval
Environment=HOST=0.0.0.0
Environment=PORT=8000
Environment=LLM_SERVER_EVAL_TZ=Asia/Shanghai
Environment=LITELLM_LOCAL_MODEL_COST_MAP=true
Environment=PATH=/opt/llm_server_eval/.venv/bin:/home/sre/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/llm_server_eval/.venv/bin/python backend/api.py --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

要点：直接用 `.venv/bin/python` 而不是 `start.sh`（避免 PID 文件机制冲突）；
`PATH` 必须含 `.venv/bin` 与 `~/.local/bin`（evalscope / pier 在那），否则页面误报不可用；
Agent 评测需 `sudo usermod -aG docker sre && newgrp docker`，否则后端退回 `sg docker`；
`TimeoutStopSec` 不小于 30s，停止时要终止压测进程组与 pier 容器。

## 4. 方式 B：单文件免安装包

适合交付给没有 Python/Node 的机器。

```bash
./packaging/build.sh               # 前端构建 + PyInstaller + 组装 + tar.gz
./packaging/build.sh --no-data     # 只打包程序
./packaging/build.sh --skip-web    # 复用已有 backend/webui
./packaging/build.sh --version 1.2 # 自定义版本号
```

产物 `dist/llm-server-eval-linux-x86_64-<日期>.tar.gz`，解压后：

```
llm-server-eval-<版本>/
├── llm-server-eval   可执行文件（内置运行时，勿删）
├── start.sh
├── dataset/          清单 dataset/datasets.json
├── model/            分词器候选
└── runs/             运行产物，必须可写
```

```bash
tar -xzf llm-server-eval-linux-x86_64-<日期>.tar.gz && cd llm-server-eval-<日期>
./start.sh                                 # 默认 0.0.0.0:8000
HOST=127.0.0.1 PORT=8080 ./start.sh
APP_ROOT=/data/llm-server-eval ./start.sh   # 数据放别处
```

约束：目标机 glibc ≥ 2.35；`dataset/`、`model/`、`runs/` 与可执行文件同级（或用 `APP_ROOT`）；
路径基准由 `backend/runtime_paths.py` 统一提供，启动时 CWD 不影响落盘位置；
升级 = 覆盖可执行文件，数据目录不动；新版改了清单需同步 `dataset/datasets.json`。

## 5. 可选组件：压测与 Agent 评测

### 5.1 evalscope（压测）

```bash
pip install 'evalscope[perf]'
export EVALSCOPE_BIN=/path/to/evalscope     # 非标准位置时
```

定位顺序：`EVALSCOPE_BIN` → `PATH` → 当前解释器同目录 → `/usr/local/bin` 等，
全未命中退化为 `<解释器> -m evalscope`。探测结果缓存 60s，页面顶部展示版本。
刻意不 import 其 Python 包（避免 modelscope/transformers 重依赖），
所以「`import evalscope` 失败」不等于压测不可用，以可执行文件 `--version` 为准。

### 5.2 pier + docker（Agent 评测）

```bash
./install.sh --agent        # 检查 uv / pier / docker / uv-base 镜像
```

手工等价：`pip install uv`（走 PyPI，**不要**用 astral.sh 的 install.sh）→
`uv tool install datacurve-pier`（可执行文件落 `~/.local/bin`）→ `docker info`。

三项本地适配**不随 `uv tool install` 自动应用**（见 develop §4）：

1. **pier 补丁**：落在 site-packages（`environments/agent_setup.py`、
   `agents/installed/mini_swe_agent.py`），原文件备份为同目录 `.orig`。
   `uv tool upgrade datacurve-pier` 会覆盖，升级后需重打；
   重打时**不得丢失** `agent_setup.py` 的 egress 端口放宽（`acl Safe_ports 80 443 3000-65535`）。
2. **uv-base 镜像** `llm-server-eval/uv-base:0.7.13`：pier 生成的 Dockerfile 用
   `COPY --from` 预置 uv，缺失会让所有任务构建失败；重建见 8.3。
   `PIER_UV_BASE_IMAGE=""` 可关闭预置（内网基本不可行）。
3. **版本锁定**：mini-swe-agent 依赖只有下界会漂移，`agent_core.py` 已固定
   `MINI_SWE_AGENT_VERSION = "2.4.6"` 与 `MINI_SWE_AGENT_EXTRA_PACKAGES = ["mcp>=2.2.0,<3"]`。

镜像拉取并发默认 8（`LLM_SERVER_EVAL_IMAGE_PULL_CONCURRENCY`，上限 16，1 为串行）：
串行 65 个镜像实测 21 分钟，8 并发约 3 分钟。

## 6. 运行配置

### 6.1 端点与密钥

| 用途 | 命令行 | Web 表单 | 默认值 |
| --- | --- | --- | --- |
| 被测模型 | `--endpoint` / `--api-key` | 「被测模型」组 | `consts.DEFAULT_*` |
| 裁判模型 | `--judge-endpoint` / `--judge-api-key` | 「裁判模型」组（留空复用被测） | `consts.DEFAULT_JUDGE_*` |
| 压测目标 | 表单独立配置 | 「压力测试」页 | `consts.DEFAULT_STRESS_*` |

切换服务统一改 `backend/consts.py`；页面提交过的配置会覆盖默认值（见 6.4），
删掉对应 last_config 文件即回到内置默认。

### 6.2 思考开关

被测与裁判独立三态：不传 / 开启（可带 `low|medium|high|max`）/ 关闭，请求体附加
`{"thinking": {"type": "enabled"|"disabled"}, "reasoning_effort": "<level>"}`。
关闭时 `reasoning_tokens_avg` 趋于 0，也不再触发 `--max-tokens` 截断翻倍重试。

### 6.3 时区

起止时间、任务日志、任务目录名（run_id）统一按 `LLM_SERVER_EVAL_TZ` 渲染，默认
`Asia/Shanghai`。容器里跑 UTC 时**必须**确认该项，否则页面时间差 8 小时；
`local` 表示沿用系统时区。历史任务无需迁移（由落盘 epoch 重算）。

### 6.4 配置记忆与文件权限

| 文件 | 内容 | 权限 |
| --- | --- | --- |
| `runs/.last_config.json` | 精度测试上次表单 | 600（含密钥） |
| `runs/.last_stress_config.json` | 压测上次表单 | 600（含密钥） |
| `runs/stress/<id>/args.json` | 完整命令行参数 | 600（页面展示脱敏） |
| `runs/stress/<id>/proc.json` | 压测进程组号，收尾删除 | 600 |

`runs/` 必须服务用户可写，且不要放在会被清理的临时目录。

### 6.5 前端

```bash
npm --prefix frontend install
npm --prefix frontend run build      # 产物 -> backend/webui/
npm --prefix frontend run dev        # :5173，/api 代理到 :8000
```

改前端必须重新 build，后端只托管 `backend/webui/`（浏览器强刷）。

## 7. 上线验收清单

```bash
# 1) 接口活着
curl -sf http://127.0.0.1:8000/api/meta | head -c 200
# 2) 前端产物被托管
curl -sf http://127.0.0.1:8000/ | grep -q '<div id="app"' && echo 前端OK
# 3) 数据集清单解析（含 runnable / agent 段）
curl -sf http://127.0.0.1:8000/api/meta | python3 -c "import json,sys;d=json.load(sys.stdin);print([x['name'] for x in d['datasets']])"
# 4) 被测模型连通（换成实际 ENDPOINT/KEY/MODEL）
curl -s -X POST "$ENDPOINT" -H "Content-Type: application/json" -H "Authorization: Bearer $KEY" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}"
# 5) 压测引擎（可选）
evalscope --version
# 6) Agent 就绪状态（可选）
curl -sf http://127.0.0.1:8000/api/agent/env | python3 -m json.tool | head -40
# 7) 队列
curl -sf http://127.0.0.1:8000/api/queue
# 8) 磁盘余量（Agent 评测前必看）
df -h / /var/lib/docker 2>/dev/null
```

功能冒烟（各跑一次最小任务）：

| 功能 | 最小配置 | 期望 |
| --- | --- | --- |
| 精度测试 | SimpleQA 抽 2 题，并发 2 | 有逐题记录，汇总出 Pass@1 |
| 压力测试 | 固定并发 2，每个并发 1 次 | 有 TTFT/吞吐，`benchmark.log` 与页面数值一致 |
| Agent 评测 | 终端任务 1 个 | 子任务跑完，通过率或明确的剔除原因 |

口径见 `README.md` 第 5、6 章：`JUDGE_EMPTY` / `JUDGE_INVALID` 不进 Pass@1 分母；
Agent 的环境异常与超时失败也不进分母（`num_scored` / `num_excluded`）。

## 8. 故障排查

先跑这三条，多数问题能定位：

```bash
./start.sh --check                                            # 依赖 / 产物 / 端口
curl -s localhost:8000/api/agent/env | python3 -m json.tool    # pier/docker/egress
tail -n 100 runs/server.log                                   # 后台模式日志
```

### 8.1 安装与启动

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `创建 venv 失败` | 缺 `python3-venv` | `sudo apt install python3-venv` |
| `Python 版本过低` | < 3.10 | 装 3.10+ 或用 `--python` 指定 |
| `Node.js 版本过低` | Vite 6 需 18+ | 升级 Node，或 `--no-frontend` |
| 构建后缺 `index.html` | build 失败 / 目录权限 | 看 npm 报错；受限环境 `--no-emptyOutDir` |
| 端口被占用 | 旧进程未退 | 按提示 `kill <pid>` 或 `PORT=8010 ./start.sh` |
| `--daemon` 30s 未就绪 | 起得慢或依赖缺失 | 看脚本打印的日志尾部 / `runs/server.log` |
| 页面空白或 404 | 产物与后端不匹配 | 重新 `npm run build`，浏览器强刷 |
| 时间少 8 小时 | 服务器 UTC | 设 `LLM_SERVER_EVAL_TZ=Asia/Shanghai` 后重启 |

### 8.2 精度测试

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 大量 `JUDGE_EMPTY` | 裁判空输出（预算被思考耗尽 / 限流） | 关裁判思考（`--no-judge-thinking`）或换裁判模型 |
| 大量 `JUDGE_INVALID` | 裁判输出无 `CORRECT/INCORRECT/NOT_ATTEMPTED` 标签 | 检查裁判是否按模板作答，降思考强度或换模型 |
| 输出反复截断重试 | `--max-tokens` 不够（思考 + 正文共享预算） | 提高 `--max-tokens`（上限 65536）或关思考 |
| 带图题报错 | 端点不支持 OpenAI 多模态 `image_url` + base64 | 换支持多模态的服务，或标 `runnable=false` |
| 数据集下拉框为空 | `datasets.json` 与目录名不匹配 | 目录名必须等于 `name`，`file` 用相对该目录的通配路径 |
| 选项禁用并标注原因 | `file` 不存在或 `runnable=false` | 补数据文件；确认是否本就只用于 Agent |
| 长时间「排队中」 | 三处共用串行队列 | 查 `GET /api/queue`；前序任务结束 5s 后启动下一个 |
| 重启后变「已中断」 | 队列只在内存 | 预期行为，重新提交 |

### 8.3 Agent 评测

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `docker 不可用` | 不在 docker 组 / daemon 未起 | `sudo usermod -aG docker $USER && newgrp docker`；后端会自动退回 `sg docker` |
| 构建卡在 curl 131s 后失败 | uv 预置缺失，回退 GitHub 拉 `install.sh` | 重建 `uv-base`（见下）；确认 `PIER_UV_BASE_IMAGE` 未被置空 |
| `COPY --from=llm-server-eval/uv-base:0.7.13` 解析失败 | 镜像被 prune/删除 | 按下方命令重建 |
| `ModuleNotFoundError: No module named 'mcp'` | litellm 1.106 把 mcp 声明为 extra 却无条件 import | `agent_core` 已追加 `mcp>=2.2.0,<3`；确认 `--ak` 生效（值必须是 JSON 字面量） |
| 前几天能跑、后来全失败 | mini-swe-agent 依赖只有下界，版本漂移 | 已锁 `2.4.6`；改动时同步核对 |
| `/root/.local/bin/env: No such file` + `mini-swe-agent: command not found` | 预置 uv 后 `install.sh` 不执行，PATH/env 文件缺失 | pier 补丁已修；被 `uv tool upgrade` 覆盖后需重打 |
| `set: Illegal option -o pipefail` | 任务镜像默认 shell 是 dash | pier 用 `bash -c`；手写验证 Dockerfile 要加 `SHELL ["/bin/bash","-c"]` |
| 任务镜像拉取超时 | Docker Hub 直连不通 | 配 `registry-mirrors` 或 `LLM_SERVER_EVAL_IMAGE_MIRRORS`（只对 Docker Hub 生效，ECR/ghcr 套前缀会更糟） |
| 启动阶段长时间无日志 | pier 构建/起容器只写 job.log（DEBUG），平台不转发 | 静默 180s 自动补 `[进度]` 行；调 `LLM_SERVER_EVAL_AGENT_HEARTBEAT` |
| `status=succeeded` 但任务全 error | pier 退出码只表示其自身正常退出 | 以 `out.items.jsonl` 的 `status` / `resolved` 为准 |
| 停止后容器仍 Up | 中断时来不及 `compose down` | 三层兜底自动回收；手工按 label `com.docker.compose.project` 删容器→网络→卷 |
| 清理后下一轮极慢 | 镜像 tag 被撤需重拉 | 别用 `docker image prune -a` 清任务镜像（见第 9 章） |

`uv-base` 重建（被删或误 prune 后）：

```bash
curl -sSL "https://pypi.org/pypi/uv/0.7.13/json" \
  | python3 -c "import json,sys;print([u['url'] for u in json.load(sys.stdin)['urls'] if 'manylinux' in u['filename'] and 'x86_64' in u['filename']][0])" \
  | xargs curl -sSL -o /tmp/uv.whl
unzip -o -j /tmp/uv.whl "uv-0.7.13.data/scripts/uv" -d /tmp/uvbase/
printf 'FROM debian:bookworm-slim\nCOPY uv /uv\nRUN chmod 0755 /uv\n' > /tmp/uvbase/Dockerfile
docker build -t llm-server-eval/uv-base:0.7.13 /tmp/uvbase
```

（自动回收只清构建缓存、不删有 tag 的镜像，正常不会丢；`ensure_uv_base()` 也会兜底重建。）

### 8.4 压力测试

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 页面提示 evalscope 不可用 | 未装 / 不在 PATH | `pip install 'evalscope[perf]'`，或设 `EVALSCOPE_BIN` 后重启 |
| 提交即 400 且提示连不上 | 端点 TCP 预检失败（只握手不发请求） | 核对 host:port、防火墙、`/v1/chat/completions` 路径 |
| 跑几秒就 `database is locked` | 读结果库时读事务过长，evalscope 的 commit 失败 | 平台已限每次 ≤512 行、拿锁 ≤0.5s；仍出现则降低轮询频率/减少并发档位 |
| 全部请求失败 | 上下文超长、鉴权、模型名错误 | 看 `perf/failed_requests.log` 的 HTTP 状态码与响应体摘要 |
| TTFT 全为 0 / 明细显示 `—` | 流里没有非空 `delta.content` / `reasoning_content` | 确认服务端真流式且返回增量文本；纯思考输出的模型需看 reasoning 字段 |
| 报 `tokenizer.chat_template is not set` | 分词器目录缺对话模板 | 放入 `chat_template.jinja` 或 `tokenizer_config.json` 的 `chat_template` |
| 提交提示分词器路径非法 | 分词器不在 `model/` 下 | 把目录拷进 `model/`，刷新页面重新选 |
| 长序列长度与服务端不符 | 分词器词表与被测模型不一致 | 用同一套词表；以 `usage.prompt_tokens` 校准模板开销 |
| 停止后仍占端口/显存 | 进程组未退 | 平台按进程组 SIGTERM→SIGKILL；重启服务也会清理孤儿进程 |

### 8.5 磁盘与容器环境

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/` 持续增长但 `/var/lib/docker` 不变 | Docker 29 用 **containerd 镜像存储**，镜像与构建缓存在 `/var/lib/containerd` | 见第 9 章，把 containerd 的 root 也迁到大盘 |
| `df` 涨了但 `du` 算不出来 | 文件被删除但仍被进程持有 | `sudo lsof +L1` 找出持有者，重启对应进程 |
| Agent 跑一轮系统盘就满 | 镜像 + buildkit 缓存只进不出 | `docker builder prune -f`；镜像不要整批删（见第 9 章） |
| `df` 与 `docker system df` 差距大 | 共享层重复计数 / 缓存已清但镜像记录在 | 以 `df` 为准判断真实占用 |

## 9. 磁盘规划与容量治理

Agent 评测是磁盘杀手：单任务镜像数 GB，且**每个任务的 `FROM` 都不同**，buildkit 缓存
永远不命中（见 develop §2.2），一次批量评测就能把 400G 系统盘写满。部署时必须先规划磁盘。

### 9.1 三类占用与回收策略

| 占用 | 位置 | 回收 |
| --- | --- | --- |
| 任务镜像层 | `/var/lib/containerd`（Docker 29 用 containerd 镜像存储） | 不要整批删：pier 撤 tag 后无容器引用，`docker image prune -a` 会让下一轮全量重拉 |
| buildkit 构建缓存 | 同上 | 唯一该定期清的：`docker builder prune`，最终都会重新生成 |
| 运行产物 | `runs/`（精度/Agent/压测结果与日志） | 按需删 `runs/<run_id>`，历史任务不可恢复 |

平台内置两层保护：

- `LLM_SERVER_EVAL_DOCKER_DISK_LIMIT_GB`（默认 200）：**只**约束构建缓存占用，超阈且无 pier 任务在跑时清理；
- `LLM_SERVER_EVAL_DOCKER_MIN_FREE_GB`（默认 10）：系统盘可用低于该值才清「悬空镜像」
  （无 tag、无引用）；**有 tag 的镜像永不自动删除**；
- `/etc/docker/daemon.json` 的 `builder.gc.defaultKeepStorage`（建议 50GB 以内）由 buildkit 自行压制缓存。

### 9.2 迁移 Docker 数据到大盘（踩过的坑）

只把 `/var/lib/docker` 挂到新盘**不生效**：Docker 29 默认启用 containerd 快照器
（`docker info` 里 `driver-type: io.containerd.snapshotter.v1`），镜像与构建缓存落在
**系统 containerd 的 root**（默认 `/var/lib/containerd`），`dockerd` 只是通过
`--containerd=/run/containerd/containerd.sock` 连它。实测现象：

```
df -h /            → 持续增长
/var/lib/docker    → 62M（只有元数据）
docker system df   → Images 271.8GB + Build Cache 98.2GB
/var/lib/containerd→ 319G  ← 真正的大头
```

正确做法（大盘挂 `/data`，两个 root 都指过去）：

```bash
sudo systemctl stop docker docker.socket containerd
sudo rsync -aHAX --numeric-ids /var/lib/containerd/ /data/containerd/
sudo rsync -aHAX --numeric-ids /var/lib/docker/     /data/docker/

# /etc/containerd/config.toml 顶层
#   root = "/data/containerd"
# /etc/docker/daemon.json 顶层
#   "data-root": "/data/docker",

sudo systemctl start containerd docker
docker info --format '{{.DockerRootDir}}'   # 应为 /data/docker
docker images -q | wc -l                   # 与迁移前一致
docker run --rm <任一镜像> echo ok          # 快照可用
```

校验通过后再删旧目录，回滚就是把两处配置改回来重启。

`fstab` 建议：`UUID=<uuid> /data ext4 defaults,noatime 0 2`。**不要**加 `nofail`——
挂载失败时 containerd 会静默把数据写回系统盘的 `/data` 隐藏目录，问题会延后暴露。

### 9.3 容量基线

| 场景 | 建议预留 |
| --- | --- |
| 仅精度测试 | 20G（含数百题 JSONL 与 CSV 结果） |
| 压力测试 | 50G（结果库 + 原始报告 + 长序列请求体，单 run 可达数 GB） |
| Agent 评测（10 任务级） | 300G 起；批量 60+ 任务建议 1T 独立盘 |

## 10. 日常运维

### 10.1 日志与产物

| 路径 | 内容 |
| --- | --- |
| `runs/server.log` | `--daemon` 模式的 stdout/stderr |
| `runs/<run_id>/` | 精度测试：`meta.json` / `items.jsonl` / `results.csv` |
| `runs/agent/<run_id>/` | Agent：`meta.json` + `pier/`（沙箱与逐 trial 日志） |
| `runs/stress/<run_id>/` | 压测：`meta.json` / `args.json` / `perf/`（`benchmark.log`、`perf_report.html`、`evalscope_stderr.log`、`failed_requests.log`） |

请求日志默认开启（`LLM_SERVER_EVAL_LOG_REQ=0` 关闭），每次 LLM 调用一行，
提示词正文、图片 data-url、API Key 都不落盘，只留长度摘要。

### 10.2 备份与迁移

需要备份的只有数据目录：`dataset/`（含 `datasets.json`）、`model/`、`runs/`。
换机器/改目录后历史 Agent 任务的路径会自动重定位（`rebase_dataset_path`），
解析不出来时子任务看板退化为「只看磁盘」，不会整页报错。

### 10.3 升级

```bash
git pull && ./install.sh            # 源码部署
./install.sh --agent                # 若涉及 pier/uv 改动，重新检查 pier 与 uv-base
```

免安装包部署：替换可执行文件即可。升级必查：

1. pier 补丁是否被 `uv tool upgrade` 覆盖（见 5.2）；
2. `llm-server-eval/uv-base:0.7.13` 是否仍在（`docker images | grep uv-base`）；
3. 前端产物是否与新后端匹配；
4. 服务重启后排队中的任务会变「已中断」，压测孤儿进程会在启动时被清理。

### 10.4 多实例注意

队列、压测进程组归属都按「本实例」判定：启动时只回收属于自己的孤儿进程与容器。
同一台机器跑多个后端实例时，务必用不同 `PORT` 与不同 `RUNS_DIR`，
否则任务列表与 PID 文件会互相覆盖。

## 11. 附录：命令与环境变量

### 11.1 环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址与端口（也可 `--host/--port`） |
| `APP_ROOT` | 项目根 / 可执行文件目录 | 数据根（`dataset/` `model/` `runs/` 的基准） |
| `RUNS_DIR` | `<数据根>/runs` | 单独指定运行产物目录 |
| `LLM_SERVER_EVAL_TZ` | `Asia/Shanghai` | 展示时区，`local` 用系统时区 |
| `LLM_SERVER_EVAL_LOG_REQ` | `1` | 设为 `0` 关闭逐次 LLM 请求日志 |
| `EVALSCOPE_BIN` | 空 | 指定 evalscope 可执行文件 |
| `PERF_PROBE_LIMIT` | `3` | 压测失败请求重放次数，`0` 关闭 |
| `LLM_SERVER_EVAL_IMAGE_MIRRORS` | 4 个加速器 | 任务镜像加速器（逗号分隔），空串表示只用本地镜像 |
| `LLM_SERVER_EVAL_IMAGE_PULL_CONCURRENCY` | `8` | 启动前并发补镜像数，上限 16，1 为串行 |
| `LLM_SERVER_EVAL_AGENT_HEARTBEAT` | `180` | 主任务日志静默心跳秒数，0 关闭，上限 3600 |
| `LLM_SERVER_EVAL_DOCKER_DISK_LIMIT_GB` | `200` | 构建缓存超阈才清理，0 关闭 |
| `LLM_SERVER_EVAL_DOCKER_MIN_FREE_GB` | `10` | 系统盘可用低于该值才清悬空镜像 |
| `PIER_UV_BASE_IMAGE` | `llm-server-eval/uv-base:0.7.13` | 置空可关闭 uv 预置 |
| `LITELLM_LOCAL_MODEL_COST_MAP` | `true` | 禁止 litellm 拉远程价目表（内网必需） |

### 11.2 常用命令

```bash
./install.sh --all                       # 全量安装
./start.sh --daemon                      # 后台启动
./start.sh --status && ./start.sh --stop  # 状态与停止
npm --prefix frontend run build          # 重建前端
./packaging/build.sh --version 1.2       # 打免安装包
docker builder prune -f                  # 清构建缓存（安全）
docker system df                         # 看镜像/缓存占用
```

### 11.3 目录一览

```
llm_server_eval/
├── backend/
│   ├── api.py               # FastAPI 入口 + 托管前端
│   ├── consts.py            # 端点/密钥/默认参数
│   ├── runtime_paths.py     # 数据根与产物目录解析
│   ├── task_queue.py        # 三处共用的串行队列
│   ├── run_manager.py       # 精度测试任务
│   ├── agent_core.py        # pier 命令构造与容器回收
│   ├── perf_core.py         # evalscope 集成
│   ├── perf_manager.py      # 压测任务
│   ├── adapters/            # 数据集适配器（csv/parquet/hle/tasks）
│   └── webui/               # 前端构建产物
├── frontend/                # Vue3 + Vite 源码
├── dataset/                 # 数据集与 datasets.json（数据不入库）
├── model/                   # 分词器目录 + tokenizers.json 缓存
├── packaging/               # PyInstaller 打包
├── runs/                    # 运行产物（不入库）
├── install.sh / start.sh    # 安装与启动
└── README.md / develop.md / DEPLOY.md
```

