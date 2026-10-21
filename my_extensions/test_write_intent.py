"""写请求识别：在三套评测问句上量化精确率/召回率（离线、不调模型），并检查误触发对照。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from open_deep_research.write_intent import build_write_request_hint, detect_write_request  # noqa: E402
import agent_eval_cases as ac  # noqa: E402
import agent_eval_cases_heldout as h  # noqa: E402


class _Msg:
    def __init__(self, t, c):
        self.type, self.content = t, c


class TestWriteIntent(unittest.TestCase):
    def _split(self, cases):
        pos = {c["id"] for c in cases if c["expected"]["kind"] == "refuse_write"}
        hit = {c["id"] for c in cases if detect_write_request(c["question"])}
        return pos, hit

    def test_exact_on_all_suites(self):
        for name, cases in (("main", ac.build_cases()), ("heldout1", h.build_heldout_cases()), ("heldout2", h.build_heldout2_cases())):
            pos, hit = self._split(cases)
            self.assertEqual(hit - pos, set(), f"{name} 误触发: {sorted(hit - pos)}")
            self.assertEqual(pos - hit, set(), f"{name} 漏检: {sorted(pos - hit)}")

    def test_confusable_reads_not_triggered(self):
        for q in ("2026 年新增了几笔融资？", "最近一次更新的融资记录是哪家公司的？", "How many rounds were added in 2026?",
                  "What are the latest updates on RoboWorks?", "数据库里一共有几家公司？"):
            self.assertFalse(detect_write_request(q), q)

    def test_hint_only_for_human_messages_and_switch(self):
        msgs = [_Msg("human", "请把X的金额改成1亿"), _Msg("ai", "好的，已经删除了")]
        self.assertIn("DATA-MODIFICATION", build_write_request_hint(msgs))
        self.assertEqual(build_write_request_hint([_Msg("ai", "请删除记录")]), "")
        os.environ["ODR_WRITE_REQUEST_HINT"] = "0"
        try:
            self.assertEqual(build_write_request_hint(msgs), "")
        finally:
            del os.environ["ODR_WRITE_REQUEST_HINT"]

    def test_hint_versions(self):
        from open_deep_research.write_intent import HINT_V1, HINT_V2
        msgs = [_Msg("human", "请把X的金额改成1亿")]
        self.assertTrue(HINT_V2.startswith(HINT_V1) and len(HINT_V2) > len(HINT_V1))
        self.assertEqual(build_write_request_hint(msgs), HINT_V1)  # 默认 V1
        os.environ["ODR_WRITE_HINT_VERSION"] = "2"
        try:
            self.assertEqual(build_write_request_hint(msgs), HINT_V2)
        finally:
            del os.environ["ODR_WRITE_HINT_VERSION"]


if __name__ == "__main__":
    unittest.main()
