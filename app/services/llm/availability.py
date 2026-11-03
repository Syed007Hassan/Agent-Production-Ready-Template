"""Per-model availability tracking driven by provider errors.

Free-tier quotas are per model, so a 429 on one model says nothing about the
next one. This module classifies a failed call and remembers which models are
out of quota (and until when), so the fallback loop skips them instead of
spending the request's time budget on calls that are certain to fail.

State is in-process: with several uvicorn workers each one learns a model is
exhausted from its own first 429, which costs one extra failed call per worker.
"""

import re
import time
from datetime import (
    datetime,
    timedelta,
)
from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Optional,
)
from zoneinfo import ZoneInfo

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from app.core.logging import logger

# Gemini resets requests-per-day quotas at midnight Pacific time.
_QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")
# Used when a per-minute 429 carries no RetryInfo.
_DEFAULT_MINUTE_COOLDOWN_SECONDS = 60.0
_RETRY_DELAY_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)s$")


class FailureKind(str, Enum):
    """What a failed LLM call means for the next attempt."""

    DAILY_QUOTA = "daily_quota"  # skip this model until the daily reset
    MINUTE_QUOTA = "minute_quota"  # skip this model for the provider's retryDelay
    SERVER_ERROR = "server_error"  # transient, worth one quick retry on the same model
    TIMEOUT = "timeout"  # model is slow right now; move on rather than wait again
    AUTH = "auth"  # bad key / no access; every model shares the key, so stop
    OTHER = "other"  # model-specific failure (bad request, parse error); move on


class LLMUnavailableError(RuntimeError):
    """No model in the fallback chain could serve the request.

    Attributes:
        quota_exhausted: True when every model in the chain is out of quota.
        retry_after_seconds: Seconds until the first model frees up, if known.
    """

    def __init__(self, message: str, quota_exhausted: bool = False, retry_after_seconds: Optional[int] = None):
        """Initialize the error with its retry hint."""
        super().__init__(message)
        self.quota_exhausted = quota_exhausted
        self.retry_after_seconds = retry_after_seconds

    @property
    def user_message(self) -> str:
        """Message that is safe to show to an end user."""
        if self.quota_exhausted:
            return "The assistant has reached its usage limit for now. Please try again later."
        return "The assistant is temporarily unavailable. Please try again in a moment."


def _error_details(exc: Exception) -> List[Dict[str, Any]]:
    """Return the ``google.rpc`` detail objects from an OpenAI-SDK error body.

    Gemini's OpenAI-compatible endpoint returns ``[{"error": {..., "details": [...]}}]``.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        body = body.get("error", body)
    details = body.get("details") if isinstance(body, dict) else None
    return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []


def _is_daily_quota(exc: Exception) -> bool:
    for detail in _error_details(exc):
        for violation in detail.get("violations", []):
            if "PerDay" in str(violation.get("quotaId", "")):
                return True
    return False


def retry_delay_seconds(exc: Exception) -> float:
    """Return the provider's RetryInfo delay for a 429, or a 60s default when it sends none."""
    for detail in _error_details(exc):
        if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            match = _RETRY_DELAY_PATTERN.match(str(detail.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return _DEFAULT_MINUTE_COOLDOWN_SECONDS


def classify_error(exc: Exception) -> FailureKind:
    """Map a provider exception to what the fallback loop should do next.

    Args:
        exc: Exception raised by the model call.

    Returns:
        The failure kind.
    """
    if isinstance(exc, RateLimitError):
        return FailureKind.DAILY_QUOTA if _is_daily_quota(exc) else FailureKind.MINUTE_QUOTA
    # APITimeoutError subclasses APIConnectionError, so it must be checked first.
    if isinstance(exc, APITimeoutError):
        return FailureKind.TIMEOUT
    if isinstance(exc, (InternalServerError, APIConnectionError)):
        return FailureKind.SERVER_ERROR
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return FailureKind.AUTH
    return FailureKind.OTHER


def next_daily_reset(now: Optional[datetime] = None) -> datetime:
    """Return the next midnight Pacific time (timezone-aware).

    Args:
        now: Reference time; defaults to the current time.

    Returns:
        The next quota reset moment.
    """
    local_now = (now or datetime.now(_QUOTA_RESET_TZ)).astimezone(_QUOTA_RESET_TZ)
    return datetime.combine(local_now.date() + timedelta(days=1), datetime.min.time(), tzinfo=_QUOTA_RESET_TZ)


class ModelAvailability:
    """Tracks which models are temporarily out of quota."""

    def __init__(self):
        """Initialize with every model available."""
        self._blocked_until: Dict[str, float] = {}  # model name -> unix timestamp

    def is_available(self, model_name: str) -> bool:
        """Return whether ``model_name`` may be called now."""
        until = self._blocked_until.get(model_name)
        if until is None:
            return True
        if time.time() >= until:
            del self._blocked_until[model_name]
            logger.info("llm_model_available_again", model=model_name)
            return True
        return False

    def record_failure(self, model_name: str, kind: FailureKind, exc: Exception) -> None:
        """Block ``model_name`` if ``kind`` is a quota failure; otherwise do nothing."""
        if kind is FailureKind.DAILY_QUOTA:
            until = next_daily_reset().timestamp()
        elif kind is FailureKind.MINUTE_QUOTA:
            until = time.time() + retry_delay_seconds(exc)
        else:
            return
        self._blocked_until[model_name] = until
        logger.warning(
            "llm_model_blocked",
            model=model_name,
            failure_kind=kind.value,
            blocked_seconds=round(until - time.time()),
        )

    def seconds_until_available(self, model_names: List[str]) -> Optional[int]:
        """Return seconds until the first of ``model_names`` frees up, or None if none are blocked."""
        now = time.time()
        remaining = [self._blocked_until[name] - now for name in model_names if name in self._blocked_until]
        return max(1, round(min(remaining))) if remaining else None

    def all_blocked(self, model_names: List[str]) -> bool:
        """Return whether every model in ``model_names`` is currently blocked."""
        return all(not self.is_available(name) for name in model_names)


model_availability = ModelAvailability()
