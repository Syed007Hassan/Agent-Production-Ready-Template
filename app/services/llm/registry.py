"""LLM model registry with pre-initialized instances."""

from typing import (
    Any,
    Dict,
    List,
)

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from app.core.config import settings
from app.core.logging import logger
from app.services.llm.gemini_chat import GeminiChatOpenAI

# Every provider is reached through its OpenAI-compatible endpoint, so all
# models share one client family (ChatOpenAI) and one error hierarchy — the retry
# and fallback logic in LLMService matches on `openai.RateLimitError` etc.
# Adding a provider means one entry here plus its key in config. `chat_model` is
# ChatOpenAI unless the endpoint needs more, as Gemini's thought signatures do.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "chat_model": GeminiChatOpenAI,
        "base_url": settings.GEMINI_BASE_URL,
        "api_key": SecretStr(settings.GEMINI_API_KEY),
    },
}

# Roles keep fallback chains separate: an "agent" call only ever falls back to
# another "agent" model, so side calls never eat the main chat quota (and vice versa).
ROLE_AGENT = "agent"
ROLE_CLASSIFIER = "classifier"


def _build_llm(config: Dict[str, Any], **overrides: Any) -> BaseChatModel:
    """Construct a chat model for a registry entry.

    Entry kwargs are the defaults; ``overrides`` win on conflicts. Connection
    details (base_url, api_key) always come from the entry's provider, so an
    override can never send a request to the wrong endpoint or with the wrong key.
    """
    provider = PROVIDERS[config["provider"]]
    kwargs = {
        # A per-call timeout lets a hung model be skipped instead of eating the
        # whole LLM_TOTAL_TIMEOUT budget. SDK retries are off because LLMService
        # owns retry policy (its own retries plus fallback across models).
        "timeout": settings.LLM_CALL_TIMEOUT,
        "max_retries": 0,
        **config.get("kwargs", {}),
        **overrides,
    }
    return provider["chat_model"](
        model=config["model"],
        base_url=provider["base_url"],
        api_key=provider["api_key"],
        **kwargs,
    )


class LLMRegistry:
    """Registry of available LLM models with pre-initialized instances.

    This class maintains a list of LLM configurations and provides
    methods to retrieve them by name with optional argument overrides.
    """

    # Ordered by preference within each role: the first entry of a role is the
    # head of that role's circular fallback chain. Index 0 must be an agent model
    # (it is the last-resort default in LLMService).
    #
    # `reasoning` is deliberately not set: in ChatOpenAI it switches to OpenAI's
    # Responses API, which Gemini's OpenAI-compatible endpoint does not implement.
    LLM_CONFIGS: List[Dict[str, Any]] = [
        # Main agent chain: separate per-model free-tier quotas, so each hop buys fresh capacity.
        {
            "name": "gemini-3.5-flash-lite",
            "model": "gemini-3.5-flash-lite",
            "provider": "gemini",
            "role": ROLE_AGENT,
            "kwargs": {"max_completion_tokens": settings.MAX_TOKENS},
        },
        {
            "name": "gemini-3.1-flash-lite",
            "model": "gemini-3.1-flash-lite",
            "provider": "gemini",
            "role": ROLE_AGENT,
            "kwargs": {"max_completion_tokens": settings.MAX_TOKENS},
        },
        {
            "name": "gemini-3.8-flash",
            "model": "gemini-3.8-flash",
            "provider": "gemini",
            "role": ROLE_AGENT,
            "kwargs": {"max_completion_tokens": settings.MAX_TOKENS},
        },
        # Side calls (session titles, later guardrails / routing): high RPD, low TPM.
        {
            "name": "gemma-4-31b-it",
            "model": "gemma-4-31b-it",
            "provider": "gemini",
            "role": ROLE_CLASSIFIER,
            "kwargs": {"max_completion_tokens": 512},
        },
    ]

    LLMS: List[Dict[str, Any]] = [{"name": config["name"], "llm": _build_llm(config)} for config in LLM_CONFIGS]

    @classmethod
    def get(cls, model_name: str, **kwargs) -> BaseChatModel:
        """Get an LLM by name with optional argument overrides.

        When kwargs are provided a fresh model instance is returned with
        those overrides applied, leaving the shared registry entry untouched.

        Args:
            model_name: Name of the model to retrieve.
            **kwargs: Optional arguments to override default model configuration.

        Returns:
            BaseChatModel instance.

        Raises:
            ValueError: If model_name is not found in LLM_CONFIGS.
        """
        config = next((c for c in cls.LLM_CONFIGS if c["name"] == model_name), None)

        if not config:
            available = ", ".join(c["name"] for c in cls.LLM_CONFIGS)
            raise ValueError(f"model '{model_name}' not found in registry. available models: {available}")

        if kwargs:
            logger.debug(
                "creating_llm_with_custom_args",
                model_name=model_name,
                model=config["model"],
                custom_args=list(kwargs.keys()),
            )
            return _build_llm(config, **kwargs)

        logger.debug("using_default_llm_instance", model_name=model_name)
        return next(entry["llm"] for entry in cls.LLMS if entry["name"] == model_name)

    @classmethod
    def get_all_names(cls) -> List[str]:
        """Return all registered model names in order.

        Returns:
            List of model name strings.
        """
        return [config["name"] for config in cls.LLM_CONFIGS]

    @classmethod
    def get_fallback_chain(cls, index: int) -> List[int]:
        """Return the fallback order for the model at ``index``.

        Only models with the same role are included, rotated so ``index`` comes
        first, e.g. agent models [0, 1, 2] starting at 1 -> [1, 2, 0].

        Args:
            index: Registry index of the model to start from.

        Returns:
            Registry indices to try, in order.
        """
        role = cls.LLM_CONFIGS[index]["role"]
        same_role = [i for i, config in enumerate(cls.LLM_CONFIGS) if config["role"] == role]
        position = same_role.index(index)
        return same_role[position:] + same_role[:position]
