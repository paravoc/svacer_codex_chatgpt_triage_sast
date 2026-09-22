"""Isolated first-party Public API/MCP contracts; no live credentials or writes."""
import asyncio
import base64
import gzip
import json
import sys
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import triage_queue as q
from triage_connector.client import ConnectorError, PublicAPI, server_url
from triage_connector.markup import MarkupImport, canonical
from triage_connector.server import LocalBearer, build_mcp
from triage_connector.service import SvacerService, envelope

P, B, S = [f"00000000-0000-4000-8000-{i:012}" for i in range(1, 4)]
URL = "https://svacer.example.test"


@pytest.mark.parametrize("suffix, prefix", [
    ("/", ""),
    (f"/mode/review/project/{P}/branch/{B}/snapshot/{S}", ""),
    (f"/project/{P}/branch/{B}", ""),
    (f"/svacer/mode/review/project/{P}", "/svacer"),
    ("/svacer/", "/svacer"),
    ("/svacer/api/public/login", "/svacer"),
])
def test_login_url_accepts_snapshot_links_and_preserves_proxy_prefix(suffix, prefix):
    async def scenario():
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"token": "synthetic-token"})
        api = PublicAPI("  " + URL + suffix + "  ", transport=httpx.MockTransport(respond))
        try:
            await api.login("fixture", "synthetic")
            assert api.url == URL + prefix
            assert str(requests[0].url) == URL + prefix + "/api/public/login"
        finally:
            await api.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [
    "https://user:password@svacer.example.test/mode/review/project/123",
    "https://svacer.example.test/mode/review?token=synthetic",
    "https://svacer.example.test:bad", "https://svacer.example.test:99999",
])
def test_copied_server_link_still_rejects_credentials_and_invalid_ports(value):
    with pytest.raises(ConnectorError):
        server_url(value)


class FakeAPI:
    def __init__(self):
        self.markers = [{"id": "m1", "invariant": "inv1", "warnClass": "DEREF", "file": "/src/main.go",
                         "line": 5, "checker_labels": ["ГОСТ 71207-2024"], "traces": [],
                         "review": {"status": "Undecided"}, "msg": "example"}]
        self.exported = [{"invariant": "inv1", "review_data": {}, "comments": [],
                          "locations": [{"warnClass": "DEREF", "file": "/src/main.go", "line": 5,
                                         "marker_hash": "cHVibGljLXNjaGVtYS1maXh0dXJl"}]}]
        self.requests = []
        self.sent = []
        self.fail_import = False
        self.ignore_import = False
        self.bad_filter = None

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api/public/login":
            return httpx.Response(200, json={"token": "synthetic-token"})
        if path == "/api/public/afilters/apply":
            assert json.loads(request.content) == {"filter": q.GOST_FILTER, "snapshot_id": [S]}
            return httpx.Response(200, json=self.bad_filter if self.bad_filter is not None else
                                  {"marker_ids": [m["id"] for m in self.markers]})
        if path.endswith("/fullmarkers"):
            assert f"/{P}/branch/{B}/snapshots/{S}/" in path
            return httpx.Response(200, json=self.markers)
        if path == "/api/public/markup/export":
            body = json.loads(request.content)
            assert body["source_id"] == B and body["filters"] == [{"ids": [S]}]
            assert body["export_all"] and not body["compressed"]
            return httpx.Response(200, content=b"\n".join(canonical(r) for r in self.exported))
        if path == "/api/public/markup/import":
            assert request.url.params["target_id"] == B
            assert request.url.params["overwrite"] in {"none", "force"}
            assert request.url.params["compressed"] == "true"
            msg = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
            parts = list(msg.iter_parts())
            assert len(parts) == 1 and parts[0].get_param("name", header="content-disposition") == "file"
            data = gzip.decompress(parts[0].get_payload(decode=True))
            rows = [json.loads(line) for line in data.splitlines()]
            self.sent.append(rows)
            if self.fail_import:
                raise httpx.ReadTimeout("secret-backend-response", request=request)
            if not self.ignore_import:
                self.exported = rows
            return httpx.Response(200, json={"total": len(rows), "applied_reviews": len(rows),
                                           "applied_comments": len(rows)})
        raise AssertionError(f"Unexpected fixture endpoint {path}")


