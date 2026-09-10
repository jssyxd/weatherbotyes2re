# live/ — 实盘执行层 (Phase 1 只读对账 · Phase 2 干跑签名)

**Phase 1 纯只读；Phase 2 只在本地签名。** 任何阶段都不存在提交订单的代码路径 ——
Phase 1 只用 `py-clob-client` 的**只读**端点 (`/balance-allowance`、`/data/orders` GET)
和 data-api (`/positions` GET)、ipinfo.io；Phase 2 额外用 `create_order()`
(**签名只在本地做**，网络侧只有 `/book`、`/fee-rate` 等 GET) 加运行时哨兵。
`tests_live.py` 用 AST 静态断言 `live/*.py` 里不存在任何下单/撤单**调用**
(名字只允许作为字符串字面量 / `setattr` 替换目标出现)。

paper 引擎 (`reversal_*.py` / `_r_*.py` / `runner_impl.py`) 未做任何改动，本包与它
完全独立 (不 import 任何 paper 模块)。

## 运行方式

第三方依赖只有一个 `py-clob-client`，且**只存在于独立 venv**
`/home/da/桌面/poly-yes2/live-probe/.venv`（本仓库仍是 stdlib-only，不加 requirements.txt）。

```bash
cd /home/da/桌面/poly-yes2/weatherbotyes2re

# 人类可读摘要 (默认写 data/live_reconcile.json)
/home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py

# 机器可读
/home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py --json
/home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py --out /tmp/lr.json

# 单测 (stdlib，无需 py-clob-client，不联网)
python3.13 tests_live.py
```

退出码：`0` = 采集成功；`2` = 失败关闭 (凭据缺失/网络异常/未装库)。失败时报告仍会写出，
只是 `ok: false` + `reason`。

若用系统 python 跑会得到明确的报错提示，告诉你该用哪个解释器。

## 输出字段表

| 字段 | 类型 | 含义 |
|------|------|------|
| `ok` | bool | 整个采集是否成功 |
| `reason` | str\|null | 失败原因 (`异常类名: 消息`，已做密钥脱敏) |
| `ts_utc` | str | 采集时刻 (UTC, `...Z`) |
| `mode` | str\|null | `.env` 的 `YES2RE_MODE` (当前为 `live`) |
| `api_ok` | bool | CLOB 只读调用 (余额 + 挂单) 是否成功 |
| `usdc_balance` | float | 抵押品余额，由 6 位整数换算（当前抵押品是 **pUSD / Polymarket USD `0xc011a7e1…`**，非 USDC.e；同为 6 位小数，数值正确，字段名沿用旧称，Phase 3 记账前再改名） |
| `allowances` | dict | 合约地址 → 授权额度：`"max"` (≈uint256 上限) / USDC 数值 / `0` |
| `open_orders` | int | 当前挂单数 (只读 GET) |
| `positions` | list | data-api 持仓，仅保留 `currentValue > 0.1`，精简字段见下 |
| `positions_raw_count` | int | 过滤前的原始行数 (区分“确实没有”和“接口异常”) |
| `positions_value_usdc` | float | 保留持仓的 `currentValue` 合计 |
| `risk_gate` | dict | `{"allow", "reason", "detail"}`，用**真实余额** + `.env` 的 `LIVE_*` 评估 |
| `limits` | dict | `LIVE_FIRE_BUDGET_USDC` / `LIVE_MAX_OPEN_POSITIONS` / `LIVE_MAX_CAPITAL_USDC` |
| `egress_ip` / `egress_country` / `egress_org` | str\|null | 出口 IP / 国家 / ASN (ipinfo.io；失败为 `null`，不影响 `ok`) |

`positions[]` 元素字段：`asset, conditionId, outcome, outcomeIndex, size, avgPrice,
curPrice, currentValue, cashPnl, redeemable, negativeRisk, title, slug, eventSlug`。

### risk_gate 判定顺序 (先命中先返回)

