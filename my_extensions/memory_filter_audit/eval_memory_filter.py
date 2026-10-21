"""存量记忆过滤审计：对一份冻结的合成语料（正常 + 恶意存量记忆），比较“无过滤 / 加固前 / 加固后”。

用法：python eval_memory_filter.py --guard-file PATH --label LABEL [--out PATH]
  --guard-file none  表示不做任何过滤（基线：所有存量记忆都会进入上下文）。
指标：恶意记忆被忽略数（按类型）、正常记忆保留数（按类型，误伤 = 被丢弃的正常记忆）。
边界：语料由项目作者编写，不独立于防护设计；规则型过滤的结果不外推到真实攻击总体。
"""
import argparse, collections, hashlib, importlib.util, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_split import restore  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, os.environ.get("MEMORY_CORPUS", "memory_filter_corpus.json"))


EXCLUDE = {"memory_filter_corpus.json": {"M45": "合成银行卡号未通过 Luhn 校验（语料构造缺陷），不应计为应拦截项"}}


def load_guard(path):
    if path == "none":
        return None
    spec = importlib.util.spec_from_file_location("mg_under_test", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def run(guard_file, label):
    raw = restore(open(CORPUS, "rb").read())  # 还原被拆开的合成密钥样本；SHA 以还原后字节计算
    excl = EXCLUDE.get(os.path.basename(CORPUS), {})
    items = [it for it in json.loads(raw)["items"] if it["id"] not in excl]
    g = load_guard(guard_file)
    if g is None:
        kept_ids = {it["id"] for it in items}
        reasons = {}
    else:
        kept, dropped = g.filter_read([{"id": it["id"], "memory": it["memory"]} for it in items])
        kept_ids = {k["id"] for k in kept}
        reasons = {}
        # filter_read 只返回被丢弃条目前 40 字，这里逐条重判以记录原因
        for it in items:
            if it["id"] in kept_ids:
                continue
            reasons[it["id"]] = "instruction_like" if g.contains_injection(it["memory"]) else "sensitive"
    by = collections.defaultdict(lambda: {"n": 0, "ignored": 0})
    for it in items:
        c = by[it["type"]]; c["n"] += 1; c["ignored"] += it["id"] not in kept_ids
    mal = [it for it in items if it["label"] == "malicious"]
    ben = [it for it in items if it["label"] == "benign"]
    return {
        "label": label, "corpus_sha256": hashlib.sha256(raw).hexdigest(), "excluded": excl,
        "malicious_total": len(mal), "malicious_ignored": sum(it["id"] not in kept_ids for it in mal),
        "benign_total": len(ben), "benign_dropped": sum(it["id"] not in kept_ids for it in ben),
        "by_type": dict(by),
        "missed_malicious": [it["id"] for it in mal if it["id"] in kept_ids],
        "dropped_benign": [it["id"] for it in ben if it["id"] not in kept_ids],
        "drop_reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--guard-file", required=True); ap.add_argument("--label", required=True); ap.add_argument("--out")
    a = ap.parse_args()
    r = run(a.guard_file, a.label)
    print(f"[{r['label']}] 恶意记忆被忽略 {r['malicious_ignored']}/{r['malicious_total']}；正常记忆被误丢 {r['benign_dropped']}/{r['benign_total']}")
    for t, c in sorted(r["by_type"].items()):
        print(f"   {t:<20} ignored {c['ignored']}/{c['n']}")
    print("   漏掉的恶意:", r["missed_malicious"]); print("   误丢的正常:", r["dropped_benign"])
    if a.out:
        if os.path.exists(a.out): raise SystemExit(f"拒绝覆盖: {a.out}")
        json.dump(r, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
