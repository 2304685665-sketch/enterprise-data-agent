"""counterparty_due_diligence 工具 —— 客商尽调的确定性风险信号提取（ODR_DILIGENCE=1 才注册，默认关闭）。

动机：贸易公司对客商做尽调时，要手动查企业工商、司法、变更记录，再逐条判断哪些是风险信号。
这里把"判断规则"写成可测试的确定性代码：模型只负责引用结果，不自己从原始文本里挑信号，也不编造。

输入：ODR_DILIGENCE_DIR（默认 my_extensions/diligence_data）下的工商快照文本，格式见 diligence_data/README.md。
它不联网、不抓取任何网站；快照要由人从公开渠道（如国家企业信用信息公示系统）整理后放入该目录。

边界（诚实）：
- 只基于快照里写了的内容。快照没有的维度（如招投标、舆情）不判断，输出末尾会声明。
- 阈值（减资比例、时间窗口、笔数）是写在 THRESHOLDS 里的经验规则，不是法律结论，也不构成法律意见。
- 找不到快照、名称不唯一、快照缺关键字段时返回明确错误，不猜。
"""
import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from langchain_core.tools import tool

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "my_extensions" / "diligence_data"

THRESHOLDS = {
    "capital_reduction_high_ratio": 0.5,   # 减资比例 >= 50% 判高
    "capital_window_months": 60,
    "legal_rep_window_months": 24,         # 窗口内 1 次判中，>=2 次判高
    "equity_window_months": 60,            # 窗口内不同日期的股权变动：1 次低，>=2 次中，>=5 次高
    "litigation_window_months": 36,        # 窗口内新增立案：>=1 次中，>=4 次高
    "litigation_high_count": 4,
    "address_window_months": 24,
    "personnel_window_months": 24,
    "young_company_medium_months": 12,
    "young_company_low_months": 36,
    "low_headcount": 10,
}
# 风险统计项：>0 即触发；值为严重程度
STAT_SEVERITY = {
    "失信被执行人": "high", "被执行人": "high", "限制高消费": "high", "严重违法": "high", "股权冻结": "high",
    "经营异常": "medium", "行政处罚": "medium",
}
SEV_ORDER = {"high": 3, "medium": 2, "low": 1, "info": 0}
SEV_CN = {"high": "高", "medium": "中", "low": "低", "info": "提示"}

DILIGENCE_DESCRIPTION = (
    "Counterparty due diligence over a locally stored business-registration snapshot (no web access). "
    "Takes a company name (exact or unique partial match) and returns the overall risk level and the specific risk signals "
    "(capital reduction, legal-representative change, shareholder change, litigation filings, address change, "
    "enforcement / dishonest-debtor / abnormal-operation counts, very young company, tiny headcount), each with the exact source line "
    "and its date. Use it for any counterparty / customer / supplier due-diligence request and quote its findings verbatim; "
    "if it says no snapshot exists, tell the user that no registration data is available instead of guessing."
)


@dataclass
class Flag:
    id: str
    severity: str
    title: str
    detail: str
    evidence: list = field(default_factory=list)
    source: str = ""


def _months_between(d1: date, d2: date) -> int:
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) - (1 if d2.day < d1.day else 0)


def _parse_date(s: str):
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)
    except (ValueError, AttributeError):
        return None


