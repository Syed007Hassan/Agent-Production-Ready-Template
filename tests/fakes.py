"""Fake Gemini errors for offline tests.

Real OpenAI-SDK exception types, with 429 bodies shaped like an actual Gemini
free-tier response (captured 2026-11-03), so classification is tested against
what the provider really sends.
"""

import httpx
from openai import (
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

REQUEST = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")


def _quota_body(quota_id: str, retry_delay: str) -> list:
    return [
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id, "quotaValue": "500"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
                ],
            }
        }
    ]


def minute_429(retry_delay: str = "39s") -> RateLimitError:
    return RateLimitError(
        "quota",
        response=httpx.Response(429, request=REQUEST),
        body=_quota_body("GenerateContentInputTokensPerModelPerMinute-FreeTier", retry_delay),
    )


def daily_429() -> RateLimitError:
    return RateLimitError(
        "quota",
        response=httpx.Response(429, request=REQUEST),
        body=_quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "39s"),
    )


def server_500() -> InternalServerError:
    return InternalServerError("internal", response=httpx.Response(500, request=REQUEST), body=None)


def auth_401() -> AuthenticationError:
    return AuthenticationError("bad key", response=httpx.Response(401, request=REQUEST), body=None)
