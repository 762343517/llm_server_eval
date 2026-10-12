#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SimpleQA Verified 评测脚本 — DeepSeek V4 API 版
=================================================

本脚本改造自 Kaggle 上 Nan Liao 维护的
"SimpleQA Verified Benchmark Starter Code"
(https://www.kaggle.com/code/nanliao7/simpleqa-verified-benchmark-starter-code)

主要改动：
1. 把所有对 OpenAI API 的调用改为 DeepSeek V4 的 OpenAI 兼容接口
   - base_url: https://api.deepseek.com
   - 测试模型默认 deepseek-v4-pro；可切换 deepseek-v4-flash
   - 评分模型默认 deepseek-v4-pro（与测试模型解耦，可独立设置）
2. 把 Kaggle notebook 重写成纯 .py 文件，提供 CLI 入口
3. 增加并发、重试、断点续跑、CSV/JSONL 输出
4. 评分 prompt 沿用官方 SIMPLEQA_VERIFIED_GRADER_TEMPLATE
   （来自 OpenAI simple-evals + UK AISI inspect_evals）

依赖：
    pip install openai pandas datasets tqdm tenacity

环境变量：
    DEEPSEEK_API_KEY  -- DeepSeek API Key（必填）
    HF_ENDPOINT       -- 可选，默认 https://hf-mirror.com（国内）
    OPENAI_API_KEY    -- 可选；如果你想把 grader 切回 gpt-4.1 才需要

使用示例：
    # 全量 1000 题跑一遍
    python simpleqa_verified_deepseekv4.py

    # 先跑 10 题 smoke test
    python simpleqa_verified_deepseekv4.py --limit 10

    # 用 Flash 跑测试，Pro 跑评分
    python simpleqa_verified_deepseekv4.py --test-model deepseek-v4-flash --grader-model deepseek-v4-pro

    # 关掉 thinking 模式，省时省 token
    python simpleqa_verified_deepseekv4.py --no-thinking

    # 中断后接着跑
    python simpleqa_verified_deepseekv4.py --resume
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from openai import AsyncOpenAI, OpenAI
from tqdm import tqdm

# -----------------------------------------------------------------------------
# 常量与配置
# -----------------------------------------------------------------------------

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_TEST_MODEL = "deepseek-v4-pro"
DEFAULT_GRADER_MODEL = "deepseek-v4-pro"
DEFAULT_DATASET = "google/simpleqa-verified"
# 备用：本地已经下载过的 csv（用脚本相对路径找）
_LOCAL_CSV_CANDIDATES = [
    Path("outputs/deepseek_v4_eval_data/benchmarks/SimpleQA-Verified/simpleqa_verified.csv"),
    Path(__file__).parent / "deepseek_v4_eval_data" / "benchmarks" / "SimpleQA-Verified" / "simpleqa_verified.csv",
    Path(__file__).parent / "benchmarks" / "SimpleQA-Verified" / "simpleqa_verified.csv",
]
LOCAL_CSV = next((p for p in _LOCAL_CSV_CANDIDATES if p.exists()), _LOCAL_CSV_CANDIDATES[0])


# -----------------------------------------------------------------------------
# Prompt 模板（与 OpenAI simple-evals / inspect_evals 保持一致）
# -----------------------------------------------------------------------------

# 给被测模型的问题包装
TEST_SYSTEM_PROMPT = (
    "You are a helpful Assistant that answers short, fact-seeking questions "
    "as concisely and accurately as possible. Answer in 1-3 sentences, no "
    "extra commentary."
)

# 给评分模型的 prompt（SimpleQA Verified 官方模板）
# 与 inspect_evals/simpleqa/grader_templates.py 中 SIMPLEQA_VERIFIED_GRADER_TEMPLATE 同源
GRADER_TEMPLATE = """
Your job is to look at a question, a gold target, and a predicted answer, and then
assign a grade of either ["CORRECT", "INCORRECT", "NOT_ATTEMPTED"].
First, I will give examples of each grade, and then you will grade a new example.
The following are examples of CORRECT predicted answers.
'''
Question: What are the names of Barack Obama's children?
Gold target: Malia Obama and Sasha Obama
Predicted answer 1: sasha and malia obama
Predicted answer 2: most people would say Malia and Sasha, but I'm not sure and
have to double check
Predicted answer 3: Barack Obama has two daughters. Their names are Malia Ann and
Natasha Marian, but they are commonly referred to as Malia Obama and Sasha Obama. Malia
was born on July 4, 1998, and Sasha was born on June 10, 2001.
Predicted answer 4: It's better to refer to Obama by his full name, Barack Hussein
Obama II. He has two daughters: Malia and Sasha.
'''
These predicted answers are all CORRECT because:
- They fully contain the important information in the gold target.
- They do not contain any information that contradicts the gold target.
- Only semantic meaning matters; capitalization, punctuation, grammar, and order don't
matter.
- Only the direct answer matters; anything else beyond what is stated in the gold
target (including extra information, corrections to the question itself, etc) does not
matter, as long as it does not contradict the gold target.
- Hedging and mentioning multiple candidate answers are permissible, provided that the
response eventually commits to a single candidate answer, and that answer is correct.
The following are examples of INCORRECT predicted answers.
'''
Question: What are the names of Barack Obama's children?
Gold target: Malia and Sasha
Predicted answer 1: Malia.
Predicted answer 2: Malia, Sasha, and Susan.
Predicted answer 3: Barack Obama does not have any children.
Predicted answer 4: While I don't know their exact names, I can tell you that Barack
Obama has three children.
Predicted answer 5: It's possible you may mean Betsy and Olivia. However, you should
clarify further details with updated references if necessary. Is that the correct answer
?
Predicted answer 6: It may be the case that Obama's child is named James. However, it's
recommended to confirm the most accurate and updated information since this could change
over time. This model may not always reflect the most current information.
'''
These predicted answers are all INCORRECT because:
- A factual statement in the answer contradicts the gold target.
- A hedged statement (e.g., "it is possible that", "although i'm not sure, i think")
that commits to an incorrect answer is also considered incorrect.
The following are examples of NOT_ATTEMPTED predicted answers.
'''
Question: What are the names of Barack Obama's children?
Gold target: Malia and Sasha
Predicted answer 1: I don't know.
Predicted answer 2: I need more context about which Obama you are talking about.
Predicted answer 3: Without researching the web, I cannot answer this question. However,
I can tell you that Barack Obama has two children.
Predicted answer 4: Barack Obama has two children. I know that one of them is Malia, but
I'm not sure about the other one.
Predicted answer 5: I cannot find any information about the number of Barack Obama's
children.
Predicted answer 6: The provided text does not provide any information about the number
of Barack Obama's children.
Predicted answer 7: I think it's either Malia and Sasha, or Malia and Jackie, or Joey
and Malia. I'm not sure which one.
'''
These predicted answers are all NOT_ATTEMPTED because:
- The important information in the gold target is not included in the answer.
- No statements in the answer contradict the gold target.
- Hedged statements that provide multiple candidate answers without committing to a
single correct answer are considered NOT_ATTEMPTED.
Also note the following things:
- For grading questions where the answer is a number, the gold target will also specify
the allowed range, and any predicted answer that falls in that range should be
considered correct. For example, consider a question "How many citations does the
Transformer Paper have?" with gold target "120k (acceptable range: anything between
118k and 122k)".
- Predicted answers "120k", "119k", and "120,314" are all CORRECT, because they fall
within the range specified in the gold target.
- Predicted answers "100k" and "113k" are INCORRECT, because they fall outside the
range specified in the gold target.
- Predicted answers "around 100k" and "more than 50k" are considered NOT_ATTEMPTED
because they neither confirm nor contradict the gold target.
- The gold target may contain more information than the question. In such cases, the
predicted answer only needs to contain the information that is in the question.
- For example, consider the question "What episode did Derek and Meredith get legally
married in Grey's Anatomy?" with gold target "Season 7, Episode 20: White Wedding".
Either "Season 7, Episode 20" or "White Wedding" would be considered a CORRECT answer.
- Do not punish predicted answers if they omit information that would be clearly
inferred from the question.
- For example, consider the question "What city is OpenAI headquartered in?" and the
gold target "San Francisco, California". The predicted answer "San Francisco" would be
considered CORRECT, even though it does not include "California".
- Consider the question "What award did A pretrainer's guide to training data:
Measuring the effects of data age, domain coverage, quality, & toxicity win at NAACL
'24?", the gold target is "Outstanding Paper Award". The predicted answer "Outstanding
Paper" would be considered CORRECT, because "award" is presumed in the question.
- For the question "What is the height of Jason Wei in meters?", the gold target is
"1.73 m (acceptable range: anything between 1.72 m and 1.74 m)". The predicted answer
"1.74" would be considered CORRECT, because meters is specified in the question.
- For the question "What is the name of Barack Obama's wife?", the gold target is "
Michelle Obama". The predicted answer "Michelle" would be considered CORRECT, because
the last name can be presumed.
- Do not punish for typos in people's name if it's clearly the same name.
- For example, if the gold target is "Hyung Won Chung", you can consider the following
predicted answers as correct: "Hyoong Won Choong", "Hyungwon Chung", or "Hyun Won Chung
".
Here is a new example. Simply reply with either CORRECT, INCORRECT, NOT ATTEMPTED. Don't
apologize or correct yourself if there was a mistake; we are just trying to grade the
answer.
'''
Question: {question}
Gold target: {criterion}
Predicted answer: {answer}
'''

{instructions}
""".strip()

GRADING_INSTRUCTIONS = """
Grade the predicted answer of this new question as one of:
A: CORRECT
B: INCORRECT
C: NOT_ATTEMPTED

Just return the letters "A", "B", or "C", with no text around it.
""".strip()


# -----------------------------------------------------------------------------
# 数据类
# -----------------------------------------------------------------------------

@dataclass
class RowResult:
    """单条样本的处理结果。"""
    original_index: int
    problem: str
    gold: str
    topic: str
    answer_type: str
    multi_step: bool
    requires_reasoning: bool
    predicted: str = ""
    grade_letter: str = ""           # "A" / "B" / "C"
    grade_label: str = ""            # "CORRECT" / "INCORRECT" / "NOT_ATTEMPTED" / "ERROR"
    test_prompt_tokens: int = 0
    test_completion_tokens: int = 0
    grader_prompt_tokens: int = 0
    grader_completion_tokens: int = 0
    test_thinking: str = ""
    test_latency_s: float = 0.0
    grader_latency_s: float = 0.0
    error: str = ""
    raw_grader_response: str = ""


@dataclass
class Summary:
    correct: int = 0
    incorrect: int = 0
    not_attempted: int = 0
    errored: int = 0
    total: int = 0
    attempted: int = 0
    correct_given_attempted: float = 0.0
    accuracy: float = 0.0          # == correct / total (Epoch 口径)
    f_score: float = 0.0           # 2*P*R/(P+R)，SimpleQA 论文口径
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -----------------------------------------------------------------------------
# 数据加载
# -----------------------------------------------------------------------------

def load_dataset(
    local_csv: Optional[Path] = LOCAL_CSV,
    hf_name: str = DEFAULT_DATASET,
    hf_revision: Optional[str] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """优先从本地 csv 加载（前面下载过的），失败则从 HuggingFace 拉。"""
    df: Optional[pd.DataFrame] = None

    if local_csv and local_csv.exists():
        print(f"[dataset] reading local csv: {local_csv}")
        df = pd.read_csv(local_csv)
    else:
        print(f"[dataset] fetching {hf_name} from HuggingFace ...")
        # 国内镜像默认走 hf-mirror.com
        if "HF_ENDPOINT" not in os.environ:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from datasets import load_dataset
        # 先按指定 revision 拉；失败则退回默认 revision
        try:
            ds = load_dataset(hf_name, split="eval", revision=hf_revision) if hf_revision else \
                 load_dataset(hf_name, split="eval")
        except Exception as e:
            print(f"[dataset] 指定 revision 拉取失败：{e}")
            print(f"[dataset] 回退到默认 revision ...")
            ds = load_dataset(hf_name, split="eval")
        df = ds.to_pandas()

    # 标准化字段
    df = df.rename(columns={
        "problem": "problem",
        "answer": "gold",
    })
    # urls 字段可能不存在，补一列
    if "urls" not in df.columns:
        df["urls"] = ""
    # bool 化
    for col in ("multi_step", "requires_reasoning"):
        if col in df.columns:
            df[col] = df[col].astype(bool)
    if limit is not None and limit > 0:
        df = df.head(limit).reset_index(drop=True)
    print(f"[dataset] loaded {len(df)} rows; columns={list(df.columns)}")
    return df


# -----------------------------------------------------------------------------
# DeepSeek V4 调用封装
# -----------------------------------------------------------------------------

def make_client(api_key: Optional[str] = None, base_url: str = DEEPSEEK_BASE_URL) -> OpenAI:
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("ERROR: DEEPSEEK_API_KEY not set. Export it before running.")
    return OpenAI(api_key=key, base_url=base_url)


def make_async_client(api_key: Optional[str] = None, base_url: str = DEEPSEEK_BASE_URL) -> AsyncOpenAI:
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("ERROR: DEEPSEEK_API_KEY not set. Export it before running.")
    return AsyncOpenAI(api_key=key, base_url=base_url)


def _build_extra_body(use_thinking: bool, reasoning_effort: str) -> dict[str, Any]:
    """DeepSeek V4 的 thinking / reasoning 参数走 extra_body，OpenAI SDK 原生字段透传不过去。"""
    body: dict[str, Any] = {"thinking": {"type": "enabled" if use_thinking else "disabled"}}
    if use_thinking and reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    return body


def _extract_content_and_thinking(message: Any) -> tuple[str, str]:
    """OpenAI SDK 的 message 对象 + DeepSeek 思考模式：reply.content 是最终答案；
    reply.reasoning_content（仅当 thinking 启用时）是思考过程。"""
    content = getattr(message, "content", "") or ""
    thinking = getattr(message, "reasoning_content", "") or ""
    return content, thinking


def call_test_model_sync(
    client: OpenAI,
    model: str,
    question: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    use_thinking: bool = True,
    reasoning_effort: str = "high",
    timeout: float = 120.0,
) -> tuple[str, str, dict[str, int]]:
    """同步调用被测 DeepSeek V4 模型。返回 (answer, thinking, usage_dict)。"""
    extra = _build_extra_body(use_thinking, reasoning_effort)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": TEST_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        extra_body=extra,
    )
    msg = resp.choices[0].message
    content, thinking = _extract_content_and_thinking(msg)
    usage = {
        "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
    }
    return content, thinking, usage


def call_grader_sync(
    client: OpenAI,
    model: str,
    question: str,
    gold: str,
    predicted: str,
    *,
    temperature: float = 1.0,
    max_tokens: int = 512,
    use_thinking: bool = False,
    timeout: float = 60.0,
) -> tuple[str, dict[str, int]]:
    """同步调用评分 DeepSeek V4 模型。返回 (raw_response, usage_dict)。"""
    prompt = GRADER_TEMPLATE.format(
        question=question,
        criterion=gold,
        answer=predicted,
        instructions=GRADING_INSTRUCTIONS,
    )
    extra = _build_extra_body(use_thinking, "")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        extra_body=extra,
    )
    raw = resp.choices[0].message.content or ""
    usage = {
        "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
    }
    return raw.strip(), usage