def _wan(s: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*万", s or "")
    return float(m.group(1)) if m else None


def parse_snapshot(text: str) -> dict:
    """解析快照文本；返回 {fields, stats, events}。"""
    fields, stats, events = {}, {}, []
    section = "head"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") and not line.startswith("##"):
            continue
        if line.startswith("##"):
            title = line.lstrip("#").strip()
            section = "stats" if "风险统计" in title else "events" if "动态" in title else "head"
            continue
        if section == "events":
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(.+)$", line)
            if m:
                events.append((m.group(1), m.group(2), line))
            continue
        m = re.match(r"^([^：:]+)[：:]\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1).strip(), m.group(2).strip()
        if section == "stats":
            try:
                stats[k] = int(v)
            except ValueError:
                pass
        else:
            fields[k] = v
    return {"fields": fields, "stats": stats, "events": events}


def analyze(snapshot: dict) -> dict:
    """规则引擎。返回 {company, as_of, level, flags}。缺关键字段时抛 ValueError。"""
    f, stats, events = snapshot["fields"], snapshot["stats"], snapshot["events"]
    name = f.get("企业名称")
    as_of = _parse_date(f.get("取数日期", ""))
    if not name or as_of is None:
        raise ValueError("snapshot lacks 企业名称 or a valid 取数日期 (YYYY-MM-DD)")
    T = THRESHOLDS
    flags: list = []

    def recent(months):
        for ds, body, line in events:
            d = _parse_date(ds)
            if d is not None and 0 <= _months_between(d, as_of) <= months:
                yield d, body, line

    # 1 减资
    for d, body, line in recent(T["capital_window_months"]):
        m = re.search(r"注册资本减少[：:]\s*从\s*([\d.]+)\s*万元\s*减少到\s*([\d.]+)\s*万元", body)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            ratio = (a - b) / a if a else 0
            sev = "high" if ratio >= T["capital_reduction_high_ratio"] else "medium"
            flags.append(Flag("CAPITAL_REDUCTION", sev, "注册资本减少",
                              f"{d.isoformat()} 注册资本从 {a:g} 万元减少到 {b:g} 万元，减幅 {ratio * 100:.0f}%（≥{T['capital_reduction_high_ratio'] * 100:.0f}% 判高）",
                              [line], "变更记录/动态"))
    # 2 法定代表人变更
    lr = [(d, line) for d, body, line in recent(T["legal_rep_window_months"]) if re.search(r"法定代表人从.+变更为", body)]
    if lr:
        flags.append(Flag("LEGAL_REP_CHANGE", "high" if len(lr) >= 2 else "medium", "法定代表人变更",
                          f"近 {T['legal_rep_window_months']} 个月内变更 {len(lr)} 次（1 次判中，≥2 次判高）",
                          [l for _, l in lr], "变更记录/动态"))
    # 3 股权变动（按不同日期计）
    eq = [(d, line) for d, body, line in recent(T["equity_window_months"]) if re.search(r"股东退出|股东新增|股份下降|股份上升", body)]
    eq_dates = sorted({d for d, _ in eq})
    if eq_dates:
        n = len(eq_dates)
        sev = "high" if n >= 5 else "medium" if n >= 2 else "low"
        flags.append(Flag("EQUITY_CHANGE", sev, "股权/股东变动",
                          f"近 {T['equity_window_months']} 个月内 {n} 个不同日期发生股东或持股比例变动（1 次低，≥2 次中，≥5 次高）",
                          [l for _, l in eq], "变更记录/动态"))
    # 4 涉诉
    filings = [(d, line) for d, body, line in recent(T["litigation_window_months"]) if "新增立案" in body]
    docs = [(d, line) for d, body, line in recent(T["litigation_window_months"]) if "新增裁判文书" in body]
    if filings or docs:
        sev = "high" if len(filings) >= T["litigation_high_count"] else "medium" if filings else "low"
        causes = sorted({c for _, l in filings + docs for c in re.findall(r"案由[：:]\s*([^\s，,；;]+)", l)})
        flags.append(Flag("LITIGATION", sev, "涉诉/司法案件",
                          f"近 {T['litigation_window_months']} 个月内新增立案 {len(filings)} 件、裁判文书 {len(docs)} 份"
                          + (f"，案由：{'、'.join(causes)}" if causes else "")
                          + f"（立案 ≥1 件判中，≥{T['litigation_high_count']} 件判高）",
                          [l for _, l in filings + docs], "司法案件/动态"))
    # 5 地址变更
    for d, body, line in recent(T["address_window_months"]):
        if "企业地址变更" in body:
            shell = any(k in body for k in ("秘书", "托管", "集中办公"))
            flags.append(Flag("ADDRESS_CHANGE", "low", "注册地址变更",
                              f"{d.isoformat()} 注册地址变更" + ("；原地址为秘书/托管类集中注册地址，需核实实际经营场所" if shell else ""),
                              [line], "变更记录/动态"))
    # 6 主要人员变动
    pers = [line for d, body, line in recent(T["personnel_window_months"]) if "职务变更" in body]
    if pers:
        flags.append(Flag("PERSONNEL_CHANGE", "low", "主要人员变动", f"近 {T['personnel_window_months']} 个月内职务变更 {len(pers)} 次", pers, "变更记录/动态"))
    # 7 登记状态
    status = f.get("登记状态", "")
    if status and not re.match(r"^(开业|存续|在业|在营)", status):
        flags.append(Flag("STATUS_ABNORMAL", "high", "登记状态异常", f"登记状态为「{status}」", [f"登记状态：{status}"], "工商信息"))
    # 8 风险统计
    for k, sev in STAT_SEVERITY.items():
        if stats.get(k, 0) > 0:
            flags.append(Flag("STAT_" + k, sev, k, f"{k} {stats[k]} 条", [f"{k}：{stats[k]}"], "风险统计"))
    # 9 成立时间
    est = _parse_date(f.get("成立日期", ""))
    if est:
        age = _months_between(est, as_of)
        if age < T["young_company_medium_months"]:
            flags.append(Flag("YOUNG_COMPANY", "medium", "成立时间短", f"成立仅 {age} 个月（<{T['young_company_medium_months']} 个月判中）", [f"成立日期：{est}"], "工商信息"))
        elif age < T["young_company_low_months"]:
            flags.append(Flag("YOUNG_COMPANY", "low", "成立时间较短", f"成立 {age} 个月（<{T['young_company_low_months']} 个月判低）", [f"成立日期：{est}"], "工商信息"))
    # 10 参保人数
    try:
        heads = int(re.sub(r"\D", "", f.get("参保人数", "")) or "-1")
    except ValueError:
        heads = -1
    if heads == 0:
        flags.append(Flag("HEADCOUNT", "medium", "参保人数为 0", "参保人数 0，可能为空壳或未登记社保，需核实", ["参保人数：0"], "工商信息"))
    elif 0 < heads <= T["low_headcount"]:
        flags.append(Flag("HEADCOUNT", "low", "参保人数较少", f"参保人数 {heads}（≤{T['low_headcount']} 判低）；对大额授信客户需核实经营规模", [f"参保人数：{heads}"], "工商信息"))
    # 11 实缴 < 注册
    reg, paid = _wan(f.get("注册资本", "")), _wan(f.get("实缴资本", ""))
    if reg and paid is not None and paid < reg:
        flags.append(Flag("PAIDIN_GAP", "low", "实缴资本低于注册资本", f"注册资本 {reg:g} 万元，实缴 {paid:g} 万元，实缴比例 {paid / reg * 100:.0f}%",
                          [f"注册资本：{f.get('注册资本')}", f"实缴资本：{f.get('实缴资本')}"], "工商信息"))

    flags.sort(key=lambda x: -SEV_ORDER[x.severity])
    top = max((SEV_ORDER[x.severity] for x in flags), default=-1)
    level = {3: "高", 2: "中", 1: "低", 0: "低", -1: "未发现风险信号"}[top]
    return {"company": name, "as_of": as_of.isoformat(), "level": level, "flags": flags, "fields": f}


def render(result: dict) -> str:
    fl = result["flags"]
    cnt = {s: sum(1 for x in fl if x.severity == s) for s in ("high", "medium", "low", "info")}
    out = [f"# 客商尽调：{result['company']}（快照日期 {result['as_of']}）",
           f"总体风险等级：{result['level']}（高 {cnt['high']} 项、中 {cnt['medium']} 项、低 {cnt['low']} 项）", ""]
    if not fl:
        out.append("快照范围内未发现命中规则的风险信号（不代表该公司没有风险）。")
    for x in fl:
        out.append(f"- [{SEV_CN[x.severity]}] {x.title}：{x.detail}")
        for e in x.evidence:
            out.append(f"    · 证据（{x.source}）：{e}")
    out += ["", "说明：结论仅基于所提供的工商快照，工具不联网；未覆盖招投标、舆情、财务报表、实际经营场所核查等维度；"
                "阈值为经验规则（见 THRESHOLDS），不构成法律意见，重要交易前应人工复核原始公示信息。"]
    return "\n".join(out)


def _load_snapshots(directory: Path) -> dict:
    snaps = {}
    for p in sorted(directory.glob("*.txt")):
        if p.name.lower().startswith("readme"):
            continue
        try:
            s = parse_snapshot(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        n = s["fields"].get("企业名称")
        if n:
            snaps[n] = s
    return snaps


def run_due_diligence(company_name: str, directory=None) -> str:
    directory = Path(directory or os.environ.get("ODR_DILIGENCE_DIR") or DEFAULT_DIR)
    q = (company_name or "").strip()
    if not q:
        return "Diligence Error: company_name is empty."
    if not directory.is_dir():
        return f"Diligence Error: snapshot directory not found at {directory}."
    snaps = _load_snapshots(directory)
    if q in snaps:
        hit = [q]
    else:
        hit = [n for n in snaps if q in n or n in q]
    if not hit:
        return (f"No registration snapshot found for '{q}'. Available snapshots: {', '.join(sorted(snaps)) or '(none)'}. "
                "Do not guess or invent registration data; tell the user that no data is available for this company.")
    if len(hit) > 1:
        return f"Diligence Error: '{q}' matches several snapshots: {', '.join(sorted(hit))}. Ask for the full company name."
    try:
        return render(analyze(snaps[hit[0]]))
    except ValueError as e:
        return f"Diligence Error: {e}"


@tool(description=DILIGENCE_DESCRIPTION)
async def counterparty_due_diligence(company_name: str) -> str:
    """Rule-based risk signals for a counterparty from its stored registration snapshot."""
    return await asyncio.to_thread(run_due_diligence, company_name)
