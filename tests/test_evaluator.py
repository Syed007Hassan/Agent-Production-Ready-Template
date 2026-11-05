"""Evaluator behaviour against a stub judge and a stub Langfuse (no network calls)."""

import asyncio
from types import SimpleNamespace
from typing import (
    Any,
    List,
)

import pytest

from app.core.config import settings
from evals.evaluator import Evaluator
from evals.metrics import metrics
from evals.schemas import ScoreSchema
from tests.fakes import (
    daily_429,
    minute_429,
    server_500,
)


class StubJudge:
    """Stands in for ``client.beta.chat.completions``: replays a script of errors and scores."""

    def __init__(self, script: List[Any]):
        self.script = list(script)
        self.calls = 0

    async def parse(self, **_: Any) -> SimpleNamespace:
        self.calls += 1
        step = self.script.pop(0) if self.script else 0.9
        if isinstance(step, Exception):
            raise step
        parsed = ScoreSchema(score=step, reasoning="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])


class StubLangfuse:
    def __init__(self):
        self.scores: List[dict] = []

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed = True


def make_trace(trace_id: str) -> SimpleNamespace:
    messages = [{"type": "human", "content": "hi"}, {"type": "ai", "content": "hello"}]
    return SimpleNamespace(id=trace_id, output={"messages": messages}, scores=[])


def make_evaluator(monkeypatch: pytest.MonkeyPatch, judge: StubJudge, trace_count: int) -> Evaluator:
    monkeypatch.setattr(settings, "EVALUATION_MAX_RPM", 60_000)  # 1ms spacing keeps tests fast
    evaluator = Evaluator()
    evaluator.client = SimpleNamespace(beta=SimpleNamespace(chat=SimpleNamespace(completions=judge)))  # pyright: ignore[reportAttributeAccessIssue]
    evaluator.langfuse = StubLangfuse()  # pyright: ignore[reportAttributeAccessIssue]
    traces = [make_trace(f"trace-{i}") for i in range(trace_count)]

    async def fetch() -> list:
        return traces

    monkeypatch.setattr(evaluator, "_fetch_traces", fetch)
    return evaluator


def run(evaluator: Evaluator) -> dict:
    asyncio.run(evaluator.run(generate_report_file=False))
    return evaluator.report


def test_scores_every_metric_and_pushes_to_langfuse(monkeypatch):
    evaluator = make_evaluator(monkeypatch, StubJudge([]), trace_count=2)

    report = run(evaluator)
    assert report["successful_traces"] == 2
    assert len(evaluator.langfuse.scores) == 2 * len(metrics)  # pyright: ignore[reportAttributeAccessIssue]
    assert not report["stopped_early"]


def test_daily_quota_stops_run_without_half_scoring_a_trace(monkeypatch):
    # First trace scores fully; the judge's daily quota runs out on the second metric of trace 2.
    script = [0.9] * len(metrics) + [0.9, daily_429()]
    evaluator = make_evaluator(monkeypatch, StubJudge(script), trace_count=3)

    report = run(evaluator)
    assert report["stopped_early"]
    assert report["total_traces"] == 1
    assert report["traces_remaining"] == 2
    pushed = {score["trace_id"] for score in evaluator.langfuse.scores}  # pyright: ignore[reportAttributeAccessIssue]
    assert pushed == {"trace-0"}  # trace-1 stays unscored, so the next run picks it up again


def test_minute_quota_waits_and_retries_same_judge(monkeypatch):
    judge = StubJudge([minute_429(retry_delay="0s")])
    evaluator = make_evaluator(monkeypatch, judge, trace_count=1)

    report = run(evaluator)
    assert report["successful_traces"] == 1
    assert judge.calls == len(metrics) + 1


def test_trace_with_a_failed_metric_pushes_nothing_so_the_next_run_retries_it(monkeypatch):
    judge = StubJudge([server_500(), server_500(), server_500()])
    monkeypatch.setattr("evals.evaluator._backoff", lambda _: 0)
    evaluator = make_evaluator(monkeypatch, judge, trace_count=1)

    report = run(evaluator)
    assert report["failed_traces"] == 1
    assert not report["stopped_early"]
    assert evaluator.langfuse.scores == []  # pyright: ignore[reportAttributeAccessIssue]


def test_scores_are_flushed_before_the_run_ends(monkeypatch):
    evaluator = make_evaluator(monkeypatch, StubJudge([]), trace_count=1)
    run(evaluator)
    assert evaluator.langfuse.flushed  # pyright: ignore[reportAttributeAccessIssue]


def test_trace_with_no_text_output_spends_no_judge_calls(monkeypatch):
    judge = StubJudge([])
    evaluator = make_evaluator(monkeypatch, judge, trace_count=1)
    trace = SimpleNamespace(
        id="t", output={"messages": [{"type": "human", "content": "hi"}, {"type": "ai", "content": ""}]}
    )

    async def fetch() -> list:
        return [trace]

    monkeypatch.setattr(evaluator, "_fetch_traces", fetch)
    report = run(evaluator)
    assert judge.calls == 0
    assert report["failed_traces"] == 1


def test_throttle_spaces_calls_under_the_rpm_limit(monkeypatch):
    monkeypatch.setattr(settings, "EVALUATION_MAX_RPM", 12)
    assert Evaluator()._min_call_interval == pytest.approx(5.0)
