"""用户可配置的默认值（被测/裁判/压测目标的端点、API Key、模型与运行参数）。

本模块只保存字面量常量，由调用方按需导入；需要切换服务、更换密钥或调整默认模型与
默认并发时，仅修改此处。

- 精度测试：被测与裁判是两套独立的端点/密钥/模型，裁判端点留空表示复用被测的那一套；
- 压力测试：压测目标默认复用被测服务（evalscope 走 OpenAI 兼容的
  /v1/chat/completions，多填的路径会被归一化掉，因此填到端口即可）；
- 这些只是「表单 / 命令行的初始值」，用户在页面提交过配置后由 appconfig 读取的
  last_config 覆盖。
"""

import os

# 分词器根目录：固定为项目仓库内的 model/ 目录，页面的分词器下拉框只从这里列候选
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(PROJECT_ROOT, "model")

# 数据集根目录：每个数据集以「名称」为顶级目录放在这里，清单见 dataset/datasets.json
DATASET_DIR = os.path.join(PROJECT_ROOT, "dataset")

# ---------------------------------------------------------------- 精度测试
# 被测模型：完整的 chat/completions 地址
DEFAULT_ENDPOINT = "http://10.174.0.70:34000/v1/chat/completions"

# 明文密钥：更换时直接修改本行
DEFAULT_API_KEY = "sk-vgeocsgvmkutmzivcrkaiyxxjygwcazjozlgkpyqmamovclv"

DEFAULT_MODEL = "GLM-5.3-QJ"

# 裁判模型：可指向与被测模型不同的服务；端点/密钥留空表示复用被测模型配置
DEFAULT_JUDGE_ENDPOINT = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
DEFAULT_JUDGE_API_KEY = "61c65e97758f44d9b6b20157f88011b7.y0Wod1DhOffe1ipM"
DEFAULT_JUDGE_MODEL = "glm-5.3-flash"

# 运行参数
DEFAULT_THINKING = ""              # "" 表示不传该字段，由服务端默认决定
DEFAULT_REASONING_EFFORT = ""
DEFAULT_CONCURRENCY = 8
DEFAULT_JUDGE_CONCURRENCY = 8
DEFAULT_LIMIT = 0                  # 0 = 全量
DEFAULT_SEED = 0
DEFAULT_MAX_TOKENS = 32768
DEFAULT_MAX_TOKENS_CAP = 65536
DEFAULT_MAX_INCOMPLETE_ATTEMPTS = 2

# ---------------------------------------------------------------- 压力测试
DEFAULT_STRESS_ENDPOINT = DEFAULT_ENDPOINT
DEFAULT_STRESS_API_KEY = DEFAULT_API_KEY
DEFAULT_STRESS_NAME = "本地模型压测"
DEFAULT_STRESS_MODEL = DEFAULT_MODEL
DEFAULT_STRESS_MAX_TOKENS = 512
DEFAULT_STRESS_TEMPERATURE = 0.0
DEFAULT_STRESS_TOP_P = 1.0
DEFAULT_STRESS_TOP_K = -1          # -1 = 不注入该字段
DEFAULT_STRESS_TIMEOUT = 120
DEFAULT_STRESS_PROMPT = "请用一句话介绍你自己。"
DEFAULT_STRESS_TURNS = 1
DEFAULT_STRESS_CONCURRENCY_MODE = "fixed"
DEFAULT_STRESS_CONCURRENT_USERS = 8
# 负载口径：表单只填「每个并发的请求数」，服务端换算成 evalscope 的 --number
# （= 并发数 × 每个并发的请求数）；固定并发只有一档，阶梯模式按各档并发分别换算
DEFAULT_STRESS_ITERATIONS = 3
DEFAULT_STRESS_RAMP_LEVELS_TEXT = "2, 4, 8, 16"
DEFAULT_STRESS_RAMP_PAUSE_SECONDS = 10

# ------------------------------------------------------------ Agent 评测（DeepSWE）
# 用 pier 在独立容器里跑 SWE 类编码 agent 任务（解题 → 应用测试补丁 → 判定 reward）
DEFAULT_AGENT_ENDPOINT = DEFAULT_ENDPOINT
DEFAULT_AGENT_API_KEY = DEFAULT_API_KEY
DEFAULT_AGENT_MODEL = DEFAULT_MODEL
DEFAULT_AGENT_NAME = "mini-swe-agent"
# mini-swe-agent 对 openai/ 前缀的模型默认走 Responses API，本仓库的服务只提供
# chat/completions，因此固定用 litellm 通道（可通过表单覆盖）
DEFAULT_AGENT_MODEL_CLASS = "litellm"
# 单个任务的镜像约数 GB、agent 超时以小时计，因此默认只抽样 1 个任务
DEFAULT_AGENT_N_TASKS = 1
DEFAULT_AGENT_CONCURRENCY = 2
DEFAULT_AGENT_SEED = 0
DEFAULT_AGENT_TIMEOUT_MULTIPLIER = 1.0
DEFAULT_AGENT_VERIFIER_TIMEOUT_MULTIPLIER = 1.0
DEFAULT_AGENT_POLL_SECONDS = 3.0      # 轮询 pier 结果文件的间隔

# 任务镜像（如 alexgshaw/xxx:tag）直连 registry-1.docker.io 在内网会超时。这里按顺序尝试
# 镜像加速器：先拉 `<加速器>/<原镜像>`，再 tag 回任务声明的原名（pier 只认原名）。
# 可用 ZHIPU_EVAL_IMAGE_MIRRORS 覆盖（逗号分隔），设为空串表示只用本机已有镜像。
DEFAULT_IMAGE_MIRRORS = ("docker.1ms.run", "docker.xuanyuan.me", "docker.m.daocloud.io")
DEFAULT_IMAGE_PULL_TIMEOUT = 600      # 单个镜像拉取上限（秒）：镜像可达数 GB

# 请求模式：chat = 自定义会话提问；long = 长序列 + 固定前缀（Prefix Cache 命中率）
DEFAULT_STRESS_PROMPT_MODE = "chat"
# 长序列模式：分词器路径（evalscope 的 random 数据集按 token 精确生成，必需）
# 固定取项目 model/ 目录下的分词器目录，页面下拉框的候选即来自该目录
DEFAULT_STRESS_TOKENIZER_PATH = os.path.join(MODEL_DIR, "GLM-5.3")
# 序列长度（输入 tokens；支持 "128000"、"128K"、"1M"，K=1000）
DEFAULT_STRESS_SEQ_LENGTH = 128000
# 输出长度（tokens；映射 --min-tokens/--max-tokens，保证 TPOT 口径一致）
DEFAULT_STRESS_OUTPUT_LENGTH = 1024
# Prefix Cache 命中率（0~1）：固定前缀 = 序列长度 × 命中率，所有请求共享同一前缀
DEFAULT_STRESS_PREFIX_CACHE_RATIO = 0.5
# 固定前缀长度（tokens）；>0 时优先于命中率
DEFAULT_STRESS_PREFIX_LENGTH = 0
# 固定前缀内容（可选）：填写后改用 evalscope 的长上下文前缀注入，前缀内容完全一致
DEFAULT_STRESS_PREFIX_CONTENT = ""
# 预热请求数（0 = 关闭）：先跑若干请求把共享前缀写入缓存
DEFAULT_STRESS_WARMUP_NUM = 0
# 强制按输出长度生成（对应 evalscope --extra-args '{"ignore_eos":true}'）
DEFAULT_STRESS_IGNORE_EOS = False
