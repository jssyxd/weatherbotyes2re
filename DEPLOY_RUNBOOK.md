# Weatherbotyes2re — Paper Runner 部署 Runbook

Live real‑data paper‑trading deployment + 每30分钟自动化巡察 / Telegram 汇总 .

## 1. 概览

- **引擎**: 纯 Python 3.13 (stdlib only), Polymarket 日度天气市场的 METAR‑vs‑consensus 单桶 reversal 策略。
- **模式**: `paper` only — 无真实钱包/下单。`reversal_runner.py` 硬性拒绝非 paper 模式。
- **初始资金**: 1000 USDC (paper ledger). 单笔 fire 预算 ≤ 20 USDC.
- **活动城市 (10)**: amsterdam, atlanta, austin, london, madrid, mexico‑city, paris, sao‑paulo (SBGR), tokyo (RJTT), toronto (CYYZ).
- **数据源**: CheckWX(key, 从 .env) + AviationWeather (无key) 双源 METAR; Gamma REST 规则发现; CLOB REST 只读 books (WS 未启用, REST seed 模式).

## 2. 运行中的组件 (全部在 /home/da/桌面/poly-yes2/weatherbotyes2re)

| Component | 形态 | 状态 |
|---|---|---|
| paper runner | hub 常驻服务 `paper_runner` | running (pid 动态), 持久+重启 on‑failure |
| reversal‑watch | Hermes cron, 每30min, `pi`巡察 | active, `*/30 * * * *` |
| reversal‑report | Hermes cron, 每30min, Telegram 汇总 | active, `*/30 * * * *` |
| 观测脚本 | `~/.hermes/scripts/reversal_observe.py` | deterministic fact block |
| 巡察脚本 | `~/.hermes/scripts/reversal_watch.py` | observer + `pi` 反幻觉 verdict |

### 启动/重启 runner
```bash
# 通过 hub (本 omp 会话内)
# paper_runner 已由本部署启动；如需重启: hub restart paper_runner
# 手动前台冒烟:
cd /home/da/桌面/poly-yes2/weatherbotyes2re
python3 tests_reversal.py                                   # 7 场景 (离线合成)
python3 reversal_runner.py once --config config/yes2re_reversal.json   # 一个真实数据 cycle
python3 reversal_runner.py run  --config config/yes2re_reversal.json --max-seconds 600
```
日志/健康/状态输出:
- events: `data/yes2re_events.jsonl` (arm/disarm/fire/settle/rules_refresh/cycle_error…)
- health: `data/yes2re_health.json` (含 `feed.*` 观测面)
- state:  `data/yes2re_state.json`

## 3. 30‑min 自动化

- **reversal‑watch (巡察)**: 每30min. observer 读取 data/* → 确定性 fact block → `pi -p --no-tools` 基于 fact block 反幻觉审查, 输出 JSON verdict {status, verified, anomaly_flags, recommendation}. Deliver: local (日志). 任何 flag 都是带具体 field→value 的可查证据.
- **reversal‑report**: 每30min Hermes summarizer 读 observer 输出 → 简短中文 Telegram 到 `telegram:liudi`.

手动触发单次 (若想当下跑一次, 不一定等 tick):
```bash
hermes cron run reversal-report   # ~instant (observer only)
hermes cron run reversal-watch    # 注意: 包含 pi (~90–160s), cron run CLI 会阻塞到 local delivery ack;
                                  # 真正调度器在 :00/:30 独立执行, 无此阻塞.
python3 ~/.hermes/scripts/reversal_observe.py | python3 -m json.tool   # 只看 fact block
```
调度器状态: `hermes cron list` / `hermes cron status` / `hermes cron runs <job>`.
Telegram 通道连通自检: `hermes send --to telegram:liudi "hi"`.

## 4. telemetry / 观测面 (watcher 判据)

见 `reversal_observe.py` 输出的 JSON: `runner`(alive/file_age/mode), `feed_health`(armed/open_positions/capital/metar_cities_stale/books_max_age/websocket·clob·gamma/rules_failures), `anomalies`(event_anomalies/stale METAR/rules failures), `activity_30m`, `trades`.
- METAR obs age > 3600s → 标 stale (远端站正常可 1–2h; 若 ARM 且 stale 近数小时即 feed 死亡信号).
- rules discovery 间歇性 `TimeoutError` → 重试即可; 持续失败才是 Gamma 故障.

## 5. 运维命令

```bash
cd /home/da/桌面/poly-yes2/weatherbotyes2re
python3 reversal_runner.py status --config config/yes2re_reversal.json   # 打印 armed/fired/open/capital
tail -50 data/yes2re_events.jsonl
jq . data/yes2re_health.json
```
资金/持仓核实: 见 health 的 `capital_initial_usdc`/`debit_usdc`/`open_positions` 与 state 的 `positions{}`, `entry_count`.

## 6. 待办 / 已知点
- Gamma 日度契约的**今日 local date** 规则发现存在间歇性 TimeoutError — 已放宽到 12s/req, 150s deadline; 属重试性, 不由巡察硬判定为故障.
- `websocket_market_data` 已实现但本 paper 部署用 REST seed (health 内显式 `websocket.deployed=false`); 若需 WS book 推送, 在 `_r_cycle.refresh_books` 前置 MarketStream 接入.
- CheckWX key 在 `~/.env`; 轮换后更新该文件 (勿提交, gitignored).
- 巡察/汇总脚本存放于 `~/.hermes/scripts/` (Hermes cron 要求), 非仓库内; 改动后 `hermes cron` 引用同名仍生效.
- 若本 omp 会话/机器重启, `paper_runner` hub 服务与 hermes cron gateway 均需确认起来 (本轮已 persist runner; gateway pid 1596 为系统级).