| 代码 | 触发条件 |
|------|----------|
| `invalid_input` | 任一入参缺失/非数值/负数/`NaN`，或 `fire_budget_usdc <= 0` → **fail-closed** |
| `max_open_positions_reached` | `open_positions >= max_open_positions` |
| `capital_cap_exceeded` | `committed_usdc + fire_budget_usdc > max_capital_usdc` |
| `insufficient_balance` | `usdc_balance <= 0` |
| `budget_exceeds_balance` | `fire_budget_usdc > usdc_balance` |
| `ok` | 以上全过 → `allow: true` |

## 凭据与密钥

`live/creds.py` 从 `.env` 读取并**只校验格式**：私钥 `0x`+64 hex、funder `0x`+40 hex、
`POLY_SIGNATURE_TYPE ∈ {0,1,2}`、L2 三件套**全有或全无**。报错只提键名，绝不含值；
唯一的值出口是 `mask()` → `前8…后4`。`sanitize()` 会从异常文本里抹掉已知密钥值。

## 网络注意事项

- 本机需走 `.env` 的 `http_proxy`/`https_proxy`（`reconcile` 会注入进程环境，urllib 与 httpx 都会用）。
- **强制 IPv4**：本机无 IPv6 路由，DNS 返回 AAAA 时 `connect()` 直接 `ENETUNREACH`，
  因此 `clob_client.force_ipv4()` 包了 `socket.getaddrinfo` 只返回 `AF_INET`。
- 出口位置 (`egress_ip/country`) 用于确认部署在离 CLOB 近的 VPS。

## Phase 2 — 干跑下单构造 (签名但不提交)

```bash
VENV=/home/da/桌面/poly-yes2/live-probe/.venv/bin/python

# 1) 离线合成盘口干跑 (零网络: 本地 EIP-712 签名)
$VENV live/sign_dryrun.py --scenario --confirm-dryrun

# 2) 真实盘口干跑 (默认从 paper state 的 armed 会话找活跃市场; 全部只读 GET)
$VENV live/sign_dryrun.py --confirm-dryrun --city london --date 2026-09-11 --direction high \
      --budget-usdc 3
$VENV live/sign_dryrun.py --confirm-dryrun --token-id <clob token id> --budget-usdc 3   # 直接指定

# 机器可读 + 自定义落盘
$VENV live/sign_dryrun.py --scenario --confirm-dryrun --json --out /tmp/dryrun.json
```

退出码：`0` = 已签名并落盘；`2` = 失败关闭 (缺 `--confirm-dryrun` / 缺凭据 / 计划被拒 /
网络异常)。产物默认写 `data/live_order_dryrun.json`。

### 三层"不提交"自证

1. **运行时哨兵 (sentinel)** — 构造 client 之后、任何签名之前，`install_sentinels()` 把
   client 可达的写入面 (order / cancel / RFQ / credential-admin / **state-write**) 全部用
   `setattr(target, name, blocked)` 替换成抛 `RuntimeError("SUBMIT BLOCKED (dry-run)")` 的函数：
   - **order / cancel**（7 个，**必须存在，缺一个就拒绝运行**）：
     `create_and_post_order` / `post_order` / `post_orders` / `cancel` / `cancel_orders` /
     `cancel_all` / `cancel_market_orders`；
   - **credential-admin / allowance**：`create_api_key` / `derive_api_key` / `delete_api_key` /
     `create_readonly_api_key` / `delete_readonly_api_key` / `update_balance_allowance`；
   - **state-write**：`post_heartbeat`（POST `/v1/heartbeats`，可撤下全部挂单）、
     `drop_notifications`（DELETE `/notifications`）；
   - **RFQ order entry**（在 `client.rfq` 上）：`create_rfq_request` / `cancel_rfq_request` /
     `create_rfq_quote` / `cancel_rfq_quote` / `accept_rfq_quote` / `approve_rfq_order`。

   实测 `py-clob-client 0.34.6` 上共 **21 个**哨兵。覆盖率由单测保证：枚举
   `dir(ClobClient)` / `dir(RfqClient)` 中所有写类前缀 (`post_/cancel_/delete_/update_/
   create_/drop_/derive_/approve_/accept_/set_`) 的方法，断言其 ⊆ 哨兵集；只读白名单
   (`create_order` / `create_market_order` / `create_or_derive_api_creds` / `set_api_creds`)
   逐条写明豁免理由（本地签名或纯本地状态，审计 §2.3/§8 已实测）。
   该测试在 stdlib 下用 `dir()` 快照跑、在 venv 下用真实反射跑，库升级后会漂移报警。
   随后 `prove_sentinels()` **真的逐个调用一次**并把异常记进产物 `sentinel.proof`
   (每条含 `target`/`method`/`blocked`/`patched`/`error`)——是证据，不是声明。
   哨兵未全部证明被拦下时，`run_dryrun()` 直接拒绝继续。
