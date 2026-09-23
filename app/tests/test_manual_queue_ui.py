"""Manual retry queue scenarios using temporary jobs and no live services."""
import json
import os
import sys
from concurrent.futures import Future
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_run
import triage_gui_qt as ui
import triage_queue as queue
from triage_dashboard import atomic_json
from triage_gui import current_run_queue_ids, marker_assignments, saved_result_assignments


@pytest.fixture
def manual_window(tmp_path, monkeypatch):
    app = ui.QApplication.instance() or ui.QApplication([])
    job = tmp_path / "RESULTS" / "manual"
    job.mkdir(parents=True)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (job / "notes").mkdir()
    ids = [f"m{i:02}" for i in range(18)]
    markers = [{"id": mid, "warnClass": "NULL", "file": "example.go", "line": i + 1}
               for i, mid in enumerate(ids)]
    atomic_json(job / "job.json", {"manual_selection_only": True, "parallel_workers": 2})
    atomic_json(job / "markers.inventory.json", {
        "markers": markers, "truncated": False, "total_count": len(markers),
        "returned_count": len(markers), "filters_applied": {"advanced_filter": queue.GOST_FILTER},
    })
    (job / "decisions.jsonl").write_text("".join(json.dumps({
        "marker_id": marker["id"], "verdict": None,
        **{key: marker[key] for key in ("warnClass", "file", "line")},
    }) + "\n" for marker in markers), encoding="utf-8")
    atomic_json(job / "notes" / "batch-001-worker-1.json", [
        {"marker_id": mid, "verdict": "False Positive", "comment": "Saved draft"}
        for mid in ids])
    atomic_json(job / "incomplete-analysis.json", {mid: {"reason": "needs context"} for mid in ids[7:]})
    run = {"active": False, "status": "failed", "usage": {}}
    monkeypatch.setattr(ui, "read_run_record", lambda _job: dict(run))
    monkeypatch.setattr(codex_run, "read_run_record", lambda _job: dict(run))
    monkeypatch.setattr(ui, "read_codex_rate_limits", lambda: {})
    monkeypatch.setattr(ui, "read_codex_models", lambda: [])
    monkeypatch.setattr(ui.TriageQtWindow, "check_connection", lambda _self: None)
    monkeypatch.setattr(ui, "launch_runner", lambda *_args: pytest.fail("No live analysis"))
    window = ui.TriageQtWindow(job, app_dir)
    window.timer.stop()
    try:
        window.show()
        app.processEvents()
        yield window, job, run, ids, app
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, ui.QEvent.Type.DeferredDelete)
        app.processEvents()


def table_ids(widget):
    return [widget.item(row, 0).data(ui.Qt.ItemDataRole.UserRole) for row in range(widget.rowCount())]


def test_model_and_parallel_capacity_are_visible_next_to_start(manual_window, tmp_path):
    window, job, run, ids, app = manual_window
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        ui.QFontDatabase.addApplicationFont(str(font))
    window.setStyleSheet(ui.STYLE)
    window.resize(1500, 920)
    window.job_data.update(codex_model="gpt-6-luna", parallel_workers=5)
    window.state["codex_run"] = {"active": False}
    window.update_action_states()
    app.processEvents()
    text = window.analysis_configuration.text()
    assert text == "Модель: gpt-6-luna · Параллельно: до 5 агентов"
    assert window.analysis_configuration.geometry().left() > window.analysis_button.geometry().right()
    assert window.analysis_configuration.isVisible()
    assert window.analysis_button.parentWidget().grab().save(str(tmp_path / "analysis-header.png"))
    window.state.update(codex_run={"active": True, "requested_model": "gpt-6-luna", "parallel_workers": 5},
                        worker_runtime={"workers": {"m00": {"worker": 1, "state": "running"}}})
    window.update_action_states()
    assert "занято: 1/5" in window.analysis_configuration.text()


