#!/usr/bin/env python3
"""stdlib-only tests for the live layer — Phase 1 (read-only) + Phase 2 (dry-run).

Run:  python3.13 tests_live.py    → prints PASS/FAIL per check, exit = #failures.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from live import clob_client, creds, order_plan, reconcile, risk_gate, sign_dryrun

ROOT = Path(__file__).resolve().parent

PRIVATE_KEY = "0x" + "ab" * 32
FUNDER = "0x" + "cd" * 20
MAX_UINT = str(2 ** 256 - 1)

VALID_ENV = {
    "POLY_PRIVATE_KEY": PRIVATE_KEY,
    "POLY_FUNDER_ADDRESS": FUNDER,
    "POLY_SIGNATURE_TYPE": "1",
    "POLY_API_KEY": "api-key-opaque",
    "POLY_API_SECRET": "api-secret-opaque",
    "POLY_API_PASSPHRASE": "pass-opaque",
    "YES2RE_MODE": "live",
    "LIVE_FIRE_BUDGET_USDC": "10",
    "LIVE_MAX_OPEN_POSITIONS": "22",
    "LIVE_MAX_CAPITAL_USDC": "500",
}

BALANCE_ALLOWANCE = {
    "balance": "51713622",
    "allowances": {
        "0xExchange1": MAX_UINT,
        "0xExchange2": "0",
        "0xExchange3": "1500000",
    },
}

POSITIONS = [
    {"asset": "1", "conditionId": "0xc1", "outcome": "No", "size": 20.0, "avgPrice": 0.5,
     "curPrice": 0.62, "currentValue": 12.5, "cashPnl": 2.4, "title": "Highest temp NYC",
     "slug": "highest-temp-nyc", "eventSlug": "highest-temp-nyc-2026-09-10", "redeemable": False},
    {"asset": "2", "conditionId": "0xc2", "outcome": "Yes", "size": 1.0, "avgPrice": 0.04,
     "curPrice": 0.05, "currentValue": 0.05, "title": "dust", "slug": "dust", "eventSlug": "dust"},
]


class _Fail(RuntimeError):
    pass


def _patch(**overrides):
    """Swap clob_client I/O for in-memory fakes (reads only)."""
    defs = {
        "build_client": lambda creds_, host=None, chain_id=None: object(),
        "get_balance_allowance": lambda client: dict(BALANCE_ALLOWANCE),
        "get_open_orders": lambda client: [],
        "fetch_positions": lambda address, limit=500, timeout=25: [dict(p) for p in POSITIONS],
        "fetch_egress_info": lambda timeout=15: {"ip": "203.0.113.7", "country": "MY", "org": "AS0 Test"},
    }
    defs.update(overrides)
    originals = {}
    for name, value in defs.items():
        originals[name] = getattr(clob_client, name)
        setattr(clob_client, name, value)
    return originals


def _restore(originals):
    for name, value in originals.items():
        setattr(clob_client, name, value)


# --------------------------------------------------------------------------- risk gate

def test_risk_gate_allow():
    ok = dict(usdc_balance="51.71", open_positions=1, committed_usdc="12.5",
              fire_budget_usdc="10", max_open_positions=22, max_capital_usdc=500)
    got = risk_gate.evaluate(**ok)
    assert got["allow"] is True and got["reason"] == risk_gate.OK, got
    assert set(got) == {"allow", "reason", "detail"}, got


def test_risk_gate_deny_codes():
    base = dict(usdc_balance="100", open_positions=1, committed_usdc="5",
                fire_budget_usdc="10", max_open_positions=22, max_capital_usdc=500)
    cases = [
        ({"usdc_balance": "9.99"}, risk_gate.BUDGET_EXCEEDS_BALANCE),
        ({"usdc_balance": "0"}, risk_gate.INSUFFICIENT_BALANCE),
        ({"open_positions": 22}, risk_gate.MAX_OPEN_POSITIONS),
        ({"open_positions": 30}, risk_gate.MAX_OPEN_POSITIONS),
        ({"committed_usdc": "495"}, risk_gate.CAPITAL_CAP),
        ({"max_capital_usdc": "105", "committed_usdc": "100"}, risk_gate.CAPITAL_CAP),
    ]
    for override, expected in cases:
        got = risk_gate.evaluate(**{**base, **override})
        assert got["allow"] is False and got["reason"] == expected, (override, got)
        assert got["detail"], got


def test_risk_gate_fail_closed():
    base = dict(usdc_balance="100", open_positions=1, committed_usdc="5",
                fire_budget_usdc="10", max_open_positions=22, max_capital_usdc=500)
    bad = [
        {"usdc_balance": None},
        {"open_positions": None},
        {"committed_usdc": "abc"},
        {"fire_budget_usdc": "0"},
        {"fire_budget_usdc": "-1"},
        {"max_open_positions": ""},
        {"max_capital_usdc": "NaN"},
        {"usdc_balance": "Infinity"},
        {"usdc_balance": True},
        {"open_positions": 1.5},
    ]
    for override in bad:
        got = risk_gate.evaluate(**{**base, **override})
        assert got["allow"] is False and got["reason"] == risk_gate.INVALID_INPUT, (override, got)
    # missing limit entirely (env key absent -> None) still denies
    got = risk_gate.evaluate(**{**base, "max_capital_usdc": None})
    assert got["reason"] == risk_gate.INVALID_INPUT, got


# --------------------------------------------------------------------------- creds

def test_creds_validate_ok():
    got = creds.validate_creds(VALID_ENV)
    assert got["signature_type"] == 1 and isinstance(got["signature_type"], int), got
    assert got["funder_address"] == FUNDER and got["private_key"] == PRIVATE_KEY
    assert got["api_key"] == "api-key-opaque"


def test_creds_optional_api_trio():
    env = {k: v for k, v in VALID_ENV.items() if not k.startswith("POLY_API_")}
    assert creds.validate_creds(env)["api_key"] is None
    partial = dict(VALID_ENV)
    partial.pop("POLY_API_SECRET")
    try:
        creds.validate_creds(partial)
    except creds.CredError as exc:
        assert "POLY_API_SECRET" in str(exc) and "api-secret-opaque" not in str(exc), exc
    else:
        raise AssertionError("partial API trio must be rejected")


def test_creds_rejects_bad_formats():
    bad = {
        "POLY_PRIVATE_KEY": "0x" + "ab" * 31,          # 62 hex
        "POLY_FUNDER_ADDRESS": "0x" + "cd" * 21,       # 42 hex
        "POLY_SIGNATURE_TYPE": "3",
    }
    for key, value in bad.items():
        try:
            creds.validate_creds({**VALID_ENV, key: value})
        except creds.CredError as exc:
            assert key in str(exc), exc
            assert value not in str(exc), "error must not echo the value"
        else:
            raise AssertionError(f"{key}={value!r} must be rejected")
    for key in ("POLY_PRIVATE_KEY", "POLY_FUNDER_ADDRESS", "POLY_SIGNATURE_TYPE"):
        try:
            creds.validate_creds({k: v for k, v in VALID_ENV.items() if k != key})
        except creds.CredError as exc:
            assert key in str(exc), exc
        else:
            raise AssertionError(f"missing {key} must be rejected")


def test_creds_env_file_and_mask():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        path.write_text(
            "# comment\n"
            "export POLY_PRIVATE_KEY=%s\n"
            "POLY_FUNDER_ADDRESS=%s\n"
            '\nPOLY_SIGNATURE_TYPE="2"\n' % (PRIVATE_KEY, FUNDER),
            encoding="utf-8",
        )
        env = creds.load_env_file(path)
        assert env["POLY_SIGNATURE_TYPE"] == "2" and env["POLY_PRIVATE_KEY"] == PRIVATE_KEY
        loaded = creds.validate_creds(env)
        assert loaded["signature_type"] == 2
    masked = creds.mask(PRIVATE_KEY)
    assert masked == PRIVATE_KEY[:8] + "…" + PRIVATE_KEY[-4:], masked
    assert PRIVATE_KEY not in masked and len(masked) < len(PRIVATE_KEY)
    assert creds.mask(None) == "***" and creds.mask("short") == "***"
    assert "api-secret-opaque" not in creds.sanitize(
        f"boom api-secret-opaque", VALID_ENV)


# --------------------------------------------------------------------------- reconcile

def test_reconcile_structure():
    originals = _patch()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            report = reconcile.collect(
                {**VALID_ENV, "http_proxy": "http://127.0.0.1:9"}, timeout=5
            )
    finally:
        _restore(originals)
    expected = {"ok", "reason", "ts_utc", "mode", "api_ok", "usdc_balance", "allowances",
                "open_orders", "positions", "positions_raw_count", "positions_value_usdc",
                "risk_gate", "limits",
                "egress_ip", "egress_country", "egress_org"}
    assert expected <= set(report), sorted(expected - set(report))
    assert report["ok"] is True and report["reason"] is None
    assert report["api_ok"] is True and report["mode"] == "live"
    assert report["usdc_balance"] == 51.713622, report["usdc_balance"]
    assert report["allowances"] == {"0xExchange1": "max", "0xExchange2": 0, "0xExchange3": 1.5}
    assert report["open_orders"] == 0
    assert len(report["positions"]) == 1, "currentValue <= 0.1 must be filtered out"
    assert report["positions_raw_count"] == 2, report["positions_raw_count"]
    assert report["positions"][0]["asset"] == "1"
    assert report["positions_value_usdc"] == 12.5
    assert report["risk_gate"]["allow"] is True, report["risk_gate"]
    assert report["egress_ip"] == "203.0.113.7" and report["egress_country"] == "MY"
    assert PRIVATE_KEY not in json.dumps(report) and FUNDER not in json.dumps(report)


def test_reconcile_gate_denies_on_real_balance():
    originals = _patch(get_balance_allowance=lambda client: {"balance": "5000000", "allowances": {}})
    try:
        report = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    assert report["usdc_balance"] == 5.0
    assert report["risk_gate"]["allow"] is False
    assert report["risk_gate"]["reason"] == risk_gate.BUDGET_EXCEEDS_BALANCE


def test_reconcile_non_mapping_env():
    """collect() must honour "Never raises": a bad env returns the fail-closed shape."""
    originals = _patch()
    try:
        reference = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    for bad in ([], "env", 1, 1.5, object(), ["a"], {"nested": 1}.keys()):
        try:
            got = reconcile.collect(bad)
        except Exception as exc:  # noqa: BLE001 - not raising is the whole point
            raise AssertionError(f"collect({bad!r}) raised {type(exc).__name__}: {exc}") from None
        assert got["ok"] is False, (bad, got)
        assert got["reason"] == "env: not a mapping", (bad, got["reason"])
        assert set(got) == set(reference), sorted(set(reference) ^ set(got))
        assert got["ts_utc"].endswith("Z") and got["limits"], got


def test_reconcile_non_string_values():
    """dict-shaped but ill-typed env values must not raise either (F2)."""
    originals = _patch()
    try:
        reference = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    cases = [
        {"YES2RE_MODE": 1},                                   # was: AttributeError .strip()
        {"POLY_PRIVATE_KEY": 12345678},                        # was: TypeError len(int)
        {"POLY_API_KEY": 1, "LIVE_MAX_CAPITAL_USDC": 50},      # was: TypeError len(int)
        {"LIVE_FIRE_BUDGET_USDC": 5, "POLY_SIGNATURE_TYPE": 1, "POLY_FUNDER_ADDRESS": 42},
        {"POLY_PRIVATE_KEY": None, "POLY_FUNDER_ADDRESS": None, "POLY_SIGNATURE_TYPE": None},
    ]
    for env in cases:
        try:
            got = reconcile.collect(env)
        except Exception as exc:  # noqa: BLE001 - not raising is the whole point
            raise AssertionError(f"collect({env!r}) raised {type(exc).__name__}: {exc}") from None
        assert got["ok"] is False, (env, got)
        assert set(got) == set(reference), sorted(set(reference) ^ set(got))
        assert isinstance(got["reason"], str) and got["reason"], (env, got["reason"])
    assert reconcile.collect({"YES2RE_MODE": 1})["mode"] == "1", "non-str mode is stringified"
    assert "12345678" not in reconcile.collect({"POLY_PRIVATE_KEY": 12345678})["reason"], \
        "non-string value must not be echoed into the report"


def test_reconcile_fail_closed():
    # missing creds
    report = reconcile.collect({})
    assert report["ok"] is False and "POLY_PRIVATE_KEY" in report["reason"], report["reason"]

    # network/library failure + partial snapshot kept
    originals = _patch(build_client=lambda *a, **k: (_ for _ in ()).throw(_Fail("client exploded")))
    try:
        report = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    assert report["ok"] is False and report["api_ok"] is False
    assert "_Fail: client exploded" in report["reason"], report["reason"]

    # never leaks a secret inside the error text
    originals = _patch(build_client=lambda *a, **k: (_ for _ in ()).throw(
        _Fail(f"bad key {PRIVATE_KEY}")))
    try:
        report = reconcile.collect(VALID_ENV)
    finally:
        _restore(originals)
    assert PRIVATE_KEY not in report["reason"], report["reason"]


def test_reconcile_cli_json_and_exit_codes():
    originals = _patch()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "live_reconcile.json"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = reconcile.main(["--json", "--out", str(out)])
            assert code == 0, code
            printed = json.loads(buf.getvalue())
            on_disk = json.loads(out.read_text(encoding="utf-8"))
            assert printed["ok"] and on_disk["ok"]
            assert printed["ts_utc"].endswith("Z")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = reconcile.main(["--out", str(out)])
            assert code == 0 and "risk_gate allow=True" in buf.getvalue(), buf.getvalue()
            assert PRIVATE_KEY not in buf.getvalue()

        originals2 = _patch(build_client=lambda *a, **k: (_ for _ in ()).throw(_Fail("down")))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = reconcile.main(["--out", "/tmp/does-not-matter-live.json"])
            assert code == 2 and "reason=" in buf.getvalue(), buf.getvalue()
        finally:
            _restore(originals2)
    finally:
        _restore(originals)
    Path("/tmp/does-not-matter-live.json").unlink(missing_ok=True)


# --------------------------------------------------------------------------- read-only guard

#: names that must never appear as a *real call* anywhere in live/
FORBIDDEN_CALLS = (
    "create_and_post_order", "post_order", "post_orders", "create_market_order",
    "cancel", "cancel_orders", "cancel_all", "cancel_market_orders",
    "create_api_key", "derive_api_key", "delete_api_key", "update_balance_allowance",
    "post_heartbeat", "delete_readonly_api_key", "submit",
)
#: Phase 2 may sign — and only sign — through this call
SIGN_ONLY_CALLS = ("create_order",)


def test_static_no_order_path():
    """Hard read-only guarantee, AST-based.

    Phase 2 legitimately mentions ``post_order`` and friends as string literals and as
    ``setattr(client, name, sentinel)`` *replacements* — data, never a call. So the
    assertion is on the AST: no call may use a forbidden name, forbidden names may not
    appear as real attribute accesses (``client.post_order``), and no module may invoke
    a computed function (``f()()``), which would be a hidable write path.
    """
    files = sorted((ROOT / "live").glob("*.py"))
    assert files, "no live/ modules found"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                else:
                    assert not isinstance(func, ast.Call), (
                        f"{path.name}:{node.lineno} calls a computed function (f()()) — hidable write path"
                    )
                    name = None
                assert name not in FORBIDDEN_CALLS, f"{path.name}:{node.lineno} calls {name}()"
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_CALLS, (
                    f"{path.name}:{node.lineno} accesses .{node.attr} as code "
                    "(allowed only as a string literal / setattr target)"
                )
    signers = {
        path.name for path in files
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in SIGN_ONLY_CALLS
               for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert signers == {"sign_dryrun.py"}, f"unexpected signing modules: {signers}"



# --------------------------------------------------------------------------- Phase 2: order_plan

PLAN_BOOK = {"best_ask": "0.523", "best_bid": "0.517", "tick_size": "0.01", "min_order_size": "5"}


def test_order_plan_tick_alignment_and_rounding():
    plan = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                 budget_usdc="3", price_cap="0.9")
    assert plan["ok"] and plan["reason"] == order_plan.OK, plan
    assert plan["side"] == "BUY" and plan["price"] == Decimal("0.52"), plan
    assert plan["tick"] == Decimal("0.01"), plan
    assert plan["size"] == Decimal("5.76"), plan            # 3 / 0.52, rounded down at 2dp
    assert plan["max_cost_usdc"] <= Decimal("3"), plan
    assert set(plan) >= {"ok", "reason", "price", "size", "max_cost_usdc", "tick"}

    fine = order_plan.plan_order(direction="buy_yes", token_id="T",
                                 book={**PLAN_BOOK, "tick_size": "0.001"},
                                 budget_usdc="3", price_cap="0.9")
    assert fine["price"] == Decimal("0.523"), fine          # 1-tick market: nothing to round
    assert fine["tick"] == Decimal("0.001"), fine

    coarse = order_plan.plan_order(direction="buy_yes", token_id="T",
                                   book={**PLAN_BOOK, "tick_size": "0.1"},
                                   budget_usdc="3", price_cap="0.9")
    assert coarse["price"] == Decimal("0.5"), coarse        # aligned DOWN to the tick

    six = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                budget_usdc="3", price_cap="0.9", size_decimals=6)
    assert six["size"] == Decimal("5.769230"), six          # 3 / 0.52 rounded down at 6dp
    assert order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                 budget_usdc="3", price_cap="0.9",
                                 size_decimals=9)["reason"] == order_plan.INVALID_INPUT

    override = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                     budget_usdc="3", price_cap="0.9",
                                     tick_size="0.1", min_order_size="1")
    assert override["price"] == Decimal("0.5") and override["size"] == Decimal("6.00"), override


def test_order_plan_deny_branches():
    base = dict(direction="buy_yes", token_id="T", book=PLAN_BOOK, budget_usdc="3", price_cap="0.9")
    no_min_book = {key: value for key, value in PLAN_BOOK.items() if key != "min_order_size"}
    cases = [
        (order_plan.PRICE_ABOVE_CAP, {**base, "book": {**PLAN_BOOK, "best_ask": "0.95"}}),
        (order_plan.PRICE_ABOVE_CAP, {**base, "price_cap": "0.5"}),
        (order_plan.BELOW_MIN_ORDER_SIZE, {**base, "budget_usdc": "1"}),
        (order_plan.INSUFFICIENT_BUDGET, {**base, "budget_usdc": "0.002"}),
        (order_plan.NO_BOOK, {**base, "book": {}}),
        (order_plan.NO_BOOK, {**base, "book": {**PLAN_BOOK, "best_ask": None, "asks": []}}),
        (order_plan.INVALID_INPUT, {**base, "direction": "buy"}),
        (order_plan.INVALID_INPUT, {**base, "token_id": None}),
        (order_plan.INVALID_INPUT, {**base, "token_id": "   "}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "0"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "-1"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "NaN"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": None}),
        (order_plan.INVALID_INPUT, {**base, "price_cap": "abc"}),
        (order_plan.INVALID_INPUT, {**base, "price_cap": "1.5"}),
        (order_plan.INVALID_INPUT, {**base, "min_order_size": None, "book": no_min_book}),
        (order_plan.INVALID_INPUT, {**base, "book": {**PLAN_BOOK, "tick_size": "0"}}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "1e24"}),     # F3: huge but finite
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "1e400"}),
        (order_plan.INVALID_INPUT, {**base, "budget_usdc": "1e13"}),
    ]
    for expected, kwargs in cases:
        got = order_plan.plan_order(**kwargs)
        assert got["ok"] is False and got["reason"] == expected, (kwargs, got)
        assert got["price"] is None and got["size"] is None and got["max_cost_usdc"] is None, got
        assert got["detail"], got


def test_order_plan_sell_and_caps():
    sell = order_plan.plan_order(direction="sell", token_id="T", book=PLAN_BOOK,
                                 budget_usdc="3", price_cap=None)
    assert sell["ok"] and sell["side"] == "SELL" and sell["price"] == Decimal("0.51"), sell

    caps = order_plan.load_caps()
    assert caps["no_max_ask"] == Decimal("1.0") and caps["yes_max_ask"] == Decimal("0.9"), caps
    assert order_plan.cap_for("buy_no", caps) == Decimal("1.0")
    assert order_plan.cap_for("buy_yes", caps) == Decimal("0.9")
    assert order_plan.cap_for("sell", caps) is None
    assert order_plan.cap_for("buy_yes", caps, "0.48") == Decimal("0.48")
    assert order_plan.cap_for("buy_yes", caps, "nonsense") is None
    assert order_plan.cap_for("buy_yes", caps, "nonsense") is None

    # F3: a huge-but-finite budget must fail closed, never raise
    for budget in ("1e24", "1e400"):
        try:
            got = order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                        budget_usdc=budget, price_cap="0.9")
        except Exception as exc:  # noqa: BLE001 - raising is the bug being fixed
            raise AssertionError(f"budget {budget} raised {type(exc).__name__}: {exc}") from None
        assert got["ok"] is False and got["reason"] == order_plan.INVALID_INPUT, (budget, got)
    assert order_plan.plan_order(direction="buy_yes", token_id="T", book=PLAN_BOOK,
                                 budget_usdc=str(order_plan.MAX_BUDGET_USDC),
                                 price_cap="0.9")["ok"] is True


def test_load_caps_fails_closed():
    """F5: a cap that cannot be read must never degrade into "no limit"."""
    assert issubclass(order_plan.CapsError, ValueError)
    assert order_plan.CAP_KEYS == ("no_max_ask", "yes_max_ask")
    variants = [
        ('{"strategy":{"yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"1.0"}}', "yes_max_ask"),
        ('{"strategy":{"no_max_ask":null,"yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"abc","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"NaN","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"1.5","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"0","yes_max_ask":"0.9"}}', "no_max_ask"),
        ('{"strategy":{"no_max_ask":"1.0","yes_max_ask":"-1"}}', "yes_max_ask"),
        ("{}", "no_max_ask"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for body, key in variants:
            path = Path(tmp) / "cfg.json"
            path.write_text(body, encoding="utf-8")
            try:
                order_plan.load_caps(path)
            except order_plan.CapsError as exc:
                assert key in str(exc), (body, exc)
            else:
                raise AssertionError(f"{body} must fail closed (missing/invalid {key})")
        good = Path(tmp) / "good.json"
        good.write_text('{"strategy":{"no_max_ask":"0.85","yes_max_ask":"0.48"}}', encoding="utf-8")
        assert order_plan.load_caps(good) == {"no_max_ask": Decimal("0.85"),
                                             "yes_max_ask": Decimal("0.48")}


def test_dryrun_fails_closed_when_caps_unreadable():
    """F5 end-to-end: an unreadable cap ⇒ ok:false (run_dryrun calls load_caps inside its try)."""
    def _boom(*_a, **_k):
        raise order_plan.CapsError("strategy.no_max_ask missing from config")


# --------------------------------------------------------------------------- Phase 2: sentinels

SUBMIT_METHOD_NAMES = (
    "create_and_post_order", "post_order", "post_orders", "cancel",
    "cancel_orders", "cancel_all", "cancel_market_orders",
)
RFQ_METHOD_NAMES = ("create_rfq_request", "cancel_rfq_request", "create_rfq_quote",
                    "cancel_rfq_quote", "accept_rfq_quote", "approve_rfq_order")
STATE_WRITE_METHOD_NAMES = ("post_heartbeat", "drop_notifications")


class _FakeOrder:
    def dict(self):
        return {"salt": 1, "maker": "0xmaker", "signer": "0xsigner", "taker": "0x0",
                "tokenId": "9" * 20, "makerAmount": "2995200", "takerAmount": "5760000",
                "expiration": 0, "nonce": 0, "feeRateBps": 0, "side": "BUY", "signatureType": 1}


class _FakeSigned:
    def __init__(self, signature="0x" + "ab" * 65):
        self.order = _FakeOrder()
        self.signature = signature


class _FakeRfq:
    """Stands in for the client's RFQ sub-client (a second order-entry surface)."""


