"""Concurrent desktop interactions with blocked fake PoCs; no live model/network."""
from __future__ import annotations

import shutil
import time
from threading import Event, Lock

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest

from test_issue_workspace import make_window, write_rows


def spin(window, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
        window.artifact_tasks.drain()
        QTest.qWait(10)
    assert predicate()


def inventory(window, rows):
    import triage_queue as q
    q.atomic_write_json(window.job / "markers.inventory.json", {
        "markers": [{"id": row["marker_id"], "review": "Undecided", **{key: row[key] for key in ("warnClass", "file", "line")}} for row in rows],
        "truncated": False, "total_count": len(rows), "returned_count": len(rows),
        "filters_applied": {"advanced_filter": q.GOST_FILTER},
    })
    write_rows(window.job, rows)
    window.refresh()


def test_card_poc_does_not_block_analysis_or_project_switch(tmp_path, monkeypatch):
    import triage_gui_qt as ui
    import triage_queue as q
    app, window, row = make_window(tmp_path, monkeypatch)
    started, release = Event(), Event()
    original_job = window.job
    other = original_job.parent / "job-2"
    try:
        pending = {**row, "marker_id": "pending-2", "line": 43, "verdict": None}
        inventory(window, [row, pending])
        (window.job / "START_PROMPT.txt").write_text("offline prompt")
        def fake_poc(job, marker_id, **kwargs):
            started.set()
            assert release.wait(5)
            return {"status": "needs_evidence", "directory": str(job / "test-poc"),
                    "missing_evidence": ["Need a caller"], "summary": "not run"}
        monkeypatch.setattr(ui, "generate_for_marker", fake_poc)
        monkeypatch.setattr(ui.QMessageBox, "question", lambda *a: ui.QMessageBox.StandardButton.Yes)
        launched = []
        monkeypatch.setattr(ui, "launch_runner", lambda job, *a, **k: launched.append(job) or {"runner_pid": 123})
        window.show()
        window.tabs.setCurrentWidget(window.markers_tab)
        window.select_marker_row(row["marker_id"])
        window.generate_or_open_poc()
        assert started.wait(1)
        assert not window.busy and window._task_future is None
        assert window.artifact_status.isVisible()
        assert "PoC" in window.artifact_status.text()
        assert not window.poc_button.isEnabled()
        assert window.issues_tab.add_all.isEnabled()
        assert window.issues_tab.confirmed_table.item(0, 4).text() == "Готовится"
        window.select_marker_row("pending-2")
        assert window.add_queue_button.isEnabled()
        window.add_selected_to_queue()
        assert q.priority_marker_ids(window.job / "decisions.jsonl") == ["pending-2"]
        assert window.analysis_button.isEnabled()
        window.analysis_action()
        assert launched == [original_job]
        shutil.copytree(original_job, other)
        window.load_job_options()
        index = window.job_paths.index(other.resolve())
        window.job_combo.setCurrentIndex(index)
        assert window.job == other.resolve()
        assert window.delete_job_button.isEnabled() and window.source_button.isEnabled()
        assert window.artifact_tasks.for_job(original_job)
        release.set()
        spin(window, lambda: not window.artifact_tasks.active)
        assert window.job == other.resolve()
        assert not window.busy
    finally:
        release.set()
        spin(window, lambda: not window.artifact_tasks.active)
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_issue_slots_run_in_parallel_and_new_items_wait_for_a_new_batch(tmp_path, monkeypatch):
    app, window, row = make_window(tmp_path, monkeypatch)
    tab = window.issues_tab
    gate = Event()
    entered = {f"marker-{i}": Event() for i in range(1, 5)}
    lock = Lock()
    active = maximum = 0
    prepared = []
    original_prepare = tab.store.prepare
    try:
        rows = [{**row, "marker_id": f"marker-{i}"} for i in range(1, 4)]
        write_rows(window.job, rows)
        tab.refresh(force=True)
        tab.workers.setValue(2)
        tab.enqueue(True)
        def prepare(item, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                prepared.append(item["marker_id"])
            entered[item["marker_id"]].set()
            assert gate.wait(5)
            result = original_prepare(item, **kwargs)
            with lock:
                active -= 1
            return result
        monkeypatch.setattr(tab.store, "prepare", prepare)
        tab.start_queue()
        assert entered["marker-1"].wait(1) and entered["marker-2"].wait(1)
        assert not entered["marker-3"].is_set()
        assert not window.busy and window.job_combo.isEnabled()
        assert len(tab._active_ids) == 2
        tab.workers.setValue(3)
        assert entered["marker-3"].wait(1)
        assert maximum == 3 and tab.store.workers() == 3
        write_rows(window.job, [*rows, {**row, "marker_id": "marker-4"}])
        tab.refresh(force=True)
        assert tab.add_all.isEnabled()
        tab.enqueue(True)
        gate.set()
        spin(window, lambda: not tab.running)
        queue = tab.store.load_queue()
        assert [item["status"] for item in queue] == ["draft", "draft", "draft", "queued"]
        assert set(prepared) == {"marker-1", "marker-2", "marker-3"}
        assert not entered["marker-4"].is_set()
    finally:
        gate.set()
        spin(window, lambda: not tab.running and not window.artifact_tasks.active)
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_issue_queue_waits_for_same_marker_but_prepares_other_markers(tmp_path, monkeypatch):
    app, window, row = make_window(tmp_path, monkeypatch)
    tab = window.issues_tab
    gate, started = Event(), Event()
    try:
        write_rows(window.job, [row, {**row, "marker_id": "marker-2"}])
        tab.refresh(force=True)
        def card_work():
            started.set()
            assert gate.wait(5)
            return {}
        assert window.run_artifact(window.job, row["marker_id"], card_work, lambda result: None, "fake PoC", kind="PoC")
        assert started.wait(1)
        assert not window.run_artifact(window.job, row["marker_id"], lambda: None, lambda result: None, "duplicate", kind="PoC")
        tab.enqueue(True)
        tab.start_queue()
        spin(window, lambda: tab.store.load_queue()[1]["status"] == "draft")
        assert tab.store.load_queue()[0]["status"] == "queued"
        assert tab.running and not window.busy
        gate.set()
        spin(window, lambda: not tab.running)
        assert [item["status"] for item in tab.store.load_queue()] == ["draft", "draft"]
    finally:
        gate.set()
        spin(window, lambda: not tab.running and not window.artifact_tasks.active)
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
