#!/usr/bin/env python3
"""Paper runner: weatherbotyes2re strategy + Gamma/CLOB/CheckWX infra.

ARM/FIRE + consensus filter + capped FAK paper fills.
Idle scan ~20s; ARM stations poll METAR/books at fast_poll_interval_seconds.

No wallet, no private key, no live order submission. No σ.

Usage:
  export CHECKWX_API_KEY=...
  python3 reversal_runner.py once   --config config/yes2re_reversal.json
  python3 reversal_runner.py run    --config config/yes2re_reversal.json
  python3 reversal_runner.py status --config config/yes2re_reversal.json
"""
from __future__ import annotations

import argparse
import json
import re
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

DEFAULTS = {
    "mode": "paper",
    "scan_interval_seconds": 20,
    "fast_poll_interval_seconds": 8,
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

# Process-local tracker (not serialized; rebuilds from live books each cycle)
_TRACKER: ConsensusTracker | None = None
_LAST_TAF: dict[str, Any] = {"at": 0.0, "extreme": {}}


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
        "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
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
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, default=str) + "\n")


def local_dates_for(cities: list[dict], lookahead_days: int = 1) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    now = datetime.now(timezone.utc)
    for city in cities:
        tz = ZoneInfo(city["timezone"])
        dates = []
        for delta in range(lookahead_days):
            d = (now + timedelta(days=delta)).astimezone(tz).date().isoformat()
            if d not in dates:
                dates.append(d)
        out[city["icao"]] = dates
    return out


def parse_obs_time(raw_metar: str) -> datetime | None:
    if not raw_metar:
        return None
    m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", raw_metar)
    if not m:
        return None
    now = datetime.now(timezone.utc)
    day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        dt = now.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return None
    if (now - dt).total_seconds() < -6 * 3600:
        return None
    if (now - dt).total_seconds() > 18 * 3600:
        return None
    return dt


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
        "best_bid": str(getattr(view, "best_bid", None)) if getattr(view, "best_bid", None) is not None else None,
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


