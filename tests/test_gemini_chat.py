"""Thought-signature round trip in GeminiChatOpenAI (no network calls).

Response shapes are copied from a real Gemini tool call. Gemini answers 400 on the
step after a tool call if the signature is missing, so losing it breaks all tool use.
"""

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    ToolMessage,
    convert_to_messages,
)
from pydantic import SecretStr

from app.services.llm.gemini_chat import GeminiChatOpenAI

SIGNATURE = {"google": {"thought_signature": "EmAKXgFpFH0T"}}
TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'},
    "extra_content": SIGNATURE,
}


def make_model() -> GeminiChatOpenAI:
    return GeminiChatOpenAI(model="gemini-3.5-flash-lite", api_key=SecretStr("test"), base_url="http://unused")


def ai_message_from_response() -> AIMessage:
    response = {
        "id": "r1",
        "model": "gemini-3.5-flash-lite",
        "object": "chat.completion",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
            }
        ],
    }
    message = make_model()._create_chat_result(response).generations[0].message
    assert isinstance(message, AIMessage)
    return message


def request_tool_calls(ai_message: AIMessage) -> list[dict]:
    messages = [
        HumanMessage("weather?"),
        ai_message,
        ToolMessage(content="sunny", tool_call_id="call_1", name="get_weather"),
    ]
    payload = make_model()._get_request_payload(messages)
    return payload["messages"][1]["tool_calls"]


def test_signature_from_response_is_sent_back():
    assert request_tool_calls(ai_message_from_response())[0]["extra_content"] == SIGNATURE


def test_signature_survives_the_apps_dump_and_convert_path():
    # graph.py dumps messages to dicts in prepare_messages and again before the call.
    once = convert_to_messages([ai_message_from_response().model_dump()])[0]
    twice = convert_to_messages([once.model_dump()])[0]
    assert isinstance(twice, AIMessage)
    assert request_tool_calls(twice)[0]["extra_content"] == SIGNATURE


def test_signature_from_stream_chunks_is_sent_back():
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{**TOOL_CALL, "index": 0}],
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "tool_calls"}]},
    ]
    model = make_model()
    generated = [model._convert_chunk_to_generation_chunk(c, AIMessageChunk, None) for c in chunks]
    merged = generated[0].message + generated[1].message  # pyright: ignore[reportOptionalMemberAccess]
    assert isinstance(merged, AIMessageChunk)
    assert (
        request_tool_calls(AIMessage(**merged.model_dump(exclude={"type", "tool_call_chunks"})))[0]["extra_content"]
        == SIGNATURE
    )


def test_thread_saved_without_signature_gets_skip_validator():
    old = AIMessage(content="", tool_calls=[{"name": "get_weather", "args": {}, "id": "call_1", "type": "tool_call"}])
    extra = request_tool_calls(old)[0]["extra_content"]
    assert extra["google"]["thought_signature"] == "skip_thought_signature_validator"


def test_parallel_calls_keep_signature_on_first_call_only():
    calls = [{"name": "t", "args": {}, "id": f"call_{i}", "type": "tool_call"} for i in (1, 2)]
    message = AIMessage(
        content="", tool_calls=calls, additional_kwargs={"tool_call_extra_content": {"call_1": SIGNATURE}}
    )
    payload = make_model()._get_request_payload([HumanMessage("hi"), message])
    first, second = payload["messages"][1]["tool_calls"]
    assert first["extra_content"] == SIGNATURE
    assert "extra_content" not in second


def test_plain_text_request_has_no_extra_content():
    payload = make_model()._get_request_payload([HumanMessage("hi"), AIMessage(content="hello")])
    assert "tool_calls" not in payload["messages"][1]
