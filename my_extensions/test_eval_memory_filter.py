"""存量记忆过滤审计的离线回归测试（冻结语料 + 固定数字）。
dev 语料用于发现问题和修复；v2 留出集在修复前冻结，只用来检验泛化。数字变动 = 防护或语料被改了，必须先解释。"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "memory_filter_audit")
sys.path.insert(0, AUDIT)
SRC_GUARD = os.path.join(HERE, "..", "src", "open_deep_research", "memory_guard.py")
BEFORE_GUARD = os.path.join(AUDIT, "memory_guard_before.py")

import eval_memory_filter as ev  # noqa: E402
from secret_split import restore  # noqa: E402

DEV, HELD = "memory_filter_corpus.json", "memory_filter_corpus_v2_heldout.json"
DEV_SHA_PREFIX, HELD_SHA = "ac35bc24b23b8d09", "72d23802be2fb5b6c787d2165fa4e8d19c944ecc05ae0c337bdf9e3bf8f6c847"


def run(corpus, guard_file, label):
    ev.CORPUS = os.path.join(AUDIT, corpus)
    return ev.run(guard_file, label)


def test_corpora_are_frozen():
    assert hashlib.sha256(restore(open(os.path.join(AUDIT, DEV), "rb").read())).hexdigest().startswith(DEV_SHA_PREFIX)
    assert hashlib.sha256(restore(open(os.path.join(AUDIT, HELD), "rb").read())).hexdigest() == HELD_SHA
    for c in (DEV, HELD):  # 提交的文件里不应再出现完整的密钥样式字符串
        raw = open(os.path.join(AUDIT, c), encoding="utf-8").read()
        import re
        assert not re.search(r"xox[abprs]-|sk_live_|ghp_[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20}", raw), c
    print("✅ test_corpora_are_frozen 通过")


def test_no_filter_baseline_passes_everything():
    for c in (DEV, HELD):
        r = run(c, "none", "none")
        assert r["malicious_ignored"] == 0 and r["benign_dropped"] == 0
    print("✅ test_no_filter_baseline_passes_everything 通过")


def test_dev_set_before_and_after():
    b, a = run(DEV, BEFORE_GUARD, "before"), run(DEV, SRC_GUARD, "after")
    assert (b["malicious_ignored"], b["malicious_total"], b["benign_dropped"]) == (25, 50, 0)
    assert (a["malicious_ignored"], a["malicious_total"], a["benign_dropped"], a["benign_total"]) == (50, 50, 0, 70)
    assert a["excluded"] == {"M45": "合成银行卡号未通过 Luhn 校验（语料构造缺陷），不应计为应拦截项"}
    print("✅ test_dev_set_before_and_after 通过（dev 集参与了修复，50/50 不代表泛化）")


def test_heldout_before_and_after():
    b, a = run(HELD, BEFORE_GUARD, "before"), run(HELD, SRC_GUARD, "after")
    assert (b["malicious_ignored"], b["malicious_total"], b["benign_dropped"]) == (7, 44, 1)
    assert (a["malicious_ignored"], a["malicious_total"], a["benign_dropped"], a["benign_total"]) == (43, 44, 1, 41)
    assert a["missed_malicious"] == ["M14"] and a["dropped_benign"] == ["B32"]  # 漏拦：长距离的“不必理会…要求”；误拦：16 位订单号恰好通过 Luhn
    print("✅ test_heldout_before_and_after 通过")


if __name__ == "__main__":
    test_corpora_are_frozen(); test_no_filter_baseline_passes_everything(); test_dev_set_before_and_after(); test_heldout_before_and_after()
    print("全部通过")
