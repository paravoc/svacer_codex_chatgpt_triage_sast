"""Regression coverage for lost selections and unfinished-source continuation.

All jobs, sources and model replies here are synthetic; no external calls.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as runner
import triage_queue as q
from marker_history import append_batch_history
from triage_dashboard import collect_state, set_pause
from triage_gui import current_run_queue_ids, marker_assignments
from test_queue_execution import make_job, claim, result_for, write_results


def unfinished(job, ctx, worker=1):
    assignment = ctx["batch"]["assignments"][worker - 1]
    q.atomic_write_json(job / "notes" / f"batch-{ctx['batch_number']:03d}-worker-{worker}.json", [
        {"marker_id": mid, "analysis_status": "needs_context", "proof_gaps": ["Need exact VM source"]}
        for mid in assignment["marker_ids"]
    ])


def enqueue(job, ids):
    q.enqueue_marker_ids(job / "markers.inventory.json", job / "decisions.jsonl", ids)
    set_pause(job, False)


def test_unfinished_saved_note_requires_another_model_turn(tmp_path):
    job = make_job(tmp_path, count=1)
    ctx = claim(job)
    unfinished(job, ctx)
    assert not runner.saved_worker_notes_match(job, ctx)


def test_source_request_draft_does_not_hide_running_workers(tmp_path):
    job = make_job(tmp_path, count=2, workers=2, manual=True)
    enqueue(job, ["m00", "m01"])
    ctx = claim(job, workers=2)
    for worker in (1, 2):
        unfinished(job, ctx, worker)
    state = collect_state(job)
    state["codex_run"] = {"active": True, "phase": "analysis"}
    assert marker_assignments(state) == {"m00": "Агент 1", "m01": "Агент 2"}
    assert all(not worker["current_saved_marker_ids"] for worker in state["workers"].values())


def test_continuation_uses_only_assigned_unfinished_notes_not_prior_verdicts(tmp_path):
    job = make_job(tmp_path, count=3)
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [
        {"marker_id": "m00", "analysis_status": "needs_context", "proof_gaps": ["need VM"]},
    ])
    q.atomic_write_json(job / "notes" / "batch-001-worker-2.json", [result_for(job, "m01")])
    q.atomic_write_json(job / "notes" / "batch-001-worker-3.json", [
        {"marker_id": "m02", "analysis_status": "needs_context", "proof_gaps": ["unrelated"]},
    ])
    assert runner.unfinished_note_paths(job, ["m00", "m01"]) == [str(Path("notes/batch-001-worker-1.json"))]


def test_completion_never_consumes_unfinished_selected_markers(tmp_path):
    job = make_job(tmp_path, count=3, workers=2, manual=True)
    enqueue(job, ["m00", "m01", "m02"])
    ctx = claim(job, workers=2)
    for worker in (1, 2):
        unfinished(job, ctx, worker)
    runner.mark_incomplete(job, ctx, "missing source")
    q.defer_incomplete_markers(job / "decisions.jsonl", ["m00", "m01"])
    q.record_batch_completion(job / "decisions.jsonl")
    assert q.priority_marker_ids(job / "decisions.jsonl") == ["m00", "m01", "m02"]
    assert q.runnable_priority_marker_ids(job / "decisions.jsonl") == ["m02"]
    assert claim(job, workers=2)["batch"]["marker_ids"] == ["m02"]


def test_incomplete_reasons_are_not_shared_between_workers(tmp_path):
    job = make_job(tmp_path, count=2, workers=2)
    ctx = claim(job, workers=2)
    for worker, gap in ((1, "Need VM slot writer"), (2, "Need allocator build flags")):
        q.atomic_write_json(job / "notes" / f"batch-001-worker-{worker}.json", [{
            "marker_id": f"m0{worker-1}", "analysis_status": "needs_context", "proof_gaps": [gap],
        }])
    with pytest.raises(runner.IncompleteAnalysisError) as error:
        runner.finalize_turn(job, ctx)
    runner.mark_incomplete(job, ctx, str(error.value), error.value.marker_ids)
    saved = json.loads((job / "incomplete-analysis.json").read_text())
    assert saved["m00"]["reason"] == "Need VM slot writer"
    assert saved["m01"]["reason"] == "Need allocator build flags"


def test_mixed_batch_saves_valid_worker_and_keeps_other_pending(tmp_path):
    job = make_job(tmp_path, count=2, workers=2, manual=True)
    enqueue(job, ["m00", "m01"])
    ctx = claim(job, workers=2)
    write_results(job, ctx)
    unfinished(job, ctx, 2)
    with pytest.raises(runner.IncompleteAnalysisError) as error:
        runner.finalize_turn(job, ctx)
    assert error.value.marker_ids == ["m01"]
    decisions = q.load_decisions(job / "decisions.jsonl")
    assert decisions[0]["verdict"] == "False Positive"
    assert not decisions[1]["verdict"]
    assert q.priority_marker_ids(job / "decisions.jsonl") == ["m01"]
    runner.mark_incomplete(job, ctx, str(error.value), error.value.marker_ids)
    status = json.loads((job / "workers.status.json").read_text())
    assert [w["status"] for w in status["workers"]] == ["saved", "incomplete"]
    history = append_batch_history(
        job, ctx, launch_id="test", runner_batch=1, started_at="2026-09-22T10:00:00Z",
        finished_at="2026-09-22T10:01:00Z", elapsed_seconds=60, exit_code=3,
        usage={}, agent_messages=[], failure_reason=str(error.value),
    )
    assert [row["status"] for row in history] == ["completed", "incomplete"]
    assert history[0]["failure_reason"] == ""


@pytest.mark.parametrize("all_incomplete", [False, True])
def test_manual_launch_defers_incomplete_without_losing_queue_or_looping(tmp_path, monkeypatch, all_incomplete):
    job = make_job(tmp_path, count=3, workers=2, manual=True)
    enqueue(job, ["m00", "m01", "m02"])
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("offline test")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "first", "active": True})
    monkeypatch.setattr(runner, "_job_paths", lambda _: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, workers=2))
    seen = []

    def fake_turn(_job, _app, _launch, ctx, _start, _index):
        seen.extend(ctx["batch"]["marker_ids"])
        assert len(seen) <= 3, "Unfinished markers must not loop in the same launch"
        write_results(job, ctx)
        for assignment in ctx["batch"]["assignments"]:
            if all_incomplete or "m00" in assignment["marker_ids"]:
                unfinished(job, ctx, assignment["worker"])
        return 0, 0

    monkeypatch.setattr(runner, "_run_one_codex_turn", fake_turn)
    assert runner.run_job(job, "first") == 3
    assert seen == ["m00", "m01", "m02"]
    expected = ["m00", "m01", "m02"] if all_incomplete else ["m00"]
    assert q.priority_marker_ids(job / "decisions.jsonl") == expected
    assert not q.runnable_priority_marker_ids(job / "decisions.jsonl")
    state = collect_state(job)
    assert current_run_queue_ids(q.load_decisions(job / "decisions.jsonl"), state, set(expected), expected) == expected
    assert state["deferred_marker_ids"] == expected
    assert state["one_shot_completed"] is False
    set_pause(job, False)
    assert q.runnable_priority_marker_ids(job / "decisions.jsonl") == expected
    assert claim(job, workers=2)["batch"]["marker_ids"] == expected[:2]


def test_fourth_source_round_allowed_but_limit_stays_bounded(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=1)
    ctx = claim(job)
    ctx["source_request_round"] = 3
    monkeypatch.setenv("SVACER_LOCAL_MCP_TOKEN", "offline-fixture")
    monkeypatch.setattr(runner, "read_app_settings", lambda _: {})
    calls = []

    def fetch(*args):
        calls.append(args[-1])
        return {"content": "exact source", "line": 0, "total_lines": 1}

    monkeypatch.setattr(runner, "fetch_snapshot_source", fetch)
    request = job / "notes" / "source-requests-001.json"
    q.atomic_write_json(request, [{"file_path": "src/vm_x64.dasc", "reason": "prove VM slot transfer"}])
    assert runner.resolve_source_requests(job, tmp_path, ctx)
    assert ctx["source_request_round"] == 4
    ctx["source_request_round"] = runner.MAX_SOURCE_REQUEST_ROUNDS
    q.atomic_write_json(request, [{"file_path": "src/lj_libdef.h", "reason": "prove generated retention"}])
    with pytest.raises(runner.IncompleteAnalysisError, match="предел"):
        runner.resolve_source_requests(job, tmp_path, ctx)
    assert calls == ["src/vm_x64.dasc"]
    assert request.exists()  # next explicit launch can service it


def test_new_launch_resets_round_budget_but_keeps_exact_sources(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=1, manual=True)
    enqueue(job, ["m00"])
    ctx = {**claim(job), "schema_version": 2, "review_contract_version": 1,
           "snapshot_id": "exact-snapshot", "launch_id": "old", "repository": str(tmp_path),
           "trace_files": ["raw.json"], "external_sources": [], "external_source_errors": [],
           "source_request_round": 8, "quality_repair_count": 1,
           "requested_source_paths": ["failed.h", "cached.h"],
           "source_request_errors": [{"file_path": "failed.h", "status": "fetch_failed"}]}
    q.atomic_write_json(job / "raw.json", {})
    q.atomic_write_json(job / "batch-context.json", ctx)
    job_data = json.loads((job / "job.json").read_text())
    resumed = runner.prepare_batch(job, tmp_path, job_data, tmp_path, "exact-revision", "new")
    assert resumed["source_request_round"] == 8
    assert resumed["source_request_round_start"] == 8
    assert "quality_repair_count" not in resumed
    assert resumed["trace_files"] == ["raw.json"]
    assert resumed["requested_source_paths"] == ["cached.h"]


def test_misplaced_newer_unfinished_note_is_available_for_continuation(tmp_path):
    job = make_job(tmp_path, count=1)
    nested = job / "worker-runs/old/batch-001/worker-1/notes/batch-001-worker-1.json"
    nested.parent.mkdir(parents=True)
    q.atomic_write_json(nested, [{"marker_id": "m00", "analysis_status": "needs_context", "proof_gaps": ["latest gap"]}])
    assert runner.unfinished_note_paths(job, ["m00"]) == [str(nested.relative_to(job))]
    assert runner.unfinished_note_paths(job, ["another-marker"]) == []
    q.atomic_write_json(nested, [{"marker_id": "m00", "verdict": "False Positive"}])
    assert runner.unfinished_note_paths(job, ["m00"]) == []
