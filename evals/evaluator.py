"""Evaluator for evals."""

import asyncio
import logging
import os
import sys
import time
from datetime import (
    datetime,
    timedelta,
)

import openai
from langfuse import Langfuse
from langfuse.api.resources.commons.types.trace_with_details import TraceWithDetails
from tenacity import (
    RetryCallState,
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

# Fix import path for app module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import settings
from app.core.logging import logger
from app.services.llm.availability import (
    FailureKind,
    classify_error,
    retry_delay_seconds,
)
from evals.helpers import (
    calculate_avg_scores,
    generate_report,
    get_input_output,
    initialize_metrics_summary,
    initialize_report,
    process_trace_results,
    update_failure_metrics,
    update_success_metrics,
)
from evals.metrics import metrics
from evals.schemas import ScoreSchema

# Worth retrying on the same judge model; everything else is reported as a failed metric.
_RETRYABLE_KINDS = {FailureKind.MINUTE_QUOTA, FailureKind.SERVER_ERROR, FailureKind.TIMEOUT}
_backoff = wait_exponential(multiplier=1, min=2, max=10)


class EvalQuotaExhaustedError(RuntimeError):
    """The judge model hit its daily quota; the run must stop rather than switch models."""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, Exception) and classify_error(exc) in _RETRYABLE_KINDS


