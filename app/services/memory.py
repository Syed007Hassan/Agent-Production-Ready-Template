"""Long-term memory service using mem0 and pgvector with optional cache layer."""

from typing import (
    Any,
    Dict,
)

from langchain_core.messages import BaseMessage
from mem0 import AsyncMemory

from app.core.cache import (
    cache_key,
    cache_service,
)
from app.core.config import settings
from app.core.logging import logger
from app.core.prompts import load_memory_capture_prompt
from app.services.llm import LLMRegistry
from app.utils import latest_user_text


def build_mem0_config() -> Dict[str, Any]:
    """Return the mem0 config: pgvector storage, Gemini for fact extraction and embeddings."""
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "collection_name": settings.LONG_TERM_MEMORY_COLLECTION_NAME,
                "dbname": settings.POSTGRES_DB,
                "user": settings.POSTGRES_USER,
                "password": settings.POSTGRES_PASSWORD,
                "host": settings.POSTGRES_HOST,
                "port": settings.POSTGRES_PORT,
                "embedding_model_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIMS,
            },
        },
        # mem0's "openai" LLM provider always sends OpenAI's `store` field, which Gemini rejects
        # with a 400. The "langchain" provider takes a model instance instead, so mem0 reuses the
        # registry entry (endpoint, key, timeout). It ignores mem0's response_format, so JSON mode
        # is set on the model itself — mem0 expects JSON from every call it makes.
        "llm": {
            "provider": "langchain",
            "config": {
                "model": LLMRegistry.get(
                    settings.LONG_TERM_MEMORY_MODEL,
                    temperature=0.1,
                    # mem0's own default; the update step returns JSON covering every related memory.
                    max_completion_tokens=2000,
                    model_kwargs={"response_format": {"type": "json_object"}},
                ),
            },
        },
        # The OpenAI embedder sends only input/model/dimensions, which Gemini accepts. Without an
        # explicit key and base_url it would read OPENAI_API_KEY / OPENAI_BASE_URL.
        "embedder": {
            "provider": "openai",
            "config": {
                "model": settings.LONG_TERM_MEMORY_EMBEDDER_MODEL,
                "api_key": settings.GEMINI_API_KEY,
                "openai_base_url": settings.GEMINI_BASE_URL,
                "embedding_dims": settings.LONG_TERM_MEMORY_EMBEDDING_DIMS,
            },
        },
        "custom_fact_extraction_prompt": load_memory_capture_prompt(),
    }


class MemoryService:
    """Service for managing long-term memory using mem0 and pgvector."""

    def __init__(self):
        """Initialize the memory service."""
        self._memory: AsyncMemory | None = None

    async def _get_memory(self) -> AsyncMemory:
        if self._memory is None:
            self._memory = await AsyncMemory.from_config(config_dict=build_mem0_config())
        return self._memory

    async def initialize(self) -> None:
        """Pre-warm the mem0 AsyncMemory instance and its pgvector connection pool.

        Call once at startup so the first search() or add() doesn't pay the
        ~130ms from_config + pgvector.list_cols() cold-init cost.
        """
        await self._get_memory()
        logger.info("memory_service_initialized")

    async def search(self, user_id: str | None, query: str) -> str:
        """Search relevant memories for a user.

        Checks cache first; on miss, queries mem0 and caches the result.

        Returns formatted memory string, or empty string on failure or when
        no user_id is supplied (anonymous sessions skip long-term memory
        rather than pooling under a shared partition).
        """
        if user_id is None:
            return ""
        try:
            # Check cache first
            key = cache_key("memory", str(user_id), query)
            cached = await cache_service.get(key)
            if cached is not None:
                logger.debug("memory_search_cache_hit", user_id=user_id)
                return cached

            memory = await self._get_memory()
            results = await memory.search(user_id=str(user_id), query=query)
            result = "\n".join([f"* {r['memory']}" for r in results["results"]])

            # Cache successful results
            if result:
                await cache_service.set(key, result)

            return result
        except Exception as e:
            logger.error("failed_to_get_relevant_memory", error=str(e), user_id=user_id, query=query)
            return ""

    async def add(self, user_id: str | None, messages: list[BaseMessage]) -> None:
        """Add the latest user turn to long-term memory.

        Only the latest user message is forwarded to mem0. The assistant reply
        is deliberately excluded: mem0's extractor concatenates both roles and
        paraphrases the assistant's suggestions as if the user had volunteered
        them, producing noisy facts. No-op when ``user_id`` is ``None`` (see
        ``search`` for rationale).
        """
        if user_id is None:
            return
        user_text = latest_user_text(messages).strip()
        if not user_text:
            logger.debug("long_term_memory_skipped_empty_turn", user_id=user_id)
            return

        try:
            memory = await self._get_memory()
            await memory.add(
                [{"role": "user", "content": user_text}],
                user_id=str(user_id),
            )
            logger.info("long_term_memory_updated_successfully", user_id=user_id)
        except Exception as e:
            logger.exception("failed_to_update_long_term_memory", user_id=user_id, error=str(e))


memory_service = MemoryService()