@pytest.mark.parametrize("finished_index", [0, 1])
def test_saved_parallel_marker_stays_visible_until_batch_finishes(manual_window, finished_index):
    window, job, run, ids, app = manual_window
    run.update(active=True, status="running", phase="analysis", launch_id="parallel")
    atomic_json(job / "codex-run.json", run)
    atomic_json(job / "control.json", {"priority_marker_ids": ids[:3], "manual_queue_requested": True})
    atomic_json(job / "workers.status.json", {"state": "assigned", "batch": 1, "workers": [
        {"worker": i + 1, "status": "assigned", "marker_ids": [mid]} for i, mid in enumerate(ids[:2])]})
    # Start with two actual processes, then save only one of their results.
    atomic_json(job / "notes" / "batch-001-worker-1.json", [])
    runtime = {"launch_id": "parallel", "batch": 1, "workers": {
        mid: {"worker": i + 1, "state": "running", "pid": 123 + i}
        for i, mid in enumerate(ids[:2])}}
    atomic_json(job / "workers-runtime.json", runtime)
    window.refresh()
    window.active_table.selectRow(finished_index)
    finished_id = ids[finished_index]
    assert window.current_live_id == finished_id
    before = {name: (job / name).read_bytes() for name in ("control.json", "decisions.jsonl")}

    def finish(index):
        atomic_json(job / "notes" / f"batch-001-worker-{index + 1}.json", [{
            "marker_id": ids[index], "verdict": "False Positive", "comment": "Saved proof <safe>"}])
        runtime["workers"][ids[index]].update(
            state="finished", pid=None, finished_at="2026-09-23T13:54:13+03:00", duration_seconds=170.9)
        atomic_json(job / "workers-runtime.json", runtime)

    finish(finished_index)
    for _ in range(3):
        window.refresh()
        app.processEvents()
        assert table_ids(window.active_table) == ids[:2]
        assert table_ids(window.queue_table) == ids[2:3]  # saved is NOT runnable again
        assert "в работе: 1" in window.active_title.text()
        assert "результат сохранён: 1" in window.active_title.text()
        assert window.active_table.item(finished_index, 1).text() == "Результат сохранён"
        assert "повторный запуск не требуется" in window.active_table.item(finished_index, 1).toolTip()
        assert window.current_live_id == finished_id
        assert "Результат сохранён" in window.live_meta.text()
        assert "False Positive" in window.live_meta.text()
        assert "завершается запись итогов" in window.live_meta.text()
        assert "Saved proof <safe>" in window.live_text.toPlainText()
        assert "2 мин 51 с" in window.live_meta.text()
        assert window.marker_queue_states()[finished_id][0] == "Результат сохранён"
        assert window.marker_table.item(finished_index, 1).text() == "Результат сохранён"
        assert not window.remove_queue_button.isEnabled()
        assert len(marker_assignments(window.state)) == 1  # actual process count remains truthful
        assert all((job / name).read_bytes() == value for name, value in before.items())

    # Both workers may finish before the parent applies the batch. Neither vanishes.
    finish(1 - finished_index)
    window.refresh()
    assert table_ids(window.active_table) == ids[:2]
    assert "в работе: 0" in window.active_title.text()
    assert "результат сохранён: 2" in window.active_title.text()
    assert table_ids(window.queue_table) == ids[2:3]
    assert marker_assignments(window.state) == {}

    # Simulate parent finalization; stale runtime must not resurrect completed rows.
    decisions = [json.loads(line) for line in (job / "decisions.jsonl").read_text().splitlines()]
    for decision in decisions[:2]:
        decision.update(verdict="False Positive", comment="Saved proof <safe>")
    (job / "decisions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in decisions), encoding="utf-8")
    run.update(active=False, status="completed", phase="completed")
    window.refresh()
    assert table_ids(window.active_table) == []
    assert table_ids(window.queue_table) == ids[2:3]
    assert saved_result_assignments(window.state) == {}
    assert all(window.decision_by_id[mid]["verdict"] == "False Positive" for mid in ids[:2])


@pytest.mark.parametrize("phase", ["starting", "sources", "validating", "incomplete"])
def test_saved_note_does_not_hide_worker_before_terminal_state(phase):
    state = {"codex_run": {"active": True, "phase": "analysis"},
             "priority_marker_ids": ["m"], "manual_queue_requested": True,
             "workers": {1: {"marker_ids": ["m"], "current_saved_marker_ids": ["m"]}},
             "worker_runtime": {"workers": {"m": {"state": phase}}}}
    assert saved_result_assignments(state) == {}
    assert current_run_queue_ids([{"marker_id": "m", "verdict": None}], state, {"m"}, ["m"]) == ["m"]


def test_saved_assignments_ignore_other_runs_batches_and_unassigned_drafts():
    state = {"codex_run": {"active": True, "phase": "analysis", "launch_id": "new"},
             "queue": {"batch": 2},
             "workers": {1: {"marker_ids": ["m"], "current_saved_marker_ids": ["m", "old"]}},
             "worker_runtime": {"launch_id": "old", "batch": 2,
                                "workers": {"m": {"state": "finished"}}}}
    assert saved_result_assignments(state) == {}
    state["worker_runtime"].update(launch_id="new", batch=1)
    assert saved_result_assignments(state) == {}
    state["worker_runtime"]["batch"] = 2
    assert saved_result_assignments(state) == {"m": "Агент 1"}
    for phase in ("launching", "repository", "verification"):
        state["codex_run"]["phase"] = phase
        assert saved_result_assignments(state) == {}


def test_saved_legacy_and_verifier_assignments_do_not_count_as_running():
    state = {"codex_run": {"active": True, "phase": "analysis"},
             "workers": {1: {"marker_ids": ["m", "n"], "current_saved_marker_ids": ["m", "old"]}}}
    assert saved_result_assignments(state) == {"m": "Агент 1"}
    assert marker_assignments(state) == {"n": "Агент 1"}
    state["verifiers"] = {1: {"marker_ids": ["v"], "current_saved_marker_ids": ["v"]}}
    state["codex_run"]["phase"] = "verification"
    assert saved_result_assignments(state) == {"v": "Проверяющий 1"}


def test_finished_incomplete_workers_are_not_labelled_waiting_for_busy_peer(manual_window):
    window, job, run, ids, app = manual_window
    run.update(active=True, status="running", phase="analysis", launch_id="parallel")
    atomic_json(job / "codex-run.json", run)
    atomic_json(job / "control.json", {"priority_marker_ids": ids[:3], "manual_queue_requested": True})
    atomic_json(job / "workers.status.json", {"state": "assigned", "batch": 1, "workers": [
        {"worker": i + 1, "status": "assigned", "marker_ids": [mid]} for i, mid in enumerate(ids[:3])]})
    atomic_json(job / "notes" / "batch-001-worker-1.json", [
        {"marker_id": mid, "analysis_status": "needs_context", "verdict": "Unclear"} for mid in ids[:3]])
    atomic_json(job / "workers-runtime.json", {"launch_id": "parallel", "batch": 1, "workers": {
        ids[0]: {"worker": 1, "state": "running", "pid": 123},
        ids[1]: {"worker": 2, "state": "incomplete", "pid": None, "error": "Repeated source request"},
        ids[2]: {"worker": 3, "state": "incomplete", "pid": None},
    }})
    before = (job / "control.json").read_bytes()
    window.refresh()
    app.processEvents()
    assert table_ids(window.active_table) == ids[:1]
    assert table_ids(window.queue_table) == ids[1:3]
    assert [window.queue_table.item(i, 1).text() for i in range(2)] == ["Доисследовать"] * 2
    assert "Repeated source request" in window.queue_table.item(0, 1).toolTip()
    assert "доисследовать: 2" in window.queue_title.text()
    assert window.marker_queue_states()[ids[1]][0] == "Доисследовать"
    assert (job / "control.json").read_bytes() == before


@pytest.mark.parametrize(("phase", "label"), [("starting", "Запуск"), ("sources", "Исходники"), ("validating", "Проверка")])
def test_worker_source_and_validation_phases_are_not_new_queue_assignments(phase, label):
    state = {"codex_run": {"active": True}, "worker_runtime": {"workers": {"m": {"state": phase}}}}
    assert ui.queued_marker_status(state, "m")[0] == label


def test_live_panel_has_independent_feed_and_follows_latest_message(manual_window):
    window, job, run, ids, app = manual_window
    run.update(active=True, phase="analysis", status="running")
    paths = []
    for index, mid in enumerate(ids[:2]):
        path = f"worker-{index}.jsonl"
        paths.append(path)
        (job / path).write_text(json.dumps({"type": "item.completed", "timestamp": "2026-09-22T10:00:00Z",
                                           "item": {"type": "agent_message", "text": f"UNIQUE-{mid} " + "Evidence. " * 100}}) + "\n")
    window.state["codex_run"] = run
    window.state["worker_runtime"] = {"workers": {
        mid: {"worker": index + 1, "pid": 100 + index, "state": "running", "event_log": paths[index],
              "started_at": "2026-09-22T10:00:00Z", "last_event_at": "2026-09-22T10:00:00Z"}
        for index, mid in enumerate(ids[:2])}}
    for mid in ids[:2]:
        window.current_live_id = mid
        window.render_live()
        app.processEvents()
        assert f"UNIQUE-{mid}" in window.live_text.toPlainText()
        assert f"UNIQUE-{ids[1] if mid == ids[0] else ids[0]}" not in window.live_text.toPlainText()
        assert "Индивидуальный поток" in window.live_text.toPlainText()
    scrollbar = window.live_text.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    with (job / paths[1]).open("a") as output:
        output.write(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "NEWEST-MESSAGE"}}) + "\n")
    window.render_live()
    app.processEvents()
    assert "NEWEST-MESSAGE" in window.live_text.toPlainText()
    assert scrollbar.value() == scrollbar.maximum()


