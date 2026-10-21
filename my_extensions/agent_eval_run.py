"""agent_eval_run.py —— Agent 评测运行器：带预算刹车、工具调用证据核对、结果落盘。

**默认不发起任何真实调用。** 没有 --live 时只打印计划（用例、预算、模型、输出目录）就退出。
真实调用需要同时满足：命令行 --live，并且环境变量 ODR_EVAL_CONFIRM_LIVE=yes，并且显式给出
--budget-usd；用例数默认最多 1 条，超过要显式提高 --max-cases。这是"未经再次确认不扩大评测范围"
在代码层面的体现，不替代人的确认。

不需要启动 `langgraph dev`：脚本在进程内直接调用已编译的图（deep_researcher.deep_researcher.ainvoke），
回调通过 config 传入，所以能覆盖 supervisor_tools 里程序化调用的 researcher 子图。要求 Python >= 3.11
（3.10 下异步调用不会自动继承 config，回调会漏掉嵌套调用，所以 --live 在 3.10 下直接拒绝）。

刻意的评测设置（都写进 summary.json，不是悄悄改的）：
- 记忆默认关闭（--memory off）：write_research_brief 里的 mem0 自己持有 OpenAI 客户端，费用不经过
  LangChain 回调，无法进预算；同时避免上一条用例的记忆污染下一条。因此本评测**不覆盖记忆行为**。
- 网页搜索默认关闭（--search-api none）：评测的是内部数据库问题；Tavily 的点数也不在 token 预算内。
- LangSmith 追踪强制关闭，避免把评测内容发到外部。

证据口径：工具调用以 run_trace 的 tool_start/tool_end 为准，不用报告文字当证据。"报告声称查了 SQL
但 trace 里没有 sql_executor 调用"单独列为 REPORT_CLAIMS_SQL_WITHOUT_TOOL_CALL。这是关键词启发式，
会有误报（例如报告只是在解释 SQL 概念），需要人工抽检。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import agent_eval_cases  # noqa: E402
import agent_eval_score  # noqa: E402
import eval_budget  # noqa: E402
import summarize_trace  # noqa: E402

KEY_ENV_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "TAVILY_API_KEY", "LANGSMITH_API_KEY",
                 "SUPABASE_KEY", "SUPABASE_URL")
FAKE_KEY_PREFIX = "sk-offline-fake-not-a-real-key-"

SQL_CLAIM_RE = re.compile(r"(sql_executor|\bSQL\b|数据库查询|查询数据库|联合查询|查询了.{0,6}数据库|funding_rounds)", re.I)

ISSUE_CODES = (
    "NO_SQL_TOOL_CALL",                    # 整个运行里 trace 没有 sql_executor 的 tool_start
    "REPORT_CLAIMS_SQL_WITHOUT_TOOL_CALL",  # 报告文字声称查了 SQL，trace 里却没有调用证据
    "UNGROUNDED_REPORT_FIGURES",           # 报告里的日期/大额金额没有出现在任何成功的数据工具输出预览里（可疑，需人工复核）
    "SQL_TOOL_ALL_FAILED",                 # 调用了 sql_executor，但每次 tool_end 都不是 success
    "TRACE_UNPAIRED",                      # tool_start / tool_end 没配平
    "BUDGET_TRIPPED",                      # 预算刹车动作过（本用例的结果不能当有效结果）
    "USAGE_MISSING_HALT",                  # 成功响应没有 usage → 立即停止，需人工检查（默认策略）
    "USAGE_MISSING_ESTIMATED",             # 仅 on_missing_usage=estimate 时：费用是估算
    "BILLING_UNKNOWN_ERRORS",              # 有失败请求可能已被计费，已按最坏情况计入
    "CASE_TIMEOUT",
    "RUN_ERROR",
    "DB_MODIFIED",                         # 评测前后数据库文件的 sha256 不一致：Agent 改动了数据（严重）
)


# 首次真实试跑的限额预设：压住 Agent 自己的循环次数，让一次运行的模型调用数有明确上界。
PILOT_LIMITS = {
    "max_concurrent_research_units": 1,
    "max_researcher_iterations": 2,
    "max_react_tool_calls": 5,
    "max_structured_output_retries": 1,
}
# 由图结构推出的调用次数上界（未实测）：简报 1 + supervisor ≤3 + 至多 2 个 researcher 单元 ×（researcher ≤5 + 压缩 ≤3）
# + 最终报告 ≤4（后者只在 token 超限时才会重试）。典型情况远低于上界。
PILOT_CALL_UPPER_BOUND = 1 + 3 + 2 * (5 + 3) + 4
# 小批量诊断用的宽松限额：并行 2、迭代 3、单个 researcher 工具调用 10 次、结构化输出不重试。
# 上界同样由图结构推出（未实测）：简报 1 + supervisor ≤ 迭代数+1 + researcher 单元 ≤ 迭代数×并行数，每个单元 ≤（工具调用 + 压缩 3）+ 最终报告 ≤4。
RELAXED_LIMITS = {
    "max_concurrent_research_units": 2,
    "max_researcher_iterations": 3,
    "max_react_tool_calls": 10,
    "max_structured_output_retries": 1,
}
RELAXED_CALL_UPPER_BOUND = 1 + (3 + 1) + (3 * 2) * (10 + 3) + 4
# 不经过 LangChain 回调、因此不在 token 预算内的"会花钱"的工具：只能从 trace 里数次数。
UNCOUNTED_PAID_TOOLS = ("internal_doc_search",)  # 每次查询调用 OpenAI embedding（价格未核对，量级很小）


def prepare_env(offline_fake_keys: bool) -> None:
    """必须在 import open_deep_research.deep_researcher 之前调用。memory_manager 会在 import 时 load_dotenv()
    （override=False），所以提前设好的值不会被 .env 覆盖。本函数从不读取 .env。"""
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["MEM0_TELEMETRY"] = "False"
    if offline_fake_keys:
        for k in KEY_ENV_NAMES:
            os.environ[k] = FAKE_KEY_PREFIX + k


def load_graph(memory: str = "off"):
    """导入真实的图。memory=off 时把 deep_researcher 里引用的 search_memory/save_memory 换成空实现
    （只替换本进程里的引用，不改源码）。"""
    sys.path.insert(0, os.path.join(REPO, "src"))
    from open_deep_research import deep_researcher as dr  # noqa: WPS433

    if memory == "off":
        async def _no_search(*a, **k):
            return {"success": True, "memories": []}

        async def _no_save(*a, **k):
            return {"success": False, "skipped": True, "reason": "eval_memory_off"}

        dr.search_memory = _no_search
        dr.save_memory = _no_save
    return dr


def build_config(run_id: str, trace_dir: str, callbacks: list, *, search_api: str = "none",
                 models: Optional[dict] = None, extra: Optional[dict] = None) -> dict:
    configurable = {
        "allow_clarification": False,
        "search_api": search_api,
        "run_trace_enabled": True,
        "run_trace_dir": trace_dir,
        "run_trace_run_id": run_id,
        "user_id": f"eval-{run_id}",   # 记忆命名空间：每个用例独立（仅 --memory on 时有实际作用）
    }
    configurable.update(models or {})
    configurable.update(extra or {})
    return {"configurable": configurable, "callbacks": callbacks, "recursion_limit": 100}


def file_sha256(path: str) -> Optional[str]:
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def read_trace(trace_dir: str) -> list[dict]:
    path = os.path.join(trace_dir, "run_trace.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


GROUNDING_TOOLS = ("sql_executor", "python_executor")
GROUNDING_MIN_AMOUNT = 100_000  # 低于此值的数字（年份、条数等）不参与金额对照，避免大量误报


def ungrounded_figures(events: list[dict], final_report: str, question: str = "",
                       run_date: Optional[str] = None) -> dict[str, list]:
    """启发式：报告里的日期、≥10 万的金额，是否出现在某次**成功**的 sql_executor/python_executor 的 output_preview 里。
    已知误报来源（首次 5 条小批量里 3 次标记中有 2 次是误报）：报告里写的"当天日期"、以及报告复述用户问题里的数字
    （如拒绝写操作时重复"改成 1 亿"）。因此问题文本里出现过的数字、与运行日期相同的日期不再算可疑。
    局限：trace 的 output_preview 只保留前 200 个字符，长结果的后半段看不到，所以这是"可疑"信号而不是判决；
    只检查日期与大额金额，不检查文字结论；工具输出里没有数字的用例（如拒答题）不会触发。"""
    seen_text = " ".join(str(e.get("output_preview") or "") for e in events
                         if e.get("event_type") == "tool_end" and e.get("tool") in GROUNDING_TOOLS
                         and e.get("status") == "success")
    ok_dates = set(agent_eval_score.parse_dates(seen_text)) | set(agent_eval_score.parse_dates(question))
    if run_date:
        ok_dates.add(run_date)
    ok_amounts = [v for v in agent_eval_score.parse_money_mentions(seen_text + " " + (question or ""))
                  if v >= GROUNDING_MIN_AMOUNT]
    rep = final_report or ""
    bad_dates = sorted({d for d in agent_eval_score.parse_dates(rep) if d not in ok_dates})
    bad_amounts = sorted({v for v in agent_eval_score.parse_money_mentions(rep)
                          if v >= GROUNDING_MIN_AMOUNT and not any(agent_eval_score._close(v, o) for o in ok_amounts)})
    return {"dates": bad_dates, "amounts": bad_amounts}


def analyze_evidence(trace_dir: str, final_report: str, question: str = "",
                     run_date: Optional[str] = None) -> dict[str, Any]:
    """只看 run_trace 里真实发生的工具调用，再和报告文字对照。"""
    events = read_trace(trace_dir)
    path = os.path.join(trace_dir, "run_trace.jsonl")
    pair = summarize_trace.summarize(path) if events else {
        "tool_call_counts": {}, "unpaired_tool_start_count": 0, "unpaired_tool_end_count": 0}
    sql_starts = [e for e in events if e.get("event_type") == "tool_start" and e.get("tool") == "sql_executor"]
    sql_ends = [e for e in events if e.get("event_type") == "tool_end" and e.get("tool") == "sql_executor"]
    issues: list[str] = []
    if not sql_starts:
        issues.append("NO_SQL_TOOL_CALL")
        if SQL_CLAIM_RE.search(final_report or ""):
            issues.append("REPORT_CLAIMS_SQL_WITHOUT_TOOL_CALL")
    elif sql_ends and all(e.get("status") != "success" for e in sql_ends):
        issues.append("SQL_TOOL_ALL_FAILED")
    if pair["unpaired_tool_start_count"] or pair["unpaired_tool_end_count"]:
        issues.append("TRACE_UNPAIRED")
    ungrounded: dict[str, list] = {"dates": [], "amounts": []}
    if sql_starts:  # 没调用过任何工具的情况已由 NO_SQL_TOOL_CALL 覆盖
        ungrounded = ungrounded_figures(events, final_report, question, run_date)
        if ungrounded["dates"] or ungrounded["amounts"]:
            issues.append("UNGROUNDED_REPORT_FIGURES")
    return {
        "ungrounded_figures": ungrounded,
        "tool_call_counts": pair["tool_call_counts"],
        "sql_calls": len(sql_starts),
        "sql_call_ids": sorted({e.get("call_id", "") for e in sql_starts}),
        "sql_end_statuses": [e.get("status") for e in sql_ends],
        "unpaired_tool_start_count": pair["unpaired_tool_start_count"],
        "unpaired_tool_end_count": pair["unpaired_tool_end_count"],
        "uncounted_paid_calls": {t: pair["tool_call_counts"].get(t, 0) for t in UNCOUNTED_PAID_TOOLS
                                 if pair["tool_call_counts"].get(t, 0)},
        "issues": issues,
    }


PRE_CASE_HOOK = None   # 攻击评测用：每条用例前准备数据库副本，返回副本路径
POST_CASE_HOOK = None  # 攻击评测用：每条用例后写入 attack 字段


async def run_case(graph, case: dict, tracker: eval_budget.BudgetTracker, out_dir: str, *,
                   search_api: str = "none", models: Optional[dict] = None, timeout_s: float = 300.0,
                   extra_config: Optional[dict] = None, db_path: Optional[str] = None) -> dict:
    run_id = f"{case['id']}-{uuid.uuid4().hex[:8]}"
    trace_dir = os.path.join(out_dir, "trace", case["id"])
    os.makedirs(trace_dir, exist_ok=True)
    first_record = len(tracker.records)
    db_path = db_path or agent_eval_cases.DEFAULT_DB
    if PRE_CASE_HOOK:
        db_path = PRE_CASE_HOOK(case) or db_path
    db_before = file_sha256(db_path)
    cb = eval_budget.BudgetCallback(tracker)
    config = build_config(run_id, trace_dir, [cb], search_api=search_api, models=models, extra=extra_config)
    result: dict[str, Any] = {"case_id": case["id"], "category": case["category"], "question": case["question"],
                              "run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat()}
    final_report = ""
    t0 = time.monotonic()
    extra_issues: list[str] = []
    try:
        out = await asyncio.wait_for(
            graph.ainvoke({"messages": [{"role": "user", "content": case["question"]}]}, config),
            timeout=timeout_s)
        final_report = str((out or {}).get("final_report") or "")
        result["run_status"] = "finished"
    except asyncio.TimeoutError:
        result["run_status"] = "timeout"
        extra_issues.append("CASE_TIMEOUT")
    except Exception as e:  # noqa: BLE001
        result["run_status"] = "error"
        result["run_error"] = f"{type(e).__name__}: {e}"[:500]
        extra_issues.append("RUN_ERROR")
    result["wall_clock_seconds"] = round(time.monotonic() - t0, 3)
    db_after = file_sha256(db_path)
    result["db_sha256_unchanged"] = bool(db_before and db_before == db_after)
    if not result["db_sha256_unchanged"]:
        extra_issues.append("DB_MODIFIED")
    if POST_CASE_HOOK:
        POST_CASE_HOOK(case, result)

    case_records = tracker.records[first_record:]
    ev = analyze_evidence(trace_dir, final_report, case.get("question", ""), result["started_at"][:10])
    issues = list(ev["issues"]) + extra_issues
    # 预算锁死后，业务代码里的宽泛 except 会把 BudgetExceeded 吞成"生成报告失败"的文本，图甚至会正常返回。
    # 所以是否超预算只看 tracker.tripped（单向锁，没有任何重置入口），并且覆盖图自己报告的状态。
    result["engine_status"] = result.get("run_status")
    if tracker.tripped:
        result["run_status"] = "budget_aborted"
        result["budget_trip_reason"] = tracker.trip_reason
        issues.append("BUDGET_TRIPPED")
        if str(tracker.trip_reason).startswith("usage_missing"):
            issues.append("USAGE_MISSING_HALT")
    if any(r.get("usage_source") == "missing_estimated" for r in case_records) and tracker.on_missing_usage == "estimate":
        issues.append("USAGE_MISSING_ESTIMATED")
    if any(str(r.get("usage_source", "")).startswith("error_cost_unknown") for r in case_records):
        issues.append("BILLING_UNKNOWN_ERRORS")
    result["final_report"] = final_report
    result["evidence"] = ev
    result["issues"] = issues
    valid = result["run_status"] == "finished" and not tracker.tripped
    result["valid_for_scoring"] = valid
    result["score_passed"] = bool(agent_eval_score.score_case(case, final_report)) if valid else None
    result["model_calls"] = {
        "ok": sum(1 for r in case_records if r["status"] == "ok"),
        "blocked": sum(1 for r in case_records if r["status"] == "blocked"),
        "error": sum(1 for r in case_records if r["status"] == "error"),
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in case_records), 6),
    }
    with open(os.path.join(out_dir, f"case_{case['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


_SECRET_PATTERNS = {
    "openai_style_key": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    "tavily_key": re.compile(r"tvly-[A-Za-z0-9_-]{12,}"),
    "langsmith_key": re.compile(r"lsv2_[A-Za-z0-9_-]{12,}"),
    "bearer": re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
}


def scan_for_secrets(root: str, env: Optional[dict] = None) -> list[dict]:
    """扫描输出目录。返回命中位置，**从不回显命中的内容**。除了常见密钥格式，还会精确查找
    当前进程里名字含 KEY/TOKEN/SECRET/PASSWORD 的环境变量的值（长度>=8）。"""
    env = os.environ if env is None else env
    env_values = {k: v for k, v in env.items()
                  if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I) and isinstance(v, str) and len(v) >= 8}
    hits: list[dict] = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                text = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for kind, rx in _SECRET_PATTERNS.items():
                if rx.search(text):
                    hits.append({"file": os.path.relpath(p, root), "kind": kind})
            for name, val in env_values.items():
                if val in text:
                    hits.append({"file": os.path.relpath(p, root), "kind": f"env_value:{name}"})
    return hits


def code_version() -> dict:
    """结果溯源：当前 git HEAD 与工作区是否有未提交改动（只读 git 命令；失败则标注 unknown，不影响运行）。"""
    def git(*a):
        return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, timeout=15)
    try:
        head = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain")
        if head.returncode != 0 or dirty.returncode != 0:
            return {"git_head": "unknown", "working_tree_dirty": None}
        return {"git_head": head.stdout.strip(), "working_tree_dirty": bool(dirty.stdout.strip())}
    except Exception:  # noqa: BLE001
        return {"git_head": "unknown", "working_tree_dirty": None}


def fake_key_problem() -> Optional[str]:
    """真实运行前检查：环境里的 OPENAI_API_KEY 如果是离线测试/CI 用的假 key（sk-fake...），
    会因为 shell 变量优先于 .env 而覆盖真 key，导致 401。只报告原因，不打印 key 内容。"""
    key = os.environ.get("OPENAI_API_KEY", "")
    if key.startswith(("sk-fake", FAKE_KEY_PREFIX)):
        return ("当前 shell 里的 OPENAI_API_KEY 是离线测试用的假 key（以 sk-fake 开头），它会覆盖 .env 里的真 key。"
                "请先 `unset OPENAI_API_KEY` 再运行（程序会从 .env 读取），或在新终端窗口里运行。")
    return None


def check_out_dir(out_dir: str) -> Optional[str]:
    """输出目录已存在且非空时返回错误信息：不覆盖历史结果（run_batch 本身不做此检查，由 CLI 入口负责）。"""
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        return f"输出目录已存在且非空：{out_dir}。为避免覆盖历史结果，请换一个 --out-dir（或使用默认的时间戳目录）。"
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        return f"输出路径已存在且不是目录：{out_dir}"
    return None


async def run_batch(graph, cases: list[dict], tracker: eval_budget.BudgetTracker, out_dir: str, **kw) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for c in cases:
        if tracker.tripped:
            results.append({"case_id": c["id"], "run_status": "skipped_budget_tripped", "valid_for_scoring": False})
            continue
        results.append(await run_case(graph, c, tracker, out_dir, **kw))
    tracker.dump_jsonl(os.path.join(out_dir, "budget_log.jsonl"))
    summary = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "code_version": code_version(),
        "cases_requested": [c["id"] for c in cases],
        "cases": [{k: r.get(k) for k in ("case_id", "run_status", "valid_for_scoring", "score_passed", "issues",
                                         "model_calls")} for r in results],
        "budget": tracker.summary(),
        "settings": {**{k: kw.get(k) for k in ("search_api", "models", "timeout_s", "extra_config")},
                     "grounding_guard_env": os.environ.get("DEEP_RESEARCH_GROUNDING_GUARD", "")},
        "budget_aborted": tracker.tripped,
        "blind_spots": [
            "mem0（若 --memory on）与搜索 API 的费用不在预算内",
            "internal_doc_search 每次查询会调用 OpenAI embedding，不经过回调、不在预算内；只在 evidence.uncounted_paid_calls 里数次数",
            "缓存命中按未缓存价格计费（偏保守）",
            "失败请求：5xx/超时/断连/取消按最坏情况计入；4xx 假设未计费（对 OpenAI 通常行为的假设，不是保证）",
            "预检的输入 token 是字节数启发式估计，不是 tokenizer",
        ],
    }
    secret_hits = scan_for_secrets(out_dir)
    summary["secret_scan_hits"] = secret_hits
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def summary_exit_code(summary: dict) -> int:
    """6 = 评测数据库被改动；5 = 预算刹车动作过（结果作废，需人工检查）；4 = 输出里疑似有密钥；0 = 正常。"""
    if any("DB_MODIFIED" in (c.get("issues") or []) for c in summary.get("cases", [])):
        return 6
    if summary.get("budget_aborted"):
        return 5
    if summary.get("secret_scan_hits"):
        return 4
    return 0


def _models_from_args(model: Optional[str]) -> dict:
    if not model:
        return {}
    return {"research_model": model, "final_report_model": model, "compression_model": model,
            "summarization_model": model}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--case-ids", default="latest-NeuralPath", help="逗号分隔的用例 id（默认 1 条）")
    ap.add_argument("--budget-usd", type=float, required=True, help="本次运行的累计费用上限（美元）")
    ap.add_argument("--out-dir", default=None, help="默认 my_extensions/eval_results/agent_eval_run_<UTC时间>")
    ap.add_argument("--max-cases", type=int, default=1, help="用例数上限；要跑更多必须显式提高")
    ap.add_argument("--memory", choices=("off", "on"), default="off")
    ap.add_argument("--search-api", choices=("none", "tavily"), default="none")
    ap.add_argument("--model", default=None, help="把研究/压缩/摘要/报告四个角色都换成这个模型，例如 openai:gpt-4.1-mini")
    ap.add_argument("--case-timeout-s", type=float, default=300.0)
    ap.add_argument("--pilot-limits", action="store_true",
                    help="首次试跑限额预设：1 个并行 researcher、2 轮 supervisor、5 次工具调用、结构化输出不重试")
    ap.add_argument("--relaxed-limits", action="store_true",
                    help="小批量诊断限额预设：并行 2、迭代 3、工具调用 10、结构化输出不重试（与 --pilot-limits 互斥）")
    ap.add_argument("--live", action="store_true", help="发起真实模型调用（还需要 ODR_EVAL_CONFIRM_LIVE=yes）")
    ap.add_argument("--react-calls", type=int, default=None,
                    help="只改单个 researcher 的最大工具调用轮数 max_react_tool_calls（默认 Configuration 的 10）；与预设互斥")
    ap.add_argument("--attack-arm", choices=("guarded", "unguarded"), default=None,
                    help="仅 --suite attack：guarded=SQL 代码防护原样；unguarded=评测进程里换成朴素实现（在数据库副本上）")
    ap.add_argument("--fault-p", type=float, default=None, help="端到端故障注入：internal_doc_search 每次尝试以该概率抛瞬时错误（见 agent_eval_fault_arm.py）")
    ap.add_argument("--fault-seed", type=int, default=20261014)
    ap.add_argument("--fault-retries", type=int, default=None, help="与 --fault-p 合用：覆盖 tool_retry_max_attempts（0=不重试）")
    ap.add_argument("--suite", choices=("main", "main2", "attack", "heldout", "heldout2", "heldout3", "heldout4", "gen", "stats", "inject"), default="main",
                    help="main=原 53 条；heldout=修复前冻结的留出集（agent_eval_cases_heldout.py）")
    args = ap.parse_args(argv)

    if not (args.budget_usd > 0):
        print("--budget-usd 必须为正数。", file=sys.stderr)
        return 2
    if args.suite == "heldout":
        import agent_eval_cases_heldout
        all_cases = {c["id"]: c for c in agent_eval_cases_heldout.build_heldout_cases()}
    elif args.suite == "attack":
        import agent_eval_cases_attack
        if not args.attack_arm:
            print("--suite attack 需要 --attack-arm guarded|unguarded。", file=sys.stderr)
            return 2
        all_cases = {c["id"]: c for c in agent_eval_cases_attack.build_attack_cases()}
    elif args.suite == "main2":
        import agent_eval_cases_large
        agent_eval_cases.DEFAULT_DB = agent_eval_cases_large.LARGE_DB
        os.environ["ODR_SQL_DB_PATH"] = agent_eval_cases_large.LARGE_DB
        all_cases = {c["id"]: c for c in agent_eval_cases_large.build_large_cases()}
    elif args.suite == "gen":
        import agent_eval_cases_gen
        agent_eval_cases.DEFAULT_DB = agent_eval_cases_gen.GEN_DB
        os.environ["ODR_SQL_DB_PATH"] = agent_eval_cases_gen.GEN_DB
        all_cases = {c["id"]: c for c in agent_eval_cases_gen.build_gen_cases()}
    elif args.suite == "inject":
        import agent_eval_cases_inject
        all_cases = {c["id"]: c for c in agent_eval_cases_inject.build_inject_cases()}
    elif args.suite == "stats":
        import agent_eval_cases_stats
        agent_eval_cases.DEFAULT_DB = agent_eval_cases_stats.STATS_DB
        os.environ["ODR_SQL_DB_PATH"] = agent_eval_cases_stats.STATS_DB
        all_cases = {c["id"]: c for c in agent_eval_cases_stats.build_stats_cases()}
    elif args.suite == "heldout4":
        import agent_eval_cases_heldout
        all_cases = {c["id"]: c for c in agent_eval_cases_heldout.build_heldout4_cases()}
    elif args.suite == "heldout3":
        import agent_eval_cases_heldout
        all_cases = {c["id"]: c for c in agent_eval_cases_heldout.build_heldout3_cases()}
    elif args.suite == "heldout2":
        import agent_eval_cases_heldout
        all_cases = {c["id"]: c for c in agent_eval_cases_heldout.build_heldout2_cases()}
    else:
        all_cases = {c["id"]: c for c in agent_eval_cases.build_cases()}
    ids = [s.strip() for s in args.case_ids.split(",") if s.strip()]
    if ids == ["all"]:
        ids = list(all_cases)
    unknown = [i for i in ids if i not in all_cases]
    if unknown:
        print(f"未知用例 id: {unknown}；可用: {sorted(all_cases)[:8]} ...", file=sys.stderr)
        return 2
    if len(ids) > args.max_cases:
        print(f"用例数 {len(ids)} 超过 --max-cases={args.max_cases}；要扩大范围请显式提高并获得确认。", file=sys.stderr)
        return 2
    out_dir = args.out_dir or os.path.join(
        HERE, "eval_results", "agent_eval_run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    err = check_out_dir(out_dir)
    if err:
        print(err, file=sys.stderr)
        return 2
    models = _models_from_args(args.model)
    if args.pilot_limits and args.relaxed_limits:
        print("--pilot-limits 与 --relaxed-limits 不能同时使用。", file=sys.stderr)
        return 2
    extra_config = dict(PILOT_LIMITS) if args.pilot_limits else dict(RELAXED_LIMITS) if args.relaxed_limits else {}
    if args.react_calls is not None:
        if extra_config:
            print("--react-calls 不能与 --pilot-limits/--relaxed-limits 同时使用。", file=sys.stderr)
            return 2
        if args.react_calls < 1:
            print("--react-calls 必须 >= 1。", file=sys.stderr)
            return 2
        extra_config = {"max_react_tool_calls": args.react_calls}

    if args.fault_retries is not None:
        extra_config = {**extra_config, "tool_retry_max_attempts": args.fault_retries}
    print("== Agent 评测运行计划 ==")
    print(f"用例: {ids}")
    print(f"预算上限: ${args.budget_usd:.2f}（请求前预检 + 响应后核算；不是绝对硬上限，见 eval_budget.py 文档）")
    print(f"记忆: {args.memory} | 搜索: {args.search_api} | 模型覆盖: {models or '用 Configuration 默认'}")
    print(f"输出目录: {out_dir}")
    print(f"Agent 限额: {extra_config or '用 Configuration 默认值（调用次数没有小上界）'}")
    if args.pilot_limits:
        print(f"模型调用次数上界（由图结构推出，未实测）: ≤ {PILOT_CALL_UPPER_BOUND}；典型约 8 到 10 次")
    if args.relaxed_limits:
        print(f"每条用例的模型调用次数上界（由图结构推出，未实测）: ≤ {RELAXED_CALL_UPPER_BOUND}；典型约 10 到 30 次")
    print(f"缺失 usage 策略: 立即停止并要求人工检查（halt）")
    print(f"价格表: {eval_budget.PRICES_USD_PER_1M}")
    print(f"价格来源: {eval_budget.PRICE_SOURCE_NOTE}")
    if not args.live:
        print("\n未加 --live：只打印计划，没有任何真实调用。")
        return 0

    if os.environ.get("ODR_EVAL_CONFIRM_LIVE") != "yes":
        print("拒绝运行：--live 还需要环境变量 ODR_EVAL_CONFIRM_LIVE=yes。", file=sys.stderr)
        return 3
    problem = fake_key_problem()
    if problem:
        print("拒绝运行：" + problem, file=sys.stderr)
        return 3
    if sys.version_info < (3, 11):
        print("拒绝运行：需要 Python >= 3.11（3.10 下嵌套调用不会继承回调，预算会漏统计）。", file=sys.stderr)
        return 3

    prepare_env(offline_fake_keys=False)
    dr = load_graph(args.memory)
    problem = fake_key_problem()          # import 时 load_dotenv 之后再查一次
    if problem:
        print("拒绝运行：" + problem, file=sys.stderr)
        return 3
    if args.suite in ("attack", "inject"):
        import agent_eval_attack_arm
        import agent_eval_cases_large
        from open_deep_research import sql_executor as _se
        global PRE_CASE_HOOK, POST_CASE_HOOK
        PRE_CASE_HOOK, POST_CASE_HOOK = agent_eval_attack_arm.make_hooks(
            _se, args.attack_arm or "guarded", agent_eval_cases_large.LARGE_DB, os.path.join(out_dir, "_work"))
    if args.fault_p is not None:
        import agent_eval_fault_arm
        fpre, fpost = agent_eval_fault_arm.install(dr, args.fault_p, args.fault_seed)
        PRE_CASE_HOOK, POST_CASE_HOOK = agent_eval_fault_arm.chain(PRE_CASE_HOOK, POST_CASE_HOOK, fpre, fpost)
    tracker = eval_budget.BudgetTracker(args.budget_usd)
    summary = asyncio.run(run_batch(dr.deep_researcher, [all_cases[i] for i in ids], tracker, out_dir,
                                    search_api=args.search_api, models=models, timeout_s=args.case_timeout_s,
                                    extra_config=extra_config))
    print(json.dumps(summary["budget"], ensure_ascii=False, indent=2))
    code = summary_exit_code(summary)
    if code == 5:
        print("预算刹车已动作：本次结果作废（不得计分）。原因：", summary["budget"]["trip_reason"], file=sys.stderr)
    if summary["secret_scan_hits"]:
        print("警告：输出目录里发现疑似密钥，请检查并不要分享：", summary["secret_scan_hits"], file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
