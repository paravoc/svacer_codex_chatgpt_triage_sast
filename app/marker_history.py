#!/usr/bin/env python3
"""Persistent, local-only history for Svacer marker analysis attempts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


HISTORY_FILE = "marker-history.jsonl"
EVENT_FILE = "codex-events.jsonl"
USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
OUTCOME_VERDICTS = frozenset({"Confirmed", "False Positive", "Won't fix", "Unclear"})
ERROR_STATUSES = frozenset({"failed", "incomplete"})
DECISION_SNAPSHOT_FIELDS = (
    "verdict", "confidence", "severity", "action", "review_contract_version",
    "source_revision", "decision_policy_version", "component_defect_proven", "product_defect_reachable",
    "defect_scope", "disposition_reason", "source", "control", "sink", "entrypoint",
    "build_reachability", "product_reachability", "impact", "reachable_path",
    "evidence", "counterevidence", "proof_gaps", "boundary", "comment", "source_evidence",
)
DECISION_FIELD_LABELS = {
    "verdict": "Вердикт", "confidence": "Уверенность",
    "severity": "Критичность", "action": "Действие",
    "review_contract_version": "Версия контракта проверки",
    "source_revision": "Ревизия исходников", "decision_policy_version": "Версия политики",
    "component_defect_proven": "Дефект компонента доказан",
    "product_defect_reachable": "Достижимость из продукта",
    "defect_scope": "Область дефекта", "disposition_reason": "Основание Won't fix",
    "source": "Источник", "control": "Ограничения", "sink": "Опасная операция",
    "entrypoint": "Точка входа", "build_reachability": "Достижимость сборки",
    "product_reachability": "Продуктовый путь", "impact": "Последствие",
    "reachable_path": "Путь выполнения", "evidence": "Доказательства",
    "counterevidence": "Контрдоказательства", "proof_gaps": "Пробелы доказательств",
    "boundary": "Граница доверия", "comment": "Комментарий Svacer",
    "source_evidence": "Проверяемые ссылки на исходники",
}


def has_history_outcome(record: dict[str, Any]) -> bool:
    """Show an attempt only when it saved a verdict or ended with an error."""
    return (str(record.get("verdict") or "") in OUTCOME_VERDICTS
            or str(record.get("status") or "") in ERROR_STATUSES)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    values: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def decision_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    """Keep the decision state that belonged to this exact history attempt."""
    return {
        key: row[key] for key in DECISION_SNAPSHOT_FIELDS
        if key in row
    }


def _worker_decision(job: Path, record: dict[str, Any]) -> dict[str, Any]:
    try:
        batch = int(record.get("queue_batch"))
        worker = int(record.get("worker"))
    except (TypeError, ValueError):
        return {}
    path = job / "notes" / f"batch-{batch:03d}-worker-{worker}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(value, dict):
        value = value.get("decisions") if "decisions" in value else [value]
    if not isinstance(value, list):
        return {}
    marker_id = str(record.get("marker_id") or "")
    return next(
        (item for item in value if isinstance(item, dict) and str(item.get("marker_id") or "") == marker_id),
        {},
    )


def previous_history_attempt(
    records: list[dict[str, Any]], position: int,
) -> dict[str, Any] | None:
    """Find the immediately preceding attempt for the selected marker."""
    if not 0 <= position < len(records):
        return None
    current = records[position]
    key = (
        str(current.get("_job_path") or current.get("job_id") or ""),
        str(current.get("marker_id") or ""),
    )
    candidates = [
        item for index, item in enumerate(records) if index != position
        and (
            str(item.get("_job_path") or item.get("job_id") or ""),
            str(item.get("marker_id") or ""),
        ) == key
        and str(item.get("started_at") or "") < str(current.get("started_at") or "")
    ]
    return max(candidates, key=lambda item: str(item.get("started_at") or ""), default=None)


def _summary_value(name: str, value: Any) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        if name == "source_evidence":
            refs = []
            for item in value[:8]:
                if not isinstance(item, dict):
                    refs.append(str(item))
                    continue
                path = Path(str(item.get("file_path") or "—")).name
                start, end = item.get("line_start"), item.get("line_end")
                line = str(start or "—") if start == end else f"{start or '—'}–{end or '—'}"
                roles = ",".join(map(str, item.get("roles") or []))
                refs.append(f"{path}:{line}" + (f" [{roles}]" if roles else ""))
            suffix = "; …" if len(value) > 8 else ""
            return f"{len(value)} ссылок: " + "; ".join(refs) + suffix
        items = [" ".join(str(item).split()) for item in value[:6]]
        suffix = "; …" if len(value) > 6 else ""
        text = f"{len(value)} пунктов: " + "; ".join(items) + suffix
        return text if len(text) <= 500 else text[:497] + "…"
    if isinstance(value, dict):
        return f"{len(value)} полей"
    text = " ".join(str(value).split())
    return text if len(text) <= 500 else text[:497] + "…"


def compare_history_attempts(
    previous: dict[str, Any] | None, current: dict[str, Any],
) -> dict[str, Any]:
    """Return a compact, user-facing semantic diff between two attempts."""
    if previous is None:
        return {"previous": None, "changes": [], "decision_unchanged": False}
    before = previous.get("decision_snapshot") if isinstance(previous.get("decision_snapshot"), dict) else {}
    after = current.get("decision_snapshot") if isinstance(current.get("decision_snapshot"), dict) else {}
    changes = []
    for name in DECISION_SNAPSHOT_FIELDS:
        old, new = before.get(name), after.get(name)
        if old != new:
            changes.append({
                "field": name,
                "label": DECISION_FIELD_LABELS.get(name, name),
                "before": _summary_value(name, old),
                "after": _summary_value(name, new),
            })
    return {
        "previous": previous,
        "changes": changes,
        "decision_unchanged": not changes and bool(before) and bool(after),
        "status_changed": str(previous.get("status") or "") != str(current.get("status") or ""),
    }


def history_measurements(
    record: dict[str, Any], previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Select honest display measurements without inventing per-marker token shares."""
    source = record
    inherited = False
    local_revalidation = str(record.get("launch_id") or "").endswith(":revalidated")
    if local_revalidation and previous is not None and not int(record.get("batch_total_tokens") or 0):
        source = previous
        inherited = True
    duration = source.get("duration_seconds")
    duration_scope = "Точный сеанс одного маркера"
    if duration is None:
        duration = source.get("worker_elapsed_seconds")
        duration_scope = "От начала партии до сохранения результата агентом"
    if duration is None:
        duration = source.get("batch_duration_seconds")
        duration_scope = "Общее время партии"
    tokens = source.get("attributed_tokens") if source.get("tokens_exact") else None
    token_scope = "Точный расход одного маркера"
    if tokens is None and int(source.get("batch_total_tokens") or 0) > 0:
        tokens = int(source["batch_total_tokens"])
        token_scope = "Общий расход параллельной партии"
    return {
        "duration_seconds": duration,
        "duration_scope": duration_scope if duration is not None else "Не измерено",
        "tokens": tokens,
        "token_scope": token_scope if tokens is not None else "Не измерено",
        "source_record": source,
        "inherited_from_previous": inherited,
    }


