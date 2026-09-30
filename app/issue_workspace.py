"""Persistent issue queue with opt-in English wording; analysis stays read-only."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from string import Template
from typing import Any

from developer_issues import canonical, decisions_by_id, digest, read_json
from triage_queue import decision_lock


DEFAULT_TEMPLATE = """## Description

Component: $component, commit `$short_revision`.
Location: `$location`.

$description

Reachability: $reachability

Source: $source

Sink: $sink

## Impact

$impact
"""


def _regular(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Путь для issue не может быть ссылкой или junction.")


def _write(path: Path, text: str) -> None:
    _regular(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex[:12]}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _text(value: Any, fallback: str = "Not documented; requires investigation.") -> str:
    if isinstance(value, str):
        return value.strip() or fallback
    if isinstance(value, list):
        return "\n".join(f"- {_text(item)}" for item in value) or fallback
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2) or fallback
    return str(value) if value is not None else fallback


def fence(content: str, language: str = "text") -> str:
    ticks = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", content)), default=3))
    return f"{ticks}{language}\n{content.rstrip()}\n{ticks}"


def report_fields(job: dict, row: dict) -> dict[str, str]:
    component = str(job.get("repository_url") or "component").rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    evidence = []
    for entry in row.get("source_evidence") or []:
        if not isinstance(entry, dict):
            continue
        evidence.append(f"`{entry.get('file_path', '?')}:{entry.get('line_start', '?')}-{entry.get('line_end', '?')}`\n\n"
                        + fence(str(entry.get("excerpt") or "")) + "\n\n" + _text(entry.get("supports")))
    return {
        "component": component, "version": _text(job.get("git_ref")),
        "revision": _text(job.get("git_commit")),
        "short_revision": str(job.get("git_commit") or "unknown")[:8],
        "location": f"{row.get('file', '?')}:{row.get('line', '?')}",
        "detector": _text(row.get("warnClass")), "description": _text(row.get("comment")),
        "verification": _text((row.get("verification") or {}).get("status"), "not completed"),
        "source": _text(row.get("source")), "sink": _text(row.get("sink")),
        "control": _text(row.get("control")), "reachability": _text(row.get("product_reachability")),
        "impact": _text(row.get("impact")),
        "build": _text(row.get("build_configuration") or row.get("build_context") or job.get("build_configuration")),
        "evidence": "\n\n".join(evidence) or "No saved source excerpts.",
        "fix": _text(row.get("proposed_fix"), "No reviewed patch is attached. Derive a minimal fix from the exact source."),
        "regression": _text(row.get("regression_test"), "No executed regression test is attached. Test the triggering condition and negative control."),
        "limitations": _text(row.get("proof_gaps"), "No proof gaps recorded in the saved decision.") + "\n\n" + _text(row.get("counterevidence")),
    }


def render_template(template: str, fields: dict[str, str]) -> str:
    if not template.strip() or len(template) > 100000:
        raise ValueError("Шаблон должен содержать текст размером до 100 КБ.")
    try:
        return Template(template).substitute(fields).strip() + "\n"
    except (KeyError, ValueError) as exc:
        raise ValueError("Неизвестное поле или неверный знак $ в шаблоне. Для буквального $ используйте $$.") from exc


def job_fingerprint(job: dict) -> str:
    return digest(canonical({key: job.get(key) for key in
                             ("git_commit", "git_ref", "repository_url", "snapshot_id", "build_configuration")}))


class IssueStore:
    def __init__(self, tool_root: Path):
        self.tool_root = tool_root.resolve()
        self.base = self.tool_root / "RESULTS" / "developer-issues"
        self._check(self.base)

    def _check(self, path: Path) -> Path:
        if not path.is_relative_to(self.tool_root) or not path.resolve().is_relative_to(self.tool_root):
            raise ValueError("Путь вышел за пределы приложения.")
        for parent in reversed([path, *path.parents]):
            if parent.is_relative_to(self.tool_root):
                _regular(parent)
        return path

    def job(self, path: str | Path) -> Path:
        path = Path(path)
        if not path.is_absolute():
            path = self.tool_root / path
        self._check(path)
        if path.parent not in {self.tool_root / "RESULTS", self.tool_root / "jobs"} or not (path / "job.json").is_file():
            raise ValueError("Локальная задача удалена или находится вне RESULTS/jobs.")
        self._check(path / "job.json")
        self._check(path / "decisions.jsonl")
        return path

    def candidates(self, paths: list[Path]) -> tuple[list[dict], list[str]]:
        items, errors = [], []
        for path in paths:
            try:
                job_dir = self.job(path)
                job = read_json(job_dir / "job.json")
                for row in decisions_by_id(job_dir / "decisions.jsonl").values():
                    if row.get("verdict") != "Confirmed":
                        continue
                    fields = report_fields(job, row)
                    relative = job_dir.relative_to(self.tool_root).as_posix()
                    decision_hash = digest(canonical(row))
                    context_hash = job_fingerprint(job)
                    key = digest(canonical([relative, row["marker_id"], decision_hash, context_hash]))
                    items.append({"id": key, "job": relative, "marker_id": row["marker_id"],
                                  "decision_sha256": decision_hash, "source_revision": job.get("git_commit"),
                                  "context_sha256": context_hash,
                                  "project": f"{fields['component']} {fields['version']}",
                                  "location": fields["location"], "detector": fields["detector"],
                                  "verification": fields["verification"]})
            except (OSError, ValueError) as exc:
                errors.append(f"{path.name}: {exc}")
        return items, errors

    def load_queue(self) -> list[dict]:
        path = self._check(self.base / "queue.json")
        if not path.exists():
            return []
        value = read_json(path)
        items = value.get("items")
        if (not isinstance(items, list) or any(
                not isinstance(item, dict)
                or any(not isinstance(item.get(key), str) for key in
                       ("job", "marker_id", "project", "location", "detector", "verification", "status"))
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(item.get(key) or "")) for key in
                       ("id", "decision_sha256", "context_sha256"))
                or not isinstance(item.get("with_poc"), bool)
                for item in items)
                or len({item["id"] for item in items}) != len(items)):
            raise ValueError("Сохранённая очередь issue имеет неверный формат.")
        return items

    def _save_queue(self, items: list[dict]) -> None:
        _write(self._check(self.base / "queue.json"), json.dumps({"schema_version": 1, "items": items}, ensure_ascii=False, indent=2) + "\n")

    def enqueue(self, items: list[dict], *, with_poc: bool) -> int:
        self._check(self.base).mkdir(parents=True, exist_ok=True)
        with decision_lock(self.base / "queue.json"):
            queue = self.load_queue()
            by_id = {item["id"]: item for item in queue}
            added = 0
            for selected in items:
                old = by_id.get(selected["id"])
                if old and old.get("status") in {"queued", "running"}:
                    continue
                item = {**selected, "with_poc": with_poc, "status": "queued", "message": ""}
                if old:
                    queue[queue.index(old)] = item
                else:
                    queue.append(item)
                by_id[item["id"]] = item
                added += 1
            self._save_queue(queue)
        return added

    def update(self, key: str, **fields: Any) -> None:
        with decision_lock(self._check(self.base / "queue.json")):
            queue = self.load_queue()
            item = next(item for item in queue if item["id"] == key)
            item.update(fields)
            self._save_queue(queue)

    def claim(self, key: str) -> dict | None:
        from codex_run import process_is_alive
        with decision_lock(self._check(self.base / "queue.json")):
            queue = self.load_queue()
            if any(item.get("status") == "running" and item.get("pid") != os.getpid()
                   and process_is_alive(item.get("pid")) for item in queue):
                raise ValueError("Очередь issue уже обрабатывается в другом окне приложения.")
            item = next((item for item in queue if item["id"] == key and item.get("status") == "queued"), None)
            if item is None:
                return None
            item.update(status="running", pid=os.getpid(), message="Подготовка issue")
            self._save_queue(queue)
            return dict(item)

    def retry(self, keys: set[str]) -> None:
        from codex_run import process_is_alive
        with decision_lock(self._check(self.base / "queue.json")):
            queue = self.load_queue()
            for item in queue:
                if item["id"] not in keys:
                    continue
                if item.get("status") == "running" and process_is_alive(item.get("pid")):
                    raise ValueError("Этот элемент ещё обрабатывается. Дождитесь завершения.")
                item.update(status="queued", message="Повтор запрошен пользователем")
            self._save_queue(queue)

    def remove(self, keys: set[str]) -> None:
        with decision_lock(self._check(self.base / "queue.json")):
            self._save_queue([item for item in self.load_queue() if item["id"] not in keys or item.get("status") == "running"])

    def case_dir(self, item: dict) -> Path:
        key = item.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Неверный ключ issue.")
        return self._check(self.base / "cases" / key)

    def template(self) -> str:
        path = self._check(self.base / "template.md")
        return path.read_text(encoding="utf-8") if path.exists() else DEFAULT_TEMPLATE

    def workers(self) -> int:
        path = self._check(self.base / "issue-settings.json")
        value = read_json(path).get("parallel_workers", 2) if path.exists() else 2
        return value if type(value) is int and 1 <= value <= 8 else 2

    def save_workers(self, value: int) -> None:
        if type(value) is not int or not 1 <= value <= 8:
            raise ValueError("Число одновременных issue должно быть от 1 до 8.")
        _write(self._check(self.base / "issue-settings.json"), json.dumps({"parallel_workers": value}) + "\n")

    def existing_poc(self, item: dict) -> tuple[Path | None, dict]:
        from poc_generation import existing_generations
        job_dir = self.job(item["job"])
        row = decisions_by_id(job_dir / "decisions.jsonl").get(item["marker_id"])
        if digest(canonical(row)) != item["decision_sha256"]:
            return None, {}
        generations = existing_generations(job_dir, item["marker_id"], decision=row, source_revision=item["source_revision"])
        if not generations:
            return None, {}
        path = self._check(generations[-1])
        return path, read_json(self._check(path / "generation.json"))

    def poc_attachment(self, item: dict) -> str:
        from poc_generation import existing_generations
        job_dir = self.job(item["job"])
        job = read_json(job_dir / "job.json")
        row = decisions_by_id(job_dir / "decisions.jsonl").get(item["marker_id"])
        if digest(canonical(row)) != item["decision_sha256"] or job_fingerprint(job) != item["context_sha256"]:
            raise ValueError("Решение изменилось; прежний PoC нельзя прикладывать к актуальному issue.")
        generations = existing_generations(job_dir, item["marker_id"], decision=row, source_revision=item["source_revision"])
        if not generations:
            raise ValueError("Для этого решения нет сохранённого PoC.")
        directory = self._check(generations[-1])
        metadata = read_json(self._check(directory / "generation.json"))
        if metadata.get("status") != "generated_unverified":
            raise ValueError("PoC ещё не создан: сохранён только список недостающих доказательств.")
        parts = ["## Generated PoC (not executed)\n\nThese generated files have not been run or verified. No baseline/control result is claimed."]
        total = 0
        for entry in metadata.get("files") or []:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Небезопасный путь файла PoC.")
            path = self._check(directory / relative)
            size = path.stat().st_size
            total += size
            if total > 500000:
                raise ValueError("PoC слишком велик для Markdown.")
            raw = path.read_bytes()
            if digest(raw) != entry["sha256"]:
                raise ValueError("Файл PoC изменился после генерации; проверьте его вручную.")
            parts.append(f"### {relative.as_posix()}\n\n" + fence(raw.decode("utf-8"), path.suffix.lstrip(".") or "text"))
        return "\n\n".join(parts) + "\n"

    def save_template(self, text: str) -> None:
        render_template(text, report_fields({}, {}))
        _write(self._check(self.base / "template.md"), text)

    def save_draft(self, item: dict, title: str, body: str) -> None:
        if len(body.encode("utf-8")) > 1000000 or not title.strip() or len(title) > 500:
            raise ValueError("Укажите заголовок до 500 символов; размер текста — до 1 МБ.")
        directory = self.case_dir(item)
        _write(self._check(directory / "body.md"), body)
        _write(self._check(directory / "title.json"), json.dumps({"title": title}, ensure_ascii=False) + "\n")

    def _current_finding(self, item: dict) -> tuple[Path, dict, dict]:
        job_dir = self.job(item["job"])
        job = read_json(job_dir / "job.json")
        row = decisions_by_id(job_dir / "decisions.jsonl").get(item["marker_id"])
        if (not row or row.get("verdict") != "Confirmed" or digest(canonical(row)) != item["decision_sha256"]
                or job_fingerprint(job) != item.get("context_sha256")):
            raise ValueError("Решение или ревизия изменились: обновите Confirmed и добавьте маркер заново.")
        return job_dir, job, row

    def _brief_draft(self, item: dict, *, english_via_codex: bool) -> tuple[str, dict]:
        from issue_brief import generate_brief, local_brief
        _, job, row = self._current_finding(item)
        fields = report_fields(job, row)
        if english_via_codex:
            brief = generate_brief(job, row, fields)
        else:
            brief = local_brief(fields)
        # Selected evidence must remain current after the model call.
        self._current_finding(item)
        fields.update({key: value for key, value in brief.items() if key != "title"})
        if (row.get("verification") or {}).get("status") != "verified":
            fields["description"] = "Independent review is not complete. " + fields["description"]
        return brief["title"], fields

    def rewrite_brief(self, item: dict) -> dict:
        directory = self.case_dir(item)
        body_path = self._check(directory / "body.md")
        title_path = self._check(directory / "title.json")
        before = (body_path.read_text(encoding="utf-8"), title_path.read_text(encoding="utf-8"))
        title, fields = self._brief_draft(item, english_via_codex=True)
        if before != (body_path.read_text(encoding="utf-8"), title_path.read_text(encoding="utf-8")):
            raise ValueError("Черновик изменён во время подготовки. Изменения сохранены; повторите сокращение.")
        backup = self._check(directory / "previous" / uuid.uuid4().hex[:8])
        _write(backup / "body.md", before[0])
        _write(backup / "title.json", before[1])
        self.save_draft(item, title, render_template(DEFAULT_TEMPLATE, fields))
        return {"case": str(directory), "message": "Краткий английский issue готов; прежний текст сохранён в previous"}

    def prepare(self, item: dict, *, english_via_codex: bool = False) -> dict:
        job_dir, job, row = self._current_finding(item)
        directory = self.case_dir(item)
        body_path = self._check(directory / "body.md")
        if not body_path.exists():
            title, fields = self._brief_draft(item, english_via_codex=english_via_codex)
            body = render_template(self.template(), fields)
            self.save_draft(item, title, body)
            _write(self._check(directory / "case.json"), json.dumps({**item, "publication_state": "local_draft", "reproduction_status": "not_run"}, ensure_ascii=False, indent=2) + "\n")
        result = {"case": str(directory), "status": "draft", "message": "Markdown готов"}
        previous, metadata = self.existing_poc(item)
        if previous:
            result.update(poc=str(previous), status="poc_unverified" if metadata.get("status") == "generated_unverified" else "needs_evidence",
                          message="Markdown готов; PoC не запускался" if metadata.get("status") == "generated_unverified" else "Markdown готов; для PoC нужны данные")
        if item.get("with_poc"):
            from poc_generation import existing_generations, generate_for_marker
            try:
                generations = existing_generations(job_dir, row["marker_id"], decision=row, source_revision=job.get("git_commit"))
                generated = read_json(generations[-1] / "generation.json") if generations else generate_for_marker(job_dir, row["marker_id"], model=job.get("codex_model") or None)
                directory_poc = str(generations[-1]) if generations else generated["directory"]
                # Recheck after the potentially long model call. Keep the draft and
                # artifacts, but never associate stale evidence with a new decision.
                current_job = read_json(job_dir / "job.json")
                current = decisions_by_id(job_dir / "decisions.jsonl").get(row["marker_id"])
                if digest(canonical(current)) != item["decision_sha256"] or job_fingerprint(current_job) != item["context_sha256"]:
                    raise ValueError("Решение изменилось во время генерации; PoC не привязан к issue.")
                result.update(poc=directory_poc, status="poc_unverified" if generated["status"] == "generated_unverified" else "needs_evidence",
                              message="PoC создан, не запускался" if generated["status"] == "generated_unverified" else "Для PoC нужны дополнительные доказательства")
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                result.update(status="poc_error", message=f"Markdown готов; PoC: {exc}")
        return result
