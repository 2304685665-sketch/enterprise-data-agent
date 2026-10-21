import subprocess, time, json, os, signal, urllib.request, urllib.error, sys
PY="/tmp/full313/bin/python"; RES={}
def sh(*a, **k): return subprocess.Popen(list(a), **k)
def http(method, path, body=None, port=8791):
    req=urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body else None, headers={"content-type":"application/json"})
    try:
        r=urllib.request.urlopen(req, timeout=5); return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}"), dict(e.headers)
    except Exception as e: return "conn_err", str(e)[:80], {}
def redis_start():
    os.makedirs("/tmp/rr/data",exist_ok=True)
    subprocess.run(["redis-server","--port","6391","--dir","/tmp/rr/data","--appendonly","yes","--daemonize","yes"],stdout=subprocess.DEVNULL); time.sleep(1)
def redis_kill():
    out=subprocess.run(["pgrep","-f","redis-server.*6391"],capture_output=True,text=True).stdout.split()
    for p in out: os.kill(int(p), signal.SIGKILL)
    time.sleep(0.5)
def svc_start():
    env=dict(os.environ, PYTHONPATH="/tmp/rr")
    p=sh(PY,"-m","uvicorn","svc:app","--port","8791","--log-level","error",cwd="/tmp/rr",env=env); time.sleep(3); return p
subprocess.run("rm -rf /tmp/rr/data; mkdir -p /tmp/rr/data",shell=True)
redis_start(); s=svc_start()
print("healthz", http("GET","/healthz")[:2])
# 场景A：任务执行中 Redis 被 SIGKILL
st,b,_=http("POST","/research",{"question":"scenario A"}); tid=b["task_id"]; print("A submit",st)
time.sleep(1); redis_kill()
print("A redis killed ->", "healthz", http("GET","/healthz")[:2], "| status", http("GET",f"/research/{tid}")[:2], "| submit", http("POST","/research",{"question":"x"})[:2])
redis_start(); time.sleep(8)
print("A after restart (AOF) status", http("GET",f"/research/{tid}")[:2])
print("A healthz", http("GET","/healthz")[:2])
# 场景B：服务进程被 SIGKILL，新进程接管
st,b,_=http("POST","/research",{"question":"scenario B"}); tidb=b["task_id"]; time.sleep(1)
s.kill(); s.wait(); time.sleep(0.5)
s2=svc_start()
print("B status immediately after new process", http("GET",f"/research/{tidb}")[:2])
time.sleep(3)
print("B status after >stale", http("GET",f"/research/{tidb}")[:2])
print("B replay idempotent POST", http("POST","/research",{"question":"scenario B"})[:2])
s2.kill(); redis_kill()
