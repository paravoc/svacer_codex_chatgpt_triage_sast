#!/usr/bin/env python3
"""Lightweight Windows console dashboard for one Svacer triage job."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from triage_queue import markers_for_triage, marker_review_status, atomic_write_json, decision_lock, job_run_mode


VALID_VERDICTS = {"Confirmed", "False Positive", "Won't fix", "Unclear"}
NOTE_RE = re.compile(r"^batch-(\d+)-worker-(\d+)\.json$", re.IGNORECASE)
VERIFIER_NOTE_RE = re.compile(r"^verify-batch-(\d+)-verifier-(\d+)\.json$", re.IGNORECASE)
ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}
_LAST_FRAME: str | None = None
_LAST_FRAME_LINES = 0
_VT_ENABLED = False


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def atomic_json(path: Path, value: dict) -> None:
    atomic_write_json(path, value)


def note_rows(path: Path) -> list[dict]:
    value = read_json(path)
    if isinstance(value, dict):
        value = value.get("decisions")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def saved_context_metrics(job: Path) -> dict:
    """Approximate only persisted JSON/text context, never claim account usage."""
    files: list[Path] = []
    for name in ("markers.inventory.json", "decisions.jsonl", "job.json", "progress.md"):
        path = job / name
        if path.is_file():
            files.append(path)
    for directory_name in ("raw", "notes"):
        directory = job / directory_name
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    byte_count = 0
    for path in files:
        try:
            byte_count += path.stat().st_size
        except OSError:
            continue
    notes = job / "notes"
    primary_calls = 0
    verifier_calls = 0
    if notes.is_dir():
        for path in notes.iterdir():
            if NOTE_RE.match(path.name):
                primary_calls += 1
            elif VERIFIER_NOTE_RE.match(path.name):
                verifier_calls += 1
    return {
        "estimated_tokens": math.ceil(byte_count / 4),
        "bytes": byte_count,
        "files": len(files),
        "primary_calls": primary_calls,
        "verifier_calls": verifier_calls,
    }


def collect_state(job: Path) -> dict:
    state: dict[str, Any] = {
        "job": job.name,
        "job_path": str(job),
        "inventory_total": 0,
        "already_reviewed": 0,
        "total": 0,
        "completed": 0,
        "pending": 0,
        "by_verdict": Counter(),
        "workers": {},
        "verifiers": {},
        "verifier_queue": None,
        "verification": Counter(),
        "context": saved_context_metrics(job),
        "token_warning": 0,
        "queue": None,
        "paused": False,
        "single_batch_completed": False,
        "one_shot_completed": False,
        "analysis_started": False,
        "import": "not prepared",
        "preview": None,
        "priority_marker_ids": [],
        "recheck_marker_ids": [],
        "manual_queue_requested": False,
        "single_marker_requested": False,
        "run_mode": "until_complete",
        "batch_size": 1,
        "parallel_workers": 1,
        "run_remaining": None,
        "errors": [],
    }
    inventory_path = job / "markers.inventory.json"
    decisions_path = job / "decisions.jsonl"
    target_ids: set[str] = set()
    try:
        if inventory_path.exists():
            inventory = read_json(inventory_path)
            markers = inventory.get("markers") if isinstance(inventory, dict) else None
            if isinstance(markers, list):
                target_markers = markers_for_triage(markers)
                target_ids = {str(marker.get("id") or "") for marker in target_markers}
                state["inventory_total"] = len(markers)
                state["already_reviewed"] = sum(marker_review_status(m) != "Undecided" for m in markers)
                state["total"] = len(target_markers)
            else:
                target_ids = set()
        if decisions_path.exists():
            decisions = read_jsonl(decisions_path)
            state["by_verdict"] = Counter(
                row.get("verdict") for row in decisions
                if str(row.get("marker_id") or "") in target_ids
                and row.get("verdict") in VALID_VERDICTS
            )
            state["completed"] = sum(state["by_verdict"].values())
            confirmed = [
                row for row in decisions
                if str(row.get("marker_id") or "") in target_ids
                and row.get("verdict") == "Confirmed"
            ]
            for row in confirmed:
                verification = row.get("verification")
                status = verification.get("status") if isinstance(verification, dict) else "pending"
                state["verification"][status if status in {"pending", "verified", "challenged"} else "pending"] += 1
        state["pending"] = max(0, state["total"] - state["completed"])
    except (OSError, json.JSONDecodeError, SystemExit) as exc:
        state["errors"].append(f"inventory/decisions: {exc}")

    worker_saved: dict[int, set[str]] = defaultdict(set)
    worker_saved_by_batch: dict[tuple[int, int], set[str]] = defaultdict(set)
    worker_batches: dict[int, set[int]] = defaultdict(set)
    worker_updated: dict[int, float] = defaultdict(float)
    notes = job / "notes"
    if notes.is_dir():
        for path in notes.iterdir():
            match = NOTE_RE.match(path.name)
            if not match:
                continue
            batch, worker = int(match.group(1)), int(match.group(2))
            try:
                for row in note_rows(path):
                    marker_id = str(row.get("marker_id") or "")
                    if marker_id:
                        worker_saved[worker].add(marker_id)
                        worker_saved_by_batch[(batch, worker)].add(marker_id)
                worker_batches[worker].add(batch)
                worker_updated[worker] = max(worker_updated[worker], path.stat().st_mtime)
            except (OSError, json.JSONDecodeError) as exc:
                state["errors"].append(f"{path.name}: {exc}")

    current_by_worker: dict[int, dict] = {}
    status_path = job / "workers.status.json"
    if status_path.exists():
        try:
            queue = read_json(status_path)
            if isinstance(queue, dict):
                state["queue"] = queue
                state["analysis_started"] = True
                for worker in queue.get("workers") or []:
                    if isinstance(worker, dict) and type(worker.get("worker")) is int:
                        current_by_worker[worker["worker"]] = worker
        except (OSError, json.JSONDecodeError) as exc:
            state["errors"].append(f"workers.status.json: {exc}")

    current_batch = state["queue"].get("batch") if isinstance(state.get("queue"), dict) else None
    for worker in sorted(set(worker_saved) | set(current_by_worker)):
        current = current_by_worker.get(worker, {})
        current_saved = (
            worker_saved_by_batch[(current_batch, worker)]
            if type(current_batch) is int else set()
        )
        state["workers"][worker] = {
            "saved": len(worker_saved[worker]),
            "batches": len(worker_batches[worker]),
            "updated": worker_updated[worker],
            "current_status": current.get("status", "idle"),
            "assigned": int(current.get("assigned") or 0),
            "current_saved": int(current.get("saved") or 0),
            "marker_ids": [str(item) for item in (current.get("marker_ids") or [])],
            "saved_marker_ids": sorted(worker_saved[worker]),
            "current_saved_marker_ids": sorted(current_saved),
        }

    verifier_saved: dict[int, set[str]] = defaultdict(set)
    verifier_saved_by_batch: dict[tuple[int, int], set[str]] = defaultdict(set)
    verifier_batches: dict[int, set[int]] = defaultdict(set)
    verifier_updated: dict[int, float] = defaultdict(float)
    if notes.is_dir():
        for path in notes.iterdir():
            match = VERIFIER_NOTE_RE.match(path.name)
            if not match:
                continue
            batch, verifier = int(match.group(1)), int(match.group(2))
            try:
                for row in note_rows(path):
                    marker_id = str(row.get("marker_id") or "")
                    if marker_id:
                        verifier_saved[verifier].add(marker_id)
                        verifier_saved_by_batch[(batch, verifier)].add(marker_id)
                verifier_batches[verifier].add(batch)
                verifier_updated[verifier] = max(verifier_updated[verifier], path.stat().st_mtime)
            except (OSError, json.JSONDecodeError) as exc:
                state["errors"].append(f"{path.name}: {exc}")

    current_verifiers: dict[int, dict] = {}
    verifier_status_path = job / "verifiers.status.json"
    if verifier_status_path.exists():
        try:
            verifier_queue = read_json(verifier_status_path)
            if isinstance(verifier_queue, dict):
                state["verifier_queue"] = verifier_queue
                for verifier in verifier_queue.get("verifiers") or []:
                    if isinstance(verifier, dict) and type(verifier.get("verifier")) is int:
                        current_verifiers[verifier["verifier"]] = verifier
        except (OSError, json.JSONDecodeError) as exc:
            state["errors"].append(f"verifiers.status.json: {exc}")
    current_verifier_batch = (
        state["verifier_queue"].get("batch")
        if isinstance(state.get("verifier_queue"), dict) else None
    )
    for verifier in sorted(set(verifier_saved) | set(current_verifiers)):
        current = current_verifiers.get(verifier, {})
        current_saved = (
            verifier_saved_by_batch[(current_verifier_batch, verifier)]
            if type(current_verifier_batch) is int else set()
        )
        state["verifiers"][verifier] = {
            "saved": len(verifier_saved[verifier]),
            "batches": len(verifier_batches[verifier]),
            "updated": verifier_updated[verifier],
            "current_status": current.get("status", "idle"),
            "assigned": int(current.get("assigned") or 0),
            "current_saved": int(current.get("saved") or 0),
            "marker_ids": [str(item) for item in (current.get("marker_ids") or [])],
            "saved_marker_ids": sorted(verifier_saved[verifier]),
            "current_saved_marker_ids": sorted(current_saved),
        }

    job_path = job / "job.json"
    if job_path.exists():
        try:
            job_data = read_json(job_path)
            state["token_warning"] = int(job_data.get("saved_context_token_warning") or 0)
            state["run_mode"] = str(job_data.get("run_mode") or "until_complete")
            state["batch_size"] = max(1, int(job_data.get("batch_size") or 1))
            state["parallel_workers"] = max(1, int(job_data.get("parallel_workers") or 1))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            state["errors"].append(f"job.json: {exc}")

    control_path = job / "control.json"
    if control_path.exists():
        try:
            control = read_json(control_path)
            state["paused"] = (
                bool(control.get("pause_requested") or control.get("one_shot_completed") or
                     (job_run_mode(decisions_path) == "single_batch" and control.get("single_batch_completed")))
                if isinstance(control, dict) else False
            )
            if isinstance(control, dict):
                state["manual_queue_requested"] = control.get("manual_queue_requested") is True
                state["single_marker_requested"] = control.get("single_marker_requested") is True
                state["single_batch_completed"] = bool(control.get("single_batch_completed"))
                state["one_shot_completed"] = bool(control.get("one_shot_completed"))
                state["analysis_started"] = bool(
                    state["analysis_started"] or control.get("analysis_started")
                )
                state["priority_marker_ids"] = [
                    str(item) for item in (control.get("priority_marker_ids") or [])
                    if isinstance(item, str) and item
                ]
                state["recheck_marker_ids"] = [
                    str(item) for item in (control.get("recheck_marker_ids") or [])
                    if isinstance(item, str) and item
                ]
                if control.get("run_remaining") is not None:
                    state["run_remaining"] = max(0, int(control["run_remaining"]))
        except (OSError, json.JSONDecodeError, SystemExit, TypeError, ValueError) as exc:
            state["errors"].append(f"control.json: {exc}")

    state["analysis_started"] = bool(
        state["analysis_started"]
        or state["completed"]
        or state["context"].get("primary_calls")
        or state["context"].get("verifier_calls")
    )

    attempt_path = job / "svacer-import-attempt.json"
    preview_path = job / "svacer-import-preview.json"
    if attempt_path.exists():
        try:
            attempt = read_json(attempt_path)
            state["import"] = str(attempt.get("status") or "attempt recorded")
        except (OSError, json.JSONDecodeError):
            state["import"] = "attempt file is invalid"
    elif preview_path.exists():
        try:
            state["preview"] = read_json(preview_path)
            state["import"] = "prepared; waiting for confirmation"
        except (OSError, json.JSONDecodeError):
            state["import"] = "preview is invalid"
    return state


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S") if timestamp else "-"


def progress_bar(completed: int, total: int, width: int = 34) -> str:
    ratio = completed / total if total else 0.0
    filled = min(width, max(0, round(width * ratio)))
    return "█" * filled + "░" * (width - filled)


def paint(text: str, color: str, enabled: bool) -> str:
    return f"{ANSI[color]}{text}{ANSI['reset']}" if enabled else text


def render(
    state: dict,
    message: str = "",
    mcp_status: str = "не проверялся",
    *,
    use_color: bool = False,
) -> str:
    total, completed = state["total"], state["completed"]
    percent = (100 * completed / total) if total else 0.0
    confirmed = state["by_verdict"].get("Confirmed", 0)
    false_positive = state["by_verdict"].get("False Positive", 0)
    wont_fix = state["by_verdict"].get("Won't fix", 0)
    unclear = state["by_verdict"].get("Unclear", 0)
    pending = state["pending"]
    verification_required = sum(state.get("verification", {}).values())
    verification_verified = state.get("verification", {}).get("verified", 0)
    verification_pending = state.get("verification", {}).get("pending", 0)
    verification_challenged = state.get("verification", {}).get("challenged", 0)
    context = state.get("context") or {}
    estimated_tokens = int(context.get("estimated_tokens") or 0)
    token_warning = int(state.get("token_warning") or 0)
    token_color = "yellow" if token_warning and estimated_tokens >= token_warning else "gray"
    bar_color = "green" if total and completed == total else "blue"
    queue_text = "ПАУЗА после текущей партии" if state["paused"] else "работа разрешена"
    queue_color = "yellow" if state["paused"] else "green"
    import_color = (
        "green" if state["import"] == "completed_verified"
        else "red" if "unknown" in state["import"] or "unverified" in state["import"] or "invalid" in state["import"]
        else "cyan" if state["import"] != "not prepared"
        else "gray"
    )
    lines = [
        paint("SVACER TRIAGE — локальная панель", "cyan", use_color),
        paint("=" * 72, "gray", use_color),
        f"Задача: {state['job']}",
        f"Результаты: {state['job_path']}",
        f"MCP: {paint(mcp_status, 'green' if mcp_status == 'подключён' else 'yellow', use_color)}",
        (
            f"Область: {state.get('inventory_total', total)} маркеров | "
            f"уже размечено {state.get('already_reviewed', 0)} | для доразметки {total}"
        ),
        (
            "Прогресс: "
            f"[{paint(progress_bar(completed, total), bar_color, use_color)}] "
            f"{paint(f'{completed}/{total} ({percent:.1f}%)', bar_color, use_color)}"
        ),
        (
            "Вердикты: "
            f"{paint(f'Confirmed {confirmed}', 'red', use_color)} | "
            f"{paint(f'FP {false_positive}', 'green', use_color)} | "
            f"{paint(f'Wont fix {wont_fix}', 'yellow', use_color)} | "
            f"{paint(f'Unclear {unclear}', 'magenta', use_color)} | "
            f"{paint(f'Pending {pending}', 'gray', use_color)}"
        ),
        (
            "Проверка Confirmed: "
            f"{paint(f'подтверждено {verification_verified}/{verification_required}', 'green', use_color)} | "
            f"{paint(f'ожидает {verification_pending}', 'yellow', use_color)} | "
            f"{paint(f'оспорено {verification_challenged}', 'red', use_color)}"
        ),
        (
            "Токены: "
            f"{paint(f'≈{estimated_tokens:,}'.replace(',', ' '), token_color, use_color)} "
            "по сохранённым JSON/трассам; исходники и внутренние рассуждения не входят"
        ),
        (
            "Вызовы агентов: "
            f"основной анализ {context.get('primary_calls', 0)} | "
            f"независимая проверка {context.get('verifier_calls', 0)}"
        ),
        f"Очередь: {paint(queue_text, queue_color, use_color)}",
    ]
    queue = state.get("queue") or {}
    if state["workers"] or queue:
        lines.extend([
            "",
            paint("Работники:", "bold", use_color),
            "  №    сохранено   партий   текущая партия            последний файл",
        ])
        for number, worker in state["workers"].items():
            current = f"{worker['current_status']}: {worker['current_saved']}/{worker['assigned']}"
            lines.append(
                f"  {number:<4} {worker['saved']:<11} {worker['batches']:<7} "
                f"{current:<24} {format_time(worker['updated'])}"
            )
    if queue:
        lines.append(f"Текущая партия: {queue.get('batch') or '-'}; состояние: {queue.get('state') or '-'}")
    verifier_queue = state.get("verifier_queue") or {}
    if state["verifiers"] or verifier_queue:
        lines.extend(["", paint("Независимая проверка Confirmed:", "bold", use_color)])
        for number, verifier in state["verifiers"].items():
            current = f"{verifier['current_status']}: {verifier['current_saved']}/{verifier['assigned']}"
            lines.append(
                f"  verifier-{number}: сохранено {verifier['saved']}; партий {verifier['batches']}; "
                f"{current}; {format_time(verifier['updated'])}"
            )
        if verifier_queue:
            lines.append(
                f"Партия проверки: {verifier_queue.get('batch') or '-'}; "
                f"состояние: {verifier_queue.get('state') or '-'}"
            )
    lines.extend(["", f"Импорт: {paint(state['import'], import_color, use_color)}"])
    preview = state.get("preview")
    if isinstance(preview, dict):
        lines.append(
            f"  маркеров {preview.get('marker_count', 0)}, конфликтов {preview.get('conflict_count', 0)}, "
            f"режим {'force' if preview.get('requires_force') else 'none'}"
        )
        phrase = preview.get("force_confirmation") if preview.get("requires_force") else preview.get("confirmation")
        lines.append(f"  подтверждение: {phrase}")
    if state["errors"]:
        lines.extend(
            ["", paint("Ошибки чтения:", "red", use_color)]
            + [paint(f"  - {error}", "red", use_color) for error in state["errors"][:5]]
        )
    if message:
        message_color = "red" if any(word in message.lower() for word in ("ошиб", "не выполн", "не подтверж")) else "cyan"
        lines.extend(["", f"Сообщение: {paint(message, message_color, use_color)}"])
    lines.extend([
        "",
        (
            f"{paint('[P]', 'yellow', use_color)} приостановить после партии   "
            f"{paint('[C]', 'green', use_color)} разрешить продолжение"
        ),
        (
            f"{paint('[I]', 'cyan', use_color)} проверить и подготовить      "
            f"{paint('[S]', 'red', use_color)} отправить результаты"
        ),
        (
            f"{paint('[O]', 'blue', use_color)} открыть папку                "
            f"{paint('[M]', 'magenta', use_color)} подключить Svacer"
        ),
        (
            f"{paint('[R]', 'blue', use_color)} обновить                     "
            f"{paint('[Q]', 'gray', use_color)} закрыть панель"
        ),
        "",
        "Пауза не обрывает уже работающего агента. После C при необходимости напишите",
        "в задаче Codex «продолжи»: панель не может сама разбудить остановленную задачу.",
    ])
    return "\n".join(lines)


def set_pause(job: Path, paused: bool) -> None:
    with decision_lock(job / "decisions.jsonl"):
        _set_pause_locked(job, paused)


def _set_pause_locked(job: Path, paused: bool) -> None:
    path = job / "control.json"
    control: dict[str, Any] = {}
    if path.exists():
        loaded = read_json(path)
        if isinstance(loaded, dict):
            control.update(loaded)
    control.update({
        "pause_requested": paused,
        "single_batch_completed": False,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    if not paused:
        control["analysis_started"] = True
        control["one_shot_completed"] = False
        # A failed/incomplete batch has a reserved remainder. Keep it when
        # resuming after a transient Svacer/model failure; otherwise starting
        # again would silently turn 4 remaining markers into a fresh batch of 10.
        try:
            run = read_json(job / "codex-run.json")
        except (OSError, json.JSONDecodeError):
            run = {}
        resume_failed_batch = (
            isinstance(run, dict) and run.get("status") in {"failed", "incomplete"}
            and control.get("run_remaining") is not None
        )
        if not resume_failed_batch:
            control.pop("run_remaining", None)
    atomic_json(path, control)


def mcp_text(result: Any) -> str:
    texts = [block.text for block in getattr(result, "content", []) if hasattr(block, "text")]
    text = "\n".join(texts).strip()
    if getattr(result, "isError", False):
        raise RuntimeError(text or "MCP tool returned an error")
    if not text:
        raise RuntimeError("MCP tool returned no text result")
    return text


async def call_mcp_tool(mcp_url: str, token: str, tool: str, arguments: dict) -> str:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        async with streamable_http_client(mcp_url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return mcp_text(await session.call_tool(tool, arguments=arguments))


async def list_mcp_tools(mcp_url: str, token: str) -> set[str]:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        async with streamable_http_client(mcp_url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.list_tools()
                return {tool.name for tool in response.tools}


def describe_exception(exc: BaseException) -> str:
    """Return useful leaf errors instead of AnyIO's generic TaskGroup wrapper."""
    messages: list[str] = []
    seen: set[int] = set()

    def visit(value: BaseException | None) -> None:
        if value is None or id(value) in seen:
            return
        seen.add(id(value))
        nested = getattr(value, "exceptions", None)
        if isinstance(nested, (list, tuple)) and nested:
            for item in nested:
                if isinstance(item, BaseException):
                    visit(item)
            return
        text = str(value).strip()
        if text and "unhandled errors in a TaskGroup" not in text:
            messages.append(f"{type(value).__name__}: {text}")
        cause = value.__cause__ or value.__context__
        if isinstance(cause, BaseException):
            visit(cause)

    visit(exc)
    unique = list(dict.fromkeys(messages))
    return " | ".join(unique[:4]) or f"{type(exc).__name__}: {exc}"


