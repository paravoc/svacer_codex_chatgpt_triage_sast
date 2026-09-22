"""Offline regressions: no model calls, no Svacer writes, no production jobs."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import triage_queue as q
import codex_run as runner
from triage_dashboard import set_pause


def make_job(tmp_path, count=20, workers=1, budget=15, mode="single_batch", manual=False):
    job = tmp_path / "job"
    job.mkdir()
    (job / "notes").mkdir()
    rows = [{"id": f"m{i:02}", "warnClass": "NULL", "file": "same.go", "line": i + 1}
            for i in range(count)]
    q.atomic_write_json(job / "markers.inventory.json", {
        "markers": rows, "truncated": False, "total_count": count, "returned_count": count,
        "filters_applied": {"advanced_filter": q.GOST_FILTER}})
    q.atomic_write_jsonl(job / "decisions.jsonl", [
        {"schema_version": 2, "marker_id": r["id"], "verdict": None,
         **{key: r[key] for key in ("warnClass", "file", "line")}} for r in rows])
    q.atomic_write_json(job / "job.json", {"batch_size": budget, "parallel_workers": workers,
                                           "run_mode": mode, "manual_selection_only": manual,
                                           "snapshot_id": "exact-snapshot"})
    return job


def claim(job, budget=15, workers=1):
    result = q.claim_next_batch(job / "markers.inventory.json", job / "decisions.jsonl", budget, workers)
    return {**result, "execution_policy_version": 1, "revision": "exact-revision"}


def result_for(job, mid, verdict="False Positive"):
    row = next(r for r in q.load_decisions(job / "decisions.jsonl") if r["marker_id"] == mid)
    row.update(verdict=verdict, confidence="high", decision_policy_version=1,
               defect_scope={"False Positive": "none", "Confirmed": "product", "Won't fix": "component", "Unclear": "unknown"}[verdict],
               source="input", control="guard", sink="read", entrypoint="entry",
               build_reachability="enabled", product_reachability="path checked", impact="proved",
               evidence=["same.go:1"], counterevidence=["guard"], proof_gaps=[],
               reachable_path=["entry -> sink"], comment="Guard prevents the invalid state.",
               boundary={"product_surface": "entry", "source_trust": "untrusted",
                         "policy_basis": "source contract", "boundary_crossed": False})
    if verdict == "Confirmed":
        row.update(severity="Major", action="Fix required")
    if verdict == "Won't fix":
        row["disposition_reason"] = "API contains defect but not used in this product build."
    return row


def write_results(job, ctx, verdict="False Positive"):
    for a in ctx["batch"]["assignments"]:
        q.atomic_write_json(job / "notes" / f"batch-{ctx['batch_number']:03d}-worker-{a['worker']}.json",
                            [result_for(job, mid, verdict) for mid in a["marker_ids"]])


def test_one_marker_object_is_normalized_and_still_checked(tmp_path):
    job = make_job(tmp_path, count=2, budget=1)
    ctx = claim(job, budget=1)
    path = job / "notes" / f"batch-{ctx['batch_number']:03d}-worker-1.json"
    q.atomic_write_json(path, result_for(job, "m00"))
    assert runner.saved_worker_notes_match(job, ctx)
    runner.finalize_turn(job, ctx)
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] == "False Positive"
    q.atomic_write_json(path, result_for(job, "m01"))
    assert not runner.saved_worker_notes_match(job, ctx)


def test_resume_saved_singleton_note_without_repeating_agent(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=2, budget=1)
    ctx = claim(job, budget=1)
    q.atomic_write_json(job / "notes" / f"batch-{ctx['batch_number']:03d}-worker-1.json",
                        result_for(job, "m00"))
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("test", encoding="utf-8")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "resume", "active": True})
    monkeypatch.setattr(runner, "_job_paths", lambda path: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, budget=1))
    monkeypatch.setattr(runner, "_run_one_codex_turn",
                        lambda *args: pytest.fail("Saved note must be validated, not regenerated"))
    assert runner.run_job(job, "resume") == 0
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] == "False Positive"


def test_failed_run_preserves_real_start_time(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=1)
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("test", encoding="utf-8")
    monkeypatch.setattr(runner, "_job_paths", lambda _: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("preparation failed")))
    ticks = iter(["2026-09-19T10:00:00Z"])
    monkeypatch.setattr(runner, "now_iso",
                        lambda: next(ticks, "2026-09-19T10:01:00Z"))
    assert runner.run_job(job, "launch") == 1
    run = json.loads((job / runner.RUN_FILE).read_text(encoding="utf-8"))
    assert run["started_at"] == "2026-09-19T10:00:00Z"
    assert run["finished_at"] == "2026-09-19T10:01:00Z"


def test_provider_403_preserves_manual_queue_and_records_real_failure(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=7, workers=2, manual=True)
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "test-launch", "active": True})
    ids = [f"m{i:02}" for i in range(7)]
    q.atomic_write_json(job / "control.json", {
        "priority_marker_ids": ids, "manual_queue_requested": True,
        "analysis_started": True, "pause_requested": False,
    })
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("test", encoding="utf-8")
    monkeypatch.setattr(runner, "_job_paths", lambda _: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, workers=2))
    calls = []

    def fail_turn(*args):
        calls.append(1)
        with (job / runner.EVENT_LOG).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "turn.failed", "error": {
                "message": "unexpected status 403 Forbidden: <html>blocked</html>",
            }}) + "\n")
        return 1, 999999

    monkeypatch.setattr(runner, "_run_one_codex_turn", fail_turn)
    assert runner.run_job(job, "test-launch") == 1
    assert calls == [1]  # No retries against a blocked provider.
    record = json.loads((job / runner.RUN_FILE).read_text(encoding="utf-8"))
    assert record["status"] == "failed" and record["active"] is False
    assert "HTTP 403" in record["reason"]
    assert q.priority_marker_ids(job / "decisions.jsonl") == ids
    assert all(not row.get("verdict") for row in q.load_decisions(job / "decisions.jsonl"))
    resumed = claim(job, workers=2)
    assert resumed["batch"]["marker_ids"] == ids[:2]
    rows = [json.loads(line) for line in (job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["marker_id"] for row in rows} == set(ids[:2])
    assert all(row["failure_reason"] == record["reason"] for row in rows)


def test_later_provider_failure_is_not_hidden_by_previous_incomplete_marker(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=2, budget=2, mode="until_complete")
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("test", encoding="utf-8")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "test-launch", "active": True})
    monkeypatch.setattr(runner, "_job_paths", lambda _: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, budget=2))

    def fake_turn(_job, _app, _launch, context, _start, _index):
        marker_id = context["batch"]["marker_ids"][0]
        if marker_id == "m00":
            q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [{
                "marker_id": marker_id, "analysis_status": "needs_context", "proof_gaps": ["need provider"],
            }])
            return 0, 0
        with (job / runner.EVENT_LOG).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "turn.failed", "error": {
                "message": "unexpected status 403 Forbidden",
            }}) + "\n")
        return 1, 0

    monkeypatch.setattr(runner, "_run_one_codex_turn", fake_turn)
    assert runner.run_job(job, "test-launch") == 1
    record = json.loads((job / runner.RUN_FILE).read_text(encoding="utf-8"))
    assert "HTTP 403" in record["reason"]
    rows = [json.loads(line) for line in (job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows] == ["incomplete", "failed"]
    assert "HTTP 403" not in rows[0]["failure_reason"]
    assert "HTTP 403" in rows[1]["failure_reason"]


def test_fifteen_fifo_markers_are_sequential_and_budget_stops(tmp_path):
    job = make_job(tmp_path)
    seen = []
    for i in range(15):
        ctx = claim(job)
        assert ctx["batch"]["count"] == 1
        assert ctx["batch"]["marker_ids"] == [f"m{i:02}"]
        assert claim(job)["batch_number"] == ctx["batch_number"]
        write_results(job, ctx)
        runner.finalize_turn(job, ctx)
        q.record_batch_completion(job / "decisions.jsonl")  # must be idempotent
        seen += ctx["batch"]["marker_ids"]
    assert len(set(seen)) == 15
    assert claim(job)["paused"]
    assert sum(r["verdict"] is None for r in q.load_decisions(job / "decisions.jsonl")) == 5


@pytest.mark.parametrize("workers", [1, 2, 3, 8])
def test_each_worker_has_exactly_one_marker(tmp_path, workers):
    job = make_job(tmp_path, workers=workers)
    ctx = claim(job, workers=workers)
    assert ctx["batch"]["count"] == workers
    assert all(a["count"] == 1 for a in ctx["batch"]["assignments"])
    assert len(set(ctx["batch"]["marker_ids"])) == workers


def test_until_complete_ignores_run_budget(tmp_path):
    job = make_job(tmp_path, count=15, budget=1, mode="until_complete")
    for _ in range(15):
        ctx = claim(job, budget=1)
        write_results(job, ctx)
        runner.finalize_turn(job, ctx)
    assert claim(job, budget=1)["batch"] is None


def test_one_shot_does_not_continue_previous_queue(tmp_path):
    job = make_job(tmp_path, mode="until_complete")
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ["m07"]})
    ctx = claim(job)
    assert ctx["batch"]["marker_ids"] == ["m07"]
    assert json.loads((job / "control.json").read_text(encoding="utf-8"))["priority_marker_ids"] == ["m07"]
    write_results(job, ctx)
    runner.finalize_turn(job, ctx)
    assert claim(job)["paused"]
    assert sum(bool(r["verdict"]) for r in q.load_decisions(job / "decisions.jsonl")) == 1


def test_fifteen_selected_ids_remain_sequential(tmp_path):
    job = make_job(tmp_path, mode="until_complete")
    ids = [f"m{i:02}" for i in reversed(range(15))]
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ids})
    for mid in ids:
        ctx = claim(job)
        assert ctx["batch"]["marker_ids"] == [mid]
        write_results(job, ctx)
        runner.finalize_turn(job, ctx)
    assert claim(job)["paused"]


def test_manual_queue_rechecks_without_clearing_verdict_before_start(tmp_path):
    job = make_job(tmp_path, count=6, budget=10, mode="single_batch")
    decisions_path = job / "decisions.jsonl"
    rows = q.load_decisions(decisions_path)
    rows[0] = result_for(job, "m00")
    q.atomic_write_jsonl(decisions_path, rows)
    q.atomic_write_json(job / "svacer-import-preview.json", {"marker_count": 1})

    queued = q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                                  ["m00", "m04", "m02"])
    assert queued["queued"] == ["m00", "m04", "m02"]
    assert queued["rechecks"] == ["m00"]
    assert q.load_decisions(decisions_path)[0]["verdict"] == "False Positive"
    assert (job / "svacer-import-preview.json").exists()
    assert claim(job)["paused"]
    assert q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                                ["m02", "m03"])["queued"] == ["m00", "m04", "m02", "m03"]
    assert q.load_decisions(decisions_path)[0]["verdict"] == "False Positive"

    set_pause(job, False)
    for mid in ("m00", "m04", "m02", "m03"):
        ctx = claim(job)
        assert ctx["batch"]["marker_ids"] == [mid]
        if mid == "m00":
            assert q.load_decisions(decisions_path)[0]["verdict"] is None
            assert not (job / "svacer-import-preview.json").exists()
        write_results(job, ctx)
        runner.finalize_turn(job, ctx)
    assert claim(job)["paused"]
    assert q.load_decisions(decisions_path)[1]["verdict"] is None


def test_reset_staged_recheck_keeps_original_verdict(tmp_path):
    job = make_job(tmp_path, count=2)
    decisions_path = job / "decisions.jsonl"
    rows = q.load_decisions(decisions_path)
    rows[0] = result_for(job, "m00")
    q.atomic_write_jsonl(decisions_path, rows)
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, ["m00"])
    q.reset_queue_assignments(decisions_path)
    assert q.load_decisions(decisions_path)[0]["verdict"] == "False Positive"
    assert "priority_marker_ids" not in json.loads((job / "control.json").read_text(encoding="utf-8"))


def test_remove_selected_queue_entries_preserves_order_and_recheck_verdict(tmp_path):
    job = make_job(tmp_path, count=5, manual=True)
    decisions_path = job / "decisions.jsonl"
    rows = q.load_decisions(decisions_path)
    rows[0] = result_for(job, "m00")
    q.atomic_write_jsonl(decisions_path, rows)
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                         ["m00", "m02", "m04", "m03"])

    result = q.dequeue_marker_ids(decisions_path, ["m00", "m04"])
    assert result == {"removed": ["m00", "m04"], "removed_rechecks": ["m00"],
                      "queued": ["m02", "m03"]}
    control = json.loads((job / "control.json").read_text(encoding="utf-8"))
    assert control["priority_marker_ids"] == ["m02", "m03"]
    assert control["recheck_marker_ids"] == []
    assert q.load_decisions(decisions_path)[0]["verdict"] == "False Positive"
    with pytest.raises(SystemExit, match="уже не находятся в очереди"):
        q.dequeue_marker_ids(decisions_path, ["m00", "m03"])
    assert q.priority_marker_ids(decisions_path) == ["m02", "m03"]

    q.dequeue_marker_ids(decisions_path, ["m02", "m03"])
    control = json.loads((job / "control.json").read_text(encoding="utf-8"))
    assert "priority_marker_ids" not in control
    assert "recheck_marker_ids" not in control
    assert "manual_queue_requested" not in control
    assert control["pause_requested"] is True
    assert q.load_decisions(decisions_path)[0]["verdict"] == "False Positive"
    assert q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                                ["m01"])["queued"] == ["m01"]


def test_remove_waiting_marker_during_active_run_keeps_assignment(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=4, manual=True)
    decisions_path = job / "decisions.jsonl"
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                         ["m00", "m01", "m02"])
    set_pause(job, False)
    current = claim(job, workers=1)
    assert current["batch"]["marker_ids"] == ["m00"]
    monkeypatch.setattr(runner, "read_run_record", lambda _job: {"active": True, "status": "running"})

    before = (job / "control.json").read_bytes()
    with pytest.raises(SystemExit, match="Назначенный агенту"):
        q.dequeue_marker_ids(decisions_path, ["m00", "m02"])
    assert (job / "control.json").read_bytes() == before
    assert q.dequeue_marker_ids(decisions_path, ["m02"])["queued"] == ["m00", "m01"]
    assert json.loads((job / "control.json").read_text(encoding="utf-8"))["pause_requested"] is False
    write_results(job, current)
    runner.finalize_turn(job, current)
    assert claim(job)["batch"]["marker_ids"] == ["m01"]


def test_remove_last_waiting_marker_stops_after_current_result(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=3, manual=True)
    decisions_path = job / "decisions.jsonl"
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, ["m00", "m01"])
    set_pause(job, False)
    current = claim(job, workers=1)
    monkeypatch.setattr(runner, "read_run_record", lambda _job: {"active": True, "status": "running"})
    assert q.dequeue_marker_ids(decisions_path, ["m01"])["queued"] == ["m00"]
    write_results(job, current)
    runner.finalize_turn(job, current)
    assert claim(job)["paused"]
    assert q.load_decisions(decisions_path)[1]["verdict"] is None


def test_remove_stale_assignment_after_stop_allows_new_selection(tmp_path):
    job = make_job(tmp_path, count=3, manual=True)
    decisions_path = job / "decisions.jsonl"
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                         ["m00", "m01"])
    set_pause(job, False)
    assert claim(job)["batch"]["marker_ids"] == ["m00"]
    q.atomic_write_json(job / "codex-run.json", {"active": False, "status": "stopped"})

    assert q.dequeue_marker_ids(decisions_path, ["m00"])["queued"] == ["m01"]
    status = json.loads((job / "workers.status.json").read_text(encoding="utf-8"))
    assert status["state"] == "superseded"
    assert status["workers"] == []
    assert q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                                ["m02"])["queued"] == ["m01", "m02"]


def test_manual_queue_can_explicitly_retry_draft_and_reset_preserves_decisions(tmp_path):
    job = make_job(tmp_path, count=3, mode="until_complete")
    decisions_path = job / "decisions.jsonl"
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [result_for(job, "m01")])
    queued = q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, ["m01"])
    assert queued["queued"] == ["m01"]
    set_pause(job, False)
    assert claim(job)["batch"]["marker_ids"] == ["m01"]
    q.atomic_write_json(job / "codex-run.json", {"active": False})
    q.reset_queue_assignments(decisions_path)
    control = json.loads((job / "control.json").read_text(encoding="utf-8"))
    assert "priority_marker_ids" not in control
    assert "recheck_marker_ids" not in control
    assert "manual_queue_requested" not in control


@pytest.mark.parametrize("status", ["failed", "stopped", "incomplete"])
def test_append_after_failed_manual_run_keeps_existing_reservation(tmp_path, status):
    job = make_job(tmp_path, count=5, workers=2, manual=True)
    inventory, decisions = job / "markers.inventory.json", job / "decisions.jsonl"
    q.enqueue_marker_ids(inventory, decisions, ["m00", "m01", "m02"])
    set_pause(job, False)
    current = claim(job, workers=2)
    q.atomic_write_json(job / runner.RUN_FILE, {"active": False, "status": status})
    before = (job / "workers.status.json").read_bytes()
    assert q.enqueue_marker_ids(inventory, decisions, ["m01", "m03"])["queued"] == ["m00", "m01", "m02", "m03"]
    assert (job / "workers.status.json").read_bytes() == before
    assert claim(job, workers=2)["paused"]  # Adding never starts analysis.
    set_pause(job, False)
    assert claim(job, workers=2)["batch"]["marker_ids"] == current["batch"]["marker_ids"]


@pytest.mark.parametrize("workers", [1, 2])
def test_fifteen_manual_draft_retries_finish_fifo_without_unselected_backlog(tmp_path, monkeypatch, workers):
    job = make_job(tmp_path, count=18, workers=workers, manual=True)
    chosen = [f"m{i:02}" for i in range(15)]
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [
        result_for(job, mid) for mid in chosen])
    q.enqueue_marker_ids(job / "markers.inventory.json", job / "decisions.jsonl", chosen)
    set_pause(job, False)
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("offline test", encoding="utf-8")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "test-launch", "active": True})
    monkeypatch.setattr(runner, "_job_paths", lambda _: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, workers=workers))
    batches = []

    def finish_turn(_job, _app, _launch, context, _start, _index):
        batches.append(context["batch"]["marker_ids"])
        assert len(context["batch"]["assignments"]) <= workers
        assert all(len(assignment["marker_ids"]) == 1 for assignment in context["batch"]["assignments"])
        write_results(job, context)
        return 0, 0

    monkeypatch.setattr(runner, "_run_one_codex_turn", finish_turn)
    assert runner.run_job(job, "test-launch") == 0
    assert [mid for batch in batches for mid in batch] == chosen
    assert len(batches) == (15 + workers - 1) // workers
    assert q.priority_marker_ids(job / "decisions.jsonl") == []
    saved = q.load_decisions(job / "decisions.jsonl")
    assert all(row["verdict"] == "False Positive" for row in saved[:15])
    assert all(not row["verdict"] for row in saved[15:])


def test_runner_finishes_selected_rechecks_when_project_has_no_pending_markers(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=4, budget=10, mode="single_batch")
    decisions_path = job / "decisions.jsonl"
    q.atomic_write_jsonl(decisions_path, [result_for(job, f"m{i:02}") for i in range(4)])
    q.atomic_write_json(job / "control.json", {"pause_requested": True, "one_shot_completed": True})
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, ["m01", "m03"])
    set_pause(job, False)
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("test", encoding="utf-8")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "manual", "active": True})
    monkeypatch.setattr(runner, "_job_paths", lambda path: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, budget=10))
    seen = []

    def fake_turn(_job, _app, _launch, ctx, _start, _index):
        seen.extend(ctx["batch"]["marker_ids"])
        write_results(job, ctx)
        return 0, 0

    monkeypatch.setattr(runner, "_run_one_codex_turn", fake_turn)
    assert runner.run_job(job, "manual") == 0
    assert seen == ["m01", "m03"]
    assert json.loads((job / runner.RUN_FILE).read_text(encoding="utf-8"))["status"] == "completed"


def test_manual_queue_precedes_unrelated_confirmed_verification(tmp_path):
    job = make_job(tmp_path, count=3, mode="single_batch")
    decisions_path = job / "decisions.jsonl"
    rows = q.load_decisions(decisions_path)
    rows[0] = result_for(job, "m00", "Confirmed")
    q.atomic_write_jsonl(decisions_path, rows)
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, ["m02"])
    set_pause(job, False)
    next_work = runner._queue_next(job, tmp_path, {"batch_size": 10, "parallel_workers": 1})
    assert next_work["batch"]["marker_ids"] == ["m02"]
    assert not next_work.get("verification_only")


def test_manual_queue_gives_two_agents_one_marker_each(tmp_path):
    job = make_job(tmp_path, count=6, workers=2, budget=10, mode="single_batch")
    decisions_path = job / "decisions.jsonl"
    rows = q.load_decisions(decisions_path)
    rows[0] = result_for(job, "m00")
    q.atomic_write_jsonl(decisions_path, rows)
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                         ["m00", "m02", "m04", "m05"])
    set_pause(job, False)
    first = claim(job, budget=10, workers=2)
    assert first["batch"]["marker_ids"] == ["m00", "m02"]
    assert [worker["count"] for worker in first["batch"]["assignments"]] == [1, 1]
    write_results(job, first)
    runner.finalize_turn(job, first)
    second = claim(job, budget=10, workers=2)
    assert second["batch"]["marker_ids"] == ["m04", "m05"]
    assert [worker["count"] for worker in second["batch"]["assignments"]] == [1, 1]


def test_five_selected_markers_are_partitioned_to_five_unique_workers(tmp_path):
    job = make_job(tmp_path, count=7, workers=5, budget=10, manual=True)
    decisions_path = job / "decisions.jsonl"
    selected = ["m06", "m01", "m04", "m00", "m03"]
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, selected)
    set_pause(job, False)

    context = claim(job, budget=10, workers=5)

    assert context["batch"]["marker_ids"] == selected
    assert context["batch"]["worker_count"] == 5
    assert [assignment["worker"] for assignment in context["batch"]["assignments"]] == [1, 2, 3, 4, 5]
    assert all(len(assignment["marker_ids"]) == 1 for assignment in context["batch"]["assignments"])
    assigned = {marker_id for assignment in context["batch"]["assignments"]
                for marker_id in assignment["marker_ids"]}
    assert assigned == set(selected)

    write_results(job, context)
    runner.finalize_turn(job, context)
    saved = {row["marker_id"]: row["verdict"] for row in q.load_decisions(decisions_path)}
    assert all(saved[marker_id] == "False Positive" for marker_id in selected)
    assert q.priority_marker_ids(decisions_path) == []


def test_manual_selection_never_fills_from_unselected_backlog(tmp_path):
    job = make_job(tmp_path, count=25, workers=1, budget=10, manual=True)
    decisions_path = job / "decisions.jsonl"
    assert q.manual_selection_only(decisions_path)
    assert claim(job, budget=10)["paused"]
    assert runner._queue_next(job, tmp_path, {"batch_size": 10, "parallel_workers": 1})["paused"]

    chosen = ["m17", "m03", "m22"]
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, chosen)
    set_pause(job, False)
    for mid in chosen:
        ctx = runner._queue_next(job, tmp_path, {"batch_size": 10, "parallel_workers": 1})
        assert ctx["batch"]["marker_ids"] == [mid]
        write_results(job, ctx)
        runner.finalize_turn(job, ctx)
    assert runner._queue_next(job, tmp_path, {"batch_size": 10, "parallel_workers": 1})["paused"]
    assert sum(row["verdict"] is None for row in q.load_decisions(decisions_path)) == 22


def test_existing_job_without_selection_setting_defaults_to_manual(tmp_path):
    job = make_job(tmp_path, count=12)
    data = json.loads((job / "job.json").read_text(encoding="utf-8"))
    data.pop("manual_selection_only")
    q.atomic_write_json(job / "job.json", data)
    assert q.manual_selection_only(job / "decisions.jsonl")
    assert claim(job)["paused"]


def test_manual_selection_uses_agent_count_not_old_batch_size(tmp_path):
    job = make_job(tmp_path, count=8, workers=2, budget=1, manual=True)
    decisions_path = job / "decisions.jsonl"
    q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                         ["m06", "m04", "m01", "m07", "m02"])
    set_pause(job, False)
    first = runner._queue_next(job, tmp_path, {"batch_size": 1, "parallel_workers": 2})
    assert first["batch"]["marker_ids"] == ["m06", "m04"]
    write_results(job, first)
    runner.finalize_turn(job, first)
    second = runner._queue_next(job, tmp_path, {"batch_size": 1, "parallel_workers": 2})
    assert second["batch"]["marker_ids"] == ["m01", "m07"]


def test_manual_selection_accepts_more_than_old_batch_limit(tmp_path):
    job = make_job(tmp_path, count=77, workers=1, budget=10, manual=True)
    ids = [f"m{i:02}" for i in range(77)]
    queued = q.enqueue_marker_ids(job / "markers.inventory.json", job / "decisions.jsonl", ids)
    assert queued["queued"] == ids
    set_pause(job, False)
    first = runner._queue_next(job, tmp_path, {"batch_size": 10, "parallel_workers": 1})
    assert first["batch"]["marker_ids"] == ["m00"]
    write_results(job, first)
    runner.finalize_turn(job, first)
    assert q.priority_marker_ids(job / "decisions.jsonl") == ids[1:]


def test_manual_selection_rejects_empty_launch_without_spawning(tmp_path):
    job = make_job(tmp_path, count=3, manual=True)
    (job / "START_PROMPT.txt").write_text("local prompt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Очередь пуста"):
        runner.launch_runner(job, tmp_path)
    assert not (job / runner.RUN_FILE).exists()


def test_manual_selection_keeps_independent_confirmed_verification(tmp_path):
    job = make_job(tmp_path, count=2, manual=True)
    rows = q.load_decisions(job / "decisions.jsonl")
    rows[0] = result_for(job, "m00", "Confirmed")
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    next_work = runner._queue_next(job, tmp_path, {"parallel_workers": 1})
    assert next_work["verification_only"] is True
    assert next_work["batch"]["marker_ids"] == ["m00"]


def test_live_queue_appends_after_current_assignment_and_skips_duplicate(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=6, workers=1, budget=4)
    decisions_path = job / "decisions.jsonl"
    current = claim(job, budget=4)
    assert current["batch"]["marker_ids"] == ["m00"]
    monkeypatch.setattr(runner, "read_run_record", lambda _job: {"active": True, "status": "running"})

    unchanged = q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path, ["m00"])
    assert unchanged["added"] == []
    assert unchanged["already_active"] == ["m00"]
    assert not (job / "control.json").exists()

    queued = q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                                  ["m00", "m03", "m02"])
    assert queued["added"] == ["m03", "m02"]
    assert queued["queued"] == ["m00", "m03", "m02"]
    control = json.loads((job / "control.json").read_text(encoding="utf-8"))
    status = json.loads((job / "workers.status.json").read_text(encoding="utf-8"))
    assert control["pause_requested"] is False
    assert control["manual_queue_requested"] is True
    assert status["one_shot"] is True
    assert status["workers"][0]["marker_ids"] == ["m00"]

    write_results(job, current)
    runner.finalize_turn(job, current)
    for mid in ("m03", "m02"):
        following = claim(job, budget=4)
        assert following["batch"]["marker_ids"] == [mid]
        write_results(job, following)
        runner.finalize_turn(job, following)
    assert claim(job, budget=4)["paused"]
    assert [row["marker_id"] for row in q.load_decisions(decisions_path) if row["verdict"]] == [
        "m00", "m02", "m03",
    ]


def test_live_queue_two_agents_preserves_both_assignments_and_recheck(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=6, workers=2, budget=4)
    decisions_path = job / "decisions.jsonl"
    rows = q.load_decisions(decisions_path)
    rows[5] = result_for(job, "m05")
    q.atomic_write_jsonl(decisions_path, rows)
    current = claim(job, budget=4, workers=2)
    assert current["batch"]["marker_ids"] == ["m00", "m01"]
    monkeypatch.setattr(runner, "read_run_record", lambda _job: {"active": True, "status": "running"})

    queued = q.enqueue_marker_ids(job / "markers.inventory.json", decisions_path,
                                  ["m01", "m04", "m05"])
    assert queued["added"] == ["m04", "m05"]
    assert queued["rechecks"] == ["m05"]
    assert queued["queued"] == ["m00", "m01", "m04", "m05"]
    assert q.load_decisions(decisions_path)[5]["verdict"] == "False Positive"
    write_results(job, current)
    runner.finalize_turn(job, current)
    following = claim(job, budget=4, workers=2)
    assert following["batch"]["marker_ids"] == ["m04", "m05"]
    assert q.load_decisions(decisions_path)[5]["verdict"] is None
    write_results(job, following)
    runner.finalize_turn(job, following)
    assert claim(job, budget=4, workers=2)["paused"]


@pytest.mark.parametrize("bad", ["missing", "foreign_id", "wrong_policy", "proof_gap", "forged_manual"])
def test_bad_result_does_not_partially_apply(tmp_path, bad):
    job = make_job(tmp_path, workers=2)
    ctx = claim(job, workers=2)
    write_results(job, ctx)
    path = job / "notes" / f"batch-{ctx['batch_number']:03d}-worker-2.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    if bad == "missing":
        path.unlink()
    else:
        if bad == "foreign_id": rows[0]["marker_id"] = "m19"
        if bad == "wrong_policy": rows[0].pop("decision_policy_version")
        if bad == "proof_gap": rows[0]["proof_gaps"] = ["unknown"]
        if bad == "forged_manual":
            rows[0].update(counterevidence=[], manual_verdict_override={"verdict": "False Positive"})
        q.atomic_write_json(path, rows)
    original = (job / "decisions.jsonl").read_bytes()
    with pytest.raises(RuntimeError): runner.finalize_turn(job, ctx)
    assert (job / "decisions.jsonl").read_bytes() == original


@pytest.mark.parametrize("verdict", ["Unclear", None])
def test_missing_evidence_stays_unfinished(tmp_path, verdict):
    job = make_job(tmp_path)
    ctx = claim(job)
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [
        {"marker_id": "m00", "verdict": verdict, "analysis_status": "needs_context",
         "proof_gaps": ["Need actual embedded JSON provider values"]}])
    with pytest.raises(runner.IncompleteAnalysisError): runner.finalize_turn(job, ctx)
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] is None


def test_status_alias_with_complete_evidence_is_validated_and_saved(tmp_path):
    job = make_job(tmp_path, count=2)
    ctx = claim(job)
    row = result_for(job, "m00")
    row["status"] = row.pop("verdict")
    row.pop("proof_gaps")  # An absent empty optional array is not missing evidence.
    path = job / "notes" / "batch-001-worker-1.json"
    q.atomic_write_json(path, [row])
    assert q.saved_draft_ids(job, q.load_decisions(job / "decisions.jsonl")) == {"m00"}
    assert q.load_worker_results([path])[0]["verdict"] == "False Positive"
    runner.finalize_turn(job, ctx)
    applied = q.load_decisions(job / "decisions.jsonl")[0]
    assert applied["verdict"] == "False Positive"
    assert applied["proof_gaps"] == []
    assert "status" not in applied


def test_saved_status_alias_draft_can_be_reviewed_and_approved(tmp_path):
    job = make_job(tmp_path, count=1)
    row = result_for(job, "m00")
    row["status"] = row.pop("verdict")
    row.pop("proof_gaps")
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [row])
    result = q.approve_saved_draft(job / "markers.inventory.json", job / "decisions.jsonl", "m00")
    assert result["applied"] == ["m00"]
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] == "False Positive"


def test_conflicting_status_and_verdict_are_rejected(tmp_path):
    job = make_job(tmp_path, count=1)
    row = result_for(job, "m00")
    row["status"] = "Confirmed"
    current = q.load_decisions(job / "decisions.jsonl")[0]
    assert any("contradicts" in error for error in q.validate_worker_result(row, current))


def test_malformed_status_does_not_crash_draft_scan(tmp_path):
    job = make_job(tmp_path, count=1)
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [
        {"marker_id": "m00", "status": ["False Positive"], "verdict": None},
    ])
    assert q.saved_draft_ids(job, q.load_decisions(job / "decisions.jsonl")) == set()


def test_confirmed_requires_independent_verification(tmp_path):
    job = make_job(tmp_path)
    ctx = claim(job)
    write_results(job, ctx, "Confirmed")
    runner.finalize_turn(job, ctx)
    verify = runner.verification_context(job, ctx)
    assert verify["verification_only"]
    assert q.load_decisions(job / "decisions.jsonl")[0]["verification"]["status"] == "pending"
    q.atomic_write_json(job / "notes" / "verify-batch-001-verifier-1.json", [
        {"marker_id": "m00", "decision": "verified", "verifier_id": "independent",
         "reason": "Rechecked", "evidence": ["same.go:1"], "rechecked_paths": ["entry -> sink"]}])
    runner.finalize_turn(job, verify)
    assert q.load_decisions(job / "decisions.jsonl")[0]["verification"]["status"] == "verified"


def test_pause_blocks_prepared_context_and_resume_resets_quota(tmp_path):
    job = make_job(tmp_path)
    ctx = claim(job)
    q.atomic_write_json(job / "batch-context.json", ctx)
    set_pause(job, True)
    assert runner.prepare_batch(job, tmp_path, {}, tmp_path, "exact-revision", "launch")["paused"]
    q.atomic_write_json(job / "control.json", {"run_remaining": 0, "single_batch_completed": True})
    set_pause(job, False)
    assert claim(job)["batch"]


def test_resume_after_failed_run_keeps_saved_quota(tmp_path):
    job = make_job(tmp_path, count=20, budget=10)
    q.atomic_write_json(job / "control.json", {
        "run_remaining": 4, "single_batch_completed": False, "pause_requested": False,
    })
    q.atomic_write_json(job / "codex-run.json", {"active": False, "status": "failed"})
    set_pause(job, False)
    assert json.loads((job / "control.json").read_text(encoding="utf-8"))["run_remaining"] == 4
    assert claim(job, budget=10)["batch"]["marker_ids"] == ["m00"]


def test_source_request_is_snapshot_bound_and_cannot_loop(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    ctx = claim(job)
    q.atomic_write_json(tmp_path / "svacer-settings.json", {"mcp_url": "http://local-test"})
    monkeypatch.setenv("SVACER_LOCAL_MCP_TOKEN", "dummy-test-value")
    calls = []
    async def fake_call(url, token, tool, args):
        calls.append(args)
        return json.dumps({"content": "source", "line": 0, "total_lines": 1})
    monkeypatch.setattr(runner, "call_mcp_tool", fake_call)
    request = job / "notes" / "source-requests-001.json"
    payload = [{"file_path": "external/pkg/source.go", "reason": "prove constructor contract"}]
    q.atomic_write_json(request, payload)
    assert runner.resolve_source_requests(job, tmp_path, ctx)
    assert calls[0]["snapshot_id"] == "exact-snapshot"
    assert (job / ctx["external_sources"][0]["local_path"]).exists()
    q.atomic_write_json(request, payload)
    with pytest.raises(runner.IncompleteAnalysisError): runner.resolve_source_requests(job, tmp_path, ctx)
    assert len(calls) == 1


def test_exact_snapshot_source_is_paged_and_retried(monkeypatch):
    lines = [f"source line {number}" for number in range(1, 63)]
    calls = []

    async def fake_call(_url, _token, tool, args):
        assert tool == "get_advanced_file_preview"
        calls.append((args["line"], args["after"]))
        if args["line"] == 31 and args["after"] == 29:
            raise RuntimeError("upstream timeout")
        start = args["line"] - 1
        selected = lines[start:start + args["after"] + 1]
        return json.dumps({"line": start, "total_lines": len(lines),
                           "content": "\n".join(selected) + "\n"})

    monkeypatch.setattr(runner, "call_mcp_tool", fake_call)
    preview = runner.fetch_snapshot_source("http://local-test", "dummy-test-value", "snapshot", "dep.go")
    assert preview["content"].splitlines() == lines
    assert preview["total_lines"] == 62
    assert calls[:3] == [(1, 29), (31, 29), (31, 14)]


def test_exact_snapshot_source_rejects_overlapping_pages(monkeypatch):
    async def fake_call(_url, _token, _tool, args):
        return json.dumps({"line": 0, "total_lines": 35,
                           "content": "\n".join("x" for _ in range(args["after"] + 1))})

    monkeypatch.setattr(runner, "call_mcp_tool", fake_call)
    with pytest.raises(ValueError, match="Страницы исходника"):
        runner.fetch_snapshot_source("http://local-test", "dummy-test-value", "snapshot", "dep.go")


def test_marker_trace_read_retries_and_preserves_request(monkeypatch):
    calls = []

    async def fake_call(_url, _token, _tool, args):
        calls.append(dict(args))
        if len(calls) < 3:
            raise RuntimeError("temporary backend timeout")
        return json.dumps({"markers": [{"id": "m00"}]})

    monkeypatch.setattr(runner, "call_mcp_tool", fake_call)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    request = {"snapshot_id": "snapshot", "warnClass": ["NULL"]}
    assert runner.fetch_marker_group("http://local-test", "dummy-test-value", request)["markers"][0]["id"] == "m00"
    assert calls == [request, request, request]


def test_marker_trace_read_stops_after_bounded_failures(monkeypatch):
    calls = []

    async def fake_call(*args):
        calls.append(1)
        raise RuntimeError("temporary backend timeout")

    monkeypatch.setattr(runner, "call_mcp_tool", fake_call)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="Назначение сохранено"):
        runner.fetch_marker_group("http://local-test", "dummy-test-value", {})
    assert len(calls) == 3


def test_marker_trace_read_reports_timeout_without_raw_mcp_text(monkeypatch):
    async def failed(*args):
        raise RuntimeError(
            "Error executing tool get_markers: API request GET "
            "http://internal.example/api/public/markers failed: ReadTimeout"
        )

    monkeypatch.setattr(runner, "call_mcp_tool", failed)
    with pytest.raises(RuntimeError, match=r"1 попыток \(ReadTimeout\)") as result:
        runner.fetch_marker_group("http://local-test", "dummy-test-value", {}, max_attempts=1)
    assert "internal.example" not in str(result.value)


def test_preflight_cache_requires_exact_snapshot_revision_filter_and_complete_trace(tmp_path):
    job = make_job(tmp_path, count=1)
    preflight = job / "preflight"
    preflight.mkdir()
    report = {"snapshot_id": "exact-snapshot", "revision": "exact-revision"}
    arguments = {"advanced_filter": q.GOST_FILTER, "warnClass": ["NULL"],
                 "file": ["same.go"], "traces": True, "checker_info": True,
                 "fields": ["*"], "limit": 0}
    marker = {"id": "m00", "warnClass": "NULL", "file": "same.go", "line": 1,
              "traces": [{"locations": [{"file": "same.go", "line": 1}]}]}
    payload = {"markers": [marker], "total_count": 1, "returned_count": 1,
               "truncated": False, "filters_applied": arguments}
    q.atomic_write_json(preflight / "report.json", report)
    q.atomic_write_json(preflight / "trace-group-001.json", payload)
    metadata = {"snapshot_id": "exact-snapshot", "advanced_filter": q.GOST_FILTER}

    assert runner.cached_preflight_group(job, metadata, "exact-revision", arguments, {"m00"}) == payload
    assert runner.cached_preflight_group(job, metadata, "other-revision", arguments, {"m00"}) is None
    assert runner.cached_preflight_group(job, {**metadata, "snapshot_id": "other"},
                                         "exact-revision", arguments, {"m00"}) is None
    assert runner.cached_preflight_group(job, metadata, "exact-revision",
                                         {**arguments, "file": ["other.go"]}, {"m00"}) is None
    payload["markers"][0]["traces"] = []
    q.atomic_write_json(preflight / "trace-group-001.json", payload)
    assert runner.cached_preflight_group(job, metadata, "exact-revision", arguments, {"m00"}) is None


def test_prepare_batch_uses_validated_trace_cache_after_api_failure(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=1, budget=1, manual=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "same.go").write_text("package same\n", encoding="utf-8")
    (job / "preflight").mkdir()
    metadata = {"project_id": "project", "branch_id": "branch",
                "snapshot_id": "exact-snapshot", "advanced_filter": q.GOST_FILTER,
                "batch_size": 1, "parallel_workers": 1, "manual_selection_only": True,
                "codex_model": "gpt-6-astra"}
    q.atomic_write_json(job / "job.json", metadata)
    q.atomic_write_json(tmp_path / "svacer-settings.json", {"mcp_url": "http://local-test"})
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "launch", "active": True})
    q.atomic_write_json(job / "preflight" / "report.json",
                        {"snapshot_id": "exact-snapshot", "revision": "exact-revision"})
    payload = {"markers": [{"id": "m00", "warnClass": "NULL", "file": "same.go",
                            "line": 1, "traces": [{"locations": [{"file": "same.go", "line": 1}]}]}],
               "total_count": 1, "returned_count": 1, "truncated": False,
               "filters_applied": {"advanced_filter": q.GOST_FILTER,
                                   "warnClass": ["NULL"], "file": ["same.go"],
                                   "traces": True, "checker_info": True,
                                   "fields": ["*"], "limit": 0}}
    q.atomic_write_json(job / "preflight" / "trace-group-001.json", payload)
    q.enqueue_marker_ids(job / "markers.inventory.json", job / "decisions.jsonl", ["m00"])
    set_pause(job, False)
    monkeypatch.setenv("SVACER_LOCAL_MCP_TOKEN", "dummy-test-value")
    calls = []

    def failed(*args, **kwargs):
        calls.append(kwargs["max_attempts"])
        raise RuntimeError("Svacer API read failed: ReadTimeout")

    monkeypatch.setattr(runner, "fetch_marker_group", failed)
    context = runner.prepare_batch(job, tmp_path, metadata, repository, "exact-revision", "launch")
    assert calls == [1], context
    assert context["cached_trace_groups"] == [["m00"]]
    assert context["codex_model"] == "gpt-6-astra"
    assert json.loads((job / runner.BATCH_CONTEXT_FILE).read_text(encoding="utf-8"))["codex_model"] == "gpt-6-astra"
    assert context["trace_history_unverified"] is True
    assert len(context["trace_files"]) == 1
    assert "history freshness is not verified" in runner.build_runtime_prompt(job, tmp_path, context)


def test_wontfix_needs_real_defect_and_disposition(tmp_path):
    job = make_job(tmp_path)
    current = q.load_decisions(job / "decisions.jsonl")[0]
    row = result_for(job, "m00", "Won't fix")
    assert not q.validate_worker_result(row, current)
    row["defect_scope"] = "none"
    assert q.validate_worker_result(row, current)
    row["defect_scope"] = "component"
    row["disposition_reason"] = ""
    assert q.validate_worker_result(row, current)


@pytest.mark.parametrize("verdict,component,product", [
    ("False Positive", False, False),
    ("Won't fix", True, False),
    ("Confirmed", True, True),
])
def test_policy_v2_verdict_matrix(tmp_path, verdict, component, product):
    job = make_job(tmp_path)
    current = q.load_decisions(job / "decisions.jsonl")[0]
    row = result_for(job, "m00", verdict)
    row.update(decision_policy_version=2, component_defect_proven=component,
               product_defect_reachable=product)
    assert q.validate_worker_result(row, current) == []
    row["product_defect_reachable"] = not product
    assert any("verdict contradicts" in error for error in q.validate_worker_result(row, current))


def test_policy_v2_rejects_unknown_disguised_as_final_or_missing_policy(tmp_path):
    job = make_job(tmp_path)
    current = q.load_decisions(job / "decisions.jsonl")[0]
    row = result_for(job, "m00", "Won't fix")
    row.update(decision_policy_version=2, component_defect_proven=None,
               product_defect_reachable=False)
    assert any("verdict contradicts" in error for error in q.validate_worker_result(row, current))
    row.pop("decision_policy_version")
    assert any("decision_policy_version" in error for error in q.validate_worker_result(row, current))
    row = result_for(job, "m00", "Unclear")
    row.update(decision_policy_version=2, component_defect_proven=True,
               product_defect_reachable=None, proof_gaps=["Не доказана достижимость из продукта"])
    assert q.validate_worker_result(row, current) == []
    row["product_defect_reachable"] = False
    assert any("Unclear requires" in error for error in q.validate_worker_result(row, current))


def test_runtime_prompt_separates_component_defect_from_product_reachability(tmp_path):
    job = make_job(tmp_path)
    prompt = runner.build_runtime_prompt(job, tmp_path, claim(job))
    assert "true / false -> Won't fix" in prompt
    assert "false / false -> False Positive" in prompt
    assert "decision_policy_version=2" in prompt
    assert "product path is\n  proven not to reach it" in prompt


def test_model_instructions_are_english_but_require_a_russian_comment(tmp_path):
    job = make_job(tmp_path)
    context = claim(job)
    prompt = runner.build_runtime_prompt(job, tmp_path, context)
    verification_prompt = runner.build_runtime_prompt(
        job, tmp_path, {**context, "verification_only": True},
    )
    manual = (Path(__file__).resolve().parents[1] / "CODEX_TASK.md").read_text(encoding="utf-8")

    assert not any("\u0400" <= char <= "\u04ff" for char in prompt)
    assert not any("\u0400" <= char <= "\u04ff" for char in verification_prompt)
    assert not any("\u0400" <= char <= "\u04ff" for char in manual.replace("ГОСТ", ""))
    assert "translate only the\ncomment field into clear Russian" in prompt
    assert "Use concise English for verifier fields" in verification_prompt


@pytest.mark.parametrize("mode", ["good", "missing", "incomplete", "stopped", "confirmed"])
def test_runner_end_to_end_without_model_or_network(tmp_path, monkeypatch, mode):
    job = make_job(tmp_path, count=3, budget=3, mode="until_complete")
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("test", encoding="utf-8")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "test-launch", "active": True})
    monkeypatch.setattr(runner, "_job_paths", lambda path: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, budget=3))
    calls = []
    def fake_turn(job, app, launch, ctx, start, index):
        calls.append(list(ctx["batch"]["marker_ids"]))
        if mode == "stopped":
            q.atomic_write_json(job / "stop-request.json", {"launch_id": launch})
            return -1, 0
        if mode == "incomplete":
            a = ctx["batch"]["assignments"][0]
            q.atomic_write_json(job / "notes" / f"batch-{ctx['batch_number']:03d}-worker-1.json", [
                {"marker_id": a["marker_ids"][0], "analysis_status": "needs_context", "proof_gaps": ["need provider"]}])
        elif mode == "confirmed" and ctx.get("verification_only"):
            q.atomic_write_json(job / "notes" / f"verify-batch-{ctx['batch_number']:03d}-verifier-1.json", [
                {"marker_id": ctx["batch"]["marker_ids"][0], "decision": "verified",
                 "verifier_id": "reviewer", "reason": "independently checked",
                 "evidence": ["same.go:1"], "rechecked_paths": ["entry -> sink"]}])
        elif mode != "missing":
            write_results(job, ctx, "Confirmed" if mode == "confirmed" else "False Positive")
        return 0, 0
    monkeypatch.setattr(runner, "_run_one_codex_turn", fake_turn)
    runner.run_job(job, "test-launch")
    record = json.loads((job / runner.RUN_FILE).read_text(encoding="utf-8"))
    expected = {"good": "completed", "confirmed": "completed", "missing": "failed",
                "incomplete": "incomplete", "stopped": "stopped"}[mode]
    assert record["status"] == expected, record
    assert record["active"] is False
    assert len(calls) == (6 if mode == "confirmed" else 3 if mode in {"good", "incomplete"} else 1)
    if mode in {"incomplete", "missing", "stopped"}:
        assert all(not r["verdict"] for r in q.load_decisions(job / "decisions.jsonl"))


def test_single_launch_attempts_ten_after_one_incomplete(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=12, budget=10, mode="single_batch")
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("test", encoding="utf-8")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "test-launch", "active": True})
    monkeypatch.setattr(runner, "_job_paths", lambda path: (tmp_path, prompt))
    monkeypatch.setattr(runner, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    monkeypatch.setattr(runner, "prepare_batch", lambda *args: claim(job, budget=10))
    seen = []

    def fake_turn(_job, _app, _launch, ctx, _start, _index):
        marker_id = ctx["batch"]["marker_ids"][0]
        seen.append(marker_id)
        if marker_id == "m00":
            q.atomic_write_json(job / "notes" / f"batch-{ctx['batch_number']:03d}-worker-1.json", [
                {"marker_id": marker_id, "analysis_status": "needs_context", "proof_gaps": ["need provider"]},
            ])
        else:
            write_results(job, ctx)
        return 0, 0

    monkeypatch.setattr(runner, "_run_one_codex_turn", fake_turn)
    assert runner.run_job(job, "test-launch") == 3
    assert seen == [f"m{i:02}" for i in range(10)]
    decisions = q.load_decisions(job / "decisions.jsonl")
    assert sum(bool(row["verdict"]) for row in decisions) == 9
    assert decisions[0]["verdict"] is None
    assert (job / "incomplete-analysis.json").exists()
    control = json.loads((job / "control.json").read_text(encoding="utf-8"))
    assert control["run_remaining"] == 0
    assert control["single_batch_completed"] is True


def test_stop_signal_survives_progress_race_and_new_launch_is_separate(tmp_path):
    job = make_job(tmp_path)
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "old", "active": True})
    q.atomic_write_json(job / "stop-request.json", {"launch_id": "old"})
    runner._update_run(job, "old", stop_requested=False, phase="running")
    assert runner._stop_requested(job, "old")
    q.atomic_write_json(job / runner.RUN_FILE, {"launch_id": "new", "active": True})
    assert not runner._stop_requested(job, "new")


def test_resume_after_one_shot_reopens_queue(tmp_path):
    job = make_job(tmp_path, mode="until_complete")
    q.atomic_write_json(job / "control.json", {"pause_requested": True, "one_shot_completed": True})
    set_pause(job, False)
    assert claim(job)["batch"]["count"] == 1


def test_source_mapping_never_uses_unrelated_checkout(tmp_path):
    repo = tmp_path / "repository"
    (repo / "internal").mkdir(parents=True)
    (repo / "internal" / "a.go").write_text("package a", encoding="utf-8")
    outside = tmp_path / "outside.go"
    outside.write_text("stale unrelated source", encoding="utf-8")
    assert runner._repository_has_source(repo, "/src/src/internal/a.go")
    assert not runner._repository_has_source(repo, str(outside))


def test_unfinished_note_visible_in_gui(tmp_path):
    from triage_gui import unapplied_draft_results, friendly_run_state
    job = make_job(tmp_path)
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [
        {"marker_id": "m00", "analysis_status": "needs_context", "proof_gaps": ["provider"]}])
    assert unapplied_draft_results(job, q.load_decisions(job / "decisions.jsonl"))["m00"]["analysis_status"] == "needs_context"
    assert friendly_run_state({"status": "incomplete"}, {})[0] == "Не завершена"


def test_crashed_writer_does_not_leave_queue_permanently_busy(tmp_path):
    import subprocess
    job = make_job(tmp_path)
    script = (
        "import sys,os;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
        "from triage_queue import decision_lock\n"
        "with decision_lock(Path(sys.argv[2])): os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(Path(q.__file__).parent),
                    str(job / "decisions.jsonl")], check=True, timeout=10,
                   **runner.hidden_subprocess_kwargs())
    assert claim(job)["batch"]["count"] == 1


def test_competing_writers_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    job = make_job(tmp_path)
    waiting = threading.Event()
    def contender():
        waiting.set()
        return claim(job)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with q.decision_lock(job / "decisions.jsonl"):
            pending = pool.submit(contender)
            assert waiting.wait(1)
            assert not pending.done()
        assert pending.result(timeout=5)["batch"]["count"] == 1


def test_finished_marker_can_get_verification_without_primary_context(tmp_path):
    job = make_job(tmp_path)
    rows = q.load_decisions(job / "decisions.jsonl")
    rows[0] = result_for(job, "m00", "Confirmed")
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    ctx = runner._queue_next(job, tmp_path, {"batch_size": 15, "parallel_workers": 2})
    assert ctx["verification_only"]
    assert ctx["batch"]["marker_ids"] == ["m00"]


def test_increasing_workers_takes_effect_on_next_assignment(tmp_path):
    job = make_job(tmp_path, mode="until_complete")
    first = claim(job)
    assert claim(job, workers=2)["batch"]["count"] == 1  # don't reassign active work
    write_results(job, first)
    runner.finalize_turn(job, first)
    second = claim(job, workers=2)
    assert second["batch"]["marker_ids"] == ["m01", "m02"]
    assert all(a["count"] == 1 for a in second["batch"]["assignments"])


def test_state_distinguishes_current_batch_result_from_old_retry_draft(tmp_path):
    from triage_dashboard import collect_state

    job = make_job(tmp_path, count=2, workers=2)
    first = claim(job, workers=2)
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [
        {"marker_id": first["batch"]["marker_ids"][0], "verdict": "False Positive"},
    ])
    q.atomic_write_json(job / "notes" / "batch-000-worker-2.json", [
        {"marker_id": first["batch"]["marker_ids"][1], "verdict": "False Positive"},
    ])

    state = collect_state(job)

    assert state["workers"][1]["current_saved_marker_ids"] == [first["batch"]["marker_ids"][0]]
    assert state["workers"][2]["saved_marker_ids"] == [first["batch"]["marker_ids"][1]]
    assert state["workers"][2]["current_saved_marker_ids"] == []


@pytest.mark.parametrize("preview", [{}, {"error": "not found"}, {"content": "a", "line": 0, "total_lines": 5},
                                      {"content": "a", "line": 10, "total_lines": 1}])
def test_empty_or_truncated_source_is_not_accepted(preview):
    with pytest.raises(ValueError): runner.validate_source_preview(preview)


def test_source_request_failure_stays_bounded(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    ctx = claim(job)
    q.atomic_write_json(tmp_path / "svacer-settings.json", {})
    monkeypatch.setenv("SVACER_LOCAL_MCP_TOKEN", "dummy-test-value")
    async def failed(*args): raise RuntimeError("HTTP 500")
    monkeypatch.setattr(runner, "call_mcp_tool", failed)
    q.atomic_write_json(job / "notes" / "source-requests-001.json", [
        {"file_path": "provider.go", "reason": "missing constructor"}])
    assert runner.resolve_source_requests(job, tmp_path, ctx)
    assert ctx["source_request_errors"] == [{"file_path": "provider.go", "status": "fetch_failed"}]
    assert ctx["source_request_round"] == 1
    assert not (job / "notes" / "source-requests-001.json").exists()
    assert all(not row["verdict"] for row in q.load_decisions(job / "decisions.jsonl"))


def test_source_request_fetches_other_files_when_one_path_fails(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    ctx = claim(job)
    q.atomic_write_json(tmp_path / "svacer-settings.json", {})
    monkeypatch.setenv("SVACER_LOCAL_MCP_TOKEN", "dummy-test-value")

    async def fake_call(_url, _token, _tool, args):
        if args["file_path"] == "missing.go":
            raise RuntimeError("HTTP 500")
        return json.dumps({"content": "source\n", "line": 0, "total_lines": 1})

    monkeypatch.setattr(runner, "call_mcp_tool", fake_call)
    q.atomic_write_json(job / "notes" / "source-requests-001.json", [
        {"file_path": "missing.go", "reason": "check dependency"},
        {"file_path": "available.go", "reason": "check dependency"},
    ])
    assert runner.resolve_source_requests(job, tmp_path, ctx)
    assert [entry["file_path"] for entry in ctx["external_sources"]] == ["available.go"]
    assert ctx["source_request_errors"] == [{"file_path": "missing.go", "status": "fetch_failed"}]
    assert (job / ctx["external_sources"][0]["local_path"]).is_file()


def test_reset_preserves_decisions_and_drafts(tmp_path):
    job = make_job(tmp_path)
    ctx = claim(job)
    write_results(job, ctx)
    before = (job / "decisions.jsonl").read_bytes()
    q.atomic_write_json(job / "control.json", {"run_remaining": 0})
    q.reset_queue_assignments(job / "decisions.jsonl")
    assert (job / "decisions.jsonl").read_bytes() == before
    assert (job / "notes" / "batch-001-worker-1.json").exists()
    set_pause(job, False)
    assert claim(job)["batch"]


def test_saved_draft_is_not_assigned_or_consumed(tmp_path):
    job = make_job(tmp_path, count=3, mode="until_complete")
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [result_for(job, "m00")])
    q.atomic_write_json(job / "notes" / "batch-001-worker-2.json", [
        {"marker_id": "m01", "analysis_status": "needs_context", "proof_gaps": ["missing provider"]}])
    assert q.saved_draft_ids(job, q.load_decisions(job / "decisions.jsonl")) == {"m00", "m01"}
    first = claim(job)
    assert first["batch"]["marker_ids"] == ["m02"]
    write_results(job, first)
    runner.finalize_turn(job, first)
    assert claim(job)["batch"] is None
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] is None


def test_old_priority_and_assignment_do_not_requeue_draft(tmp_path):
    job = make_job(tmp_path, count=3, mode="until_complete", workers=2)
    original = claim(job, workers=2)
    assert original["batch"]["marker_ids"] == ["m00", "m01"]
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [result_for(job, "m00")])
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ["m00", "m02"]})
    next_batch = claim(job, workers=2)
    assert next_batch["batch"]["marker_ids"] == ["m02"]
    assert json.loads((job / "control.json").read_text(encoding="utf-8"))["priority_marker_ids"] == ["m02"]


def test_explicit_retry_can_choose_saved_draft_only(tmp_path):
    job = make_job(tmp_path, count=3, mode="until_complete")
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [result_for(job, "m00")])
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ["m00"],
                                               "single_marker_requested": True})
    batch = claim(job)
    assert batch["batch"]["marker_ids"] == ["m00"]
    assert batch["one_shot"] is True


def test_incomplete_marker_is_not_requeued_automatically(tmp_path):
    job = make_job(tmp_path, count=2, mode="until_complete")
    q.atomic_write_json(job / "incomplete-analysis.json", {"m00": {"status": "incomplete"}})
    assert claim(job)["batch"]["marker_ids"] == ["m01"]


def test_mark_incomplete_finishes_worker_status_without_applying_verdict(tmp_path):
    job = make_job(tmp_path, count=2, mode="until_complete")
    context = claim(job, workers=2)

    runner.mark_incomplete(job, context, "Need the exact provider implementation")

    status = json.loads((job / "workers.status.json").read_text(encoding="utf-8"))
    assert status["state"] == "incomplete"
    assert status["reason"] == "Need the exact provider implementation"
    assert {worker["status"] for worker in status["workers"]} == {"incomplete"}
    decisions = q.load_decisions(job / "decisions.jsonl")
    assert all(row["verdict"] is None for row in decisions)


def test_draft_disappears_after_manual_approval_without_deleting_note(tmp_path):
    job = make_job(tmp_path, count=2, mode="until_complete")
    note = job / "notes" / "batch-001-worker-1.json"
    q.atomic_write_json(note, [result_for(job, "m00")])
    q.approve_saved_draft(job / "markers.inventory.json", job / "decisions.jsonl", "m00")
    assert note.exists()
    assert "m00" not in q.saved_draft_ids(job, q.load_decisions(job / "decisions.jsonl"))
    assert claim(job)["batch"]["marker_ids"] == ["m01"]
