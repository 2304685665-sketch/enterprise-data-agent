"""Main LangGraph implementation for the Deep Research agent."""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from langchain.chat_models import init_chat_model
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    filter_messages,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from open_deep_research import approval, run_trace
from open_deep_research.citation_verification import (
    extract_internal_doc_ids,
    verify_citations,
    verify_doc_id_citations,
)
from open_deep_research.memory_manager import save_memory, search_memory, DEMO_USER_ID
from open_deep_research.configuration import (
    Configuration,
)
from open_deep_research.python_executor import python_executor_tool_call
from open_deep_research.internal_doc_search import internal_doc_search_tool_call
from open_deep_research.tool_reliability import (
    TransientToolError,
    TRANSIENT_NETWORK_EXCEPTION_TYPES,
    NonRetryableInfrastructureError,
)
from open_deep_research.write_intent import build_write_request_hint, detect_write_request, _human_texts
from open_deep_research.report_write_filter import filter_report_for_write_request
from open_deep_research.prompts import (
    clarify_with_user_instructions,
    compress_research_simple_human_message,
    compress_research_system_prompt,
    final_report_generation_prompt,
    lead_researcher_prompt,
    research_system_prompt,
    transform_messages_into_research_topic_prompt,
)
from open_deep_research.model_fallback import ainvoke_with_fallback
from open_deep_research.skills import skills_enabled, skills_prompt_for
from open_deep_research.tool_routing import filter_tools, mark_for
from open_deep_research.state import (
    AgentInputState,
    AgentState,
    ClarifyWithUser,
    ConductResearch,
    ResearchComplete,
    ResearcherOutputState,
    ResearcherState,
    ResearchQuestion,
    SupervisorState,
)
from open_deep_research.utils import (
    anthropic_websearch_called,
    get_all_tools,
    get_api_key_for_model,
    get_model_token_limit,
    get_notes_from_tool_calls,
    get_today_str,
    is_token_limit_exceeded,
    openai_websearch_called,
    remove_up_to_last_ai_message,
    think_tool,
)

# Initialize a configurable model that we will use throughout the agent
configurable_model = init_chat_model(
    configurable_fields=("model", "max_tokens", "api_key"),
)


def _skip_brief_enabled() -> bool:
    """ODR_SKIP_BRIEF=1（实验性，默认关闭）：不调用模型改写研究简报，直接用用户原话。"""
    return os.environ.get("ODR_SKIP_BRIEF", "0") == "1"


def _skip_compress_enabled() -> bool:
    """ODR_SKIP_COMPRESS=1（实验性，默认关闭）：researcher 结束后不调用模型压缩，直接把工具输出与最终回答拼接。"""
    return os.environ.get("ODR_SKIP_COMPRESS", "0") == "1"


def _last_human_text(messages) -> str:
    for m in reversed(messages or []):
        if getattr(m, "type", None) == "human":
            c = getattr(m, "content", "")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
            return str(c)
    return ""

async def clarify_with_user(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "__end__"]]:
    """Analyze user messages and ask clarifying questions if the research scope is unclear.
    
    This function determines whether the user's request needs clarification before proceeding
    with research. If clarification is disabled or not needed, it proceeds directly to research.
    
    Args:
        state: Current agent state containing user messages
        config: Runtime configuration with model settings and preferences
        
    Returns:
        Command to either end with a clarifying question or proceed to research brief
    """
    # Step 1: Check if clarification is enabled in configuration
    configurable = Configuration.from_runnable_config(config)
    if not configurable.allow_clarification:
        # Skip clarification step and proceed directly to research
        return Command(goto="write_research_brief")
    
    # Step 2: Prepare the model for structured clarification analysis
    messages = state["messages"]
    model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Configure model with structured output and retry logic
    clarification_model = (
        configurable_model
        .with_structured_output(ClarifyWithUser)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(model_config)
    )
    
    # Step 3: Analyze whether clarification is needed
    prompt_content = clarify_with_user_instructions.format(
        messages=get_buffer_string(messages), 
        date=get_today_str()
    )
    response = await clarification_model.ainvoke([HumanMessage(content=prompt_content)])
    
    # Step 4: Route based on clarification analysis
    if response.need_clarification:
        # End with clarifying question for user
        return Command(
            goto=END, 
            update={"messages": [AIMessage(content=response.question)]}
        )
    else:
        # Proceed to research with verification message
        return Command(
            goto="write_research_brief", 
            update={"messages": [AIMessage(content=response.verification)]}
        )


def _memory_user_id(config) -> str:
    """记忆命名空间：优先用 config["configurable"]["user_id"]（评测给每个用例分配独立 ID；真实服务应由认证层传入），
    没有时退回固定的 DEMO_USER_ID，与此前行为一致。"""
    try:
        uid = (config or {}).get("configurable", {}).get("user_id")
    except AttributeError:
        uid = None
    return uid if isinstance(uid, str) and uid.strip() else DEMO_USER_ID


async def write_research_brief(state: AgentState, config: RunnableConfig) -> Command[Literal["research_supervisor", "direct_research"]]:
    """Transform user messages into a structured research brief and initialize supervisor.
    
    This function analyzes the user's messages and generates a focused research brief
    that will guide the research supervisor. It also sets up the initial supervisor
    context with appropriate prompts and instructions.
    
    Args:
        state: Current agent state containing user messages
        config: Runtime configuration with model settings
        
    Returns:
        Command to proceed to research supervisor with initialized context
    """
    # Step 1: Set up the research model for structured output
    configurable = Configuration.from_runnable_config(config)
    research_model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Configure model for structured research question generation
    research_model = (
        configurable_model
        .with_structured_output(ResearchQuestion)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(research_model_config)
    )
    
    # Step 2: Generate structured research brief from user messages
    # 先检索这个用户的历史记忆（跨对话的偏好/约束），拼进生成简报的上下文
    user_messages_text = get_buffer_string(state.get("messages", []))
    memory_search_result = await search_memory(user_messages_text, user_id=_memory_user_id(config))
    remembered_context = ""
    if memory_search_result["success"] and memory_search_result["memories"]:
        remembered_items = [m.get("memory", "") for m in memory_search_result["memories"]]
        remembered_context = (
            "\n\n[以下是这个用户之前对话中设定过的、可能仍然有效的偏好或约束，"
            "仅供参考，如果本次提问明确给出了不同的信息，以本次为准]\n"
            + "\n".join(f"- {item}" for item in remembered_items)
        )

    prompt_content = transform_messages_into_research_topic_prompt.format(
        messages=user_messages_text + remembered_context,
        date=get_today_str()
    )
    if _skip_brief_enabled():
        # 快速路径：不再调用模型改写简报，直接使用用户最后一条提问（加记忆上下文）作为简报（EVIDENCE §39）
        class _Brief:  # 与 ResearchQuestion 同名字段，下游只读 research_brief
            research_brief = _last_human_text(state.get("messages", [])) + remembered_context
        response = _Brief()
    else:
        response = await research_model.ainvoke([HumanMessage(content=prompt_content)])

    # 写记忆：只存这次提问本身的原始文本，不存 research_brief（模型生成的、
    # 已经转写过的内容）——避免把模型的转述/推断当成"用户说的事实"存下来，
    # 这次提问文本本身可能包含偏好，也可能不包含，mem0 内部的提炼机制会
    # 自己判断值不值得存，这里只负责"喂给它这次用户说了什么"这个原始输入，
    # 不做额外筛选（筛选逻辑交给 mem0 自己的实现，我们不重复造轮子）。
    await save_memory(user_messages_text, user_id=_memory_user_id(config))
    
    # Step 3: Initialize supervisor with research brief and instructions
    supervisor_system_prompt = lead_researcher_prompt.format(
        date=get_today_str(),
        max_concurrent_research_units=configurable.max_concurrent_research_units,
        max_researcher_iterations=configurable.max_researcher_iterations
    )
    
    # 工具路由标记：用户原话命中尽调/台账写请求时，在简报末尾打标，随任务传给研究员节点
    # （默认关闭，未启用时 mark_for 恒为空串，简报原样不变）
    _route_mark = mark_for(user_messages_text)
    response.research_brief = response.research_brief + _route_mark

    return Command(
        goto=("direct_research" if _direct_research_enabled() else "research_supervisor"),
        update={
            "research_brief": response.research_brief,
            "supervisor_messages": {
                "type": "override",
                "value": [
                    SystemMessage(content=supervisor_system_prompt),
                    HumanMessage(content=response.research_brief)
                ]
            }
        }
    )


async def supervisor(state: SupervisorState, config: RunnableConfig) -> Command[Literal["supervisor_tools"]]:
    """Lead research supervisor that plans research strategy and delegates to researchers.
    
    The supervisor analyzes the research brief and decides how to break down the research
    into manageable tasks. It can use think_tool for strategic planning, ConductResearch
    to delegate tasks to sub-researchers, or ResearchComplete when satisfied with findings.
    
    Args:
        state: Current supervisor state with messages and research context
        config: Runtime configuration with model settings
        
    Returns:
        Command to proceed to supervisor_tools for tool execution
    """
    # Step 1: Configure the supervisor model with available tools
    configurable = Configuration.from_runnable_config(config)
    research_model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Available tools: research delegation, completion signaling, and strategic thinking
    lead_researcher_tools = [ConductResearch, ResearchComplete, think_tool]
    
    # Configure model with tools, retry logic, and model settings
    research_model = (
        configurable_model
        .bind_tools(lead_researcher_tools)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(research_model_config)
    )
    
    # Step 2: Generate supervisor response based on current context
    supervisor_messages = state.get("supervisor_messages", [])
    response = await research_model.ainvoke(supervisor_messages)
    
    # Step 3: Update state and proceed to tool execution
    return Command(
        goto="supervisor_tools",
        update={
            "supervisor_messages": [response],
            "research_iterations": state.get("research_iterations", 0) + 1
        }
    )

