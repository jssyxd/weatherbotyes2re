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

## 阶段梯子

| 阶段 | 内容 | 允许的动作 | 当前状态 |
|------|------|-----------|----------|
| **Phase 1** | 只读对账：余额/授权/挂单/持仓/出口/风控预判 | 只有 GET | ✅ 本包实现 |
| **Phase 2** | 干跑签名：真实构建订单并本地签名，**不提交**；哨兵 + 产物不可提交 | 本地签名 + 只读 GET | ✅ 本包实现 |
| **Phase 3** | 最小单：单城市、单腿、极小 notional，人工确认 + 对账闭环 | 首次真实下单 | 待做 |
| **Phase 4** | 放量：多城市并发、逐级放大 `LIVE_*` 上限 | 常态实盘 | 待做 |

升级闸门（每一级都必须满足才进下一级）：Phase 1 连续 N 天 `ok=true` 且余额/挂单与人工
对账一致；Phase 2 干跑签名与 CLOB 校验一致；Phase 3 最小单成交/费用/结算与对账逐值吻合。
