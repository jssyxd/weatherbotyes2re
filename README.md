# weatherbotyes2re

METAR vs TAF **reversal** strategy — speed edge on consensus break.

When live METAR proves the current reference extreme wrong, the market must reprice the broken bucket and the new bucket. Edge is **not** forecasting better than TAF. Edge is **seeing the break a few seconds earlier and filling before the scramble finishes**.

Default: paper / observe.

## Core rules (2026-09-02)

### 1. Break confirmation
- High: `running_max`; Low: `running_min`.
- Reference extreme: **TAF TX/TN** if present; else **market 1–2h rank-1 YES TWAP** bucket mid.
- Jump allowed for YES leg: **exactly 1 bucket**. Jump ≥ 2 → NO-only on broken bucket.
- Must be a **new `obs_time`** (not a duplicate push).
- Observation age ≤ `require_fresh_obs_seconds` (default 180).

### 2. Long-horizon consensus filter (required by default)
Broken bucket must have been **rank-1** on YES TWAP over `consensus_window_seconds` (default 7200):
- Filters “market already rotated” false breaks.
- Needs continuous book sampling *before* the break (`ConsensusTracker.record_books`).

### 3. Time window
- High: local hour ≥ 14 (best 15–17).
- Low: local hour ≤ 10.

### 4. Legs
| Leg | Role | Default cap | Notional |
|-----|------|-------------|----------|
| BUY NO broken | Main (near-deterministic) | 0.65 | 75% |
| BUY YES new | Optional | 0.48 | 25% |

### 5. Execution (HFT-shaped)
- Idle poll normal; **ARM** (distance to ref ≤ `arm_c`) → fast poll 5–10s on that ICAO only.
- Prefetch token ids + keep **CLOB WS books** in memory.
- FIRE: parallel FAK, budget ~8s, ladder +0/+1 tick, **abort if ask > cap** (no chase).
- One fire per `city|date|direction`.

## Trade example (high)

TAF / consensus TX bucket = 31. METAR running max prints 32.

| Leg | Action |
|-----|--------|
| Broken 31 | **BUY NO** |
| New 32 | **BUY YES** (if jump==1 and ask ≤ cap) |

## Data path recommendation (across your repos)

| Need | Prefer | Source in your stack |
|------|--------|----------------------|
| Live top-of-book / depth | **CLOB Market WebSocket** | `poly-yes2/websocket_market_data.py` + `local_order_book.py` |
| Event + bucket map + token ids | **Gamma REST** (refresh ≤ 15–30 min) | `market_adapter.py` |
| Snapshot / fee / fallback | CLOB REST `/book`, fee endpoints | paper path only on signal |
| METAR/SPECI | AviationWeather + CheckWX; dual-source **on ARM only** | weatherbot `metar_observer` |
| TAF TX/TN | CheckWX / AWC | optional; market consensus is fallback |

**Do not** REST-poll books on the fire path. Subscribe early, keep LocalOrderBook fresh, FAK against in-memory L2.

## Hardware / latency

- Low-latency VPS near Polymarket infra (commonly **US East**), stable UDP/TCP, no residential CGNAT.
- Co-locate process: METAR parse + consensus state + WS books + signer in **one process / shared memory**.
- Avoid disk/SQLite on the fire path; append-only audit after.
- China egress: mirrored WSL or overseas VPS; Telegram optional via relay.

Go/Rust can shave parse/WS jitter, but **your bottleneck is METAR publish lag + CLOB RTT**, not Python’s microseconds. Keep strategy in Python until live path is proven; rewrite only the hot path (WS decode, order build, HTTP/2 post) if profiling shows need.

## Config

See `config.example.json`.

## Files

- `reversal_strategy.py` — arm/fire + consensus gate
- `consensus_tracker.py` — rolling YES TWAP / rank
- `re_execution.py` — capped FAK ladder + paper L2 match
- `paper_reversal_sim.py` — scenarios + loop

## Paper

```bash
python3 tests_reversal.py
python3 paper_reversal_sim.py --scenarios-only
```

## Safety

Paper by default. No wallet in this repo. Live requires separate reconciled balances and a real executor.
