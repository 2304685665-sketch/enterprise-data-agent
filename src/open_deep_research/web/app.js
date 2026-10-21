// Deep Research 控制台（React 18，UMD，无构建步骤）。
// 约定：所有用户/服务端文本只通过 React 文本节点渲染（不使用 dangerouslySetInnerHTML），避免 XSS；
// API key 只保存在组件内存里，不写 localStorage，刷新页面即清除。
(function () {
  const h = React.createElement;
  const { useState, useEffect, useRef, useCallback } = React;

  function newKey() {
    return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
  }

  function useApi(apiKey) {
    return useCallback(async function (path, opts) {
      const headers = Object.assign({ "Content-Type": "application/json" }, (opts && opts.headers) || {});
      if (apiKey) headers["X-API-Key"] = apiKey;
      return fetch(path, Object.assign({}, opts, { headers }));
    }, [apiKey]);
  }

  function fmt(v, digits) {
    if (v === null || v === undefined) return "—";
    return typeof v === "number" ? (digits !== undefined ? v.toFixed(digits) : String(v)) : String(v);
  }

  function MetricsPanel({ api }) {
    const [m, setM] = useState(null);
    const [err, setErr] = useState("");
    useEffect(function () {
      let alive = true;
      async function load() {
        try {
          const r = await api("/metrics/summary");
          if (!r.ok) throw new Error("HTTP " + r.status);
          const j = await r.json();
          if (alive) { setM(j); setErr(""); }
        } catch (e) { if (alive) setErr(String(e.message || e)); }
      }
      load();
      const t = setInterval(load, 5000);
      return function () { alive = false; clearInterval(t); };
    }, [api]);
    if (err) return h("div", { className: "card" }, h("h2", null, "指标"), h("div", { className: "muted" }, "无法加载：" + err));
    if (!m) return h("div", { className: "card" }, h("h2", null, "指标"), h("div", { className: "muted" }, "加载中…"));
    const rate = m.success_rate === null ? "—" : (m.success_rate * 100).toFixed(1) + "%";
    const stat = function (label, value) {
      return h("div", { className: "stat", key: label }, h("b", null, value), h("span", null, label));
    };
    return h("div", { className: "card" },
      h("h2", null, "指标（最近统计）"),
      h("div", { className: "grid" },
        stat("调用量", fmt(m.tasks_created)),
        stat("成功率", rate),
        stat("p50 耗时(ms)", fmt(m.duration_ms.p50, 0)),
        stat("p95 耗时(ms)", fmt(m.duration_ms.p95, 0)),
        stat("幂等命中", fmt(m.idempotent_replays)),
        stat("被限流", fmt(m.rate_limited)),
        stat("今日回访调用方", fmt(m.returning_clients_today))
      ),
      h("div", { className: "muted", style: { marginTop: 8 } }, "耗时样本数：" + m.duration_ms.samples + "；成功率 = 成功 /（成功 + 失败），尚未完成的任务不计入。")
    );
  }

  function ApprovalsPanel({ api }) {
    const [items, setItems] = useState(null);
    const load = useCallback(async function () {
      const r = await api("/approvals");
      if (r.status === 404) { setItems(null); return; }
      if (r.ok) setItems((await r.json()).pending);
    }, [api]);
    useEffect(function () {
      load().catch(function () {});
      const t = setInterval(function () { load().catch(function () {}); }, 3000);
      return function () { clearInterval(t); };
    }, [load]);
    if (items === null) return null;
    async function decide(id, approve) {
      await api("/approvals/" + id, { method: "POST", body: JSON.stringify({ approve: approve }) });
      load();
    }
    return h("div", { className: "card" },
      h("h2", null, "待人工审批（" + items.length + "）"),
      items.length === 0 ? h("div", { className: "muted" }, "暂无") :
        h("ul", null, items.map(function (it) {
          return h("li", { key: it.id },
            h("pre", null, it.summary),
            h("div", { className: "row", style: { margin: "6px 0 10px" } },
              h("button", { onClick: function () { decide(it.id, true); } }, "批准"),
              h("button", { className: "secondary", onClick: function () { decide(it.id, false); } }, "拒绝")));
        })));
  }

  function App() {
    const [apiKey, setApiKey] = useState("");
    const [question, setQuestion] = useState("");
    const [task, setTask] = useState(null); // {id, status, result, error, replay}
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState("");
    const idemRef = useRef(newKey());
    const api = useApi(apiKey);

    async function followEvents(taskId) {
      const r = await api("/research/" + taskId + "/events");
      if (!r.ok || !r.body) throw new Error("事件流失败：HTTP " + r.status);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
          const line = block.split("\n").find(function (l) { return l.indexOf("data:") === 0; });
          if (!line) continue;
          const d = JSON.parse(line.slice(5));
          setTask(function (t) { return Object.assign({}, t, { status: d.status, result: d.result, error: d.error }); });
        }
      }
    }

    async function submit() {
      setBusy(true); setMsg("");
      try {
        const r = await api("/research", {
          method: "POST",
          headers: { "Idempotency-Key": idemRef.current },
          body: JSON.stringify({ question: question })
        });
        const j = await r.json();
        if (r.status === 429) { setMsg("请求过于频繁，请 " + (r.headers.get("Retry-After") || "稍后") + " 秒后重试"); return; }
        if (!r.ok) { setMsg("提交失败：" + (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail))); return; }
        setTask({ id: j.task_id, status: "pending", replay: j.idempotent_replay });
        idemRef.current = newKey(); // 下一次提交使用新的幂等键；同一次点击的重试沿用同一个键
        await followEvents(j.task_id);
      } catch (e) {
        setMsg(String(e.message || e));
      } finally { setBusy(false); }
    }

    return h("main", null,
      h("div", { className: "card" },
        h("h1", null, "Deep Research 控制台"),
        h("div", { className: "muted" }, "提交研究问题，实时查看任务状态；下方为服务指标与待审批的高风险工具调用。")),
      h("div", { className: "card" },
        h("h2", null, "提交任务"),
        h("div", { className: "row", style: { marginBottom: 8 } },
          h("input", { type: "password", placeholder: "X-API-Key（服务未启用鉴权可留空）", value: apiKey,
                       onChange: function (e) { setApiKey(e.target.value); }, autoComplete: "off" })),
        h("textarea", { placeholder: "输入问题…", value: question, onChange: function (e) { setQuestion(e.target.value); } }),
        h("div", { className: "row", style: { marginTop: 8 } },
          h("button", { onClick: submit, disabled: busy || !question.trim() }, busy ? "运行中…" : "提交"),
          msg ? h("span", { className: "muted" }, msg) : null)),
      task ? h("div", { className: "card" },
        h("h2", null, "任务 " + task.id),
        h("div", { className: "row" },
          h("span", { className: "pill " + task.status }, task.status),
          task.replay ? h("span", { className: "muted" }, "（幂等命中：复用已有任务）") : null),
        task.error ? h("pre", { style: { color: "var(--bad)", marginTop: 8 } }, task.error) : null,
        task.result ? h("pre", { style: { marginTop: 8 } }, task.result) : null) : null,
      h(ApprovalsPanel, { api: api }),
      h(MetricsPanel, { api: api })
    );
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();
