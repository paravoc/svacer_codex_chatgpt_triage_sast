#!/usr/bin/env python3
"""Qt desktop interface for local Svacer triage.

Never apply markup without explicit confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from html import escape
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

from PySide6.QtCore import QEasingCurve, QEvent, QItemSelectionModel, QPointF, QPropertyAnimation, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFontDatabase, QIcon, QMovie, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHeaderView, QMainWindow, QProgressBar, QPushButton,
    QScrollArea, QSpinBox,
    QMessageBox, QPlainTextEdit, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from codex_run import (
    console_python_executable, hidden_subprocess_kwargs, launch_runner,
    normalize_codex_model, read_codex_models, read_codex_rate_limits,
    read_codex_usage, read_run_record, stop_run,
)
from marker_history import (
    compare_history_attempts, history_measurements, load_marker_history,
    previous_history_attempt,
)
from local_jobs import trash_local_job, update_marker_inventory, require_idle
from project_setup_qt import ProjectSetupDialog
from svacer_login_qt import SvacerLoginDialog
from svacer_connection import local_port, read_local_mcp_token
from marker_notifications import dismiss_notification, dismiss_notifications, sync_notifications
from triage_dashboard import (
    atomic_json, call_mcp_tool, check_mcp, collect_state, friendly_mcp_error,
    read_json, read_jsonl, resolve_job, set_pause,
    stop_svacer_connection,
)
from triage_queue import (
    VALID_ACTIONS, VALID_SEVERITIES, approve_saved_draft, dequeue_marker_ids,
    complete_desktop_settings, edit_saved_decision, enqueue_marker_ids,
    markers_for_triage, marker_review_status, load_decisions, reset_queue_assignments,
)
from triage_gui import (
    FILTERS, MARKER_INVENTORY_FIELDS, SAVED_RESULT_DETAIL, VALID_VERDICTS, active_marker_ids,
    codex_activity_entries, comment_without_heading, compact_job_timestamp, create_user_report,
    current_run_queue_ids, format_count, queued_marker_status,
    friendly_run_state, format_run_event_time, job_identity, job_selector_label, latest_codex_activity,
    list_saved_jobs, list_text, live_run_timing, marker_assignments,
    marker_matches_filter, marker_svacer_url, saved_result_assignments, short_file, unapplied_draft_results,
    validate_marker_inventory,
)


COLORS = {
    "bg": "#111317", "surface": "#1b1e23", "surface2": "#20242b",
    "line": "#323842", "text": "#e7e9ed", "muted": "#9da5b0",
    "blue": "#a8cef4", "green": "#78cba6", "amber": "#e5bd79",
    "red": "#ed9292", "violet": "#baa8ef",
}

STATUS_TONES = {
    "Confirmed": "red", "False Positive": "green", "Won't fix": "amber",
    "Unclear": "violet", "Черновик": "blue", "В работе": "green",
    "Не завершён": "amber", "Ошибка": "red", "Ожидает": "muted",
    "Перепроверка": "violet", "Результат сохранён": "blue",
    "В очереди": "blue", "Не в очереди": "muted", "Доисследовать": "amber",
}


def format_history_seconds(value: Any) -> str:
    try:
        seconds = max(0, round(float(value)))
    except (TypeError, ValueError):
        return "Не измерено"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {seconds:02d} с"
    return f"{seconds} с"


def history_measurement_cells(measurement: dict[str, Any]) -> tuple[str, str]:
    duration = measurement.get("duration_seconds")
    tokens = measurement.get("tokens")
    inherited = bool(measurement.get("inherited_from_previous"))
    if duration is None:
        duration_text = "Не измерено"
    else:
        duration_text = format_history_seconds(duration)
        if inherited:
            duration_text += " · исходно"
        elif str(measurement.get("duration_scope") or "").startswith("От начала"):
            duration_text += " · до сохранения"
        elif str(measurement.get("duration_scope") or "").startswith("Общее"):
            duration_text += " · партия"
    if tokens is None:
        token_text = "Не измерено"
    else:
        token_text = format_count(int(tokens))
        if inherited:
            token_text += " · исходно"
        elif str(measurement.get("token_scope") or "").startswith("Общий"):
            token_text += " · общие"
    return duration_text, token_text

STYLE = """
QMainWindow, QDialog, QWidget#root { background: #111317; color: #e7e9ed; font-family: 'Segoe UI'; font-size: 13px; }
QWidget { color: #e7e9ed; font-family: 'Segoe UI'; font-size: 13px; }
QLabel { background: transparent; }
QFrame#card { background: #1b1e23; border: 1px solid #323842; border-radius: 12px; }
QFrame#subcard { background: #20242b; border: 1px solid #323842; border-radius: 10px; }
QFrame#notificationPanel { background: #191e24; border: 1px solid #46505b; border-radius: 13px; }
QScrollArea#notificationScroll { background: transparent; border: 0; }
QScrollArea#settingsScroll, QWidget#settingsBody { background: #111317; border: 0; }
QPushButton#notificationDismissAll { color: #b7c7d7; background: #2a3139; border: 1px solid #46505b; border-radius: 7px; padding: 5px 9px; }
QPushButton#notificationDismissAll:hover { color: #f1f7fd; background: #3a4754; border-color: #79a3c2; }
QPushButton#notificationDismissAll:pressed { background: #25313c; }
QPushButton#toastGreen { text-align: left; color: #e7fff2; background: #1c3d32; border: 1px solid #4d9e76; border-radius: 9px; padding: 8px 11px; }
QPushButton#toastGreen:hover { background: #265542; border-color: #83d2a7; }
QPushButton#toastYellow { text-align: left; color: #fff1d5; background: #483922; border: 1px solid #ad854a; border-radius: 9px; padding: 8px 11px; }
QPushButton#toastYellow:hover { background: #5e4829; border-color: #e3b96d; }
QPushButton#toastRed { text-align: left; color: #ffe8e8; background: #49292c; border: 1px solid #b36169; border-radius: 9px; padding: 8px 11px; }
QPushButton#toastRed:hover { background: #633338; border-color: #ed9292; }
QLabel#title { font-size: 19px; font-weight: 700; }
QLabel#section { font-size: 15px; font-weight: 650; }
QLabel#muted { color: #9da5b0; }
QPushButton { color: #d9edff; background: #253849; border: 1px solid #486b86; border-radius: 8px; padding: 7px 12px; font-weight: 600; }
QPushButton:hover { background: #31506a; border-color: #74add7; }
QPushButton:pressed { background: #1c3040; }
QPushButton[tone='primary'] { color: #f1f9ff; background: #355e81; border-color: #78b9e9; }
QPushButton[tone='primary']:hover { background: #477ca7; }
QPushButton[tone='primary']:pressed { background: #294c69; }
QPushButton[tone='success'] { color: #e5fff1; background: #245846; border-color: #55ad86; }
QPushButton[tone='success']:hover { background: #327458; }
QPushButton[tone='success']:pressed { background: #1d4938; }
QPushButton[tone='warning'] { color: #fff0d2; background: #674b29; border-color: #c99a5d; }
QPushButton[tone='warning']:hover { background: #806033; }
QPushButton[tone='warning']:pressed { background: #513c23; }
QPushButton[tone='danger'] { color: #ffe5e5; background: #633739; border-color: #d08488; }
QPushButton[tone='danger']:hover { background: #7e4548; }
QPushButton[tone='danger']:pressed { background: #502e30; }
QPushButton[tone='violet'] { color: #f0e8ff; background: #493d62; border-color: #a38acb; }
QPushButton[tone='violet']:hover { background: #5e507d; }
QPushButton[tone='violet']:pressed { background: #3b3150; }
QPushButton:disabled, QPushButton[tone='primary']:disabled,
QPushButton[tone='success']:disabled, QPushButton[tone='warning']:disabled,
QPushButton[tone='danger']:disabled, QPushButton[tone='violet']:disabled {
    color: #777f89; background: #24282e; border-color: #343a42;
}
QTabWidget::pane { border: 0; background: #111317; }
QTabBar::tab { background: transparent; color: #9da5b0; padding: 10px 20px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #e7e9ed; border-bottom-color: #b9d9f5; }
QTabBar::tab:hover { color: #e7e9ed; }
QSplitter::handle { background: #111317; width: 9px; height: 9px; }
QTableWidget { background: #1b1e23; alternate-background-color: #20242b; border: 0; gridline-color: transparent; selection-background-color: #34485a; selection-color: #f5f8fb; }
QHeaderView::section { background: #242b33; color: #b7c1ce; border: 0; padding: 8px; font-weight: 650; }
QTextBrowser, QPlainTextEdit { background: #20242b; color: #e7e9ed; border: 1px solid #323842; border-radius: 8px; padding: 8px; selection-background-color: #34485a; }
QLineEdit, QComboBox, QSpinBox { background: #20242b; color: #e7e9ed; border: 1px solid #3a424c; border-radius: 7px; padding: 6px; }
QComboBox QAbstractItemView { background: #20242b; color: #e7e9ed; selection-background-color: #34485a; }
QProgressBar { background: #29323b; border: 0; border-radius: 4px; height: 8px; }
QProgressBar::chunk { background: #92c4ec; border-radius: 4px; }
QScrollBar:vertical { background: #1b1e23; width: 10px; }
QScrollBar::handle:vertical { background: #48515b; border-radius: 5px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #1b1e23; height: 10px; }
QScrollBar::handle:horizontal { background: #48515b; border-radius: 5px; min-width: 25px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


def label(text: str, kind: str | None = None) -> QLabel:
    widget = QLabel(text)
    if kind:
        widget.setObjectName(kind)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class HoverButton(QPushButton):
    """Animate a small color glow without changing geometry or rebuilding the style."""

    def __init__(self, text: str, tone: str = "neutral") -> None:
        super().__init__(text)
        self.setProperty("tone", tone)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._hover_effect = QGraphicsDropShadowEffect(self)
        self._hover_effect.setBlurRadius(14)
        self._hover_effect.setOffset(0, 0)
        self._hover_effect.setColor(QColor(0, 0, 0, 0))
        self._hover_effect.setEnabled(False)
        self.setGraphicsEffect(self._hover_effect)
        self._hover_animation = QPropertyAnimation(self._hover_effect, b"color", self)
        self._hover_animation.setDuration(150)
        self._hover_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_animation.finished.connect(self._finish_hover_animation)

    def _hover_color(self, active: bool) -> QColor:
        tone = self.property("tone")
        key = {"success": "green", "warning": "amber", "danger": "red", "violet": "violet"}.get(tone, "blue")
        color = QColor(COLORS[key])
        color.setAlpha(125 if active else 0)
        return color

    def _update_hover(self, *, animate: bool = True) -> None:
        active = self.isEnabled() and (self._hovered or self.hasFocus())
        target = self._hover_color(active)
        self._hover_animation.stop()
        if not animate or not self.isEnabled():
            self._hover_effect.setColor(target)
            self._hover_effect.setEnabled(active)
            return
        if self._hover_effect.color() == target:
            return
        self._hover_effect.setEnabled(True)
        self._hover_animation.setStartValue(self._hover_effect.color())
        self._hover_animation.setEndValue(target)
        self._hover_animation.start()

    def _finish_hover_animation(self) -> None:
        if self._hover_effect.color().alpha() == 0:
            self._hover_effect.setEnabled(False)

    def enterEvent(self, event: Any) -> None:
        super().enterEvent(event)
        self._hovered = True
        self._update_hover()

    def leaveEvent(self, event: Any) -> None:
        super().leaveEvent(event)
        self._hovered = False
        self._update_hover()

    def focusInEvent(self, event: Any) -> None:
        super().focusInEvent(event)
        self._update_hover()

    def focusOutEvent(self, event: Any) -> None:
        super().focusOutEvent(event)
        self._update_hover()

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and hasattr(self, "_hover_effect"):
            self.setCursor(Qt.CursorShape.PointingHandCursor if self.isEnabled() else Qt.CursorShape.ArrowCursor)
            self._update_hover(animate=False)


def button(text: str, *, tone: str = "neutral") -> QPushButton:
    return HoverButton(text, tone)


def set_button_tone(widget: QPushButton, tone: str) -> None:
    if widget.property("tone") == tone:
        return
    widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
    if isinstance(widget, HoverButton):
        widget._update_hover(animate=False)


def format_codex_limit(data: dict[str, Any] | None) -> tuple[str, str]:
    """Show the remaining allowance and the window it actually belongs to."""
    unavailable = ("Доступно Codex: данные временно недоступны", COLORS["muted"])
    if not isinstance(data, dict):
        return unavailable
    buckets = data.get("rateLimitsByLimitId")
    limit = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(limit, dict):
        limit = data.get("rateLimits")
    if not isinstance(limit, dict):
        return unavailable
    windows = [limit.get(name) for name in ("primary", "secondary")]
    windows = [window for window in windows if isinstance(window, dict)]
    window = next((item for item in windows if item.get("windowDurationMins") == 10080), None)
    if window is None:
        window = next((item for item in windows if item.get("usedPercent") is not None), None)
    if window is None:
        return unavailable
    try:
        used = float(window["usedPercent"])
        if not math.isfinite(used):
            return unavailable
        remaining = 100.0 - max(0.0, min(100.0, used))
    except (KeyError, TypeError, ValueError):
        return unavailable
    duration = window.get("windowDurationMins")
    if duration == 10080:
        period = "недельный лимит"
    elif duration == 300:
        period = "лимит на 5 часов"
    elif isinstance(duration, (int, float)) and duration > 0:
        period = f"окно {int(duration)} мин"
    else:
        period = "период неизвестен"
    reset_text = "время сброса неизвестно"
    try:
        reset = datetime.fromtimestamp(int(window.get("resetsAt"))).astimezone()
        reset_text = "сброс " + reset.strftime("%d.%m.%Y в %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    value = f"{remaining:.0f}%" if remaining.is_integer() else f"{remaining:.1f}%"
    color = COLORS["green"] if remaining > 30 else COLORS["amber"] if remaining > 10 else COLORS["red"]
    return f"Доступно Codex: {value}  •  {period}  •  {reset_text}", color


def card(*, sub: bool = False) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("subcard" if sub else "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(15, 13, 15, 13)
    layout.setSpacing(8)
    return frame, layout


def table(headers: list[str]) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setStretchLastSection(True)
    widget.setShowGrid(False)
    widget.setSortingEnabled(False)
    widget.verticalHeader().setDefaultSectionSize(29)
    return widget


def size_columns(widget: QTableWidget, fixed: dict[int, int], stretch: int) -> None:
    header = widget.horizontalHeader()
    header.setStretchLastSection(False)
    for column, width in fixed.items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        widget.setColumnWidth(column, width)
    header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)


def set_rows(widget: QTableWidget, rows: list[tuple[Any, ...]], keys: list[str] | None = None) -> None:
    """Update only changed cells: avoid blanking the table on every timer tick."""
    multi = widget.selectionMode() == QTableWidget.SelectionMode.ExtendedSelection
    selected_keys = []
    if multi and keys:
        for index in widget.selectionModel().selectedRows(0):
            item = widget.item(index.row(), 0)
            if item is not None:
                selected_keys.append(item.data(Qt.ItemDataRole.UserRole))
    selected = widget.currentRow()
    selected_item = widget.item(selected, 0) if selected >= 0 else None
    selected_key = selected_item.data(Qt.ItemDataRole.UserRole) if selected_item else None
    signals_blocked = widget.blockSignals(True)
    widget.setUpdatesEnabled(False)
    try:
        widget.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column_index, value in enumerate(values):
                text = str(value if value not in (None, "") else "—")
                item = widget.item(row_index, column_index)
                if item is None:
                    item = QTableWidgetItem(text)
                    widget.setItem(row_index, column_index, item)
                elif item.text() != text:
                    item.setText(text)
                item.setToolTip(text)
                if keys and column_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, keys[row_index])
        if multi and keys:
            widget.clearSelection()
            for key in selected_keys:
                if key in keys:
                    widget.selectionModel().select(
                        widget.model().index(keys.index(key), 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                    )
        elif keys and selected_key in keys:
            widget.selectRow(keys.index(selected_key))
        elif 0 <= selected < len(rows):
            widget.selectRow(selected)
        elif rows:
            widget.selectRow(0)
    finally:
        widget.setUpdatesEnabled(True)
        widget.blockSignals(signals_blocked)


def color_status_cells(widget: QTableWidget, statuses: list[str], column: int = 0) -> None:
    """Color only status cells; retain the row selection and text layout."""
    for row, status in enumerate(statuses):
        item = widget.item(row, column)
        if item is None:
            continue
        tone = STATUS_TONES.get(status.removesuffix(" · Svacer"))
        if tone is None:
            item.setData(Qt.ItemDataRole.ForegroundRole, None)
            item.setData(Qt.ItemDataRole.BackgroundRole, None)
            font = item.font()
            if font.bold():
                font.setBold(False)
                item.setFont(font)
            continue
        accent = QColor(COLORS[tone])
        background = QColor(accent)
        background.setAlpha(32 if tone == "muted" else 42)
        item.setForeground(QBrush(accent))
        item.setBackground(QBrush(background))
        font = item.font()
        if not font.bold():
            font.setBold(True)
            item.setFont(font)


def scan_job_rows(paths: list[Path]) -> tuple[list[tuple[Any, ...]], list[str]]:
    """File-only work done off the Qt event loop."""
    rows: list[tuple[Any, ...]] = []
    keys: list[str] = []
    for path in paths:
        try:
            _, target = job_identity(path)
            state = collect_state(path)
            run = read_run_record(path)
            state["codex_run"] = run
            status, _ = friendly_run_state(run, state)
            rows.append((
                f"{target} · {compact_job_timestamp(path)}",
                status,
                f"{state.get('already_reviewed', 0)}/{state.get('inventory_total', 0)}",
                f"{state.get('completed', 0)}/{state.get('total', 0)}",
                len(active_marker_ids(state)),
            ))
            keys.append(str(path.resolve()))
        except (OSError, ValueError):
            continue
    return rows, keys


PREPARATION_PHASE_LABELS = {
    "launching": "Запуск подготовки анализа…",
    "preparing": "Подготовка анализа…",
    "repository": "Подготовка исходников…",
    "batch": "Назначение маркеров и загрузка данных…",
    "traces": "Загрузка трасс маркеров…",
    "sources": "Проверка исходников и контекста…",
}


def preparation_indicator_text(run: dict[str, Any]) -> str:
    if not run.get("active"):
        return ""
    phase = str(run.get("phase") or "").casefold()
    if phase in PREPARATION_PHASE_LABELS:
        return PREPARATION_PHASE_LABELS[phase]
    if str(run.get("status") or "").casefold() in {"launching", "preparing"}:
        return PREPARATION_PHASE_LABELS["preparing"]
    return ""


class AmbientBackdrop(QWidget):
    """A slow, low-cost glow behind the header; never repaints the tables."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.phase = 0.0
        self.motion_enabled = os.getenv("SVACER_REDUCE_MOTION", "").strip().lower() not in {"1", "true", "yes"}
        self.timer = QTimer(self)
        self.timer.setInterval(90)
        self.timer.timeout.connect(self.advance)
        if self.motion_enabled:
            self.timer.start()

    def advance(self) -> None:
        if self.isVisible():
            self.phase = (self.phase + 0.025) % (2 * math.pi)
            self.update()

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#151a20"))
        width, height = float(self.width()), float(self.height())
        for center, tint in (
            (0.24 + 0.07 * math.sin(self.phase), QColor(86, 150, 194, 38)),
            (0.78 + 0.06 * math.cos(self.phase * 0.8), QColor(92, 167, 130, 25)),
        ):
            glow = QRadialGradient(QPointF(width * center, height * 0.45), width * 0.48)
            glow.setColorAt(0.0, tint)
            fade = QColor(tint)
            fade.setAlpha(0)
            glow.setColorAt(1.0, fade)
            painter.fillRect(self.rect(), QBrush(glow))
        painter.end()


class TriageQtWindow(QMainWindow):
    def __init__(self, job: Path, app_directory: Path):
        super().__init__()
        self._ui_ready = False
        self.job = job.resolve()
        self.app_directory = app_directory
        self.tool_directory = app_directory.parent
        loaded_settings = (read_json(app_directory / "svacer-settings.json")
                           if (app_directory / "svacer-settings.json").exists() else {})
        self.settings = complete_desktop_settings(loaded_settings)
        self.mcp_url = str(self.settings.get("mcp_url") or "http://127.0.0.1:8002/mcp")
        self.token = read_local_mcp_token()
        self.busy = False
        self.import_applying = False
        self.connected = False
        self.connection_retries = 0
        self._initial_connection_prompted = False
        self.job_data, self.target_name = job_identity(self.job)
        self.state: dict[str, Any] = {}
        self.inventory: dict[str, dict[str, Any]] = {}
        self.traces: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.decision_by_id: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.marker_signature: tuple[int, ...] | None = None
        self.history_signature: tuple[Any, ...] | None = None
        self.history_records: list[dict[str, Any]] = []
        self.job_paths: list[Path] = []
        self.current_marker_id: str | None = None
        self.current_live_id: str | None = None
        self._table_signatures: dict[str, tuple[Any, ...]] = {}
        self._live_html = ""
        self._activity_signature: tuple[int, int] | None = None
        self._activity_entries: list[str] = []
        self._notification_signature: tuple[str, ...] | None = None
        self.notification_buttons: dict[str, QPushButton] = {}
        self._notification_targets: dict[Path, set[str]] = {}
        self._tab_animation: QPropertyAnimation | None = None
        self._animated_tab: QWidget | None = None
        self.live_dialog: QDialog | None = None
        self.login_dialog: SvacerLoginDialog | None = None
        self._job_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qt-job-list")
        self._jobs_future: Future[tuple[list[tuple[Any, ...]], list[str]]] | None = None
        self._jobs_updated_at = 0.0
        self._task_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qt-action")
        self._task_future: Future[Any] | None = None
        self._task_done: Callable[[Any], None] | None = None
        self._task_failed: Callable[[Exception], None] | None = None
        self._connection_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qt-connection")
        self._connection_future: Future[str] | None = None
        self._codex_limit_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qt-codex-limit")
        self._codex_limit_future: Future[dict[str, Any]] | None = None
        self._codex_limit_updated_at = 0.0
        self._model_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qt-model-list")
        self._model_future: Future[list[dict[str, str]]] | None = None
        self._model_catalog: list[dict[str, str]] = []
        self._model_catalog_loaded = False

        self.setWindowTitle(f"Svacer Triage — {self.target_name}")
        self.setMinimumSize(1060, 720)
        self.resize(1280, 840)
        self.build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.jobs_poll_timer = QTimer(self)
        self.jobs_poll_timer.setInterval(100)
        self.jobs_poll_timer.timeout.connect(self.drain_jobs)
        self.task_poll_timer = QTimer(self)
        self.task_poll_timer.setInterval(100)
        self.task_poll_timer.timeout.connect(self.drain_task)
        self.connection_timer = QTimer(self)
        self.connection_timer.setSingleShot(True)
        self.connection_timer.timeout.connect(self.check_connection)
        self.connection_poll_timer = QTimer(self)
        self.connection_poll_timer.setInterval(100)
        self.connection_poll_timer.timeout.connect(self.drain_connection)
        self.codex_limit_poll_timer = QTimer(self)
        self.codex_limit_poll_timer.setInterval(100)
        self.codex_limit_poll_timer.timeout.connect(self.drain_codex_limit)
        self.model_poll_timer = QTimer(self)
        self.model_poll_timer.setInterval(100)
        self.model_poll_timer.timeout.connect(self.drain_model_catalog)
        self._ui_ready = True
        self.refresh()
        self.timer.start()
        QTimer.singleShot(500, self.check_connection)

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.root_widget = root
        self.setCentralWidget(root)
        self.ambient_backdrop = AmbientBackdrop(root)
        self.ambient_backdrop.lower()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 12)
        outer.setSpacing(9)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.addWidget(label("Svacer Triage", "title"))
        self.subtitle = label("", "muted")
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box, 1)
        self.connection = label("Svacer: проверка…", "muted")
        header.addWidget(self.connection)
        outer.addLayout(header)

        projects = QHBoxLayout()
        projects.addWidget(label("Сохранённая задача", "muted"))
        self.job_combo = QComboBox()
        self.job_combo.currentIndexChanged.connect(self.switch_job)
        projects.addWidget(self.job_combo, 1)
        refresh_jobs = button("Обновить список")
        self.refresh_jobs_button = refresh_jobs
        refresh_jobs.clicked.connect(self.load_job_options)
        projects.addWidget(refresh_jobs)
        self.new_job_button = button("Новый проект", tone="violet")
        self.new_job_button.clicked.connect(self.open_new_job_wizard)
        projects.addWidget(self.new_job_button)
        self.delete_job_button = button("Удалить локально", tone="danger")
        self.delete_job_button.clicked.connect(self.delete_current_job)
        projects.addWidget(self.delete_job_button)
        outer.addLayout(projects)

        self.tabs = QTabWidget()
        self.overview_tab = QWidget()
        self.markers_tab = QWidget()
        self.history_tab = QWidget()
        self.settings_tab = QWidget()
        for name, widget in (
            ("Обзор", self.overview_tab), ("Маркеры", self.markers_tab),
            ("История", self.history_tab), ("Настройки", self.settings_tab),
        ):
            self.tabs.addTab(widget, name)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        outer.addWidget(self.tabs, 1)
        self.build_overview()
        self.build_markers()
        self.build_history()
        self.build_settings()

        footer = QHBoxLayout()
        self.fetch_button = button("Получить маркеры", tone="primary")
        self.fetch_button.clicked.connect(self.fetch_markers)
        footer.addWidget(self.fetch_button)
        self.reset_button = button("Сбросить очередь", tone="warning")
        self.reset_button.clicked.connect(self.reset_current_queue)
        footer.addWidget(self.reset_button)
        self.report_button = button("Открыть отчёт")
        self.report_button.clicked.connect(self.open_report)
        footer.addWidget(self.report_button)
        self.connection_button = button("Войти в Svacer", tone="violet")
        self.connection_button.clicked.connect(self.toggle_connection)
        footer.addWidget(self.connection_button)
        self.update_button = button("Обновить")
        self.update_button.clicked.connect(self.refresh_and_check)
        footer.addWidget(self.update_button)
        footer.addStretch(1)
        self.send_button = button("Отправить в Svacer", tone="warning")
        self.send_button.clicked.connect(self.send_import)
        footer.addWidget(self.send_button)
        outer.addLayout(footer)
        self.status = label("Панель обновляется автоматически.", "muted")
        outer.addWidget(self.status)
        self.build_notification_panel(root)
        root.installEventFilter(self)
        self.load_job_options()

    def build_notification_panel(self, root: QWidget) -> None:
        """Toast-like cards live inside the main window, never in a popup."""
        panel = QFrame(root)
        panel.setObjectName("notificationPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(11, 9, 11, 10)
        panel_layout.setSpacing(7)
        header_row = QHBoxLayout()
        self.notification_header = label("Уведомления", "section")
        header_row.addWidget(self.notification_header)
        header_row.addStretch(1)
        self.notification_dismiss_all = QPushButton("Скрыть все", panel)
        self.notification_dismiss_all.setObjectName("notificationDismissAll")
        self.notification_dismiss_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.notification_dismiss_all.setToolTip("Убрать показанные уведомления, не открывая маркеры")
        self.notification_dismiss_all.clicked.connect(self.hide_all_notifications)
        header_row.addWidget(self.notification_dismiss_all)
        panel_layout.addLayout(header_row)
        scroll = QScrollArea(panel)
        scroll.setObjectName("notificationScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet("background: transparent;")
        self.notification_content = QWidget()
        self.notification_content.setStyleSheet("background: transparent;")
        self.notification_list = QVBoxLayout(self.notification_content)
        self.notification_list.setContentsMargins(2, 2, 2, 2)
        self.notification_list.setSpacing(7)
        scroll.setWidget(self.notification_content)
        panel_layout.addWidget(scroll, 1)
        panel.hide()
        self.notification_panel = panel

    def eventFilter(self, watched: Any, event: QEvent) -> bool:
        if watched is getattr(self, "root_widget", None) and event.type() == QEvent.Type.Resize:
            self.position_ambient_backdrop()
            QTimer.singleShot(0, self.position_notifications)
        return super().eventFilter(watched, event)

    def position_ambient_backdrop(self) -> None:
        root = self.root_widget
        self.ambient_backdrop.setGeometry(9, 5, max(0, root.width() - 18), 60)
        self.ambient_backdrop.lower()

    def position_notifications(self) -> None:
        panel = getattr(self, "notification_panel", None)
        if panel is None or not panel.isVisible():
            return
        root = self.root_widget
        width = min(370, max(250, root.width() - 24))
        card_count = len(self.notification_buttons)
        height = min(49 + card_count * 96, max(150, int(root.height() * 0.48)))
        panel.setGeometry(max(8, root.width() - width - 15),
                          max(8, root.height() - height - 53), width, height)
        panel.raise_()

    def refresh_notifications(self) -> None:
        pending: list[dict[str, str]] = []
        paths = list(dict.fromkeys([self.job, *self.job_paths]))
        for path in paths:
            try:
                _, target = job_identity(path)
                for entry in sync_notifications(path):
                    note = dict(entry)
                    note["_job_path"] = str(path.resolve())
                    note["_project"] = target
                    pending.append(note)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.set_message(f"Уведомления задачи {path.name}: {exc}", error=True)
        pending.sort(key=lambda note: str(note.get("created_at") or ""))
        signature = tuple(f"{note['_job_path']}:{note['id']}" for note in pending)
        if signature == self._notification_signature:
            return
        self._notification_signature = signature
        while self.notification_list.count():
            item = self.notification_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.notification_buttons = {}
        self._notification_targets = {}
        if not pending:
            self.notification_panel.hide()
            return
        self.notification_header.setText(f"Уведомления · {len(pending)}")
        for note in reversed(pending):
            notification_id = str(note["id"])
            self._notification_targets.setdefault(Path(note["_job_path"]), set()).add(notification_id)
            button_key = f"{note['_job_path']}:{notification_id}"
            tone = str(note.get("tone") or "yellow")
            project = str(note.get("_project") or "")
            subject = str(note.get("subject") or "Маркер")
            toast = QPushButton(
                f"{note.get('title') or 'Уведомление'}\n"
                f"{project if len(project) <= 40 else project[:39] + '…'}\n"
                f"{subject if len(subject) <= 47 else subject[:46] + '…'}\n"
                f"{note.get('detail') or ''}", self.notification_content,
            )
            toast.setObjectName({"green": "toastGreen", "red": "toastRed"}.get(tone, "toastYellow"))
            toast.setAccessibleName(f"{note.get('title') or 'Уведомление'}: {note.get('subject') or ''}")
            toast.setToolTip(f"{project}\n{subject}\nНажмите, чтобы открыть и убрать уведомление")
            toast.setMinimumHeight(84)
            toast.clicked.connect(lambda _checked=False, value=dict(note): self.open_notification(value))
            self.notification_list.addWidget(toast)
            self.notification_buttons[button_key] = toast
        self.notification_panel.show()
        self.position_notifications()

    def hide_all_notifications(self) -> None:
        """Dismiss the visible cards across jobs without changing tabs."""
        errors = []
        for job, notification_ids in self._notification_targets.items():
            try:
                dismiss_notifications(job, notification_ids)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{job.name}: {exc}")
        self._notification_signature = None
        self.refresh_notifications()
        if errors:
            self.set_message("Не удалось скрыть уведомления: " + "; ".join(errors), error=True)

    def open_notification(self, note: dict[str, str]) -> None:
        job = Path(note.get("_job_path") or str(self.job)).resolve()
        if job != self.job:
            index = next((i for i, path in enumerate(self.job_paths) if path.resolve() == job), -1)
            if self.busy or index < 0:
                self.set_message("Сейчас нельзя открыть задачу уведомления; оно останется до следующего нажатия.", error=True)
                return
            self.job_combo.setCurrentIndex(index)
            if self.job != job:
                return
        dismiss_notification(job, str(note["id"]))
        self._notification_signature = None
        self.refresh_notifications()
        marker_id = str(note.get("marker_id") or "")
        if marker_id and marker_id in self.inventory:
            self.search.clear()
            self.verdict_filter.setCurrentIndex(0)
            self.current_marker_id = marker_id
            self.tabs.setCurrentWidget(self.markers_tab)
            self.populate_markers()
            self.select_marker_row(marker_id)
        else:
            self.tabs.setCurrentWidget(self.overview_tab)

    def build_overview(self) -> None:
        layout = QVBoxLayout(self.overview_tab)
        layout.setContentsMargins(0, 9, 0, 0)
        progress, body = card()
        top = QHBoxLayout()
        top.addWidget(label("Прогресс анализа", "section"))
        self.analysis_button = button("Начать анализ", tone="success")
        self.analysis_button.clicked.connect(self.analysis_action)
        top.addWidget(self.analysis_button)
        self.analysis_configuration = label("", "muted")
        self.analysis_configuration.setWordWrap(True)
        self.analysis_configuration.setAccessibleName("Модель и параллельность анализа")
        top.addWidget(self.analysis_configuration)
        self.preparation_animation = QLabel()
        self.preparation_animation.setFixedSize(96, 96)
        self.preparation_animation.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preparation_animation.setAccessibleName("Анимация подготовки анализа")
        self.preparation_animation.setStyleSheet("background: transparent; border: 0;")
        self.preparation_animation.hide()
        top.addWidget(self.preparation_animation)
        movie_path = self.app_directory / "assets" / "svacer-hamster-loading.gif"
        self.preparation_movie = QMovie(str(movie_path))
        self.preparation_movie.setCacheMode(QMovie.CacheMode.CacheNone)
        self.preparation_movie.setScaledSize(QSize(96, 96))
        self.preparation_animation.setMovie(self.preparation_movie)
        self.preparation_motion_enabled = (
            os.getenv("SVACER_REDUCE_MOTION", "").strip().lower() not in {"1", "true", "yes"}
        )
        self.preparation_indicator = label("", "muted")
        self.preparation_indicator.setStyleSheet(f"color: {COLORS['green']}; font-weight: 600;")
        self.preparation_indicator.hide()
        top.addWidget(self.preparation_indicator)
        top.addStretch(1)
        self.progress_text = label("0 из 0")
        top.addWidget(self.progress_text)
        body.addLayout(top)
        self.scope = label("", "muted")
        self.run_status = label("", "muted")
        self.usage = label("", "muted")
        self.usage_stop_status = label("", "muted")
        self.codex_limit = label("Доступно Codex: получаю данные…", "muted")
        for widget in (self.scope, self.run_status, self.usage, self.codex_limit, self.usage_stop_status):
            widget.setWordWrap(True)
            body.addWidget(widget)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        body.addWidget(self.progress)
        self._preparation_text = ""
        self._preparation_frame = 0
        self.preparation_timer = QTimer(self)
        self.preparation_timer.setInterval(140)
        self.preparation_timer.timeout.connect(self.advance_preparation_indicator)
        layout.addWidget(progress)

        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        left, left_box = card()
        right, right_box = card()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 4)
        split.setSizes([400, 800])
        jobs_title = label("Задачи  ·  Независимые запуски", "section")
        left_box.addWidget(jobs_title)
        self.jobs_table = table(["Компонент", "Состояние", "В Svacer", "Локально", "В работе"])
        size_columns(self.jobs_table, {1: 112, 2: 82, 3: 82, 4: 71}, 0)
        self.jobs_table.horizontalHeaderItem(2).setToolTip(
            "Маркеры, уже имеющие разметку в загруженном снимке Svacer"
        )
        self.jobs_table.horizontalHeaderItem(3).setToolTip(
            "Маркеры, независимо перепроверенные агентами в этой локальной задаче"
        )
        self.jobs_table.setMaximumHeight(150)
        self.jobs_table.itemDoubleClicked.connect(self.open_job_row)
        left_box.addWidget(self.jobs_table)
        job_actions = QHBoxLayout()
        self.open_job_button = button("Открыть выбранную", tone="primary")
        self.open_job_button.clicked.connect(lambda: self.open_job_row(self.jobs_table.currentItem()))
        job_actions.addWidget(self.open_job_button)
        job_actions.addStretch(1)
        stop_job_button = button("Остановить выбранную", tone="danger")
        self.stop_job_button = stop_job_button
        stop_job_button.clicked.connect(self.stop_selected_job)
        job_actions.addWidget(stop_job_button)
        left_box.addLayout(job_actions)
        self.queue_title = label("В очереди — 0", "section")
        left_box.addWidget(self.queue_title)
        self.queue_table = table(["№", "Статус", "Детектор", "Файл", "Стр."])
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        size_columns(self.queue_table, {0: 37, 1: 110, 2: 155, 4: 56}, 3)
        self.queue_table.itemSelectionChanged.connect(self.on_queue_selected)
        self.queue_table.cellClicked.connect(lambda _row, _column: self.on_queue_selected())
        self.queue_table.itemDoubleClicked.connect(self.open_live_marker_card)
        left_box.addWidget(self.queue_table, 1)
        queue_actions = QHBoxLayout()
        queue_actions.addWidget(label("Ctrl/Shift — выбрать несколько", "muted"), 1)
        self.remove_queue_button = button("Убрать из очереди", tone="warning")
        self.remove_queue_button.setToolTip(
            "Убрать выбранные ожидающие маркеры; назначенные агенту маркеры не затрагиваются"
        )
        self.remove_queue_button.clicked.connect(self.remove_selected_from_queue)
        queue_actions.addWidget(self.remove_queue_button)
        left_box.addLayout(queue_actions)
        self.active_title = label("В работе — 0", "section")
        right_box.addWidget(self.active_title)
        self.active_table = table(["Исполнитель / №", "Состояние", "Детектор", "Файл", "Строка"])
        size_columns(self.active_table, {0: 140, 1: 190, 2: 195, 4: 70}, 3)
        self.active_table.setMaximumHeight(240)
        self.active_table.itemSelectionChanged.connect(self.on_active_selected)
        self.active_table.cellClicked.connect(lambda _row, _column: self.on_active_selected())
        self.active_table.itemDoubleClicked.connect(self.open_live_monitor)
        right_box.addWidget(self.active_table)
        live, live_box = card(sub=True)
        live_head = QHBoxLayout()
        self.live_title = label("Выберите маркер", "section")
        live_head.addWidget(self.live_title, 1)
        live_svacer = button("Открыть в Svacer")
        self.live_svacer_button = live_svacer
        live_svacer.clicked.connect(self.open_live_in_svacer)
        live_head.addWidget(live_svacer)
        self.expand_live_button = button("Развернуть", tone="primary")
        self.expand_live_button.clicked.connect(self.open_live_monitor)
        self.expand_live_button.setEnabled(False)
        live_head.addWidget(self.expand_live_button)
        live_box.addLayout(live_head)
        self.live_meta = label("", "muted")
        live_box.addWidget(self.live_meta)
        self.live_text = QTextBrowser()
        live_box.addWidget(self.live_text, 1)
        right_box.addWidget(live, 1)

    def build_markers(self) -> None:
        layout = QVBoxLayout(self.markers_tab)
        layout.setContentsMargins(0, 9, 0, 0)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label("Поиск", "muted"))
        self.search = QLineEdit()
        self.search.textChanged.connect(self.populate_markers)
        toolbar.addWidget(self.search, 1)
        self.verdict_filter = QComboBox()
        self.verdict_filter.addItems(list(FILTERS))
        self.verdict_filter.currentIndexChanged.connect(self.populate_markers)
        toolbar.addWidget(self.verdict_filter)
        self.marker_count = label("0 маркеров", "muted")
        toolbar.addWidget(self.marker_count)
        layout.addLayout(toolbar)
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        list_card, list_box = card()
        detail_card, detail_box = card()
        split.addWidget(list_card)
        split.addWidget(detail_card)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([500, 750])
        self.marker_table = table(["Статус", "Очередь", "Детектор", "Файл", "Строка"])
        self.marker_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        size_columns(self.marker_table, {0: 160, 1: 115, 2: 171, 4: 64}, 3)
        self.marker_table.itemSelectionChanged.connect(self.on_marker_selected)
        # Moving focus within an existing multi-selection need not change selection.
        self.marker_table.currentCellChanged.connect(lambda *_: self.on_marker_selected())
        list_box.addWidget(self.marker_table)
        queue_actions = QHBoxLayout()
        queue_actions.addWidget(label("Ctrl/Shift — выбрать несколько", "muted"), 1)
        self.add_queue_button = button("Добавить в очередь", tone="primary")
        self.add_queue_button.clicked.connect(self.add_selected_to_queue)
        queue_actions.addWidget(self.add_queue_button)
        list_box.addLayout(queue_actions)
        detail_head = QHBoxLayout()
        detail_head.addWidget(label("Карточка маркера", "section"), 1)
        for title, key, callback, tone in (
            ("Открыть в Svacer", "open_svacer_button", self.open_marker_in_svacer, "primary"),
            ("Изменить поля", "edit_button", self.edit_current_decision, "violet"),
        ):
            action = button(title, tone=tone)
            setattr(self, key, action)
            action.clicked.connect(callback)
            detail_head.addWidget(action)
        detail_box.addLayout(detail_head)
        decision_head = QHBoxLayout()
        decision_head.addStretch(1)
        for title, key, callback, tone in (
            ("Подтвердить черновик", "approve_button", self.approve_current_draft, "success"),
            ("Разметить только этот", "triage_one_button", self.queue_selected_marker, "primary"),
        ):
            action = button(title, tone=tone)
            setattr(self, key, action)
            action.clicked.connect(callback)
            decision_head.addWidget(action)
        detail_box.addLayout(decision_head)
        self.marker_detail = QTextBrowser()
        detail_box.addWidget(self.marker_detail, 1)

    def build_history(self) -> None:
        layout = QVBoxLayout(self.history_tab)
        layout.setContentsMargins(0, 9, 0, 0)
        heading, heading_box = card(sub=True)
        row = QHBoxLayout()
        row.addWidget(label("История анализа маркеров", "section"), 1)
        update = button("Обновить")
        update.clicked.connect(self.populate_history)
        row.addWidget(update)
        row.addWidget(label("Проект", "muted"))
        self.history_project = QComboBox()
        self.history_project.currentIndexChanged.connect(self.populate_history)
        row.addWidget(self.history_project)
        heading_box.addLayout(row)
        layout.addWidget(heading)
        self.history_summary = label("История пока пуста", "muted")
        layout.addWidget(self.history_summary)
        split = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(split, 1)
        history_card, history_box = card()
        detail_card, detail_box = card(sub=True)
        split.addWidget(history_card)
        split.addWidget(detail_card)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.history_table = table(["Запуск", "Проект", "Детектор", "Файл", "Стр.", "Результат", "Время", "Токены"])
        for column, width in enumerate((145, 145, 175, 210, 55, 140, 120, 125)):
            self.history_table.setColumnWidth(column, width)
        self.history_table.itemSelectionChanged.connect(self.on_history_selected)
        history_box.addWidget(self.history_table)
        self.history_detail_title = label("Выберите запуск маркера", "section")
        detail_head = QHBoxLayout()
        detail_head.addWidget(self.history_detail_title, 1)
        self.history_open_button = button("Открыть маркер", tone="primary")
        self.history_open_button.clicked.connect(self.open_history_marker)
        detail_head.addWidget(self.history_open_button)
        detail_box.addLayout(detail_head)
        self.history_detail = QTextBrowser()
        detail_box.addWidget(self.history_detail)

    def build_settings(self) -> None:
        outer = QVBoxLayout(self.settings_tab)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("settingsBody")
        scroll.setWidget(body)
        outer.addWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 9, 0, 0)
        heading, box = card(sub=True)
        box.addWidget(label("Параметры анализа", "section"))
        box.addWidget(label("Вы сами выбираете маркеры на вкладке «Маркеры». «Применить» не запускает анализ.", "muted"))
        layout.addWidget(heading)
        capacity, box = card()
        box.addWidget(label("Параллельность", "section"))
        row = QHBoxLayout()
        row.addWidget(label("Одновременных агентов"))
        self.workers = QSpinBox()
        self.workers.setRange(1, 8)
        row.addWidget(self.workers)
        row.addStretch(1)
        box.addLayout(row)
        self.capacity_hint = label("", "muted")
        box.addWidget(self.capacity_hint)
        self.workers.valueChanged.connect(self.update_capacity_preview)
        layout.addWidget(capacity)
        quota_card, box = card()
        box.addWidget(label("Останавливать при остатке Codex", "section"))
        self.usage_stop_input = QSpinBox()
        self.usage_stop_input.setRange(0, 99)
        self.usage_stop_input.setSuffix(" %")
        self.usage_stop_input.setSpecialValueText("Отключено")
        self.usage_stop_input.setAccessibleName("Останавливать при остатке Codex, процентов")
        box.addWidget(self.usage_stop_input)
        quota_hint = label(
            "Например, 20%: остановить анализ, когда доступно 20% или меньше. 0 — отключено. "
            "Это остаток лимита аккаунта, а не бюджет токенов на запуск. "
            "Проверка перед запуском и каждые 15 секунд, в том числе при закрытом окне. "
            "Изменение действует со следующей проверки; автоматического возобновления нет. "
            "Готовые результаты, черновики и очередь сохраняются. При недоступном лимите анализ останавливается. "
            "Порог не гарантирует точный остаток: учёт расхода может запаздывать. "
            "Более строгая защита активной автоматической кампании остаётся в силе.", "muted")
        quota_hint.setWordWrap(True)
        box.addWidget(quota_hint)
        layout.addWidget(quota_card)
        model_card, box = card()
        box.addWidget(label("Модель Codex для анализа", "section"))
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.addItem("По умолчанию Codex", "")
        model_row.addWidget(self.model_combo, 1)
        self.model_refresh_button = button("Обновить список")
        self.model_refresh_button.clicked.connect(self.refresh_model_catalog)
        model_row.addWidget(self.model_refresh_button)
        box.addLayout(model_row)
        self.model_hint = label(
            "Список моделей загружается из вашего локального Codex. Изменение не прерывает текущий маркер.",
            "muted",
        )
        box.addWidget(self.model_hint)
        layout.addWidget(model_card)
        scope_card, box = card()
        box.addWidget(label("Область разметки этой задачи", "section"))
        from analysis_scope import SCOPE_LABELS
        self.analysis_scope_combo = QComboBox()
        for value, title in SCOPE_LABELS.items():
            self.analysis_scope_combo.addItem(title, value)
        box.addWidget(self.analysis_scope_combo)
        scope_hint = label(
            "Для поставляемого продукта отдельно подтверждённые build-only инструменты получают "
            "Won't fix: вне области. Это не означает, что инструмент безопасен. "
            "Смешанные и неизвестные зависимости проходят обычный анализ.", "muted")
        scope_hint.setWordWrap(True)
        box.addWidget(scope_hint)
        layout.addWidget(scope_card)
        source_card, box = card()
        box.addWidget(label("Исходники проекта", "section"))
        self.source_hint = label("", "muted")
        self.source_hint.setWordWrap(True)
        box.addWidget(self.source_hint)
        self.source_button = button("Выбрать Git-тег / ревизию", tone="primary")
        self.source_button.clicked.connect(self.edit_project_source)
        box.addWidget(self.source_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(source_card)
        save, box = card(sub=True)
        action = button("Применить", tone="success")
        self.settings_apply_button = action
        action.clicked.connect(self.save_execution_settings)
        box.addWidget(action, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(save)
        layout.addStretch(1)
        self.load_settings_form()

    def load_job_options(self) -> None:
        if self.busy:
            return
        self.job_paths = list_saved_jobs(self.tool_directory)
        self.job_combo.blockSignals(True)
        self.job_combo.clear()
        selected = -1
        for index, path in enumerate(self.job_paths):
            try:
                display = job_selector_label(path)
            except (OSError, ValueError):
                display = path.name
            self.job_combo.addItem(display)
            if path.resolve() == self.job:
                selected = index
        self.job_combo.setCurrentIndex(selected)
        self.job_combo.blockSignals(False)
        self.load_history_projects()
        self._jobs_updated_at = 0.0
        if (self._ui_ready and selected < 0 and self.job_paths
                and not (self.job / "job.json").is_file()):
            # Another window can delete/recreate a job. Prefer its replacement
            # for the same project, instead of silently opening another project.
            replacement = next((i for i, path in enumerate(self.job_paths)
                                if job_identity(path)[1] == self.target_name), 0)
            self.job_combo.setCurrentIndex(replacement)

    def load_history_projects(self) -> None:
        previous = self.history_project.currentData()
        self.history_project.blockSignals(True)
        self.history_project.clear()
        self.history_project.addItem("Все проекты", None)
        seen: set[str] = set()
        for path in self.job_paths:
            try:
                _, target = job_identity(path)
            except (OSError, ValueError):
                continue
            if target not in seen:
                self.history_project.addItem(target, target)
                seen.add(target)
        index = self.history_project.findData(previous)
        self.history_project.setCurrentIndex(max(index, 0))
        self.history_project.blockSignals(False)

    def switch_job(self, index: int) -> None:
        if index < 0 or index >= len(self.job_paths):
            return
        if self.busy:
            current = next((i for i, path in enumerate(self.job_paths) if path.resolve() == self.job), -1)
            self.job_combo.blockSignals(True)
            self.job_combo.setCurrentIndex(current)
            self.job_combo.blockSignals(False)
            return
        new_job = self.job_paths[index].resolve()
        if not (new_job / "job.json").is_file():
            self.load_job_options()
            return
        if new_job == self.job:
            return
        self.job = new_job
        self.job_data, self.target_name = job_identity(new_job)
        self.marker_signature = None
        self.history_signature = None
        self._activity_signature = None
        self._activity_entries = []
        self._notification_signature = None
        self._table_signatures.clear()
        self.current_marker_id = None
        self.current_live_id = None
        self.search.clear()
        self.verdict_filter.setCurrentIndex(0)
        self.load_settings_form()
        self.refresh()
        self.set_message(f"Открыта задача {self.target_name}. Предыдущая задача не изменена.")

    def open_job_row(self, _item: QTableWidgetItem) -> None:
        if self.busy:
            return
        row = self.jobs_table.currentRow()
        if row >= 0:
            first = self.jobs_table.item(row, 0)
            path = first.data(Qt.ItemDataRole.UserRole) if first else None
            if path:
                for index, option in enumerate(self.job_paths):
                    if str(option.resolve()) == path:
                        self.job_combo.setCurrentIndex(index)
                        break

    def on_tab_changed(self, index: int) -> None:
        if index == 2:
            self.populate_history()
        elif index == 3 and not self._model_catalog_loaded and self._model_future is None:
            self.refresh_model_catalog()
        if self._tab_animation is not None:
            self._tab_animation.stop()
            self._tab_animation.deleteLater()
            self._tab_animation = None
        if self._animated_tab is not None:
            self._animated_tab.setGraphicsEffect(None)
            self._animated_tab = None
        # Keep the large tables visible immediately; fade only the tiny tab bar.
        current = self.tabs.tabBar()
        effect = QGraphicsOpacityEffect(current)
        current.setGraphicsEffect(effect)
        effect.setOpacity(0.84)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(120)
        animation.setStartValue(0.84)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        def finish() -> None:
            current.setGraphicsEffect(None)
            if self._tab_animation is animation:
                self._tab_animation = None
                self._animated_tab = None
            animation.deleteLater()

        animation.finished.connect(finish)
        self._tab_animation = animation
        self._animated_tab = current
        animation.start()

    def set_preparation_indicator(self, text: str, detail: str = "") -> None:
        if text:
            if text != self._preparation_text:
                self._preparation_frame = 0
            self._preparation_text = text
            self.preparation_indicator.setToolTip(detail)
            self.preparation_indicator.show()
            if self.preparation_movie.isValid():
                self.preparation_timer.stop()
                self.preparation_indicator.setText(text)
                self.preparation_animation.show()
                if self.preparation_motion_enabled:
                    self.preparation_movie.start()
                else:
                    self.preparation_movie.jumpToFrame(0)
            else:
                self.preparation_animation.hide()
                self.advance_preparation_indicator()
                if not self.preparation_timer.isActive():
                    self.preparation_timer.start()
            return
        self.preparation_timer.stop()
        self.preparation_movie.stop()
        self.preparation_animation.hide()
        self._preparation_text = ""
        self._preparation_frame = 0
        self.preparation_indicator.clear()
        self.preparation_indicator.setToolTip("")
        self.preparation_indicator.hide()

    def advance_preparation_indicator(self) -> None:
        if not self._preparation_text:
            return
        frames = ("◐", "◓", "◑", "◒")
        frame = frames[self._preparation_frame % len(frames)]
        self._preparation_frame += 1
        self.preparation_indicator.setText(f"{frame}  {self._preparation_text}")

    def refresh(self) -> None:
        if self.busy:
            return
        if not (self.job / "job.json").is_file():
            self.load_job_options()
            if not (self.job / "job.json").is_file():
                self.show_empty_workspace()
            return
        try:
            state = collect_state(self.job)
            run = read_run_record(self.job)
            state["codex_run"] = run
            self.state = state
            self.job_data, self.target_name = job_identity(self.job)
            self.subtitle.setText(f"{self.target_name}  •  задача {self.job.name}")
            self.setWindowTitle(f"Svacer Triage — {self.target_name}")
            total = int(state.get("total") or 0)
            done = int(state.get("completed") or 0)
            percent = 100 * done / total if total else 0
            self.progress_text.setText(f"Локально проверено: {done} из {total}  •  {percent:.1f}%")
            detail = str(run.get("phase_detail") or run.get("reason") or "")
            preparation = preparation_indicator_text(run)
            self.set_preparation_indicator(preparation, detail)
            if preparation:
                # The exact duration of cloning, trace loading and source checks
                # is unknown, so show an honest indeterminate busy bar.
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, max(total, 1))
                self.progress.setValue(done)
            queue_state = (
                "завершается" if run.get("active") and state.get("paused") else
                "работает" if run.get("active") else
                "ожидает повторного запуска" if run.get("status") in {"failed", "incomplete"} else
                "остановлена" if state.get("paused") else "готова к запуску"
            )
            self.scope.setText(
                f"В снимке {state.get('inventory_total', 0)}  •  "
                f"в Svacer размечено {state.get('already_reviewed', 0)} из {state.get('inventory_total', 0)}  •  "
                f"локально для проверки {total}  •  очередь {queue_state}"
            )
            run_state, run_kind = friendly_run_state(run, state)
            if preparation:
                run_state = "Подготовка"
            event_time = format_run_event_time(run, self.job)
            run_time = f"  •  {event_time}" if event_time else ""
            run_model = (
                f"  •  модель: {run.get('requested_model') or 'по умолчанию Codex'}"
                if "requested_model" in run else ""
            )
            self.run_status.setText(
                f"Фоновая задача: {run_state}" + run_time
                + (f"  •  {detail}" if detail else "") + run_model
            )
            tone = (COLORS["red"] if run_kind == "failed" else
                    COLORS["green"] if run_kind in {"running", "completed"} else COLORS["muted"])
            style = f"color: {tone};"
            if self.run_status.styleSheet() != style:
                self.run_status.setStyleSheet(style)
            usage = run.get("usage") if isinstance(run.get("usage"), dict) else read_codex_usage(self.job)
            total_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            self.usage.setText(f"Фактический расход: {format_count(total_tokens)} токенов")
            from usage_guard import SETTING, display_text
            self.usage_stop_status.setText(display_text(state.get(SETTING, 0)))
            self.refresh_codex_limit_if_needed()
            self.load_markers()
            self.apply_job_rows(
                [tuple(self.jobs_table.item(row, col).text()
                       for col in range(self.jobs_table.columnCount()))
                 for row in range(self.jobs_table.rowCount())],
                [str(self.jobs_table.item(row, 0).data(Qt.ItemDataRole.UserRole))
                 for row in range(self.jobs_table.rowCount())],
            )
            self.populate_jobs()
            self.populate_work_queue()
            if self.tabs.currentIndex() == 2:
                self.populate_history()
            self.update_action_states()
            self.refresh_notifications()
            if state.get("errors"):
                self.set_message("; ".join(str(item) for item in state["errors"]), error=True)
        except Exception as exc:
            self.set_message(f"Ошибка чтения состояния: {exc}", error=True)

    def refresh_codex_limit_if_needed(self) -> None:
        if self._codex_limit_future is not None or time.monotonic() - self._codex_limit_updated_at < 60:
            return
        self._codex_limit_updated_at = time.monotonic()
        self._codex_limit_future = self._codex_limit_executor.submit(read_codex_rate_limits)
        self.codex_limit_poll_timer.start()

    def drain_codex_limit(self) -> None:
        future = self._codex_limit_future
        if future is None or not future.done():
            return
        self.codex_limit_poll_timer.stop()
        self.model_poll_timer.stop()
        self._codex_limit_future = None
        try:
            data = future.result()
        except Exception:
            data = None
        text, color = format_codex_limit(data)
        self.codex_limit.setText(text)
        style = f"color: {color};"
        if self.codex_limit.styleSheet() != style:
            self.codex_limit.setStyleSheet(style)

    def load_markers(self) -> None:
        inventory_path = self.job / "markers.inventory.json"
        decisions_path = self.job / "decisions.jsonl"
        notes = self.job / "notes"
        paths = [inventory_path, decisions_path, self.job / "incomplete-analysis.json"]
        if notes.is_dir():
            paths.extend(sorted(notes.glob("batch-*-worker-*.json")))
        raw = self.job / "raw"
        if raw.is_dir():
            paths.extend(sorted(raw.glob("*.json")))
        signature = tuple(path.stat().st_mtime_ns if path.exists() else 0 for path in paths)
        if signature == self.marker_signature:
            # Queue/run state can change without any marker or decision file changing.
            self.populate_markers()
            return
        self.marker_signature = signature
        inventory = read_json(inventory_path) if inventory_path.exists() else {}
        values = inventory.get("markers") if isinstance(inventory, dict) else []
        self.inventory = {str(item["id"]): item for item in values or [] if isinstance(item, dict) and item.get("id")}
        self.traces = {}
        if raw.is_dir():
            for path in sorted(raw.glob("*.json")):
                try:
                    payload = read_json(path)
                except (OSError, json.JSONDecodeError):
                    continue
                for item in payload.get("markers") or []:
                    if isinstance(item, dict) and item.get("id"):
                        self.traces[str(item["id"])] = item
        self.decisions = load_decisions(decisions_path) if decisions_path.exists() else []
        self.decision_by_id = {str(item["marker_id"]): item for item in self.decisions if item.get("marker_id")}
        self.drafts = unapplied_draft_results(self.job, self.decisions)
        self.populate_markers()

    def populate_jobs(self) -> None:
        if self._jobs_future is not None or time.monotonic() - self._jobs_updated_at < 10:
            return
        self._jobs_future = self._job_executor.submit(scan_job_rows, list(self.job_paths))
        self.jobs_poll_timer.start()

    def drain_jobs(self) -> None:
        future = self._jobs_future
        if future is None or not future.done():
            return
        self.jobs_poll_timer.stop()
        self._jobs_future = None
        self._jobs_updated_at = time.monotonic()
        try:
            rows, keys = future.result()
        except Exception as exc:
            self.status.setText(f"Не удалось прочитать список задач: {exc}")
            return
        self.apply_job_rows(rows, keys)

    def apply_job_rows(self, rows: list[tuple[Any, ...]], keys: list[str]) -> None:
        """Keep the selected job consistent with the overview, even after a stale scan."""
        valid = {str(path.resolve()) for path in self.job_paths}
        pairs = [(row, key) for row, key in zip(rows, keys) if key in valid]
        rows, keys = [p[0] for p in pairs], [p[1] for p in pairs]
        current_key = str(self.job.resolve())
        if self.job in self.job_paths or current_key in keys:
            status, _ = friendly_run_state(self.state.get("codex_run") or {}, self.state)
            current_row = (
                f"{self.target_name} · {compact_job_timestamp(self.job)}", status,
                f"{self.state.get('already_reviewed', 0)}/{self.state.get('inventory_total', 0)}",
                f"{self.state.get('completed', 0)}/{self.state.get('total', 0)}",
                len(active_marker_ids(self.state)),
            )
            if current_key in keys:
                rows[keys.index(current_key)] = current_row
            else:
                keys.insert(0, current_key)
                rows.insert(0, current_row)
        signature = tuple((key, *(str(value) for value in row)) for key, row in zip(keys, rows))
        if self._table_signatures.get("jobs") != signature:
            set_rows(self.jobs_table, rows, keys)
            self._table_signatures["jobs"] = signature

    def closeEvent(self, event: Any) -> None:
        if self.import_applying and self._task_future is not None and not self._task_future.done():
            event.ignore()
            self.set_message("Дождитесь завершения отправки и обратной проверки Svacer.", error=True)
            return
        self.timer.stop()
        self.jobs_poll_timer.stop()
        self.task_poll_timer.stop()
        self.connection_timer.stop()
        self.connection_poll_timer.stop()
        self.codex_limit_poll_timer.stop()
        if self.live_dialog is not None:
            self.live_dialog.close()
        if self.login_dialog is not None:
            self.login_dialog.shutdown()
        self._job_executor.shutdown(wait=False, cancel_futures=True)
        self._task_executor.shutdown(wait=False, cancel_futures=True)
        self._connection_executor.shutdown(wait=False, cancel_futures=True)
        self._codex_limit_executor.shutdown(wait=False, cancel_futures=True)
        self._model_executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)

    def populate_work_queue(self) -> None:
        assignments = marker_assignments(self.state)
        saved = saved_result_assignments(self.state)
        displayed = {**assignments, **saved}
        active_ids = sorted(displayed, key=displayed.__getitem__)
        triage_ids = [str(marker["id"]) for marker in markers_for_triage(list(self.inventory.values()))]
        queued_ids = current_run_queue_ids(self.decisions, self.state, set(self.drafts), triage_ids)
        active_rows = []
        for index, marker_id in enumerate(active_ids, 1):
            marker = self.decision_by_id.get(marker_id) or self.inventory.get(marker_id, {})
            status = "Результат сохранён" if marker_id in saved else "В работе"
            active_rows.append((displayed[marker_id], status, marker.get("warnClass") or "—",
                                short_file(marker.get("file")), marker.get("line") or "—"))
        queue_rows = []
        queue_details = []
        for index, marker_id in enumerate(queued_ids, 1):
            marker = self.decision_by_id.get(marker_id) or self.inventory.get(marker_id, {})
            status, detail = queued_marker_status(self.state, marker_id)
            queue_details.append(detail)
            queue_rows.append((index, status,
                               marker.get("warnClass") or "—", short_file(marker.get("file")), marker.get("line") or "—"))
        for name, widget, rows, ids in (
            ("active", self.active_table, active_rows, active_ids),
            ("queue", self.queue_table, queue_rows, queued_ids),
        ):
            signature = tuple((marker_id, *row) for marker_id, row in zip(ids, rows))
            if name == "queue":
                signature = (signature, tuple(queue_details))
            if self._table_signatures.get(name) != signature:
                set_rows(widget, rows, ids)
                if name == "queue":
                    color_status_cells(widget, [str(row[1]) for row in rows], column=1)
                    for row_index, detail in enumerate(queue_details):
                        widget.item(row_index, 1).setToolTip(detail)
                else:
                    color_status_cells(widget, [str(row[1]) for row in rows], column=1)
                    for row_index, marker_id in enumerate(ids):
                        widget.item(row_index, 1).setToolTip(
                            SAVED_RESULT_DETAIL if marker_id in saved else f"В работе · {assignments[marker_id]}"
                        )
                self._table_signatures[name] = signature
        self.active_title.setText(
            f"Текущая партия — в работе: {len(assignments)} • результат сохранён: {len(saved)}"
            if saved else
            f"В работе — исполнителей: {len(set(assignments.values()))} • назначено маркеров: {len(assignments)}"
        )
        run = self.state.get("codex_run") or {}
        if run.get("active"):
            prefix = "В очереди текущего запуска"
        elif self.state.get("priority_marker_ids"):
            prefix = "Выбрано к запуску"
        else:
            prefix = "В очереди"
        title = f"{prefix} — {len(queued_ids)}"
        incomplete_count = sum(row[1] == "Доисследовать" for row in queue_rows)
        if incomplete_count:
            title = f"Очередь и доисследование — {len(queued_ids)} · доисследовать: {incomplete_count}"
        if not queued_ids and not active_ids:
            title += " · Выберите маркеры на вкладке «Маркеры»"
        self.queue_title.setText(title)
        if self.current_live_id not in active_ids + queued_ids:
            self.current_live_id = (active_ids + queued_ids)[0] if active_ids or queued_ids else None
        self.sync_live_selection(active_ids, queued_ids)
        self.update_remove_queue_button()
        self.render_live()

    def sync_live_selection(self, active_ids: list[str], queued_ids: list[str]) -> None:
        """Only the table owning the displayed marker may keep a selection."""
        for widget, ids in ((self.active_table, active_ids), (self.queue_table, queued_ids)):
            target = ids.index(self.current_live_id) if self.current_live_id in ids else -1
            selected = widget.selectionModel().selectedRows(0)
            current = selected[0].row() if selected else -1
            if widget is self.queue_table and target >= 0 and any(
                index.row() == target for index in selected
            ):
                continue
            if current == target:
                continue
            blocked = widget.blockSignals(True)
            try:
                if target < 0:
                    widget.clearSelection()
                else:
                    widget.selectRow(target)
            finally:
                widget.blockSignals(blocked)

    def on_active_selected(self) -> None:
        self.select_live_from(self.active_table)
        self.update_remove_queue_button()

    def on_queue_selected(self) -> None:
        self.select_live_from(self.queue_table)
        self.update_remove_queue_button()

    def selected_queue_ids(self) -> list[str]:
        ids = []
        for index in sorted(self.queue_table.selectionModel().selectedRows(0),
                            key=lambda value: value.row()):
            item = self.queue_table.item(index.row(), 0)
            if item is not None:
                ids.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return ids

    def update_remove_queue_button(self) -> None:
        count = len(self.selected_queue_ids())
        self.remove_queue_button.setText(
            f"Убрать из очереди ({count})" if count else "Убрать из очереди"
        )
        self.remove_queue_button.setEnabled(bool(count) and not self.busy)

    def remove_selected_from_queue(self) -> None:
        ids = self.selected_queue_ids()
        if not ids:
            self.set_message("Выберите маркер в очереди.", error=True)
            return
        answer = QMessageBox.question(
            self, "Убрать из очереди",
            f"Убрать выбранные маркеры из очереди ({len(ids)})? "
            "Сохранённые решения и черновики останутся; уже назначенная работа продолжится.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = dequeue_marker_ids(self.job / "decisions.jsonl", ids)
        except (SystemExit, OSError, json.JSONDecodeError) as exc:
            self.set_message(f"Не удалось изменить очередь: {exc}", error=True)
            self.refresh()
            return
        self._table_signatures.pop("queue", None)
        self.refresh()
        self.set_message(
            f"Убрано из очереди: {len(result['removed'])}. "
            "Сохранённые решения и черновики не изменены."
        )

    def select_live_from(self, widget: QTableWidget) -> None:
        row = widget.currentRow()
        item = widget.item(row, 0) if row >= 0 else None
        marker_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if marker_id:
            self.current_live_id = str(marker_id)
            other = self.queue_table if widget is self.active_table else self.active_table
            if other.selectionModel().hasSelection():
                blocked = other.blockSignals(True)
                try:
                    other.clearSelection()
                finally:
                    other.blockSignals(blocked)
            self.render_live()

    def render_live(self) -> None:
        marker_id = self.current_live_id
        self.expand_live_button.setEnabled(bool(marker_id))
        if not marker_id:
            self.live_title.setText("Выберите маркер")
            self.live_meta.setText("Нет маркеров в работе или очереди")
            self.live_text.clear()
            return
        marker = self.decision_by_id.get(marker_id) or self.inventory.get(marker_id, {})
        self.live_title.setText(f"{marker.get('warnClass') or 'Маркер'} — {short_file(marker.get('file'))}:{marker.get('line') or '—'}")
        assignments = marker_assignments(self.state)
        saved = saved_result_assignments(self.state)
        runtime_worker = (self.state.get("worker_runtime") or {}).get("workers", {}).get(marker_id)
        run = self.state.get("codex_run") or {}
        if runtime_worker and run.get("active") and run.get("phase") != "verification":
            states = {"preparing": "Подготовка маркера", "starting": "Запуск исполнителя", "running": "В работе",
                      "sources": "Получение дополнительных исходников", "validating": "Проверка доказательств",
                      "verifying": "Независимая проверка Confirmed", "applied": "Сохранено в результатах и истории",
                      "finished": "Результат сохранён; завершается запись итогов",
                      "incomplete": "Доисследовать — доказательств пока недостаточно"}
            status = f"{states.get(runtime_worker.get('state'), 'Ожидает')} • Агент {runtime_worker.get('worker')}"
            event_file = runtime_worker.get("event_log", "codex-events.jsonl")
            if runtime_worker.get("state") == "running":
                status += " • " + latest_codex_activity(self.job, event_file)
            timing, _ = live_run_timing(self.job, run, worker=runtime_worker)
            if runtime_worker.get("finished_at"):
                timing = f"Время исследования: {format_history_seconds(runtime_worker.get('duration_seconds'))}"
            try:
                stat = (self.job / event_file).stat()
                signature = (marker_id, stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = (marker_id, None)
            if signature != self._activity_signature:
                self._activity_entries = codex_activity_entries(self.job, event_file=event_file, include_steps=True)
                self._activity_signature = signature
            shown = self._activity_entries
            scope = "Индивидуальный поток выбранного маркера. Время обновляется только по событиям его исполнителя."
            if runtime_worker.get("error"):
                scope += " " + str(runtime_worker["error"])
        elif marker_id in assignments:
            run = self.state.get("codex_run") or {}
            agent = assignments[marker_id]
            agent_ids = [key for key, value in assignments.items() if value == agent]
            position = agent_ids.index(marker_id) + 1
            phase = str(run.get("phase_detail") or latest_codex_activity(self.job))
            status = f"В работе • {agent} • маркер {position} из {len(agent_ids)} • {phase}"
            timing, _ = live_run_timing(self.job, run)
            event_file = self.job / "codex-events.jsonl"
            try:
                stat = event_file.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = None
            if signature != self._activity_signature:
                self._activity_entries = codex_activity_entries(self.job)
                self._activity_signature = signature
            entries = self._activity_entries
            scope = (
                "Эта партия содержит один маркер: сообщения ниже относятся к нему."
                if len(assignments) == 1 else
                f"Общий поток и таймер старого запуска: назначено {len(assignments)} маркеров. "
                "Число назначений не подтверждает число параллельно работающих процессов."
            )
            shown = entries
        elif marker_id in saved:
            status = f"Результат сохранён • {saved[marker_id]} • завершается запись итогов"
            timing, scope, shown = "", SAVED_RESULT_DETAIL, []
        elif marker_id in self.drafts:
            draft = self.drafts[marker_id]
            status = (
                "Не завершён • требуется продолжение исследования"
                if draft.get("analysis_status") == "needs_context" or draft.get("verdict") == "Unclear"
                else f"Черновик сохранён • {draft.get('verdict')}"
            )
            timing, scope, shown = "", "Индивидуальный черновик находится в карточке маркера.", []
        else:
            queued_ids = current_run_queue_ids(
                self.decisions, self.state, set(self.drafts),
                [str(item["id"]) for item in markers_for_triage(list(self.inventory.values()))],
            )
            status = (f"В очереди • позиция {queued_ids.index(marker_id) + 1} из {len(queued_ids)}"
                      if marker_id in queued_ids else "Ожидает анализа")
            timing, scope, shown = "", "Маркер ещё не выполняется: индивидуальных сообщений по нему пока нет.", []
        saved_result = self.drafts.get(marker_id, {}) if marker_id in saved else {}
        if saved_result.get("verdict") in VALID_VERDICTS:
            status += f" • {saved_result['verdict']}"
        self.live_meta.setText(f"{status} • ID {marker_id}" + (f"\n{timing}" if timing else ""))
        message = marker.get("msg") or self.inventory.get(marker_id, {}).get("msg") or "Анализ ещё не завершён."
        shown_html = "".join(
            f"<p style='color:{COLORS['violet']}'><b>{index:02d}</b> {escape(entry)}</p>"
            for index, entry in enumerate(shown, 1)
        )
        html = (
            f"<p style='color:{COLORS['amber']}'>{escape(scope)}</p>"
            + (shown_html or f"<p style='color:{COLORS['muted']}'>Пока нет сообщений агента.</p>")
            + (f"<h3 style='color:{COLORS['blue']}'>Сохранённый результат</h3>"
               f"<p>{escape(comment_without_heading(saved_result.get('comment')) or 'Комментарий доступен в карточке маркера.')}</p>"
               f"<p style='color:{COLORS['muted']}'>{escape(SAVED_RESULT_DETAIL)}</p>"
               if marker_id in saved else "")
            + f"<h3 style='color:{COLORS['blue']}'>Описание Svacer</h3><p>{escape(str(message))}</p>"
        )
        if self._live_html != html:
            scroll = self.live_text.verticalScrollBar()
            old_position = scroll.value()
            follow = scroll.maximum() - old_position <= 4
            self.live_text.setHtml(html)
            scroll.setValue(scroll.maximum() if follow else min(old_position, scroll.maximum()))
            self._live_html = html
        if self.live_dialog is not None and self.live_dialog.isVisible():
            self.live_dialog.setWindowTitle(self.live_title.text())
            self.live_dialog_meta.setText(self.live_meta.text())
            if self._live_dialog_html != html:
                scroll = self.live_dialog_text.verticalScrollBar()
                old_position = scroll.value()
                follow = scroll.maximum() - old_position <= 4
                self.live_dialog_text.setHtml(html)
                scroll.setValue(scroll.maximum() if follow else min(old_position, scroll.maximum()))
                self._live_dialog_html = html

    def open_live_monitor(self, _item: Any = None) -> None:
        if not self.current_live_id:
            return
        if self.live_dialog is None:
            dialog = QDialog(self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(18, 14, 18, 14)
            self.live_dialog_meta = label("", "muted")
            layout.addWidget(self.live_dialog_meta)
            self.live_dialog_text = QTextBrowser()
            layout.addWidget(self.live_dialog_text, 1)
            self.live_dialog = dialog
        self.live_dialog.setWindowTitle(self.live_title.text())
        self.live_dialog_meta.setText(self.live_meta.text())
        self.live_dialog_text.setHtml(self._live_html)
        self._live_dialog_html = self._live_html
        self.live_dialog.showMaximized()
        self.live_dialog.raise_()

    def open_live_marker_card(self, _item: Any = None) -> None:
        if not self.current_live_id:
            return
        self.current_marker_id = self.current_live_id
        self.tabs.setCurrentWidget(self.markers_tab)
        for row in range(self.marker_table.rowCount()):
            item = self.marker_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == self.current_marker_id:
                self.marker_table.selectRow(row)
                self.marker_table.scrollToItem(item)
                break
        self.render_marker()

    def marker_queue_states(self) -> dict[str, tuple[str, str]]:
        """Use the same effective FIFO and live assignments as the Overview tab."""
        assignments = marker_assignments(self.state)
        waiting = current_run_queue_ids(self.decisions, self.state, set(self.drafts), list(self.inventory))
        rechecks = set(self.state.get("recheck_marker_ids") or [])
        states = {}
        for index, mid in enumerate(waiting, 1):
            status, status_detail = queued_marker_status(self.state, mid)
            if status not in {"Ожидает", "Перепроверка"}:
                states[mid] = (status, status_detail)
                continue
            detail = f"В очереди · позиция {index} из {len(waiting)}"
            if mid in rechecks:
                detail += " · перепроверка"
            states[mid] = ("В очереди", detail)
        for mid, agent in assignments.items():
            states[mid] = ("В работе", f"В работе · {agent}")
        for mid, agent in saved_result_assignments(self.state).items():
            states[mid] = ("Результат сохранён", f"{SAVED_RESULT_DETAIL} · {agent}")
        return states

    def populate_markers(self) -> None:
        if not hasattr(self, "marker_table"):
            return
        selected_filter = FILTERS.get(self.verdict_filter.currentText(), "all")
        query = self.search.text().strip().casefold()
        in_work = active_marker_ids(self.state)
        queue_states = self.marker_queue_states()
        rows = []
        keys = []
        queue_details = []
        for decision in self.decisions:
            marker_id = str(decision.get("marker_id") or "")
            marker = self.inventory.get(marker_id, {})
            draft = self.drafts.get(marker_id, {})
            verdict = decision.get("verdict")
            review_status = marker_review_status(marker)
            if not marker_matches_filter(selected_filter, marker_id, verdict, bool(draft), in_work, review_status):
                continue
            haystack = " ".join(str(value or "") for value in (
                marker_id, decision.get("warnClass"), decision.get("file"), marker.get("msg"),
                decision.get("comment"), draft.get("comment"),
            )).casefold()
            if query and query not in haystack:
                continue
            status = str(verdict or (
                "Не завершён" if draft.get("analysis_status") == "needs_context" or draft.get("verdict") == "Unclear" else
                "Черновик" if draft else "Ожидает"
            ))
            if not verdict and not draft and review_status != "Undecided":
                status = f"{review_status} · Svacer"
            queue_status, queue_detail = queue_states.get(marker_id, ("Не в очереди", "Не в очереди"))
            rows.append((status, queue_status, decision.get("warnClass") or "—", short_file(decision.get("file")), decision.get("line") or "—"))
            keys.append(marker_id)
            queue_details.append(queue_detail)
        signature = tuple((marker_id, *row, detail) for marker_id, row, detail in zip(keys, rows, queue_details))
        if self._table_signatures.get("markers") != signature:
            set_rows(self.marker_table, rows, keys)
            color_status_cells(self.marker_table, [str(row[0]) for row in rows])
            color_status_cells(self.marker_table, [str(row[1]) for row in rows], column=1)
            for index, detail in enumerate(queue_details):
                self.marker_table.item(index, 1).setToolTip(detail)
            self._table_signatures["markers"] = signature
        self.marker_count.setText(f"Показано: {len(rows)}")
        self.tabs.setTabText(1, f"Маркеры  {len(self.decisions)}")
        if self.current_marker_id not in keys:
            self.current_marker_id = keys[0] if keys else None
        self.render_marker()

    def on_marker_selected(self) -> None:
        row = self.marker_table.currentRow()
        item = self.marker_table.item(row, 0) if row >= 0 else None
        if item:
            self.current_marker_id = str(item.data(Qt.ItemDataRole.UserRole))
            self.render_marker()
            self.update_action_states()

    def selected_marker_ids(self) -> list[str]:
        ids = []
        for index in sorted(self.marker_table.selectionModel().selectedRows(0), key=lambda value: value.row()):
            item = self.marker_table.item(index.row(), 0)
            if item is not None:
                ids.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return ids

    def add_selected_to_queue(self) -> None:
        ids = self.selected_marker_ids()
        if not ids:
            self.set_message("Выберите один или несколько маркеров в таблице.", error=True)
            return
        running = bool(read_run_record(self.job).get("active"))
        rechecks = [mid for mid in ids if (self.decision_by_id.get(mid, {}).get("verdict") in VALID_VERDICTS
                    or marker_review_status(self.inventory.get(mid, {})) != "Undecided")
                    and mid not in set(self.state.get("priority_marker_ids") or [])]
        if rechecks:
            answer = QMessageBox.question(
                self, "Добавить перепроверку",
                f"Добавить {len(rechecks)} готовых решений на перепроверку? "
                "Старая разметка Svacer останется без изменений. При назначении будет очищено "
                "только прежнее локальное решение, если оно есть.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            result = enqueue_marker_ids(
                self.job / "markers.inventory.json", self.job / "decisions.jsonl", ids,
            )
        except (SystemExit, OSError, json.JSONDecodeError) as exc:
            self.set_message(f"Не удалось изменить очередь: {exc}", error=True)
            return
        self._table_signatures.pop("queue", None)
        self.refresh()
        if result["added"]:
            next_step = (
                "Текущий маркер продолжит работу; выбранные маркеры пойдут следом."
                if running else
                "Анализ не запущен — нажмите «Начать анализ» на вкладке «Обзор»."
            )
            self.set_message(
                f"Добавлено в очередь: {len(result['added'])}; перепроверок: {len(result['rechecks'])}. "
                + next_step
            )
        elif result["already_active"]:
            self.set_message("Выбранный маркер уже в работе. Выберите другой маркер для очереди.")
        else:
            self.set_message("Выбранные маркеры уже есть в очереди.")

    def render_marker(self) -> None:
        marker_id = self.current_marker_id
        if not marker_id:
            message = ("Маркеры ещё не загружены. Нажмите «Получить маркеры»."
                       if not (self.job / "markers.inventory.json").is_file()
                       else "По выбранному фильтру ничего не найдено.")
            self.marker_detail.setHtml(f"<p>{message}</p>")
            self._marker_html = None
            return
        decision = self.decision_by_id.get(marker_id, {})
        marker = self.inventory.get(marker_id, {})
        traced = self.traces.get(marker_id, marker)
        draft = self.drafts.get(marker_id, {})
        result = decision if decision.get("verdict") else draft or decision
        in_work = marker_id in active_marker_ids(self.state)
        verdict = decision.get("verdict") or (
            "Не завершён" if draft.get("analysis_status") == "needs_context" or draft.get("verdict") == "Unclear"
            else f"Черновик: {draft.get('verdict')}" if draft
            else "В работе" if in_work else "Ожидает анализа"
        )
        title = escape(str(result.get("warnClass") or marker.get("warnClass") or "Маркер"))
        meta = escape(
            f"{short_file(result.get('file') or marker.get('file'))}:"
            f"{result.get('line') or marker.get('line') or '—'} • {verdict} • ID {marker_id}"
        )
        chunks = [f"<h2>{title}</h2><p style='color:{COLORS['muted']}'>{meta}</p>"]
        def section(heading: str, value: Any, *, always: bool = False) -> None:
            if value or always:
                body = escape(str(value or "—")).replace("\n", "<br>")
                chunks.append(f"<h3 style='color:{COLORS['blue']}'>{escape(heading)}</h3><p>{body}</p>")

        _, queue_detail = self.marker_queue_states().get(marker_id, ("Не в очереди", "Не в очереди"))
        section("Очередь", queue_detail)
        section("Описание Svacer", marker.get("msg") or traced.get("msg"))
        remote_status = marker_review_status(marker)
        if remote_status != "Undecided":
            section("Разметка Svacer при загрузке", remote_status +
                    ". Это прежняя разметка, а не результат локальной перепроверки.")
        if marker.get("function") or marker.get("mtid"):
            section("Контекст", f"Функция: {marker.get('function') or '—'}\nMTID: {marker.get('mtid') or '—'}")
        traces = traced.get("traces") if isinstance(traced, dict) else None
        if isinstance(traces, list) and traces:
            trace_lines = []
            for trace in traces:
                if not isinstance(trace, dict):
                    continue
                trace_lines.append(f"{trace.get('role') or 'роль'}:")
                for location in trace.get("locations") or []:
                    if not isinstance(location, dict):
                        continue
                    col = f":{location['col']}" if location.get("col") else ""
                    trace_lines.append(
                        f"  • {short_file(location.get('file'))}:"
                        f"{location.get('line') or '—'}{col} — {location.get('info') or ''}"
                    )
            section("Трасса", "\n".join(trace_lines))
        else:
            section("Трасса", "Полная трасса будет загружена перед анализом этого маркера.")
        if (result.get("analysis_status") == "needs_context" or result.get("verdict") == "Unclear") and not decision.get("verdict"):
            section("Продолжение анализа",
                    "Проверка остановилась без окончательного вердикта: не хватает доказательств. "
                    "Исходники и черновик сохранены. Маркер можно оставить в очереди и нажать «Начать анализ» "
                    "после завершения текущего запуска. Готовые результаты других маркеров сохраняются отдельно.", always=True)
            section("Не завершён — требуется продолжение исследования", list_text(result.get("proof_gaps")), always=True)
        elif result.get("verdict"):
            if draft and not decision.get("verdict"):
                section("Статус черновика", "Результат сохранён, но партия остановлена до применения. Он не будет отправлен в Svacer.")
            for heading, value in (
                ("Точка входа", result.get("entrypoint")), ("Source", result.get("source")),
                ("Проверки и ограничения", result.get("control")), ("Sink", result.get("sink")),
                ("Достижимость в сборке", result.get("build_reachability")),
                ("Достижимость в продукте", result.get("product_reachability")),
                ("Влияние", result.get("impact")),
                ("Доказательства", list_text(result.get("evidence"))),
                ("Контраргументы", list_text(result.get("counterevidence"))),
            ):
                section(heading, value)
            if result.get("verdict") == "Confirmed":
                section("Поля Svacer", f"Severity: {result.get('severity') or '—'}\nДействие: {result.get('action') or '—'}")
            section("Комментарий для Svacer", comment_without_heading(result.get("comment")))
        else:
            section("Статус", "Маркер назначен агенту и сейчас анализируется." if in_work else "Локальный анализ ещё не выполнялся.")
        new_html = "".join(chunks)
        if getattr(self, "_marker_html", None) != new_html:
            self.marker_detail.setHtml(new_html)
            self._marker_html = new_html

    def populate_history(self) -> None:
        if not hasattr(self, "history_table"):
            return
        chosen = self.history_project.currentData()
        paths = []
        for path in self.job_paths:
            try:
                _, target = job_identity(path)
            except (OSError, ValueError):
                continue
            if chosen is None or target == chosen:
                paths.append(path)
        signature = (chosen, tuple(
            (str(path), tuple(
                (path / name).stat().st_mtime_ns if (path / name).exists() else 0
                for name in ("marker-history.jsonl", "decisions.jsonl")
            )) for path in paths
        ))
        if signature == self.history_signature:
            return
        self.history_signature = signature
        records = []
        for path in paths:
            try:
                _, target = job_identity(path)
                for record in load_marker_history(path):
                    value = dict(record)
                    value["_target"] = target
                    value["_job_path"] = str(path.resolve())
                    records.append(value)
            except (OSError, ValueError):
                continue
        records.sort(key=lambda value: str(value.get("started_at") or ""), reverse=True)
        self.history_records = records
        rows = []
        exact_measurements = 0
        shared_measurements = 0
        missing_measurements = 0
        for position, record in enumerate(records):
            previous = previous_history_attempt(records, position)
            measurement = history_measurements(record, previous)
            duration_text, token_text = history_measurement_cells(measurement)
            if measurement.get("tokens") is None:
                missing_measurements += 1
            elif str(measurement.get("token_scope") or "").startswith("Точный"):
                exact_measurements += 1
            else:
                shared_measurements += 1
            rows.append((
                str(record.get("started_at") or "—")[:16].replace("T", " "),
                record.get("_target") or "—", record.get("warnClass") or "—",
                short_file(record.get("file")), record.get("line") or "—",
                record.get("verdict") or ("Ошибка" if record.get("status") == "failed" else "Не завершён"),
                duration_text,
                token_text,
            ))
        set_rows(self.history_table, rows)
        color_status_cells(self.history_table, [str(row[5]) for row in rows], column=5)
        self.history_summary.setText(
            f"Записей: {len(records)} • точный расход: {exact_measurements} • "
            f"общий расход партии: {shared_measurements} • без замера: {missing_measurements}"
        )
        self.on_history_selected()

    def on_history_selected(self) -> None:
        row = self.history_table.currentRow()
        if row < 0 or row >= len(self.history_records):
            self.history_detail_title.setText("Выберите запуск маркера")
            self.history_detail.setPlainText("История появится после завершения анализа маркера.")
            self.history_open_button.setEnabled(False)
            return
        record = self.history_records[row]
        previous = previous_history_attempt(self.history_records, row)
        comparison = compare_history_attempts(previous, record)
        measurement = history_measurements(record, previous)
        duration_text, token_text = history_measurement_cells(measurement)
        self.history_detail_title.setText(
            f"{record.get('warnClass') or 'Маркер'} — {short_file(record.get('file'))}:{record.get('line') or '—'}"
        )
        measured_record = measurement.get("source_record") if isinstance(measurement.get("source_record"), dict) else record
        usage = measured_record.get("batch_usage") if isinstance(measured_record.get("batch_usage"), dict) else {}
        chunks = []
        for heading, value in (
            ("ID", record.get("marker_id") or "—"),
            ("Проект", f"{record.get('_target') or '—'} • задача {record.get('job_id') or '—'}"),
            ("Общий запуск", f"{str(record.get('started_at') or '—')[:19].replace('T', ' ')} • исполнитель {record.get('worker') or '—'}"),
            ("Выбор модели", record.get("requested_model") or "по умолчанию Codex"),
            ("Результат", record.get("verdict") or record.get("status") or "—"),
            ("Время", f"{duration_text} • {measurement.get('duration_scope')}"),
            ("Токены", f"{token_text} • {measurement.get('token_scope')}"),
            ("Общий расход исходного запуска", f"маркеров {measured_record.get('batch_marker_count') or 1} • "
             f"всего {format_count(int(measured_record.get('batch_total_tokens') or 0))} • "
             f"вход {format_count(int(usage.get('input_tokens') or 0))} • "
             f"кэш {format_count(int(usage.get('cached_input_tokens') or 0))} • "
             f"выход {format_count(int(usage.get('output_tokens') or 0))}"),
        ):
            chunks.append(
                f"<p><span style='color:{COLORS['muted']}'>{escape(heading)}:</span> "
                f"{escape(str(value))}</p>"
            )
        if measurement.get("inherited_from_previous"):
            chunks.append(
                f"<p style='color:{COLORS['blue']}'>Локальная повторная проверка не запускала модель; "
                "показаны измерения исходной попытки.</p>"
            )
        elif int(measured_record.get("batch_marker_count") or 1) > 1:
            chunks.append(
                f"<p style='color:{COLORS['amber']}'>Время измерено до сохранения результата этим агентом. "
                "Токены относятся ко всей параллельной партии и не делятся между маркерами приблизительно.</p>"
            )

        chunks.append(f"<h3 style='color:{COLORS['blue']}'>Сравнение с предыдущей попыткой</h3>")
        if previous is None:
            chunks.append(f"<p style='color:{COLORS['muted']}'>Предыдущей попытки для этого маркера нет.</p>")
        else:
            previous_snapshot = previous.get("decision_snapshot") if isinstance(previous.get("decision_snapshot"), dict) else {}
            current_snapshot = record.get("decision_snapshot") if isinstance(record.get("decision_snapshot"), dict) else {}
            previous_verdict = previous.get("verdict") or previous_snapshot.get("verdict") or "—"
            current_verdict = record.get("verdict") or current_snapshot.get("verdict") or "—"
            chunks.append(
                f"<p><span style='color:{COLORS['muted']}'>Попытки:</span> "
                f"{escape(str(previous.get('started_at') or '—')[:19].replace('T', ' '))} → "
                f"{escape(str(record.get('started_at') or '—')[:19].replace('T', ' '))}</p>"
            )
            chunks.append(
                f"<p><span style='color:{COLORS['muted']}'>Статус:</span> "
                f"{escape(str(previous.get('status') or '—'))} → {escape(str(record.get('status') or '—'))}</p>"
            )
            chunks.append(
                f"<p><span style='color:{COLORS['muted']}'>Вердикт:</span> "
                f"{escape(str(previous_verdict))} → {escape(str(current_verdict))}</p>"
            )
            if comparison.get("decision_unchanged"):
                chunks.append(
                    f"<p style='color:{COLORS['green']}'>Вердикт и доказательная часть не изменились; "
                    "сохранённый черновик только прошёл строгую повторную проверку.</p>"
                )
            elif comparison.get("changes"):
                chunks.append(f"<p style='color:{COLORS['amber']}'>Изменённые поля:</p><ul>")
                for change in comparison["changes"]:
                    chunks.append(
                        f"<li><b>{escape(str(change['label']))}</b><br>"
                        f"Было: {escape(str(change['before']))}<br>"
                        f"Стало: {escape(str(change['after']))}</li>"
                    )
                chunks.append("</ul>")
            else:
                chunks.append(f"<p style='color:{COLORS['muted']}'>Для старой попытки детальная копия решения не сохранилась.</p>")
        chunks.append(f"<h3 style='color:{COLORS['blue']}'>Сообщения агента</h3>")
        messages = record.get("agent_messages") if isinstance(record.get("agent_messages"), list) else []
        if record.get("messages_scope") == "batch" and messages:
            chunks.append(f"<p style='color:{COLORS['amber']}'>Общий поток параллельной партии; не относится целиком к этому маркеру.</p>")
        if messages:
            chunks.extend(
                f"<p><span style='color:{COLORS['muted']}'>{index:02d}</span> "
                f"<span style='color:{COLORS['violet']}'>{escape(str(message))}</span></p>"
                for index, message in enumerate(messages, 1)
            )
        else:
            chunks.append(f"<p style='color:{COLORS['muted']}'>Для этого запуска сообщения не сохранились.</p>")
        self.history_detail.setHtml("".join(chunks))
        self.history_open_button.setEnabled(not self.busy)

    def open_history_marker(self, _item: Any = None) -> None:
        if self.busy:
            return
        row = self.history_table.currentRow()
        if not 0 <= row < len(self.history_records):
            self.set_message("Сначала выберите запись истории.", error=True)
            return
        record = self.history_records[row]
        job_path = Path(str(record.get("_job_path") or ""))
        marker_id = str(record.get("marker_id") or "")
        if job_path and job_path.exists() and job_path.resolve() != self.job:
            index = next((i for i, path in enumerate(self.job_paths) if path.resolve() == job_path.resolve()), -1)
            if index >= 0:
                self.job_combo.setCurrentIndex(index)
        self.search.clear()
        self.verdict_filter.setCurrentIndex(0)
        self.current_marker_id = marker_id
        self.tabs.setCurrentWidget(self.markers_tab)
        self.select_marker_row(marker_id)

    def select_marker_row(self, marker_id: str) -> None:
        for row in range(self.marker_table.rowCount()):
            item = self.marker_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == marker_id:
                self.marker_table.selectRow(row)
                self.marker_table.scrollToItem(item)
                self.render_marker()
                self.update_action_states()
                return
        self.set_message("Маркер не найден в текущей задаче.", error=True)

    def show_current_marker_history(self) -> None:
        marker_id = self.current_marker_id
        if not marker_id:
            return
        index = self.history_project.findData(self.target_name)
        self.history_project.setCurrentIndex(index if index >= 0 else 0)
        self.history_signature = None
        self.populate_history()
        self.tabs.setCurrentWidget(self.history_tab)
        for row, record in enumerate(self.history_records):
            if (str(record.get("marker_id") or "") == marker_id
                    and str(record.get("_job_path") or "") == str(self.job.resolve())):
                self.history_table.selectRow(row)
                self.history_table.scrollToItem(self.history_table.item(row, 0))
                self.on_history_selected()
                return
        self.history_detail_title.setText("Истории этого маркера пока нет")
        self.history_detail.setPlainText("Запись появится после завершения анализа маркера.")

    def open_svacer_url(self, marker_id: str) -> None:
        marker = self.decision_by_id.get(marker_id) or self.inventory.get(marker_id, {})
        url = marker_svacer_url(
            str(self.job_data.get("snapshot_url") or ""), marker_id, str(marker.get("file") or ""),
        )
        if not url:
            self.set_message("Ссылка на маркер Svacer недоступна.", error=True)
            return
        try:
            os.startfile(url)
            self.set_message("Маркер открыт в Svacer.")
        except OSError as exc:
            self.set_message(f"Не удалось открыть Svacer: {exc}", error=True)

    def open_marker_in_svacer(self) -> None:
        if self.current_marker_id:
            self.open_svacer_url(self.current_marker_id)

    def open_live_in_svacer(self) -> None:
        if self.current_live_id:
            self.open_svacer_url(self.current_live_id)

    def set_message(self, message: str, *, error: bool = False) -> None:
        self.status.setText(message)
        self.status.setStyleSheet(f"color: {COLORS['red'] if error else COLORS['muted']};")

    def set_busy(self, value: bool) -> None:
        self.busy = value
        for widget in (
            self.fetch_button, self.connection_button, self.send_button,
            self.triage_one_button, self.approve_button, self.edit_button,
            self.new_job_button, self.refresh_jobs_button, self.settings_apply_button,
            self.delete_job_button,
        ):
            widget.setEnabled(not value)
        self.job_combo.setEnabled(not value)
        self.update_action_states()

    def run_background(
        self, work: Callable[[], Any], done: Callable[[Any], None], message: str,
        *, failed: Callable[[Exception], None] | None = None,
    ) -> None:
        if self.busy or self._task_future is not None:
            self.set_message("Дождитесь завершения текущей операции.", error=True)
            return
        self._task_done = done
        self._task_failed = failed
        self._task_future = self._task_executor.submit(work)
        self.set_busy(True)
        self.set_message(message)
        self.task_poll_timer.start()

    def drain_task(self) -> None:
        future = self._task_future
        if future is None or not future.done():
            return
        self.task_poll_timer.stop()
        self._task_future = None
        done = self._task_done
        self._task_done = None
        failed = self._task_failed
        self._task_failed = None
        self.import_applying = False
        self.set_busy(False)
        try:
            result = future.result()
        except Exception as exc:
            if failed is not None:
                failed(exc)
            else:
                self.set_message(f"Ошибка: {friendly_mcp_error(exc)}", error=True)
            return
        if done is not None:
            try:
                done(result)
            except Exception as exc:
                self.set_message(f"Ошибка обработки ответа: {exc}", error=True)

    def load_settings_form(self) -> None:
        self.workers.setValue(int(self.job_data.get("parallel_workers") or 1))
        self.usage_stop_input.setValue(int(self.job_data.get("codex_min_remaining_percent") or 0))
        self.populate_model_options(str(self.job_data.get("codex_model") or ""))
        from analysis_scope import FULL_SCOPE
        scope_index = self.analysis_scope_combo.findData(self.job_data.get("analysis_scope", FULL_SCOPE))
        self.analysis_scope_combo.setCurrentIndex(max(0, scope_index))
        self.update_capacity_preview()
        self.source_hint.setText(
            f"{self.job_data.get('repository_url') or 'Репозиторий не задан'}\n"
            f"Версия: {self.job_data.get('git_ref') or 'не задана'} · "
            f"commit: {self.job_data.get('git_commit') or 'не закреплён'}"
        )

    def populate_model_options(self, selected: str) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem("По умолчанию Codex", "")
        for entry in self._model_catalog:
            model = entry["model"]
            self.model_combo.addItem(f"{entry['display_name']}  ·  {model}", model)
        index = self.model_combo.findData(selected)
        if index < 0 and selected:
            self.model_combo.addItem(f"{selected}  ·  сохранена, доступность не подтверждена", selected)
            index = self.model_combo.count() - 1
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.blockSignals(False)

    def refresh_model_catalog(self) -> None:
        if self._model_future is not None:
            return
        self.model_refresh_button.setEnabled(False)
        self.model_hint.setText("Получаю доступные модели из локального Codex…")
        self._model_future = self._model_executor.submit(read_codex_models)
        self.model_poll_timer.start()

    def drain_model_catalog(self) -> None:
        future = self._model_future
        if future is None or not future.done():
            return
        self.model_poll_timer.stop()
        self._model_future = None
        self.model_refresh_button.setEnabled(True)
        try:
            catalog = future.result()
        except Exception as exc:
            self.model_hint.setText(
                f"Список моделей недоступен ({type(exc).__name__}). "
                "Текущую настройку можно оставить или выбрать Codex по умолчанию."
            )
            return
        selected = str(self.model_combo.currentData() or "")
        self._model_catalog = catalog
        self._model_catalog_loaded = True
        self.populate_model_options(selected)
        self.model_hint.setText(
            f"Доступно моделей: {len(catalog)}. Выбор сохраняется для этой задачи и не меняет текущий маркер."
        )

    def update_capacity_preview(self, *_args: Any) -> None:
        agents = self.workers.value()
        if agents == 1:
            value = "Один агент: маркеры из выбранной вами очереди обрабатываются последовательно."
        else:
            value = f"До {agents} маркеров одновременно: по одному на агента. Остальные ожидают в вашей очереди."
        self.capacity_hint.setText(value)

    def save_execution_settings(self) -> None:
        workers = self.workers.value()
        if not 1 <= workers <= 8:
            self.set_message("Число агентов должно быть от 1 до 8.", error=True)
            return
        try:
            data = read_json(self.job / "job.json")
            from analysis_scope import FULL_SCOPE, SCOPE_LABELS
            selected_scope = self.analysis_scope_combo.currentData()
            if selected_scope not in SCOPE_LABELS:
                raise ValueError("Выберите область разметки.")
            if (selected_scope != data.get("analysis_scope", FULL_SCOPE)
                    and read_run_record(self.job).get("active")):
                raise ValueError("Перед изменением области разметки завершите текущий анализ.")
            previous_model = normalize_codex_model(data.get("codex_model"))
            selected_model = normalize_codex_model(self.model_combo.currentData())
            if (selected_model != previous_model and selected_model
                    and selected_model not in {entry["model"] for entry in self._model_catalog}):
                self.set_message("Выбранная модель не подтверждена списком Codex. Обновите список моделей.", error=True)
                return
            data.update({
                "parallel_workers": workers,
                "manual_selection_only": True,
                "codex_model": selected_model or "",
                "analysis_scope": selected_scope,
                "codex_min_remaining_percent": self.usage_stop_input.value(),
            })
            # Leave legacy fields untouched for an already-running coordinator
            # that may still have the previous code loaded in memory.
            atomic_json(self.job / "job.json", data)
            self.job_data = data
            self.refresh()
            run = read_run_record(self.job)
            if run.get("active") and "requested_model" not in run:
                activation = "Текущий анализ запущен старой версией; новая модель применится после его завершения и нового запуска."
            elif run.get("active"):
                activation = "Текущий маркер не прерывается; новая модель применится со следующей партии."
            else:
                activation = "Модель применится при следующем запуске анализа."
            self.set_message(
                f"Настройки сохранены: агентов {workers}, модель {selected_model or 'по умолчанию Codex'}. "
                f"{activation} В работу попадут только маркеры, которые вы добавили в очередь."
                " Порог остатка Codex действует со следующей проверки (до 15 секунд); старому процессу нужен перезапуск."
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.set_message(f"Не удалось сохранить настройки: {exc}", error=True)

    def load_project_scope(self, kind: str, scope: dict[str, str]) -> Any:
        """Read-only wizard lookup; called on its background worker, not the UI."""
        if kind not in {"branch", "snapshot"}:
            raise ValueError("Неизвестный список Svacer.")
        if check_mcp(self.mcp_url, self.token) != "подключён":
            raise ValueError("Svacer не подключён. Закройте форму, нажмите «Войти в Svacer» и повторите.")
        tool = "get_projects" if kind == "branch" else "get_snapshots"
        args = {} if kind == "branch" else {key: scope[key] for key in ("project_id", "branch_id")}
        try:
            return json.loads(asyncio.run(call_mcp_tool(self.mcp_url, self.token, tool, args)))
        except Exception as exc:
            raise ValueError(f"Не удалось загрузить список Svacer: {friendly_mcp_error(exc)}") from exc

    def open_new_job_wizard(self) -> None:
        if self.busy:
            return
        dialog = ProjectSetupDialog(self, self.tool_directory, self.settings, button_factory=button,
                                    scope_loader=self.load_project_scope)
        created = False
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.created_job is not None:
                self.load_job_options()
                index = self.job_paths.index(dialog.created_job.resolve())
                self.job_combo.setCurrentIndex(index)
                created = True
        finally:
            dialog.shutdown()
            dialog.deleteLater()
        if created:
            self.fetch_markers(automatic=True)

    def edit_project_source(self) -> None:
        if self.busy:
            return
        try:
            require_idle(self.job)
            data = read_json(self.job / "job.json")
        except (OSError, ValueError) as exc:
            self.set_message(str(exc), error=True)
            return
        dialog = ProjectSetupDialog(self, self.tool_directory, self.settings, job=self.job,
                                    job_data=data, button_factory=button)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.job_data, self.target_name = job_identity(self.job)
                self.load_job_options()
                self.load_settings_form()
                self.refresh()
                self.set_message("Версия исходников сохранена. Очередь не изменена; анализ запускается кнопкой «Начать анализ».")
        finally:
            dialog.shutdown()
            dialog.deleteLater()

    def open_report(self) -> None:
        try:
            path = create_user_report(self.job, self.target_name)
            os.startfile(path)
            self.set_message("Отчёт открыт.")
        except OSError as exc:
            self.set_message(f"Не удалось открыть отчёт: {exc}", error=True)

    def edit_current_decision(self) -> None:
        marker_id = self.current_marker_id or ""
        decision = self.decision_by_id.get(marker_id, {})
        if decision.get("verdict") not in VALID_VERDICTS:
            self.set_message("Сначала дождитесь готового решения или подтвердите черновик.", error=True)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Изменить поля Svacer")
        dialog.resize(760, 560)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(
            f"{decision.get('warnClass') or 'Маркер'} — "
            f"{short_file(decision.get('file'))}:{decision.get('line') or '—'}", "section",
        ))
        layout.addWidget(label(f"ID {marker_id}", "muted"))
        layout.addWidget(label("Вердикт"))
        verdict_box = QComboBox()
        verdict_box.addItems(("Confirmed", "False Positive", "Won't fix", "Unclear"))
        verdict_box.setCurrentText(str(decision.get("verdict")))
        layout.addWidget(verdict_box)
        confirmed = QWidget()
        confirmed_row = QHBoxLayout(confirmed)
        confirmed_row.setContentsMargins(0, 0, 0, 0)
        confirmed_row.addWidget(label("Severity"))
        severity_box = QComboBox()
        severity_box.addItems(("Critical", "Major", "Minor"))
        severity_box.setCurrentText(str(decision.get("severity") or "Major"))
        confirmed_row.addWidget(severity_box)
        confirmed_row.addWidget(label("Действие"))
        action_box = QComboBox()
        action_box.addItems(("Fix required", "Fix submitted", "Ignore"))
        action_box.setCurrentText(str(decision.get("action") or "Fix required"))
        confirmed_row.addWidget(action_box)
        layout.addWidget(confirmed)
        verdict_box.currentTextChanged.connect(lambda value: confirmed.setVisible(value == "Confirmed"))
        confirmed.setVisible(verdict_box.currentText() == "Confirmed")
        layout.addWidget(label("Комментарий для Svacer"))
        comment = QPlainTextEdit()
        comment.setPlainText(comment_without_heading(decision.get("comment")))
        layout.addWidget(comment, 1)
        hint = label("Изменения сохранятся локально. Отправка в Svacer выполняется отдельно.", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controls = QHBoxLayout()
        controls.addStretch(1)
        cancel = button("Отмена")
        cancel.clicked.connect(dialog.reject)
        controls.addWidget(cancel)
        save = button("Сохранить", tone="success")
        controls.addWidget(save)
        layout.addLayout(controls)

        def save_decision() -> None:
            verdict = verdict_box.currentText()
            try:
                result = edit_saved_decision(
                    self.job / "markers.inventory.json", self.job / "decisions.jsonl",
                    marker_id, comment.toPlainText(), verdict=verdict,
                    severity=severity_box.currentText() if verdict == "Confirmed" else None,
                    action=action_box.currentText() if verdict == "Confirmed" else None,
                )
            except SystemExit as exc:
                QMessageBox.warning(dialog, "Не удалось сохранить", str(exc))
                return
            dialog.accept()
            self.marker_signature = None
            self._table_signatures.pop("queue", None)
            self.refresh()
            details = f"Поля сохранены локально: {result.get('verdict')}"
            if verdict == "Confirmed":
                details += f"; {result.get('severity')}, {result.get('action')}"
            if result.get("invalidated_import_files"):
                details += "; предыдущая локальная подготовка отправки сброшена"
            self.set_message(details + ". В Svacer ничего не отправлено.")

        save.clicked.connect(save_decision)
        dialog.exec()

    def prepare_import_payload(self) -> dict[str, Any]:
        status = check_mcp(self.mcp_url, self.token)
        if status != "подключён":
            raise RuntimeError(status)
        reply = asyncio.run(call_mcp_tool(
            self.mcp_url, self.token, "prepare_markup_import",
            {"job_directory": str(self.job)},
        ))
        return json.loads(reply)

    def send_import(self) -> None:
        state = collect_state(self.job)
        if not state.get("import_ready"):
            self.set_message(
                state.get("import_error") or "Нет новых готовых решений: остальные уже отправлены или требуют проверки.",
                error=True,
            )
            return
        if state.get("import_blocked"):
            self.set_message("Попытка отправки уже записана; повтор заблокирован.", error=True)
            return
        self.run_background(
            self.prepare_import_payload, self.confirm_and_send_prepared,
            "Проверяю решения, область маркеров и текущую разметку Svacer…",
        )

    def confirm_and_send_prepared(self, _payload: dict[str, Any]) -> None:
        try:
            preview = read_json(self.job / "svacer-import-preview.json")
            force = bool(preview.get("requires_force"))
            expected = str(preview.get("force_confirmation" if force else "confirmation") or "")
            if not expected:
                raise ValueError("Не получена фраза подтверждения.")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.set_message(f"Подготовка отправки не завершена: {exc}", error=True)
            return
        conflict = "\nСуществующая разметка будет заменена." if force else ""
        reply = QMessageBox.question(
            self, "Отправка в Svacer",
            f"Проверка завершена. Маркеров: {preview.get('marker_count')}; "
            f"конфликтов: {preview.get('conflict_count')}.{conflict}\n"
            f"Не включены в эту отправку: {len(preview.get('selection', {}).get('skipped', {}))}.\n\n"
            f"Из них требуют проверки: {preview.get('selection', {}).get('needs_attention', 0)}; "
            f"уже отправлены: {preview.get('selection', {}).get('already_sent', 0)}.\n\n"
            f"{preview.get('scope', '')}\n\n"
            "Следующий шаг изменит разметку Svacer. Продолжить?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.set_message("Проверка завершена, отправка отменена. Ничего в Svacer не изменено.")
            return
        typed, accepted = QInputDialog.getText(
            self, "Точное подтверждение", f"Введите дословно:\n\n{expected}",
        )
        if not accepted or typed != expected:
            self.set_message("Фраза не совпала. Ничего не отправлено.", error=True)
            return

        def work() -> dict[str, Any]:
            response = asyncio.run(call_mcp_tool(
                self.mcp_url, self.token, "apply_markup_import",
                {"job_directory": str(self.job), "confirmation": typed,
                 "overwrite": "force" if force else "none"},
            ))
            return json.loads(response)

        def done(payload: dict[str, Any]) -> None:
            verified = bool((payload.get("verification") or {}).get("verified"))
            self.refresh()
            self.set_message(
                "Разметка отправлена и подтверждена обратной проверкой." if verified else
                "Итог отправки не подтверждён. Проверьте журнал Svacer и папку результатов; повтор заблокирован.",
                error=not verified,
            )

        self.import_applying = True
        self.run_background(work, done, "Отправляю разметку и выполняю обратную проверку…")

    def approve_current_draft(self) -> None:
        marker_id = self.current_marker_id or ""
        if marker_id not in self.drafts:
            self.set_message("У выбранного маркера нет сохранённого черновика.", error=True)
            return
        if read_run_record(self.job).get("active"):
            self.set_message("Дождитесь завершения текущего анализа перед подтверждением черновика.", error=True)
            return
        try:
            result = approve_saved_draft(
                self.job / "markers.inventory.json", self.job / "decisions.jsonl", marker_id,
            )
        except SystemExit as exc:
            self.set_message(str(exc), error=True)
            return
        self.marker_signature = None
        self._table_signatures.pop("queue", None)
        self.refresh()
        self.set_message(
            f"Черновик подтверждён ({result.get('count', 0)}). В Svacer ничего не отправлено."
        )

    def queue_selected_marker(self) -> None:
        marker_id = self.current_marker_id
        if not marker_id:
            self.set_message("Сначала выберите маркер.", error=True)
            return
        if read_run_record(self.job).get("active"):
            self.set_message("Дождитесь завершения текущей партии или остановите её.", error=True)
            return
        existing_queue = list(self.state.get("priority_marker_ids") or [])
        if existing_queue and existing_queue != [marker_id]:
            answer = QMessageBox.question(
                self, "Заменить очередь",
                "Отдельный запуск этого маркера заменит выбранную вручную очередь. Продолжить?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        decision = self.decision_by_id.get(marker_id, {})
        if decision.get("verdict"):
            answer = QMessageBox.question(
                self, "Повторный анализ",
                "Для маркера уже есть локальное решение. Очистить его и поставить на повторный анализ?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            result = subprocess.run(
                [console_python_executable(), str(self.app_directory / "triage_queue.py"),
                 "--inventory", str(self.job / "markers.inventory.json"),
                 "--decisions", str(self.job / "decisions.jsonl"), "reopen", "--ids", marker_id],
                cwd=str(self.app_directory), capture_output=True, text=True,
                encoding="utf-8", check=False, **hidden_subprocess_kwargs(),
            )
            if result.returncode != 0:
                self.set_message(
                    f"Не удалось открыть маркер повторно: {(result.stderr or result.stdout).strip()}",
                    error=True,
                )
                return
        try:
            reset_queue_assignments(self.job / "decisions.jsonl")
        except SystemExit as exc:
            self.set_message(str(exc), error=True)
            return
        control_path = self.job / "control.json"
        control = read_json(control_path) if control_path.exists() else {}
        if not isinstance(control, dict):
            control = {}
        control.update({
            "pause_requested": False, "single_batch_completed": False,
            "one_shot_completed": False, "single_marker_requested": True,
            "priority_marker_ids": [marker_id], "analysis_started": True,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "triage_gui_qt single marker",
        })
        atomic_json(control_path, control)
        try:
            launched = launch_runner(self.job, self.app_directory)
        except Exception as exc:
            control["pause_requested"] = True
            control["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            atomic_json(control_path, control)
            self.set_message(f"Маркер выбран, но Codex не запустился: {exc}", error=True)
            self.refresh()
            return
        self.marker_signature = None
        self._table_signatures.pop("queue", None)
        self.refresh()
        pid = launched.get("codex_pid") or launched.get("runner_pid")
        self.set_message(f"Выбранный маркер поставлен в отдельный запуск (PID {pid}).")

    def selected_job(self) -> Path | None:
        row = self.jobs_table.currentRow()
        item = self.jobs_table.item(row, 0) if row >= 0 else None
        identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return Path(identity) if identity else None

    def update_action_states(self) -> None:
        from triage_gui import analysis_configuration_text
        state = self.state
        configuration_text = analysis_configuration_text(self.job_data, state)
        self.analysis_configuration.setText(configuration_text)
        self.analysis_configuration.setMinimumWidth(min(
            520, self.analysis_configuration.fontMetrics().horizontalAdvance(configuration_text) + 4,
        ))
        run = state.get("codex_run") or {}
        running = bool(run.get("active"))
        ready = (self.job / "markers.inventory.json").is_file() and (self.job / "decisions.jsonl").is_file()
        has_job = (self.job / "job.json").is_file()
        self.fetch_button.setVisible(has_job)
        self.fetch_button.setText("Обновить маркеры" if ready else "Получить маркеры")
        self.delete_job_button.setEnabled(has_job and not running and not self.busy)
        self.settings_apply_button.setEnabled(has_job and not self.busy)
        self.source_button.setEnabled(has_job and not running and not self.busy)
        self.report_button.setEnabled(has_job and ready and not self.busy)
        self.reset_button.setVisible(ready or running)
        self.reset_button.setEnabled(ready and not running and not self.busy)
        self.analysis_button.setVisible(ready or running)
        verification_pending = bool(state.get("verification", {}).get("pending"))
        stopping = bool(state.get("paused") or run.get("stop_requested"))
        if running:
            self.analysis_button.setText("Завершается…" if stopping else "Завершить анализ")
            self.analysis_button.setEnabled(not stopping and not self.busy)
            set_button_tone(self.analysis_button, "danger")
        elif verification_pending and not state.get("priority_marker_ids"):
            self.analysis_button.setText("Продолжить проверку")
            self.analysis_button.setEnabled(ready and not self.busy)
            set_button_tone(self.analysis_button, "success")
        elif not state.get("priority_marker_ids"):
            self.analysis_button.setText("Выберите маркеры")
            self.analysis_button.setEnabled(False)
            set_button_tone(self.analysis_button, "neutral")
        else:
            self.analysis_button.setText("Начать анализ")
            self.analysis_button.setEnabled(ready and not self.busy)
            set_button_tone(self.analysis_button, "success")
        self.fetch_button.setEnabled(has_job and not running and not self.busy
                                     and not (self.job / "svacer-import-attempt.json").exists())
        import_ready = int(state.get("import_ready") or 0)
        self.send_button.setText(f"Отправить готовые ({import_ready})")
        self.send_button.setToolTip(state.get("import_error") or (
            "Предыдущая отправка не подтверждена; проверьте её результат." if state.get("import_blocked") else
            "Отправляются только новые готовые решения. Остальные маркеры и очередь не меняются."
        ))
        self.send_button.setEnabled(ready and import_ready > 0 and not self.busy and not state.get("import_blocked"))
        selected_ids = self.selected_marker_ids()
        queue_states = self.marker_queue_states()
        new_ids = [mid for mid in selected_ids if mid not in queue_states]
        if selected_ids and not new_ids:
            all_active = all(queue_states[mid][0] == "В работе" for mid in selected_ids)
            all_waiting = all(queue_states[mid][0] == "В очереди" for mid in selected_ids)
            self.add_queue_button.setText("Уже в работе" if all_active else "Уже в очереди" if all_waiting else "Уже назначены")
        else:
            self.add_queue_button.setText(f"В очередь ({len(new_ids)})" if new_ids else "Добавить в очередь")
        self.add_queue_button.setEnabled(bool(new_ids) and ready and not self.busy)
        selected_job = self.selected_job()
        self.open_job_button.setEnabled(bool(selected_job) and not self.busy)
        self.stop_job_button.setEnabled(bool(
            selected_job and not self.busy and read_run_record(selected_job).get("active")
        ))
        marker_id = self.current_marker_id
        decision = self.decision_by_id.get(marker_id or "", {})
        draft = self.drafts.get(marker_id or "", {})
        self.edit_button.setEnabled(not self.busy and decision.get("verdict") in VALID_VERDICTS)
        can_approve = bool(draft and draft.get("analysis_status") != "needs_context"
                           and draft.get("verdict") in VALID_VERDICTS and not running and not self.busy)
        self.approve_button.setVisible(bool(draft and draft.get("analysis_status") != "needs_context"
                                            and draft.get("verdict") in VALID_VERDICTS))
        self.approve_button.setEnabled(can_approve)
        if marker_id in queue_states:
            self.triage_one_button.setText(queue_states[marker_id][0])
            self.triage_one_button.setEnabled(False)
            set_button_tone(self.triage_one_button, "neutral")
        else:
            self.triage_one_button.setText(
                "Перепроверить этот" if decision.get("verdict") or marker_review_status(self.inventory.get(marker_id, {})) != "Undecided" else
                "Повторить этот" if draft else "Разметить только этот"
            )
            self.triage_one_button.setEnabled(bool(marker_id) and not self.busy and not running)
            set_button_tone(self.triage_one_button, "warning" if decision.get("verdict") or draft else "primary")
        source_file = str(decision.get("file") or self.inventory.get(marker_id or "", {}).get("file") or "")
        self.open_svacer_button.setEnabled(bool(
            marker_id and marker_svacer_url(str(self.job_data.get("snapshot_url") or ""), marker_id, source_file)
        ))
        live_id = self.current_live_id
        live_source = str((self.decision_by_id.get(live_id or "") or self.inventory.get(live_id or "", {})).get("file") or "")
        self.live_svacer_button.setEnabled(bool(
            live_id and marker_svacer_url(str(self.job_data.get("snapshot_url") or ""), live_id, live_source)
        ))
        self.history_open_button.setEnabled(self.history_table.currentRow() >= 0 and not self.busy)
        self.update_remove_queue_button()

    def analysis_action(self) -> None:
        run = read_run_record(self.job)
        if run.get("active"):
            state = collect_state(self.job)
            if not state.get("paused"):
                set_pause(self.job, True)
                self.set_message("Завершение запрошено: текущий маркер сохранит результат, новая работа не начнётся.")
            self.refresh()
            return
        state = collect_state(self.job)
        if not (self.job / "markers.inventory.json").is_file() or not (self.job / "decisions.jsonl").is_file():
            self.set_message("Сначала нажмите «Получить маркеры».", error=True)
            return
        if not state.get("priority_marker_ids") and not state.get("verification", {}).get("pending"):
            self.set_message("Очередь пуста. Выберите маркеры на вкладке «Маркеры» и нажмите «В очередь».")
            return
        control_path = self.job / "control.json"
        try:
            control = read_json(control_path) if control_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            control = {}
        if isinstance(control, dict) and control.get("one_shot_completed"):
            self.set_message("Одноразовая разметка завершена. Выберите следующий маркер или сбросьте очередь.", error=True)
            return
        prompt = self.job / "START_PROMPT.txt"
        if not prompt.is_file() or not prompt.read_text(encoding="utf-8-sig").strip():
            self.set_message("Не найден сохранённый промпт запуска задачи.", error=True)
            return
        set_pause(self.job, False)
        try:
            launched = launch_runner(self.job, self.app_directory)
        except Exception as exc:
            set_pause(self.job, True)
            self.set_message(f"Не удалось запустить Codex: {exc}", error=True)
            self.refresh()
            return
        self.refresh()
        pid = launched.get("codex_pid") or launched.get("runner_pid")
        self.set_message(f"Анализ запущен в фоне (PID {pid}). Окно можно закрыть.")

    def reset_current_queue(self) -> None:
        if read_run_record(self.job).get("active"):
            self.set_message("Сначала завершите текущий анализ.", error=True)
            return
        answer = QMessageBox.question(
            self, "Сбросить очередь",
            "Очистить выбранную очередь? Готовые решения и черновики сохранятся.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            reset_queue_assignments(self.job / "decisions.jsonl")
        except SystemExit as exc:
            self.set_message(str(exc), error=True)
            return
        self.marker_signature = None
        self._table_signatures.pop("queue", None)
        self.refresh()
        self.set_message("Очередь сброшена; готовые решения и черновики сохранены. Ничего не отправлено в Svacer.")

    def stop_selected_job(self) -> None:
        path = self.selected_job()
        if path is None:
            self.set_message("Сначала выберите работающую задачу.", error=True)
            return
        if not read_run_record(path).get("active"):
            self.set_message("Выбранная задача уже остановлена.")
            return
        _, target = job_identity(path)
        answer = QMessageBox.question(
            self, "Остановить анализ",
            f"Немедленно остановить анализ «{target}»? Несохранённая работа текущего маркера будет отброшена.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.run_background(
            lambda: stop_run(path),
            lambda _result: (self.refresh(), self.set_message(f"Задача «{target}» остановлена.")),
            f"Останавливаю задачу «{target}»…",
        )

    def check_connection(self) -> None:
        if self._connection_future is not None or self.login_dialog is not None:
            return
        self.connection.setText("Svacer: проверка…")
        self._connection_future = self._connection_executor.submit(check_mcp, self.mcp_url, self.token)
        self.connection_poll_timer.start()

    def drain_connection(self) -> None:
        future = self._connection_future
        if future is None or not future.done():
            return
        self.connection_poll_timer.stop()
        self._connection_future = None
        try:
            status = future.result()
        except Exception as exc:
            status = friendly_mcp_error(exc)

        self.connected = status == "подключён"
        self.connection.setText("Локальный MCP подключён" if self.connected else "Локальный MCP не подключён")
        self.connection.setStyleSheet(
            f"color: {COLORS['green'] if self.connected else COLORS['amber']};"
        )
        self.connection_button.setText("Выйти из Svacer" if self.connected else "Войти в Svacer")
        set_button_tone(self.connection_button, "danger" if self.connected else "violet")
        if self.connected:
            self.connection_retries = 0
            self.set_message("Локальный MCP работает. Доступность Svacer API проверяется при чтении данных.")
        elif self.connection_retries > 0:
            self.connection_retries -= 1
            self.connection_timer.start(3000)
            self.set_message("Завершите вход в открывшемся окне — подключение проверится снова.")
        else:
            self.set_message(f"Svacer недоступен: {status}", error=True)
            if not self._initial_connection_prompted:
                self._initial_connection_prompted = True
                self.set_message("Для начала работы войдите в Svacer. Открываю форму подключения.")
                QTimer.singleShot(0, self.toggle_connection)

    def refresh_and_check(self) -> None:
        self.connection_retries = 0
        self.refresh()
        self.check_connection()

    def toggle_connection(self) -> None:
        if self.busy or self.login_dialog is not None:
            return
        if not self.connected:
            self._initial_connection_prompted = True
            # Ignore a pre-login probe's stale result; the dialog verifies readiness itself.
            self.connection_timer.stop()
            self.connection_poll_timer.stop()
            self._connection_future = None
            dialog = SvacerLoginDialog(
                self, self.app_directory, str(self.settings.get("svacer_url") or ""),
                self.mcp_url, self.token, button_factory=button,
            )
            self.login_dialog = dialog
            try:
                accepted = dialog.exec() == QDialog.DialogCode.Accepted
            finally:
                dialog.shutdown()
                self.login_dialog = None
                dialog.deleteLater()
            self.connection_retries = 0
            if accepted:
                self.settings["svacer_url"] = dialog.server_url
                self.connected = True
                self.connection.setText("Локальный MCP подключён")
                self.connection.setStyleSheet(f"color: {COLORS['green']};")
                self.connection_button.setText("Выйти из Svacer")
                set_button_tone(self.connection_button, "danger")
                self.set_message("Вход выполнен. Подключение работает в фоне, консоль не нужна.")
            else:
                self.set_message("Вход отменён. Задачи и результаты не изменены.")
                self.check_connection()
            return

        def done(_result: Any) -> None:
            self.connected = False
            self.connection_retries = 0
            self.connection.setText("Svacer не подключён")
            self.connection_button.setText("Войти в Svacer")
            set_button_tone(self.connection_button, "violet")
            self.set_message("Локальное подключение завершено. Задачи и результаты не изменены.")

        self.run_background(
            lambda: stop_svacer_connection(self.app_directory, local_port(self.mcp_url)), done,
            "Завершаю локальное подключение Svacer…",
        )

    def fetch_markers(self, *, automatic: bool = False) -> None:
        if self.busy:
            return
        job = self.job.resolve()

        def work() -> dict[str, Any]:
            data = read_json(job / "job.json")
            advanced_filter = str(data.get("advanced_filter") or "")
            configured = str(self.settings.get("advanced_filter") or "")
            required = ("project_id", "branch_id", "snapshot_id")
            missing = [name for name in required if not str(data.get(name) or "").strip()]
            if missing:
                raise ValueError(f"В job.json отсутствуют поля: {', '.join(missing)}")
            if not advanced_filter or advanced_filter != configured:
                raise ValueError("Фильтр задачи отсутствует или не совпадает с настройкой программы.")
            require_idle(job)
            if check_mcp(self.mcp_url, self.token) != "подключён":
                raise RuntimeError("Svacer не подключён. Сначала нажмите «Войти в Svacer».")
            reply = asyncio.run(call_mcp_tool(
                self.mcp_url, self.token, "get_markers", {
                    "project_id": str(data["project_id"]),
                    "branch_id": str(data["branch_id"]),
                    "snapshot_id": str(data["snapshot_id"]),
                    "advanced_filter": advanced_filter,
                    "traces": False, "checker_info": False,
                    "review_history": False, "comment_history": False,
                    "fields": MARKER_INVENTORY_FIELDS, "limit": 0,
                },
            ))
            inventory = validate_marker_inventory(json.loads(reply), advanced_filter)
            return update_marker_inventory(job, self.tool_directory, inventory)

        def done(result: dict[str, Any]) -> None:
            self.marker_signature = None
            self.refresh()
            if automatic:
                self.tabs.setCurrentWidget(self.markers_tab)
            self.set_message(
                f"Маркеры загружены: {result['inventory_total']}; для анализа: {result['triage_total']}. "
                + ("По текущему ГОСТ-фильтру в этом снимке маркеров нет."
                   if not result["inventory_total"] else
                   "Локальные результаты и очередь сохранены. Выберите нужные маркеры и добавьте их в очередь.")
            )

        if automatic:
            def failed(exc: Exception) -> None:
                self.set_message(
                    "Проект сохранён, но маркеры автоматически не загрузились. "
                    f"{friendly_mcp_error(exc)} Повторите загрузку кнопкой «Получить маркеры».",
                    error=True,
                )

            self.run_background(
                work, done, "Проект создан. Автоматически загружаю маркеры из Svacer…",
                failed=failed,
            )
        else:
            self.run_background(work, done, "Получаю маркеры из Svacer и готовлю локальную очередь…")

    def delete_current_job(self) -> None:
        if self.busy or not (self.job / "job.json").is_file():
            return
        job = self.job
        try:
            require_idle(job)
        except (ValueError, OSError) as exc:
            self.set_message(str(exc), error=True)
            return
        answer = QMessageBox.question(
            self, "Удалить локальную задачу?",
            f"Переместить в корзину задачу «{self.target_name}»?\n{job}\n\n"
            "Будут убраны её локальные результаты, история и очередь. "
            "Другие задачи, общий кэш исходников и проект в Svacer не изменятся. "
            "Восстановление — через корзину Windows.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def done(_result: Any) -> None:
            if self.live_dialog is not None:
                self.live_dialog.close()
            self._jobs_future = None
            self.jobs_poll_timer.stop()
            self.marker_signature = None
            self.history_signature = None
            self._notification_signature = None
            self._table_signatures.clear()
            self.load_job_options()
            self.refresh()
            self.set_message("Локальная задача перемещена в корзину. Svacer и другие задачи не изменены.")

        self.run_background(lambda: trash_local_job(job, self.tool_directory), done,
                            "Перемещаю локальную задачу в корзину…")

    def show_empty_workspace(self) -> None:
        self.state = {}
        self.job_data = {}
        self.inventory, self.decision_by_id, self.drafts, self.traces = {}, {}, {}, {}
        self.decisions = []
        self.current_marker_id = self.current_live_id = None
        self._table_signatures.clear()
        self.subtitle.setText("Локальных задач нет — нажмите «Новый проект».")
        self.setWindowTitle("Svacer Triage")
        for table in (self.jobs_table, self.queue_table, self.active_table, self.marker_table, self.history_table):
            set_rows(table, [], [])
        self.scope.setText("Создайте новый проект или обновите список задач.")
        self.run_status.setText("")
        self.usage.setText("")
        self.usage_stop_status.setText("")
        self.progress_text.setText("0 из 0")
        self.set_preparation_indicator("")
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.populate_markers()
        self.populate_work_queue()
        self.notification_panel.hide()
        self.update_action_states()


def main() -> int:
    parser = argparse.ArgumentParser(description="Графическая панель Svacer Triage")
    parser.add_argument("--job")
    parser.add_argument("--new-project", action="store_true")
    parser.add_argument("--startup-ready-file", help=argparse.SUPPRESS)
    args = parser.parse_args()
    app_directory = Path(__file__).resolve().parent
    saved_jobs = list_saved_jobs(app_directory.parent)
    job = (resolve_job(app_directory.parent, args.job)
           if args.job or saved_jobs
           else app_directory.parent / "RESULTS" / ".no-job")
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Svacer.Triage.Desktop")
        except (AttributeError, OSError):
            pass
    app = QApplication([])
    app.setApplicationName("Svacer Triage")
    app.setApplicationDisplayName("Svacer Triage")
    app.setOrganizationName("Svacer Triage")
    icon_path = app_directory / "assets" / "svacer-triage-v2.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    windows_font = Path("C:/Windows/Fonts/segoeui.ttf")
    if windows_font.is_file():
        QFontDatabase.addApplicationFont(str(windows_font))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = TriageQtWindow(job, app_directory)
    if icon_path.is_file():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.showMaximized()
    if args.startup_ready_file:
        ready_path = Path(args.startup_ready_file)

        def signal_startup_ready() -> None:
            try:
                ready_path.write_text("ready\n", encoding="utf-8")
            except OSError:
                pass

        QTimer.singleShot(0, signal_startup_ready)
    if args.new_project:
        QTimer.singleShot(0, window.open_new_job_wizard)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
