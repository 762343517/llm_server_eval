# 大模型服务精度与性能测试框架

面向本地部署的大模型服务，前端 Vue3 + 后端 FastAPI 前后端分离，包含两个 Tab：

- **精度测试**：逐题作答 → LLM 裁判判分 → 汇总 Pass@1（数据集清单由 `dataset/datasets.json`
  配置、页面下拉选择，见「数据集配置」）；
- **压力测试**：调用 [EvalScope](https://evalscope.readthedocs.io/) 的 `evalscope perf`
  并发压测，实时展示吞吐、首字延迟、端到端延迟、阶梯并发对比与多轮上下文增长指标。

## 目录结构

```
zhipuEval
├── backend/               # FastAPI 后端
│   ├── api.py             # HTTP 路由 + 托管前端产物（入口）
│   ├── consts.py          # 端点/API Key 等连接默认值
│   ├── appconfig.py       # 表单默认值与「上次配置」持久化
│   ├── run_manager.py     # 精度测试任务管理（状态/进度/导出）
│   ├── dataset_registry.py # 数据集清单（dataset/datasets.json）解析：reader/字段/任务目录
│   ├── adapters/          # 数据集适配器层：业务代码读取数据集的唯一入口
│   │   ├── base.py        # 适配器接口、注册表、统一加载入口与表格归一化
│   │   ├── table.py       # csv / parquet
│   │   ├── hle.py         # HLE（按 row group 读内嵌图片）
│   │   └── tasks.py       # Agent 任务集（task.toml / instruction.md 约定）
│   ├── eval_core.py       # 精度测试：生成、裁判、汇总（数据集读取已下沉到 adapters）
│   ├── simpleqa_eval.py   # 精度测试命令行入口
│   ├── perf_core.py       # EvalScope 集成：数据集生成、命令行构造、结果解析、指标聚合
│   ├── perf_manager.py    # 压测任务管理（子进程/实时进度/导出）
│   ├── csvview.py         # CSV 预览
│   └── webui/             # 前端构建产物（Vite 生成，FastAPI 托管）
├── frontend/src/{views,components}   # Vue3 + Vite 前端源码
├── dataset/               # 数据集：每个数据集一个顶级目录（数据不入库，仅 datasets.json 入库）
├── runs/                  # 运行数据（不入版本库）
│   ├── <run_id>/          # 精度测试：meta.json / items.jsonl / results.csv
│   └── stress/<run_id>/   # 压测：meta / dataset / args / proc.json / perf/
└── endpoint.md            # 服务访问端点记录
```

## 安装与启动

```bash
./install.sh                 # 建 .venv + 装后端依赖 + 构建前端（需 Node 18+）
./install.sh --all           # 再加压测引擎 evalscope[perf] 与 Agent 依赖（uv/pier/docker 检查）
./install.sh --dry-run       # 只打印将要执行的命令

./start.sh                   # 前台启动，默认 0.0.0.0:8000
PORT=8010 ./start.sh         # 换端口（也可 --port 8010）
./start.sh --daemon          # 后台启动，日志 runs/server.log
./start.sh --status          # 查看运行状态
./start.sh --stop            # 停止后台服务
./start.sh --check           # 只做启动前检查，不启动
```

`start.sh` 启动前会检查依赖、前端产物与**端口占用**：端口被占时打印占用进程并提示
换端口，避免出现"改了端口却仍访问到旧服务"；`--daemon` 模式会等 `/api/meta` 就绪后
才返回，失败则打印日志尾部并清理进程。

手动方式（不建 venv）：

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install 'evalscope[perf]'               # 压测引擎（可选）
python3 backend/api.py --host 0.0.0.0 --port 8000      # 启动时打印前端/evalscope 自检信息
```

`backend/webui/` 已含前端构建产物，启动后访问 `http://<host>:8000`。修改前端源码：

```bash
npm --prefix frontend install
npm --prefix frontend run build      # 产物输出到 backend/webui/
npm --prefix frontend run dev        # 开发态热更新（:5173，/api 代理到 :8000）
```

精度测试命令行用法：

```bash
python3 backend/simpleqa_eval.py --data dataset/HLE/test-00000-of-00001.parquet \
    --model GLM-5.2-zhipu --judge-model DeepSeek-V4-Flash-0731 --out results/hle
```

## 打包分发（免安装）

把服务打成单文件可执行（内置 Python 运行时与前端产物），目标机器无需 Python/pip/Node：

```bash
./packaging/build.sh                 # 前端构建 + PyInstaller + 组装 + tar.gz
./packaging/build.sh --no-data       # 只打包程序，数据集另行准备
```

产物：`dist/zhipu-eval-linux-x86_64-<日期>.tar.gz`，解压后 `./start.sh` 即可启动。
数据目录 `dataset/`、`model/`、`runs/` 与可执行文件同级（可用 `APP_ROOT` 覆盖），
路径基准统一由 `backend/runtime_paths.py` 提供：开发态指向项目根，打包后指向可执行文件
所在目录，因此启动时的工作目录不再影响数据落盘位置。

## 全局任务队列

精度测试与压力测试共用一条串行队列（`backend/task_queue.py`）：提交后任务先入队，由单一
调度线程逐个执行 —— 队列空闲时立即开跑，否则等前序任务**完全结束**（压测子进程退出、精度
任务写完汇总）再开始，两者之间再留 5 秒静默间隔，让被测服务释放连接与显存，保证每次测试
都从相同的初始状态起跑，避免并行测试互相争抢算力导致指标失真。

- 排队中的任务状态为 `排队中`（快照里是 `queued`），列表与详情页展示排队位置
  （`queue_position`，1 表示下一个开跑）；耗时只统计真正执行的时间，排队期间显示 `—`；
- 「取消排队」会把排队中的任务直接从队列摘除并记为 `已停止`，不会启动任何子进程或压测；
- `GET /api/queue` 返回队列快照（正在运行的任务 + 排队列表），两个任务列表上方展示概览；
- 队列只存在于内存：服务重启后排队中的任务不会恢复执行，启动时统一收敛为 `已中断`。

## 数据集配置

数据集清单、数据文件位置与字段映射全部写在 `dataset/datasets.json` 里（该文件入库；
`dataset/` 下的数据集目录与数据文件都在 `.gitignore` 里，只保留在本机），新增数据集
**不需要改代码**，只需两步：

1. 把数据集目录拷进 `dataset/`，**以数据集名称作为顶级目录名**（数据文件放在该目录内，
   可再分子目录）；
2. 在 `dataset/datasets.json` 的 `datasets` 里加一条：

```json
{
  "name": "MyDataset",
  "title": "我的数据集",
  "reader": "parquet",
  "file": "data/*.parquet",
  "fields": {"problem": "question", "answer": "answer",
             "topic": "category", "subject": "raw_subject"}
}
```

字段说明：`name` 为顶级目录名（即数据集名称）；`title` 是下拉框显示名（缺省用 `name`）；
`reader` 取 `csv` / `parquet` / `hle`，缺省按文件扩展名推断；`file` 是数据集目录内的数据
文件，支持 `*` 通配（多分片自动拼接）；`fields` 是「统一字段 → 源列名」映射，未声明的字段
按同名列读取，列不存在时填空值；此外可用 `runnable: false` + `note` 标记「不适合当前
流程」的数据集，页面会展示原因并禁止选择。

统一字段为 `problem / answer / topic / answer_type / subject / image`，各数据集经映射归一
化后进入同一条流水线（生成 → LLM 裁判 → 汇总）：

| reader | 适用 | 说明 |
| --- | --- | --- |
| `csv` | 表格类数据集 | 多分片 CSV 按文件名顺序拼接 |
| `parquet` | 通用 parquet | 多分片交由 pandas 拼接（图片列原样透传） |
| `hle` | HLE 专用 | 按 row group 只读采样命中行的 `image` 列，整表含 base64 图片时快很多 |

### 适配器层

读取实现集中在 `backend/adapters/`，上层（`eval_core` / `agent_core` / `api`）只调用
`adapters.load()` / `adapters.catalog()` / `adapters.tasks.*`，不感知具体格式：

| 模块 | 负责 | 说明 |
| --- | --- | --- |
| `adapters/table.py` | `csv` / `parquet` | 通用表格：字段映射、采样、图片抽取共用 |
| `adapters/hle.py` | `hle` | 按 row group 只读采样行的图片列 |
| `adapters/tasks.py` | Agent 任务集 | 任务目录 / `task.toml` / `instruction.md` 约定与抽样 |

新增一种「数据格式」（不是新增数据集）时：在 `adapters/` 下实现 `DatasetAdapter.load()`，
用 `@register` 登记 reader 名，配置里写该 reader 即可，业务代码零改动；新增同格式数据集
仍然只是拷目录 + 改 `datasets.json`。压测侧的数据集（`custom_multi_turn` / `random` /
`line_by_line`）是运行时合成的载荷，不读 `dataset/` 清单，仍由 `perf_core.py` 自行构造。

当前已登记的数据集：

| 数据集 | 目录 | reader | 字段映射 |
| --- | --- | --- | --- |
| SimpleQA-Verified | `dataset/SimpleQA-Verified/` | `csv` | 原列名（`original_index/problem/answer/topic/answer_type`） |
| HLE (Humanity's Last Exam) | `dataset/HLE/` | `hle` | `question→problem`、`category→topic`、`raw_subject→subject`、`answer_type` 原样 |
| DeepSWE | `dataset/deepswe/` | `parquet` | `problem_statement→problem`、`category→topic`、`language→subject`；`runnable=false`（SWE 类编码 agent 任务，需 docker 执行环境与测试用例验证，不能用本页的单轮作答 + 裁判判分流程评测） |
| Terminal-Bench 2.0 | `dataset/terminal-bench-2.0/` | 不适用 | 终端 agent 任务集（89 个任务）：`agent.framework=pier` + `agent.tasks="."`，任务目录直接位于数据集根目录，每个任务自带 `task.toml` / `environment/` / `tests/`；`runnable=false`（在「Agent 评测」页发起，由 pier 拉起 docker 沙箱逐任务执行并跑任务自带测试脚本判定） |

页面的数据集下拉框即来自该清单：数据文件缺失或 `runnable=false` 的条目标注原因并禁止选择；
「自定义数据集路径」输入框可绕过配置直接跑任意 `.csv` / `.parquet`（按扩展名推断 reader）。
清单读取带缓存（配置文件与各数据集目录的 mtime 变化时自动重建），新增数据集后刷新页面即可
看到；CLI 的 `--data` 默认取配置里的 `default`。

配置里的 `agent` 段标记「Agent 任务集」（DeepSWE、Terminal-Bench 2.0）：`tasks` 是任务目录相对
数据集目录的路径，留空或写 `"."` 表示任务目录就是数据集根目录（下拉框显示为「数据集根目录」）；
这类数据集由「Agent 评测」页交给 pier 拉起 docker 沙箱逐任务执行，`reader` / `fields` 不参与。

题型（`exactMatch` / `multipleChoice`）仅影响问题文本，打分模板一致。HLE 带图题目（342 题）
以 OpenAI 多模态格式发送（`image_url` + base64 data-url），图片不写入逐题记录，
仅记录 `has_image` 标记。

### 容器回收

pier 正常跑完会自己 `docker compose down --rmi all`；但被中断（点停止、后端重启、
进程被 SIGKILL、pier 崩溃）时来不及执行，任务容器与 egress 代理会一直 Up。为此有三层
兜底，按 compose 的 `com.docker.compose.project` label 依次回收容器 → 网络 → 卷：

1. 每轮 run 结束（含停止）在 `run_agent_eval` 的 finally 里回收本轮 job 的全部 trial；
2. 服务启动时后台扫描 `runs/agent/*/pier/`，回收历史 run 的残留；
3. 判定「是否有主」不看快照状态（被 SIGKILL 的 run 会一直写着 running），而是看
   `ps` 里是否还有 `pier ... --job-name <run_id>` 进程，避免误清别的部署正在跑的任务。

回收不删镜像 tag（与 pier 的 `--rmi all` 不同），下一轮可直接复用，省一次 pull。

## 判定与计分口径

- 裁判请求不指定 `max_tokens`，上限由服务端默认参数决定（思考与判定正文共享该预算）。
- 有效判定：`CORRECT` / `INCORRECT` / `NOT_ATTEMPTED`（模型拒答或未作答）；
  无效判定：`JUDGE_EMPTY`（裁判空输出）、`JUDGE_INVALID`（裁判有输出但无可识别标签）。
- 无效判定按「无效作答」处理，不计入 Pass@1、拒答率、仅计作答等正确率指标的分母；
  token/时延等性能指标仍按全部记录统计。结果页分别展示两类判定，可单独筛选。

## 压力测试（EvalScope 集成）

### 依赖与识别

压测通过 [EvalScope](https://evalscope.readthedocs.io/) 的**命令行入口**执行
（`evalscope perf ...`，以子进程调用，不 import 其 Python 包，避免 modelscope /
transformers 等重依赖进入本项目环境）：

```bash
pip install 'evalscope[perf]'             # 压测功能需要 perf 附加依赖
export EVALSCOPE_BIN=/path/to/evalscope   # 非标准安装位置时指定
```

后端按 `EVALSCOPE_BIN` → `PATH` → 常见安装目录的顺序定位可执行文件，均未找到时
退化为 `<当前解释器> -m evalscope`。后端启动时与页面顶部展示探测结果
（`evalscope --version`，缓存 60 秒），不可用时禁用「启动压测」按钮并提示原因。

### 负载模型

一次压测对应一个 `evalscope perf` 进程。为免去逐档手算请求总数，表单只填**每个并发的
请求数**，`--number` 由后端换算：

- `--parallel` = **并发数**：同时发起的请求数（在飞请求上限）；
- 每个并发的请求数 = 单个并发串行发出的请求数；
- `--number` = **本档请求总数** = 并发数 × 每个并发的请求数；chat 多轮模式下按**会话**
  计数，实际请求数 = 会话数 × 对话轮数。

| 表单项 | 含义 |
| --- | --- |
| 请求模式 | `自定义会话提问`（chat）或 `长序列 + 固定前缀`（long，见下节） |
| 固定并发 | 填 `--parallel` = 并发数，共一档，`--number` = 并发数 × 每个并发的请求数 |
| 阶梯加压 | 填并发档位列表（逗号分隔，自动升序去重、最多 20 档、单档并发上限 1024），每档 `--number` = 该档并发数 × 每个并发的请求数 |
| 阶梯停顿 | 档与档之间的休眠秒数（`--sleep-interval`） |
| 每个并发的请求数 | 单个并发串行发出的请求数；页面统计卡实时展示换算出的各档 `--number` 与预计请求总数 |
| 对话轮数 | >1 时数据集内重复多轮同一提问，历史中携带模型上一轮回答（长上下文/KV 缓存场景） |
| 会话唯一前缀 | 每条会话首条 user 消息拼接 1024 字符随机前缀，关闭前缀缓存以测冷启动 |

换算后的 `--number` 与 `iterations` 一并写入任务快照，历史任务（含早期直接填 `--number`
列表的快照）仍按各自记录的口径复现与展示。

提问内容以自定义会话数据集下发（`--dataset custom_multi_turn --dataset-path ...`）：每行
一条 `messages` 数组，assistant 消息仅作轮次分界（内容由 evalscope 丢弃，真实回答由
执行器追加），重复 N 轮同一提问即等价多轮追问。

evalscope 不自动补全请求路径：端点仅到端口时补 `/v1/chat/completions`（如
`http://10.174.0.70:34000`），已带路径前缀（如 `/v1`、`/api/paas/v4`）时追加
`/chat/completions`。

### 长序列与 Prefix Cache 命中率

请求模式选 `长序列 + 固定前缀` 后，按「序列长度 + 固定前缀」两个维度组织载荷，
所有请求共享同一段固定前缀，以固定前缀占序列长度的比例模拟 Prefix Cache 命中率：

| 表单项 | 含义 |
| --- | --- |
| 序列长度 | 单次请求输入 tokens，支持 `128000` / `128K` / `1M`（K=1000、M=1000000） |
| 固定前缀长度 | 所有请求共享的前缀 tokens；填 0 时 = 序列长度 × 命中率 |
| Prefix Cache 命中率 | 固定前缀 / 序列长度，变长后缀 = 序列长度 − 固定前缀（与固定前缀长度择一填写，显式长度优先） |
| 固定前缀内容 | 可选。留空由 evalscope 生成共享随机前缀（长度精确可控）；填写则以该正文补齐为固定前缀 |
| 提问内容 | 仅在填写固定前缀内容时生效（每条请求的可变后缀）；留空时页面不显示该字段，`random` 数据集的请求内容由分词器随机 token 生成 |
| 分词器 | 必需，页面下拉框选择；候选固定来自仓库内的 `model/` 目录（后端只接受该目录下的本地路径），需与被测模型同一词表（evalscope 按 token 精确生成长序列） |
| 输出长度 | 映射 `--min-tokens` / `--max-tokens`，固定输出长度使 TPOT 口径一致 |
| 预热请求数 | 先发若干请求将共享前缀写入缓存再统计（建议不小于并发数，0 = 关闭） |
| 强制输出长度 | `--extra-args '{"ignore_eos": true}'`，忽略停止符按输出长度生成 |

对应 evalscope 参数：留空固定前缀内容 → `--dataset random`（`--prefix-length` 为共享前缀
token 数、`--min/max-prompt-length` 为变长后缀长度，各请求前缀一致、后缀不同）；填写
固定前缀内容 → `--dataset line_by_line` + `--dataset-args`（`prefix_file` 指向任务目录的
`prefix.txt`、`target_input_len` 为序列长度、`input_len_mode=cap`，前缀补齐到「序列长度 −
提问长度」，后缀取 `prompts.txt` 各行，即表单里那条提问，各请求内容相同）。

分词器目录固定在仓库的 `model/` 下：页面下拉框由后端扫描该目录（含 `tokenizer.json` 或
`tokenizer_config.json` 的子目录）生成，带对话模板的排在最前，默认选中第一个；提交时后端
校验本地路径必须位于该目录内（`perf_core.validate_tokenizer`），放到别处会被拒绝并提示。
新增分词器只需把整个模型目录拷进 `model/`，刷新页面即可在下拉框里看到。

扫描结果缓存在 `model/tokenizers.json`（记录各子目录的 mtime 指纹）：请求先命中进程内
缓存，服务重启后直接读该文件，只有目录新增/改名/删除（指纹变化）或索引结构版本升级时才
重新扫盘并回写，避免每次打开页面或提交都遍历分词器目录。该文件是自动生成的缓存，可随时
删除，下次请求会重建。

目录里没有对话模板（`tokenizer_config.json` 无 `chat_template` 字段、也没有
`chat_template.jinja`）时，后端按目录名匹配族系（`deepseek*` → DeepSeek 风格 `User:/
Assistant:` 模板，其余 → ChatML）自动生成 `chat_template.jinja`，省去手工准备模板；下拉框
会给这类目录标注「模板自动生成」。生成的文件只影响客户端计数，若与线上服务真实使用的模板
不一致，直接用官方模板覆盖同名文件即可（文件头部的注释标记用于识别「自动生成」）。

分词器要求（实测 GLM-5.3 满足）：

- 目录内需有对话模板（`chat_template.jinja` 或 `tokenizer_config.json` 的 `chat_template`）：
  evalscope 在客户端套用模板估算 prompt token 数（`random` 数据集须扣除模板开销），
  缺失会报 `tokenizer.chat_template is not set`，后端会在启动前拦截并提示；仓库内
  `model/GLM-5.3/chat_template.jinja` 即为此用途，仅影响客户端计数，不改变分词表与请求体；
- 词表需与被测模型一致：以 `model/GLM-5.3` 对比服务返回的 `usage.prompt_tokens`，不同
  内容长度下的差值恒为模板开销（14），内容 token 数完全对齐，序列长度可按 token 精确
  控制（实测配置 2048，服务端实际收到 2048）。

long 模式为**单轮**请求：`--number` 即请求数，进度与预计请求数同按单轮口径统计；预热
请求不写入结果库，平台指标只统计有效请求。页面提供 `128K / 1024K` × `命中率 50% / 90%`
四个预设按钮。

### 指标口径

逐请求明细取自 evalscope 结果库，聚合后补齐 p50 与错误分布：

- `ttft_*` 首字延迟（`first_chunk_latency`）、`tpot_mean_s` 每输出 token 耗时
  （`time_per_output_token`）、`e2e_*` 端到端延迟（`latency`）；
- `tokens_per_second_mean` 单请求输出速度、`total_tokens_per_second` 输出吞吐（日志
  Output Throughput）、`overall_tokens_per_second` 输入 + 输出总吞吐（日志 Total
  Throughput，长序列下远大于输出吞吐）、`requests_per_second` 吞吐（含失败请求）；
- `total_duration_s` 最早请求开始 → 最晚请求结束，并发场景下不等于墙钟耗时；
- 接口统一返回**秒**（与 evalscope 原始字段对齐），页面将 TTFT / TPOT 换算为
  **毫秒（ms）**，端到端仍用秒；
- 分位数用**最近秩法** `ceil(n·p) − 1`，均值按全部成功请求计数（同 evalscope），与
  `benchmark.log` 的分位表逐值一致；
- TTFT 仅在解析到流的**非空增量文本**时记录，未记录时库中为 0：页面明细显示 `—`，
  聚合值仍除以全部请求（与日志 Avg TTFT 一致）。阶梯/长序列下多数请求无 TTFT 时，需
  确认服务端流式返回的 `delta.content` / `reasoning_content` 是否为空；
- 多轮场景额外给出逐轮 TTFT/端到端与上下文增长系数；evalscope 不采集目标侧 KV cache
  指标，相关列（`kv_cache_usage` / `cache_hit_rate`）为空。

### 任务目录与停止语义

`runs/stress/<run_id>/`：

- `dataset.jsonl` 自定义会话数据集（每行一条 `messages`）；
- `prompts.txt` / `prefix.txt` 长序列 + 固定前缀内容模式的逐行提问与固定前缀正文
  （`line_by_line` + `prefix_file`）；固定前缀在 `meta.json` 中只保留字符数；
- `args.json` 完整命令行与参数（含密钥，权限 600，页面展示时脱敏）；
- `perf/<档位目录>/benchmark_data.db` evalscope 的 SQLite 结果库，以
  `--db-commit-interval 1` 逐行提交，后端据此边跑边读，实时刷新进度、曲线与明细；
- `meta.json` 任务快照（状态、实时指标、汇总、日志），key 已脱敏；
- `proc.json` 运行期间记录 evalscope 进程组号与所属服务进程号（权限 600），收尾删除；
- `results.csv` 点击「下载 CSV / 结果表格」时按需导出（UTF-8 BOM）；
- `perf/benchmark.log` / `performance_summary.txt` / `perf_report.html`：evalscope 原生
  报告，详情页「原始产物」卡片可在线查看（文本或 iframe 内嵌）或下载；接口为
  `GET /api/stress/runs/{run_id}/files`（清单）与 `.../file?path=<相对路径>[&download=1]`
  （正文），仅放行上述三个文件名的白名单路径。

提交任务后、启动 evalscope 前，后端对目标端点做一次 TCP 连通性预检（3 秒超时，只握手
不发请求、不消耗 token）：连接被拒 / 超时 / 域名解析失败直接以 400 返回原因，无需等待
evalscope 的 `connect_test` 耗尽 `--total-timeout`。

「停止」按进程组终止 evalscope（SIGTERM 等待 2 秒 → SIGKILL），并以
`/proc/<pgid>/cmdline` 校验归属避免误杀；已完成结果全部保留，状态记为 `已停止`。进程组
已落盘，服务重启后仍可停止上次遗留的压测：启动时终止这类孤儿进程（属于其它仍在运行的
后端实例的任务除外）并将快照收敛为 `已中断`，退出时同样终止自身启动的全部压测。子进程
退出码非 0、未产出结果或全部请求失败时状态记为 `失败`，页面显示原因。

## 时间显示时区

容器/服务器常以 UTC 运行，直接按系统时区渲染会使任务起止时间比本地时间少 8 小时。
起止时间、任务日志、任务目录名（run_id）统一按 `ZHIPU_EVAL_TZ` 渲染，默认 `Asia/Shanghai`：

```bash
ZHIPU_EVAL_TZ=Asia/Shanghai python3 backend/api.py --host 0.0.0.0 --port 8000
ZHIPU_EVAL_TZ=local python3 backend/api.py --host 0.0.0.0 --port 8000  # 沿用系统时区
```

历史任务无需迁移：起止时间由落盘 epoch 重新计算，日志时间按写入进程当时的偏移整体平移。

## 请求日志

每次调用 LLM（生成与裁判）向 stderr 输出一行实际请求参数：

```
2026-10-13 16:18:48 [INFO] [LLM请求] q7 stream https://open.bigmodel.cn/api/paas/v4/chat/completions {"model": "GLM-5.2-zhipu", "messages": [{"role": "user", "content": "<prompt omitted: 66 chars>"}], "max_tokens": 32768, "temperature": 0.0, "stream": true, "stream_options": {"include_usage": true}, "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
```

- 行首 `q7` / `j9` 为题号（生成 / 裁判），`stream` / `json` 表示是否流式；
- 提示词正文、图片 data-url 与 API Key 不写入日志，仅保留长度摘要；
- 时间按 `ZHIPU_EVAL_TZ` 渲染；`nohup ... >> runs/logs/webapp.log 2>&1 &` 启动时写入
  `runs/logs/webapp.log`；
- `ZHIPU_EVAL_LOG_REQ=0` 关闭该日志（重试的每次调用单独记录一行）。

## 思考开关与强度

被测模型与裁判模型各自独立控制，取值三态：**不传**（默认，由服务端决定）、**开启**、
**关闭**；开启时可指定强度 `low|medium|high|max`（关闭或不传时不注入该字段）。请求体
附加字段沿用 `simpleqa_verified_deepseekv4.py` 的约定：
`{"thinking": {"type": "enabled"|"disabled"}, "reasoning_effort": "<level>"}`。

CLI：

```bash
--thinking / --no-thinking              # 被测模型
--reasoning-effort low|medium|high|max  # 被测模型强度
--judge-thinking / --no-judge-thinking  # 裁判模型
--judge-reasoning-effort ...            # 裁判模型强度
```

Web 表单对应「被测/裁判模型思考」与「…思考强度」四个下拉框；生效值写入
`runs/<id>/meta.json` 的 `cfg`、任务日志与 `<out>.json` 汇总（`thinking` /
`reasoning_effort` / `judge_thinking` / `judge_reasoning_effort`），便于对比不同配置。
关闭思考时 `reasoning_tokens_avg` 趋于 0，`--max-tokens` 的截断翻倍重试逻辑不再触发。

## 被测/裁判模型的服务配置

被测模型与裁判模型可指向不同服务，端点与密钥分别配置：

```bash
--endpoint / --api-key                  # 被测模型
--judge-endpoint / --judge-api-key      # 裁判模型；留空则复用被测模型的端点/密钥
```

Web 表单对应「被测模型」与「裁判模型」两个分组，后者留空即复用；生效值记录在
`runs/<id>/meta.json` 的 `cfg.judge_endpoint` / `cfg.judge_api_key`（密钥脱敏）与任务
日志中。默认值集中在 `backend/consts.py`（`DEFAULT_ENDPOINT` / `DEFAULT_API_KEY` /
`DEFAULT_STRESS_ENDPOINT` / `DEFAULT_STRESS_API_KEY`），精度测试与压测共用同一份。

Web 表单记住上次提交的配置（精度测试 `runs/.last_config.json`，压测
`runs/.last_stress_config.json`，权限 600、含密钥）：刷新首页、从详情页返回或删除任务
后重新渲染，表单内容保持不变；删除这些文件即恢复内置默认值。压测表单顶部会显示
「表单已沿用上次保存的配置（时间）」，进入页面时强制重读 `/api/meta`（否则共享的
meta 缓存会回填上一次的配置），并可用「保存配置」按钮（`POST /api/stress/config`，
校验口径与启动一致）在**不启动任务**的前提下立即落盘。