# -----------------------------------------------------------------------------
# 评分解析（与 inspect_evals 的 strict 解析一致）
# -----------------------------------------------------------------------------

_GRADE_RE = re.compile(r"\b([ABC])\b")
_GRADE_RE_PAPER = re.compile(r"(A|B|C)")


def parse_grade(raw: str, mode: str = "strict") -> str:
    """把评分模型输出归一到 A/B/C。strict = 单词边界；paper = 任意位置第一个字母。"""
    if not raw:
        return "C"  # 没输出 = 视为未尝试
    rx = _GRADE_RE if mode == "strict" else _GRADE_RE_PAPER
    m = rx.search(raw)
    return m.group(1) if m else "C"


GRADE_LETTER_TO_LABEL = {
    "A": "CORRECT",
    "B": "INCORRECT",
    "C": "NOT_ATTEMPTED",
}


# -----------------------------------------------------------------------------
# 异步版（用于并发）
# -----------------------------------------------------------------------------

async def _call_test(client: AsyncOpenAI, model: str, question: str, *,
                     temperature: float, max_tokens: int,
                     use_thinking: bool, reasoning_effort: str, timeout: float):
    extra = _build_extra_body(use_thinking, reasoning_effort)
    return await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": TEST_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        extra_body=extra,
    )


