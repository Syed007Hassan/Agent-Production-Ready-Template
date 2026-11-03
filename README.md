# FastAPI LangGraph Agent Template

A production-ready template for building AI agent backends with FastAPI and LangGraph. Handles the hard parts — stateful conversations, long-term memory, tool calling, observability, rate limiting, auth — so you can focus on your agent logic.

**Built for AI engineers** who want a solid foundation, not a tutorial project.                  

---

## Runs on the Gemini API free tier

> Fork of [wassim249/fastapi-langgraph-agent-production-ready-template](https://github.com/wassim249/fastapi-langgraph-agent-production-ready-template) (MIT), reworked to run entirely on the **Gemini API free tier** — no paid LLM provider.

Every model is reached through Gemini's [OpenAI-compatible endpoint](https://ai.google.dev/gemini-api/docs/openai), so the stack keeps using LangChain's `ChatOpenAI` and the OpenAI SDK's error types. Free-tier quotas are **per model**, so the agent falls back across models with separate quotas:

```
Agent calls     ──► 3.5 Flash Lite ──429──► 3.1 Flash Lite ──429──► 3.8 Flash ──► 503 "usage limit reached"
Session titles  ──► Gemma 4 31B          (own quota, strict structured output)
mem0 facts      ──► 3.5 Flash Lite       (JSON mode)
mem0 embeddings ──► Gemini Embedding 2   (1536 dims, own quota)
Eval judge      ──► 3.5 Flash Lite       (one fixed model, throttled to 12 RPM)
Unit tests      ──► stub models          (no network)
```

**Setup**

1. Create a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey). **Don't link a billing account** to its Google Cloud project — that moves it off the free tier.
2. Copy `.env.example` to `.env.development` and set:

   ```env
   GEMINI_API_KEY=<your-gemini-key>
   DEFAULT_LLM_MODEL=gemini-3.5-flash-lite
   MEM0_TELEMETRY=False
   ```

**What the fallback does** (details in [docs/llm-service.md](docs/llm-service.md)):

- Reads Google's `QuotaFailure` / `RetryInfo` error details to tell a **per-minute** 429 (skip that model for `retryDelay`) from a **per-day** 429 (skip it until midnight Pacific).
- Retries the same model only on transient 5xx errors; timeouts and quota errors move straight to the next model.
- Each request starts at the top of the chain — fallback is never sticky or shared between requests.
- When every model is out, `/chat` returns **503 with `Retry-After`** instead of a raw error.

**Known limitation:** falling back between models of one provider handles *quota exhaustion*, not a Google outage or an invalid key — all models fail together then. Providers are just `base_url` + key entries in `app/services/llm/registry.py`, so a second OpenAI-compatible provider can be added for cross-provider fallback.

---

## What's included

- **LangGraph** stateful agent with checkpointing, tool calling, and human-in-the-loop support
- **Long-term memory** via mem0 + pgvector — semantic search per user, cache-backed
- **LLM service** with quota-aware fallback across Gemini models, retries for transient errors, per-call and total timeout budgets
- **Langfuse** tracing on all LLM calls; Prometheus metrics + Grafana dashboards
- **JWT auth** with session management; rate limiting via slowapi
- **Alembic** migrations; optional Valkey/Redis cache layer
- **Structured logging** with request/session/user context on every line

## Quickstart

```bash
git clone <repo-url> my-agent && cd my-agent
cp .env.example .env.development   # set GEMINI_API_KEY and JWT_SECRET_KEY
make install
make docker-up                     # starts API + PostgreSQL
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) to see the interactive API.

> For local development without Docker see [docs/getting-started.md](docs/getting-started.md).

## Documentation

| Guide | What it covers |
|---|---|
| [Getting Started](docs/getting-started.md) | Prerequisites, local setup, first API call |
| [Architecture](docs/architecture.md) | System design, request flow, component diagrams |
| [Configuration](docs/configuration.md) | All environment variables with defaults |
| [Authentication](docs/authentication.md) | JWT flow, sessions, endpoint reference |
| [Database & Migrations](docs/database.md) | Schema, Alembic migrations, pgvector |
| [LLM Service](docs/llm-service.md) | Models, retries, fallback, timeout budget |
| [Memory](docs/memory.md) | mem0 long-term memory, cache layer |
| [Observability](docs/observability.md) | Langfuse, structured logging, Prometheus, profiling |
| [Evaluation](docs/evaluation.md) | Eval framework, custom metrics, reports |
| [Docker](docs/docker.md) | Docker, Compose, full monitoring stack |

## Project structure

```
app/
  api/v1/          # Route handlers
  core/
    langgraph/     # Agent graph + tools
    prompts/       # System prompt template
    cache.py       # Valkey/Redis + in-memory fallback
    config.py      # Settings
    middleware.py  # Metrics, logging context, profiling
    limiter.py     # Rate limiting
  models/          # SQLModel ORM models
  schemas/         # Pydantic request/response schemas
  services/        # LLM, database, memory services
