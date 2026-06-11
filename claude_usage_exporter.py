#!/usr/bin/env python3
"""
claude-usage-exporter
=====================

Prometheus exporter for Claude Code usage. Parses the JSONL transcript files
that Claude Code writes under ~/.claude/projects and exposes token usage, an
API-equivalent cost estimate, and the current 5-hour rolling-window ("block")
state — including the time remaining until the limit resets.

Zero third-party dependencies: stdlib only. Point Prometheus at /metrics and
build the Grafana dashboard from the metrics below.

Environment variables
---------------------
  CLAUDE_PROJECTS_DIR   transcript dir (default: ~/.claude/projects)
  EXPORTER_PORT         listen port   (default: 9183)
  EXPORTER_ADDR         bind address  (default: 0.0.0.0)
  SCRAPE_INTERVAL_SEC   min seconds between full rescans (default: 15)
  BLOCK_TOKEN_LIMIT     optional token budget per 5h block, for a % gauge
                        (default: 0 = disabled)

Metrics
-------
  claude_usage_tokens_total{model,type}     counter  cumulative tokens
  claude_usage_cost_usd_total{model}        counter  API-equivalent USD
  claude_usage_messages_total{model}        counter  assistant messages
  claude_block_tokens{type}                 gauge    tokens in active block
  claude_block_cost_usd                     gauge    USD in active block
  claude_block_messages                     gauge    messages in active block
  claude_block_active                       gauge    1 if a block is active now
  claude_block_start_timestamp_seconds      gauge    active block start (unix)
  claude_block_end_timestamp_seconds        gauge    reset time (unix)
  claude_block_seconds_until_reset          gauge    seconds until reset
  claude_block_token_limit                  gauge    BLOCK_TOKEN_LIMIT (if set)
  claude_usage_files_tracked                gauge    transcript files seen
  claude_usage_entries_total                gauge    deduped usage entries
  claude_usage_scrape_duration_seconds      gauge    last rescan duration
  claude_usage_up                           gauge    1 if last rescan ok

The "type" label is one of: input, output, cache_read, cache_write_5m,
cache_write_1h.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- configuration ---------------------------------------------------------
PROJECTS_DIR = os.path.expanduser(
    os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
)
PORT = int(os.environ.get("EXPORTER_PORT", "9183"))
ADDR = os.environ.get("EXPORTER_ADDR", "0.0.0.0")
SCRAPE_INTERVAL = float(os.environ.get("SCRAPE_INTERVAL_SEC", "15"))
BLOCK_TOKEN_LIMIT = int(os.environ.get("BLOCK_TOKEN_LIMIT", "0"))
# Local-time offset in hours for "today"/"this month" boundaries (e.g. 9 = JST).
USAGE_TZ_OFFSET = float(os.environ.get("USAGE_TZ_OFFSET", "0")) * 3600

# 5-hour rolling window used by Claude subscription rate limits.
BLOCK_SECONDS = 5 * 3600

# --- pricing (USD per 1,000,000 tokens) ------------------------------------
# Base input/output rates from the Anthropic model catalog. Cache rates follow
# the documented multipliers: cache read = 0.1x input, cache write (5m) =
# 1.25x input, cache write (1h) = 2x input. This yields an API-EQUIVALENT cost
# — what the same tokens would cost on the pay-as-you-go API. On a Pro/Max
# subscription you are not billed per token; treat this as a usage proxy.
PRICING = {
    # family keyword -> per-MTok rates
    "opus":   {"input": 5.0, "output": 25.0, "cache_read": 0.50,
               "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30,
               "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "haiku":  {"input": 1.0, "output": 5.0, "cache_read": 0.10,
               "cache_write_5m": 1.25, "cache_write_1h": 2.0},
}
ZERO_RATES = {k: 0.0 for k in
              ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")}


def rates_for(model: str) -> dict:
    m = (model or "").lower()
    for key, rates in PRICING.items():
        if key in m:
            return rates
    return ZERO_RATES


# --- state ------------------------------------------------------------------
class Entry:
    __slots__ = ("ts", "model", "input", "output",
                 "cache_read", "cw5m", "cw1h", "cost")

    def __init__(self, ts, model, inp, out, cr, cw5m, cw1h, cost):
        self.ts = ts
        self.model = model
        self.input = inp
        self.output = out
        self.cache_read = cr
        self.cw5m = cw5m
        self.cw1h = cw1h
        self.cost = cost


_lock = threading.Lock()
_entries: list[Entry] = []          # deduped usage entries, append-only
_seen: set[str] = set()             # dedup keys (requestId|message.id)
_offsets: dict[str, tuple[int, int]] = {}  # path -> (inode, byte offset)
_files_tracked = 0
_scrape_duration = 0.0
_scrape_ok = 0
_last_scan = 0.0


def _parse_ts(s: str) -> float | None:
    if not s:
        return None
    try:
        # transcripts use ISO 8601 with a trailing Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ingest_line(line: str) -> None:
    """Parse one JSONL line; append an Entry if it carries assistant usage."""
    try:
        d = json.loads(line)
    except (ValueError, json.JSONDecodeError):
        return
    if d.get("type") != "assistant":
        return
    msg = d.get("message") or {}
    usage = msg.get("usage")
    if not usage:
        return
    model = msg.get("model") or "unknown"
    if model == "<synthetic>":
        return

    key = f"{d.get('requestId')}|{msg.get('id')}"
    if key in _seen:
        return
    _seen.add(key)

    ts = _parse_ts(d.get("timestamp")) or 0.0
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cc = usage.get("cache_creation") or {}
    cw5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    # fall back to the flat field if the detailed breakdown is absent
    if not cw5m and not cw1h:
        cw5m = int(usage.get("cache_creation_input_tokens") or 0)

    r = rates_for(model)
    cost = (inp * r["input"] + out * r["output"] + cr * r["cache_read"]
            + cw5m * r["cache_write_5m"] + cw1h * r["cache_write_1h"]) / 1_000_000

    _entries.append(Entry(ts, model, inp, out, cr, cw5m, cw1h, cost))


def _scan() -> None:
    """Incrementally read appended bytes from every transcript file."""
    global _files_tracked, _scrape_duration, _scrape_ok, _last_scan
    start = time.monotonic()
    files = 0
    try:
        for root, _dirs, names in os.walk(PROJECTS_DIR):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                files += 1
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                prev = _offsets.get(path)
                offset = 0
                if prev and prev[0] == st.st_ino and prev[1] <= st.st_size:
                    offset = prev[1]  # same file, resume where we stopped
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        for line in fh:
                            _ingest_line(line)
                        _offsets[path] = (st.st_ino, fh.tell())
                except OSError:
                    continue
        _files_tracked = files
        _scrape_ok = 1
    except Exception as exc:  # never let the scan thread die
        _scrape_ok = 0
        print(f"[claude-usage-exporter] scan error: {exc}", file=sys.stderr)
    finally:
        _scrape_duration = time.monotonic() - start
        _last_scan = time.time()


def _floor_hour(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0).timestamp()


def _day_start(now: float) -> float:
    """Epoch of local midnight today (local = UTC + USAGE_TZ_OFFSET)."""
    lt = datetime.fromtimestamp(now + USAGE_TZ_OFFSET, tz=timezone.utc)
    d = lt.replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp() - USAGE_TZ_OFFSET


def _month_start(now: float) -> float:
    """Epoch of local midnight on the 1st of the current month."""
    lt = datetime.fromtimestamp(now + USAGE_TZ_OFFSET, tz=timezone.utc)
    d = lt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp() - USAGE_TZ_OFFSET


def _active_block(entries: list[Entry], now: float):
    """Reconstruct 5h blocks and return the one active at `now` (or None).

    Mirrors ccusage: a block starts at the hour-floor of its first entry and
    spans 5 hours. A new block begins when an entry lands past the current
    block's end, or more than 5h after the previous entry.
    """
    if not entries:
        return None
    ordered = sorted(entries, key=lambda e: e.ts)
    cur = None
    last_ts = None
    blocks = []
    for e in ordered:
        if cur is None:
            start = _floor_hour(e.ts)
            cur = {"start": start, "end": start + BLOCK_SECONDS, "entries": [e]}
        elif e.ts >= cur["end"] or (last_ts is not None and e.ts - last_ts >= BLOCK_SECONDS):
            blocks.append(cur)
            start = _floor_hour(e.ts)
            cur = {"start": start, "end": start + BLOCK_SECONDS, "entries": [e]}
        else:
            cur["entries"].append(e)
        last_ts = e.ts
    if cur:
        blocks.append(cur)
    last = blocks[-1]
    return last if now < last["end"] else None


# --- metrics rendering ------------------------------------------------------
def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def render() -> str:
    now = time.time()
    with _lock:
        entries = list(_entries)
        files = _files_tracked
        dur = _scrape_duration
        ok = _scrape_ok

    # cumulative per-model totals
    tok: dict[str, dict[str, int]] = {}
    cost: dict[str, float] = {}
    msgs: dict[str, int] = {}
    for e in entries:
        t = tok.setdefault(e.model, {"input": 0, "output": 0, "cache_read": 0,
                                     "cache_write_5m": 0, "cache_write_1h": 0})
        t["input"] += e.input
        t["output"] += e.output
        t["cache_read"] += e.cache_read
        t["cache_write_5m"] += e.cw5m
        t["cache_write_1h"] += e.cw1h
        cost[e.model] = cost.get(e.model, 0.0) + e.cost
        msgs[e.model] = msgs.get(e.model, 0) + 1

    out = []

    def line(name, value, **labels):
        if labels:
            lbl = ",".join(f'{k}="{_esc(str(v))}"' for k, v in labels.items())
            out.append(f"{name}{{{lbl}}} {value}")
        else:
            out.append(f"{name} {value}")

    out.append("# HELP claude_usage_tokens_total Cumulative tokens by model and type.")
    out.append("# TYPE claude_usage_tokens_total counter")
    for model, t in sorted(tok.items()):
        for typ, val in t.items():
            line("claude_usage_tokens_total", val, model=model, type=typ)

    out.append("# HELP claude_usage_cost_usd_total Cumulative API-equivalent cost (USD).")
    out.append("# TYPE claude_usage_cost_usd_total counter")
    for model, c in sorted(cost.items()):
        line("claude_usage_cost_usd_total", f"{c:.6f}", model=model)

    out.append("# HELP claude_usage_messages_total Cumulative assistant messages by model.")
    out.append("# TYPE claude_usage_messages_total counter")
    for model, c in sorted(msgs.items()):
        line("claude_usage_messages_total", c, model=model)

    # windowed totals: today / this month (local calendar via USAGE_TZ_OFFSET)
    day0 = _day_start(now)
    mon0 = _month_start(now)
    win = {"today": {"cost": 0.0, "tokens": 0, "messages": 0},
           "month": {"cost": 0.0, "tokens": 0, "messages": 0}}
    for e in entries:
        if e.ts >= mon0:
            tt = e.input + e.output + e.cache_read + e.cw5m + e.cw1h
            win["month"]["cost"] += e.cost
            win["month"]["tokens"] += tt
            win["month"]["messages"] += 1
            if e.ts >= day0:
                win["today"]["cost"] += e.cost
                win["today"]["tokens"] += tt
                win["today"]["messages"] += 1

    out.append("# HELP claude_usage_cost_usd_today API-equivalent cost since local midnight.")
    out.append("# TYPE claude_usage_cost_usd_today gauge")
    line("claude_usage_cost_usd_today", f"{win['today']['cost']:.6f}")
    out.append("# HELP claude_usage_cost_usd_month API-equivalent cost this calendar month.")
    out.append("# TYPE claude_usage_cost_usd_month gauge")
    line("claude_usage_cost_usd_month", f"{win['month']['cost']:.6f}")
    out.append("# HELP claude_usage_tokens_today Total tokens since local midnight.")
    out.append("# TYPE claude_usage_tokens_today gauge")
    line("claude_usage_tokens_today", win["today"]["tokens"])
    out.append("# HELP claude_usage_tokens_month Total tokens this calendar month.")
    out.append("# TYPE claude_usage_tokens_month gauge")
    line("claude_usage_tokens_month", win["month"]["tokens"])
    out.append("# HELP claude_usage_messages_today Assistant messages since local midnight.")
    out.append("# TYPE claude_usage_messages_today gauge")
    line("claude_usage_messages_today", win["today"]["messages"])
    out.append("# HELP claude_usage_messages_month Assistant messages this calendar month.")
    out.append("# TYPE claude_usage_messages_month gauge")
    line("claude_usage_messages_month", win["month"]["messages"])

    # active 5h block
    block = _active_block(entries, now)
    b_tok = {"input": 0, "output": 0, "cache_read": 0,
             "cache_write_5m": 0, "cache_write_1h": 0}
    b_cost = 0.0
    b_msgs = 0
    b_active = 0
    b_start = 0.0
    b_end = 0.0
    until = 0.0
    if block:
        b_active = 1
        b_start = block["start"]
        b_end = block["end"]
        until = max(0.0, b_end - now)
        for e in block["entries"]:
            b_tok["input"] += e.input
            b_tok["output"] += e.output
            b_tok["cache_read"] += e.cache_read
            b_tok["cache_write_5m"] += e.cw5m
            b_tok["cache_write_1h"] += e.cw1h
            b_cost += e.cost
            b_msgs += 1

    out.append("# HELP claude_block_tokens Tokens used in the active 5h block by type.")
    out.append("# TYPE claude_block_tokens gauge")
    for typ, val in b_tok.items():
        line("claude_block_tokens", val, type=typ)

    out.append("# HELP claude_block_cost_usd API-equivalent cost in the active 5h block.")
    out.append("# TYPE claude_block_cost_usd gauge")
    line("claude_block_cost_usd", f"{b_cost:.6f}")

    out.append("# HELP claude_block_messages Assistant messages in the active 5h block.")
    out.append("# TYPE claude_block_messages gauge")
    line("claude_block_messages", b_msgs)

    out.append("# HELP claude_block_active 1 if a 5h block is currently active.")
    out.append("# TYPE claude_block_active gauge")
    line("claude_block_active", b_active)

    out.append("# HELP claude_block_start_timestamp_seconds Active block start (unix).")
    out.append("# TYPE claude_block_start_timestamp_seconds gauge")
    line("claude_block_start_timestamp_seconds", f"{b_start:.0f}")

    out.append("# HELP claude_block_end_timestamp_seconds Limit reset time (unix).")
    out.append("# TYPE claude_block_end_timestamp_seconds gauge")
    line("claude_block_end_timestamp_seconds", f"{b_end:.0f}")

    out.append("# HELP claude_block_seconds_until_reset Seconds until the limit resets.")
    out.append("# TYPE claude_block_seconds_until_reset gauge")
    line("claude_block_seconds_until_reset", f"{until:.0f}")

    out.append("# HELP claude_block_token_limit Configured token budget per block (0=unset).")
    out.append("# TYPE claude_block_token_limit gauge")
    line("claude_block_token_limit", BLOCK_TOKEN_LIMIT)

    # exporter health
    out.append("# HELP claude_usage_files_tracked Transcript files discovered.")
    out.append("# TYPE claude_usage_files_tracked gauge")
    line("claude_usage_files_tracked", files)

    out.append("# HELP claude_usage_entries_total Deduped usage entries parsed.")
    out.append("# TYPE claude_usage_entries_total gauge")
    line("claude_usage_entries_total", len(entries))

    out.append("# HELP claude_usage_scrape_duration_seconds Duration of the last rescan.")
    out.append("# TYPE claude_usage_scrape_duration_seconds gauge")
    line("claude_usage_scrape_duration_seconds", f"{dur:.4f}")

    out.append("# HELP claude_usage_up 1 if the last rescan succeeded.")
    out.append("# TYPE claude_usage_up gauge")
    line("claude_usage_up", ok)

    return "\n".join(out) + "\n"


# --- http -------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        # rescan at most once per SCRAPE_INTERVAL, regardless of scrape rate
        global _last_scan
        with _lock:
            due = time.time() - _last_scan >= SCRAPE_INTERVAL
            if due:
                _scan()
        body = render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type",
                         "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence default access log
        pass


def main():
    print(f"[claude-usage-exporter] projects dir: {PROJECTS_DIR}", file=sys.stderr)
    print(f"[claude-usage-exporter] listening on {ADDR}:{PORT}/metrics",
          file=sys.stderr)
    with _lock:
        _scan()  # warm the cache before the first scrape
    print(f"[claude-usage-exporter] initial scan: {len(_entries)} entries "
          f"from {_files_tracked} files", file=sys.stderr)
    ThreadingHTTPServer((ADDR, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
