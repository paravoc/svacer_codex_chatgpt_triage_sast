#!/usr/bin/env python3
"""Start the private Svacer connector and the authenticated web service."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
from pathlib import Path

from svacer_connection import start_connection, stop_owned_process
from triage_queue import GOST_FILTER


APP_DIRECTORY = Path(__file__).resolve().parent


def secret_file(name: str, *, minimum: int = 1) -> str:
    path = os.getenv(f"{name}_FILE", "").strip()
    if not path:
        raise RuntimeError(f"{name}_FILE is required")
    try:
        value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise RuntimeError(f"Cannot read {name}_FILE") from exc
    if len(value) < minimum or "\x00" in value:
        raise RuntimeError(f"{name}_FILE has an invalid value")
    return value


def positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}")
    return value


def write_settings(data_root: Path, port: int) -> Path:
    settings = {
        "svacer_url": os.getenv("SVACER_URL", "").strip(),
        "mcp_url": f"http://127.0.0.1:{port}/mcp",
        "filter_name": "ГОСТ 71207-2024",
        "advanced_filter": GOST_FILTER,
        "parallel_workers": positive_int("SVACER_PARALLEL_WORKERS", 3, 8),
        "verification_enabled": True,
        "verification_verdicts": ["Confirmed"],
        "verification_workers": positive_int("SVACER_VERIFICATION_WORKERS", 2, 8),
        "saved_context_token_warning": max(0, int(os.getenv("SVACER_CONTEXT_TOKEN_WARNING", "200000"))),
    }
    if not settings["svacer_url"]:
        raise RuntimeError("SVACER_URL is required")
    path = data_root / "svacer-settings.json"
    temporary = data_root / ".svacer-settings.json.tmp"
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def main() -> int:
    data_root = Path(os.getenv("SVACER_DATA_DIR", "/data")).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "RESULTS").mkdir(exist_ok=True)
    port = positive_int("SVACER_MCP_PORT", 8002, 65535)
    settings_path = write_settings(data_root, port)
    login = secret_file("SVACER_LOGIN")
    password = secret_file("SVACER_PASSWORD")
    # Validate web and model secrets before opening a listening socket. Their
    # values are never copied into settings, process arguments, or logs.
    secret_file("SVACER_WEB_TOKEN", minimum=32)
    if os.getenv("CODEX_API_KEY_FILE", "").strip():
        secret_file("CODEX_API_KEY", minimum=20)
    token = secrets.token_urlsafe(48)
    mcp_url = f"http://127.0.0.1:{port}/mcp"
    os.environ["SVACER_DATA_DIR"] = str(data_root)
    os.environ["SVACER_SETTINGS_FILE"] = str(settings_path)
    os.environ["SVACER_LOCAL_MCP_TOKEN"] = token
    cancel = threading.Event()
    connector = None
    web = None
    try:
        connector = start_connection(
            APP_DIRECTORY, os.getenv("SVACER_URL", ""), mcp_url, token,
            login, password, cancel, timeout=45, root_directory=data_root,
        )
        login = password = token = ""
        command = [
            sys.executable, "-m", "uvicorn", "web_service:app",
            "--host", os.getenv("SVACER_WEB_HOST", "0.0.0.0"),
            "--port", str(positive_int("SVACER_WEB_PORT", 8080, 65535)),
            "--no-access-log",
        ]
        web = subprocess.Popen(command, cwd=str(APP_DIRECTORY), env=dict(os.environ))

        def terminate(_signum, _frame):
            cancel.set()
            if web is not None and web.poll() is None:
                web.terminate()

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        return web.wait()
    finally:
        login = password = token = ""
        cancel.set()
        if web is not None and web.poll() is None:
            web.terminate()
            try:
                web.wait(timeout=5)
            except subprocess.TimeoutExpired:
                web.kill()
                web.wait(timeout=5)
        if connector is not None:
            stop_owned_process(connector)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
