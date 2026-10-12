"""SimpleQA-Verified 精度评测 (Pass@1)

带 502 重试、断点续跑、分阶段落盘。

用法:
  python3 simpleqa_eval.py --model GLM-5.2-zhipu --judge-model DeepSeek-V4-Flash-0731 \
      --concurrency 8 --limit 1000 --out results/GLM-5.2-zhipu

产出:
  <out>.gen.jsonl    模型作答逐题记录(可续跑)
  <out>.judge.jsonl  裁判逐题记录(可续跑)
  <out>.json         汇总指标
"""
import argparse
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

ENDPOINT = os.environ.get("ENDPOINT", "http://10.174.0.70:34000/v1/chat/completions")
API_KEY = os.environ.get(
    "API_KEY", "sk-vgeocsgvmkutmzivcrkaiyxxjygwcazjozlgkpyqmamovclv"
)

ANSWER_PROMPT = """Your response should be in the following format:
Explanation: {{your explanation for your final answer}}
Exact Answer: {{your succinct, final answer}}
Confidence: {{your confidence score between 0% and 100% for your answer}}

Question: {question}"""

GRADER_TEMPLATE = """Your job is to look at a question, a gold target, and a predicted answer, and then return a letter grade of either [Correct] or [Incorrect] or [Not_Attempted]. Before giving a grade, first write a brief explanation. Then give the grade.

Note that the predicted answer may contain additional information, and it is okay if it contains additional information as long as the information is correct and the answer is correct.

The gold target may be of the form "X, Y, or Z", in which case any of X, Y, or Z is a correct answer. The gold target may also list several acceptable answers.

Use [Not_Attempted] when the predicted answer is a refusal, an expression of uncertainty, or otherwise does not attempt to answer. Use [Incorrect] when the model attempts an answer but it is wrong.

Question: {question}
Gold target: {target}
Predicted answer: {predicted_answer}

First write your explanation, then on a final line output exactly one of: [Correct], [Incorrect], [Not_Attempted]"""

class HttpError(RuntimeError):
    """带状态码的 HTTP 错误，便于区分 502/429 等重试策略。"""

    def __init__(self, status, body):
        super().__init__(f"http {status}: {body}")
        self.status = status


try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _ENC = None

_TOKEN_CACHE = {}
_TOKEN_CACHE_LOCK = threading.Lock()


