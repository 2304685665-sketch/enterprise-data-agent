"""mcp_server.py —— 把企业内部工具以 MCP（Model Context Protocol）服务形式对外暴露。

注意方向：上游 open_deep_research 已经有 MCP *客户端*能力（可以连接外部 MCP
服务器取工具）；本文件是 MCP *服务端*——把本项目已有的两个只读工具
（sql_executor、internal_doc_search）封装成标准 MCP 工具，供任何 MCP 客户端调用。

暴露哪些、不暴露哪些（刻意的安全取舍）：
- 暴露 sql_executor：它本身只读（mode=ro + query_only + authorizer 拒绝 ATTACH，
  仅允许 SELECT），经 MCP 暴露不扩大权限面。
- 暴露 internal_doc_search：只读检索。
- **不暴露 python_executor**：它执行任意代码（虽有 Docker 隔离），把任意代码
  执行通过网络协议暴露给任意客户端，风险面与另外两个只读工具不在一个量级，
  需要单独的鉴权/配额设计之后才应考虑。
- 本服务本身不做鉴权与限流：默认只用 stdio（本机子进程）；使用 streamable-http
  时默认只绑定 127.0.0.1，不要直接暴露到公网。

工具实现是对现有 LangChain 工具的薄封装（ainvoke），不复制业务逻辑，所以
所有已有的防护、超时、错误分类行为都原样保留。

用法：
  python -m open_deep_research.mcp_server                      # stdio
  python -m open_deep_research.mcp_server --transport streamable-http --port 8765
"""

import argparse

from mcp.server.fastmcp import FastMCP

from open_deep_research.internal_doc_search import internal_doc_search as _internal_doc_search_tool
from open_deep_research.sql_executor import sql_executor as _sql_executor_tool

SERVER_NAME = "odr-enterprise-tools"


def build_server(host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    server = FastMCP(SERVER_NAME, host=host, port=port)

    @server.tool(
        name="sql_executor",
        description=(
            "Run a single read-only SELECT query against the demo SQLite database. "
            "Write statements, ATTACH/DETACH and multi-statement input are rejected. "
            "Errors are returned as text starting with 'SQL Error:'."
        ),
    )
    async def sql_executor(query: str) -> str:
        return await _sql_executor_tool.ainvoke({"query": query})

    @server.tool(
        name="internal_doc_search",
        description=(
            "Search the internal document knowledge base and return the top matching "
            "snippets, each prefixed by a '[Source: <doc_id>, section: ..., "
            "relevance_distance: ...]' header."
        ),
    )
    async def internal_doc_search(query: str, n_results: int = 3) -> str:
        return await _internal_doc_search_tool.ainvoke({"query": query, "n_results": n_results})

    return server


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    build_server(host=args.host, port=args.port).run(transport=args.transport)


if __name__ == "__main__":
    main()
