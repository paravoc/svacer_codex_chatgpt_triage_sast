"""Real subprocesses, deterministic fixtures; never calls a model or Svacer."""
import copy
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as r
import triage_queue as q
from continuous_analysis import Scheduler, run_continuous, resumable_context
from parallel_analysis import read_worker_runtime
from triage_gui import analysis_configuration_text, marker_assignments, current_run_queue_ids
from test_queue_execution import make_job, claim, result_for


def setup(tmp_path, monkeypatch, *, mode="rolling", count=7, workers=3, budget=7, manual=False):
    job = make_job(tmp_path, count=count, workers=workers, budget=budget, manual=manual)
    if manual:
        q.atomic_write_json(job / "control.json", {"priority_marker_ids": [f"m{i:02}" for i in range(budget)],
                                                  "manual_queue_requested": True})
    initial = claim(job, budget=budget, workers=workers)
    initial.update(repository=str(tmp_path), schema_version=2, snapshot_id="exact-snapshot")
    r.atomic_json(job / r.RUN_FILE, {"launch_id": "test", "active": True})
    script = Path(__file__).with_name("fake_marker_process.py")
    monkeypatch.setattr(r, "find_codex_executable", lambda: sys.executable)
    monkeypatch.setattr(r, "codex_child_environment", lambda: {})
    monkeypatch.setattr(r, "build_codex_command", lambda exe, job, reply, model: [
        exe, str(script), str(reply.parent), mode, str(reply)])
    for i in range(count):
        r.atomic_json(job / f"fixture-marker-m{i:02}.json", [result_for(job, f"m{i:02}")])
    original_prepare = r.prepare_batch

    def prepare(job, app, metadata, repo, revision, launch, *, queued, context_file):
        context = {**queued, "repository": str(repo), "revision": revision, "snapshot_id": "exact-snapshot",
                   "continuous": True, "scheduler_context_file": context_file}
        r.atomic_json(job / context_file, context)
        return context
    monkeypatch.setattr(r, "prepare_batch", prepare)
    return job, initial, original_prepare


def execute(job, initial):
    return run_continuous(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)


