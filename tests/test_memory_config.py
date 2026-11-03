"""Guards for the mem0 config: each assertion is a failure seen while moving memory to Gemini."""

from langchain_core.language_models.chat_models import BaseChatModel
from mem0.configs.base import MemoryConfig

from app.services.memory import build_mem0_config

# pgvector's HNSW index rejects `vector` columns wider than this, and mem0 always builds that index.
PGVECTOR_HNSW_MAX_DIMS = 2000


def test_config_passes_mem0_validation():
    MemoryConfig(**build_mem0_config())


def test_embedding_dims_fit_hnsw_and_match_the_table():
    config = build_mem0_config()
    embedder_dims = config["embedder"]["config"]["embedding_dims"]
    assert embedder_dims <= PGVECTOR_HNSW_MAX_DIMS
    assert config["vector_store"]["config"]["embedding_model_dims"] == embedder_dims


def test_llm_avoids_openai_provider_and_forces_json():
    # mem0's "openai" LLM provider sends `store`, which Gemini rejects with a 400.
    llm_config = build_mem0_config()["llm"]
    assert llm_config["provider"] == "langchain"
    model = llm_config["config"]["model"]
    assert isinstance(model, BaseChatModel)
    assert model.model_kwargs["response_format"] == {"type": "json_object"}  # pyright: ignore[reportAttributeAccessIssue]


def test_embedder_never_falls_back_to_openai_env_vars():
    embedder = build_mem0_config()["embedder"]["config"]
    assert embedder["openai_base_url"].startswith("https://generativelanguage.googleapis.com")
    assert embedder["api_key"]
