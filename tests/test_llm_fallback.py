"""Fallback behaviour of LLMService against stub models (no network calls).

The stubs raise the real OpenAI-SDK exception types built in ``tests.fakes``.
"""

import asyncio
import time
from typing import (
    Any,
    List,
)

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
)
from openai import (
    APITimeoutError,
    AuthenticationError,
)

from app.services.llm import (
    LLMRegistry,
    LLMService,
    LLMUnavailableError,
)
from app.services.llm import availability as availability_module
from app.services.llm.availability import (
    FailureKind,
    ModelAvailability,
    classify_error,
    next_daily_reset,
)
from tests.fakes import (
    REQUEST,
    auth_401,
    daily_429,
    minute_429,
    server_500,
)

PRIMARY, SECOND, THIRD = "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"


class StubModel:
    """Fake tool-bound model: raises the queued errors in order, then answers."""

    def __init__(self, name: str, errors: List[Exception] | None = None):
        self.name = name
        self.errors = list(errors or [])
        self.calls = 0

    async def ainvoke(self, messages: Any) -> AIMessage:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return AIMessage(content=f"answer from {self.name}")


@pytest.fixture()
def availability(monkeypatch: pytest.MonkeyPatch) -> ModelAvailability:
    """Fresh quota state per test (the module-level tracker is process-wide)."""
    fresh = ModelAvailability()
    monkeypatch.setattr("app.services.llm.service.model_availability", fresh)
    return fresh


def make_service(errors_by_model: dict[str, List[Exception]]) -> tuple[LLMService, dict[str, StubModel]]:
    """Build a service whose default chain is backed by stub models."""
    service = LLMService()
    names = LLMRegistry.get_all_names()
    stubs = {}
    for idx in LLMRegistry.get_fallback_chain(service._default_index):
        name = names[idx]
        stubs[name] = StubModel(name, errors_by_model.get(name))
        service._bound_llms[idx] = stubs[name]
    return service, stubs


def ask(service: LLMService) -> Any:
    return asyncio.run(service.call([HumanMessage("hi")]))


# --- classification -------------------------------------------------------


def test_classifies_minute_and_daily_quota_from_gemini_body():
    assert classify_error(minute_429()) is FailureKind.MINUTE_QUOTA
    assert classify_error(daily_429()) is FailureKind.DAILY_QUOTA


def test_classifies_timeout_before_connection_error():
    # APITimeoutError subclasses APIConnectionError; it must not be treated as a retryable 5xx.
    assert classify_error(APITimeoutError(request=REQUEST)) is FailureKind.TIMEOUT
    assert classify_error(server_500()) is FailureKind.SERVER_ERROR
    assert classify_error(auth_401()) is FailureKind.AUTH


def test_daily_reset_is_next_midnight_pacific():
    reset = next_daily_reset()
    assert (reset.hour, reset.minute) == (0, 0)
    assert 0 < reset.timestamp() - time.time() <= 25 * 3600


# --- fallback loop --------------------------------------------------------


def test_minute_quota_falls_back_without_retrying_and_blocks_model(availability):
    service, stubs = make_service({PRIMARY: [minute_429()]})

    assert ask(service).content == f"answer from {SECOND}"
    assert stubs[PRIMARY].calls == 1  # no same-model retry on 429
    assert not availability.is_available(PRIMARY)

    # Next request skips the blocked model entirely instead of paying for another 429.
    ask(service)
    assert stubs[PRIMARY].calls == 1


def test_fallback_is_not_sticky_once_block_expires(availability, monkeypatch):
    service, stubs = make_service({PRIMARY: [minute_429()]})
    ask(service)

    an_hour_later = time.time() + 3600
    monkeypatch.setattr(availability_module.time, "time", lambda: an_hour_later)
    assert ask(service).content == f"answer from {PRIMARY}"


def test_server_error_retries_same_model_then_succeeds(availability):
    service, stubs = make_service({PRIMARY: [server_500()]})

    assert ask(service).content == f"answer from {PRIMARY}"
    assert stubs[PRIMARY].calls == 2
    assert availability.is_available(PRIMARY)  # transient errors never block a model


def test_timeout_moves_on_without_retry_or_block(availability):
    service, stubs = make_service({PRIMARY: [APITimeoutError(request=REQUEST)]})

    assert ask(service).content == f"answer from {SECOND}"
    assert stubs[PRIMARY].calls == 1
    assert availability.is_available(PRIMARY)


def test_auth_error_stops_immediately(availability):
    service, stubs = make_service({PRIMARY: [auth_401()]})

    with pytest.raises(AuthenticationError):
        ask(service)
    assert stubs[SECOND].calls == 0


def test_all_models_out_of_daily_quota_raises_graceful_error(availability):
    service, stubs = make_service({name: [daily_429()] for name in (PRIMARY, SECOND, THIRD)})

    with pytest.raises(LLMUnavailableError) as excinfo:
        ask(service)
    assert excinfo.value.quota_exhausted
    assert excinfo.value.retry_after_seconds and excinfo.value.retry_after_seconds <= 25 * 3600
    assert "usage limit" in excinfo.value.user_message

    # Once exhausted, later requests fail fast without calling any model.
    with pytest.raises(LLMUnavailableError):
        ask(service)
    assert [stub.calls for stub in stubs.values()] == [1, 1, 1]


def test_outage_is_reported_as_unavailable_not_quota(availability):
    service, _ = make_service({name: [APITimeoutError(request=REQUEST)] for name in (PRIMARY, SECOND, THIRD)})

    with pytest.raises(LLMUnavailableError) as excinfo:
        ask(service)
    assert not excinfo.value.quota_exhausted
    assert excinfo.value.retry_after_seconds is None
