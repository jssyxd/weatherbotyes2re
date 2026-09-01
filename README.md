# weatherbotyes2re

METAR vs TAF **reversal** strategy.

When live METAR proves the current TAF extreme is wrong, the market must reprice the broken bucket and the new bucket. Edge is **not** forecasting better than TAF. Edge is **seeing the break a few seconds earlier and filling before the scramble finishes**.

Default: paper / observe.

## Trade (high example)

TAF TX says 31. METAR running max prints 32 and enters the next bucket.

| Leg | Action | Why |
|-----|--------|-----|
| Broken TAF bucket | **BUY NO** | That YES is now almost dead if we assume 1-bucket error |
| New METAR bucket | **BUY YES** | This is the new candidate extreme |

Low is symmetric: METAR running min breaks below TAF TN.

Hard assumption: TAF error is usually **at most one bucket**. We only trade the broken bucket and the immediately adjacent new bucket. If METAR jumps two buckets, **do not** buy YES two steps away.

## What the original idea misses

1. **Broken-bucket NO is the cleaner leg.** Once the high has already printed above the TAF bucket, that bucket cannot be the daily high. YES on the *new* bucket can still be wrong if temperature keeps rising.
2. **One print is not a break.** A single spike / bad decode / runway vs official site mismatch will fake you out. Require: valid METAR, observation time fresh, and preferably 2 prints or 1 print + TAF already close.
3. **METAR cadence is the real bottleneck.** Many airports update every 20–60 minutes. If you poll every 120s like tree12, you are not early — you are in the same wave as every other bot. Speed lives in the *armed* state, not in the global scan.
4. **Everyone else is also reversing.** After the break, YES ask on the new bucket and NO ask on the old bucket gap up. Uncapped FAK is how you donate edge.
5. **Two-bucket jump kills the YES leg.** If it goes TAF 31 → 33 and buckets are 1°C, buying YES at 32 loses. Size the YES leg smaller than the NO leg.
6. **Official settlement source may not be your METAR.** Confirm city→station mapping. If Polymarket uses a different site, this strategy is noise.
7. **Do not keep adding during the scramble.** One fire per city/date/direction.
8. **Late-day vs early-day.** A break at 11:00 local (high) is weak. A break at 15:00–18:00 is much more informative.

## Recommended payoff structure

Default size split:

- **70% notional on NO** of the broken bucket (higher confidence)
- **30% notional on YES** of the new bucket (optional, can disable)
- Caps: buy NO only if ask ≤ `0.62`, buy YES only if ask ≤ `0.48`
- If either cap is blown, skip that leg. Taking only the NO leg is still a valid trade.

After fill:

- NO of broken bucket: hold to settlement unless a corrected METAR retracts the break (rare). Take profit if NO bid ≥ 0.85.
- YES of new bucket: if running extreme leaves this bucket too, flip — sell YES / do not hero-hold. This is a one-bucket ride, not a trend follower.

## How to react before the crowd

The 120s observer loop is too slow. Use a two-stage runtime:

### Stage A — ARM (before the break)

When `distance(running_extreme, TAF_extreme) <= arm_c` (default 1.0°C) **and** local hour is in the window (high ≥ 14, low ≤ 10):

- Pin that station to a **fast poll** (5–10s), dual source if possible (aviationweather + checkwx).
- Prefetch both token ids, tick size, and live WS books.
- Pre-build unsigned / signed order templates for:
  - BUY NO broken bucket @ cap
  - BUY YES next bucket @ cap
- Keep WS book warm. Do not discover the token after the break.

### Stage B — FIRE (on first accepted break)

Break rule (high):

```
obs_ok AND running_max > taf_tx
AND new_bucket == taf_bucket + 1
AND local_hour >= fire_hour
AND (obs_count_after_arm >= 1)
AND not already fired today
```

Then submit both FAK legs in parallel with a **time budget of ~8 seconds**. After that, cancel remainder. Do not walk the cap.

### What actually makes you earlier

- Fast poll only on armed stations, not 49 cities.
- Parse METAR `obs_time`, ignore stale copies of the same observation.
- Treat a new observation timestamp as the event, not "any JSON 200".
- Books already in memory; no REST fan-out after the trigger.
- FAK at `min(best_ask + 1 tick, cap)`, not GTC that sits behind the queue.
- One process, in-memory state. Disk/SQLite on the fire path is too slow.

You will not beat a colocated market maker. You can beat a 2-minute scanner.

## Fill module (`re_execution.py`)

Design: **arm → fire FAK → hard cap → short ladder → abort**.

```
for each leg:
  t=0ms   FAK size@min(ask, cap)
  t=1500  if unfilled and ask_now <= cap: FAK remainder@min(ask+1tick, cap)
  t=4000  if unfilled and still <= cap: FAK remainder@cap
  t=8000  abort leftover. never lift through cap
```

Rules:

- No cap walk. If the book is already 0.70 after the scramble, you are late. Stand down.
- Parallel legs, independent abort.
- Paper path must simulate against the same L2 snapshot, not mid.
- Live path must use already-reconciled balances; fire path cannot wait for a full account refresh.
- Dedup key: `city|date|direction|broken_bucket|new_bucket` so a second METAR copy cannot double fire.

## State machine

```
IDLE --(near TAF, in hour window)--> ARMED
ARMED --(break +1 bucket, fresh obs)--> FIRED
ARMED --(extreme moves away / hour ends)--> IDLE
FIRED --(done / abort)--> COOLDOWN (no re-fire same day/direction)
```

## Config

```json
{
  "mode": "paper",
  "weatherbotyes2re_enabled": true,
  "arm_c": 1.0,
  "max_bucket_jump": 1,
  "no_max_ask": "0.62",
  "yes_max_ask": "0.48",
  "no_notional_pct": 0.70,
  "yes_notional_pct": 0.30,
  "fast_poll_seconds": 8,
  "fire_budget_ms": 8000,
  "high_fire_local_hour": 14,
  "low_fire_local_hour_end": 10,
  "require_fresh_obs_seconds": 180,
  "yes_leg_enabled": true
}
```

## Files

- `reversal_strategy.py` — arm/fire rules, break detection, two-leg sizing.
- `re_execution.py` — fill module: cap FAK ladder, time budget, dedup.

Wire into weatherbot by: (1) fast-poll armed ICAOs, (2) keep CLOB WS on the two tokens, (3) call `maybe_fire_reversal` on each new METAR obs_time.
