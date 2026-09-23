"""Offline security and workflow checks for the container web control plane."""

from __future__ import annotations

import sys
from pathlib import Path

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import triage_queue as queue
import web_service as web


def make_web_job(tmp_path: Path) -> Path:
    job = tmp_path / "RESULTS" / "20260922-120000-test"
    (job / "raw").mkdir(parents=True)
    (job / "notes").mkdir()
    queue.atomic_write_json(job / "job.json", {
        "repository_url": "https://github.com/example/project.git",
        "git_ref": "v1.0.0", "created_at": "2026-09-22T12:00:00+03:00",
        "parallel_workers": 3, "saved_context_token_warning": 200000,
    })
    marker = {"id": "marker-1", "invariant": "inv", "warnClass": "NULL",
              "file": "main.go", "line": 7, "review": None}
    queue.atomic_write_json(job / "markers.inventory.json", {
        "markers": [marker], "total_count": 1, "returned_count": 1,
        "truncated": False, "filters_applied": {"advanced_filter": queue.GOST_FILTER},
    })
    queue.atomic_write_jsonl(job / "decisions.jsonl", [queue.new_pending_decision(marker)])
    return job


def authenticated_client(monkeypatch, tmp_path: Path) -> tuple[TestClient, str]:
    monkeypatch.setenv("SVACER_WEB_TOKEN", "w" * 48)
    monkeypatch.setenv("SVACER_COOKIE_SECURE", "0")
    web.DATA_ROOT = tmp_path.resolve()
    web.SESSIONS.clear()
    client = TestClient(web.app, base_url="http://localhost")
    response = client.post("/login", data={"token": "w" * 48}, follow_redirects=False)
    assert response.status_code == 303
    csrf = client.get("/api/session").json()["csrf"]
    assert csrf
    return client, csrf


def test_health_is_public_but_data_requires_login(monkeypatch, tmp_path):
    monkeypatch.setenv("SVACER_WEB_TOKEN", "w" * 48)
    web.DATA_ROOT = tmp_path.resolve()
    web.SESSIONS.clear()
    with TestClient(web.app, base_url="http://localhost") as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/").status_code == 200  # TestClient follows the login redirect.


def test_cookie_session_requires_csrf_for_queue_changes(monkeypatch, tmp_path):
    make_web_job(tmp_path)
    client, csrf = authenticated_client(monkeypatch, tmp_path)
    with client:
        jobs = client.get("/api/jobs").json()["jobs"]
        assert len(jobs) == 1 and jobs[0]["label"] == "project v1.0.0"
        job_id = jobs[0]["id"]
        markers = client.get(f"/api/jobs/{job_id}/markers").json()["markers"]
        assert markers[0]["id"] == "marker-1"
        assert client.post(f"/api/jobs/{job_id}/queue", json={"ids": ["marker-1"]}).status_code == 401
        response = client.post(
            f"/api/jobs/{job_id}/queue", json={"ids": ["marker-1"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["queued"] == ["marker-1"]

        assert client.post("/logout").status_code == 401
        logged_out = client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert logged_out.status_code == 200
        assert logged_out.json() == {"ok": True}


def test_unknown_job_id_never_becomes_a_filesystem_path(monkeypatch, tmp_path):
    client, _csrf = authenticated_client(monkeypatch, tmp_path)
    with client:
        response = client.get("/api/jobs/..%2F..%2Fetc%2Fpasswd")
        assert response.status_code in {404, 400}
        assert str(tmp_path) not in response.text


def test_web_comment_is_plain_text_but_stored_evidence_is_unchanged(monkeypatch, tmp_path):
    job = make_web_job(tmp_path)
    rows = queue.load_decisions(job / "decisions.jsonl")
    rows[0].update(verdict="False Positive",
                   comment="В `Get()` есть проверка ([main.go:7](/root/build/main.go:7)).",
                   source_evidence=[{"file_path": "/root/build/main.go", "excerpt": "`literal`"}])
    queue.atomic_write_jsonl(job / "decisions.jsonl", rows)
    before = (job / "decisions.jsonl").read_bytes()
    client, _ = authenticated_client(monkeypatch, tmp_path)
    with client:
        job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
        decision = client.get(f"/api/jobs/{job_id}/markers/marker-1").json()["decision"]
        assert decision == {**rows[0], "comment": "В Get() есть проверка (main.go:7)."}
    assert (job / "decisions.jsonl").read_bytes() == before


def test_incomplete_selection_stays_in_web_queue_not_active(monkeypatch, tmp_path):
    job = make_web_job(tmp_path)
    queue.atomic_write_json(job / "control.json", {
        "priority_marker_ids": ["marker-1"], "deferred_marker_ids": ["marker-1"],
        "manual_queue_requested": True,
    })
    queue.atomic_write_json(job / "workers.status.json", {
        "state": "incomplete", "batch": 1,
        "workers": [{"worker": 1, "status": "incomplete", "marker_ids": ["marker-1"]}],
    })
    client, _ = authenticated_client(monkeypatch, tmp_path)
    with client:
        job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
        response = client.get(f"/api/jobs/{job_id}/markers?status=queued")
        assert response.status_code == 200
        marker = response.json()["markers"][0]
        assert marker["status"] == "needs_context"
        assert marker["queued"] is True and marker["active"] is False


def test_web_reports_incomplete_worker_before_batch_deferral(monkeypatch, tmp_path):
    job = make_web_job(tmp_path)
    queue.atomic_write_json(job / "control.json", {
        "priority_marker_ids": ["marker-1"], "manual_queue_requested": True,
    })
    queue.atomic_write_json(job / "workers.status.json", {
        "state": "assigned", "batch": 1,
        "workers": [{"worker": 1, "status": "assigned", "marker_ids": ["marker-1"]}],
    })
    queue.atomic_write_json(job / "codex-run.json", {"launch_id": "live"})
    queue.atomic_write_json(job / "workers-runtime.json", {
        "launch_id": "live", "batch": 1,
        "workers": {"marker-1": {"state": "incomplete", "pid": None}},
    })
    monkeypatch.setattr(web, "read_run_record", lambda _: {"active": True, "launch_id": "live"})
    client, _ = authenticated_client(monkeypatch, tmp_path)
    with client:
        job_id = client.get("/api/jobs").json()["jobs"][0]["id"]
        marker = client.get(f"/api/jobs/{job_id}/markers?status=queued").json()["markers"][0]
        assert marker["status"] == "needs_context" and not marker["active"]


def test_container_uses_locked_dependencies_and_secret_files():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    entrypoint = (root / "app" / "container_entrypoint.py").read_text(encoding="utf-8")
    assert "poetry sync --without desktop --without dev --no-root" in dockerfile
    assert "AS python-builder" in dockerfile and "AS runtime" in dockerfile
    assert "COPY --from=python-builder /opt/svacer-venv /opt/svacer-venv" in dockerfile
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert "POETRY_VERSION" not in runtime and "/opt/poetry" not in runtime
    assert "@openai/codex@${CODEX_CLI_VERSION}" in dockerfile
    assert "SVACER_PASSWORD_FILE: /run/secrets/svacer_password" in compose
    assert "CODEX_API_KEY_FILE: /run/secrets/codex_api_key" in compose
    assert "secret_file(\"SVACER_PASSWORD\")" in entrypoint
    environment = compose.split("environment:", 1)[1].split("secrets:", 1)[0]
    assert "SVACER_PASSWORD:" not in environment
