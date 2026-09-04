"""State, config, log, and health I/O for the weatherbotyes2re paper runner.

Faithful reconstruction of the intended ``_r_state`` module (see CHANGELOG +
``runner_impl.py`` import contract). Stdlib only. All money values travel as
``Decimal`` across the live session and are only widened to float/JSON strings
at the persistence boundary (``paper_capital``/``save_state``).
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Runtime schema version. Bump and add a migration in ``load_state`` when the
# on-disk shape of ``data/yes2re_state.json`` changes incompatibly.
STATE_VERSION = 2

# State tree sections we create on first load.
_SECTIONS = ("armed", "fired", "running_extremes", "taf_forecasts", "last_obs", "last_obs_time")

# Sensible defaults when a key is absent from a (hand-written) config JSON.
DEFAULTS: dict[str, Any] = {
    "mode": "paper",
    "scan_interval_seconds": 20,
    "fast_poll_interval_seconds": 8,
    "idle_metar_interval_seconds": 45,
    "idle_book_interval_seconds": 30,
    "arm_metar_interval_seconds": 8,
    "arm_book_interval_seconds": 8,
    "rules_refresh_interval_seconds": 1200,
    "taf_refresh_interval_seconds": 1800,
    "tail_hours": 144,
    "checkwx_api_key_env": "CHECKWX_API_KEY",
    "base_fee_rate": "0.02",
    "paper_initial_capital_usdc": 1000.0,
    "fire_budget_usdc": 20.0,
    "max_open_positions": 12,
    "settle_grace_hours": 6,
    "settle_max_hours": 72,
    "contract_cities_path": "config/contract_cities.json",
    # Optional: restrict the discovery/trading universe. Absent -> whole registry.
    "active_icaos": None,
    "state_path": "data/yes2re_state.json",
    "log_path": "data/yes2re_events.jsonl",
    "health_path": "data/yes2re_health.json",
    "strategy": {},
}

_LOG_FIELDS_STR = None


def _decode(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


def load_config(path: str | os.PathLike) -> dict[str, Any]:
    """Read + validate a single run-config JSON, merging missing keys with
    :data:`DEFAULTS`. The ``strategy`` sub-dict is merged shallowly with the
    strategy module defaults at call time (see ``reversal_strategy``)."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    p = Path(path)
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            user = json.load(fh)
        cfg.update({k: v for k, v in user.items() if v is not None})
        strat = dict(cfg.get("strategy") or {})
        if isinstance(user.get("strategy"), dict):
            strat.update(_decode(user["strategy"]))
        cfg["strategy"] = strat
    _validate_config(cfg, p)
    # Default active universe: whole registry unless restricted.
    cfg.setdefault("active_icaos", None)
    return cfg


def _validate_config(cfg: dict[str, Any], source: Path) -> None:
    mode = str(cfg.get("mode", "paper")).lower()
    if mode != "paper":
        raise SystemExit(f"refusing non-paper mode {mode!r}: safety lock (paper only)")
    intervs = [
        "scan_interval_seconds",
        "fast_poll_interval_seconds",
        "idle_metar_interval_seconds",
        "idle_book_interval_seconds",
    ]
    for k in intervs:
        try:
            v = float(cfg.get(k, DEFAULTS.get(k, 0)))
        except (TypeError, ValueError):
            v = -1
        if cfg.get(k) is None or v < 0:
            raise SystemExit(f"config {source}: bad required interval {k}={cfg.get(k)!r}")
    for money in ("paper_initial_capital_usdc", "fire_budget_usdc"):
        if cfg.get(money) is None or float(cfg[money]) < 0:
            raise SystemExit(f"config {source}: bad money field {money}={cfg.get(money)!r}")


def _blank_state(cfg: dict[str, Any]) -> dict[str, Any]:
    capital = float(cfg.get("paper_initial_capital_usdc", DEFAULTS["paper_initial_capital_usdc"]))
    return {
        "positions": {},
        "entry_count": 0,
        "weatherbotyes2re": {s: {} for s in _SECTIONS},
        "version": STATE_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_saved_at_utc": None,
        "paper_initial_capital_usdc": capital,
        "paper_total_debit_usdc": 0.0,
    }


def load_state(path: str | os.PathLike) -> dict[str, Any]:
    """Load the state blob from ``path``, migrating/blanking if stale/missing.
    Returned dict is fully mutable and safe for the strategy to read/write."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return _blank_state({})  # capital patched by caller via setdefault
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _blank_state({})
    if not isinstance(raw, dict):
        return _blank_state({})
    if raw.get("version") != STATE_VERSION:
        # Preserve capital across schema bumps, then re-blank.
        capital = raw.get("paper_initial_capital_usdc", _blank_state({})["paper_initial_capital_usdc"])
        st = _blank_state({})
        st["paper_initial_capital_usdc"] = float(capital)
        st.pop("removed_capital_note", None)
        return st
    tree = raw.setdefault("weatherbotyes2re", {})
    for s in _SECTIONS:
        tree.setdefault(s, {})
    raw.setdefault("positions", {})
    raw.setdefault("entry_count", 0)
    raw.setdefault("paper_total_debit_usdc", 0.0)
    raw.setdefault("paper_initial_capital_usdc", DEFAULTS["paper_initial_capital_usdc"])
    return raw


def save_state(path: str | os.PathLike, state: dict[str, Any]) -> None:
    """Serialize the state blob to disk (float-widened for JSON)."""
    d = Path(path)
    if d.parent and not d.parent.exists():
        d.parent.mkdir(parents=True, exist_ok=True)
    state["last_saved_at_utc"] = datetime.now(timezone.utc).isoformat()
    tmp = d.with_suffix(d.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(d)


def log_event(path: str | os.PathLike, event: dict[str, Any]) -> None:
    """Append one JSON line to the JSONL events log. ``event`` gains an
    ISO-``ts_utc`` if absent. Never raises on write failure: the observer path
    must stay up even if disk hiccups."""
    p = Path(path)
    try:
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        if "ts_utc" not in event:
            event["ts_utc"] = datetime.now(timezone.utc).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
    except OSError:
        pass


def monitor_epoch() -> float:
    return time.time()


def _iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