def make_service(fake):
    api = PublicAPI(URL, transport=httpx.MockTransport(fake.handle))
    return SvacerService(api)


def make_job(tmp_path, fake, verdict="False Positive"):
    job = tmp_path / "RESULTS" / "sample"
    job.mkdir(parents=True)
    q.atomic_write_json(job / "job.json", {"snapshot_url": URL + f"/project/{P}/branch/{B}/snapshot/{S}",
                                          "project_id": P, "branch_id": B, "snapshot_id": S})
    q.atomic_write_json(job / "markers.inventory.json", envelope(fake.markers, 0, ["*"],
                                                               filters={"advanced_filter": q.GOST_FILTER}))
    decisions = []
    for m in fake.markers:
        d = {"schema_version": 2, "decision_policy_version": 2,
             "marker_id": m["id"], **{k: m[k] for k in ("warnClass", "file", "line")},
             "verdict": verdict, "confidence": "high", "source": "input", "control": "guard", "sink": "read",
             "entrypoint": "entry", "build_reachability": "enabled", "product_reachability": "path checked",
             "impact": "proved", "evidence": ["main.go:5"], "counterevidence": ["guard"], "proof_gaps": [],
             "reachable_path": ["entry -> read"], "comment": "Guard excludes the invalid state.",
             "boundary": {"product_surface": "entry", "source_trust": "untrusted",
                          "policy_basis": "source contract", "boundary_crossed": False},
             "component_defect_proven": verdict != "False Positive", "product_defect_reachable": verdict == "Confirmed",
             "defect_scope": {"False Positive": "none", "Won't fix": "component", "Confirmed": "product", "Unclear": "unknown"}[verdict]}
        if verdict == "Unclear":
            d.update(component_defect_proven=None, product_defect_reachable=None,
                     proof_gaps=["Stored legacy decision explicitly marked Unclear by reviewer."])
        if verdict == "Won't fix":
            d["disposition_reason"] = "Defective optional API excluded by this product build."
        if verdict == "Confirmed":
            d.update(severity="Major", action="Fix required", verification={"status": "verified"})
        decisions.append(d)
    q.atomic_write_jsonl(job / "decisions.jsonl", decisions)
    return job


def run(coro):
    return asyncio.run(coro)


def test_full_inventory_exact_filter_and_fields_preserved():
    fake = FakeAPI()
    service = make_service(fake)
    result = run(service.get_markers(P, B, S, limit=0, fields=["*"], advanced_filter=q.GOST_FILTER, traces=True))
    assert result["markers"] == fake.markers
    assert result["total_count"] == result["returned_count"] == 1 and not result["truncated"]
    assert result["filters_applied"]["advanced_filter"] == q.GOST_FILTER
    assert fake.requests[-1].url.params["traces"] == "true"


@pytest.mark.parametrize("bad", [{}, {"errors": "unsupported", "marker_ids": []}, {"marker_ids": "all"},
                                 {"marker_ids": ["m1", "m1"]}, {"marker_ids": [None]}])
def test_advanced_filter_never_falls_back_to_full_snapshot(bad):
    fake = FakeAPI()
    fake.bad_filter = bad
    with pytest.raises(ConnectorError):
        run(make_service(fake).get_markers(P, B, S, advanced_filter=q.GOST_FILTER))
    assert len(fake.requests) == 1


def test_empty_filter_is_empty_inventory_not_all():
    fake = FakeAPI()
    fake.bad_filter = {"marker_ids": []}
    result = run(make_service(fake).get_markers(P, B, S, advanced_filter=q.GOST_FILTER))
    assert result["markers"] == [] and result["total_count"] == 0


