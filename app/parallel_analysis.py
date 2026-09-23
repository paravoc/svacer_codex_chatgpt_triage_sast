"""Application-owned single-marker processes; no model-managed delegation."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from web_research import save_web_event
from investigation import incomplete_review_feedback
from result_transport import read_final_reply

RUNTIME_FILE = "workers-runtime.json"
TRANSIENT_RETRY_DELAYS = (5.0, 15.0)
INACTIVITY_TIMEOUT_SECONDS = 10 * 60
MARKER_TIMEOUT_SECONDS = 45 * 60


def transient_failure_kind(value: dict) -> str | None:
    """Classify only transport error events, never source/tool/agent prose."""
    if value.get("type") not in {"error", "turn.failed"}:
        return None
    error = value.get("error")
    message = (error.get("message", "") if isinstance(error, dict)
               else value.get("message", ""))
    if not isinstance(message, str):
        return None
    message = message.casefold()
    if "model is at capacity" in message or "model is currently at capacity" in message:
        return "model_capacity"
    if any(fragment in message for fragment in (
        "503 service unavailable", "502 bad gateway", "504 gateway timeout",
        "temporarily unavailable", "server overloaded",
    )):
        return "service_unavailable"
    return None


def wait_for_retry(delay: float, should_stop) -> bool:
    """Backoff is local to this worker and remains responsive to Stop."""
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if should_stop():
            return False
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    return not should_stop()


class WorkerRuntime:
    """One synchronized journal shared by all slots of a rolling queue."""

    def __init__(self, r: Any, job: Path, launch_id: str):
        self.lock = threading.RLock()
        self.value = {"launch_id": launch_id, "scheduler": "continuous", "workers": {}}
        self.r, self.job = r, job

    def update(self, mid: str, **changes: Any) -> None:
        with self.lock:
            self.value["workers"].setdefault(mid, {}).update(changes)
            self.r.atomic_json(self.job / RUNTIME_FILE, self.value)


def read_worker_runtime(job: Path, run: dict | None = None) -> dict:
    try:
        value = json.loads((job / RUNTIME_FILE).read_text(encoding="utf-8"))
        current = run if run is not None else json.loads((job / "codex-run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("launch_id") != current.get("launch_id"):
        return {}
    return value


def run_workers(r: Any, job: Path, app: Path, launch_id: str, context: dict,
                started_at: str, batch_index: int, *, shared: WorkerRuntime | None = None) -> tuple[int, int]:
    """Each process finishes its own source rounds independently of its peers.

    Only the parent applies results/updates the queue, after the normal evidence
    checks. Worker failures remain unfinished, never trigger sequential fallback.
    """
    assignments = context["batch"]["assignments"]
    ids = [str(mid) for a in assignments for mid in a["marker_ids"]]
    numbers = [int(a["worker"]) for a in assignments]
    if (not 1 <= len(assignments) <= 8 or any(len(a["marker_ids"]) != 1 for a in assignments)
            or len(set(ids)) != len(ids) or len(set(numbers)) != len(numbers)
            or set(ids) != set(map(str, context["batch"]["marker_ids"]))):
        raise ValueError("Для параллельного запуска требуется один уникальный маркер на исполнителя.")
    lock = shared.lock if shared else threading.RLock()
    previous = read_worker_runtime(job)
    runtime = {"launch_id": launch_id, "batch": context["batch_number"], "workers": {}}
    if shared:
        runtime = shared.value
    elif previous.get("batch") == context["batch_number"]:
        runtime["workers"] = previous.get("workers", {})
    if not shared:
        r._update_run(job, launch_id, status="running", active=True, started_at=started_at,
                  runner_pid=os.getpid(), codex_pid=None, execution_mode="independent_workers",
                  phase="analysis", phase_detail=f"Независимый анализ: {len(ids)} маркеров",
                  batch_index=batch_index)

    def update(mid: str, **changes: Any) -> None:
        with lock:
            runtime["workers"].setdefault(mid, {}).update(changes)
            r.atomic_json(job / RUNTIME_FILE, runtime)

    def event(mid: str, number: int, value: dict, stream: Any) -> None:
        # Stamp at receipt, not on a GUI refresh. Do not log commands' environment.
        stamped = {**value, "timestamp": r.now_iso(), "marker_id": mid,
                   "worker": number, "launch_id": launch_id}
        line = json.dumps(stamped, ensure_ascii=False, separators=(",", ":")) + "\n"
        stream.write(line)
        stream.flush()
        try:
            save_web_event(job, context, mid, stamped)
        except (OSError, ValueError):
            pass  # A navigation-cache failure must not discard a worker's result.
        with lock:
            with (job / r.EVENT_LOG).open("a", encoding="utf-8") as merged:
                merged.write(line)
            update(mid, last_event_at=stamped["timestamp"])

    cancelled = threading.Event()

    def worker(assignment: dict) -> dict:
        mid = str(assignment["marker_ids"][0])
        number = int(assignment["worker"])
        directory = job / "worker-runs" / launch_id / f"batch-{int(context['batch_number']):03d}" / f"worker-{number}"
        verification = bool(context.get("verification_only"))
        if verification:
            directory = directory / "verification"
        directory.mkdir(parents=True, exist_ok=True)
        relative = directory.relative_to(job).as_posix()
        worker_context = copy.deepcopy(context)
        # Parent finalization can reject one worker's schema. Its repair request
        # and budget must not restart or exhaust the budget of a healthy peer.
        repair_ids = context.get("quality_repair_marker_ids")
        if repair_ids is not None and mid not in repair_ids:
            worker_context.pop("quality_feedback", None)
        worker_context.pop("quality_repair_count", None)
        # Resume only this worker's previously fetched context in this launch.
        saved_context = directory / "context.json"
        if saved_context.exists():
            previous_context = r.read_json(saved_context)
            for key in ("external_sources", "source_request_errors", "requested_source_paths",
                        "source_request_round", "source_request_round_start", "source_request_resolutions",
                        "available_source_requests", "source_request_reuse_counts", "worker_quality_repair_count",
                        "verification_repair_count"):
                if key in previous_context:
                    worker_context[key] = previous_context[key]
        if worker_context.get("quality_feedback"):
            worker_context["worker_quality_repair_count"] = 1
        worker_context.update(launch_id=launch_id, context_file=f"{relative}/context.json",
                              source_request_file=f"{relative}/source-requests.json")
        worker_context["batch"] = {**worker_context["batch"], "marker_ids": [mid], "count": 1,
                                   "worker_count": 1, "assignments": [assignment]}
        # Expose only assigned trace rows; the checkout/catalog remain read-only inputs.
        traces = []
        for index, path in enumerate(context.get("trace_files", [])):
            payload = r.read_json(job / path)
            if isinstance(payload, dict):
                payload["markers"] = [row for row in payload.get("markers", []) if str(row.get("id")) == mid]
                if not payload["markers"]:
                    continue
            target = directory / f"trace-{index}.json"
            r.atomic_json(target, payload)
            traces.append(target.relative_to(job).as_posix())
        worker_context["trace_files"] = traces
        note = job / "notes" / (f"verify-batch-{int(context['batch_number']):03d}-verifier-{int(context.get('verifier_number') or 1)}.json" if verification
                                else f"batch-{int(context['batch_number']):03d}-worker-{number}.json")
        recovered = None if verification else r.recover_completed_worker_result(job, worker_context, assignment)
        if recovered:
            r.atomic_json(note, recovered)
        if not verification and not worker_context.get("quality_feedback") and r.saved_worker_notes_match(job, worker_context):
            update(mid, worker=number, state="finished", pid=None)
            return {"context": worker_context, "pid": 0, "exit_code": 0}
        clock_start = time.monotonic()
        with lock:
            old = dict(runtime["workers"].get(mid, {}))
        update(mid, worker=number, state="starting", pid=None, event_log=f"{relative}/events.jsonl",
               started_at=old.get("started_at") or r.now_iso(), finished_at=None,
               duration_seconds=float(old.get("duration_seconds") or 0), error="")
        code, pid = 0, 0
        latest_row = None
        usage_complete = True
        retry_count = 0
        process, reader = None, None
        try:
            if cancelled.is_set() or r._stop_requested(job, launch_id):
                raise r.IncompleteAnalysisError("Исследование остановлено пользователем.")
            # A user-approved scope exclusion is a local policy decision. Do not
            # ask a model to invent a component defect or investigate excluded CI.
            from analysis_scope import build_only_result
            marker = next((m for m in assignment.get("markers", []) if str(m.get("id")) == mid), {})
            scoped = None if verification else build_only_result(job, worker_context, marker)
            if scoped is not None:
                errors = r.review_result(job, worker_context, scoped)
                if errors:
                    raise r.IncompleteAnalysisError("Не пройдена проверка области: " + "; ".join(errors))
                r.atomic_json(saved_context, worker_context)
                r.atomic_json(directory / "scope-result.json", [scoped])
                r.atomic_json(note, [scoped])
                update(mid, state="finished", execution_kind="scope_policy",
                       policy_message=scoped["disposition_reason"])
                return {"context": worker_context, "pid": 0, "exit_code": 0}
            codex = r.find_codex_executable()
            environment = r.codex_child_environment()
            while True:
                if time.monotonic() - clock_start > MARKER_TIMEOUT_SECONDS:
                    raise r.IncompleteAnalysisError("Истекло время исследования маркера; черновик и очередь сохранены.")
                if cancelled.is_set() or r._stop_requested(job, launch_id):
                    raise r.IncompleteAnalysisError("Исследование остановлено пользователем.")
                turn_id = uuid.uuid4().hex
                result_file = directory / f"result-{turn_id}.json"
                reply_file = directory / f"reply-{turn_id}.txt"
                worker_context.update(result_file=str(result_file.resolve()),
                                      source_request_file=str((directory / f"sources-{turn_id}.json").resolve()))
                r.atomic_json(saved_context, worker_context)
                prompt = r.build_runtime_prompt(job, app, worker_context)
                (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
                # Keep immutable prompts per turn for diagnostics, without shared paths.
                (directory / f"prompt-{uuid.uuid4().hex}.txt").write_text(prompt, encoding="utf-8")
                command = r.build_codex_command(codex, job, reply_file, context.get("codex_model"))
                with (directory / "stderr.log").open("a", encoding="utf-8") as stderr, (
                    directory / "events.jsonl"
                ).open("a", encoding="utf-8") as output:
                    process = subprocess.Popen(command, cwd=str(job), stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=stderr, text=True,
                                               encoding="utf-8", errors="replace", env=environment,
                                               **r.hidden_subprocess_kwargs())
                    pid = process.pid
                    update(mid, state="running", pid=pid, process_started_at=r.now_iso(), retry_reason="")
                    reader_errors: list[Exception] = []
                    completed = threading.Event()
                    failure_kind = None
                    last_activity = time.monotonic()

                    def drain() -> None:
                        nonlocal failure_kind, last_activity
                        try:
                            for line in process.stdout:
                                try:
                                    value = json.loads(line)
                                except ValueError:
                                    continue
                                if isinstance(value, dict):
                                    last_activity = time.monotonic()
                                    event(mid, number, value, output)
                                    if value.get("type") in {"error", "turn.failed"}:
                                        failure_kind = transient_failure_kind(value)
                                    if value.get("type") == "turn.completed":
                                        completed.set()
                        except Exception as exc:
                            reader_errors.append(exc)

                    reader = threading.Thread(target=drain, daemon=True)
                    reader.start()
                    turn_start = time.monotonic()
                    try:
                        process.stdin.write(prompt)
                        process.stdin.close()
                        while process.poll() is None:
                            if cancelled.is_set() or r._stop_requested(job, launch_id):
                                cancelled.set()
                                raise r.IncompleteAnalysisError("Исследование остановлено пользователем.")
                            if time.monotonic() - turn_start > r.CODEX_TURN_TIMEOUT_SECONDS:
                                raise r.IncompleteAnalysisError("Истекло время сеанса маркера; черновик и очередь сохранены.")
                            if time.monotonic() - last_activity > INACTIVITY_TIMEOUT_SECONDS:
                                raise r.IncompleteAnalysisError("Нет событий исполнителя 10 минут; черновик и очередь сохранены.")
                            if time.monotonic() - clock_start > MARKER_TIMEOUT_SECONDS:
                                raise r.IncompleteAnalysisError("Истекло время исследования маркера; черновик и очередь сохранены.")
                            time.sleep(0.1)
                        code = process.returncode
                    finally:
                        if process.poll() is None:
                            r._terminate_process_tree(process)
                        reader.join(timeout=10)
                        if not reader.is_alive():
                            process.stdout.close()
                        if not process.stdin.closed:
                            try:
                                process.stdin.close()
                            except OSError:
                                pass
                        update(mid, pid=None)
                    if reader.is_alive() or reader_errors:
                        raise r.IncompleteAnalysisError("Не удалось прочитать журнал исполнителя; результат не применён.")
                    if code or not completed.is_set():
                        usage_complete = False  # An interrupted turn may lack final token usage.
                        if failure_kind:
                            label = ("Выбранная модель временно перегружена" if failure_kind == "model_capacity"
                                     else "Сервис модели временно недоступен")
                            if retry_count < len(TRANSIENT_RETRY_DELAYS):
                                delay = TRANSIENT_RETRY_DELAYS[retry_count]
                                retry_count += 1
                                worker_context["transport_retry_count"] = retry_count
                                update(mid, state="starting", retry_count=retry_count, retry_reason=label, pid=None)
                                # Do not consume source/schema repair budgets or switch models.
                                # A failed turn's final-looking file is never accepted as success.
                                if not wait_for_retry(delay, lambda: cancelled.is_set() or r._stop_requested(job, launch_id)):
                                    raise r.IncompleteAnalysisError("Исследование остановлено пользователем.")
                                continue
                            raise r.IncompleteAnalysisError(
                                f"{label}; {retry_count} автоматических повтора не помогли. "
                                "Исходники и черновик сохранены; повторите запуск позже.")
                        raise r.IncompleteAnalysisError(
                            f"Исполнитель завершился без подтверждения окончания сеанса (код {code}). См. журнал исполнителя.")
                # The CLI writes its final message outside the model sandbox.
                # An exact-ID JSON reply is transport only, never a validation bypass.
                reply = read_final_reply(reply_file, [mid])
                if reply is not None:
                    reply_rows, requests = reply
                    r.atomic_json(result_file, reply_rows)
                    if requests:
                        r.atomic_json(Path(worker_context["source_request_file"]), requests)
                # Only a new, per-turn file may become the result. Historical or
                # accidentally nested notes cannot masquerade as fresh output.
                rows = None
                if result_file.is_file() and not result_file.is_symlink():
                    try:
                        rows = r.read_json(result_file)
                        if isinstance(rows, dict):
                            rows = rows.get("decisions", [rows])
                    except (OSError, ValueError):
                        pass
                valid_rows = (isinstance(rows, list) and len(rows) == 1
                              and isinstance(rows[0], dict) and rows[0].get("marker_id") == mid)
                if valid_rows and not verification:
                    rows = [r.prune_redundant_invalid_source_evidence(job, worker_context, rows[0])]
                if valid_rows:
                    worker_context["previous_result_file"] = str(result_file.resolve())
                    latest_row = rows[0]
                if valid_rows and (rows[0].get("analysis_status") == "needs_context" or rows[0].get("verdict") == "Unclear"):
                    r.atomic_json(note, rows)  # Preserve new proof gaps across source rounds.
                update(mid, state="sources")
                if r.resolve_source_requests(job, app, worker_context):
                    continue
                update(mid, state="validating")
                if not valid_rows:
                    if int(worker_context.get("output_repair_count", 0)) < 1:
                        worker_context.update(output_repair_count=1, quality_feedback=[
                            "По новому result_file нет корректного результата. Сохрани только назначенный маркер "
                            "по НОВОМУ абсолютному result_file из контекста этого сеанса. Старые или вложенные "
                            "заметки не являются текущим результатом. Сохрани уже собранные доказательства и не "
                            "повторяй исследование только ради сериализации."])
                        continue
                    raise r.IncompleteAnalysisError("Исполнитель не сохранил новый результат своего маркера по указанному пути.")
                row = rows[0]
                if verification:
                    from triage_queue import validate_verification_result
                    current = next(item for item in r.load_decisions(job / "decisions.jsonl")
                                   if str(item.get("marker_id")) == mid)
                    errors = validate_verification_result(row, current)
                    if errors:
                        # A rejected shape is not a missing source or a changed
                        # verdict. Give only this verifier one bounded repair.
                        if int(worker_context.get("verification_repair_count", 0)) < 1:
                            worker_context.update(verification_repair_count=1, quality_feedback=errors)
                            continue
                        raise r.IncompleteAnalysisError("Не пройдена независимая проверка: " + "; ".join(errors))
                    r.atomic_json(note, rows)
                    update(mid, state="finished")
                    break
                if row.get("analysis_status") == "needs_context" or row.get("verdict") == "Unclear":
                    feedback = incomplete_review_feedback(worker_context, row)
                    if feedback:
                        worker_context.update(incomplete_review_count=1, quality_feedback=feedback)
                        update(mid, state="validating")
                        continue
                    update(mid, state="incomplete")
                    break
                errors = r.review_result(job, worker_context, row) if context.get("review_contract_version") == 1 else []
                if context.get("review_contract_version") == 1:
                    from triage_queue import validate_worker_result
                    # Assignment markers are a display subset without the queue's
                    # schema_version. Validate against the authoritative decision.
                    current = next((item for item in r.load_decisions(job / "decisions.jsonl")
                                    if str(item.get("marker_id")) == mid), None)
                    if current is None:
                        raise r.IncompleteAnalysisError("Назначенный маркер отсутствует в локальной очереди.")
                    errors.extend(validate_worker_result(row, current))
                if errors:
                    if int(worker_context.get("worker_quality_repair_count", 0)) >= 1:
                        raise r.IncompleteAnalysisError("Не пройдена проверка результата: " + "; ".join(errors))
                    worker_context.update(worker_quality_repair_count=1, quality_feedback=errors)
                    continue
                r.atomic_json(note, rows)
                update(mid, state="finished")
                break
        except (Exception, SystemExit) as exc:
            # An infrastructure failure must not discard another worker's result.
            code = code or 3
            reason = str(exc) if isinstance(exc, r.IncompleteAnalysisError) else f"Ошибка исполнителя: {type(exc).__name__}"
            try:
                previous_rows = r.read_json(note) if note.exists() else []
            except (OSError, ValueError):
                previous_rows = []
            previous_row = next((row for row in previous_rows if isinstance(row, dict) and row.get("marker_id") == mid), {}) if isinstance(previous_rows, list) else {}
            if latest_row is not None:
                previous_row = latest_row  # Keep this attempt's evidence even when its schema needs repair.
            previous_gaps = previous_row.get("proof_gaps")
            previous_gaps = [gap for gap in previous_gaps if isinstance(gap, str)] if isinstance(previous_gaps, list) else []
            r.atomic_json(note, [{**previous_row, "marker_id": mid, "analysis_status": "needs_context",
                                 "verdict": "Unclear", "execution_error": reason,
                                 "proof_gaps": list(dict.fromkeys([reason, *previous_gaps]))}])
            update(mid, state="incomplete", error=reason, pid=None)
        finally:
            # This also covers an exception immediately after Popen, before the
            # inner supervision loop starts (e.g. a persistent status-write error).
            if process is not None:
                if process.poll() is None:
                    r._terminate_process_tree(process)
                if reader is not None and reader.is_alive():
                    reader.join(timeout=5)
                if reader is None or not reader.is_alive():
                    for pipe in (process.stdin, process.stdout):
                        if pipe is not None and not pipe.closed:
                            pipe.close()
            elapsed = time.monotonic() - clock_start + float(old.get("duration_seconds") or 0)
            update(mid, finished_at=r.now_iso(), duration_seconds=round(elapsed, 3), pid=None,
                   usage_complete=usage_complete and code == 0, exit_code=code)
        return {"context": worker_context, "pid": pid, "exit_code": code}

    with ThreadPoolExecutor(max_workers=len(assignments), thread_name_prefix="triage-marker") as pool:
        results = list(pool.map(worker, assignments))
    # Merge exact source references only after writers are done. No shared model files.
    sources = {row["file_path"]: row for row in context.get("external_sources", [])}
    dependencies = {row["root"]: row for row in context.get("dependency_sources", [])}
    for result in results:
        for row in result["context"].get("external_sources", []):
            sources[row["file_path"]] = row
        for row in result["context"].get("dependency_sources", []):
            dependencies[row["root"]] = row
    context["external_sources"] = list(sources.values())
    context["dependency_sources"] = list(dependencies.values())
    with lock:
        context["worker_measurements"] = {mid: copy.deepcopy(runtime["workers"].get(mid, {})) for mid in ids}
    r.atomic_json(job / context.get("scheduler_context_file", r.BATCH_CONTEXT_FILE), context)
    # Normal finalization applies ready rows and defers incomplete rows separately.
    return 0, 0
