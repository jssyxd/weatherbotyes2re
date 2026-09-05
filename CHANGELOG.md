# Changelog — weatherbotyes2re

## 2026-09-05 — Capital & settlement tuning; remove ghost max_open_positions

- **paper_initial_capital_usdc: 1000 → 5000.** First soak (2026-09-04) revealed
  capital exhaustion after 7 concurrent fires (insufficient to sustain reversal
  stream across 49-city universe). Increased 5x to allow ~20–25 simultaneous
  positions before capital constraint (typical Polymarket daily weather window
  has ~5–10 armed sessions in parallel).
- **settle_grace_hours: 6 → 2; added settle_poll_seconds: 60.** Position
  settlement polling was using the default 3600s (1h) cadence. Gamma resolution
  API is fast (typically completes within 5–10 min of settlement time); the
  1h grace window meant capital stayed locked for an hour post-resolution,
  starving new fires. Reduced grace to 2h and poll every 60s so resolved
  positions recycle capital into the available pool within 1–2 min of
  settlement.
- **Removed max_open_positions: 12.** Configuration parameter was unimplemented:
  `_r_cycle.py` counts open positions only to decide settlement cadence, never
  to gate fires. The limit created confusion (appearing active in config while
  having no effect). Actual constraint is `paper_initial_capital_usdc` via
  `paper_capital.reserve()`.
- **Root cause audit:** Paper trading halted mid-stream because OPEN orders
  (submitted but unfilled) + stale settlements (1h grace, 1h poll) formed a
  capital lockup cascade. First 7 fires (all jump==1, solid signals) succeeded,
  but positions held cash for hours; subsequent fires (jump>=2 or different
  cities) couldn't reserve. Fixed by 5x capital + 60x faster settlement flow.

## 2026-09-04 — Fire deadlock fix; WS live feed; paper-ledger fix (audited)

- **obs sanity window (was: absolute 180 s age gate → structurally zero fires).**
  METAR/SPECI obs_time age swings 0-60 min on hourly cadence (US AWS publish
  ~7 min early); `require_fresh_obs_seconds=180` made `stale_obs` block every
  fire. Replaced with sanity window `max_obs_lookback_seconds=5400` /
  `max_obs_future_seconds=900`: any NEW observation (deduped by
  `is_new_obs_time`) may fire unless the feed is >90 min behind or the stamp
  is >15 min in the future. First live fire within 27 min of deploy.
- **Full skip audit.** `_r_cycle` no longer silently drops skips: every
  re_skip / re_skip_yes / re_disarm is logged with reason/jump/consensus
  (silent skips previously hid the 0-fire deadlock).
- **NO cap 0.65 → 0.85** (broken-bucket NO redeems ~1.0; wider cap = fills);
  YES leg cap unchanged 0.48.
- **Universe: 10 → all 49 cities** (drop `active_icaos` allowlist; both high
  & low directions). `idle_metar_interval_seconds` 45 → 60 (49 cities = 3
  CheckWX batches; 4320 req/day < 5000 paid cap).
- **Market WebSocket live** (`market_ws_transport.py` stdlib-only WS client
  through the CONNECT proxy + `ws_bridge.py` daemon thread). 2000+ tokens
  subscribed; fresh (<5 s) WS LocalOrderBook snapshots overlay the ladder
  cache (epoch-guarded, never clobbers newer REST data); auto-reconnect
  5/10/30 s; REST /books remains the correctness backbone (the public market
  channel is near-frozen per py-clob-client #292 — WS is an accelerator).
- **Paper ledger fix.** `release()` no longer clamps total debit to zero —
  a negative debit is realized profit (equity = initial − debit). The clamp
  had silently discarded +52.80 USDC of paper profit (cost 49.06 vs payout
  101.86). `total_debit_usdc()` now reads negative values directly instead of
  through the `parsed >= 0` filter.
- **Audit hardening (pi + omp cross-review 2026-09-04):** `ensure_tokens`
  and `mark_disconnected` thread-safety (dict-size-change race during
  reconnect); `_ws_pump` epoch comparison made real (docstring now honest).
- Verified: 7/7 scenario tests; equity 1000 → 1052.79 after first US market
  settlements (NO legs 4/4 wins; one YES lottery leg lost).

## 2026-09-03 — Dual-rate paper runner; no σ; real-API soak

- **Zero σ / bias / fade-NO / dead-NO / BUY-YES** on the run path.
- Dual-rate METAR/books (ARM ~8s; idle METAR ~45s + consensus books ~30s).
- Dual-source METAR (CheckWX + AWC); C/F via `c_to_market_unit`; Gamma rules cache 20min.
- Modules: `runner_impl.py`, `_r_globals.py`, `_r_state.py`, `_r_data.py`, `_r_cycle.py`, `_r_exec.py`.
- **10-min paper soak (real CheckWX+Gamma+CLOB):** 49/49 METAR, 98 rules, Atlanta+Denver ARMed, 0 FIRE, capital 1000 USDC, no cycle_error.

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Not merged: TAF/σ arms. This repo is strategy + paper runtime.