2. **只签名** — 订单只经 `client.create_order()` (本地 EIP-712 签名；`--scenario` 走
   `py_order_utils` 的等价本地路径)。产物里 `submit.attempted=false`。
3. **产物不可提交** — 原始签名**刻意不落盘**，只留 `signature_present` / `signature_length`
   / `signature_prefix`，因此 `data/live_order_dryrun.json` 无法被任何读到它的人拿去下单。

### 产物字段

| 字段 | 含义 |
|------|------|
| `sentinel` | `installed` (`target.method` 列表) / `missing` (该库版本没有的方法) / `proof` (逐个调用被拦下的证据) / `order_submit_methods` (必须存在的 7 个) |
| `market` | 选中的真实市场 (city/date/direction/bucket/token_id/title/volume) |
| `discovery.attempts` | 市场发现过程 (Gamma 提示 + CLOB 盘口裁决，每次尝试的状态) |
| `book` | CLOB 盘口快照 (`best_ask`=min(asks)、`best_bid`=max(bids)、tick、min_order_size、neg_risk) |
| `plan` | `order_plan.plan_order` 结果 (方向/价格/股数/最大成本/tick/cap/budget) |
| `signed_order` | `hash` (EIP-712 digest) / `maker_amount` / `taker_amount` / `order` (全字段) / 签名"存在性" |
| `submit` | `attempted=false` + 哨兵清单 + 明确声明 |
| `caps` / `caps_source` | 从 `config/yes2re_reversal.json` **只读**取得的 `no_max_ask`/`yes_max_ask`；**缺键/非法即 `CapsError` → `ok:false` + exit 2**（绝不默认成"无上限"） |

### order_plan 的决策码

`ok` / `invalid_input` (缺参/非法/`min_order_size` 缺失) / `no_book` (无对手价) /
`price_above_cap` (ask 超过 cap，含对齐后仍超) / `below_min_order_size` /
`insufficient_budget`，以及 `invalid_input` 的两个边界：`budget_usdc` 超过
`MAX_BUDGET_USDC` (1e12) 或大到无法按 tick 量子量化 (避免 `decimal.InvalidOperation`)。
价格向下对齐到 tick，股数向下取整到 2 位 (可选 6 位)，`max_cost_usdc = size × price ≤ budget`，
绝不向上取整。上限语义与 paper 侧一致 (`no_max_ask`/`yes_max_ask`，只读引用，不写回)；
**上限读不到就拒绝**（`load_caps()` 缺键/非法/越界 → `CapsError` → `ok:false` + exit 2），
不会退化成"无上限"。

## Phase 3 — 冒烟单 (受控的真实提交通道)

**性质变化**：Phase 1/2 的铁律是"写路径不可达"；Phase 3 需要一条**受控、最小**的真实写路径，
于是要求变成：**写路径不可误触发、不可超限、每一步可审计**。整个包内只有 `live/submit.py`
能下单/撤单（AST 单测断言：全包 **恰好一处** `post_order` 调用点、一处 `cancel` 调用点，
其余模块不得出现写调用；`live/smoke.py` 只能经 `submit.submit_order`/`submit.cancel_order`）。

### 操作手册（在 my155 上执行）

