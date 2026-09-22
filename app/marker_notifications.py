"""Durable, local-only notifications for finished marker attempts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from triage_dashboard import atomic_json, read_json


NOTIFICATIONS_FILE = "ui-notifications.json"
HISTORY_FILE = "marker-history.jsonl"
RESULT_VERDICTS = frozenset({"Confirmed", "False Positive", "Won't fix", "Unclear"})
_HISTORY_FIELDS = frozenset({
    "attempt_id", "launch_id", "runner_batch", "marker_id", "status", "verdict",
    "warnClass", "file", "line", "finished_at", "failure_reason",
})
_HISTORY_CACHE: dict[Path, tuple[tuple[int, int, int], list[dict[str, Any]]]] = {}


def _short(value: Any, limit: int = 55) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _location(record: dict[str, Any]) -> str:
    name = str(record.get("file") or "").replace("\\", "/").rsplit("/", 1)[-1]
    line = record.get("line")
    return f"{_short(name, 42)}:{line}" if name and line else _short(name, 42)


def _history_records(job: Path) -> list[dict[str, Any]]:
    path = job / HISTORY_FILE
    if not path.is_file():
        _HISTORY_CACHE.pop(path, None)
        return []
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    cached = _HISTORY_CACHE.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            # History also contains full agent messages. The notification poll only
            # needs compact event metadata, and runs every two seconds in the UI.
            records.append({key: value[key] for key in _HISTORY_FIELDS if key in value})
    _HISTORY_CACHE[path] = (signature, records)
    return records


def _history_notification(record: dict[str, Any]) -> dict[str, str] | None:
    marker_id = str(record.get("marker_id") or "")
    status = str(record.get("status") or "")
    verdict = str(record.get("verdict") or "")
    if not marker_id or not (status in {"failed", "incomplete"} or
                             status == "completed" and verdict in RESULT_VERDICTS):
        return None
    attempt_id = str(record.get("attempt_id") or "")
    if not attempt_id:
        attempt_id = ":".join((str(record.get("launch_id") or "legacy"),
                               str(record.get("runner_batch") or 0), marker_id))
    if status == "failed":
        title, tone, detail = "Ошибка маркера", "red", "Анализ завершился с ошибкой"
    elif status == "incomplete" or verdict == "Unclear":
        title, tone, detail = "Маркер требует внимания", "yellow", "Результат не завершён"
    else:
        title, tone, detail = "Маркер просканирован", "green", f"Результат: {verdict}"
    if status in {"failed", "incomplete"} and record.get("failure_reason"):
        detail = _short(record["failure_reason"], 220)
    subject = " · ".join(part for part in (
        _short(record.get("warnClass"), 42), _location(record)
    ) if part)
    return {
        "id": f"attempt:{attempt_id}", "marker_id": marker_id,
        "title": title, "tone": tone, "subject": subject,
        "detail": detail, "created_at": str(record.get("finished_at") or ""),
    }


def _run_notification(job: Path, history: list[dict[str, Any]]) -> dict[str, str] | None:
    run_path = job / "codex-run.json"
    if not run_path.is_file():
        return None
    try:
        run = read_json(run_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(run, dict) or run.get("status") not in {"failed", "incomplete"}:
        return None
    launch_id = str(run.get("launch_id") or "")
    if not launch_id:
        return None
    marker_id = ""
    context_path = job / "batch-context.json"
    if context_path.is_file():
        try:
            context = read_json(context_path)
        except (OSError, json.JSONDecodeError):
            context = {}
        batch = context.get("batch") if isinstance(context, dict) else None
        ids = batch.get("marker_ids") if isinstance(batch, dict) else None
        if (isinstance(context, dict) and context.get("launch_id") == launch_id
                and isinstance(ids, list) and len(ids) == 1):
            marker_id = str(ids[0])
    same_launch = [record for record in history if str(record.get("launch_id") or "") == launch_id]
    if any(record.get("status") in {"failed", "incomplete"} for record in same_launch):
        return None  # Per-marker history already explains this failed run.
    if any(record.get("status") == "completed" and record.get("marker_id") == marker_id
           for record in same_launch):
        marker_id = ""  # A later job failure must not be blamed on a completed marker.
    tone = "red" if run.get("status") == "failed" else "yellow"
    title = ("Ошибка маркера" if tone == "red" else "Маркер требует внимания") if marker_id else (
        "Ошибка анализа" if tone == "red" else "Анализ не завершён")
    return {
        "id": f"run:{launch_id}", "marker_id": marker_id,
        "title": title, "tone": tone,
        "subject": _short(marker_id, 42) if marker_id else _short(job.name, 55),
        "detail": "Откройте маркер для подробностей" if marker_id else "Откройте обзор для подробностей",
        "created_at": str(run.get("finished_at") or ""),
    }


def _candidate_notifications(job: Path) -> list[dict[str, str]]:
    history = _history_records(job)
    candidates = [note for record in history if (note := _history_notification(record))]
    run_note = _run_notification(job, history)
    if run_note:
        candidates.append(run_note)
    return candidates


def _load_state(job: Path) -> dict[str, Any] | None:
    path = job / NOTIFICATIONS_FILE
    if not path.exists():
        return None
    value = read_json(path)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("seen"), list)
            or not all(isinstance(item, str) for item in value["seen"])
            or not isinstance(value.get("pending"), list)
            or not all(isinstance(item, dict) and isinstance(item.get("id"), str)
                       for item in value["pending"])):
        raise ValueError("Файл уведомлений повреждён; непрочитанные события сохранены без изменений")
    return value


def sync_notifications(job: Path) -> list[dict[str, str]]:
    """Show only new attempts; keep pending cards until an explicit click."""
    candidates = _candidate_notifications(job)
    state = _load_state(job)
    if state is None:
        # Do not flood a newly installed UI with its entire historical log.
        atomic_json(job / NOTIFICATIONS_FILE, {
            "schema_version": 1,
            "seen": list(dict.fromkeys(note["id"] for note in candidates)),
            "pending": [],
        })
        return []
    seen = set(str(value) for value in state["seen"])
    new = []
    for note in candidates:
        if note["id"] not in seen:
            seen.add(note["id"])
            new.append(note)
    if new:
        state["seen"].extend(note["id"] for note in new)
        state["pending"].extend(new)
        atomic_json(job / NOTIFICATIONS_FILE, state)
    return [note for note in state["pending"] if isinstance(note, dict)]


def dismiss_notification(job: Path, notification_id: str) -> None:
    dismiss_notifications(job, [notification_id])


def dismiss_notifications(job: Path, notification_ids: Iterable[str]) -> None:
    """Dismiss only the displayed IDs; newly arrived events stay unread."""
    ids = set(notification_ids)
    if not ids:
        return
    state = _load_state(job)
    if state is None:
        return
    pending = [note for note in state["pending"]
               if not isinstance(note, dict) or note.get("id") not in ids]
    if len(pending) != len(state["pending"]):
        state["pending"] = pending
        atomic_json(job / NOTIFICATIONS_FILE, state)
