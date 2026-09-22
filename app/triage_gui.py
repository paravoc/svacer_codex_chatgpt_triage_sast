#!/usr/bin/env python3
"""Graphical dashboard and finding browser for local Svacer triage."""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from desktop_theme import (
    BG, SURFACE, SURFACE_2, TEXT, MUTED, BLUE, GREEN, YELLOW, RED, PURPLE,
    SELECTION, apply_theme, style_window,
)

from codex_run import (
    console_python_executable,
    hidden_subprocess_kwargs,
    launch_runner,
    read_codex_rate_limits,
    read_codex_usage,
    read_run_record,
    stop_run,
)
from marker_history import (
    compare_history_attempts, history_measurements, load_marker_history,
    previous_history_attempt,
)
from triage_queue import (
    VALID_ACTIONS,
    VALID_SEVERITIES,
    approve_saved_draft,
    complete_desktop_settings,
    edit_saved_decision,
    markers_for_triage,
    marker_review_status,
    load_decisions,
    normalize_worker_result,
    reset_queue_assignments,
)
from triage_dashboard import (
    atomic_json,
    call_mcp_tool,
    check_mcp,
    collect_state,
    friendly_mcp_error,
    read_json,
    read_jsonl,
    resolve_job,
    set_pause,
    start_svacer_reconnect,
    stop_svacer_connection,
)


ACTIVITY_TAG_COLORS = {
    "line_number": MUTED,
    "normal": TEXT,
    "active": BLUE,
    "success": GREEN,
    "warning": YELLOW,
    "error": RED,
    "agent": PURPLE,
}
ACTIVITY_ANIMATION_FRAMES = 13
ACTIVITY_ANIMATION_INTERVAL_MS = 18
ACTIVITY_ANIMATION_OFFSET_PX = 9

VERDICT_HEADINGS = {"CONFIRMED", "FALSE POSITIVE", "WON'T FIX", "UNCLEAR"}
VALID_VERDICTS = {"Confirmed", "False Positive", "Won't fix", "Unclear"}
DRAFT_NOTE_RE = re.compile(r"^batch-(\d+)-worker-(\d+)\.json$", re.IGNORECASE)
MARKER_INVENTORY_FIELDS = [
    "id", "invariant", "warnClass", "file", "line", "msg", "function",
    "review", "tool", "mtid",
]
FILTERS = {
    "Все маркеры": "all",
    "Размечены в Svacer": "svacer_reviewed",
    "Не размечены в Svacer": "svacer_unreviewed",
    "В работе": "active",
    "Черновики": "draft",
    "Размеченные": "completed",
    "Ожидают анализа": "pending",
    "Confirmed": "Confirmed",
    "False Positive": "False Positive",
    "Won't fix": "Won't fix",
    "Unclear": "Unclear",
}


def marker_matches_filter(
    selected: str,
    marker_id: str,
    verdict: Any,
    has_draft: bool,
    in_work: set[str],
    review_status: str = "Undecided",
) -> bool:
    """Return whether a marker belongs to the selected, non-overlapping view."""
    if selected == "all":
        return True
    reviewed = review_status != "Undecided"
    if selected == "svacer_reviewed":
        return reviewed
    if selected == "svacer_unreviewed":
        return not reviewed
    if selected == "active":
        return marker_id in in_work
    if selected == "draft":
        return has_draft and not verdict and marker_id not in in_work
    if selected == "completed":
        return bool(verdict) or reviewed
    if selected == "pending":
        return not verdict and not reviewed and not has_draft and marker_id not in in_work
    return (verdict or review_status) == selected


def active_marker_ids(state: dict[str, Any]) -> set[str]:
    return set(marker_assignments(state))


