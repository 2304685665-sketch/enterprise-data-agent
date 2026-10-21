import json, sys, os, collections
R=os.path.join(os.path.dirname(os.path.abspath(__file__)),"eval_results")+os.sep
def load(d):
    s=json.load(open(R+d+"/summary.json")); full={}
    for c in s["cases"]:
        f=R+d+"/case_"+c["case_id"]+".json"
        full[c["case_id"]]=json.load(open(f)) if os.path.exists(f) else {}
    return s,full
def stats(d):
    s,full=load(d); rows=[]
    for c in s["cases"]:
        f=full[c["case_id"]]; ev=f.get("evidence",{})
        rows.append(dict(id=c["case_id"],cat=f.get("category"),status=c["run_status"],valid=c["valid_for_scoring"],
          passed=c["score_passed"],issues=c["issues"],sql=ev.get("sql_calls"),calls=c["model_calls"]["ok"],
          cost=c["model_calls"]["cost_usd"],wall=f.get("wall_clock_seconds"),
          ung=bool(ev.get("ungrounded_figures",{}).get("dates") or ev.get("ungrounded_figures",{}).get("amounts"))))
    return s,rows
def usage(rows):
    n=len(rows) or 1
    return dict(model_calls_per_case=sum(r["calls"] for r in rows)/n, cost_per_case_usd=sum(r["cost"] for r in rows)/n,
                sql_calls_per_case=sum(r["sql"] or 0 for r in rows)/n)
def agg(rows):
    by=collections.defaultdict(lambda:[0,0,0])
    for r in rows:
        for k in (r["cat"],"ALL"):
            by[k][0]+=1; by[k][1]+=1 if r["passed"] else 0; by[k][2]+=1 if r["valid"] else 0
    return by
if __name__=="__main__":
    a=sys.argv[1]; b=sys.argv[2] if len(sys.argv)>2 else None
    sa,ra=stats(a)
    print("A",a,"guard_env=",repr(sa["settings"]["grounding_guard_env"]),"cost",sa["budget"]["actual_usd"],"calls",sa["budget"]["calls_ok"],"tripped",sa["budget"]["tripped"],"abort",sa["budget_aborted"],"secret",sa["secret_scan_hits"])
    for k,v in sorted(agg(ra).items()): print("  ",k,"pass",v[1],"/",v[0],"valid",v[2])
    print("  limits",sa["settings"].get("extra_config") or "Configuration defaults","usage",{k:round(v,4) for k,v in usage(ra).items()})
    ic=collections.Counter(i for r in ra for i in r["issues"]); print("  issues",dict(ic)); print("  ungrounded cases",[r["id"] for r in ra if r["ung"]])
    print("  invalid",[(r["id"],r["status"]) for r in ra if not r["valid"]])
    print("  failed",[r["id"] for r in ra if r["valid"] and not r["passed"]])
    if b:
        sb,rb=stats(b)
        print("B",b,"guard_env=",repr(sb["settings"]["grounding_guard_env"]),"cost",sb["budget"]["actual_usd"],"calls",sb["budget"]["calls_ok"],"tripped",sb["budget"]["tripped"],"abort",sb["budget_aborted"],"secret",sb["secret_scan_hits"],"code",sa["code_version"]==sb["code_version"])
        for k,v in sorted(agg(rb).items()): print("  ",k,"pass",v[1],"/",v[0],"valid",v[2])
        print("  limits",sb["settings"].get("extra_config") or "Configuration defaults","usage",{k:round(v,4) for k,v in usage(rb).items()})
        ic=collections.Counter(i for r in rb for i in r["issues"]); print("  issues",dict(ic)); print("  ungrounded cases",[r["id"] for r in rb if r["ung"]])
        print("  failed",[r["id"] for r in rb if r["valid"] and not r["passed"]])
        A={r["id"]:r for r in ra}; B={r["id"]:r for r in rb}
        print("new_fail(B only fails)",[i for i in A if A[i]["passed"] and not B[i]["passed"]])
        print("new_pass(B only passes)",[i for i in A if not A[i]["passed"] and B[i]["passed"]])
        print("sql_calls mean A/B",sum(r["sql"] or 0 for r in ra)/len(ra),sum(r["sql"] or 0 for r in rb)/len(rb))
        print("wall mean A/B",sum(r["wall"] or 0 for r in ra)/len(ra),sum(r["wall"] or 0 for r in rb)/len(rb))
