# 待处理事项（PENDING） — weatherbotyes2re paper + LIVE

> 维护约定：本文件是**未完成/待决策/已知异常**的单一清单。每项含：现状 → 影响 → 待办/决策点 → 相关文件。
> 最后更新：2026-09-11 01:20 CST（UTC 17:20）· HEAD `95b1660` + 本轮 3b-4 改动
> 相关日志：`ops/repair_log.md`（paper healer）、`ops/repair_log_live.md`（LIVE healer）

---

## 0. 当前系统全貌（先读这段）

| | **paper（本机 192.168.1.98）** | **LIVE（my155, 155.254.60.38 MY）** |
|---|---|---|
| 服务 | systemd user `yes2re-paper` | systemd `yes2re-live` |
| 代码 | 同一仓库 `~/桌面/poly-yes2/weatherbotyes2re` | `/root/weatherbotyes2re`（同一 HEAD） |
| 模式 | `YES2RE_MODE=paper` | `YES2RE_MODE=live`（`.env`） |
| 成交 | `PaperPort`（内存 FAK 模拟） | `LivePort`（真实 CLOB v2 下单/撤单/对账） |
| 真实下单闸门 | 不适用 | **未开**：引擎侧三闸门（`YES2RE_LIVE_ENABLE_SUBMIT`+`LIVE_SUBMIT_ENABLED`+当日 `YES2RE_LIVE_CONFIRM`）均未设 → 每次 fire 被端口拒绝并记 `fire_port_refused`（详见 §1 D-1） |
| 资金 | 虚拟（账本初始 500，config 默认 600） | **真实 51.713622 USDC**（pUSD 结算资产） |
| 汇报 | `reversal-report` 每 30min → Telegram | `live-report` 每 30min → Telegram（①开仓 ②当前权益） |
| 体检/自愈 | `yes2re-healer` 每 15min（monitor） | `live-healer` 每 15min（monitor） |
| 观测脚本 | `~/.hermes/scripts/reversal_{observe,triage,watch}.py` | 同三件 + `live_{mirror_sync,observe,triage}.py`（镜像 `~/桌面/poly-yes2/live-mirror`） |

**策略/逻辑/基建完全同源**：`_r_cycle.py` 的 fire 段通过 `live/port.py` 选端口，策略判定/时间窗/共识/sleeve/腿 sizing/状态 schema/事件/健康/结算记账全部共用；两实例共用**同一份** `config/yes2re_reversal.json`，差异只由环境变量控制（见 §4）。

---

## 1. 需要操作者决策（阻塞项）

### D-1. LIVE 真实下单闸门何时开启（Phase 4）
- **现状**：my155 `yes2re-live` 正常运行但闸门关闭 → 只观察、绝不下单（`fire_port_refused` 事件记账）。
- **开启方式**（引擎侧需**三个服务级闸门同时满足**，代码位置 `live/port.py:ENV_ENABLE_SUBMIT/ENV_CONFIRM` + `live/submit.py:gates_all_passed`）：
  1. my155 的 systemd 单元里加三个 `Environment=`（**刻意放单元不进 `.env`**，避免误开）：
     ```
     Environment=YES2RE_LIVE_ENABLE_SUBMIT=1
     Environment=LIVE_SUBMIT_ENABLED=1
     Environment=YES2RE_LIVE_CONFIRM=SMOKE-$(date -u +%Y-%m-%d)   # 当日 UTC 日期短语
     ```
  2. `systemctl daemon-reload && systemctl restart yes2re-live`
  3. 验证：`git rev-parse --short HEAD` 不变、`journalctl -u yes2re-live | tail` 无 `fire_port_refused`；或在 my155 跑 `/root/live-probe-v2/.venv/bin/python live/v2_transport.py --status` 看闸门状态。
- **⚠ 日期短语会过期**：跨过 UTC 零点后闸门自动失效 → 服务需重启（刻意的：连续无人值守放量不属于 Phase 4 范围）。若要长期开启，改为每日重载或去掉短语（需你明确同意）。
- **开启后行为**：真实 fire 时按引擎同一套判定下单 —— NO 腿 75% / YES 腿 25%，预算 `YES2RE_FIRE_BUDGET_USDC=12`（引擎侧）与 `LIVE_FIRE_BUDGET_USDC=12`（端口上限）一致；单笔名义额 ≤12、累计 ≤50、最多 10 个并发仓；只下**不会立即成交**的 post-only 限价单（BUY 价 < best_ask，提交前重取盘口夹紧）。
- **决策点**：何时开？是否先只开特定城市/方向？是否接受"每日需重启续期"？