def marker_svacer_url(snapshot_url: str, marker_id: str, file_name: str) -> str:
    if not snapshot_url or not marker_id or not file_name:
        return ""
    payload = json.dumps(
        {"markerID": marker_id, "file": file_name},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{snapshot_url.rstrip('/')}/marker/{encoded}"


def latest_codex_activity(job: Path) -> str:
    """Return a short, non-sensitive activity label from recent JSONL events."""
    path = job / "codex-events.jsonl"
    try:
        lines = path.read_bytes()[-65_536:].decode("utf-8", errors="replace").splitlines()
    except OSError:
        return "запускается"
    active: dict[str, str] = {}
    last_completed = ""
    labels = {
        "command_execution": "выполняет локальную команду",
        "mcp_tool_call": "получает данные через MCP",
        "web_search": "проверяет внешние источники",
        "agent_message": "анализирует результаты",
    }
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        item_type = str(item.get("type") or "")
        label = labels.get(item_type, "обрабатывает задачу")
        if event.get("type") == "item.started" and item_id:
            active[item_id] = label
        elif event.get("type") == "item.completed":
            active.pop(item_id, None)
            last_completed = label
    if active:
        return next(reversed(active.values()))
    if last_completed:
        return "готовит следующий шаг"
    return "инициализируется"


def _short_activity_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def codex_activity_entries(job: Path, limit: int = 30) -> list[str]:
    """Build a quiet feed containing only user-facing agent messages."""
    path = job / "codex-events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "triage.run.started":
            entries.clear()
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if event_type != "item.completed":
            continue
        item_type = str(item.get("type") or "")
        if item_type == "agent_message":
            text = _short_activity_text(item.get("text"))
            if text:
                entries.append(f"Агент: {text}")
    compact: list[str] = []
    for entry in entries:
        if not compact or compact[-1] != entry:
            compact.append(entry)
    return compact[-limit:]


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {seconds:02d} с"
    return f"{seconds} с"


def format_run_event_time(run: dict[str, Any], job: Path | None = None) -> str:
    """Timestamp a terminal run state without presenting file mtime as exact event time."""
    if run.get("active"):
        return ""
    status = str(run.get("status") or "not_started")
    labels = {
        "failed": "время ошибки",
        "completed": "завершено",
        "incomplete": "остановлено",
        "paused": "остановлено",
        "stopped": "остановлено",
    }
    label = labels.get(status)
    if not label:
        return ""

    for key in ("finished_at", "updated_at"):
        raw = str(run.get(key) or "").strip()
        if not raw:
            continue
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return f"{label}: {moment.astimezone().strftime('%d.%m.%Y %H:%M:%S')}"

    if job is not None:
        try:
            moment = datetime.fromtimestamp((job / "codex-run.json").stat().st_mtime).astimezone()
        except OSError:
            pass
        else:
            return f"запись обновлена: {moment.strftime('%d.%m.%Y %H:%M:%S')}"
    return ""


def live_run_timing(
    job: Path, run: dict[str, Any], *, now_timestamp: float | None = None,
) -> tuple[str, str]:
    """Describe elapsed and idle time without pretending to know model ETA."""
    if not run.get("active"):
        return "", MUTED
    now_timestamp = time.time() if now_timestamp is None else now_timestamp
    try:
        started = datetime.fromisoformat(str(run.get("started_at") or "")).timestamp()
    except (TypeError, ValueError):
        started = now_timestamp
    try:
        last_activity = (job / "codex-events.jsonl").stat().st_mtime
    except OSError:
        last_activity = started
    idle_seconds = max(0, now_timestamp - min(last_activity, now_timestamp))
    elapsed = format_elapsed(now_timestamp - min(started, now_timestamp))
    idle = format_elapsed(idle_seconds)
    if idle_seconds >= 15 * 60:
        return (
            f"Прошло {elapsed}  •  новых событий нет {idle}  •  возможно, анализ завис",
            RED,
        )
    if idle_seconds >= 5 * 60:
        return (
            f"Прошло {elapsed}  •  новых событий нет {idle}  •  выполняется долгий шаг",
            YELLOW,
        )
    return f"Прошло {elapsed}  •  последнее обновление {idle} назад", GREEN


def format_codex_run_status(
    target_name: str, job_name: str, run: dict[str, Any], activity: str = "",
    job: Path | None = None,
) -> tuple[str, str]:
    status = str(run.get("status") or "not_started")
    prefix = f"Фоновая задача: {target_name}  •  {job_name}"
    if run.get("active"):
        pid = run.get("codex_pid") or run.get("runner_pid") or "—"
        action = activity or ("запускается" if status == "launching" else "работает")
        return f"{prefix}  •  работает  •  PID {pid}  •  {action}", GREEN
    reason = str(run.get("reason") or "").strip()
    event_time = format_run_event_time(run, job)
    when = f"  •  {event_time}" if event_time else ""
    if status == "completed":
        return f"{prefix}  •  завершена{when}", GREEN
    if status == "failed":
        return f"{prefix}  •  ошибка{when}  •  {reason or 'причина не определена'}", RED
    if status == "incomplete":
        return f"{prefix}  •  не завершена{when}  •  {reason}", YELLOW
    if status == "paused":
        if "после завершения текущей партии" in reason or reason.startswith("Партия завершена"):
            return f"{prefix}  •  партия завершена{when}", GREEN
        return f"{prefix}  •  остановлена{when}" + (f"  •  {reason}" if reason else ""), YELLOW
    if status == "stopped":
        return f"{prefix}  •  остановлена{when}" + (f"  •  {reason}" if reason else ""), YELLOW
    return f"{prefix}  •  ещё не запускалась", MUTED


def format_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def short_file(value: Any) -> str:
    return Path(str(value or "").replace("\\", "/")).name or "—"


def comment_without_heading(value: Any) -> str:
    lines = str(value or "").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().upper() in VERDICT_HEADINGS:
        lines.pop(0)
    return "\n".join(lines).strip()


def list_text(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "—"
    return "\n".join(f"• {item}" for item in value)


def job_identity(job: Path) -> tuple[dict[str, Any], str]:
    data = read_json(job / "job.json") if (job / "job.json").exists() else {}
    repository = str(data.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1]
    if repository.endswith(".git"):
        repository = repository[:-4]
    git_ref = str(data.get("git_ref") or "")
    return data, " ".join(part for part in (repository, git_ref) if part) or job.name


def list_saved_jobs(tool_directory: Path) -> list[Path]:
    roots = [tool_directory / "RESULTS", tool_directory / "jobs"]
    jobs = [
        path for root in roots if root.is_dir()
        for path in root.iterdir()
        if path.is_dir() and (path / "job.json").is_file()
    ]
    return sorted(jobs, key=lambda path: path.stat().st_mtime, reverse=True)


def job_selector_label(job: Path) -> str:
    _data, target = job_identity(job)
    return f"{target}  —  {job.name}"


def compact_job_timestamp(job: Path) -> str:
    match = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", job.name)
    if match:
        _year, month, day, hour, minute = match.groups()
        return f"{day}.{month} {hour}:{minute}"
    try:
        return datetime.fromtimestamp(job.stat().st_mtime).strftime("%d.%m %H:%M")
    except OSError:
        return "без даты"


def friendly_run_state(run: dict[str, Any], state: dict[str, Any]) -> tuple[str, str]:
    if run.get("active"):
        return "Работает", "running"
    status = str(run.get("status") or "not_started")
    if status == "completed":
        return "Завершена", "completed"
    if status == "failed":
        return "Ошибка", "failed"
    if status == "incomplete":
        return "Не завершена", "paused"
    if (status == "paused" and state.get("one_shot_completed")
            and not state.get("verification", {}).get("pending")
            and not state.get("verification", {}).get("challenged")):
        return "Выбранное обработано", "completed"
    if status == "paused" and state.get("single_batch_completed"):
        return "Партия готова", "completed"
    if state.get("paused") or status in {"paused", "stopped"}:
        return "Остановлена", "paused"
    return "Не запущена", "idle"


def marker_assignments(state: dict[str, Any]) -> dict[str, str]:
    run = state.get("codex_run")
    if isinstance(run, dict) and (not run.get("active") or run.get("phase") in {"launching", "repository"}):
        return {}
    result: dict[str, str] = {}
    for number, worker in (state.get("workers") or {}).items():
        if not isinstance(worker, dict):
            continue
        assigned = [str(value) for value in (worker.get("marker_ids") or [])]
        current_status = str(worker.get("current_status") or "").casefold()
        saved = {str(value) for value in (worker.get("current_saved_marker_ids") or [])}
        for marker_id in assigned:
            if marker_id not in saved and current_status in {"", "assigned", "working", "running"}:
                result[marker_id] = f"Агент {number}"
    for number, verifier in (state.get("verifiers") or {}).items():
        if not isinstance(verifier, dict):
            continue
        assigned = [str(value) for value in (verifier.get("marker_ids") or [])]
        current_status = str(verifier.get("current_status") or "").casefold()
        saved = {str(value) for value in (verifier.get("current_saved_marker_ids") or [])}
        for marker_id in assigned:
            if marker_id not in saved and current_status in {"", "assigned", "working", "running"}:
                result[marker_id] = f"Проверяющий {number}"
    return result


def current_saved_marker_ids(state: dict[str, Any]) -> set[str]:
    """Return drafts saved by the currently running worker/verifier batch."""
    result: set[str] = set()
    for group_name in ("workers", "verifiers"):
        for item in (state.get(group_name) or {}).values():
            if isinstance(item, dict):
                result.update(
                    str(value) for value in (item.get("current_saved_marker_ids") or [])
                )
    return result


def pending_marker_ids(
    decisions: list[dict[str, Any]], state: dict[str, Any],
    draft_ids: set[str] | None = None,
) -> list[str]:
    active = set(marker_assignments(state))
    drafts = draft_ids or set()
    pending = [
        str(item.get("marker_id")) for item in decisions
        if item.get("marker_id") and not item.get("verdict")
        and str(item.get("marker_id")) not in active
        and str(item.get("marker_id")) not in drafts
    ]
    priority = [
        str(value) for value in (state.get("priority_marker_ids") or [])
        if str(value) in pending
    ]
    priority_set = set(priority)
    return priority + [marker_id for marker_id in pending if marker_id not in priority_set]


def current_run_queue_ids(
    decisions: list[dict[str, Any]], state: dict[str, Any],
    draft_ids: set[str], inventory_ids: list[str],
) -> list[str]:
    """Show the current launch or next launch's window, not the project backlog."""
    run = state.get("codex_run")
    active_run = isinstance(run, dict) and bool(run.get("active"))
    if not active_run and state.get("one_shot_completed"):
        return []
    active = set(marker_assignments(state))
    current_saved = current_saved_marker_ids(state)
    priority = [str(value) for value in state.get("priority_marker_ids") or []]
    # Match claim_next_batch: old drafts are not automatically queued, but an
    # explicit user retry must remain visible until it is assigned/completed.
    explicit_retry = (set(priority) if state.get("manual_queue_requested") is True
                      or state.get("single_marker_requested") is True else set())
    pending = pending_marker_ids(decisions, state, draft_ids - explicit_retry)
    pending_set = set(pending)
    if not priority:
        return []
    recheck_set = set(state.get("recheck_marker_ids") or [])
    return [marker_id for marker_id in priority
            if marker_id not in active and marker_id not in current_saved
            and (marker_id in pending_set or marker_id in recheck_set)]


def unapplied_draft_results(
    job: Path, decisions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the latest saved worker result that was not applied atomically."""
    completed = {
        str(item.get("marker_id")) for item in decisions
        if item.get("marker_id") and isinstance(item.get("verdict"), str)
        and item["verdict"] in VALID_VERDICTS
    }
    notes = job / "notes"
    paths = [path for path in notes.iterdir() if DRAFT_NOTE_RE.match(path.name) and not path.is_symlink()] if notes.is_dir() else []
    paths.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
    drafts: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            value = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            value = value.get("decisions")
        if not isinstance(value, list):
            continue
        for raw_item in value:
            if not isinstance(raw_item, dict):
                continue
            item = normalize_worker_result(raw_item)
            marker_id = str(item.get("marker_id") or "")
            if not marker_id or marker_id in completed or (
                not (isinstance(item.get("verdict"), str) and item["verdict"] in VALID_VERDICTS)
                and item.get("analysis_status") != "needs_context"
            ):
                continue
            draft = dict(item)
            draft["_note_file"] = path.name
            draft["_saved_at"] = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(
                timespec="seconds"
            )
            drafts[marker_id] = draft
    incomplete_path = job / "incomplete-analysis.json"
    if incomplete_path.exists():
        try:
            unfinished = read_json(incomplete_path)
            if isinstance(unfinished, dict):
                for marker_id, entry in unfinished.items():
                    if marker_id not in completed and isinstance(entry, dict):
                        drafts[marker_id] = {**drafts.get(marker_id, {}), "marker_id": marker_id,
                                            "analysis_status": "needs_context",
                                            "proof_gaps": [entry.get("reason") or "Требуется продолжение исследования"]}
        except (OSError, json.JSONDecodeError):
            pass
    return drafts


def validate_marker_inventory(payload: Any, expected_filter: str) -> dict[str, Any]:
    """Validate that get_markers returned the complete requested marker scope."""
    if not isinstance(payload, dict):
        raise ValueError("Svacer вернул инвентарь неизвестного формата.")
    markers = payload.get("markers")
    if not isinstance(markers, list):
        raise ValueError("В ответе Svacer отсутствует массив markers.")
    if payload.get("truncated") is not False:
        raise ValueError("Svacer вернул неполный список маркеров (truncated=true).")
    returned = payload.get("returned_count")
    total = payload.get("total_count")
    if returned != total or returned != len(markers):
        raise ValueError(
            f"Svacer вернул неполный инвентарь: получено {returned}, всего {total}."
        )
    applied = payload.get("filters_applied")
    actual_filter = applied.get("advanced_filter") if isinstance(applied, dict) else None
    if actual_filter != expected_filter:
        raise ValueError("Svacer не подтвердил точный фильтр этой задачи.")
    ids = [str(marker.get("id") or "") for marker in markers if isinstance(marker, dict)]
    if len(ids) != len(markers) or any(not marker_id for marker_id in ids):
        raise ValueError("В инвентаре есть маркер без ID или запись неизвестного формата.")
    if len(ids) != len(set(ids)):
        raise ValueError("В инвентаре Svacer обнаружены повторяющиеся marker ID.")
    return payload


def create_user_report(job: Path, target_name: str) -> Path:
    """Build a small human-facing report folder from persisted local decisions."""
    report = job / "ОТЧЁТ"
    report.mkdir(parents=True, exist_ok=True)
    decisions_path = job / "decisions.jsonl"
    decisions = read_jsonl(decisions_path) if decisions_path.exists() else []
    completed = [item for item in decisions if item.get("verdict") in {
        "Confirmed", "False Positive", "Won't fix", "Unclear",
    }]
    pending = [item for item in decisions if not item.get("verdict")]

    columns = [
        "Статус", "Детектор", "Файл", "Строка", "Уверенность", "Комментарий",
        "ID", "Точка входа", "Source", "Проверки", "Sink",
        "Достижимость в сборке", "Достижимость в продукте", "Влияние",
        "Проверка Confirmed",
    ]
    results_path = report / "РЕЗУЛЬТАТЫ.csv"
    with results_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter=";")
        writer.writeheader()
        for item in completed:
            verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
            writer.writerow({
                "Статус": item.get("verdict") or "",
                "Детектор": item.get("warnClass") or "",
                "Файл": item.get("file") or "",
                "Строка": item.get("line") or "",
                "Уверенность": item.get("confidence") or "",
                "Комментарий": comment_without_heading(item.get("comment")),
                "ID": item.get("marker_id") or "",
                "Точка входа": item.get("entrypoint") or "",
                "Source": item.get("source") or "",
                "Проверки": item.get("control") or "",
                "Sink": item.get("sink") or "",
                "Достижимость в сборке": item.get("build_reachability") or "",
                "Достижимость в продукте": item.get("product_reachability") or "",
                "Влияние": item.get("impact") or "",
                "Проверка Confirmed": verification.get("status") or "",
            })

    pending_path = report / "ОЖИДАЮТ.csv"
    with pending_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["Детектор", "Файл", "Строка", "ID"], delimiter=";",
        )
        writer.writeheader()
        for item in pending:
            writer.writerow({
                "Детектор": item.get("warnClass") or "",
                "Файл": item.get("file") or "",
                "Строка": item.get("line") or "",
                "ID": item.get("marker_id") or "",
            })

    state = collect_state(job)
    verdicts = state.get("by_verdict") or {}
    summary = "\n".join((
        f"Компонент: {target_name}",
        f"Задача: {job.name}",
        f"Готово: {state.get('completed', 0)} из {state.get('total', 0)}",
        f"Confirmed: {verdicts.get('Confirmed', 0)}",
        f"False Positive: {verdicts.get('False Positive', 0)}",
        "Won't fix: {}".format(verdicts.get("Won't fix", 0)),
        f"Unclear: {verdicts.get('Unclear', 0)}",
        f"Ожидают анализа: {state.get('pending', 0)}",
        "",
        "РЕЗУЛЬТАТЫ.csv — только уже готовые решения и комментарии.",
        "ОЖИДАЮТ.csv — ещё не обработанные маркеры.",
        "",
        f"Обновлено: {datetime.now().astimezone().isoformat(timespec='seconds')}",
    )) + "\n"
    (report / "СВОДКА.txt").write_text(summary, encoding="utf-8-sig")
    return report


class TriageGui:
    def __init__(self, root: tk.Tk, job: Path, app_directory: Path) -> None:
        self.root = root
        self.job = job
        self.app_directory = app_directory
        self.tool_directory = app_directory.parent
        self.settings = complete_desktop_settings(read_json(app_directory / "svacer-settings.json"))
        self.mcp_url = str(self.settings.get("mcp_url") or "http://127.0.0.1:8002/mcp")
        self.token = os.getenv("SVACER_LOCAL_MCP_TOKEN", "")
        self.busy = False
        self.connected = False
        self.connection_retry_remaining = 0
        self.closed = False
        self.refresh_after_id: str | None = None
        self.connection_after_id: str | None = None
        self.marker_signature: tuple[int, ...] | None = None
        self.inventory_by_id: dict[str, dict[str, Any]] = {}
        self.trace_by_id: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.decision_by_id: dict[str, dict[str, Any]] = {}
        self.draft_by_id: dict[str, dict[str, Any]] = {}
        self.visible_marker_ids: list[str] = []
        self.current_marker_id: str | None = None
        self.current_state: dict[str, Any] = {}
        self.last_codex_run_notice: tuple[Any, ...] | None = None
        self.activity_signature: tuple[int, int] | None = None
        self.job_options: dict[str, Path] = {}
        self.jobs_row_paths: dict[str, Path] = {}
        self.jobs_active_by_path: dict[Path, bool] = {}
        self.jobs_refresh_in_flight = False
        self.jobs_refresh_results: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.active_row_markers: dict[str, str] = {}
        self.pending_row_markers: dict[str, str] = {}
        self.history_project_options: dict[str, Path | None] = {}
        self.history_row_records: dict[str, dict[str, Any]] = {}
        self.history_signature: tuple[Any, ...] | None = None
        self.live_marker_id: str | None = None
        self.latest_activity_entries: list[str] = []
        self.live_display_entries: list[str] = []
        self.live_monitor_window: tk.Toplevel | None = None
        self.monitor_activity_text: tk.Text | None = None
        self.monitor_status_label: tk.Label | None = None
        self.monitor_timing_label: tk.Label | None = None
        self.live_status_color = MUTED
        self.live_timing_color = MUTED
        self.work_queue_signature: tuple[Any, ...] | None = None
        self.codex_limit_data: dict[str, Any] | None = None
        self.codex_limit_updated_at = 0.0
        self.codex_limit_loading = False
        self.jobs_expanded = True
        self.active_expanded = True
        self.pending_expanded = True
        self.jobs_expanded_width = 340
        self.panel_animations: dict[tk.Misc, dict[str, Any]] = {}
        self.window_animations: dict[tk.Toplevel, str] = {}
        self.window_closing: set[tk.Toplevel] = set()
        self.root_size: tuple[int, int] | None = None

        self.job_data, self.target_name = job_identity(job)

        root.title(f"Svacer Triage — {self.target_name}")
        root.configure(bg=BG)
        root.minsize(1060, 720)
        root.geometry("1280x840")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Configure>", self.on_root_resized, add="+")

        self.configure_style()
        self.build_ui()
        self.refresh()
        self.check_connection()

    def configure_style(self) -> None:
        apply_theme(self.root)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(20, 12, 20, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        title_box = ttk.Frame(header)
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="Svacer Triage", style="Title.TLabel").pack(anchor="w")
        self.subtitle_var = tk.StringVar(value=f"{self.target_name}  •  задача {self.job.name}")
        ttk.Label(title_box, textvariable=self.subtitle_var, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        self.connection_label = tk.Label(
            header, text="Svacer: проверка…", bg=SURFACE_2, fg=YELLOW,
            font=("Segoe UI Semibold", 9), padx=12, pady=7,
        )
        self.connection_label.pack(side="right")

        project_bar = ttk.Frame(outer)
        project_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(project_bar, text="Сохранённая задача", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.job_selector_var = tk.StringVar()
        self.job_selector = ttk.Combobox(
            project_bar, textvariable=self.job_selector_var, state="readonly", width=62,
        )
        self.job_selector.pack(side="left", fill="x", expand=True)
        self.job_selector.bind("<<ComboboxSelected>>", self.switch_selected_job)
        self.job_refresh_button = ttk.Button(
            project_bar, text="Обновить список", command=self.refresh_job_selector,
            style="Neutral.TButton",
        )
        self.job_refresh_button.pack(side="left", padx=6)
        self.new_job_button = ttk.Button(
            project_bar, text="Новый проект", command=self.open_new_job_wizard,
            style="Accent.TButton",
        )
        self.new_job_button.pack(side="left")
        self.refresh_job_selector()

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.overview_tab = ttk.Frame(self.notebook, padding=(0, 8, 0, 0))
        self.markers_tab = ttk.Frame(self.notebook, padding=(0, 8, 0, 0))
        self.history_tab = ttk.Frame(self.notebook, padding=(0, 8, 0, 0))
        self.settings_tab = ttk.Frame(self.notebook, padding=(0, 8, 0, 0))
        self.notebook.add(self.overview_tab, text="Обзор")
        self.notebook.add(self.markers_tab, text="Маркеры")
        self.notebook.add(self.history_tab, text="История")
        self.notebook.add(self.settings_tab, text="Настройки")
        self.build_overview()
        self.build_markers()
        self.build_history()
        self.build_settings()
        self.notebook.bind("<<NotebookTabChanged>>", self.on_notebook_tab_changed)
        self.notebook.select(self.overview_tab)

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.workflow_action_box = ttk.Frame(actions)
        self.workflow_action_box.pack(side="left", padx=(0, 5))
        self.fetch_markers_button = ttk.Button(
            self.workflow_action_box, text="Получить маркеры", command=self.fetch_markers,
            style="Accent.TButton",
        )
        self.fetch_markers_button.pack(side="left")
        self.reset_queue_button = ttk.Button(
            self.workflow_action_box, text="Сбросить очередь", command=self.reset_current_queue,
            style="Warning.TButton",
        )
        ttk.Button(actions, text="Открыть отчёт", command=self.open_report, style="Neutral.TButton").pack(side="left", padx=5)
        self.connection_button = ttk.Button(actions, text="Войти в Svacer", command=self.toggle_connection, style="Accent.TButton")
        self.connection_button.pack(side="left", padx=5)
        ttk.Button(actions, text="Обновить", command=self.refresh_and_check, style="Neutral.TButton").pack(side="left", padx=5)
        self.send_button = ttk.Button(actions, text="Отправить в Svacer", command=self.send_import, style="Danger.TButton")
        self.send_button.pack(side="right")

        self.message_var = tk.StringVar(value="Панель обновляется автоматически.")
        self.message_label = tk.Label(
            outer, textvariable=self.message_var, bg=BG, fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left", wraplength=1200,
        )
        self.message_label.grid(row=4, column=0, sticky="ew", pady=(9, 0))

    def build_overview(self) -> None:
        progress_panel = ttk.Frame(self.overview_tab, style="Surface.Card.TFrame", padding=10)
        progress_panel.pack(fill="x")
        top = ttk.Frame(progress_panel, style="Surface.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Прогресс анализа", style="Section.TLabel", background=SURFACE).pack(side="left")
        self.analysis_button = ttk.Button(
            top, text="Начать анализ", command=self.analysis_action,
            style="Success.TButton", width=24,
        )
        self.analysis_button.pack(side="left", padx=(18, 0))
        self.progress_var = tk.StringVar(value="0 из 0")
        ttk.Label(top, textvariable=self.progress_var, style="Surface.TLabel").pack(side="right")
        self.scope_var = tk.StringVar()
        ttk.Label(progress_panel, textvariable=self.scope_var, style="CardTitle.TLabel").pack(anchor="w", pady=(3, 4))
        self.analysis_run_var = tk.StringVar(value="Фоновая задача: ещё не запускалась")
        self.analysis_run_label = tk.Label(
            progress_panel, textvariable=self.analysis_run_var, bg=SURFACE, fg=MUTED,
            font=("Segoe UI Semibold", 9), anchor="w", justify="left",
        )
        self.analysis_run_label.pack(fill="x", pady=(0, 2))
        self.session_usage_var = tk.StringVar()
        ttk.Label(
            progress_panel, textvariable=self.session_usage_var, style="CardTitle.TLabel",
        ).pack(anchor="w", pady=(0, 3))
        self.codex_limit_var = tk.StringVar(value="Доступно Codex: получаю данные…")
        self.codex_limit_label = tk.Label(
            progress_panel, textvariable=self.codex_limit_var, bg=SURFACE, fg=MUTED,
            font=("Segoe UI Semibold", 9), anchor="w",
        )
        self.codex_limit_label.pack(fill="x", pady=(0, 3))
        self.progress = ttk.Progressbar(progress_panel, mode="determinate")
        self.progress.pack(fill="x")

        workspace = ttk.Panedwindow(self.overview_tab, orient="horizontal")
        workspace.pack(fill="both", expand=True, pady=(10, 0))
        self.overview_workspace = workspace

        jobs_panel = ttk.Frame(workspace, style="Surface.Card.TFrame", padding=12)
        marker_panel = ttk.Frame(workspace, style="Surface.Card.TFrame", padding=12)
        self.jobs_panel = jobs_panel
        self.marker_panel = marker_panel
        workspace.add(jobs_panel, weight=1)
        workspace.add(marker_panel, weight=5)

        jobs_head = ttk.Frame(jobs_panel, style="Surface.TFrame")
        self.jobs_head = jobs_head
        jobs_head.pack(fill="x", pady=(0, 7))
        ttk.Label(
            jobs_head, text="Задачи", style="Section.TLabel", background=SURFACE,
        ).pack(side="left")
        self.jobs_toggle_button = ttk.Button(
            jobs_head, text="−", width=3, command=self.toggle_jobs_panel,
            style="Neutral.TButton",
        )
        self.jobs_toggle_button.pack(side="right")
        ttk.Label(
            jobs_head, text="Независимые запуски",
            style="CardTitle.TLabel",
        ).pack(side="left", padx=(10, 0))
        jobs_body = ttk.Frame(jobs_panel, style="Surface.TFrame")
        self.jobs_body = jobs_body
        jobs_body.pack(fill="x")
        jobs_table_frame = ttk.Frame(jobs_body, style="Surface.TFrame")
        self.jobs_table_frame = jobs_table_frame
        jobs_table_frame.pack(fill="x")
        job_columns = ("component", "status", "progress", "active")
        self.jobs_table = ttk.Treeview(
            jobs_table_frame, columns=job_columns, show="headings", height=4,
            selectmode="browse",
        )
        for name, title, width, anchor in (
            ("component", "Компонент", 145, "w"),
            ("status", "Состояние", 78, "w"),
            ("progress", "Готово", 56, "center"),
            ("active", "В работе", 58, "center"),
        ):
            self.jobs_table.heading(name, text=title)
            self.jobs_table.column(name, width=width, anchor=anchor)
        self.jobs_table.tag_configure("running", foreground=GREEN)
        self.jobs_table.tag_configure("failed", foreground=RED)
        self.jobs_table.tag_configure("paused", foreground=YELLOW)
        self.jobs_table.bind("<<TreeviewSelect>>", self.on_monitored_job_selected)
        self.jobs_table.bind("<Double-1>", self.open_selected_monitored_job)
        jobs_scroll = ttk.Scrollbar(
            jobs_table_frame, orient="vertical", command=self.jobs_table.yview,
        )
        self.jobs_table.configure(yscrollcommand=jobs_scroll.set)
        self.jobs_table.pack(side="left", fill="x", expand=True)
        jobs_scroll.pack(side="right", fill="y")
        job_actions = ttk.Frame(jobs_body, style="Surface.TFrame")
        self.job_actions = job_actions
        job_actions.pack(fill="x", pady=(8, 0))
        self.open_monitored_job_button = ttk.Button(
            job_actions, text="Открыть выбранную", command=self.open_selected_monitored_job,
            style="Accent.TButton",
        )
        self.open_monitored_job_button.pack(side="left")
        self.stop_monitored_job_button = ttk.Button(
            job_actions, text="Остановить выбранную", command=self.stop_selected_monitored_job,
            style="Danger.TButton", state="disabled",
        )
        self.stop_monitored_job_button.pack(side="right")

        pending_head = ttk.Frame(jobs_panel, style="Surface.TFrame")
        self.pending_head = pending_head
        pending_head.pack(fill="x", pady=(14, 0))
        self.pending_title_var = tk.StringVar(value="В очереди — 0")
        ttk.Label(
            pending_head, textvariable=self.pending_title_var, style="Section.TLabel",
            background=SURFACE,
        ).pack(side="left")
        self.pending_toggle_button = ttk.Button(
            pending_head, text="−", width=3, command=self.toggle_pending_panel,
            style="Neutral.TButton",
        )
        self.pending_toggle_button.pack(side="right")
        pending_frame = ttk.Frame(jobs_panel, style="Surface.TFrame")
        self.pending_frame = pending_frame
        pending_frame.pack(fill="both", expand=True, pady=(6, 0))
        pending_columns = ("position", "state", "detector", "file", "line")
        self.pending_marker_table = ttk.Treeview(
            pending_frame, columns=pending_columns, show="headings", height=12,
            selectmode="browse",
        )
        for name, title, width, anchor in (
            ("position", "№", 34, "center"),
            ("state", "Статус", 76, "w"),
            ("detector", "Детектор", 120, "w"),
            ("file", "Файл", 145, "w"),
            ("line", "Стр.", 44, "center"),
        ):
            self.pending_marker_table.heading(name, text=title)
            self.pending_marker_table.column(name, width=width, anchor=anchor)
        self.pending_marker_table.tag_configure("pending", foreground=MUTED)
        self.pending_marker_table.tag_configure("draft", foreground=YELLOW)
        self.pending_marker_table.bind(
            "<<TreeviewSelect>>", lambda _event: self.on_monitor_marker_selected("pending"),
        )
        self.pending_marker_table.bind(
            "<Double-1>", lambda _event: self.open_live_marker_card(),
        )
        pending_scroll = ttk.Scrollbar(
            pending_frame, orient="vertical", command=self.pending_marker_table.yview,
        )
        self.pending_marker_table.configure(yscrollcommand=pending_scroll.set)
        self.pending_marker_table.pack(side="left", fill="both", expand=True)
        pending_scroll.pack(side="right", fill="y")

        active_head = ttk.Frame(marker_panel, style="Surface.TFrame")
        active_head.pack(fill="x")
        self.active_title_var = tk.StringVar(value="В работе — 0")
        ttk.Label(
            active_head, textvariable=self.active_title_var, style="Section.TLabel",
            background=SURFACE,
        ).pack(side="left")
        self.active_toggle_button = ttk.Button(
            active_head, text="−", width=3, command=self.toggle_active_panel,
            style="Neutral.TButton",
        )
        self.active_toggle_button.pack(side="right")
        ttk.Label(
            active_head, text="Маркеры сгруппированы по исполнителям",
            style="CardTitle.TLabel",
        ).pack(side="right", padx=(0, 8))
        active_frame = ttk.Frame(marker_panel, style="Surface.TFrame")
        self.active_frame = active_frame
        active_frame.pack(fill="x", pady=(6, 7))
        marker_columns = ("detector", "file", "line")
        self.active_marker_table = ttk.Treeview(
            active_frame, columns=marker_columns, show="tree headings", height=4,
            selectmode="browse",
        )
        self.active_marker_table.heading("#0", text="Исполнитель / №")
        self.active_marker_table.column("#0", width=165, minwidth=135, anchor="w")
        for name, title, width, anchor in (
            ("detector", "Детектор", 170, "w"),
            ("file", "Файл", 210, "w"),
            ("line", "Строка", 60, "center"),
        ):
            self.active_marker_table.heading(name, text=title)
            self.active_marker_table.column(name, width=width, anchor=anchor)
        self.active_marker_table.tag_configure("active", foreground=GREEN)
        self.active_marker_table.tag_configure(
            "agent_group", foreground=YELLOW, font=("Segoe UI Semibold", 9),
        )
        self.active_marker_table.bind(
            "<<TreeviewSelect>>", lambda _event: self.on_monitor_marker_selected("active"),
        )
        self.active_marker_table.bind("<Double-1>", self.open_selected_active_monitor)
        active_scroll = ttk.Scrollbar(
            active_frame, orient="vertical", command=self.active_marker_table.yview,
        )
        self.active_marker_table.configure(yscrollcommand=active_scroll.set)
        self.active_marker_table.pack(side="left", fill="x", expand=True)
        active_scroll.pack(side="right", fill="y")

        live_panel = ttk.Frame(marker_panel, style="Surface2.Card.TFrame", padding=(13, 10))
        self.live_panel = live_panel
        live_panel.pack(fill="both", expand=True, pady=(0, 2))
        live_head = ttk.Frame(live_panel, style="Surface2.TFrame")
        live_head.pack(fill="x")
        self.live_marker_title_var = tk.StringVar(value="Выберите маркер в работе или в очереди")
        ttk.Label(
            live_head, textvariable=self.live_marker_title_var, style="Section.TLabel",
            background=SURFACE_2,
        ).pack(anchor="w", fill="x", pady=(0, 3))
        self.live_open_button = ttk.Button(
            live_head, text="Развернуть", command=self.open_live_monitor_window,
            style="Accent.TButton", state="disabled",
        )
        self.live_open_button.pack(side="right")
        self.live_svacer_button = ttk.Button(
            live_head, text="Открыть в Svacer", command=self.open_live_marker_in_svacer,
            style="Neutral.TButton", state="disabled",
        )
        self.live_svacer_button.pack(side="right", padx=(0, 6))
        self.live_marker_status_var = tk.StringVar(value="Здесь будет текущий этап и исполнитель.")
        ttk.Label(
            live_panel, textvariable=self.live_marker_status_var, style="Surface2.TLabel",
            wraplength=1050,
        ).pack(anchor="w", pady=(3, 0))
        self.live_timing_var = tk.StringVar(value="")
        self.live_timing_label = tk.Label(
            live_panel, textvariable=self.live_timing_var, bg=SURFACE_2, fg=MUTED,
            font=("Segoe UI Semibold", 9), anchor="w", justify="left",
        )
        self.live_timing_label.pack(fill="x")
        ttk.Label(
            live_panel, text="Общий ход текущей партии", style="Surface2Card.TLabel",
        ).pack(anchor="w")
        self.live_activity_scope_var = tk.StringVar(value="")
        tk.Label(
            live_panel, textvariable=self.live_activity_scope_var,
            bg=SURFACE_2, fg=YELLOW, font=("Segoe UI Semibold", 9),
            anchor="w", justify="left", wraplength=1050,
        ).pack(fill="x")
        activity_frame = ttk.Frame(live_panel, style="Surface2.TFrame")
        activity_frame.pack(fill="both", expand=True, pady=(3, 0))
        self.live_activity_text = tk.Text(
            activity_frame, height=6, wrap="word", state="disabled",
            bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", borderwidth=0,
            highlightthickness=0, font=("Segoe UI", 9), padx=9, pady=7,
        )
        live_scroll = ttk.Scrollbar(
            activity_frame, orient="vertical", command=self.live_activity_text.yview,
        )
        self.live_activity_text.configure(yscrollcommand=live_scroll.set)
        self.live_activity_text.pack(side="left", fill="both", expand=True)
        live_scroll.pack(side="right", fill="y")
        self.root.after_idle(self.apply_default_overview_layout)
        workspace.bind("<Configure>", self.fit_overview_rows, add="+")

        self.card_values = {
            key: tk.StringVar(value="0")
            for key in ("confirmed", "fp", "wont", "unclear", "active", "pending")
        }
        self.verification_var = tk.StringVar()
        self.context_var = tk.StringVar()
        self.import_var = tk.StringVar()

    def fit_overview_rows(self, event: Any) -> None:
        # Only presentation changes: all rows remain accessible via the existing scrollbars.
        rowheight = int(ttk.Style(self.root).lookup("Treeview", "rowheight") or 30)
        rows = max(1, min(int((event.height * 0.30 - rowheight) / rowheight),
                          int((event.height - 280) / rowheight)))
        for table, limit in ((self.jobs_table, 4), (self.active_marker_table, 8)):
            height = min(limit, rows)
            if int(table.cget("height")) != height:
                table.configure(height=height)

    def apply_default_overview_layout(self) -> None:
        if self.closed or not self.jobs_expanded:
            return
        try:
            available = self.overview_workspace.winfo_width()
            width = max(360, min(500, int(available * 0.34)))
            self.jobs_expanded_width = width
            self.overview_workspace.sashpos(0, width)
        except tk.TclError:
            pass

    def on_root_resized(self, event: Any) -> None:
        if event.widget is not self.root:
            return
        size = (event.width, event.height)
        if size != self.root_size:
            # Let Tk own layout during live resize/maximize. An old fixed-height
            # animation target would otherwise fight the new window geometry.
            # Treat the first observed root size as a resize too: on slower Tk
            # builds the first Configure event can arrive after an animation
            # has already been scheduled.
            for frame in list(self.panel_animations):
                self._finish_panel_animation(frame)
        self.root_size = size

    def _finish_panel_animation(self, frame: tk.Misc) -> None:
        animation = self.panel_animations.pop(frame, None)
        if animation is None:
            return
        timer = animation.get("after_id")
        if timer is not None:
            self.root.after_cancel(timer)
        if not frame.winfo_exists():
            return
        if not animation["expanded"]:
            frame.pack_forget()
        frame.pack_propagate(True)

    def _animate_panel(self, frame: tk.Misc, expanded: bool, **pack_options: Any) -> None:
        previous = self.panel_animations.pop(frame, None)
        if previous is not None and previous.get("after_id") is not None:
            self.root.after_cancel(previous["after_id"])
        was_packed = bool(frame.winfo_manager())
        current = max(1, int(frame.cget("height"))) if previous else (
            max(1, frame.winfo_height()) if was_packed else 1
        )
        open_height = previous["open_height"] if previous else current
        if expanded and not was_packed:
            frame.pack(**pack_options)
            frame.pack_propagate(True)
            self.root.update_idletasks()
            open_height = max(1, frame.winfo_height())
        target = open_height if expanded else 1
        if not self.root.winfo_viewable() or abs(target - current) < 2:
            if not expanded:
                frame.pack_forget()
            frame.pack_propagate(True)
            return

        frame.pack_propagate(False)
        frame.configure(height=current)
        animation: dict[str, Any] = {
            "expanded": expanded, "open_height": open_height, "after_id": None,
            "from_height": current, "to_height": target, "started": time.monotonic(),
        }
        self.panel_animations[frame] = animation

        def step() -> None:
            if self.panel_animations.get(frame) is not animation:
                return
            animation["after_id"] = None
            if self.closed or not frame.winfo_exists():
                self.panel_animations.pop(frame, None)
                return
            progress = min(1.0, (time.monotonic() - animation["started"]) / 0.16)
            eased = progress * progress * (3.0 - 2.0 * progress)
            height = round(animation["from_height"] + (
                animation["to_height"] - animation["from_height"]
            ) * eased)
            frame.configure(height=max(1, height))
            if progress >= 1.0:
                self._finish_panel_animation(frame)
            else:
                animation["after_id"] = self.root.after(16, step)

        animation["after_id"] = self.root.after(16, step)

    def toggle_jobs_panel(self) -> None:
        if self.jobs_expanded:
            self._animate_panel(self.jobs_body, False)
            self.jobs_toggle_button.configure(text="+")
            self.jobs_expanded = False
        else:
            self._animate_panel(self.jobs_body, True, fill="x", after=self.jobs_head)
            self.jobs_toggle_button.configure(text="−")
            self.jobs_expanded = True

    def _set_jobs_width(self, width: int) -> None:
        try:
            self.overview_workspace.sashpos(0, width)
        except tk.TclError:
            pass

    def toggle_active_panel(self) -> None:
        if self.active_expanded:
            self._animate_panel(self.active_frame, False)
            self.active_toggle_button.configure(text="+")
        else:
            self._animate_panel(self.active_frame, True, fill="x", pady=(6, 7), before=self.live_panel)
            self.active_toggle_button.configure(text="−")
        self.active_expanded = not self.active_expanded

    def toggle_pending_panel(self) -> None:
        if self.pending_expanded:
            self._animate_panel(self.pending_frame, False)
            self.pending_toggle_button.configure(text="+")
        else:
            self._animate_panel(self.pending_frame, True, fill="both", expand=True, pady=(6, 0))
            self.pending_toggle_button.configure(text="−")
        self.pending_expanded = not self.pending_expanded

    def build_markers(self) -> None:
        toolbar = ttk.Frame(self.markers_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="Поиск", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.search_var = tk.StringVar()
        search = tk.Entry(
            toolbar, textvariable=self.search_var, bg=SURFACE_2, fg=TEXT,
            insertbackground=TEXT, relief="flat", font=("Segoe UI", 10),
        )
        search.pack(side="left", fill="x", expand=True, ipady=7)
        search.bind("<KeyRelease>", lambda _event: self.apply_marker_filter())
        self.filter_var = tk.StringVar(value="Все маркеры")
        verdict_filter = ttk.Combobox(toolbar, textvariable=self.filter_var, values=list(FILTERS), state="readonly", width=21)
        verdict_filter.pack(side="left", padx=(8, 0), ipady=4)
        verdict_filter.bind("<<ComboboxSelected>>", lambda _event: self.apply_marker_filter())
        self.marker_count_var = tk.StringVar(value="0 маркеров")
        ttk.Label(toolbar, textvariable=self.marker_count_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))

        paned = ttk.Panedwindow(self.markers_tab, orient="horizontal")
        paned.pack(fill="both", expand=True)
        list_panel = ttk.Frame(paned, style="Surface.Card.TFrame", padding=12)
        detail_panel = ttk.Frame(paned, style="Surface.Card.TFrame", padding=12)
        paned.add(list_panel, weight=2)
        paned.add(detail_panel, weight=3)

        columns = ("status", "detector", "file", "line")
        self.marker_table = ttk.Treeview(list_panel, columns=columns, show="headings", selectmode="browse")
        for name, title, width, anchor in (
            ("status", "Статус", 120, "w"), ("detector", "Детектор", 160, "w"),
            ("file", "Файл", 180, "w"), ("line", "Строка", 62, "center"),
        ):
            self.marker_table.heading(name, text=title)
            self.marker_table.column(name, width=width, anchor=anchor)
        self.marker_table.tag_configure("pending", foreground=MUTED)
        self.marker_table.tag_configure("draft", foreground=YELLOW)
        self.marker_table.tag_configure("confirmed", foreground=GREEN)
        self.marker_table.tag_configure("fp", foreground=BLUE)
        self.marker_table.tag_configure("wont", foreground=YELLOW)
        self.marker_table.tag_configure("unclear", foreground=PURPLE)
        self.marker_table.bind("<<TreeviewSelect>>", self.on_marker_selected)
        marker_scroll = ttk.Scrollbar(list_panel, orient="vertical", command=self.marker_table.yview)
        self.marker_table.configure(yscrollcommand=marker_scroll.set)
        self.marker_table.pack(side="left", fill="both", expand=True)
        marker_scroll.pack(side="right", fill="y")

        detail_head = ttk.Frame(detail_panel, style="Surface.TFrame")
        detail_head.pack(fill="x", pady=(0, 8))
        ttk.Label(detail_head, text="Карточка маркера", style="Section.TLabel", background=SURFACE).pack(anchor="w", pady=(0, 8))
        detail_actions = ttk.Frame(detail_head, style="Surface.TFrame")
        detail_actions.pack(fill="x")
        decision_actions = ttk.Frame(detail_head, style="Surface.TFrame")
        decision_actions.pack(fill="x", pady=(6, 0))
        self.triage_one_button = ttk.Button(
            decision_actions, text="Разметить только этот", command=self.queue_selected_marker,
            style="Success.TButton",
        )
        self.triage_one_button.pack(side="right")
        self.approve_draft_button = ttk.Button(
            decision_actions, text="Подтвердить черновик", command=self.approve_current_draft,
            style="Accent.TButton",
        )
        self.marker_history_button = ttk.Button(
            detail_actions, text="История", command=self.show_current_marker_history,
            style="Neutral.TButton", state="disabled",
        )
        self.marker_history_button.pack(side="right", padx=(0, 6))
        self.edit_decision_button = ttk.Button(
            detail_actions, text="Изменить поля", command=self.edit_current_decision,
            style="Warning.TButton", state="disabled",
        )
        self.edit_decision_button.pack(side="right", padx=(0, 6))
        self.open_svacer_button = ttk.Button(
            detail_actions, text="Открыть в Svacer", command=self.open_marker_in_svacer,
            style="Accent.TButton",
        )
        self.open_svacer_button.pack(side="right", padx=(0, 6))

        detail_body = ttk.Frame(detail_panel, style="Surface.TFrame")
        detail_body.pack(fill="both", expand=True)
        self.detail_text = tk.Text(
            detail_body, bg=SURFACE, fg=TEXT, insertbackground=TEXT, relief="flat",
            wrap="word", font=("Segoe UI", 10), padx=8, pady=4, spacing1=2, spacing3=5,
        )
        self.detail_text.tag_configure("title", font=("Segoe UI Semibold", 15), foreground=TEXT, spacing3=3)
        self.detail_text.tag_configure("meta", font=("Segoe UI", 9), foreground=MUTED, spacing3=10)
        self.detail_text.tag_configure("section", font=("Segoe UI Semibold", 10), foreground=BLUE, spacing1=10, spacing3=3)
        self.detail_text.tag_configure("body", foreground=TEXT)
        self.detail_text.tag_configure("pending", foreground=MUTED)
        detail_scroll = ttk.Scrollbar(detail_body, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=detail_scroll.set, state="disabled")
        self.detail_text.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.render_empty_detail()

    def build_history(self) -> None:
        container = ttk.Frame(self.history_tab, padding=(12, 4, 12, 12))
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container, style="Surface2.Card.TFrame", padding=(16, 12))
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(
            header, text="История анализа маркеров", style="Section.TLabel",
            background=SURFACE_2,
        ).pack(side="left")
        self.history_project_var = tk.StringVar(value="Все проекты")
        self.history_project_combo = ttk.Combobox(
            header, textvariable=self.history_project_var, state="readonly", width=54,
        )
        self.history_project_combo.pack(side="right", padx=(8, 0))
        self.history_project_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.refresh_history_table(force=True),
        )
        ttk.Label(
            header, text="Проект", style="Surface2Card.TLabel",
        ).pack(side="right")
        ttk.Button(
            header, text="Обновить", command=lambda: self.refresh_history_table(force=True),
            style="Neutral.TButton",
        ).pack(side="right", padx=(0, 12))

        self.history_summary_var = tk.StringVar(value="История пока пуста")
        ttk.Label(
            container, textvariable=self.history_summary_var, style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        history_split = ttk.Panedwindow(container, orient="vertical")
        history_split.pack(fill="both", expand=True)

        table_panel = ttk.Frame(history_split, style="Surface.Card.TFrame", padding=12)
        detail_panel = ttk.Frame(history_split, style="Surface2.Card.TFrame", padding=12)
        history_split.add(table_panel, weight=3)
        history_split.add(detail_panel, weight=2)

        columns = ("time", "project", "detector", "file", "line", "verdict", "duration", "tokens")
        self.history_table = ttk.Treeview(
            table_panel, columns=columns, show="headings", selectmode="browse", height=12,
        )
        for name, title, width, anchor in (
            ("time", "Запуск", 125, "w"),
            ("project", "Проект", 190, "w"),
            ("detector", "Детектор", 170, "w"),
            ("file", "Файл", 225, "w"),
            ("line", "Стр.", 55, "center"),
            ("verdict", "Результат", 105, "w"),
            ("duration", "Время", 80, "e"),
            ("tokens", "Токены", 95, "e"),
        ):
            self.history_table.heading(name, text=title)
            self.history_table.column(name, width=width, anchor=anchor)
        self.history_table.tag_configure("confirmed", foreground=GREEN)
        self.history_table.tag_configure("false_positive", foreground=BLUE)
        self.history_table.tag_configure("wont_fix", foreground=YELLOW)
        self.history_table.tag_configure("unclear", foreground=PURPLE)
        self.history_table.tag_configure("failed", foreground=RED)
        self.history_table.bind("<<TreeviewSelect>>", self.on_history_selected)
        self.history_table.bind("<Double-1>", self.open_history_marker)
        history_scroll = ttk.Scrollbar(
            table_panel, orient="vertical", command=self.history_table.yview,
        )
        self.history_table.configure(yscrollcommand=history_scroll.set)
        self.history_table.pack(side="left", fill="both", expand=True)
        history_scroll.pack(side="right", fill="y")

        detail_head = ttk.Frame(detail_panel, style="Surface2.TFrame")
        detail_head.pack(fill="x", pady=(0, 7))
        self.history_detail_title_var = tk.StringVar(value="Выберите запуск маркера")
        ttk.Label(
            detail_head, textvariable=self.history_detail_title_var,
            style="Section.TLabel", background=SURFACE_2,
        ).pack(side="left")
        self.history_open_button = ttk.Button(
            detail_head, text="Открыть маркер", command=self.open_history_marker,
            style="Accent.TButton", state="disabled",
        )
        self.history_open_button.pack(side="right")

        detail_body = ttk.Frame(detail_panel, style="Surface2.TFrame")
        detail_body.pack(fill="both", expand=True)
        self.history_detail_text = tk.Text(
            detail_body, wrap="word", state="disabled", bg=BG, fg=TEXT,
            insertbackground=TEXT, relief="flat", borderwidth=0,
            highlightthickness=0, font=("Segoe UI", 10), padx=12, pady=10,
            spacing1=2, spacing3=5,
        )
        self.history_detail_text.tag_configure(
            "heading", foreground=BLUE, font=("Segoe UI Semibold", 11), spacing1=7,
        )
        self.history_detail_text.tag_configure("meta", foreground=MUTED)
        self.history_detail_text.tag_configure("value", foreground=TEXT)
        self.history_detail_text.tag_configure("agent", foreground=PURPLE)
        detail_scroll = ttk.Scrollbar(
            detail_body, orient="vertical", command=self.history_detail_text.yview,
        )
        self.history_detail_text.configure(yscrollcommand=detail_scroll.set)
        self.history_detail_text.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.refresh_history_projects()
        self.refresh_history_table(force=True)

    def on_notebook_tab_changed(self, _event: Any = None) -> None:
        if self.notebook.select() == str(self.history_tab):
            self.refresh_history_table()
        else:
            self.refresh_jobs_table()

    def refresh_history_projects(self) -> None:
        current = self.history_project_var.get() if hasattr(self, "history_project_var") else "Все проекты"
        options: dict[str, Path | None] = {"Все проекты": None}
        for path in list_saved_jobs(self.tool_directory):
            try:
                label = job_selector_label(path)
            except (OSError, json.JSONDecodeError):
                continue
            if label in options:
                label = f"{label}  [{path.parent.name}]"
            options[label] = path.resolve()
        self.history_project_options = options
        values = tuple(options)
        if tuple(self.history_project_combo.cget("values")) != values:
            self.history_project_combo.configure(values=values)
        selected = current if current in options else "Все проекты"
        if self.history_project_var.get() != selected:
            self.history_project_var.set(selected)

    @staticmethod
    def format_history_time(value: Any) -> str:
        try:
            return datetime.fromisoformat(str(value)).astimezone().strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def format_history_duration(value: Any) -> str:
        try:
            seconds = max(0, round(float(value)))
        except (TypeError, ValueError):
            return "—"
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours} ч {minutes:02d} мин"
        if minutes:
            return f"{minutes} мин {seconds:02d} с"
        return f"{seconds} с"

    @staticmethod
    def history_row_tag(record: dict[str, Any]) -> str:
        if record.get("status") == "failed":
            return "failed"
        return {
            "Confirmed": "confirmed",
            "False Positive": "false_positive",
            "Won't fix": "wont_fix",
            "Unclear": "unclear",
        }.get(str(record.get("verdict") or ""), "")

    def refresh_history_table(self, *, force: bool = False) -> None:
        self.refresh_history_projects()
        selected_path = self.history_project_options.get(self.history_project_var.get())
        paths = [selected_path] if selected_path is not None else list_saved_jobs(self.tool_directory)
        signature_parts: list[tuple[Any, ...]] = []
        for path in paths:
            values: list[Any] = [str(path.resolve())]
            # Live command events are intentionally excluded: durable history is
            # appended once per completed batch, so the tab does not reparse large
            # JSONL logs every second while an agent is working.
            for name in ("marker-history.jsonl", "decisions.jsonl"):
                try:
                    stat = (path / name).stat()
                    values.extend((stat.st_mtime_ns, stat.st_size))
                except OSError:
                    values.extend((0, 0))
            signature_parts.append(tuple(values))
        signature = (self.history_project_var.get(), tuple(signature_parts))
        if not force and signature == self.history_signature:
            return
        self.history_signature = signature

        records: list[dict[str, Any]] = []
        for path in paths:
            try:
                _job_data, target = job_identity(path)
                job_records = load_marker_history(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for record in job_records:
                value = dict(record)
                value["_job_path"] = str(path.resolve())
                value["_project_label"] = target
                records.append(value)
        records.sort(key=lambda value: str(value.get("started_at") or ""), reverse=True)

        previous_attempt = None
        selection = self.history_table.selection()
        if selection:
            previous_attempt = self.history_row_records.get(selection[0], {}).get("attempt_id")
        self.history_table.delete(*self.history_table.get_children())
        self.history_row_records = {}
        selected_row = ""
        for position, record in enumerate(records):
            identity = str(record.get("attempt_id") or f"{record.get('marker_id')}:{position}")
            row_id = "history-" + hashlib.sha1(
                (str(record.get("_job_path")) + identity).encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()[:20]
            self.history_row_records[row_id] = record
            previous = previous_history_attempt(records, position)
            measurement = history_measurements(record, previous)
            duration_text = self.format_history_duration(measurement.get("duration_seconds"))
            token_text = (
                format_count(int(measurement["tokens"]))
                if measurement.get("tokens") is not None else "Не измерено"
            )
            if measurement.get("inherited_from_previous"):
                duration_text += " · исходно"
                token_text += " · исходно"
            else:
                if str(measurement.get("duration_scope") or "").startswith("От начала"):
                    duration_text += " · до сохранения"
                elif str(measurement.get("duration_scope") or "").startswith("Общее"):
                    duration_text += " · партия"
                if str(measurement.get("token_scope") or "").startswith("Общий"):
                    token_text += " · общие"
            values = (
                self.format_history_time(record.get("started_at")),
                record.get("_project_label") or "—",
                record.get("warnClass") or "—",
                Path(str(record.get("file") or "")).name or "—",
                record.get("line") or "—",
                record.get("verdict") or ("Не завершён" if record.get("status") == "incomplete" else "Ошибка" if record.get("status") == "failed" else "Без результата"),
                duration_text,
                token_text,
            )
            tag = self.history_row_tag(record)
            self.history_table.insert(
                "", "end", iid=row_id, values=values, tags=(tag,) if tag else (),
            )
            if identity == previous_attempt:
                selected_row = row_id

        measurements = [
            history_measurements(record, previous_history_attempt(records, index))
            for index, record in enumerate(records)
        ]
        exact = sum(
            1 for item in measurements
            if item.get("tokens") is not None and str(item.get("token_scope") or "").startswith("Точный")
        )
        shared = sum(
            1 for item in measurements
            if item.get("tokens") is not None and not str(item.get("token_scope") or "").startswith("Точный")
        )
        missing = sum(1 for item in measurements if item.get("tokens") is None)
        suffix = (
            f"  •  точный расход {exact}  •  общий расход партии {shared}  •  без замера {missing}"
            if records else ""
        )
        self.history_summary_var.set(f"Записей с результатом или ошибкой: {len(records)}{suffix}")
        if selected_row:
            self.history_table.selection_set(selected_row)
            self.history_table.see(selected_row)
        elif records:
            first = self.history_table.get_children()[0]
            self.history_table.selection_set(first)
        self.on_history_selected()

    def selected_history_record(self) -> dict[str, Any] | None:
        selection = self.history_table.selection()
        return self.history_row_records.get(selection[0]) if selection else None

    def on_history_selected(self, _event: Any = None) -> None:
        record = self.selected_history_record()
        self.history_open_button.configure(state="normal" if record else "disabled")
        text = self.history_detail_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        if not record:
            self.history_detail_title_var.set("Выберите запуск маркера")
            text.insert("end", "История появится после запуска анализа.", "meta")
            text.configure(state="disabled")
            return

        title = f"{record.get('warnClass') or 'Маркер'} — {Path(str(record.get('file') or '')).name}:{record.get('line') or '—'}"
        self.history_detail_title_var.set(title)
        row_index = next(
            (index for index, item in enumerate(self.history_row_records.values()) if item is record),
            -1,
        )
        ordered_records = list(self.history_row_records.values())
        previous = previous_history_attempt(ordered_records, row_index)
        comparison = compare_history_attempts(previous, record)
        measurement = history_measurements(record, previous)
        duration = self.format_history_duration(measurement.get("duration_seconds"))
        tokens = (
            format_count(int(measurement["tokens"]))
            if measurement.get("tokens") is not None else "Не измерено"
        )
        measured_record = measurement.get("source_record") if isinstance(measurement.get("source_record"), dict) else record
        usage = measured_record.get("batch_usage") if isinstance(measured_record.get("batch_usage"), dict) else {}
        lines = (
            ("Маркер", str(record.get("marker_id") or "—")),
            ("Проект", f"{record.get('_project_label') or '—'}  •  задача {record.get('job_id') or '—'}"),
            ("Общий запуск", f"{self.format_history_time(record.get('started_at'))}  •  исполнитель {record.get('worker') or '—'}"),
            ("Результат", str(record.get("verdict") or record.get("status") or "—")),
            ("Время", f"{duration}  •  {measurement.get('duration_scope')}"),
            ("Токены", f"{tokens}  •  {measurement.get('token_scope')}"),
            (
                "Общий расход исходного запуска",
                f"маркеров {measured_record.get('batch_marker_count') or 1}  •  всего {format_count(int(measured_record.get('batch_total_tokens') or 0))}  •  "
                f"вход {format_count(int(usage.get('input_tokens') or 0))}  •  кэш {format_count(int(usage.get('cached_input_tokens') or 0))}  •  "
                f"выход {format_count(int(usage.get('output_tokens') or 0))}",
            ),
        )
        for label, value in lines:
            text.insert("end", label + ": ", "meta")
            text.insert("end", value + "\n", "value")

        if measurement.get("inherited_from_previous"):
            text.insert("end", "Локальная повторная проверка не запускала модель; показаны измерения исходной попытки.\n", "meta")
        elif int(measured_record.get("batch_marker_count") or 1) > 1:
            text.insert(
                "end", "Время измерено до сохранения результата агентом; токены относятся ко всей параллельной партии.\n", "meta",
            )

        text.insert("end", "\nСравнение с предыдущей попыткой\n", "heading")
        if previous is None:
            text.insert("end", "Предыдущей попытки для этого маркера нет.\n", "meta")
        else:
            before = previous.get("decision_snapshot") if isinstance(previous.get("decision_snapshot"), dict) else {}
            after = record.get("decision_snapshot") if isinstance(record.get("decision_snapshot"), dict) else {}
            text.insert("end", "Статус: ", "meta")
            text.insert("end", f"{previous.get('status') or '—'} → {record.get('status') or '—'}\n", "value")
            text.insert("end", "Вердикт: ", "meta")
            text.insert("end", f"{previous.get('verdict') or before.get('verdict') or '—'} → {record.get('verdict') or after.get('verdict') or '—'}\n", "value")
            if comparison.get("decision_unchanged"):
                text.insert("end", "Вердикт и доказательства не изменились; черновик прошёл строгую повторную проверку.\n", "value")
            elif comparison.get("changes"):
                for change in comparison["changes"]:
                    text.insert("end", str(change["label"]) + ": ", "meta")
                    text.insert("end", f"{change['before']} → {change['after']}\n", "value")
            else:
                text.insert("end", "Для старой попытки детальная копия решения не сохранилась.\n", "meta")

        messages = record.get("agent_messages") if isinstance(record.get("agent_messages"), list) else []
        text.insert("end", "\nСообщения агента\n", "heading")
        if record.get("messages_scope") == "batch" and messages:
            text.insert(
                "end",
                "Сообщения относятся ко всей параллельной партии; маркер указан в тексте, когда агент его сообщил.\n\n",
                "meta",
            )
        if messages:
            for index, message in enumerate(messages, 1):
                text.insert("end", f"{index:02d}  ", "meta")
                text.insert("end", str(message) + "\n\n", "agent")
        else:
            text.insert("end", "Для этого старого запуска сообщения не сохранились.\n", "meta")
        text.configure(state="disabled")

    def open_history_marker(self, _event: Any = None) -> None:
        record = self.selected_history_record()
        if not record:
            return
        path = Path(str(record.get("_job_path") or ""))
        marker_id = str(record.get("marker_id") or "")
        if not path.is_dir() or not marker_id:
            self.set_message("Сохранённая задача маркера больше недоступна.", error=True)
            return
        if path.resolve() != self.job.resolve():
            self.activate_job(path)
        self.filter_var.set("Все маркеры")
        self.search_var.set("")
        self.apply_marker_filter()
        if marker_id in self.marker_table.get_children():
            self.marker_table.selection_set(marker_id)
            self.marker_table.see(marker_id)
            self.render_marker(marker_id)
            self.notebook.select(self.markers_tab)
        else:
            self.set_message("Маркер найден в истории, но отсутствует в текущем инвентаре.", error=True)

    def build_settings(self) -> None:
        container = ttk.Frame(self.settings_tab, padding=(12, 4, 12, 12))
        container.pack(fill="both", expand=True)

        heading = ttk.Frame(container, style="Surface2.Card.TFrame", padding=(18, 14))
        heading.pack(fill="x", pady=(0, 10))
        ttk.Label(
            heading, text="Параметры анализа", style="Section.TLabel",
            background=SURFACE_2,
        ).pack(anchor="w")
        ttk.Label(
            heading,
            text=(
                "Настройки сохраняются только для выбранной задачи и начинают действовать "
                "со следующей партии. Нажатие «Применить» не запускает анализ."
            ),
            style="Surface2Card.TLabel", wraplength=1050,
        ).pack(anchor="w", pady=(4, 0))

        mode_panel = ttk.Frame(container, style="Surface.Card.TFrame", padding=18)
        mode_panel.pack(fill="x", pady=(0, 10))
        ttk.Label(mode_panel, text="Режим обработки", style="Section.TLabel", background=SURFACE).pack(anchor="w")
        ttk.Label(
            mode_panel,
            text="Режим применяется на границе партии и не прерывает уже работающих агентов.",
            style="CardTitle.TLabel",
        ).pack(anchor="w", pady=(3, 12))
        self.run_mode_var = tk.StringVar(value=str(self.job_data.get("run_mode") or "until_complete"))
        ttk.Radiobutton(
            mode_panel, text="Обработать заданное число маркеров и остановиться",
            variable=self.run_mode_var, value="single_batch", command=self.update_mode_help,
        ).pack(anchor="w", pady=3)
        ttk.Radiobutton(
            mode_panel, text="До завершения — автоматически брать следующие партии",
            variable=self.run_mode_var, value="until_complete", command=self.update_mode_help,
        ).pack(anchor="w", pady=3)
        self.mode_help_var = tk.StringVar()
        ttk.Label(mode_panel, textvariable=self.mode_help_var, style="CardTitle.TLabel", wraplength=1050).pack(anchor="w", pady=(10, 0))

        capacity = ttk.Frame(container, style="Surface.Card.TFrame", padding=18)
        capacity.pack(fill="x", pady=(0, 10))
        ttk.Label(capacity, text="Размер работы", style="Section.TLabel", background=SURFACE).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        self.workers_var = tk.StringVar(value=str(self.job_data.get("parallel_workers") or 1))
        self.batch_size_var = tk.StringVar(value=str(self.job_data.get("batch_size") or 15))
        ttk.Label(capacity, text="Одновременных агентов", style="Surface.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(capacity, from_=1, to=8, textvariable=self.workers_var, width=8).grid(row=1, column=1, sticky="w", padx=(12, 35))
        ttk.Label(capacity, text="Маркеров за один запуск", style="Surface.TLabel").grid(row=1, column=2, sticky="w")
        ttk.Spinbox(capacity, from_=1, to=50, textvariable=self.batch_size_var, width=8).grid(row=1, column=3, sticky="w", padx=(12, 0))
        ttk.Label(
            capacity,
            text="Один агент обрабатывает один маркер за раз. Лимит запуска действует в режиме одной партии; до завершения обрабатывается вся очередь.",
            style="CardTitle.TLabel", wraplength=1050,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.capacity_preview_var = tk.StringVar()
        tk.Label(
            capacity, textvariable=self.capacity_preview_var, bg=SURFACE, fg=YELLOW,
            font=("Segoe UI Semibold", 9), anchor="w", justify="left", wraplength=1050,
        ).grid(row=3, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        self.workers_var.trace_add("write", self.update_capacity_preview)
        self.batch_size_var.trace_add("write", self.update_capacity_preview)

        save_panel = ttk.Frame(container, style="Surface2.Card.TFrame", padding=(18, 13))
        save_panel.pack(fill="x")
        ttk.Button(
            save_panel, text="Применить", command=self.save_execution_settings,
            style="Accent.TButton",
        ).pack(side="left")
        self.update_mode_help()
        self.update_capacity_preview()

    def update_mode_help(self) -> None:
        if self.run_mode_var.get() == "single_batch":
            text = "Маркеры берутся из очереди по одному на исполнителя. После указанного числа маркеров запуск завершится; «Начать анализ» запустит следующий."
        else:
            text = "После сохранения партии Codex продолжает брать работу, пока не останется ожидающих маркеров или вы не завершите анализ."
        self.mode_help_var.set(text)

    def update_capacity_preview(self, *_args: Any) -> None:
        try:
            workers = max(1, int(self.workers_var.get()))
            batch_size = max(1, int(self.batch_size_var.get()))
        except (TypeError, ValueError):
            self.capacity_preview_var.set("Укажите целые значения для агентов и размера партии.")
            return
        active_workers = min(workers, batch_size)
        if active_workers == 1:
            hint = "Последовательно: один маркер → сохранение результата → следующий из очереди."
        else:
            hint = f"Одновременно до {active_workers} маркеров: по одному на агента. Остальные ожидают в очереди."
        self.capacity_preview_var.set(hint)

    def refresh_job_selector(self) -> None:
        current = self.job.resolve()
        options: dict[str, Path] = {}
        for path in list_saved_jobs(self.tool_directory):
            try:
                label = job_selector_label(path)
            except (OSError, json.JSONDecodeError):
                continue
            if label in options:
                label = f"{label}  [{path.parent.name}]"
            options[label] = path.resolve()
        self.job_options = options
        values = list(options)
        if tuple(self.job_selector.cget("values")) != tuple(values):
            self.job_selector.configure(values=values)
        selected = next((label for label, path in options.items() if path == current), "")
        if selected and self.job_selector_var.get() != selected:
            self.job_selector_var.set(selected)

    def refresh_jobs_table(self) -> None:
        if self.jobs_refresh_in_flight or self.notebook.select() != str(self.overview_tab):
            return
        self.jobs_refresh_in_flight = True

        def collect() -> None:
            rows: list[tuple[str, Path, tuple[Any, ...], str, bool]] = []
            try:
                for path in list_saved_jobs(self.tool_directory):
                    try:
                        _data, target = job_identity(path)
                        state = collect_state(path)
                        run = read_run_record(path)
                        state["codex_run"] = run
                    except (OSError, ValueError, json.JSONDecodeError):
                        continue
                    resolved = path.resolve()
                    row_id = "job-" + hashlib.sha1(
                        str(resolved).casefold().encode("utf-8"), usedforsecurity=False,
                    ).hexdigest()[:16]
                    status, tag = friendly_run_state(run, state)
                    rows.append((
                        row_id, resolved,
                        (
                            f"{target} · {compact_job_timestamp(path)}",
                            status,
                            f"{state.get('completed', 0)}/{state.get('total', 0)}",
                            len(active_marker_ids(state)),
                        ),
                        tag, bool(run.get("active")),
                    ))
            except Exception as exc:
                self.jobs_refresh_results.put(exc)
                return
            self.jobs_refresh_results.put(rows)

        threading.Thread(target=collect, daemon=True).start()
        self.root.after(80, self.drain_jobs_refresh)

    def drain_jobs_refresh(self) -> None:
        if self.closed:
            return
        try:
            result = self.jobs_refresh_results.get_nowait()
        except queue.Empty:
            if self.jobs_refresh_in_flight:
                self.root.after(80, self.drain_jobs_refresh)
            return
        if isinstance(result, Exception):
            self.jobs_refresh_in_flight = False
            self.set_message(f"Ошибка обновления списка задач: {result}", error=True)
            return
        self.apply_jobs_rows(result)

    def apply_jobs_rows(
        self, rows: list[tuple[str, Path, tuple[Any, ...], str, bool]],
    ) -> None:
        self.jobs_refresh_in_flight = False
        selected_path = self.selected_monitored_job()
        desired = selected_path or self.job.resolve()
        selected_row = next((row_id for row_id, path, *_ in rows if path == desired), "")
        existing = set(self.jobs_table.get_children())
        wanted = {row_id for row_id, *_ in rows}
        for row_id in existing - wanted:
            self.jobs_table.delete(row_id)
        self.jobs_row_paths = {}
        self.jobs_active_by_path = {}
        for position, (row_id, path, values, tag, active) in enumerate(rows):
            self.jobs_row_paths[row_id] = path
            self.jobs_active_by_path[path] = active
            if row_id in existing:
                current = tuple(self.jobs_table.item(row_id, "values"))
                expected = tuple(str(value) for value in values)
                if current != expected or tuple(self.jobs_table.item(row_id, "tags")) != (tag,):
                    self.jobs_table.item(row_id, values=values, tags=(tag,))
                if self.jobs_table.index(row_id) != position:
                    self.jobs_table.move(row_id, "", position)
            else:
                self.jobs_table.insert(
                    "", position, iid=row_id, values=values, tags=(tag,),
                )
        if selected_row and self.jobs_table.selection() != (selected_row,):
            self.jobs_table.selection_set(selected_row)
            self.jobs_table.see(selected_row)
        self.on_monitored_job_selected()

    def refresh_work_queue_tables(self, state: dict[str, Any]) -> None:
        assignments = marker_assignments(state)
        active_ids = list(assignments)
        triage_ids = [
            str(marker["id"]) for marker in markers_for_triage(list(self.inventory_by_id.values()))
        ]
        queued_ids = current_run_queue_ids(
            self.decisions, state, set(self.draft_by_id), triage_ids,
        )
        selected_marker = self.live_marker_id
        signature = (
            tuple(assignments.items()), tuple(queued_ids), tuple(sorted(self.draft_by_id)),
        )
        if signature == self.work_queue_signature:
            self.refresh_live_marker_monitor(state)
            return
        self.work_queue_signature = signature

        children = self.active_marker_table.get_children()
        if children:
            self.active_marker_table.delete(*children)
        self.active_row_markers = {}
        grouped: dict[str, list[str]] = {}
        for marker_id in active_ids:
            grouped.setdefault(assignments[marker_id], []).append(marker_id)
        selected_row = ""
        for group_index, (agent, marker_ids) in enumerate(grouped.items(), 1):
            parent_id = f"active-group-{group_index}"
            self.active_marker_table.insert(
                "", "end", iid=parent_id, text=f"{agent} — назначено {len(marker_ids)}",
                values=("Назначенная группа", "", ""), tags=("agent_group",), open=True,
            )
            for marker_index, marker_id in enumerate(marker_ids, 1):
                decision = self.decision_by_id.get(marker_id, {})
                marker = self.inventory_by_id.get(marker_id, {})
                row_id = f"active-{group_index}-{marker_index}"
                self.active_row_markers[row_id] = marker_id
                self.active_marker_table.insert(
                    parent_id, "end", iid=row_id, text=f"Маркер {marker_index}/{len(marker_ids)}",
                    values=(
                        decision.get("warnClass") or marker.get("warnClass") or "—",
                        short_file(decision.get("file") or marker.get("file")),
                        decision.get("line") or marker.get("line") or "—",
                    ),
                    tags=("active",),
                )
                if marker_id == selected_marker:
                    selected_row = row_id

        children = self.pending_marker_table.get_children()
        if children:
            self.pending_marker_table.delete(*children)
        self.pending_row_markers = {}
        for position, marker_id in enumerate(queued_ids, 1):
            decision = self.decision_by_id.get(marker_id, {})
            marker = self.inventory_by_id.get(marker_id, {})
            row_id = f"pending-{position}"
            self.pending_row_markers[row_id] = marker_id
            self.pending_marker_table.insert(
                "", "end", iid=row_id,
                values=(
                    position,
                    "Ожидает",
                    decision.get("warnClass") or marker.get("warnClass") or "—",
                    short_file(decision.get("file") or marker.get("file")),
                    decision.get("line") or marker.get("line") or "—",
                ),
                tags=("pending",),
            )
            if marker_id == selected_marker:
                self.pending_marker_table.selection_set(row_id)
                self.pending_marker_table.see(row_id)

        self.active_title_var.set(
            f"В работе — исполнителей: {len(grouped)} • назначено маркеров: {len(active_ids)}"
        )
        queue_title = (
            "В очереди текущего запуска"
            if (state.get("codex_run") or {}).get("active") else "Следующие к запуску"
        )
        self.pending_title_var.set(f"{queue_title} — {len(queued_ids)}")
        if selected_marker not in set(active_ids) | set(queued_ids):
            self.live_marker_id = active_ids[0] if active_ids else (queued_ids[0] if queued_ids else None)
            selected_row = next(
                (
                    row_id for row_id, marker_id in self.active_row_markers.items()
                    if marker_id == self.live_marker_id
                ),
                "",
            )
        if selected_row:
            self.active_marker_table.selection_set(selected_row)
            self.active_marker_table.see(selected_row)
        self.refresh_live_marker_monitor(state)

    def on_monitor_marker_selected(self, source: str) -> None:
        table = self.active_marker_table if source == "active" else self.pending_marker_table
        mapping = self.active_row_markers if source == "active" else self.pending_row_markers
        selection = table.selection()
        marker_id = mapping.get(selection[0]) if selection else None
        if not marker_id:
            return
        self.live_marker_id = marker_id
        other = self.pending_marker_table if source == "active" else self.active_marker_table
        other.selection_remove(other.selection())
        self.refresh_live_marker_monitor(self.current_state)

    def open_selected_active_monitor(self, _event: Any = None) -> None:
        selection = self.active_marker_table.selection()
        if selection and selection[0] in self.active_row_markers:
            self.open_live_monitor_window()

    def refresh_live_marker_monitor(self, state: dict[str, Any]) -> None:
        marker_id = self.live_marker_id
        if not marker_id:
            self.live_marker_title_var.set("Нет маркеров в работе или в очереди")
            self.live_marker_status_var.set("")
            self.live_timing_var.set("")
            self.live_activity_scope_var.set("")
            self.live_status_color = MUTED
            self.live_timing_color = MUTED
            self.sync_live_monitor_colors()
            self.set_live_activity([])
            self.live_open_button.configure(state="disabled")
            self.live_svacer_button.configure(state="disabled")
            return
        decision = self.decision_by_id.get(marker_id, {})
        marker = self.inventory_by_id.get(marker_id, {})
        detector = decision.get("warnClass") or marker.get("warnClass") or "Маркер"
        file_name = decision.get("file") or marker.get("file") or ""
        line = decision.get("line") or marker.get("line") or "—"
        assignments = marker_assignments(state)
        triage_ids = [
            str(marker["id"]) for marker in markers_for_triage(list(self.inventory_by_id.values()))
        ]
        queued = current_run_queue_ids(
            self.decisions, state, set(self.draft_by_id), triage_ids,
        )
        if marker_id in assignments:
            run = state.get("codex_run") or {}
            phase = str(run.get("phase_detail") or latest_codex_activity(self.job))
            agent = assignments[marker_id]
            agent_markers = [
                assigned_id for assigned_id, assigned_agent in assignments.items()
                if assigned_agent == agent
            ]
            marker_position = agent_markers.index(marker_id) + 1
            status = (
                f"В работе • {agent} • маркер {marker_position} из {len(agent_markers)} "
                f"у этого исполнителя • {phase}"
            )
            executor_count = len(set(assignments.values()))
            if len(assignments) == 1:
                scope = "Эта партия содержит один маркер: сообщения ниже относятся к нему."
            else:
                scope = (
                    f"Это общий поток всей партии — исполнителей: {executor_count}, "
                    f"маркеров: {len(assignments)}. Он одинаков для строк и не является "
                    "комментарием выбранного маркера. Индивидуальный комментарий появится "
                    "в карточке после сохранения решения."
                )
            self.live_activity_scope_var.set(scope)
            timing, timing_color = live_run_timing(self.job, run)
            self.live_timing_var.set(timing)
            self.live_timing_label.configure(fg=timing_color)
            self.live_status_color = GREEN
            self.live_timing_color = timing_color
        elif marker_id in self.draft_by_id:
            draft = self.draft_by_id[marker_id]
            status = (
                "Не завершён • требуется продолжение исследования"
                if draft.get("analysis_status") == "needs_context" or draft.get("verdict") == "Unclear" else
                f"Черновик сохранён • {draft.get('verdict')} • "
                "партия была остановлена до атомарного применения"
            )
            self.live_timing_var.set("")
            self.live_activity_scope_var.set(
                "Для этого маркера уже есть индивидуальный черновик — откройте его карточку."
            )
            self.live_status_color = YELLOW
            self.live_timing_color = MUTED
        else:
            try:
                position = queued.index(marker_id) + 1
                status = f"В очереди • позиция {position} из {len(queued)}"
            except ValueError:
                status = str(decision.get("verdict") or "Состояние изменилось")
            self.live_timing_var.set("")
            self.live_activity_scope_var.set(
                "Маркер ещё не выполняется, поэтому индивидуальных сообщений по нему пока нет."
            )
            self.live_status_color = BLUE if marker_id in queued else MUTED
            self.live_timing_color = MUTED
        self.live_marker_title_var.set(f"{detector} — {short_file(file_name)}:{line}")
        self.live_marker_status_var.set(f"{status}  •  ID {marker_id}")
        self.sync_live_monitor_colors()
        recent = self.latest_activity_entries[-6:] if marker_id in assignments else []
        fallback = (
            "Откройте карточку: в ней виден сохранённый черновик."
            if marker_id in self.draft_by_id
            else "Нажмите «Открыть карточку», чтобы посмотреть описание и трассу."
        )
        self.set_live_activity(recent or [fallback])
        self.live_open_button.configure(state="normal")
        url = marker_svacer_url(
            str(self.job_data.get("snapshot_url") or ""), marker_id, str(file_name),
        )
        self.live_svacer_button.configure(state="normal" if url else "disabled")

    def set_live_activity(self, entries: list[str]) -> None:
        self.live_display_entries = list(entries)
        self.render_activity_widget(self.live_activity_text, entries)
        if self.monitor_activity_text is not None and self.monitor_activity_text.winfo_exists():
            self.render_activity_widget(
                self.monitor_activity_text, self.expanded_live_activity_entries(),
            )

    def expanded_live_activity_entries(self) -> list[str]:
        if (
            self.live_marker_id in marker_assignments(self.current_state)
            and self.latest_activity_entries
        ):
            return list(self.latest_activity_entries)
        return list(self.live_display_entries)

    @staticmethod
    def activity_entry_tag(entry: str) -> str:
        lowered = entry.casefold()
        if "ошиб" in lowered or "не удалось" in lowered or "завис" in lowered:
            return "error"
        if "сохран" in lowered or "заверш" in lowered or "получен результат" in lowered:
            return "success"
        if "предупреж" in lowered or "долгий" in lowered or "ожида" in lowered:
            return "warning"
        if entry.startswith("Агент:"):
            return "agent"
        if entry.startswith("Сейчас:") or "анализир" in lowered or "выполняет" in lowered:
            return "active"
        return "normal"

    def render_activity_widget(self, widget: tk.Text, entries: list[str]) -> None:
        previous_entries = list(getattr(widget, "_activity_entries", []))
        if previous_entries == entries:
            return

        yview = widget.yview()
        was_at_bottom = not previous_entries or not yview or yview[1] >= 0.995
        common_prefix = 0
        for previous, current in zip(previous_entries, entries):
            if previous != current:
                break
            common_prefix += 1

        append_only = common_prefix == len(previous_entries)
        if previous_entries and not append_only:
            overlap = 0
            for length in range(min(len(previous_entries), len(entries)), 0, -1):
                if previous_entries[-length:] == entries[:length]:
                    overlap = length
                    break
            animated_from = max(common_prefix, overlap)
        elif previous_entries:
            animated_from = common_prefix
        else:
            # The first render should be immediately readable. Only later updates animate.
            animated_from = len(entries)

        generation = int(getattr(widget, "_activity_animation_generation", 0)) + 1
        widget._activity_animation_generation = generation
        widget.configure(state="normal")
        for tag_name in widget.tag_names():
            if tag_name.startswith("activity_number_animation_") or tag_name.startswith(
                "activity_body_animation_"
            ):
                widget.tag_delete(tag_name)
        if not append_only:
            widget.delete("1.0", "end")
            insert_from = 0
        else:
            insert_from = common_prefix

        widget.tag_configure(
            "line_number", foreground=ACTIVITY_TAG_COLORS["line_number"],
            font=("Cascadia Mono", 10),
        )
        widget.tag_configure("normal", foreground=ACTIVITY_TAG_COLORS["normal"])
        widget.tag_configure(
            "active", foreground=ACTIVITY_TAG_COLORS["active"],
            font=("Segoe UI Semibold", 11),
        )
        widget.tag_configure(
            "success", foreground=ACTIVITY_TAG_COLORS["success"],
            font=("Segoe UI Semibold", 11),
        )
        widget.tag_configure(
            "warning", foreground=ACTIVITY_TAG_COLORS["warning"],
            font=("Segoe UI Semibold", 11),
        )
        widget.tag_configure(
            "error", foreground=ACTIVITY_TAG_COLORS["error"],
            font=("Segoe UI Semibold", 11),
        )
        widget.tag_configure("agent", foreground=ACTIVITY_TAG_COLORS["agent"])

        animated_ranges: list[tuple[str, str, str, str, str]] = []
        for index, entry in enumerate(entries[insert_from:], insert_from + 1):
            number_start = widget.index("end-1c")
            widget.insert("end", f"{index:02d}  ", "line_number")
            number_end = widget.index("end-1c")
            body_start = number_end
            entry_tag = self.activity_entry_tag(entry)
            widget.insert("end", entry.strip(), entry_tag)
            body_end = widget.index("end-1c")
            widget.insert("end", "\n\n")
            if index > animated_from:
                animated_ranges.append(
                    (number_start, number_end, body_start, body_end, entry_tag)
                )

        widget._activity_entries = list(entries)
        widget.configure(state="disabled")
        if was_at_bottom:
            widget.see("end")
        elif not append_only and yview:
            widget.yview_moveto(yview[0])

        # Hidden tabs must be ready to read on return, not replay their backlog.
        if not widget.winfo_viewable() or len(animated_ranges) != 1:
            return
        for position, ranges in enumerate(animated_ranges):
            self.animate_activity_entry(
                widget, generation, ranges, delay_ms=min(position * 35, 140),
            )

    def animate_activity_entry(
        self,
        widget: tk.Text,
        generation: int,
        ranges: tuple[str, str, str, str, str],
        *,
        delay_ms: int,
    ) -> None:
        number_start, number_end, body_start, body_end, entry_tag = ranges
        tag_suffix = f"{generation}_{body_start.replace('.', '_')}"
        number_animation_tag = f"activity_number_animation_{tag_suffix}"
        body_animation_tag = f"activity_body_animation_{tag_suffix}"
        widget.configure(state="normal")
        widget.tag_add(number_animation_tag, number_start, number_end)
        widget.tag_add(body_animation_tag, body_start, body_end)
        widget.tag_configure(
            number_animation_tag, foreground=BG, offset=ACTIVITY_ANIMATION_OFFSET_PX,
        )
        widget.tag_configure(
            body_animation_tag, foreground=BG, offset=ACTIVITY_ANIMATION_OFFSET_PX,
        )
        widget.configure(state="disabled")

        def draw_frame(frame: int) -> None:
            try:
                if self.closed or not widget.winfo_exists():
                    return
                if getattr(widget, "_activity_animation_generation", 0) != generation:
                    return
                progress = frame / ACTIVITY_ANIMATION_FRAMES
                eased = progress * progress * (3.0 - 2.0 * progress)
                offset = round(ACTIVITY_ANIMATION_OFFSET_PX * (1.0 - eased))
                widget.tag_configure(
                    number_animation_tag,
                    foreground=self.blend_color(
                        BG, ACTIVITY_TAG_COLORS["line_number"], eased,
                    ),
                    offset=offset,
                )
                widget.tag_configure(
                    body_animation_tag,
                    foreground=self.blend_color(
                        BG, ACTIVITY_TAG_COLORS[entry_tag], eased,
                    ),
                    offset=offset,
                )
                if frame >= ACTIVITY_ANIMATION_FRAMES:
                    widget.tag_delete(number_animation_tag)
                    widget.tag_delete(body_animation_tag)
                    return
                self.root.after(
                    ACTIVITY_ANIMATION_INTERVAL_MS,
                    lambda: draw_frame(frame + 1),
                )
            except tk.TclError:
                return

        self.root.after(delay_ms, lambda: draw_frame(0))

    @staticmethod
    def blend_color(start: str, end: str, progress: float) -> str:
        progress = max(0.0, min(1.0, progress))
        start_rgb = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
        end_rgb = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
        blended = tuple(
            round(source + (target - source) * progress)
            for source, target in zip(start_rgb, end_rgb)
        )
        return "#{:02x}{:02x}{:02x}".format(*blended)

    def sync_live_monitor_colors(self) -> None:
        if self.monitor_status_label is not None and self.monitor_status_label.winfo_exists():
            self.monitor_status_label.configure(fg=self.live_status_color)
        if self.monitor_timing_label is not None and self.monitor_timing_label.winfo_exists():
            self.monitor_timing_label.configure(fg=self.live_timing_color)

    def _cancel_window_animation(self, window: tk.Toplevel) -> None:
        timer = self.window_animations.pop(window, None)
        if timer is not None:
            self.root.after_cancel(timer)

    def _animate_window(
        self, window: tk.Toplevel, *, closing: bool = False,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        """Fade only the window opacity; native geometry stays resizable."""
        self._cancel_window_animation(window)
        if not window.winfo_exists():
            return
        try:
            start = float(window.attributes("-alpha"))
        except (tk.TclError, TypeError, ValueError):
            if closing and on_closed is not None:
                on_closed()
            return
        target = 0.78 if closing else 1.0
        started = time.monotonic()
        duration = 0.10 if closing else 0.14

        def step() -> None:
            self.window_animations.pop(window, None)
            if self.closed or not window.winfo_exists():
                return
            progress = min(1.0, (time.monotonic() - started) / duration)
            eased = progress * progress * (3.0 - 2.0 * progress)
            try:
                window.attributes("-alpha", start + (target - start) * eased)
            except tk.TclError:
                progress = 1.0
            if progress >= 1.0:
                if closing and on_closed is not None:
                    on_closed()
            else:
                self.window_animations[window] = self.root.after(20, step)

        self.window_animations[window] = self.root.after(20, step)

    def _monitor_destroyed(self, event: Any, window: tk.Toplevel) -> None:
        if event.widget is not window:
            return
        self._cancel_window_animation(window)
        self.window_closing.discard(window)
        if self.live_monitor_window is window:
            self.live_monitor_window = None
            self.monitor_activity_text = None
            self.monitor_status_label = None
            self.monitor_timing_label = None

    def close_live_monitor_window(self) -> None:
        window = self.live_monitor_window
        if window is None or not window.winfo_exists() or window in self.window_closing:
            return
        self.window_closing.add(window)
        self._animate_window(window, closing=True, on_closed=window.destroy)

    def open_live_monitor_window(self) -> None:
        if not self.live_marker_id:
            return
        if self.live_monitor_window is not None and self.live_monitor_window.winfo_exists():
            self.window_closing.discard(self.live_monitor_window)
            self._animate_window(self.live_monitor_window)
            self.live_monitor_window.deiconify()
            try:
                self.live_monitor_window.state("zoomed")
            except tk.TclError:
                pass
            self.live_monitor_window.lift()
            self.live_monitor_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        style_window(window)
        self.live_monitor_window = window
        window.bind("<Destroy>", lambda event: self._monitor_destroyed(event, window), add="+")
        window.title(f"Ход анализа — {self.live_marker_title_var.get()}")
        window.configure(bg=BG)
        try:
            window.attributes("-alpha", 0.82)
        except tk.TclError:
            pass
        window.minsize(900, 620)
        window.geometry("1280x820")
        window.protocol("WM_DELETE_WINDOW", self.close_live_monitor_window)
        window.bind("<Escape>", lambda _event: self.close_live_monitor_window())
        try:
            window.state("zoomed")
        except tk.TclError:
            pass

        outer = ttk.Frame(window, padding=18)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="Surface2.Card.TFrame", padding=(20, 16))
        header.pack(fill="x", pady=(0, 12))
        controls = ttk.Frame(header, style="Surface2.TFrame")
        controls.pack(side="right", padx=(16, 0))
        ttk.Button(
            controls, text="Закрыть", command=self.close_live_monitor_window,
            style="Neutral.TButton",
        ).pack(side="right")
        ttk.Button(
            controls, text="Карточка маркера", command=self.open_live_marker_card,
            style="Accent.TButton",
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            controls, text="Открыть в Svacer", command=self.open_live_marker_in_svacer,
            style="Neutral.TButton",
        ).pack(side="right", padx=(0, 8))
        tk.Label(
            header, textvariable=self.live_marker_title_var, bg=SURFACE_2, fg=TEXT,
            font=("Segoe UI Semibold", 20), anchor="w", justify="left",
        ).pack(fill="x")
        self.monitor_status_label = tk.Label(
            header, textvariable=self.live_marker_status_var, bg=SURFACE_2,
            fg=self.live_status_color, font=("Segoe UI Semibold", 11),
            anchor="w", justify="left", wraplength=1250,
        )
        self.monitor_status_label.pack(fill="x", pady=(9, 0))
        self.monitor_timing_label = tk.Label(
            header, textvariable=self.live_timing_var, bg=SURFACE_2,
            fg=self.live_timing_color, font=("Segoe UI Semibold", 11),
            anchor="w", justify="left",
        )
        self.monitor_timing_label.pack(fill="x", pady=(5, 0))
        tk.Label(
            header, textvariable=self.live_activity_scope_var, bg=SURFACE_2,
            fg=YELLOW, font=("Segoe UI Semibold", 10), anchor="w",
            justify="left", wraplength=1250,
        ).pack(fill="x", pady=(7, 0))

        body = ttk.Frame(outer, style="Surface.Card.TFrame", padding=(18, 15))
        body.pack(fill="both", expand=True)
        ttk.Label(
            body, text="Общий ход текущей партии", style="Section.TLabel", background=SURFACE,
        ).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "Показываются сообщения координатора без скрытых рассуждений модели. "
                "Отдельный комментарий маркера появляется только после сохранения решения."
            ),
            style="CardTitle.TLabel",
        ).pack(anchor="w", pady=(3, 10))
        text_frame = ttk.Frame(body, style="Surface.TFrame")
        text_frame.pack(fill="both", expand=True)
        self.monitor_activity_text = tk.Text(
            text_frame, wrap="word", state="disabled", bg=BG, fg=TEXT,
            insertbackground=TEXT, relief="flat", borderwidth=0, highlightthickness=0,
            font=("Segoe UI", 11), padx=16, pady=14, spacing1=2, spacing3=7,
        )
        scrollbar = ttk.Scrollbar(
            text_frame, orient="vertical", command=self.monitor_activity_text.yview,
        )
        self.monitor_activity_text.configure(yscrollcommand=scrollbar.set)
        self.monitor_activity_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.render_activity_widget(
            self.monitor_activity_text, self.expanded_live_activity_entries(),
        )
        self.sync_live_monitor_colors()
        self._animate_window(window)

    def open_live_marker_card(self) -> None:
        marker_id = self.live_marker_id
        if not marker_id:
            return
        self.filter_var.set("Все маркеры")
        self.search_var.set("")
        self.apply_marker_filter()
        if marker_id in self.marker_table.get_children():
            self.marker_table.selection_set(marker_id)
            self.marker_table.see(marker_id)
            self.render_marker(marker_id)
            self.notebook.select(self.markers_tab)

    def open_live_marker_in_svacer(self) -> None:
        marker_id = self.live_marker_id
        if not marker_id:
            return
        decision = self.decision_by_id.get(marker_id, {})
        marker = self.inventory_by_id.get(marker_id, {})
        url = marker_svacer_url(
            str(self.job_data.get("snapshot_url") or ""), marker_id,
            str(decision.get("file") or marker.get("file") or ""),
        )
        if not url:
            self.set_message("Для этого маркера не удалось сформировать ссылку Svacer.", error=True)
            return
        try:
            os.startfile(url)
            self.set_message("Маркер открыт в Svacer.")
        except OSError as exc:
            self.set_message(f"Не удалось открыть маркер в Svacer: {exc}", error=True)

    def switch_selected_job(self, _event: Any = None) -> None:
        candidate = self.job_options.get(self.job_selector_var.get())
        if candidate is None or candidate == self.job.resolve():
            return
        self.activate_job(candidate)

    def activate_job(self, candidate: Path) -> None:
        try:
            self.job = candidate.resolve()
            self.job_data, self.target_name = job_identity(candidate)
            self.root.title(f"Svacer Triage — {self.target_name}")
            self.subtitle_var.set(f"{self.target_name}  •  задача {self.job.name}")
            self.run_mode_var.set(str(self.job_data.get("run_mode") or "until_complete"))
            self.workers_var.set(str(self.job_data.get("parallel_workers") or 1))
            self.batch_size_var.set(str(self.job_data.get("batch_size") or 15))
            self.update_mode_help()
            self.marker_signature = None
            self.current_marker_id = None
            self.current_state = {}
            self.last_codex_run_notice = None
            self.activity_signature = None
            self.latest_activity_entries = []
            self.live_marker_id = None
            self.work_queue_signature = None
            self.inventory_by_id = {}
            self.trace_by_id = {}
            self.decisions = []
            self.decision_by_id = {}
            self.draft_by_id = {}
            self.search_var.set("")
            self.filter_var.set("Все маркеры")
            self.refresh()
            self.set_message(
                f"Открыта сохранённая задача {self.target_name}. Предыдущая задача осталась на диске без изменений."
            )
            self.last_codex_run_notice = None
        except Exception as exc:
            self.set_message(f"Не удалось открыть задачу: {exc}", error=True)
            self.refresh_job_selector()

    def selected_monitored_job(self) -> Path | None:
        selection = self.jobs_table.selection()
        return self.jobs_row_paths.get(selection[0]) if selection else None

    def on_monitored_job_selected(self, _event: Any = None) -> None:
        path = self.selected_monitored_job()
        active = bool(path and self.jobs_active_by_path.get(path, False))
        self.open_monitored_job_button.configure(state="normal" if path else "disabled")
        self.stop_monitored_job_button.configure(
            state="normal" if path and active and not self.busy else "disabled",
        )

    def open_selected_monitored_job(self, _event: Any = None) -> None:
        path = self.selected_monitored_job()
        if path is None:
            self.set_message("Сначала выберите задачу в списке.", error=True)
            return
        if path == self.job.resolve():
            return
        self.activate_job(path)

    def stop_selected_monitored_job(self) -> None:
        path = self.selected_monitored_job()
        if path is None:
            self.set_message("Сначала выберите работающую задачу.", error=True)
            return
        self.confirm_stop_job(path)

    def open_new_job_wizard(self) -> None:
        script = self.app_directory / "new_triage_job.ps1"
        if not script.exists():
            self.set_message(f"Не найден {script.name}.", error=True)
            return
        try:
            subprocess.Popen(
                [
                    "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script), "-NoDashboard",
                ],
                cwd=str(self.app_directory),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            self.set_message(
                "Открыт мастер нового проекта. Текущая задача сохранена; после создания нажмите «Обновить список»."
            )
        except OSError as exc:
            self.set_message(f"Не удалось открыть мастер: {exc}", error=True)

    def fetch_markers(self) -> None:
        """Load and validate the initial marker inventory from Svacer."""
        job = self.job.resolve()
        inventory_path = job / "markers.inventory.json"
        decisions_path = job / "decisions.jsonl"

        def work() -> dict[str, Any]:
            job_data = read_json(job / "job.json")
            advanced_filter = str(job_data.get("advanced_filter") or "")
            configured_filter = str(self.settings.get("advanced_filter") or "")
            required = ("project_id", "branch_id", "snapshot_id")
            missing = [name for name in required if not str(job_data.get(name) or "").strip()]
            if missing:
                raise ValueError(f"В job.json отсутствуют поля: {', '.join(missing)}")
            if not advanced_filter:
                raise ValueError("В job.json отсутствует advanced_filter.")
            if advanced_filter != configured_filter:
                raise ValueError("Фильтр задачи не совпадает с фильтром программы.")

            downloaded = False
            if inventory_path.exists():
                inventory = validate_marker_inventory(read_json(inventory_path), advanced_filter)
            else:
                status = check_mcp(self.mcp_url, self.token)
                if status != "подключён":
                    raise RuntimeError(
                        "Svacer не подключён. Сначала нажмите «Войти в Svacer» и завершите вход."
                    )
                reply = asyncio.run(call_mcp_tool(
                    self.mcp_url,
                    self.token,
                    "get_markers",
                    {
                        "project_id": str(job_data["project_id"]),
                        "branch_id": str(job_data["branch_id"]),
                        "snapshot_id": str(job_data["snapshot_id"]),
                        "advanced_filter": advanced_filter,
                        "traces": False,
                        "checker_info": False,
                        "review_history": False,
                        "comment_history": False,
                        "fields": MARKER_INVENTORY_FIELDS,
                        "limit": 0,
                    },
                ))
                inventory = validate_marker_inventory(json.loads(reply), advanced_filter)
                atomic_json(inventory_path, inventory)
                downloaded = True

            if not decisions_path.exists():
                completed = subprocess.run(
                    [
                        console_python_executable(),
                        str(self.app_directory / "make_mcp_decisions_template.py"),
                        "--inventory", str(inventory_path),
                        "--out", str(decisions_path),
                    ],
                    cwd=str(self.app_directory), capture_output=True, text=True,
                    encoding="utf-8", check=False,
                    **hidden_subprocess_kwargs(),
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout).strip()
                    raise RuntimeError(detail or "Не удалось создать локальную очередь маркеров.")

            decisions = load_decisions(decisions_path)
            return {
                "inventory_total": len(inventory.get("markers") or []),
                "triage_total": len(decisions),
                "downloaded": downloaded,
            }

        def done(result: dict[str, Any]) -> None:
            self.marker_signature = None
            self.refresh()
            self.set_message(
                f"Маркеры загружены: {result['inventory_total']}; для анализа: "
                f"{result['triage_total']}. Выберите нужные маркеры и добавьте их в очередь."
            )

        self.run_background(
            work,
            done,
            "Получаю маркеры из Svacer и готовлю локальную очередь…",
        )

    @staticmethod
    def validated_execution_settings(workers: Any, batch_size: Any, run_mode: Any) -> tuple[int, int, str]:
        try:
            worker_count = int(workers)
            marker_count = int(batch_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("Число агентов и размер партии должны быть целыми числами.") from exc
        if worker_count < 1 or worker_count > 8:
            raise ValueError("Число агентов должно быть от 1 до 8.")
        if marker_count < 1 or marker_count > 50:
            raise ValueError("Размер партии должен быть от 1 до 50.")
        mode = str(run_mode)
        if mode not in {"single_batch", "until_complete"}:
            raise ValueError("Выберите режим обработки.")
        return worker_count, marker_count, mode

    def save_execution_settings(self) -> None:
        try:
            workers, batch_size, run_mode = self.validated_execution_settings(
                self.workers_var.get(), self.batch_size_var.get(), self.run_mode_var.get()
            )
            job_data = read_json(self.job / "job.json")
            job_data.update({
                "parallel_workers": workers,
                "batch_size": batch_size,
                "run_mode": run_mode,
            })
            atomic_json(self.job / "job.json", job_data)
            self.job_data = job_data

            # Saving preferences must not resume work or change stop controls.

            mode_label = "одна партия" if run_mode == "single_batch" else "до завершения"
            run_active = bool(read_run_record(self.job).get("active"))
            suffix = (
                "Текущий анализ продолжится; изменения начнут действовать со следующей партии."
                if run_active else
                "Фоновый анализ сейчас не запущен; запустить его можно на вкладке «Обзор»."
            )
            self.set_message(
                f"Настройки применены: режим «{mode_label}», агентов {workers}, "
                f"маркеров в партии {batch_size}. {suffix}"
            )
            self.refresh()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.set_message(f"Не удалось сохранить настройки: {exc}", error=True)

    def set_message(self, text: str, *, error: bool = False) -> None:
        self.message_var.set(text)
        self.message_label.configure(fg=RED if error else MUTED)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.send_button.configure(state=state)
        self.connection_button.configure(state=state)
        self.triage_one_button.configure(state=state)
        self.approve_draft_button.configure(state=state)
        if busy:
            self.edit_decision_button.configure(state="disabled")
        else:
            self.update_selected_marker_action()
        self.fetch_markers_button.configure(state=state)
        self.analysis_button.configure(state=state)
        self.open_monitored_job_button.configure(state=state)
        self.stop_monitored_job_button.configure(state=state)
        self.job_selector.configure(state="disabled" if busy else "readonly")
        self.job_refresh_button.configure(state=state)
        self.new_job_button.configure(state=state)

    def run_background(self, work: Callable[[], Any], done: Callable[[Any], None], busy_message: str) -> None:
        if self.busy:
            self.set_message("Дождитесь завершения текущей операции.", error=True)
            return
        self.set_busy(True)
        self.set_message(busy_message)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                self._post_to_ui(lambda error=exc: self.background_error(error))
                return
            self._post_to_ui(lambda: self.background_done(done, result))

        threading.Thread(target=runner, daemon=True).start()

    def _post_to_ui(self, callback: Callable[[], None]) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: None if self.closed else callback())
        except (RuntimeError, tk.TclError):
            # The main window may have closed between the worker's check and after().
            pass

    def background_error(self, exc: Exception) -> None:
        self.set_busy(False)
        self.set_message(f"Ошибка: {friendly_mcp_error(exc)}", error=True)

    def background_done(self, done: Callable[[Any], None], result: Any) -> None:
        self.set_busy(False)
        done(result)

    def refresh(self) -> None:
        if self.closed:
            return
        if self.refresh_after_id is not None:
            self.root.after_cancel(self.refresh_after_id)
        self.refresh_after_id = None
        try:
            state = collect_state(self.job)
            state["codex_run"] = read_run_record(self.job)
            self.current_state = state
            total = int(state["total"])
            completed = int(state["completed"])
            percent = (100.0 * completed / total) if total else 0.0
            self.progress.configure(maximum=max(total, 1), value=completed)
            self.progress_var.set(f"{completed} из {total}  •  {percent:.1f}%")
            queue_text = "остановлена" if state["paused"] else "работает"
            self.scope_var.set(
                f"В снимке {state['inventory_total']}  •  ранее размечено {state['already_reviewed']}  •  "
                f"в этой задаче {total}  •  очередь {queue_text}"
            )
            run = state["codex_run"]
            activity = latest_codex_activity(self.job) if run.get("active") else ""
            if run.get("active") and activity in {"запускается", "инициализируется", "готовит следующий шаг"}:
                activity = str(run.get("phase_detail") or activity)
            run_text, run_color = format_codex_run_status(
                self.target_name, self.job.name, run, activity, self.job,
            )
            self.analysis_run_var.set(run_text)
            self.analysis_run_label.configure(fg=run_color)
            self.refresh_codex_limit_if_needed()
            self.refresh_activity_feed(run)
            verdicts = state["by_verdict"]
            self.card_values["confirmed"].set(str(verdicts.get("Confirmed", 0)))
            self.card_values["fp"].set(str(verdicts.get("False Positive", 0)))
            self.card_values["wont"].set(str(verdicts.get("Won't fix", 0)))
            self.card_values["unclear"].set(str(verdicts.get("Unclear", 0)))
            self.card_values["active"].set(str(len(active_marker_ids(state))))
            self.card_values["pending"].set(str(state["pending"]))
            verification = state["verification"]
            required = sum(verification.values())
            self.verification_var.set(
                f"Независимая проверка Confirmed: {verification.get('verified', 0)} из {required}  •  "
                f"ожидает {verification.get('pending', 0)}  •  оспорено {verification.get('challenged', 0)}"
            )
            context = state["context"]
            self.context_var.set(
                f"Сохранённые материалы: ≈{format_count(int(context.get('estimated_tokens') or 0))} токенов  •  "
                f"ответов агентов {context.get('primary_calls', 0)}  •  проверок {context.get('verifier_calls', 0)}"
            )
            usage = run.get("usage") if isinstance(run.get("usage"), dict) else read_codex_usage(self.job)
            input_tokens = int(usage.get("input_tokens") or 0)
            cached_tokens = int(usage.get("cached_input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = input_tokens + output_tokens
            if total_tokens:
                self.session_usage_var.set(
                    f"Фактический расход этой задачи: {format_count(total_tokens)} токенов  •  "
                    f"кэш {format_count(cached_tokens)}  •  выход {format_count(output_tokens)}"
                )
            else:
                self.session_usage_var.set("Фактический расход появится после завершения первого сеанса Codex.")
            self.import_var.set(f"Отправка: {self.friendly_import_status(state['import'])}")
            self.reload_markers_if_changed()
            self.update_workflow_action(state)
            self.refresh_work_queue_tables(state)
            self.update_selected_marker_action()
            self.refresh_job_selector()
            self.refresh_jobs_table()
            if self.notebook.select() == str(self.history_tab):
                self.refresh_history_table()
            if state["errors"]:
                self.set_message("; ".join(state["errors"]), error=True)
            else:
                self.update_codex_run_notice(state["codex_run"])
        except Exception as exc:
            self.set_message(f"Ошибка обновления: {exc}", error=True)
        finally:
            if not self.closed:
                self.refresh_after_id = self.root.after(1000, self.refresh)

    @staticmethod
    def format_codex_limit(data: dict[str, Any] | None) -> tuple[str, str]:
        if not isinstance(data, dict):
            return "Доступно Codex: данные временно недоступны", MUTED
        buckets = data.get("rateLimitsByLimitId")
        limit = buckets.get("codex") if isinstance(buckets, dict) else None
        if not isinstance(limit, dict):
            limit = data.get("rateLimits")
        primary = limit.get("primary") if isinstance(limit, dict) else None
        if not isinstance(primary, dict) or primary.get("usedPercent") is None:
            return "Доступно Codex: данные временно недоступны", MUTED
        try:
            used = max(0.0, min(100.0, float(primary["usedPercent"])))
        except (TypeError, ValueError):
            return "Доступно Codex: данные временно недоступны", MUTED
        remaining = max(0.0, 100.0 - used)
        duration = int(primary.get("windowDurationMins") or 0)
        window = "недельный лимит" if duration == 10080 else f"окно {duration} мин"
        reset_text = "время сброса неизвестно"
        try:
            reset = datetime.fromtimestamp(int(primary.get("resetsAt"))).astimezone()
            reset_text = "сброс " + reset.strftime("%d.%m.%Y в %H:%M")
        except (TypeError, ValueError, OSError):
            pass
        color = GREEN if remaining > 30 else YELLOW if remaining > 10 else RED
        value = f"{remaining:.0f}%" if remaining.is_integer() else f"{remaining:.1f}%"
        return f"Доступно Codex: {value}  •  {window}  •  {reset_text}", color

    def refresh_codex_limit_if_needed(self) -> None:
        now = time.monotonic()
        if self.codex_limit_loading or now - self.codex_limit_updated_at < 60:
            return
        self.codex_limit_loading = True
        self.codex_limit_updated_at = now

        def worker() -> None:
            try:
                data = read_codex_rate_limits()
            except Exception:
                data = None

            def done() -> None:
                self.codex_limit_loading = False
                self.codex_limit_data = data
                text, color = self.format_codex_limit(data)
                self.codex_limit_var.set(text)
                self.codex_limit_label.configure(fg=color)

            self._post_to_ui(done)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def friendly_import_status(value: Any) -> str:
        mapping = {"not prepared": "ещё не подготовлена", "prepared; waiting for confirmation": "проверено, ожидается подтверждение"}
        return mapping.get(str(value), str(value))

    def update_workflow_action(self, state: dict[str, Any]) -> None:
        ready = (self.job / "markers.inventory.json").is_file() and (
            self.job / "decisions.jsonl"
        ).is_file()
        button_state = "disabled" if self.busy else "normal"
        run_active = bool((state.get("codex_run") or {}).get("active"))
        if not ready and not run_active:
            self.analysis_button.pack_forget()
            self.reset_queue_button.pack_forget()
            if not self.fetch_markers_button.winfo_manager():
                self.fetch_markers_button.pack(side="left")
            self.fetch_markers_button.configure(state=button_state)
            return

        self.fetch_markers_button.pack_forget()
        if not self.analysis_button.winfo_manager():
            self.analysis_button.pack(side="left", padx=(18, 0))
        if not self.reset_queue_button.winfo_manager():
            self.reset_queue_button.pack(side="left", padx=(6, 0))
        self.reset_queue_button.configure(
            state="disabled" if self.busy or run_active else "normal",
        )
        total = int(state.get("total") or 0)
        completed = int(state.get("completed") or 0)
        control_path = self.job / "control.json"
        try:
            control = read_json(control_path) if control_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            control = {}
        one_shot_done = bool(
            isinstance(control, dict) and control.get("one_shot_completed")
        )
        if run_active:
            stopping = bool(state.get("paused") or (state.get("codex_run") or {}).get("stop_requested"))
            self.analysis_button.configure(
                text="Завершается…" if stopping else "Завершить анализ",
                style="Danger.TButton",
                state="disabled" if stopping else button_state,
            )
        elif total and completed == total:
            self.analysis_button.configure(
                text="Анализ завершён", style="Neutral.TButton", state="disabled",
            )
        elif one_shot_done and not run_active:
            self.analysis_button.configure(
                text="Выберите следующий маркер", style="Neutral.TButton", state="disabled",
            )
        else:
            self.analysis_button.configure(
                text="Начать анализ", style="Success.TButton", state=button_state,
            )

    def refresh_activity_feed(self, run: dict[str, Any]) -> None:
        path = self.job / "codex-events.jsonl"
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = (0, 0)
        phase = str(run.get("phase_detail") or "").strip()
        phase_key = hash((run.get("status"), run.get("active"), phase))
        combined_signature = (signature[0], signature[1] ^ phase_key)
        if combined_signature == self.activity_signature:
            return
        self.activity_signature = combined_signature
        entries = codex_activity_entries(self.job)
        if run.get("active") and phase:
            entries.append(f"Сейчас: {phase}")
        elif not entries:
            entries.append("Анализ ещё не запускался.")
        self.latest_activity_entries = entries
        self.refresh_live_marker_monitor(self.current_state)

    def update_codex_run_notice(self, run: dict[str, Any]) -> None:
        key = (
            run.get("status"), run.get("active"), run.get("exit_code"),
            run.get("finished_at"), run.get("reason"),
        )
        if key == self.last_codex_run_notice:
            return
        self.last_codex_run_notice = key
        status = str(run.get("status") or "not_started")
        reason = str(run.get("reason") or "").strip()
        if run.get("active"):
            pid = run.get("codex_pid") or run.get("runner_pid")
            self.set_message(
                f"Анализ выполняется автоматически в фоне (PID {pid}). "
                "Окно можно закрыть — работа продолжится."
            )
        elif status == "failed":
            self.set_message(reason or "Фоновый анализ остановился с ошибкой.", error=True)
        elif status in {"paused", "stopped", "completed", "incomplete"} and reason:
            self.set_message(reason)

    def marker_files_signature(self) -> tuple[int, ...]:
        paths = [
            self.job / "markers.inventory.json",
            self.job / "decisions.jsonl",
            self.job / "incomplete-analysis.json",
            self.job / "workers.status.json",
            self.job / "verifiers.status.json",
        ]
        raw = self.job / "raw"
        if raw.is_dir():
            paths.extend(sorted(raw.glob("*.json")))
        notes = self.job / "notes"
        if notes.is_dir():
            paths.extend(sorted(path for path in notes.glob("*.json") if DRAFT_NOTE_RE.match(path.name)))
        return tuple(path.stat().st_mtime_ns if path.exists() else 0 for path in paths)

    def reload_markers_if_changed(self) -> None:
        signature = self.marker_files_signature()
        if signature == self.marker_signature:
            return
        self.marker_signature = signature
        inventory_path = self.job / "markers.inventory.json"
        decisions_path = self.job / "decisions.jsonl"
        if not inventory_path.exists() or not decisions_path.exists():
            self.inventory_by_id = {}
            self.trace_by_id = {}
            self.decisions = []
            self.decision_by_id = {}
            self.draft_by_id = {}
            self.apply_marker_filter()
            self.render_empty_detail(
                "Задача создана. Нажмите «Получить маркеры»: программа загрузит их "
                "из Svacer и подготовит локальную очередь."
            )
            return
        inventory = read_json(inventory_path)
        markers = inventory.get("markers") if isinstance(inventory, dict) else []
        self.inventory_by_id = {str(item.get("id")): item for item in markers if isinstance(item, dict) and item.get("id")}
        self.decisions = load_decisions(decisions_path)
        self.decision_by_id = {str(item.get("marker_id")): item for item in self.decisions if item.get("marker_id")}
        self.draft_by_id = unapplied_draft_results(self.job, self.decisions)
        self.trace_by_id = {}
        raw = self.job / "raw"
        if raw.is_dir():
            for path in sorted(raw.glob("*.json")):
                try:
                    payload = read_json(path)
                except (OSError, json.JSONDecodeError):
                    continue
                for marker in payload.get("markers") or []:
                    if isinstance(marker, dict) and marker.get("id"):
                        self.trace_by_id[str(marker["id"])] = marker
        self.apply_marker_filter()
        if self.current_marker_id in self.decision_by_id:
            self.render_marker(self.current_marker_id)

    def apply_marker_filter(self) -> None:
        selected = FILTERS.get(self.filter_var.get(), "all")
        in_work = active_marker_ids(self.current_state)
        query = self.search_var.get().strip().casefold()
        previous = self.current_marker_id
        children = self.marker_table.get_children()
        if children:
            self.marker_table.delete(*children)
        self.visible_marker_ids = []
        for decision in self.decisions:
            marker_id = str(decision.get("marker_id") or "")
            marker = self.inventory_by_id.get(marker_id, {})
            draft = self.draft_by_id.get(marker_id, {})
            verdict = decision.get("verdict")
            review_status = marker_review_status(marker)
            if not marker_matches_filter(selected, marker_id, verdict, bool(draft), in_work, review_status):
                continue
            haystack = " ".join(str(value or "") for value in (
                marker_id, decision.get("warnClass"), decision.get("file"), marker.get("msg"),
                decision.get("comment"), decision.get("source"), decision.get("sink"),
                draft.get("verdict"), draft.get("comment"), draft.get("source"), draft.get("sink"),
            )).casefold()
            if query and query not in haystack:
                continue
            status = str(verdict or ("В работе" if marker_id in in_work else "Не завершён" if draft.get("analysis_status") == "needs_context" else "Черновик" if draft else "Ожидает"))
            if not verdict and not draft and marker_id not in in_work and review_status != "Undecided":
                status = f"{review_status} · Svacer"
            tag = {"Confirmed": "confirmed", "False Positive": "fp", "Won't fix": "wont", "Unclear": "unclear"}.get(
                verdict or review_status, "draft" if draft else "pending"
            )
            self.marker_table.insert("", "end", iid=marker_id, values=(status, decision.get("warnClass") or "—", short_file(decision.get("file")), decision.get("line") or "—"), tags=(tag,))
            self.visible_marker_ids.append(marker_id)
        self.marker_count_var.set(f"Показано: {len(self.visible_marker_ids)}")
        self.notebook.tab(self.markers_tab, text=f"Маркеры  {len(self.decisions)}")
        if previous in self.visible_marker_ids:
            self.marker_table.selection_set(previous)
            self.marker_table.see(previous)
        elif self.visible_marker_ids:
            first = self.visible_marker_ids[0]
            self.marker_table.selection_set(first)
            self.render_marker(first)
        else:
            self.render_empty_detail("По выбранному фильтру ничего не найдено.")

    def show_active_markers(self) -> None:
        self.filter_var.set("В работе")
        self.apply_marker_filter()
        self.notebook.select(self.markers_tab)

    def on_marker_selected(self, _event: Any = None) -> None:
        selection = self.marker_table.selection()
        if selection:
            self.render_marker(selection[0])

    def detail_insert(self, text: str, tag: str = "body") -> None:
        self.detail_text.insert("end", text, tag)

    def detail_section(self, title: str, value: Any) -> None:
        text = str(value or "").strip() or "—"
        self.detail_insert(f"\n{title}\n", "section")
        self.detail_insert(f"{text}\n", "body")

    def render_empty_detail(self, text: str = "Выберите маркер слева, чтобы посмотреть подробности.") -> None:
        self.current_marker_id = None
        self.triage_one_button.configure(text="Разметить только этот", state="disabled")
        self.approve_draft_button.pack_forget()
        self.marker_history_button.configure(state="disabled")
        self.edit_decision_button.configure(state="disabled")
        self.open_svacer_button.configure(state="disabled")
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_insert("Маркеры\n", "title")
        self.detail_insert(text, "pending")
        self.detail_text.configure(state="disabled")

    def render_marker(self, marker_id: str) -> None:
        self.current_marker_id = marker_id
        decision = self.decision_by_id.get(marker_id, {})
        draft = self.draft_by_id.get(marker_id, {})
        result = decision if decision.get("verdict") else draft or decision
        marker = self.inventory_by_id.get(marker_id, {})
        traced = self.trace_by_id.get(marker_id, marker)
        in_work = marker_id in active_marker_ids(self.current_state)
        verdict = decision.get("verdict") or (
            "Не завершён" if draft.get("analysis_status") == "needs_context" or draft.get("verdict") == "Unclear"
            else f"Черновик: {draft.get('verdict')}" if draft
            else "В работе" if in_work else "Ожидает анализа"
        )
        source_file = str(result.get("file") or marker.get("file") or "")
        self.open_svacer_button.configure(
            state="normal" if marker_svacer_url(
                str(self.job_data.get("snapshot_url") or ""), marker_id, source_file,
            ) else "disabled"
        )
        self.marker_history_button.configure(state="normal")
        self.update_selected_marker_action()
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_insert(f"{result.get('warnClass') or marker.get('warnClass') or 'Маркер'}\n", "title")
        self.detail_insert(
            f"{short_file(result.get('file') or marker.get('file'))}:{result.get('line') or marker.get('line') or '—'}"
            f"   •   {verdict}   •   ID {marker_id}\n", "meta",
        )
        self.detail_section("Описание Svacer", marker.get("msg") or traced.get("msg"))
        remote_status = marker_review_status(marker)
        if remote_status != "Undecided":
            self.detail_section("Разметка Svacer при загрузке", remote_status +
                                ". Это прежняя разметка, а не результат локальной перепроверки.")
        if marker.get("function") or marker.get("mtid"):
            self.detail_section("Контекст", f"Функция: {marker.get('function') or '—'}\nMTID: {marker.get('mtid') or '—'}")
        traces = traced.get("traces") if isinstance(traced, dict) else None
        if isinstance(traces, list) and traces:
            lines: list[str] = []
            for trace in traces:
                lines.append(f"{trace.get('role') or 'роль'}:")
                for location in trace.get("locations") or []:
                    col = location.get("col")
                    suffix = f":{col}" if col else ""
                    lines.append(f"  • {short_file(location.get('file'))}:{location.get('line') or '—'}{suffix} — {location.get('info') or ''}")
            self.detail_section("Трасса", "\n".join(lines))
        else:
            self.detail_section("Трасса", "Полная трасса будет загружена перед анализом этого маркера.")
        if (result.get("analysis_status") == "needs_context" or result.get("verdict") == "Unclear") and not decision.get("verdict"):
            self.detail_section("Не завершён — требуется продолжение исследования", list_text(result.get("proof_gaps")))
        elif result.get("verdict"):
            if draft and not decision.get("verdict"):
                self.detail_section(
                    "Статус черновика",
                    "Агент сохранил результат, но партия была остановлена до атомарного применения. "
                    "Он не считается готовым вердиктом и не будет отправлен в Svacer."
                )
            self.detail_section("Точка входа", result.get("entrypoint"))
            self.detail_section("Source", result.get("source"))
            self.detail_section("Проверки и ограничения", result.get("control"))
            self.detail_section("Sink", result.get("sink"))
            self.detail_section("Достижимость в сборке", result.get("build_reachability"))
            self.detail_section("Достижимость в продукте", result.get("product_reachability"))
            self.detail_section("Влияние", result.get("impact"))
            self.detail_section("Доказательства", list_text(result.get("evidence")))
            self.detail_section("Контраргументы", list_text(result.get("counterevidence")))
            if result.get("verdict") == "Confirmed":
                self.detail_section(
                    "Поля Svacer",
                    f"Severity: {result.get('severity') or '—'}\n"
                    f"Действие: {result.get('action') or '—'}",
                )
            self.detail_section("Комментарий для Svacer", comment_without_heading(result.get("comment")))
        else:
            self.detail_section(
                "Статус",
                "Маркер назначен агенту и сейчас анализируется."
                if in_work else "Маркер ещё не анализировался.",
            )
        self.detail_text.configure(state="disabled")
        self.detail_text.see("1.0")

    def show_current_marker_history(self) -> None:
        marker_id = self.current_marker_id
        if not marker_id:
            return
        self.refresh_history_projects()
        label = next(
            (
                name for name, path in self.history_project_options.items()
                if path is not None and path.resolve() == self.job.resolve()
            ),
            "Все проекты",
        )
        self.history_project_var.set(label)
        self.refresh_history_table(force=True)
        matching = [
            row_id for row_id, record in self.history_row_records.items()
            if str(record.get("marker_id") or "") == marker_id
        ]
        self.notebook.select(self.history_tab)
        if matching:
            self.history_table.selection_set(matching[0])
            self.history_table.see(matching[0])
            self.on_history_selected()
        else:
            self.history_detail_title_var.set("Истории этого маркера пока нет")
            self.history_detail_text.configure(state="normal")
            self.history_detail_text.delete("1.0", "end")
            self.history_detail_text.insert(
                "end", "Запись появится после завершения анализа маркера.", "meta",
            )
            self.history_detail_text.configure(state="disabled")

    def update_selected_marker_action(self) -> None:
        marker_id = self.current_marker_id
        if not marker_id:
            self.triage_one_button.configure(text="Разметить только этот", state="disabled")
            self.approve_draft_button.pack_forget()
            self.edit_decision_button.configure(state="disabled")
            return
        selected_draft = self.draft_by_id.get(marker_id, {})
        if selected_draft and selected_draft.get("analysis_status") != "needs_context" and selected_draft.get("verdict") != "Unclear":
            if not self.approve_draft_button.winfo_manager():
                self.approve_draft_button.pack(side="right", padx=(0, 6))
            self.approve_draft_button.configure(
                state="disabled" if self.busy or read_run_record(self.job).get("active") else "normal",
            )
        else:
            self.approve_draft_button.pack_forget()
        decision_ready = self.decision_by_id.get(marker_id, {}).get("verdict") in VALID_VERDICTS
        self.edit_decision_button.configure(
            state="normal" if decision_ready and not self.busy else "disabled",
        )
        queued_ids = set(self.current_state.get("priority_marker_ids") or [])
        queued_ids.update(active_marker_ids(self.current_state))
        if marker_id in queued_ids and not self.decision_by_id.get(marker_id, {}).get("verdict"):
            self.triage_one_button.configure(text="В работе", state="disabled")
        elif self.decision_by_id.get(marker_id, {}).get("verdict"):
            self.triage_one_button.configure(
                text="Перепроверить этот", state="disabled" if self.busy else "normal",
            )
        elif marker_id in self.draft_by_id:
            self.triage_one_button.configure(
                text="Повторить этот", state="disabled" if self.busy else "normal",
            )
        else:
            self.triage_one_button.configure(
                text="Разметить только этот", state="disabled" if self.busy else "normal",
            )

    def edit_current_decision(self) -> None:
        marker_id = self.current_marker_id or ""
        decision = self.decision_by_id.get(marker_id, {})
        verdict = str(decision.get("verdict") or "")
        if verdict not in VALID_VERDICTS:
            self.set_message(
                "Сначала дождитесь готового решения или подтвердите сохранённый черновик.",
                error=True,
            )
            return
        dialog = tk.Toplevel(self.root)
        style_window(dialog)
        dialog.title("Изменить поля Svacer")
        dialog.configure(bg=BG)
        try:
            dialog.attributes("-alpha", 0.82)
        except tk.TclError:
            pass
        dialog.geometry("760x560")
        dialog.minsize(620, 460)
        dialog.transient(self.root)
        dialog.grab_set()

        container = ttk.Frame(dialog, style="Surface.TFrame", padding=18)
        container.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Label(
            container,
            text=f"{decision.get('warnClass') or 'Маркер'} — {short_file(decision.get('file'))}:{decision.get('line') or '—'}",
            style="Section.TLabel",
            background=SURFACE,
        ).pack(anchor="w")
        ttk.Label(
            container,
            text=f"ID {marker_id}",
            style="Muted.TLabel",
            background=SURFACE,
        ).pack(anchor="w", pady=(4, 14))

        verdict_fields = ttk.Frame(container, style="Surface.TFrame")
        verdict_fields.pack(fill="x", pady=(0, 14))
        ttk.Label(
            verdict_fields, text="Вердикт", style="Surface.TLabel", background=SURFACE,
        ).pack(anchor="w")
        verdict_var = tk.StringVar(value=verdict)
        verdict_combo = ttk.Combobox(
            verdict_fields,
            textvariable=verdict_var,
            values=("Confirmed", "False Positive", "Won't fix", "Unclear"),
            state="readonly",
            width=22,
        )
        verdict_combo.pack(anchor="w", pady=(4, 0))

        confirmed_fields = ttk.Frame(container, style="Surface.TFrame")
        severity_var = tk.StringVar(value=str(decision.get("severity") or "Major"))
        action_var = tk.StringVar(value=str(decision.get("action") or "Fix required"))
        ttk.Label(
            confirmed_fields, text="Severity", style="Surface.TLabel", background=SURFACE,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        severity_combo = ttk.Combobox(
            confirmed_fields,
            textvariable=severity_var,
            values=("Critical", "Major", "Minor"),
            state="readonly",
            width=18,
        )
        severity_combo.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(4, 0))
        ttk.Label(
            confirmed_fields, text="Действие", style="Surface.TLabel", background=SURFACE,
        ).grid(row=0, column=1, sticky="w")
        action_combo = ttk.Combobox(
            confirmed_fields,
            textvariable=action_var,
            values=("Fix required", "Fix submitted", "Ignore"),
            state="readonly",
            width=22,
        )
        action_combo.grid(row=1, column=1, sticky="ew", pady=(4, 0))
        confirmed_fields.columnconfigure(0, weight=1)
        confirmed_fields.columnconfigure(1, weight=1)

        comment_label = ttk.Label(
            container, text="Комментарий для Svacer", style="Surface.TLabel", background=SURFACE,
        )
        comment_label.pack(anchor="w")

        def sync_confirmed_fields(_event: object | None = None) -> None:
            if verdict_var.get() == "Confirmed":
                if not confirmed_fields.winfo_manager():
                    confirmed_fields.pack(
                        fill="x", pady=(0, 14), before=comment_label,
                    )
            elif confirmed_fields.winfo_manager():
                confirmed_fields.pack_forget()

        verdict_combo.bind("<<ComboboxSelected>>", sync_confirmed_fields)
        sync_confirmed_fields()
        comment_frame = ttk.Frame(container, style="Surface.TFrame")
        comment_frame.pack(fill="both", expand=True, pady=(6, 14))
        comment_text = tk.Text(
            comment_frame,
            bg=BG,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=SELECTION,
            relief="flat",
            wrap="word",
            font=("Segoe UI", 10),
            padx=10,
            pady=10,
            undo=True,
        )
        comment_scroll = ttk.Scrollbar(comment_frame, orient="vertical", command=comment_text.yview)
        comment_text.configure(yscrollcommand=comment_scroll.set)
        comment_text.pack(side="left", fill="both", expand=True)
        comment_scroll.pack(side="right", fill="y")
        comment_text.insert("1.0", comment_without_heading(decision.get("comment")))

        hint = ttk.Label(
            container,
            text="Изменения сохранятся локально. В Svacer они попадут только после общей отправки.",
            style="Muted.TLabel",
            background=SURFACE,
        )
        hint.pack(anchor="w", pady=(0, 12))
        controls = ttk.Frame(container, style="Surface.TFrame")
        controls.pack(fill="x")
        closing = False

        def close_dialog() -> None:
            nonlocal closing
            if closing:
                return
            closing = True

            def destroy_dialog() -> None:
                if dialog.winfo_exists():
                    dialog.grab_release()
                    dialog.destroy()

            self._animate_window(dialog, closing=True, on_closed=destroy_dialog)

        def save() -> None:
            if closing:
                return
            selected_verdict = verdict_var.get()
            try:
                result = edit_saved_decision(
                    self.job / "markers.inventory.json",
                    self.job / "decisions.jsonl",
                    marker_id,
                    comment_text.get("1.0", "end-1c"),
                    verdict=selected_verdict,
                    severity=severity_var.get() if selected_verdict == "Confirmed" else None,
                    action=action_var.get() if selected_verdict == "Confirmed" else None,
                )
            except SystemExit as exc:
                messagebox.showerror("Не удалось сохранить", str(exc), parent=dialog)
                return
            close_dialog()
            self.marker_signature = None
            self.work_queue_signature = None
            self.refresh()
            details = f"Поля сохранены локально: {result.get('verdict')}"
            if selected_verdict == "Confirmed":
                details += f"; {result.get('severity')}, {result.get('action')}"
            if result.get("invalidated_import_files"):
                details += "; предыдущая локальная подготовка отправки сброшена"
            self.set_message(details + ". В Svacer ничего не отправлено.")

        ttk.Button(
            controls, text="Отмена", command=close_dialog, style="Neutral.TButton",
        ).pack(side="right")
        ttk.Button(
            controls, text="Сохранить", command=save, style="Success.TButton",
        ).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.bind("<Escape>", lambda _event: close_dialog())
        comment_text.focus_set()
        self._animate_window(dialog)

    def copy_to_clipboard(self, text: str, message: str) -> None:
        if not text:
            self.set_message("Для выбранного маркера пока нечего копировать.", error=True)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_message(message)

    def copy_comment(self) -> None:
        decision = self.decision_by_id.get(self.current_marker_id or "", {})
        result = decision if decision.get("verdict") else self.draft_by_id.get(self.current_marker_id or "", decision)
        self.copy_to_clipboard(comment_without_heading(result.get("comment")), "Комментарий скопирован без служебного заголовка.")

    def copy_marker_info(self) -> None:
        if not self.current_marker_id:
            self.set_message("Сначала выберите маркер.", error=True)
            return
        decision = self.decision_by_id.get(self.current_marker_id, {})
        result = decision if decision.get("verdict") else self.draft_by_id.get(self.current_marker_id, decision)
        marker = self.inventory_by_id.get(self.current_marker_id, {})
        url = marker_svacer_url(
            str(self.job_data.get("snapshot_url") or ""),
            self.current_marker_id,
            str(result.get("file") or marker.get("file") or ""),
        )
        text = "\n".join((
            f"{result.get('warnClass')} — {result.get('file')}:{result.get('line')}",
            f"ID: {self.current_marker_id}",
            f"Описание: {marker.get('msg') or '—'}",
            f"Вердикт: {result.get('verdict') or 'Ожидает анализа'}",
            f"Комментарий: {comment_without_heading(result.get('comment')) or '—'}",
            f"Svacer: {url or '—'}",
        ))
        self.copy_to_clipboard(text, "Карточка маркера скопирована.")

    def open_marker_in_svacer(self) -> None:
        marker_id = self.current_marker_id or ""
        decision = self.decision_by_id.get(marker_id, {})
        marker = self.inventory_by_id.get(marker_id, {})
        url = marker_svacer_url(
            str(self.job_data.get("snapshot_url") or ""),
            marker_id,
            str(decision.get("file") or marker.get("file") or ""),
        )
        if not url:
            self.set_message("Для этого маркера не удалось сформировать ссылку Svacer.", error=True)
            return
        try:
            os.startfile(url)
            self.set_message("Маркер открыт в Svacer.")
        except OSError as exc:
            self.set_message(f"Не удалось открыть маркер в Svacer: {exc}", error=True)

    def resolve_source_path(self, source: str) -> Path | None:
        raw = source.replace("\\", "/")
        candidates = [Path(source)]
        repository = self.job / "repository"
        candidates.append(repository / raw.lstrip("/"))
        for marker in ("/execroot/envoy/", "/envoy/"):
            if marker in raw:
                candidates.append(repository / raw.split(marker, 1)[1])
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def open_source(self) -> None:
        decision = self.decision_by_id.get(self.current_marker_id or "", {})
        source = str(decision.get("file") or "")
        path = self.resolve_source_path(source)
        if path is None:
            self.copy_to_clipboard(source, "Локальный файл сторонней зависимости не найден; исходный путь скопирован.")
            return
        try:
            os.startfile(path)
            self.set_message(f"Открыт {path.name}.")
        except OSError as exc:
            self.set_message(f"Не удалось открыть исходник: {exc}", error=True)

    def approve_current_draft(self) -> None:
        marker_id = self.current_marker_id or ""
        if not marker_id or marker_id not in self.draft_by_id:
            self.set_message("У выбранного маркера нет черновика.", error=True)
            return
        try:
            result = approve_saved_draft(
                self.job / "markers.inventory.json",
                self.job / "decisions.jsonl",
                marker_id,
            )
        except SystemExit as exc:
            self.set_message(str(exc), error=True)
            self.refresh()
            return
        self.marker_signature = None
        self.work_queue_signature = None
        self.refresh()
        self.set_message(
            f"Черновик подтверждён и сохранён как готовое локальное решение ({result.get('count', 0)}). "
            "В Svacer ничего не отправлено."
        )

    def queue_selected_marker(self) -> None:
        marker_id = self.current_marker_id
        if not marker_id:
            self.set_message("Сначала выберите маркер.", error=True)
            return
        if read_run_record(self.job).get("active"):
            self.set_message(
                "Сейчас выполняется другая партия. Дождитесь её сохранения или остановите задачу Codex.",
                error=True,
            )
            return

        decision = self.decision_by_id.get(marker_id, {})
        if decision.get("verdict"):
            if not messagebox.askyesno(
                "Повторный анализ",
                "Для этого маркера уже есть локальное решение. Очистить его и поставить маркер на повторный анализ?",
                parent=self.root,
            ):
                return
            completed = subprocess.run(
                [
                    console_python_executable(), str(self.app_directory / "triage_queue.py"),
                    "--inventory", str(self.job / "markers.inventory.json"),
                    "--decisions", str(self.job / "decisions.jsonl"),
                    "reopen", "--ids", marker_id,
                ],
                cwd=str(self.app_directory), capture_output=True, text=True, encoding="utf-8",
                check=False,
                **hidden_subprocess_kwargs(),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                self.set_message(f"Не удалось открыть маркер повторно: {detail}", error=True)
                return

        try:
            reset_queue_assignments(self.job / "decisions.jsonl")
        except SystemExit as exc:
            self.set_message(str(exc), error=True)
            self.refresh()
            return

        control_path = self.job / "control.json"
        control = read_json(control_path) if control_path.exists() else {}
        if not isinstance(control, dict):
            control = {}
        control.update({
            "pause_requested": False,
            "single_batch_completed": False,
            "one_shot_completed": False,
            "single_marker_requested": True,
            "priority_marker_ids": [marker_id],
            "analysis_started": True,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "triage_gui single marker",
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
        self.last_codex_run_notice = None
        self.refresh()
        pid = launched.get("codex_pid") or launched.get("runner_pid")
        self.set_message(
            f"Выбранный маркер поставлен единственным в следующую выдачу. "
            f"Codex работает автоматически в фоне (PID {pid})."
        )

    def check_connection(self, *, auto_retry: bool = False) -> None:
        def done(status: str) -> None:
            ok = status == "подключён"
            self.connected = ok
            self.last_codex_run_notice = None
            self.connection_label.configure(text="Локальный MCP подключён" if ok else "Локальный MCP не подключён", fg=GREEN if ok else YELLOW)
            self.connection_button.configure(
                text="Выйти из Svacer" if ok else "Войти в Svacer",
                style="Danger.TButton" if ok else "Accent.TButton",
            )
            if ok:
                self.connection_retry_remaining = 0
                self.set_message("Локальный MCP работает. Доступность Svacer API проверяется при чтении данных.")
            elif auto_retry and self.connection_retry_remaining > 0:
                self.connection_label.configure(text="Ожидается вход в Svacer", fg=YELLOW)
                self.set_message("Завершите вход в открывшемся окне — панель подключится автоматически.")
                self._schedule_connection_poll(3000)
            else:
                self.set_message(
                    "Svacer сейчас недоступен. Нажмите «Войти в Svacer», затем «Обновить».",
                    error=True,
                )
        self.run_background(lambda: check_mcp(self.mcp_url, self.token), done, "Проверяю подключение к Svacer…")

    def poll_connection(self) -> None:
        self.connection_after_id = None
        if self.closed or self.connected or self.connection_retry_remaining <= 0:
            return
        if self.busy:
            self._schedule_connection_poll(1000)
            return
        self.connection_retry_remaining -= 1
        self.check_connection(auto_retry=True)

    def _schedule_connection_poll(self, delay_ms: int) -> None:
        if self.connection_after_id is not None:
            self.root.after_cancel(self.connection_after_id)
        if not self.closed:
            self.connection_after_id = self.root.after(delay_ms, self.poll_connection)

    def refresh_and_check(self) -> None:
        self.connection_retry_remaining = 0
        self.refresh()
        self.check_connection()

    def reset_current_queue(self) -> None:
        run = read_run_record(self.job)
        if run.get("active"):
            self.set_message(
                "Сначала остановите текущий анализ. Сброс не прерывает работающего агента.",
                error=True,
            )
            return
        if not messagebox.askyesno(
            "Сбросить очередь",
            "Очистить текущие назначения и одноразовый режим?\n\n"
            "Готовые решения и сохранённые черновики не удалятся. После сброса можно будет "
            "запустить общую очередь или выбрать один маркер.",
            parent=self.root,
        ):
            return
        try:
            reset_queue_assignments(self.job / "decisions.jsonl")
        except SystemExit as exc:
            self.set_message(str(exc), error=True)
            self.refresh()
            return
        self.marker_signature = None
        self.work_queue_signature = None
        self.set_message(
            "Очередь сброшена. Готовые решения и черновики сохранены; ничего не отправлено в Svacer."
        )
        self.refresh()

    def analysis_action(self) -> None:
        run = read_run_record(self.job)
        if run.get("active"):
            state = collect_state(self.job)
            if not state.get("paused"):
                set_pause(self.job, True)
                self.set_message(
                    "Завершение запрошено: текущая партия сохранит результат, новая работа не начнётся."
                )
            self.refresh()
            return
        state = collect_state(self.job)
        inventory_path = self.job / "markers.inventory.json"
        decisions_path = self.job / "decisions.jsonl"
        if not inventory_path.is_file() or not decisions_path.is_file():
            self.set_message("Сначала нажмите «Получить маркеры».", error=True)
            return
        if state["total"] and state["completed"] == state["total"] and not state.get("verification", {}).get("pending"):
            self.set_message("Все маркеры уже обработаны.")
            return

        control_path = self.job / "control.json"
        try:
            control = read_json(control_path) if control_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            control = {}
        if isinstance(control, dict) and control.get("one_shot_completed"):
            self.set_message(
                "Одноразовая разметка завершена. Выберите другой маркер и нажмите «Разметить только этот». "
                "Для перехода к общей очереди сначала нажмите «Сбросить очередь»."
            )
            self.refresh()
            return

        prompt_path = self.job / "START_PROMPT.txt"
        if not prompt_path.is_file():
            self.set_message(f"Не найден автоматически сохранённый промпт: {prompt_path}", error=True)
            return
        if not prompt_path.read_text(encoding="utf-8-sig").strip():
            self.set_message("Автоматически сохранённый промпт пуст.", error=True)
            return

        set_pause(self.job, False)
        try:
            launched = launch_runner(self.job, self.app_directory)
        except Exception as exc:
            set_pause(self.job, True)
            self.set_message(f"Не удалось запустить Codex: {exc}", error=True)
            self.refresh()
            return

        self.last_codex_run_notice = None
        pid = launched.get("codex_pid") or launched.get("runner_pid")
        if run.get("active"):
            self.set_message(f"Анализ уже выполняется в фоне (PID {pid}); второй процесс не запущен.")
        else:
            self.set_message(
                f"Codex запущен автоматически в фоне (PID {pid}). "
                "Промпт сохранён в задаче; окно можно закрыть."
            )
        self.refresh()

    def confirm_stop_job(self, job: Path) -> None:
        job = job.resolve()
        run = read_run_record(job)
        if not run.get("active"):
            self.set_message("Выбранная задача уже остановлена.")
            self.refresh()
            return
        _data, target = job_identity(job)
        if not messagebox.askyesno(
            "Остановить анализ",
            f"Немедленно остановить анализ «{target}»?\n\n"
            "Уже сохранённые решения останутся. Несохранённая работа текущего маркера "
            "будет отброшена и при продолжении выполнится заново.",
            parent=self.root,
        ):
            return

        def done(_record: dict[str, Any]) -> None:
            if job == self.job.resolve():
                self.last_codex_run_notice = None
                self.activity_signature = None
            self.set_message(
                f"Задача «{target}» остановлена. Сохранённые решения не удалены."
            )
            self.refresh()

        self.run_background(
            lambda: stop_run(job),
            done,
            f"Останавливаю только задачу «{target}»…",
        )

    def open_report(self) -> None:
        try:
            report = create_user_report(self.job, self.target_name)
            os.startfile(report)
            self.set_message(
                "Открыт понятный отчёт: готовые решения, ожидающие маркеры и краткая сводка."
            )
        except OSError as exc:
            self.set_message(f"Не удалось создать или открыть отчёт: {exc}", error=True)

    def open_job_folder(self) -> None:
        try:
            os.startfile(self.job)
            self.set_message("Открыта служебная папка задачи.")
        except OSError as exc:
            self.set_message(f"Не удалось открыть служебную папку: {exc}", error=True)

    def toggle_connection(self) -> None:
        if not self.connected:
            self.connect()
            return

        def done(_result: Any) -> None:
            self.connected = False
            self.connection_retry_remaining = 0
            self.connection_label.configure(text="Svacer не подключён", fg=YELLOW)
            self.connection_button.configure(text="Войти в Svacer", style="Accent.TButton")
            self.set_message(
                "Локальное подключение Svacer завершено. Сохранённые задачи и результаты не изменены."
            )

        self.run_background(
            lambda: stop_svacer_connection(self.app_directory),
            done,
            "Завершаю локальное подключение Svacer…",
        )

    def connect(self) -> None:
        try:
            start_svacer_reconnect(self.app_directory)
            self.connected = False
            self.connection_retry_remaining = 20
            self.connection_label.configure(text="Ожидается вход в Svacer", fg=YELLOW)
            self.set_message("Выполните вход в открывшемся окне — панель подключится автоматически.")
            self._schedule_connection_poll(3000)
        except Exception as exc:
            self.set_message(f"Не удалось открыть вход: {exc}", error=True)

    def prepare_import_payload(self) -> dict[str, Any]:
        status = check_mcp(self.mcp_url, self.token)
        if status != "подключён":
            raise RuntimeError(status)
        reply = asyncio.run(call_mcp_tool(
            self.mcp_url,
            self.token,
            "prepare_markup_import",
            {"job_directory": str(self.job)},
        ))
        return json.loads(reply)

    def apply_prepared_import(self, confirmation: str, force: bool) -> dict[str, Any]:
        reply = asyncio.run(call_mcp_tool(
            self.mcp_url,
            self.token,
            "apply_markup_import",
            {
                "job_directory": str(self.job),
                "confirmation": confirmation,
                "overwrite": "force" if force else "none",
            },
        ))
        return json.loads(reply)

    def confirm_and_send_prepared(self, _payload: dict[str, Any]) -> None:
        preview = read_json(self.job / "svacer-import-preview.json")
        force = bool(preview.get("requires_force"))
        expected = str(preview.get("force_confirmation" if force else "confirmation") or "")
        conflict_note = (
            "\nВнимание: существующая разметка отличается и будет заменена."
            if force else ""
        )
        summary = (
            f"Автоматическая проверка завершена.\n\n"
            f"Маркеров: {preview.get('marker_count')}\n"
            f"Конфликтов: {preview.get('conflict_count')}"
            f"{conflict_note}\n\n{preview.get('scope', '')}\n\n"
            "Следующий шаг изменит разметку Svacer. Продолжить?"
        )
        if not messagebox.askyesno("Отправка в Svacer", summary, parent=self.root):
            self.set_message("Проверка завершена, отправка отменена. Ничего в Svacer не изменено.")
            return
        typed = simpledialog.askstring(
            "Точное подтверждение", f"Введите дословно:\n\n{expected}", parent=self.root,
        )
        if typed != expected:
            self.set_message("Фраза не совпала. Ничего не отправлено.", error=True)
            return

        def work() -> dict[str, Any]:
            return self.apply_prepared_import(typed, force)

        def done(payload: dict[str, Any]) -> None:
            verified = bool((payload.get("verification") or {}).get("verified"))
            self.set_message(
                "Разметка отправлена и подтверждена обратной проверкой."
                if verified else
                "Итог отправки не подтверждён. Проверьте журнал Svacer и папку результатов; повтор заблокирован.",
                error=not verified,
            )
            self.refresh()
        self.run_background(work, done, "Отправляю разметку и выполняю обратную проверку…")

    def send_import(self) -> None:
        state = collect_state(self.job)
        if not state["total"] or state["completed"] != state["total"]:
            self.set_message(
                f"Отправка недоступна: готово {state['completed']} из {state['total']}. Ничего не отправлено.",
                error=True,
            )
            return
        if (self.job / "svacer-import-attempt.json").exists():
            self.set_message("Попытка отправки уже записана; повтор заблокирован.", error=True)
            return
        self.run_background(
            self.prepare_import_payload,
            self.confirm_and_send_prepared,
            "Автоматически проверяю решения, область маркеров и текущую разметку Svacer…",
        )

    def close(self) -> None:
        self.closed = True
        for frame in list(self.panel_animations):
            self._finish_panel_animation(frame)
        for window in list(self.window_animations):
            self._cancel_window_animation(window)
        if self.connection_after_id is not None:
            self.root.after_cancel(self.connection_after_id)
            self.connection_after_id = None
        if self.refresh_after_id is not None:
            self.root.after_cancel(self.refresh_after_id)
            self.refresh_after_id = None
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Графическая панель Svacer triage")
    parser.add_argument("--job")
    args = parser.parse_args()
    app_directory = Path(__file__).resolve().parent
    job = resolve_job(app_directory.parent, args.job)
    root = tk.Tk()
    TriageGui(root, job, app_directory)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