class _FakeClient:
    """Stands in for ClobClient — submit-class methods return a marker until the sentinels arm."""

    def __init__(self):
        self.rfq = _FakeRfq()

    def create_order(self, *args, **kwargs):
        return _FakeSigned()


_MARKER = (lambda name: lambda self, *a, **k: {"called": name})
for _name in SUBMIT_METHOD_NAMES + STATE_WRITE_METHOD_NAMES + RFQ_METHOD_NAMES:
    setattr(_FakeRfq if _name in RFQ_METHOD_NAMES else _FakeClient, _name, _MARKER(_name))


def _patch_dryrun(**overrides):
    defs = {
        "_build_client": lambda creds_: _FakeClient(),
        "_sign": lambda client, plan, creds_, offline=False: _FakeSigned(),
        "_order_hash": lambda signed, creds_, plan: "0x" + "de" * 32,
    }
    defs.update(overrides)
    originals = {name: getattr(sign_dryrun, name) for name in defs}
    for name, value in defs.items():
        setattr(sign_dryrun, name, value)
    return originals


def _restore_dryrun(originals):
    for name, value in originals.items():
        setattr(sign_dryrun, name, value)


def test_sentinels_are_load_bearing():
    client = _FakeClient()
    # before arming these would happily "submit" — that is what makes the arm load-bearing
    assert client.post_order() == {"called": "post_order"}
    assert client.rfq.create_rfq_quote() == {"called": "create_rfq_quote"}
    before = {name: getattr(client, name) for name in SUBMIT_METHOD_NAMES}
    before_rfq = {name: getattr(client.rfq, name) for name in RFQ_METHOD_NAMES}

    armed = sign_dryrun.install_sentinels(client)
    assert armed["armed"] is True, armed
    for name in SUBMIT_METHOD_NAMES:
        assert f"client.{name}" in armed["installed"], armed
    for name in RFQ_METHOD_NAMES:
        assert f"client.rfq.{name}" in armed["installed"], armed
    for name in STATE_WRITE_METHOD_NAMES:      # F4: heartbeat / notification state writes
        assert f"client.{name}" in armed["installed"], armed
    assert len(armed["proof"]) == len(armed["installed"]), armed["proof"]
    for proof in armed["proof"]:
        assert proof["blocked"] is True, proof
        assert proof["patched"] is True, proof
        assert proof["target"] in ("client", "client.rfq"), proof
        assert "SUBMIT BLOCKED" in proof["error"], proof

    for name in SUBMIT_METHOD_NAMES:
        method = getattr(client, name)
        assert method is not before[name], f"{name} was not replaced"
        assert getattr(method, "dryrun_sentinel_for", None) == name, name
        try:
            method()
        except RuntimeError as exc:
            assert "SUBMIT BLOCKED" in str(exc), exc
        else:
            raise AssertionError(f"{name}() did not raise — sentinel is not load-bearing")

    for name in RFQ_METHOD_NAMES:  # the second order-entry surface is closed too
        method = getattr(client.rfq, name)
        assert method is not before_rfq[name], f"client.rfq.{name} was not replaced"
        try:
            method()
        except RuntimeError as exc:
            assert "SUBMIT BLOCKED" in str(exc), exc
        else:
            raise AssertionError(f"client.rfq.{name}() did not raise — sentinel is not load-bearing")


