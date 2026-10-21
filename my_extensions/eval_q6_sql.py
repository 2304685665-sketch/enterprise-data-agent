"""Q6 SQL E2E 评测试点 —— ANSWER_KEY.md 里"NeuralPath 最新一轮融资具体是多少？"

设计背景（详见 my_extensions/IMPROVEMENTS.md 里 E2E 审计相关记录，本文件不修改那份文档）：
- 旧的 test_e2e_sql.py 用 `"sql_executor" in str(state["values"])` 判断工具是否被调用，
  已经用真实运行记录（e2e_output.txt）证实这个断言会失败——因为顶层 state 只保留
  ToolMessage.content，不保留 ToolMessage.name，字符串搜索的对象从一开始就找错了。
- researcher 子图（真正调用 sql_executor 的地方）是在 supervisor_tools 函数体内部用
  `.ainvoke()` 程序化调用的，不是 add_node 挂接的图节点。这意味着无论用
  stream_subgraphs=True 还是 get_state(subgraphs=True)，都无法拿到"某个具体工具被
  调用了"这种结构化证据——这是当前架构的天花板，不是这个评测脚本能绕过的问题。

因此本脚本严格区分三层证据，且不允许互相替代：
  1) answer_matches_reference   —— 最终报告文字是否符合标准答案（最弱证据，模型完全
     可能蒙对或从 doc06 的模糊描述里编一个凑巧对的数字）
  2) raw_notes_fingerprint_hit  —— raw_notes 里是否出现 sql_executor 原始输出格式的
     指纹（间接证据：像是工具真实返回的内容，但不能证明一定来自 sql_executor 本身，
     也不能证明"工具被调用"这件事）
  3) tool_invocation_structurally_confirmed —— 是否有结构化证据（如独立的 tool_calls
     记录）证明工具真的被调用。当前架构下这一层永远拿不到，所以这个字段在代码里是
     写死的字面量 False，不是任何条件表达式的结果，也不允许被前两层的命中结果影响。

本脚本只负责单次运行 + 落盘结构化结果，不做批量评测、不做人工判断，
不修改 sql_executor / 数据库 / 知识库 / Prompt / 图结构。
"""

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from langgraph_sdk import get_client

# ---------------------------------------------------------------------------
# 基本配置
# ---------------------------------------------------------------------------

LANGGRAPH_URL = "http://127.0.0.1:2024"
ASSISTANT_ID = "Deep Researcher"

# 刻意贴近 research_system_prompt / lead_researcher_prompt 里点名的
# "our database" / "internal records" 这类触发词，避免踩到"没显式提示内部就不
# 触发内部检索/路由"这个已知坑——测的是 SQL 能力，不是路由措辞的坑。
QUESTION_TEXT = (
    "我们公司内部数据库里，NeuralPath 最新一轮融资的金额、轮次和日期分别是多少？"
    "请直接查询数据库获取准确数字，不要凭印象回答。"
)

DB_PATH = os.path.join(os.path.dirname(__file__), "demo.db")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "eval_results", "q6.json")

# 单次运行允许等待的最长时间。这是客户端侧的超时，不是 LangGraph 服务端的超时——
# 超时后仍然会尝试查一次 run 的真实状态，不会因为客户端等不及就把这次运行判定为失败。
JOIN_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# Ground truth：直接查 demo.db，不读 ANSWER_KEY.md，也不经过 sql_executor 本身
# （避免"用同一段逻辑既生成期望值又验证期望值"的循环论证）
# ---------------------------------------------------------------------------

def get_ground_truth_from_db() -> dict:
    """只读方式打开 demo.db，查 NeuralPath 最新一轮融资的真实数据。

    用 round_date 排序取最新一条，而不是假设"只有一条记录"，这样即使以后
    demo.db 的数据变了（比如给 NeuralPath 加了新一轮融资），这里也能跟着变，
    不会给出过时的期望值。
    """
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT f.round_type, f.amount_usd, f.round_date
            FROM companies c
            JOIN funding_rounds f ON c.id = f.company_id
            WHERE c.name = 'NeuralPath'
            ORDER BY f.round_date DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise RuntimeError(
            "demo.db 里查不到 NeuralPath 的融资记录，ground truth 无法生成，"
            "评测本身无法进行——这不是评测失败，是数据前提不满足。"
        )

    round_type, amount_usd, round_date = row
    return {
        "round_type": round_type,
        "amount_usd": amount_usd,
        "round_date": round_date,
        "verification_query": (
            "SELECT f.round_type, f.amount_usd, f.round_date FROM companies c "
            "JOIN funding_rounds f ON c.id = f.company_id "
            "WHERE c.name = 'NeuralPath' ORDER BY f.round_date DESC LIMIT 1"
        ),
    }


