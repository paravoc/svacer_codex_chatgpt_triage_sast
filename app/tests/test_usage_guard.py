"""Offline account-percentage guard regressions. No real account/model calls."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usage_guard as g
import codex_run as r
import triage_queue as q


def quota(used=10, secondary=None):
    return {"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": used}, "secondary": secondary}}}


def configure(job, threshold=20):
    path = job / "job.json"
    data = r.read_json(path) if path.exists() else {}
    r.atomic_json(path, {**data, g.SETTING: threshold})


@pytest.mark.parametrize("value", [-1, 100, True, False, "20", 20.5, None])
def test_threshold_validation(value):
    with pytest.raises(ValueError):
        g.validate_threshold(value)


@pytest.mark.parametrize("payload,remaining", [
    (quota(79), 21), (quota(80), 20), (quota(81), 19),
    (quota(30, {"usedPercent": 85}), 15),
    ({"rateLimits": {"primary": {"usedPercent": 75}}}, 25),
    ({**quota(0), "ordinaryUsageAllowed": False}, 0),
])
def test_remaining_not_used_percentage(payload, remaining):
    assert g.remaining_percent(payload) == remaining


@pytest.mark.parametrize("payload", [{}, quota(None), quota(True), quota(float("nan")),
                                     quota("10"), quota(110), quota(-1)])
def test_unknown_quota_is_not_unlimited(payload):
    with pytest.raises(ValueError):
        g.remaining_percent(payload)


@pytest.mark.parametrize("used,stopped", [(79, False), (80, True), (81, True)])
def test_twenty_means_remaining_at_or_below_twenty(tmp_path, used, stopped):
    configure(tmp_path)
    guard = g.UsageGuard(tmp_path, "run", lambda: quota(used))
    guard.check()
    assert bool(guard.reason) == stopped
    assert g.snapshot(tmp_path)["remaining_percent"] == 100 - used
    if stopped:
        guard.read_limits = lambda: pytest.fail("Quota recovery must not auto-resume a stopped run")
        guard.check()
        assert "20%" in guard.reason


def test_threshold_persists_and_manual_restart_rechecks_actual_account(tmp_path):
    configure(tmp_path, 20)
    first = g.UsageGuard(tmp_path, "first", lambda: quota(85))
    first.check()
    assert first.reason
    second = g.UsageGuard(tmp_path, "second", lambda: quota(85))
    second.check()
    assert second.reason
    third = g.UsageGuard(tmp_path, "third", lambda: quota(60))
    third.check()
    assert not third.reason
    assert r.read_json(tmp_path / "job.json")[g.SETTING] == 20


def test_changes_apply_on_next_poll_without_spawning_a_model(tmp_path):
    configure(tmp_path, 0)
    guard = g.UsageGuard(tmp_path, "run", lambda: pytest.fail("Disabled guard must not query account"))
    guard.check()
    assert not guard.reason
    configure(tmp_path, 20)
    guard.read_limits = lambda: quota(70)
    guard.check()
    assert not guard.reason
    configure(tmp_path, 35)
    guard.check()
    assert guard.reason and guard.remaining == 30


def test_api_failure_stops_and_does_not_expose_exception_details(tmp_path):
    configure(tmp_path)
    def fail():
        raise RuntimeError("do not echo transport headers")
    guard = g.UsageGuard(tmp_path, "run", fail)
    guard.check()
    assert "Не удалось проверить" in guard.reason
    assert "headers" not in json.dumps(g.snapshot(tmp_path))


def test_preflight_low_quota_does_not_prepare_or_spawn_and_preserves_queue(tmp_path, monkeypatch):
    from test_queue_execution import make_job
    job = make_job(tmp_path, count=2, manual=True)
    configure(job)
    r.atomic_json(job / "control.json", {"priority_marker_ids": ["m00", "m01"]})
    (job / "marker-history.jsonl").write_text("saved history\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in [job / "control.json", job / "decisions.jsonl", job / "marker-history.jsonl"]}
    prompt = tmp_path / "START_PROMPT.txt"
    prompt.write_text("offline fixture", encoding="utf-8")
    monkeypatch.setattr(r, "_job_paths", lambda _: (tmp_path, prompt))
    monkeypatch.setattr(r, "read_codex_rate_limits", lambda: quota(85))
    monkeypatch.setattr(r, "prepare_repository", lambda *args: pytest.fail("Must stop before preparation"))
    assert r.run_job(job, "blocked") == 0
    record = r.read_json(job / r.RUN_FILE)
    assert record["status"] == "paused" and record["stop_kind"] == "codex_percentage"
    assert "15%" in record["reason"] and "20%" in record["reason"]
    assert not g._guards
    assert all((job / name).read_bytes() == value for name, value in before.items())


def test_running_parallel_processes_stop_without_losing_selection(tmp_path, monkeypatch):
    from test_continuous_analysis import setup, execute
    job, initial, _ = setup(tmp_path, monkeypatch, count=5, workers=2, budget=5, manual=True)
    configure(job)
    monkeypatch.setattr(g, "POLL_SECONDS", .02)
    calls = []
    def read():
        calls.append(1)
        return quota(81 if list((job / "worker-runs").rglob("started.json")) else 50)
    guard = g.UsageGuard(job, "test", read).start()
    try:
        execute(job, initial)
        assert guard.reason and len(calls) >= 2
        started = list((job / "worker-runs").rglob("started.json"))
        assert 1 <= len(started) <= 2
        assert q.priority_marker_ids(job / "decisions.jsonl") == [f"m{i:02}" for i in range(5)]
        assert r.read_json(job / "scheduler-state.json")["leases"]
        for path in started:
            assert not r.process_is_alive(json.loads(path.read_text())["pid"])
    finally:
        guard.close()


def test_verifier_wait_checks_guard_while_process_is_running(monkeypatch):
    class Process:
        args = ["offline-fixture"]
        def wait(self, timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
    process = Process()
    stopped = []
    monkeypatch.setattr(r, "_terminate_process_tree", lambda child: stopped.append(child))
    answers = iter([False, True])
    assert r._wait_for_codex(process, should_stop=lambda: next(answers)) == -1
    assert stopped == [process]


def test_portable_package_contains_guard():
    manifest = (Path(__file__).resolve().parents[1] / "make_portable_package.ps1").read_text(encoding="utf-8-sig")
    assert "'usage_guard.py'" in manifest
