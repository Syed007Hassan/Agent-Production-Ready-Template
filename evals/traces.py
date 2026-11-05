"""Read evaluation candidates from Langfuse's public API.

Langfuse organizations created after 2026-09-16 get a 410 from the legacy trace and
score endpoints the SDK wraps, so this calls the replacements directly: v2 observations
(a trace's root observation holds the run's final output) and v3 scores.
"""

import json
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from typing import (
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Set,
)

import httpx
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import logger

_PAGE_SIZE = 100
_LOOKBACK = timedelta(hours=24)
_ROOT_ONLY_FILTER = json.dumps([{"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True}])


class EvalTrace(BaseModel):
    """One agent run to evaluate."""

    id: str
    output: Dict[str, Any]


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_transient),
    reraise=True,
)
async def _get_page(client: httpx.AsyncClient, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    response = await client.get(path, params=params)
    response.raise_for_status()
    return response.json()


async def _pages(client: httpx.AsyncClient, path: str, params: Dict[str, Any]) -> AsyncIterator[List[Dict[str, Any]]]:
    cursor: Optional[str] = None
    while True:
        page = await _get_page(client, path, {**params, "limit": _PAGE_SIZE, **({"cursor": cursor} if cursor else {})})
        yield page.get("data", [])
        cursor = (page.get("meta") or {}).get("cursor")
        if not cursor:
            return


async def _scored_trace_ids(client: httpx.AsyncClient, since: str, metric_names: Set[str]) -> Set[str]:
    """Return traces already scored by one of our metrics; other score names don't count."""
    scored: Set[str] = set()
    params = {"fromTimestamp": since, "fields": "core,subject"}
    async for scores in _pages(client, "/api/public/v3/scores", params):
        for score in scores:
            subject = score.get("subject") or {}
            if subject.get("kind") == "trace" and score.get("name") in metric_names:
                scored.add(subject["id"])
    return scored


def _parse_output(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a run's output if its last message has text to judge.

    Failed runs have no messages, and runs paused on a tool call end with empty content. Neither
    can ever be scored, so returning them would use up a slot under the cap on every run.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict) or not raw.get("messages"):
        return None
    return raw if raw["messages"][-1].get("content") else None


async def fetch_unscored_traces(client: httpx.AsyncClient, metric_names: Set[str], max_traces: int) -> List[EvalTrace]:
    """Fetch the newest runs from the last 24 hours that no metric has scored yet.

    Args:
        client: HTTP client with the Langfuse base URL and credentials.
        metric_names: Names of the metrics this evaluator pushes.
        max_traces: Most runs to return; each one costs one judge call per metric.

    Returns:
        Runs to evaluate, newest first.

    Raises:
        httpx.HTTPError: If Langfuse can't be read. Never treated as "no traces",
            which would make a broken run look like a clean one.
    """
    since = (datetime.now(timezone.utc) - _LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    scored = await _scored_trace_ids(client, since, metric_names)
    traces: List[EvalTrace] = []
    seen = skipped_no_output = 0
    params = {"fromStartTime": since, "fields": "core,basic,io", "filter": _ROOT_ONLY_FILTER}

    async for observations in _pages(client, "/api/public/v2/observations", params):
        for observation in observations:
            seen += 1
            if observation["traceId"] in scored:
                continue
            output = _parse_output(observation.get("output"))
            if output is None:
                skipped_no_output += 1
                continue
            traces.append(EvalTrace(id=observation["traceId"], output=output))
            if len(traces) == max_traces:
                break
        if len(traces) == max_traces:
            break

    logger.info(
        "langfuse_traces_fetched",
        runs_seen=seen,
        already_scored=len(scored),
        skipped_no_output=skipped_no_output,
        to_evaluate=len(traces),
    )
    return traces
