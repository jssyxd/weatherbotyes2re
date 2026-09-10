# Changelog — weatherbotyes2re

## 2026-09-10 — LIVE 执行层 Phase 1: 只读对账层 (`live/`)

- **新增完全独立的只读 live 侧模块**（主引擎仍硬性 paper-only；未改动任何现有模块、config 值或服务）：
  - `live/creds.py` — `.env` 凭据读取 + 格式校验（仅掩码输出，绝不打印全值）
  - `live/clob_client.py` — `py-clob-client` 惰性薄封装（依赖缺失时给出明确指引）
  - `live/risk_gate.py` — 纯函数风控闸门（fail-closed，机器可读 reason 码）
  - `live/reconcile.py` — 只读对账 CLI（余额 / 授权 / 挂单 / 持仓 / 风控评估 / 出口 IP），产出 `data/live_reconcile.json`
  - `tests_live.py` — 14 项单测（含"无下单调用"静态断言、非 mapping env 与非法值契约用例）
  - `live/README.md` — 运行方式 + 阶段梯子（只读对账 → 干跑签名 → 最小单 → 放量）
- **硬保证**：`live/` 内零下单/撤单/授权写入路径（静态扫描 + 运行时出网 trace 双验，全部 GET）；fail-closed（异常 ⇒ `ok:false` + exit 2）；密钥卫生（正常与异常路径输出全量 grep 零命中）。
- **首次实跑核账**（本机走 7890 代理、my155 直连，两路径结果一致）：USDC(pUSD) **51.713622**、4 个合约 allowance = uint256-max、挂单 0、79 条历史仓 `currentValue` 全 0（无活跃持仓）。
- **风控参数（操作者拍板）**：`LIVE_MAX_CAPITAL_USDC=50` / `LIVE_MAX_OPEN_POSITIONS=10` / `LIVE_FIRE_BUDGET_USDC=5`（10×5=50，与资金上限自洽）。
- **独立审计**（herdr 双 agent：impl + audit，对抗性立场）：**APPROVE**（含变异测试、链上独立复核 pUSD 余额、工作树独立性核对）；F1 契约瑕疵已闭合，F2 一行硬化同批修复。
- **工程卫生**：`.gitignore` 增加 `.env*`——此前 `.env.bak.<ts>` 备份（含私钥）未被忽略，存在误提交风险。
- **依赖说明**：live 侧第三方依赖仅 `py-clob-client`，装在独立 venv（本机 `~/桌面/poly-yes2/live-probe/.venv`、my155 `/root/live-probe/.venv`），仓库自身仍保持 stdlib-only。

## 2026-09-09 — Same-session double fire (追火) with symmetric NO+YES legs (da12518)

- **A market key (city|date|direction) may now fire at most TWICE per day.**
  Warsaw 2026-09-09 LOW 17→16→15 double break (operator decision, 2026-09-09):
  fire #1 bought YES on 16°C when the reference broke 17→16; temperature then
  fell through to 15°C and the 16°C YES was headed to zero with no further
  action possible under the old one-shot `already_fired` lock.
  New rule:
  - Fire #1 unchanged. After it, the session stays eligible for ONE 追火 when a
    fresh obs (age ≤ 180s) breaks ONE bucket past the YES bucket fire #1
    actually bought (reference — TAF or consensus rank-1 — having ratcheted
    onto that bucket first), with every gate identical to fire #1: jump=1,
    local fire window (HIGH 13-17 / LOW 1-9), consensus filter, YES price gate
    `(yes_min_ask, yes_max_ask]`.
  - 追火 leg structure is symmetric with fire #1: `buy_no_broken` on the
    newly-broken bucket (= the bucket fire #1 holds YES in; its NO is priced
    ~1 by then — placed per operator decision, cap 1.0) + `buy_yes_new` on the
    new bucket YES.
  - On 追火 fill, fire #1's old-bucket YES leg is sold at best_bid (shared
    `close_leg_at_best_bid` in `paper_capital.py`, same mode as sleeve-timeout
    closes; no bid → written off at 0, the new NO leg hedges).
  - Breaks past fire #2 take no further action (`fires >= 2` → `already_fired`).
  - `fires` counter persisted in each `fired` record; legacy records (no
    `fires` field) migrate as "1 used" — an old fired key may still 追火 once,
    but eligibility requires an open un-settled YES leg with shares, so pure
    lock/no-fill records stay inert.
  - Events: `fire` carries `fire_no` 1/2; new `close_old_yes` event logs
    shares / bid_at_close / proceeds_usdc / loss_usdc for the liquidation.
