# Changelog — weatherbotyes2re

## 2026-09-03 — Ops fixes (no σ); dual METAR; units; dual-rate consensus

- **Confirmed: zero σ / fair-value / bias path** in this repo (never imported).
- **(1)** Dual-rate sampling: ARM stations fast METAR/books; full universe keeps idle METAR (~45s) + YES-book consensus (~30s) so rank filter does not freeze.
- **(2)** `local_order_book.py` + `websocket_market_data.py` added; runner REST-seeds in-memory L2 for FAK (live WS transport optional).
- **(3)** Dual-source METAR: CheckWX + AviationWeather; prefer fresher `obs_time`.
- **(4)** `c_to_market_unit`: map °C METAR/TAF into city `market_unit` (°F buckets for US).
- **(5)** `fetch_market_resolution` typed as `tuple[str|None, source] | None`.
- **(6)** Docs: poly-yes2 may be archived for *reversal paper*; do not delete if you still need three-arm history/ops.
- **(7)** Gamma rules cache default 20 minutes.
- **(8)** README restores latency / WS notes.

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Paper runtime: `reversal_runner.py` + Gamma/CLOB/CheckWX/cities/capital/settlement.
- ARM fast-poll; `consensus_min_samples=20`.
- Not merged: TAF/σ arms (BUY-YES, fade-NO, dead-NO grid).

## 2026-09-02 — Consensus filter + stricter break (HFT path)

- Long-horizon consensus filter, new obs_time, market rank-1 reference fallback.
- Capped FAK ladder; paper by default.
