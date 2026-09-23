"""Read-only selection of validated, not-yet-published local decisions."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path

import triage_queue as queue
from comment_format import svacer_comment_text

BATCHES = "svacer-import-batches"


def fingerprint(row: dict) -> str:
    value = {key: row.get(key) for key in ("marker_id", "warnClass", "file", "line", "verdict")}
    value.update(comment=svacer_comment_text(str(row.get("comment") or "")),
                 severity=row.get("severity", "Unspecified"), action=row.get("action", "Undecided"))
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def scope(config: dict) -> dict:
    return {key: config.get(key) for key in ("snapshot_url", "project_id", "branch_id", "snapshot_id")}


def safe_local(path: Path) -> Path:
    for node in (path, *path.parents):
        if node.is_symlink() or (node.exists() and getattr(node.stat(), "st_file_attributes", 0)
                                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("Журнал отправки не должен находиться за ссылкой.")
    return path


def receipts(job: Path, config: dict) -> list[dict]:
    root = safe_local(job / BATCHES)
    result = []
    if root.is_dir():
        for path in root.glob("*/receipt.json"):
            row = json.loads(safe_local(path).read_text(encoding="utf-8"))
            if not isinstance(row, dict):
                raise ValueError("Некорректная квитанция отправки.")
            hashes = row.get("fingerprints")
            if (row.get("schema_version") != 1 or row.get("verified") is not True
                    or row.get("nonce") != path.parent.name or row.get("scope") != scope(config)
                    or not isinstance(row.get("finished_at"), str)
                    or not isinstance(row.get("payload_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", row["payload_sha256"])
                    or not isinstance(hashes, dict) or not hashes
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in hashes.items())):
                raise ValueError("Некорректная квитанция отправки; повторная отправка заблокирована.")
            result.append(row)
    return result


def blocking_attempt(job: Path, config: dict) -> bool:
    path = safe_local(job / "svacer-import-attempt.json")
    if not path.exists():
        return False
    attempt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(attempt, dict):
        raise ValueError("Некорректный журнал попытки отправки.")
    # A crash after saving a verified receipt but before clearing the temporary
    # job lock is recoverable without another network write. Legacy attempts and
    # uncertain outcomes remain blocked for manual reconciliation.
    return not (attempt.get("status") == "completed_verified" and any(
        row["nonce"] == attempt.get("nonce") and row.get("payload_sha256") == attempt.get("payload_sha256")
        for row in receipts(job, config)))


def select_ready(job: Path, config: dict, inventory: list[dict], decisions: list[dict],
                 marker_ids: list[str] | None = None) -> tuple[list[dict], dict]:
    queue.state(inventory, decisions)  # preserve duplicate/missing/assignment checks
    sent = {mid: value for row in sorted(receipts(job, config), key=lambda item: item["finished_at"])
            for mid, value in row["fingerprints"].items()}
    original = {row["id"]: row for row in inventory}
    valid, skipped = {}, {}
    for row in decisions:
        mid = row["marker_id"]
        if row.get("verdict") not in queue.VALID_VERDICTS or row.get("analysis_status") == "needs_context":
            skipped[mid] = "Не завершён"
            continue
        current = dict(original[mid])
        if row.get("schema_version") is not None:
            current["schema_version"] = row["schema_version"]
        override = row.get("manual_verdict_override")
        errors = queue.validate_worker_result(row, current, allow_manual_verdict_override=
            isinstance(override, dict) and override.get("verdict") == row.get("verdict"))
        if errors:
            skipped[mid] = "Не пройдена проверка формата: " + "; ".join(errors)
        elif row["verdict"] == "Confirmed" and queue.verification_status(row) != "verified":
            skipped[mid] = "Confirmed ожидает независимой проверки"
        else:
            valid[mid] = row

    # Svacer stores a verdict for an invariant, not an individual marker. Never
    # silently apply it to a pending/invalid alias in the same local inventory.
    groups: dict[str, list[str]] = {}
    for marker in inventory:
        groups.setdefault(str(marker.get("invariant") or marker["id"]), []).append(marker["id"])
    for ids in groups.values():
        if any(mid not in valid for mid in ids):
            for mid in ids:
                if mid in valid:
                    skipped[mid] = "Другой маркер того же инварианта не готов"
                    valid.pop(mid)
        elif len({(valid[mid]["verdict"], valid[mid].get("severity"), valid[mid].get("action")) for mid in ids}) > 1:
            raise ValueError("Одинаковому инварианту назначены разные решения. Импорт запрещён.")

    ready = []
    for mid, row in valid.items():
        if sent.get(mid) == fingerprint(row):
            skipped[mid] = "Уже отправлен"
        else:
            ready.append(row)
    if marker_ids is not None:
        if not marker_ids or len(marker_ids) != len(set(marker_ids)):
            raise ValueError("Некорректный состав подготовленной отправки.")
        by_id = {row["marker_id"]: row for row in ready}
        if not set(marker_ids) <= by_id.keys():
            raise ValueError("Подготовленные решения изменились, уже отправлены или больше не готовы.")
        ready = [by_id[mid] for mid in marker_ids]
    return ready, {"total": len(decisions), "ready": len(ready), "skipped": skipped,
                   "pending": sum(reason == "Не завершён" for reason in skipped.values()),
                   "needs_attention": sum(reason not in {"Не завершён", "Уже отправлен"} for reason in skipped.values()),
                   "already_sent": sum(reason == "Уже отправлен" for reason in skipped.values())}
