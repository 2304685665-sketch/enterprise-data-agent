"""ODR_SQL_SCHEMA_HINT：关闭时与旧行为逐字一致；开启时工具说明含实时读到的建表语句，且研究员提示词不再要求先查 sqlite_master。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")

from open_deep_research import prompts, sql_executor as se  # noqa: E402


class SchemaHint(unittest.TestCase):
    def test_off_is_old_text(self):
        d = se.build_sql_executor_description(False)
        self.assertIn("You do NOT know the schema in advance", d)
        self.assertNotIn("CREATE TABLE", d)

    def test_on_contains_live_schema(self):
        for db in (os.path.join(HERE, "demo.db"), os.path.join(HERE, "eval_large.db")):
            d = se.build_sql_executor_description(True, db)
            self.assertIn("CREATE TABLE companies", d)
            self.assertIn("funding_rounds", d)
            self.assertNotIn("You do NOT know the schema in advance", d)

    def test_missing_db_falls_back(self):
        d = se.build_sql_executor_description(True, "/nonexistent/x.db")
        self.assertIn("You do NOT know the schema in advance", d)

    def test_prompt_replacement(self):
        base = prompts.research_system_prompt
        on = prompts.apply_sql_schema_hint(base, True)
        self.assertNotIn("You do not know the schema in advance", on)
        self.assertIn("schema is given in the tool description", on)
        self.assertEqual(prompts.apply_sql_schema_hint(base, False), base)


if __name__ == "__main__":
    unittest.main()