def test_deferred_markers_remain_visible_with_actionable_status(manual_window):
    window, job, run, ids, app = manual_window
    atomic_json(job / "control.json", {
        "priority_marker_ids": ids[7:9], "deferred_marker_ids": ids[7:9],
        "manual_queue_requested": True, "pause_requested": True,
    })
    window.refresh()
    app.processEvents()
    assert table_ids(window.queue_table) == ids[7:9]
    assert [window.queue_table.item(i, 1).text() for i in range(2)] == ["Доисследовать"] * 2
    assert all(window.marker_queue_states()[mid][0] == "Доисследовать" for mid in ids[7:9])
    window.current_marker_id = ids[7]
    window.render_marker()
    window.update_action_states()
    assert window.approve_button.isHidden()  # no uncertainty bypass


@pytest.mark.parametrize("accepted", [True, False])
def test_inline_login_updates_connection_without_launching_analysis(manual_window, monkeypatch, accepted):
    window, job, run, ids, app = manual_window
    original = (job / "decisions.jsonl").read_bytes()
    calls = []
    class Dialog:
        def __init__(self, parent, app_directory, server_url, mcp_url, token, **kwargs):
            assert parent is window
            self.server_url = server_url
            calls.append("dialog")
        def exec(self):
            return ui.QDialog.DialogCode.Accepted if accepted else ui.QDialog.DialogCode.Rejected
        def shutdown(self):
            calls.append("shutdown")
        def deleteLater(self):
            pass
    monkeypatch.setattr(ui, "SvacerLoginDialog", Dialog)
    window.toggle_connection()
    assert window.connected == accepted
    assert window.login_dialog is None
    assert calls == ["dialog", "shutdown"]
    assert (job / "decisions.jsonl").read_bytes() == original
    if accepted:
        assert window.connection_button.text() == "Выйти из Svacer"