def test_detector_path_and_review_filters():
    fake = FakeAPI()
    svc = make_service(fake)
    result = run(svc.get_warnings(P, B, S, file=["main.go"], warnClass=["DEREF"], review=["Undecided"]))
    assert len(result["warnings"]) == 1
    encoded = fake.requests[-1].url.params["filters"]
    assert json.loads(base64.b64decode(encoded))["marker"]["file"] == r"(main\.go)"
    assert run(svc.get_warnings(P, B, S, review=["Confirmed"]))["warnings"] == []


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_bad_limit_rejected_before_request(limit):
    fake = FakeAPI()
    with pytest.raises(ConnectorError):
        run(make_service(fake).get_markers(P, B, S, limit=limit))
    assert not fake.requests


@pytest.mark.parametrize("verdict", ["False Positive", "Won't fix", "Confirmed", "Unclear"])
def test_prepare_apply_roundtrip_preserves_verdict_and_comment(tmp_path, verdict):
    fake = FakeAPI()
    job = make_job(tmp_path, fake, verdict)
    imports = MarkupImport(make_service(fake), tmp_path, "fixture-user")
    async def scenario():
        preview = await imports.prepare(str(job))
        assert not fake.sent and preview["marker_count"] == 1
        result = await imports.apply(str(job), preview["confirmation"])
        assert result["verification"]["verified"] and result["status"] == "completed_verified"
        with pytest.raises(ConnectorError, match="уже"):
            await imports.apply(str(job), preview["confirmation"])
    run(scenario())
    assert len(fake.sent) == 1
    assert fake.sent[0][0]["review_data"]["status"] == verdict
    assert fake.sent[0][0]["locations"][0]["marker_hash"] == "cHVibGljLXNjaGVtYS1maXh0dXJl"
    assert fake.sent[0][0]["comments"][0]["text"] == "Guard excludes the invalid state."


@pytest.mark.parametrize("change", ["phrase", "payload", "decisions", "remote", "last", "scope"])
def test_apply_rejects_changed_or_unconfirmed_import(tmp_path, change):
    fake = FakeAPI()
    job = make_job(tmp_path, fake)
    imports = MarkupImport(make_service(fake), tmp_path, "fixture-user")
    async def scenario():
        preview = await imports.prepare(str(job))
        phrase = preview["confirmation"]
        mode = "none"
        if change == "phrase":
            phrase = "yes"
        elif change == "payload":
            (job / "svacer-import.jsonl").write_text("{}", encoding="utf-8")
        elif change == "decisions":
            rows = q.load_decisions(job / "decisions.jsonl")
            rows[0]["comment"] = "different proof"
            q.atomic_write_jsonl(job / "decisions.jsonl", rows)
        elif change == "remote":
            fake.exported[0]["comments"] = [{"text": "concurrent change"}]
        elif change == "scope":
            fake.markers[0]["line"] = 100
        else:
            mode = "last"
        with pytest.raises(ConnectorError):
            await imports.apply(str(job), phrase, mode)
    run(scenario())
    assert not fake.sent and not (job / "svacer-import-attempt.json").exists()


def test_conflict_requires_force_phrase(tmp_path):
    fake = FakeAPI()
    job = make_job(tmp_path, fake)
    fake.exported[0]["review_data"] = {"status": "Confirmed", "severity": "Major", "action": "Fix required"}
    imports = MarkupImport(make_service(fake), tmp_path, "fixture-user")
    async def scenario():
        p = await imports.prepare(str(job))
        assert p["requires_force"] and p["conflict_count"] == 1
        with pytest.raises(ConnectorError):
            await imports.apply(str(job), p["confirmation"])
        with pytest.raises(ConnectorError):
            await imports.apply(str(job), p["confirmation"], "force")
        result = await imports.apply(str(job), p["force_confirmation"], "force")
        assert result["verification"]["verified"]
    run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "ignored"])
def test_uncertain_import_never_retried_or_claimed_success(tmp_path, mode):
    fake = FakeAPI()
    fake.fail_import = mode == "timeout"
    fake.ignore_import = mode == "ignored"
    job = make_job(tmp_path, fake)
    imports = MarkupImport(make_service(fake), tmp_path, "fixture-user")
    async def scenario():
        p = await imports.prepare(str(job))
        result = await imports.apply(str(job), p["confirmation"])
        assert not result["verification"]["verified"]
        assert result["status"] == "completed_unverified"
        assert "secret-backend-response" not in json.dumps(result)
        with pytest.raises(ConnectorError):
            await imports.prepare(str(job))
    run(scenario())
    assert len(fake.sent) == 1