def amount_string_candidates(amount_usd: float) -> list[str]:
    """生成金额的几种常见书写形式，用于宽松匹配。

    sqlite 的 REAL 类型读出来是带小数点的（比如 12000000.0），但最终报告或
    raw_notes 里的文字完全可能被格式化成不带小数点、或带千分位逗号的形式，
    三种都要认。
    """
    as_float = float(amount_usd)
    as_int_str = str(int(as_float)) if as_float.is_integer() else str(as_float)
    candidates = {
        str(as_float),          # 例如 "12000000.0"
        as_int_str,              # 例如 "12000000"
        f"{int(as_float):,}",    # 例如 "12,000,000"（仅整数金额时有意义）
    }
    return sorted(candidates)


def find_amount_matches_with_boundary(text: str, candidates: list[str]) -> list[str]:
    """在 text 里查找候选金额字符串，要求候选串前后不是数字字符。

    简单的 `candidate in text` 会把 "12000000" 误命中在 "112000000" 或
    "12000000123" 这种更长数字串的中间，这里用 (?<!\\d) / (?!\\d) 做边界检查，
    确保候选串左右不是紧贴着的数字字符（候选串内部的逗号、小数点不受影响）。
    """
    matches = []
    for candidate in candidates:
        pattern = r"(?<!\d)" + re.escape(candidate) + r"(?!\d)"
        if re.search(pattern, text):
            matches.append(candidate)
    return matches


def build_date_pattern(iso_date: str) -> re.Pattern:
    """把 "YYYY-MM-DD" 形式的 ground truth 日期，编译成能同时匹配 ISO 格式和
    中文日期格式的正则，覆盖模型撰写报告时常见的日期转写方式。

    背景（Q6 真实运行发现）：sql_executor 返回的日期是 ISO 格式（如
    "2025-11-02"），但最终报告经常被模型转写成中文日期（如 "2025年11月2日"），
    原来 `ground_truth["round_date"] in final_report` 这种精确子串匹配会把这种
    情况判成假阴性——回答本身没错，只是日期书写格式变了。

    覆盖范围：
      - ISO 原格式："2025-11-02"
      - 中文格式，日/月可选前导零："2025年11月2日" / "2025年11月02日"
      - 中文格式，数字与汉字之间可以有空格（真实报告里出现过）："2025 年 11 月 2 日"

    边界保护（避免误判，做法与 find_amount_matches_with_boundary 一致）：
      - ISO 分支和中文分支里的年/月/日数字，前后都加 (?<!\\d)/(?!\\d)，避免
        "2025-11-02" 被 "2025-11-020" 这种更长数字串误命中，也避免日期"2日"
        被"12日"这种同月更大的日子里的尾部数字误命中。
      - 数字与汉字单位之间只允许空格/制表符（[ \\t]*），不使用 \\s（那样会连
        换行符也一起放行）——换行打断的日期表达式（如 "2025年\\n11月2日"）
        视为已知不支持，不在这次最小方案范围内，由测试显式固定这个限制。
    """
    year, month, day = iso_date.split("-")
    month_i, day_i = int(month), int(day)

    # 只有个位数的月/日才需要"可选前导零"分支；两位数（10/11/12月，10-31日）
    # 不生成这个分支，避免出现 "0?11" 这种能匹配 "011" 的无意义写法。
    month_part = rf"0?{month_i}" if month_i < 10 else str(month_i)
    day_part = rf"0?{day_i}" if day_i < 10 else str(day_i)

    iso_part = re.escape(iso_date)
    ws = r"[ \t]*"
    cn_part = (
        rf"{year}{ws}年{ws}(?<!\d){month_part}(?!\d){ws}月{ws}"
        rf"(?<!\d){day_part}(?!\d){ws}日"
    )
    return re.compile(rf"(?<!\d){iso_part}(?!\d)|{cn_part}")


# ---------------------------------------------------------------------------
# 证据层 1：最终答案是否符合标准答案
# ---------------------------------------------------------------------------

def check_answer_matches_reference(final_report: str, ground_truth: dict) -> dict:
    amount_hits = find_amount_matches_with_boundary(
        final_report, amount_string_candidates(ground_truth["amount_usd"])
    )
    round_type_hit = ground_truth["round_type"] in final_report
    date_hit = bool(build_date_pattern(ground_truth["round_date"]).search(final_report))

    return {
        "amount_matched": bool(amount_hits),
        "amount_matched_candidates": amount_hits,
        "round_type_matched": round_type_hit,
        "round_date_matched": date_hit,
        "all_three_matched": bool(amount_hits) and round_type_hit and date_hit,
    }


