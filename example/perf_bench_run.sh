#!/usr/bin/env bash
set -euo pipefail

# 固定部署：GLM-5.3-w8a8c8，64卡PD分离；在evalscope-bench容器内执行。
MODEL_NAME="glm-5.3"
URL="http://10.174.0.25:9000/v1/chat/completions"
TOKENIZER_PATH="/models/tokenizer"
OUTPUT_LEN=1024

# 先从低并发探索；确认容量后再增加并发。
PARALLELS_128K="16"
PARALLELS_1024K="4"
NUMBER_128K=64
NUMBER_1024K=16
RESULT_DIR="/app/work/outputs/$(date +%Y%m%d_%H%M%S)"

# 参数：场景名、总输入tokens、共享前缀百分比、并发列表、正式请求数。
run_scene() {
  local SCENE="$1"
  local TOTAL_INPUT="$2"
  local CACHE_PERCENT="$3"
  local PARALLELS="$4"
  local NUMBER="$5"
  local PREFIX_LEN=$(( TOTAL_INPUT * CACHE_PERCENT / 100 ))
  local SUFFIX_LEN=$(( TOTAL_INPUT - PREFIX_LEN ))
  local PARALLEL WARMUP_NUM OUT_DIR

  for PARALLEL in $PARALLELS; do
    # 每个并发点单独预热：至少4条，且不少于2倍并发。
    WARMUP_NUM=$(( PARALLEL * 2 ))
    if (( WARMUP_NUM < 4 )); then
      WARMUP_NUM=4
    fi

    OUT_DIR="$RESULT_DIR/${SCENE}/C${PARALLEL}"
    mkdir -p "$OUT_DIR"
    echo "=== $SCENE | 输入=$TOTAL_INPUT | 前缀=$PREFIX_LEN | 后缀=$SUFFIX_LEN | 并发=$PARALLEL | 预热=$WARMUP_NUM ==="

    evalscope perf \
      --model "$MODEL_NAME" \
      --url "$URL" \
      --api openai \
      --dataset random \
      --tokenizer-path "$TOKENIZER_PATH" \
      --prefix-length "$PREFIX_LEN" \
      --min-prompt-length "$SUFFIX_LEN" \
      --max-prompt-length "$SUFFIX_LEN" \
      --min-tokens "$OUTPUT_LEN" \
      --max-tokens "$OUTPUT_LEN" \
      --parallel "$PARALLEL" \
      --number "$NUMBER" \
      --warmup-num "$WARMUP_NUM" \
      --stream \
      --temperature 0 \
      --read-timeout 3600 \
      --extra-args '{"ignore_eos":true}' \
      --outputs-dir "$OUT_DIR" \
      2>&1 | tee "$OUT_DIR/client.log"
  done
}

# 顺序与Excel一致；K=1000，输出保持1024 tokens。
# 高吞吐：TTFT<=10s，TPOT<=50ms。
run_scene T1_128K_cache50_high_tps 128000 50 "$PARALLELS_128K" "$NUMBER_128K"
run_scene T2_128K_cache90_high_tps 128000 90 "$PARALLELS_128K" "$NUMBER_128K"

# 低时延：TTFT<=10s，TPOT<=30ms。
# run_scene T3_128K_cache50_low_latency 128000 50 "$PARALLELS_128K" "$NUMBER_128K"
# run_scene T4_128K_cache90_low_latency 128000 90 "$PARALLELS_128K" "$NUMBER_128K"

# 最长序列：TTFT<=40s，TPOT<=50ms。
# run_scene T5_1024K_cache50_long_context 1024000 50 "$PARALLELS_1024K" "$NUMBER_1024K"
# run_scene T6_1024K_cache90_long_context 1024000 90 "$PARALLELS_1024K" "$NUMBER_1024K"

echo "执行结束，结果目录：$RESULT_DIR"
