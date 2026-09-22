"""Compatibility operations implemented from public REST specifications.

The interface is also consumed by triage_gui*, codex_run and CODEX_TASK.md.
No third-party connector modules are imported.
"""
from __future__ import annotations

import base64
import json
import re
from collections import Counter
from uuid import UUID

from .client import ConnectorError, PublicAPI


def identifier(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ConnectorError("Требуется полный UUID проекта, ветки или снимка.") from None


def encoded_filter(value: dict) -> str:
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii")


def object_rows(value, context: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ConnectorError(f"{context}: ожидается полный массив объектов.")
    return value


def review_data(row: dict) -> dict:
    value = row.get("review")
    if value is None:
        return {}
    if isinstance(value, str):
        return {"status": value}
    if not isinstance(value, dict):
        raise ConnectorError("Некорректная разметка маркера.")
    return value


def select_fields(row: dict, fields: list[str] | None) -> dict:
    if fields == ["*"]:
        return dict(row)
    keys = fields if fields is not None else [
        "id", "warnClass", "file", "line", "msg", "function", "review",
        "traces", "checkerInfo", "review_history", "comments",
    ]
    return {key: row[key] for key in keys if key in row}


def envelope(rows: list[dict], limit: int, fields: list[str] | None, *, key: str = "markers", filters=None) -> dict:
    if type(limit) is not int or limit < 0:
        raise ConnectorError("limit должен быть неотрицательным целым числом.")
    chosen = rows if limit == 0 else rows[:limit]
    return {"total_count": len(rows), "returned_count": len(chosen),
            "truncated": len(chosen) != len(rows), "filters_applied": filters or {},
            key: [select_fields(row, fields) for row in chosen]}


class SvacerService:
    def __init__(self, api: PublicAPI):
        self.api = api

    async def get_projects(self) -> list[dict]:
        rows = object_rows(await self.api.json("GET", "/api/public/projects", read_only=True), "Проекты")
        result = []
        for row in rows:
            project = row.get("project")
            if not isinstance(project, dict) or not project.get("id"):
                raise ConnectorError("Svacer вернул проект без идентификатора.")
            result.append({"project_id": project["id"], "project_name": project.get("name"),
                           "created": project.get("time"), "created_by": project.get("created_by"),
                           "branches": [{"branch_id": b["id"], "branch_name": b.get("name"),
                                         "created": b.get("time")}
                                        for b in object_rows(row.get("branches", []), "Ветки")]})
        return result

    async def get_snapshots(self, project_id: str, branch_id: str, name_filter: str | None = None) -> list[dict]:
        path = f"/api/public/projects/{identifier(project_id)}/branch/{identifier(branch_id)}/snapshots"
        params = {"filters": encoded_filter({"snapshot": {"name": name_filter}})} if name_filter else {}
        rows = object_rows(await self.api.json("GET", path, params=params, read_only=True), "Снимки")
        result = []
        for row in rows:
            details = row.get("details") if isinstance(row.get("details"), dict) else {}
            result.append({**row, "snapshot_id": row["id"], "import_time": row.get("import_time") or row.get("time"),
                           "commit_hash": details.get("commit_hash"), "markers_count": details.get("markers_count")})
        return sorted(result, key=lambda row: row.get("import_time") or "", reverse=True)

    async def marker_rows(self, project_id: str, branch_id: str, snapshot_id: str, *,
                          severity: list[str] | None = None, review: list[str] | None = None,
                          warnClass: list[str] | None = None, file: list[str] | None = None,
                          traces: bool = False, checker_info: bool = False,
                          review_history: bool = False, comment_history: bool = False,
                          custom_filter: str | None = None, advanced_filter: str | None = None) -> tuple[list[dict], dict]:
        project, branch, snapshot = map(identifier, (project_id, branch_id, snapshot_id))
        selected_ids = None
        if advanced_filter:
            selected = await self.api.json("POST", "/api/public/afilters/apply", read_only=True,
                                           json={"filter": advanced_filter, "snapshot_id": [snapshot]})
            if (not isinstance(selected, dict) or selected.get("errors")
                    or not isinstance(selected.get("marker_ids"), list)
                    or any(not isinstance(x, str) or not x for x in selected["marker_ids"])):
                raise ConnectorError("Svacer не подтвердил выполнение advanced_filter. Выборка остановлена.")
            selected_ids = set(selected["marker_ids"])
            if len(selected_ids) != len(selected["marker_ids"]):
                raise ConnectorError("Фильтр вернул повторяющиеся ID.")
        params = {"traces": str(traces).lower(), "checker_info": str(checker_info).lower(),
                  "review_history": str(review_history).lower(), "comment_history": str(comment_history).lower()}
        marker_filter = {}
        if warnClass:
            marker_filter["checker"] = "^(" + "|".join(re.escape(x) for x in warnClass) + r")($|\.)"
        if file:
            marker_filter["file"] = "(" + "|".join(re.escape(x) for x in file) + ")"
        if marker_filter:
            params["filters"] = encoded_filter({"marker": marker_filter})
        if custom_filter:
            params["custom_filter"] = custom_filter
        path = f"/api/public/projects/{project}/branch/{branch}/snapshots/{snapshot}/fullmarkers"
        rows = object_rows(await self.api.json("GET", path, params=params, read_only=True), "Маркеры")
        ids = [r.get("id") for r in rows]
        if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
            raise ConnectorError("Svacer вернул пустые или повторяющиеся ID маркеров.")
        if selected_ids is not None:
            if not marker_filter and not custom_filter and not selected_ids.issubset(set(ids)):
                raise ConnectorError("Не все маркеры advanced_filter получены из снимка.")
            rows = [r for r in rows if r["id"] in selected_ids]
        # Also enforce documented field semantics locally; server filters may be broader.
        if warnClass:
            rows = [r for r in rows if any(r.get("warnClass") == w or str(r.get("warnClass", "")).startswith(w + ".") for w in warnClass)]
        if file:
            rows = [r for r in rows if any(f in str(r.get("file", "")) for f in file)]
        if review:
            rows = [r for r in rows if (review_data(r).get("status") or "Undecided") in review]
        if severity:
            rows = [r for r in rows if (review_data(r).get("severity") or "Unspecified") in severity]
        filters = {k: v for k, v in {"severity": severity, "review": review, "warnClass": warnClass,
                    "file": file, "custom_filter": custom_filter, "advanced_filter": advanced_filter,
                    "traces": traces, "checker_info": checker_info, "review_history": review_history,
                    "comment_history": comment_history}.items() if v}
        return rows, filters

    async def get_markers(self, project_id: str, branch_id: str, snapshot_id: str, *,
                          limit: int = 30, fields: list[str] | None = None, **kwargs) -> dict:
        if type(limit) is not int or limit < 0:
            raise ConnectorError("limit должен быть неотрицательным целым числом.")
        rows, filters = await self.marker_rows(project_id, branch_id, snapshot_id, **kwargs)
        return envelope(rows, limit, fields, filters=filters)

    async def get_warnings(self, project_id: str, branch_id: str, snapshot_id: str, *,
                           limit: int | None = None, fields: list[str] | None = None, **kwargs) -> dict:
        if limit is None:
            limit = 8 if any(kwargs.get(k) for k in ("traces", "review_history", "comment_history")) else 30
        result = await self.get_markers(project_id, branch_id, snapshot_id, limit=limit, fields=fields, **kwargs)
        result["warnings"] = result.pop("markers")
        return result

    async def get_project_stats(self, project_id: str, branch_id: str, snapshot_id: str) -> dict:
        rows, _ = await self.marker_rows(project_id, branch_id, snapshot_id)
        return {"total_warnings": len(rows),
                "by_severity": dict(Counter(review_data(r).get("severity") or "Unspecified" for r in rows)),
                "by_review_status": dict(Counter(review_data(r).get("status") or "Undecided" for r in rows)),
                "by_checker": dict(Counter(r.get("warnClass", "") for r in rows))}

    async def get_advanced_file_preview(self, snapshot_id: str, file_path: str, line: int = 1,
                                        before: int = 0, after: int = 99999) -> dict:
        if not file_path or "\0" in file_path or type(line) is not int or line < 1:
            raise ConnectorError("Нужны путь исходника и номер строки от 1.")
        if any(type(n) is not int or n < 0 for n in (before, after)):
            raise ConnectorError("before/after должны быть неотрицательными.")
        result = await self.api.json("GET", "/api/public/advanced_file_preview", read_only=True,
            params={"snapshot": identifier(snapshot_id), "file": file_path, "line": line,
                    "before": before, "after": after, "output": "json"})
        if (not isinstance(result, dict) or not isinstance(result.get("content"), str)
                or type(result.get("line")) is not int or type(result.get("total_lines")) is not int
                or result["line"] != max(0, line - before - 1)):
            raise ConnectorError("Svacer вернул неполный или несовместимый preview исходника.")
        return result

    async def get_project_groups(self, name_or_id: str) -> dict:
        if not name_or_id.strip():
            raise ConnectorError("Нужно имя или ID группы.")
        return await self.api.json("POST", "/api/public/admin/server/project-groups", read_only=True,
                                   json={"action": "get", "project_group_name_or_id": name_or_id})

    async def get_diff(self, base_snapshot_id: str, head_snapshot_id: str | None = None,
                       level: int = 0, checker_info: bool = False, limit: int = 20,
                       fields: list[str] | None = None) -> dict:
        if type(level) is not int or level not in (0, 1, 2) or type(limit) is not int or limit < 0:
            raise ConnectorError("level должен быть 0, 1 или 2; limit — от 0.")
        params = {"snapshot_v1": identifier(base_snapshot_id), "level": level,
                  "checker_info": str(checker_info).lower()}
        if head_snapshot_id:
            params["snapshot_v2"] = identifier(head_snapshot_id)
        result = await self.api.json("GET", "/api/public/diff", params=params, read_only=True)
        if not isinstance(result, dict):
            raise ConnectorError("Svacer не вернул сравнение снимков.")
        if level == 2:
            groups = result.get("markers")
            if not isinstance(groups, dict):
                raise ConnectorError("В сравнении отсутствуют категории маркеров.")
            result["markers"] = {k: envelope(object_rows(v or [], "Категория diff"), limit, fields)
                                 for k, v in groups.items()}
        return result