def test_sentinel_refuses_when_methods_absent():
    """If a library version drops an order-submit method, the dry run must refuse to run."""
    class _BareNoCancel:
        rfq = None
        post_order = _MARKER("post_order")

    class _Bare:
        rfq = None

    originals = _patch_dryrun(_build_client=lambda creds_: _BareNoCancel())
    try:
        report = sign_dryrun.run_dryrun(scenario=True, confirm=True, env=VALID_ENV)
    finally:
        _restore_dryrun(originals)
    assert report["ok"] is False, report
    assert "order-submit sentinels missing" in report["reason"], report["reason"]
    assert "client.cancel" in report["reason"], report["reason"]

    bare = sign_dryrun.install_sentinels(_Bare())
    assert bare["armed"] is False and bare["installed"] == [], bare
    assert len(bare["missing"]) == (len(sign_dryrun.SUBMIT_METHODS)
                                    + len(sign_dryrun.ADMIN_METHODS)
                                    + len(sign_dryrun.STATE_WRITE_METHODS)), bare
    assert len(bare["missing"]) == (len(sign_dryrun.SUBMIT_METHODS)
                                    + len(sign_dryrun.ADMIN_METHODS)
                                    + len(sign_dryrun.STATE_WRITE_METHODS)), bare


#: every name matching these prefixes must have a sentinel (F4 coverage)
WRITE_PREFIXES = ("post_", "cancel_", "delete_", "update_", "create_", "drop_",
                  "derive_", "approve_", "accept_", "set_")

