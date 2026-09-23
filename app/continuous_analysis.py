"""Durable, bounded work-conserving slots. A slow marker never holds a batch barrier."""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import triage_queue as q
from parallel_analysis import WorkerRuntime, run_workers

STATE_FILE = "scheduler-state.json"


def resumable_context(job: Path, revision: str, snapshot: str) -> dict | None:
    path = job / STATE_FILE
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not value.get("leases"):
        return None
    if value.get("revision") != revision or value.get("snapshot_id") != snapshot:
        raise ValueError("Сохранённые назначения относятся к другой ревизии или снимку. Сбросьте очередь.")
    return {"scheduler_version": 2, "resume_stream": True,
            "batch": {"marker_ids": [item["marker_id"] for item in value["leases"].values()]}}


class Scheduler:
    def __init__(self, r, job, app, repository, revision, launch_id, started_at, initial):
        self.r, self.job, self.app = r, job, app
        self.repository, self.revision = repository, revision
        self.launch_id, self.started_at = launch_id, started_at
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.halt = threading.Event()
        self.inflight: set[str] = set()
        self.runtime = WorkerRuntime(r, job, launch_id)
        self.incomplete = 0
        self.decisions = job / "decisions.jsonl"
        metadata = r.read_json(job / "job.json")
        self.model = metadata.get("codex_model") or None
        previous_runtime = job / "workers-runtime.json"
        self.previous_workers = (r.read_json(previous_runtime).get("workers", {})
                                 if previous_runtime.exists() else {})
        self.capacity = int(metadata.get("parallel_workers") or 1)
        if not 1 <= self.capacity <= 8:
            raise ValueError("Число параллельных исполнителей должно быть от 1 до 8")
        if initial.get("resume_stream"):
            self.state = r.read_json(job / STATE_FILE)
        else:
            self.state = {"schema_version": 1, "revision": revision,
                          "snapshot_id": str(metadata.get("snapshot_id") or ""),
                          "selection_only": bool(initial.get("one_shot")) or q.manual_selection_only(self.decisions),
                          "leases": {}, "attempted": [], "launch_id": launch_id}
            for assignment in initial["batch"]["assignments"]:
                context = copy.deepcopy(initial)
                context["batch"] = {**context["batch"], "assignments": [assignment],
                                    "marker_ids": assignment["marker_ids"], "count": 1, "worker_count": 1,
                                    "trace_groups": assignment.get("groups", [])}
                self._add(context, int(assignment["worker"]), prepared=True)
        if self.state.get("launch_id") != launch_id:
            by_id = {row["marker_id"]: row for row in q.load_decisions(self.decisions)}
            for entry in self.state["leases"].values():
                # Continuing interrupted investigation is a new history attempt.
                # An already applied result instead resumes its original commit.
                if not by_id.get(entry["marker_id"], {}).get("verdict"):
                    entry.update(attempt_launch_id=launch_id, started_at=r.now_iso())
            self.state.update(launch_id=launch_id, attempted=[])
        self._publish()
        r._update_run(job, launch_id, status="running", active=True, started_at=started_at,
                      runner_pid=os.getpid(), codex_pid=None, parallel_workers=self.capacity,
                      requested_model=metadata.get("codex_model") or None,
                      execution_mode="independent_workers", scheduler="continuous", phase="analysis",
                      phase_detail=f"Непрерывная очередь: до {self.capacity} независимых исполнителей")

    def _add(self, context, slot, *, prepared):
        number = int(context["batch_number"])
        relative = f"scheduler/contexts/batch-{number:03d}-worker-{slot}.json"
        context.update(continuous=True, scheduler_version=2, scheduler_context_file=relative)
        (self.job / relative).parent.mkdir(parents=True, exist_ok=True)
        self.r.atomic_json(self.job / relative, context)
        self.state["leases"][str(slot)] = {
            "marker_id": context["batch"]["marker_ids"][0], "batch": number,
            "context_file": relative, "prepared": prepared,
            "attempt_launch_id": self.launch_id, "started_at": self.r.now_iso(),
        }

    def _publish(self):
        # The journal is authoritative after a crash; status is a rebuildable UI view.
        self.r.atomic_json(self.job / STATE_FILE, self.state)
        workers = [{"worker": int(slot), "batch": entry["batch"], "status": "assigned",
                    "assigned": 1, "saved": 0, "marker_ids": [entry["marker_id"]]}
                   for slot, entry in self.state["leases"].items()]
        old = self.r.read_json(self.job / "workers.status.json") if (self.job / "workers.status.json").exists() else {}
        self.r.atomic_json(self.job / "workers.status.json", {
            "state": "assigned" if workers else "saved", "scheduler": "continuous",
            "batch": max([int(old.get("batch") or 0)] + [item["batch"] for item in workers]),
            "one_shot": self.state["selection_only"], "updated_at": self.r.now_iso(), "workers": workers,
        })

    def claim(self, slot):
        with self.lock, q.decision_lock(self.decisions):
            if self.halt.is_set() or self.r._stop_requested(self.job, self.launch_id):
                return None
            existing = self.state["leases"].get(str(slot))
            if existing:
                self.inflight.add(str(slot))
                return copy.deepcopy(existing)
            # After reducing concurrency, old numbered reservations still own
            # their note paths. Run them in the bounded pool, not queued lanes
            # that can deadlock behind idle threads waiting for those leases.
            orphan = next((owner for owner in self.state["leases"]
                           if int(owner) > self.capacity and owner not in self.inflight), None)
            if orphan is not None:
                self.inflight.add(orphan)
                return {**copy.deepcopy(self.state["leases"][orphan]), "_owner_slot": int(orphan)}
            if q.primary_queue_blocked(self.decisions):
                return None
            inventory = q.load_inventory(self.job / "markers.inventory.json")
            decisions = q.load_decisions(self.decisions)
            by_id = {str(row["marker_id"]): row for row in decisions}
            control_path = self.job / "control.json"
            control = self.r.read_json(control_path) if control_path.exists() else {}
            if control.get("manual_queue_requested") or control.get("single_marker_requested"):
                self.state["selection_only"] = True
            active = {item["marker_id"] for item in self.state["leases"].values()}
            excluded = active | set(self.state["attempted"]) | set(control.get("deferred_marker_ids") or [])
            rechecks = set(control.get("recheck_marker_ids") or [])
            preferred = [mid for mid in q.runnable_priority_marker_ids(self.decisions)
                         if mid not in excluded and mid in by_id
                         and (not by_id[mid].get("verdict") or mid in rechecks
                              or q.verification_status(by_id[mid]) == "pending")]
            drafts = q.saved_draft_ids(self.job, decisions)
            explicit = control.get("manual_queue_requested") or control.get("single_marker_requested")
            if not explicit:
                preferred = [mid for mid in preferred if mid not in drafts]
            if not preferred and self.state["selection_only"]:
                return None
            metadata = self.r.read_json(self.job / "job.json")
            if not self.state["selection_only"] and q.job_run_mode(self.decisions) == "single_batch":
                remaining = int(control.get("run_remaining", metadata.get("batch_size") or 1))
                if len(active) >= remaining:
                    return None
            verification_only = bool(preferred and by_id[preferred[0]].get("verdict") == "Confirmed"
                                     and q.verification_status(by_id[preferred[0]]) == "pending")
            if verification_only:
                decisions = [{**row, "verdict": None} if row["marker_id"] == preferred[0] else row
                             for row in decisions]
            elif preferred and preferred[0] in rechecks and by_id[preferred[0]].get("verdict"):
                q.reopen(decisions, [preferred[0]], self.decisions,
                         {str(marker["id"]) for marker in q.markers_for_triage(inventory)})
                decisions = q.load_decisions(self.decisions)
            queued = q.next_parallel_batch(inventory, decisions, 1, 1, preferred[:1] or None,
                                          excluded_ids=excluded | (drafts - set(preferred)))
            if not queued.get("batch"):
                return None
            queued.update(batch_number=q.next_batch_number(self.job), one_shot=self.state["selection_only"])
            if verification_only:
                queued["verification_only"] = True
            queued["batch"]["assignments"][0]["worker"] = slot
            self._add(queued, slot, prepared=False)
            self._publish()
            self.inflight.add(str(slot))
            return copy.deepcopy(self.state["leases"][str(slot)])

    def finish(self, slot, entry, *, incomplete):
        with self.lock, q.decision_lock(self.decisions):
            mid = entry["marker_id"]
            control_path = self.job / "control.json"
            control = self.r.read_json(control_path) if control_path.exists() else {}
            receipt = f"{entry['attempt_launch_id']}:{entry['batch']}:{mid}"
            receipts = list(control.get("scheduler_completed_attempts") or [])
            # A crash after updating control but before dropping the lease must not
            # consume the run budget a second time on recovery.
            if receipt not in receipts:
                receipts.append(receipt)
                control["scheduler_completed_attempts"] = receipts
                if incomplete:
                    control["deferred_marker_ids"] = list(dict.fromkeys([
                        *(control.get("deferred_marker_ids") or []), mid]))
                else:
                    for key in ("priority_marker_ids", "recheck_marker_ids", "deferred_marker_ids"):
                        control[key] = [value for value in control.get(key, []) if value != mid]
                if not self.state["selection_only"] and q.job_run_mode(self.decisions) == "single_batch":
                    metadata = self.r.read_json(self.job / "job.json")
                    remaining = max(0, int(control.get("run_remaining", metadata.get("batch_size") or 1)) - 1)
                    control.update(run_remaining=remaining, single_batch_completed=remaining == 0)
                if self.state["selection_only"] and not control.get("priority_marker_ids"):
                    control.update(one_shot_completed=True, pause_requested=True)
                control["updated_at"] = self.r.now_iso()
                self.r.atomic_json(control_path, control)
            self.state["attempted"] = list(dict.fromkeys([*self.state["attempted"], mid]))
            self.state["leases"].pop(str(slot), None)
            self._publish()
            self.changed.notify_all()

    def history(self, context, entry, exit_code, reason, *, verification=False):
        with self.lock, q.decision_lock(self.decisions):
            self.r.append_batch_history(
                self.job, context,
                launch_id=self.launch_id + ":verification" if verification else entry["attempt_launch_id"],
                runner_batch=int(entry["batch"]), started_at=entry["started_at"],
                finished_at=self.r.now_iso(), elapsed_seconds=context.get("elapsed_seconds", 0),
                exit_code=exit_code, usage={}, agent_messages=[], failure_reason=reason,
            )

    def process(self, slot, entry):
        r, job = self.r, self.job
        context = r.read_json(job / entry["context_file"])
        mid = entry["marker_id"]
        clock = time.monotonic()
        code, reason = 0, ""
        measurement = context.get("worker_measurements", {}).get(mid) or self.previous_workers.get(mid, {})
        expected_log = f"/batch-{int(entry['batch']):03d}/worker-{slot}/events.jsonl"
        if (measurement.get("state") == "finished" and
                str(measurement.get("event_log") or "").endswith(expected_log)):
            self.runtime.update(mid, **{**measurement, "pid": None})
        else:
            self.runtime.update(mid, worker=slot, state="preparing", pid=None, started_at=entry["started_at"])
        try:
            if not entry["prepared"]:
                metadata = r.read_json(job / "job.json")
                metadata["codex_model"] = self.model
                app = Path(metadata.get("app_directory") or self.app / "app")
                context = r.prepare_batch(job, app, metadata, self.repository, self.revision, self.launch_id,
                                          queued=context, context_file=entry["context_file"])
                with self.lock:
                    self.state["leases"][str(slot)]["prepared"] = True
                    r.atomic_json(job / STATE_FILE, self.state)
            else:
                context.update(continuous=True, scheduler_context_file=entry["context_file"])
            context["codex_model"] = self.model
            app = Path(r.read_json(job / "job.json").get("app_directory") or self.app / "app")
            current = next(row for row in q.load_decisions(self.decisions) if row["marker_id"] == mid)

            def analyze(value):
                if r._stop_requested(job, self.launch_id):
                    raise r.IncompleteAnalysisError("Исследование остановлено пользователем.")
                run_workers(r, job, app, self.launch_id, value, self.started_at,
                            int(entry["batch"]), shared=self.runtime)

            if not context.get("verification_only") and not current.get("verdict"):
                analyze(context)
                r.finalize_with_quality_repair(job, app, context, analyze, commit_lock=self.lock)
            # Save the primary verdict AND history now, before any verifier or peer.
            context["elapsed_seconds"] = time.monotonic() - clock
            if not context.get("verification_only"):
                self.history(context, entry, 0, "")
            self.runtime.update(mid, state="applied", pid=None)
            verify = context if context.get("verification_only") else r.verification_context(job, context)
            if verify and not r._stop_requested(job, self.launch_id):
                # Resumed verification-only batches can contain several slots.
                # Their note paths must remain independent, just like primaries.
                verify["verifier_number"] = slot
                self.runtime.update(mid, state="verifying", started_at=r.now_iso(), duration_seconds=0)
                try:
                    analyze(verify)
                    with self.lock:
                        r.finalize_turn(job, verify)
                except (Exception, SystemExit) as exc:
                    code, reason = 3, str(exc)
                self.history(verify, entry, code, reason, verification=True)
                if code:
                    self.runtime.update(mid, state="incomplete", error=reason, pid=None)
                else:
                    self.runtime.update(mid, state="applied", pid=None)
        except (Exception, SystemExit) as exc:
            code, reason = 3, str(exc)
            with self.lock, q.decision_lock(self.decisions):
                r.mark_incomplete(job, context, reason, [mid])
            context["elapsed_seconds"] = time.monotonic() - clock
            self.history(context, entry, code, reason)
            self.runtime.update(mid, state="incomplete", error=reason, pid=None)
        if code:
            with self.lock:
                self.incomplete += 1
        if not r._stop_requested(job, self.launch_id):
            self.finish(slot, entry, incomplete=bool(code))

    def lane(self, slot):
        try:
            while True:
                entry = self.claim(slot)
                if entry is None:
                    with self.changed:
                        if (not self.state["leases"] or self.halt.is_set()
                                or self.r._stop_requested(self.job, self.launch_id)
                                or q.primary_queue_blocked(self.decisions)):
                            return
                        # Stay available while other slots work: the user can add
                        # more selected markers, or a reservation can release quota.
                        self.changed.wait(timeout=.2)
                    continue
                owner = entry.get("_owner_slot", slot)
                try:
                    self.process(owner, entry)
                finally:
                    with self.lock:
                        self.inflight.discard(str(owner))
        except BaseException:
            self.halt.set()  # Leave reservations durable; let already running peers save.
            raise


def run_continuous(r, job, app, repository, revision, launch_id, started_at, initial):
    scheduler = Scheduler(r, job, app, repository, revision, launch_id, started_at, initial)
    slots = range(1, scheduler.capacity + 1)
    errors = []
    with ThreadPoolExecutor(max_workers=scheduler.capacity, thread_name_prefix="triage-slot") as pool:
        futures = [pool.submit(scheduler.lane, slot) for slot in slots]
        for future in futures:
            try:
                future.result()
            except (Exception, SystemExit) as exc:
                errors.append(str(exc))
    return (2 if errors else 0), "; ".join(errors), scheduler.incomplete
