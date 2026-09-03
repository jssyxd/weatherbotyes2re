# Changelog — weatherbotyes2re

## 2026-09-03 — Paper soak + dual-rate runner (no σ)

- **No σ / bias / fade-NO / dead-NO / BUY-YES grid** on any run path (comments only).
- Full paper entry: `runner_impl.py` (or assembled from `runner_impl.b64.*` via thin `reversal_runner.py`).
- Dual-rate: ARM stations fast METAR/books (~8s); idle full-universe METAR (~45s) + YES-book consensus (~30s) so TWAP never freezes when one city is armed.
- Dual-source METAR (CheckWX + AviationWeather), prefer fresher `obs_time`.
- C/F via `c_to_market_unit` for US buckets.
- Gamma rules cache 20min; `--max-seconds` soak; cycle_stats JSONL.
- Paper capital default 1000 USDC; mode hard-blocked to paper only.
- **Sandbox 10-min paper soak (real CheckWX + Gamma + CLOB):** 49/49 METAR; 98 rules; Atlanta+Denver ARMed (F units + market_rank1/TAF refs); 0 false FIRE; capital stayed 1000; no cycle_error.

## 2026-09-02 — Merge poly-yes2 paper infra; drop σ

- Paper runtime lives here. **Not merged:** poly-yes2 TAF/σ arms. Canonical strategy+paper repo is this one.
