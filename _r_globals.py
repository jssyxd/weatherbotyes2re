"""Process-local caches for paper runner. No sigma."""
from __future__ import annotations
from typing import Any
from consensus_tracker import ConsensusTracker

_TRACKER: ConsensusTracker | None = None
_LAST_TAF: dict[str, Any] = {"at": 0.0, "extreme": {}}
_LAST_RULES: dict[str, Any] = {"at": 0.0, "rules": [], "by_city_date": {}}
_LAST_IDLE_METAR: float = 0.0
_LAST_IDLE_BOOK: float = 0.0
_METAR_CACHE: dict[str, dict[str, Any]] = {}
_BOOK_CACHE: dict[str, dict] = {}
