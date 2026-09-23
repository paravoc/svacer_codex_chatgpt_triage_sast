"""Explicit manual restart after a campaign stop; no network/model processes."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis_campaign as c
import codex_run as r
import triage_queue as q
import usage_guard as g
from test_queue_execution import make_job


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    results = tmp_path / "RESULTS"
    results.mkdir()
    job = make_job(results, count=2, manual=True)
    other = results / "other"
    other.mkdir()
    r.atomic_json(other / "job.json", {})
    app = tmp_path / "app"
    app.mkdir()
    (job / "START_PROMPT.txt").write_text("offline task", encoding="utf-8")
    r.atomic_json(job / "control.json", {"priority_marker_ids": ["m01", "m00"],
                                       "pause_requested": False, "manual_queue_requested": True})
    r.atomic_json(job / r.RUN_FILE, {"status": "stopped", "launch_id": "old", "runner_pid": 999999})
    r.atomic_json(job / "stop-request.json", {"launch_id": "old"})
    r.atomic_json(job / "notes" / "draft.json", {"proof": "retained"})
    (job / "marker-history.jsonl").write_text("saved history\n", encoding="utf-8")
    path = results / "analysis-campaign.json"
    r.atomic_json(path, {"root": str(tmp_path), "minimum_remaining_percent": 55,
                        "reserve_percent": 5, "jobs": [
                            {"job": job.name, "marker_ids": ["m00", "m01"]},
                            {"job": other.name, "marker_ids": []}]})
    campaign = c.Campaign(path)
    campaign.state.update(status="user_stopped", reason="User stopped the previous campaign")
    campaign.guard(False, campaign.state["reason"], remaining=71)
    campaign.save()
    monkeypatch.setattr(r, "process_is_alive", lambda _pid: False)
    spawned = []
    def spawn(*args, **kwargs):
        spawned.append(args)
        return SimpleNamespace(pid=123456)
    monkeypatch.setattr(r.subprocess, "Popen", spawn)
    monkeypatch.setattr(r, "read_codex_rate_limits", lambda: pytest.fail("Launcher must not call an account/model"))
    return campaign, job, other, app, spawned


@pytest.mark.parametrize("existing,expected", [(None, 60), (0, 0), (20, 20), (80, 80)])
def test_manual_start_only_transfers_guard_and_preserves_every_result(stopped, existing, expected):
    campaign, job, other, app, spawned = stopped
    if existing is not None:
        r.atomic_json(job / "job.json", {**r.read_json(job / "job.json"), g.SETTING: existing})
    protected = [job / name for name in ("decisions.jsonl", "control.json", "marker-history.jsonl",
                                         "notes/draft.json", "stop-request.json")]
    protected += [campaign.path, campaign.status_path, other / c.GUARD_FILE]
    before = {p: p.read_bytes() for p in protected}
    with pytest.raises(RuntimeError, match="User stopped"):
        r.launch_runner(job, app)
    assert not spawned
    launched = r.launch_runner(job, app, manual_start=True)
    assert len(spawned) == 1
    assert launched["manual_resume_threshold"] == expected
    assert g.display_text(expected) in launched["resume_notice"]
    assert r.read_json(job / "job.json")[g.SETTING] == expected
    guard = r.read_json(job / c.GUARD_FILE)
    assert guard["enabled"] is False and guard["allow"] is False
    assert guard["manual_resume"]["previous_setting"] == (existing or 0)
    assert not c.lease_reason(job)
    assert not r._stop_requested(job, launched["launch_id"])
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not campaign.tick()  # Does not resurrect automatic continuation.
    # Handover happens once; an explicit later change is not overwritten again.
    r.atomic_json(job / "job.json", {**r.read_json(job / "job.json"), g.SETTING: 75})
    assert c.release_user_stop_for_manual_start(job) is None
    assert r.read_json(job / "job.json")[g.SETTING] == 75


@pytest.mark.parametrize("status", ["running", "budget_paused", "quota_unknown", "supervisor_error", "selection_changed", "unknown"])
def test_manual_start_cannot_clear_non_user_stop(stopped, status):
    campaign, job, _, app, spawned = stopped
    campaign.state["status"] = status
    campaign.save()
    before = {p: p.read_bytes() for p in (job / c.GUARD_FILE, job / "job.json", job / "control.json")}
    with pytest.raises(RuntimeError):
        r.launch_runner(job, app, manual_start=True)
    assert not spawned and all(p.read_bytes() == data for p, data in before.items())


def test_live_supervisor_and_exiting_runner_are_not_detached(stopped, monkeypatch):
    _, job, _, app, spawned = stopped
    before = (job / c.GUARD_FILE).read_bytes()
    monkeypatch.setattr(r, "process_is_alive", lambda pid: pid == os.getpid())
    with pytest.raises(RuntimeError, match="Контроллер"):
        r.launch_runner(job, app, manual_start=True)
    monkeypatch.setattr(r, "process_is_alive", lambda pid: pid == 999999)
    with pytest.raises(RuntimeError, match="ещё завершает"):
        r.launch_runner(job, app, manual_start=True)
    assert not spawned and (job / c.GUARD_FILE).read_bytes() == before


def test_failed_handover_keeps_old_guard_enabled(stopped, monkeypatch):
    _, job, _, app, spawned = stopped
    original = r.atomic_json
    def fail_guard(path, data):
        if path == job / c.GUARD_FILE:
            raise OSError("simulated write failure")
        original(path, data)
    monkeypatch.setattr(r, "atomic_json", fail_guard)
    with pytest.raises(OSError, match="simulated"):
        r.launch_runner(job, app, manual_start=True)
    assert not spawned and r.read_json(job / c.GUARD_FILE)["enabled"] is True
    assert r.read_json(job / "job.json")[g.SETTING] == 60
    assert c.lease_reason(job)


def test_active_run_is_not_duplicated_or_detached(stopped, monkeypatch):
    _, job, _, app, spawned = stopped
    r.atomic_json(job / r.RUN_FILE, {"status": "running", "launch_id": "active", "runner_pid": 12345})
    monkeypatch.setattr(r, "process_is_alive", lambda pid: pid == 12345)
    before = (job / c.GUARD_FILE).read_bytes()
    assert r.launch_runner(job, app, manual_start=True)["launch_id"] == "active"
    assert not spawned and (job / c.GUARD_FILE).read_bytes() == before


@pytest.mark.parametrize("invalid", [None, True, "60", float("nan"), 100])
def test_invalid_old_quota_never_disables_guard(stopped, invalid):
    _, job, _, app, spawned = stopped
    r.atomic_json(job / c.GUARD_FILE, {**r.read_json(job / c.GUARD_FILE), "stop_at_remaining_percent": invalid})
    before = (job / c.GUARD_FILE).read_bytes()
    with pytest.raises(RuntimeError, match="защитный порог"):
        r.launch_runner(job, app, manual_start=True)
    assert not spawned and (job / c.GUARD_FILE).read_bytes() == before


def test_missing_campaign_state_stays_blocked(stopped):
    campaign, job, _, app, spawned = stopped
    campaign.status_path.unlink()
    before = (job / c.GUARD_FILE).read_bytes()
    with pytest.raises(OSError):
        r.launch_runner(job, app, manual_start=True)
    assert not spawned and (job / c.GUARD_FILE).read_bytes() == before


def test_empty_queue_does_not_release_guard(stopped):
    _, job, _, app, spawned = stopped
    r.atomic_json(job / "control.json", {"priority_marker_ids": []})
    with pytest.raises(RuntimeError, match="Очередь пуста"):
        r.launch_runner(job, app, manual_start=True)
    assert not spawned and c.lease_reason(job)


def test_new_runner_still_checks_fresh_quota_before_model_work(stopped, monkeypatch):
    _, job, _, app, spawned = stopped
    launch = r.launch_runner(job, app, manual_start=True)
    monkeypatch.setattr(r, "read_codex_rate_limits", lambda: {"rateLimits": {"primary": {"usedPercent": 41}}})
    monkeypatch.setattr(r, "prepare_repository", lambda *_args: pytest.fail("Quota must stop before preparation"))
    assert r.run_job(job, launch["launch_id"]) == 0
    record = r.read_json(job / r.RUN_FILE)
    assert record["stop_kind"] == "codex_percentage" and record["status"] == "paused"
    assert "59%" in record["reason"] and "60%" in record["reason"]
    assert len(spawned) == 1  # Only the stubbed local runner, no model.
    assert q.priority_marker_ids(job / "decisions.jsonl") == ["m01", "m00"]
