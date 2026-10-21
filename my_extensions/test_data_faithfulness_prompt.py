"""最终报告提示词必须包含数据忠实规则，且 .format 占位符仍然可用（不含多余花括号）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")

from open_deep_research import prompts  # noqa: E402


class TestDataFaithfulnessPrompt(unittest.TestCase):
    def test_rules_present(self):
        p = prompts.final_report_generation_prompt
        self.assertIn("<Data Faithfulness Rules>", p)
        for kw in ("not recorded in the database", "stand-in", "read-only access", "instead of asking the user for a list"):
            self.assertIn(kw, p)

    def test_format_still_works(self):
        out = prompts.final_report_generation_prompt.format(
            research_brief="b", messages="m", findings="f", date="2026-10-13")
        self.assertIn("2026-10-13", out)
        self.assertNotIn("{research_brief}", out)

    def test_other_prompts_untouched(self):
        for name in ("research_system_prompt", "lead_researcher_prompt", "compress_research_system_prompt"):
            self.assertNotIn("Data Faithfulness", getattr(prompts, name))


if __name__ == "__main__":
    unittest.main()
