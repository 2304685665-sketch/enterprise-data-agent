"""由 knowledge_base_v3 生成被投毒的副本 knowledge_base_poison/（确定性；不改动原目录）。
注入文字作为新的一段追加在对应文档最后一节的末尾（因此与该节同属一个 chunk）。
之后用 `python my_extensions/build_faiss_index.py --kb-dir my_extensions/knowledge_base_poison --out my_extensions/faiss_db_poison` 建索引。"""
import os, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from agent_eval_cases_inject import SPEC

SRC = os.path.join(HERE, "knowledge_base_v3"); DST = os.path.join(HERE, "knowledge_base_poison")


def build(dst=DST, src=SRC):
    if os.path.exists(dst): shutil.rmtree(dst)
    shutil.copytree(src, dst)
    for cid, doc, *_rest in SPEC:
        inj = _rest[3]
        p = os.path.join(dst, doc + ".md")
        t = open(p, encoding="utf-8").read().rstrip("\n")
        open(p, "w", encoding="utf-8").write(t + "\n\n" + inj + "\n")
    return dst


if __name__ == "__main__":
    print("已生成", build())