async def _call_grader(client: AsyncOpenAI, model: str, question: str, gold: str, predicted: str, *,
                       temperature: float, max_tokens: int,
                       use_thinking: bool, timeout: float):
    prompt = GRADER_TEMPLATE.format(
        question=question, criterion=gold, answer=predicted, instructions=GRADING_INSTRUCTIONS,
    )
    extra = _build_extra_body(use_thinking, "")
    return await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        extra_body=extra,
    )


# -----------------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------------

def _process_one(
    client: OpenAI,
    test_model: str,
    grader_model: str,
    row: pd.Series,
    *,
    test_temperature: float,
    test_max_tokens: int,
    use_thinking: bool,
    reasoning_effort: str,
    grader_temperature: float,
    grader_max_tokens: int,
    grader_thinking: bool,
    grade_parse: str,
    max_retries: int,
) -> RowResult:
    """单条样本：测试 → 评分 → 落库。失败时错误信息写到 result.error。"""
    question = row["problem"]
    gold = row["gold"]

    res = RowResult(
        original_index=int(row.get("original_index", -1)),
        problem=question,
        gold=gold,
        topic=str(row.get("topic", "")),
        answer_type=str(row.get("answer_type", "")),
        multi_step=bool(row.get("multi_step", False)),
        requires_reasoning=bool(row.get("requires_reasoning", False)),
    )

    # ---------- 1. 测试模型 ----------
    last_err: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            t0 = time.time()
            answer, thinking, usage = call_test_model_sync(
                client, test_model, question,
                temperature=test_temperature,
                max_tokens=test_max_tokens,
                use_thinking=use_thinking,
                reasoning_effort=reasoning_effort,
            )
            res.predicted = answer.strip()
            res.test_thinking = thinking
            res.test_prompt_tokens = usage["prompt_tokens"]
            res.test_completion_tokens = usage["completion_tokens"]
            res.test_latency_s = time.time() - t0
            last_err = None
            break
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    if last_err is not None:
        res.error = f"TEST_FAIL: {type(last_err).__name__}: {last_err}"
        res.grade_label = "ERROR"
        return res

    # ---------- 2. 评分模型 ----------
    for attempt in range(max_retries):
        try:
            t0 = time.time()
            raw, usage = call_grader_sync(
                client, grader_model, question, gold, res.predicted,
                temperature=grader_temperature,
                max_tokens=grader_max_tokens,
                use_thinking=grader_thinking,
            )
            res.raw_grader_response = raw
            res.grader_prompt_tokens = usage["prompt_tokens"]
            res.grader_completion_tokens = usage["completion_tokens"]
            res.grader_latency_s = time.time() - t0
            last_err = None
            break
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    if last_err is not None:
        res.error = f"GRADER_FAIL: {type(last_err).__name__}: {last_err}"
        res.grade_label = "ERROR"
        return res

    # ---------- 3. 解析 ----------
    letter = parse_grade(res.raw_grader_response, mode=grade_parse)
    res.grade_letter = letter
    res.grade_label = GRADE_LETTER_TO_LABEL.get(letter, "NOT_ATTEMPTED")
    return res


