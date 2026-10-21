"""离线检验写语句过滤：只读已有评测结果里的 final_report，不调用任何 API。
统计：①写请求报告中含写语句的数量 过滤前→后；②非写请求/无写语句报告被改动的数量（误伤）；
③被移除的围栏代码块里是否同时含只读 SELECT（已知代价）。"""
import glob, importlib.util, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", "src", "open_deep_research", name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
F, W = _load("report_write_filter"), _load("write_intent")
SELECT = re.compile(r"\bselect\b", re.I)
rows = []
for p in sorted(glob.glob(os.path.join(HERE, "eval_results", "*", "case_*.json"))):
    d = json.load(open(p, encoding="utf-8")); rep = d.get("final_report")
    if not isinstance(rep, str) or not rep: continue
    run = p.split(os.sep)[-2]
    w = W.detect_write_request(d.get("question", ""))
    after, n = F.strip_write_sql(rep)
    rows.append(dict(run=run, id=d["case_id"], write_intent=w, before=bool(F.WRITE_RE.search(rep)), after=bool(F.WRITE_RE.search(after)),
                     changed=(after != rep), n=n, lost_select=any(SELECT.search(b) and F.WRITE_RE.search(b) for b in re.findall(r"```[^\n]*\n.*?```", rep, re.S))))
def agg(rs): return dict(reports=len(rs), contain_write_sql_before=sum(r["before"] for r in rs), after=sum(r["after"] for r in rs), changed=sum(r["changed"] for r in rs))
W_ = [r for r in rows if r["write_intent"]]; R_ = [r for r in rows if not r["write_intent"]]
out = dict(write_intent=agg(W_), non_write=agg(R_),
           non_write_would_change_if_filter_were_applied=sum(r["changed"] for r in R_),
           blocks_losing_select=sum(r["lost_select"] for r in W_),
           by_run={k: agg([r for r in W_ if r["run"] == k]) for k in sorted({r["run"] for r in W_})})
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(dict(summary=out, rows=rows), open(os.path.join(HERE, "eval_results", "report_write_filter_offline_20261013.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