# ---------------------------------------------------------------------------
# 证据层 2：raw_notes 里是否出现 sql_executor 原始输出格式的指纹
# ---------------------------------------------------------------------------

def check_raw_notes_fingerprint(raw_notes: list, ground_truth: dict) -> dict:
    """在 raw_notes 里找"像是 sql_executor 原始返回"的指纹。

    sql_executor.py 的输出是 " | ".join(columns) 做表头、逐行 " | ".join(...)
    拼数据（见 src/open_deep_research/sql_executor.py 的 _run_query），所以：
      - 金额数值本身出现，是弱信号（模型转述报告时也会写数字）；
      - 金额数值 + 竖线分隔符 " | " 或列名（amount_usd / round_date / round_type）
        同时出现，是强得多的信号——模型自己叙述时几乎不会主动带上这种
        "表头+竖线分隔"的原始格式。

    这里的返回结果只是"命中/未命中 + 命中片段"，不做任何"因此工具被调用了"的
    结论性判断，那个判断属于证据层 3，且这里的结果不允许反过来影响证据层 3。

    金额信号和格式信号必须出现在同一行才算命中——按 "\\n" 把 raw_notes 拆行后
    逐行检查，避免"金额出现在第一段叙述里、列名/竖线出现在几百字之后的另一段
    无关内容里"这种被拼出来的假阳性，确保两个信号确实来自同一段文本。
    """
    raw_notes_text = "\n".join(str(n) for n in raw_notes)
    amount_candidates = amount_string_candidates(ground_truth["amount_usd"])
    format_hints = ["amount_usd", "round_date", "round_type", " | "]

    matched_line = None
    matched_amount_hits: list[str] = []
    matched_format_hits: list[str] = []
    for line in raw_notes_text.split("\n"):
        line_amount_hits = find_amount_matches_with_boundary(line, amount_candidates)
        if not line_amount_hits:
            continue
        line_format_hits = [h for h in format_hints if h in line]
        if line_format_hits:
            matched_line = line
            matched_amount_hits = line_amount_hits
            matched_format_hits = line_format_hits
            break

    hit = matched_line is not None

    # 仅供排查参考，不参与 hit 判定：整段 raw_notes 里（不要求同一行）
    # 究竟出现过哪些候选金额/格式信号。
    amount_hits_anywhere = find_amount_matches_with_boundary(raw_notes_text, amount_candidates)
    format_hits_anywhere = [h for h in format_hints if h in raw_notes_text]

    return {
        "hit": hit,
        "matched_line": matched_line,
        "matched_amount_hits": matched_amount_hits,
        "matched_format_hits": matched_format_hits,
        "amount_hits_anywhere": amount_hits_anywhere,
        "format_hits_anywhere": format_hits_anywhere,
        "caveat": (
            "间接证据：命中说明 raw_notes 里某一行同时出现了疑似 sql_executor 原始"
            "输出格式的金额和列名/竖线特征，不能证明该内容一定来自 sql_executor，"
            "也不能证明工具确实被调用过。"
        ),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def run_q6_eval() -> dict:
    result: dict = {
        "question_id": "Q6",
        "input_question": QUESTION_TEXT,
        "run_config": {"configurable": {"allow_clarification": False}},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "thread_id": None,
        "run_id": None,
        "run_status": None,
        "wall_clock_seconds": None,
        "final_report": "",
        "raw_notes": [],
        "tasks_errors": [],
        "ground_truth": None,
        "evidence": {
            "answer_matches_reference": None,
            "raw_notes_fingerprint_hit": None,
            # 写死的字面量 False：当前架构下 researcher 子图是在 supervisor_tools
            # 函数体内部用 .ainvoke() 程序化调用的，不是 add_node 挂接的图节点，
            # 结构上没有任何字段能证明"具体某个工具被调用了"。这个值不随前两层
            # 证据的命中结果变化，任何时候都不能被改写成条件表达式。
            "tool_invocation_structurally_confirmed": False,
        },
        "run_error": None,
        "human_review_status": "not_reviewed",
        "notes_for_reviewer": "",
    }

    # ---- Ground truth：先于任何网络调用完成，失败则直接终止 ----
    try:
        ground_truth = get_ground_truth_from_db()
        result["ground_truth"] = ground_truth
    except Exception as e:
        result["run_error"] = f"生成 ground truth 失败，评测未执行：{type(e).__name__}: {e}"
        result["run_status"] = "aborted_before_run"
        return result

    client = get_client(url=LANGGRAPH_URL)

    start_time = time.monotonic()
    try:
        thread = await client.threads.create()
        result["thread_id"] = thread["thread_id"]

        run = await client.runs.create(
            thread_id=thread["thread_id"],
            assistant_id=ASSISTANT_ID,
            input={"messages": [{"role": "human", "content": QUESTION_TEXT}]},
            config={"configurable": {"allow_clarification": False}},
        )
        result["run_id"] = run["run_id"]

        try:
            await asyncio.wait_for(
                client.runs.join(thread_id=thread["thread_id"], run_id=run["run_id"]),
                timeout=JOIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # 客户端等不下去了，但运行不一定真的卡死——不直接判失败，
            # 下面照样去查一次服务端的真实 run 状态和当前 state。
            result["run_error"] = (
                f"客户端等待超过 {JOIN_TIMEOUT_SECONDS} 秒未收到 join() 返回，"
                "已停止等待，但运行本身可能仍在服务端继续，run_status 是当前查询到的快照。"
            )

        result["wall_clock_seconds"] = time.monotonic() - start_time

        # ---- 查服务端记录的真实运行状态，不依赖 join() 的返回值语义 ----
        try:
            run_info = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
            result["run_status"] = run_info.get("status")
        except Exception as e:
            result["run_status"] = "unknown"
            existing_error = result["run_error"]
            new_error = f"查询 run 状态失败：{type(e).__name__}: {e}"
            result["run_error"] = f"{existing_error}; {new_error}" if existing_error else new_error

        # ---- 取最终 state：字段一律用 .get 带默认值，避免缺字段导致脚本崩溃 ----
        try:
            state = await client.threads.get_state(thread["thread_id"])
            values = state.get("values", {}) if isinstance(state, dict) else {}
            result["final_report"] = values.get("final_report", "") or ""
            result["raw_notes"] = values.get("raw_notes", []) or []

            tasks = state.get("tasks", []) if isinstance(state, dict) else []
            result["tasks_errors"] = [
                {"name": t.get("name"), "error": t.get("error")}
                for t in tasks
                if isinstance(t, dict) and t.get("error")
            ]
        except Exception as e:
            existing_error = result["run_error"]
            new_error = f"读取 state 失败：{type(e).__name__}: {e}"
            result["run_error"] = f"{existing_error}; {new_error}" if existing_error else new_error

    except Exception as e:
        result["wall_clock_seconds"] = time.monotonic() - start_time
        result["run_status"] = result["run_status"] or "error"
        existing_error = result["run_error"]
        new_error = f"运行过程发生异常：{type(e).__name__}: {e}"
        result["run_error"] = f"{existing_error}; {new_error}" if existing_error else new_error

    # ---- 证据计算：final_report / raw_notes 即使为空也照常跑一遍（空输入得到空结果，不跳过） ----
    result["evidence"]["answer_matches_reference"] = check_answer_matches_reference(
        result["final_report"], ground_truth
    )
    result["evidence"]["raw_notes_fingerprint_hit"] = check_raw_notes_fingerprint(
        result["raw_notes"], ground_truth
    )
    # tool_invocation_structurally_confirmed 已在 result 初始化时写死为 False，
    # 这里特意不再赋值，避免未来改动时被误改成依赖上面两层证据的表达式。

    if not result["final_report"] and not result["raw_notes"]:
        result["notes_for_reviewer"] = (
            "final_report 和 raw_notes 均为空，可能是运行未完成、图提前退出，"
            "或 state 读取失败——请结合 run_status 和 run_error 人工核实，"
            "不要把这种情况当作'工具/回答均未命中'的确定性负面结果。"
        )

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


async def main() -> None:
    result = await run_q6_eval()

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"结果已写入 {OUTPUT_PATH}")
    print(f"run_status = {result['run_status']}")
    print(f"answer_matches_reference = {result['evidence']['answer_matches_reference']}")
    print(f"raw_notes_fingerprint_hit.hit = {result['evidence']['raw_notes_fingerprint_hit']['hit']}")
    print(
        "tool_invocation_structurally_confirmed = "
        f"{result['evidence']['tool_invocation_structurally_confirmed']}"
        "  (架构限制：固定为 False，不代表工具一定没被调用，只代表拿不到结构化证据)"
    )
    if result["run_error"]:
        print(f"run_error: {result['run_error']}")


if __name__ == "__main__":
    asyncio.run(main())