def friendly_mcp_error(exc: BaseException) -> str:
    detail = describe_exception(exc)
    lowered = detail.lower()
    if "method not found" in lowered or "unknown tool" in lowered or "prepare_markup_import" in lowered:
        return "Подключение требует обновления. Нажмите «Войти в Svacer» для повторного входа."
    if any(text in lowered for text in ("connecterror", "connection refused", "all connection attempts failed")):
        return "Нет активного подключения. Нажмите «Войти в Svacer» — подключение запустится автоматически."
    if "401" in lowered or "unauthorized" in lowered:
        return "Локальная авторизация устарела. Нажмите «Войти в Svacer» для повторного входа."
    return detail


def check_mcp(mcp_url: str, token: str) -> str:
    if not token:
        return "не настроен — запустите START.cmd"
    try:
        names = asyncio.run(list_mcp_tools(mcp_url, token))
    except Exception as exc:
        return friendly_mcp_error(exc)
    required = {"prepare_markup_import", "apply_markup_import"}
    if not required.issubset(names):
        return "Подключение требует обновления. Нажмите «Войти в Svacer»."
    return "подключён"


def start_svacer_reconnect(app_directory: Path) -> None:
    script = app_directory / "restart_svacer_http.ps1"
    if not script.exists():
        raise FileNotFoundError(f"Не найден {script.name}")
    subprocess.Popen(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
        ],
        cwd=str(app_directory),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def stop_svacer_connection(app_directory: Path, port: int = 8002) -> None:
    """Stop only the local Svacer MCP process; do not touch saved credentials."""
    script = app_directory / "stop_components.ps1"
    if not script.exists():
        raise FileNotFoundError(f"Не найден {script.name}")
    completed = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Mode", "Svacer", "-NoElevation", "-Port", str(port),
        ],
        cwd=str(app_directory),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("Не удалось остановить локальное подключение Svacer")