### D-2. paper 初始资金：config 600 vs 账本 500
- **现状**：`config/yes2re_reversal.json` `paper_initial_capital_usdc=600.0`（你此前说"有意"），但 paper 账本 state 里是 `500.0`（9/8 清场时建立）。healer 每轮以 `state_initial_mismatch` CRIT 上报（待人工项①）。
- **影响**：只是告警噪音（guard 生效，账本仍是 500），但会让 healer 每 15min 判 CRIT、影响告警信噪比。
- **待办**：二选一 —— ① 把 config 改回 500（与账本一致，需你确认 600 是否真的要生效）；② 保留 600 并接受"下次清场重建账本才生效"，同时把 healer 的该 class 降级为 INFO（需要改 `reversal_triage.py`）。

### D-3. `miami no_book → already_fired` 永久锁
- **现状**：fire 决定后若 NO 盘口无卖盘（`no_book`）则该 `city|date|direction` 被永久标记 `already_fired`，同窗口不再重试。
- **影响**：可能错过后续出现的流动性。
- **待办**：是否允许"同窗口内 no_book 后有限重试"（例如窗口内最多 N 次、间隔 ≥15min）；改动在 `reversal_strategy.py`（策略层，需你拍板）。

### D-4. LIVE 结算与领取（settlement / claim）尚未实现
- **现状**：LIVE 端口目前覆盖 **下单 → 成交对账 → 撤单**。真实仓位到期后由 Polymarket 自动 resolve，资金/份额需在链上 `redeem`（或由页面自动领取）才会回到可用余额。
- **影响**：若不实现，live 账本会显示"已结算"而真实 USDC 未回笼（对账缺口）。
- **待办（Phase 5）**：实现结算检测（同 paper 的 resolution 检测）→ 链上 redeem（可用 relayer key 免 gas）→ 与账本对账。需你确认是否用 relayer（`RELAYER_API_KEY` 已配）。

---

## 2. LIVE 侧技术待办（不阻塞）

### T-1. 实盘账本初始资金对齐真实余额（本轮已加机制，待部署生效）
- 已新增 env 覆盖 `YES2RE_INITIAL_CAPITAL_USDC`（`_r_state.load_config`，非法值 fail-closed）。
- **部署步骤（待执行）**：my155 `.env` 加 `export YES2RE_INITIAL_CAPITAL_USDC=51.713622` → 删除 `/root/weatherbotyes2re/data/yes2re_state.json`（当前无持仓、无成交，可安全重建）→ `systemctl restart yes2re-live` → 验证 `equity.initial_capital_usdc=51.713622`。
- 否则 live 的"当前权益"会以 config 的 600 起算，与真实账户不可比。

### T-2. my155 WebSocket `connected=false`
- 现状：live 实例 `websocket.connected=false`（`connect_errors=0`、订阅 2156 token），引擎退回 REST 轮询（`books_max_age_s` 正常、rules 98 正常），**不影响正确性**，只影响反应速度（WS 早拉用于抢窗口）。
- healer 首轮已给确定性根因定位（见 `ops/repair_log_live.md`）。
- 待办：修复 WS 连接（或确认 my155 出口对 WS 端点的可达性）；修复前 live 的窗口抢单能力弱于 paper。

### T-3. 冒烟对账"列表延迟误报"（本轮已修，待生效验证）
- 实测：撤单 confirmed=CANCELED 之后，`open_orders` 列表仍短暂返回该 id → 工具报 `RESIDUAL RISK`。
- 已修：有限重读（默认 3 次 × 5s）+ 区分"确认已撤但列表滞后"与"真残留"。
- 待办：部署后重跑一次冒烟验收，确认不再误报、且真残留仍会报。

### T-4. 真实订单的运维流程（含事故复盘）
- **红线**：任何"真实下单流程"必须在 my155 上以 `setsid nohup ... &` 独立运行，**不得**跑在 vibeshell 会话的前台 —— 2026-09-11 00:4x 我 kill 会话时误杀了正在跑的冒烟进程，导致一个 20 股@0.25 的真实挂单滞留约 1 分钟（已手工撤销，余额未变）。
- **残留挂单的处置命令**（任何时刻可用，只读+撤单）：
  `YES2RE_REPO=/root/weatherbotyes2re /root/live-probe-v2/.venv/bin/python /root/live-probe-v2/cancel.py`