- Verified: `tests_reversal.py` 24/24 PASS (incl. refire_above_cap,
  refire_out_of_window, refire_persists_across_restart, warsaw-style
  two-branch close scenarios), sleeve 13+4, `tests_fill_gate.py` 6,
  `paper_reversal_sim.py --scenarios-only` exit 0. Dual herdr agent review
  (impl + independent audit) APPROVED. Deployed 2026-09-09 21:3x CST.
- Deployed on 192.168.1.98 (本机) with the account still at 500 USDC paper
  (config initial capital intentionally 600 per operator — takes effect on the
  next blank-state rebuild; the live guard never rewrites a trading account).

## 2026-09-09 — YES/NO leg fill floor; TAF AMD fix; market-ref fire gate; rules-refresh hardening (f5318c7)

- **Optional YES-leg fill floor** (`plan_leg_attempts`): ladder rungs are
  skipped while `best_ask <= leg.floor` (breakout not yet confirmed), aborted
  above cap as before, traded only inside `(floor, cap]`.
  `re_execution.py` + `tests_fill_gate.py`. Config: `yes_max_ask` 1.0 → 0.9,
  new `yes_min_ask` 0.48. (YES bought at ~0.91 / 0.945 / 0.99 in the 9/8–9/9
  fires was structurally too expensive — gate now caps the entry.)
- **TAF AMD/COR/RTD key fix** (`research/common.py` `checkwx_taf`): amended
  TAFs like `TAF AMD EGLC ...` were keyed under `out["AMD"]`, silently losing
  the TAF reference for that airport (London case: reference fell back to
  market rank-1 and the bot traded against the market). Corrector markers are
  now skipped and the real ICAO is always the key; multi-line TAF bodies
  handled.
- **`taf_no_extreme` event**: a TAF with no parseable TX/TN now logs a
  rate-limited per-ICAO event instead of failing silently (fail-closed
  semantics unchanged).
- **Market-ref fire gate**: `allow_market_ref_fire` config key makes
  fire-on-market-rank-1-reference optional; TAF-sourced sessions unaffected.
  HIGH direction gained an upper local-hour bound (late-evening off-window
  fires suppressed).
- **Rules-refresh failure hardening** (`_r_cycle.refresh_rules`, 2026-09-08
  KR-egress 451 incident): a full-failure round NEVER wipes the previously
  good rules index (old rules stay usable; caller filters non-today dates) and
  cold-start failures back off 120 s instead of storming Gamma every cycle
  (~6 900 wasted refreshes observed 2026-09-08). Prior-art reference:
  polymarket-market-data skill gamma-refresh-failure-diagnosis.
- Verified: `tests_reversal.py` incl. `market_ref_fire_allowed` /
  `high_late_evening_skip`, sleeve tests, fill-gate 6 — all green. Deployed
  with the double-fire change.

## 2026-09-08 — Fire-window intervalization (HIGH 13-17 / LOW 1-9 local)

- **`reversal_strategy.py` fire window switched from single-edge bounds to
  inclusive local hour intervals.** One-bucket reversal fires now gate on:
  - HIGH: local `13 <= hour <= 17` (was `hour >= 14`, no upper bound)
  - LOW: local `1 <= hour <= 9` (was `hour <= 10`, no lower bound)
- Constants: `HIGH_FIRE_LOCAL_HOUR` / `LOW_FIRE_LOCAL_HOUR_END` removed →
  `HIGH_FIRE_LOCAL_START=13` / `HIGH_FIRE_LOCAL_END=17` /
  `LOW_FIRE_LOCAL_START=1` / `LOW_FIRE_LOCAL_END=9`. `hour_ok` now takes the
  four window bounds and enforces `start <= h <= end` per direction. Both `arm`
  and the pre-fire `hour_not_in_window` gate use the same window. `prune`
  low-zombie sweep follows the new low end (9).
- Config keys: `high_fire_local_hour` / `low_fire_local_hour_end` →
  `high_fire_local_start` / `high_fire_local_end` /
  `low_fire_local_start` / `low_fire_local_end` (`config/yes2re_reversal.json`
  updated; old keys removed).
- **Rationale:** the daily extreme (and the capped peak-tick reversal this
  strategy sells) forms inside the window, not outside it. Real losses from
  out-of-window fires: mexico-city low 02:02 (LOST), SF 9/7 01:00 local fire
  (open, floating underwater) — both broke the reference at hours the peak
  window does not span. Fires observed off-window are now suppressed.
  Direction-specific windows also stop one city's LOW-break drift from firing
  into the afternoon or a HIGH from firing predawn. On-window losses of the
  chengdu class are a separate open question (bucket-break confirmation) and
  are not addressed by this change.
- Verified: `python3 tests_reversal.py` 16/16 PASS before and after (all 16
  scenarios keep passing under the interval semantics); window-boundary
  assertion (high 12/18 rejected, 13..17 accepted; low 0/10 rejected, 1..9
  accepted) green.

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
