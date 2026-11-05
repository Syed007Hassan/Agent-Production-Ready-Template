"""ChatOpenAI subclass that round-trips Gemini's thought signatures.

Gemini returns ``extra_content.google.thought_signature`` on tool calls and rejects
the next request with a 400 unless it is sent back. ChatOpenAI drops the field, which
breaks the step after every tool call, so this class keeps it in ``additional_kwargs``
(which the checkpointer persists) and restores it when building the request.
"""

from typing import (
    Any,
    Dict,
    Mapping,
    override,
)

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.outputs import (
    ChatGenerationChunk,
    ChatResult,
)
from langchain_openai import ChatOpenAI
from openai import BaseModel as OpenAIBaseModel

from app.core.logging import logger

_EXTRA_CONTENT_KEY = "tool_call_extra_content"
# Google's documented value for tool calls with no real signature, e.g. threads saved before
# signatures were kept. Validation passes, so a stored thread can't stay broken forever.
_SKIP_VALIDATOR_EXTRA_CONTENT = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def _extra_content_by_tool_call_id(raw_message: Mapping[str, Any] | None) -> Dict[str, Any]:
    tool_calls = (raw_message or {}).get("tool_calls") or []
    return {call["id"]: call["extra_content"] for call in tool_calls if call.get("id") and call.get("extra_content")}


class GeminiChatOpenAI(ChatOpenAI):
    """ChatOpenAI for Gemini's OpenAI-compatible endpoint."""

    @override
    def _create_chat_result(
        self,
        response: dict | OpenAIBaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        """Keep each tool call's extra_content from a non-streaming response."""
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw.get("choices") or [], strict=False):
            extra_content = _extra_content_by_tool_call_id(choice.get("message"))
            if extra_content:
                generation.message.additional_kwargs[_EXTRA_CONTENT_KEY] = extra_content
        return result

    @override
    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        """Keep each tool call's extra_content from a streamed chunk.

        LangGraph's "messages" stream mode makes the model stream even under ``ainvoke``,
        and LangChain merges ``additional_kwargs`` when it joins the chunks back together.
        """
        generation = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices") or []
        if generation is not None and choices:
            extra_content = _extra_content_by_tool_call_id(choices[0].get("delta"))
            if extra_content:
                generation.message.additional_kwargs[_EXTRA_CONTENT_KEY] = extra_content
        return generation

    @override
    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Restore saved extra_content onto the assistant tool calls in the request."""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        for message, payload_message in zip(messages, payload.get("messages", []), strict=False):
            tool_calls = payload_message.get("tool_calls")
            if not isinstance(message, AIMessage) or not tool_calls:
                continue
            saved = message.additional_kwargs.get(_EXTRA_CONTENT_KEY) or {}
            for tool_call in tool_calls:
                if tool_call["id"] in saved:
                    tool_call["extra_content"] = saved[tool_call["id"]]
            # In a parallel set only the first call carries a signature, so one is enough.
            if not saved:
                logger.warning("thought_signature_missing_using_skip_validator", tool_call_id=tool_calls[0]["id"])
                tool_calls[0]["extra_content"] = _SKIP_VALIDATOR_EXTRA_CONTENT
        return payload
