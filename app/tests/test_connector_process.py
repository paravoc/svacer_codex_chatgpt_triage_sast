"""Launch the real connector against an isolated local fake REST server."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


@pytest.fixture
def rest_server():
    events = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            events.append(self.path)
            assert self.path == "/api/public/login"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert data == {"login": "fixture", "password": "synthetic"}
            self.respond({"token": "synthetic-api-token"})

        def do_GET(self):
            events.append(self.path)
            assert self.path == "/api/public/projects"
            assert self.headers["Authorization"] == "Bearer synthetic-api-token"
            self.respond([{"project": {"id": "00000000-0000-4000-8000-000000000001", "name": "fixture"},
                           "branches": []}])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def environment(url, tmp_path):
    # No inherited Svacer settings/credentials, .env, or original editable package.
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC"}}
    env.update(SVACER_URL=url, SVACER_LOGIN="fixture", SVACER_PASSWORD="synthetic",
               SVACER_MCP_TOKEN="x" * 32, SVACER_TRIAGE_ROOT=str(tmp_path), PYTHONUTF8="1")
    return env


async def verify_session(session):
    await session.initialize()
    listed = await session.list_tools()
    assert len(listed.tools) == 10
    for _ in range(3):
        result = await session.call_tool("get_projects", {})
        assert not result.isError
        assert json.loads(result.content[0].text)[0]["project_name"] == "fixture"


def test_real_http_entrypoint_reuses_one_authenticated_pool(rest_server, tmp_path):
    url, events = rest_server
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = Path(os.environ.get("TRIAGE_TEST_APP") or Path(__file__).resolve().parents[1])
    env = environment(url, tmp_path)
    env["SVACER_HTTP_PORT"] = str(port)
    process = subprocess.Popen([sys.executable, str(app / "start_svacer_http.py")], env=env,
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode("utf-8", errors="replace"))
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail("Fixture MCP did not start")

        async def scenario():
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + "x" * 32}) as client:
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await verify_session(session)
        asyncio.run(scenario())
        assert events.count("/api/public/login") == 1
        assert events.count("/api/public/projects") == 3
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_real_stdio_entrypoint(rest_server, tmp_path):
    url, events = rest_server
    app = Path(os.environ.get("TRIAGE_TEST_APP") or Path(__file__).resolve().parents[1])
    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=[str(app / "start_svacer_stdio.py")],
                                       env=environment(url, tmp_path), cwd=str(tmp_path))
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await verify_session(session)
    asyncio.run(scenario())
    assert events.count("/api/public/login") == 1 and events.count("/api/public/projects") == 3
