"""Stop at a user-selected remaining Codex percentage, never a per-run token budget."""
from __future__ import annotations

import math
import threading
from datetime import datetime
from pathlib import Path

from triage_dashboard import atomic_json, read_json

SETTING = "codex_min_remaining_percent"
STATE_FILE = "usage-guard.json"
POLL_SECONDS = 15
_guards: dict[tuple[str, str], "UsageGuard"] = {}


def validate_threshold(value) -> int:
    if type(value) is not int or not 0 <= value <= 99:
        raise ValueError("Порог остатка Codex должен быть целым числом от 0 до 99%; 0 — отключено.")
    return value


def remaining_percent(payload: dict) -> float:
    """Use the tightest core Codex window; unknown is not zero or unlimited."""
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


class UsageGuard:
    def __init__(self, job: Path, launch_id: str, read_limits):
        self.job, self.launch_id, self.read_limits = job, launch_id, read_limits
        self.reason = ""
        self.threshold = 0
        self.remaining = None
        self.closed = threading.Event()
        self.thread = None
        self.key = (str(job.resolve()), launch_id)

    def check(self):
        if self.reason or self.closed.is_set():
            return  # A recovered limit never auto-resumes this stopped launch.
        try:
            self.threshold = validate_threshold(read_json(self.job / "job.json").get(SETTING, 0))
            if self.threshold:
                self.remaining = remaining_percent(self.read_limits())
                if self.remaining <= self.threshold:
                    self.reason = (f"Остановка по лимиту Codex: осталось {self.remaining:g}%, "
                                   f"порог ≤ {self.threshold}%. Результаты и очередь сохранены.")
            else:
                self.remaining = None
        except (Exception, SystemExit):
            self.reason = "Не удалось проверить остаток лимита Codex; анализ остановлен, результаты и очередь сохранены."
        if self.closed.is_set():
            return
        try:
            atomic_json(self.job / STATE_FILE, {
                "launch_id": self.launch_id, "threshold": self.threshold,
                "remaining_percent": self.remaining,
                "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": "paused" if self.reason else "watching" if self.threshold else "disabled",
                "reason": self.reason,
            })
        except (OSError, ValueError):
            self.reason = "Не удалось сохранить состояние контроля лимита; анализ остановлен."

    def start(self):
        _guards[self.key] = self
        self.check()  # Fresh preflight, before preparation and any model process.
        if not self.reason:
            self.thread = threading.Thread(target=self._poll, name="codex-usage-guard", daemon=True)
            self.thread.start()
        return self

    def _poll(self):
        while not self.closed.wait(POLL_SECONDS):
            self.check()
            if self.reason:
                return

    def close(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        if _guards.get(self.key) is self:
            _guards.pop(self.key, None)


def stop_reason(job: Path, launch_id: str) -> str:
    guard = _guards.get((str(job.resolve()), launch_id))
    # Never wait for network in worker supervision loops.
    return guard.reason if guard is not None else ""


def snapshot(job: Path) -> dict:
    try:
        value = read_json(job / STATE_FILE)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def display_text(threshold: int) -> str:
    return f"Автоостановка при остатке Codex ≤ {threshold}%" if threshold else "Автоостановка по остатку Codex: отключена"
