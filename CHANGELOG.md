# Changelog — weatherbotyes2re

## 2026-09-07 — F-market unit audit + boundary-confirmation margin

- **Polymarket unit rules audited & documented** (`research/common.py`
  `c_to_market_unit` docstring):
  - Buckets: US cities 1-2°F integer buckets; EU/Asia cities 1°C buckets.
  - Resolution: Wunderground station "Daily Observations" — finalized daily
    extreme at whole degrees, post-QC (NOT intraday METAR, NOT the NWS CLI
    summary, NOT the WU "Day High & Low" box). Stated precision rule is
    truncation for °C buckets (23.9°C → 23).
  - METAR has NO native °F anywhere (global °C, incl. US ASOS). US ASOS
    displays whole °F via rounding — our °C→°F round matches that display
    convention; the Polymarket truncation rule applies to the °C-bucket side
    where whole-degree METAR already aligns naturally.
- **F-market break-confirmation margin** (`reversal_strategy.py`,
  `break_confirm_margin_f` default 1.0, config key added): a °F-market fire
  requires the whole-degree converted extreme to clear the broken-bucket
  boundary by ≥1°F. Motivation: SF 9/4 low misfire — METAR 14°C converted to
  57.92°F < 58 (break), but Wunderground finalized 58.x°F (no break): METAR
  whole-°C granularity spans ±0.9°F after conversion and the finalized daily
  extreme can differ ~1°F from the intraday METAR extreme.
- **Back-test on real °F fills (2026-09-05→07, 5 trades)**: margin=1 would
  have kept SF 9/6 (YES@0.52 WON) and SF 9/7 (open), and filtered chicago
  9/5 low (NO@0.97 LOST — the SF-class false break) — but it would also have
  filtered atlanta 9/6 low (NO@0.92 WON + YES WON) and austin 9/5 high
  (YES@0.98 WON), both genuine near-boundary breaks. Trade-off is documented
  and tunable: 0.0 = legacy float behavior (fire all near-boundary breaks),
  1.0 = filter all <1°F-deep breaks (default, prevents SF-class false
  breaks at the cost of genuine near-boundary fills). C markets are exempt
  (whole-degree truncation aligns exactly).

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
