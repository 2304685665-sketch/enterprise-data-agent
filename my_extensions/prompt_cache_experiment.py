"""Prompt 缓存实验，见 PROTOCOL_prompt_cache.md。用法: python my_extensions/prompt_cache_experiment.py --out my_extensions/prompt_cache_results/run1.json
需要 OPENAI_API_KEY（从环境或 .env 读取，不打印）。--dry 只打印前缀 token 估算，不调用 API。"""
import argparse, json, os, sys, time, uuid
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
MODEL = "gpt-4.1-mini"
PRICE_IN, PRICE_CACHED = 0.40 / 1e6, 0.10 / 1e6
QUESTIONS = ["列出融资轮次最多的三家公司。", "2023 年的融资总额是多少？", "最大的一笔融资发生在哪一年？",
             "哪个行业的平均融资额最高？", "比较 A 轮与 B 轮的平均融资额。"]
ROUNDS = 6
BUDGET_USD = 0.20
REFERENCE = ("参考材料（合成，用于补足前缀体量）：" + "融资数据表字段说明与口径约定：金额单位为万美元；轮次按时间升序；缺失值不参与均值。" * 40)


def build_prefix():
    from open_deep_research.prompts import research_system_prompt
    from open_deep_research.sql_executor import build_sql_executor_description
    sp = research_system_prompt.format(date="2026-10-13", mcp_prompt="")
    return sp + "\n\n" + build_sql_executor_description(True, os.path.join(HERE, "demo.db")) + "\n\n" + REFERENCE


def make_messages(prefix, question, k, nonce=None):
    sys_text = (f"[{nonce}] " if nonce else "") + prefix
    msgs = [{"role": "system", "content": sys_text}, {"role": "user", "content": question}]
    for i in range(1, k):
        msgs.append({"role": "assistant", "content": f"第 {i} 步：已查询并记录要点。"})
        msgs.append({"role": "user", "content": f"观察 {i}：查询返回 5 行结果，字段 name, year, amount。请继续。"})
    return msgs


def cost(prompt_tokens, cached):
    return (prompt_tokens - cached) * PRICE_IN + cached * PRICE_CACHED


def run(client, out):
    prefix = build_prefix()
    rows, total = [], 0.0
    for arm in ("CACHED", "CONTROL"):
        for qi, q in enumerate(QUESTIONS):
            for k in range(1, ROUNDS + 1):
                if total > BUDGET_USD:
                    raise SystemExit(f"超出预算 ${BUDGET_USD}，停止（已记录 {len(rows)} 次）")
                msgs = make_messages(prefix, q, k, nonce=uuid.uuid4().hex if arm == "CONTROL" else None)
                r = client.chat.completions.create(model=MODEL, messages=msgs, max_tokens=16)
                u = r.usage
                cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                c = cost(u.prompt_tokens, cached); total += c
                rows.append({"arm": arm, "q": qi, "round": k, "prompt_tokens": u.prompt_tokens, "cached_tokens": cached,
                             "cost_usd": round(c, 6), "cost_without_cache_usd": round(cost(u.prompt_tokens, 0), 6)})
                time.sleep(1.0)
    return rows, total


def summarize(rows):
    out = {}
    for arm in ("CACHED", "CONTROL"):
        for scope, f in (("all", lambda r: True), ("round>=2", lambda r: r["round"] >= 2)):
            sel = [r for r in rows if r["arm"] == arm and f(r)]
            pt = sum(r["prompt_tokens"] for r in sel); ct = sum(r["cached_tokens"] for r in sel)
            out[f"{arm}/{scope}"] = {"calls": len(sel), "prompt_tokens": pt, "cached_tokens": ct,
                                     "hit_rate": round(ct / pt, 4) if pt else None,
                                     "cost": round(sum(r["cost_usd"] for r in sel), 5),
                                     "cost_without_cache": round(sum(r["cost_without_cache_usd"] for r in sel), 5)}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out"); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    prefix = build_prefix()
    est = len(prefix) // 2  # 粗估：中英混合约 2 字符/token，只用于 dry 提示
    print(f"前缀字符数 {len(prefix)}，粗估 ≥{est} token（实际以 API 返回的 prompt_tokens 为准）")
    if a.dry: raise SystemExit(0)
    if not a.out: raise SystemExit("需要 --out")
    if os.path.exists(a.out): raise SystemExit(f"拒绝覆盖: {a.out}")
    from dotenv import load_dotenv; load_dotenv()
    from openai import OpenAI
    rows, total = run(OpenAI(), a.out)
    res = {"model": MODEL, "total_cost_usd": round(total, 5), "summary": summarize(rows), "rows": rows}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(res, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(res["summary"], ensure_ascii=False, indent=1)); print("total $", res["total_cost_usd"])