```bash
VENV=/home/da/桌面/poly-yes2/live-probe/.venv/bin/python     # 部署机上换成对应 venv
cd /root/weatherbotyes2re

# 0) 探针（不触网、不下单）
$VENV live/submit.py --phrase        # 打印今天的确认短语 SMOKE-YYYY-MM-DD
$VENV live/submit.py --status        # 三条闸门各自是否满足
$VENV live/smoke.py --dry-plan       # 用合成盘口打印计划（零网络、零写路径）
$VENV live/submit.py --open-orders   # 只读：当前挂单
$VENV live/submit.py --audit-summary # 只读：审计日志里到底发生过什么（submit/cancel 计数与 order id）

# 0b) 只读预演：跑完真实链路（找市场→风控→计划→限额→被动性）后在"写"之前停下
#     只读网络、不可能下单、不需要三重闸门
$VENV live/smoke.py --readonly-preflight --city london --date 2026-09-11 --direction high \
      --budget-usdc 5

# 1) 冒烟单（三重闸门全需满足）
export LIVE_SUBMIT_ENABLED=1                      # ② 刻意不写进 .env
$VENV live/smoke.py --enable-submit \
      --confirm "$($VENV live/submit.py --phrase)" # ① + ③
```

预期输出（成功）：`ok=True`、`order: id=… confirmed=live after_cancel=canceled`、
`reconcile: open_orders=0`，产物写 `data/live_smoke.json`，退出码 `0`。
审计日志 `data/live_events.jsonl` 会依次出现
`intent → sentinel_armed → discover → risk → plan → limits → non_marketable → submit → query → cancel → query → reconcile → complete`。

### 三重闸门（为什么是三个）

| # | 闸门 | 防的是什么 |
|---|------|-----------|
| ① | CLI `--enable-submit` | **误调用**：裸跑 `smoke.py`（cron/循环/复制来的只读命令）永远进不了写路径 |
| ② | 环境变量 `LIVE_SUBMIT_ENABLED=1` | **误机器/误会话**：该变量刻意**不放进 `.env`**，任何只是加载 `.env` 的进程（本机排查、测试）都仍是只读；只有显式 export 的那台机器/会话被授权 |
| ③ | `--confirm SMOKE-<UTC日期>` | **陈旧重放**：短语按 UTC 日期生成，昨天的命令行/脚本/历史记录今天必定失败，且强制操作者看一眼今天的短语 |

三者缺一即拒绝，**退出码 3**，并写一条 `gate_deny` 审计记录（拒绝也必须留痕）。
纵深防御：`submit.submit_order()` **自身**要求传入"三闸门全部通过"的记录，否则抛
`PermissionError`（连签名都不会发生）；而 `cancel_order()` 刻意**不设**此要求 ——
撤单是恢复方向，必须永远可用。

### 提交前四道检查（顺序固定，全部通过才提交）

1. `risk_gate.evaluate` — 真实余额 / 持仓数 / 已占用资金 + `LIVE_*` 上限
2. `order_plan.plan_order` — tick 对齐、股数取整、`min_order_size`、价格上限
3. 单笔名义额 ≤ `LIVE_FIRE_BUDGET_USDC` 且 已占用 + 名义额 ≤ `LIVE_MAX_CAPITAL_USDC`
4. **禁止可立即成交**：BUY 价必须 **严格低于** best_ask（SELL 严格高于 best_bid）；
   冒烟单价格 = `min(best_bid, best_ask − 2·tick)`，向下对齐到 tick，且 ≥ 1 tick；
   下单时再叠加 `post_only=True`（交易所侧 maker-only 兜底）

### 最小权限哨兵

先按 Phase 2 装齐全部 21 个哨兵，然后**只解除** `post_order` + `cancel`（写）；
`get_order`/`get_orders`/`get_trades`/`get_balance_allowance` 是只读调用（Phase 2 从未拦截，
此处仅显式记录）。其余（全部 RFQ、凭据/授权管理、`post_heartbeat`、`drop_notifications`）
**保持拦截**，且运行时与单测都断言其仍抛 `RuntimeError`。被解除的写方法只被"恢复/记录"，
**不会被调用**（调用即真实下单）。

### 审计日志

