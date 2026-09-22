#!/usr/bin/env python3
"""Open the standalone themed Svacer login without a console window."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from svacer_connection import local_port, read_local_mcp_token
from svacer_login_qt import SvacerLoginDialog
from triage_gui_qt import STYLE, button


DEFAULT_MCP_URL = "http://127.0.0.1:8002/mcp"


def connection_settings(app_directory: Path) -> tuple[str, str]:
    """Load public connection settings; a fresh clone intentionally has none."""
    settings: dict[str, object] = {}
    path = app_directory / "svacer-settings.json"
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, ValueError):
            pass
    server_url = str(settings.get("svacer_url") or "").strip()
    mcp_url = str(settings.get("mcp_url") or DEFAULT_MCP_URL).strip()
    try:
        local_port(mcp_url)
    except Exception:
        mcp_url = DEFAULT_MCP_URL
    return server_url, mcp_url


def main() -> int:
    app_directory = Path(__file__).resolve().parent
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Svacer.Triage.Login")
        except (AttributeError, OSError):
            pass
    app = QApplication([])
    app.setApplicationName("Svacer Triage")
    app.setApplicationDisplayName("Вход в Svacer — Svacer Triage")
    app.setOrganizationName("Svacer Triage")
    icon_path = app_directory / "assets" / "svacer-triage-v2.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    windows_font = Path("C:/Windows/Fonts/segoeui.ttf")
    if windows_font.is_file():
        QFontDatabase.addApplicationFont(str(windows_font))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)

    token = read_local_mcp_token()
    if len(token) < 32:
        QMessageBox.critical(
            None, "Svacer Triage",
            "Локальное подключение ещё не установлено. Откройте START — "
            "установка выполнится автоматически, затем повторите вход.",
        )
        return 2
    server_url, mcp_url = connection_settings(app_directory)
    dialog = SvacerLoginDialog(
        None, app_directory, server_url, mcp_url, token, button_factory=button,
    )
    if icon_path.is_file():
        dialog.setWindowIcon(QIcon(str(icon_path)))
    try:
        return 0 if dialog.exec() == QDialog.DialogCode.Accepted else 1
    finally:
        dialog.shutdown()
        dialog.deleteLater()
        token = ""


if __name__ == "__main__":
    raise SystemExit(main())
