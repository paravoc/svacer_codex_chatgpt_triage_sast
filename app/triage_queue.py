#!/usr/bin/env python3
"""Small persistent work queue for Svacer MCP triage.

The decisions JSONL is the state store: verdict=null means pending. This keeps
resume semantics transparent and avoids another MCP server or database.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from comment_format import svacer_comment_text


VALID_VERDICTS = {"Confirmed", "False Positive", "Won't fix", "Unclear"}
GOST_FILTER = 'filter(markers, "ГОСТ 71207-2024" in .checker_labels)'
DEFAULT_MCP_URL = "http://127.0.0.1:8002/mcp"
VALID_CONFIDENCE = {"high", "medium", "low"}
VALID_SEVERITIES = {"Critical", "Major", "Minor"}
VALID_ACTIONS = {"Fix required", "Fix submitted", "Ignore"}
STRICT_SCHEMA_VERSION = 2
VALID_VERIFICATION_STATUSES = {"not_required", "pending", "verified", "challenged"}
VALID_VERIFICATION_DECISIONS = {"verified", "challenged"}
VALID_CHALLENGE_TYPES = {
    "source_contradiction",
    "preventing_control",
    "build_reachability_gap",
    "product_reachability_gap",
    "impact_gap",
    "revision_mismatch",
}
VALID_CHALLENGE_VERDICTS = {"False Positive", "Won't fix", "Unclear"}
VALID_RUN_MODES = {"single_batch", "until_complete"}
HEADINGS = {
    "Confirmed": "CONFIRMED",
    "False Positive": "FALSE POSITIVE",
    "Won't fix": "WONT FIX",
    "Unclear": "UNCLEAR",
}
WORKER_NOTE_RE = re.compile(r"^batch-(\d+)-worker-(\d+)\.json$", re.IGNORECASE)
VERIFIER_NOTE_RE = re.compile(r"^verify-batch-(\d+)-verifier-(\d+)\.json$", re.IGNORECASE)


def complete_desktop_settings(settings: dict | None = None) -> dict:
    """Add safe workflow defaults without replacing explicit user choices."""
    result = dict(settings) if isinstance(settings, dict) else {}
    defaults = {
        "mcp_url": DEFAULT_MCP_URL,
        "filter_name": "ГОСТ 71207-2024",
        "advanced_filter": GOST_FILTER,
        "parallel_workers": 1,
        "verification_enabled": True,
        "verification_verdicts": ["Confirmed"],
        "verification_workers": 1,
        "saved_context_token_warning": 200000,
    }
    for key, value in defaults.items():
        if key not in result:
            result[key] = list(value) if isinstance(value, list) else value
    return result


def empty_verification(verdict: str | None) -> dict:
    return {
        "status": "pending" if verdict == "Confirmed" else "not_required",
        "verifier_id": None,
        "reason": "",
        "evidence": [],
        "rechecked_paths": [],
        "verified_at": None,
    }


def verification_status(decision: dict) -> str:
    if decision.get("verdict") != "Confirmed":
        return "not_required"
    verification = decision.get("verification")
    if not isinstance(verification, dict):
        return "pending"
    status = verification.get("status")
    return status if status in VALID_VERIFICATION_STATUSES else "pending"


def read_state_text(path: Path) -> str:
    """Read a whole state snapshot, retrying brief Windows replace/AV locks only."""
    for attempt in range(20):
        try:
            return path.read_text(encoding="utf-8-sig")
        except PermissionError as exc:
            if (os.name != "nt" and getattr(exc, "winerror", None) not in {5, 32, 33}) or attempt == 19:
                raise
            time.sleep(min(.01 * (attempt + 1), .1))
    raise AssertionError("unreachable")


def load_inventory(path: Path) -> list[dict]:
    value = json.loads(read_state_text(path))
    return validate_inventory(value)


def validate_inventory(value: object) -> list[dict]:
    """Validate a complete inventory before persisting or using it."""
    if isinstance(value, str):
        value = json.loads(value)
    markers = value.get("markers") if isinstance(value, dict) else None
    if not isinstance(markers, list):
        raise SystemExit("Некорректный inventory.json: отсутствует массив markers")
    if value.get("truncated") is not False:
        raise SystemExit("Инвентарь не подтверждает truncated=false")
    if any(type(value.get(key)) is not int or value[key] != len(markers)
           for key in ("total_count", "returned_count")):
        raise SystemExit("Инвентарь обрезан; повтори get_markers с limit=0")
    filters = value.get("filters_applied")
    if not isinstance(filters, dict) or filters.get("advanced_filter") != GOST_FILTER:
        raise SystemExit("Инвентарь не подтверждает точный фильтр ГОСТ 71207-2024")
    if any(filters.get(key) for key in ("severity", "review", "warnClass", "file", "custom_filter")):
        raise SystemExit("Инвентарь должен содержать весь ГОСТ без дополнительных фильтров")
    if any(not isinstance(marker, dict) for marker in markers):
        raise SystemExit("Некорректный элемент markers")
    ids = [str(marker.get("id") or "") for marker in markers]
    if any(not marker_id for marker_id in ids) or len(set(ids)) != len(ids):
        raise SystemExit("Пустые или повторяющиеся marker id в inventory.json")
    return markers


def load_decisions(path: Path, *, include_reviewed: bool = True) -> list[dict]:
    result = []
    for number, line in enumerate(read_state_text(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Ошибка JSONL, строка {number}: {exc}") from exc
        if not isinstance(item, dict):
            raise SystemExit(f"Строка {number}: ожидался JSON-объект")
        result.append(item)
    inventory_path = path.parent / "markers.inventory.json"
    if include_reviewed and inventory_path.is_file():
        # Older jobs omitted reviewed markers. Expose blank local work records,
        # never copy the remote verdict into a proven local decision.
        inventory = load_inventory(inventory_path)
        known = {row.get("marker_id") for row in result}
        missing = [new_pending_decision(marker) for marker in inventory
                   if marker["id"] not in known and marker_review_status(marker) != "Undecided"]
        if missing:
            result.extend(missing)
            order = {marker["id"]: index for index, marker in enumerate(inventory)}
            result.sort(key=lambda row: order.get(row.get("marker_id"), len(order)))
    return result


def marker_review_status(marker: dict) -> str:
    """Return the current Svacer review status from a compact marker row."""
    review = marker.get("review")
    if review is None:
        return "Undecided"
    if isinstance(review, dict):
        status = review.get("status")
    elif isinstance(review, str):
        status = review
    else:
        raise SystemExit("Некорректное поле review в inventory.json")
    normalized = str(status or "").strip()
    if not normalized or normalized.casefold() == "undecided":
        return "Undecided"
    return normalized


def markers_for_triage(inventory: list[dict]) -> list[dict]:
    """Every inventory marker is selectable; only explicit selections are queued."""
    return list(inventory)


def new_pending_decision(marker: dict) -> dict:
    return {
        "schema_version": 2, "marker_id": marker["id"],
        **{key: marker.get(key) for key in ("warnClass", "file", "line")},
        "verdict": None, "confidence": None,
        **{key: "" for key in ("entrypoint", "source", "control", "sink",
                               "build_reachability", "product_reachability", "impact", "comment")},
        **{key: [] for key in ("reachable_path", "evidence", "counterevidence", "proof_gaps")},
        "boundary": {"product_surface": "unknown", "source_trust": "unknown",
                     "boundary_crossed": None, "policy_basis": "unknown"},
        "verification": empty_verification(None),
    }


def state(inventory: list[dict], decisions: list[dict]) -> tuple[dict[str, dict], Counter]:
    by_id = {str(item.get("marker_id") or ""): item for item in decisions}
    if "" in by_id or len(by_id) != len(decisions):
        raise SystemExit("Пустые или повторяющиеся marker_id в decisions.jsonl")
    inventory_by_id = {str(marker["id"]): marker for marker in inventory}
    inventory_ids = set(inventory_by_id)
    triage_ids = {str(marker["id"]) for marker in markers_for_triage(inventory)}
    if not triage_ids.issubset(by_id) or not set(by_id).issubset(inventory_ids):
        missing = sorted(triage_ids - set(by_id))
        extra = sorted(set(by_id) - inventory_ids)
        raise SystemExit(f"decisions.jsonl не совпадает с inventory: missing={missing[:5]}, extra={extra[:5]}")
    counts = Counter()
    for decision in decisions:
        marker_id = str(decision["marker_id"])
        marker = inventory_by_id[marker_id]
        if any(decision.get(key) != marker.get(key) for key in ("warnClass", "file", "line")):
            raise SystemExit("Метаданные решения не совпадают с инвентарём")
        if marker_id not in triage_ids:
            continue
        verdict = decision.get("verdict")
        if verdict is not None and (not isinstance(verdict, str) or verdict not in VALID_VERDICTS):
            raise SystemExit("Недопустимый verdict в очереди; запись не считается Pending")
        counts[verdict if verdict in VALID_VERDICTS else "Pending"] += 1
    return by_id, counts


def progress_payload(inventory: list[dict], decisions: list[dict]) -> dict:
    _, counts = state(inventory, decisions)
    triage_total = len(markers_for_triage(inventory))
    pending = counts.get("Pending", 0)
    triage_ids = {str(marker["id"]) for marker in markers_for_triage(inventory)}
    confirmed = [
        decision for decision in decisions
        if str(decision.get("marker_id") or "") in triage_ids
        and decision.get("verdict") == "Confirmed"
    ]
    verification_counts = Counter(verification_status(decision) for decision in confirmed)
    return {
        "inventory_total": len(inventory),
        "already_reviewed": sum(marker_review_status(m) != "Undecided" for m in inventory),
        "total": triage_total,
        "completed": triage_total - pending,
        "pending": pending,
        "by_verdict": {name: counts.get(name, 0) for name in sorted(VALID_VERDICTS)},
        "verification": {
            "required": len(confirmed),
            "verified": verification_counts.get("verified", 0),
            "pending": verification_counts.get("pending", 0),
            "challenged": verification_counts.get("challenged", 0),
            "import_ready": verification_counts.get("verified", 0) == len(confirmed),
        },
    }


def saved_draft_ids(job: Path, decisions: list[dict]) -> set[str]:
    """Unapplied work belongs in Drafts, not in the automatic work queue."""
    pending = {str(row["marker_id"]) for row in decisions
               if row.get("marker_id") and row.get("verdict") not in VALID_VERDICTS}
    drafts: set[str] = set()
    notes = job / "notes"
    if notes.is_dir():
        for path in notes.iterdir():
            if not WORKER_NOTE_RE.match(path.name) or path.is_symlink():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            rows = value.get("decisions") if isinstance(value, dict) else value
            if not isinstance(rows, list):
                continue
            for raw_row in rows:
                row = normalize_worker_result(raw_row) if isinstance(raw_row, dict) else raw_row
                if isinstance(row, dict) and (
                    (isinstance(row.get("verdict"), str) and row["verdict"] in VALID_VERDICTS)
                    or row.get("analysis_status") == "needs_context"
                ):
                    marker_id = str(row.get("marker_id") or "")
                    if marker_id in pending:
                        drafts.add(marker_id)
    incomplete_path = job / "incomplete-analysis.json"
    if incomplete_path.is_file() and not incomplete_path.is_symlink():
        try:
            value = json.loads(incomplete_path.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                drafts.update(pending.intersection(value))
        except (OSError, json.JSONDecodeError):
            pass
    return drafts


def next_parallel_batch(
    inventory: list[dict], decisions: list[dict], limit: int, workers: int,
    preferred_ids: list[str] | None = None,
    excluded_ids: set[str] | None = None,
) -> dict:
    if not 1 <= limit <= 50 or not 1 <= workers <= 8:
        raise SystemExit("Некорректный размер очереди или число агентов")
    # An assignment is one marker, never an opaque queue of work for an agent.
    limit = min(limit, workers)
    by_id, _ = state(inventory, decisions)
    pending = [
        marker
        for marker in markers_for_triage(inventory)
        if by_id[str(marker["id"])].get("verdict") not in VALID_VERDICTS
        and str(marker["id"]) not in (excluded_ids or set())
    ]
    payload = progress_payload(inventory, decisions)
    if not pending:
        return {"progress": payload, "batch": None}

    if preferred_ids:
        pending_by_id = {str(marker["id"]): marker for marker in pending}
        unavailable = [marker_id for marker_id in preferred_ids if marker_id not in pending_by_id]
        if unavailable:
            raise SystemExit(
                "Выбранные маркеры уже обработаны или не входят в очередь: "
                + ", ".join(unavailable)
            )
        if len(preferred_ids) != len(set(preferred_ids)):
            raise SystemExit("Повторяющиеся ID в выбранной очереди")
        selected = [pending_by_id[marker_id] for marker_id in preferred_ids[:limit]]
    else:
        selected = pending[:limit]
    grouped: OrderedDict[tuple[object, object], list[dict]] = OrderedDict()
    for marker in selected:
        key = (marker.get("warnClass"), marker.get("file"))
        grouped.setdefault(key, []).append(marker)

    chunks = [
        {"key": key, "markers": markers}
        for key, markers in grouped.items()
    ]
    target_workers = min(workers, len(selected))
    while len(chunks) < target_workers:
        splittable = [chunk for chunk in chunks if len(chunk["markers"]) > 1]
        if not splittable:
            break
        largest = max(splittable, key=lambda chunk: len(chunk["markers"]))
        chunks.remove(largest)
        midpoint = (len(largest["markers"]) + 1) // 2
        chunks.append({"key": largest["key"], "markers": largest["markers"][:midpoint]})
        chunks.append({"key": largest["key"], "markers": largest["markers"][midpoint:]})

    assignments = [
        {"worker": number + 1, "count": 0, "groups": [], "marker_ids": [], "markers": []}
        for number in range(target_workers)
    ]
    for chunk in sorted(chunks, key=lambda item: len(item["markers"]), reverse=True):
        assignment = min(assignments, key=lambda item: (item["count"], item["worker"]))
        warn_class, file_name = chunk["key"]
        compact = [
            {
                "id": marker.get("id"),
                "warnClass": marker.get("warnClass"),
                "file": marker.get("file"),
                "line": marker.get("line"),
                "msg": marker.get("msg"),
                "function": marker.get("function"),
                "review": marker.get("review"),
            }
            for marker in chunk["markers"]
        ]
        marker_ids = [str(marker["id"]) for marker in compact]
        assignment["groups"].append(
            {
                "warnClass": warn_class,
                "file": file_name,
                "marker_ids": marker_ids,
            }
        )
        assignment["marker_ids"].extend(marker_ids)
        assignment["markers"].extend(compact)
        assignment["count"] += len(compact)

    trace_groups = [
        {
            "warnClass": key[0],
            "file": key[1],
            "marker_ids": [str(marker["id"]) for marker in markers],
        }
        for key, markers in grouped.items()
    ]
    return {
        "progress": payload,
        "batch": {
            "count": len(selected),
            "worker_count": len(assignments),
            "marker_ids": [str(marker["id"]) for marker in selected],
            "trace_groups": trace_groups,
            "assignments": assignments,
        },
    }


def next_verification_batch(
    inventory: list[dict], decisions: list[dict], limit: int, workers: int,
) -> dict:
    triage_ids = {str(marker["id"]) for marker in markers_for_triage(inventory)}
    pending = [
        decision for decision in decisions
        if str(decision.get("marker_id") or "") in triage_ids
        and decision.get("verdict") == "Confirmed"
        and verification_status(decision) == "pending"
    ]
    if not pending:
        return {"count": 0, "worker_count": 0, "marker_ids": [], "assignments": []}
    selected = pending[:limit]
    target_workers = min(workers, len(selected))
    assignments = [
        {"verifier": number + 1, "count": 0, "marker_ids": [], "decisions": []}
        for number in range(target_workers)
    ]
    for decision in selected:
        assignment = min(assignments, key=lambda item: (item["count"], item["verifier"]))
        assignment["marker_ids"].append(str(decision["marker_id"]))
        assignment["decisions"].append(decision)
        assignment["count"] += 1
    return {
        "count": len(selected),
        "worker_count": len(assignments),
        "marker_ids": [str(item["marker_id"]) for item in selected],
        "assignments": assignments,
    }


def replace_state_file(temporary: Path, path: Path) -> None:
    """Windows readers/AV can briefly deny replace; never truncate the old state.

    A permanent error preserves both the old file and pending temp for recovery.
    Retries are bounded and apply only to sharing/access-denied errors.
    """
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            if (os.name != "nt" and getattr(exc, "winerror", None) not in {5, 32, 33}) or attempt == 19:
                raise
            time.sleep(min(.01 * (attempt + 1), .1))


def atomic_write_jsonl(path: Path, records: list[dict]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_state_file(temporary, path)


def atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_state_file(temporary, path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pause_requested(decisions_path: Path) -> bool:
    control_path = decisions_path.parent / "control.json"
    if not control_path.exists():
        return False
    try:
        value = json.loads(read_state_text(control_path))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Некорректный control.json: {exc}") from exc
    if not isinstance(value, dict) or type(value.get("pause_requested", False)) is not bool:
        raise SystemExit("Некорректный control.json: pause_requested должен быть bool")
    return value.get("pause_requested", False)


def priority_marker_ids(decisions_path: Path) -> list[str]:
    """Return a one-shot marker selection requested by the local GUI."""
    path = decisions_path.parent / "control.json"
    if not path.exists():
        return []
    try:
        value = json.loads(read_state_text(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Некорректный control.json: {exc}") from exc
    raw = value.get("priority_marker_ids", []) if isinstance(value, dict) else None
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise SystemExit("Некорректный control.json: priority_marker_ids должен быть массивом ID")
    if len(raw) != len(set(raw)):
        raise SystemExit("Некорректный control.json: priority_marker_ids содержит повторы")
    return raw


def clear_priority_request(decisions_path: Path) -> None:
    path = decisions_path.parent / "control.json"
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Некорректный control.json: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("Некорректный control.json: ожидался JSON-объект")
    value.pop("priority_marker_ids", None)
    value.pop("single_marker_requested", None)
    value.pop("manual_queue_requested", None)
    value.pop("recheck_marker_ids", None)
    value.pop("deferred_marker_ids", None)
    value["updated_at"] = utc_now()
    atomic_write_json(path, value)


def runnable_priority_marker_ids(decisions_path: Path) -> list[str]:
    """Keep unfinished selections visible, but attempt them once per user launch."""
    selected = priority_marker_ids(decisions_path)
    path = decisions_path.parent / "control.json"
    control = json.loads(read_state_text(path)) if path.exists() else {}
    deferred = set(control.get("deferred_marker_ids") or [])
    return [mid for mid in selected if mid not in deferred]


def defer_incomplete_markers(decisions_path: Path, marker_ids: list[str]) -> None:
    """Called under decision_lock. Defer, never consume, unfinished manual work."""
    path = decisions_path.parent / "control.json"
    control = json.loads(read_state_text(path)) if path.exists() else {}
    selected = control.get("priority_marker_ids") or []
    deferred = set(control.get("deferred_marker_ids") or []) | set(marker_ids)
    control["deferred_marker_ids"] = [mid for mid in selected if mid in deferred]
    control["updated_at"] = utc_now()
    atomic_write_json(path, control)


def enqueue_marker_ids(inventory_path: Path, decisions_path: Path, ids: list[str]) -> dict:
    """Append an explicit FIFO selection without disturbing a running assignment."""
    if not ids or any(not isinstance(mid, str) or not mid for mid in ids):
        raise SystemExit("Выберите хотя бы один маркер")
    if len(ids) != len(set(ids)):
        raise SystemExit("В выбранных маркерах есть повторяющиеся ID")
    with decision_lock(decisions_path):
        job = decisions_path.parent
        from codex_run import read_run_record
        run = read_run_record(job)
        active = bool(run.get("active"))
        if active and run.get("status") == "stopping":
            raise SystemExit("Анализ завершается. Добавьте маркеры после его остановки")
        if (job / "svacer-import-attempt.json").exists():
            raise SystemExit("Очередь нельзя менять после попытки отправки в Svacer")
        inventory = load_inventory(inventory_path)
        decisions = load_decisions(decisions_path)
        by_id, _ = state(inventory, decisions)
        triage_ids = {str(marker["id"]) for marker in markers_for_triage(inventory)}
        unknown = [mid for mid in ids if mid not in by_id]
        excluded = [mid for mid in ids if mid not in triage_ids]
        if unknown or excluded:
            raise SystemExit(f"Маркеры не входят в локальную очередь: {unknown or excluded}")
        status_path = job / "workers.status.json"
        status: dict = {}
        if status_path.exists():
            status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            if not isinstance(status, dict):
                raise SystemExit("Некорректный workers.status.json")
        assigned = [str(mid) for worker in status.get("workers", [])
                    for mid in worker.get("marker_ids", [])]
        if active:
            if status.get("state") != "assigned" or not assigned:
                raise SystemExit("Дождитесь назначения текущего маркера и повторите добавление")
        control_path = job / "control.json"
        control = json.loads(control_path.read_text(encoding="utf-8-sig")) if control_path.exists() else {}
        if not isinstance(control, dict):
            raise SystemExit("Некорректный control.json")
        if active and control.get("pause_requested"):
            raise SystemExit("Анализ останавливается. Добавьте маркеры после его остановки")
        existing = priority_marker_ids(decisions_path)
        if (not active and status.get("state") == "assigned" and assigned
                and (len(set(assigned)) != len(assigned)
                     or set(existing[:len(assigned)]) != set(assigned))):
            raise SystemExit("Есть незавершённое назначение. Сначала завершите или сбросьте прежнюю очередь")
        # A stopped/failed manual launch keeps its reservation at the FIFO head.
        # Worker grouping may list that same reservation in a different order.
        # Appending after that unchanged prefix is safe and must not require a reset.
        rechecks = control.get("recheck_marker_ids") or []
        if not isinstance(rechecks, list) or any(not isinstance(mid, str) for mid in rechecks):
            raise SystemExit("Некорректный control.json: recheck_marker_ids")
        assigned_set = set(assigned) if active else set()
        already_active = [mid for mid in ids if mid in assigned_set]
        already_queued = [mid for mid in ids if mid in existing and mid not in assigned_set]
        added = [mid for mid in ids if mid not in existing and mid not in assigned_set]
        if not added:
            return {"added": [], "rechecks": [], "queued": existing,
                    "already_active": already_active, "already_queued": already_queued}
        new_rechecks = []
        reviewed = {str(m["id"]) for m in inventory if marker_review_status(m) != "Undecided"}
        for mid in added:
            if (by_id[mid].get("verdict") in VALID_VERDICTS or mid in reviewed) and mid not in rechecks:
                rechecks.append(mid)
                new_rechecks.append(mid)
        # Keep the current reservation first: prepare_batch compares the saved
        # context with this prefix when it resumes after a trace fetch failure.
        queued = ([*dict.fromkeys(assigned + existing)] if active else existing) + added
        control.update({
            "priority_marker_ids": queued,
            "recheck_marker_ids": rechecks,
            "manual_queue_requested": True,
            "pause_requested": False if active else True,
            "single_batch_completed": False,
            "one_shot_completed": False,
            "updated_at": utc_now(),
            "source": "manual marker queue",
        })
        control.pop("single_marker_requested", None)
        control.pop("run_remaining", None)
        if len(decisions) != len(load_decisions(decisions_path, include_reviewed=False)):
            atomic_write_jsonl(decisions_path, decisions)
        atomic_write_json(control_path, control)
        if active:
            # The current batch was assigned before manual mode was requested.
            # Its completion must consume that assignment and continue with the
            # newly appended FIFO instead of closing the original batch quota.
            status["one_shot"] = True
            atomic_write_json(status_path, status)
        return {"added": added, "rechecks": new_rechecks,
                "queued": queued, "already_active": already_active,
                "already_queued": already_queued}


def dequeue_marker_ids(decisions_path: Path, ids: list[str]) -> dict:
    """Remove selected waiting markers, leaving assigned work and decisions intact."""
    if not ids or any(not isinstance(mid, str) or not mid for mid in ids):
        raise SystemExit("Выберите хотя бы один маркер из очереди")
    if len(ids) != len(set(ids)):
        raise SystemExit("В выбранных маркерах есть повторяющиеся ID")
    with decision_lock(decisions_path):
        job = decisions_path.parent
        from codex_run import read_run_record
        run = read_run_record(job)
        active = bool(run.get("active"))
        if active and run.get("status") == "stopping":
            raise SystemExit("Анализ завершается. Дождитесь остановки перед изменением очереди")
        if (job / "svacer-import-attempt.json").exists():
            raise SystemExit("Очередь нельзя менять после попытки отправки в Svacer")
        control_path = job / "control.json"
        if not control_path.is_file():
            raise SystemExit("Очередь пуста")
        try:
            control = json.loads(control_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Некорректный control.json: {exc}") from exc
        if not isinstance(control, dict):
            raise SystemExit("Некорректный control.json")
        if active and control.get("pause_requested"):
            raise SystemExit("Анализ останавливается. Дождитесь остановки перед изменением очереди")
        existing = priority_marker_ids(decisions_path)
        absent = [mid for mid in ids if mid not in existing]
        if absent:
            raise SystemExit("Выбранные маркеры уже не находятся в очереди: " + ", ".join(absent))

        assigned: set[str] = set()
        stale_worker_assigned: set[str] = set()
        stale_worker_status: dict | None = None
        for filename, key in (("workers.status.json", "workers"),
                              ("verifiers.status.json", "verifiers")):
            path = job / filename
            if not path.is_file():
                continue
            try:
                status = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SystemExit(f"Некорректный {filename}: {exc}") from exc
            if (not isinstance(status, dict) or not isinstance(status.get(key, []), list)
                    or any(not isinstance(worker, dict)
                           or not isinstance(worker.get("marker_ids", []), list)
                           for worker in status.get(key, []))):
                raise SystemExit(f"Некорректный {filename}")
            if filename == "workers.status.json":
                stale_worker_status = status
            if status.get("state") == "assigned":
                status_ids = {str(mid) for worker in status[key]
                              for mid in worker.get("marker_ids", [])}
                assigned.update(status_ids)
                if filename == "workers.status.json":
                    stale_worker_assigned = status_ids
        if active and any(mid in assigned for mid in ids):
            raise SystemExit("Назначенный агенту маркер нельзя удалить из очереди; дождитесь его завершения")

        removed = set(ids)
        remaining = [mid for mid in existing if mid not in removed]
        rechecks = control.get("recheck_marker_ids") or []
        if not isinstance(rechecks, list) or any(not isinstance(mid, str) for mid in rechecks):
            raise SystemExit("Некорректный control.json: recheck_marker_ids")
        removed_rechecks = [mid for mid in rechecks if mid in removed]
        control["deferred_marker_ids"] = [mid for mid in control.get("deferred_marker_ids", [])
                                           if mid not in removed]
        if remaining:
            control["priority_marker_ids"] = remaining
            control["recheck_marker_ids"] = [mid for mid in rechecks if mid not in removed]
        else:
            control.pop("priority_marker_ids", None)
            control.pop("recheck_marker_ids", None)
            control.pop("manual_queue_requested", None)
            control.pop("single_marker_requested", None)
            control.pop("deferred_marker_ids", None)
            if not active:
                control["pause_requested"] = True
        control["updated_at"] = utc_now()
        control["source"] = "manual marker queue removal"
        atomic_write_json(control_path, control)
        if (not active and stale_worker_status is not None
                and any(mid in stale_worker_assigned for mid in ids)):
            # An old reservation cannot be resumed after its marker was removed.
            stale_worker_status.update(state="superseded", workers=[], updated_at=utc_now())
            atomic_write_json(job / "workers.status.json", stale_worker_status)
        scheduler_path = job / "scheduler-state.json"
        if not active and scheduler_path.exists():
            scheduler = json.loads(scheduler_path.read_text(encoding="utf-8-sig"))
            scheduler["leases"] = {slot: entry for slot, entry in scheduler.get("leases", {}).items()
                                   if entry.get("marker_id") not in ids}
            atomic_write_json(scheduler_path, scheduler)
        return {"removed": ids, "removed_rechecks": removed_rechecks, "queued": remaining}


def job_run_mode(decisions_path: Path) -> str:
    """Return the execution policy for the job containing decisions_path."""
    path = decisions_path.parent / "job.json"
    if not path.exists():
        return "until_complete"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Некорректный job.json: {exc}") from exc
    mode = value.get("run_mode", "until_complete") if isinstance(value, dict) else None
    if mode not in VALID_RUN_MODES:
        raise SystemExit("job.json: run_mode должен быть single_batch или until_complete")
    return str(mode)


def manual_selection_only(decisions_path: Path) -> bool:
    """New and existing desktop jobs require an explicit selected-marker FIFO.

    The opt-out only keeps older CLI fixtures/workflows readable; the desktop
    creates and edits manual jobs exclusively.
    """
    path = decisions_path.parent / "job.json"
    if not path.exists():
        return True
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Некорректный job.json: {exc}") from exc
    if not isinstance(value, dict) or type(value.get("manual_selection_only", True)) is not bool:
        raise SystemExit("job.json: manual_selection_only должен быть bool")
    return value.get("manual_selection_only", True)


def single_batch_completed(decisions_path: Path) -> bool:
    path = decisions_path.parent / "control.json"
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Некорректный control.json: {exc}") from exc
    completed = value.get("single_batch_completed", False) if isinstance(value, dict) else None
    if type(completed) is not bool:
        raise SystemExit("Некорректный control.json: single_batch_completed должен быть bool")
    return completed


def one_shot_completed(decisions_path: Path) -> bool:
    """Return whether a strict single-marker request has finished."""
    path = decisions_path.parent / "control.json"
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Некорректный control.json: {exc}") from exc
    completed = value.get("one_shot_completed", False) if isinstance(value, dict) else None
    if type(completed) is not bool:
        raise SystemExit("Некорректный control.json: one_shot_completed должен быть bool")
    return completed


def primary_queue_blocked(decisions_path: Path) -> bool:
    return pause_requested(decisions_path) or one_shot_completed(decisions_path) or (
        job_run_mode(decisions_path) == "single_batch" and single_batch_completed(decisions_path)
    )


def reset_queue_assignments(decisions_path: Path) -> dict:
    """Clear only transient assignments, preserving decisions and saved drafts."""
    with decision_lock(decisions_path):
        return _reset_queue_assignments_locked(decisions_path)


def _reset_queue_assignments_locked(decisions_path: Path) -> dict:
    job_directory = decisions_path.parent
    run_path = job_directory / "codex-run.json"
    if run_path.exists():
        try:
            run = json.loads(run_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Некорректный codex-run.json: {exc}") from exc
        if isinstance(run, dict) and run.get("active"):
            raise SystemExit("Нельзя сбросить очередь во время анализа. Сначала остановите задачу.")

    control_path = job_directory / "control.json"
    control: dict = {}
    if control_path.exists():
        try:
            loaded = json.loads(control_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                control.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Некорректный control.json: {exc}") from exc
    control.update({
        "pause_requested": True,
        "single_batch_completed": False,
        "one_shot_completed": False,
        "updated_at": utc_now(),
        "source": "queue reset",
    })
    control.pop("priority_marker_ids", None)
    control.pop("single_marker_requested", None)
    control.pop("manual_queue_requested", None)
    control.pop("recheck_marker_ids", None)
    control.pop("run_remaining", None)
    control.pop("deferred_marker_ids", None)
    control.pop("last_completed_batch", None)
    control.pop("scheduler_completed_attempts", None)
    atomic_write_json(control_path, control)

    scheduler_path = job_directory / "scheduler-state.json"
    if scheduler_path.exists():
        scheduler = json.loads(scheduler_path.read_text(encoding="utf-8-sig"))
        scheduler.update(leases={}, attempted=[])
        atomic_write_json(scheduler_path, scheduler)

    atomic_write_json(job_directory / "workers.status.json", {
        "state": "paused", "updated_at": utc_now(), "batch": None, "workers": [],
    })
    verifier_path = job_directory / "verifiers.status.json"
    if verifier_path.exists():
        atomic_write_json(verifier_path, {
            "state": "paused", "updated_at": utc_now(), "batch": None, "verifiers": [],
        })
    try:
        (job_directory / "batch-context.json").unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SystemExit(f"Не удалось очистить текущее назначение: {exc}") from exc
    return {"reset": True, "decisions_preserved": True, "drafts_preserved": True}


def record_single_batch_completion(decisions_path: Path) -> bool:
    """Block only the next primary batch; verification may still finish."""
    if job_run_mode(decisions_path) != "single_batch":
        return False
    path = decisions_path.parent / "control.json"
    value: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                value.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Некорректный control.json: {exc}") from exc
    job = json.loads((decisions_path.parent / "job.json").read_text(encoding="utf-8-sig"))
    status = json.loads((decisions_path.parent / "workers.status.json").read_text(encoding="utf-8-sig"))
    if value.get("last_completed_batch") == status.get("batch") and status.get("batch") is not None:
        return bool(value.get("single_batch_completed"))
    saved_count = sum(int(worker.get("assigned") or 0) for worker in status.get("workers", []))
    remaining = max(0, int(value.get("run_remaining", job.get("batch_size") or 1)) - saved_count)
    value.update({
        "run_remaining": remaining,
        "last_completed_batch": status.get("batch"),
        "single_batch_completed": remaining == 0,
        "updated_at": utc_now(),
        "source": "run_mode:single_batch",
    })
    atomic_write_json(path, value)
    return remaining == 0


def next_batch_number(job_directory: Path) -> int:
    maximum = 0
    notes = job_directory / "notes"
    if notes.is_dir():
        for path in notes.iterdir():
            match = WORKER_NOTE_RE.match(path.name)
            if match:
                maximum = max(maximum, int(match.group(1)))
    status_path = job_directory / "workers.status.json"
    if status_path.exists():
        try:
            current = json.loads(status_path.read_text(encoding="utf-8-sig"))
            if isinstance(current, dict) and type(current.get("batch")) is int:
                maximum = max(maximum, current["batch"])
        except (OSError, json.JSONDecodeError):
            pass
    return maximum + 1


def record_assignments(
    decisions_path: Path, batch: dict | None, *, paused: bool = False,
    one_shot: bool = False,
) -> None:
    status_path = decisions_path.parent / "workers.status.json"
    if paused:
        value = {
            "state": "paused",
            "updated_at": utc_now(),
            "batch": None,
            "workers": [],
        }
    elif batch is None:
        value = {
            "state": "complete",
            "updated_at": utc_now(),
            "batch": None,
            "workers": [],
        }
    else:
        value = {
            "state": "assigned",
            "updated_at": utc_now(),
            "batch": next_batch_number(decisions_path.parent),
            "one_shot": one_shot,
            "workers": [
                {
                    "worker": assignment["worker"],
                    "status": "assigned",
                    "assigned": assignment["count"],
                    "saved": 0,
                    "marker_ids": assignment["marker_ids"],
                }
                for assignment in batch["assignments"]
            ],
        }
    atomic_write_json(status_path, value)


def record_batch_completion(decisions_path: Path) -> bool:
    """Apply the one-shot policy first, otherwise the configured run mode."""
    status_path = decisions_path.parent / "workers.status.json"
    one_shot = False
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            one_shot = bool(status.get("one_shot")) if isinstance(status, dict) else False
        except (OSError, json.JSONDecodeError):
            one_shot = False
    if not one_shot:
        return record_single_batch_completion(decisions_path)

    control_path = decisions_path.parent / "control.json"
    control: dict = {}
    if control_path.exists():
        try:
            loaded = json.loads(control_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                control.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Некорректный control.json: {exc}") from exc
    completed = {str(row["marker_id"]) for row in load_decisions(decisions_path)
                 if row.get("verdict") in VALID_VERDICTS}
    consumed = {
        str(marker_id) for worker in status.get("workers", [])
        for marker_id in worker.get("marker_ids", [])
        if str(marker_id) in completed
    }
    remaining = [mid for mid in control.get("priority_marker_ids", []) if mid not in consumed]
    rechecks = [mid for mid in control.get("recheck_marker_ids", []) if mid not in consumed]
    if remaining:
        control["priority_marker_ids"] = remaining
        control["recheck_marker_ids"] = rechecks
        control["deferred_marker_ids"] = [mid for mid in control.get("deferred_marker_ids", [])
                                           if mid in remaining]
        control["one_shot_completed"] = False
        control["updated_at"] = utc_now()
        atomic_write_json(control_path, control)
        return False
    control.update({
        "pause_requested": True,
        "single_batch_completed": False,
        "one_shot_completed": True,
        "updated_at": utc_now(),
        "source": "manual marker queue",
    })
    control.pop("priority_marker_ids", None)
    control.pop("single_marker_requested", None)
    control.pop("manual_queue_requested", None)
    control.pop("recheck_marker_ids", None)
    control.pop("deferred_marker_ids", None)
    atomic_write_json(control_path, control)
    return True


def record_saved_workers(decisions_path: Path, result_paths: list[Path]) -> None:
    status_path = decisions_path.parent / "workers.status.json"
    if not status_path.exists():
        return
    try:
        value = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(value, dict) or not isinstance(value.get("workers"), list):
        return
    saved_by_worker: Counter[int] = Counter()
    for path in result_paths:
        match = WORKER_NOTE_RE.match(path.name)
        if not match:
            continue
        try:
            saved_by_worker[int(match.group(2))] += len(load_worker_results([path]))
        except SystemExit:
            continue
    for worker in value["workers"]:
        if not isinstance(worker, dict) or type(worker.get("worker")) is not int:
            continue
        number = worker["worker"]
        worker["saved"] = saved_by_worker.get(number, 0)
        if worker["saved"] >= int(worker.get("assigned") or 0):
            worker["status"] = "saved"
    value["state"] = "saved"
    value["updated_at"] = utc_now()
    atomic_write_json(status_path, value)


def next_verification_batch_number(job_directory: Path) -> int:
    maximum = 0
    notes = job_directory / "notes"
    if notes.is_dir():
        for path in notes.iterdir():
            match = VERIFIER_NOTE_RE.match(path.name)
            if match:
                maximum = max(maximum, int(match.group(1)))
    status_path = job_directory / "verifiers.status.json"
    if status_path.exists():
        try:
            current = json.loads(status_path.read_text(encoding="utf-8-sig"))
            if isinstance(current, dict) and type(current.get("batch")) is int:
                maximum = max(maximum, current["batch"])
        except (OSError, json.JSONDecodeError):
            pass
    return maximum + 1


def record_verifier_assignments(decisions_path: Path, batch: dict, *, paused: bool = False) -> None:
    status_path = decisions_path.parent / "verifiers.status.json"
    if paused:
        value = {"state": "paused", "updated_at": utc_now(), "batch": None, "verifiers": []}
    elif not batch.get("marker_ids"):
        value = {"state": "complete", "updated_at": utc_now(), "batch": None, "verifiers": []}
    else:
        value = {
            "state": "assigned",
            "updated_at": utc_now(),
            "batch": next_verification_batch_number(decisions_path.parent),
            "verifiers": [
                {
                    "verifier": assignment["verifier"],
                    "status": "assigned",
                    "assigned": assignment["count"],
                    "saved": 0,
                    "marker_ids": assignment["marker_ids"],
                }
                for assignment in batch["assignments"]
            ],
        }
    atomic_write_json(status_path, value)


def record_saved_verifiers(decisions_path: Path, result_paths: list[Path]) -> None:
    status_path = decisions_path.parent / "verifiers.status.json"
    if not status_path.exists():
        return
    try:
        value = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(value, dict) or not isinstance(value.get("verifiers"), list):
        return
    saved_by_verifier: Counter[int] = Counter()
    for path in result_paths:
        match = VERIFIER_NOTE_RE.match(path.name)
        if not match:
            continue
        try:
            saved_by_verifier[int(match.group(2))] += len(load_worker_results([path]))
        except SystemExit:
            continue
    for verifier in value["verifiers"]:
        if not isinstance(verifier, dict) or type(verifier.get("verifier")) is not int:
            continue
        number = verifier["verifier"]
        verifier["saved"] = saved_by_verifier.get(number, 0)
        if verifier["saved"] >= int(verifier.get("assigned") or 0):
            verifier["status"] = "saved"
    value["state"] = "saved"
    value["updated_at"] = utc_now()
    atomic_write_json(status_path, value)


@contextmanager
def decision_lock(path: Path):
    """OS-owned mutex: automatically released if a writer crashes or is stopped."""
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + 3
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise SystemExit("Очередь занята сохранением. Повторите действие через несколько секунд.") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        # Never unlink the mutex file: another process may already be waiting on it.


def normalize_worker_result(row: dict) -> dict:
    """Accept an unambiguous verdict stored under ``status`` by older agents.

    This is a structural repair only: it never invents a verdict or evidence.
    The regular evidence and policy validators still run before application.
    """
    result = dict(row)
    if isinstance(result.get("comment"), str):
        result["comment"] = svacer_comment_text(result["comment"])
    status = result.get("status")
    if isinstance(status, str) and status in VALID_VERDICTS:
        if not result.get("verdict"):
            result["verdict"] = status
        if result.get("verdict") == status:
            result.pop("status", None)
    if isinstance(result.get("verdict"), str) and result["verdict"] in VALID_VERDICTS:
        for name in ("reachable_path", "counterevidence", "proof_gaps"):
            result.setdefault(name, [])
        # Some agents follow the generic ``evidence`` field literally and put
        # the complete review-contract records there. This is an unambiguous
        # schema alias only when every item already contains all checked fields;
        # no quote, role, path or conclusion is manufactured here.
        generic = result.get("evidence")
        if "source_evidence" not in result and isinstance(generic, list) and generic and all(
            isinstance(item, dict)
            and {"file_path", "line_start", "line_end", "excerpt", "supports", "roles"} <= set(item)
            for item in generic
        ):
            result["source_evidence"] = generic
        if result["verdict"] != "Confirmed":
            for name in ("severity", "action"):
                if result.get(name) is None:
                    result.pop(name, None)
    return result


def load_worker_results(paths: list[Path]) -> list[dict]:
    results: list[dict] = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise SystemExit(f"Файл результата не найден: {path}") from exc
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Некорректный JSON в {path}: {exc}") from exc
        if isinstance(value, dict):
            # A one-marker worker sometimes writes the decision object directly.
            # Normalise only that unambiguous shape; ownership and quality are
            # still checked by the caller before the result can be applied.
            value = value.get("decisions") if "decisions" in value else (
                [value] if isinstance(value.get("marker_id"), str) else None
            )
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise SystemExit(f"{path}: ожидался JSON-массив решений")
        results.extend(normalize_worker_result(item) for item in value)
    return results


def validate_worker_result(
    result: dict,
    current: dict,
    *,
    allow_manual_verdict_override: bool = False,
) -> list[str]:
    marker_id = str(result.get("marker_id") or "")
    errors: list[str] = []
    if result.get("analysis_status") == "needs_context":
        errors.append(f"{marker_id}: analysis is incomplete; gather the missing evidence")
    for name in ("warnClass", "file", "line"):
        if result.get(name) != current.get(name):
            errors.append(f"{marker_id}: {name} does not match the queue assignment")
    verdict = result.get("verdict")
    if isinstance(result.get("status"), str) and result["status"] in VALID_VERDICTS and result["status"] != verdict:
        errors.append(f"{marker_id}: status contradicts verdict")
    if not isinstance(verdict, str) or verdict not in VALID_VERDICTS:
        errors.append(f"{marker_id}: invalid verdict {verdict!r}")
        return errors
    if result.get("confidence") not in VALID_CONFIDENCE:
        errors.append(f"{marker_id}: invalid confidence")
    strict = current.get("schema_version") == STRICT_SCHEMA_VERSION
    if strict and result.get("schema_version") != STRICT_SCHEMA_VERSION:
        errors.append(f"{marker_id}: schema_version={STRICT_SCHEMA_VERSION} is required")
    required_text = ["source", "control", "sink"]
    if strict:
        required_text.extend([
            "entrypoint", "build_reachability", "product_reachability", "impact",
        ])
    for name in required_text:
        if not isinstance(result.get(name), str) or not result[name].strip():
            errors.append(f"{marker_id}: {name} must be non-empty")
    for name in ("reachable_path", "evidence", "counterevidence", "proof_gaps"):
        if not isinstance(result.get(name), list):
            errors.append(f"{marker_id}: {name} must be an array")
    if isinstance(result.get("evidence"), list) and not result["evidence"]:
        errors.append(f"{marker_id}: evidence must be non-empty")
    boundary = result.get("boundary")
    if not isinstance(boundary, dict):
        errors.append(f"{marker_id}: boundary must be an object")
    elif strict:
        for name in ("product_surface", "source_trust", "policy_basis"):
            value = boundary.get(name)
            if not isinstance(value, str) or not value.strip() or value.strip().casefold() == "unknown":
                errors.append(f"{marker_id}: boundary.{name} is not proven")
        if type(boundary.get("boundary_crossed")) is not bool:
            errors.append(f"{marker_id}: boundary.boundary_crossed must be boolean")
    comment = str(result.get("comment") or "")
    nonempty = [line.strip() for line in comment.splitlines() if line.strip()]
    if not nonempty:
        errors.append(f"{marker_id}: Russian comment must be non-empty")
    policy_version = result.get("decision_policy_version")
    known_policy = type(policy_version) is int and policy_version in (1, 2, 3)
    scope_exclusion = policy_version == 3 and result.get("disposition_kind") == "scope_exclusion"
    if policy_version == 3 and not allow_manual_verdict_override:
        if (not scope_exclusion or verdict != "Won't fix" or result.get("defect_scope") != "out_of_scope"
                or result.get("component_defect_proven") is not None
                or result.get("product_defect_reachable") is not None
                or not isinstance(result.get("scope_exclusion"), dict)
                or result.get("analysis_status") != "complete"):
            errors.append(f"{marker_id}: policy 3 is only an explicit scope exclusion, not a defect verdict")
    elif result.get("disposition_kind") == "scope_exclusion" and not allow_manual_verdict_override:
        errors.append(f"{marker_id}: scope exclusion requires decision_policy_version=3")
    if strict and not known_policy and not allow_manual_verdict_override:
        errors.append(f"{marker_id}: decision_policy_version=2 is required (1 is accepted for a saved legacy run)")
    if known_policy and not allow_manual_verdict_override:
        expected = {
            "False Positive": {"none"}, "Confirmed": {"product"},
            "Won't fix": ({"out_of_scope"} if policy_version == 3 else
                          {"component"} if policy_version == 2 else {"component", "product"}),
            "Unclear": {"unknown"},
        }
        if result.get("defect_scope") not in expected[verdict]:
            errors.append(f"{marker_id}: defect_scope contradicts the verdict")
        if verdict == "Won't fix" and not str(result.get("disposition_reason") or "").strip():
            errors.append(f"{marker_id}: Won't fix requires disposition_reason")
    if strict and known_policy and policy_version == 2 and not allow_manual_verdict_override:
        component = result.get("component_defect_proven")
        product = result.get("product_defect_reachable")
        matrix = {
            "False Positive": (False, False),
            "Won't fix": (True, False),
            "Confirmed": (True, True),
        }
        if verdict == "Unclear":
            if (component is not None and type(component) is not bool) or (product is not None and type(product) is not bool):
                errors.append(f"{marker_id}: reachability axes must be boolean or null")
            elif component is not None and product is not None:
                errors.append(f"{marker_id}: Unclear requires at least one unproven axis set to null")
        elif type(component) is not bool or type(product) is not bool:
            for field in ("component_defect_proven", "product_defect_reachable"):
                if type(result.get(field)) is not bool:
                    errors.append(
                        f"{marker_id}: {field} must be an explicit JSON boolean (true or false); "
                        "repair the output format using the existing evidence, do not infer it from the verdict"
                    )
        elif (component, product) != matrix[verdict]:
            errors.append(f"{marker_id}: verdict contradicts component proof or product reachability")
    if verdict == "Confirmed":
        if result.get("severity") not in VALID_SEVERITIES:
            errors.append(f"{marker_id}: Confirmed requires severity: Critical | Major | Minor (not confidence low/medium/high)")
        if result.get("action") not in VALID_ACTIONS:
            errors.append(f"{marker_id}: Confirmed requires action: Fix required | Fix submitted | Ignore (not a patch description)")
        if strict and not allow_manual_verdict_override and not result.get("reachable_path"):
            errors.append(f"{marker_id}: Confirmed requires a complete reachable_path")
        if strict and not allow_manual_verdict_override and result.get("proof_gaps"):
            errors.append(f"{marker_id}: Confirmed cannot contain proof_gaps")
    elif "severity" in result or "action" in result:
        errors.append(f"{marker_id}: severity/action must be absent for {verdict}")
    if strict and not allow_manual_verdict_override and verdict == "False Positive":
        if not result.get("counterevidence"):
            errors.append(f"{marker_id}: False Positive requires non-reachability counterevidence")
        if result.get("proof_gaps"):
            errors.append(f"{marker_id}: False Positive cannot contain proof_gaps")
    if strict and not allow_manual_verdict_override and verdict == "Won't fix":
        if not scope_exclusion and not result.get("reachable_path"):
            errors.append(f"{marker_id}: Won't fix requires a proven reachable_path")
        if result.get("proof_gaps"):
            errors.append(f"{marker_id}: Won't fix cannot contain proof_gaps")
    if (
        strict
        and not allow_manual_verdict_override
        and verdict == "Unclear"
        and not result.get("proof_gaps")
    ):
        errors.append(f"{marker_id}: Unclear requires concrete proof_gaps")
    return errors


def validate_verification_result(result: dict, current: dict) -> list[str]:
    marker_id = str(result.get("marker_id") or "")
    errors: list[str] = []
    if current.get("verdict") != "Confirmed":
        return [f"{marker_id}: independent verification is allowed only for Confirmed"]
    if verification_status(current) != "pending":
        return [f"{marker_id}: independent verification is already saved or needs manual review"]
    decision = result.get("decision")
    if decision not in VALID_VERIFICATION_DECISIONS:
        errors.append(f"{marker_id}: decision must be verified or challenged")
    verifier_id = result.get("verifier_id")
    if not isinstance(verifier_id, str) or not verifier_id.strip():
        errors.append(f"{marker_id}: verifier_id must be non-empty")
    for name in ("reason",):
        if not isinstance(result.get(name), str) or not result[name].strip():
            errors.append(f"{marker_id}: {name} must be non-empty")
    for name in ("evidence", "rechecked_paths"):
        value = result.get(name)
        if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{marker_id}: {name} must be a non-empty string array")
    if decision == "challenged":
        if result.get("challenge_type") not in VALID_CHALLENGE_TYPES:
            errors.append(f"{marker_id}: invalid challenge_type")
        for name in ("specific_issue", "resolution_needed"):
            if not isinstance(result.get(name), str) or not result[name].strip():
                errors.append(f"{marker_id}: challenged decision requires {name}")
        if result.get("recommended_verdict") not in VALID_CHALLENGE_VERDICTS:
            errors.append(f"{marker_id}: challenged decision requires a non-Confirmed recommended_verdict")
    return errors


def apply_worker_result_rows(
    decisions: list[dict], results: list[dict], allowed_ids: list[str], path: Path,
    triage_ids: set[str],
) -> dict:
    requested = set(allowed_ids)
    if not requested or len(requested) != len(allowed_ids):
        raise SystemExit("Назначение пустое либо содержит повторяющиеся ID")
    result_ids = [str(item.get("marker_id") or "") for item in results]
    if "" in result_ids or len(result_ids) != len(set(result_ids)):
        raise SystemExit("В результатах подагентов есть пустые или повторяющиеся marker_id")
    if set(result_ids) != requested:
        missing = sorted(requested - set(result_ids))
        extra = sorted(set(result_ids) - requested)
        raise SystemExit(f"Результаты не совпадают с назначением: missing={missing}, extra={extra}")

    by_id = {str(item.get("marker_id")): item for item in decisions}
    unknown = sorted(requested - set(by_id))
    if unknown:
        raise SystemExit(f"Неизвестные marker_id: {unknown}")
    excluded = sorted(requested - triage_ids)
    if excluded:
        raise SystemExit(f"Маркеры уже размечены в Svacer и исключены из доразметки: {excluded}")
    errors: list[str] = []
    for result in results:
        marker_id = str(result["marker_id"])
        current = by_id[marker_id]
        if current.get("verdict") in VALID_VERDICTS:
            errors.append(f"{marker_id}: решение уже заполнено")
            continue
        errors.extend(validate_worker_result(result, current))
    if errors:
        raise SystemExit("Ошибки результатов:\n- " + "\n- ".join(errors))

    result_by_id: dict[str, dict] = {}
    for item in results:
        stored = dict(item)
        # Only the human editor can create this flag. A worker must not bypass
        # evidence validation by copying metadata from a previous decision.
        stored.pop("manual_verdict_override", None)
        lines = str(stored.get("comment") or "").splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].strip().upper() in set(HEADINGS.values()) | {"WON'T FIX"}:
            lines.pop(0)
        stored["comment"] = svacer_comment_text("\n".join(lines))
        # Verification is controlled by the queue, never by the primary analyst.
        stored["verification"] = empty_verification(stored.get("verdict"))
        result_by_id[str(stored["marker_id"])] = stored
    merged = [result_by_id.get(str(item["marker_id"]), item) for item in decisions]
    atomic_write_jsonl(path, merged)
    return {"applied": sorted(result_by_id), "count": len(result_by_id)}


def apply_worker_results(
    decisions: list[dict], result_paths: list[Path], allowed_ids: list[str], path: Path,
    triage_ids: set[str],
) -> dict:
    return apply_worker_result_rows(
        decisions, load_worker_results(result_paths), allowed_ids, path, triage_ids,
    )


def approve_saved_draft(inventory_path: Path, decisions_path: Path, marker_id: str) -> dict:
    """Promote one saved worker draft to a local decision without rerunning analysis."""
    run_path = decisions_path.parent / "codex-run.json"
    if run_path.exists():
        try:
            run = json.loads(run_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Некорректный codex-run.json: {exc}") from exc
        if isinstance(run, dict) and run.get("active"):
            raise SystemExit("Сначала остановите текущий анализ")

    inventory = load_inventory(inventory_path)
    notes = decisions_path.parent / "notes"
    selected: dict | None = None
    selected_path: Path | None = None
    if notes.is_dir():
        for note_path in sorted(notes.iterdir(), key=lambda item: item.name):
            if not WORKER_NOTE_RE.match(note_path.name):
                continue
            for item in load_worker_results([note_path]):
                if str(item.get("marker_id") or "") == marker_id:
                    selected = item
                    selected_path = note_path
    if selected is None or selected_path is None:
        raise SystemExit(f"Черновик для {marker_id} не найден")

    # Older saved drafts may omit empty array fields that are mandatory in the
    # current on-disk schema. Confirming a draft is a user decision, so normalize
    # only these structural omissions without changing its verdict or evidence.
    selected = dict(selected)
    for name in ("reachable_path", "evidence", "counterevidence", "proof_gaps"):
        selected.setdefault(name, [])

    with decision_lock(decisions_path):
        decisions = load_decisions(decisions_path)
        state(inventory, decisions)
        triage_ids = {str(marker["id"]) for marker in markers_for_triage(inventory)}
        result = apply_worker_result_rows(
            decisions, [selected], [marker_id], decisions_path, triage_ids,
        )
    result["draft_source"] = selected_path.name
    return result


def edit_saved_decision(
    inventory_path: Path,
    decisions_path: Path,
    marker_id: str,
    comment: str,
    *,
    verdict: str | None = None,
    severity: str | None = None,
    action: str | None = None,
) -> dict:
    """Edit user-facing fields of one completed local decision.

    Evidence stays untouched. Prepared import artifacts are invalidated so a
    later Svacer submission is always rebuilt from the edited decision.
    """
    if (decisions_path.parent / "svacer-import-attempt.json").exists():
        raise SystemExit(
            "Изменение заблокировано: для этой задачи уже была попытка отправки в Svacer"
        )

    normalized_comment = svacer_comment_text(str(comment or ""))
    if not normalized_comment:
        raise SystemExit("Комментарий для Svacer не может быть пустым")

    inventory = load_inventory(inventory_path)
    with decision_lock(decisions_path):
        # An import may have started while this editor waited for the lock.
        if (decisions_path.parent / "svacer-import-attempt.json").exists():
            raise SystemExit("Изменение заблокировано: началась отправка в Svacer")
        decisions = load_decisions(decisions_path)
        state(inventory, decisions)
        by_id = {str(item.get("marker_id") or ""): item for item in decisions}
        current = by_id.get(marker_id)
        if current is None:
            raise SystemExit(f"Неизвестный marker_id: {marker_id}")
        current_verdict = current.get("verdict")
        if current_verdict not in VALID_VERDICTS:
            raise SystemExit("Сначала дождитесь готового решения или подтвердите черновик")
        selected_verdict = verdict if verdict is not None else current_verdict
        if selected_verdict not in VALID_VERDICTS:
            raise SystemExit("Выберите допустимый вердикт")

        updated = dict(current)
        updated["verdict"] = selected_verdict
        updated["comment"] = normalized_comment
        if selected_verdict == "Confirmed":
            if severity not in VALID_SEVERITIES:
                raise SystemExit("Для Confirmed выберите severity: Critical, Major или Minor")
            if action not in VALID_ACTIONS:
                raise SystemExit(
                    "Для Confirmed выберите действие: Fix required, Fix submitted или Ignore"
                )
            updated["severity"] = severity
            updated["action"] = action
        else:
            updated.pop("severity", None)
            updated.pop("action", None)

        if selected_verdict != current_verdict:
            updated["manual_verdict_override"] = {
                "previous_verdict": current_verdict,
                "verdict": selected_verdict,
                "updated_at": utc_now(),
            }
            # A verification of an earlier verdict must never be reused after
            # the user changes the verdict manually.
            updated["verification"] = empty_verification(selected_verdict)

        errors = validate_worker_result(
            updated,
            current,
            allow_manual_verdict_override=isinstance(updated.get("manual_verdict_override"), dict)
            and updated["manual_verdict_override"].get("verdict") == selected_verdict,
        )
        if errors:
            raise SystemExit("Решение не прошло проверку:\n- " + "\n- ".join(errors))

        # These files are only a local, reproducible preview.  Keeping an old
        # preview after a manual edit could send stale values to Svacer.
        invalidated: list[str] = []
        for name in ("svacer-import.jsonl", "svacer-import-preview.json"):
            path = decisions_path.parent / name
            if path.exists():
                path.unlink()
                invalidated.append(name)

        merged = [updated if str(item.get("marker_id") or "") == marker_id else item for item in decisions]
        atomic_write_jsonl(decisions_path, merged)

    return {
        "marker_id": marker_id,
        "verdict": selected_verdict,
        "severity": updated.get("severity"),
        "action": updated.get("action"),
        "invalidated_import_files": invalidated,
    }


def apply_verification_results(
    decisions: list[dict], result_paths: list[Path], allowed_ids: list[str], path: Path,
    triage_ids: set[str],
) -> dict:
    results = load_worker_results(result_paths)
    requested = set(allowed_ids)
    if not requested or len(requested) != len(allowed_ids):
        raise SystemExit("Назначение проверки пустое либо содержит повторяющиеся ID")
    result_ids = [str(item.get("marker_id") or "") for item in results]
    if "" in result_ids or len(result_ids) != len(set(result_ids)):
        raise SystemExit("В результатах проверки есть пустые или повторяющиеся marker_id")
    if set(result_ids) != requested:
        missing = sorted(requested - set(result_ids))
        extra = sorted(set(result_ids) - requested)
        raise SystemExit(f"Результаты проверки не совпадают с назначением: missing={missing}, extra={extra}")
    by_id = {str(item.get("marker_id")): item for item in decisions}
    unknown = sorted(requested - set(by_id))
    if unknown:
        raise SystemExit(f"Неизвестные marker_id: {unknown}")
    excluded = sorted(requested - triage_ids)
    if excluded:
        raise SystemExit(
            f"Маркеры уже размечены в Svacer и исключены из независимой проверки: {excluded}"
        )
    errors: list[str] = []
    for result in results:
        errors.extend(validate_verification_result(result, by_id[str(result["marker_id"])]))
    if errors:
        raise SystemExit("Ошибки независимой проверки:\n- " + "\n- ".join(errors))

    result_by_id = {str(item["marker_id"]): item for item in results}
    for marker_id, result in result_by_id.items():
        decision = by_id[marker_id]
        status = str(result["decision"])
        verification = {
            "status": status,
            "verifier_id": result["verifier_id"].strip(),
            "reason": result["reason"].strip(),
            "evidence": result["evidence"],
            "rechecked_paths": result["rechecked_paths"],
            "verified_at": utc_now(),
        }
        if status == "challenged":
            verification.update({
                "challenge_type": result["challenge_type"],
                "specific_issue": result["specific_issue"].strip(),
                "resolution_needed": result["resolution_needed"].strip(),
                "recommended_verdict": result["recommended_verdict"],
            })
        decision["verification"] = verification
    atomic_write_jsonl(path, decisions)
    return {
        "applied": sorted(result_by_id),
        "count": len(result_by_id),
        "verified": sorted(
            marker_id for marker_id, result in result_by_id.items()
            if result["decision"] == "verified"
        ),
        "challenged": sorted(
            marker_id for marker_id, result in result_by_id.items()
            if result["decision"] == "challenged"
        ),
    }


def reopen(decisions: list[dict], ids: list[str], path: Path, triage_ids: set[str]) -> dict:
    if (path.parent / "svacer-import-attempt.json").exists():
        raise SystemExit("Повторный анализ заблокирован после попытки отправки в Svacer")
    requested = set(ids)
    known = {str(item.get("marker_id")) for item in decisions}
    unknown = sorted(requested - known)
    if unknown:
        raise SystemExit(f"Неизвестные marker_id: {unknown}")
    excluded = sorted(requested - triage_ids)
    if excluded:
        raise SystemExit(f"Маркеры уже размечены в Svacer и не входят в локальную очередь: {excluded}")
    for item in decisions:
        if str(item.get("marker_id")) not in requested:
            continue
        item["verdict"] = None
        item["confidence"] = None
        if item.get("schema_version") == STRICT_SCHEMA_VERSION:
            item["entrypoint"] = ""
        item["source"] = ""
        item["control"] = ""
        item["sink"] = ""
        if item.get("schema_version") == STRICT_SCHEMA_VERSION:
            item["build_reachability"] = ""
            item["product_reachability"] = ""
            item["impact"] = ""
        item["reachable_path"] = []
        item["evidence"] = []
        item["counterevidence"] = []
        item["proof_gaps"] = []
        item["comment"] = ""
        item["verification"] = empty_verification(None)
        item.pop("severity", None)
        item.pop("action", None)
        item.pop("manual_verdict_override", None)
        item.pop("decision_policy_version", None)
        item.pop("defect_scope", None)
        item.pop("disposition_reason", None)
        item.pop("disposition_kind", None)
        item.pop("scope_exclusion", None)
        item.pop("component_defect_proven", None)
        item.pop("product_defect_reachable", None)
        item.pop("review_contract_version", None)
        item.pop("source_revision", None)
        item.pop("source_evidence", None)
    for name in ("svacer-import.jsonl", "svacer-import-preview.json"):
        preview = path.parent / name
        if preview.exists():
            preview.unlink()
    atomic_write_jsonl(path, decisions)
    return {"reopened": sorted(requested), "count": len(requested)}


def claim_next_batch(inventory_path: Path, decisions_path: Path, limit: int, workers: int) -> dict:
    """Reserve work under the same lock as result application; repeated calls are idempotent."""
    if not 1 <= limit <= 50 or not 1 <= workers <= 8:
        raise SystemExit("Некорректный размер очереди или число агентов")
    with decision_lock(decisions_path):
        inventory = load_inventory(inventory_path)
        decisions = load_decisions(decisions_path)
        drafts = saved_draft_ids(decisions_path.parent, decisions)
        if primary_queue_blocked(decisions_path):
            return {"progress": progress_payload(inventory, decisions), "paused": True, "batch": None}
        preferred = runnable_priority_marker_ids(decisions_path)
        control_path = decisions_path.parent / "control.json"
        control = json.loads(control_path.read_text(encoding="utf-8-sig")) if control_path.exists() else {}
        recheck_ids = set(control.get("recheck_marker_ids") or [])
        by_id, _ = state(inventory, decisions)
        # A recovered/applied result may outlive a stale GUI selection. It is
        # not an implicit recheck: preserve the verdict, remove only that stale
        # queue entry, and continue with the still-unfinished FIFO entries.
        stale_completed = [mid for mid in preferred if mid in by_id
                           and by_id[mid].get("verdict") in VALID_VERDICTS
                           and mid not in recheck_ids]
        if stale_completed:
            stale_set = set(stale_completed)
            preferred = [mid for mid in preferred if mid not in stale_set]
            retained = [mid for mid in priority_marker_ids(decisions_path) if mid not in stale_set]
            if retained:
                control["priority_marker_ids"] = retained
            else:
                control.pop("priority_marker_ids", None)
                control.pop("manual_queue_requested", None)
            control["deferred_marker_ids"] = [
                mid for mid in control.get("deferred_marker_ids", []) if mid in retained
            ]
            control["updated_at"] = utc_now()
            atomic_write_json(control_path, control)
        selected_priority = preferred[:min(limit, workers)]
        selected_rechecks = [mid for mid in selected_priority if mid in recheck_ids
                             and mid in by_id and by_id[mid].get("verdict") in VALID_VERDICTS]
        if selected_rechecks:
            reopen(decisions, selected_rechecks, decisions_path,
                   {str(marker["id"]) for marker in markers_for_triage(inventory)})
            decisions = load_decisions(decisions_path)
        # A fresh explicit single-marker retry may select a draft. Old priority
        # lists must not silently put drafts back into the automatic queue.
        explicit_retry = (set(preferred) if control.get("single_marker_requested") is True
                          or control.get("manual_queue_requested") is True else set())
        eligible_priority = [mid for mid in preferred if mid not in drafts or mid in explicit_retry]
        if eligible_priority != preferred:
            retained = [mid for mid in priority_marker_ids(decisions_path)
                        if mid not in preferred or mid in eligible_priority]
            if retained:
                control["priority_marker_ids"] = retained
            else:
                control.pop("priority_marker_ids", None)
            atomic_write_json(control_path, control)
        preferred = eligible_priority
        if job_run_mode(decisions_path) == "single_batch" and not preferred:
            limit = min(limit, int(control.get("run_remaining", limit)))
            if limit <= 0:
                return {"progress": progress_payload(inventory, decisions), "paused": True, "batch": None}
        status_path = decisions_path.parent / "workers.status.json"
        status = json.loads(status_path.read_text(encoding="utf-8-sig")) if status_path.exists() else {}
        assigned = [str(mid) for worker in status.get("workers", []) for mid in worker.get("marker_ids", [])]
        by_id, _ = state(inventory, decisions)
        if status.get("state") == "assigned" and assigned and all(mid in by_id and not by_id[mid].get("verdict") for mid in assigned):
            resumed = [mid for mid in assigned if mid not in drafts or mid in explicit_retry]
            if len(resumed) != len(assigned):
                # Saved drafts already exist: do not recreate their old reservation.
                assigned = []
            if assigned:
                if len(assigned) > min(limit, workers):
                    raise SystemExit("Старое назначение превышает новые настройки. Сначала сбросьте очередь.")
                result = next_parallel_batch(inventory, decisions, limit, workers, assigned,
                                             excluded_ids=drafts - explicit_retry)
                result["one_shot"] = bool(status.get("one_shot"))
                result["batch_number"] = status.get("batch")
                return result
        if manual_selection_only(decisions_path) and not preferred:
            return {"progress": progress_payload(inventory, decisions), "paused": True, "batch": None}
        # Later rechecks keep their old verdict until their turn. Validate only
        # the IDs being assigned now; the rest stay in the saved FIFO.
        result = next_parallel_batch(inventory, decisions, limit, workers,
                                     preferred[:min(limit, workers)] if preferred else None,
                                     excluded_ids=drafts - explicit_retry)
        result["one_shot"] = bool(preferred)
        record_assignments(decisions_path, result.get("batch"), one_shot=bool(preferred))
        status = json.loads(status_path.read_text(encoding="utf-8-sig"))
        result["batch_number"] = status.get("batch")
        return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Очередь локальной разметки Svacer")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--decisions", required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)
    next_parser = subparsers.add_parser("next")
    next_parser.add_argument("--limit", type=int, default=15)
    next_parser.add_argument("--workers", type=int, default=1)
    verify_next_parser = subparsers.add_parser("verify-next")
    verify_next_parser.add_argument("--limit", type=int, default=5)
    verify_next_parser.add_argument("--workers", type=int, default=1)
    subparsers.add_parser("progress")
    reopen_parser = subparsers.add_parser("reopen")
    reopen_parser.add_argument("--ids", nargs="+", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--results", nargs="+", required=True)
    apply_parser.add_argument("--allowed-ids", nargs="+", required=True)
    verify_apply_parser = subparsers.add_parser("verify-apply")
    verify_apply_parser.add_argument("--results", nargs="+", required=True)
    verify_apply_parser.add_argument("--allowed-ids", nargs="+", required=True)
    approve_draft_parser = subparsers.add_parser("approve-draft")
    approve_draft_parser.add_argument("--id", required=True)
    subparsers.add_parser("reset-queue")
    args = parser.parse_args()

    inventory_path = Path(args.inventory).expanduser().resolve()
    decisions_path = Path(args.decisions).expanduser().resolve()
    inventory = load_inventory(inventory_path)
    decisions = load_decisions(decisions_path)

    if args.action == "approve-draft":
        result = approve_saved_draft(inventory_path, decisions_path, str(args.id))
    elif args.action == "reset-queue":
        result = reset_queue_assignments(decisions_path)
    elif args.action == "next":
        result = claim_next_batch(inventory_path, decisions_path, args.limit, args.workers)
    elif args.action == "verify-next":
        if args.limit < 1 or args.limit > 50:
            raise SystemExit("--limit должен быть от 1 до 50")
        if args.workers < 1 or args.workers > 8:
            raise SystemExit("--workers должен быть от 1 до 8")
        if pause_requested(decisions_path):
            result = {"progress": progress_payload(inventory, decisions), "paused": True, "batch": None}
            record_verifier_assignments(decisions_path, {}, paused=True)
        else:
            batch = next_verification_batch(inventory, decisions, args.limit, args.workers)
            result = {"progress": progress_payload(inventory, decisions), "batch": batch}
            record_verifier_assignments(decisions_path, batch)
    elif args.action == "progress":
        result = progress_payload(inventory, decisions)
    else:
        with decision_lock(decisions_path):
            # Reload only after obtaining the lock: another process may have saved
            # results since the initial read. Never overwrite with a stale copy.
            decisions = load_decisions(decisions_path)
            state(inventory, decisions)
            triage_ids = {str(marker["id"]) for marker in markers_for_triage(inventory)}
            if args.action in {"apply", "verify-apply"}:
                result_paths = [Path(name).expanduser().resolve() for name in args.results]
                if args.action == "apply":
                    result = apply_worker_results(
                        decisions,
                        result_paths,
                        args.allowed_ids,
                        decisions_path,
                        triage_ids,
                    )
                    record_saved_workers(decisions_path, result_paths)
                    result["paused_after_batch"] = record_batch_completion(decisions_path)
                else:
                    result = apply_verification_results(
                        decisions,
                        result_paths,
                        args.allowed_ids,
                        decisions_path,
                        triage_ids,
                    )
                    record_saved_verifiers(decisions_path, result_paths)
            else:
                result = reopen(decisions, args.ids, decisions_path, triage_ids)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