@pytest.mark.parametrize("bad", ["pending", "needs_context", "unverified", "missing", "duplicate", "wrong_server"])
def test_incomplete_inputs_fail_closed(tmp_path, bad):
    fake = FakeAPI()
    job = make_job(tmp_path, fake, "Confirmed")
    rows = q.load_decisions(job / "decisions.jsonl")
    if bad == "pending": rows[0]["verdict"] = None
    if bad == "needs_context": rows[0]["analysis_status"] = "needs_context"
    if bad == "unverified": rows[0]["verification"] = {"status": "pending"}
    if bad == "missing": rows = []
    if bad == "duplicate": rows += rows
    if bad == "wrong_server":
        config = json.loads((job / "job.json").read_text(encoding="utf-8"))
        config["snapshot_url"] = "https://other.test/"
        q.atomic_write_json(job / "job.json", config)
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    with pytest.raises(ConnectorError):
        run(MarkupImport(make_service(fake), tmp_path, "tester").prepare(str(job)))
    assert not fake.requests


def test_same_invariant_conflicting_decisions_blocked(tmp_path):
    fake = FakeAPI()
    fake.markers.append({**fake.markers[0], "id": "m2"})
    job = make_job(tmp_path, fake)
    rows = q.load_decisions(job / "decisions.jsonl")
    rows[1].update(verdict="Won't fix", component_defect_proven=True, defect_scope="component",
                   disposition_reason="Product excluded")
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    with pytest.raises(ConnectorError, match="инварианту"):
        run(MarkupImport(make_service(fake), tmp_path, "tester").prepare(str(job)))
    assert not fake.sent


def test_two_process_instances_only_one_attempt(tmp_path):
    fake = FakeAPI()
    job = make_job(tmp_path, fake)
    a = MarkupImport(make_service(fake), tmp_path, "tester")
    b = MarkupImport(make_service(fake), tmp_path, "tester")
    async def scenario():
        p = await a.prepare(str(job))
        results = await asyncio.gather(a.apply(str(job), p["confirmation"]),
                                       b.apply(str(job), p["confirmation"]), return_exceptions=True)
        assert sum(isinstance(r, dict) and r["verification"]["verified"] for r in results) == 1
    run(scenario())
    assert len(fake.sent) == 1


def test_path_outside_results_rejected(tmp_path):
    with pytest.raises(ConnectorError):
        MarkupImport(make_service(FakeAPI()), tmp_path, "tester").job_path(str(tmp_path))


def test_read_retries_but_write_does_not_leak_or_retry(monkeypatch):
    monkeypatch.setattr("triage_connector.client.asyncio.sleep", AsyncMock())
    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(503, text="secret-backend-body")
    api = PublicAPI(URL, transport=httpx.MockTransport(fail))
    for read, count in [(True, 3), (False, 1)]:
        calls.clear()
        with pytest.raises(ConnectorError) as exc:
            run(api.json("POST", "/api/public/markup/import", read_only=read))
        assert "secret" not in str(exc.value) and len(calls) == count


@pytest.mark.parametrize("bad", ["file:///tmp/a", "https://user:password@example.test", "https://a.test/?token=x"])
def test_no_credentials_or_invalid_url(bad):
    with pytest.raises(ConnectorError) as exc:
        PublicAPI(bad)
    assert bad not in str(exc.value)