def run_cycle(cfg: dict, state: dict, now: datetime) -> bool:
    """Returns True if any station is ARMed (caller may sleep faster)."""
    env = common.load_env()
    api_key = env.get(cfg["checkwx_api_key_env"], "")
    if not api_key:
        print("missing CHECKWX_API_KEY", file=sys.stderr)
        return False

    cities_list = common.load_cities(cfg["contract_cities_path"])
    by_id = {c["city_id"]: c for c in cities_list}
    by_icao = {c["icao"]: c for c in cities_list}
    fee_rate = Decimal(str(cfg["base_fee_rate"]))
    budget_usdc = Decimal(str(cfg["fire_budget_usdc"]))
    strat = cfg["strategy"]
    market_data = CLOBMarketData(timeout_seconds=8.0)
    tracker = get_tracker(cfg)
    ensure_re_state(state)

    local_dates = local_dates_for(cities_list, 1)
    rules, _failures = market_adapter.refresh_market_rules(by_id, local_dates)
    rules_by_city_date = {(r["city_id"], r["market_local_date"]): r for r in rules}

    # TAF cache
    global _LAST_TAF
    if time.time() - float(_LAST_TAF.get("at") or 0) > float(cfg.get("taf_refresh_interval_seconds", 1800)):
        try:
            tafs = common.checkwx_taf([c["icao"] for c in cities_list], api_key)
        except RuntimeError:
            tafs = {}
        extreme: dict[str, dict] = {}
        for city in cities_list:
            raw = tafs.get(city["icao"])
            if not raw:
                continue
            parsed = common.parse_tx_tn(raw)
            local_d = common.local_date_for(city, now)
            if parsed.get("tx_c") is not None:
                extreme[city["icao"]] = {
                    "direction": "high",
                    "extreme_c": float(parsed["tx_c"]),
                    "market_local_date": local_d,
                }
            if parsed.get("tn_c") is not None:
                # prefer high if both present for afternoon strategy; still store low separately
                extreme.setdefault(city["icao"] + "|low", {
                    "direction": "low",
                    "extreme_c": float(parsed["tn_c"]),
                    "market_local_date": local_d,
                })
                if city["icao"] not in extreme:
                    extreme[city["icao"]] = {
                        "direction": "low",
                        "extreme_c": float(parsed["tn_c"]),
                        "market_local_date": local_d,
                    }
        _LAST_TAF = {"at": time.time(), "extreme": extreme}
    taf_extreme = _LAST_TAF.get("extreme") or {}

    # ARM: only refresh those ICAOs (fast poll). Idle: full universe for consensus.
    armed_ids = {str(k).split("|")[0] for k in ensure_re_state(state).get("armed", {})}
    focus = [c for c in cities_list if c["city_id"] in armed_ids] if armed_ids else cities_list
    try:
        metars = common.checkwx_metar([c["icao"] for c in focus], api_key)
    except RuntimeError:
        metars = {}

    focus_ids = {c["city_id"] for c in focus}
    tokens: list[str] = []
    for r in rules:
        if r.get("city_id") not in focus_ids:
            continue
        for b in r.get("buckets", []):
            if b.get("yes_token_id"):
                tokens.append(b["yes_token_id"])
            if b.get("no_token_id"):
                tokens.append(b["no_token_id"])
    books_raw = market_data.fetch_books(tokens)
    books: dict[str, dict] = {}
    for token, snap in books_raw.items():
        view = orderbook.from_book_snapshot(snap)
        d = book_view_to_dict(view)
        if d:
            books[token] = d

    any_armed = False
    for icao, city in by_icao.items():
        taf_info = taf_extreme.get(icao)
        raw = metars.get(icao)
        temp_c = common.parse_metar_temp_c(raw) if raw else None
        # Allow no TAF: strategy can fall back to market rank-1
        direction = taf_info["direction"] if taf_info else "high"
        market_local_date = (
            taf_info["market_local_date"] if taf_info else common.local_date_for(city, now)
        )
        extreme_c = float(taf_info["extreme_c"]) if taf_info else None
        rule = rules_by_city_date.get((city["city_id"], market_local_date))
        if rule is None or temp_c is None:
            continue
        # Always sample books for consensus even if obs is duplicate later
        tracker.record_books(
            city["city_id"],
            market_local_date,
            direction,
            rule.get("buckets", []),
            books,
            now,
        )
        obs_time = parse_obs_time(raw) or now
        actions = maybe_arm_or_fire(
            state,
            city,
            market_local_date,
            direction,
            rule.get("buckets", []),
            extreme_c,
            temp_c,
            obs_time,
            now,
            books_by_token=books,
            config=strat,
            consensus_tracker=tracker,
        )
        for action in actions:
            atype = action.get("action_type")
            if atype == "re_arm":
                any_armed = True
                log_event(cfg["log_path"], {"type": "re_arm", **{k: v for k, v in action.items() if k != "action_type"}})
            elif atype == "re_fire":
                log_event(
                    cfg["log_path"],
                    {
                        "type": "re_fire",
                        "key": action.get("key"),
                        "icao": action.get("icao"),
                        "jump": action.get("jump"),
                        "ref_source": action.get("ref_source"),
                        "ref_extreme": action.get("ref_extreme"),
                        "taf_extreme": action.get("taf_extreme"),
                        "running_extreme": action.get("running_extreme"),
                        "consensus": action.get("consensus"),
                        "legs": action.get("legs"),
                    },
                )
                _execute_fire(cfg, state, action, books, budget_usdc, fee_rate, now)
            elif atype in ("re_skip", "re_skip_yes", "re_disarm"):
                if action.get("reason") in ("consensus_filter", "stale_obs", "hour_not_in_window"):
                    log_event(
                        cfg["log_path"],
                        {"type": atype, "reason": action.get("reason"), "key": action.get("key"),
                         "consensus": action.get("consensus")},
                    )

    if ensure_re_state(state).get("armed"):
        any_armed = True

    _settle(cfg, state, by_icao, now)
    return any_armed


