# Configuration

All configuration is read from environment variables. Use `.env.development`, `.env.staging`, or `.env.production` — the app loads the right file based on the `APP_ENV` variable.

Copy `.env.example` to get started:

```bash
cp .env.example .env.development
```

---

## Application

| Variable | Default | Description |
| --- | --- | --- |
| `APP_ENV` | `development` | Environment: `development`, `staging`, `production`, `test` |
| `PROJECT_NAME` | `FastAPI LangGraph Template` | Displayed in API docs and logs |
| `VERSION` | `1.0.0` | API version |
| `DEBUG` | `false` | Enables debug logging and profiling middleware |
| `API_V1_STR` | `/api/v1` | API prefix |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins |

---

## LLM

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | — | Yes | Gemini API key from [AI Studio](https://aistudio.google.com/apikey) (free tier; do not link billing) |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | No | Gemini's OpenAI-compatible endpoint |
| `DEFAULT_LLM_MODEL` | `gemini-3.5-flash-lite` | No | Starting agent model — see [LLM Service](llm-service.md) for fallback order |
| `SESSION_NAMING_MODEL` | `gemma-4-31b-it` | No | Model for auto-generated session titles (separate quota from the agent) |
| `DEFAULT_LLM_TEMPERATURE` | `0.2` | No | Temperature for chat completions |
| `MAX_TOKENS` | `2000` | No | Max tokens per LLM response |
| `MAX_LLM_CALL_RETRIES` | `2` | No | Attempts per model on transient 5xx errors; quota errors switch model immediately |
| `LLM_CALL_TIMEOUT` | `20` | No | Max seconds for one model call before falling back |
| `LLM_TOTAL_TIMEOUT` | `60` | No | Max seconds for the entire fallback loop |
| `SESSION_NAMING_ENABLED` | `true` | No | Auto-generate a session title from the user's first message using an LLM background task |

---

## Long-term memory

| Variable | Default | Description |
| --- | --- | --- |
| `LONG_TERM_MEMORY_COLLECTION_NAME` | `longterm_memory` | pgvector collection name |
| `LONG_TERM_MEMORY_MODEL` | `gemini-3.5-flash-lite` | Registry model used by mem0 to extract memories |
| `LONG_TERM_MEMORY_EMBEDDER_MODEL` | `gemini-embedding-2` | Embedding model for semantic search |
| `LONG_TERM_MEMORY_EMBEDDING_DIMS` | `1536` | Embedding size; must be ≤ 2000 (pgvector HNSW limit) |
| `MEM0_TELEMETRY` | `True` (mem0 default) | Set `False` to stop mem0 sending usage events to PostHog |

---

## Database

| Variable | Default | Description |
| --- | --- | --- |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `food_order_db` | Database name |
| `POSTGRES_USER` | `postgres` | Database user |
| `POSTGRES_PASSWORD` | `postgres` | Database password |
| `POSTGRES_POOL_SIZE` | `20` | SQLAlchemy connection pool size |
| `POSTGRES_MAX_OVERFLOW` | `10` | Max overflow connections above pool size |

---

## Auth

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `JWT_SECRET_KEY` | — | Yes | Secret used to sign JWT tokens — use a long random string in production |
| `JWT_ALGORITHM` | `HS256` | No | JWT signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_DAYS` | `30` | No | Token lifetime in days |

---

## Cache (Valkey/Redis — optional)

When `VALKEY_HOST` is set, the app uses Valkey/Redis for memory search caching and rate limiting. When absent, it falls back to an in-memory TTL cache (not shared across instances).

| Variable | Default | Description |
| --- | --- | --- |
| `VALKEY_HOST` | `` (disabled) | Valkey/Redis host — leave empty to use in-memory fallback |
| `VALKEY_PORT` | `6379` | Port |
| `VALKEY_DB` | `0` | Database index |
| `VALKEY_PASSWORD` | `` | Password (if required) |
| `VALKEY_MAX_CONNECTIONS` | `20` | Connection pool size |
| `CACHE_TTL_SECONDS` | `60` | TTL for cached memory search results |

---

## Observability (Langfuse)

| Variable | Default | Description |
| --- | --- | --- |
| `LANGFUSE_TRACING_ENABLED` | `true` | Set to `false` to disable tracing entirely |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | — | Langfuse project secret key |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse host (self-hosted or cloud) |

---

## Rate limiting

| Variable | Default | Description |
| --- | --- | --- |
| `RATE_LIMIT_DEFAULT` | `200 per day, 50 per hour` | Fallback limit |
| `RATE_LIMIT_CHAT` | `30 per minute` | POST /chat |
| `RATE_LIMIT_CHAT_STREAM` | `20 per minute` | POST /chat/stream |
| `RATE_LIMIT_MESSAGES` | `50 per minute` | GET/DELETE /messages |
| `RATE_LIMIT_LOGIN` | `20 per minute` | POST /auth/login |
| `RATE_LIMIT_REGISTER` | `10 per hour` | POST /auth/register |

When Valkey is configured, rate limiting is shared across all app instances. Without it, limits are per-process.

---

## Profiling (debug only)

Only active when `DEBUG=true`. Profiles every request and saves a JSON report when the request exceeds the threshold.

| Variable | Default | Description |
| --- | --- | --- |
| `PROFILING_DIR` | `/tmp/fastapi_profiles` | Directory for profile JSON files |
| `PROFILING_THRESHOLD_SECONDS` | `2.0` | Minimum wall time to trigger saving a profile. Set to `0` to profile every request. |

---

## Logging

| Variable | Default (dev) | Default (prod) | Description |
| --- | --- | --- | --- |
| `LOG_LEVEL` | `DEBUG` | `WARNING` | Log level |
| `LOG_FORMAT` | `console` | `json` | `console` for coloured dev output, `json` for structured production logs |
