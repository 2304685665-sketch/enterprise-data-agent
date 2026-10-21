"""counterparty_due_diligence：预期值按规则手工推导（不是拿工具输出反推）。
用法：PYTHONPATH=src python my_extensions/test_due_diligence.py"""
import sys, tempfile
from pathlib import Path

from open_deep_research import due_diligence as dd

DATA = Path(__file__).resolve().parent / "diligence_data"


def ids(res):
    return {f.id for f in res["flags"]}


def sev(res, fid):
    return next(f.severity for f in res["flags"] if f.id == fid)


def load(name):
    return dd.parse_snapshot((DATA / name).read_text(encoding="utf-8"))


def base(**over):
    """最小干净快照文本，按需覆盖字段 / 追加动态。"""
    head = {"取数日期": "2026-10-20", "企业名称": "测试有限公司", "登记状态": "开业（存续）", "成立日期": "2010-01-01",
            "注册资本": "100万元", "实缴资本": "100万元", "参保人数": "50"}
    events = over.pop("events", [])
    stats = over.pop("stats", {})
    head.update(over)
    lines = [f"{k}：{v}" for k, v in head.items()] + ["## 风险统计"] + [f"{k}：{v}" for k, v in stats.items()] + ["## 动态"] + events
    return dd.parse_snapshot("\n".join(lines))



def test_sample_b_c():
    b = dd.analyze(load("sample_b_synthetic_risky.txt"))
    assert {"STAT_失信被执行人", "STAT_被执行人", "STAT_限制高消费"} <= ids(b) and sev(b, "STAT_经营异常") == "medium"
    assert sev(b, "LEGAL_REP_CHANGE") == "high" and sev(b, "LITIGATION") == "high"      # 2 次法人变更；4 件立案
    assert sev(b, "CAPITAL_REDUCTION") == "high"                                         # 恰好 50% → 高（边界）
    assert sev(b, "YOUNG_COMPANY") == "medium" and sev(b, "HEADCOUNT") == "medium"       # 成立 8 个月；参保 0
    assert b["level"] == "高"
    c = dd.analyze(load("sample_c_synthetic_clean.txt"))
    assert c["flags"] == [] and c["level"] == "未发现风险信号", c["flags"]               # 2018 增资、2015 新增股东均超窗
    assert "不代表该公司没有风险" in dd.render(c)
    print("✅ 样例 B（多项高风险）与样例 C（无信号，且不说\"没有风险\"）符合预期")


def test_thresholds_boundaries():
    def red(a, b, date="2024-05-24"):
        return dd.analyze(base(events=[f"{date} 注册资本减少：从{a}万元减少到{b}万元"]))
    assert sev(red(100, 51), "CAPITAL_REDUCTION") == "medium"        # 49%
    assert sev(red(100, 50), "CAPITAL_REDUCTION") == "high"          # 50%
    assert "CAPITAL_REDUCTION" in ids(red(100, 10, "2021-10-20"))    # 恰 60 个月，含
    assert "CAPITAL_REDUCTION" in ids(red(100, 10, "2021-10-19"))      # 60 个月零 1 天，按整月数仍是 60，含
    assert "CAPITAL_REDUCTION" not in ids(red(100, 10, "2021-09-20"))  # 恰 61 个月，不含
    lr = lambda d: dd.analyze(base(events=[f"{d} 法定代表人从“甲”变更为“乙”"]))
    assert "LEGAL_REP_CHANGE" in ids(lr("2024-10-20")) and "LEGAL_REP_CHANGE" in ids(lr("2024-10-19"))   # 24 个月 / 24 个月零 1 天
    assert "LEGAL_REP_CHANGE" not in ids(lr("2024-09-20"))                                                # 恰 25 个月
    r5 = dd.analyze(base(events=[f"2025-0{i}-10 股东新增：某" for i in range(1, 6)]))
    r1 = dd.analyze(base(events=["2025-01-10 股东新增：某", "2025-01-10 股东退出：某"]))   # 同一天只算 1 个日期
    assert sev(r5, "EQUITY_CHANGE") == "high" and sev(r1, "EQUITY_CHANGE") == "low"
    four = dd.analyze(base(events=[f"2025-0{i}-10 新增立案，案由：买卖合同纠纷" for i in range(1, 5)]))
    three = dd.analyze(base(events=[f"2025-0{i}-10 新增立案，案由：买卖合同纠纷" for i in range(1, 4)]))
    only_doc = dd.analyze(base(events=["2025-01-10 新增裁判文书，案由：买卖合同纠纷"]))
    assert sev(four, "LITIGATION") == "high" and sev(three, "LITIGATION") == "medium" and sev(only_doc, "LITIGATION") == "low"
    print("✅ 减资 49%/50%、窗口 60/61 个月（整月数口径）、法人 24 个月、股权同日合并、立案 3/4 件的边界均正确")