async def supervisor_tools(state: SupervisorState, config: RunnableConfig) -> Command[Literal["supervisor", "__end__"]]:
    """Execute tools called by the supervisor, including research delegation and strategic thinking.
    
    This function handles three types of supervisor tool calls:
    1. think_tool - Strategic reflection that continues the conversation
    2. ConductResearch - Delegates research tasks to sub-researchers
    3. ResearchComplete - Signals completion of research phase
    
    Args:
        state: Current supervisor state with messages and iteration count
        config: Runtime configuration with research limits and model settings
        
    Returns:
        Command to either continue supervision loop or end research phase
    """
    # Step 1: Extract current state and check exit conditions
    configurable = Configuration.from_runnable_config(config)
    supervisor_messages = state.get("supervisor_messages", [])
    research_iterations = state.get("research_iterations", 0)
    most_recent_message = supervisor_messages[-1]
    
    # Define exit criteria for research phase
    exceeded_allowed_iterations = research_iterations > configurable.max_researcher_iterations
    no_tool_calls = not most_recent_message.tool_calls
    research_complete_tool_call = any(
        tool_call["name"] == "ResearchComplete" 
        for tool_call in most_recent_message.tool_calls
    )
    
    # Exit if any termination condition is met
    if exceeded_allowed_iterations or no_tool_calls or research_complete_tool_call:
        return Command(
            goto=END,
            update={
                "notes": get_notes_from_tool_calls(supervisor_messages),
                "research_brief": state.get("research_brief", "")
            }
        )
    
    # Step 2: Process all tool calls together (both think_tool and ConductResearch)
    all_tool_messages = []
    update_payload = {"supervisor_messages": []}
    
    # Handle think_tool calls (strategic reflection)
    think_tool_calls = [
        tool_call for tool_call in most_recent_message.tool_calls 
        if tool_call["name"] == "think_tool"
    ]
    
    for tool_call in think_tool_calls:
        reflection_content = tool_call["args"]["reflection"]
        all_tool_messages.append(ToolMessage(
            content=f"Reflection recorded: {reflection_content}",
            name="think_tool",
            tool_call_id=tool_call["id"]
        ))
    
    # Handle ConductResearch calls (research delegation)
    conduct_research_calls = [
        tool_call for tool_call in most_recent_message.tool_calls 
        if tool_call["name"] == "ConductResearch"
    ]
    
    if conduct_research_calls:
        try:
            # Limit concurrent research units to prevent resource exhaustion
            allowed_conduct_research_calls = conduct_research_calls[:configurable.max_concurrent_research_units]
            overflow_conduct_research_calls = conduct_research_calls[configurable.max_concurrent_research_units:]
            
            # 2026-10 B3：只有启用了run_trace时才构造config副本——每个并行
            # researcher各自拿一份独立的researcher_id，解决多个并行researcher
            # 共享同一个config对象、trace事件分不清"谁是谁"的问题（已在Step 0
            # 用代码证据确认过：原来的config是同一个对象，塞进去的任何标识都会
            # 被所有并行调用共享）。未启用时原样传同一个config对象，不构造
            # 任何副本，默认行为（包括对象identity）完全不变。
            if run_trace.is_enabled(config):
                def _per_researcher_config(cfg):
                    return {**cfg, "configurable": {
                        **cfg.get("configurable", {}),
                        "run_trace_researcher_id": run_trace.new_id(),
                    }}
                research_configs = [_per_researcher_config(config) for _ in allowed_conduct_research_calls]
            else:
                research_configs = [config for _ in allowed_conduct_research_calls]

            # Execute research tasks in parallel
            # 简报带路由标记时，把标记附到每个研究任务末尾（标记不存在则原样）
            _brief = state.get("research_brief", "") or ""
            _tm = _brief[_brief.rfind("\n[路由:"):] if "\n[路由:" in _brief else ""
            research_tasks = [
                researcher_subgraph.ainvoke({
                    "researcher_messages": [
                        HumanMessage(content=tool_call["args"]["research_topic"] + _tm)
                    ],
                    "research_topic": tool_call["args"]["research_topic"] + _tm
                }, per_call_config)
                for tool_call, per_call_config in zip(allowed_conduct_research_calls, research_configs)
            ]
            
            tool_results = await asyncio.gather(*research_tasks)
            
            # Create tool messages with research results
            for observation, tool_call in zip(tool_results, allowed_conduct_research_calls):
                all_tool_messages.append(ToolMessage(
                    content=observation.get("compressed_research", "Error synthesizing research report: Maximum retries exceeded"),
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"]
                ))
            
            # Handle overflow research calls with error messages
            for overflow_call in overflow_conduct_research_calls:
                all_tool_messages.append(ToolMessage(
                    content=f"Error: Did not run this research as you have already exceeded the maximum number of concurrent research units. Please try again with {configurable.max_concurrent_research_units} or fewer research units.",
                    name="ConductResearch",
                    tool_call_id=overflow_call["id"]
                ))
            
            # Aggregate raw notes from all research results
            raw_notes_concat = "\n".join([
                "\n".join(observation.get("raw_notes", []))
                for observation in tool_results
            ])

            if raw_notes_concat:
                update_payload["raw_notes"] = [raw_notes_concat]

            # 2026-10 T3 新增：degradation_events/fetched_sources 用 extend 合并，
            # **不**像 raw_notes 那样 join 成一个无结构字符串——每条事件/来源
            # 本身已经是带 research_topic 归属的结构化dict，join 成字符串会
            # 丢失"哪个并行研究方向产生的"这个关键信息（这正是T3诊断阶段
            # 发现的 raw_notes 现有合并方式的问题，这里刻意不重蹈覆辙）。
            degradation_events_from_researchers = []
            fetched_sources_from_researchers = []
            fetched_doc_ids_from_researchers = []
            for observation in tool_results:
                degradation_events_from_researchers.extend(observation.get("degradation_events", []))
                fetched_sources_from_researchers.extend(observation.get("fetched_sources", []))
                fetched_doc_ids_from_researchers.extend(observation.get("fetched_doc_ids", []))
            if degradation_events_from_researchers:
                update_payload["degradation_events"] = degradation_events_from_researchers
            if fetched_sources_from_researchers:
                update_payload["fetched_sources"] = fetched_sources_from_researchers
            if fetched_doc_ids_from_researchers:
                update_payload["fetched_doc_ids"] = fetched_doc_ids_from_researchers

        except Exception as e:
            # Handle research execution errors
            if is_token_limit_exceeded(e, configurable.research_model) or True:
                # Token limit exceeded or other error - end research phase
                return Command(
                    goto=END,
                    update={
                        "notes": get_notes_from_tool_calls(supervisor_messages),
                        "research_brief": state.get("research_brief", "")
                    }
                )
    
    # Step 3: Return command with all tool results
    update_payload["supervisor_messages"] = all_tool_messages
    return Command(
        goto="supervisor",
        update=update_payload
    ) 

# Supervisor Subgraph Construction
# Creates the supervisor workflow that manages research delegation and coordination
supervisor_builder = StateGraph(SupervisorState, config_schema=Configuration)

# Add supervisor nodes for research management
supervisor_builder.add_node("supervisor", supervisor)           # Main supervisor logic
supervisor_builder.add_node("supervisor_tools", supervisor_tools)  # Tool execution handler

# Define supervisor workflow edges
supervisor_builder.add_edge(START, "supervisor")  # Entry point to supervisor

# Compile supervisor subgraph for use in main workflow
supervisor_subgraph = supervisor_builder.compile()

async def researcher(state: ResearcherState, config: RunnableConfig) -> Command[Literal["researcher_tools"]]:
    """Individual researcher that conducts focused research on specific topics.
    
    This researcher is given a specific research topic by the supervisor and uses
    available tools (search, think_tool, MCP tools) to gather comprehensive information.
    It can use think_tool for strategic planning between searches.
    
    Args:
        state: Current researcher state with messages and topic context
        config: Runtime configuration with model settings and tool availability
        
    Returns:
        Command to proceed to researcher_tools for tool execution
    """
    # Step 1: Load configuration and validate tool availability
    configurable = Configuration.from_runnable_config(config)
    researcher_messages = state.get("researcher_messages", [])
    
    # Get all available research tools (search, MCP, think_tool)
    tools = await get_all_tools(config)
    if len(tools) == 0:
        raise ValueError(
            "No tools found to conduct research: Please configure either your "
            "search API or add MCP tools to your configuration."
        )
    
    # Step 2: Configure the researcher model with tools
    research_model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Prepare system prompt with MCP context if available
    researcher_prompt = research_system_prompt.format(
        mcp_prompt=configurable.mcp_prompt or "", 
        date=get_today_str()
    )
    # 工具层路由（默认关闭，ODR_TOOL_ROUTING=1 才启用）：尽调 / 台账写请求不给联网搜索工具，见 tool_routing.py。
    if researcher_messages:
        _first = researcher_messages[0]
        tools = filter_tools(tools, _first.content if isinstance(getattr(_first, "content", None), str) else "")
    # Skill 注入（默认关闭，DEEP_RESEARCH_SKILLS=1 才启用）：按研究主题关键词挑选流程规范。
    if skills_enabled() and researcher_messages:
        first = researcher_messages[0]
        topic_text = first.content if isinstance(getattr(first, "content", None), str) else ""
        extra = skills_prompt_for(topic_text)
        if extra:
            researcher_prompt = researcher_prompt + "\n\n" + extra
    
    # Configure model with tools, retry logic, and settings
    research_model = (
        configurable_model
        .bind_tools(tools)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(research_model_config)
    )
    
    # Step 3: Generate researcher response with system context
    messages = [SystemMessage(content=researcher_prompt)] + researcher_messages
    if configurable.research_model_fallback:
        fallback_model = (
            configurable_model
            .bind_tools(tools)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                **research_model_config,
                "model": configurable.research_model_fallback,
                "api_key": get_api_key_for_model(configurable.research_model_fallback, config),
            })
        )
        response, served_by = await ainvoke_with_fallback(
            [("primary", research_model), ("fallback", fallback_model)], messages
        )
        if served_by != "primary":
            logging.getLogger(__name__).warning(
                "researcher served by fallback model %s", configurable.research_model_fallback
            )
    else:
        response = await research_model.ainvoke(messages)
    
    # Step 4: Update state and proceed to tool execution
    return Command(
        goto="researcher_tools",
        update={
            "researcher_messages": [response],
            "tool_call_iterations": state.get("tool_call_iterations", 0) + 1
        }
    )

