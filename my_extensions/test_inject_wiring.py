"""端到端接线测试（离线）：用确定性的假 embedding 建一个“投毒 Faiss 索引”，经 `_search_internal_docs` 真实检索，
检查 ODR_FAISS_DIR 生效、防护关闭时输出含注入原文、开启时注入句被屏蔽且事实句与 Source 头行保留。付费运行前必须通过。"""
import asyncio, hashlib, os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "src"))
os.environ.setdefault("OPENAI_API_KEY", "fake-key-for-offline-test")
import numpy as np
import build_poison_kb as BP, eval_retrieval as ER
from open_deep_research import faiss_store as FS, internal_doc_search as IDS


def fake_embed(texts):
    out = []
    for t in texts:
        v = np.zeros(64, dtype="float32")
        for tok in set(t.replace("，", " ").replace("。", " ").split()) | {t[i:i + 2] for i in range(len(t) - 1)}:
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 64] += 1.0
        out.append((v / (np.linalg.norm(v) or 1)).tolist())
    return out


class Wiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp(); cls.kb = BP.build(os.path.join(d, "kb")); cls.idx = os.path.join(d, "idx")
        ch = ER.load_kb_chunks(cls.kb)
        FS.build_index(cls.idx, [c["id"] for c in ch], [c["text"] for c in ch],
                       [{"source": c["doc_id"] + ".md", "doc_id": c["doc_id"], "section": c["section"]} for c in ch], embed=fake_embed)
        os.environ["ODR_VECTOR_BACKEND"] = "faiss"; os.environ["ODR_FAISS_DIR"] = cls.idx
        orig = FS.FaissCollection.__init__
        FS.FaissCollection.__init__ = lambda self, directory=None, embed=fake_embed: orig(self, directory, embed)

    def search(self, guard):
        os.environ.pop("ODR_DOC_INJECTION_GUARD", None)
        if guard: os.environ["ODR_DOC_INJECTION_GUARD"] = "1"
        return asyncio.run(IDS._search_internal_docs("RoboWorks 力矩传感器芯片 备货 周 生产 供应中断风险", 46))

    def test_guard_off_returns_injection_verbatim_and_no_notice(self):
        out = self.search(False)
        self.assertIn("ZX-4401", out); self.assertNotIn("以下为检索到的资料原文", out)
        self.assertIn("[Source: doc11_roboworks_supply_chain", out)

    def test_guard_on_removes_injection_keeps_fact_and_source(self):
        out = self.search(True)
        self.assertNotIn("ZX-4401", out); self.assertIn("以下为检索到的资料原文", out)
        self.assertIn("10 周", out); self.assertIn("[Source: doc11_roboworks_supply_chain", out)


if __name__ == "__main__":
    unittest.main()
