"""A failed LLM call must not swallow the user's next message (no network, no database)."""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import (
    END,
    StateGraph,
)

from app.core.langgraph import graph as graph_module
from app.core.langgraph.graph import LangGraphAgent
from app.schemas import (
    GraphState,
    Message,
)
from app.services.llm import LLMUnavailableError


def build_agent(monkeypatch: pytest.MonkeyPatch, replies: list[Any], seen: list[list[Any]]) -> LangGraphAgent:
    async def fake_call(messages: Any) -> Any:
        seen.append(messages)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def no_memory(*_: Any) -> str:
        return ""

    agent = LangGraphAgent()
    monkeypatch.setattr(agent.llm_service, "call", fake_call)
    monkeypatch.setattr(graph_module.memory_service, "search", no_memory)
    monkeypatch.setattr(graph_module.memory_service, "add", no_memory)

    builder = StateGraph(GraphState)
    builder.add_node("chat", agent._chat, destinations=("tool_call", END))
    builder.add_node("tool_call", agent._tool_call, destinations=("chat",))
    builder.set_entry_point("chat")
    agent._graph = builder.compile(checkpointer=InMemorySaver())
    return agent


def test_message_after_failed_llm_call_is_answered_not_dropped(monkeypatch):
    seen: list[list[Any]] = []
    replies = [
        AIMessage(content="", tool_calls=[{"name": "nope", "args": {}, "id": "c1", "type": "tool_call"}]),
        LLMUnavailableError("all models down"),
        AIMessage(content="working"),
    ]
    agent = build_agent(monkeypatch, replies, seen)
    agent.tools_by_name["nope"] = type("T", (), {"ainvoke": staticmethod(lambda _: _tool_result())})()

    async def scenario() -> list[Message]:
        with pytest.raises(LLMUnavailableError):
            await agent.get_response([Message(role="user", content="first question")], "thread-1")
        return await agent.get_response([Message(role="user", content="second question")], "thread-1")

    result = asyncio.run(scenario())
    assert result[-1].content == "working"
    assert "second question" in str(seen[-1])  # the retry path used to send only the old messages


async def _tool_result() -> str:
    return "tool output"


def test_human_in_the_loop_still_resumes():
    pending_tool = type("S", (), {"next": ("tool_call",)})()
    finished = type("S", (), {"next": ()})()
    failed_llm = type("S", (), {"next": ("chat",)})()
    assert graph_module._should_resume(pending_tool)
    assert not graph_module._should_resume(finished)
    assert not graph_module._should_resume(failed_llm)
