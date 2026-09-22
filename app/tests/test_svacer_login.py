"""Login UI and real hidden processes; synthetic credentials and loopback servers only."""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import svacer_connection as connection
import svacer_login_app as login_app
import svacer_login_qt as login_ui
import triage_gui_qt as ui
import triage_queue as queue


@pytest.fixture
def fake_svacer():
    state = {"code": 200, "calls": [], "entered": threading.Event(), "release": threading.Event(), "wait": False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state["calls"].append(self.path)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/api/public/login"
            assert body == {"login": "тест", "password": "dummy-password"}
            state["entered"].set()
            if state["wait"]:
                state["release"].wait(10)
            # Deliberately malicious error text must never be surfaced by the login UI.
            response = json.dumps({"token": "dummy-api-token"} if state["code"] == 200 else
                                  {"detail": "SECRET-ECHO dummy-password"}).encode()
            self.send_response(state["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except (ConnectionError, OSError):
                pass
    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass  # expected connection resets during cancellation tests
    server = Server(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        state["release"].set()
        server.shutdown()
        server.server_close()
        worker.join(3)


@pytest.fixture
def local_endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"


@pytest.fixture
def clean_environment(monkeypatch):
    # The subprocess must not pick up actual credentials, auth providers or tool restrictions.
    for key in tuple(os.environ):
        if key.upper().startswith("SVACER_"):
            monkeypatch.delenv(key)


@pytest.fixture
def captured_launch(monkeypatch, clean_environment):
    launches = []
    real_popen = subprocess.Popen
    def launch(command, **kwargs):
        if "--login-stdin" in command:
            assert "dummy-password" not in str(command) and "z" * 32 not in str(command)
            if os.name == "nt":
                assert Path(command[0]).name.casefold() == "pythonw.exe"
            assert kwargs["stderr"] == subprocess.DEVNULL
            assert not any(key in kwargs["env"] for key in (
                "SVACER_LOGIN", "SVACER_PASSWORD", "SVACER_MCP_TOKEN", "SVACER_LOCAL_MCP_TOKEN"))
            if os.name == "nt":
                assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
                assert not kwargs["creationflags"] & subprocess.CREATE_NEW_CONSOLE
        process = real_popen(command, **kwargs)
        if "--login-stdin" in command:
            launches.append(process)
        return process
    monkeypatch.setattr(connection.subprocess, "Popen", launch)
    yield launches
    for process in launches:
        connection.stop_owned_process(process)


def start(url, endpoint, cancel=None, **kwargs):
    return connection.start_connection(Path(__file__).resolve().parents[1], url, endpoint,
                                       "z" * 32, "тест", "dummy-password", cancel or threading.Event(), **kwargs)


@pytest.mark.parametrize("suffix", ["", "/mode/review/project/00000000-0000-4000-8000-000000000001"])
def test_real_hidden_login_and_background_lifetime(fake_svacer, local_endpoint, captured_launch, suffix):
    url, state = fake_svacer
    process = start(url + suffix, local_endpoint)
    assert process.poll() is None and process is captured_launch[0]
    assert asyncio.run(connection._ready(local_endpoint, "z" * 32))
    assert not asyncio.run(connection._ready(local_endpoint, "wrong-token"))
    assert state["calls"] == ["/api/public/login"]  # no markup or even project reads
    connection.stop_owned_process(process)
    assert process.poll() is not None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", connection.local_port(local_endpoint)))


@pytest.mark.skipif(os.name != "nt", reason="Windows hidden disconnect integration")
def test_real_disconnect_stops_only_test_connector(fake_svacer, local_endpoint, captured_launch):
    from triage_dashboard import stop_svacer_connection
    url, state = fake_svacer
    process = start(url, local_endpoint)
    # This is a random test port, not the live application's 8002.
    stop_svacer_connection(Path(__file__).resolve().parents[1], connection.local_port(local_endpoint))
    process.wait(timeout=5)
    assert state["calls"] == ["/api/public/login"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", connection.local_port(local_endpoint)))


@pytest.mark.skipif(os.name != "nt", reason="Windows stale-clone connector integration")
def test_relogin_releases_connector_started_from_another_clone(
        fake_svacer, local_endpoint, captured_launch, tmp_path, monkeypatch):
    source_app = Path(__file__).resolve().parents[1]
    old_app = tmp_path / "old-clone" / "app"
    old_app.mkdir(parents=True)
    shutil.copy2(source_app / "start_svacer_http.py", old_app / "start_svacer_http.py")
    current_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH", str(source_app) + (os.pathsep + current_pythonpath if current_pythonpath else ""),
    )
    url, _state = fake_svacer
    process = connection.start_connection(
        old_app, url, local_endpoint, "z" * 32, "тест", "dummy-password",
        threading.Event(), root_directory=source_app.parent,
    )
    assert process.poll() is None
    port = connection.local_port(local_endpoint)
    assert connection.release_stale_owned_connector(source_app, port)
    process.wait(timeout=5)
    assert connection.port_available(port)


@pytest.mark.parametrize("code, phrase", [(401, "логин и пароль"), (403, "HTTP 403"), (503, "не ответил")])
def test_real_rejected_login_is_safe_and_reaped(fake_svacer, local_endpoint, captured_launch, code, phrase):
    url, state = fake_svacer
    state["code"] = code
    with pytest.raises(connection.LoginError, match=phrase) as caught:
        start(url, local_endpoint)
    assert "dummy-password" not in str(caught.value) and "SECRET-ECHO" not in str(caught.value)
    assert all(process.poll() is not None for process in captured_launch)


def test_real_cancel_during_login_leaves_no_child(fake_svacer, local_endpoint, captured_launch):
    url, state = fake_svacer
    state["wait"] = True
    cancel = threading.Event()
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(start, url, local_endpoint, cancel)
        assert state["entered"].wait(10)
        cancel.set()
        with pytest.raises(connection.LoginCancelled):
            future.result(timeout=8)
    assert all(process.poll() is not None for process in captured_launch)
    state["release"].set()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", connection.local_port(local_endpoint)))


def test_real_startup_timeout_is_bounded(fake_svacer, local_endpoint, captured_launch):
    url, state = fake_svacer
    state["wait"] = True
    with pytest.raises(connection.LoginError, match="Время подключения"):
        start(url, local_endpoint, timeout=.5)
    assert all(process.poll() is not None for process in captured_launch)


def test_busy_unknown_port_is_not_killed_and_credentials_not_sent(
        fake_svacer, captured_launch, monkeypatch):
    url, state = fake_svacer
    cleanup_attempts = []
    monkeypatch.setattr(
        connection, "release_stale_owned_connector",
        lambda _app, port: cleanup_attempts.append(port) or False,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        with pytest.raises(connection.LoginError, match="порт занят"):
            start(url, endpoint)
        assert not captured_launch and not state["calls"]
        assert cleanup_attempts == [sock.getsockname()[1]]


def test_stale_connector_cleanup_is_hidden_and_bounded(tmp_path, monkeypatch):
    (tmp_path / "stop_components.ps1").touch()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(connection.os, "name", "nt")
    monkeypatch.setattr(connection.subprocess, "run", run)
    monkeypatch.setattr(connection, "port_available", lambda port: port == 18765)
    assert connection.release_stale_owned_connector(tmp_path, 18765)
    command, kwargs = calls[0]
    assert command[-5:] == ["-Mode", "Svacer", "-NoElevation", "-Port", "18765"]
    assert "-NonInteractive" in command
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 20


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:8002/mcp", "http://example.test/mcp",
                                      "http://user:password@127.0.0.1/mcp", "http://127.0.0.1:0/no",
                                      "http://127.0.0.1:not-a-port/mcp"])
def test_only_loopback_connector_allowed(endpoint):
    with pytest.raises(connection.LoginError):
        connection.local_port(endpoint)


@pytest.fixture
def dialog(monkeypatch, tmp_path):
    app = ui.QApplication.instance() or ui.QApplication([])
    widget = login_ui.SvacerLoginDialog(None, tmp_path, "https://svacer.example.test",
                                      "http://127.0.0.1:8002/mcp", "z" * 32, button_factory=ui.button)
    widget.setStyleSheet(ui.STYLE)
    try:
        widget.show()
        app.processEvents()
        yield widget, app
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()
        app.sendPostedEvents(None, ui.QEvent.Type.DeferredDelete)
        app.processEvents()


def fill(widget):
    widget.login_edit.setText("тест")
    widget.password_edit.setText("dummy-password")


def spin(app, condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        app.processEvents()
        time.sleep(.01)
    assert condition()


def test_dialog_mask_validation_and_cancel_clears_inputs(dialog):
    widget, app = dialog
    assert widget.password_edit.echoMode() == ui.QLineEdit.EchoMode.Password
    assert widget.server_edit.text() == "https://svacer.example.test"
    assert not widget.submit_button.isEnabled()
    fill(widget)
    assert widget.submit_button.isEnabled()
    widget.reject()
    assert not widget.password_edit.text() and not widget.login_edit.text()


@pytest.mark.parametrize("suffix", ["/", "/mode/review/project/00000000-0000-4000-8000-000000000001"])
def test_dialog_accepts_and_saves_server_on_first_run(tmp_path, monkeypatch, suffix):
    app = ui.QApplication.instance() or ui.QApplication([])
    widget = login_ui.SvacerLoginDialog(
        None, tmp_path, "", "http://127.0.0.1:8002/mcp", "z" * 32,
        button_factory=ui.button,
    )
    calls = []
    monkeypatch.setattr(login_ui, "start_connection", lambda *args: calls.append(args) or object())
    try:
        widget.server_edit.setText("https://svacer.example.test" + suffix)
        fill(widget)
        assert widget.submit_button.isEnabled()
        widget.submit()
        spin(app, lambda: widget._closed)
        saved = json.loads((tmp_path / "svacer-settings.json").read_text(encoding="utf-8"))
        assert saved == {
            "advanced_filter": queue.GOST_FILTER,
            "filter_name": "ГОСТ 71207-2024",
            "mcp_url": "http://127.0.0.1:8002/mcp",
            "parallel_workers": 1,
            "saved_context_token_warning": 200000,
            "svacer_url": "https://svacer.example.test",
            "verification_enabled": True,
            "verification_verdicts": ["Confirmed"],
            "verification_workers": 1,
        }
        assert widget.server_url == "https://svacer.example.test"
        assert calls[0][1] == "https://svacer.example.test"
        assert calls and calls[0][4:6] == ("тест", "dummy-password")
    finally:
        widget.shutdown()
        widget.close()
        widget.deleteLater()
        app.sendPostedEvents(None, ui.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_standalone_login_accepts_fresh_clone_without_settings(tmp_path):
    assert login_app.connection_settings(tmp_path) == ("", login_app.DEFAULT_MCP_URL)
    (tmp_path / "svacer-settings.json").write_text(
        json.dumps({"svacer_url": "https://svacer.example.test", "mcp_url": "bad"}),
        encoding="utf-8",
    )
    assert login_app.connection_settings(tmp_path) == (
        "https://svacer.example.test", login_app.DEFAULT_MCP_URL,
    )


def test_dialog_background_success_no_duplicate_submit(dialog, monkeypatch):
    widget, app = dialog
    calls, release = [], threading.Event()
    def work(*args):
        calls.append(args[4:6])
        release.wait(3)
        return object()
    monkeypatch.setattr(login_ui, "start_connection", work)
    fill(widget)
    try:
        widget.submit()
        widget.submit()
        assert not widget.password_edit.text() and not widget.submit_button.isEnabled()
        assert widget.progress.isVisible()
        release.set()
        spin(app, lambda: widget._closed)
        assert widget.result() == ui.QDialog.DialogCode.Accepted
        assert calls == [("тест", "dummy-password")]
    finally:
        release.set()


@pytest.mark.parametrize("known", [True, False])
def test_dialog_error_allows_retry_without_leaking_unknown_exception(dialog, monkeypatch, known):
    widget, app = dialog
    def fail(*_args):
        if known:
            raise connection.LoginError("Проверьте логин и пароль.")
        raise ValueError("SECRET-ECHO dummy-password")
    monkeypatch.setattr(login_ui, "start_connection", fail)
    fill(widget)
    widget.submit()
    spin(app, lambda: widget.future is None)
    assert "SECRET" not in widget.status.text() and "dummy-password" not in widget.status.text()
    assert not widget.password_edit.text() and widget.password_edit.isEnabled()
    assert not widget.submit_button.isEnabled()
    widget.password_edit.setText("new-dummy-password")
    assert widget.submit_button.isEnabled()


def test_dialog_cancel_after_worker_success_reaps_unclaimed_process(dialog, monkeypatch):
    widget, app = dialog
    stopped, process = [], object()
    widget.future = Future()
    widget.future.set_result(process)
    monkeypatch.setattr(login_ui, "stop_owned_process", stopped.append)
    widget.reject()
    widget.timer.start()
    spin(app, lambda: widget._closed)
    assert stopped == [process]
    assert widget.result() == ui.QDialog.DialogCode.Rejected


def test_dialog_close_during_login_cancels_without_blocking_ui(dialog, monkeypatch):
    widget, app = dialog
    entered = threading.Event()
    def work(*args):
        entered.set()
        assert args[-1].wait(3)
        raise connection.LoginCancelled("Вход отменён.")
    monkeypatch.setattr(login_ui, "start_connection", work)
    fill(widget)
    widget.submit()
    assert entered.wait(1)
    widget.close()
    assert widget.cancel.is_set()
    spin(app, lambda: widget._closed)
    assert not widget.password_edit.text()


def test_dialog_geometry_keeps_controls_visible(dialog):
    widget, app = dialog
    widget.resize(540, 550)
    app.processEvents()
    assert widget.height() < 650
    for control in (widget.submit_button, widget.cancel_button, widget.password_edit):
        top_left = control.mapTo(widget, control.rect().topLeft())
        bottom_right = control.mapTo(widget, control.rect().bottomRight())
        assert widget.rect().contains(top_left) and widget.rect().contains(bottom_right)
    assert widget.password_edit.height() >= 40 and widget.login_edit.height() >= 40


def test_disconnect_is_hidden_bounded_and_uses_configured_port(tmp_path, monkeypatch):
    import triage_dashboard as dashboard
    (tmp_path / "stop_components.ps1").touch()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 30
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        if os.name == "nt":
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(dashboard.subprocess, "run", run)
    dashboard.stop_svacer_connection(tmp_path, 18765)
    assert calls[0][-3:] == ["-NoElevation", "-Port", "18765"]


def test_stop_script_does_not_terminate_gui_or_arbitrary_python():
    script = (Path(__file__).resolve().parents[1] / "stop_components.ps1").read_text(encoding="utf-8-sig")
    assert "$rootId = $parentId" not in script
    assert "Stop-Process -Id $listenerId -Force -ErrorAction Stop" in script
    assert 'Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $listenerId"' in script
    assert "Stop-ProcessTree" not in script
    assert "$commandLine -match $entrypointPattern" in script
    assert "start_svacer_http\\.py" in script and "--login-stdin" in script
    assert "Join-Path $PSScriptRoot \"start_svacer_http.py\"" not in script
    assert "-not $NoElevation" in script