#: exemptions, each with the reason it is safe (verified by the Phase-2 audit, §2.3 / §8)
READ_ONLY_WHITELIST = {
    "create_order": "Phase 2's only write call — local EIP-712 signing; audited traffic: GET only",
    "create_market_order": "same local signing path as create_order; audited: 1 GET / 0 non-GET",
    "create_or_derive_api_creds": "composed call; delegates to create_api_key/derive_api_key, both sentineled",
    "set_api_creds": "local-only: rewrites self.creds/self.mode, zero network",
}

#: dir(ClobClient) / dir(RfqClient) snapshot (py-clob-client 0.34.6) so the coverage
#: test also runs under a stdlib interpreter; refreshed when the library is upgraded.
CLOB_CLIENT_METHODS = (
    "are_orders_scoring", "assert_builder_auth", "assert_level_1_auth", "assert_level_2_auth",
    "calculate_market_price", "can_builder_auth", "cancel", "cancel_all", "cancel_market_orders",
    "cancel_orders", "clear_tick_size_cache", "create_and_post_order", "create_api_key",
    "create_market_order", "create_or_derive_api_creds", "create_order", "create_readonly_api_key",
    "delete_api_key", "delete_readonly_api_key", "derive_api_key", "drop_notifications",
    "get_address", "get_api_keys", "get_balance_allowance", "get_builder_trades",
    "get_closed_only_mode", "get_collateral_address", "get_conditional_address",
    "get_exchange_address", "get_fee_rate_bps", "get_last_trade_price", "get_last_trades_prices",
    "get_market", "get_market_trades_events", "get_markets", "get_midpoint", "get_midpoints",
    "get_neg_risk", "get_notifications", "get_ok", "get_order", "get_order_book",
    "get_order_book_hash", "get_order_books", "get_orders", "get_price", "get_prices",
    "get_readonly_api_keys", "get_sampling_markets", "get_sampling_simplified_markets",
    "get_server_time", "get_simplified_markets", "get_spread", "get_spreads", "get_tick_size",
    "get_trades", "is_order_scoring", "post_heartbeat", "post_order", "post_orders",
    "set_api_creds", "update_balance_allowance", "validate_readonly_api_key",
)
RFQ_CLIENT_METHODS = (
    "accept_rfq_quote", "approve_rfq_order", "cancel_rfq_quote", "cancel_rfq_request",
    "create_rfq_quote", "create_rfq_request", "get_rfq_best_quote", "get_rfq_quoter_quotes",
    "get_rfq_requester_quotes", "get_rfq_requests", "rfq_config",
)


