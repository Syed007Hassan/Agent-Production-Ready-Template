"""Reading evaluation candidates from Langfuse, against a fake HTTP server (no network)."""

import asyncio
import json
from typing import (
    Callable,
    Dict,
    List,
)

import httpx
import pytest

from evals.traces import fetch_unscored_traces

BASE = "https://langfuse.test"
METRICS = {"helpfulness", "toxicity"}


def run_output(text: str) -> str:
    # Real root observations store the run's final state as a JSON string.
    return json.dumps({"messages": [{"type": "human", "content": "q"}, {"type": "ai", "content": text}]})


def observation(trace_id: str, output: object) -> Dict[str, object]:
    return {"traceId": trace_id, "output": output, "isRootObservation": True}


def score(trace_id: str, name: str = "helpfulness") -> Dict[str, object]:
    return {"name": name, "subject": {"kind": "trace", "id": trace_id}}


def fetch(handler: Callable[[httpx.Request], httpx.Response], max_traces: int = 10) -> List:
    async def go() -> List:
        async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as client:
            return await fetch_unscored_traces(client, METRICS, max_traces)

    return asyncio.run(go())


def server(observation_pages: List[Dict], score_pages: List[Dict] | None = None, seen: List | None = None):
    scores = score_pages or [{"data": [], "meta": {}}]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        pages = observation_pages if "v2/observations" in request.url.path else scores
        index = int(request.url.params.get("cursor", 0))
        return httpx.Response(200, json=pages[index])

    return handler


def test_parses_json_string_output_and_requests_root_observations_with_io():
    seen: List = []
    page = {"data": [observation("t1", run_output("answer"))], "meta": {}}
    traces = fetch(server([page], seen=seen))

    assert traces[0].id == "t1"
    assert traces[0].output["messages"][-1]["content"] == "answer"
    request = next(u for u in seen if "v2/observations" in u.path)
    assert "io" in request.params["fields"]
    assert "isRootObservation" in request.params["filter"]


def test_skips_traces_scored_by_our_metrics_but_not_by_other_scores():
    page = {"data": [observation("done", run_output("a")), observation("probed", run_output("b"))], "meta": {}}
    scores = {"data": [score("done"), score("probed", name="some_other_score")], "meta": {}}

    assert [t.id for t in fetch(server([page], [scores]))] == ["probed"]


def test_follows_cursors_on_both_endpoints():
    pages = [
        {"data": [observation("t1", run_output("a"))], "meta": {"cursor": "1"}},
        {"data": [observation("t2", run_output("b"))], "meta": {}},
    ]
    scores = [{"data": [], "meta": {"cursor": "1"}}, {"data": [score("t2")], "meta": {}}]

    assert [t.id for t in fetch(server(pages, scores))] == ["t1"]


def test_stops_at_max_traces_without_reading_further_pages():
    seen: List = []
    pages = [
        {"data": [observation("t1", run_output("a")), observation("t2", run_output("b"))], "meta": {"cursor": "1"}},
        {"data": [observation("t3", run_output("c"))], "meta": {}},
    ]

    assert [t.id for t in fetch(server(pages, seen=seen), max_traces=2)] == ["t1", "t2"]
    assert sum("v2/observations" in u.path for u in seen) == 1


PAUSED_ON_TOOL_CALL = json.dumps({"messages": [{"type": "human", "content": "q"}, {"type": "ai", "content": ""}]})


@pytest.mark.parametrize(
    "output", [None, "not json", json.dumps({"messages": []}), json.dumps(["x"]), PAUSED_ON_TOOL_CALL]
)
def test_runs_with_nothing_to_judge_are_skipped(output):
    assert fetch(server([{"data": [observation("t", output)], "meta": {}}])) == []


def test_legacy_style_410_raises_instead_of_looking_like_no_traces():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": "LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION"})

    with pytest.raises(httpx.HTTPStatusError):
        fetch(handler)
