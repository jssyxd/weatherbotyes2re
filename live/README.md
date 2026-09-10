# live/ — 实盘执行层 (Phase 1: 只读对账)

**Phase 1 是纯只读的。** 这个包里没有任何下单/签名/撤单代码路径 —— 只有
`py-clob-client` 的**只读**端点 (`/balance-allowance`、`/data/orders` GET) 和
Polymarket data-api (`/positions` GET)、ipinfo.io。`tests_live.py` 会用静态扫描
断言 `live/*.py` 中不出现任何下单/撤单调用名 (CI 意义上的硬保证)。

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

## 阶段梯子

| 阶段 | 内容 | 允许的动作 | 当前状态 |
|------|------|-----------|----------|
| **Phase 1** | 只读对账：余额/授权/挂单/持仓/出口/风控预判 | 只有 GET | ✅ 本包实现 |
| **Phase 2** | 干跑签名：真实构建订单并本地签名，**不提交**，落盘比对期望单 | 签名 (无网络写) | 待做 |
| **Phase 3** | 最小单：单城市、单腿、极小 notional，人工确认 + 对账闭环 | 首次真实下单 | 待做 |
| **Phase 4** | 放量：多城市并发、逐级放大 `LIVE_*` 上限 | 常态实盘 | 待做 |

升级闸门（每一级都必须满足才进下一级）：Phase 1 连续 N 天 `ok=true` 且余额/挂单与人工
对账一致；Phase 2 干跑签名与 CLOB 校验一致；Phase 3 最小单成交/费用/结算与对账逐值吻合。