def total_tokens(usage: dict[str, Any]) -> int:
    """Return the same billable-style total shown by the GUI."""
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def read_turn_observability(job: Path, byte_offset: int) -> tuple[dict[str, int], list[str]]:
    """Read usage and visible agent messages emitted by one Codex turn."""
    try:
        with (job / EVENT_FILE).open("rb") as stream:
            stream.seek(max(0, byte_offset))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return {}, []

    usage = {key: 0 for key in USAGE_KEYS}
    messages: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            for key in USAGE_KEYS:
                usage[key] += int(event["usage"].get(key) or 0)
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            text = " ".join(str(item.get("text") or "").split())
            if text and (not messages or messages[-1] != text):
                messages.append(text)
    return ({key: value for key, value in usage.items() if value}, messages)


def _decision_map(job: Path) -> dict[str, dict[str, Any]]:
    return {
        str(value.get("marker_id")): value
        for value in _read_jsonl(job / "decisions.jsonl")
        if value.get("marker_id")
    }


def _inventory_map(job: Path) -> dict[str, dict[str, Any]]:
    inventory = _read_json(job / "markers.inventory.json")
    return {
        str(value.get("id")): value
        for value in (inventory.get("markers") or [])
        if isinstance(value, dict) and value.get("id")
    }


def _assignment_data(batch: dict[str, Any]) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    workers: dict[str, int] = {}
    markers: dict[str, dict[str, Any]] = {}
    for assignment in batch.get("assignments") or []:
        if not isinstance(assignment, dict):
            continue
        worker = int(assignment.get("worker") or 0)
        for marker_id in assignment.get("marker_ids") or []:
            workers[str(marker_id)] = worker
        for marker in assignment.get("markers") or []:
            if isinstance(marker, dict) and marker.get("id"):
                markers[str(marker["id"])] = marker
    return workers, markers


def _iso_from_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


def _elapsed_seconds(started_at: str, finished_at: str) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds())
    except (TypeError, ValueError):
        return None


