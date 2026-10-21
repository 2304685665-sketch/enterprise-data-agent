import os, statistics, sqlite3, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_eval_cases_stats as S


class StatsCases(unittest.TestCase):
    def test_count_ids_unique_and_expected_positive(self):
        cs = S.build_stats_cases()
        self.assertEqual(len(cs), 12)
        self.assertEqual(len({c["id"] for c in cs}), 12)
        self.assertTrue(all(c["expected"]["kind"] == "money" and c["expected"]["value"] > 0 for c in cs))

    def test_pop_and_sample_stddev_are_distinguishable_under_tolerance(self):
        import agent_eval_score as v1
        c = sqlite3.connect(f"file:{S.STATS_DB}?mode=ro", uri=True)
        a = [r[0] for r in c.execute("SELECT amount_usd FROM orders")]
        p, s = statistics.pstdev(a), statistics.stdev(a)
        self.assertGreater(abs(p - s) / s, v1.REL_TOL)  # 总体 vs 样本不会被容差混为一谈

    def test_scorer_accepts_correct_and_rejects_wrong(self):
        import agent_eval_score as v1
        c = [x for x in S.build_stats_cases() if x["id"] == "S-pstdev-all"][0]
        v = c["expected"]["value"]
        self.assertTrue(v1.score_case(c, f"总体标准差约为 {v:,.2f} 美元。"))
        self.assertFalse(v1.score_case(c, f"总体标准差约为 {v*1.02:,.2f} 美元。"))


if __name__ == "__main__":
    unittest.main()
