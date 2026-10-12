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