def count_tokens(text):
    """估算可见回答的 token 数。思考内容不流式返回，只能用可见文本反推。"""
    if not text:
        return 0
    if _ENC is None:
        return max(1, len(text) // 4)
    with _TOKEN_CACHE_LOCK:
        hit = _TOKEN_CACHE.get(text)
    if hit is not None:
        return hit
    n = len(_ENC.encode(text))
    with _TOKEN_CACHE_LOCK:
        if len(_TOKEN_CACHE) < 20000:
            _TOKEN_CACHE[text] = n
    return n


_local = threading.local()
_io_lock = threading.Lock()


def _session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update(
            {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
        )
        _local.s = s
    return _local.s


def _post_stream(model, prompt, max_tokens, temperature=0.0):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    ttft = None
    parts, usage, finish = [], {}, None
    with _session().post(ENDPOINT, json=payload, timeout=1800, stream=True) as r:
        if r.status_code != 200:
            raise HttpError(r.status_code, r.text[:200])
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            chunk = json.loads(body)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    parts.append(piece)
    total = time.perf_counter() - t0
    return "".join(parts), usage, (ttft if ttft is not None else total), total, finish


def _backoff(k, status=None):
    """402/429 限流退避要长；502/503/504 属上游抖动，退避短一些快速重试。"""
    if status in (429, 402):
        return min(5.0 * (k + 1), 60.0) + random.random()
    if status in (500, 502, 503, 504):
        return min(1.0 + 0.7 * k, 15.0) + random.random()
    return min(1.5 + 0.8 * k, 25.0) + random.random()


def call_stream(model, prompt, max_tokens=32768, retries=20, tag=""):
    last = None
    for k in range(retries):
        try:
            return _post_stream(model, prompt, max_tokens)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(_backoff(k, getattr(e, "status", None)))
    raise RuntimeError(f"stream failed after {retries}: {last} {tag}")


def call_json(model, prompt, max_tokens=2048, retries=8, temperature=0.0):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    last = None
    for k in range(retries):
        try:
            r = _session().post(ENDPOINT, json=payload, timeout=1800)
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(1.0 + 0.5 * k, 6) + random.random())
    raise RuntimeError(f"chat failed after {retries}: {last}")


def parse_grade(text):
    low = (text or "").lower()
    pos, grade = -1, "NOT_ATTEMPTED"
    for label, name in (
        ("[correct]", "CORRECT"),
        ("[incorrect]", "INCORRECT"),
        ("[not_attempted]", "NOT_ATTEMPTED"),
    ):
        idx = low.rfind(label)
        if idx > pos:
            pos, grade = idx, name
    return grade


def extract_exact_answer(text):
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        if ln.lower().startswith("exact answer"):
            return ln.split(":", 1)[-1].strip()
    return lines[-1] if lines else ""


def load_jsonl(path, key):
    done = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    done[rec[key]] = rec
                except json.JSONDecodeError:
                    continue
    return done


def stuck_indexes(path, limit):
    """统计已被标记 incomplete 的次数，达到上限的题目视为终态，不再无限重跑。"""
    counts = {}
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("incomplete"):
                idx = rec["original_index"]
                counts[idx] = counts.get(idx, 0) + 1
    return {k for k, v in counts.items() if v >= limit}


def append_jsonl(path, rec):
    with _io_lock:
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--judge-model", required=True)
    ap.add_argument("--data", default="dataset/SimpleQA-Verified/simpleqa_verified.csv")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--judge-concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=32768,
                    help="首轮生成上限；思考模式截断时会自动翻倍重试")
    ap.add_argument("--max-tokens-cap", type=int, default=65536)
    ap.add_argument("--max-incomplete-attempts", type=int, default=2,
                    help="同一题被反复判定截断后放弃重做的次数")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df = pd.read_csv(args.data)
    if args.limit and args.limit < len(df):
        df = df.sample(n=args.limit, random_state=args.seed).sort_index()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    gen_path, judge_path = args.out + ".gen.jsonl", args.out + ".judge.jsonl"

    # ---------- 阶段一: 生成 ----------
    # incomplete=True 的记录(思考被 max_tokens 截断/空回答)不算完成，续跑时会重做
    cached = load_jsonl(gen_path, "original_index")
    done = {k: v for k, v in cached.items() if not v.get("incomplete")}
    stuck = stuck_indexes(gen_path, args.max_incomplete_attempts)
    todo = [
        r for r in df.to_dict("records")
        if int(r["original_index"]) not in done
        and int(r["original_index"]) not in stuck
    ]
    print(
        f"[gen] model={args.model} total={len(df)} cached_valid={len(done)} "
        f"cached_incomplete={len(cached) - len(done)} stuck={len(stuck)} "
        f"todo={len(todo)} concurrency={args.concurrency} "
        f"max_tokens={args.max_tokens}",
        flush=True,
    )

    def gen(row):
        prompt = ANSWER_PROMPT.format(question=row["problem"])
        tag = f"q{row['original_index']}"
        mt = args.max_tokens
        while True:
            text, usage, ttft, total, finish = call_stream(
                args.model, prompt, max_tokens=mt, tag=tag
            )
            out_tok = usage.get("completion_tokens") or 0
            # 思考占满上限 => 答案还没写出来，翻倍上限重试一轮
            if finish == "length" and mt < args.max_tokens_cap:
                print(f"  [truncated] {tag} hit {mt} tok, retry with {mt * 2}", flush=True)
                mt = min(mt * 2, args.max_tokens_cap)
                continue
            break
        # 思考 token 不流式输出，需用可见文本 token 数做分母
        content_tokens = count_tokens(text)
        tpot = (total - ttft) / (content_tokens - 1) if content_tokens > 1 else None
        detail = usage.get("completion_tokens_details") or {}
        incomplete = finish == "length" or not text.strip()
        return {
            "original_index": int(row["original_index"]),
            "problem": row["problem"],
            "answer": row["answer"],
            "topic": row["topic"],
            "answer_type": row["answer_type"],
            "prediction": text,
            "exact_answer": extract_exact_answer(text),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": out_tok,
            "content_tokens": content_tokens,
            "reasoning_tokens": detail.get("reasoning_tokens")
            or max(out_tok - content_tokens, 0),
            "finish_reason": finish,
            "max_tokens_used": mt,
            "incomplete": incomplete,
            "ttft_s": round(ttft, 4),
            "total_s": round(total, 4),
            "tpot_visible_s": round(tpot, 5) if tpot else None,
        }

    if todo:
        t0 = time.perf_counter()
        errors, done_n, toks = [], 0, 0
        with ThreadPoolExecutor(args.concurrency) as ex:
            futs = {ex.submit(gen, r): r for r in todo}
            for f in as_completed(futs):
                row = futs[f]
                try:
                    rec = f.result()
                except Exception as e:  # noqa: BLE001
                    errors.append((int(row["original_index"]), str(e)[:150]))
                    continue
                append_jsonl(gen_path, rec)
                done_n += 1
                toks += rec["completion_tokens"]
                if done_n % 25 == 0:
                    el = time.perf_counter() - t0
                    print(
                        f"  gen {done_n}/{len(todo)} elapsed={el/60:.1f}min "
                        f"tput={toks/el:.1f}tps",
                        flush=True,
                    )
        el = time.perf_counter() - t0
        print(
            f"[gen] done={done_n} errors={len(errors)} elapsed={el/60:.1f}min "
            f"tput={toks/max(el,1):.1f}tps",
            flush=True,
        )
        if errors:
            with open(args.out + ".gen_errors.json", "w") as f:
                json.dump(errors, f, ensure_ascii=False, indent=2)

    gen_all = load_jsonl(gen_path, "original_index")
    gen_recs = {k: v for k, v in gen_all.items() if not v.get("incomplete")}
    incomplete_n = len(gen_all) - len(gen_recs)
    failed_n = len(df) - len(gen_all)
    print(
        f"[gen] valid={len(gen_recs)}/{len(df)} incomplete={incomplete_n} "
        f"failed={failed_n}",
        flush=True,
    )

    # ---------- 阶段二: 裁判 ----------
    judged = load_jsonl(judge_path, "original_index")
    to_judge = [r for r in gen_recs.values() if r["original_index"] not in judged]
    print(
        f"[judge] model={args.judge_model} cached={len(judged)} todo={len(to_judge)} "
        f"concurrency={args.judge_concurrency}",
        flush=True,
    )

    def judge(rec):
        out = call_json(
            args.judge_model,
            GRADER_TEMPLATE.format(
                question=rec["problem"],
                target=rec["answer"],
                predicted_answer=rec["prediction"] or "(empty)",
            ),
        )
        return {"original_index": rec["original_index"], "judge_output": out,
                "grade": parse_grade(out)}

    if to_judge:
        t0 = time.perf_counter()
        jerr, done_n = [], 0
        with ThreadPoolExecutor(args.judge_concurrency) as ex:
            futs = {ex.submit(judge, r): r for r in to_judge}
            for f in as_completed(futs):
                rec = futs[f]
                try:
                    jr = f.result()
                except Exception as e:  # noqa: BLE001
                    jerr.append((rec["original_index"], str(e)[:150]))
                    continue
                append_jsonl(judge_path, jr)
                done_n += 1
                if done_n % 100 == 0:
                    print(f"  judge {done_n}/{len(to_judge)}", flush=True)
        print(
            f"[judge] done={done_n} errors={len(jerr)} "
            f"elapsed={(time.perf_counter()-t0)/60:.1f}min",
            flush=True,
        )

    judged = load_jsonl(judge_path, "original_index")

    # ---------- 汇总 ----------
    records = []
    for idx, rec in gen_recs.items():
        j = judged.get(idx)
        if not j:
            continue
        rec = dict(rec)
        rec["grade"] = j["grade"]
        records.append(rec)
    if not records:
        raise SystemExit("no records to summarise")

    dfr = pd.DataFrame(records)
    n = len(records)
    c = int((dfr.grade == "CORRECT").sum())
    ic = int((dfr.grade == "INCORRECT").sum())
    na = int((dfr.grade == "NOT_ATTEMPTED").sum())
    toks = dfr.completion_tokens.tolist()
    ttfts = dfr.ttft_s.tolist()
    tpots = (dfr.tpot_visible_s.dropna().tolist()
             if "tpot_visible_s" in dfr else [])
    walls = dfr.total_s.tolist()

    def pct(v, p):
        v = sorted(v)
        return v[min(int(len(v) * p), len(v) - 1)]

    summary = {
        "model": args.model,
        "judge_model": args.judge_model,
        "dataset": os.path.basename(args.data),
        "num_questions": n,
        "expected_questions": len(df),
        "coverage": round(n / len(df), 4),
        "num_incomplete": incomplete_n,
        "num_failed": failed_n,
        "temperature": 0.0,
        "counts": {"CORRECT": c, "INCORRECT": ic, "NOT_ATTEMPTED": na},
        "pass_at_1": round(c / n, 4),
        "pass_at_1_ci95": [
            round(c / n - 1.96 * ((c / n) * (1 - c / n) / n) ** 0.5, 4),
            round(c / n + 1.96 * ((c / n) * (1 - c / n) / n) ** 0.5, 4),
        ],
        "accuracy_attempted": round(c / (c + ic), 4) if (c + ic) else None,
        "not_attempted_rate": round(na / n, 4),
        "output_tokens_avg": round(sum(toks) / n, 1),
        "output_tokens_p50": round(pct(toks, 0.5)),
        "visible_tokens_avg": (
            round(float(dfr.content_tokens.mean()), 1)
            if "content_tokens" in dfr else None
        ),
        "reasoning_tokens_avg": (
            round(float(dfr.reasoning_tokens.dropna().mean()), 1)
            if "reasoning_tokens" in dfr and dfr.reasoning_tokens.notna().any()
            else None
        ),
        "ttft_visible_p50_s": round(pct(ttfts, 0.5), 2),
        "ttft_visible_p90_s": round(pct(ttfts, 0.9), 2),
        "tpot_visible_p50_s": round(pct(tpots, 0.5), 4) if tpots else None,
        "e2e_latency_p50_s": round(pct(walls, 0.5), 2),
        "e2e_latency_p90_s": round(pct(walls, 0.9), 2),
    }
    summary["by_topic"] = {
        t: {"n": int(len(g)), "pass_at_1": round(float((g.grade == "CORRECT").mean()), 4)}
        for t, g in dfr.groupby("topic")
    }
    summary["by_answer_type"] = {
        t: {"n": int(len(g)), "pass_at_1": round(float((g.grade == "CORRECT").mean()), 4)}
        for t, g in dfr.groupby("answer_type")
    }

    with open(args.out + ".json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(args.out + ".items.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("by_")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
