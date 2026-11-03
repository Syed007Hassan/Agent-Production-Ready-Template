"""LLM service with retries, quota-aware fallback, and optional structured output."""

import asyncio
import logging
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
    overload,
)

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from pydantic import BaseModel
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.availability import (
    FailureKind,
    LLMUnavailableError,
    classify_error,
    model_availability,
)
from app.services.llm.registry import LLMRegistry

T = TypeVar("T", bound=BaseModel)


def _is_server_error(exc: BaseException) -> bool:
    """Only transient 5xx / connection errors are worth retrying on the same model."""
    return isinstance(exc, Exception) and classify_error(exc) is FailureKind.SERVER_ERROR


class LLMService:
    """Service for managing LLM calls with retries and quota-aware fallback.

    Every call walks the fallback chain of its starting model's role from the
    top, skipping models that are currently out of quota. Nothing about a
    failure is stored on the service itself, so one request's fallback never
    changes which model another concurrent request starts on.

    Two execution paths:

    - **Default path** (no model_name / response_format / model_kwargs): uses
      the tool-bound agent models, bound once in ``bind_tools``.

    - **One-off path** (any override provided): builds a fresh ``Runnable``
      per attempt from the registry with the overrides applied.
    """

    def __init__(self):
        """Initialize the LLM service with the configured default model."""
        self._bound_tools: List = []
        self._bound_llms: Dict[int, Any] = {}  # registry index -> tool-bound Runnable

        all_names = LLMRegistry.get_all_names()
        if settings.DEFAULT_LLM_MODEL in all_names:
            self._default_index = all_names.index(settings.DEFAULT_LLM_MODEL)
            logger.info(
                "llm_service_initialized",
                default_model=settings.DEFAULT_LLM_MODEL,
                fallback_chain=[all_names[i] for i in LLMRegistry.get_fallback_chain(self._default_index)],
                environment=settings.ENVIRONMENT.value,
            )
        else:
            self._default_index = 0
            logger.warning(
                "default_model_not_found_using_first",
                requested=settings.DEFAULT_LLM_MODEL,
                using=all_names[0],
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @overload
    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = ...,
        response_format: None = ...,
        **model_kwargs: Any,
    ) -> BaseMessage: ...

    @overload
    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = ...,
        *,
        response_format: Type[T],
        **model_kwargs: Any,
    ) -> T: ...

    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = None,
        response_format: Optional[Type[BaseModel]] = None,
        **model_kwargs: Any,
    ) -> Union[BaseMessage, BaseModel]:
        """Call the LLM with retries and quota-aware fallback.

        Args:
            messages: Conversation messages to send.
            model_name: Override the starting model. ``None`` uses the default.
            response_format: Pydantic schema for structured output. When
                provided the call chains ``.with_structured_output(schema)``
                and returns a validated instance of that schema instead of a
                raw ``BaseMessage``.
            **model_kwargs: Extra kwargs forwarded to ``LLMRegistry.get`` when
                constructing a one-off model instance (e.g. ``temperature``,
                ``max_completion_tokens``).

        Returns:
            ``BaseMessage`` when ``response_format`` is ``None``, otherwise a
            validated instance of ``response_format``.

        Raises:
            LLMUnavailableError: When every model in the chain failed, was out
                of quota, or the total timeout budget was exceeded.
        """
        try:
            return await asyncio.wait_for(
                self._call_with_fallback(messages, model_name, response_format, model_kwargs),
                timeout=settings.LLM_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.exception(
                "llm_total_timeout_exceeded",
                timeout_seconds=settings.LLM_TOTAL_TIMEOUT,
            )
            raise LLMUnavailableError(f"llm call timed out after {settings.LLM_TOTAL_TIMEOUT}s total budget")

    def get_llm(self) -> Any:
        """Return the default model (tool-bound once ``bind_tools`` has run).

        Returns:
            The default model's ``Runnable``.
        """
        return self._bound_llms.get(self._default_index) or LLMRegistry.LLMS[self._default_index]["llm"]

    def bind_tools(self, tools: List) -> "LLMService":
        """Bind tools to every model in the default model's fallback chain.

        Args:
            tools: List of tools to bind.

        Returns:
            Self for method chaining.
        """
        self._bound_tools = tools
        self._bound_llms = {
            idx: LLMRegistry.LLMS[idx]["llm"].bind_tools(tools)
            for idx in LLMRegistry.get_fallback_chain(self._default_index)
        }
        logger.debug("tools_bound_to_llm", tool_count=len(tools), model_count=len(self._bound_llms))
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(settings.MAX_LLM_CALL_RETRIES),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception(_is_server_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _invoke_with_retry(self, llm: Any, messages: LanguageModelInput) -> Any:
        """Invoke an LLM runnable, retrying the same model only on transient server errors.

        Quota errors and timeouts are not retried here: another model in the
        chain has its own quota, so moving on is faster than waiting.

        Args:
            llm: Any LangChain ``Runnable`` (plain model or structured-output chain).
            messages: Messages to send.

        Returns:
            The runnable's response (``BaseMessage`` or a ``BaseModel`` instance).

        Raises:
            Exception: Whatever the provider raised, after retries for server errors.
        """
        try:
            response = await llm.ainvoke(messages)
            logger.debug("llm_call_successful")
            return response
        except (TypeError, AttributeError, KeyError):
            # Programming errors, not provider failures — let them surface
            # as-is instead of logging them as a failed LLM call.
            raise
        except Exception as e:
            logger.warning(
                "llm_call_failed",
                error_type=type(e).__name__,
                failure_kind=classify_error(e).value,
                error=str(e),
            )
            raise

    async def _call_with_fallback(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str],
        response_format: Optional[Type[BaseModel]],
        model_kwargs: dict,
    ) -> Union[BaseMessage, BaseModel]:
        """Pick the starting model and target resolver, then run the fallback loop."""

        def _override_target(idx: int) -> Any:
            base = LLMRegistry.get(LLMRegistry.LLMS[idx]["name"], **model_kwargs)
            return base.with_structured_output(response_format) if response_format else base

        def _default_target(idx: int) -> Any:
            return self._bound_llms.get(idx) or LLMRegistry.LLMS[idx]["llm"]

        if model_name or response_format or model_kwargs:
            all_names = LLMRegistry.get_all_names()
            if model_name and model_name not in all_names:
                logger.error("requested_model_not_found", model_name=model_name)
                raise ValueError(
                    f"model '{model_name}' not found in registry. available models: {', '.join(all_names)}"
                )

            start = all_names.index(model_name) if model_name else self._default_index
            return await self._fallback_loop(messages, start, _override_target)

        return await self._fallback_loop(messages, self._default_index, _default_target)

    async def _fallback_loop(
        self,
        messages: LanguageModelInput,
        start: int,
        get_target: Callable[[int], Any],
    ) -> Any:
        """Try each available same-role model in turn until one succeeds.

        Args:
            messages: Messages to send.
            start: Registry index to begin from.
            get_target: Returns the ``Runnable`` to invoke for a given index.

        Returns:
            The first successful response.

        Raises:
            LLMUnavailableError: When no model in the chain could serve the call.
        """
        chain = LLMRegistry.get_fallback_chain(start)
        chain_names = [LLMRegistry.LLMS[idx]["name"] for idx in chain]
        last_error: Optional[Exception] = None
        models_tried = 0

        for idx, current_name in zip(chain, chain_names, strict=True):
            if not model_availability.is_available(current_name):
                logger.info("llm_model_skipped_unavailable", model=current_name)
                continue

            models_tried += 1
            try:
                return await self._invoke_with_retry(get_target(idx), messages)
            except (TypeError, AttributeError, KeyError):
                # Programming errors would fail identically on every model.
                raise
            except Exception as e:
                kind = classify_error(e)
                if kind is FailureKind.AUTH:
                    # Every model shares the provider key, so the next one would fail too.
                    logger.exception("llm_auth_failed", model=current_name)
                    raise
                model_availability.record_failure(current_name, kind, e)
                last_error = e
                logger.warning(
                    "llm_falling_back",
                    model=current_name,
                    failure_kind=kind.value,
                    models_tried=models_tried,
                    chain_length=len(chain),
                )

        quota_exhausted = model_availability.all_blocked(chain_names)
        retry_after = model_availability.seconds_until_available(chain_names)
        logger.error(
            "all_models_failed",
            starting_model=chain_names[0],
            models_tried=models_tried,
            quota_exhausted=quota_exhausted,
            retry_after_seconds=retry_after,
            last_error=str(last_error) if last_error else None,
        )
        raise LLMUnavailableError(
            f"no model available after trying {models_tried} of {len(chain)}. last error: {last_error}",
            quota_exhausted=quota_exhausted,
            retry_after_seconds=retry_after,
        ) from last_error


llm_service = LLMService()
