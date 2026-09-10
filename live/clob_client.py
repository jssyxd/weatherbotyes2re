#!/usr/bin/env python3
"""Lazy, read-only wrappers: py-clob-client (balance/allowance, open orders) + data-api.

Read-only by construction — the only CLOB endpoints touched are
``/balance-allowance`` and ``/data/orders`` (GET). No order-placing or
order-teardown path exists anywhere in ``live/``; ``tests_live.py`` asserts
that statically.

Third-party ``py-clob-client`` is imported lazily so this module stays importable
(and testable) under a plain stdlib interpreter.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
DATA_API = "https://data-api.polymarket.com"
USER_AGENT = "weatherbotyes2re-live/1.0 (read-only reconcile)"
#: collateral decimals — 6 dp. Today's collateral is pUSD (Polymarket USD,
#: 0xc011a7e1…), not USDC.e; same 6 decimals, so this constant is unchanged.
USDC_DECIMALS = 6

VENV_HINT = (
    "py-clob-client is not importable with this interpreter. Third-party deps live in the "
    "existing isolated venv — run with:\n"
    "  /home/da/桌面/poly-yes2/live-probe/.venv/bin/python live/reconcile.py\n"
    "(stdlib-only unit tests: python3.13 tests_live.py)"
)

_IPV4_INSTALLED = False


def force_ipv4() -> bool:
    """Force every DNS lookup to ``AF_INET``.

    Why: this host (192.168.1.98) has no IPv6 route. When DNS returns an AAAA
    record, ``connect()`` fails with ``ENETUNREACH`` before it ever tries the A
    record — so pin the family instead of hoping for happy-eyeballs. Idempotent.
    """
    global _IPV4_INSTALLED
    if _IPV4_INSTALLED:
        return True
    original = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo
    _IPV4_INSTALLED = True
    return True


def http_json(url: str, *, timeout: int = 20) -> Any:
    """GET a JSON document (urllib honours ``http_proxy``/``https_proxy`` from env)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url.split('?')[0]}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error for {url.split('?')[0]}: {exc.reason}") from None


def _py_clob() -> dict[str, Any]:
    """Lazy import of py-clob-client; raises a clear error when absent."""
    force_ipv4()
    try:
        from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
        from py_clob_client.client import ClobClient
    except ImportError as exc:  # pragma: no cover - exercised only outside the venv
        raise RuntimeError(f"{exc}\n{VENV_HINT}") from None
    return {
        "ClobClient": ClobClient,
        "ApiCreds": ApiCreds,
        "AssetType": AssetType,
        "BalanceAllowanceParams": BalanceAllowanceParams,
    }


def build_client(creds: dict, *, host: str = CLOB_HOST, chain_id: int = CHAIN_ID) -> Any:
    """Construct a Level-2 (read) ClobClient from validated creds."""
    lib = _py_clob()
    api_creds = None
    if creds.get("api_key") and creds.get("api_secret") and creds.get("api_passphrase"):
        api_creds = lib["ApiCreds"](
            creds["api_key"], creds["api_secret"], creds["api_passphrase"]
        )
    return lib["ClobClient"](
        host,
        chain_id=chain_id,
        key=creds["private_key"],
        creds=api_creds,
        signature_type=creds["signature_type"],
        funder=creds["funder_address"],
    )


def get_balance_allowance(client: Any) -> dict:
    """Collateral (USDC) balance + per-contract allowances. Read-only."""
    lib = _py_clob()
    params = lib["BalanceAllowanceParams"](asset_type=lib["AssetType"].COLLATERAL)
    return client.get_balance_allowance(params)


def get_open_orders(client: Any) -> list:
    """Currently open orders for this API key. Read-only."""
    return client.get_orders()


def fetch_positions(address: str, *, limit: int = 500, timeout: int = 25) -> list:
    """Positions held by ``address`` from Polymarket's data-api. Read-only."""
    query = urllib.parse.urlencode({"user": address, "limit": limit, "sizeThreshold": 0.1})
    data = http_json(f"{DATA_API}/positions?{query}", timeout=timeout)
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("data-api /positions returned an unexpected shape")
    return data


def fetch_egress_info(*, timeout: int = 15) -> dict:
    """Best-effort public egress identity (ipinfo.io). Caller handles failure."""
    data = http_json("https://ipinfo.io/json", timeout=timeout)
    return {
        "ip": data.get("ip"),
        "country": data.get("country"),
        "org": data.get("org"),
    }