def resolve_job(root: Path, value: str | None) -> Path:
    result_roots = [path.resolve() for path in (root / "RESULTS", root / "jobs") if path.is_dir()]
    if not result_roots:
        raise SystemExit("Папка RESULTS ещё не создана")
    if value:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=True)
    else:
        candidates = [path for directory in result_roots for path in directory.iterdir()
                      if path.is_dir() and (path / "job.json").is_file()]
        if not candidates:
            raise SystemExit("В каталоге RESULTS ещё нет задач")
        candidate = max(candidates, key=lambda path: path.stat().st_mtime).resolve()
    if candidate.parent not in result_roots:
        raise SystemExit("Разрешена только задача непосредственно из каталога RESULTS")
    return candidate


def enable_virtual_terminal() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if handle in (0, -1) or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except (AttributeError, OSError):
        return False


def reset_screen() -> None:
    global _LAST_FRAME, _LAST_FRAME_LINES
    if _VT_ENABLED:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    elif sys.stdout.isatty() and os.name == "nt":
        os.system("cls")
    _LAST_FRAME = None
    _LAST_FRAME_LINES = 0


def display_frame(frame: str, *, force: bool = False) -> None:
    """Update one dashboard in place and skip unchanged refreshes."""
    global _LAST_FRAME, _LAST_FRAME_LINES
    if not force and frame == _LAST_FRAME:
        return
    if not sys.stdout.isatty():
        print(frame, flush=True)
    elif _VT_ENABLED:
        lines = frame.splitlines()
        sys.stdout.write("\033[H")
        for line in lines:
            sys.stdout.write("\033[2K" + line + "\n")
        for _ in range(max(0, _LAST_FRAME_LINES - len(lines))):
            sys.stdout.write("\033[2K\n")
        sys.stdout.write("\033[J")
        sys.stdout.flush()
        _LAST_FRAME_LINES = len(lines)
    else:
        # Old consoles without VT cannot do colored in-place drawing.  Clear
        # only when content actually changed, not on every one-second tick.
        os.system("cls")
        print(frame, flush=True)
    _LAST_FRAME = frame


