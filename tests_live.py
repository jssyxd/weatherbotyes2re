#!/usr/bin/env python3
"""stdlib-only tests for the live Phase-1 layer (no py-clob-client, no network).

Run:  python3.13 tests_live.py    → prints PASS/FAIL per check, exit = #failures.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path

from live import clob_client, creds, reconcile, risk_gate

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

FORBIDDEN = (
    "post_order", "post_orders", "create_order", "create_market_order",
    "create_and_post_order", "cancel", "submit", "sign_order",
    "create_api_key", "derive_api_key", "delete_api_key", "update_balance_allowance",
    "post_heartbeat", "post_orders_args", "delete_readonly_api_key",
)


def test_static_no_order_path():
    """Hard read-only guarantee: no order/cancel call name anywhere in live/."""
    files = sorted((ROOT / "live").glob("*.py"))
    assert files, "no live/ modules found"
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for name in FORBIDDEN:
            assert name not in text, f"{path.name} mentions {name!r} — Phase 1 must stay read-only"
    this = Path(__file__).resolve()
    assert this.name == "tests_live.py"


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
