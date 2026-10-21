"""mcp_server 的离线测试：不调用任何模型/Embedding API，不连外网。

覆盖：工具清单（含"不暴露 python_executor"）、经 MCP 协议调用 sql_executor
的行为（SELECT 成功、写入/ATTACH/多语句被拒绝、数据库未被改动）、
internal_doc_search 的转发与参数、真实 stdio 子进程冒烟、本机 streamable-http 冒烟。
"""

import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from open_deep_research import internal_doc_search as ids_mod  # noqa: E402
from open_deep_research import mcp_server  # noqa: E402
from open_deep_research import sql_executor as se  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEMO_DB = os.path.abspath(os.path.join(ROOT, "my_extensions", "demo.db"))


def _text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


async def _session():
    server = mcp_server.build_server()._mcp_server
    return create_connected_server_and_client_session(server)


async def test_tool_list_exposes_exactly_two_readonly_tools_and_not_python_executor():
    async with create_connected_server_and_client_session(mcp_server.build_server()._mcp_server) as client:
        tools = (await client.list_tools()).tools
    names = sorted(t.name for t in tools)
    assert names == ["internal_doc_search", "sql_executor"], names
    assert "python_executor" not in names, "任意代码执行不得经 MCP 暴露"
    by = {t.name: t for t in tools}
    assert by["sql_executor"].inputSchema["required"] == ["query"]
    assert by["internal_doc_search"].inputSchema["required"] == ["query"]
    assert "n_results" in by["internal_doc_search"].inputSchema["properties"]
    print("✅ test_tool_list_exposes_exactly_two_readonly_tools_and_not_python_executor 通过")


async def test_sql_select_succeeds_through_mcp_and_guards_still_apply():
    assert os.path.exists(DEMO_DB), "需要先运行 my_extensions/setup_demo_db.py"
    before = _sha(DEMO_DB)
    async with create_connected_server_and_client_session(mcp_server.build_server()._mcp_server) as client:
        ok = _text(await client.call_tool("sql_executor", {"query": "SELECT COUNT(*) AS n FROM companies"}))
        assert "n" in ok and "SQL Error" not in ok, ok
        for bad in (
            "DROP TABLE companies",
            "DELETE FROM companies",
            "ATTACH DATABASE '/tmp/mcp_evil.db' AS e",
            "SELECT 1; DROP TABLE companies",
        ):
            out = _text(await client.call_tool("sql_executor", {"query": bad}))
            # 多语句在 Python 3.10/3.11 上抛的是 sqlite3.Warning（sql_executor 只捕获
            # sqlite3.Error，由 FastMCP 兜底成 "Error executing tool ..."），3.12+ 才是
            # "SQL Error: ..."。这里只断言"被拒绝（返回错误文本）且数据库未被改动"，
            # 不把版本差异写死；该差异是 sql_executor 的已知问题，需单独修复。
            assert "Error" in out and "rows" not in out.lower(), (bad, out)
    assert _sha(DEMO_DB) == before, "经 MCP 的攻击语句不得改动数据库"
    assert not os.path.exists("/tmp/mcp_evil.db"), "ATTACH 不得创建文件"
    print("✅ test_sql_select_succeeds_through_mcp_and_guards_still_apply 通过（数据库 sha256 不变，ATTACH 未创建文件）")


async def test_internal_doc_search_forwards_arguments_and_returns_text():
    seen = {}

    async def fake_search(query, n_results):
        seen["args"] = (query, n_results)
        return "[Source: doc01_x, section: S, relevance_distance: 0.100]\nbody"

    with patch.object(ids_mod, "_search_internal_docs", fake_search):
        async with create_connected_server_and_client_session(mcp_server.build_server()._mcp_server) as client:
            out = _text(await client.call_tool("internal_doc_search", {"query": "融资", "n_results": 2}))
            default = _text(await client.call_tool("internal_doc_search", {"query": "x"}))
    assert seen["args"] == ("x", 3), "未传 n_results 时应使用默认值 3（最后一次调用）"
    assert out.startswith("[Source: doc01_x"), out
    assert default.startswith("[Source: doc01_x")
    print("✅ test_internal_doc_search_forwards_arguments_and_returns_text 通过")


async def test_internal_doc_search_error_is_returned_as_text_not_crash():
    async def boom(query, n_results):
        raise RuntimeError("chroma exploded")

    with patch.object(ids_mod, "_search_internal_docs", boom):
        async with create_connected_server_and_client_session(mcp_server.build_server()._mcp_server) as client:
            out = _text(await client.call_tool("internal_doc_search", {"query": "x"}))
    assert "Document search error" in out and "chroma exploded" in out, out
    print("✅ test_internal_doc_search_error_is_returned_as_text_not_crash 通过（沿用原工具的错误回传，不让服务端崩溃）")


async def test_real_stdio_subprocess_lists_tools():
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONDONTWRITEBYTECODE="1",
               OPENAI_API_KEY="sk-fake-test-key-no-real-network-calls")
    params = StdioServerParameters(command=sys.executable, args=["-m", "open_deep_research.mcp_server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), 20)
            names = sorted(t.name for t in (await asyncio.wait_for(s.list_tools(), 20)).tools)
            out = _text(await asyncio.wait_for(s.call_tool("sql_executor", {"query": "SELECT 1 AS one"}), 20))
    assert names == ["internal_doc_search", "sql_executor"], names
    assert "one" in out and "SQL Error" not in out, out
    print("✅ test_real_stdio_subprocess_lists_tools 通过（真实 stdio 子进程）")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_streamable_http_localhost_smoke():
    port = _free_port()
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), PYTHONDONTWRITEBYTECODE="1",
               OPENAI_API_KEY="sk-fake-test-key-no-real-network-calls")
    proc = subprocess.Popen(
        [sys.executable, "-m", "open_deep_research.mcp_server", "--transport", "streamable-http",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                await asyncio.sleep(0.3)
        else:
            raise AssertionError("streamable-http 服务未在 20 秒内监听")
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await asyncio.wait_for(s.initialize(), 20)
                names = sorted(t.name for t in (await asyncio.wait_for(s.list_tools(), 20)).tools)
        assert names == ["internal_doc_search", "sql_executor"], names
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("✅ test_streamable_http_localhost_smoke 通过（仅本机 127.0.0.1）")


async def main():
    await test_tool_list_exposes_exactly_two_readonly_tools_and_not_python_executor()
    await test_sql_select_succeeds_through_mcp_and_guards_still_apply()
    await test_internal_doc_search_forwards_arguments_and_returns_text()
    await test_internal_doc_search_error_is_returned_as_text_not_crash()
    await test_real_stdio_subprocess_lists_tools()
    await test_streamable_http_localhost_smoke()
    print("\n全部测试通过！")


if __name__ == "__main__":
    asyncio.run(main())