def test_other_rules():
    assert sev(dd.analyze(base(登记状态="吊销，未注销")), "STATUS_ABNORMAL") == "high"
    assert sev(dd.analyze(base(登记状态="注销")), "STATUS_ABNORMAL") == "high"
    assert "STATUS_ABNORMAL" not in ids(dd.analyze(base(登记状态="在业")))
    assert sev(dd.analyze(base(成立日期="2026-04-21")), "YOUNG_COMPANY") == "medium"   # 5 个月
    assert sev(dd.analyze(base(成立日期="2024-10-21")), "YOUNG_COMPANY") == "low"      # 23 个月
    assert "YOUNG_COMPANY" not in ids(dd.analyze(base(成立日期="2023-10-20")))         # 恰 36 个月，不判
    assert dd.analyze(base(参保人数="10"))["flags"][0].id == "HEADCOUNT"
    assert "HEADCOUNT" not in ids(dd.analyze(base(参保人数="11")))
    s = dd.analyze(base(stats={"失信被执行人": 0, "经营异常": 2, "行政处罚": 0}))
    assert ids(s) == {"STAT_经营异常"} and s["level"] == "中"
    sh = dd.analyze(base(events=["2025-06-01 企业地址变更：从“北京市海淀区某路1号”变更为“北京市朝阳区某路2号”"]))
    assert "秘书" not in dd.render(sh) and sev(sh, "ADDRESS_CHANGE") == "low"
    print("✅ 状态异常、成立时间、参保人数、风险统计、普通地址变更规则正确")


def test_errors_and_lookup():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "a.txt").write_text("取数日期：2026-10-20\n企业名称：甲贸易有限公司\n", encoding="utf-8")
        (d / "b.txt").write_text("取数日期：2026-10-20\n企业名称：甲矿业有限公司\n", encoding="utf-8")
        (d / "bad.txt").write_text("企业名称：丙有限公司\n", encoding="utf-8")                       # 缺取数日期
        assert "matches several" in dd.run_due_diligence("甲", d)
        assert "No registration snapshot found" in dd.run_due_diligence("丁", d) and "invent" in dd.run_due_diligence("丁", d)
        assert "Diligence Error" in dd.run_due_diligence("丙有限公司", d) and "取数日期" in dd.run_due_diligence("丙有限公司", d)
        assert "总体风险等级" in dd.run_due_diligence("甲贸易有限公司", d)                              # 精确匹配优先
    assert dd.run_due_diligence("  ", DATA).startswith("Diligence Error")
    assert dd.run_due_diligence("x", DATA / "nope").startswith("Diligence Error")
    print("✅ 名称不唯一 / 无快照 / 缺关键字段 / 空名 / 目录缺失均返回明确信息，不猜")


def test_tool_wrapper_and_default_off():
    import asyncio, importlib, os
    out = asyncio.run(dd.counterparty_due_diligence.ainvoke({"company_name": "示例丙"}))
    assert "未发现风险信号" in out
    os.environ.pop("ODR_DILIGENCE", None)
    from open_deep_research import utils
    import inspect
    assert 'os.environ.get("ODR_DILIGENCE") == "1"' in inspect.getsource(utils.get_all_tools)
    print("✅ 工具封装可调用；注册受 ODR_DILIGENCE=1 控制（默认关闭）")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL PASSED")