def _surface_names(kind):
    """Live reflection when py-clob-client is importable, else the recorded snapshot."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.rfq.rfq_client import RfqClient
    except ImportError:
        names = CLOB_CLIENT_METHODS if kind == "ClobClient" else RFQ_CLIENT_METHODS
        return tuple(names), "recorded snapshot"
    cls = ClobClient if kind == "ClobClient" else RfqClient
    return tuple(sorted(name for name in dir(cls) if not name.startswith("_"))), "live reflection"


def _is_write(name):
    return any(name.startswith(prefix) for prefix in WRITE_PREFIXES)


def _surface(names):
    obj = type("Surface", (), {})()
    for name in names:
        setattr(obj, name, _MARKER(name))
    return obj


def test_sentinel_coverage_over_client_surface():
    """F4: every write-looking client method (and RFQ method) must end up sentineled."""
    client_names, source = _surface_names("ClobClient")
    rfq_names, _ = _surface_names("RfqClient")
    for name, reason in READ_ONLY_WHITELIST.items():
        assert name in client_names, f"stale whitelist entry {name!r} ({reason})"

    client = _surface(client_names)
    client.rfq = _surface(rfq_names)
    armed = sign_dryrun.install_sentinels(client)
    installed = set(armed["installed"])
    assert armed["missing"] == [], armed["missing"]
    assert all(p["blocked"] for p in armed["proof"]), armed["proof"]

    expected = {f"client.{name}" for name in client_names
                if _is_write(name) and name not in READ_ONLY_WHITELIST}
    expected |= {f"client.rfq.{name}" for name in rfq_names
                 if _is_write(name) and name not in READ_ONLY_WHITELIST}
    uncovered = expected - installed
    assert not uncovered, f"[{source}] write methods without a sentinel: {sorted(uncovered)}"
    for name in STATE_WRITE_METHOD_NAMES:      # the two F4 additions, explicitly
        assert name in {entry.split(".", 1)[1] for entry in installed}, (name, installed)
    print(f"    ({source}: {len(expected)} write methods covered, "
          f"{len(READ_ONLY_WHITELIST)} whitelisted with reasons)")


def test_scenario_dryrun_offline():
    originals = _patch_dryrun()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "live_order_dryrun.json"
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = sign_dryrun.main(["--scenario", "--confirm-dryrun", "--json", "--out", str(out)])
            assert code == 0, (code, captured.getvalue())
            report = json.loads(out.read_text(encoding="utf-8"))
    finally:
        _restore_dryrun(originals)

    assert report["ok"] is True and report["scenario"] is True, report["reason"]
    assert report["phase"] == "phase2-dryrun"
    assert report["sentinel"]["armed"] is True
    assert report["sentinel"]["proof"] and all(p["blocked"] for p in report["sentinel"]["proof"])
    assert report["plan"]["ok"] is True, report["plan"]
    assert report["plan"]["token_id"] == sign_dryrun.SCENARIO["market"]["token_id"]
    assert report["submit"]["attempted"] is False
    assert "NO ORDER SUBMITTED" in report["submit"]["statement"]
    assert report["signed_order"]["hash"] == "0x" + "de" * 32
    assert report["signed_order"]["signature_present"] is True
    assert report["signed_order"]["maker_amount"] == "2995200"
    assert "signature" not in report["signed_order"], "the raw signature must not be persisted"
    blob = json.dumps(report, ensure_ascii=False)
    assert PRIVATE_KEY not in blob and FUNDER not in blob and "api-secret-opaque" not in blob


def test_dryrun_fail_closed():
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = sign_dryrun.main(["--scenario"])          # no --confirm-dryrun
    assert code == 2 and "confirm_flag_missing" in captured.getvalue(), captured.getvalue()

    report = sign_dryrun.run_dryrun(scenario=True, confirm=True, env={})   # no creds
    assert report["ok"] is False and "POLY_PRIVATE_KEY" in report["reason"], report["reason"]

    originals = _patch_dryrun()
    try:
        report = sign_dryrun.run_dryrun(scenario=True, confirm=True, env=VALID_ENV,
                                        budget_usdc="0.002")
    finally:
        _restore_dryrun(originals)
    assert report["ok"] is False and report["reason"] == "order_plan:insufficient_budget", report["reason"]
    assert report["signed_order"] is None, "nothing may be signed when the plan denies"
    assert all(p["blocked"] for p in report["sentinel"]["proof"]), "proof missing on the deny path"
    assert report["submit"]["attempted"] is False


CHECKS = [
    ("risk_gate: allow path", test_risk_gate_allow),
    ("risk_gate: all deny codes", test_risk_gate_deny_codes),
    ("risk_gate: fail-closed inputs", test_risk_gate_fail_closed),
    ("creds: valid set", test_creds_validate_ok),
    ("creds: optional/partial API trio", test_creds_optional_api_trio),
    ("creds: bad formats rejected", test_creds_rejects_bad_formats),
    ("creds: .env parsing + masking", test_creds_env_file_and_mask),
    ("reconcile: output structure", test_reconcile_structure),
    ("reconcile: gate uses real balance", test_reconcile_gate_denies_on_real_balance),
    ("reconcile: fail-closed + secret-free", test_reconcile_fail_closed),
    ("reconcile: non-mapping env never raises", test_reconcile_non_mapping_env),
    ("reconcile: non-string env values never raise", test_reconcile_non_string_values),
    ("reconcile: CLI json/exit codes", test_reconcile_cli_json_and_exit_codes),
    ("live/: no order/cancel path (static)", test_static_no_order_path),
    ("order_plan: tick alignment + rounding", test_order_plan_tick_alignment_and_rounding),
    ("order_plan: deny branches", test_order_plan_deny_branches),
    ("order_plan: sell path + config caps", test_order_plan_sell_and_caps),
    ("dry-run: sentinels are load-bearing", test_sentinels_are_load_bearing),
    ("dry-run: refuses when sentinels cannot arm", test_sentinel_refuses_when_methods_absent),
    ("dry-run: sentinel coverage over client surface", test_sentinel_coverage_over_client_surface),
    ("order_plan: load_caps fails closed", test_load_caps_fails_closed),
    ("dry-run: fails closed when caps unreadable", test_dryrun_fails_closed_when_caps_unreadable),
    ("dry-run: --scenario offline artifact", test_scenario_dryrun_offline),
    ("dry-run: fail-closed paths", test_dryrun_fail_closed),
]


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test runner reports everything
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
