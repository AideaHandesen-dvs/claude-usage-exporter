# claude-usage-exporter

A tiny, zero-dependency **Prometheus exporter for [Claude Code](https://claude.com/claude-code) usage.**
It reads the JSONL transcript files Claude Code writes under `~/.claude/projects`,
and exposes token usage, an API-equivalent cost estimate, and the state of the
current **5-hour rolling rate-limit window** — including **how long until the limit resets**.

Point Prometheus at it, drop in the bundled Grafana dashboard, and you get this:

- tokens by model and type (input / output / cache read / cache write)
- API-equivalent cost in USD (what the same tokens would cost on the pay-as-you-go API)
- a live **countdown to the next limit reset**
- per-block usage so you can see how close you are to the cap

No API key, no network calls, no third-party Python packages — it only reads local files.

---

## Why this works

Claude Code records every assistant turn to `~/.claude/projects/<project>/<session>.jsonl`.
Each turn carries a `usage` block:

```json
{"type":"assistant","timestamp":"2026-06-10T13:21:17.543Z","requestId":"req_...",
 "message":{"model":"claude-opus-4-8","id":"msg_...",
   "usage":{"input_tokens":2113,"output_tokens":940,
            "cache_read_input_tokens":16281,
            "cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":2555}}}}
```

The exporter tails these files (incrementally — it remembers byte offsets and
deduplicates on `requestId|message.id`), aggregates the numbers, and reconstructs
the 5-hour usage blocks Claude uses for subscription rate limiting. That gives a
reliable answer to **"how much have I used, and when does it reset?"** — rendered
in Grafana.

> **About the reset window.** Claude Pro/Max plans meter usage in a rolling
> **5-hour window** that starts at your first activity (floored to the hour) and
> resets 5 hours later. The exporter reconstructs the active window and exposes
> its end time, so Grafana can show a countdown. (Plans may *also* have weekly
> limits; those are not modeled here.)

---

## Quick start

```bash
git clone https://github.com/<you>/claude-usage-exporter.git
cd claude-usage-exporter
python3 claude_usage_exporter.py
# -> serving metrics on 0.0.0.0:9183/metrics
curl -s localhost:9183/metrics | grep claude_block_seconds_until_reset
```

Requires Python 3.9+. That's the entire install.

### Docker

```bash
docker compose up -d   # mounts ~/.claude/projects read-only
```

### systemd (run as your user)

```bash
cp examples/claude-usage-exporter.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-usage-exporter
```

---

## Configuration

All via environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Where the transcripts live |
| `EXPORTER_PORT` | `9183` | Listen port |
| `EXPORTER_ADDR` | `0.0.0.0` | Bind address |
| `SCRAPE_INTERVAL_SEC` | `15` | Minimum seconds between full rescans (a scrape inside this window reuses cached state) |
| `BLOCK_TOKEN_LIMIT` | `0` | Optional token budget per 5h block. Set it to draw a "% of block used" gauge in Grafana. `0` disables. |

---

## Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `claude_usage_tokens_total` | counter | `model`, `type` | Cumulative tokens. `type` ∈ input, output, cache_read, cache_write_5m, cache_write_1h |
| `claude_usage_cost_usd_total` | counter | `model` | Cumulative API-equivalent cost (USD) |
| `claude_usage_messages_total` | counter | `model` | Cumulative assistant messages |
| `claude_block_tokens` | gauge | `type` | Tokens used in the active 5h block |
| `claude_block_cost_usd` | gauge | | API-equivalent cost in the active block |
| `claude_block_messages` | gauge | | Messages in the active block |
| `claude_block_active` | gauge | | `1` if a block is active right now |
| `claude_block_start_timestamp_seconds` | gauge | | Active block start (unix) |
| `claude_block_end_timestamp_seconds` | gauge | | **Reset time** (unix) |
| `claude_block_seconds_until_reset` | gauge | | **Seconds until reset** |
| `claude_block_token_limit` | gauge | | `BLOCK_TOKEN_LIMIT`, if set |
| `claude_usage_files_tracked` | gauge | | Transcript files discovered |
| `claude_usage_entries_total` | gauge | | Deduped usage entries parsed |
| `claude_usage_scrape_duration_seconds` | gauge | | Duration of the last rescan |
| `claude_usage_up` | gauge | | `1` if the last rescan succeeded |

### Useful PromQL

```promql
# Countdown to reset (seconds) — or use the gauge directly
claude_block_end_timestamp_seconds - time()

# % of the 5h block budget consumed (requires BLOCK_TOKEN_LIMIT)
100 * sum(claude_block_tokens) / claude_block_token_limit

# Spend over the last 24h
sum(increase(claude_usage_cost_usd_total[24h]))

# Output token rate per model
sum by (model) (rate(claude_usage_tokens_total{type="output"}[5m]))
```

---

## Pricing / cost model

Cost is **API-equivalent**, not what you're billed on a subscription. Rates
(USD per 1M tokens), from the Anthropic model catalog, with the standard cache
multipliers (read = 0.1×, write 5m = 1.25×, write 1h = 2×):

| Model | Input | Output | Cache read | Cache write 5m | Cache write 1h |
|---|---|---|---|---|---|
| Opus 4.x | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 |
| Sonnet 4.6 | $3.00 | $15.00 | $0.30 | $3.75 | $6.00 |
| Haiku 4.5 | $1.00 | $5.00 | $0.10 | $1.25 | $2.00 |

Models are matched by family keyword (`opus` / `sonnet` / `haiku`), so new point
releases keep working. Unknown models still count tokens but contribute $0 — edit
the `PRICING` table in `claude_usage_exporter.py` to add rates.

---

## Grafana

Import `grafana/dashboard.json`. It expects a Prometheus datasource and includes:

- a **reset countdown** stat (turns red as the window fills)
- block usage gauge (% of `BLOCK_TOKEN_LIMIT`, if configured)
- cumulative cost by model
- token throughput by type
- active-block cost and message count

A Prometheus scrape config is in `examples/prometheus.yml`.

---

## Limitations

- Cost is an **API-equivalent estimate**, not subscription billing.
- Only the **5-hour** window is modeled; weekly limits are not.
- The reset reconstruction follows the
  [`ccusage`](https://github.com/ryoppippi/ccusage) block heuristic (hour-floored
  start, 5h span, new block after a >5h gap). It's a faithful approximation of
  the client-visible window, not an authoritative read of server-side limits.

## License

MIT — see [LICENSE](LICENSE).
