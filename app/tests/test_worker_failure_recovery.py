"""Transport failures and schema errors stay distinct from source-proof gaps."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as r
import parallel_analysis as p
from triage_gui import queued_marker_status
from triage_queue import validate_worker_result
from test_parallel_processes import setup_quality_job, setup_job, execute


@pytest.mark.parametrize("mode,attempts,state", [
    ("capacity_once", 2, "finished"), ("capacity_always", 3, "incomplete"),
])
def test_capacity_retry_is_bounded_and_does_not_restart_other_workers(tmp_path, monkeypatch, mode, attempts, state):
    job, context = setup_quality_job(tmp_path, monkeypatch, mode)
    monkeypatch.setattr(p, "TRANSIENT_RETRY_DELAYS", (0, 0))
    # Pretend a previous source gap has already been satisfied locally.
    affected = next(a["marker_ids"][0] for a in context["batch"]["assignments"] if a["worker"] == 2)
    old = {"marker_id": affected, "analysis_status": "needs_context", "proof_gaps": ["old missing source"]}
    r.atomic_json(job / "notes/batch-001-worker-2.json", [old])
    execute(job, context)
    for mid, worker in p.read_worker_runtime(job)["workers"].items():
        directory = (job / worker["event_log"]).parent
        assert len(list(directory.glob("prompt-*.txt"))) == (attempts if worker["worker"] == 2 else 1)
        assert worker["state"] == (state if worker["worker"] == 2 else "finished")
        saved = r.read_json(directory / "context.json")
        assert not saved.get("worker_quality_repair_count")
        if worker["worker"] == 2:
            assert not worker["usage_complete"]  # Failed turn usage is not fabricated as zero.
            row = r.read_json(job / "notes/batch-001-worker-2.json")[0]
            if state == "incomplete":
                assert "модель временно перегружена" in row["execution_error"]
                assert row["proof_gaps"][0] == row["execution_error"]
                assert "old missing source" in row["proof_gaps"]
                assert row.get("verdict") != "False Positive"
            else:
                assert row["verdict"] == "False Positive"
    assert all(not row.get("verdict") for row in r.load_decisions(job / "decisions.jsonl"))


@pytest.mark.parametrize("message,kind", [
    ("Selected model is at capacity. Please try a different model.", "model_capacity"),
    ("HTTP error: 503 Service Unavailable", "service_unavailable"),
    ("401 Unauthorized", None), ("Usage limit exceeded", None),
    ("unknown model", None), ("invalid JSON result", None),
])
def test_retry_only_known_transport_failures_not_user_content(message, kind):
    assert p.transient_failure_kind({"type": "turn.failed", "error": {"message": message}}) == kind
    assert p.transient_failure_kind({"type": "item.completed", "item": {"type": "agent_message", "text": message}}) is None


def test_stop_interrupts_backoff_and_ui_explains_the_wait():
    started = time.monotonic()
    assert not p.wait_for_retry(60, lambda: True)
    assert time.monotonic() - started < 1
    status, detail = queued_marker_status({"codex_run": {"active": True}, "worker_runtime": {
        "workers": {"m": {"state": "starting", "retry_reason": "Модель перегружена"}}}}, "m")
    assert status == "Повтор подключения" and "Модель перегружена" in detail


def test_confirmed_invalid_enums_are_repaired_with_visible_feedback(tmp_path, monkeypatch):
    job, context = setup_quality_job(tmp_path, monkeypatch, "schema_enums")
    path = job / "fixture-result-2.json"
    row = r.read_json(path)[0]
    row.update(verdict="Confirmed", defect_scope="product", component_defect_proven=True,
               product_defect_reachable=True, severity="Minor", action="Fix required")
    r.atomic_json(path, [row])
    execute(job, context)
    for mid, worker in p.read_worker_runtime(job)["workers"].items():
        assert worker["state"] == "finished"
        directory = (job / worker["event_log"]).parent
        assert len(list(directory.glob("prompt-*.txt"))) == (2 if worker["worker"] == 2 else 1)
        if worker["worker"] == 2:
            saved = r.read_json(job / "notes/batch-001-worker-2.json")[0]
            current = next(item for item in r.load_decisions(job / "decisions.jsonl") if item["marker_id"] == mid)
            assert validate_worker_result(saved, current) == []
            assert saved["severity"] == "Minor" and saved["action"] == "Fix required"


@pytest.mark.parametrize("gaps", [None, "not a list", [{"invalid": "gap"}], ["earlier gap"]])
def test_terminal_error_preserves_latest_rejected_evidence(tmp_path, monkeypatch, gaps):
    job, context = setup_quality_job(tmp_path, monkeypatch, "quality_repair_fails")
    fixture = job / "fixture-result-2.json"
    rows = r.read_json(fixture)
    rows[0]["proof_gaps"] = gaps
    r.atomic_json(fixture, rows)
    execute(job, context)
    failed = r.read_json(job / "notes/batch-001-worker-2.json")[0]
    assert failed["analysis_status"] == "needs_context"
    assert failed["source_evidence"] == r.read_json(job / "fixture-result-2.json")[0]["source_evidence"]
    assert failed["proof_gaps"][0] == failed["execution_error"]


def test_idle_timeout_stops_only_silent_worker_and_preserves_peers(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "stall_second")
    monkeypatch.setattr(p, "INACTIVITY_TIMEOUT_SECONDS", 2)
    execute(job, context)
    for worker in p.read_worker_runtime(job)["workers"].values():
        directory = (job / worker["event_log"]).parent
        started = r.read_json(directory / "started.json")
        assert not r.process_is_alive(started["pid"])
        if worker["worker"] == 2:
            assert "Нет событий исполнителя" in worker["error"]
            assert not worker["usage_complete"]
        else:
            assert not worker.get("error") and worker["usage_complete"]
