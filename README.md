# 大模型服务精度与性能测试框架

面向**本地部署大模型服务**的评测平台，前端 Vue3 + 后端 FastAPI 的前后端分离架构，
两个 Tab：

- **精度测试**：SimpleQA-Verified / HLE 数据集逐题作答 → LLM 裁判判分 → 汇总 Pass@1；
- **压力测试**：调用 [llm-grill](https://pypi.org/project/llm-grill/) CLI 对被服务并发压测，
  实时展示吞吐、首字延迟、端到端延迟、阶梯并发对比与多轮 KV 缓存指标。

## 目录结构

```
zhipuEval
├── backend/               # FastAPI 后端（Python）
│   ├── api.py             # HTTP 路由 + 托管前端产物（入口）
│   ├── consts.py          # 端点/API Key 等连接默认值（集中维护）
│   ├── appconfig.py       # 表单默认值与「上次配置」持久化
│   ├── run_manager.py     # 精度测试任务管理（状态/进度/导出）
│   ├── eval_core.py       # 数据集解析、生成、裁判、汇总
│   ├── simpleqa_eval.py   # 精度测试命令行入口
│   ├── grill_core.py      # llm-grill 场景生成、结果解析、指标聚合
│   ├── grill_manager.py   # 压测任务管理（子进程/实时进度/导出）
│   ├── csvview.py         # 工作区内 CSV 预览
│   └── webui/             # 前端构建产物（由 Vite 生成，FastAPI 托管）
├── frontend/              # Vue3 + Vite 前端源码
│   └── src/{views,components}
├── dataset/               # 数据集
├── runs/                  # 运行数据（已 gitignore）
│   ├── <run_id>/          # 精度测试任务：meta.json / items.jsonl / results.csv
│   └── stress/<run_id>/   # 压测任务：meta.json / scenario.yaml / results.jsonl
└── endpoint.md            # 服务访问端点备忘
```

## 启动

后端（等价于原来的 `script/webapp.py`）：

```bash
python3 -m pip install -r requirements.txt
python3 backend/api.py --host 0.0.0.0 --port 8000      # 打印前端/llm-grill 自检信息
```

前端：仓库内 `backend/webui/` 已包含构建产物，直接用上面的命令即可访问
`http://<host>:8000`。需要改前端时：

```bash
npm --prefix frontend install
npm --prefix frontend run build      # 产物输出到 backend/webui/
npm --prefix frontend run dev        # 开发态热更新（:5173，/api 代理到 :8000）
```

精度测试命令行用法不变：

```bash
python3 backend/simpleqa_eval.py --data dataset/HLE/test-00000-of-00001.parquet \
    --model GLM-5.2-zhipu --judge-model DeepSeek-V4-Flash-0731 --out results/hle
```

## 支持的数据集

| 数据集 | 文件 | 格式 | 字段映射 |
| --- | --- | --- | --- |
| SimpleQA-Verified | `dataset/SimpleQA-Verified/simpleqa_verified.csv` | CSV | 原字段 `original_index/problem/answer/topic/answer_type` |
| HLE (Humanity's Last Exam) | `dataset/HLE/*.parquet` | parquet | `question→problem`、`category→topic`、`raw_subject→subject`、`answer_type` 原样 |

两类数据统一为同一套字段后进入同一条流水线（生成 → LLM 裁判 → 汇总），
题型（`exactMatch` / `multipleChoice`）仅影响问题文本，打分模板一致。
HLE 中带图题目（342 题）会自动以 OpenAI 多模态格式发送（`image_url` + base64 data-url），
图片不落入逐题记录，只记 `has_image` 标记。

## 判定与计分口径

- 裁判请求不指定 `max_tokens`，输出上限交由服务端默认参数决定（裁判思考与判定正文共享该预算）。
- 逐题判定分五类：`CORRECT` / `INCORRECT` / `NOT_ATTEMPTED`（模型拒答或未作答）属有效判定；
  `JUDGE_EMPTY`（裁判空输出）与 `JUDGE_INVALID`（裁判有输出但无可识别标签）属无效判定。
- 无效判定按「无效作答」处理：**不计入 Pass@1、拒答率、仅计作答等正确率类指标的分母**；
  token/时延等性能指标仍按全部记录统计。结果页对两类分别展示、可单独筛选。

## 压力测试（llm-grill 集成）

### 依赖与识别

压测由 llm-grill 的**命令行**执行（子进程方式调用，不 import 它的 Python 包，
避免与本项目依赖混用造成版本冲突）：

```bash
uv tool install llm-grill          # 或 pipx / pip install
export LLM_GRILL_BIN=/path/to/llm-grill   # 非标准安装位置时指定
```

后端启动与页面顶部会显示探测结果（`llm-grill --version`，60 秒缓存）；
不可用时「启动压测」按钮禁用并给出原因。

### 负载模型

| 表单项 | 含义 |
| --- | --- |
| 固定并发 | 并发用户数 × 每用户迭代次数 × 对话轮数 = 总请求数 |
| 阶梯加压 | 按「并发档位」逐档加压（自动升序去重），每档跑满即换档，可设每档停留时长 |
| 爬坡时长 / 思考时间 | 并发线性爬坡秒数 / 每轮请求之间的等待秒数 |
| 对话轮数 | >1 时同一提问重复多轮，历史中带模型上一轮回答（长上下文/KV 缓存场景） |
| 会话唯一前缀 | 每会话首条消息拼接唯一前缀，关闭前缀缓存以测冷启动 |

llm-grill 客户端固定请求 `/v1/chat/completions`，端点只需填到端口
（如 `http://10.174.0.70:34000`），多填的路径会被自动去掉。

### 指标口径

与 llm-grill 的 `AggregatedMetrics` 对齐，另加 p50 与错误分布：

- `ttft_*`：首字延迟（首 token 到达）；`tpot_mean_s`：每输出 token 耗时；
- `e2e_*`：端到端延迟；`tokens_per_second_mean`：单请求输出速度；
  `total_tokens_per_second`：集群输出速度；`requests_per_second`：吞吐；
- `total_duration_s`：最早请求开始 → 最晚请求结束（并发场景下不等于墙钟耗时）；
- P95 采用与 llm-grill 相同的 `quantiles(n=20)[18]`；
- 多轮场景额外给出逐轮 TTFT/端到端、上下文增长系数、KV 缓存命中率与占用。

### 任务目录与停止语义

`runs/stress/<run_id>/`：

- `scenario.yaml`：交给 llm-grill 的场景文件（含密钥，权限 600）；
- `results.jsonl`：llm-grill 逐请求结果，**每条即落盘**，后端 tail 该文件得到实时进度；
- `meta.json`：任务快照（状态、实时指标、汇总、日志），key 已脱敏；
- `results.csv`：在页面上点「下载 CSV / 结果表格」时按需导出（UTF-8 BOM）。

「停止」会终止 llm-grill 进程组（SIGTERM → 超时后 SIGKILL），已完成的结果全部保留，
状态记为 `已停止`；子进程退出码非 0、未产出任何结果、或全部请求失败时状态记为
`失败` 并在页面显示原因。Web 服务退出时也会清理所有在跑的压测子进程。

## 时间显示时区

容器/服务器常以 UTC 运行，若直接按系统时区渲染，任务起止时间会比本地时间少 8 小时。
起止时间、任务日志、任务目录名（run_id）统一按 `ZHIPU_EVAL_TZ` 渲染，默认 `Asia/Shanghai`：

```bash
ZHIPU_EVAL_TZ=Asia/Shanghai python3 backend/api.py --host 0.0.0.0 --port 8000
ZHIPU_EVAL_TZ=local python3 backend/api.py --host 0.0.0.0 --port 8000  # 沿用系统时区
```

历史任务无需迁移：起止时间由落盘 epoch 重新计算，日志时间按写入进程当时的偏移整体平移。

## 请求日志

每次调用 LLM（生成 + 裁判）都会往 stderr 打一行实际发出的请求参数，格式：

```
2026-10-13 16:18:48 [INFO] [LLM请求] q7 stream https://open.bigmodel.cn/api/paas/v4/chat/completions {"model": "GLM-5.2-zhipu", "messages": [{"role": "user", "content": "<prompt omitted: 66 chars>"}], "max_tokens": 32768, "temperature": 0.0, "stream": true, "stream_options": {"include_usage": true}, "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
```

- 行首 `q7` / `j9` 是题号（生成 / 裁判），`stream` / `json` 表示流式与否；
- 提示词正文、图片 data-url、API Key 都不落日志，只保留长度摘要；
- 时间同样按 `ZHIPU_EVAL_TZ` 渲染；
- Web 服务以 `nohup ... >> runs/logs/webapp.log 2>&1 &` 启动时，这些行会写进 `runs/logs/webapp.log`；
- `ZHIPU_EVAL_LOG_REQ=0` 可关闭该日志（重试的每一次调用都会单独记一行）。

## 思考开关与强度

被测模型与裁判模型各自独立控制，取值三态：**不传**（默认，交给服务端决定）、**开启**、**关闭**。
开启时可再指定强度 `low|medium|high|max`（关闭或不传时不注入该字段）。

请求体附加字段沿用 `simpleqa_verified_deepseekv4.py` 验证过的约定：
`{"thinking": {"type": "enabled"|"disabled"}, "reasoning_effort": "<level>"}`。

CLI：

```bash
--thinking / --no-thinking              # 被测模型
--reasoning-effort low|medium|high|max  # 被测模型强度
--judge-thinking / --no-judge-thinking  # 裁判模型
--judge-reasoning-effort ...            # 裁判模型强度
```

Web 表单中对应「被测/裁判模型思考」与「…思考强度」四个下拉框。
实际取值会写进 `runs/<id>/meta.json` 的 `cfg`、任务日志与 `<out>.json` 汇总（`thinking` /
`reasoning_effort` / `judge_thinking` / `judge_reasoning_effort`），便于对比不同配置。

注：关闭思考时 `reasoning_tokens_avg` 会趋于 0，`--max-tokens` 的截断翻倍重试逻辑不再触发。

## 被测/裁判模型的服务配置

被测模型与裁判模型可以指向不同的服务，端点与密钥分别配置：

```bash
--endpoint / --api-key                  # 被测模型
--judge-endpoint / --judge-api-key      # 裁判模型；留空则复用被测模型的端点/密钥
```

Web 表单中对应「被测模型」与「裁判模型」两个分组，裁判模型的端点/密钥留空即复用上方配置；
生效值记录在 `runs/<id>/meta.json` 的 `cfg.judge_endpoint` / `cfg.judge_api_key`（密钥打码）与任务日志中。

端点与密钥的默认值集中在 `backend/consts.py`（`DEFAULT_ENDPOINT` / `DEFAULT_API_KEY` /
`DEFAULT_STRESS_ENDPOINT` / `DEFAULT_STRESS_API_KEY`），需要指向其它服务时改该文件即可；
精度测试与压测目标共用同一份默认值。

Web 表单会记住上次提交的配置（精度测试存于 `runs/.last_config.json`，
压测存于 `runs/.last_stress_config.json`，权限 600、含密钥），
刷新首页、从任务详情页返回、或在首页删除任务后重新渲染，表单内容均保持不变；
删除这些文件即可恢复为内置默认值。
