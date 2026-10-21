"""间接注入防护的离线检测评测（确定性、不花钱）：投毒后的 chunk 里，注入句有多少被 doc_content_guard 屏蔽、事实句有多少被保留；
以及 46 个真实 chunk 上的误屏蔽数。用法: python inject_offline_detection.py [--out PATH]"""
import json, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")
import eval_retrieval as ER, build_poison_kb as BP
from agent_eval_cases_inject import SPEC
from open_deep_research import doc_content_guard as G


def run():
    base = ER.load_kb_chunks(BP.SRC)
    fp = [c["id"] for c in base if G.sanitize_chunk(c["text"])[1] > 0]
    tmp = tempfile.mkdtemp(); BP.build(os.path.join(tmp, "p"))
    pois = {c["id"]: c["text"] for c in ER.load_kb_chunks(os.path.join(tmp, "p"))}
    rows = []
    for cid, doc, q, expect, kind, inj, canary in SPEC:
        txt = [t for k, t in pois.items() if k.startswith(doc + "_chunk") and inj in t]
        assert len(txt) == 1, (cid, len(txt))
        out, n = G.sanitize_chunk(txt[0])
        rows.append({"id": cid, "kind": kind, "blocked": n > 0, "injection_survives": inj in out or (canary and canary in out),
                     "fact_kept": expect in out})
    res = {"real_chunks": len(base), "false_positive_chunks": fp,
           "explicit": {"n": sum(r["kind"] == "explicit" for r in rows), "blocked": sum(r["blocked"] for r in rows if r["kind"] == "explicit")},
           "paraphrase": {"n": sum(r["kind"] == "paraphrase" for r in rows), "blocked": sum(r["blocked"] for r in rows if r["kind"] == "paraphrase")},
           "facts_kept": sum(r["fact_kept"] for r in rows), "rows": rows}
    return res


if __name__ == "__main__":
    r = run(); print(json.dumps({k: v for k, v in r.items() if k != "rows"}, ensure_ascii=False, indent=1))
    print("未被屏蔽的:", [x["id"] for x in r["rows"] if not x["blocked"]], "| 事实句丢失:", [x["id"] for x in r["rows"] if not x["fact_kept"]])
    if "--out" in sys.argv:
        o = sys.argv[sys.argv.index("--out") + 1]
        if os.path.exists(o): raise SystemExit(f"拒绝覆盖: {o}")
        json.dump(r, open(o, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
