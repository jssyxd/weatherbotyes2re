# weatherbotyes2re

METAR vs TAF **reversal** — speed edge on consensus break.

When live METAR proves the current reference extreme wrong, the market must reprice
the broken bucket and the new bucket. Edge is **seeing the break a few seconds
earlier and filling before the scramble finishes**, not forecasting better than TAF.

Paper only. No wallet. No σ / fade-NO / BUY-YES grid.

This repo now owns both the strategy kernel and the paper runtime (Gamma, CLOB,
CheckWX, cities, capital, settlement) that used to live in `poly-yes2`.

## Rules

- High: `running_max`; Low: `running_min`.
- Reference: TAF TX/TN if present, else 1–2h rank-1 YES TWAP bucket mid.
- YES leg only if jump **exactly 1** bucket. Jump ≥ 2 → NO-only.
- New `obs_time`, age ≤ 180s.
- Broken bucket must have been rank-1 over `consensus_window_seconds` (default 7200).
- High local hour ≥ 14; low ≤ 10.
- Legs: BUY NO broken (cap 0.65, 75%) + optional BUY YES new (cap 0.48, 25%).
- Idle poll ~20s; **ARM** (distance ≤ `arm_c`) → fast poll those ICAOs only (~8s).
- FIRE: in-memory L2 FAK, 8s budget, +0/+1 tick, abort if ask > cap. One fire per `city|date|direction`.

## Run (paper)

```bash
export CHECKWX_API_KEY=...
python3 tests_reversal.py
python3 reversal_runner.py once --config config/yes2re_reversal.json
python3 reversal_runner.py run  --config config/yes2re_reversal.json
# logs: data/yes2re_events.jsonl  health: data/yes2re_health.json
```

WSL: `networkingMode=mirrored` so Gamma/CLOB resolve.

## Layout

| File | Role |
|------|------|
| `reversal_strategy.py` | ARM/FIRE + consensus gate |
| `consensus_tracker.py` | rolling YES TWAP / rank |
| `re_execution.py` | capped FAK ladder + paper L2 match |
| `reversal_runner.py` | paper loop: CheckWX + Gamma + CLOB + settle |
| `config/contract_cities.json` | 49 cities |
| `config/yes2re_reversal.json` | runner config |

## Safety

`reversal_runner.py` hard-blocks any mode other than `paper`.
