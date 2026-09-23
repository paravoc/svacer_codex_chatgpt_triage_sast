"""Local, resumable supervision of explicitly selected jobs, with a quota reserve.

No Svacer publication, credential reads, model changes, or guessed verdicts.
The account API reports snapshots, not an atomic spending reservation.
"""
from __future__ import annotations

import argparse
import math
import os
import time
from datetime import datetime
from pathlib import Path

import codex_run as r
import triage_queue as q
from local_jobs import checked_job
from triage_dashboard import set_pause

POLL_SECONDS = 15
LEASE_SECONDS = 60
STALL_SECONDS = 12 * 60
PREPARATION_SECONDS = 15 * 60
GUARD_FILE = "campaign-guard.json"


def remaining_percent(payload: dict) -> float:
    """Fail closed on unknown/malformed data. Use the tightest Codex window."""
    buckets = payload.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else payload.get("rateLimits")
    if not isinstance(bucket, dict):
        raise ValueError("Codex allowance is unavailable")
    if payload.get("ordinaryUsageAllowed") is False or bucket.get("spendControlReached") is True:
        return 0.0
    values = []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window is None:
            continue
        used = window.get("usedPercent") if isinstance(window, dict) else None
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            raise ValueError("Codex allowance has an invalid percentage")
        values.append(100.0 - used)
    if not values:
        raise ValueError("Codex allowance has no measured window")
    return min(values)


def lease_reason(job: Path, now: float | None = None) -> str:
    """New runners stop if the supervisor disappears or its quota lease expires."""
    path = job / GUARD_FILE
    if not path.exists():
        return ""
    try:
        value = r.read_json(path)
        if value.get("enabled") is False:
            return ""
        stamp = value.get("checked_at_epoch")
        now = time.time() if now is None else now
        if (not value.get("allow") or type(stamp) not in (int, float)
                or not math.isfinite(stamp) or not 0 <= now - stamp <= LEASE_SECONDS):
            return str(value.get("reason") or "Контроль лимита остановлен или устарел; очередь сохранена.")
        if not r.process_is_alive(value.get("supervisor_pid")):
            return "Контроль лимита не работает; очередь сохранена."
    except (OSError, ValueError, TypeError, AttributeError):
        return "Не удалось проверить ограничение расхода; очередь сохранена."
    return ""


def age_seconds(stamp: str | None, now: float) -> float:
    try:
        return max(0, now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def release_user_stop_for_manual_start(job: Path) -> int | None:
    """Detach ONE explicitly started idle job; keep quota protection and campaign stop.

    Caller holds the job lifecycle/decision locks. Never use from automatic retries.
    The campaign lock also excludes a supervisor starting during this handover.
    """
    from usage_guard import SETTING, validate_threshold

    job = job.resolve()
    guard_path = job / GUARD_FILE
    if not guard_path.exists():
        return None
    guard = r.read_json(guard_path)
    if guard.get("enabled") is False:
        return None
    campaign_path = Path(str(guard.get("campaign") or "")).resolve()
    if campaign_path.parent != job.parent or campaign_path.suffix != ".json":
        raise RuntimeError("Не удалось проверить прежнюю кампанию; ограничение сохранено.")
    if r.process_is_alive(guard.get("supervisor_pid")):
        raise RuntimeError("Контроллер кампании ещё работает. Дождитесь его остановки.")
    with q.decision_lock(campaign_path):
        # Re-read under the supervisor's own lock, not a stale UI snapshot.
        guard = r.read_json(guard_path)
        config = r.read_json(campaign_path)
        state = r.read_json(campaign_path.with_name(campaign_path.stem + "-status.json"))
        if (Path(str(guard.get("campaign") or "")).resolve() != campaign_path
                or Path(str(config.get("root") or "")).resolve() != job.parent.parent
                or job.name not in {item.get("job") for item in config.get("jobs", [])}):
            raise RuntimeError("Задача не соответствует прежней кампании; ограничение сохранено.")
        if state.get("status") != "user_stopped" or guard.get("allow") is not False:
            raise RuntimeError(lease_reason(job) or "Кампания не остановлена пользователем.")
        if any(r.process_is_alive(pid) for pid in (guard.get("supervisor_pid"), state.get("supervisor_pid"))):
            raise RuntimeError("Контроллер кампании ещё работает. Дождитесь его остановки.")
        run = r.read_run_record(job)
        if run.get("active") or any(r.process_is_alive(run.get(key)) for key in ("runner_pid", "codex_pid")):
            raise RuntimeError("Прежний анализ ещё завершает работу. Повторите запуск после остановки.")
        floor, reserve = config.get("minimum_remaining_percent"), config.get("reserve_percent", 5)
        previous = guard.get("stop_at_remaining_percent")
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (floor, reserve, previous))
                or not 0 <= floor < floor + reserve < 100 or not 0 < previous < 100):
            raise RuntimeError("Не удалось перенести защитный порог кампании; ограничение сохранено.")
        metadata = r.read_json(job / "job.json")
        old_setting = validate_threshold(metadata.get(SETTING, 0))
        # A chosen setting belongs to the new manual run, not the retired campaign.
        # Older jobs without a setting inherit the former guard rather than losing it.
        threshold = (old_setting if SETTING in metadata else
                     validate_threshold(math.ceil(max(floor + reserve, previous))))
        # Write the replacement protection FIRST. Partial failure stays fail-closed.
        r.atomic_json(job / "job.json", {**metadata, SETTING: threshold})
        r.atomic_json(guard_path, {**guard, "enabled": False, "manual_resume": {
            "requested_at": r.now_iso(), "previous_setting": old_setting,
            "remaining_percent_threshold": threshold,
            "reason": "Явный ручной запуск после остановки кампании пользователем; выбранный порог сохранён, при отсутствии настройки унаследован порог кампании.",
        }})
        return threshold