alembic/           # Database migrations
evals/             # LLM evaluation framework
tests/             # Offline tests with stub models (uv run --group test pytest)
```

## Contributing

PRs welcome. Please read [docs/getting-started.md](docs/getting-started.md) to get your environment set up, then follow the coding conventions in [AGENTS.md](AGENTS.md).

Report security issues privately — see [SECURITY.md](SECURITY.md).

## License

See [LICENSE](LICENSE).

## FAQ

### General

**What is this template?**
A production-ready foundation for AI agent backends built on FastAPI + LangGraph. It bundles the components you'd otherwise wire up by hand: stateful conversations, long-term memory, tool calling, observability, rate limiting, and JWT auth.

**How does this differ from a basic LangGraph setup?**
The base LangGraph quickstart stops at "agent runs locally". This template adds Alembic migrations, mem0 + pgvector long-term memory, Langfuse tracing, Prometheus + Grafana dashboards, JWT sessions, slowapi rate limiting, structured logging with per-request context, and a circular-fallback LLM service — production concerns you'd otherwise build separately.

### Setup & Configuration

**Do I need Docker?**
Recommended but not required. `make docker-up` starts the API + PostgreSQL together. For local-only setup see [docs/getting-started.md](docs/getting-started.md).

**Which LLM providers are supported?**
Any provider with an **OpenAI-compatible endpoint**. The default is the Gemini API free tier. Providers are `base_url` + key entries in `PROVIDERS` in `app/services/llm/registry.py`; models are `LLM_CONFIGS` entries with a `provider` and a `role`:

```python
# app/services/llm/registry.py
LLM_CONFIGS: List[Dict[str, Any]] = [
    {"name": "gemini-3.5-flash-lite", "model": "gemini-3.5-flash-lite", "provider": "gemini", "role": ROLE_AGENT,
     "kwargs": {"max_completion_tokens": settings.MAX_TOKENS}},
    ...
]
```

Keeping every provider OpenAI-compatible means one client and one error hierarchy, which the quota-aware fallback relies on. List the exact model IDs your key can use with `curl "https://generativelanguage.googleapis.com/v1beta/models" -H "x-goog-api-key: $GEMINI_API_KEY"`.

**How do I configure long-term memory?**
Long-term memory is self-hosted: mem0 runs in-process and persists into your existing PostgreSQL via pgvector — there is no separate mem0 cloud account or API key. It uses your `GEMINI_API_KEY` for fact extraction (Flash Lite) and embeddings (Gemini Embedding 2 at 1536 dims, under pgvector's 2000-dim HNSW limit). See [docs/memory.md](docs/memory.md) for details.

### Development

**How do I add a custom tool?**
Drop a LangChain `@tool`-decorated function in `app/core/langgraph/tools/` and register it in the `tools` list exported from that package. The agent picks it up on next start; no graph changes needed.

**Can I run multiple agents (supervisor, sub-agents)?**
Yes. The template ships a single agent, but any compiled graph can be added as a node of the main graph. It shares `GraphState` (so `messages` flow through) and inherits the parent's Postgres checkpointer, so no extra persistence setup is needed. In `create_graph()` in `app/core/langgraph/graph.py`:

```python
research = StateGraph(GraphState)
research.add_node("research", research_node)
research.set_entry_point("research")
research_agent = research.compile()  # no checkpointer: inherits the parent's

graph_builder.add_node("chat", self._chat, destinations=("tool_call", "research_agent", END))
graph_builder.add_node("research_agent", research_agent)
graph_builder.add_edge("research_agent", "chat")
```

`chat` then acts as the supervisor: return `Command(goto="research_agent")` to delegate, and the sub-agent's reply comes back to `chat`. Add one node per specialist. For a prebuilt version, see [`langgraph-supervisor`](https://github.com/langchain-ai/langgraph-supervisor-py).

**How does the LLM service handle failures?**
Each failure is classified by what it means for the next attempt: a 429 blocks that model (for Google's `retryDelay`, or until the daily reset) and moves to the next model with its own quota; a transient 5xx is retried on the same model via `tenacity`; an auth error stops immediately. A per-call timeout lets a hung model be skipped, and a total timeout caps the whole call. If no model can answer, the API returns 503 with `Retry-After`. See [docs/llm-service.md](docs/llm-service.md).

**Can I use this without Langfuse?**
Yes. Set `LANGFUSE_TRACING_ENABLED=false` (or omit the Langfuse keys). The agent runs unchanged; structured logs still capture request/session/user context.

### Troubleshooting

**The API won't start**
- Ensure PostgreSQL is running (`make docker-up` brings it up alongside the API)
- Confirm `.env.development` exists — copy from `.env.example` and fill in required keys
- Apply migrations: `make migrate`

**Memory / semantic search returns nothing**
- Verify the `pgvector` extension is enabled in your PostgreSQL instance
- Confirm `GEMINI_API_KEY` is valid (mem0 calls Gemini for fact extraction + embeddings)
- If you changed `LONG_TERM_MEMORY_EMBEDDING_DIMS`, drop the `longterm_memory` table so mem0 recreates it
- Check `LONG_TERM_MEMORY_MODEL` and `LONG_TERM_MEMORY_EMBEDDER_MODEL` are set in `.env.development`

**Chat returns 503 "usage limit reached"**
Every model in the agent chain hit its free-tier quota. Per-minute limits clear within a minute; daily limits reset at midnight Pacific. The `Retry-After` header says how long to wait, and `llm_model_blocked` log lines show which model hit which limit.

**Rate limiting is too aggressive**
Limits are defined in `app/core/limiter.py` (slowapi). Adjust per-route decorators or the default rate in that file. See [docs/configuration.md](docs/configuration.md) for the related env vars.
