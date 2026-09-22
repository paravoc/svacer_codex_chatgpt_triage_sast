#!/usr/bin/env python3
"""Run one shared, authenticated Svacer MCP server on loopback only."""

from __future__ import annotations

import os
import json
import sys

from triage_connector.server import main


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    if "--login-stdin" in sys.argv[1:]:
        def report(status):
            try:
                print(status, flush=True)
            except OSError:
                pass

        try:
            raw = sys.stdin.buffer.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise ValueError
            bootstrap = json.loads(raw)
            raw = b""
            if not isinstance(bootstrap, dict):
                raise ValueError
        except Exception:
            report("failed")
            raise SystemExit(2) from None
        finally:
            sys.stdin.close()
        main(bootstrap=bootstrap, startup_report=report)
    else:
        main()
