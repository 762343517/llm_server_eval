"""用户可配置的默认值（被测/裁判/压测目标的端点、API Key、模型与运行参数）。

只放纯字面量：要指向别的服务、换密钥、改默认模型或默认并发时，只改这里，调用方按需导入。

- 精度测试：被测与裁判是两套独立的端点/密钥/模型，裁判端点留空表示复用被测的那一套；
- 压力测试：压测目标默认复用被测服务（llm-grill 固定请求 /v1/chat/completions，
  多填的路径会被归一化掉，因此填到端口即可）；
- 这些只是「表单 / 命令行的初始值」，用户在页面提交过配置后由 appconfig 读取的
  last_config 覆盖。
"""

# ---------------------------------------------------------------- 精度测试
# 被测模型：完整 chat/completions 地址
DEFAULT_ENDPOINT = "http://10.174.0.70:34000/v1/chat/completions"

# 明文密钥：需要更换时直接改这一行
DEFAULT_API_KEY = "sk-vgeocsgvmkutmzivcrkaiyxxjygwcazjozlgkpyqmamovclv"

DEFAULT_MODEL = "GLM-5.2-zhipu"

# 裁判模型：可与被测指向不同服务；端点/密钥留空 = 复用被测模型的
DEFAULT_JUDGE_ENDPOINT = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
DEFAULT_JUDGE_API_KEY = "61c65e97758f44d9b6b20157f88011b7.y0Wod1DhOffe1ipM"
DEFAULT_JUDGE_MODEL = "glm-5.3-flash"

# 运行参数
DEFAULT_THINKING = ""              # "" = 不传该字段，由服务端决定
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
DEFAULT_STRESS_BACKEND_NAME = "target"
DEFAULT_STRESS_BACKEND_TYPE = "openai"
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
DEFAULT_STRESS_ITERATIONS = 3
DEFAULT_STRESS_RAMP_LEVELS_TEXT = "2, 4, 8, 16"
DEFAULT_STRESS_RAMP_PAUSE_SECONDS = 10