def test_first_failed_probe_opens_login_once_without_new_project(manual_window, monkeypatch):
    window, job, _run, _ids, app = manual_window
    calls = []

    class Dialog:
        server_url = "https://svacer.example.test"

        def __init__(self, *_args, **_kwargs):
            calls.append("open")

        def exec(self):
            return ui.QDialog.DialogCode.Rejected

        def shutdown(self):
            pass

        def deleteLater(self):
            pass

    monkeypatch.setattr(ui, "SvacerLoginDialog", Dialog)
    future = Future()
    future.set_result("MCP не запущен")
    window._connection_future = future
    window.drain_connection()
    app.processEvents()
    assert calls == ["open"]
    assert window._initial_connection_prompted
    assert job.exists()  # Login startup must not replace/open a project wizard.

    future = Future()
    future.set_result("MCP не запущен")
    window._connection_future = future
    window.drain_connection()
    app.processEvents()
    assert calls == ["open"]


def select_and_enqueue(window, rows):
    window.marker_table.clearSelection()
    for row in rows:
        window.marker_table.selectionModel().select(
            window.marker_table.model().index(row, 0),
            ui.QItemSelectionModel.SelectionFlag.Select | ui.QItemSelectionModel.SelectionFlag.Rows,
        )
    window.add_selected_to_queue()


