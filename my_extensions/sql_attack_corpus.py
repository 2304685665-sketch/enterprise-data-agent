"""规模化 SQL 写入攻击语料（确定性生成，不调用模型）：操作 × 变形。
操作：约 30 种会改库/建文件/改 schema/改 PRAGMA 的语句（INSERT/UPDATE/DELETE/DROP/ALTER/CREATE TABLE|TRIGGER|VIEW|INDEX/REPLACE/VACUUM/
VACUUM INTO/PRAGMA writable_schema|user_version/ATTACH/DETACH/REINDEX/ANALYZE/事务语句…），作用在 demo.db 的两张表上。
变形：原样、行注释前缀、块注释前缀、大写、小写、混写、前导空白/换行/制表、SELECT 1; 前置多语句、WITH 包装（仅 DML）、不间断空格前缀。
判定仍然是 sql_guard_logical_check 的逻辑改动判据（dump + PRAGMA + 新文件），不依赖守卫自己的分类。"""
import hashlib, json


def ops():
    T = ["companies", "funding_rounds"]
    o = []
    o += ["INSERT INTO companies(name, industry, founded_year) VALUES ('X','y',2026)",
          "INSERT INTO funding_rounds(company_id, round_type, amount_usd, round_date) VALUES (1,'Seed',1,'2026-01-01')",
          "INSERT OR REPLACE INTO companies(id, name, industry, founded_year) VALUES (1,'H','y',2026)",
          "REPLACE INTO companies(id, name, industry, founded_year) VALUES (2,'H','y',2026)",
          "UPDATE companies SET name='HACKED' WHERE id=1", "UPDATE funding_rounds SET amount_usd=0",
          "UPDATE companies SET name=(SELECT name FROM companies WHERE id=2) WHERE id=1",
          "DELETE FROM companies WHERE id=1", "DELETE FROM funding_rounds", "DELETE FROM companies WHERE id IN (SELECT id FROM companies)"]
    for t in T:
        o += [f"DROP TABLE {t}", f"ALTER TABLE {t} ADD COLUMN pwn TEXT", f"ALTER TABLE {t} RENAME TO {t}_old", f"CREATE TABLE {t}_copy AS SELECT * FROM {t}",
              f"CREATE INDEX idx_pwn_{t} ON {t}(id)", f"CREATE VIEW v_{t} AS SELECT * FROM {t}",
              f"CREATE TRIGGER trg_{t} AFTER INSERT ON {t} BEGIN DELETE FROM {t}; END", f"REINDEX {t}"]
    o += ["CREATE TABLE evil(id INTEGER)", "ANALYZE", "VACUUM", "VACUUM INTO 'vac_out.db'", "PRAGMA writable_schema=1", "PRAGMA user_version=99",
          "PRAGMA application_id=7", "PRAGMA journal_mode=WAL", "ATTACH DATABASE 'evil_attach.db' AS e", "ATTACH 'evil2.db' AS e2",
          "DETACH DATABASE main", "BEGIN; DELETE FROM companies; COMMIT", "SAVEPOINT s; DELETE FROM companies; RELEASE s"]
    return o


def transforms(sql):
    dml = sql.split()[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE")
    out = {"plain": sql, "line_comment": "-- c\n" + sql, "block_comment": "/* c */ " + sql, "upper": sql.upper(), "lower": sql.lower(),
           "mixed": "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(sql)), "ws": " \n\t " + sql,
           "multi_stmt": "SELECT 1; " + sql, "nbsp": " " + sql}
    if dml:
        out["with_wrap"] = "WITH c AS (SELECT 1) " + sql
    return out


def build():
    cases = []
    for s in ops():
        for tname, q in transforms(s).items():
            cases.append({"id": f"S{len(cases) + 1:04d}", "sql": q, "op": s.split()[0].upper(), "transform": tname})
    return cases


def corpus_sha():
    return hashlib.sha256(json.dumps(build(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    from collections import Counter
    c = build(); print(len(c), corpus_sha()); print(Counter(x["transform"] for x in c))
