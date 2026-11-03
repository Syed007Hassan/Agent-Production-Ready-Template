"""LLM service with retries, circular fallback, and optional structured output."""

import asyncio
import logging
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
    overload,
)

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from openai import (
    APIError,
    APITimeoutError,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.registry import LLMRegistry

T = TypeVar("T", bound=BaseModel)


class LLMService:
    """Service for managing LLM calls with retries and circular fallback.

    Two distinct execution paths:

    - **Default path** (no model_name / response_format / model_kwargs): uses
      ``self._llm`` which is the tool-bound agent model. Circular fallback
      updates ``self._llm`` so tool bindings are preserved across retries.

    - **One-off path** (any override provided): resolves a fresh, local
      ``Runnable`` for the call without ever touching ``self._llm``, so
      concurrent default-path calls are never affected.
    """

    def __init__(self):
        """Initialize the LLM service with the configured default model."""
        self._llm: Any = None  # BaseChatModel pre-bind_tools, Runnable after
        self._current_model_index: int = 0
        self._bound_tools: List = []

        all_names = LLMRegistry.get_all_names()
        try:
            self._current_model_index = all_names.index(settings.DEFAULT_LLM_MODEL)
            self._llm = LLMRegistry.get(settings.DEFAULT_LLM_MODEL)
            logger.info(
                "llm_service_initialized",
                default_model=settings.DEFAULT_LLM_MODEL,
                model_index=self._current_model_index,
                total_models=len(all_names),
                environment=settings.ENVIRONMENT.value,
            )
        except Exception as e:
            self._current_model_index = 0
            self._llm = LLMRegistry.LLMS[0]["llm"]
            logger.warning(
                "default_model_not_found_using_first",
                requested=settings.DEFAULT_LLM_MODEL,
                using=all_names[0] if all_names else "none",
                error=str(e),
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
        """Call the LLM with retries and circular fallback.

        Args:
            messages: Conversation messages to send.
            model_name: Override the model. ``None`` uses the current default.
            response_format: Pydantic schema for structured output. When
                provided the call chains ``.with_structured_output(schema)``
                and returns a validated instance of that schema instead of a
                raw ``BaseMessage``.
            **model_kwargs: Extra kwargs forwarded to ``LLMRegistry.get`` when
                constructing a one-off model instance (e.g. ``temperature``,
                ``max_tokens``, ``reasoning``).

        Returns:
            ``BaseMessage`` when ``response_format`` is ``None``, otherwise a
            validated instance of ``response_format``.

        Raises:
            RuntimeError: When all models fail after retries or the total
                timeout budget is exceeded.
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
            raise RuntimeError(f"llm call timed out after {settings.LLM_TOTAL_TIMEOUT}s total budget")

    def get_llm(self) -> Any:
        """Return the current tool-bound default LLM instance.

        Returns:
            Current ``BaseChatModel`` instance or ``None`` if not initialised.
        """
        return self._llm

    def bind_tools(self, tools: List) -> "LLMService":
        """Bind tools to the default LLM instance.

        Args:
            tools: List of tools to bind.

        Returns:
            Self for method chaining.
        """
        if self._llm:
            self._bound_tools = tools
            self._llm = self._llm.bind_tools(tools)
            logger.debug("tools_bound_to_llm", tool_count=len(tools))
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(settings.MAX_LLM_CALL_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _invoke_with_retry(self, llm: Any, messages: LanguageModelInput) -> Any:
        """Invoke an LLM runnable with automatic per-model retry logic.

        Args:
            llm: Any LangChain ``Runnable`` (plain model or structured-output chain).
            messages: Messages to send.

        Returns:
            The runnable's response (``BaseMessage`` or a ``BaseModel`` instance).

        Raises:
            Exception: Propagated after all retry attempts are exhausted —
                OpenAI's error types on the backoff-retry path, or whatever
                the invoked provider's runnable raised otherwise.
        """
        try:
            response = await llm.ainvoke(messages)
            logger.debug("llm_call_successful")
            return response
        except (RateLimitError, APITimeoutError, APIError) as e:
            logger.warning(
                "llm_call_failed_retrying",
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            raise
        except (TypeError, AttributeError, KeyError):
            # Programming errors, not provider failures — let them surface
            # as-is instead of logging them as a failed LLM call.
            raise
        except Exception as e:
            # The backoff retry above only matches OpenAI's error types, but a
            # non-OpenAI registry entry can fail with its own provider's
            # exception type — catch broadly so `_fallback_loop` still
            # advances to the next model regardless of which one raised.
            logger.error(
                "llm_call_failed",
                error_type=type(e).__name__,
                error=str(e),
            )
            raise

    def _switch_to_model(self, index: int) -> None:
        """Make the registry entry at ``index`` the default model.

        Mutates ``self._llm`` and ``self._current_model_index`` and rebinds tools,
        so tool bindings survive model switches on the default agent path.

        Args:
            index: Registry index of the model to switch to.
        """
        next_entry = LLMRegistry.LLMS[index]
        logger.warning(
            "switching_to_next_model",
            from_model=LLMRegistry.LLMS[self._current_model_index]["name"],
            to_model=next_entry["name"],
        )
        self._current_model_index = index
        self._llm = next_entry["llm"]
        if self._bound_tools:
            self._llm = self._llm.bind_tools(self._bound_tools)
        logger.info("model_switched", new_model=next_entry["name"], new_index=index)

    async def _call_with_fallback(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str],
        response_format: Optional[Type[BaseModel]],
        model_kwargs: dict,
    ) -> Union[BaseMessage, BaseModel]:
        """Build the path-specific target resolver and delegate to the shared fallback loop.

        One-off path (any override set):
            ``get_target`` builds a fresh registry instance per attempt;
            ``self._llm`` is never touched.

        Default path (no overrides):
            ``get_target`` switches ``self._llm`` (tool-bound) when the loop
            moves on, so bindings persist.
        """

        def _override_target(idx: int) -> Any:
            base = LLMRegistry.get(LLMRegistry.LLMS[idx]["name"], **model_kwargs)
            return base.with_structured_output(response_format) if response_format else base

        def _default_target(idx: int) -> Any:
            if idx != self._current_model_index:
                self._switch_to_model(idx)
            return self._llm

        if model_name or response_format or model_kwargs:
            all_names = LLMRegistry.get_all_names()
            if model_name and model_name not in all_names:
                logger.error("requested_model_not_found", model_name=model_name)
                raise ValueError(
                    f"model '{model_name}' not found in registry. available models: {', '.join(all_names)}"
                )

            start = all_names.index(model_name) if model_name else self._current_model_index
            return await self._fallback_loop(messages, start, _override_target)

        return await self._fallback_loop(messages, self._current_model_index, _default_target)

    async def _fallback_loop(
        self,
        messages: LanguageModelInput,
        start: int,
        get_target: Callable[[int], Any],
    ) -> Any:
        """Shared fallback loop — try each same-role model in turn until one succeeds.

        Args:
            messages: Messages to send.
            start: Registry index to begin from.
            get_target: Returns the ``Runnable`` to invoke for a given index.

        Returns:
            The first successful response.

        Raises:
            RuntimeError: When every model in the chain has failed.
        """
        chain = LLMRegistry.get_fallback_chain(start)
        last_error: Optional[Exception] = None

        for models_tried, current in enumerate(chain, start=1):
            current_name = LLMRegistry.LLMS[current]["name"]
            try:
                return await self._invoke_with_retry(get_target(current), messages)
            except (TypeError, AttributeError, KeyError):
                # Programming errors would fail identically on every model, so
                # re-raise immediately rather than cycling through the chain
                # and leaving the service switched to a fallback model.
                raise
            except Exception as e:
                # Broad on purpose: whatever the provider raised, the next model
                # in the chain may still succeed.
                last_error = e
                logger.error(
                    "llm_call_failed_after_retries",
                    model=current_name,
                    models_tried=models_tried,
                    total_models=len(chain),
                    error=str(e),
                )

        logger.error("all_models_failed", models_tried=len(chain), starting_model=LLMRegistry.LLMS[start]["name"])
        raise RuntimeError(
            f"failed to get response from llm after trying {len(chain)} models. last error: {str(last_error)}"
        )


llm_service = LLMService()
