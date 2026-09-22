"""Local project creation and read-only Git reference discovery."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from local_jobs import checked_job, job_operation_lock, require_idle
from triage_queue import (GOST_FILTER, atomic_write_json, complete_desktop_settings,
                          decision_lock, load_decisions)


COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
UUID_PATTERN = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"


def scope_fields(value: str, server: str) -> dict[str, str]:
    """Parse a project, branch, snapshot or marker URL without choosing a snapshot."""
    uri, configured = urlsplit(value.strip()), urlsplit(server.strip())
    if (uri.scheme not in {"http", "https"} or not uri.hostname or uri.username
            or uri.password or uri.query or uri.fragment):
        raise ValueError("Нужна ссылка на проект, ветку или снимок Svacer без пароля, токена или параметров запроса.")
    if (uri.scheme, uri.netloc.lower()) != (configured.scheme, configured.netloc.lower()):
        raise ValueError("Сервер ссылки не совпадает с настроенным сервером Svacer.")
    match = re.search(
        rf"/project/({UUID_PATTERN})(?:/branch/({UUID_PATTERN})(?:/snapshot/({UUID_PATTERN}))?)?(?=/|$)",
        uri.path,
    )
    if not match:
        raise ValueError("В ссылке не найден корректный UUID проекта Svacer.")
    project, branch, snapshot = match.groups()
    if uri.path[match.end():].startswith(("/branch", "/snapshot")):
        raise ValueError("В ссылке некорректный UUID ветки или снимка Svacer.")
    result = {"project_id": project.lower(),
              "scope_url": f"{uri.scheme}://{uri.netloc}/mode/review/project/{project.lower()}"}
    for name, item in (("branch", branch), ("snapshot", snapshot)):
        if item:
            result[f"{name}_id"] = item.lower()
            result["scope_url"] += f"/{name}/{item.lower()}"
    return result


def snapshot_fields(value: str, server: str) -> dict[str, str]:
    scope = scope_fields(value, server)
    if "snapshot_id" not in scope:
        raise ValueError("Выбрана только ветка Svacer, без снимка. Нажмите «Выбрать снимок»."
                         if "branch_id" in scope else
                         "Ссылка ведёт только на проект Svacer. Нажмите «Выбрать снимок» "
                         "и выберите ветку и снимок либо вставьте полную ссылку /branch/…/snapshot/….")
    scope["snapshot_url"] = scope.pop("scope_url")
    return scope


def scope_options(payload: object, kind: str, project_id: str) -> list[dict[str, str]]:
    """Validate connector rows before allowing their IDs into a saved job."""
    if not isinstance(payload, list):
        raise ValueError("Svacer вернул неверный формат списка.")
    if kind == "branch":
        projects = [p for p in payload if isinstance(p, dict)
                    and str(p.get("project_id", "")).lower() == project_id]
        if len(projects) != 1:
            raise ValueError("Проект не найден в Svacer или нет доступа к нему.")
        payload = projects[0].get("branches")
    elif kind != "snapshot":
        raise ValueError("Неизвестный тип списка Svacer.")
    if not isinstance(payload, list):
        raise ValueError("Svacer вернул неверный формат списка.")
    result, seen = [], set()
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Svacer вернул неверный формат записи.")
        item_id = str(row.get(f"{kind}_id") or "").lower()
        if not re.fullmatch(UUID_PATTERN, item_id) or item_id in seen:
            raise ValueError("Svacer вернул некорректные или повторяющиеся идентификаторы.")
        seen.add(item_id)
        name = str(row.get("branch_name" if kind == "branch" else "name") or item_id)
        when = str(row.get("import_time") or "") if kind == "snapshot" else ""
        result.append({"id": item_id, "label": f"{name} · {when}" if when else name,
                       "commit": str(row.get("commit_hash") or "")})
    return result


def repository_url(value: str) -> str:
    value = value.strip()
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Укажите URL Git-репозитория без пробелов.")
    # SCP-style SSH URLs have a username, but never a password or a token.
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", value):
        return value
    uri = urlsplit(value)
    if (uri.scheme not in {"https", "http", "ssh"} or not uri.hostname or not uri.path.strip("/")
            or uri.password or uri.query or uri.fragment or "%" in uri.netloc
            or (uri.username and not (uri.scheme == "ssh" and uri.username == "git"))):
        raise ValueError("Нужен HTTP(S) или SSH URL репозитория без встроенных паролей и токенов.")
    return value.rstrip("/")


def repository_search_query(value: str) -> str:
    """Validate a public repository search without accepting hidden input."""
    if any(ord(char) < 32 for char in value):
        raise ValueError("Поисковая фраза не должна содержать управляющие символы.")
    value = " ".join(value.split())
    if len(value) < 2:
        raise ValueError("Введите хотя бы два символа для поиска репозитория.")
    if len(value) > 100:
        raise ValueError("Поисковая фраза слишком длинная: максимум 100 символов.")
    return value


def search_public_github_repositories(value: str, limit: int = 12) -> list[dict[str, object]]:
    """Find public GitHub repositories; no Svacer or project data is sent."""
    query = repository_search_query(value)
    limit = max(1, min(int(limit), 20))
    parameters = urlencode({
        # README-only matches tend to put unrelated "awesome" lists above the
        # actual project (for example for "luajit"). Name and description give
        # a much more useful repository picker.
        "q": f"{query} in:name,description fork:false archived:false",
        "sort": "stars", "order": "desc", "per_page": limit,
    })
    request = Request(
        f"https://api.github.com/search/repositories?{parameters}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Svacer-Triage/1.0"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code in {403, 429}:
            raise ValueError("GitHub временно ограничил частоту поиска. Подождите минуту и повторите.") from exc
        raise ValueError(f"GitHub не выполнил поиск (HTTP {exc.code}). Повторите позже.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("Не удалось подключиться к GitHub за 15 секунд. Проверьте сеть и повторите.") from exc
    except (json.JSONDecodeError, UnicodeError, TypeError) as exc:
        raise ValueError("GitHub вернул некорректный ответ поиска.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("GitHub вернул некорректный ответ поиска.")

    results = []
    for row in payload["items"]:
        if (not isinstance(row, dict) or row.get("private") or row.get("fork")
                or row.get("archived") or row.get("disabled")):
            continue
        full_name = str(row.get("full_name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
            continue
        candidate = str(row.get("clone_url") or "")
        try:
            candidate = repository_url(candidate)
        except ValueError:
            continue
        uri = urlsplit(candidate)
        if uri.scheme != "https" or (uri.hostname or "").lower() != "github.com":
            continue
        description = " ".join(str(row.get("description") or "").split())[:240]
        try:
            stars = max(0, int(row.get("stargazers_count") or 0))
        except (TypeError, ValueError):
            stars = 0
        results.append({
            "name": full_name, "url": candidate, "description": description,
            "stars": stars, "language": str(row.get("language") or ""),
        })
        if len(results) >= limit:
            break
    compact_query = re.sub(r"[^\w]+", "", query.casefold())
    tokens = [part for part in re.split(r"[^\w]+", query.casefold()) if part]

    def relevance(row: dict[str, object]) -> tuple[int, int, int, int]:
        name = str(row["name"]).casefold()
        repository_name = name.rsplit("/", 1)[-1]
        compact_name = re.sub(r"[^\w]+", "", repository_name)
        description = str(row["description"]).casefold()
        return (
            int(bool(compact_query) and compact_name == compact_query),
            sum(token in name for token in tokens),
            sum(token in description for token in tokens),
            int(row["stars"]),
        )

    # Exact/name matches should precede popular projects that only mention the
    # term in their description. GitHub star order remains the final tie-break.
    return sorted(results, key=relevance, reverse=True)


def parse_remote_refs(output: str) -> list[dict[str, str]]:
    refs, peeled = {}, {}
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or not COMMIT_RE.fullmatch(parts[0]):
            continue
        commit, ref = parts
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = commit.lower()
        elif ref.startswith(("refs/tags/", "refs/heads/")):
            kind = "tag" if ref.startswith("refs/tags/") else "branch"
            refs[ref] = {"ref": ref, "name": ref.split("/", 2)[2], "kind": kind, "commit": commit.lower()}
    for ref, commit in peeled.items():
        if ref in refs:
            refs[ref]["commit"] = commit
    def order(row):
        # Natural descending version order (v2.11 after v2.9), tags before branches.
        return tuple((1, int(part)) if part.isdigit() else (0, part.lower())
                     for part in re.split(r"(\d+)", row["name"]))
    return (sorted((r for r in refs.values() if r["kind"] == "tag"), key=order, reverse=True)
            + sorted((r for r in refs.values() if r["kind"] == "branch"), key=lambda r: r["name"].casefold()))


def list_remote_refs(url: str) -> list[dict[str, str]]:
    url = repository_url(url)
    git = shutil.which("git")
    if not git:
        raise ValueError("Git не найден. Установите Git и повторите загрузку версий.")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    # No hidden terminal/password prompt can hold the GUI indefinitely.
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10"
    try:
        result = subprocess.run([git, "ls-remote", "--tags", "--heads", "--", url],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30, env=env, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git не ответил за 30 секунд. Проверьте сеть и доступ к репозиторию.") from exc
    if result.returncode:
        # Git diagnostics may contain authentication details supplied by helpers.
        raise ValueError("Не удалось получить теги и ветки. Проверьте URL, сеть и доступ к репозиторию. "
                         "Для закрытого репозитория выполните вход в Git самостоятельно.")
    return parse_remote_refs(result.stdout)


def source_fields(url: str, selection: dict[str, str]) -> dict[str, str]:
    url = repository_url(url)
    kind, ref, commit = (str(selection.get(key) or "") for key in ("kind", "ref", "commit"))
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("Нужен полный commit SHA выбранной версии.")
    prefix = {"tag": "refs/tags/", "branch": "refs/heads/"}.get(kind)
    if kind == "commit":
        if ref.lower() != commit.lower():
            raise ValueError("Commit SHA не совпадает с выбранной ревизией.")
        name = commit.lower()
    elif (not prefix or not ref.startswith(prefix) or not ref[len(prefix):]
          or any(c.isspace() or c in "~^:?*[\\" or ord(c) < 32 for c in ref)
          or ".." in ref or "@{" in ref or "//" in ref or ref.endswith(("/", ".", ".lock"))):
        raise ValueError("Выберите существующий тег или ветку из списка.")
    else:
        name = ref[len(prefix):]
    return {"repository_url": url, "git_ref": name, "git_ref_full": ref,
            "git_ref_kind": kind, "git_commit": commit.lower()}


def create_project(root: Path, settings: dict, snapshot: str, url: str, selection: dict) -> Path:
    settings = complete_desktop_settings(settings)
    scope = snapshot_fields(snapshot, str(settings.get("svacer_url") or ""))
    source = source_fields(url, selection)
    if settings.get("advanced_filter") != GOST_FILTER:
        raise ValueError("В настройках нужен точный фильтр ГОСТ 71207-2024.")
    if settings.get("verification_enabled") is not True or settings.get("verification_verdicts") != ["Confirmed"]:
        raise ValueError("Для Confirmed должна быть включена независимая проверка.")
    workers = int(settings.get("parallel_workers", 1))
    verifiers = int(settings.get("verification_workers", 1))
    warning = int(settings.get("saved_context_token_warning", 200000))
    if not (1 <= workers <= 8 and 1 <= verifiers <= 8) or warning < 0:
        raise ValueError("Некорректные настройки числа агентов или лимита контекста.")
    root = root.resolve(strict=True)
    results = root / "RESULTS"
    if results.is_symlink() or (results.exists() and results.resolve() != results):
        raise ValueError("Каталог RESULTS не должен быть ссылкой.")
    results.mkdir(exist_ok=True)
    name = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    job = results / name
    job.mkdir()
    (job / "raw").mkdir()
    (job / "notes").mkdir()
    (job / "START_PROMPT.txt").write_text(
        "Automated Svacer Triage job.\n"
        f"Configuration: {job / 'job.json'}\n"
        "The Start analysis action prepares the selected revision, assigned marker traces, "
        "and a compact English runtime prompt. This file is retained for recovery.\n"
        "All analysis fields use English; only the final Svacer comment is translated to Russian.\n",
        encoding="utf-8",
    )
    # Write job.json last: the dashboard only discovers a fully initialized job.
    data = {**scope, **source, "filter_name": settings.get("filter_name") or "ГОСТ 71207-2024",
            "advanced_filter": GOST_FILTER, "parallel_workers": workers, "codex_model": "",
            "manual_selection_only": True, "verification_enabled": True,
            "verification_verdicts": ["Confirmed"], "verification_workers": verifiers,
            "saved_context_token_warning": warning, "tool_directory": str(root),
            "app_directory": str(root / "app"), "job_directory": str(job),
            "created_at": datetime.now().astimezone().isoformat()}
    atomic_write_json(job / "job.json", data)
    return job


def update_project_source(job: Path, root: Path, url: str, selection: dict) -> None:
    source = source_fields(url, selection)
    job = checked_job(job, root)
    try:
        with job_operation_lock(job), decision_lock(job / "decisions.jsonl"):
            require_idle(job)
            data = json.loads((job / "job.json").read_text(encoding="utf-8-sig"))
            if all(data.get(k) == v for k, v in source.items()):
                return
            # Never combine old evidence/verdicts with a different source tree.
            decisions = load_decisions(job / "decisions.jsonl") if (job / "decisions.jsonl").exists() else []
            used = (any(d.get("verdict") for d in decisions)
                    or any((job / "notes").glob("*.json"))
                    or any((job / "raw").glob("*.json"))
                    or any((job / name).exists() for name in ("revision.txt", "batch-context.json", "svacer-import-attempt.json")))
            if used or (job / "repository").exists():
                raise ValueError("У задачи уже есть исходники или результаты анализа. "
                                 "Для другой ревизии создайте новый проект; прежние данные сохранены.")
            backup = job / "source-settings-backups"
            backup.mkdir(exist_ok=True)
            atomic_write_json(backup / f"{uuid.uuid4().hex}.json", data)
            data.update(source)
            atomic_write_json(job / "job.json", data)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
