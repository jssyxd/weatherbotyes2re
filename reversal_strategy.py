"""METAR-vs-TAF one-bucket reversal with long-horizon consensus filter.

IDLE -> ARMED -> FIRED -> COOLDOWN

Fire only when:
  1) running extreme breaks the reference extreme by exactly one bucket
     (reference = TAF TX/TN if present, else market rank-1 consensus bucket)
  2) obs is fresh (new obs_time, age <= require_fresh_obs_seconds)
  3) local hour in fire window
  4) broken bucket was long-horizon market consensus (1–2h TWAP rank-1)

NO leg on broken bucket is the main trade; YES on new bucket is optional and smaller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from consensus_tracker import ConsensusTracker, DEFAULT_TRACKER

ARM_C = 1.0
MAX_BUCKET_JUMP = 1
NO_MAX_ASK = Decimal("0.65")
YES_MAX_ASK = Decimal("0.48")
NO_NOTIONAL_PCT = Decimal("0.75")
YES_NOTIONAL_PCT = Decimal("0.25")
HIGH_FIRE_LOCAL_HOUR = 14
LOW_FIRE_LOCAL_HOUR_END = 10
REQUIRE_FRESH_OBS_SECONDS = 180  # legacy absolute-age gate — deprecated 2026-09-03 (see OBS_* window below)
OBS_MAX_LOOKBACK_SECONDS = 5400  # 90 min sanity: obs older than this = stale feed, do not fire
OBS_MAX_FUTURE_SECONDS = 900     # 15 min sanity: US AWS stations publish ~7 min EARLY; >15 min ahead = bad stamp
CONSENSUS_WINDOW_SECONDS = 7200  # 2h default; config can set 3600
CONSENSUS_MIN_SAMPLES = 20
CONSENSUS_MIN_LEAD = Decimal("0.03")
ZERO = Decimal("0")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bucket_contains(bucket: dict[str, Any], value: float) -> bool:
    lo, hi = bucket.get("lo"), bucket.get("hi")
    return (lo is None or value >= float(lo)) and (hi is None or value < float(hi))


def ensure_re_state(state: dict[str, Any]) -> dict[str, Any]:
    tree = state.setdefault("weatherbotyes2re", {})
    for name in ("armed", "fired", "running_extremes", "taf_forecasts", "last_obs", "last_obs_time"):
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


def hour_ok(direction: str, local_hour: int, high_hour: int, low_hour_end: int) -> bool:
    if direction == "high":
        return local_hour >= high_hour
    return local_hour <= low_hour_end


def obs_is_fresh(obs_time_utc: datetime | None, now_utc: datetime, max_age: int) -> bool:
    if obs_time_utc is None:
        return False
    return (now_utc.astimezone(timezone.utc) - obs_time_utc.astimezone(timezone.utc)).total_seconds() <= max_age


def is_new_obs_time(state: dict[str, Any], key: str, obs_time_utc: datetime | None) -> bool:
    """Reject duplicate pushes of the same observation timestamp."""
    if obs_time_utc is None:
        return False
    tree = ensure_re_state(state)
    prev = tree["last_obs_time"].get(key)
    stamp = iso_utc(obs_time_utc)
    if prev == stamp:
        return False
    tree["last_obs_time"][key] = stamp
    return True


def reference_extreme_from_consensus(
    tracker: ConsensusTracker,
    city_id: str,
    market_local_date: str,
    direction: str,
    ordered: list[dict[str, Any]],
    now_utc: datetime,
    window_seconds: int,
) -> tuple[float | None, dict[str, Any] | None, str]:
    """When TAF missing: use long-horizon rank-1 bucket mid as reference extreme."""
    ranks = tracker.rank_buckets(city_id, market_local_date, direction, now_utc, window_seconds)
    if not ranks:
        return None, None, "no_consensus"
    top_id, twap, _ = ranks[0]
    for b in ordered:
        if str(b.get("bucket_id") or b.get("id") or "") == top_id:
            return mid_value(b), b, "market_rank1"
    return None, None, "rank1_unmapped"


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
    consensus_tracker: ConsensusTracker | None = None,
) -> list[dict[str, Any]]:
    """Main hook: call on every new METAR observation timestamp.

    Always samples books into consensus_tracker when provided so the
    long-horizon rank filter has data before a break.
    """
    actions: list[dict[str, Any]] = []
    if observed_temp is None:
        return actions
    cfg = config or {}
    arm_c = float(cfg.get("arm_c", ARM_C))
    max_jump = int(cfg.get("max_bucket_jump", MAX_BUCKET_JUMP))
    fresh_s = int(cfg.get("require_fresh_obs_seconds", REQUIRE_FRESH_OBS_SECONDS))  # legacy, unused by fire window
    obs_lookback_s = int(cfg.get("max_obs_lookback_seconds", OBS_MAX_LOOKBACK_SECONDS))
    obs_future_s = int(cfg.get("max_obs_future_seconds", OBS_MAX_FUTURE_SECONDS))
    high_hour = int(cfg.get("high_fire_local_hour", HIGH_FIRE_LOCAL_HOUR))
    low_hour_end = int(cfg.get("low_fire_local_hour_end", LOW_FIRE_LOCAL_HOUR_END))
    cons_win = int(cfg.get("consensus_window_seconds", CONSENSUS_WINDOW_SECONDS))
    cons_min_samples = int(cfg.get("consensus_min_samples", CONSENSUS_MIN_SAMPLES))
    cons_min_lead = Decimal(str(cfg.get("consensus_min_lead", CONSENSUS_MIN_LEAD)))
    require_consensus = bool(cfg.get("require_consensus_filter", True))
    allow_market_ref = bool(cfg.get("allow_market_consensus_reference", True))

    tracker = consensus_tracker or DEFAULT_TRACKER
    tree = ensure_re_state(state)
    key = session_key(city["city_id"], market_local_date, direction)

    # Continuous consensus sampling (even before break)
    tracker.record_books(
        city["city_id"],
        market_local_date,
        direction,
        buckets,
        books_by_token,
        now_utc,
    )

    if key in tree["fired"]:
        return [{"action_type": "re_skip", "reason": "already_fired", "key": key}]

    # Duplicate obs_time guard (must run after fired check so we still record books)
    if not is_new_obs_time(state, key, obs_time_utc):
        return [{"action_type": "re_skip", "reason": "duplicate_obs_time", "key": key}]

    local_hour = now_utc.astimezone(ZoneInfo(city["timezone"])).hour
    rec = update_running_extreme(
        state, city["city_id"], market_local_date, direction, float(observed_temp), now_utc
    )
    running = float(rec["value"])
    ordered = ordered_buckets(buckets)

    # Reference extreme: prefer TAF; fallback to market rank-1 mid
    ref_source = "taf"
    ref_extreme = float(taf_extreme) if taf_extreme is not None else None
    taf_b = find_bucket(ordered, float(taf_extreme)) if taf_extreme is not None else None
    if taf_b is None and allow_market_ref:
        ref_extreme, taf_b, ref_source = reference_extreme_from_consensus(
            tracker,
            city["city_id"],
            market_local_date,
            direction,
            ordered,
            now_utc,
            cons_win,
        )
    if ref_extreme is None or taf_b is None:
        return [{"action_type": "re_skip", "reason": "no_reference_extreme", "key": key, "ref_source": ref_source}]

    run_b = find_bucket(ordered, running)
    taf_i = bucket_index(ordered, taf_b)
    run_i = bucket_index(ordered, run_b)
    if taf_i is None or run_i is None:
        return [{"action_type": "re_skip", "reason": "bucket_unmapped", "key": key}]

    distance_c = abs(running - float(ref_extreme))
    jump = run_i - taf_i if direction == "high" else taf_i - run_i

    armed = tree["armed"].get(key)
    if jump <= 0 and distance_c <= arm_c and hour_ok(direction, local_hour, high_hour, low_hour_end):
        tree["armed"][key] = {
            "status": "armed",
            "taf_bucket_id": str(taf_b.get("bucket_id") or taf_b.get("id") or ""),
            "ref_extreme": float(ref_extreme),
            "ref_source": ref_source,
            "running": running,
            "armed_at_utc": iso_utc(now_utc),
            "fast_poll": True,
        }
        actions.append({
            "action_type": "re_arm",
            "key": key,
            "distance_c": distance_c,
            "ref_source": ref_source,
            "taf_bucket_id": tree["armed"][key]["taf_bucket_id"],
            "prefetch_tokens": True,
            "fast_poll": True,
            "fast_poll_seconds": int(cfg.get("fast_poll_seconds", 8)),
        })
        return actions

    if jump <= 0:
        if armed and distance_c > arm_c + 0.7:
            tree["armed"].pop(key, None)
            actions.append({"action_type": "re_disarm", "key": key, "reason": "moved_away"})
        return actions

    # jump > 0 : potential break
    if not hour_ok(direction, local_hour, high_hour, low_hour_end):
        return [{"action_type": "re_skip", "reason": "hour_not_in_window", "key": key, "jump": jump}]
    # Freshness = "a NEW observation arrived" (deduped by is_new_obs_time
    # above) — NOT "the observation happened within N seconds". METAR/SPECI
    # run on a 20-60 min cadence: obs_time age swings 0-60 min between
    # reports by design (US AWS publish ~7 min EARLY, others 1-8 min late).
    # An absolute age gate (<=180s) structurally killed every fire with
    # stale_obs while obs were perfectly current (0 trades, 2026-09-03).
    # Sanity window only: reject a stalled feed (>90 min behind) and
    # impossible future stamps (>15 min ahead).
    if obs_time_utc is None:
        return [{"action_type": "re_skip", "reason": "stale_obs", "key": key}]
    obs_age = (now_utc.astimezone(timezone.utc) - obs_time_utc.astimezone(timezone.utc)).total_seconds()
    if obs_age > obs_lookback_s or obs_age < -obs_future_s:
        return [{"action_type": "re_skip", "reason": "stale_obs", "key": key, "obs_age_s": round(obs_age, 1)}]

    broken = taf_b
    broken_id = str(broken.get("bucket_id") or broken.get("id") or "")

    # Long-horizon consensus filter on the *broken* bucket
    consensus_meta: dict[str, Any] = {"ok": True, "reason": "disabled"}
    if require_consensus:
        consensus_meta = tracker.is_long_horizon_consensus(
            city["city_id"],
            market_local_date,
            direction,
            broken_id,
            now_utc=now_utc,
            window_seconds=cons_win,
            min_lead=cons_min_lead,
            require_rank1=True,
            min_samples=cons_min_samples,
        )
        if not consensus_meta.get("ok"):
            return [{
                "action_type": "re_skip",
                "reason": "consensus_filter",
                "key": key,
                "jump": jump,
                "consensus": consensus_meta,
            }]

    if jump > max_jump:
        actions.append({"action_type": "re_skip_yes", "reason": "jump_gt_one", "key": key, "jump": jump})
        fire_yes = False
        new_b = None
    else:
        fire_yes = bool(cfg.get("yes_leg_enabled", True))
        new_b = run_b if jump == 1 else None

    fire = {
        "key": key,
        "city_id": city["city_id"],
        "icao": city.get("icao"),
        "market_local_date": market_local_date,
        "direction": direction,
        "ref_extreme": float(ref_extreme),
        "ref_source": ref_source,
        "taf_extreme": float(taf_extreme) if taf_extreme is not None else None,
        "running_extreme": running,
        "jump": jump,
        "broken_bucket_id": broken_id,
        "broken_no_token": broken.get("no_token_id") or broken.get("_no_token_id"),
        "new_bucket_id": str(new_b.get("bucket_id") or new_b.get("id") or "") if new_b else None,
        "new_yes_token": (new_b.get("yes_token_id") or new_b.get("_yes_token_id")) if new_b else None,
        "consensus": consensus_meta,
        "legs": [],
        "fire_budget_ms": int(cfg.get("fire_budget_ms", 8000)),
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
    tree["fired"][key] = {
        "status": "fired",
        "at_utc": iso_utc(now_utc),
        "jump": jump,
        "ref_source": ref_source,
        "consensus_rank": consensus_meta.get("rank"),
    }
    tree["armed"].pop(key, None)
    actions.append({"action_type": "re_fire", **fire})
    return actions
