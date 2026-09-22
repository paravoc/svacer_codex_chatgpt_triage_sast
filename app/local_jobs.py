"""Local job lifecycle. No credentials, model calls, or remote mutations."""
from __future__ import annotations

import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from triage_queue import (atomic_write_json, atomic_write_jsonl, decision_lock,
                          load_decisions, load_inventory, new_pending_decision,
                          state, validate_inventory)


def checked_job(job: Path, root: Path) -> Path:
    root = root.resolve(strict=True)
    # Refuse symlinks/junctions, including redirected RESULTS roots.
    for path in (job, job.parent):
        if path.is_symlink() or (getattr(path.lstat(), "st_file_attributes", 0)
                               & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("Операция с задачей-ссылкой запрещена.")
    target = job.resolve(strict=True)
    if target.parent not in (root / "RESULTS", root / "jobs") or not (target / "job.json").is_file():
        raise ValueError("Можно изменять только отдельную локальную задачу из RESULTS/jobs.")
    return target


@contextmanager
def job_operation_lock(job: Path):
    # Outside the job so Windows can move the entire directory to the recycle bin.
    with decision_lock(job.parent / f".{job.name}.lifecycle"):
        if not (job / "job.json").is_file():
            raise ValueError("Локальная задача уже удалена или перемещена.")
        yield


def require_idle(job: Path) -> None:
    from codex_run import read_run_record
    record = job / "codex-run.json"
    if record.exists():
        value = json.loads(record.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Не удалось проверить состояние анализа.")
    if read_run_record(job).get("active"):
        raise ValueError("Сначала завершите анализ этой задачи.")


def trash_local_job(job: Path, root: Path) -> None:
    target = checked_job(job, root)
    try:
        with job_operation_lock(target):
            require_idle(target)
            if (target / "svacer-import-attempt.json").exists() and not (target / "svacer-import-result.json").exists():
                raise ValueError("Отправка разметки ещё не завершена или не подтверждена. Сначала проверьте её результат.")
            from PySide6.QtCore import QFile
            moved, _trash_path = QFile.moveToTrash(str(target))
            if not moved:
                raise OSError("Не удалось переместить задачу в корзину. Ничего безвозвратно не удалено.")
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc


def update_marker_inventory(job: Path, root: Path, payload: dict) -> dict:
    """Refresh the same snapshot, preserving every existing local result and FIFO."""
    target = checked_job(job, root)
    try:
        markers = validate_inventory(payload)
        with job_operation_lock(target), decision_lock(target / "decisions.jsonl"):
            require_idle(target)
            if (target / "svacer-import-attempt.json").exists():
                raise ValueError("После попытки отправки разметки создайте новую задачу для обновления.")
            inventory_path = target / "markers.inventory.json"
            decisions_path = target / "decisions.jsonl"
            old = load_inventory(inventory_path) if inventory_path.exists() else []
            incoming = {str(m["id"]): m for m in markers}
            for marker in old:
                fresh = incoming.get(str(marker["id"]))
                if fresh is None or any(marker.get(k) != fresh.get(k)
                                        for k in ("invariant", "warnClass", "file", "line")):
                    raise ValueError("Состав или расположение прежних маркеров изменились. "
                                     "Локальные данные сохранены; создайте отдельную задачу для нового набора.")
            decisions = load_decisions(decisions_path) if decisions_path.exists() else []
            if decisions_path.exists():
                state(old, decisions)
            known = {str(d["marker_id"]) for d in decisions}
            decisions.extend(new_pending_decision(m) for m in markers if str(m["id"]) not in known)
            state(markers, decisions)
            # Keep exact old files as a recoverable local backup, including old reviews.
            backup = target / "inventory-backups" / uuid.uuid4().hex
            backup.mkdir(parents=True)
            for path in (inventory_path, decisions_path):
                if path.exists():
                    (backup / path.name).write_bytes(path.read_bytes())
            try:
                atomic_write_jsonl(decisions_path, decisions)
                atomic_write_json(inventory_path, payload)
            except OSError:
                for path in (inventory_path, decisions_path):
                    saved = backup / path.name
                    if saved.exists():
                        # Copy first: retain the backup even if replacement fails.
                        restored = path.with_name(path.name + ".restore")
                        restored.write_bytes(saved.read_bytes())
                        os.replace(restored, path)
                    elif path.exists():
                        # Only a file created by this failed initial transaction.
                        path.unlink()
                raise
            return {"inventory_total": len(markers), "triage_total": len(decisions),
                    "added": len(set(incoming) - {str(m["id"]) for m in old})}
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
