#!/usr/bin/env python3
"""Paper runner: weatherbotyes2re strategy + Gamma/CLOB/CheckWX/AWC infra.

No sigma / bias / fade-NO / dead-NO / BUY-YES grid.
ARM/FIRE + consensus filter + capped FAK paper fills.

Polling model
  - Idle: full-universe METAR (~45s) + books for consensus (~30s)
  - ARM: those ICAOs poll METAR/books at fast_poll_interval_seconds (~8s)
  - Always keep slow book sampling for non-armed cities so TWAP never freezes

Usage:
  export CHECKWX_API_KEY=...
  python3 reversal_runner.py once  --config config/yes2re_reversal.json
  python3 reversal_runner.py run   --config config/yes2re_reversal.json --max-seconds 600
  python3 reversal_runner.py status --config config/yes2re_reversal.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import market_adapter
import paper_capital
from adapters.polymarket import orderbook
from clob_market_data import CLOBMarketData
from research import common
import re_execution
from consensus_tracker import ConsensusTracker
from reversal_strategy import ensure_re_state, maybe_arm_or_fire

STATE_VERSION = 2

DEFAULTS: dict[str, Any] = {
    "mode": "paper",
    "scan_interval_seconds": 20,
    "fast_poll_interval_seconds": 8,
    "idle_metar_interval_seconds": 45,
    "idle_book_interval_seconds": 30,
    "rules_refresh_interval_seconds": 1200,
    "taf_refresh_interval_seconds": 1800,
    "checkwx_api_key_env": "CHECKWX_API_KEY",
    "base_fee_rate": "0.02",
    "paper_initial_capital_usdc": 1000.0,
    "fire_budget_usdc": 20.0,
    "max_open_positions": 12,
    "settle_grace_hours": 6,
    "contract_cities_path": "config/contract_cities.json",
    "state_path": "data/yes2re_state.json",
    "log_path": "data/yes2re_events.jsonl",
    "health_path": "data/yes2re_health.json",
    "strategy": {
        "arm_c": 1.0,
        "max_bucket_jump": 1,
        "no_max_ask": "0.65",
        "yes_max_ask": "0.48",
        "no_notional_pct": "0.75",
        "yes_notional_pct": "0.25",
        "yes_leg_enabled": True,
        "require_fresh_obs_seconds": 180,
        "require_consensus_filter": True,
        "consensus_window_seconds": 7200,
        "consensus_min_samples": 20,
        "consensus_min_lead": "0.03",
        "allow_market_consensus_reference": True,
        "high_fire_local_hour": 14,
        "low_fire_local_hour_end": 10,
        "fast_poll_seconds": 8,
        "fire_budget_ms": 8000,
    },
}

_TRACKER: ConsensusTracker | None = None
_LAST_TAF: dict[str, Any] = {"at": 0.0, "extreme": {}}
_LAST_RULES: dict[str, Any] = {"at": 0.0, "rules": [], "by_city_date": {}}
_LAST_IDLE_METAR: float = 0.0
_LAST_IDLE_BOOK: float = 0.0
_METAR_CACHE: dict[str, dict[str, Any]] = {}
_BOOK_CACHE: dict[str, dict] = {}


def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    merged = json.loads(json.dumps(DEFAULTS))
    for key, value in cfg.items():
        if key == "strategy" and isinstance(value, dict):
            merged["strategy"].update(value)
        else:
            merged[key] = value
    if str(merged.get("mode", "paper")).lower() != "paper":
        raise SystemExit("live mode blocked — only paper is allowed in this runner")
    return merged


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            bak = p.with_suffix(p.suffix + ".bak")
            if bak.exists():
                return json.loads(bak.read_text(encoding="utf-8"))
    return {
        "positions": {},
        "weatherbotyes2re": {
            "armed": {},
            "fired": {},
            "running_extremes": {},
            "last_obs_time": {},
        },
        "version": STATE_VERSION,
    }


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        bak = p.with_suffix(p.suffix + ".bak")
        bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def log_event(path: str, event: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    event = dict(event)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(event, default=str) + "\n"
    for attempt in range(3):
        try:
            with p.open("a", encoding="utf-8") as fh:
                fh.write(line)
            return
        except OSError:
            time.sleep(0.05 * (attempt + 1))


def local_dates_for(cities: list[dict], lookahead_days: int = 1) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    now = datetime.now(timezone.utc)
    for city in cities:
        tz = ZoneInfo(city["timezone"])
        dates: list[str] = []
        for delta in range(lookahead_days):
            d = (now + timedelta(days=delta)).astimezone(tz).date().isoformat()
            if d not in dates:
                dates.append(d)
        out[city["icao"]] = dates
    return out


def book_view_to_dict(view) -> dict | None:
    if view is None:
        return None
    asks = []
    for level in getattr(view, "asks", ()) or ():
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            asks.append({"price": str(level[0]), "size": str(level[1])})
        elif isinstance(level, dict):
            asks.append({"price": str(level.get("price")), "size": str(level.get("size"))})
    best = getattr(view, "best_ask", None)
    tick = getattr(view, "tick_size", None) or Decimal("0.01")
    return {
        "best_ask": str(best) if best is not None else (asks[0]["price"] if asks else None),
        "best_bid": str(getattr(view, "best_bid", None))
        if getattr(view, "best_bid", None) is not None
        else None,
        "tick_size": str(tick),
        "asks": asks,
    }


def get_tracker(cfg: dict) -> ConsensusTracker:
    global _TRACKER
    strat = cfg["strategy"]
    if _TRACKER is None:
        _TRACKER = ConsensusTracker(
            window_seconds=int(strat.get("consensus_window_seconds", 7200)),
            min_samples=int(strat.get("consensus_min_samples", 20)),
        )
    return _TRACKER