def _judge_wait(retry_state: RetryCallState) -> float:
    """Wait the provider's retryDelay after a per-minute 429, otherwise back off exponentially."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, Exception) and classify_error(exc) is FailureKind.MINUTE_QUOTA:
        return retry_delay_seconds(exc)
    return _backoff(retry_state)


class Evaluator:
    """Evaluates model outputs using predefined metrics.

    This class handles fetching traces from Langfuse, evaluating them against
    metrics, and uploading scores back to Langfuse.

    The judge is one fixed model and never falls back: switching judges mid-run
    would make scores incomparable. If its daily quota runs out the run stops,
    and the unscored traces are picked up by the next run.

    Attributes:
        client: OpenAI-compatible client for the judge model.
        langfuse: Langfuse client for trace management.
    """

    def __init__(self):
        """Initialize Evaluator with the judge and Langfuse clients."""
        # SDK retries off: `_call_judge` owns retry policy and must see every 429.
        self.client = openai.AsyncOpenAI(
            api_key=settings.EVALUATION_API_KEY,
            base_url=settings.EVALUATION_BASE_URL,
            max_retries=0,
            timeout=60,
        )
        self.langfuse = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
            timeout=60,  # In seconds
        )
        self._min_call_interval = 60.0 / settings.EVALUATION_MAX_RPM
        self._last_call_at = 0.0
        # Initialize report data structure
        self.report = initialize_report(settings.EVALUATION_LLM)
        initialize_metrics_summary(self.report, metrics)

    async def run(self, generate_report_file=True):
        """Main execution function that fetches and evaluates traces.

        Retrieves traces from Langfuse, evaluates each one against all metrics,
        and uploads the scores back to Langfuse.

        Args:
            generate_report_file: Whether to generate a JSON report after evaluation. Defaults to True.
        """
        start_time = time.time()
        traces = self.__fetch_traces()
        trace_results = {}

        for trace in tqdm(traces, desc="Evaluating traces"):
            try:
                scores = await self._evaluate_trace(trace)
            except EvalQuotaExhaustedError:
                self.report["stopped_early"] = True
                logger.warning(
                    "evaluation_stopped_daily_quota",
                    model=settings.EVALUATION_LLM,
                    traces_evaluated=self.report["total_traces"],
                    traces_remaining=len(traces) - self.report["total_traces"],
                )
                break

            # Record and push only once every metric has run, so a run cut short never
            # leaves a half-scored trace (scored traces are skipped by the next run's fetch).
            self.report["total_traces"] += 1
            trace_results[trace.id] = {
                "success": False,
                "metrics_evaluated": len(metrics),
                "metrics_succeeded": 0,
                "metrics_results": {},
            }
            for metric in metrics:
                score = scores[metric["name"]]
                if score:
                    self._push_to_langfuse(trace, score, metric)
                    update_success_metrics(self.report, trace.id, metric["name"], score, trace_results)
                else:
                    update_failure_metrics(self.report, trace.id, metric["name"], trace_results)
            process_trace_results(self.report, trace.id, trace_results, len(metrics))

        self.report["traces_remaining"] = len(traces) - self.report["total_traces"]
        self.report["duration_seconds"] = round(time.time() - start_time, 2)
        calculate_avg_scores(self.report)

        if generate_report_file:
            generate_report(self.report)

        logger.info(
            "evaluation_completed",
            total_traces=self.report["total_traces"],
            successful_traces=self.report["successful_traces"],
            failed_traces=self.report["failed_traces"],
            stopped_early=self.report["stopped_early"],
            duration_seconds=self.report["duration_seconds"],
        )

    async def _evaluate_trace(self, trace: TraceWithDetails) -> dict[str, ScoreSchema | None]:
        """Run every metric on one trace.

        Raises:
            EvalQuotaExhaustedError: Propagated so the run stops before recording this trace.
        """
        input, output = get_input_output(trace)
        if input is None or output is None:
            logger.error("metric_evaluation_failed_missing_io", trace_id=trace.id)
            return {metric["name"]: None for metric in metrics}
        return {metric["name"]: await self._run_metric_evaluation(metric, input, output) for metric in metrics}

    def _push_to_langfuse(self, trace: TraceWithDetails, score: ScoreSchema, metric: dict):
        """Push evaluation score to Langfuse.

        Args:
            trace: The trace to score.
            score: The evaluation score.
            metric: The metric used for evaluation.
        """
        self.langfuse.create_score(
            trace_id=trace.id,
            name=metric["name"],
            data_type="NUMERIC",
            value=score.score,
            comment=score.reasoning,
        )

    async def _run_metric_evaluation(self, metric: dict, input: str, output: str) -> ScoreSchema | None:
        """Evaluate a single trace against a specific metric.

        Args:
            metric: The metric definition to use for evaluation.
            input: The input to evaluate.
            output: The output to evaluate.

        Returns:
            ScoreSchema with evaluation results or None if evaluation failed.

        Raises:
            EvalQuotaExhaustedError: When the judge model's daily quota is used up.
        """
        metric_name = metric["name"]
        try:
            score = await self._call_judge(metric["prompt"], input, output)
        except Exception as e:
            kind = classify_error(e)
            if kind is FailureKind.DAILY_QUOTA:
                raise EvalQuotaExhaustedError(f"daily quota exhausted for {settings.EVALUATION_LLM}") from e
            if kind is FailureKind.AUTH:
                raise
            logger.exception("metric_evaluation_failed", metric_name=metric_name, failure_kind=kind.value)
            return None

        if score is None:
            logger.error("metric_evaluation_failed_unparsed", metric_name=metric_name)
            return None
        logger.info(
            "metric_evaluation_completed",
            metric_name=metric_name,
            score=score.score,
            reasoning=score.reasoning,
        )
        return score

    async def _throttle(self) -> None:
        """Space judge calls to stay under EVALUATION_MAX_RPM."""
        wait = self._last_call_at + self._min_call_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_at = time.monotonic()

    @retry(
        stop=stop_after_attempt(3),
        wait=_judge_wait,
        retry=retry_if_exception(_is_retryable),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _call_judge(self, metric_system_prompt: str, input: str, output: str) -> ScoreSchema | None:
        """Ask the judge model to score one input/output pair.

        Args:
            metric_system_prompt: System prompt defining the evaluation metric.
            input: Formatted input messages.
            output: Formatted output message.

        Returns:
            ScoreSchema with evaluation results, or None if the reply could not be parsed.
        """
        await self._throttle()
        response = await self.client.beta.chat.completions.parse(
            model=settings.EVALUATION_LLM,
            messages=[
                {"role": "system", "content": metric_system_prompt},
                {"role": "user", "content": f"Input: {input}\nGeneration: {output}"},
            ],
            response_format=ScoreSchema,
        )
        return response.choices[0].message.parsed

    def __fetch_traces(self) -> list[TraceWithDetails]:
        """Fetch traces from the past 24 hours without scores.

        Returns:
            List of traces that haven't been scored yet.
        """
        last_24_hours = datetime.now() - timedelta(hours=24)
        logger.info("fetching_langfuse_traces", from_timestamp=str(last_24_hours))
        try:
            traces = self.langfuse.api.trace.list(
                from_timestamp=last_24_hours, order_by="timestamp.asc", limit=100
            ).data
            traces_without_scores = [trace for trace in traces if not trace.scores]
            return traces_without_scores
        except Exception:
            logger.exception("langfuse_traces_fetch_failed")
            return []