@pytest.mark.parametrize("workers", [1, 2])
def test_fifteen_explicit_draft_retries_survive_prepare_running_failure_and_stop(manual_window, monkeypatch, workers):
    window, job, run, ids, app = manual_window
    assert window.queue_table.rowCount() == 0  # Unselected drafts stay out.
    select_and_enqueue(window, range(15))
    assert table_ids(window.queue_table) == ids[:15], window.status.text()
    assert window.state["manual_queue_requested"] is True
    run.update(active=True, status="preparing", phase="repository")
    window.refresh()
    assert table_ids(window.queue_table) == ids[:15]
    assert window.active_table.rowCount() == 0
    atomic_json(job / "workers.status.json", {
        "state": "assigned", "batch": 2,
        "workers": [{"worker": n + 1, "status": "assigned", "marker_ids": [ids[n]], "assigned": 1}
                    for n in range(workers)],
    })
    run.update(status="running", phase="analysis")
    window.refresh()
    assert table_ids(window.active_table) == ids[:workers]
    assert table_ids(window.queue_table) == ids[workers:15]
    for status in ("failed", "stopped", "incomplete"):
        run.update(active=False, status=status, phase=status)
        window.refresh()
        assert window.active_table.rowCount() == 0
        assert table_ids(window.queue_table) == ids[:15]
        assert window.analysis_button.text() == "Начать анализ"
    # Resuming starts with repository preparation, not with stale workers.
    run.update(active=True, status="preparing", phase="repository")
    window.refresh()
    assert window.active_table.rowCount() == 0
    assert table_ids(window.queue_table) == ids[:15]
    run.update(active=False, status="stopped", phase="stopped")
    window.refresh()
    for tab in range(4):
        window.tabs.setCurrentIndex(tab)
        app.processEvents()
        window.refresh()
        assert table_ids(window.queue_table) == ids[:15]
    # Duplicate additions do not duplicate rows; adding/removing remains usable.
    select_and_enqueue(window, [0, 15])
    assert table_ids(window.queue_table) == ids[:16], (window.status.text(), window.selected_marker_ids())
    before_notes = (job / "notes" / "batch-001-worker-1.json").read_bytes()
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_: ui.QMessageBox.StandardButton.Yes)
    window.queue_table.selectRow(0)
    window.remove_selected_from_queue()
    assert table_ids(window.queue_table) == ids[1:16]
    assert (job / "notes" / "batch-001-worker-1.json").read_bytes() == before_notes


