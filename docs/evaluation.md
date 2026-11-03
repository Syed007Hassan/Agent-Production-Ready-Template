# Evaluation

The template includes a metric-based evaluation framework that fetches traces from Langfuse, scores them with LLM judges, and generates JSON reports.

## Running evaluations

```bash
make eval                        # interactive mode — prompts for settings
make eval-quick                  # runs with defaults, no prompts
make eval-no-report              # runs but skips report generation
make eval ENV=production         # run against production traces
```

## How it works

```mermaid
flowchart LR
    Langfuse -->|"fetch recent traces"| Evaluator
    Metrics["Metric prompts\nevals/metrics/prompts/*.md"] --> Evaluator
    Evaluator -->|"LLM judge\nper trace × metric"| Score["pass / fail"]
    Score --> Report["JSON report\nevals/reports/"]
```

1. **Fetch traces** — pulls recent LLM traces from Langfuse (configured via `LANGFUSE_*` env vars)
2. **Score** — for each trace × metric combination, an LLM judge evaluates the output and returns pass/fail
3. **Report** — a JSON report with aggregated stats and per-trace results is saved to `evals/reports/`

## Built-in metrics

| Metric | What it checks |
| --- | --- |
| `helpfulness` | Did the response actually help the user? |
| `conciseness` | Was the response appropriately concise? |
| `hallucination` | Did the response contain made-up facts? |
| `relevancy` | Was the response on-topic? |
| `toxicity` | Did the response contain harmful content? |

## Adding a custom metric

1. Create a markdown file in `evals/metrics/prompts/`:

```markdown
# My Metric

Evaluate whether the assistant response...

## Scoring

Return "pass" if... Return "fail" if...
```

2. The evaluator auto-discovers and applies all `.md` files in that directory.

## Report format

Reports are saved to `evals/reports/evaluation_report_YYYYMMDD_HHMMSS.json`:

```json
{
  "summary": {
    "total_traces": 50,
    "success_rate": 0.92,
    "duration_seconds": 34.2
  },
  "metrics": {
    "helpfulness": {"pass": 48, "fail": 2, "rate": 0.96},
    "hallucination": {"pass": 45, "fail": 5, "rate": 0.90}
  },
  "traces": [...]
}
```

## Eval LLM configuration

The judge is **one fixed model** with its own client — it never uses the agent's fallback chain, because switching judges mid-run would make scores incomparable between runs.

```bash
EVALUATION_LLM=gemini-3.5-flash-lite   # default
EVALUATION_BASE_URL=...                # defaults to GEMINI_BASE_URL
EVALUATION_API_KEY=...                 # defaults to GEMINI_API_KEY
EVALUATION_MAX_RPM=12                  # judge calls per minute
```

### Free-tier behaviour

- **Throttled:** judge calls are spaced to `EVALUATION_MAX_RPM` (default 12, under the free tier's 15 RPM). Each trace costs one call per metric.
- **Per-minute 429:** waits Google's `retryDelay`, then retries the same judge (tenacity, 3 attempts). 5xx and timeouts back off exponentially.
- **Daily 429:** the run **stops** instead of switching models. The report has `stopped_early: true` and `traces_remaining`. Scores are pushed only after every metric for a trace succeeds, so unscored traces are picked up by the next run — rerun after the daily reset (midnight Pacific).
- Only traces from the **last 24 hours** are fetched, so resume within a day.
- The judge's calls share the daily quota of `EVALUATION_LLM` with the app when they're the same model.