def stalled_run(job: Path, run: dict, now: float) -> bool:
    runtime_path = job / "workers-runtime.json"
    runtime = r.read_json(runtime_path) if runtime_path.exists() else {}
    if runtime.get("launch_id") == run.get("launch_id"):
        workers = [row for row in runtime.get("workers", {}).values()
                   if row.get("state") in {"preparing", "starting", "running", "sources", "validating", "verifying"}]
        if workers:
            # Do not interrupt healthy peers because one model is taking longer.
            return all(age_seconds(row.get("last_event_at") or row.get("started_at"), now) > STALL_SECONDS
                       for row in workers)
    return age_seconds(run.get("started_at"), now) > PREPARATION_SECONDS


def retryable_reason(reason: str) -> bool:
    text = reason.casefold()
    return any(term in text for term in (
        "модель временно перегружена", "сервис модели временно недоступен",
        "без подтверждения окончания сеанса", "connecttimeout", "readtimeout",
        "истекло время сеанса", "нет событий исполнителя", "timeout",
    ))


class Campaign:
    def __init__(self, config_path: Path):
        self.path = config_path.resolve()
        self.config = r.read_json(self.path)
        self.root = Path(self.config["root"]).resolve()
        if self.path.parent != self.root / "RESULTS":
            raise ValueError("Campaign must live inside this application's RESULTS")
        self.jobs = [checked_job(self.root / "RESULTS" / item["job"], self.root)
                     for item in self.config["jobs"]]
        if not self.jobs or len(set(self.jobs)) != len(self.jobs):
            raise ValueError("Campaign needs unique local jobs")
        floor = self.config.get("minimum_remaining_percent")
        reserve = self.config.get("reserve_percent", 5)
        if type(floor) not in (int, float) or type(reserve) not in (int, float) or not 0 <= floor < floor + reserve < 100:
            raise ValueError("Invalid quota floor/reserve")
        self.threshold = floor + reserve
        self.status_path = self.path.with_name(self.path.stem + "-status.json")
        self.state = r.read_json(self.status_path) if self.status_path.exists() else {
            "status": "running", "cursor": 0, "jobs": {}, "started_at": r.now_iso()}
        self.state.update(supervisor_pid=os.getpid())

    def save(self):
        self.state["updated_at"] = r.now_iso()
        r.atomic_json(self.status_path, self.state)

    def guard(self, allow: bool, reason: str = "", remaining: float | None = None):
        for job in self.jobs:
            r.atomic_json(job / GUARD_FILE, {
                "enabled": True, "allow": allow, "reason": reason,
                "checked_at_epoch": time.time(), "supervisor_pid": os.getpid(),
                "remaining_percent": remaining, "minimum_remaining_percent": self.config["minimum_remaining_percent"],
                "stop_at_remaining_percent": self.threshold, "campaign": str(self.path)})

    def stop_all(self, status: str, reason: str):
        self.state.update(status=status, reason=reason)
        self.guard(False, reason, self.state.get("remaining_percent"))
        self.save()  # Latch BEFORE sending any signals. A restart may not clear it.
        for job in self.jobs:
            if r.read_run_record(job).get("active"):
                r.stop_run(job)

    def tick(self) -> bool:
        if self.state["status"] != "running":
            return False
        try:
            remaining = remaining_percent(r.read_codex_rate_limits())
        except (Exception, SystemExit) as exc:
            self.stop_all("quota_unknown", f"Не удалось получить свежий лимит ({type(exc).__name__}); анализ остановлен.")
            return False
        self.state["remaining_percent"] = remaining
        if remaining <= self.threshold:
            self.stop_all("budget_paused", f"Остановка по лимиту: осталось {remaining:g}%; резерв {self.threshold:g}% для границы {self.config['minimum_remaining_percent']:g}%.")
            return False
        self.guard(True, remaining=remaining)
        cursor = self.state["cursor"]
        if cursor >= len(self.jobs):
            has_gaps = any(entry.get("status") == "needs_attention" for entry in self.state["jobs"].values())
            self.state.update(status="needs_attention" if has_gaps else "completed")
            self.guard(False, "Автоматическая очередь завершена; результаты сохранены.", remaining)
            self.save()
            return False
        job = self.jobs[cursor]
        entry = self.state["jobs"].setdefault(job.name, {"restarts": 0})
        selected = set(q.priority_marker_ids(job / "decisions.jsonl"))
        allowed = set(self.config["jobs"][cursor]["marker_ids"])
        if not selected <= allowed:
            self.stop_all("selection_changed", "Выбранная очередь изменена вне задания; автоматическое продолжение остановлено.")
            return False
        run = r.read_run_record(job)
        if run.get("active"):
            entry.setdefault("launch_id", run["launch_id"])
            if entry["launch_id"] != run["launch_id"]:
                self.stop_all("selection_changed", "Запуск изменён пользователем; автоматическое продолжение остановлено.")
                return False
            if selected and q.pause_requested(job / "decisions.jsonl") and not entry.get("restart_requested"):
                entry["user_pause_requested"] = True
            if stalled_run(job, run, time.time()):
                entry["restart_requested"] = True
                entry["reason"] = "Нет событий исполнителей дольше установленного времени."
                r.stop_run(job)
            self.save()
            return True
        # Detached launchers can write final state just before their process exits.
        if r.process_is_alive(run.get("runner_pid")):
            self.save()
            return True
        if run.get("stop_kind") == "codex_percentage":
            self.stop_all("budget_paused", run.get("reason") or "Остановка по остатку лимита Codex.")
            return False
        rows = q.load_decisions(job / "decisions.jsonl")
        pending_verification = any(row["marker_id"] in allowed and q.verification_status(row) == "pending" for row in rows)
        if entry.get("user_pause_requested"):
            self.stop_all("user_stopped", "Пользователь завершил анализ; автоматическое продолжение остановлено.")
            return False
        if not selected and not pending_verification:
            unfinished = [row["marker_id"] for row in rows if row["marker_id"] in allowed
                          and (not row.get("verdict") or q.verification_status(row) == "challenged")]
            entry.update(status="needs_attention" if unfinished else "completed", remaining_ids=unfinished, completed_at=r.now_iso())
            self.state["cursor"] += 1
            self.save()
            return True
        retry_ids = None
        if entry.get("launch_id"):
            if run.get("status") == "stopped" and not entry.get("restart_requested"):
                self.stop_all("user_stopped", "Анализ остановлен; автоматический запуск не отменяет остановку пользователя.")
                return False
            gaps = r.read_json(job / "incomplete-analysis.json") if (job / "incomplete-analysis.json").exists() else {}
            retry_ids = selected if entry.get("restart_requested") else {mid for mid in selected if retryable_reason(str(gaps.get(mid, "")))}
            # Older runners left verifier serialization failures in the worker
            # journal, not incomplete-analysis.json. Retry only pending checks
            # from this exact launch; never replay a completed primary verdict.
            runtime_path = job / "workers-runtime.json"
            runtime = r.read_json(runtime_path) if runtime_path.exists() else {}
            if runtime.get("launch_id") == run.get("launch_id"):
                pending = {row["marker_id"] for row in rows if q.verification_status(row) == "pending"}
                for mid, worker in runtime.get("workers", {}).items():
                    error = str(worker.get("error") or "")
                    if (mid in selected & pending and worker.get("state") == "incomplete"
                            and ("evidence must be a non-empty string array" in error
                                 or "rechecked_paths must be a non-empty string array" in error)):
                        retry_ids.add(mid)
            retryable = bool(retry_ids)
            if not retryable or entry["restarts"] >= 1:
                entry.update(status="needs_attention", remaining_ids=sorted(selected), reason=run.get("reason", ""))
                self.state["cursor"] += 1
                self.save()
                return True
            entry["restarts"] += 1
        # Persist attempt accounting first, so a crash cannot give infinite retries.
        entry.update(status="launching", restart_requested=False)
        self.save()
        set_pause(job, False)
        if retry_ids is not None:
            with q.decision_lock(job / "decisions.jsonl"):
                control = r.read_json(job / "control.json")
                control["deferred_marker_ids"] = sorted(selected - retry_ids)
                r.atomic_json(job / "control.json", control)
        launched = r.launch_runner(job, self.root / "app")
        entry.update(launch_id=launched["launch_id"], status="running")
        self.save()
        return True

    def run(self):
        # Hold an OS-backed lock for the entire lifetime; a second guard cannot
        # race the cursor, refresh an expired lease or double-spend retry budgets.
        with q.decision_lock(self.path):
            if self.status_path.exists():
                self.state = r.read_json(self.status_path)
            self.state["supervisor_pid"] = os.getpid()
            try:
                while self.tick():
                    time.sleep(POLL_SECONDS)
            except BaseException as exc:
                self.stop_all("supervisor_error", f"Ошибка фонового контроля ({type(exc).__name__}); очередь сохранена.")
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    Campaign(args.run).run()


if __name__ == "__main__":
    main()
