"""Live-cycle orchestration for the weatherbotyes2re paper reversal runner.

Reconstructs the intended ``_r_cycle`` module (the ``run_cycle`` import in
``runner_impl.py``). Pure orchestration over the existing data/strategy/fill
modules; no sigma, no live orders, paper fills only. Every externally useful
event is appended to the JSONL log for the watcher/Hermes layer.

``run_cycle(cfg, state, now_utc) -> bool``  (True while any session stays ARMed)

Cycle responsibilities (cadence-gated per-process in :mod:`_r_globals`):
  1. load the active city universe (contract registry filtered by cfg)
  2. discover today's Gamma rules (high/low bucket markets) per city, ~rules TTL
  3. dual-source METAR at metar cadence; convert to market unit
  4. refresh CLOB book ladders for every in-play token at book cadence
  5. continuously sample books into the consensus tracker (even w/o new METAR)
  6. feed each fresh METAR obs through ``maybe_arm_or_fire``
  7. on ``re_fire`` run the capped-FAK paper fire window + reserve cash + record
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import market_adapter
import re_execution
from _r_exec import settle_markets
from _r_globals import book_cache, bump, clob, set_health_extra, stamp, tracker
from _r_state import DEFAULTS, log_event
from adapters.polymarket.orderbook import from_any
from paper_capital import reserve
from research import common
from reversal_strategy import ensure_re_state, maybe_arm_or_fire, prune_stale_sessions
from ws_bridge import ws_bridge

LOG_FIELDS = None
ZERO = Decimal("0")

# Which side token we sample for consensus.
# Last-good METAR map, carried so telemetry (and the watcher reading health)
# reports real per-city obs ages even between METAR fetches (~45s cadence).
_LAST_GOOD_METAR: dict[str, dict[str, Any]] = {}

# Last-good TAF parse map {ICAO: {tx_c, tn_c, issue_dt, tx_valid_utc, tn_valid_utc, raw}}.
# TAF updates only every 4-6h; on pull failure we keep last-good and retry on
# the next due cycle (fail closed, same contract as _LAST_GOOD_METAR).
_LAST_GOOD_TAF: dict[str, dict[str, Any]] = {}

# duplicate_obs_time skip logs throttled per key (expected every fast cycle at
# 5s cadence — logging all of them would add ~98 lines/cycle to the JSONL).
_DUP_LOG: dict[str, float] = {}
_DUP_LOG_INTERVAL_S = 300.0


# --------------------------------------------------------------------------- #
# Universe + date window
# --------------------------------------------------------------------------- #
def load_active_cities(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Full registry filtered to the configured active ICAOs (or all of them)."""
    allc = common.load_cities(cfg.get("contract_cities_path"))
    active = cfg.get("active_icaos")
    if not active:
        return list(allc)
    want = {str(x).strip().upper() for x in active if str(x).strip()}
    return [c for c in allc if str(c.get("icao", "")).upper() in want]


def target_dates_by_icao(
    cities: list[dict[str, Any]],
    cfg: dict[str, Any],
    now_utc: datetime,
) -> dict[str, list[str]]:
    """Map icao -> the local calendar date being traded (today, in city tz).

    A live reversal needs real METAR now vs a *current* consensus-ranked bucket,
    so we only open today's (local) daily market. Adjacent dates are surfaced
    for information but not driven by live METAR (their books belong to forecast
    markets whose prices are set ahead of time, not by intraday observation)."""
    out: dict[str, list[str]] = {}
    for city in cities:
        icao = str(city.get("icao", "")).upper()
        z = city.get("timezone")
        if not z:
            continue
        from zoneinfo import ZoneInfo
        local_date = now_utc.astimezone(ZoneInfo(z)).date().isoformat()
        out.setdefault(icao, []).append(local_date)
    return out


# --------------------------------------------------------------------------- #
# Rule discovery
# --------------------------------------------------------------------------- #
def _rules_key(city_id: str, local_date: str, direction: str) -> str:
    return f"{city_id}|{local_date}|{direction}"


