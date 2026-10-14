"""被测服务 / 压测目标的连接默认值（端点、API Key）——集中在本文件维护。

只放纯字面量：需要指向别的服务或更换密钥时，只改这里，调用方按需导入即可。
- DEFAULT_ENDPOINT：精度测试用的完整 chat/completions 地址；
- DEFAULT_STRESS_ENDPOINT：压测用端点。llm-grill 固定请求 /v1/chat/completions，
  多填的路径会被归一化掉，因此填到端口即可，与 DEFAULT_ENDPOINT 同值也可用。
"""

DEFAULT_ENDPOINT = "http://10.174.0.70:34000/v1/chat/completions"

# 明文密钥：需要更换时直接改这一行
DEFAULT_API_KEY = "sk-vgeocsgvmkutmzivcrkaiyxxjygwcazjozlgkpyqmamovclv"

# 压测默认复用被测服务的端点/密钥
DEFAULT_STRESS_ENDPOINT = DEFAULT_ENDPOINT
DEFAULT_STRESS_API_KEY = DEFAULT_API_KEY
