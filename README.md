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