# Tool Execution Helper Function

# 哪些工具有"抛类型化异常"的入口可以用来做重试判断（而不是只能走会吞掉一切
# 异常、返回字符串的公开 .ainvoke() 接口）。没有在这里注册的工具（搜索工具、
# MCP 工具、think_tool、ResearchComplete）仍然走原来的 tool.ainvoke() 路径，
# 行为和加这层重试之前完全一致。sql_executor 没有出现在这里：它所有失败路径
# 都已经在工具内部被转换成字符串返回、从不抛异常，对 execute_tool_safely 而言
# 天然就是"不重试"，不需要额外包装（对应 2026-10 诊断报告里"sql_executor：
# 所有错误都不重试"这条分类——不是遗漏，是本来就已经满足）。
_RETRY_AWARE_TOOL_CALLS: dict[str, Callable] = {
    "python_executor": python_executor_tool_call,
    "internal_doc_search": internal_doc_search_tool_call,
}

# 已知局限（2026-10 确认，不是推测）：对注册在 _RETRY_AWARE_TOOL_CALLS 里的
# 工具，execute_tool_safely 调用的是裸的异步函数，不是 tool.ainvoke(args,
# config)。实测验证过——用一个挂了 on_tool_start/on_tool_end 的回调处理器，
# 走 tool.ainvoke() 这条路径（sql_executor 和所有未注册的工具）会正常触发
# 这两个回调；直接调用 python_executor_tool_call(...) 则完全不会触发任何
# 回调事件。这意味着 python_executor / internal_doc_search 经由重试逻辑
# 执行时，不会在 LangSmith/LangGraph 的 tracing 里单独出现为一条工具调用
# span——它们的执行会被"吞"进 researcher_tools 这个节点自身的 span 里，
# 看不到独立的输入/输出/耗时记录。sql_executor 和其他未注册的工具不受影响，
# 仍然走 tool.ainvoke()，tracing 完整。
#
# 为什么不直接修：要保留 tracing，最直接的办法是让 execute_tool_safely 始终
# 调用 tool.ainvoke(args, config)，并让 TransientToolError 真的从公开的
# python_executor / internal_doc_search 工具函数里逃出来（而不是像现在这样
# 被公开函数自己的 except TransientToolError 分支转换回字符串）。但这样会
# 改变这两个工具"无论如何都返回字符串、从不抛异常"这条从加固阶段就定下来的
# 设计原则本身——不只是不破坏现有测试的问题，是任何人以后直接
# `await python_executor.ainvoke(...)` 调用它时的行为预期都会变。这是一次
# 需要明确讨论的设计取舍，不是这一轮顺手能定的事，所以暂时保留现状、如实
# 记录这个局限。


_tool_reliability_logger = logging.getLogger("open_deep_research.tool_reliability")


def _log_tool_attempt(
    tool_name: str, attempt: int, failure_kind: str, duration_s: float, will_retry: bool
) -> None:
    """每次工具尝试（不管成功失败）都记一条结构化日志，作为 2026-10 确认过的
    tracing 丢失（python_executor/internal_doc_search 经由重试逻辑执行时不会
    出现在 LangSmith/LangGraph 的工具调用 span 里）的补偿——至少本地日志里能
    看到"这次 researcher_tools 调用里，哪个工具试了几次、每次什么结果、花了
    多久、有没有接着重试"，也供 T2 第四步的 E2E 故障注入实验分析用。
    用 logger.info 而不是 print，方便按需过滤/重定向，不强制写文件。
    """
    _tool_reliability_logger.info(
        "tool_attempt tool=%s attempt=%d failure_kind=%s duration_s=%.3f will_retry=%s",
        tool_name, attempt, failure_kind, duration_s, will_retry,
    )


async def execute_tool_safely(
    tool, args, config, configurable: "Configuration | None" = None, research_topic: str = ""
) -> tuple[str, str, str, int]:
    """执行单个工具调用，对分类为瞬时性的失败做有限次数的指数退避重试。

    2026-10 B3：新增可选参数research_topic（有默认值，不影响任何现有调用
    方），只用于run_trace.emit()的事件标注，不参与任何判断逻辑。**返回值
    签名未变**，仍是 (content, status, failure_kind, attempts) 四元组。

    返回 (content, status, failure_kind, attempts) 四元组（2026-10 T3 新增
    attempts——记的是这次调用总共尝试了几次，给 researcher_tools 记录
    degradation_events 用，不是单独统计重试次数的另一套机制）。这是 2026-10 的一次修正：
    之前只返回 content 字符串，熔断逻辑被迫靠"看起来像不像错误"（status）去
    判断要不要计入熔断——这是错的：sql_executor 把原始报错返回给模型自我
    纠错是设计本身的一部分，模型探索表结构时连续写错几条 SQL 非常正常，不该
    被当成"这个工具坏了"而熔断掉。status 和 failure_kind 是两个独立的维度：

    - status（"success" / "error"）：纯展示性的，靠各工具已有的错误字符串
      前缀判断（见 _looks_like_tool_error），用于设置 ToolMessage.status，
      判断错了的代价只是展示不准，不影响任何重试/熔断逻辑。
    - failure_kind（"none" / "tool_input_error" / "infrastructure_error"）：
      决定要不要计入熔断，**只能**由"有没有真的抛出异常、异常是什么类型"
      这个结构性事实决定，绝不根据字符串内容猜测：
        * 工具自己把失败处理成一个字符串正常返回（没有异常逃出来）——无论
          这个字符串是不是看起来像错误（SQL语法错、Python代码跑错、
          python_executor 自己的"代码跑太久"超时）——都是 tool_input_error，
          这是"模型这次的输入/代码有问题"，不是环境的问题，不计入熔断。
        * 有异常真的从工具实现里逃出来，被这里的 except 捕获——无论是
          TransientToolError 重试耗尽、还是未分类的其他异常、还是没注册
          "抛异常版"入口的工具自己 ainvoke() 时抛的异常——都是
          infrastructure_error，计入熔断。

    分类依据真实抛出的异常类型，不解析返回字符串的前缀或内容：
    - 工具在 _RETRY_AWARE_TOOL_CALLS 里注册了"抛异常版"入口时，调用那个入口，
      按 TransientToolError 或已知网络异常类型（TRANSIENT_NETWORK_EXCEPTION_TYPES，
      目前是 openai.APIConnectionError / APITimeoutError）判断是否重试；重试
      次数耗尽后，把异常的原始消息原样返回——和直接调用对应公开工具时会得到
      的字符串完全一致，不额外加前缀。
    - 没有注册"抛异常版"入口的工具，走原来的 tool.ainvoke()，行为和加这层
      重试之前完全一致：任何异常都不重试，直接格式化成
      "Error executing tool: {消息}" 返回，归为 infrastructure_error。

    重试安全性前提：当前已知的全部工具都是只读或无副作用操作，重复执行不会
    产生"副作用被多执行一次"的风险（详见 tool_reliability.py 模块文档字符串）。
    """
    max_retries = configurable.tool_retry_max_attempts if configurable else 2
    base_delay = configurable.tool_retry_base_delay_seconds if configurable else 0.5

    tool_name = tool.name if hasattr(tool, "name") else "<unknown>"

    # 人工审批闸门（默认关闭）：被拒绝/超时/没有审批代理时，这次调用不执行。
    # 归为 tool_input_error（不是基础设施故障），因此不计入熔断，也不重试。
    if configurable is not None and getattr(configurable, "human_approval_enabled", False):
        allowed, decision = await approval.gate(
            tool_name, args, float(getattr(configurable, "human_approval_timeout_seconds", 300))
        )
        if not allowed:
            msg = f"Tool call not executed: human approval {decision} for '{tool_name}'."
            run_trace.emit("tool_start", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=1, params=args)
            run_trace.emit("tool_end", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=1, duration_ms=0.0, status="error",
                            failure_kind="tool_input_error", output=msg)
            return msg, "error", "tool_input_error", 1
    raising_call = _RETRY_AWARE_TOOL_CALLS.get(tool_name) if tool_name else None

    if raising_call is None:
        t0 = time.monotonic()
        run_trace.emit("tool_start", config=config, research_topic=research_topic,
                        tool=tool_name, attempt=1, params=args)
        try:
            result = await tool.ainvoke(args, config)
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            _log_tool_attempt(tool_name, attempt=1, failure_kind="infrastructure_error",
                               duration_s=time.monotonic() - t0, will_retry=False)
            run_trace.emit("tool_end", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=1, duration_ms=duration_ms,
                            status="error", failure_kind="infrastructure_error",
                            output=str(e))
            return f"Error executing tool: {str(e)}", "error", "infrastructure_error", 1
        content, status, failure_kind = _classify_clean_return(result)
        duration_ms = (time.monotonic() - t0) * 1000
        _log_tool_attempt(tool_name, attempt=1, failure_kind=failure_kind,
                           duration_s=time.monotonic() - t0, will_retry=False)
        run_trace.emit("tool_end", config=config, research_topic=research_topic,
                        tool=tool_name, attempt=1, duration_ms=duration_ms,
                        status=status, failure_kind=failure_kind,
                        output=content if isinstance(content, str) else None)
        return content, status, failure_kind, 1

    attempt = 0
    while True:
        attempt += 1
        t0 = time.monotonic()
        run_trace.emit("tool_start", config=config, research_topic=research_topic,
                        tool=tool_name, attempt=attempt, params=args)
        try:
            result = await raising_call(**args)
        except NonRetryableInfrastructureError as e:
            # 分类为基础设施问题（计入熔断），但刻意不重试——见
            # NonRetryableInfrastructureError 的文档字符串（目前唯一场景是
            # internal_doc_search 超时：被放弃的后台线程仍在运行，重试会让
            # 这类线程堆积）。
            duration_ms = (time.monotonic() - t0) * 1000
            _log_tool_attempt(tool_name, attempt=attempt, failure_kind="infrastructure_error",
                               duration_s=time.monotonic() - t0, will_retry=False)
            run_trace.emit("tool_end", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=attempt, duration_ms=duration_ms,
                            status="error", failure_kind="infrastructure_error", output=str(e))
            return str(e), "error", "infrastructure_error", attempt
        except (TransientToolError,) + TRANSIENT_NETWORK_EXCEPTION_TYPES as e:
            will_retry = attempt <= max_retries
            duration_ms = (time.monotonic() - t0) * 1000
            _log_tool_attempt(tool_name, attempt=attempt, failure_kind="infrastructure_error",
                               duration_s=time.monotonic() - t0, will_retry=will_retry)
            run_trace.emit("tool_end", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=attempt, duration_ms=duration_ms,
                            status="error", failure_kind="infrastructure_error", output=str(e))
            if not will_retry:
                return str(e), "error", "infrastructure_error", attempt
            run_trace.emit("retry", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=attempt, reason="transient_error")
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
        except Exception as e:
            # 未被分类成瞬时性错误的其他异常——不重试，直接返回，算
            # infrastructure_error（这是一个我们没预料到的失败形态，不是
            # 工具按自己的逻辑产出的、可以理解的"输入有问题"）。
            duration_ms = (time.monotonic() - t0) * 1000
            _log_tool_attempt(tool_name, attempt=attempt, failure_kind="infrastructure_error",
                               duration_s=time.monotonic() - t0, will_retry=False)
            run_trace.emit("tool_end", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=attempt, duration_ms=duration_ms,
                            status="error", failure_kind="infrastructure_error", output=str(e))
            return f"Error executing tool: {str(e)}", "error", "infrastructure_error", attempt
        else:
            content, status, failure_kind = _classify_clean_return(result)
            duration_ms = (time.monotonic() - t0) * 1000
            _log_tool_attempt(tool_name, attempt=attempt, failure_kind=failure_kind,
                               duration_s=time.monotonic() - t0, will_retry=False)
            run_trace.emit("tool_end", config=config, research_topic=research_topic,
                            tool=tool_name, attempt=attempt, duration_ms=duration_ms,
                            status=status, failure_kind=failure_kind,
                            output=content if isinstance(content, str) else None)
            return content, status, failure_kind, attempt


