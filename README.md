# 大模型服务精度与性能测试框架

面向本地部署的大模型服务，前端 Vue3 + 后端 FastAPI 前后端分离，包含两个 Tab：

- **精度测试**：SimpleQA-Verified / HLE 数据集逐题作答 → LLM 裁判判分 → 汇总 Pass@1；
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
│   ├── eval_core.py       # 数据集解析、生成、裁判、汇总
│   ├── simpleqa_eval.py   # 精度测试命令行入口
│   ├── perf_core.py       # EvalScope 集成：数据集生成、命令行构造、结果解析、指标聚合
│   ├── perf_manager.py    # 压测任务管理（子进程/实时进度/导出）
│   ├── csvview.py         # CSV 预览
│   └── webui/             # 前端构建产物（Vite 生成，FastAPI 托管）
├── frontend/src/{views,components}   # Vue3 + Vite 前端源码
├── dataset/               # 数据集
├── runs/                  # 运行数据（不入版本库）
│   ├── <run_id>/          # 精度测试：meta.json / items.jsonl / results.csv
│   └── stress/<run_id>/   # 压测：meta / dataset / args / proc.json / perf/
└── endpoint.md            # 服务访问端点记录
```

## 启动

后端入口 `backend/api.py`（等价于原 `script/webapp.py`）：

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

## 支持的数据集

| 数据集 | 文件 | 格式 | 字段映射 |
| --- | --- | --- | --- |
| SimpleQA-Verified | `dataset/SimpleQA-Verified/simpleqa_verified.csv` | CSV | 原字段 `original_index/problem/answer/topic/answer_type` |
| HLE (Humanity's Last Exam) | `dataset/HLE/*.parquet` | parquet | `question→problem`、`category→topic`、`raw_subject→subject`、`answer_type` 原样 |

两类数据统一为同一套字段后进入同一条流水线（生成 → LLM 裁判 → 汇总）；题型
（`exactMatch` / `multipleChoice`）仅影响问题文本，打分模板一致。HLE 带图题目（342 题）
以 OpenAI 多模态格式发送（`image_url` + base64 data-url），图片不写入逐题记录，
仅记录 `has_image` 标记。

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
| 分词器路径 | 必需，需与被测模型同一词表（evalscope 按 token 精确生成长序列） |
| 输出长度 | 映射 `--min-tokens` / `--max-tokens`，固定输出长度使 TPOT 口径一致 |
| 预热请求数 | 先发若干请求将共享前缀写入缓存再统计（建议不小于并发数，0 = 关闭） |
| 强制输出长度 | `--extra-args '{"ignore_eos": true}'`，忽略停止符按输出长度生成 |

对应 evalscope 参数：留空固定前缀内容 → `--dataset random`（`--prefix-length` 为共享前缀
token 数、`--min/max-prompt-length` 为变长后缀长度，各请求前缀一致、后缀不同）；填写
固定前缀内容 → `--dataset line_by_line` + `--dataset-args`（`prefix_file` 指向任务目录的
`prefix.txt`、`target_input_len` 为序列长度、`input_len_mode=cap`，前缀补齐到「序列长度 −
提问长度」，后缀取 `prompts.txt` 各行，即表单里那条提问，各请求内容相同）。

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
