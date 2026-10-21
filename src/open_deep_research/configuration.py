"""Configuration management for the Open Deep Research system."""

import os
from enum import Enum
from typing import Any, List, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field


class SearchAPI(Enum):
    """Enumeration of available search API providers."""
    
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    TAVILY = "tavily"
    NONE = "none"

class MCPConfig(BaseModel):
    """Configuration for Model Context Protocol (MCP) servers."""
    
    url: Optional[str] = Field(
        default=None,
        optional=True,
    )
    """The URL of the MCP server"""
    tools: Optional[List[str]] = Field(
        default=None,
        optional=True,
    )
    """The tools to make available to the LLM"""
    auth_required: Optional[bool] = Field(
        default=False,
        optional=True,
    )
    """Whether the MCP server requires authentication"""

class Configuration(BaseModel):
    """Main configuration class for the Deep Research agent."""
    
    # General Configuration
    max_structured_output_retries: int = Field(
        default=3,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 3,
                "min": 1,
                "max": 10,
                "description": "Maximum number of retries for structured output calls from models"
            }
        }
    )
    tool_retry_max_attempts: int = Field(
        default=2,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 2,
                "min": 0,
                "max": 5,
                "description": (
                    "工具执行被分类为瞬时性失败（TransientToolError 或已知网络异常类型）时，"
                    "最多重试几次（不含首次尝试）。这个重试只覆盖工具执行本身，和上面的 "
                    "max_structured_output_retries（模型生成/工具调用决策阶段的重试）是两件事，"
                    "不要混用。"
                )
            }
        }
    )
    tool_retry_base_delay_seconds: float = Field(
        default=0.5,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 0.5,
                "min": 0.1,
                "max": 5.0,
                "description": "工具重试的指数退避基础延迟（秒）：第 N 次重试等待 base * 2^(N-1)"
            }
        }
    )
    tool_call_timeout_seconds: float = Field(
        default=30.0,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 30.0,
                "min": 5.0,
                "max": 120.0,
                "description": (
                    "单次工具调用的外层统一超时（秒），覆盖的是 execute_tool_safely 整个调用"
                    "（包括它内部的重试循环和每次重试之间的退避等待），不是单次尝试的超时。"
                    "作为没有自带超时保护的工具的安全网。必须大于所有已知工具各自的内部超时+"
                    "清理耗时上限（python_executor 最坏情况约 15s 执行 + 8s 清理 ≈ 23s；"
                    "sql_executor 5s；internal_doc_search 15s），否则外层超时会在工具自己的"
                    "清理逻辑完成前就截断等待，参见 "
                    "test_outer_timeout_larger_than_inner_does_not_leak_container 的验证。"
                )
            }
        }
    )
    tool_circuit_breaker_threshold: int = Field(
        default=3,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 3,
                "min": 1,
                "max": 10,
                "description": (
                    "单个工具在同一次 researcher 子图运行内连续失败达到这个次数后，"
                    "本次运行内后续对该工具的调用直接返回'暂时不可用'，不再真正执行。"
                    "状态存在 ResearcherState.tool_failure_counts 里，随子图调用天然按运行隔离。"
                )
            }
        }
    )
    run_trace_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": (
                    "是否开启本地JSONL执行轨迹记录（B3新增，见run_trace.py）。默认关闭，"
                    "开启后还必须同时配置run_trace_dir才会真正写文件——只开这一个开关、"
                    "不配目录，视为未启用（记一条warning，不写任何文件）。"
                )
            }
        }
    )
    run_trace_dir: Optional[str] = Field(
        default=None,
        metadata={
            "x_oap_ui_config": {
                "type": "string",
                "default": None,
                "description": (
                    "run_trace_enabled=True时，JSONL轨迹文件的落盘目录。没有默认值——"
                    "必须显式配置，避免在没人注意的情况下悄悄写到某个固定路径。"
                )
            }
        }
    )
    loop_detection_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": (
                    "是否开启researcher工具调用层的循环检测（B1新增）。默认关闭，"
                    "不影响任何现有行为。开启后对重复调用/ABAB交替调用做拦截。"
                )
            }
        }
    )
    loop_detection_repeat_window: int = Field(
        default=6,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 6,
                "min": 2,
                "max": 20,
                "description": (
                    "重复检测的滑动窗口大小W：在最近W次模型发起的调用里统计同一规范化签名的"
                    "出现次数。注意：W必须>=loop_detection_repeat_threshold才有意义——如果"
                    "W小于阈值N，窗口里最多只能数到W次，永远达不到N，重复检测会永远不触发"
                    "（这里不引入配置校验器去阻止这种设置，只是如实在文档里说明，配置者需要"
                    "自己保证W>=N）。"
                )
            }
        }
    )
    loop_detection_repeat_threshold: int = Field(
        default=3,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 3,
                "min": 2,
                "max": 10,
                "description": (
                    "重复检测阈值N：同一规范化签名在窗口内第N次出现时，这次调用被拦截（即"
                    "允许执行N-1次）。注意：如果N大于loop_detection_repeat_window（窗口比"
                    "阈值还小），窗口里永远数不到N次，重复检测会永远不触发——这不是bug，是"
                    "这两个参数组合起来天然的数学结果，这里不引入校验器去阻止，只是文档说明。"
                )
            }
        }
    )
    loop_detection_abab_cycles: int = Field(
        default=2,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 2,
                "min": 2,
                "max": 6,
                "description": "ABAB交替检测的周期数M：最近2M次调用恰好构成A,B,A,B,...严格交替时，完成该模式的那次调用被拦截。"
            }
        }
    )
    loop_detection_force_stop_after_hits: int = Field(
        default=3,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 3,
                "min": 1,
                "max": 10,
                "description": "连续命中K轮循环检测后（模型对提示没有反应），强制结束该researcher进入压缩。"
            }
        }
    )
    allow_clarification: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether to allow the researcher to ask the user clarifying questions before starting research"
            }
        }
    )
    max_concurrent_research_units: int = Field(
        default=5,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 5,
                "min": 1,
                "max": 20,
                "step": 1,
                "description": "Maximum number of research units to run concurrently. This will allow the researcher to use multiple sub-agents to conduct research. Note: with more concurrency, you may run into rate limits."
            }
        }
    )
    # Research Configuration
    search_api: SearchAPI = Field(
        default=SearchAPI.TAVILY,
        metadata={
            "x_oap_ui_config": {
                "type": "select",
                "default": "tavily",
                "description": "Search API to use for research. NOTE: Make sure your Researcher Model supports the selected search API.",
                "options": [
                    {"label": "Tavily", "value": SearchAPI.TAVILY.value},
                    {"label": "OpenAI Native Web Search", "value": SearchAPI.OPENAI.value},
                    {"label": "Anthropic Native Web Search", "value": SearchAPI.ANTHROPIC.value},
                    {"label": "None", "value": SearchAPI.NONE.value}
                ]
            }
        }
    )
    max_researcher_iterations: int = Field(
        default=6,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 6,
                "min": 1,
                "max": 10,
                "step": 1,
                "description": "Maximum number of research iterations for the Research Supervisor. This is the number of times the Research Supervisor will reflect on the research and ask follow-up questions."
            }
        }
    )
    max_react_tool_calls: int = Field(
        default=10,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 10,
                "min": 1,
                "max": 30,
                "step": 1,
                "description": "Maximum number of tool calling iterations to make in a single researcher step."
            }
        }
    )
    # Model Configuration
    summarization_model: str = Field(
        default="openai:gpt-4.1-mini",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1-mini",
                "description": "Model for summarizing research results from Tavily search results"
            }
        }
    )
    summarization_model_max_tokens: int = Field(
        default=8192,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 8192,
                "description": "Maximum output tokens for summarization model"
            }
        }
    )
    max_content_length: int = Field(
        default=50000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 50000,
                "min": 1000,
                "max": 200000,
                "description": "Maximum character length for webpage content before summarization"
            }
        }
    )
    research_model: str = Field(
        default="openai:gpt-4.1",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1",
                "description": "Model for conducting research. NOTE: Make sure your Researcher Model supports the selected search API."
            }
        }
    )
    research_model_fallback: Optional[str] = Field(
        default=None,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": (
                    "备用研究模型（可选，默认不启用）。主模型在连接失败、超时、限流或服务端 5xx "
                    "（且 with_retry 重试耗尽）后才会改用它；认证失败、参数错误等请求类错误不会降级。"
                )
            }
        }
    )
    human_approval_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": (
                    "是否对高风险工具调用启用人工审批（python_executor 的代码、引用 >=2 张表的 SQL）。"
                    "需要先用 approval.set_broker() 设置审批代理；启用但没有代理时，需审批的调用一律不执行。"
                )
            }
        }
    )
    human_approval_timeout_seconds: int = Field(
        default=300,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 300,
                "min": 5,
                "max": 3600,
                "description": "等待人工审批的最长时间（秒）；超时按未批准处理，该次调用不执行。"
            }
        }
    )
    research_model_max_tokens: int = Field(
        default=10000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 10000,
                "description": "Maximum output tokens for research model"
            }
        }
    )
    compression_model: str = Field(
        default="openai:gpt-4.1",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1",
                "description": "Model for compressing research findings from sub-agents. NOTE: Make sure your Compression Model supports the selected search API."
            }
        }
    )
    compression_model_max_tokens: int = Field(
        default=8192,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 8192,
                "description": "Maximum output tokens for compression model"
            }
        }
    )
    final_report_model: str = Field(
        default="openai:gpt-4.1",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1",
                "description": "Model for writing the final report from all research findings"
            }
        }
    )
    final_report_model_max_tokens: int = Field(
        default=10000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 10000,
                "description": "Maximum output tokens for final report model"
            }
        }
    )
    # MCP server configuration
    mcp_config: Optional[MCPConfig] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "mcp",
                "description": "MCP server configuration"
            }
        }
    )
    mcp_prompt: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Any additional instructions to pass along to the Agent regarding the MCP tools that are available to it."
            }
        }
    )


    @classmethod
    def from_runnable_config(
        cls, config: Optional[RunnableConfig] = None
    ) -> "Configuration":
        """Create a Configuration instance from a RunnableConfig."""
        configurable = config.get("configurable", {}) if config else {}
        field_names = list(cls.model_fields.keys())
        values: dict[str, Any] = {
            field_name: os.environ.get(field_name.upper(), configurable.get(field_name))
            for field_name in field_names
        }
        return cls(**{k: v for k, v in values.items() if v is not None})

    class Config:
        """Pydantic configuration."""
        
        arbitrary_types_allowed = True