# 各工具已有的错误字符串前缀——仅用于给 ToolMessage 设置展示性的 status 字段
# 和（在"工具自己正常返回字符串，没有异常逃出来"的情况下）区分
# tool_input_error 还是 none。**不用于判断 infrastructure_error**——那个
# 判断只看有没有异常真的逃出来（见 execute_tool_safely 的文档字符串）。
# 这些前缀本来就是各工具一直在用的既有约定，不是本轮新发明的。
_ERROR_STRING_PREFIXES = (
    "SQL Error:",
    "Python Error:",
    "Document search error:",
    "Error executing tool:",
)


def _looks_like_tool_error(result) -> bool:
    return isinstance(result, str) and result.startswith(_ERROR_STRING_PREFIXES)


def _classify_clean_return(result) -> tuple[str, str, str]:
    """工具没有抛异常、正常返回了 result 时的分类：status 靠字符串前缀判断
    （纯展示性，见上）；failure_kind 要么是 "none"（看起来是成功），要么是
    "tool_input_error"（看起来是失败，但既然是正常返回而不是抛异常，就说明
    是工具自己识别并处理掉的、关于这次输入/代码的问题，不是环境问题）——
    这里**不可能**产出 "infrastructure_error"，那个标签只在有异常真的逃出来
    时才会被打上。
    """
    if _looks_like_tool_error(result):
        return result, "error", "tool_input_error"
    return result, "success", "none"


# ---------------------------------------------------------------------------
# B1 循环检测：4个纯函数，不接触任何state/config，方便直接单测边界情况。
# ---------------------------------------------------------------------------

def _canonical_args(args: dict) -> str:
    """保守的参数规范化：只做结构层面的标准化，不做任何语义层面的"理解"。
    - dict键排序（顺序不应该影响判断）。
    - 字符串值strip()首尾空白（纯格式差异，不是语义差异）。
    刻意不做：大小写归一——SQL表名、代码变量名、URL路径里的大小写很多
    时候是语义相关的，统一转小写可能把两个本质不同的调用误判成相同，
    这是"宁可漏检，不可错判"的保守取向。也不做任何同义词/语序/改写识别——
    "iPhone 15 pro价格"和"iPhone15 Pro 的价格"在这套规则下是两个不同的
    签名，检测不到，这是已知局限，不是bug。
    """
    def _normalize(v):
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            return {k: _normalize(v[k]) for k in sorted(v.keys())}
        if isinstance(v, list):
            return [_normalize(x) for x in v]
        return v
    return json.dumps(_normalize(args), sort_keys=True, ensure_ascii=False)


def _call_signature(tool_name: str, args: dict) -> str:
    """一次"模型发起的调用"的规范化签名——同一个签名意味着同一个工具、
    规范化后完全相同的参数。不区分这次调用最终是成功/失败/被拦截。"""
    return f"{tool_name}::{_canonical_args(args)}"


def _is_repeat_hit(history_with_this_call: list, signature: str, window: int, threshold: int) -> bool:
    """重复检测：在最近window次模型发起的调用（history_with_this_call
    已经包含了这次调用本身，调用方负责把这次的signature追加进去再传进来）
    里，同一个signature出现的次数达到threshold就判定命中。用>=而不是==，
    避免窗口边界效应导致漏判；在threshold<=window的正常配置下，第1到
    threshold-1次出现时计数都<threshold（允许执行），恰好第threshold次
    出现时计数首次达到threshold（判定命中）——这就是"允许执行N-1次，
    第N次被拦截"的边界语义。
    """
    window_slice = history_with_this_call[-window:]
    return window_slice.count(signature) >= threshold


def _is_abab_hit(history_with_this_call: list, cycles: int) -> bool:
    """ABAB交替检测：最近2*cycles次调用（history_with_this_call已经包含
    这次调用本身）如果恰好构成A,B,A,B,...,A,B这种严格交替模式（A和B是
    两个不同的签名），就判定命中——完成这个模式的那次调用（也就是传进来
    的最后一个元素）被拦截。A-B-A-C这种不是严格交替的序列不会命中（第3
    项和期望的B不符，直接判空）。"""
    period = 2 * cycles
    if len(history_with_this_call) < period:
        return False
    tail = history_with_this_call[-period:]
    a, b = tail[0], tail[1]
    if a == b:
        return False
    expected = [a if i % 2 == 0 else b for i in range(period)]
    return tail == expected