def append_batch_history(
    job: Path,
    context: dict[str, Any],
    *,
    launch_id: str,
    runner_batch: int,
    started_at: str,
    finished_at: str,
    elapsed_seconds: float,
    exit_code: int,
    usage: dict[str, int],
    agent_messages: list[str],
    failure_reason: str = "",
) -> list[dict[str, Any]]:
    """Append one durable history record for every marker in a completed turn."""
    batch = context.get("batch") if isinstance(context.get("batch"), dict) else {}
    marker_ids = [str(value) for value in batch.get("marker_ids") or []]
    if not marker_ids:
        return []

    workers, batch_markers = _assignment_data(batch)
    inventory = _inventory_map(job)
    decisions = _decision_map(job)
    job_data = _read_json(job / "job.json")
    exact_total = total_tokens(usage)
    queue_batch = int(context.get("batch_number") or runner_batch)
    records: list[dict[str, Any]] = []

    for marker_id in marker_ids:
        marker = batch_markers.get(marker_id) or inventory.get(marker_id) or decisions.get(marker_id) or {}
        decision = decisions.get(marker_id) or {}
        worker = int(workers.get(marker_id) or 0)
        marker_finished_at = finished_at
        marker_elapsed = elapsed_seconds
        if worker:
            note = job / "notes" / f"batch-{queue_batch:03d}-worker-{worker}.json"
            note_finished = _iso_from_mtime(note)
            note_elapsed = _elapsed_seconds(started_at, note_finished or "")
            if note_finished and note_elapsed is not None:
                marker_finished_at = note_finished
                marker_elapsed = note_elapsed

        snapshot_source = decision
        if not decision.get("verdict"):
            snapshot_source = _worker_decision(job, {
                "queue_batch": queue_batch,
                "worker": worker,
                "marker_id": marker_id,
            }) or decision

        records.append({
            "schema_version": 2,
            "attempt_id": f"{launch_id}:{runner_batch}:{marker_id}",
            "launch_id": launch_id,
            "runner_batch": runner_batch,
            "queue_batch": queue_batch,
            "job_id": job.name,
            "project_id": str(job_data.get("project_id") or ""),
            "repository_url": str(job_data.get("repository_url") or ""),
            "git_ref": str(job_data.get("git_ref") or ""),
            "requested_model": context.get("codex_model"),
            "marker_id": marker_id,
            "warnClass": str(marker.get("warnClass") or decision.get("warnClass") or ""),
            "file": str(marker.get("file") or decision.get("file") or ""),
            "line": marker.get("line") or decision.get("line"),
            "worker": worker or None,
            "started_at": started_at,
            "finished_at": marker_finished_at,
            "duration_seconds": round(elapsed_seconds, 3) if len(marker_ids) == 1 else None,
            "duration_scope": "single_marker_session" if len(marker_ids) == 1 else "unavailable",
            "batch_duration_seconds": round(elapsed_seconds, 3),
            "worker_elapsed_seconds": round(marker_elapsed, 3),
            "exit_code": exit_code,
            "failure_reason": failure_reason if exit_code else "",
            "status": "completed" if exit_code == 0 else "incomplete" if exit_code == 3 else "failed",
            "verdict": decision.get("verdict"),
            "confidence": decision.get("confidence"),
            "decision_snapshot": decision_snapshot(snapshot_source),
            "batch_marker_count": len(marker_ids),
            "batch_usage": {key: int(usage.get(key) or 0) for key in USAGE_KEYS},
            "batch_total_tokens": exact_total,
            "attributed_tokens": exact_total if len(marker_ids) == 1 and usage else None,
            "tokens_exact": len(marker_ids) == 1 and bool(usage),
            "token_accounting": "exact_single_marker" if len(marker_ids) == 1 and usage else "unavailable",
            "agent_messages": list(agent_messages),
            "messages_scope": "marker" if len(marker_ids) == 1 else "batch",
        })

    history_path = job / HISTORY_FILE
    with history_path.open("a", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return records


def _legacy_history(job: Path) -> list[dict[str, Any]]:
    """Recover token/message history from old event logs created before HISTORY_FILE."""
    events = _read_jsonl(job / EVENT_FILE)
    inventory = _inventory_map(job)
    decisions = _decision_map(job)
    job_data = _read_json(job / "job.json")
    records: list[dict[str, Any]] = []
    launch_id = "legacy"
    current: dict[str, Any] | None = None

    def finish_current(usage: dict[str, Any] | None = None, status: str = "completed") -> None:
        nonlocal current
        if current is None:
            return
        marker_ids = current["marker_ids"]
        batch_usage = {key: int((usage or {}).get(key) or 0) for key in USAGE_KEYS}
        batch_total = total_tokens(batch_usage)
        for marker_id in marker_ids:
            marker = inventory.get(marker_id) or decisions.get(marker_id) or {}
            decision = decisions.get(marker_id) or {}
            records.append({
                "schema_version": 0,
                "attempt_id": f"legacy:{current['launch_id']}:{current['batch']}:{marker_id}",
                "launch_id": current["launch_id"],
                "runner_batch": current["batch"],
                "queue_batch": None,
                "job_id": job.name,
                "project_id": str(job_data.get("project_id") or ""),
                "repository_url": str(job_data.get("repository_url") or ""),
                "git_ref": str(job_data.get("git_ref") or ""),
                "marker_id": marker_id,
                "warnClass": str(marker.get("warnClass") or decision.get("warnClass") or ""),
                "file": str(marker.get("file") or decision.get("file") or ""),
                "line": marker.get("line") or decision.get("line"),
                "worker": None,
                "started_at": current.get("started_at"),
                "finished_at": None,
                "duration_seconds": None,
                "duration_scope": "unavailable",
                "exit_code": None,
                "status": status,
                # Today's decision cannot prove which older attempt produced it.
                # Legacy events have no per-attempt decision snapshot.
                "verdict": None,
                "confidence": None,
                "batch_marker_count": len(marker_ids),
                "batch_usage": batch_usage,
                "batch_total_tokens": batch_total,
                "attributed_tokens": (
                    batch_total if len(marker_ids) == 1
                    else round(batch_total / len(marker_ids)) if marker_ids else 0
                ),
                "tokens_exact": len(marker_ids) == 1,
                "token_accounting": "exact_single_marker" if len(marker_ids) == 1 else "estimated_equal_share",
                "agent_messages": list(current["messages"]),
                "messages_scope": "marker" if len(marker_ids) == 1 else "batch",
                "legacy": True,
            })
        current = None

    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "triage.run.started":
            finish_current(status="interrupted")
            launch_id = str(event.get("launch_id") or "legacy")
        elif event_type == "triage.batch.started":
            finish_current(status="interrupted")
            current = {
                "launch_id": launch_id,
                "batch": int(event.get("batch") or 0),
                "marker_ids": [str(value) for value in event.get("marker_ids") or []],
                "started_at": event.get("timestamp"),
                "messages": [],
            }
        elif current is not None and event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = " ".join(str(item.get("text") or "").split())
                if text and (not current["messages"] or current["messages"][-1] != text):
                    current["messages"].append(text)
        elif current is not None and event_type == "turn.completed":
            finish_current(event.get("usage") if isinstance(event.get("usage"), dict) else {})
    finish_current(status="interrupted")
    return records


def load_marker_history(job: Path) -> list[dict[str, Any]]:
    """Load new durable records and compatible history reconstructed from old logs."""
    saved = _read_jsonl(job / HISTORY_FILE)
    saved_keys = {
        (str(record.get("launch_id") or ""), int(record.get("runner_batch") or 0), str(record.get("marker_id") or ""))
        for record in saved
    }
    legacy = [
        record for record in _legacy_history(job)
        if (
            str(record.get("launch_id") or ""),
            int(record.get("runner_batch") or 0),
            str(record.get("marker_id") or ""),
        ) not in saved_keys
    ]
    records = []
    current_decisions = _decision_map(job)
    for original in saved + legacy:
        if not has_history_outcome(original):
            continue
        record = normalize_history_measurements(original)
        if not isinstance(record.get("decision_snapshot"), dict) or not record["decision_snapshot"]:
            source = _worker_decision(job, record)
            if not source and record.get("verdict"):
                source = current_decisions.get(str(record.get("marker_id") or ""), {})
            if source:
                record["decision_snapshot"] = decision_snapshot(source)
        records.append(record)
    return records


def normalize_history_measurements(record: dict[str, Any]) -> dict[str, Any]:
    """Do not present batch shares or worker file timestamps as marker measurements."""
    record = dict(record)
    single = int(record.get("batch_marker_count") or 1) == 1
    if not single:
        if record.get("duration_scope") == "marker_worker":
            record.setdefault("worker_elapsed_seconds", record.get("duration_seconds"))
        elif record.get("duration_scope") == "batch":
            record.setdefault("batch_duration_seconds", record.get("duration_seconds"))
        record["duration_seconds"] = None
        record["duration_scope"] = "unavailable"
        record["attributed_tokens"] = None
        record["tokens_exact"] = False
        record["token_accounting"] = "unavailable"
    elif not any((record.get("batch_usage") or {}).values()):
        record["attributed_tokens"] = None
        record["tokens_exact"] = False
        record["token_accounting"] = "unavailable"
    elif record.get("duration_scope") in {"marker_worker", "batch"}:
        record["duration_scope"] = "single_marker_session"
    return record
