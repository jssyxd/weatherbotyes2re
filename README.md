# weatherbotyes2re

METAR vs consensus **reversal** — see the break early, paper-fill before the scramble.

**No σ. No fade-NO / BUY-YES grid. No wallet. Paper only.**

## Rules

- High: `running_max`; Low: `running_min`.
- Reference: TAF TX/TN if present (converted to market unit), else 1–2h rank-1 YES TWAP mid.
- YES leg only if jump exactly 1 bucket; jump ≥ 2 → NO-only.
- New `obs_time`, age ≤ 180s; high local hour ≥ 14; low ≤ 10.
- Broken bucket must be rank-1 over `consensus_window_seconds` (default 7200).
- Legs: BUY NO broken (cap 0.65, 75%) + optional BUY YES new (cap 0.48, 25%).
- Idle ~20s; **ARM** → fast poll those ICAOs (~8s) while **full universe** still samples books/METAR slowly for consensus.
- FIRE: in-memory L2 FAK, 8s budget, abort above cap. One fire per `city|date|direction`.

## Data path

| Need | Source |
|------|--------|
| METAR | **CheckWX + AviationWeather** (fresher wins) |
| TAF TX/TN | CheckWX (optional; market rank-1 fallback) |
| Units | METAR/TAF °C → city `market_unit` (°C/°F) |
| Buckets / tokens | Gamma REST (cached ~20 min) |
| Books | CLOB REST seed into `LocalOrderBook`; optional Market WS |

## Run

```bash
export CHECKWX_API_KEY=...   # still useful; AWC works without key
python3 tests_reversal.py
python3 reversal_runner.py once --config config/yes2re_reversal.json
python3 reversal_runner.py run  --config config/yes2re_reversal.json
```

Logs: `data/yes2re_events.jsonl` · Health: `data/yes2re_health.json`

WSL: `networkingMode=mirrored` for Gamma/CLOB.

## Latency notes

- Prefer a low-latency VPS near Polymarket CLOB (often US East).
- Keep token ids + books warm before FIRE; do not discover tokens on the fire path.
- Bottleneck is METAR publish lag + CLOB RTT, not Python microbenchmarks.

## poly-yes2

This repo is the **canonical reversal paper** stack. Archive or ignore poly-yes2 treeB for reversal.
Keep poly-yes2 only if you still need the old three-arm / Hermes / settlement-review history.

## Safety

`reversal_runner.py` hard-blocks any mode other than `paper`.
