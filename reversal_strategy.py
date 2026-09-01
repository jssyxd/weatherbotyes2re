"""METAR-vs-TAF one-bucket reversal.

IDLE -> ARMED -> FIRED -> COOLDOWN
Fire only when running extreme breaks TAF by exactly one bucket.
NO leg on broken bucket is the main trade; YES on new bucket is optional and smaller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

ARM_C = 1.0
MAX_BUCKET_JUMP = 1
NO_MAX_ASK = Decimal("0.62")
YES_MAX_ASK = Decimal("0.48")
NO_NOTIONAL_PCT = Decimal("0.70")
YES_NOTIONAL_PCT = Decimal("0.30")
HIGH_FIRE_LOCAL_HOUR = 14
LOW_FIRE_LOCAL_HOUR_END = 10
REQUIRE_FRESH_OBS_SECONDS = 180
ZERO = Decimal("0")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bucket_contains(bucket: dict[str, Any], value: float) -> bool:
    lo, hi = bucket.get("lo"), bucket.get("hi")
    return (lo is None or value >= float(lo)) and (hi is None or value < float(hi))


def ensure_re_state(state: dict[str, Any]) -> dict[str, Any]:
    tree = state.setdefault("weatherbotyes2re", {})
    for name in ("armed", "fired", "running_extremes", "taf_forecasts", "last_obs"):
        tree.setdefault(name, {})
    return tree


def mid_value(bucket: dict[str, Any]) -> float:
    lo, hi = bucket.get("lo"), bucket.get("hi")
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2.0
    if lo is not None:
        return float(lo)
    if hi is not None:
        return float(hi)
    return 0.0


def ordered_buckets(buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(buckets, key=mid_value)


def find_bucket(buckets: list[dict[str, Any]], value: float) -> dict[str, Any] | None:
    for b in buckets:
        if bucket_contains(b, value):
            return b
    return None


def bucket_index(ordered: list[dict[str, Any]], bucket: dict[str, Any] | None) -> int | None:
    if bucket is None:
        return None
    bid = str(bucket.get("bucket_id") or bucket.get("id") or "")
    for i, b in enumerate(ordered):
        if str(b.get("bucket_id") or b.get("id") or "") == bid:
            return i
        if b is bucket:
            return i
    return None


def session_key(city_id: str, market_local_date: str, direction: str) -> str:
    return f"{city_id}|{market_local_date}|{direction}"


def update_running_extreme(state, city_id, market_local_date, direction, temp: float, now_utc: datetime):
    tree = ensure_re_state(state)
    key = session_key(city_id, market_local_date, direction)
    rec = tree["running_extremes"].get(key) or {"value": None, "obs_count": 0}
    prev = rec.get("value")
    if direction == "high":
        new_val = temp if prev is None else max(float(prev), temp)
    else:
        new_val = temp if prev is None else min(float(prev), temp)
    rec["value"] = new_val
    rec["obs_count"] = int(rec.get("obs_count") or 0) + 1
    rec["updated_at_utc"] = iso_utc(now_utc)
    tree["running_extremes"][key] = rec
    return rec


def hour_ok(direction: str, local_hour: int) -> bool:
    if direction == "high":
        return local_hour >= HIGH_FIRE_LOCAL_HOUR
    return local_hour <= LOW_FIRE_LOCAL_HOUR_END


def obs_is_fresh(obs_time_utc: datetime | None, now_utc: datetime, max_age=REQUIRE_FRESH_OBS_SECONDS) -> bool:
    if obs_time_utc is None:
        return False
    return (now_utc.astimezone(timezone.utc) - obs_time_utc.astimezone(timezone.utc)).total_seconds() <= max_age


def maybe_arm_or_fire(
    state: dict[str, Any],
    city: dict[str, Any],
    market_local_date: str,
    direction: str,
    buckets: list[dict[str, Any]],
    taf_extreme: float | None,
    observed_temp: float | None,
    obs_time_utc: datetime | None,
    now_utc: datetime,
    books_by_token: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Main hook: call on every new METAR observation timestamp."""
    actions: list[dict[str, Any]] = []
    if observed_temp is None or taf_extreme is None:
        return actions
    cfg = config or {}
    arm_c = float(cfg.get("arm_c", ARM_C))
    max_jump = int(cfg.get("max_bucket_jump", MAX_BUCKET_JUMP))
    tree = ensure_re_state(state)
    key = session_key(city["city_id"], market_local_date, direction)
    if key in tree["fired"]:
        return [{"action_type": "re_skip", "reason": "already_fired", "key": key}]

    local_hour = now_utc.astimezone(ZoneInfo(city["timezone"])).hour
    rec = update_running_extreme(state, city["city_id"], market_local_date, direction, float(observed_temp), now_utc)
    running = float(rec["value"])
    ordered = ordered_buckets(buckets)
    taf_b = find_bucket(ordered, float(taf_extreme))
    run_b = find_bucket(ordered, running)
    taf_i = bucket_index(ordered, taf_b)
    run_i = bucket_index(ordered, run_b)
    if taf_i is None or run_i is None:
        return [{"action_type": "re_skip", "reason": "bucket_unmapped", "key": key}]

    distance_c = abs(running - float(taf_extreme))
    jump = run_i - taf_i if direction == "high" else taf_i - run_i

    armed = tree["armed"].get(key)
    if jump <= 0 and distance_c <= arm_c and hour_ok(direction, local_hour):
        tree["armed"][key] = {
            "status": "armed",
            "taf_bucket_id": str(taf_b.get("bucket_id") or taf_b.get("id") or ""),
            "taf_extreme": float(taf_extreme),
            "running": running,
            "armed_at_utc": iso_utc(now_utc),
        }
        actions.append({
            "action_type": "re_arm",
            "key": key,
            "distance_c": distance_c,
            "taf_bucket_id": tree["armed"][key]["taf_bucket_id"],
            "prefetch_tokens": True,
            "fast_poll": True,
        })
        return actions

    if jump <= 0:
        if armed and distance_c > arm_c + 0.7:
            tree["armed"].pop(key, None)
            actions.append({"action_type": "re_disarm", "key": key, "reason": "moved_away"})
        return actions

    # jump > 0 : potential break
    if not hour_ok(direction, local_hour):
        return [{"action_type": "re_skip", "reason": "hour_not_in_window", "key": key, "jump": jump}]
    if not obs_is_fresh(obs_time_utc, now_utc):
        return [{"action_type": "re_skip", "reason": "stale_obs", "key": key}]
    if jump > max_jump:
        # too far; optional: still allow NO on TAF bucket only
        actions.append({"action_type": "re_skip_yes", "reason": "jump_gt_one", "key": key, "jump": jump})
        fire_yes = False
    else:
        fire_yes = bool(cfg.get("yes_leg_enabled", True))

    broken = taf_b
    new_b = run_b if jump == 1 else None
    fire = {
        "key": key,
        "city_id": city["city_id"],
        "icao": city.get("icao"),
        "market_local_date": market_local_date,
        "direction": direction,
        "taf_extreme": float(taf_extreme),
        "running_extreme": running,
        "jump": jump,
        "broken_bucket_id": str(broken.get("bucket_id") or broken.get("id") or ""),
        "broken_no_token": broken.get("no_token_id") or broken.get("_no_token_id"),
        "new_bucket_id": str(new_b.get("bucket_id") or new_b.get("id") or "") if new_b else None,
        "new_yes_token": (new_b.get("yes_token_id") or new_b.get("_yes_token_id")) if new_b else None,
        "legs": [],
    }
    fire["legs"].append({
        "leg": "buy_no_broken",
        "token_id": fire["broken_no_token"],
        "side": "BUY",
        "outcome": "NO",
        "cap": str(cfg.get("no_max_ask", NO_MAX_ASK)),
        "notional_pct": str(cfg.get("no_notional_pct", NO_NOTIONAL_PCT)),
    })
    if fire_yes and new_b is not None:
        fire["legs"].append({
            "leg": "buy_yes_new",
            "token_id": fire["new_yes_token"],
            "side": "BUY",
            "outcome": "YES",
            "cap": str(cfg.get("yes_max_ask", YES_MAX_ASK)),
            "notional_pct": str(cfg.get("yes_notional_pct", YES_NOTIONAL_PCT)),
        })
    tree["fired"][key] = {"status": "fired", "at_utc": iso_utc(now_utc), "jump": jump}
    tree["armed"].pop(key, None)
    actions.append({"action_type": "re_fire", **fire})
    return actions