def _execute_fire(cfg, state, fire, books, budget_usdc, fee_rate, now) -> None:
    remaining = re_execution.size_legs(fire, budget_usdc)
    for name in list(remaining):
        if remaining[name] <= 0:
            remaining.pop(name)
    if not remaining:
        return
    for elapsed_ms in (0, 1600, 4100, 8100):
        for intent in re_execution.plan_fire_cycle(fire, books, remaining, now, elapsed_ms):
            leg = intent.get("leg")
            if intent.get("status") != "send_fak":
                log_event(
                    cfg["log_path"],
                    {
                        "type": "re_fill_attempt",
                        "key": fire.get("key"),
                        "leg": leg,
                        "status": intent.get("status"),
                        "best_ask": intent.get("best_ask"),
                        "cap": intent.get("cap"),
                    },
                )
                continue
            token = intent["token_id"]
            limit = Decimal(intent["limit_price"])
            shares = Decimal(intent["shares"])
            book = books.get(str(token))
            match = re_execution.paper_match_fak(book if book is not None else {}, limit, shares)
            if match["filled_shares"] > 0:
                remaining[leg] = match["unfilled"]
                _open_position(cfg, state, fire, leg, token, match, limit, fee_rate, now)
                log_event(
                    cfg["log_path"],
                    {
                        "type": "re_fill",
                        "key": fire.get("key"),
                        "leg": leg,
                        "filled_shares": str(match["filled_shares"]),
                        "avg_price": str(match["avg_price"]),
                        "cost_usdc": str(match["cost"]),
                    },
                )
            else:
                remaining[leg] = match["unfilled"]
                log_event(
                    cfg["log_path"],
                    {
                        "type": "re_fill_attempt",
                        "key": fire.get("key"),
                        "leg": leg,
                        "status": "no_fill",
                        "elapsed_ms": elapsed_ms,
                    },
                )
        if not any(v > 0 for v in remaining.values()):
            break


def _open_position(cfg, state, fire, leg, token, match, limit, fee_rate, now) -> None:
    cost = match["cost"] + (match["cost"] * fee_rate)
    if paper_capital.reserve(state, cost) is None:
        log_event(cfg["log_path"], {"type": "entry_skip", "key": fire.get("key"), "leg": leg, "reason": "insufficient_capital"})
        return
    open_count = sum(1 for p in state["positions"].values() if not p.get("settled"))
    if open_count >= int(cfg.get("max_open_positions", 12)):
        log_event(cfg["log_path"], {"type": "entry_skip", "key": fire.get("key"), "leg": leg, "reason": "max_positions"})
        return
    if leg == "buy_no_broken":
        outcome, bucket_id = "NO", fire["broken_bucket_id"]
    else:
        outcome, bucket_id = "YES", fire["new_bucket_id"]
    pos_key = f"{fire['city_id']}|{fire['market_local_date']}|{fire['direction']}|{outcome}|{bucket_id}"
    if pos_key in state["positions"]:
        return
    state["positions"][pos_key] = {
        "strategy": "weatherbotyes2re",
        "leg": leg,
        "city_id": fire["city_id"],
        "icao": fire.get("icao"),
        "market_local_date": fire["market_local_date"],
        "direction": fire["direction"],
        "bucket_id": bucket_id,
        "side": outcome,
        "outcome": outcome,
        "token_id": token,
        "shares": str(match["filled_shares"]),
        "avg_price": str(match["avg_price"]) if match["avg_price"] is not None else str(limit),
        "cost_usdc": str(cost),
        "limit": str(limit),
        "ref_extreme": fire.get("ref_extreme"),
        "ref_source": fire.get("ref_source"),
        "running_extreme": fire.get("running_extreme"),
        "jump": fire.get("jump"),
        "opened_at": now.isoformat(),
    }
    state["entry_count"] = state.get("entry_count", 0) + 1


def _settle(cfg, state, by_icao, now) -> None:
    for pos_key, pos in list(state["positions"].items()):
        if pos.get("settled"):
            continue
        city = by_icao.get(pos["icao"])
        if city is None:
            continue
        tz = ZoneInfo(city["timezone"])
        local_start = datetime.fromisoformat(pos["market_local_date"]).replace(tzinfo=tz)
        local_day_end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
        if now <= local_day_end:
            continue
        res = market_adapter.fetch_market_resolution(
            city, pos["market_local_date"], pos["direction"], pos["bucket_id"]
        )
        if res is not None and res[0] is not None:
            outcome, resolution_source = res
            win = outcome == pos.get("outcome", "YES")
            _close_settled(cfg, state, pos_key, pos, win, "gamma", now, outcome, resolution_source)
            continue
        if now > local_day_end + timedelta(hours=cfg.get("settle_grace_hours", 6)):
            _close_settled(cfg, state, pos_key, pos, False, "obs_high_fail_closed", now, None, None)


