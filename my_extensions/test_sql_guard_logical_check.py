"""逻辑变更判据自身的测试：用无防护基线（无需 langchain）验证判据能区分“真改动”与“无改动”。"""
import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import sql_guard_logical_check as L

NAIVE = os.path.join(HERE, "naive_baseline", "src")


def case(sql, cid="T"):
    return {"id": cid, "sql": sql}


class Logical(unittest.TestCase):
    def test_write_is_detected(self):
        for sql in ("DELETE FROM companies WHERE id = 1", "DROP TABLE companies", "CREATE TABLE zz(x)", "PRAGMA user_version=77",
                    "ATTACH DATABASE 'new_evil.db' AS e"):
            self.assertTrue(L.run_case(NAIVE, case(sql))["logical_change"], sql)

    def test_read_only_select_is_not_a_change(self):
        r = L.run_case(NAIVE, case("SELECT COUNT(*) FROM companies"))
        self.assertFalse(r["logical_change"])
        self.assertIsNone(r["infra_failure"])

    def test_noop_write_is_not_a_change(self):
        # 更新成相同的值：逻辑上没有变化（文件哈希可能变，但判据看内容）
        r = L.run_case(NAIVE, case("UPDATE companies SET name = name WHERE id = 1"))
        self.assertFalse(r["logical_change"])


if __name__ == "__main__":
    unittest.main()