def read_key(timeout: float = 1.0) -> str | None:
    if os.name != "nt":
        time.sleep(timeout)
        return None
    import msvcrt
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            return msvcrt.getwch().lower()
        time.sleep(0.05)
    return None


def main() -> int:
    global _VT_ENABLED
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Консольная панель Svacer triage")
    parser.add_argument("--job")
    parser.add_argument("--once", action="store_true", help="Показать состояние один раз")
    args = parser.parse_args()
    app_directory = Path(__file__).resolve().parent
    root = app_directory.parent
    job = resolve_job(root, args.job)
    settings = read_json(app_directory / "svacer-settings.json")
    mcp_url = str(settings.get("mcp_url") or "http://127.0.0.1:8002/mcp")
    token = os.getenv("SVACER_LOCAL_MCP_TOKEN", "")
    message = ""
    mcp_status = check_mcp(mcp_url, token)
    try:
        _VT_ENABLED = enable_virtual_terminal()
        reset_screen()
        while True:
            state = collect_state(job)
            display_frame(render(state, message, mcp_status, use_color=_VT_ENABLED))
            message = ""
            if args.once:
                return 0
            key = read_key()
            if key is None:
                continue
            if key == "q":
                return 0
            if key == "r":
                mcp_status = check_mcp(mcp_url, token)
                message = "Состояние обновлено."
                continue
            if key == "p":
                set_pause(job, True)
                message = "Пауза запрошена: новая партия больше не будет выдана."
            elif key == "c":
                set_pause(job, False)
                message = "Продолжение разрешено. Если Codex уже остановился, напишите ему «продолжи»."
            elif key == "o":
                os.startfile(job)
                message = "Папка задачи открыта."
            elif key == "m":
                try:
                    start_svacer_reconnect(app_directory)
                    mcp_status = "ожидается вход"
                    message = "Открыто окно входа в Svacer. После успешного запуска нажмите R."
                except Exception as exc:
                    mcp_status = "ошибка запуска"
                    message = f"Не удалось открыть подключение: {describe_exception(exc)}"
            elif key in {"i", "s"}:
                if not token:
                    message = "Нет локального MCP-токена. Откройте START.cmd и выберите пункт 6."
                    continue
                mcp_status = check_mcp(mcp_url, token)
                if mcp_status != "подключён":
                    message = "Импорт не запускался: сначала нажмите M, войдите в Svacer, затем нажмите R."
                    continue
                if key == "i":
                    if state["total"] == 0 or state["completed"] != state["total"]:
                        message = "Импорт можно готовить только после заполнения всех решений."
                        continue
                    try:
                        message = "Проверяю фильтр, маркеры и текущую разметку Svacer. Подождите..."
                        display_frame(render(collect_state(job), message, mcp_status, use_color=_VT_ENABLED), force=True)
                        reply = asyncio.run(call_mcp_tool(
                            mcp_url, token, "prepare_markup_import", {"job_directory": str(job)}
                        ))
                        payload = json.loads(reply)
                        mcp_status = "подключён"
                        message = (
                            f"Файл подготовлен: {payload.get('marker_count')} маркеров, "
                            f"конфликтов {payload.get('conflict_count')}. Проверьте preview."
                        )
                    except Exception as exc:
                        mcp_status = "ошибка"
                        message = f"Подготовка не выполнена: {friendly_mcp_error(exc)}"
                else:
                    preview_path = job / "svacer-import-preview.json"
                    if not preview_path.exists():
                        message = "Сначала нажмите I и проверьте preview."
                        continue
                    if (job / "svacer-import-attempt.json").exists():
                        message = "Попытка импорта уже записана; повторная отправка заблокирована."
                        continue
                    try:
                        preview = read_json(preview_path)
                        force = bool(preview.get("requires_force"))
                        expected = preview.get("force_confirmation" if force else "confirmation")
                        reset_screen()
                        print("ОТПРАВКА ИЗМЕНИТ РАЗМЕТКУ В SVACER\n")
                        print(f"Ветка: {preview.get('branch_id')}")
                        print(f"Маркеров: {preview.get('marker_count')}")
                        print(f"Конфликтов: {preview.get('conflict_count')}")
                        print(preview.get("scope", ""))
                        print(f"Режим: {'force — существующая разметка будет заменена' if force else 'none'}")
                        print("\nДля подтверждения введите дословно:")
                        print(expected)
                        typed = input("\n> ")
                        if typed != expected:
                            message = "Фраза не совпала. Ничего не отправлено."
                            continue
                        message = "Отправляю разметку и выполняю обратную проверку. Не закрывайте окно..."
                        display_frame(render(collect_state(job), message, mcp_status, use_color=_VT_ENABLED), force=True)
                        reply = asyncio.run(call_mcp_tool(
                            mcp_url,
                            token,
                            "apply_markup_import",
                            {
                                "job_directory": str(job),
                                "confirmation": typed,
                                "overwrite": "force" if force else "none",
                            },
                        ))
                        payload = json.loads(reply)
                        mcp_status = "подключён"
                        verified = bool((payload.get("verification") or {}).get("verified"))
                        message = (
                            "Отправка выполнена и проверена обратным экспортом."
                            if verified else
                            "Итог отправки не подтверждён. Проверьте журнал Svacer и result/readback; повтор заблокирован."
                        )
                    except Exception as exc:
                        mcp_status = "ошибка"
                        message = f"Отправка не подтверждена: {friendly_mcp_error(exc)}"
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
