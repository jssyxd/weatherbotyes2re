# Changelog — weatherbotyes2re

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Paper runtime now lives here: `reversal_runner.py` + Gamma/CLOB/CheckWX/cities/capital/settlement.
- ARM stations fast-poll METAR/books only (idle still samples the full universe).
- `consensus_min_samples` aligned to **20** (was 8 in the poly-yes2 copy).
- `paper_match_fak` guards `book is None`.
- **Not merged:** poly-yes2 `paper_runner` TAF/σ arms (BUY-YES, fade-NO, dead-NO grid). Retired.
- Canonical repo is this one. `poly-yes2` (main / treeA / treeB) can be deleted.

## 2026-09-02 — Consensus filter + stricter break (HFT path)

### Strategy
- **Long-horizon consensus filter** (`consensus_tracker.py`):
  - Continuous sampling of YES mid / best ask / top depth per bucket.
  - Default window **7200s (2h)** TWAP; require rank-1 on the *broken* bucket before FIRE.
  - Configurable `consensus_min_samples`, `consensus_min_lead`, `require_consensus_filter`.
- **Break confirmation tightened**:
  - Must be a **new `obs_time`** (duplicate pushes skipped).
  - Freshness default ≤ 180s.
  - Jump exactly 1 bucket for YES leg; jump > 1 → NO-only.
- **Reference extreme**:
  - Prefer TAF TX/TN.
  - If missing: fall back to market rank-1 bucket mid (`allow_market_consensus_reference`).
- Default notional split **75% NO / 25% YES**; NO cap raised slightly to **0.65** (still hard abort above cap).

### Execution
- FAK ladder unchanged in spirit: 0ms → 1.5s (+1 tick) → 4s → 8s abort; **never chase through cap**.
- `fire_budget_ms` plumbable from fire event / config.
- ARM still signals `fast_poll` + `prefetch_tokens` for the host observer.

### Ops / hardware (document only; you provision)
- Prefer a **low-latency VPS** close to Polymarket CLOB (often US East) with stable egress to AviationWeather / CheckWX.
- Keep **WebSocket order books warm** for candidate tokens; do not discover token IDs on the fire path.
- Dual-source METAR only on ARM stations (5–10s); global scan stays slower to respect rate limits.

### Files
- Added `consensus_tracker.py`
- Updated `reversal_strategy.py`, `re_execution.py`, `config.example.json`, `README.md`

### Not in this commit
- Live order signing / wallet path (still paper-first).
- Go/Rust port (see README section; Python remains the decision layer for now).
