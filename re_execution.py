"""Reversal fill module.

arm: caller keeps books + token ids warm.
fire: short FAK ladder under a hard cap and an 8s budget.
Never walk the cap. If the scramble already repriced above cap, abort that leg.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

FIRE_BUDGET_MS = 8000
LADDER_MS = (0, 1500, 4000)
ZERO = Decimal("0")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def best_ask(book: Any) -> Decimal | None:
    if book is None:
        return None
    if isinstance(book, dict):
        raw = book.get("best_ask")
        if raw is None and book.get("asks"):
            row = book["asks"][0]
            raw = row.get("price") if isinstance(row, dict) else row
        return _dec(raw) if raw is not None else None
    raw = getattr(book, "best_ask", None)
    return _dec(raw) if raw is not None else None


def tick_of(book: Any) -> Decimal:
    if isinstance(book, dict):
        return _dec(book.get("tick_size"), "0.01") or Decimal("0.01")
    return _dec(getattr(book, "tick_size", None), "0.01") or Decimal("0.01")


def cap_price(ask: Decimal | None, cap: Decimal, tick: Decimal, extra_ticks: int = 0) -> Decimal | None:
    if ask is None or ask <= ZERO:
        return None
    px = ask + tick * extra_ticks
    if px > cap:
        px = cap
    if ask > cap:
        return None  # already too expensive, do not lift
    if tick > 0:
        steps = (px / tick).to_integral_value(rounding=ROUND_DOWN)
        px = steps * tick
    return px


def plan_leg_attempts(
    leg: dict[str, Any],
    book: Any,
    target_shares: Decimal,
    now_utc: datetime,
    elapsed_ms: int,
) -> dict[str, Any]:
    cap = _dec(leg.get("cap"), "0.50")
    ask = best_ask(book)
    tick = tick_of(book)
    token = leg.get("token_id")
    if token is None:
        return {"status": "missing_token", "leg": leg.get("leg")}
    if elapsed_ms > FIRE_BUDGET_MS:
        return {"status": "abort_timeout", "leg": leg.get("leg"), "unfilled": str(target_shares)}
    if ask is None:
        return {"status": "no_book", "leg": leg.get("leg")}
    if ask > cap:
        return {
            "status": "abort_above_cap",
            "leg": leg.get("leg"),
            "best_ask": str(ask),
            "cap": str(cap),
        }

    extra = 0
    if elapsed_ms >= LADDER_MS[2]:
        extra = 1  # last try: still at cap, allow +0..1 tick already clamped
    elif elapsed_ms >= LADDER_MS[1]:
        extra = 1
    limit = cap_price(ask, cap, tick, extra_ticks=extra)
    if limit is None:
        return {"status": "abort_above_cap", "leg": leg.get("leg"), "best_ask": str(ask), "cap": str(cap)}
    return {
        "status": "send_fak",
        "leg": leg.get("leg"),
        "token_id": token,
        "side": leg.get("side", "BUY"),
        "outcome": leg.get("outcome"),
        "order_type": "FAK",
        "limit_price": str(limit),
        "shares": str(target_shares),
        "best_ask": str(ask),
        "cap": str(cap),
        "elapsed_ms": elapsed_ms,
        "at_utc": iso_utc(now_utc),
    }


def plan_fire_cycle(
    fire_event: dict[str, Any],
    books_by_token: dict[str, Any],
    remaining_by_leg: dict[str, Decimal],
    now_utc: datetime,
    elapsed_ms: int,
    total_budget_usdc: Decimal,
) -> list[dict[str, Any]]:
    """Called repeatedly during the 8s fire window."""
    actions = []
    legs = fire_event.get("legs") or []
    for leg in legs:
        name = str(leg.get("leg"))
        left = remaining_by_leg.get(name, ZERO)
        if left <= ZERO:
            continue
        pct = _dec(leg.get("notional_pct"), "0")
        # remaining is already in shares; just route
        book = books_by_token.get(str(leg.get("token_id") or ""))
        actions.append(plan_leg_attempts(leg, book, left, now_utc, elapsed_ms))
    return actions


def shares_from_notional(notional: Decimal, cap: Decimal) -> Decimal:
    if cap <= ZERO:
        return ZERO
    return (notional / cap).quantize(Decimal("0.01"))


def size_legs(fire_event: dict[str, Any], budget_usdc: Decimal) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for leg in fire_event.get("legs") or []:
        pct = _dec(leg.get("notional_pct"), "0")
        cap = _dec(leg.get("cap"), "0.50")
        notion = (budget_usdc * pct).quantize(Decimal("0.01"))
        out[str(leg.get("leg"))] = shares_from_notional(notion, cap)
    return out