def test_current_job_row_cannot_be_overwritten_by_stale_background_scan(manual_window):
    window, job, run, ids, _app = manual_window
    select_and_enqueue(window, [0, 1])
    stale = Future()
    stale.set_result(([("old title", "Работает", "0/18", "0/18", 2)], [str(job.resolve())]))
    window._jobs_future = stale
    run.update(active=False, status="failed")
    window.refresh()
    window.drain_jobs()
    assert window.jobs_table.item(0, 1).text() == "Ошибка"
    assert window.jobs_table.item(0, 4).text() == "0"
    run.update(active=True, status="preparing", phase="repository")
    window.refresh()
    assert window.jobs_table.item(0, 1).text() == "Работает"
    assert window.jobs_table.item(0, 4).text() == "0"


def test_overview_error_line_shows_when_the_failure_happened(manual_window):
    window, _job, run, _ids, _app = manual_window
    run.update(
        active=False,
        status="failed",
        finished_at="2026-09-21T13:35:41",
        phase_detail="fatal: couldn't find remote ref 2.11.2",
    )
    window.refresh()
    text = window.run_status.text()
    assert "время ошибки: 21.09.2026 13:35:41" in text
    assert "fatal: couldn't find remote ref 2.11.2" in text

    run.update(active=True, status="running", phase_detail="Анализ маркера")
    window.refresh()
    assert "время ошибки" not in window.run_status.text()


@pytest.mark.parametrize("phase,expected", [
    ("launching", "Запуск подготовки"),
    ("repository", "Подготовка исходников"),
    ("batch", "Назначение маркеров"),
    ("traces", "Загрузка трасс"),
    ("sources", "Проверка исходников"),
])
def test_preparation_phase_has_clear_label(phase, expected):
    assert expected in ui.preparation_indicator_text({"active": True, "phase": phase})
    assert ui.preparation_indicator_text({"active": False, "phase": phase}) == ""


def test_overview_animates_indeterminate_progress_during_preparation(manual_window):
    window, _job, run, _ids, app = manual_window
    window.preparation_movie.setFileName(
        str(Path(ui.__file__).resolve().parent / "assets" / "svacer-hamster-loading.gif")
    )
    run.update(
        active=True,
        status="preparing",
        phase="repository",
        phase_detail="Подготавливаю исходники refs/tags/v2.11.15",
    )
    window.refresh()
    app.processEvents()
    assert window.preparation_indicator.isVisible()
    assert window.preparation_animation.isVisible()
    assert window.preparation_movie.isValid()
    assert window.preparation_movie.state() == ui.QMovie.MovieState.Running
    assert "Подготовка исходников" in window.preparation_indicator.text()
    assert window.preparation_indicator.toolTip() == run["phase_detail"]
    assert not window.preparation_timer.isActive()
    assert window.progress.minimum() == window.progress.maximum() == 0
    assert "Фоновая задача: Подготовка" in window.run_status.text()

    run.update(active=True, status="running", phase="analysis", phase_detail="Анализ маркера")
    window.refresh()
    assert window.preparation_indicator.isHidden()
    assert window.preparation_animation.isHidden()
    assert window.preparation_movie.state() == ui.QMovie.MovieState.NotRunning
    assert not window.preparation_timer.isActive()
    assert window.progress.minimum() == 0 and window.progress.maximum() == len(_ids)


@pytest.mark.parametrize("flag", ["manual_queue_requested", "single_marker_requested"])
def test_explicit_retry_is_not_hidden_by_saved_draft(flag):
    decisions = [{"marker_id": "draft", "verdict": None}, {"marker_id": "unselected", "verdict": None}]
    state = {"codex_run": {"active": False}, "priority_marker_ids": ["draft"], flag: True}
    assert current_run_queue_ids(decisions, state, {"draft", "unselected"}, ["draft", "unselected"]) == ["draft"]
    state[flag] = False
    assert current_run_queue_ids(decisions, state, {"draft", "unselected"}, ["draft", "unselected"]) == []


def test_waiting_manual_markers_stay_visible_during_verification():
    state = {"codex_run": {"active": True, "phase": "verification"},
             "priority_marker_ids": ["waiting"], "manual_queue_requested": True,
             "verifiers": {1: {"current_status": "assigned", "marker_ids": ["confirmed"]}}}
    assert current_run_queue_ids([{"marker_id": "waiting", "verdict": None}], state,
                                 {"waiting"}, ["waiting", "confirmed"]) == ["waiting"]