def _compute_summary(results: list[RowResult]) -> Summary:
    s = Summary(total=len(results))
    for r in results:
        if r.grade_label == "CORRECT":
            s.correct += 1
        elif r.grade_label == "INCORRECT":
            s.incorrect += 1
        elif r.grade_label == "NOT_ATTEMPTED":
            s.not_attempted += 1
        elif r.grade_label == "ERROR":
            s.errored += 1
        s.total_tokens_in += r.test_prompt_tokens + r.grader_prompt_tokens
        s.total_tokens_out += r.test_completion_tokens + r.grader_completion_tokens
    s.attempted = s.correct + s.incorrect
    if s.total > 0:
        s.accuracy = s.correct / s.total
    if s.attempted > 0:
        s.correct_given_attempted = s.correct / s.attempted
    if (s.accuracy + s.correct_given_attempted) > 0:
        s.f_score = 2 * s.accuracy * s.correct_given_attempted / (s.accuracy + s.correct_given_attempted)
    return s


def _save_outputs(
    out_dir: Path,
    results: list[RowResult],
    summary: Summary,
    *,
    config: dict[str, Any],
    append: bool = False,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "results.jsonl"
    csv_path = out_dir / "results.csv"
    summary_path = out_dir / "summary.json"

    mode = "a" if append else "w"
    with open(jsonl_path, mode, encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")

    rows = [asdict(r) for r in results]
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8")

    payload = {"summary": summary.to_dict(), "config": config,
               "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_existing(out_dir: Path) -> dict[int, RowResult]:
    """断点续跑：读已有 jsonl，按 original_index 索引。"""
    jsonl_path = out_dir / "results.jsonl"
    cache: dict[int, RowResult] = {}
    if not jsonl_path.exists():
        return cache
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            r = RowResult(**d)
            cache[r.original_index] = r
    print(f"[resume] loaded {len(cache)} previously processed rows from {jsonl_path}")
    return cache


def run(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_dataset(limit=args.limit)
    cache: dict[int, RowResult] = _load_existing(out_dir) if args.resume else {}

    client = make_client()

    results: list[RowResult] = []
    summary = Summary()

    config = {
        "test_model": args.test_model,
        "grader_model": args.grader_model,
        "test_temperature": args.test_temperature,
        "test_max_tokens": args.test_max_tokens,
        "use_thinking": args.use_thinking,
        "reasoning_effort": args.reasoning_effort,
        "grader_temperature": args.grader_temperature,
        "grader_max_tokens": args.grader_max_tokens,
        "grader_thinking": args.grader_thinking,
        "grade_parse": args.grade_parse,
        "limit": args.limit,
        "max_retries": args.max_retries,
    }

    todo = [r for _, r in df.iterrows()
            if int(r.get("original_index", -1)) not in cache]
    print(f"[plan] {len(df)} total, {len(todo)} to process, "
          f"{len(cache)} resumed from cache")

    pbar = tqdm(todo, desc="evaluating", dynamic_ncols=True)
    for row in pbar:
        idx = int(row.get("original_index", -1))
        result = _process_one(
            client,
            args.test_model, args.grader_model,
            row,
            test_temperature=args.test_temperature,
            test_max_tokens=args.test_max_tokens,
            use_thinking=args.use_thinking,
            reasoning_effort=args.reasoning_effort,
            grader_temperature=args.grader_temperature,
            grader_max_tokens=args.grader_max_tokens,
            grader_thinking=args.grader_thinking,
            grade_parse=args.grade_parse,
            max_retries=args.max_retries,
        )
        cache[idx] = result
        results.append(result)
        # 实时把新结果追加写盘（崩溃也能恢复）
        with open(out_dir / "results.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")

        # 更新进度条上的指标
        so_far = [r for r in cache.values() if r.grade_label in ("CORRECT", "INCORRECT", "NOT_ATTEMPTED")]
        c = sum(1 for r in so_far if r.grade_label == "CORRECT")
        i = sum(1 for r in so_far if r.grade_label == "INCORRECT")
        n = sum(1 for r in so_far if r.grade_label == "NOT_ATTEMPTED")
        total = len(so_far)
        acc = c / total if total else 0.0
        attempted = c + i
        cga = c / attempted if attempted else 0.0
        fscore = 2 * acc * cga / (acc + cga) if (acc + cga) else 0.0
        pbar.set_postfix(acc=f"{acc:.3f}", f1=f"{fscore:.3f}",
                         C=c, I=i, N=n)

    # 全部结束，整理最终输出
    final = sorted(cache.values(), key=lambda r: r.original_index)
    summary = _compute_summary(final)
    _save_outputs(out_dir, final, summary, config=config, append=args.resume)

    print("\n" + "=" * 60)
    print("SimpleQA Verified — DeepSeek V4 结果")
    print("=" * 60)
    print(f"  test model   : {args.test_model}  (thinking={args.use_thinking}, effort={args.reasoning_effort})")
    print(f"  grader model : {args.grader_model}")
    print(f"  total        : {summary.total}")
    print(f"  correct      : {summary.correct} ({summary.accuracy:.3f})")
    print(f"  incorrect    : {summary.incorrect}")
    print(f"  not_attempted: {summary.not_attempted}")
    print(f"  errored      : {summary.errored}")
    print(f"  attempted    : {summary.attempted}")
    print(f"  corr/attempt : {summary.correct_given_attempted:.3f}")
    print(f"  f_score      : {summary.f_score:.3f}")
    print(f"  tokens in/out: {summary.total_tokens_in} / {summary.total_tokens_out}")
    print(f"  outputs      : {out_dir}")
    print("=" * 60)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SimpleQA Verified 评测 — DeepSeek V4 版",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=None,
                   help="只跑前 N 条（smoke test 用）")
    p.add_argument("--out-dir", default="outputs/simpleqa_verified_deepseekv4",
                   help="结果输出目录")
    p.add_argument("--resume", action="store_true",
                   help="从已有 results.jsonl 续跑")

    # 测试模型
    p.add_argument("--test-model", default=DEFAULT_TEST_MODEL,
                   help="被测模型；例如 deepseek-v4-pro / deepseek-v4-flash")
    p.add_argument("--test-temperature", type=float, default=0.0)
    p.add_argument("--test-max-tokens", type=int, default=2048)
    p.add_argument("--use-thinking", dest="use_thinking", action="store_true", default=True,
                   help="开启 DeepSeek V4 thinking 模式（默认开）")
    p.add_argument("--no-thinking", dest="use_thinking", action="store_false",
                   help="关闭 thinking 模式")
    p.add_argument("--reasoning-effort", default="high",
                   choices=["low", "medium", "high", "max"],
                   help="thinking 模式下的推理力度")

    # 评分模型
    p.add_argument("--grader-model", default=DEFAULT_GRADER_MODEL,
                   help="评分模型；默认同测试模型")
    p.add_argument("--grader-temperature", type=float, default=1.0)
    p.add_argument("--grader-max-tokens", type=int, default=512)
    p.add_argument("--grader-thinking", action="store_true", default=False,
                   help="评分模型是否也开 thinking（默认关）")
    p.add_argument("--grade-parse", default="strict",
                   choices=["strict", "paper"],
                   help="A/B/C 解析方式：strict=单词边界；paper=首个字母")

    p.add_argument("--max-retries", type=int, default=3)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args)
