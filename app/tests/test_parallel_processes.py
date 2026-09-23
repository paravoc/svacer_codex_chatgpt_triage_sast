"""Prove real OS process overlap and isolation without invoking Codex or Svacer."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as r
from parallel_analysis import read_worker_runtime
from marker_history import append_batch_history, normalize_history_measurements
from triage_gui import codex_activity_entries, live_run_timing, marker_assignments
from test_queue_execution import make_job, claim, result_for


def setup_job(tmp_path, monkeypatch, mode="normal"):
    job = make_job(tmp_path, count=3, workers=3)
    context = claim(job, workers=3)
    r.atomic_json(job / r.RUN_FILE, {"launch_id": "test", "active": True})
    monkeypatch.setattr(r, "find_codex_executable", lambda: sys.executable)
    monkeypatch.setattr(r, "codex_child_environment", lambda: {})
    script = Path(__file__).with_name("fake_marker_process.py")
    monkeypatch.setattr(r, "build_codex_command",
                        lambda executable, job, last, model: [executable, str(script), str(last.parent), mode, str(last)])
    return job, context


def execute(job, context):
    return r._run_one_codex_turn(job, job, "test", context, r.now_iso(), 1)


def test_processes_overlap_with_separate_logs_and_exact_metrics(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch)
    assert execute(job, context) == (0, 0)
    runtime = read_worker_runtime(job)["workers"]
    starts, ends, pids = [], [], []
    for mid, row in runtime.items():
        directory = (job / row["event_log"]).parent
        start = r.read_json(directory / "started.json")
        end = r.read_json(directory / "finished.json")
        starts.append(start["start"])
        ends.append(end["finish"])
        pids.append(start["pid"])
        entries = codex_activity_entries(job, event_file=row["event_log"])
        assert len(entries) == 1 and mid in entries[0]
        prompt = (directory / "prompt.txt").read_text(encoding="utf-8")
        assert "Субагентов не запускай" in prompt
        assert row["pid"] is None and row["state"] == "incomplete"
    assert len(set(pids)) == 3
    assert max(starts) < min(ends), "all processes must start before any one finishes"
    assert r.read_codex_usage(job)["input_tokens"] == 60
    history = append_batch_history(job, context, launch_id="test", runner_batch=1,
                                   started_at=r.now_iso(), finished_at=r.now_iso(), elapsed_seconds=2,
                                   exit_code=3, usage={"input_tokens": 60}, agent_messages=[])
    for row in history:
        assert row["tokens_exact"]
        assert row["attributed_tokens"] == row["worker"] * 11
        assert normalize_history_measurements(row)["duration_seconds"] > 0
        assert row["messages_scope"] == "marker"


def test_usage_can_be_scoped_to_current_launch(tmp_path):
    job = make_job(tmp_path, count=1)
    events = [
        {"type": "turn.completed", "launch_id": "old", "usage": {"input_tokens": 900}},
        {"type": "turn.completed", "launch_id": "new", "usage": {"input_tokens": 20, "output_tokens": 3}},
    ]
    (job / r.EVENT_LOG).write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
    assert r.read_codex_usage(job)["input_tokens"] == 920
    assert r.read_codex_usage(job, "new") == {"input_tokens": 20, "cached_input_tokens": 0,
                                                "output_tokens": 3, "reasoning_output_tokens": 0}


def test_activity_feed_keeps_all_agent_messages_and_hides_command_steps(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    events = []
    for index in range(8):
        events.extend([
            {"type": "item.started", "item": {"id": f"cmd-{index}", "type": "command_execution"}},
            {"type": "item.completed", "item": {"id": f"cmd-{index}", "type": "command_execution"}},
            {"type": "item.completed", "item": {"id": f"msg-{index}", "type": "agent_message",
                                                   "text": f"Сообщение {index + 1}"}},
        ])
    (job / "codex-events.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in events) + "\n",
        encoding="utf-8",
    )

    entries = codex_activity_entries(job, include_steps=True)

    assert len(entries) == 8
    assert entries[0].endswith("Сообщение 1")
    assert entries[-1].endswith("Сообщение 8")
    assert all("Локальная проверка" not in entry for entry in entries)


def test_one_worker_failure_does_not_cancel_or_reassign_peers(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "fail")
    execute(job, context)
    workers = {row["worker"]: row for row in read_worker_runtime(job)["workers"].values()}
    assert workers[2]["error"] and not workers[2]["usage_complete"]
    assert workers[1]["usage_complete"] and workers[3]["usage_complete"]
    assert len(list((job / "worker-runs").rglob("started.json"))) == 3


def test_nested_result_does_not_reuse_stale_canonical_note(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "wrong_path")
    old = job / "notes/batch-001-worker-1.json"
    mid = next(a["marker_ids"][0] for a in context["batch"]["assignments"] if a["worker"] == 1)
    r.atomic_json(old, [{"marker_id": mid, "analysis_status": "needs_context", "proof_gaps": ["old evidence gap"]}])
    execute(job, context)
    workers = read_worker_runtime(job)["workers"]
    assert all("не сохранил новый результат" in row["error"] for row in workers.values())
    assert "old evidence gap" in r.read_json(old)[0]["proof_gaps"]
    assert all(row["exit_code"] != 0 for row in workers.values())


def test_missing_file_has_one_bounded_serialization_repair(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "repair_path")
    execute(job, context)
    for mid, row in read_worker_runtime(job)["workers"].items():
        directory = (job / row["event_log"]).parent
        assert len(list(directory.glob("prompt-*.txt"))) == 2
        saved = r.read_json(directory / "context.json")
        assert Path(saved["result_file"]).is_absolute()
        assert Path(saved["source_request_file"]).is_absolute()
        assert str(Path(saved["result_file"])) in (directory / "prompt.txt").read_text(encoding="utf-8")
        assert row["exit_code"] == 0
        assert r.read_json(job / f"notes/batch-001-worker-{row['worker']}.json")[0]["marker_id"] == mid


def test_valid_same_assignment_result_recovery_respects_chronology(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch)
    assignment = context["batch"]["assignments"][0]
    mid = assignment["marker_ids"][0]
    repository = job / "repository"
    repository.mkdir()
    (repository / "same.go").write_text("guard prevents invalid state\n", encoding="utf-8")
    context.update(repository=str(repository), review_contract_version=1, revision="exact-revision", launch_id="test")
    row = result_for(job, mid)
    row.update(review_contract_version=1, source_revision="exact-revision",
               comment="Проверенная защита исключает состояние (same.go:1).",
               source_evidence=[{"file_path": "same.go", "line_start": 1, "line_end": 1,
                                 "excerpt": "guard prevents invalid state",
                                 "supports": "Защита исключает опасное состояние.",
                                 "roles": ["source", "sink", "control", "product_reachability"]}])
    row["source_evidence"].append({"file_path": "same.go", "line_start": 99, "line_end": 99,
                                   "excerpt": "invented", "supports": "redundant bad reference",
                                   "roles": ["control"]})
    candidate = job / "worker-runs/test/batch-001/worker-1/result-old.json"
    candidate.parent.mkdir(parents=True)
    r.atomic_json(candidate, [row])
    recovered = r.recover_completed_worker_result(job, context, assignment)
    assert recovered and recovered[0]["marker_id"] == mid
    assert recovered[0]["verdict"] == "False Positive"
    assert len(recovered[0]["source_evidence"]) == 1
    assert r.recover_completed_worker_result(job, {**context, "launch_id": "recheck"}, assignment) is None
    assert r.recover_completed_worker_result(job, {**context, "batch_number": 2}, assignment) is None
    newest = candidate.with_name("result-newer.json")
    r.atomic_json(newest, [{"marker_id": mid, "analysis_status": "needs_context", "proof_gaps": ["new doubt"]}])
    import os
    os.utime(newest, ns=(candidate.stat().st_atime_ns, candidate.stat().st_mtime_ns + 1000000000))
    assert r.recover_completed_worker_result(job, context, assignment) is None


def test_runtime_prompt_is_russian_and_schema_is_unambiguous(tmp_path):
    job = make_job(tmp_path, count=1)
    context = claim(job)
    context.update(repository=str(tmp_path), revision="exact-revision", result_file=str(tmp_path / "result.json"))
    prompt = r.build_runtime_prompt(job, tmp_path, context)
    assert "Пиши аналитические поля и итоговый comment кратко на русском" in prompt
    assert "Обязательно создай отдельный непустой массив source_evidence" in prompt
    assert "ТОЛЬКО по этому абсолютному пути" in prompt
    assert "concise English" not in prompt
    assert "source_inspect.py" in prompt
    assert "git -C" in prompt
    assert "Nullable-return сам по себе не доказывает дефект" in prompt
    assert "конкретная product wiring не нужна" in prompt


def test_source_helper_uses_console_python_even_from_windowless_runner(tmp_path, monkeypatch):
    job = make_job(tmp_path, count=1)
    context = claim(job)
    context["previous_result_file"] = str(job / "worker-runs/prior-turn.json")
    monkeypatch.setattr(r, "console_python_executable", lambda: "C:/python dir/python.exe")
    prompt = r.build_runtime_prompt(job, tmp_path, context)
    assert "C:/python dir/python.exe" in prompt
    assert "utf8" in prompt
    assert str(job / "worker-runs/prior-turn.json") in prompt


def test_new_worker_dependency_context_reaches_final_validation(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "sources")
    def sources(job, app, ctx):
        request = job / ctx["source_request_file"]
        if not request.exists():
            return False
        request.unlink()
        ctx["source_request_round"] = 1
        ctx["dependency_sources"] = [{"name": "new-dependency", "root": "dependency-sources/new-dependency/cache"}]
        return True
    monkeypatch.setattr(r, "resolve_source_requests", sources)
    execute(job, context)
    assert context["dependency_sources"] == [{"name": "new-dependency", "root": "dependency-sources/new-dependency/cache"}]
    worker = next(row for row in read_worker_runtime(job)["workers"].values() if row["worker"] == 2)
    saved = r.read_json((job / worker["event_log"]).parent / "context.json")
    assert Path(saved["previous_result_file"]).is_file()


def test_source_round_resumes_without_waiting_for_other_worker(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "sources")
    resumed = []
    def sources(job, app, ctx):
        request = job / ctx["source_request_file"]
        if not request.exists():
            return False
        request.unlink()
        ctx["source_request_round"] = 1
        resumed.append(time.time())
        return True
    monkeypatch.setattr(r, "resolve_source_requests", sources)
    execute(job, context)
    workers = read_worker_runtime(job)["workers"]
    slow = (job / next(row for row in workers.values() if row["worker"] == 1)["event_log"]).parent
    assert resumed[0] < r.read_json(slow / "finished.json")["finish"]
    assert r.read_codex_usage(job)["input_tokens"] == 80


def test_stop_signal_joins_all_owned_processes(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "stop")
    task = threading.Thread(target=execute, args=(job, context))
    task.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        workers = read_worker_runtime(job).get("workers", {})
        if len(workers) == 3 and all(row.get("pid") for row in workers.values()):
            break
        time.sleep(.05)
    pids = [row["pid"] for row in workers.values()]
    r.atomic_json(job / "stop-request.json", {"launch_id": "test"})
    task.join(timeout=15)
    assert not task.is_alive()
    assert not any(r.process_is_alive(pid) for pid in pids)
    assert all(row["state"] == "incomplete" for row in read_worker_runtime(job)["workers"].values())


def test_mixed_failure_still_applies_proven_peers_and_preserves_failed_marker(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "mixed")
    for assignment in context["batch"]["assignments"]:
        r.atomic_json(job / f"fixture-result-{assignment['worker']}.json",
                      [result_for(job, assignment["marker_ids"][0])])
    execute(job, context)
    with pytest.raises(r.IncompleteAnalysisError):
        r.finalize_turn(job, context)
    saved = r.load_decisions(job / "decisions.jsonl")
    assert sum(row.get("verdict") == "False Positive" for row in saved) == 2
    assert sum(not row.get("verdict") for row in saved) == 1


def setup_quality_job(tmp_path, monkeypatch, mode):
    job, context = setup_job(tmp_path, monkeypatch, mode)
    repository = job / "repository"
    repository.mkdir()
    source = "guard prevents invalid state\n"
    (repository / "same.go").write_text(source, encoding="utf-8")
    context.update(repository=str(repository), review_contract_version=1, revision="exact-revision", launch_id="test")
    for assignment in context["batch"]["assignments"]:
        row = result_for(job, assignment["marker_ids"][0])
        row.update(review_contract_version=1, source_revision="exact-revision", decision_policy_version=2,
                   component_defect_proven=False, product_defect_reachable=False,
                   comment="Проверенная защита исключает состояние (same.go:1).",
                   source_evidence=[{"file_path": "same.go", "line_start": 1, "line_end": 1,
                                     "excerpt": source.strip(), "supports": "Защита исключает опасное состояние.",
                                     "roles": ["source", "sink", "control", "product_reachability"]}])
        r.atomic_json(job / f"fixture-result-{assignment['worker']}.json", [row])
    return job, context


@pytest.mark.parametrize("mode", ["quality_repair", "quality_repair_fails"])
def test_worker_repairs_its_own_policy_schema_without_restarting_healthy_peers(tmp_path, monkeypatch, mode):
    job, context = setup_quality_job(tmp_path, monkeypatch, mode)
    execute(job, context)
    workers = read_worker_runtime(job)["workers"]
    for mid, worker in workers.items():
        directory = (job / worker["event_log"]).parent
        assert len(list(directory.glob("prompt-*.txt"))) == (2 if worker["worker"] == 2 else 1)
        expected = "incomplete" if mode == "quality_repair_fails" and worker["worker"] == 2 else "finished"
        assert worker["state"] == expected
        saved_context = r.read_json(directory / "context.json")
        if worker["worker"] == 2:
            assert saved_context["worker_quality_repair_count"] == 1
            assert all(mid in error for error in saved_context["quality_feedback"])
        else:
            assert not saved_context.get("quality_feedback")
    assert not context.get("quality_repair_count")  # parent budget is distinct


def test_parent_quality_repair_reuses_healthy_results_and_measurements(tmp_path, monkeypatch):
    job, context = setup_quality_job(tmp_path, monkeypatch, "quality_repair")
    execute(job, context)
    before = read_worker_runtime(job)["workers"]
    affected = next(mid for mid, value in before.items() if value["worker"] == 2)
    context.update(quality_repair_count=1, quality_feedback=[f"{affected}: parent validation failed"],
                   quality_repair_marker_ids=[affected])
    execute(job, context)
    after = read_worker_runtime(job)["workers"]
    for mid, worker in after.items():
        directory = job / "worker-runs/test/batch-001" / f"worker-{worker['worker']}"
        assert len(list(directory.glob("prompt-*.txt"))) == (3 if mid == affected else 1)
        assert worker["state"] == "finished"
        if mid != affected:
            assert worker["duration_seconds"] == before[mid]["duration_seconds"]
            assert worker["event_log"] == before[mid]["event_log"]


def test_numbered_quotes_finish_without_a_model_repair_round(tmp_path, monkeypatch):
    job, context = setup_quality_job(tmp_path, monkeypatch, "evidence_numbered")
    execute(job, context)
    for mid, worker in read_worker_runtime(job)["workers"].items():
        directory = (job / worker["event_log"]).parent
        assert len(list(directory.glob("prompt-*.txt"))) == 1
        assert worker["state"] == "finished"
        rows = r.read_json(job / f"notes/batch-001-worker-{worker['worker']}.json")
        assert not r.review_result(job, context, rows[0])
        assert rows[0]["source_evidence"][0]["excerpt"] == "guard prevents invalid state"


def test_recovery_checks_queue_schema_not_just_assignment_display_fields(tmp_path, monkeypatch):
    job, context = setup_quality_job(tmp_path, monkeypatch, "quality_repair")
    assignment = context["batch"]["assignments"][0]
    row = r.read_json(job / f"fixture-result-{assignment['worker']}.json")[0]
    row["component_defect_proven"] = True  # valid excerpts cannot rescue a contradictory FP
    candidate = job / "worker-runs/test/batch-001" / f"worker-{assignment['worker']}" / "result-test.json"
    candidate.parent.mkdir(parents=True)
    r.atomic_json(candidate, [row])
    assert r.recover_completed_worker_result(job, context, assignment) is None


def test_timeout_leaves_unfinished_evidence_not_fake_success(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "stop")
    monkeypatch.setattr(r, "CODEX_TURN_TIMEOUT_SECONDS", .25)
    execute(job, context)
    workers = read_worker_runtime(job)["workers"]
    assert len(workers) == 3
    assert all("Истекло время" in row["error"] for row in workers.values())
    assert all(not row["usage_complete"] for row in workers.values())


def test_status_write_failure_after_spawn_does_not_orphan_child(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch, "normal")
    atomic = r.atomic_json
    failed_pid = []
    def fail_once(path, value):
        if path.name == "workers-runtime.json" and not failed_pid:
            running = next((w for w in value["workers"].values() if w.get("pid")), None)
            if running:
                failed_pid.append(running["pid"])
                raise PermissionError("test permanent status failure")
        atomic(path, value)
    monkeypatch.setattr(r, "atomic_json", fail_once)
    execute(job, context)
    assert failed_pid and not r.process_is_alive(failed_pid[0])
    assert sum(row["exit_code"] != 0 for row in read_worker_runtime(job)["workers"].values()) == 1


def test_reject_duplicate_assignments_before_spending_tokens(tmp_path, monkeypatch):
    job, context = setup_job(tmp_path, monkeypatch)
    context["batch"]["assignments"][1] = context["batch"]["assignments"][0]
    with pytest.raises(ValueError, match="уникальный"):
        execute(job, context)
    assert not (job / "worker-runs").exists()


def test_only_actual_running_worker_is_reported_and_clock_is_independent(tmp_path):
    run = {"active": True, "started_at": "2026-09-22T10:00:00+00:00"}
    state = {"codex_run": run, "workers": {1: {"marker_ids": ["m1"], "current_status": "assigned"},
                                           2: {"marker_ids": ["m2"], "current_status": "assigned"}},
             "worker_runtime": {"workers": {"m1": {"state": "running", "pid": 1},
                                            "m2": {"state": "finished", "pid": None}}}}
    assert marker_assignments(state) == {"m1": "Агент 1"}
    now = 1790071800.0
    first = live_run_timing(tmp_path, run, now_timestamp=now,
                            worker={"started_at": "2026-09-22T10:00:00+00:00", "last_event_at": "2026-09-22T10:01:00+00:00"})
    second = live_run_timing(tmp_path, run, now_timestamp=now,
                             worker={"started_at": "2026-09-22T10:00:00+00:00", "last_event_at": "2026-09-22T10:05:00+00:00"})
    assert first != second
