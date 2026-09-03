# Changelog — weatherbotyes2re

## 2026-09-03 — Restore reversal_runner (was PLACEHOLDER) + dual-rate paper path

- **Critical:** `reversal_runner.py` had been reduced to the literal string `PLACEHOLDER` (11 bytes). Restored full paper entrypoint.
- No σ / bias / fade-NO / dead-NO / BUY-YES grid anywhere on the run path.
- Dual-rate: ARM stations fast METAR/books; idle full-universe METAR (~45s) + YES-book consensus (~30s) so TWAP never freezes when one city is armed.
- Dual-source METAR (CheckWX + AviationWeather), prefer fresher `obs_time`.
- C/F unit conversion via `c_to_market_unit` for US buckets.
- Gamma rules cache 20min; `--max-seconds` for soak tests; cycle_stats JSONL.
- paper capital default 1000 USDC; mode hard-blocked to paper only.
- Sandbox 10-min paper soak: real CheckWX + Gamma + CLOB; 49/49 METAR; 98 rules; Chicago/Dallas ARMed; no false FIRE; capital stayed 1000.

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Paper runtime lives here: `reversal_runner.py` + Gamma/CLOB/CheckWX/cities/capital/settlement.
- **Not merged:** poly-yes2 TAF/σ arms (BUY-YES, fade-NO, dead-NO grid). Retired.
- Canonical strategy+paper repo is this one. Keep poly-yes2 only if you still need hermes/settlement boards.
