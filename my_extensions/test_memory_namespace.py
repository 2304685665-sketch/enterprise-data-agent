"""记忆命名空间：图从 config 取 user_id（评测每个用例独立），没有时退回 DEMO_USER_ID；不同用例互不可见（用假后端验证命名空间键）。"""
import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")
from open_deep_research import deep_researcher as dr
from open_deep_research.memory_manager import DEMO_USER_ID
import agent_eval_run as run


class Namespace(unittest.TestCase):
    def test_default_is_demo_user_when_absent(self):
        for cfg in (None, {}, {"configurable": {}}, {"configurable": {"user_id": ""}}, {"configurable": {"user_id": 5}}):
            self.assertEqual(dr._memory_user_id(cfg), DEMO_USER_ID)

    def test_config_user_id_wins(self):
        self.assertEqual(dr._memory_user_id({"configurable": {"user_id": "alice"}}), "alice")

    def test_eval_gives_each_case_its_own_namespace(self):
        a = run.build_config("L-x-aaaa", "/tmp/t", [])["configurable"]["user_id"]
        b = run.build_config("L-x-bbbb", "/tmp/t", [])["configurable"]["user_id"]
        self.assertNotEqual(a, b); self.assertTrue(a.startswith("eval-"))

    def test_fake_store_keyed_by_user_id_is_isolated(self):
        store = {}
        def save(text, uid): store.setdefault(uid, []).append(text)
        def search(uid): return list(store.get(uid, []))
        save("偏好：用万美元", dr._memory_user_id({"configurable": {"user_id": "eval-1"}}))
        self.assertEqual(search(dr._memory_user_id({"configurable": {"user_id": "eval-2"}})), [])
        self.assertEqual(search(dr._memory_user_id({"configurable": {"user_id": "eval-1"}})), ["偏好：用万美元"])


if __name__ == "__main__":
    unittest.main()
