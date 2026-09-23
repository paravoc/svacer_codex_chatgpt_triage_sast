#!/usr/bin/env python3
"""Authenticated web control plane for the local Svacer triage engine."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from codex_run import launch_runner, read_run_record, stop_run
from comment_format import svacer_comment_text
from local_jobs import require_idle, update_marker_inventory
from project_setup import create_project, list_remote_refs
from triage_dashboard import call_mcp_tool, collect_state, read_json, read_jsonl, set_pause
from triage_queue import (
    dequeue_marker_ids,
    enqueue_marker_ids,
    load_decisions,
    marker_review_status,
    priority_marker_ids,
)


APP_DIRECTORY = Path(__file__).resolve().parent
STATIC_DIRECTORY = APP_DIRECTORY / "web"
DATA_ROOT = Path(os.getenv("SVACER_DATA_DIR", str(APP_DIRECTORY.parent))).resolve()
SESSION_TTL = 12 * 60 * 60
MAX_BODY = 64 * 1024
MARKER_FIELDS = [
    "id", "invariant", "warnClass", "file", "line", "msg", "function",
    "review", "tool", "mtid",
]
SESSIONS: dict[str, dict[str, Any]] = {}


def secret_value(name: str) -> str:
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Cannot read {name}_FILE") from exc
    return os.getenv(name, "").strip()


def web_token() -> str:
    token = secret_value("SVACER_WEB_TOKEN")
    if len(token) < 32:
        raise RuntimeError("SVACER_WEB_TOKEN must contain at least 32 characters")
    return token


def app_settings() -> dict[str, Any]:
    configured = os.getenv("SVACER_SETTINGS_FILE", "").strip()
    path = Path(configured) if configured else APP_DIRECTORY / "svacer-settings.json"
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError("Некорректный файл настроек Svacer.")
    return value


def mcp_connection() -> tuple[str, str]:
    settings = app_settings()
    token = os.getenv("SVACER_LOCAL_MCP_TOKEN", "").strip()
    if len(token) < 32:
        raise RuntimeError("Подключение к Svacer ещё не настроено.")
    return str(settings.get("mcp_url") or "http://127.0.0.1:8002/mcp"), token


def known_jobs() -> dict[str, Path]:
    jobs: dict[str, Path] = {}
    for root_name in ("RESULTS", "jobs"):
        root = DATA_ROOT / root_name
        if not root.is_dir() or root.is_symlink():
            continue
        for path in root.iterdir():
            if not path.is_dir() or path.is_symlink() or not (path / "job.json").is_file():
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if resolved.parent != root.resolve():
                continue
            key = hashlib.sha256(f"{root_name}/{path.name}".encode()).hexdigest()[:20]
            jobs[key] = resolved
    return jobs


def selected_job(job_id: str) -> Path:
    job = known_jobs().get(job_id)
    if job is None:
        raise LookupError("Локальная задача не найдена.")
    return job


def job_label(job: Path) -> str:
    data = read_json(job / "job.json")
    repository = str(data.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1]
    if repository.endswith(".git"):
        repository = repository[:-4]
    return " ".join(part for part in (repository, str(data.get("git_ref") or "")) if part) or job.name


def validate_inventory(payload: Any, expected_filter: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("markers"), list):
        raise ValueError("Svacer вернул инвентарь неизвестного формата.")
    markers = payload["markers"]
    if payload.get("truncated") is not False:
        raise ValueError("Svacer вернул неполный список маркеров.")
    if payload.get("returned_count") != payload.get("total_count") or payload.get("returned_count") != len(markers):
        raise ValueError("Svacer вернул неполный инвентарь маркеров.")
    filters = payload.get("filters_applied")
    if not isinstance(filters, dict) or filters.get("advanced_filter") != expected_filter:
        raise ValueError("Svacer не подтвердил точный фильтр задачи.")
    ids = [str(row.get("id") or "") for row in markers if isinstance(row, dict)]
    if len(ids) != len(markers) or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("Инвентарь содержит некорректные или повторяющиеся marker ID.")
    return payload


async def refresh_inventory(job: Path) -> dict[str, Any]:
    require_idle(job)
    data = read_json(job / "job.json")
    settings = app_settings()
    expected = str(settings.get("advanced_filter") or "")
    if not expected or str(data.get("advanced_filter") or "") != expected:
        raise ValueError("Фильтр задачи не совпадает с настройкой сервиса.")
    missing = [name for name in ("project_id", "branch_id", "snapshot_id") if not str(data.get(name) or "")]
    if missing:
        raise ValueError("В задаче отсутствуют идентификаторы: " + ", ".join(missing))
    mcp_url, token = mcp_connection()
    reply = await call_mcp_tool(mcp_url, token, "get_markers", {
        "project_id": str(data["project_id"]),
        "branch_id": str(data["branch_id"]),
        "snapshot_id": str(data["snapshot_id"]),
        "advanced_filter": expected,
        "traces": False,
        "checker_info": False,
        "review_history": False,
        "comment_history": False,
        "fields": MARKER_FIELDS,
        "limit": 0,
    })
    return update_marker_inventory(job, DATA_ROOT, validate_inventory(json.loads(reply), expected))


def cleanup_sessions() -> None:
    now = time.time()
    for session_id in [key for key, value in SESSIONS.items() if float(value.get("expires", 0)) <= now]:
        SESSIONS.pop(session_id, None)


def session_for(request: Request) -> tuple[str, dict[str, Any]] | None:
    cleanup_sessions()
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer ") and hmac.compare_digest(authorization[7:], web_token()):
        return "bearer", {"csrf": ""}
    session_id = request.cookies.get("svacer_session", "")
    session = SESSIONS.get(session_id)
    if session is not None:
        session["expires"] = time.time() + SESSION_TTL
        return session_id, session
    return None


def require_auth(request: Request, *, mutation: bool = False) -> dict[str, Any]:
    authenticated = session_for(request)
    if authenticated is None:
        raise PermissionError("Требуется вход.")
    identity, session = authenticated
    if mutation and identity != "bearer":
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or not hmac.compare_digest(supplied, str(session.get("csrf") or "")):
            raise PermissionError("Проверка запроса не пройдена. Обновите страницу.")
    return session


async def json_body(request: Request) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY:
        raise ValueError("Запрос слишком большой.")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("Запрос слишком большой.")
    try:
        value = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Ожидался JSON-объект.") from exc
    if not isinstance(value, dict):
        raise ValueError("Ожидался JSON-объект.")
    return value


def error_response(exc: BaseException, status: int = 400) -> JSONResponse:
    if isinstance(exc, PermissionError):
        status = 401
    elif isinstance(exc, LookupError):
        status = 404
    message = str(exc).strip() or type(exc).__name__
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def guarded(request: Request, action, *, mutation: bool = False) -> Response:
    try:
        require_auth(request, mutation=mutation)
        return await action()
    except (PermissionError, LookupError, ValueError, RuntimeError, OSError, SystemExit) as exc:
        return error_response(exc)


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def login_page(request: Request) -> Response:
    if session_for(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIRECTORY / "login.html")


async def login(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > 4096:
        return HTMLResponse("Login request is too large", status_code=400)
    form = parse_qs(raw.decode("utf-8", errors="strict"), keep_blank_values=True)
    supplied = (form.get("token") or [""])[0]
    if not supplied or not hmac.compare_digest(supplied, web_token()):
        return RedirectResponse("/login?error=1", status_code=303)
    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = {"csrf": secrets.token_urlsafe(32), "expires": time.time() + SESSION_TTL}
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "svacer_session", session_id, max_age=SESSION_TTL, httponly=True,
        secure=os.getenv("SVACER_COOKIE_SECURE", "1") not in {"0", "false", "False"},
        samesite="strict", path="/",
    )
    return response


async def logout(request: Request) -> Response:
    try:
        require_auth(request, mutation=True)
        authenticated = session_for(request)
        if authenticated and authenticated[0] != "bearer":
            SESSIONS.pop(authenticated[0], None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("svacer_session", path="/")
        return response
    except PermissionError as exc:
        return error_response(exc)


async def index(request: Request) -> Response:
    if session_for(request) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIRECTORY / "index.html")


async def api_session(request: Request) -> Response:
    async def action() -> Response:
        authenticated = session_for(request)
        assert authenticated is not None
        return JSONResponse({"ok": True, "csrf": authenticated[1].get("csrf", "")})
    return await guarded(request, action)


async def api_jobs(request: Request) -> Response:
    async def action() -> Response:
        rows = []
        for key, job in known_jobs().items():
            try:
                data = read_json(job / "job.json")
                state = collect_state(job)
                run = read_run_record(job)
                rows.append({
                    "id": key, "label": job_label(job), "created_at": data.get("created_at"),
                    "snapshot_url": data.get("snapshot_url"), "repository_url": data.get("repository_url"),
                    "git_ref": data.get("git_ref"), "total": state.get("inventory_total", 0),
                    "completed": state.get("completed", 0), "active": bool(run.get("active")),
                })
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return JSONResponse({"ok": True, "jobs": rows})
    return await guarded(request, action)


def public_state(job: Path) -> dict[str, Any]:
    state = collect_state(job)
    state.pop("job_path", None)
    state["by_verdict"] = dict(state.get("by_verdict") or {})
    state["verification"] = dict(state.get("verification") or {})
    state["codex_run"] = read_run_record(job)
    return state


async def api_job(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        data = read_json(job / "job.json")
        return JSONResponse({
            "ok": True, "job": {"id": request.path_params["job_id"], "label": job_label(job),
            "snapshot_url": data.get("snapshot_url"), "repository_url": data.get("repository_url"),
            "git_ref": data.get("git_ref")}, "state": public_state(job),
        })
    return await guarded(request, action)


async def api_markers(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        inventory = read_json(job / "markers.inventory.json") if (job / "markers.inventory.json").is_file() else {}
        markers = inventory.get("markers") if isinstance(inventory, dict) else []
        if not isinstance(markers, list):
            markers = []
        decisions = load_decisions(job / "decisions.jsonl") if (job / "decisions.jsonl").is_file() else []
        by_id = {str(row.get("marker_id") or ""): row for row in decisions}
        state = collect_state(job)
        queued = set(state.get("priority_marker_ids") or [])
        deferred = set(state.get("deferred_marker_ids") or [])
        running = bool(read_run_record(job).get("active"))
        active = {
            str(marker_id) for worker in (state.get("workers") or {}).values()
            for marker_id in (worker.get("marker_ids") or [])
            if running and worker.get("current_status") in {"assigned", "working", "running"}
            and marker_id not in set(worker.get("current_saved_marker_ids") or [])
        }
        runtime = state.get("worker_runtime") or {}
        if runtime:
            active = {mid for mid, worker in runtime.get("workers", {}).items()
                      if running and worker.get("state") == "running" and worker.get("pid")}
            deferred.update(mid for mid, worker in runtime.get("workers", {}).items()
                            if worker.get("state") == "incomplete")
        query = request.query_params.get("q", "").strip().casefold()
        status_filter = request.query_params.get("status", "all")
        rows = []
        for marker in markers:
            if not isinstance(marker, dict):
                continue
            marker_id = str(marker.get("id") or "")
            decision = by_id.get(marker_id, {})
            review = marker_review_status(marker)
            verdict = str(decision.get("verdict") or "")
            status = ("active" if marker_id in active else "needs_context" if marker_id in deferred
                      and marker_id in queued else "queued" if marker_id in queued else verdict or review)
            haystack = " ".join(str(marker.get(key) or "") for key in ("id", "warnClass", "file", "line", "msg")).casefold()
            if query and query not in haystack:
                continue
            if status_filter != "all" and status_filter != status and not (status_filter == "queued" and marker_id in queued):
                continue
            rows.append({
                "id": marker_id, "status": status, "queued": marker_id in queued,
                "active": marker_id in active, "review": review, "verdict": verdict,
                "warnClass": marker.get("warnClass"), "file": marker.get("file"),
                "line": marker.get("line"), "msg": marker.get("msg"),
            })
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            limit = min(500, max(1, int(request.query_params.get("limit", "200"))))
        except ValueError as exc:
            raise ValueError("Некорректная страница списка.") from exc
        start = (page - 1) * limit
        return JSONResponse({"ok": True, "markers": rows[start:start + limit], "total": len(rows),
                             "page": page, "limit": limit})
    return await guarded(request, action)


async def api_marker(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        marker_id = request.path_params["marker_id"]
        inventory = read_json(job / "markers.inventory.json")
        marker = next((row for row in inventory.get("markers", [])
                       if isinstance(row, dict) and str(row.get("id") or "") == marker_id), None)
        if marker is None:
            raise LookupError("Маркер не найден.")
        decision = next((row for row in load_decisions(job / "decisions.jsonl")
                         if str(row.get("marker_id") or "") == marker_id), {})
        if isinstance(decision.get("comment"), str):
            decision = {**decision, "comment": svacer_comment_text(decision["comment"])}
        return JSONResponse({"ok": True, "marker": marker, "decision": decision})
    return await guarded(request, action)


async def api_refresh(request: Request) -> Response:
    async def action() -> Response:
        result = await refresh_inventory(selected_job(request.path_params["job_id"]))
        return JSONResponse({"ok": True, "result": result})
    return await guarded(request, action, mutation=True)


async def api_queue(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        body = await json_body(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ValueError("ids должен быть массивом marker ID.")
        result = enqueue_marker_ids(job / "markers.inventory.json", job / "decisions.jsonl", ids)
        return JSONResponse({"ok": True, "result": result})
    return await guarded(request, action, mutation=True)


async def api_dequeue(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        body = await json_body(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ValueError("ids должен быть массивом marker ID.")
        result = dequeue_marker_ids(job / "decisions.jsonl", ids)
        return JSONResponse({"ok": True, "result": result})
    return await guarded(request, action, mutation=True)


async def api_start(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        if not (job / "markers.inventory.json").is_file():
            raise ValueError("Сначала получите маркеры.")
        if not priority_marker_ids(job / "decisions.jsonl"):
            raise ValueError("Очередь пуста. Выберите маркеры перед запуском.")
        set_pause(job, False)
        launched = launch_runner(job, APP_DIRECTORY)
        return JSONResponse({"ok": True, "run": launched})
    return await guarded(request, action, mutation=True)


async def api_stop(request: Request) -> Response:
    async def action() -> Response:
        return JSONResponse({"ok": True, "run": stop_run(selected_job(request.path_params["job_id"]))})
    return await guarded(request, action, mutation=True)


async def api_refs(request: Request) -> Response:
    async def action() -> Response:
        body = await json_body(request)
        refs = await asyncio.to_thread(list_remote_refs, str(body.get("repository_url") or ""))
        return JSONResponse({"ok": True, "refs": refs})
    return await guarded(request, action, mutation=True)


async def api_create_project(request: Request) -> Response:
    async def action() -> Response:
        body = await json_body(request)
        selection = body.get("selection")
        if not isinstance(selection, dict):
            raise ValueError("Выберите Git-ревизию.")
        job = await asyncio.to_thread(
            create_project, DATA_ROOT, app_settings(), str(body.get("snapshot_url") or ""),
            str(body.get("repository_url") or ""), selection,
        )
        data = read_json(job / "job.json")
        data["tool_directory"] = str(DATA_ROOT)
        data["app_directory"] = str(APP_DIRECTORY)
        temporary = job / "job.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, job / "job.json")
        marker_result: dict[str, Any] | None = None
        warning = ""
        try:
            marker_result = await refresh_inventory(job)
        except Exception as exc:
            warning = str(exc)
        job_id = next(key for key, value in known_jobs().items() if value == job.resolve())
        return JSONResponse({"ok": True, "job_id": job_id, "markers": marker_result, "warning": warning})
    return await guarded(request, action, mutation=True)


async def api_import_preview(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        state = collect_state(job)
        if not state.get("import_ready") or state.get("import_blocked"):
            raise ValueError(state.get("import_error") or "Нет новых готовых решений либо предыдущая отправка не подтверждена.")
        mcp_url, token = mcp_connection()
        reply = await call_mcp_tool(mcp_url, token, "prepare_markup_import", {"job_directory": str(job)})
        return JSONResponse({"ok": True, "preview": json.loads(reply)})
    return await guarded(request, action, mutation=True)


async def api_import_apply(request: Request) -> Response:
    async def action() -> Response:
        job = selected_job(request.path_params["job_id"])
        body = await json_body(request)
        preview = read_json(job / "svacer-import-preview.json")
        force = bool(preview.get("requires_force"))
        expected = str(preview.get("force_confirmation" if force else "confirmation") or "")
        supplied = str(body.get("confirmation") or "")
        if not expected or not hmac.compare_digest(supplied, expected):
            raise ValueError("Фраза подтверждения не совпала; ничего не отправлено.")
        mcp_url, token = mcp_connection()
        reply = await call_mcp_tool(mcp_url, token, "apply_markup_import", {
            "job_directory": str(job), "confirmation": supplied,
            "overwrite": "force" if force else "none",
        })
        return JSONResponse({"ok": True, "result": json.loads(reply)})
    return await guarded(request, action, mutation=True)


routes = [
    Route("/healthz", health), Route("/login", login_page, methods=["GET"]),
    Route("/login", login, methods=["POST"]), Route("/logout", logout, methods=["POST"]),
    Route("/", index), Route("/api/session", api_session), Route("/api/jobs", api_jobs),
    Route("/api/jobs", api_create_project, methods=["POST"]),
    Route("/api/repositories/refs", api_refs, methods=["POST"]),
    Route("/api/jobs/{job_id}", api_job), Route("/api/jobs/{job_id}/markers", api_markers),
    Route("/api/jobs/{job_id}/markers/{marker_id}", api_marker),
    Route("/api/jobs/{job_id}/refresh", api_refresh, methods=["POST"]),
    Route("/api/jobs/{job_id}/queue", api_queue, methods=["POST"]),
    Route("/api/jobs/{job_id}/dequeue", api_dequeue, methods=["POST"]),
    Route("/api/jobs/{job_id}/start", api_start, methods=["POST"]),
    Route("/api/jobs/{job_id}/stop", api_stop, methods=["POST"]),
    Route("/api/jobs/{job_id}/import/preview", api_import_preview, methods=["POST"]),
    Route("/api/jobs/{job_id}/import/apply", api_import_apply, methods=["POST"]),
]
app = Starlette(debug=False, routes=routes)
app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")
allowed_hosts = [value.strip() for value in os.getenv("SVACER_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if value.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)


async def security_headers(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse({"ok": False, "error": "Внутренняя ошибка сервиса."}, status_code=500)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)