async def researcher_tools(state: ResearcherState, config: RunnableConfig) -> Command[Literal["researcher", "compress_research"]]:
    """Execute tools called by the researcher, including search tools and strategic thinking.
    
    This function handles various types of researcher tool calls:
    1. think_tool - Strategic reflection that continues the research conversation
    2. Search tools (tavily_search, web_search) - Information gathering
    3. MCP tools - External tool integrations
    4. ResearchComplete - Signals completion of individual research task
    
    Args:
        state: Current researcher state with messages and iteration count
        config: Runtime configuration with research limits and tool settings
        
    Returns:
        Command to either continue research loop or proceed to compression
    """
    # Step 1: Extract current state and check early exit conditions
    configurable = Configuration.from_runnable_config(config)
    researcher_messages = state.get("researcher_messages", [])
    most_recent_message = researcher_messages[-1]
    
    # Early exit if no tool calls were made (including native web search)
    has_tool_calls = bool(most_recent_message.tool_calls)
    has_native_search = (
        openai_websearch_called(most_recent_message) or 
        anthropic_websearch_called(most_recent_message)
    )
    
    if not has_tool_calls and not has_native_search:
        return Command(goto="compress_research")
    
    # Step 2: Handle other tool calls (search, MCP tools, etc.)
    tools = await get_all_tools(config)
    tools_by_name = {
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search"): tool
        for tool in tools
    }

    # Execute all tool calls in parallel
    tool_calls = most_recent_message.tool_calls

    # 熔断计数快照：来自本次 researcher 子图运行的 state，每次调用都会算出
    # 更新后的完整字典再整体写回（见下面 Command 的 update），天然随每次
    # researcher_subgraph.ainvoke(...) 调用重新开始，不会跨运行/跨并发污染
    # ——不需要额外加锁或命名空间隔离，LangGraph 的 state 传递机制本身就
    # 保证了这一点（和 tool_call_iterations 走的是同一条路）。
    tool_failure_counts = dict(state.get("tool_failure_counts", {}))

    research_topic = state.get("research_topic", "")

    # B1 循环检测状态快照——和tool_failure_counts同一种模式：从state读一份
    # 快照，这一轮算完整的新值再整体写回（不用reducer），天然随每次
    # researcher_subgraph.ainvoke(...)调用重新开始，并行researcher之间
    # 互不污染。
    recent_tool_calls = list(state.get("recent_tool_calls", []))
    loop_detection_consecutive_hits = state.get("loop_detection_consecutive_hits", 0)
    _loop_history_max_len = max(
        configurable.loop_detection_repeat_window,
        2 * configurable.loop_detection_abab_cycles,
    )

    async def _run_one(tool_call: dict) -> tuple[str, str, str, "str | None", "int | None"]:
        """返回 (content, status, failure_kind, degradation_reason, attempts)。

        status 只取 "success" / "error"，纯展示性。
        failure_kind 取 "none" / "tool_input_error" / "infrastructure_error"，
        决定要不要计入熔断（见下面的计数更新和 execute_tool_safely 的文档
        字符串）——未知工具名和"已被熔断"这两种情况下 failure_kind 填什么
        无所谓，因为紧接着的计数更新循环会用 `name not in tools_by_name`
        跳过未知工具名，不会让它污染任何真实工具的计数；"已被熔断"的情况下
        继续累加 infrastructure_error 计数也无害（只会让计数继续增长，不影响
        "是否 >= 阈值"这个判断）。

        degradation_reason（2026-10 T3 新增）只在真正需要记 degradation_event
        时才非None："circuit_breaker_open"（熔断已经打开，本次调用被直接拦
        截，工具根本没被真的调用）或"retries_exhausted"（execute_tool_safely
        内部重试全部用尽，或被外层安全超时打断）。幻觉工具名**不**算
        source_unavailable——那是"根本没有这个源"，不是"这个源不可用"，
        语义不同，这里刻意不记（范围边界，已在汇报里单独说明）。
        attempts 对应 degradation_reason 非None 时的真实尝试次数；外层超时
        打断的情况下不知道内部具体跑到第几次，用 None 如实表示"未知"，不
        瞎猜一个数字凑数。
        """
        name = tool_call["name"]

        # 2026-10 审查后新增：call_id复用tool_call自带的id（模型产生这次
        # 工具调用时自己生成的，ToolMessage.tool_call_id用的就是它），不是
        # 另外发明一个。只在启用追踪时构造一份config副本把它塞进去——和
        # supervisor_tools那处构造researcher_id副本的做法完全一致（关闭时
        # 原样传config，不构造任何副本，默认行为不变）。这样execute_tool_
        # safely内部的emit调用不需要新增任何参数就能拿到call_id，不用改
        # 它的函数签名。
        if run_trace.is_enabled(config):
            trace_config = {**config, "configurable": {
                **config.get("configurable", {}),
                "run_trace_call_id": tool_call.get("id", ""),
            }}
        else:
            trace_config = config

        # B1 循环检测：必须在这里（unknown-tool-name检查、熔断检查、真正
        # 执行之前）就拦截，才能实现"不执行该调用"这个处置方案。ResearchComplete
        # 整个排除在检测之外——不管它被调用了多少次、参数是什么，永远不拦截
        # （而且也不会被计入下面的历史追加，否则会污染其它工具的ABAB判断）。
        # 幻觉工具名（不在tools_by_name里的）不豁免——模型反复产生同一个
        # 幻觉调用也是一种值得拦截的循环，检测本身不需要工具真的存在。
        if configurable.loop_detection_enabled and name != "ResearchComplete":
            signature = _call_signature(name, tool_call.get("args", {}))
            candidate = recent_tool_calls + [signature]
            is_repeat = _is_repeat_hit(
                candidate, signature,
                configurable.loop_detection_repeat_window,
                configurable.loop_detection_repeat_threshold,
            )
            is_abab = _is_abab_hit(candidate, configurable.loop_detection_abab_cycles)
            if is_repeat or is_abab:
                loop_reason = "repeat" if is_repeat else "abab"
                run_trace.emit("loop_detected", config=trace_config, research_topic=research_topic,
                                tool=name, reason=loop_reason)
                # status="error"（不是"success"）、正文不含"URL:"字样——
                # 确保不会被下面的fetched_sources提取逻辑误收录（那段逻辑
                # 只在status=="success"时才扫描内容，这里status是"error"，
                # 天然被排除，这里的措辞也刻意不用"URL:"这个词）。
                # failure_kind用一个全新的值"loop_detected"——不是
                # "infrastructure_error"也不是"none"，下面的熔断计数更新
                # 循环（只认这两个值）不会被这个值影响，熔断计数原样不变；
                # degradation_reason（第4个元素）是None，不会进入
                # degradation_events，不会出现在最终报告的披露章节——循环
                # 检测不是"数据源不可用"，是模型自己的行为模式问题。
                return (
                    f"Loop detected: the exact same call to tool '{name}' (or an "
                    "alternating pattern with another tool) has repeated too many "
                    "times without new progress. Please try a different tool, "
                    "meaningfully different arguments, or call ResearchComplete if "
                    "you already have enough information.",
                    "error",
                    "loop_detected",
                    None,
                    None,
                )

        # 2026-10 修复：之前这里是 tools_by_name[tool_call["name"]] 的裸索引，
        # 模型一旦产生幻觉调用了不存在的工具名就会抛未捕获的 KeyError，
        # 把整个 researcher_tools 节点崩掉（已经在诊断阶段用真实的
        # researcher_tools(state, config) 调用复现过这个崩溃）。
        if name not in tools_by_name:
            available = ", ".join(sorted(tools_by_name.keys()))
            return (
                f"Error executing tool: unknown tool '{name}'. "
                f"Available tools: {available}",
                "error",
                "infrastructure_error",
                None,
                None,
            )

        if tool_failure_counts.get(name, 0) >= configurable.tool_circuit_breaker_threshold:
            run_trace.emit("breaker_block", config=trace_config, research_topic=research_topic,
                            tool=name, status="error", failure_kind="infrastructure_error",
                            reason="circuit_breaker_open")
            return (
                f"Error executing tool: '{name}' has failed "
                f"{tool_failure_counts[name]} times in a row during this "
                "research task and is temporarily marked unavailable for the "
                "rest of it.",
                "error",
                "infrastructure_error",
                "circuit_breaker_open",
                0,  # 熔断拦截，工具这次根本没被真的调用，尝试次数是0
            )

        try:
            content, status, failure_kind, attempts = await asyncio.wait_for(
                execute_tool_safely(
                    tools_by_name[name], tool_call["args"], trace_config, configurable,
                    research_topic=research_topic,
                ),
                timeout=configurable.tool_call_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # 2026-10 B3：execute_tool_safely内部被asyncio.wait_for取消时，
            # 正在执行中的那次attempt来不及自己发tool_end（协程被cancel，
            # 根本没跑到_log_tool_attempt/emit那一行）——这里补发一条
            # status="outer_timeout"的tool_end，填上这个缺口。call_id和
            # 那次被取消的attempt共享同一个tool_call["id"]，summarize_
            # trace.py按(researcher_id, tool, call_id)配对时能对上；如果
            # 取消恰好发生在两次重试之间的backoff sleep窗口（没有新attempt
            # 在执行），这条补发的tool_end就没有对应的tool_start——这是
            # 已知局限，见run_trace.py模块文档字符串。
            run_trace.emit("tool_end", config=trace_config, research_topic=research_topic,
                            tool=name, status="outer_timeout", failure_kind="infrastructure_error",
                            reason="outer_timeout")
            # 没有自带超时保护的工具的安全网，覆盖的是 execute_tool_safely
            # 整个调用（包括它内部的重试循环和退避等待），不只是单次尝试。
            # 对已有内部超时的三个自建工具（sql_executor 5s / python_executor
            # 最坏约23s含清理 / internal_doc_search 15s）而言，
            # tool_call_timeout_seconds 的默认值（30s）刻意设得比它们各自
            # 最坏情况更长，所以这条分支正常不会抢在工具自己的内部超时之前
            # 触发——它们会先用自己的机制（比如 python_executor 的 docker
            # kill + 轮询确认）干净地返回，不依赖这里的 cancel 去做清理。
            # 这是"执行环境异常导致的超时"，不是代码本身的问题，算
            # infrastructure_error。
            # 2026-10 修正（审查意见）：degradation_reason 用独立的
            # "outer_timeout"，不能和execute_tool_safely内部重试循环真的
            # 跑完、确认失败的"retries_exhausted"混用同一个值——两者的
            # 确定性不一样：retries_exhausted时我们确实知道工具被真的调用过
            # 、失败过N次；outer_timeout时我们只知道"没在限定时间内跑完"，
            # 不知道它是卡住了、还是终究会成功，不能说这是"数据源不可用"，
            # 只能说"这次调用没有在限定时间内完成"。两个reason分开后，
            # 披露小节才能用不同措辞分别处理（见 _DEGRADATION_REASON_TEXT）。
            return (
                f"Error executing tool: '{name}' did not complete within "
                f"{configurable.tool_call_timeout_seconds}s (outer safety "
                "timeout).",
                "error",
                "infrastructure_error",
                "outer_timeout",
                None,  # 外层超时打断，不知道内部具体跑到第几次，如实标None
            )

        degradation_reason = "retries_exhausted" if failure_kind == "infrastructure_error" else None
        return content, status, failure_kind, degradation_reason, attempts

    results = await asyncio.gather(*(_run_one(tc) for tc in tool_calls))

    # 更新熔断计数：只看 failure_kind，不看 status（这是T2修复的核心）。
    #   infrastructure_error -> 计数 +1
    #   tool_input_error     -> 计数不变（既不清零也不增加——SQL写错/代码
    #                            报错是模型自我纠错流程的正常部分，不该让它
    #                            悄悄侵蚀熔断计数，但也不该因为"看起来像
    #                            成功"而被当成"这个工具现在没问题了"）
    #   none（真正成功）      -> 计数清零
    # 幻觉工具名不计入任何真实工具的失败计数（见 _run_one 的说明）。
    for (_, _, failure_kind, _, _), tool_call in zip(results, tool_calls):
        name = tool_call["name"]
        if name not in tools_by_name:
            continue
        if failure_kind == "infrastructure_error":
            tool_failure_counts[name] = tool_failure_counts.get(name, 0) + 1
        elif failure_kind == "none":
            tool_failure_counts[name] = 0
        # failure_kind == "tool_input_error"：有意不做任何改动。

    # 2026-10 T3 新增：degradation_events——只在 degradation_reason 非None时
    # 才记一条，结构见 state.py 的字段注释。原始事件全部保留，不在这里去重
    # （去重是最终报告披露小节的责任，不是事件记录本身的责任）。
    degradation_events = []
    for (_, _, failure_kind, degradation_reason, attempts), tool_call in zip(results, tool_calls):
        if degradation_reason is None:
            continue
        degradation_events.append({
            "type": "source_unavailable",
            "source": tool_call["name"],
            "reason": degradation_reason,
            "failure_kind": failure_kind,
            "attempts": attempts,
            "research_topic": research_topic,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        run_trace.emit("degradation", config=config, research_topic=research_topic,
                        tool=tool_call["name"], attempt=attempts, status="error",
                        failure_kind=failure_kind, reason=degradation_reason)

    # 2026-10 T3 新增：fetched_sources——只从 ToolMessage 的真实返回内容里
    # 抽取URL，不从 raw_notes 解析（raw_notes 混了 AI 消息，AI 消息里的URL
    # 可能是模型自己编的，不是真的抓取过的）。这里匹配的是 tavily_search 自己
    # 固定的输出格式（utils.py 里 "URL: {url}\n\n" 这一行，逐字核对过源码），
    # 不是对任意文本做关键词猜测——这是解析一个我们读过、确认稳定的结构化
    # 输出格式，范围严格限定在这一个已知格式上。
    # 已知局限：Anthropic/OpenAI 的 native websearch 结果不经过 ToolMessage
    # （直接是AIMessage自己的content block/annotation，openai_websearch_called/
    # anthropic_websearch_called 这两个函数就是为了检测这种情况而存在的）——
    # 当前 search_api 配置默认是 SearchAPI.TAVILY（configuration.py:144），
    # 这是本机 .env 实际在用的（TAVILY_API_KEY 已配置）；如果换成 Anthropic/
    # OpenAI 原生搜索，这里的 fetched_sources 提取不会覆盖那些来源，不是静默
    # 漏掉——这段注释就是明确声明。
    # 2026-10 T3 修正（测试过程中真实复现过的bug，不是猜的）：ResearchComplete
    # 这个工具本身也在 tools_by_name 里、会被当成普通工具走到这里——
    # tool(ResearchComplete).ainvoke({}) 返回的不是字符串，是一个
    # ResearchComplete() 模型实例本身（status仍然是"success"，因为没有异常
    # 逃出来）。之前只有 ToolMessage 的构造函数会碰到这个值，它的校验器会把
    # 非字符串内容悄悄处理掉，从来没出过问题；但这里是新加的第二个消费者，
    # 直接对 content 做 re.findall 会在 status=="success" 且 content 不是
    # 字符串时崩掉。加 isinstance 保护，不是因为"可能"发生，是已经真实踩到过。
    fetched_sources = []
    for (content, status, failure_kind, _, _), tool_call in zip(results, tool_calls):
        if status != "success" or not isinstance(content, str):
            continue
        for url in re.findall(r"^URL: (\S+)$", content, re.MULTILINE):
            fetched_sources.append({
                "source": tool_call["name"],
                "url": url,
                "research_topic": research_topic,
            })

    # 2026-10 T3b 新增：fetched_doc_ids——只来自工具名为 internal_doc_search、
    # 状态为成功且内容为字符串的 ToolMessage；按 chunk 切分后只解析每个 chunk
    # 的首行（见 citation_verification.extract_internal_doc_ids），不解析 chunk
    # 正文，也不解析其他工具的输出（tavily 抓回的网页正文里完全可能出现
    # "[Source: ..." 字样，那不是内部文档检索记录）。没调用过该工具时恒为空列表。
    fetched_doc_ids = []
    for (content, status, failure_kind, _, _), tool_call in zip(results, tool_calls):
        if tool_call["name"] != "internal_doc_search":
            continue
        if status != "success" or not isinstance(content, str):
            continue
        for doc_id in extract_internal_doc_ids(content):
            fetched_doc_ids.append({
                "source": tool_call["name"],
                "doc_id": doc_id,
                "research_topic": research_topic,
            })

    # Create tool messages from execution results
    tool_outputs = [
        ToolMessage(
            content=content,
            name=tool_call["name"],
            tool_call_id=tool_call["id"],
            status=status,
        )
        for (content, status, _, _, _), tool_call in zip(results, tool_calls)
    ]

    # B1 循环检测：按tool_calls原始顺序重新算一遍这一轮的签名追加进历史——
    # ResearchComplete整个排除（第1条审批要求：不参与检测，也不计入历史，
    # 否则会污染其它工具之间的ABAB判断）。截断到max(W, 2*周期数)条，和
    # 上面读快照时算的_loop_history_max_len是同一个值。是否"这一轮有命中"
    # 直接看results里有没有任何一个failure_kind=="loop_detected"——这正是
    # _run_one那个分支返回的标记，不需要另外维护一份"这轮拦截了几次"的
    # 计数。连续命中计数器按"轮"计（不是按单次调用计）：这一轮只要有命中
    # 就+1，没有命中就清零。关闭检测时整段跳过——不计算、不改变
    # recent_tool_calls/loop_detection_consecutive_hits（保持读到的默认
    # 快照原样写回），确保"关闭时行为与之前完全一致"不只是"结果凑巧一样"，
    # 而是真的一行多余计算都没发生。
    if configurable.loop_detection_enabled:
        new_signatures = [
            _call_signature(tc["name"], tc.get("args", {}))
            for tc in tool_calls if tc["name"] != "ResearchComplete"
        ]
        recent_tool_calls = (recent_tool_calls + new_signatures)[-_loop_history_max_len:]
        round_had_loop_hit = any(failure_kind == "loop_detected" for (_, _, failure_kind, _, _) in results)
        loop_detection_consecutive_hits = (loop_detection_consecutive_hits + 1) if round_had_loop_hit else 0

    # Step 3: Check late exit conditions (after processing tools)
    exceeded_iterations = state.get("tool_call_iterations", 0) >= configurable.max_react_tool_calls
    research_complete_called = any(
        tool_call["name"] == "ResearchComplete"
        for tool_call in most_recent_message.tool_calls
    )
    # 方案(b)：模型对方案(a)的提示完全没反应，连续命中达到阈值才强制止损——
    # 不新增独立的state通道，只是把这个条件or进现有的提前退出判断，复用
    # 同一条goto="compress_research"路径。只写trace（reason="force_stop"），
    # 不进入最终报告的披露章节（不是"数据源不可用"，不往degradation_events
    # 里加任何东西）——这一点在已知局限里也会写明。
    loop_force_stop = (
        configurable.loop_detection_enabled
        and loop_detection_consecutive_hits >= configurable.loop_detection_force_stop_after_hits
    )
    if loop_force_stop:
        run_trace.emit("loop_detected", config=config, research_topic=research_topic,
                        reason="force_stop")

    if exceeded_iterations or research_complete_called or loop_force_stop:
        # End research and proceed to compression
        return Command(
            goto="compress_research",
            update={
                "researcher_messages": tool_outputs,
                "tool_failure_counts": tool_failure_counts,
                "degradation_events": degradation_events,
                "fetched_sources": fetched_sources,
                "fetched_doc_ids": fetched_doc_ids,
                "recent_tool_calls": recent_tool_calls,
                "loop_detection_consecutive_hits": loop_detection_consecutive_hits,
            }
        )

    # Continue research loop with tool results
    return Command(
        goto="researcher",
        update={
            "researcher_messages": tool_outputs,
            "tool_failure_counts": tool_failure_counts,
            "degradation_events": degradation_events,
            "fetched_sources": fetched_sources,
            "fetched_doc_ids": fetched_doc_ids,
            "recent_tool_calls": recent_tool_calls,
            "loop_detection_consecutive_hits": loop_detection_consecutive_hits,
        }
    )

async def compress_research(state: ResearcherState, config: RunnableConfig):
    """Compress and synthesize research findings into a concise, structured summary.
    
    This function takes all the research findings, tool outputs, and AI messages from
    a researcher's work and distills them into a clean, comprehensive summary while
    preserving all important information and findings.
    
    Args:
        state: Current researcher state with accumulated research messages
        config: Runtime configuration with compression model settings
        
    Returns:
        Dictionary containing compressed research summary and raw notes
    """
    if _skip_compress_enabled():
        # 快速路径：不调用压缩模型。compressed_research = 各工具输出 + researcher 最后的文字回答；raw_notes 与原实现一致
        raw_notes_content = "\n".join([
            str(message.content)
            for message in filter_messages(state.get("researcher_messages", []), include_types=["tool", "ai"])
        ])
        return {"compressed_research": raw_notes_content, "raw_notes": [raw_notes_content]}

    # Step 1: Configure the compression model
    configurable = Configuration.from_runnable_config(config)
    synthesizer_model = configurable_model.with_config({
        "model": configurable.compression_model,
        "max_tokens": configurable.compression_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.compression_model, config),
        "tags": ["langsmith:nostream"]
    })
    
    # Step 2: Prepare messages for compression
    researcher_messages = state.get("researcher_messages", [])

    
    # Add instruction to switch from research mode to compression mode
    researcher_messages.append(HumanMessage(content=compress_research_simple_human_message))
    
    # Step 3: Attempt compression with retry logic for token limit issues
    synthesis_attempts = 0
    max_attempts = 3
    
    while synthesis_attempts < max_attempts:
        try:
            # Create system prompt focused on compression task
            compression_prompt = compress_research_system_prompt.format(date=get_today_str())
            messages = [SystemMessage(content=compression_prompt)] + researcher_messages
            
            # Execute compression
            response = await synthesizer_model.ainvoke(messages)
            
            # Extract raw notes from all tool and AI messages
            raw_notes_content = "\n".join([
                str(message.content) 
                for message in filter_messages(researcher_messages, include_types=["tool", "ai"])
            ])
            
            # Return successful compression result
            return {
                "compressed_research": str(response.content),
                "raw_notes": [raw_notes_content]
            }
            
        except Exception as e:
            synthesis_attempts += 1
            
            # Handle token limit exceeded by removing older messages
            if is_token_limit_exceeded(e, configurable.research_model):
                researcher_messages = remove_up_to_last_ai_message(researcher_messages)
                continue
            
            # For other errors, continue retrying
            continue
    
    # Step 4: Return error result if all attempts failed
    raw_notes_content = "\n".join([
        str(message.content) 
        for message in filter_messages(researcher_messages, include_types=["tool", "ai"])
    ])
    
    return {
        "compressed_research": "Error synthesizing research report: Maximum retries exceeded",
        "raw_notes": [raw_notes_content]
    }

# Researcher Subgraph Construction
# Creates individual researcher workflow for conducting focused research on specific topics
researcher_builder = StateGraph(
    ResearcherState, 
    output=ResearcherOutputState, 
    config_schema=Configuration
)

# Add researcher nodes for research execution and compression
researcher_builder.add_node("researcher", researcher)                 # Main researcher logic
researcher_builder.add_node("researcher_tools", researcher_tools)     # Tool execution handler
researcher_builder.add_node("compress_research", compress_research)   # Research compression

# Define researcher workflow edges
researcher_builder.add_edge(START, "researcher")           # Entry point to researcher
researcher_builder.add_edge("compress_research", END)      # Exit point after compression

# Compile researcher subgraph for parallel execution by supervisor
researcher_subgraph = researcher_builder.compile()

def _dedupe_degradation_events(degradation_events: list[dict]) -> list[dict]:
    """按 (source, research_topic, reason) 去重，保留第一次出现的那条（含
    timestamp等其它字段，但去重判断本身不看timestamp）。原始事件列表本身
    不受影响，这个函数只用于生成披露小节/提示时的展示，不修改state。"""
    seen = set()
    deduped = []
    for event in degradation_events:
        key = (event.get("source"), event.get("research_topic"), event.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


_DEGRADATION_SECTION_MARKER = "## 数据源可用性说明"

_DEGRADATION_REASON_TEXT = {
    "retries_exhausted": "重试后仍然不可用",
    "circuit_breaker_open": "连续失败后在本次任务中被暂时标记为不可用",
    # 2026-10 修正（审查意见）：outer_timeout 时我们只知道"没在限定时间内
    # 跑完"，不知道这个数据源到底是不可用还是只是慢——不能断言"不可用"，
    # 只能如实描述成"这次调用失败"，具体次数/原因码在 _event_detail_text
    # 里附加说明，不在这里断言任何确定性结论。
    "outer_timeout": "调用失败（未在限定时间内完成，无法确定数据源本身是否可用）",
}


def _event_detail_text(event: dict) -> str:
    """单条事件的措辞。outer_timeout 需要额外注明尝试次数和原因码——
    因为这条reason本身就是"不下确定性断言"，必须把依据（次数未知/原因码）
    摆出来，不能只留一句模糊的话。其余reason已经是确定性结论（真的被
    调用过且失败过），不需要这个额外说明。"""
    reason = event.get("reason")
    reason_text = _DEGRADATION_REASON_TEXT.get(reason, str(reason))
    if reason == "outer_timeout":
        attempts = event.get("attempts")
        attempts_text = "未知" if attempts is None else str(attempts)
        return f"{reason_text}（尝试次数：{attempts_text}，原因码：{reason}）"
    return reason_text


def _format_degradation_disclosure(deduped_events: list[dict]) -> str:
    # 小节导语本身不下任何确定性结论（不说"不可用"）——具体是"确实不可用"
    # 还是"调用失败、无法确定是否可用"，由下面每一条各自的措辞决定
    # （见 _event_detail_text），导语只负责提示"相关结论可能受影响"这件事。
    lines = [f"\n\n{_DEGRADATION_SECTION_MARKER}\n",
             "本次研究过程中，以下数据源出现过异常情况，相关结论可能受此影响（具体情况见各条说明）：\n"]
    for event in deduped_events:
        topic = event.get("research_topic") or "（未指定研究方向）"
        lines.append(f"- **{event.get('source')}**（研究方向：{topic}）：{_event_detail_text(event)}")
    return "\n".join(lines)


def _append_degradation_disclosure(final_report_text: str, degradation_events: list[dict]) -> str:
    """程序兜底追加固定标题小节。幂等：
    - degradation_events 为空 -> 原样返回，不改一个字节。
    - final_report_text 里已经出现过固定标题（_DEGRADATION_SECTION_MARKER）
      -> 原样返回，不重复追加（防止这个函数意外被调用多次时重复累加）。
    去重按 (source, research_topic, reason)；timestamp 不出现在小节里。
    """
    if not degradation_events:
        return final_report_text
    if _DEGRADATION_SECTION_MARKER in final_report_text:
        return final_report_text
    deduped = _dedupe_degradation_events(degradation_events)
    if not deduped:
        return final_report_text
    return final_report_text + _format_degradation_disclosure(deduped)


def _build_degradation_prompt_hint(degradation_events: list[dict]) -> str:
    """提示注入用：生成一段追加进findings的文本，提示模型主动在报告里披露
    数据源限制——这是"尽量让模型自己写得自然"的那一层，不保证模型一定照做，
    真正保证一定出现的是 _append_degradation_disclosure 这个程序化兜底。
    events为空时返回空字符串，不往findings里加任何东西。
    """
    deduped = _dedupe_degradation_events(degradation_events)
    if not deduped:
        return ""
    lines = ["\n\n[系统观测到的数据源可用性事实，请在报告中如实、自然地披露这些限制，"
             "不要忽略也不要淡化]"]
    for event in deduped:
        topic = event.get("research_topic") or "（未指定研究方向）"
        lines.append(f"- 工具 {event.get('source')}（研究方向：{topic}）：{_event_detail_text(event)}")
    return "\n".join(lines)


GROUNDING_GUARD_ENV_VAR = "DEEP_RESEARCH_GROUNDING_GUARD"
GROUNDING_GUARD_MARKER = "[数据依据硬性要求]"


def _grounding_guard_enabled() -> bool:
    return os.environ.get(GROUNDING_GUARD_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def _build_grounding_prompt_hint() -> str:
    """数据依据约束（默认关闭，DEEP_RESEARCH_GROUNDING_GUARD=1 才追加进 findings）。
    动机：评测中观察到工具调用额度用尽、关键 SQL 从未成功执行，最终报告却写出库里不存在的轮次/金额/日期，
    并把未执行的查询列为已执行。这只是提示词层的约束，不保证模型照做，效果以评测对比为准。
    关闭时返回空字符串，findings 与改动前逐字节一致。"""
    if not _grounding_guard_enabled():
        return ""
    return (f"\n\n{GROUNDING_GUARD_MARKER}报告中的每个具体数字、日期、轮次、名称，必须出现在上面研究笔记里工具的实际返回结果中。"
            "如果笔记显示某次查询报错、被拒绝或没有执行，就必须在报告里明确写“未取得该数据”，"
            "不得根据常识、上下文或其他数据补全或推测；不得把没有成功执行的查询写成已执行。"
            "宁可回答“数据库中没有查到”，也不要给出笔记里没有的数值。")


_CITATION_VERIFICATION_SECTION_MARKER = "## 引用可追溯性核验"


def _format_citation_verification_disclosure(verification: dict) -> str:
    """只标记，不删除——citation_verification.verify_citations是纯离线的
    结构化比对，这里只是把它的invalid部分渲染成文字，不改final_report正文
    里已经写好的任何一句话。

    2026-10 修正（审查意见）：只渲染invalid，不渲染missing。
    - 措辞改成中性的"未能匹配到"，不写"可能是生成报告时产生的错误"——
      我们的匹配规则本身是保守的结构化比对（见normalize_url的文档字符串：
      "归一化不掉的差异就判定为不同URL，不做启发式近似匹配"），一个URL被
      判成invalid，更可能是匹配规则本身比较严格导致的false positive，
      不应该直接断言"这是模型编造的错误"。
    - missing（真实检索到但报告没引用）不在最终报告里展示——verify_citations
      的返回值本身仍然完整计算并保留missing（供评测/日志等其它消费方用），
      只是"要不要把missing写进报告正文给读者看"这件事本身，和"报告里有没有
      不可追溯的引用"是两个不同严重程度的问题，这里只改渲染条件，不改
      verify_citations的计算/返回结构。
    """
    if not verification["invalid"]:
        return ""
    lines = [
        f"\n\n{_CITATION_VERIFICATION_SECTION_MARKER}\n",
        "以下引用链接未能在本次检索记录中匹配到（匹配规则较保守，请自行核实）：\n",
    ]
    for url in verification["invalid"]:
        lines.append(f"- {url}")
    return "\n".join(lines)


def _append_citation_verification_disclosure(final_report_text: str, fetched_sources: list[dict]) -> str:
    """程序兜底追加固定标题小节。幂等：
    - fetched_sources 为空 -> 原样返回，不改一个字节——没有任何真实检索
      记录可做比对依据时，不应该产出任何可能误导的结论。
    - final_report_text 里已经出现过固定标题 -> 原样返回，不重复追加。
    - verify_citations 结果里 invalid 为空 -> 原样返回，不追加任何小节
      （2026-10修正：只看invalid，不再看missing——"存在未引用的已检索来源"
      不足以让最终报告多一段披露，只有"报告里有链接匹配不到检索记录"才值得
      提醒读者；missing仍然被verify_citations完整计算并返回，只是不写进
      最终报告正文）。
    """
    if not fetched_sources:
        return final_report_text
    if _CITATION_VERIFICATION_SECTION_MARKER in final_report_text:
        return final_report_text
    verification = verify_citations(final_report_text, fetched_sources)
    if not verification["invalid"]:
        return final_report_text
    return final_report_text + _format_citation_verification_disclosure(verification)


_DOC_ID_CITATION_SECTION_MARKER = "## 内部文档引用核验"


def _report_body_for_doc_verification(final_report_text: str) -> str:
    """内部文档核验只看报告正文，不看前面已经追加的披露小节——其它小节里
    可能出现的 URL（路径里碰巧含 docNN_ 之类）或工具名不应被当成 doc_id 引用。
    """
    cut = len(final_report_text)
    for marker in (_DEGRADATION_SECTION_MARKER, _CITATION_VERIFICATION_SECTION_MARKER):
        idx = final_report_text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return final_report_text[:cut]


def _format_doc_id_citation_disclosure(invalid_ids: list[str]) -> str:
    """只渲染 invalid，措辞中性（与 URL 核验一致）：doc_id 的提取规则本身比
    URL 弱（依赖固定输出格式与知识库命名约定），不能断言“这是模型编造的”。
    """
    lines = [
        f"\n\n{_DOC_ID_CITATION_SECTION_MARKER}\n",
        "以下内部文档引用未能在本次检索记录中匹配到（匹配规则较保守，请自行核实）：\n",
    ]
    for doc_id in invalid_ids:
        lines.append(f"- {doc_id}")
    return "\n".join(lines)


def _append_doc_id_citation_disclosure(final_report_text: str, fetched_doc_ids: list[dict]) -> str:
    """程序兜底追加固定标题小节，幂等：
    - fetched_doc_ids 为空（本次研究没成功调用过 internal_doc_search）-> 原样返回，
      不改一个字节，行为与未引入 T3b 时完全一致；
    - 报告里已经出现过固定标题 -> 原样返回，不重复追加；
    - 没有 invalid -> 原样返回（missing 只存在于 verify_doc_id_citations 的返回值）。
    只证明“被引用的 doc_id 本次确实被检索到过”，不证明文档内容支持报告结论。
    """
    if not fetched_doc_ids:
        return final_report_text
    if _DOC_ID_CITATION_SECTION_MARKER in final_report_text:
        return final_report_text
    verification = verify_doc_id_citations(
        _report_body_for_doc_verification(final_report_text), fetched_doc_ids
    )
    if not verification["invalid"]:
        return final_report_text
    return final_report_text + _format_doc_id_citation_disclosure(verification["invalid"])



def _direct_research_enabled() -> bool:
    """单研究员直连模式（实验性，默认关闭）：ODR_DIRECT_RESEARCH=1 时跳过 supervisor，
    由一个 researcher 直接处理研究简报。动机：单库查询类问题经 supervisor 来回约 3 次模型调用，
    而这类问题不需要并行拆解（EVIDENCE §37）。不适合需要并行多方向网络调研的任务。"""
    return os.environ.get("ODR_DIRECT_RESEARCH", "0") == "1"


async def direct_research(state: AgentState, config: RunnableConfig):
    """跳过 supervisor：把研究简报原样交给一个 researcher，结果以与 supervisor_tools 相同的方式写入状态。"""
    brief = state.get("research_brief", "") or ""
    cfg = config
    if run_trace.is_enabled(config):
        cfg = {**config, "configurable": {**config.get("configurable", {}),
                                          "run_trace_researcher_id": run_trace.new_id()}}
    try:
        obs = await researcher_subgraph.ainvoke(
            {"researcher_messages": [HumanMessage(content=brief)], "research_topic": brief}, cfg)
    except Exception:
        return {"notes": ["Error synthesizing research report: Maximum retries exceeded"],
                "research_brief": brief}
    update = {"notes": [obs.get("compressed_research", "Error synthesizing research report: Maximum retries exceeded")],
              "research_brief": brief}
    raw = "\n".join(obs.get("raw_notes", []))
    if raw:
        update["raw_notes"] = [raw]
    if obs.get("degradation_events"):
        update["degradation_events"] = list(obs["degradation_events"])
    if obs.get("fetched_sources"):
        update["fetched_sources"] = list(obs["fetched_sources"])
    if obs.get("fetched_doc_ids"):
        update["fetched_doc_ids"] = list(obs["fetched_doc_ids"])
    return update


async def final_report_generation(state: AgentState, config: RunnableConfig):
    """Generate the final comprehensive research report with retry logic for token limits.

    This function takes all collected research findings and synthesizes them into a
    well-structured, comprehensive final report using the configured report generation model.

    Args:
        state: Agent state containing research findings and context
        config: Runtime configuration with model settings and API keys

    Returns:
        Dictionary containing the final report and cleared state
    """
    # Step 1: Extract research findings and prepare state cleanup
    notes = state.get("notes", [])
    cleared_state = {"notes": {"type": "override", "value": []}}
    degradation_events = state.get("degradation_events", [])
    findings = ("\n".join(notes) + _build_degradation_prompt_hint(degradation_events)
                + _build_grounding_prompt_hint()
                + build_write_request_hint(state.get("messages", [])))

    # Step 2: Configure the final report generation model
    configurable = Configuration.from_runnable_config(config)
    writer_model_config = {
        "model": configurable.final_report_model,
        "max_tokens": configurable.final_report_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.final_report_model, config),
        "tags": ["langsmith:nostream"]
    }

    # Step 3: Attempt report generation with token limit retry logic
    max_retries = 3
    current_retry = 0
    findings_token_limit = None
    result = None  # 2026-10 T3：不再在循环内部直接return，统一收集到这里，
    # 循环结束后只在一个地方对 final_report 文本做一次披露小节追加
    # （approved design: "重试循环之后，对最终字符串做一次"）。

    while current_retry <= max_retries:
        try:
            # Create comprehensive prompt with all research context
            final_report_prompt = final_report_generation_prompt.format(
                research_brief=state.get("research_brief", ""),
                messages=get_buffer_string(state.get("messages", [])),
                findings=findings,
                date=get_today_str()
            )

            # Generate the final report
            final_report = await configurable_model.with_config(writer_model_config).ainvoke([
                HumanMessage(content=final_report_prompt)
            ])

            result = {
                "final_report": final_report.content,
                "messages": [final_report],
                **cleared_state
            }
            break

        except Exception as e:
            # Handle token limit exceeded errors with progressive truncation
            if is_token_limit_exceeded(e, configurable.final_report_model):
                current_retry += 1

                if current_retry == 1:
                    # First retry: determine initial truncation limit
                    model_token_limit = get_model_token_limit(configurable.final_report_model)
                    if not model_token_limit:
                        result = {
                            "final_report": f"Error generating final report: Token limit exceeded, however, we could not determine the model's maximum context length. Please update the model map in deep_researcher/utils.py with this information. {e}",
                            "messages": [AIMessage(content="Report generation failed due to token limits")],
                            **cleared_state
                        }
                        break
                    # Use 4x token limit as character approximation for truncation
                    findings_token_limit = model_token_limit * 4
                else:
                    # Subsequent retries: reduce by 10% each time
                    findings_token_limit = int(findings_token_limit * 0.9)

                # Truncate findings and retry
                findings = findings[:findings_token_limit]
                continue
            else:
                # Non-token-limit error: return error immediately
                result = {
                    "final_report": f"Error generating final report: {e}",
                    "messages": [AIMessage(content="Report generation failed due to an error")],
                    **cleared_state
                }
                break

    if result is None:
        # Step 4: Return failure result if all retries exhausted
        result = {
            "final_report": "Error generating final report: Maximum retries exceeded",
            "messages": [AIMessage(content="Report generation failed after maximum retries")],
            **cleared_state
        }

    # 写请求场景：确定性移除报告里的写语句（ODR_WRITE_SQL_FILTER=1 才启用，默认关闭；EVIDENCE §36）
    if isinstance(result.get("final_report"), str):
        _is_write = any(detect_write_request(t) for t in _human_texts(state.get("messages", [])))
        result["final_report"] = filter_report_for_write_request(result["final_report"], _is_write)

    # 重试循环之后，对最终字符串做一次披露小节追加——幂等，events为空或
    # 小节已存在时原样返回，见 _append_degradation_disclosure 的文档字符串。
    result["final_report"] = _append_degradation_disclosure(
        result["final_report"], degradation_events
    )
    # 同样在重试循环之后、只做一次：引用可追溯性核验，纯离线比对，
    # 只标记不删除，见 _append_citation_verification_disclosure 的文档字符串。
    fetched_sources = state.get("fetched_sources", [])
    result["final_report"] = _append_citation_verification_disclosure(
        result["final_report"], fetched_sources
    )
    # 2026-10 T3b：内部文档（internal_doc_search）引用核验，同样只做一次、
    # 幂等、只标记不删除。fetched_doc_ids 为空时原样返回。
    fetched_doc_ids = state.get("fetched_doc_ids", [])
    result["final_report"] = _append_doc_id_citation_disclosure(
        result["final_report"], fetched_doc_ids
    )
    return result

# Main Deep Researcher Graph Construction
# Creates the complete deep research workflow from user input to final report
deep_researcher_builder = StateGraph(
    AgentState, 
    input=AgentInputState, 
    config_schema=Configuration
)

# Add main workflow nodes for the complete research process
deep_researcher_builder.add_node("clarify_with_user", clarify_with_user)           # User clarification phase
deep_researcher_builder.add_node("write_research_brief", write_research_brief)     # Research planning phase
deep_researcher_builder.add_node("research_supervisor", supervisor_subgraph)       # Research execution phase
deep_researcher_builder.add_node("direct_research", direct_research)                # 单研究员直连（ODR_DIRECT_RESEARCH=1，默认不走）
deep_researcher_builder.add_node("final_report_generation", final_report_generation)  # Report generation phase

# Define main workflow edges for sequential execution
deep_researcher_builder.add_edge(START, "clarify_with_user")                       # Entry point
deep_researcher_builder.add_edge("research_supervisor", "final_report_generation") # Research to report
deep_researcher_builder.add_edge("direct_research", "final_report_generation")      # 直连模式的出口
deep_researcher_builder.add_edge("final_report_generation", END)                   # Final exit point

# Compile the complete deep researcher workflow
deep_researcher = deep_researcher_builder.compile()