def refresh_rules(
    cfg: dict[str, Any],
    cities: list[dict[str, Any]],
    dates: dict[str, list[str]],
    now_utc: datetime,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Refresh Gamma rule discovery on TTL; returns (index, failures).

    Index keyed ``city_id|date|direction`` -> rule dict (see
    ``market_adapter.parse_event_rules``). Caches failures by key so a down
    Gamma doesn't spam the log every tick (retried only at rules TTL)."""
    ttl = float(cfg.get("rules_refresh_interval_seconds", DEFAULTS["rules_refresh_interval_seconds"]))
    if time.time() - stamp("rules") < ttl:
        idx, failures = _load_rule_cache()
        if idx:
            return idx, failures
    cities_by_icao = {str(c["icao"]).upper(): c for c in cities}
    limited = {icao: cities_by_icao[icao] for icao in dates if icao in cities_by_icao}
    # Generous per-request timeout + deadline: 10 cities × 2 directions × 1 date
    # = 20 Gamma lookups. 5s/req with a 30s wall deadline lost ~30% on the first
    # soak (cold cache). Bump so one slow endpoint doesn't starve the others.
    rules, failures = market_adapter.refresh_market_rules(
        limited,
        dates,
        timeout_seconds=12.0,
        total_deadline_seconds=150.0,
    )
    idx: dict[str, Any] = {}
    for rule in rules:
        k = _rules_key(rule["city_id"], rule["market_local_date"], rule["direction"])
        idx[k] = rule
    _store_rule_cache(idx, failures)
    bump("rules", time.time())
    return idx, failures


_RULE_MEMO: dict[str, Any] = {"idx": {}, "failures": {}}


def _store_rule_cache(idx: dict[str, Any], failures: dict[str, str]) -> None:
    _RULE_MEMO["idx"] = idx
    _RULE_MEMO["failures"] = failures


def _load_rule_cache() -> tuple[dict[str, Any], dict[str, str]]:
    return _RULE_MEMO["idx"], _RULE_MEMO["failures"]


def _all_tokens_for_rules(rules: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for rule in rules.values():
        for b in rule.get("buckets", []):
            for side in ("yes_token_id", "no_token_id"):
                tok = str(b.get(side) or "")
                if tok and tok not in tokens:
                    tokens.append(tok)
    return tokens


def _rule_ref_extreme(rule: dict[str, Any], state: dict[str, Any]) -> float | None:
    """Reference extreme (market units) for a rule, from armed-session state if
    present, else None. The armed record stores the reference the strategy is
    currently watching (ref_extreme), which is exactly the bucket whose NO we
    would buy on a breach — so its ±1 neighbours are the fire-critical tokens."""
    tree = state.get("weatherbotyes2re", {})
    key = _rules_key(rule.get("city_id"), rule.get("market_local_date"), rule.get("direction"))
    armed = (tree.get("armed") or {}).get(key)
    if armed and armed.get("ref_extreme") is not None:
        try:
            return float(armed["ref_extreme"])
        except (TypeError, ValueError):
            return None
    return None


def _warm_tokens_for_rules(rules: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Fire-critical token subset to keep hot while sessions are armed.

    For every rule with an armed session we derive the reference bucket from
    the armed ref_extreme, then collect:
      - the reference bucket's NO token   (fire leg: BUY NO broken bucket)
      - the neighbour bucket on the likely breach side — high direction: the
        next-higher bucket (a higher observed extreme breaks into it, so its
        YES is the second fire leg); low direction: the next-lower bucket
      - that neighbour's NO token too (so a 2-bucket TAF breach can still fill)
    Buckets are located in temperature order around the reference. Boundary
    buckets (lo/hi None — "≤x" / "≥x") are handled the same way the strategy
    does: a None bound never excludes a value on that side.

    Returns a small deduped token list (tens, not the ~1000-token full set) so
    the armed fast-poll can refresh just these every few seconds and a fire
    finds a warm book instead of no_book.
    """
    warm: list[str] = []
    for rule in rules.values():
        ref = _rule_ref_extreme(rule, state)
        if ref is None:
            continue
        buckets = rule.get("buckets", [])
        if not buckets:
            continue
        # order by lo where present; open lower bound sorts first
        def _lo_key(b: dict[str, Any]) -> float:
            lo = b.get("lo")
            return float(lo) if lo is not None else float("-inf")
        ordered = sorted(buckets, key=_lo_key)
        # find the bucket containing the reference extreme (None bound = open)
        idx = None
        for i, b in enumerate(ordered):
            lo = b.get("lo")
            hi = b.get("hi")
            if (lo is None or ref >= float(lo)) and (hi is None or ref < float(hi)):
                idx = i
                break
        if idx is None:
            continue
        ref_bucket = ordered[idx]
        # likely breach neighbour
        if rule.get("direction") == "high":
            nxt = ordered[idx + 1] if idx + 1 < len(ordered) else None
        else:
            nxt = ordered[idx - 1] if idx - 1 >= 0 else None
        cands = [ref_bucket]
        if nxt is not None:
            cands.append(nxt)
        for b in cands:
            for side in ("no_token_id", "yes_token_id"):
                tok = str(b.get(side) or "")
                if tok and tok not in warm:
                    warm.append(tok)
    return warm


def _normalize_snapshot(token_id: str, snapshot: Any) -> dict[str, Any] | None:
    """Turn any CLOB/WS book snapshot into the pure ladder-dict the strategy
    and ``paper_match_fak`` were written against:
      {best_ask, best_bid, tick_size, asks:[{price,size}], bids:[...]}"""
    view = from_any(snapshot, token_id=token_id)
    if view is None:
        return None
    if not view.asks and not view.bids:
        return None
    asks = [{"price": str(p), "size": str(s)} for p, s in view.asks]
    bids = [{"price": str(p), "size": str(s)} for p, s in view.bids]
    return {
        "best_ask": str(view.best_ask) if view.best_ask is not None else (asks[0]["price"] if asks else None),
        "best_bid": str(view.best_bid) if view.best_bid is not None else (bids[0]["price"] if bids else None),
        "tick_size": str(view.tick_size) if view.tick_size is not None else "0.01",
        "asks": asks,
        "bids": bids,
        "fetched_at_epoch": snapshot.fetched_at_epoch if hasattr(snapshot, "fetched_at_epoch") else time.time(),
    }


def refresh_books(cfg: dict[str, Any], token_ids: list[str], now_utc: datetime) -> dict[str, Any]:
    """Fetch CLOB books for ``token_ids`` and warm the process-ladder cache.

    Returns {token_id: book_dict} for tokens that returned a live ladder; tokens
    with no executable side are omitted so downstream reads fail closed."""
    cache = book_cache()
    fresh: dict[str, Any] = {}
    if not token_ids:
        return fresh
    try:
        fetched = clob(cfg.get("clob_timeout_seconds", 8.0)).fetch_books(token_ids)
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "book_fetch_failed", "error": f"{type(exc).__name__}: {exc}"})
        return fresh
    for tid, snap in fetched.items():
        ladder = _normalize_snapshot(tid, snap)
        if ladder is None:
            continue
        cache[tid] = ladder
        fresh[tid] = ladder
    return fresh


def _ws_pump(wsb: Any, cache: dict[str, Any], max_age_s: float = 5.0) -> int:
    """Overlay fresh (<max_age_s) WS-fed LocalOrderBook snapshots onto the
    ladder cache. A snapshot only overwrites the cached ladder when it is
    strictly newer (fetched_at_epoch comparison) — a quiet/stale WS book can
    never clobber a fresher REST ladder."""
    stream = getattr(wsb, "stream", None)
    if stream is None:
        return 0
    n = 0
    now_e = time.time()
    for tid, lb in list(stream.books.items()):
        try:
            if lb.is_fresh(max_age_s, now=now_e):
                snap = lb.snapshot()
                ladder = _normalize_snapshot(tid, snap)
                if ladder is None:
                    continue
                cur = cache.get(tid)
                new_epoch = float(ladder.get("fetched_at_epoch") or 0)
                old_epoch = float((cur or {}).get("fetched_at_epoch") or 0)
                if new_epoch >= old_epoch:
                    cache[tid] = ladder
                    n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


# --------------------------------------------------------------------------- #
# METAR
# --------------------------------------------------------------------------- #
def _fetch_metar(
    cfg: dict[str, Any],
    icaos: list[str],
    now_utc: datetime,
) -> dict[str, dict[str, Any]]:
    """Dual-source METAR (CheckWX primary, AWC backup) for the given ICAO set.

    The caller picks the subset: whole universe on the idle cadence, armed
    ICAOs only on the fast cadence. Returns {icao: {raw, temp_c, obs_time,
    source}}; on failure logs ``metar_fetch_failed`` and returns {} so the
    caller keeps last-good obs and retries next cycle (fail closed)."""
    icaos = sorted({str(i).upper() for i in icaos})
    if not icaos:
        return {}
    api_key = cfg.get("_checkwx_key")
    try:
        return common.dual_source_metar(icaos, api_key, now=now_utc)
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "metar_fetch_failed", "error": f"{type(exc).__name__}: {exc}"})
        return {}


def _fetch_taf(
    cfg: dict[str, Any],
    icaos: list[str],
    now_utc: datetime,
) -> dict[str, dict[str, Any]]:
    """Fetch + parse TAF TX/TN for the given ICAO set via CheckWX.

    Returns {ICAO: {tx_c, tn_c, issue_dt, tx_valid_utc, tn_valid_utc, raw}}.
    Fail closed: on any error logs ``taf_fetch_failed`` and returns {} so the
    caller keeps last-good TAFs and retries next due cycle. Without a CheckWX
    key TAF is disabled (returns {}) and the strategy falls back to the
    market-rank-1 consensus reference.
    """
    icaos = sorted({str(i).upper() for i in icaos})
    if not icaos:
        return {}
    api_key = cfg.get("_checkwx_key")
    if not api_key:
        return {}
    try:
        raw_by_icao = common.checkwx_taf(icaos, api_key)
        out: dict[str, dict[str, Any]] = {}
        for icao, raw in raw_by_icao.items():
            parsed = common.parse_tx_tn(raw)
            if not parsed:
                continue
            issue_dt = common.parse_taf_issue_time(raw, ref_utc=now_utc)
            entry: dict[str, Any] = {
                "raw": raw,
                "tx_c": parsed.get("tx_c"),
                "tn_c": parsed.get("tn_c"),
                "issue_dt": issue_dt.isoformat() if issue_dt else None,
            }
            if issue_dt is not None:
                tx_day = parsed.get("tx_day")
                tx_hour = parsed.get("tx_hour")
                if tx_day and tx_hour is not None and parsed.get("tx_c") is not None:
                    v = common.resolve_tx_valid_utc(issue_dt, tx_day, int(tx_hour))
                    if v is not None:
                        entry["tx_valid_utc"] = v.isoformat()
                # TN uses the same day/hour fields on the TN side; resolve only
                # when parse_tx_tn returned a TN day/hour (it stores them under
                # tn_day/tn_hour). resolve_tx_valid_utc's day parsing expects
                # the TX-style day token; TN tokens share the same format.
                tn_day = parsed.get("tn_day")
                tn_hour = parsed.get("tn_hour")
                if tn_day and tn_hour is not None and parsed.get("tn_c") is not None:
                    v = common.resolve_tx_valid_utc(issue_dt, tn_day, int(tn_hour))
                    if v is not None:
                        entry["tn_valid_utc"] = v.isoformat()
            out[icao] = entry
        return out
    except Exception as exc:  # noqa: BLE001
        log_event(cfg.get("log_path"), {"type": "taf_fetch_failed", "error": f"{type(exc).__name__}: {exc}"})
        return {}


def _armed_icaos(armed_keys: set[str], cities: list[dict[str, Any]]) -> set[str]:
    """Map armed session keys (``city_id|local_date|direction``) to station ICAOs.

    The fast METAR pull only re-polls the armed cities' stations, so a fresh
    obs reaches ``maybe_arm_or_fire`` on the ~10s armed cadence instead of
    waiting for the next ~60s full-universe pull."""
    city_by_id = {str(c.get("city_id")): c for c in cities if c.get("city_id") is not None}
    icaos: set[str] = set()
    for key in armed_keys or ():
        city = city_by_id.get(str(key).split("|", 1)[0])
        if city is not None:
            icaos.add(str(city.get("icao")).upper())
    return icaos


# --------------------------------------------------------------------------- #
# Fire (paper) window
# --------------------------------------------------------------------------- #
def _paper_fire(
    cfg: dict[str, Any],
    state: dict[str, Any],
    fire: dict[str, Any],
    now_utc: datetime,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Run the capped-FAK paper fill for a ``re_fire`` event over warmed books.

    Mirrors the sim's ``run_fire_window`` but against the live normalized
    ladder cache, reserving real paper cash and recording the position on the
    state blob. Returns (position_legs_or_None, ladder_log)."""
    cache = book_cache()
    budget = Decimal(str(cfg.get("fire_budget_usdc", DEFAULTS["fire_budget_usdc"])))
    remaining = re_execution.size_legs(fire, budget)
    fills: dict[str, dict[str, Any]] = {}
    ladlog: list[dict[str, Any]] = []
    for leg in fire.get("legs", []):
        name = str(leg["leg"])
        fills[name] = {"shares": ZERO, "cost": ZERO, "fill_price": None}
    budget_ms = int(fire.get("fire_budget_ms") or cfg.get("fire_budget_ms", 8000))
    now_ms = int(now_utc.timestamp() * 1000)

    # If every leg's token is missing from the cache on the first attempt, do
    # one targeted refresh of exactly the fire's tokens before the ladder
    # starts — a fire that trips while the armed fast-poll hasn't refreshed
    # yet (e.g. fresh arm on this very cycle) would otherwise spin all three
    # ladder rungs as no_book and fill nothing.
    fire_tokens = [str(l.get("token_id") or "") for l in fire.get("legs", []) if l.get("token_id")]
    missing = [t for t in fire_tokens if t not in cache]
    if missing:
        refresh_books(cfg, missing, now_utc)
        cache = book_cache()

    for elapsed in (0, 1500, 4000):
        if elapsed > budget_ms:
            break
        intents = re_execution.plan_fire_cycle(
            fire,
            cache,  # live ladder dicts keyed by token
            remaining,
            now_utc + timedelta(milliseconds=elapsed),
            elapsed,
            budget_ms=budget_ms,
        )
        for intent in intents:
            # always log FAK ladder intent
            ladlog.append(intent)
            if intent.get("status") != "send_fak":
                continue
            match = re_execution.paper_match_fak(
                cache.get(intent.get("token_id")) if intent.get("token_id") in cache else {},
                Decimal(intent["limit_price"]),
                Decimal(intent["shares"]),
            )
            fills[intent["leg"]]["shares"] += match["filled_shares"]
            fills[intent["leg"]]["cost"] += match["cost"]
            remaining[intent["leg"]] = match["unfilled"]
            intent["fill"] = {
                "filled": str(match["filled_shares"]),
                "avg": str(match["avg_price"]) if match["avg_price"] is not None else None,
                "unfilled": str(match["unfilled"]),
            }
            if match["avg_price"] is not None:
                fills[intent["leg"]]["fill_price"] = str(match["avg_price"])

    total_cost = sum((fills[k]["cost"] for k in fills), ZERO)
    # Fail closed: if we could not reserve the filled cost, stand the whole
    # fire down (do not record a position we cannot fund).
    if total_cost > ZERO and reserve(state, total_cost) is None:
        log_event(cfg.get("log_path"), {"type": "fire_insufficient_capital", "key": fire["key"], "need": str(total_cost)})
        return None, ladlog
    # Start ledger baseline: paper_account debit already incremented by reserve.

    position = {
        "key": fire["key"],
        "city_id": fire.get("city_id"),
        "icao": fire.get("icao"),
        "market_local_date": fire.get("market_local_date"),
        "direction": fire.get("direction"),
        "fires_at_utc": re_execution.iso_utc(now_utc),
        "ref_extreme": fire.get("ref_extreme"),
        "ref_source": fire.get("ref_source"),
        "running_extreme": fire.get("running_extreme"),
        "jump": fire.get("jump"),
        "budget_usdc": str(budget),
        "settled": False,
        "legs": [],
    }
    # Map legs back to position-level fields for settlement bookkeeping.
    pos_bucket_by_leg = {str(l["leg"]): l for l in fire.get("legs", [])}
    pos_legs_by_name: dict[str, dict[str, Any]] = {}
    for leg in fire.get("legs", []):
        name = str(leg["leg"])
        fl = fills.get(name, {})
        pos_legs_by_name[name] = {
            "leg": name,
            "token_id": leg.get("token_id"),
            "side": leg.get("side"),
            "outcome": leg.get("outcome"),
            "cap": leg.get("cap"),
            "notional_pct": leg.get("notional_pct"),
            "cost_usdc": str(fl["cost"]),
            "shares": str(fl["shares"]),
            "avg_price": fl["fill_price"],
            "bucket_id": None,
            "settled": False,
            "leg_won": None,
        }
    # attach bucket_id by matching back through the fire leg spec (broken_no/new_yes)
    for leg in fire.get("legs", []):
        name = str(leg["leg"])
        if name == "buy_no_broken":
            bucket_id = fire.get("broken_bucket_id")
        elif name == "buy_yes_new":
            bucket_id = fire.get("new_bucket_id")
        else:
            continue
        pos_legs_by_name[name]["bucket_id"] = bucket_id
    position["legs"] = list(pos_legs_by_name.values())

    return position, ladlog


def _record_fire_event(cfg, state, fire, position, ladlog, now_utc) -> None:
    ensure_re_state(state)
    legs = position.get("legs") or []
    fill_summary = {
        str(lg.get("leg")): {"shares": lg.get("shares"), "cost": lg.get("cost_usdc"), "avg": lg.get("avg_price")}
        for lg in legs
    }
    state["entry_count"] = int(state.get("entry_count") or 0) + 1
    pos = state.setdefault("positions", {})
    # Merge repeated fires for the same key are impossible (one fire per session),
    # but guard against clobbering anyway.
    prev = pos.get(fire["key"])
    if prev is not None and prev.get("settled") is not True:
        pass  # key already recorded; keep first, log anomaly
    else:
        pos[fire["key"]] = position
    log_event(
        cfg.get("log_path"),
        {
            "type": "fire",
            "key": fire.get("key"),
            "city_id": fire.get("city_id"),
            "icao": fire.get("icao"),
            "direction": fire.get("direction"),
            "jump": fire.get("jump"),
            "ref_source": fire.get("ref_source"),
            "fills": fill_summary,
            # First-hand evidence for why a fire did not fill: every FAK
            # ladder intent (send_fak / abort_above_cap / no_book /
            # abort_timeout / missing_token) with best_ask/cap/limit so the
            # 15-min watcher can reverse-trace no-fill causes without guessing.
            "ladder": [
                {k: v for k, v in intent.items() if k != "fill"} | ({"fill": intent.get("fill")} if intent.get("fill") else {})
                for intent in ladlog
            ],
        },
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_cycle(
    cfg: dict[str, Any],
    state: dict[str, Any],
    now_utc: datetime | None = None,
    *,
    force_metar: bool = False,
    force_books: bool = False,
    force_rules: bool = False,
) -> bool:
    """Run one full poll cycle. Returns True while any session remains armed
    (caller keeps fast cadence; prune drops stale sessions).

    Caller (``runner_impl``) chooses sleep cadence from the return value:
    fast-poll (~10s) when armed, else scan interval (~20s)."""
    now = now_utc or datetime.now(timezone.utc)
    log_path = cfg.get("log_path", "data/yes2re_events.jsonl")
    cities = load_active_cities(cfg)
    if not cities:
        log_event(log_path, {"type": "empty_universe"})
        return False

    # Ensure strategy state sections present.
    ensure_re_state(state)

    # 1) Rule discovery (TTL-gated, caches failures)
    ttl_rules = float(cfg.get("rules_refresh_interval_seconds", DEFAULTS["rules_refresh_interval_seconds"]))
    rules_needed = force_rules or (time.time() - stamp("rules") >= ttl_rules)
    dates = target_dates_by_icao(cities, cfg, now)
    if rules_needed:
        refresh_rules(cfg, cities, dates, now)

    # Rebuild index from cache this cycle (even off-TTL we still know rules)
    rules_idx, rule_failures = _load_rule_cache()
    healthy_rules = {k: v for k, v in rule_failures.items() if k}
    log_event(log_path, {
        "type": "rules_refresh",
        "rules": len(rules_idx),
        "failures": rule_failures,
    })

    # Prune stale sessions before deriving armed_keys: the fast-poll / fast
    # METAR subset must only ever see the cleaned set — yesterday's keys or
    # low-direction zombies must not keep an ICAO hot-polled. prune never
    # raises (contract), so no guard is needed here.
    removed = prune_stale_sessions(state, cities, now)
    if removed:
        log_event(log_path, {"type": "prune_stale_sessions", "removed": removed})

    # Determine armed keys
    tree = ensure_re_state(state)
    armed_keys = set(tree.get("armed", {}).keys())
    # 2) Books for consensus/arm at rule scope
    rules_now = list(rules_idx.values())
    # token set from every *enabled active* rule bucket
    tokens = _all_tokens_for_rules(rules_idx)
    book_ttl = float(cfg.get("idle_book_interval_seconds", DEFAULTS["idle_book_interval_seconds"]))
    arm_book_ttl = float(cfg.get("arm_book_interval_seconds", DEFAULTS["arm_book_interval_seconds"]))
    do_book = force_books or (time.time() - stamp("book") >= book_ttl)
    if do_book and tokens:
        refresh_books(cfg, tokens, now)
        bump("book", time.time())
    # Armed fast-path: while any session is armed, keep the fire-critical
    # token subset (reference-bucket NO + breach-neighbour buckets from each
    # armed rule) hot on the armed cadence. The full-universe pull above is
    # ~1000 tokens and only runs on the idle TTL; without this fast path a
    # fire's legs frequently find no_book because their tokens were never
    # refreshed since the previous idle pull (or fell out of a chunk that
    # timed out). Warm subset is tens of tokens — cheap to refresh every few
    # seconds and exactly what plan_fire_cycle needs at fire time.
    warm_tokens = _warm_tokens_for_rules(rules_idx, state)
    if armed_keys and not do_book and warm_tokens and (time.time() - stamp("book_fast")) >= arm_book_ttl:
        refresh_books(cfg, warm_tokens, now)
        bump("book_fast", time.time())
    cache = book_cache()

    # ---- WebSocket bridge: live L2 overlay on the ladder cache ------------ #
    # WS feeds LocalOrderBook per token on a daemon thread (self-reconnecting);
    # every cycle we overlay any fresh (<5 s) local snapshot onto the ladder
    # cache, so paper FAK sees sub-second book updates when the feed is live.
    # REST /books above remains the correctness backbone and seeds on startup.
    wsb = ws_bridge()
    if cfg.get("market_ws_enabled", True) and tokens:
        if not wsb.running:
            try:
                wsb.start(tokens)
            except Exception as exc:  # noqa: BLE001
                log_event(log_path, {"type": "ws_start_failed", "error": f"{type(exc).__name__}: {exc}"})
        wsb.ensure_tokens(tokens)
        _ws_pump(wsb, cache)
    ws_tel = wsb.telemetry()

    # 3) METAR — dual-rate pulls.
    # Full universe still runs on the idle cadence (stamp "metar", ~60s) and
    # feeds consensus + every rule. While any session is armed, a fast subset
    # pull of only the armed ICAOs additionally runs on the armed cadence
    # (stamp "metar_fast", ~10s) so a fresh obs reaches the strategy ~10s after
    # publication instead of ~60s. Both paths share common.dual_source_metar
    # (CheckWX primary, AWC backup) and both bump their stamp only on a
    # non-empty result: a failed pull leaves last-good in place and retries on
    # the next due cycle instead of hammering every tick.
    idle_metar = float(cfg.get("idle_metar_interval_seconds", DEFAULTS["idle_metar_interval_seconds"]))
    arm_metar = float(cfg.get("arm_metar_interval_seconds", DEFAULTS["arm_metar_interval_seconds"]))
    do_metar = force_metar or (time.time() - stamp("metar") >= idle_metar)
    metar_by_icao: dict[str, dict[str, Any]] = {}
    if do_metar:
        # Full-universe pull (every configured city ICAO).
        fresh = _fetch_metar(cfg, sorted({str(c.get("icao")).upper() for c in cities}), now)
        if fresh:
            _LAST_GOOD_METAR.clear()
            _LAST_GOOD_METAR.update(fresh)
            metar_by_icao = fresh
            bump("metar", time.time())
        else:
            # Pull failed: keep surfacing last-good obs (never wipe them).
            metar_by_icao = dict(_LAST_GOOD_METAR)
    else:
        # Between METAR fetches, still surface the last good obs so the
        # watcher can see obs-age growth and the consensus loop has data.
        metar_by_icao = dict(_LAST_GOOD_METAR)
    # Fast pull: armed-only ICAO subset, on the armed cadence, only when this
    # cycle did not already run the full pull (a successful full pull already
    # covers the armed subset). Empty fast result preserves last-good for those
    # ICAOs — feed jitter must not erase an observation.
    if armed_keys and not do_metar and (time.time() - stamp("metar_fast")) >= arm_metar:
        fast_icaos = sorted(_armed_icaos(armed_keys, cities))
        if fast_icaos:
            fast = _fetch_metar(cfg, fast_icaos, now)
            if fast:
                metar_by_icao.update(fast)
                _LAST_GOOD_METAR.update(fast)
                bump("metar_fast", time.time())

    # 3b) TAF TX/TN — full-universe pull on a slow cadence (TAF updates every
    # 4-6h; no fast/armed variant needed). Parsed TX/TN per ICAO feed the
    # strategy's reference-extreme (ref_source="taf") so the reversal fires
    # relative to the forecast extreme, not the market rank-1 bucket — which
    # was the cause of the jump=6 misfires (running extreme vs favourite
    # bucket can be many buckets apart in extreme weather).
    taf_ttl = float(cfg.get("taf_refresh_interval_seconds", DEFAULTS.get("taf_refresh_interval_seconds", 1800)))
    do_taf = force_metar or (time.time() - stamp("taf")) >= taf_ttl
    taf_by_icao: dict[str, dict[str, Any]] = {}
    if do_taf:
        fresh_taf = _fetch_taf(cfg, sorted({str(c.get("icao")).upper() for c in cities}), now)
        if fresh_taf:
            _LAST_GOOD_TAF.clear()
            _LAST_GOOD_TAF.update(fresh_taf)
            bump("taf", time.time())
        taf_by_icao = dict(_LAST_GOOD_TAF)
    else:
        taf_by_icao = dict(_LAST_GOOD_TAF)

    # 4+5) Feed strategy per (city,direction) live contract for today
    armed_any = False
    city_by_id = {c["city_id"]: c for c in cities}
    for rule in rules_now:
        # Build per-rule book map for the YES tokens that have addresses
        rule_books: dict[str, dict[str, Any]] = {}
        for b in rule.get("buckets", []):
            yes = str(b.get("yes_token_id") or "")
            if yes and yes in cache:
                rule_books[yes] = cache[yes]
            else:
                rule_books[yes] = {}
        # sample consensus even without new METAR (book cadence ~30s)
        t = tracker()
        t.record_books(
            rule.get("city_id"),
            rule.get("market_local_date"),
            rule.get("direction"),
            rule.get("buckets", []),
            rule_books,
            now,
        )
        icao = city_by_id.get(rule.get("city_id"), {}).get("icao", "").upper()
        obs = metar_by_icao.get(icao)
        if obs is None or obs.get("temp_c") is None:
            continue
        city = city_by_id.get(rule.get("city_id"))
        if city is None:
            continue
        market_unit = city.get("market_unit", "C")
        temp = common.c_to_market_unit(float(obs["temp_c"]), market_unit)
        rule_buckets = rule.get("buckets", [])
        # TAF reference extreme for this rule: TX for high-direction markets,
        # TN for low-direction markets. The extreme must fall inside the
        # market's local calendar date — a TAF spans ~30h and its TX/TN can
        # belong to the neighbouring local day; using a same-day mismatch
        # would recreate the jump-misfire bug against a stale reference. When
        # TAF is missing or off-date we pass None and the strategy falls back
        # to the market rank-1 consensus reference (allow_market_consensus_reference).
        taf_extreme_market: float | None = None
        taf_rec = taf_by_icao.get(icao)
        if taf_rec is not None:
            valid_iso = taf_rec.get("tx_valid_utc" if rule.get("direction") == "high" else "tn_valid_utc")
            taf_c = taf_rec.get("tx_c" if rule.get("direction") == "high" else "tn_c")
            if valid_iso and taf_c is not None:
                try:
                    from zoneinfo import ZoneInfo
                    tz_name = city.get("timezone") or "UTC"
                    valid_dt = datetime.fromisoformat(valid_iso.replace("Z", "+00:00"))
                    local_date = valid_dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
                    if local_date == rule.get("market_local_date"):
                        taf_extreme_market = common.c_to_market_unit(float(taf_c), market_unit)
                except Exception:  # noqa: BLE001 — bad TAF metadata → None → consensus fallback
                    taf_extreme_market = None
        # pass all known books for the whole rule (only YES needed for consensus)
        actions = maybe_arm_or_fire(
            state,
            city,
            rule.get("market_local_date"),
            rule.get("direction"),
            rule_buckets,
            taf_extreme_market,  # TAF TX/TN (market units) or None → consensus fallback
            temp,
            obs.get("obs_time"),
            now,
            rule_books,
            cfg.get("strategy") or {},
            consensus_tracker=t,
        )
        for action in actions:
            atype = action.get("action_type")
            if atype == "re_arm":
                armed_any = True
                log_event(log_path, {"type": "arm", "key": action.get("key"), **{k: action[k] for k in ("ref_source", "distance_c") if k in action}})
            elif atype in ("re_fire",):
                log_event(log_path, {"type": "fire_attempt", "key": action.get("key"), "jump": action.get("jump"), "ref_source": action.get("ref_source")})
                position, ladlog = _paper_fire(cfg, state, action, now)
                if position is not None:
                    _record_fire_event(cfg, state, action, position, ladlog, now)
                else:
                    # insufficient capital / nothing fillable — mark fired anyway
                    # so we don't retry-fire the same session each tick.
                    tree.setdefault("fired", {})[action["key"]] = {
                        "status": "fired_no_fill", "at_utc": re_execution.iso_utc(now), "jump": action.get("jump"),
                    }
                # fire branch complete — never fall through to skip logging
                continue
            elif atype in ("re_skip", "re_skip_yes"):
                # Every skip is recorded (2026-09-03: silent skips hid the
                # stale_obs deadlock — 0 fires with zero audit trail).
                reason = action.get("reason")
                if reason == "duplicate_obs_time":
                    # Expected on every fast cycle (no new METAR for that session):
                    # keep the audit but bound JSONL growth to ~1/key/5 min.
                    key = action.get("key")
                    now_f = time.time()
                    if now_f - _DUP_LOG.get(key, 0.0) < _DUP_LOG_INTERVAL_S:
                        continue
                    _DUP_LOG[key] = now_f
                log_event(log_path, {"type": "skip", "key": action.get("key"),
                                     "reason": reason,
                                     "jump": action.get("jump"),
                                     "consensus": action.get("consensus")})
                continue
            elif atype in ("re_disarm",):
                # disarm is audit-only; log and move on
                log_event(log_path, {"type": "disarm", "key": action.get("key"),
                                     "reason": action.get("reason")})
                continue

    # 6) Passive settlement of resolved positions (best-effort, TTL-gated).
    # Gamma pulls only when open positions exist and settle cadence elapsed.
    open_pos = [p for p in state.get("positions", {}).values() if not p.get("settled")]
    settle_ttl = float(cfg.get("settle_poll_seconds", 3600))
    if open_pos and (force_books or time.time() - stamp("settle") >= settle_ttl):
        pos_meta = {}
        for p in open_pos:
            c = city_by_id.get(p.get("city_id"))
            if c:
                pos_meta[p.get("key")] = {"city": c}
        try:
            settled = settle_markets(cfg, state, position_meta=pos_meta)
            if settled:
                log_event(log_path, {"type": "settled", "count": len(settled)})
        except Exception as exc:  # noqa: BLE001
            log_event(log_path, {"type": "settle_failed", "error": f"{type(exc).__name__}: {exc}"})
        bump("settle", time.time())

    # ---- Publish feed telemetry for the watcher/hermes summary ---------- #
    now_epoch = time.time()
    metar_tel: dict[str, Any] = {}
    for icao, rec in metar_by_icao.items():
        age_s = None
        if rec.get("obs_time") is not None:
            age_s = max(0.0, (now - rec["obs_time"]).total_seconds())
        metar_tel[icao] = {
            "source": rec.get("source"),
            "obs_age_s": round(age_s, 1) if age_s is not None else None,
            "temp_c": rec.get("temp_c"),
            "fetched_this_cycle": True,
            "fields_ok": rec.get("temp_c") is not None and rec.get("obs_time") is not None,
        }
    book_age_s = {}
    _now_epoch = now_epoch
    for tid, bk in cache.items():
        f = bk.get("fetched_at_epoch")
        book_age_s[tid] = round(max(0.0, _now_epoch - float(f)), 1) if f else None
    tel: dict[str, Any] = {
        "ts_utc": re_execution.iso_utc(now),
        "cycle_armed": bool(armed_any),
        "armed_keys": list(tree.get("armed", {}).keys()),
        "fired_keys": list(tree.get("fired", {}).keys()),
        "open_positions": sum(1 for p in state.get("positions", {}).values() if not p.get("settled")),
        "rules": {
            "count": len(rules_idx),
            "failures": rule_failures,
            "age_s": round(max(0.0, now_epoch - stamp("rules")), 1) if stamp("rules") else None,
        },
        "metar": {
            "fetched": len(metar_by_icao),
            "cities_ok": sum(1 for r in metar_tel.values() if r["fields_ok"]),
            "max_obs_age_s": max((r["obs_age_s"] for r in metar_tel.values() if r["obs_age_s"] is not None), default=None),
            "per_icao": metar_tel,
        },
        "books": {
            "cached_tokens": len(cache),
            "oldest_age_s": max((a for a in book_age_s.values() if a is not None), default=None),
            "per_token_sample": {k: book_age_s[k] for k in list(book_age_s)[:6]},
        },
        # Polymarket market websocket: live when the bridge thread is up.
        "websocket_market": ws_tel,
        "clob": {"mode": "read_only", "submits_orders": False},
        "gamma": {"mode": "public_read_only", "events_discovered": len(rules_idx) // 2 if rules_idx else 0},
        "signal_latency_s": {"not_yet_fired": True},
    }
    set_health_extra(tel)
    # Post-processing truth: fires/disarms/prune this cycle already popped
    # their sessions, so only sessions still armed keep the caller fast-polling.
    return bool(tree.get("armed"))
