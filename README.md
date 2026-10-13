# 大模型服务精度与性能测试框架
该项目为测试本地部署大模型服务性能设计


## 目录结构
zhipuEval
- dataset 用于准确度测试的数据集
- script 测试的脚本
- endpoint 服务访问端点
- README.md 项目介绍文件

## 支持的数据集
| 数据集 | 文件 | 格式 | 字段映射 |
| --- | --- | --- | --- |
| SimpleQA-Verified | `dataset/SimpleQA-Verified/simpleqa_verified.csv` | CSV | 原字段 `original_index/problem/answer/topic/answer_type` |
| HLE (Humanity's Last Exam) | `dataset/HLE/*.parquet` | parquet | `question→problem`、`category→topic`、`raw_subject→subject`、`answer_type` 原样 |

两类数据统一为同一套字段后进入同一条流水线（生成 → LLM 裁判 → 汇总），
题型（`exactMatch` / `multipleChoice`）仅影响问题文本，打分模板一致。
HLE 中带图题目（342 题）会自动以 OpenAI 多模态格式发送（`image_url` + base64 data-url），
图片不落入逐题记录，只记 `has_image` 标记。

## 启动
```bash
python3 script/webapp.py --host 0.0.0.0 --port 8000   # Web 界面，数据集在表单中选择
python3 script/simpleqa_eval.py --data dataset/HLE/test-00000-of-00001.parquet \
    --model GLM-5.2-zhipu --judge-model DeepSeek-V4-Flash-0731 --out results/hle
```

## 时间显示时区

容器/服务器常以 UTC 运行，若直接按系统时区渲染，任务起止时间会比本地时间少 8 小时。
起止时间、任务日志、任务目录名（run_id）统一按 `ZHIPU_EVAL_TZ` 渲染，默认 `Asia/Shanghai`：

```bash
ZHIPU_EVAL_TZ=Asia/Shanghai python3 script/webapp.py --host 0.0.0.0 --port 8000
ZHIPU_EVAL_TZ=local python3 script/webapp.py --host 0.0.0.0 --port 8000  # 沿用系统时区
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

Web 表单会记住上次提交的配置（存于 `runs/.last_config.json`，权限 600，含密钥），
刷新首页、从任务详情页返回、或在首页删除任务后重新渲染，表单内容均保持不变；
删除该文件即可恢复为内置默认值。任务详情页的「逐题明细」中，问题/标准答案/模型回答
以可滚动单元格完整展示，不再截断文本。