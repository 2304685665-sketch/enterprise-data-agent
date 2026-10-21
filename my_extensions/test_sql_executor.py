"""sql_executor 工具的单元测试 —— 用 assert 自动判断结果，不靠肉眼看输出。

2026-10-09 安全加固后新增的测试（见文件下半部分）验证：mode=ro 只读连接
是真正的安全边界、注释前缀不能绕过检查、WITH/CTE 被明确拒绝、数据库文件
缺失时不会被创建、超大结果集会被截断、复杂查询会被超时打断、并发只读查询
互不干扰。新增测试用的是临时数据库文件或对真实 demo.db 的只读查询，不会
修改 demo.db 本身的业务数据（已在每条测试里用 try/finally 还原被 monkeypatch
过的模块状态）。
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, 'src/open_deep_research')
import sql_executor as se  # noqa: E402
from sql_executor import sql_executor  # noqa: E402


async def test_error_on_wrong_table():
    """查一个不存在的表，应该拿到原始报错，而不是崩溃或者返回空结果。"""
    result = await sql_executor.ainvoke({'query': 'SELECT * FROM company'})
    assert "SQL Error" in result
    assert "no such table" in result
    print("✅ test_error_on_wrong_table 通过")


async def test_schema_discovery():
    """查 sqlite_master，应该能看到 companies 和 funding_rounds 这两张表。"""
    result = await sql_executor.ainvoke({
        'query': "SELECT name, sql FROM sqlite_master WHERE type='table'"
    })
    assert "companies" in result
    assert "funding_rounds" in result
    print("✅ test_schema_discovery 通过")


async def test_correct_join_query():
    """基于真实 schema 写出的连表查询，应该能正确返回融资金额前三的公司。"""
    result = await sql_executor.ainvoke({'query': '''
        SELECT c.name, f.round_type, f.amount_usd
        FROM companies c JOIN funding_rounds f ON c.id = f.company_id
        ORDER BY f.amount_usd DESC LIMIT 3
    '''})
    assert "SynthMotion" in result
    assert "80000000" in result
    print("✅ test_correct_join_query 通过")


async def test_write_operation_rejected():
    """故意传一条 DELETE 语句，应该被拒绝，数据不应该被真的删除。"""
    result = await sql_executor.ainvoke({'query': 'DELETE FROM companies WHERE id = 1'})
    assert "only SELECT" in result

    check = await sql_executor.ainvoke({'query': 'SELECT name FROM companies WHERE id = 1'})
    assert "RoboWorks" in check
    print("✅ test_write_operation_rejected 通过（DELETE 被拒绝，数据完好）")


async def test_pragma_now_rejected():
    """加固后追加：PRAGMA 语句应该被拒绝，因为部分 PRAGMA（如 user_version）
    能对数据库文件做持久性写入，不是真正的只读操作。"""
    result = await sql_executor.ainvoke({'query': "PRAGMA user_version = 999"})
    assert "SQL Error" in result
    assert "only SELECT" in result
    print("✅ test_pragma_now_rejected 通过（PRAGMA 被正确拒绝）")


async def test_comment_prefix_delete_blocked_and_data_intact():
    """注释前缀绕过尝试：开头塞一条无害的 `--` 注释，后面跟一条 DELETE。
    不管字符串分类这一层有没有拦住它（这里剥离注释后识别出真正的首词是
    delete，所以会被分类判断直接拒绝），最终都不能真的删除数据——
    用查完之后 RoboWorks 还在来确认这一点，而不是只看返回的错误文案。"""
    result = await sql_executor.ainvoke({
        'query': '-- harmless comment\nDELETE FROM companies WHERE id = 1'
    })
    assert "SQL Error" in result
    assert "RoboWorks" not in result  # 不应该把数据当成查询结果返回

    check = await sql_executor.ainvoke({'query': 'SELECT name FROM companies WHERE id = 1'})
    assert "RoboWorks" in check
    print("✅ test_comment_prefix_delete_blocked_and_data_intact 通过（注释前缀的DELETE被拒绝，数据完好）")


async def test_readonly_connection_is_the_real_backstop():
    """直接绕开字符串分类层，验证"数据库层面的只读保护"本身才是真正的安全
    边界，不是只依赖上面那层字符串检查。直接调用内部的同步执行函数
    `_run_query_sync`（跳过 sql_executor 里的分类判断），传一条 DELETE
    进去——即便分类判断完全不存在，mode=ro 连接也必须在这里报错，
    不能真的改动数据。"""
    result = await asyncio.to_thread(se._run_query_sync, 'DELETE FROM companies WHERE id = 1')
    assert "SQL Error" in result
    assert "readonly" in result.lower()

    check = await sql_executor.ainvoke({'query': 'SELECT name FROM companies WHERE id = 1'})
    assert "RoboWorks" in check
    print("✅ test_readonly_connection_is_the_real_backstop 通过（绕开字符串分类层，mode=ro仍然拦住了写操作）")


async def test_attach_blocked_at_db_layer():
    """真实漏洞回归测试（2026-10-09）：`mode=ro` 只保护主数据库连接本身，
    并不能阻止 `ATTACH DATABASE 'evil.db' AS e`——ATTACH 打开的是一个
    全新的、独立的数据库文件，不受主连接 mode=ro 这个 URI 参数的约束。
    这个漏洞最初是这样被发现的：绕开上层字符串分类器，直接在一个独立的
    临时目录里调用 `_run_query_sync("ATTACH DATABASE 'evil.db' AS e")`，
    结果返回"Query executed successfully"，且磁盘上真的创建出了
    evil.db——此前的审查只验证过"ATTACH 经过 sql_executor 的字符串分类器
    会被拒绝"，从未验证过绕开分类器之后数据库层本身是否真的扛得住，这是
    审查方法上的一个真实缺口，不是这次才存在的新漏洞。

    修复方式：`conn.set_authorizer(...)` 对 SQLITE_ATTACH/SQLITE_DETACH
    显式返回 SQLITE_DENY（见 `_deny_attach_and_detach`）。

    这条测试必须直接调用 `_run_query_sync`，不能只测上层 `sql_executor`
    ——否则只是又验证了一遍字符串分类器，无法证明数据库层的 authorizer
    真的生效。"""
    original_cwd = os.getcwd()
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)
    try:
        result = await asyncio.to_thread(
            se._run_query_sync, "ATTACH DATABASE 'evil.db' AS e"
        )
        assert result.startswith("SQL Error"), f"应该以 SQL Error 开头，实际: {result!r}"
        assert not os.path.exists(os.path.join(tmpdir, "evil.db")), (
            "ATTACH 不应该在磁盘上创建出 evil.db"
        )
        print(f"✅ test_attach_blocked_at_db_layer 通过（{result!r}，evil.db 未被创建）")
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_legit_cte_select_rejected_with_specific_message():
    """合法的 WITH...SELECT 目前被明确拒绝，且报错信息应该说明这是
    "暂不支持 CTE"，而不是泛泛的"只允许 SELECT"——这样模型才能理解
    "这条语法暂时不支持"和"这是写操作"是两件不同的事。"""
    result = await sql_executor.ainvoke({
        'query': 'WITH c AS (SELECT 1) SELECT * FROM c'
    })
    assert "SQL Error" in result
    assert "WITH" in result and "not currently supported" in result
    print("✅ test_legit_cte_select_rejected_with_specific_message 通过（合法CTE也被拒绝，且报错信息准确）")


async def test_with_prefixed_delete_rejected_before_reaching_db():
    """SQLite 允许 WITH 子句出现在 DELETE/INSERT/UPDATE 前面，不只是
    SELECT 前面（已验证：`WITH c AS (SELECT 1) DELETE FROM t` 在可写连接
    上真的会删除数据）。所以不能简单地把"以 WITH 开头"当成安全信号——
    这里验证这种伪装成 CTE 的写操作同样会被拒绝，且数据保持完好。"""
    result = await sql_executor.ainvoke({
        'query': 'WITH c AS (SELECT 1) DELETE FROM companies WHERE id = 1'
    })
    assert "SQL Error" in result
    assert "not currently supported" in result

    check = await sql_executor.ainvoke({'query': 'SELECT name FROM companies WHERE id = 1'})
    assert "RoboWorks" in check
    print("✅ test_with_prefixed_delete_rejected_before_reaching_db 通过（伪装成CTE的DELETE被拒绝，数据完好）")


async def test_missing_database_file_returns_clear_error_without_creating_one():
    """数据库文件不存在时，必须返回明确错误，且绝对不能创建一个新的空
    数据库文件。用系统临时目录下一个保证不存在的路径，monkeypatch
    sql_executor.DB_PATH 指向它——不碰真实的 demo.db。"""
    missing_path = Path(tempfile.gettempdir()) / "odr_sql_executor_missing_test.db"
    if missing_path.exists():
        missing_path.unlink()

    original_db_path = se.DB_PATH
    se.DB_PATH = missing_path
    try:
        result = await sql_executor.ainvoke({'query': 'SELECT 1'})
        assert "SQL Error" in result
        assert "not found" in result
        assert not missing_path.exists(), "不应该因为这次查询创建出一个新的数据库文件"
        print("✅ test_missing_database_file_returns_clear_error_without_creating_one 通过（明确报错，且没有创建新文件）")
    finally:
        se.DB_PATH = original_db_path
        if missing_path.exists():
            missing_path.unlink()


async def test_large_result_set_is_truncated_at_row_limit():
    """结果行数超过上限时，只应该返回规定数量的行，并明确提示已截断。
    用临时数据库文件生成 600 行测试数据（超过 MAX_ROWS=500），不碰真实
    demo.db。"""
    import sqlite3

    fd, tmp_path_str = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(tmp_path_str)  # 让 sqlite3 在这个路径上新建一个干净的文件
    tmp_path = Path(tmp_path_str)

    setup_conn = sqlite3.connect(tmp_path_str)
    setup_conn.execute("CREATE TABLE bigtable (x INTEGER)")
    setup_conn.executemany(
        "INSERT INTO bigtable VALUES (?)", [(i,) for i in range(600)]
    )
    setup_conn.commit()
    setup_conn.close()

    original_db_path = se.DB_PATH
    se.DB_PATH = tmp_path
    try:
        result = await sql_executor.ainvoke({'query': 'SELECT x FROM bigtable'})
        assert "truncated at 500 rows" in result
        lines = result.strip().split("\n")
        data_lines = [line for line in lines if line and not line.startswith("...") and line != "x"]
        assert len(data_lines) == se.MAX_ROWS, f"应该恰好返回 {se.MAX_ROWS} 行数据，实际 {len(data_lines)} 行"
        print(f"✅ test_large_result_set_is_truncated_at_row_limit 通过（600行数据被截断为{len(data_lines)}行，并有明确提示）")
    finally:
        se.DB_PATH = original_db_path
        tmp_path.unlink(missing_ok=True)


async def test_complex_query_times_out_with_clear_error():
    """复杂查询（14 张表自连接，实测在这台机器上原本需要约 4 秒）应该在
    超时阈值附近被打断，返回可识别的超时错误，而不是让查询无限跑下去。

    第二轮审查修正：之前这里是靠 monkeypatch 模块级的全局变量
    `se.QUERY_TIMEOUT_SECONDS` 来模拟短超时的，这是一种"共享可变状态"
    ——即使当前测试都是顺序执行、没有真的撞上并发问题，这个模式本身
    仍然是隐患（如果未来测试改成并发跑，或者生产代码其他地方也去读这个
    全局变量，就可能互相干扰）。现在改成直接调用内部函数
    `_run_query_sync` 并显式传入 `timeout_seconds=1` 参数，整个过程完全
    不触碰任何模块级/共享的可变状态，天然对并发安全。

    断言只检查"明显早于自然完成时间就返回了"和"报错信息里有timed out"，
    不依赖精确到毫秒的时间窗，避免测试脆弱。"""
    tables = ", ".join(["companies"] * 14)  # 4^14 ≈ 2.7亿行的笛卡尔积
    start = time.time()
    result = await asyncio.to_thread(se._run_query_sync, f'SELECT COUNT(*) FROM {tables}', 1)
    elapsed = time.time() - start
    assert "timed out" in result.lower()
    assert "SQL Error" in result
    assert elapsed < 3, f"应该远早于自然完成时间（约4秒）就被打断，实际耗时 {elapsed:.2f}s"
    print(f"✅ test_complex_query_times_out_with_clear_error 通过（{elapsed:.2f}s 内被打断，未触碰任何共享模块状态）")


async def test_normal_sql_error_not_misreported_as_timeout():
    """审查要点：确认普通 SQL 错误（比如语法错误）不会被误报成超时。
    一个打错列名的查询应该立刻报出真实的语法/列名错误，而不是被误判成
    "timed out"（`timed_out` 标志只应该在 progress handler 真正判断
    超过 deadline 时才被置位）。"""
    result = await sql_executor.ainvoke({'query': 'SELECT FRIM companies'})
    assert "SQL Error" in result
    assert "timed out" not in result.lower()
    assert "no such column" in result.lower() or "syntax error" in result.lower()
    print(f"✅ test_normal_sql_error_not_misreported_as_timeout 通过（真实报错：{result!r}，没有被误判成超时）")


async def test_multi_statement_stacking_rejected():
    """审查要点：多语句堆叠（用分号拼接一条合法SELECT和一条DELETE）不能
    被当成安全的单条SELECT执行。Python 的 sqlite3 模块本身在现代版本下
    会拒绝一次执行多条语句（已实测确认，会报
    "You can only execute one statement at a time."），这里验证这个
    防护在经过我们的分类判断之后依然生效，且数据保持完好。"""
    result = await sql_executor.ainvoke({
        'query': 'SELECT 1; DELETE FROM companies WHERE id = 1'
    })
    assert "SQL Error" in result

    check = await sql_executor.ainvoke({'query': 'SELECT name FROM companies WHERE id = 1'})
    assert "RoboWorks" in check
    print("✅ test_multi_statement_stacking_rejected 通过（分号堆叠的多语句被拒绝，数据完好）")


async def test_comment_prefixed_with_cte_rejected():
    """审查要点：WITH 前缀的检测也要能穿透开头的注释，不能被
    `-- 注释\\nWITH ...` 这种写法绕过。"""
    result = await sql_executor.ainvoke({
        'query': '-- sneaky comment\nWITH c AS (SELECT 1) DELETE FROM companies WHERE id = 1'
    })
    assert "SQL Error" in result
    assert "not currently supported" in result

    check = await sql_executor.ainvoke({'query': 'SELECT name FROM companies WHERE id = 1'})
    assert "RoboWorks" in check
    print("✅ test_comment_prefixed_with_cte_rejected 通过（注释前缀的WITH同样被识别并拒绝，数据完好）")


async def test_mixed_case_keywords_handled_correctly():
    """审查要点：大小写混写（WiTh / SeLeCt）不能绕过或误伤判断——
    WITH 前缀的大小写变体应该照样被拒绝，普通 SELECT 的大小写变体应该
    照样被允许。"""
    rejected = await sql_executor.ainvoke({
        'query': 'WiTh c AS (SeLeCt 1) SeLeCt * FROM c'
    })
    assert "SQL Error" in rejected
    assert "not currently supported" in rejected

    allowed = await sql_executor.ainvoke({'query': 'SeLeCt name FROM companies WHERE id = 1'})
    assert "RoboWorks" in allowed
    print("✅ test_mixed_case_keywords_handled_correctly 通过（大小写混写的WITH被拒绝，SELECT正常执行）")


async def test_exact_row_limit_boundary_not_off_by_one():
    """审查要点：确认返回行数的边界是精确的 MAX_ROWS，不是
    MAX_ROWS+1。用临时数据库分别构造"恰好500行"（不应截断）和
    "恰好501行"（应截断为500行）两种边界情况，而不是像之前那样只测
    明显超量（600行）的情况。"""
    import sqlite3

    fd, tmp_path_str = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(tmp_path_str)
    tmp_path = Path(tmp_path_str)

    setup_conn = sqlite3.connect(tmp_path_str)
    setup_conn.execute("CREATE TABLE t (x INTEGER)")
    setup_conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(501)])
    setup_conn.commit()
    setup_conn.close()

    original_db_path = se.DB_PATH
    se.DB_PATH = tmp_path
    try:
        # 恰好 500 行：不应该被截断
        exact = await sql_executor.ainvoke({'query': 'SELECT x FROM t WHERE x < 500'})
        assert "truncated" not in exact
        exact_lines = [l for l in exact.strip().split("\n") if l != "x"]
        assert len(exact_lines) == se.MAX_ROWS, f"恰好500行不应该被截断，实际返回{len(exact_lines)}行"

        # 恰好 501 行：应该截断为 500
        over = await sql_executor.ainvoke({'query': 'SELECT x FROM t'})
        assert "truncated at 500 rows" in over
        over_lines = [l for l in over.strip().split("\n") if l and not l.startswith("...") and l != "x"]
        assert len(over_lines) == se.MAX_ROWS, f"501行应该被截断为恰好500行，实际返回{len(over_lines)}行"
        print("✅ test_exact_row_limit_boundary_not_off_by_one 通过（500行不截断，501行精确截断为500行，没有差一错误）")
    finally:
        se.DB_PATH = original_db_path
        tmp_path.unlink(missing_ok=True)


async def test_concurrent_readonly_queries_do_not_interfere():
    """多个只读查询并发执行，验证各自独立的连接不会互相干扰或串结果。
    用 asyncio.gather 同时对真实 demo.db 发起 5 条不同的只读查询
    （只读查询，不修改任何数据），断言每条返回的结果跟预期的公司/数字
    一一对应。"""
    queries = [
        ("SELECT name FROM companies WHERE id = 1", "RoboWorks"),
        ("SELECT name FROM companies WHERE id = 2", "NeuralPath"),
        ("SELECT name FROM companies WHERE id = 3", "SynthMotion"),
        ("SELECT name FROM companies WHERE id = 4", "AtlasAI"),
        ("SELECT COUNT(*) FROM funding_rounds", "5"),
    ]

    async def _run(query):
        return await sql_executor.ainvoke({'query': query})

    results = await asyncio.gather(*[_run(q) for q, _ in queries])
    for (query, expected), result in zip(queries, results):
        assert expected in result, f"查询 {query!r} 应该返回包含 {expected!r} 的结果，实际是 {result!r}"
    print("✅ test_concurrent_readonly_queries_do_not_interfere 通过（5个并发只读查询结果各自正确，没有串数据）")


async def test_concurrent_calls_with_different_timeouts_do_not_interfere():
    """审查要点：确认每次调用的超时 deadline 是各自独立的局部状态，
    不会因为"共享可变状态"而互相干扰。同时并发跑两个 `_run_query_sync`
    调用——一个故意传 `timeout_seconds=1` 去跑那条14表自连接的慢查询
    （应该被打断），另一个用默认超时跑一条正常的快查询（应该正常成功，
    不应该被另一个调用的短超时连累）。"""
    tables = ", ".join(["companies"] * 14)

    async def _slow():
        return await asyncio.to_thread(se._run_query_sync, f"SELECT COUNT(*) FROM {tables}", 1)

    async def _fast():
        return await asyncio.to_thread(se._run_query_sync, "SELECT name FROM companies WHERE id = 1")

    slow_result, fast_result = await asyncio.gather(_slow(), _fast())
    assert "timed out" in slow_result.lower(), f"慢查询应该超时，实际: {slow_result!r}"
    assert "RoboWorks" in fast_result, f"快查询不应该被慢查询的短超时连累，实际: {fast_result!r}"
    print("✅ test_concurrent_calls_with_different_timeouts_do_not_interfere 通过（不同超时的并发调用互不干扰）")


async def main():
    await test_error_on_wrong_table()
    await test_schema_discovery()
    await test_correct_join_query()
    await test_write_operation_rejected()
    await test_pragma_now_rejected()
    await test_comment_prefix_delete_blocked_and_data_intact()
    await test_readonly_connection_is_the_real_backstop()
    await test_attach_blocked_at_db_layer()
    await test_legit_cte_select_rejected_with_specific_message()
    await test_with_prefixed_delete_rejected_before_reaching_db()
    await test_missing_database_file_returns_clear_error_without_creating_one()
    await test_large_result_set_is_truncated_at_row_limit()
    await test_complex_query_times_out_with_clear_error()
    await test_normal_sql_error_not_misreported_as_timeout()
    await test_multi_statement_stacking_rejected()
    await test_comment_prefixed_with_cte_rejected()
    await test_mixed_case_keywords_handled_correctly()
    await test_exact_row_limit_boundary_not_off_by_one()
    await test_concurrent_readonly_queries_do_not_interfere()
    await test_concurrent_calls_with_different_timeouts_do_not_interfere()
    print("\n全部测试通过！")


asyncio.run(main())
