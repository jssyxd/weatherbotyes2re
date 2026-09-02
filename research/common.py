"""Shared helpers for the poly-yes2 research pipeline (stdlib only).

No pandas / requests — matches the weatherbot stdlib-only philosophy so the
research layer runs anywhere Python 3.10+ is available (Windows, WSL, Docker).
"""
from __future__ import annotations

import csv
import json
import os
import re
import urllib.error
import urllib.request
import calendar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# TX/TN forecast temperature groups. TX34/1015Z = max 34C valid day 10 @15Z.
# TX M05/1005Z = -5C. Day is optional (defaults to TAF validity day).
TX_RE = re.compile(r"TX(?P<sign>M)?(?P<temp>\d{2})/(?:(?P<day>\d{2}))?(?P<hour>\d{2})Z", re.I)
TN_RE = re.compile(r"TN(?P<sign>M)?(?P<temp>\d{2})/(?:(?P<day>\d{2}))?(?P<hour>\d{2})Z", re.I)

CHECKWX_BASE = "https://api.checkwx.com"


def load_env(path: str | os.PathLike | None = None) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env (no dotenv dependency)."""
    env: dict[str, str] = {}
    # environment variables take precedence over the .env file, so the Docker
    # compose env_file can supply secrets without baking .env into the image.
    env.update(os.environ)
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() not in env or not env[key.strip()]:
            env[key.strip()] = value.strip()
    return env


def load_cities(path: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    p = Path(path) if path else ROOT / "config" / "contract_cities.json"
    if not p.is_absolute():
        p = ROOT / p
    return json.loads(p.read_text(encoding="utf-8"))


def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error for {url}: {exc.reason}") from exc


def checkwx_taf(icaos: list[str], api_key: str, chunk: int = 15) -> dict[str, str]:
    """Fetch current TAFs from CheckWX in batches. Returns {icao: raw_taf}."""
    out: dict[str, str] = {}
    for i in range(0, len(icaos), chunk):
        batch = icaos[i : i + chunk]
        url = f"{CHECKWX_BASE}/taf/{','.join(batch)}"
        data = http_json(url, headers={"X-API-Key": api_key})
        for entry in data.get("data", []):
            # entry looks like "TAF EHAM 311140Z ..."
            parts = entry.split(None, 2)
            if len(parts) >= 3 and parts[0].upper() == "TAF":
                out[parts[1]] = parts[2]
    return out


def parse_tx_tn(raw_taf: str) -> dict[str, Any]:
    """Extract TX/TN forecast groups from a raw TAF string.

    Returns {tx_c, tx_hour, tx_day, tn_c, tn_hour, tn_day} with missing fields None.
    """
    result: dict[str, Any] = {}
    m = TX_RE.search(raw_taf)
    if m:
        temp = int(m.group("temp"))
        if m.group("sign"):
            temp = -temp
        result.update(tx_c=temp, tx_hour=int(m.group("hour")), tx_day=m.group("day"))
    m = TN_RE.search(raw_taf)
    if m:
        temp = int(m.group("temp"))
        if m.group("sign"):
            temp = -temp
        result.update(tn_c=temp, tn_hour=int(m.group("hour")), tn_day=m.group("day"))
    return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_csv(path: str | os.PathLike, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


ISSUE_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})Z")


def parse_taf_issue_time(raw_taf: str, ref_utc: datetime | None = None) -> datetime | None:
    """Parse the TAF issue time (DDHHMMZ) into an aware UTC datetime.

    Month/year are inferred from ``ref_utc`` (defaults to now). If the issue
    day is >15 days ahead of ref (month boundary), we assume the previous month.
    """
    ref = ref_utc or utc_now()
    m = ISSUE_RE.match(raw_taf.strip())
    if not m:
        return None
    day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        dt = datetime(ref.year, ref.month, day, hh, mm, tzinfo=timezone.utc)
    except ValueError:
        return None
    if (dt - ref) > timedelta(days=15):
        prev = ref.month - 1 or 12
        year = ref.year - 1 if prev == 12 else ref.year
        try:
            dt = datetime(year, prev, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            return None
    return dt


def resolve_tx_valid_utc(issue_dt: datetime, tx_day: str | None, tx_hour: int) -> datetime | None:
    """Resolve TX valid datetime from issue time + optional day-of-month + hour."""
    year, month = issue_dt.year, issue_dt.month
    if tx_day is not None:
        day = int(tx_day)
        if day < issue_dt.day:
            month += 1
            if month > 12:
                month, year = 1, year + 1
    else:
        day = issue_dt.day
    _, last = calendar.monthrange(year, month)
    day = min(day, last)
    try:
        return datetime(year, month, day, tx_hour, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


def local_date_for(city: dict[str, Any], dt_utc: datetime) -> str:
    """IANA local date for a city at a UTC instant (matches weatherbot rule)."""
    from zoneinfo import ZoneInfo
    return dt_utc.astimezone(ZoneInfo(city["timezone"])).date().isoformat()


METAR_T_RE = re.compile(r"T([01])(\d{3})([01])(\d{3})")
METAR_TT_RE = re.compile(r"(?<![A-Za-z])(M?\d{2})/(M?\d{2})(?![A-Za-z])")


def parse_metar_temp_c(raw_metar: str) -> float | None:
    """Extract air temperature (°C) from a METAR string. T-group preferred."""
    m = METAR_T_RE.search(raw_metar)
    if m:
        sign = -1.0 if m.group(1) == "1" else 1.0
        return sign * int(m.group(2)) / 10.0
    m = METAR_TT_RE.search(raw_metar)
    if m:
        temp = m.group(1)
        sign = -1.0 if temp.startswith("M") else 1.0
        return sign * float(temp.lstrip("M"))
    return None


def checkwx_metar(icaos: list[str], api_key: str, chunk: int = 20) -> dict[str, str]:
    """Fetch current METARs from CheckWX. Returns {icao: raw_metar}."""
    out: dict[str, str] = {}
    for i in range(0, len(icaos), chunk):
        batch = icaos[i : i + chunk]
        url = f"{CHECKWX_BASE}/metar/{','.join(batch)}"
        data = http_json(url, headers={"X-API-Key": api_key})
        for entry in data.get("data", []):
            parts = entry.split(None, 2)
            if len(parts) >= 3 and parts[0].upper() == "METAR":
                out[parts[1]] = parts[2]
    return out