def test_fast_slot_saves_history_and_starts_next_before_slow_peer_finishes(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch)
    outcome = []
    task = threading.Thread(target=lambda: outcome.append(execute(job, initial)))
    task.start()
    deadline = time.monotonic() + 15
    observed = False
    while task.is_alive() and time.monotonic() < deadline:
        decisions = q.load_decisions(job / "decisions.jsonl")
        if not decisions[0].get("verdict") and sum(bool(row.get("verdict")) for row in decisions) >= 4:
            history = (job / "marker-history.jsonl").read_text(encoding="utf-8")
            assert "m03" in history or "m04" in history
            observed = True
            break
        time.sleep(.03)
    task.join(timeout=20)
    assert not task.is_alive() and outcome == [(0, "", 0)]
    assert observed, "fast slots must commit results and refill before m00 exits"
    assert all(row.get("verdict") == "False Positive" for row in q.load_decisions(job / "decisions.jsonl"))
    starts = [json.loads(p.read_text()) for p in (job / "worker-runs").rglob("started.json")]
    assert len(starts) == 7 and len({row["pid"] for row in starts}) == 7
    intervals = []
    for directory in (job / "worker-runs").rglob("started.json"):
        intervals.append((json.loads(directory.read_text())["start"], 1))
        intervals.append((json.loads(directory.with_name("finished.json").read_text())["finish"], -1))
    live, peak = 0, 0
    for _, change in sorted(intervals):
        live += change
        peak = max(peak, live)
    assert peak == 3
    history = [json.loads(line) for line in (job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(history) == 7 and all(row["tokens_exact"] for row in history)
    assert all(row["duration_seconds"] > 0 for row in history)
    assert not r.read_json(job / "scheduler-state.json")["leases"]


@pytest.mark.parametrize("manual", [True, False])
def test_selection_and_single_batch_limit_do_not_expand_to_whole_project(tmp_path, monkeypatch, manual):
    job, initial, _ = setup(tmp_path, monkeypatch, count=10, budget=5, manual=manual)
    assert execute(job, initial) == (0, "", 0)
    assert sum(bool(row.get("verdict")) for row in q.load_decisions(job / "decisions.jsonl")) == 5
    assert len(list((job / "worker-runs").rglob("started.json"))) == 5


def test_incomplete_marker_is_preserved_without_blocking_or_repeating_peers(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, mode="rolling_incomplete", manual=True)
    assert execute(job, initial) == (0, "", 1)
    decisions = q.load_decisions(job / "decisions.jsonl")
    assert sum(bool(row.get("verdict")) for row in decisions) == 6
    assert not decisions[1].get("verdict")
    control = r.read_json(job / "control.json")
    assert control["priority_marker_ids"] == ["m01"]
    assert control["deferred_marker_ids"] == ["m01"]
    assert len(list((job / "worker-runs").rglob("started.json"))) == 7


@pytest.mark.parametrize("confirmed_ids", [["m01"], ["m01", "m02"]])
def test_confirmed_verification_is_local_to_its_slot(tmp_path, monkeypatch, confirmed_ids):
    job, initial, _ = setup(tmp_path, monkeypatch)
    for mid in confirmed_ids:
        r.atomic_json(job / f"fixture-marker-{mid}.json", [result_for(job, mid, "Confirmed")])
    assert execute(job, initial) == (0, "", 0)
    for decision in q.load_decisions(job / "decisions.jsonl"):
        if decision["marker_id"] in confirmed_ids:
            assert decision["verification"]["status"] == "verified"
    history = [json.loads(line) for line in (job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(history) == 7 + len(confirmed_ids)
    assert len([item for item in history if item["marker_id"] == "m01"]) == 2
    notes = list((job / "notes").glob("verify-batch-001-verifier-*.json"))
    assert len(notes) == len(confirmed_ids)


@pytest.mark.parametrize("mode,expected_status", [
    ("rolling_verifier_shape", "verified"),
    ("rolling_verifier_shape_fails", "pending"),
])
def test_verifier_shape_repair_is_bounded_without_repeating_primary_or_peers(tmp_path, monkeypatch, mode, expected_status):
    job, initial, _ = setup(tmp_path, monkeypatch, mode=mode, count=3, budget=3, manual=True)
    r.atomic_json(job / "fixture-marker-m01.json", [result_for(job, "m01", "Confirmed")])
    outcome = execute(job, initial)
    assert outcome[2] == (expected_status == "pending")
    decisions = {row["marker_id"]: row for row in q.load_decisions(job / "decisions.jsonl")}
    assert decisions["m01"]["verdict"] == "Confirmed"
    assert decisions["m01"]["verification"]["status"] == expected_status
    for mid in ("m00", "m02"):
        assert decisions[mid]["verdict"] == "False Positive"
    assert len(list((job / "worker-runs").rglob("prompt-*.txt"))) == 5  # three primaries + two verifier turns
    if expected_status == "pending":
        assert q.priority_marker_ids(job / "decisions.jsonl") == ["m01"]
        assert "m01" in r.read_json(job / "control.json")["deferred_marker_ids"]
    history = [json.loads(line) for line in (job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(history) == 4


def test_retry_pending_verifiers_never_reopens_primary_even_with_recheck_flags(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=5, budget=5, workers=2, manual=True)
    rows = [result_for(job, f"m{i:02}", "Confirmed") for i in range(5)]
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    control = r.read_json(job / "control.json")
    control["recheck_marker_ids"] = [row["marker_id"] for row in rows]
    r.atomic_json(job / "control.json", control)
    initial = r._queue_next(job, job.parent, {"parallel_workers": 2})
    assert initial["verification_only"] and initial["batch"]["count"] == 2
    initial.update(repository=str(tmp_path), revision="exact-revision", snapshot_id="exact-snapshot")
    assert execute(job, initial) == (0, "", 0)
    final = q.load_decisions(job / "decisions.jsonl")
    assert all(row["verification"]["status"] == "verified" for row in final)
    for before, after in zip(rows, final):
        assert {k: v for k, v in before.items() if k != "verification"} == {k: v for k, v in after.items() if k != "verification"}
    assert len(list((job / "worker-runs").rglob("prompt-*.txt"))) == 5
    assert all(p.parent.name == "verification" for p in (job / "worker-runs").rglob("prompt-*.txt"))
    notes = list((job / "notes").glob("verify-batch-*-verifier-*.json"))
    assert len(notes) == 5
    assert {r.read_json(p)[0]["marker_id"] for p in notes} == {row["marker_id"] for row in rows}


def test_configuration_text_uses_launch_capacity_not_later_settings():
    data = {"codex_model": "gpt-6-luna", "parallel_workers": 5}
    assert analysis_configuration_text(data, {}) == "Модель: gpt-6-luna · Параллельно: до 5 агентов"
    state = {"codex_run": {"active": True, "requested_model": "gpt-6-sol", "parallel_workers": 3},
             "worker_runtime": {"workers": {"m1": {"worker": 1, "state": "running"},
                                             "m2": {"worker": 2, "state": "preparing"},
                                             "m3": {"worker": 3, "state": "applied"}}}}
    assert analysis_configuration_text(data, state) == "Модель: gpt-6-sol · Параллельно: до 3 агентов · занято: 2/3"
    assert "по умолчанию Codex" in analysis_configuration_text({}, {})


def test_rolling_preparation_and_validation_remain_assigned_not_back_in_queue():
    state = {"codex_run": {"active": True}, "workers": {1: {"marker_ids": ["m1"]}},
             "worker_runtime": {"scheduler": "continuous", "workers": {"m1": {"state": "sources"}}}}
    assert marker_assignments(state) == {"m1": "Агент 1"}


def test_applied_recheck_does_not_flash_back_into_queue_before_slot_refill():
    state = {"codex_run": {"active": True}, "priority_marker_ids": ["m1", "m2"], "recheck_marker_ids": ["m1"],
             "workers": {1: {"marker_ids": ["m1"]}},
             "worker_runtime": {"scheduler": "continuous", "workers": {"m1": {"state": "applied"}}}}
    decisions = [{"marker_id": "m1", "verdict": "False Positive"}, {"marker_id": "m2", "verdict": None}]
    assert current_run_queue_ids(decisions, state, set(), ["m1", "m2"]) == ["m2"]


def test_saved_note_is_applied_without_another_model_call(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=1, workers=1, budget=1)
    r.atomic_json(job / "notes/batch-001-worker-1.json", [result_for(job, "m00")])
    monkeypatch.setattr(r, "find_codex_executable", lambda: pytest.fail("must recover the saved result"))
    assert execute(job, initial) == (0, "", 0)
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] == "False Positive"


def test_crash_after_apply_resumes_history_and_budget_once(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=1, workers=1, budget=1)
    scheduler = Scheduler(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)
    entry = scheduler.claim(1)
    context = r.read_json(job / entry["context_file"])
    r.atomic_json(job / "notes/batch-001-worker-1.json", [result_for(job, "m00")])
    r.finalize_turn(job, context)  # crash before history or lease removal
    monkeypatch.setattr(r, "find_codex_executable", lambda: pytest.fail("applied marker cannot rerun"))
    resume = resumable_context(job, "exact-revision", "exact-snapshot")
    assert execute(job, resume) == (0, "", 0)
    rows = (job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    # Replay the lease as if the process died between control commit and journal commit.
    state = r.read_json(job / "scheduler-state.json")
    state["leases"] = {"1": entry}
    r.atomic_json(job / "scheduler-state.json", state)
    assert execute(job, resumable_context(job, "exact-revision", "exact-snapshot")) == (0, "", 0)
    assert len((job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert r.read_json(job / "control.json")["run_remaining"] == 0


def test_interrupted_history_does_not_mask_success_on_next_launch(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=1, workers=1, budget=1)
    scheduler = Scheduler(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)
    entry = scheduler.claim(1)
    context = r.read_json(job / entry["context_file"])
    scheduler.history(context, entry, 3, "stopped")
    r.atomic_json(job / "notes/batch-001-worker-1.json", [result_for(job, "m00")])
    r.atomic_json(job / r.RUN_FILE, {"launch_id": "next-launch", "active": True})
    monkeypatch.setattr(r, "find_codex_executable", lambda: pytest.fail("saved result must not rerun"))
    outcome = run_continuous(r, job, job.parent, job.parent, "exact-revision", "next-launch", r.now_iso(),
                             resumable_context(job, "exact-revision", "exact-snapshot"))
    assert outcome == (0, "", 0)
    history = [json.loads(line) for line in (job / "marker-history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in history] == ["incomplete", "completed"]
    assert history[-1]["launch_id"] == "next-launch"


def test_stop_joins_children_and_keeps_exact_reservations(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, mode="stop", manual=True)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(execute(job, initial)))
    thread.start()
    deadline = time.monotonic() + 10
    pids = []
    while time.monotonic() < deadline:
        pids = [row["pid"] for row in read_worker_runtime(job).get("workers", {}).values() if row.get("pid")]
        if len(pids) == 3:
            break
        time.sleep(.03)
    assert len(pids) == 3
    r.atomic_json(job / "stop-request.json", {"launch_id": "test"})
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert not any(r.process_is_alive(pid) for pid in pids)
    assert len(r.read_json(job / "scheduler-state.json")["leases"]) == 3
    assert len(list((job / "worker-runs").rglob("started.json"))) == 3
    assert not any(row.get("verdict") for row in q.load_decisions(job / "decisions.jsonl"))
    assert len(r.read_json(job / "control.json")["priority_marker_ids"]) == 7


def test_reset_preserves_notes_and_removes_scheduler_reservations(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=2, workers=2, budget=2, manual=True)
    Scheduler(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)
    r.atomic_json(job / r.RUN_FILE, {"launch_id": "test", "active": False})
    r.atomic_json(job / "notes/batch-001-worker-1.json", [result_for(job, "m00")])
    before = (job / "notes/batch-001-worker-1.json").read_bytes()
    q.reset_queue_assignments(job / "decisions.jsonl")
    assert not resumable_context(job, "exact-revision", "exact-snapshot")
    assert (job / "notes/batch-001-worker-1.json").read_bytes() == before


def test_remove_stopped_marker_does_not_resurrect_its_reservation(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=2, workers=2, budget=2, manual=True)
    Scheduler(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)
    r.atomic_json(job / r.RUN_FILE, {"launch_id": "test", "active": False})
    q.dequeue_marker_ids(job / "decisions.jsonl", ["m00"])
    resumed = resumable_context(job, "exact-revision", "exact-snapshot")
    assert resumed["batch"]["marker_ids"] == ["m01"]


def test_changed_revision_cannot_resume_old_lease(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=1, workers=1, budget=1)
    Scheduler(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)
    with pytest.raises(ValueError, match="другой ревизии"):
        resumable_context(job, "different-revision", "exact-snapshot")


def test_new_assignment_preparation_uses_own_context_and_batch(tmp_path, monkeypatch):
    job, initial, original_prepare = setup(tmp_path, monkeypatch, count=4, workers=2, budget=4)
    scheduler = Scheduler(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)
    first = scheduler.claim(1)
    scheduler.finish(1, first, incomplete=True)
    entry = scheduler.claim(1)
    queued = r.read_json(job / entry["context_file"])
    metadata = {**r.read_json(job / "job.json"), "project_id": "p", "branch_id": "b", "advanced_filter": q.GOST_FILTER}
    monkeypatch.setenv("SVACER_LOCAL_MCP_TOKEN", "offline-test-token")
    monkeypatch.setattr(r, "read_app_settings", lambda app: {})
    monkeypatch.setattr(r, "cached_preflight_group", lambda *args: None)
    monkeypatch.setattr(r, "fetch_marker_group", lambda *args, **kwargs: {"markers": [
        {"id": entry["marker_id"], "file": "same.go", "line": 1}]})
    monkeypatch.setattr(r, "fetch_external_sources", lambda *args: ([], []))
    monkeypatch.setattr(r, "prepare_batch_dependencies", lambda *args: None)
    context = original_prepare(job, job.parent, metadata, job.parent, "exact-revision", "test",
                               queued=queued, context_file=entry["context_file"])
    assert context["batch_number"] == entry["batch"]
    assert context["continuous"] and context["scheduler_context_file"] == entry["context_file"]
    assert r.read_json(job / entry["context_file"])["batch"]["marker_ids"] == [entry["marker_id"]]
    assert context["trace_files"][0].startswith("raw")


def test_lower_concurrency_after_restart_does_not_deadlock_saved_leases(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=5, workers=5, budget=5)
    Scheduler(r, job, job.parent, job.parent, "exact-revision", "test", r.now_iso(), initial)
    metadata = r.read_json(job / "job.json")
    metadata["parallel_workers"] = 2
    r.atomic_json(job / "job.json", metadata)
    assert execute(job, resumable_context(job, "exact-revision", "exact-snapshot")) == (0, "", 0)
    assert all(row.get("verdict") for row in q.load_decisions(job / "decisions.jsonl"))


def test_late_queue_append_wakes_idle_slots(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=6, workers=3, budget=1, manual=True)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(execute(job, initial)))
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        workers = read_worker_runtime(job).get("workers", {})
        if workers.get("m00", {}).get("pid"):
            break
        time.sleep(.03)
    # Append while only m00 is running. Idle slots must still be alive to refill.
    with q.decision_lock(job / "decisions.jsonl"):
        control = r.read_json(job / "control.json")
        control["priority_marker_ids"] += ["m01", "m02", "m03", "m04", "m05"]
        r.atomic_json(job / "control.json", control)
    thread.join(timeout=15)
    assert not thread.is_alive() and outcomes == [(0, "", 0)]
    paths = list((job / "worker-runs").rglob("started.json"))
    assert len(paths) == 6
    first = next(path for path in paths if 'batch-001' in path.as_posix())
    slow_end = json.loads(first.with_name("finished.json").read_text())["finish"]
    assert sum(json.loads(path.read_text())["start"] < slow_end for path in paths) == 6


def test_run_job_enters_continuous_scheduler_and_records_completion(tmp_path, monkeypatch):
    job, initial, _ = setup(tmp_path, monkeypatch, count=4, workers=2, budget=4, manual=True)
    initial["scheduler_version"] = 2
    prepare_one = r.prepare_batch
    monkeypatch.setattr(r, "prepare_batch", lambda *args, **kwargs: prepare_one(*args, **kwargs) if kwargs else copy.deepcopy(initial))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("offline fixture", encoding="utf-8")
    monkeypatch.setattr(r, "_job_paths", lambda job: (tmp_path, prompt))
    monkeypatch.setattr(r, "prepare_repository", lambda *args: (tmp_path, "exact-revision"))
    assert r.run_job(job, "test") == 0
    record = r.read_json(job / r.RUN_FILE)
    assert not record["active"] and record["status"] == "completed"
    assert len(list((job / "worker-runs").rglob("started.json"))) == 4
