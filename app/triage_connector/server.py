"""First-party MCP entry point. Uses only the open MCP SDK and Public API."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
from functools import wraps
from pathlib import Path

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .client import ConnectorError, PublicAPI
from .markup import MarkupImport
from .service import SvacerService


class LocalBearer:
    def __init__(self, token: str, resource: str):
        self.token = token
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode("utf-8"), self.token.encode("utf-8")):
            return AccessToken(token=token, client_id="local-triage", scopes=[], resource=self.resource)
        return None


def build_mcp(service: SvacerService, imports: MarkupImport, *, token: str = "", port: int = 8002,
              enabled: set[str] | None = None, lifespan=None) -> FastMCP:
    resource = f"http://127.0.0.1:{port}"
    mcp = FastMCP("Svacer Triage Connector", host="127.0.0.1", port=port,
        stateless_http=True, json_response=True, lifespan=lifespan,
        token_verifier=LocalBearer(token, resource) if token else None,
        auth=AuthSettings(issuer_url=resource, resource_server_url=resource, required_scopes=[],
                          validate_token_resource=True) if token else None,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]),
    )

    async def get_markers(project_id: str, branch_id: str, snapshot_id: str,
            severity: list[str] | None = None, review: list[str] | None = None,
            warnClass: list[str] | None = None, file: list[str] | None = None,
            traces: bool = False, checker_info: bool = False, review_history: bool = False,
            comment_history: bool = False, custom_filter: str | None = None,
            advanced_filter: str | None = None, limit: int = 30, fields: list[str] | None = None) -> dict:
        """Get snapshot markers. limit=0 returns all; fields=['*'] preserves raw fields."""
        args = locals()
        args.pop("service", None)
        return await service.get_markers(**args)

    async def get_warnings(project_id: str, branch_id: str, snapshot_id: str,
            severity: list[str] | None = None, review: list[str] | None = None,
            warnClass: list[str] | None = None, file: list[str] | None = None,
            traces: bool = False, checker_info: bool = False, review_history: bool = False,
            comment_history: bool = False, custom_filter: str | None = None,
            advanced_filter: str | None = None, limit: int | None = None, fields: list[str] | None = None) -> dict:
        """Get warnings, including requested trace/review fields; limit=0 is unlimited."""
        args = locals()
        args.pop("service", None)
        return await service.get_warnings(**args)

    operations = {
        "get_projects": service.get_projects, "get_snapshots": service.get_snapshots,
        "get_markers": get_markers, "get_warnings": get_warnings,
        "get_project_stats": service.get_project_stats, "get_project_groups": service.get_project_groups,
        "get_advanced_file_preview": service.get_advanced_file_preview, "get_diff": service.get_diff,
        "prepare_markup_import": imports.prepare, "apply_markup_import": imports.apply,
    }
    if enabled is not None and (not enabled or enabled - operations.keys()):
        raise ConnectorError("SVACER_TOOLS содержит неизвестные инструменты или пуст.")

    def guarded(operation):
        @wraps(operation)
        async def invoke(*args, **kwargs) -> str:
            try:
                result = await operation(*args, **kwargs)
                return json.dumps(result, ensure_ascii=False)
            except ConnectorError as exc:
                raise ToolError(str(exc)) from None
            except (Exception, SystemExit):
                # Do not leak response bodies, local paths or credential-bearing URLs.
                raise ToolError("Операция не выполнена: неверный формат данных или локальная ошибка. Изменения не подтверждены.") from None
        # A wrapped function's return annotation describes the dict, not the wire string.
        # Disable automatic structured outputs below: existing clients read TextContent.
        return invoke

    for name, operation in operations.items():
        if enabled is None or name in enabled:
            mcp.add_tool(guarded(operation), name=name, structured_output=False,
                annotations=ToolAnnotations(readOnlyHint=name.startswith("get_"),
                    destructiveHint=name == "apply_markup_import", idempotentHint=name.startswith("get_"),
                    openWorldHint=True))
    return mcp


def main(*, transport: str = "streamable-http", bootstrap: dict | None = None,
         startup_report=None) -> None:
    password = ""
    try:
        if bootstrap is None:
            url = os.environ.get("SVACER_URL", "")
            login = os.environ.pop("SVACER_LOGIN", "")
            password = os.environ.pop("SVACER_PASSWORD", "")
            token = os.environ.pop("SVACER_MCP_TOKEN", "")
            port = int(os.environ.get("SVACER_HTTP_PORT", "8002"))
            root = Path(os.environ.get("SVACER_TRIAGE_ROOT") or Path(__file__).resolve().parents[2])
        else:
            url, login, password, token = (bootstrap.pop(key) for key in ("url", "login", "password", "token"))
            port, root = int(bootstrap.pop("port")), Path(bootstrap.pop("root"))
            bootstrap.clear()
            if not all(isinstance(value, str) for value in (url, login, password, token)):
                raise ConnectorError("Некорректные данные подключения.")
        if not login or not password:
            raise ConnectorError("Войдите в Svacer через окно подключения приложения.")
        if transport != "stdio" and len(token) < 32:
            raise ConnectorError("Для HTTP нужен локальный SVACER_MCP_TOKEN (не менее 32 символов).")
        if not 1 <= port <= 65535:
            raise ConnectorError("Некорректный SVACER_HTTP_PORT.")
        api = PublicAPI(url, ca_file=os.environ.get("SVACER_CA_FILE") or None)
        service = SvacerService(api)
        imports = MarkupImport(service, root, login)

        async def serve():
            nonlocal password
            try:
                await asyncio.wait_for(api.login(
                    login, password, auth_type=os.environ.get("SVACER_AUTH_TYPE", ""),
                    server=os.environ.get("SVACER_LDAP_SERVER", "")), timeout=30 if startup_report else 120)
                password = ""
                if startup_report:
                    startup_report("authenticated")
                # FastMCP's protocol lifespan is per HTTP request in stateless mode.
                # The shared HTTP/auth pool must live for the entire OS process instead.
                if transport == "stdio":
                    await server.run_stdio_async()
                else:
                    await server.run_streamable_http_async()
            finally:
                password = ""
                await api.close()

        enabled = os.environ.get("SVACER_TOOLS")
        server = build_mcp(service, imports, token=token, port=port,
            enabled={part.strip() for part in enabled.split(",")} if enabled else None)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except ConnectorError as exc:
        if startup_report:
            detail = str(exc)
            startup_report("unauthorized" if "HTTP 401" in detail else
                           "forbidden" if "HTTP 403" in detail else "connection_failed")
        else:
            print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        if startup_report:
            startup_report("failed")
        else:
            print("Коннектор не запущен. Проверьте настройки, зависимости и доступность Svacer.", file=sys.stderr)
        raise SystemExit(2) from None
    finally:
        password = ""
        if bootstrap is not None:
            bootstrap.clear()


if __name__ == "__main__":
    main(transport=os.environ.get("SVACER_TRANSPORT", "stdio"))
