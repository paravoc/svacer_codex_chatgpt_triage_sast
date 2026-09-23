"""Quota/campaign regressions: mocked account API, no real markers or model calls."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis_campaign as c
import codex_run as r
import triage_queue as q
from test_queue_execution import make_job


def test_portable_package_contains_runner_quota_dependency():
    manifest = (Path(__file__).resolve().parents[1] / "make_portable_package.ps1").read_text(encoding="utf-8-sig")
    assert "'analysis_campaign.py'" in manifest
    assert "'AUTOMATIC_ANALYSIS.md'" in manifest


def quota(used=10, secondary=None):
    return {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": used}, "secondary": secondary}}}


@pytest.mark.parametrize("payload,expected", [
    (quota(15), 85), (quota(10, {"usedPercent": 35}), 65),
    ({"rateLimits": {"primary": {"usedPercent": 25}}}, 75),
    ({**quota(0), "ordinaryUsageAllowed": False}, 0),
])
def test_quota_uses_remaining_tightest_window(payload, expected):
    assert c.remaining_percent(payload) == expected


@pytest.mark.parametrize("payload", [{}, {"rateLimitsByLimitId": {}}, quota(None), quota(True),
    quota("15"), quota(-1), quota(101), quota(float("nan")), quota(float("inf")),
    {"rateLimitsByLimitId": {}, "rateLimits": {"primary": {"usedPercent": 0}}}])
def test_unknown_quota_fails_closed(payload):
    with pytest.raises(ValueError):
        c.remaining_percent(payload)


def setup(tmp_path, monkeypatch, used=10):
    results = tmp_path / "RESULTS"
    results.mkdir()
    job = make_job(results, count=2, manual=True)
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ["m00", "m01"], "manual_queue_requested": True})
    path = results / "analysis-campaign.json"
    q.atomic_write_json(path, {"root": str(tmp_path), "minimum_remaining_percent": 55, "reserve_percent": 5,
        "jobs": [{"job": job.name, "marker_ids": ["m00", "m01"]}]})
    monkeypatch.setattr(r, "read_codex_rate_limits", lambda: quota(used))
    monkeypatch.setattr(r, "process_is_alive", lambda pid: pid == os.getpid())
    runs = {job: {"active": True, "status": "running", "launch_id": "existing", "started_at": r.now_iso()}}
    monkeypatch.setattr(r, "read_run_record", lambda job: runs[job])
    stops, launches = [], []
    monkeypatch.setattr(r, "stop_run", lambda job: stops.append(job))
    def launch(job, app):
        launches.append(job)
        runs[job] = {"active": True, "status": "running", "launch_id": f"new-{len(launches)}", "started_at": r.now_iso()}
        return runs[job]
    monkeypatch.setattr(r, "launch_runner", launch)
    return c.Campaign(path), job, runs, stops, launches


@pytest.mark.parametrize("used,status", [(39, "running"), (40, "budget_paused"), (45, "budget_paused")])
def test_stops_before_55_and_never_restarts_when_budget_recovers(tmp_path, monkeypatch, used, status):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch, used)
    before = (job / "decisions.jsonl").read_bytes()
    assert campaign.tick() == (status == "running")
    assert campaign.state["status"] == status
    assert not launches and (job / "decisions.jsonl").read_bytes() == before
    if status != "running":
        assert stops == [job]
        monkeypatch.setattr(r, "read_codex_rate_limits", lambda: quota(0))
        assert not campaign.tick() and not launches
        assert c.lease_reason(job)


def test_unknown_quota_stops_not_runs_without_a_guard(tmp_path, monkeypatch):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(r, "read_codex_rate_limits", lambda: {})
    assert not campaign.tick()
    assert campaign.state["status"] == "quota_unknown" and stops == [job] and not launches


def test_lease_expiry_and_guard_death_fail_closed(tmp_path, monkeypatch):
    campaign, job, *_ = setup(tmp_path, monkeypatch)
    campaign.guard(True, remaining=85)
    assert not c.lease_reason(job)
    assert c.lease_reason(job, time.time() + 61)
    monkeypatch.setattr(r, "process_is_alive", lambda _: False)
    assert c.lease_reason(job)


def test_runner_refuses_launch_and_requests_stop_when_budget_guard_denies(tmp_path, monkeypatch):
    campaign, job, *_ = setup(tmp_path, monkeypatch)
    campaign.guard(False, "Quota reserve reached", remaining=60)
    monkeypatch.setattr(r, "_job_paths", lambda *_: None)
    monkeypatch.setattr(r.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("Must not start a model"))
    with pytest.raises(RuntimeError, match="Quota reserve"):
        r._launch_runner_locked(job, job.parent)
    assert r._stop_requested(job, "existing")
    (job / c.GUARD_FILE).write_text("bad JSON")
    assert c.lease_reason(job)


def test_existing_run_is_adopted_and_explicit_stop_is_not_undone(tmp_path, monkeypatch):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    assert campaign.tick() and not launches
    runs[job] = {"active": False, "status": "stopped", "launch_id": "existing"}
    assert not campaign.tick() and not launches
    assert campaign.state["status"] == "user_stopped"


def test_no_unknown_markers_are_added_or_run(tmp_path, monkeypatch):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ["m00", "unapproved"]})
    assert not campaign.tick() and stops == [job] and not launches
    assert campaign.state["status"] == "selection_changed"


def test_incomplete_evidence_is_not_blindly_retried_or_erased(tmp_path, monkeypatch):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    campaign.tick()
    runs[job] = {"active": False, "status": "incomplete", "launch_id": "existing"}
    q.atomic_write_json(job / "incomplete-analysis.json", {"m00": {"reason": "Need exact build evidence"}})
    assert campaign.tick() and not launches
    assert campaign.state["jobs"][job.name]["status"] == "needs_attention"
    assert q.priority_marker_ids(job / "decisions.jsonl") == ["m00", "m01"]
    assert not campaign.tick() and campaign.state["status"] == "needs_attention"


def test_transport_retry_is_bounded_to_one_extra_launch(tmp_path, monkeypatch):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    campaign.tick()
    q.atomic_write_json(job / "incomplete-analysis.json", {
        "m00": {"reason": "Выбранная модель временно перегружена"},
        "m01": {"reason": "Need exact build evidence"}})
    for i in range(2):
        runs[job] = {"active": False, "status": "incomplete", "launch_id": "existing" if not i else "new-1"}
        assert campaign.tick()
    assert launches == [job]
    assert r.read_json(job / "control.json")["deferred_marker_ids"] == ["m01"]
    assert campaign.state["jobs"][job.name]["status"] == "needs_attention"


@pytest.mark.parametrize("runtime_launch,status,expected", [
    ("existing", "pending", True), ("old", "pending", False), ("existing", "verified", False),
])
def test_pending_verifier_shape_failure_retries_only_current_unfinished_check(tmp_path, monkeypatch, runtime_launch, status, expected):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    campaign.tick()
    rows = q.load_decisions(job / "decisions.jsonl")
    rows[0].update(verdict="Confirmed", verification={"status": status})
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    q.atomic_write_json(job / "workers-runtime.json", {"launch_id": runtime_launch, "workers": {
        "m00": {"state": "incomplete", "error": "m00: evidence must be a non-empty string array"},
        "m01": {"state": "incomplete", "error": "Need exact build evidence"}}})
    runs[job] = {"active": False, "status": "incomplete", "launch_id": "existing"}
    assert campaign.tick()
    assert launches == ([job] if expected else [])
    if expected:
        assert r.read_json(job / "control.json")["deferred_marker_ids"] == ["m01"]


def test_stall_does_not_cancel_healthy_peer(tmp_path, monkeypatch):
    campaign, job, *_ = setup(tmp_path, monkeypatch)
    run = {"launch_id": "live", "started_at": "2020-01-01T00:00:00Z"}
    q.atomic_write_json(job / "workers-runtime.json", {"launch_id": "live", "workers": {
        "old": {"state": "running", "last_event_at": "2020-01-01T00:00:00Z"},
        "healthy": {"state": "running", "last_event_at": r.now_iso()}}})
    assert not c.stalled_run(job, run, time.time())
    assert c.stalled_run(job, run, time.time() + c.STALL_SECONDS + 2)


def test_new_job_starts_only_after_previous_queue_completes(tmp_path, monkeypatch):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    next_job = job.parent / "second"
    next_job.mkdir()
    for name in ("job.json", "decisions.jsonl", "markers.inventory.json", "control.json"):
        (next_job / name).write_bytes((job / name).read_bytes())
    campaign.jobs.append(next_job)
    campaign.config["jobs"].append({"job": next_job.name, "marker_ids": ["m00", "m01"]})
    runs[next_job] = {"active": False, "status": "not_started"}
    campaign.tick()
    assert not launches
    runs[job] = {"active": False, "status": "completed", "launch_id": "existing"}
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": []})
    campaign.tick()
    campaign.tick()
    assert launches == [next_job]
    assert campaign.state["jobs"][job.name]["status"] == "needs_attention"  # Empty queue alone is not proof of completion.


def test_finish_button_is_not_undone_by_stale_transport_error(tmp_path, monkeypatch):
    campaign, job, runs, stops, launches = setup(tmp_path, monkeypatch)
    campaign.tick()
    control = r.read_json(job / "control.json")
    control["pause_requested"] = True
    q.atomic_write_json(job / "control.json", control)
    q.atomic_write_json(job / "incomplete-analysis.json", {"m00": {"reason": "Выбранная модель временно перегружена"}})
    campaign.tick()
    assert not stops  # Graceful finish allows the current workers to save.
    runs[job] = {"active": False, "status": "paused", "launch_id": "existing"}
    assert not campaign.tick() and not launches
    assert campaign.state["status"] == "user_stopped"