def _close_settled(cfg, state, pos_key, pos, win, source, now, outcome, resolution_source) -> None:
    shares = Decimal(str(pos.get("shares", "0")))
    cost = Decimal(str(pos.get("cost_usdc", "0")))
    proceeds = shares if win else Decimal("0")
    pnl = proceeds - cost
    pos.update(
        {
            "settled": True,
            "settle_reason": "resolution" if win else "resolution_lost",
            "settled_at": now.isoformat(),
            "realized_pnl_usdc": str(pnl),
            "proceeds_usdc": str(proceeds),
        }
    )
    paper_capital.release(state, proceeds)
    log_event(
        cfg["log_path"],
        {
            "type": "settle",
            "position": pos_key,
            "win": win,
            "source": source,
            "outcome": outcome,
            "resolution_source": resolution_source,
            **pos,
        },
    )


def write_health(cfg: dict, state: dict) -> None:
    open_pos = [k for k, p in state["positions"].items() if not p.get("settled")]
    settled = [p for p in state["positions"].values() if p.get("settled")]
    realized = sum((Decimal(str(p.get("realized_pnl_usdc", "0"))) for p in settled), Decimal("0"))
    tree = ensure_re_state(state)
    health = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": cfg["mode"],
        "strategy": "weatherbotyes2re",
        "positions_open": len(open_pos),
        "positions_settled": len(settled),
        "realized_pnl_usdc": str(realized),
        "remaining_capital_usdc": str(paper_capital.remaining_capital_usdc(state)),
        "entry_count": state.get("entry_count", 0),
        "armed": list(tree.get("armed", {}).keys()),
        "fired": list(tree.get("fired", {}).keys()),
        "open": [state["positions"][k] for k in open_pos],
    }
    p = Path(cfg["health_path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(health, indent=2, default=str), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["once", "run", "status"])
    ap.add_argument("--config", default="config/yes2re_reversal.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state = load_state(cfg["state_path"])
    if state.get("version") != STATE_VERSION:
        capital = state.get("paper_initial_capital_usdc", cfg["paper_initial_capital_usdc"])
        state = {
            "positions": {},
            "weatherbotyes2re": {"armed": {}, "fired": {}, "running_extremes": {}, "last_obs_time": {}},
            "version": STATE_VERSION,
            "paper_initial_capital_usdc": capital,
        }
    state.setdefault("paper_initial_capital_usdc", cfg["paper_initial_capital_usdc"])
    ensure_re_state(state)

    if args.command == "status":
        write_health(cfg, state)
        print(json.dumps({
            "health": cfg["health_path"],
            "armed": list(ensure_re_state(state).get("armed", {})),
            "fired": list(ensure_re_state(state).get("fired", {})),
            "open": sum(1 for p in state["positions"].values() if not p.get("settled")),
            "entries": state.get("entry_count", 0),
        }, indent=2))
        return 0

    if args.command == "once":
        run_cycle(cfg, state, datetime.now(timezone.utc))
        save_state(cfg["state_path"], state)
        write_health(cfg, state)
        print("once cycle complete")
        return 0

    print("yes2re reversal paper runner started (no live orders)", flush=True)
    while True:
        try:
            armed = run_cycle(cfg, state, datetime.now(timezone.utc))
            save_state(cfg["state_path"], state)
            write_health(cfg, state)
        except Exception as exc:
            log_event(cfg["log_path"], {"type": "cycle_error", "error": type(exc).__name__, "detail": str(exc)[:300]})
            time.sleep(cfg["scan_interval_seconds"])
            continue
        sleep_s = cfg["fast_poll_interval_seconds"] if armed else cfg["scan_interval_seconds"]
        time.sleep(float(sleep_s))


if __name__ == "__main__":
    sys.exit(main())