- 待办：把该处置命令写入 `DEPLOY_RUNBOOK.md`（若尚未）；考虑给 live 加一个"挂单超时自动撤"的兜底（当前靠 smoke 流程 + healer 报告）。

### T-5. 资金规模与并发
- 真实余额 51.713622 USDC；预算 12/笔 → 最多 4 个并发 fire；`LIVE_MAX_OPEN_POSITIONS=10` 实际受资金约束。
- 待办：如需放量，入金并同步调整 `LIVE_MAX_CAPITAL_USDC` / `YES2RE_FIRE_BUDGET_USDC`。

---

## 3. paper 侧历史遗留（不阻塞）

| 项 | 现状 | 待办 |
|---|---|---|
| `fired` 集合有无 fire 事件的条目（chengdu/munich 等，02:10–11:03 窗口） | 状态异常但**无对应开仓**（无资金影响） | 清理或标注；排查写入路径 |
| 第 13 笔 taipei 单边无卖盘（fire 零成交却占槽） | 占槽但不亏钱 | 与 D-3 一并决策（是否允许窗口内重试） |
| METAR 中国站（ZGSZ/ZHCC/ZHHH/ZUCK/ZUUU/ZSQD 等）长期 stale（~3h） | CheckWX 源侧延迟（非本栈缺陷），fail-closed 不产生候选 | 观察；如需改用 AWC 直连源，需评估（曾有双源回退阈值待人工项②） |
| 双源回退阈值（CheckWX vs AWC） | healer 待人工项② | 你确认阈值后写入 config |

---

## 4. 环境变量与配置对照（部署时必读）

| 变量 | 作用 | paper 本机 | LIVE my155 |
|---|---|---|---|
| `YES2RE_MODE` | 决定执行端口 | `paper` | `live` |
| `YES2RE_FIRE_BUDGET_USDC` | **引擎侧**单次 fire 预算 | 未设（用 config 20） | `12` |
| `YES2RE_MAX_OPEN_POSITIONS` | **引擎侧**并发仓上限 | 未设（用 config 12） | `10` |
| `YES2RE_INITIAL_CAPITAL_USDC` | 账本初始资金 | 未设（用 config 600） | 待设 `51.713622`（T-1） |
| `LIVE_FIRE_BUDGET_USDC` / `LIVE_MAX_CAPITAL_USDC` / `LIVE_MAX_OPEN_POSITIONS` | **端口侧**硬上限 | 不适用 | `12` / `50` / `10` |
| `LIVE_SUBMIT_ENABLED` | 端口闸门（真实下单） | 不适用 | **未设**（D-1 决定后设 `1`） |
| `POLY_*` / `RELAYER_*` | 实盘凭据（私钥、funder、签名类型、L2 三件套、relayer） | 存在但不用 | 存在且使用 |
| `http(s)_proxy` | 本机 DNS 污染时必须；服务器直连 | 启用 `192.168.1.5:7890` | 注释（直连） |

> 注意：两实例共用同一份 `config/yes2re_reversal.json`（策略参数完全相同）；**任何策略参数改动必须同时作用于两者**。

---

## 5. 安全红线（任何时候不得违反）

1. `.env` 权限 600；`.gitignore` 必须覆盖 `.env*`（已加，`*.bak` 已加）；镜像 `.env` 只含天气 key + 代理，**绝不含私钥**。
2. 私钥永不入聊天/日志/提交；日志与报告只允许掩码。
3. LIVE 端口**最小权限**：仅 `post_order` / `cancel_orders` + 4 个只读方法放出；RFQ 六方法、凭据管理五方法、`post_heartbeat`、`drop_notifications` 保持哨兵拦截（有静态与运行时断言）。
4. LIVE 缺任一闸门 → **拒绝并记账**（`fire_port_refused`），**绝不静默降级为 paper 成交**。
5. healer（paper/live）不得开通闸门、不得下单/撤单、不得 `git push`、不得改策略参数；改动必须先单测全绿并留 diff 给操作者审。
6. 真实订单流程一律 `setsid` 独立运行；会话级 kill 会波及前台子进程（见 T-4 事故）。
