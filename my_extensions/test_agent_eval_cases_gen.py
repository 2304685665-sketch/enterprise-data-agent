import os, sys, sqlite3, hashlib, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_eval_cases_gen as G
import agent_eval_score_v2 as V2

FROZEN_CASES_SHA = "e58cd4fb386b41c51b5111c81ae52c6c66af28a2f183625363353e4c2636664f"
FROZEN_DB_SHA = "e5c84ff434775d30a7282d5d76e1770edfd1d4306bc36a2cd00ea56611f675d5"


def test_db_and_cases_frozen():
    assert hashlib.sha256(open(G.GEN_DB, "rb").read()).hexdigest() == FROZEN_DB_SHA
    cs = G.build_gen_cases()
    assert hashlib.sha256(json.dumps(cs, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == FROZEN_CASES_SHA
    assert len(cs) == 40 and len({c["id"] for c in cs}) == 40


def test_schema_differs_from_main_db():
    c = sqlite3.connect(f"file:{G.GEN_DB}?mode=ro", uri=True)
    tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tabs == {"customers", "products", "orders"}


def test_expected_values_match_independent_sql():
    c = sqlite3.connect(f"file:{G.GEN_DB}?mode=ro", uri=True)
    by = {x["id"]: x["expected"]["value"] for x in G.build_gen_cases() if "value" in x["expected"]}
    assert by["G-count-orders"] == c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 79
    assert abs(by["G-total-all"] - c.execute("SELECT SUM(amount_usd) FROM orders").fetchone()[0]) < 1e-6
    assert by["G-zero-no-order"] == 3 and by["G-zero-cancelled"] == 0


def test_db_amounts_fallback_for_gen_db():
    a = V2.db_amounts(G.GEN_DB)
    assert len(a) == 79 and max(a) == 2580.28


def test_no_tied_entity_questions_present():
    ids = {c["id"] for c in G.build_gen_cases()}
    assert "G-cmp-latest-customer" not in ids and "G-cmp-most-orders" not in ids   # 并列题按约定不出