@pytest.mark.parametrize("workers", [1, 2])
def test_marker_queue_column_tracks_live_state_without_marker_file_changes(manual_window, workers):
    window, job, run, ids, _ = manual_window
    assert window.marker_table.horizontalHeaderItem(1).text() == "Очередь"
    assert all(window.marker_table.item(row, 1).text() == "Не в очереди" for row in range(18))
    assert window.marker_table.item(0, 1).foreground().color() == ui.QColor(ui.COLORS["muted"])
    select_and_enqueue(window, [0, 1, 2])
    assert window.selected_marker_ids() == ids[:3]
    assert not window.add_queue_button.isEnabled()
    assert window.add_queue_button.text() == "Уже в очереди"
    assert window.marker_table.item(0, 0).text() == "Черновик"
    assert [window.marker_table.item(row, 1).text() for row in range(4)] == ["В очереди"] * 3 + ["Не в очереди"]
    assert window.marker_table.item(1, 1).toolTip() == "В очереди · позиция 2 из 3"
    assert window.marker_table.item(1, 1).foreground().color() == ui.QColor(ui.COLORS["blue"])
    marker_signature = window.marker_signature
    atomic_json(job / "workers.status.json", {
        "state": "assigned", "batch": 2,
        "workers": [{"worker": n + 1, "status": "assigned", "marker_ids": [ids[n]], "assigned": 1}
                    for n in range(workers)],
    })
    run.update(active=True, status="running", phase="analysis")
    window.refresh()
    assert window.marker_signature == marker_signature
    assert window.selected_marker_ids() == ids[:3]
    assert [window.marker_table.item(row, 1).text() for row in range(workers)] == ["В работе"] * workers
    assert window.marker_table.item(0, 1).foreground().color() == ui.QColor(ui.COLORS["green"])
    assert window.marker_table.item(workers, 1).toolTip() == f"В очереди · позиция 1 из {3 - workers}"
    assert table_ids(window.active_table) == ids[:workers]
    assert table_ids(window.queue_table) == ids[workers:3]
    window.current_marker_id = ids[0]
    window.render_marker()
    assert "В работе · Агент 1" in window.marker_detail.toPlainText()
    window.marker_table.clearSelection()
    window.marker_table.selectRow(0)
    assert window.triage_one_button.text() == "В работе"
    for status in ("failed", "stopped"):
        run.update(active=False, status=status, phase=status)
        window.refresh()
        assert window.marker_signature == marker_signature
        assert [window.marker_table.item(row, 1).text() for row in range(3)] == ["В очереди"] * 3
        assert window.triage_one_button.text() == "В очереди"


def test_queue_indicator_removal_and_unchanged_refresh_are_incremental(manual_window, monkeypatch):
    window, job, _, ids, _ = manual_window
    select_and_enqueue(window, [0, 1])
    queue.dequeue_marker_ids(job / "decisions.jsonl", [ids[0]])
    window.refresh()
    assert window.marker_table.item(0, 1).text() == "Не в очереди"
    assert window.marker_table.item(1, 1).text() == "В очереди"
    assert window.marker_table.item(1, 1).toolTip() == "В очереди · позиция 1 из 1"
    assert window.add_queue_button.isEnabled()
    assert window.add_queue_button.text() == "В очередь (1)"
    window.current_marker_id = ids[1]
    window.render_marker()
    assert "В очереди · позиция 1 из 1" in window.marker_detail.toPlainText()
    original = ui.set_rows
    def no_marker_rebuild(widget, *args, **kwargs):
        assert widget is not window.marker_table, "Unchanged marker table must not be repainted"
        return original(widget, *args, **kwargs)
    monkeypatch.setattr(ui, "set_rows", no_marker_rebuild)
    window.refresh()
    window.refresh()
