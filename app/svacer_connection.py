"""Hidden connector startup. Credentials cross an anonymous pipe, never argv/files."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from codex_run import hidden_subprocess_kwargs, windowless_python_executable
from triage_connector.client import server_url


class LoginError(RuntimeError):
    pass


class LoginCancelled(LoginError):
    pass


STARTUP_ERRORS = {
    "unauthorized": "Не удалось войти. Проверьте логин и пароль.",
    "forbidden": "Svacer отклонил вход (HTTP 403). Проверьте доступ к серверу и права пользователя.",
    "connection_failed": "Svacer не ответил или отклонил запрос. Проверьте сеть/VPN и адрес сервера.",
    "failed": "Не удалось запустить подключение. Проверьте настройки и установленные зависимости.",
}


def read_local_mcp_token() -> str:
    """Read the app-generated loopback token without printing or persisting it."""
    token = os.getenv("SVACER_LOCAL_MCP_TOKEN", "")
    if token or os.name != "nt":
        return token
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, "SVACER_LOCAL_MCP_TOKEN")
        return value if isinstance(value, str) else ""
    except (OSError, ImportError):
        return ""


def local_port(mcp_url: str) -> int:
    try:
        parsed = urlsplit(mcp_url)
        port = parsed.port if parsed.port is not None else 80
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                 and not parsed.username and not parsed.password and not parsed.query
                 and not parsed.fragment and parsed.path.rstrip("/") == "/mcp"
                 and 1 <= port <= 65535)
    except ValueError:
        valid = False
    if not valid:
        raise LoginError("Для локального подключения нужен адрес http://127.0.0.1:порт/mcp.")
    return port


def port_available(port: int) -> bool:
    """Return whether a loopback listener can be created without touching its owner."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def release_stale_owned_connector(app_directory: Path, port: int) -> bool:
    """Stop an old Svacer connector, including one launched from another clone.

    The PowerShell helper verifies the listener executable and its dedicated
    entrypoint/flag before stopping it.  Unknown owners are left untouched.
    """
    if os.name != "nt":
        return False
    script = app_directory / "stop_components.ps1"
    if not script.is_file():
        return False
    try:
        completed = subprocess.run(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Mode", "Svacer", "-NoElevation", "-Port", str(port),
            ],
            cwd=str(app_directory), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=20, **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and port_available(port)


def stop_owned_process(process: subprocess.Popen) -> None:
    """Stop exactly the child we created, not a process identified by a reused PID."""
    if process.poll() is None:
        if os.name == "nt":
            # Windows venv python.exe may be a redirector with the interpreter as a child.
            # Keep the Popen handle alive while stopping this exact process tree.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                           **hidden_subprocess_kwargs())
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


async def _ready(mcp_url: str, token: str) -> bool:
    from triage_dashboard import list_mcp_tools
    try:
        names = await asyncio.wait_for(list_mcp_tools(mcp_url, token), timeout=1.5)
        return {"get_projects", "get_markers", "apply_markup_import"}.issubset(names)
    except Exception:
        return False


def start_connection(app_directory: Path, url: str, mcp_url: str, token: str,
                     login: str, password: str, cancel: threading.Event,
                     *, timeout: float = 40,
                     root_directory: Path | None = None) -> subprocess.Popen:
    """Run off the UI thread; success transfers ownership of the background process."""
    process = None
    success = False
    payload = b""
    try:
        try:
            url = server_url(url)
        except Exception:
            raise LoginError("В настройках нужен адрес Svacer без пароля, query-параметров и fragment.") from None
        port = local_port(mcp_url)
        if len(token) < 32:
            raise LoginError("Локальное подключение не настроено. Закройте приложение и откройте START — установка выполнится автоматически.")
        if not login.strip() or not password:
            raise LoginError("Введите логин и пароль.")
        if cancel.is_set():
            raise LoginCancelled("Вход отменён.")
        if not port_available(port) and not release_stale_owned_connector(app_directory, port):
            raise LoginError(
                "Локальный порт занят другой программой. Старое подключение Svacer "
                "освобождается автоматически; посторонний процесс не был остановлен."
            )
        script = app_directory / "start_svacer_http.py"
        if not script.is_file():
            raise LoginError("Не найден компонент подключения. Восстановите установку приложения.")
        payload = (json.dumps({"url": url, "port": port,
                              "root": str((root_directory or app_directory.parent).resolve()),
                              "login": login.strip(), "password": password, "token": token},
                             ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > 65536:
            raise LoginError("Слишком длинные данные входа.")
        login = password = ""
        env = {key: value for key, value in os.environ.items() if key.upper() not in {
            "SVACER_LOGIN", "SVACER_PASSWORD", "SVACER_MCP_TOKEN", "SVACER_LOCAL_MCP_TOKEN",
        }}
        env["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            [windowless_python_executable(sys.executable), str(script), "--login-stdin"],
            cwd=str(app_directory), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, **hidden_subprocess_kwargs(new_process_group=True),
        )
        process.stdin.write(payload)
        process.stdin.close()
        payload = b""
        responses: queue.Queue[bytes] = queue.Queue(maxsize=1)

        def receive():
            try:
                responses.put(process.stdout.readline(128))
            except (OSError, ValueError):
                responses.put(b"")

        reader = threading.Thread(target=receive, name="svacer-startup", daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        authenticated = False
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise LoginCancelled("Вход отменён.")
            if not authenticated:
                try:
                    status = responses.get(timeout=.1).decode("ascii", errors="replace").strip()
                except queue.Empty:
                    continue
                if status != "authenticated":
                    raise LoginError(STARTUP_ERRORS.get(status, STARTUP_ERRORS["failed"]))
                authenticated = True
            if process.poll() is not None:
                raise LoginError(STARTUP_ERRORS["failed"])
            if asyncio.run(_ready(mcp_url, token)) and process.poll() is None:
                if cancel.is_set():
                    raise LoginCancelled("Вход отменён.")
                process.stdout.close()
                success = True
                return process
            cancel.wait(.15)
        raise LoginError("Время подключения истекло. Проверьте сеть/VPN и попробуйте снова.")
    except LoginError:
        raise
    except Exception:
        # Do not surface exception text, request bodies or process output in the UI/logs.
        raise LoginError(STARTUP_ERRORS["failed"]) from None
    finally:
        login = password = ""
        payload = b""
        if process is not None and not success:
            try:
                stop_owned_process(process)
            except Exception:
                raise LoginError("Не удалось завершить отменённое подключение. "
                                 "Остановите локальный Svacer MCP через служебное меню.") from None
