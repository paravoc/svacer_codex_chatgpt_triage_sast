"""Two-phase, fail-closed markup import using the documented Public API.

Remote writes occur only in apply(), after an exclusive durable attempt record.
An uncertain network outcome must be investigated, never automatically retried.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import triage_queue as queue

from .client import ConnectorError
from .service import SvacerService, identifier


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_path(parent: Path, name: str) -> Path:
    path = parent / name
    # Also reject Windows junctions, including a link inserted after startup.
    for node in (path, *path.parents):
        try:
            attributes = getattr(node.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if node.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ConnectorError("Импорт не работает через символические ссылки или junction.")
    return path


def atomic_write(path: Path, data: bytes) -> None:
    safe_path(path.parent, path.name)
    fd, tmp = tempfile.mkstemp(prefix=".connector-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def review_fields(value: dict | None) -> dict:
    value = value or {}
    if not isinstance(value, dict):
        raise ConnectorError("Некорректный экспорт review_data.")
    return {"status": value.get("status") or "Undecided",
            "severity": value.get("severity") or "Unspecified",
            "action": value.get("action") or "Undecided"}


class MarkupImport:
    def __init__(self, service: SvacerService, root: Path, actor: str):
        self.service = service
        self.root = root.absolute()
        self.actor = actor
        self.lock = asyncio.Lock()

    def job_path(self, value: str) -> Path:
        path = Path(value).absolute()
        safe_path(path, "job.json")
        try:
            relative = path.resolve().relative_to((self.root / "RESULTS").resolve())
        except ValueError:
            raise ConnectorError("Импорт разрешён только из RESULTS текущего приложения.") from None
        if not relative.parts or not path.is_dir():
            raise ConnectorError("Нужен существующий каталог задачи в RESULTS.")
        return path

    def inputs(self, job: Path) -> tuple[dict, list[dict], list[dict], dict]:
        run_file = safe_path(job, "codex-run.json")
        if run_file.exists() and json.loads(run_file.read_text(encoding="utf-8-sig")).get("active"):
            raise ConnectorError("Дождитесь остановки анализа перед подготовкой импорта.")
        hashes = {name: digest(safe_path(job, name).read_bytes()) for name in
                  ("job.json", "markers.inventory.json", "decisions.jsonl")}
        try:
            config = json.loads((job / "job.json").read_text(encoding="utf-8-sig"))
            inventory = queue.load_inventory(job / "markers.inventory.json")
            decisions = queue.load_decisions(job / "decisions.jsonl")
            queue.state(inventory, decisions)
        except (SystemExit, ValueError, TypeError, KeyError):
            raise ConnectorError("Некорректные или неполные локальные данные задачи.") from None
        url = str(config.get("snapshot_url") or "")
        if not url.startswith(self.service.api.url + "/"):
            raise ConnectorError("Задача принадлежит другому серверу Svacer.")
        for key in ("project_id", "branch_id", "snapshot_id"):
            config[key] = identifier(config.get(key))
        original = {m["id"]: m for m in inventory}
        if not decisions:
            raise ConnectorError("Нет решений для импорта.")
        for decision in decisions:
            current = dict(original[decision["marker_id"]])
            if decision.get("schema_version") is not None:
                current["schema_version"] = decision["schema_version"]
            override = decision.get("manual_verdict_override")
            errors = queue.validate_worker_result(decision, current,
                allow_manual_verdict_override=isinstance(override, dict)
                and override.get("verdict") == decision.get("verdict"))
            if errors:
                raise ConnectorError("Есть незавершённые или некорректные решения; импорт остановлен.")
            if decision["verdict"] == "Confirmed" and queue.verification_status(decision) != "verified":
                raise ConnectorError("Confirmed должен пройти независимую проверку.")
        return config, inventory, decisions, hashes

    async def export(self, config: dict) -> dict[str, dict]:
        rows = await self.service.api.json_lines("/api/public/markup/export", read_only=True, json={
            "source_id": config["branch_id"], "format": "json", "export_all": True,
            "compressed": False, "skip_comments": False, "skip_review": False,
            "filters": [{"ids": [config["snapshot_id"]]}],
        })
        result = {}
        for row in rows:
            meta = row.get("meta") or {}
            for field, key in (("project", "project_id"), ("branch", "branch_id")):
                if meta.get(field, {}).get("id") not in (None, config[key]):
                    raise ConnectorError("Экспорт разметки принадлежит другому проекту/ветке.")
            invariant = row.get("invariant")
            if not isinstance(invariant, str) or not invariant or invariant in result:
                raise ConnectorError("Экспорт содержит пустые или неоднозначные инварианты.")
            if not isinstance(row.get("locations"), list) or not row["locations"]:
                raise ConnectorError("Экспорт не содержит точные locations для импорта.")
            result[invariant] = row
        return result

    async def build(self, job: Path, nonce: str, timestamp: str) -> tuple[dict, bytes]:
        config, inventory, decisions, hashes = self.inputs(job)
        rows, _ = await self.service.marker_rows(config["project_id"], config["branch_id"],
            config["snapshot_id"], advanced_filter=queue.GOST_FILTER)
        remote = {row["id"]: row for row in rows}
        if set(remote) != {m["id"] for m in inventory}:
            raise ConnectorError("Состав ГОСТ-маркеров изменился; обновите инвентарь.")
        for marker in inventory:
            if any(remote[marker["id"]].get(k) != marker.get(k) for k in ("warnClass", "file", "line")):
                raise ConnectorError("Местоположение маркера изменилось; обновите инвентарь.")
        exported = await self.export(config)
        grouped = {}
        for decision in decisions:
            marker = remote[decision["marker_id"]]
            invariant = marker.get("invariant")
            if not invariant or invariant not in exported:
                raise ConnectorError("Сервер не подтвердил инвариант маркера в экспорте.")
            source = exported[invariant]
            if not any(all(loc.get(k) == marker.get(k) for k in ("warnClass", "file", "line"))
                       for loc in source["locations"] if isinstance(loc, dict)):
                raise ConnectorError("Инвариант не связан с точной локацией маркера.")
            fields = {"status": decision["verdict"], "severity": decision.get("severity", "Unspecified"),
                      "action": decision.get("action", "Undecided")}
            item = grouped.setdefault(invariant, {"fields": fields, "comments": [], "marker_ids": []})
            if item["fields"] != fields:
                raise ConnectorError("Одинаковому инварианту назначены разные решения. Импорт запрещён.")
            if decision["comment"] not in item["comments"]:
                item["comments"].append(decision["comment"])
            item["marker_ids"].append(decision["marker_id"])
        payload = []
        conflicts = []
        prior = {}
        for invariant, item in sorted(grouped.items()):
            source = exported[invariant]
            # Include existing comments and timestamps in the preflight fingerprint.
            prior[invariant] = {k: source.get(k) for k in ("review_data", "comments", "locations")}
            old = review_fields(source.get("review_data"))
            if old != {"status": "Undecided", "severity": "Unspecified", "action": "Undecided"} and old != item["fields"]:
                conflicts.append({"invariant": invariant, "before": old, "after": item["fields"]})
            origin = str(uuid5(UUID(nonce), invariant))
            payload.append({"invariant": invariant, "locations": source["locations"],
                "review_data": {**item["fields"], "origin_id": origin, "create_ts": timestamp,
                                "created_by": self.actor},
                "comments": [{"text": text, "create_ts": timestamp, "createdBy": self.actor,
                    "origin_id": str(uuid5(UUID(origin), text))} for text in item["comments"]]})
        data = b"".join(canonical(row) + b"\n" for row in payload)
        sha = digest(data)
        phrase = f"IMPORT {job.name} {len(decisions)} {sha[:16]}"
        preview = {"schema_version": 1, "connector": "triage_connector", "nonce": nonce,
            "created_at": timestamp, "server": self.service.api.url,
            "project_id": config["project_id"], "branch_id": config["branch_id"],
            "snapshot_id": config["snapshot_id"], "marker_count": len(decisions),
            "invariant_count": len(payload), "marker_ids": sorted(d["marker_id"] for d in decisions),
            "by_verdict": dict(Counter(d["verdict"] for d in decisions)),
            "payload_sha256": sha, "input_sha256": hashes, "remote_sha256": digest(canonical(prior)),
            "conflicts": conflicts, "conflict_count": len(conflicts), "requires_force": bool(conflicts),
            "confirmation": phrase, "force_confirmation": "FORCE " + phrase,
            "scope": "Разметка Svacer общая для инварианта во всей ветке, включая другие снимки."}
        return preview, data

    def check_no_attempt(self, job: Path) -> None:
        if safe_path(job, "svacer-import-attempt.json").exists():
            raise ConnectorError("Попытка импорта уже зарегистрирована. Сначала проверьте её результат в Svacer.")

    async def prepare(self, job_directory: str) -> dict:
        async with self.lock:
            job = self.job_path(job_directory)
            self.check_no_attempt(job)
            preview, data = await self.build(job, str(uuid4()), datetime.now(timezone.utc).isoformat())
            safe_path(job, "decisions.jsonl.lock")
            with queue.decision_lock(job / "decisions.jsonl"):
                if self.inputs(job)[3] != preview["input_sha256"]:
                    raise ConnectorError("Решения изменились во время подготовки; повторите подготовку.")
                self.check_no_attempt(job)
                atomic_write(safe_path(job, "svacer-import.jsonl"), data)
                atomic_write(safe_path(job, "svacer-import-preview.json"), canonical(preview))
            return preview

    async def apply(self, job_directory: str, confirmation: str, overwrite: str = "none") -> dict:
        if overwrite not in {"none", "force"}:
            raise ConnectorError("Разрешены только явно выбранные overwrite=none или force.")
        async with self.lock:
            job = self.job_path(job_directory)
            self.check_no_attempt(job)
            stored = json.loads(safe_path(job, "svacer-import-preview.json").read_text(encoding="utf-8"))
            preview, data = await self.build(job, stored["nonce"], stored["created_at"])
            if stored != preview or safe_path(job, "svacer-import.jsonl").read_bytes() != data:
                raise ConnectorError("Preview, решения или серверная разметка изменились. Подготовьте импорт заново.")
            expected = preview["force_confirmation" if overwrite == "force" else "confirmation"]
            if confirmation != expected or (preview["requires_force"] and overwrite != "force"):
                raise ConnectorError("Нет точного подтверждения выбранного режима импорта.")
            attempt = {"status": "started", "payload_sha256": preview["payload_sha256"],
                       "created_at": datetime.now(timezone.utc).isoformat(), "overwrite": overwrite}
            try:
                safe_path(job, "decisions.jsonl.lock")
                with queue.decision_lock(job / "decisions.jsonl"):
                    if self.inputs(job)[3] != preview["input_sha256"]:
                        raise ConnectorError("Решения изменились; импорт остановлен.")
                    with safe_path(job, "svacer-import-attempt.json").open("x", encoding="utf-8") as stream:
                        json.dump(attempt, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
            except FileExistsError:
                raise ConnectorError("Другая попытка импорта уже начата.") from None
            # Nothing below this line may automatically resend the import.
            result = {"status": "completed_unverified", "verification": {"verified": False}}
            try:
                response = await self.service.api.json_lines("/api/public/markup/import", params={
                    "target_id": preview["branch_id"], "format": "json", "overwrite": overwrite,
                    "skip_comments": "false", "skip_review": "false", "compressed": "true",
                    "response_with_result": "true"}, files={"file": (
                        "triage.jsonl.gz", gzip.compress(data, mtime=0), "application/gzip")})
                if not response or type(response[0].get("total")) is not int:
                    raise ConnectorError("Svacer не подтвердил сводку импорта.")
                result["summary"] = response[0]
                exported = await self.export(preview)
                mismatches = []
                for row in (json.loads(line) for line in data.splitlines()):
                    actual = exported.get(row["invariant"], {})
                    actual_comments = {c.get("text") for c in actual.get("comments", [])}
                    if (review_fields(actual.get("review_data")) != review_fields(row["review_data"])
                            or any(c["text"] not in actual_comments for c in row["comments"])):
                        mismatches.append(row["invariant"])
                verified = not mismatches and response[0]["total"] == preview["invariant_count"]
                result["verification"] = {"verified": verified, "mismatched_invariants": mismatches}
                result["status"] = "completed_verified" if verified else "completed_unverified"
            except (ConnectorError, ValueError, TypeError, KeyError):
                result["message"] = "Ответ или итог импорта не подтверждён. Проверьте журнал Svacer; повторная отправка заблокирована."
            finally:
                atomic_write(safe_path(job, "svacer-import-result.json"), canonical(result))
                atomic_write(safe_path(job, "svacer-import-attempt.json"), canonical({
                    **attempt, "status": result["status"],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "verification": result["verification"]}))
            return result
