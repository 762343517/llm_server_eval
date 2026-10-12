"""SimpleQA-Verified 精度评测 (Pass@1) —— 命令行入口

带 502 重试、思考截断自动扩容、断点续跑、分阶段落盘。
核心逻辑见 eval_core.py，Web 服务见 webapp.py。

用法:
  python3 simpleqa_eval.py --model GLM-5.2-zhipu --judge-model DeepSeek-V4-Flash-0731 \
      --concurrency 8 --limit 1000 --out results/GLM-5.2-zhipu

思考开关（默认不传，由服务端决定）:
  --thinking / --no-thinking                被测模型是否开思考
  --reasoning-effort low|medium|high|max    被测模型思考强度
  --judge-thinking / --no-judge-thinking    裁判模型是否开思考
  --judge-reasoning-effort ...              裁判模型思考强度

产出:
  <out>.gen.jsonl    模型作答逐题记录(可续跑)
  <out>.judge.jsonl  裁判逐题记录(可续跑)
  <out>.json         汇总指标
  <out>.items.jsonl  逐题明细(含 grade)
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_core  # noqa: E402

DEFAULT_ENDPOINT = os.environ.get(
    "ENDPOINT", "http://10.174.0.70:34000/v1/chat/completions"
)
DEFAULT_API_KEY = os.environ.get(
    "API_KEY", "sk-vgeocsgvmkutmzivcrkaiyxxjygwcazjozlgkpyqmamovclv"
)


def make_printer():
    """把 run_eval 事件还原成原有命令行日志格式。"""
    state = {"t0": 0.0, "judge_t0": 0.0, "last_gen": 0, "last_judge": 0}

    def on_event(kind, p):
        if kind == "gen_start":
            state["t0"] = time.perf_counter()
            print(
                f"[gen] model={p['model']} total={p['total']} "
                f"cached_valid={p['cached_valid']} "
                f"cached_incomplete={p['cached_incomplete']} stuck={p['stuck']} "
                f"todo={p['todo']} concurrency={p['concurrency']} "
                f"max_tokens={p['max_tokens']} thinking={p.get('thinking', '-')}",
                flush=True,
            )
        elif kind == "truncated":
            print(
                f"  [truncated] {p['tag']} hit {p['hit']} tok, "
                f"retry with {p['retry']}",
                flush=True,
            )
        elif kind == "gen_progress":
            if p["done"] - state["last_gen"] >= 25 or not p["total"]:
                state["last_gen"] = p["done"]
                print(
                    f"  gen {p['done']}/{p['total']} "
                    f"elapsed={p['elapsed_s']/60:.1f}min tput={p['tps']:.1f}tps",
                    flush=True,
                )
        elif kind == "gen_end":
            print(
                f"[gen] done={p['done']} errors={p['errors']} "
                f"elapsed={p['elapsed_s']/60:.1f}min tput={p['tps']:.1f}tps",
                flush=True,
            )
        elif kind == "gen_summary":
            print(
                f"[gen] valid={p['valid']}/{p['expected']} "
                f"incomplete={p['incomplete']} failed={p['failed']}",
                flush=True,
            )
        elif kind == "judge_start":
            state["judge_t0"] = time.perf_counter()
            print(
                f"[judge] model={p['model']} cached={p['cached']} "
                f"todo={p['todo']} concurrency={p['concurrency']} "
                f"thinking={p.get('thinking', '-')}",
                flush=True,
            )
        elif kind == "judge_progress":
            if p["done"] - state["last_judge"] >= 100:
                state["last_judge"] = p["done"]
                print(f"  judge {p['done']}/{p['total']}", flush=True)
        elif kind == "judge_end":
            print(
                f"[judge] done={p['done']} errors={p['errors']} "
                f"elapsed={p['elapsed_s']/60:.1f}min",
                flush=True,
            )
        elif kind == "item_failed":
            print(f"  [{p['stage']}-fail] q{p['index']}: {p['error']}", flush=True)
        elif kind == "error":
            print(f"[error] {p['message']}", flush=True)

    return on_event


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--judge-model", required=True)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--api-key", default=DEFAULT_API_KEY)
    ap.add_argument("--judge-endpoint", default="",
                    help="裁判模型端点；留空则复用 --endpoint")
    ap.add_argument("--judge-api-key", default="",
                    help="裁判模型密钥；留空则复用 --api-key")
    ap.add_argument("--data", default=eval_core.DEFAULT_DATA,
                    help="数据集路径：.csv 按 SimpleQA 结构，.parquet 按 HLE 结构")
    ap.add_argument("--thinking", dest="thinking", action="store_const",
                    const="enabled", default="",
                    help="被测模型开启思考（默认不传该字段，由服务端决定）")
    ap.add_argument("--no-thinking", dest="thinking", action="store_const",
                    const="disabled", help="被测模型关闭思考")
    ap.add_argument("--reasoning-effort", default="",
                    choices=list(eval_core.EFFORT_CHOICES),
                    help="被测模型思考强度；仅开启思考时生效（默认不传）")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--judge-concurrency", type=int, default=8)
    ap.add_argument("--judge-thinking", dest="judge_thinking",
                    action="store_const", const="enabled", default="",
                    help="裁判模型开启思考（默认不传该字段）")
    ap.add_argument("--no-judge-thinking", dest="judge_thinking",
                    action="store_const", const="disabled",
                    help="裁判模型关闭思考，判分更快更省 token")
    ap.add_argument("--judge-reasoning-effort", default="",
                    choices=list(eval_core.EFFORT_CHOICES),
                    help="裁判模型思考强度；仅开启思考时生效（默认不传）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=32768,
                    help="首轮生成上限；思考模式截断时会自动翻倍重试")
    ap.add_argument("--max-tokens-cap", type=int, default=65536)
    ap.add_argument("--max-incomplete-attempts", type=int, default=2,
                    help="同一题被反复判定截断后放弃重做的次数")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cfg = {
        "endpoint": args.endpoint,
        "api_key": args.api_key,
        "judge_endpoint": args.judge_endpoint,
        "judge_api_key": args.judge_api_key,
        "model": args.model,
        "judge_model": args.judge_model,
        "data": args.data,
        "out": args.out,
        "thinking": args.thinking,
        "reasoning_effort": args.reasoning_effort,
        "judge_thinking": args.judge_thinking,
        "judge_reasoning_effort": args.judge_reasoning_effort,
        "concurrency": args.concurrency,
        "judge_concurrency": args.judge_concurrency,
        "limit": args.limit,
        "seed": args.seed,
        "max_tokens": args.max_tokens,
        "max_tokens_cap": args.max_tokens_cap,
        "max_incomplete_attempts": args.max_incomplete_attempts,
    }

    summary = eval_core.run_eval(cfg, on_event=make_printer())
    if summary is None:
        raise SystemExit("no records to summarise")
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("by_")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