**提交与撤单由 `live/submit.py` 自己审计**（它才是唯一的 `post_order` 调用点）：提交前的
`intent` 记录是**强制**的——写不进去就什么都不签、不提交；提交后的 `submit` 记录为尽力而为
（此时订单可能已在盘上，失败会打 stderr 并置 `audited=false`）。撤单方向相反：日志坏掉也
**不阻塞**撤单（恢复优先），只打 stderr。

每个动作（意图/拒绝/提交/查询/撤单/异常/救援）追加一行 JSON 到 `data/live_events.jsonl`：
`ts_utc` / `action` / `reason` / `params`(已脱敏，键名含 key/secret/pass/priv/signature 一律 `<redacted>`)
/ `response_summary` / `order_id`。日志写不进去 → 抛 `AuditError` 拒绝动作（没有审计就不许动手）。

### 失败时的人工处置

| 现象 | 含义 | 处置 |
|------|------|------|
| `submit_failed:*` + `residual_risk=true` | 连接断在提交中，**可能已挂上** | 立刻 `live/submit.py --open-orders`，看到同 token/价格的挂单就 `--cancel-order <id>`（仍需三重闸门） |
| `cancel_not_confirmed` / `open_orders_remain` | 撤单未确认/仍有挂单 | 同上，必要时到 Polymarket 网页端手动撤 |
| `unexpected_fill:size_matched=…` | 被动单竟然成交了（不该发生） | 视为**异常事件**上报；剩余挂单会被自动撤，成交部分按真实仓位对账 |
| `verify:order_not_confirmed` | 下单后查不到 resting 状态 | 视为可能有残留挂单，按第 1 行处置 |
| 退出码 3 | 三重闸门未满足 | 检查 `--status`，确认 `LIVE_SUBMIT_ENABLED=1` 与今天的短语 |
| 默认候选项报 `best_bid: missing` | 该桶单边/已死（常见于当天已结算的桶） | 显式指定活跃盘口：`--city <city> --date <本地日期> --direction high`（或 `--token-id <id>`） |

其他参数：`--leg buy_yes|buy_no`（默认 `buy_yes`；注意 `--direction` 是**市场方向** high/low，
不是腿方向）、`--price <限价>`（覆盖被动价，若可成交仍会在第 ④ 步被拒）、`--json`、
`--out <path>`（默认 `data/live_smoke.json`）。

任何失败路径都会**尽力撤单**（先按 order_id，再按 token + 价格**数值**匹配挂单列表精确撤），
并明确标注 `residual_risk`。**绝不会盲撤**：既没有 order_id 也没有 token 范围时
`rescue_cancel()` 直接拒绝（`no_scope: refusing a blind cancel scan`）——不列单、更不撤单；
`--readonly-preflight` 也不触发任何救援（那次调用从没下过单）。

### 尚未开启的部分

**真实信号接入尚未开启**：Phase 3 只到"人工触发一次冒烟单"为止。策略信号 → 下单的自动链路
（Phase 4 放量、多城市并发、逐级放大 `LIVE_*`）**没有实现**，`live/submit.py` 也不会被
paper 引擎调用（paper 引擎一行未改）。

## 阶段梯子

| 阶段 | 内容 | 允许的动作 | 当前状态 |
|------|------|-----------|----------|
| **Phase 1** | 只读对账：余额/授权/挂单/持仓/出口/风控预判 | 只有 GET | ✅ 本包实现 |
| **Phase 2** | 干跑签名：真实构建订单并本地签名，**不提交**；哨兵 + 产物不可提交 | 本地签名 + 只读 GET | ✅ 本包实现 |
| **Phase 3** | 冒烟单：5 USDC 非可成交限价单，人工触发 → 查单 → 撤单 → 对账 | 三重闸门 + 最小权限写 | ✅ 本包实现（真实下单由操作者在 my155 触发） |
| **Phase 4** | 放量：多城市并发、逐级放大 `LIVE_*` 上限 | 常态实盘 | 待做 |

升级闸门（每一级都必须满足才进下一级）：Phase 1 连续 N 天 `ok=true` 且余额/挂单与人工
对账一致；Phase 2 干跑签名与 CLOB 校验一致；Phase 3 最小单成交/费用/结算与对账逐值吻合。