def test_login_and_preview_line_contract():
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/login"):
            assert json.loads(request.content) == {"login": "test", "password": "synthetic", "auth_type": "ldap", "server": "fixture"}
            return httpx.Response(200, json={"token": "synthetic"})
        assert request.headers["authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"line": 29, "total_lines": 80, "content": "line30\nline31\n"})
    async def scenario():
        api = PublicAPI(URL, transport=httpx.MockTransport(handle))
        await api.login("test", "synthetic", auth_type="LDAP", server="fixture")
        svc = SvacerService(api)
        assert (await svc.get_advanced_file_preview(S, "/x.go", 30, 0, 1))["line"] == 29
        with pytest.raises(ConnectorError):
            await svc.get_advanced_file_preview(S, "/x.go", 31, 0, 1)
        await api.close()
    run(scenario())


def test_mcp_tool_schema_and_text_wire_contract(tmp_path):
    fake = FakeAPI()
    svc = make_service(fake)
    mcp = build_mcp(svc, MarkupImport(svc, tmp_path, "tester"), token="x" * 32)
    async def scenario():
        tools = {t.name: t for t in await mcp.list_tools()}
        assert len(tools) == 10
        assert tools["get_markers"].inputSchema["required"] == ["project_id", "branch_id", "snapshot_id"]
        assert "advanced_filter" in tools["get_markers"].inputSchema["properties"]
        assert "service" not in tools["get_markers"].inputSchema["properties"]
        result = await mcp.call_tool("get_markers", {"project_id": P, "branch_id": B, "snapshot_id": S,
                                                  "limit": 0, "fields": ["*"], "advanced_filter": q.GOST_FILTER})
        assert json.loads(result[0].text)["markers"] == fake.markers
        assert not any(n == "svacer_mcp" or n.startswith("svacer_mcp.") for n in sys.modules)
    run(scenario())


def test_http_mcp_auth_and_initialize(tmp_path):
    svc = make_service(FakeAPI())
    mcp = build_mcp(svc, MarkupImport(svc, tmp_path, "tester"), token="x" * 32)
    async def scenario():
        app = mcp.streamable_http_app()
        async with mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
                headers = {"Accept": "application/json, text/event-stream"}
                body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
                assert (await client.post("/mcp", json=body, headers=headers)).status_code == 401
                headers["Authorization"] = "Bearer " + "x" * 32
                response = await client.post("/mcp", json=body, headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()["result"]["serverInfo"]["name"] == "Svacer Triage Connector"
                headers["Host"] = "evil.test"
                assert (await client.post("/mcp", json=body, headers=headers)).status_code == 421
    run(scenario())


def test_bearer_does_not_accept_wrong_or_non_ascii():
    verifier = LocalBearer("x" * 32, "http://127.0.0.1:8002")
    assert run(verifier.verify_token("wrong")) is None
    assert run(verifier.verify_token("не токен")) is None
    assert run(verifier.verify_token("x" * 32)).client_id == "local-triage"


def test_read_refreshes_expired_auth_but_import_never_repeats():
    events = []
    def handle(request):
        events.append(request.url.path)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": f"synthetic-{len(events)}"})
        if events.count(request.url.path) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=[])
    async def scenario():
        api = PublicAPI(URL, transport=httpx.MockTransport(handle))
        await api.login("test", "synthetic")
        assert await api.json("GET", "/api/public/projects", read_only=True) == []
        assert events == ["/api/public/login", "/api/public/projects", "/api/public/login", "/api/public/projects"]
        with pytest.raises(ConnectorError):
            await api.json("POST", "/api/public/markup/import")
        assert events.count("/api/public/markup/import") == 1
        assert events.count("/api/public/login") == 2
        await api.close()
        assert api._credentials is None and "Authorization" not in api.http.headers
    run(scenario())


def test_rejected_refresh_has_finite_attempts():
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("login"):
            return httpx.Response(200, json={"token": "synthetic"})
        return httpx.Response(401)
    async def scenario():
        api = PublicAPI(URL, transport=httpx.MockTransport(handle))
        await api.login("test", "synthetic")
        with pytest.raises(ConnectorError):
            await api.json("GET", "/api/public/projects", read_only=True)
        assert len(calls) == 4
        await api.close()
    run(scenario())


def test_projects_snapshots_groups_stats_diff_and_compact_fields():
    fake = FakeAPI()
    def handle(request):
        path = request.url.path
        if path == "/api/public/projects":
            return httpx.Response(200, json=[{"project": {"id": P, "name": "fixture"},
                                            "branches": [{"id": B, "name": "master"}]}])
        if path.endswith("/snapshots"):
            assert json.loads(base64.b64decode(request.url.params["filters"])) == {"snapshot": {"name": "v1"}}
            return httpx.Response(200, json=[{"id": S, "name": "v1", "import_time": "2026-01-01",
                                              "details": {"markers_count": 1, "commit_hash": "test"}}])
        if path.endswith("/project-groups"):
            assert json.loads(request.content) == {"action": "get", "project_group_name_or_id": "team"}
            return httpx.Response(200, json={"project_group_name": "team", "projects": [{"id": P}]})
        if path == "/api/public/diff":
            assert request.url.params["snapshot_v1"] == S and request.url.params["snapshot_v2"] == B
            return httpx.Response(200, json={"stats": {"new": 1}, "markers": {"new_markers": fake.markers,
                                             "missing_markers": [], "matched_markers": [], "modified_markers": []}})
        return fake.handle(request)
    svc = SvacerService(PublicAPI(URL, transport=httpx.MockTransport(handle)))
    async def scenario():
        assert (await svc.get_projects())[0]["branches"][0]["branch_id"] == B
        assert (await svc.get_snapshots(P, B, "v1"))[0]["snapshot_id"] == S
        assert (await svc.get_project_groups("team"))["projects"][0]["id"] == P
        assert (await svc.get_project_stats(P, B, S))["by_review_status"] == {"Undecided": 1}
        result = await svc.get_diff(S, B, level=2, fields=["id"], limit=0)
        assert result["markers"]["new_markers"]["markers"] == [{"id": "m1"}]
        assert not result["markers"]["missing_markers"]["truncated"]
    run(scenario())


def test_large_list_limits_and_warning_auto_limit():
    fake = FakeAPI()
    fake.markers = [{**fake.markers[0], "id": f"m{i}"} for i in range(50)]
    svc = make_service(fake)
    async def scenario():
        compact = await svc.get_markers(P, B, S)
        assert compact["total_count"] == 50 and compact["returned_count"] == 30 and compact["truncated"]
        detailed = await svc.get_warnings(P, B, S, traces=True)
        assert detailed["returned_count"] == 8 and detailed["truncated"]
        all_rows = await svc.get_warnings(P, B, S, traces=True, limit=0)
        assert len(all_rows["warnings"]) == 50 and not all_rows["truncated"]
    run(scenario())


def test_live_analysis_prevents_import(tmp_path):
    fake = FakeAPI()
    job = make_job(tmp_path, fake)
    q.atomic_write_json(job / "codex-run.json", {"active": True})
    with pytest.raises(ConnectorError, match="остановки"):
        run(MarkupImport(make_service(fake), tmp_path, "tester").prepare(str(job)))
    assert not fake.requests


def test_editor_rechecks_attempt_inside_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    fake = FakeAPI()
    job = make_job(tmp_path, fake)
    @contextmanager
    def raced_lock(_):
        (job / "svacer-import-attempt.json").write_text("{}", encoding="utf-8")
        yield
    monkeypatch.setattr(q, "decision_lock", raced_lock)
    with pytest.raises(SystemExit, match="началась отправка"):
        q.edit_saved_decision(job / "markers.inventory.json", job / "decisions.jsonl", "m1", "modified")
    assert q.load_decisions(job / "decisions.jsonl")[0]["comment"] != "modified"


def test_install_and_entrypoints_have_no_legacy_runtime_dependency():
    app = Path(__file__).resolve().parents[1]
    for name in ("setup_mcp.ps1", "start_svacer_http.py", "start_svacer_stdio.py", "start_svacer_mcp.ps1"):
        text = (app / name).read_text(encoding="utf-8-sig")
        assert "from svacer_mcp" not in text and "-m svacer_mcp" not in text
        assert "-e $repoPath" not in text
    setup = (app / "setup_mcp.ps1").read_text(encoding="utf-8-sig")
    root = app.parent
    assert "poetry.lock" in setup and "$poetryExecutable sync" in setup
    assert "requirements-connector.txt" not in setup
    assert (root / "pyproject.toml").is_file() and (root / "poetry.lock").is_file()
