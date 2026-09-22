"""HTTP transport for the documented Svacer Public API.

Requests are restricted to one configured origin. Authentication and remote error
bodies never appear in exceptions. Only explicitly read-only operations retry.
"""
from __future__ import annotations

import asyncio
import json
import re
import ssl
from urllib.parse import urlsplit, urlunsplit

import httpx


class ConnectorError(RuntimeError):
    pass


def server_url(value: str) -> str:
    """Accept a server base or a copied Svacer UI link, retaining proxy prefixes."""
    value = value.strip()
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed or out-of-range ports before any request.
    except ValueError:
        raise ConnectorError("Некорректный адрес сервера Svacer.") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ConnectorError("SVACER_URL должен быть HTTP(S)-адресом без учётных данных.")
    path = parsed.path.rstrip("/")
    # A copied project/branch/snapshot URL is not an API base. Only remove
    # recognizable Svacer routes; /svacer and other reverse-proxy bases survive.
    route = re.search(
        r"/mode/review(?=/|$)|/project/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}"
        r"-[0-9a-fA-F]{12}(?=/|$)|/api/public(?=/|$)", path,
    )
    if route:
        path = path[:route.start()].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class PublicAPI:
    def __init__(self, url: str, *, ca_file: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = server_url(url)
        verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
        self.http = httpx.AsyncClient(
            base_url=self.url + "/", follow_redirects=False, verify=verify,
            timeout=httpx.Timeout(connect=8, read=120, write=30, pool=8),
            transport=transport,
        )
        self._credentials = None
        self._auth_generation = 0
        self._auth_lock = asyncio.Lock()

    async def close(self) -> None:
        self._credentials = None
        self.http.headers.pop("Authorization", None)
        await self.http.aclose()

    async def login(self, login: str, password: str, *, auth_type: str = "", server: str = "") -> None:
        body = {"login": login, "password": password}
        if auth_type:
            body.update(auth_type=auth_type.lower(), server=server)
        try:
            result = await self.json("POST", "/api/public/login", json=body)
        finally:
            body.clear()
        token = result.get("token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise ConnectorError("Svacer не вернул корректный токен входа.")
        self.http.headers["Authorization"] = "Bearer " + token
        # Retained only in process memory for long-running read sessions, never on disk.
        self._credentials = (login, password, auth_type, server)
        self._auth_generation += 1

    async def request(self, method: str, path: str, *, read_only: bool = False, **kwargs) -> httpx.Response:
        if not path.startswith("/api/public/") or ".." in path or "?" in path or "#" in path:
            raise ConnectorError("Недопустимый путь Public API.")
        attempts = 3 if read_only else 1
        attempt = 0
        refreshed = False
        while attempt < attempts:
            generation = self._auth_generation
            try:
                response = await self.http.request(method, path.lstrip("/"), **kwargs)
            except httpx.HTTPError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    attempt += 1
                    continue
                raise ConnectorError("Svacer API: соединение прервано или истекло время ожидания.") from None
            if response.status_code == 401 and read_only and not refreshed and self._credentials:
                async with self._auth_lock:
                    if generation == self._auth_generation:
                        login, password, auth_type, server = self._credentials
                        await self.login(login, password, auth_type=auth_type, server=server)
                refreshed = True
                continue
            if response.status_code in {429, 502, 503, 504} and attempt + 1 < attempts:
                await asyncio.sleep(0.3 * (attempt + 1))
                attempt += 1
                continue
            if not 200 <= response.status_code < 300:
                hints = {401: "Выполните вход в Svacer заново.",
                         403: "У пользователя нет прав на эту операцию Public API.",
                         404: "Запрошенный ресурс или API отсутствует.",
                         400: "Svacer отклонил параметры запроса."}
                raise ConnectorError(f"Svacer API HTTP {response.status_code}. "
                                     + hints.get(response.status_code, "Запрос не выполнен."))
            return response
        raise AssertionError("Unreachable")

    async def json(self, method: str, path: str, **kwargs):
        response = await self.request(method, path, **kwargs)
        if response.status_code == 204:
            return []
        try:
            return response.json()
        except (ValueError, UnicodeError):
            raise ConnectorError("Svacer API вернул некорректный JSON.") from None

    async def json_lines(self, path: str, *, read_only: bool = False, **kwargs) -> list[dict]:
        response = await self.request("POST", path, read_only=read_only, **kwargs)
        try:
            rows = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        except (ValueError, UnicodeError):
            raise ConnectorError("Svacer API вернул некорректную разметку JSONL.") from None
        if not all(isinstance(row, dict) for row in rows):
            raise ConnectorError("Связанные записи разметки имеют неверный формат.")
        return rows
