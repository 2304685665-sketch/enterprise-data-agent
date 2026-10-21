import asyncio, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from open_deep_research import deep_researcher as dr


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("ODR_DIRECT_RESEARCH", raising=False)
    assert dr._direct_research_enabled() is False
    monkeypatch.setenv("ODR_DIRECT_RESEARCH", "1")
    assert dr._direct_research_enabled() is True


def test_graph_has_direct_node_and_edge():
    g = dr.deep_researcher.get_graph()
    names = set(g.nodes)
    assert {"direct_research", "research_supervisor", "final_report_generation"} <= names
    edges = {(e.source, e.target) for e in g.edges}
    assert ("direct_research", "final_report_generation") in edges


def test_direct_research_maps_state_like_supervisor(monkeypatch):
    seen = {}

    async def fake_ainvoke(inp, cfg):
        seen["topic"] = inp["research_topic"]
        return {"compressed_research": "ANSWER", "raw_notes": ["a", "b"],
                "degradation_events": [{"k": 1}], "fetched_sources": [{"u": 1}], "fetched_doc_ids": [{"d": 1}]}

    monkeypatch.setattr(dr.researcher_subgraph, "ainvoke", fake_ainvoke)
    out = asyncio.run(dr.direct_research({"research_brief": "BRIEF"}, {"configurable": {}}))
    assert seen["topic"] == "BRIEF"
    assert out["notes"] == ["ANSWER"] and out["research_brief"] == "BRIEF"
    assert out["raw_notes"] == ["a\nb"] and out["degradation_events"] == [{"k": 1}]
    assert out["fetched_sources"] == [{"u": 1}] and out["fetched_doc_ids"] == [{"d": 1}]


def test_direct_research_failure_degrades(monkeypatch):
    async def boom(inp, cfg):
        raise RuntimeError("x")
    monkeypatch.setattr(dr.researcher_subgraph, "ainvoke", boom)
    out = asyncio.run(dr.direct_research({"research_brief": "B"}, {"configurable": {}}))
    assert "Error synthesizing" in out["notes"][0]


def test_skip_flags_default_off(monkeypatch):
    monkeypatch.delenv("ODR_SKIP_BRIEF", raising=False); monkeypatch.delenv("ODR_SKIP_COMPRESS", raising=False)
    assert dr._skip_brief_enabled() is False and dr._skip_compress_enabled() is False
    monkeypatch.setenv("ODR_SKIP_BRIEF", "1"); monkeypatch.setenv("ODR_SKIP_COMPRESS", "1")
    assert dr._skip_brief_enabled() is True and dr._skip_compress_enabled() is True


def test_last_human_text():
    from langchain_core.messages import HumanMessage, AIMessage
    msgs = [HumanMessage(content="first"), AIMessage(content="x"), HumanMessage(content="second")]
    assert dr._last_human_text(msgs) == "second" and dr._last_human_text([]) == ""


def test_skip_compress_makes_no_model_call(monkeypatch):
    from langchain_core.messages import ToolMessage, AIMessage
    monkeypatch.setenv("ODR_SKIP_COMPRESS", "1")

    class Boom:
        def with_config(self, *_a, **_k): raise AssertionError("compression model must not be built")
    monkeypatch.setattr(dr, "configurable_model", Boom())
    state = {"researcher_messages": [ToolMessage(content="ROWS", tool_call_id="1"), AIMessage(content="DONE")]}
    out = asyncio.run(dr.compress_research(state, {"configurable": {}}))
    assert out["compressed_research"] == "ROWS\nDONE" and out["raw_notes"] == ["ROWS\nDONE"]
