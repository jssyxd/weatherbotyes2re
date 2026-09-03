#!/usr/bin/env python3
"""Assemble runner_impl from b64 shards if needed. No sigma/bias path."""
from pathlib import Path
import base64, sys
root = Path(__file__).resolve().parent
target = root / "runner_impl.py"
if not target.exists() or target.stat().st_size < 10000:
    data = "".join((root / f"runner_impl.b64.{i}").read_text() for i in range(4))
    target.write_bytes(base64.b64decode(data))
from runner_impl import main
if __name__ == "__main__":
    sys.exit(main())
