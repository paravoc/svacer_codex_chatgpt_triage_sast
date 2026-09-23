"""Smoke checks for the Qt desktop view without starting analysis or networking."""

from __future__ import annotations

import json
import os
import sys
from threading import Event
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPointF, QTimer  # noqa: E402
from PySide6.QtGui import QEnterEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QPlainTextEdit, QPushButton, QWidget  # noqa: E402

import triage_gui_qt as qt_gui  # noqa: E402
import triage_queue as queue  # noqa: E402
from triage_gui_qt import TriageQtWindow  # noqa: E402


def test_send_ready_enabled_for_partial_job_while_other_markers_run(tmp_path, monkeypatch):
    from test_partial_import import fixture
    fake, job, _, _ = fixture(tmp_path)
    app = QApplication.instance() or QApplication([])
    app_directory = tmp_path / "app"
    app_directory.mkdir()
    monkeypatch.setattr(qt_gui, "read_codex_rate_limits", lambda: {})
    monkeypatch.setattr(qt_gui, "read_codex_models", lambda: [])
    monkeypatch.setattr(qt_gui, "check_mcp", lambda *_args: "недоступен")
    window = TriageQtWindow(job, app_directory)
    try:
        window.state.update(codex_run={"active": True}, priority_marker_ids=["m2", "m3"])
        window.update_action_states()
        assert window.state["completed"] == 1 and window.state["total"] == 4
        assert window.send_button.isEnabled()
        assert window.send_button.text() == "Отправить готовые (1)"
        window.state["import_blocked"] = True
        window.update_action_states()
        assert not window.send_button.isEnabled()
        window.state.update(import_blocked=False, import_ready=0)
        window.update_action_states()
        assert not window.send_button.isEnabled()
        assert not fake.sent
    finally:
        window.close()
        app.processEvents()


def test_ambient_header_animates_without_moving_layout_and_can_be_disabled(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(600, 90)
    backdrop = qt_gui.AmbientBackdrop(parent)
    backdrop.setGeometry(9, 5, 582, 60)
    parent.show()
    try:
        app.processEvents()
        before = backdrop.geometry()
        QTest.qWait(210)
        assert backdrop.phase > 0
        assert backdrop.geometry() == before
        assert not backdrop.grab().isNull()
    finally:
        parent.close()
    monkeypatch.setenv("SVACER_REDUCE_MOTION", "1")
    still_parent = QWidget()
    still = qt_gui.AmbientBackdrop(still_parent)
    try:
        assert not still.timer.isActive()
    finally:
        still_parent.close()


def test_status_cells_are_colored_and_update_with_verdict() -> None:
    QApplication.instance() or QApplication([])
    statuses = ["Confirmed", "False Positive", "Won't fix", "Unclear", "Черновик", "Ошибка"]
    widget = qt_gui.table(["Статус"])
    qt_gui.set_rows(widget, [(status,) for status in statuses])
    qt_gui.color_status_cells(widget, statuses)
    expected = ("red", "green", "amber", "violet", "blue", "red")
    for row, tone in enumerate(expected):
        item = widget.item(row, 0)
        assert item.foreground().color() == qt_gui.QColor(qt_gui.COLORS[tone])
        assert item.background().color().alpha() == 42
        assert item.font().bold()
    qt_gui.set_rows(widget, [("Ожидает",), *((status,) for status in statuses[1:])])
    qt_gui.color_status_cells(widget, ["Ожидает", *statuses[1:]])
    assert widget.item(0, 0).foreground().color() == qt_gui.QColor(qt_gui.COLORS["muted"])
    widget.close()


def test_buttons_respond_to_hover_without_layout_changes() -> None:
    QApplication.instance() or QApplication([])
    widget = qt_gui.button("Проверить", tone="success")
    original_size = widget.sizeHint()
    entered = QEnterEvent(QPointF(2, 2), QPointF(2, 2), QPointF(2, 2))
    widget.enterEvent(entered)
    assert widget._hover_animation.endValue().alpha() == 125
    QTest.qWait(180)
    assert abs(widget.graphicsEffect().color().alpha() - 125) <= 1
    assert widget.sizeHint() == original_size
    widget.setEnabled(False)
    assert widget.graphicsEffect().color().alpha() == 0
    assert not widget.graphicsEffect().isEnabled()
    widget.setEnabled(True)
    qt_gui.set_button_tone(widget, "danger")
    assert widget.graphicsEffect().color().red() == qt_gui.QColor(qt_gui.COLORS["red"]).red()
    widget.close()


def test_codex_limit_uses_weekly_window_and_handles_missing_data() -> None:
    text, color = qt_gui.format_codex_limit({
        "rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 20, "windowDurationMins": 300},
            "secondary": {"usedPercent": 51, "windowDurationMins": 10080},
        }},
    })
    assert "Доступно Codex: 49%" in text
    assert "недельный лимит" in text
    assert color == qt_gui.COLORS["green"]
    assert "недоступны" in qt_gui.format_codex_limit({"rateLimits": {"primary": None}})[0]
    assert "недоступны" in qt_gui.format_codex_limit({
        "rateLimits": {"primary": {"usedPercent": float("nan")}},
    })[0]


def test_job_list_does_not_show_stale_worker_assignments(tmp_path: Path, monkeypatch) -> None:
    job = tmp_path / "20260918-185413-test"
    job.mkdir()
    monkeypatch.setattr(qt_gui, "job_identity", lambda _path: ({}, "Gateway"))
    monkeypatch.setattr(qt_gui, "collect_state", lambda _path: {
        "inventory_total": 10, "already_reviewed": 7,
        "completed": 1, "total": 10,
        "workers": {1: {"marker_ids": ["m2"], "current_status": "assigned"}},
    })
    monkeypatch.setattr(qt_gui, "read_run_record", lambda _path: {"active": False, "status": "failed"})
    rows, _keys = qt_gui.scan_job_rows([job])
    assert rows[0][2] == "7/10"
    assert rows[0][3] == "1/10"
    assert rows[0][4] == 0


def test_notification_stays_in_window_until_clicked_across_tabs_and_resize(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    app_directory = tmp_path / "app"
    app_directory.mkdir()
    job = tmp_path / "RESULTS" / "sample-job"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"git_ref": "v1", "parallel_workers": 1}), encoding="utf-8")
    (job / "markers.inventory.json").write_text(json.dumps({
        "markers": [{"id": "marker-1", "warnClass": "NULL", "file": "src/check.go",
                     "line": 42, "msg": "Finding"}],
        "truncated": False, "total_count": 1, "returned_count": 1,
        "filters_applied": {"advanced_filter": queue.GOST_FILTER},
    }), encoding="utf-8")
    (job / "decisions.jsonl").write_text(json.dumps({
        "marker_id": "marker-1", "warnClass": "NULL", "file": "src/check.go",
        "line": 42, "verdict": None,
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(qt_gui, "read_codex_rate_limits", lambda: {})
    window = TriageQtWindow(job, app_directory)
    try:
        window.show()
        app.processEvents()
        assert not window.notification_panel.isVisible()
        with (job / "marker-history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "attempt_id": "new-result", "marker_id": "marker-1",
                "status": "completed", "verdict": "False Positive",
                "warnClass": "NULL", "file": "src/check.go", "line": 42,
            }) + "\n")
        window.refresh()
        app.processEvents()
        assert window.notification_panel.isVisible()
        assert window.notification_panel.parent() is window.centralWidget()
        assert not window.notification_panel.isWindow()
        assert len(window.notification_buttons) == 1
        toast = next(iter(window.notification_buttons.values()))
        assert toast.objectName() == "toastGreen"
        assert window.notification_panel.graphicsEffect() is None
        assert toast.graphicsEffect() is None
        before_panel, before_toast = window.notification_panel.geometry(), toast.geometry()
        QTest.mouseMove(toast)
        QTest.qWait(220)
        app.processEvents()
        assert window.notification_panel.geometry() == before_panel
        assert toast.geometry() == before_toast
        assert toast.visibleRegion().boundingRect().height() == toast.height()
        window.tabs.setCurrentWidget(window.settings_tab)
        window.resize(1450, 920)
        app.processEvents()
        assert window.notification_panel.isVisible()
        assert window.notification_panel.geometry().right() <= window.centralWidget().width()
        toast.click()
        app.processEvents()
        assert not window.notification_panel.isVisible()
        assert window.tabs.currentWidget() is window.markers_tab
        assert qt_gui.sync_notifications(job) == []
    finally:
        window.close()


def test_notification_from_another_job_opens_its_marker(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    app_directory = tmp_path / "app"
    app_directory.mkdir()
    jobs = []
    for name, marker_id in (("first-job", "first-marker"), ("second-job", "second-marker")):
        job = tmp_path / "RESULTS" / name
        job.mkdir(parents=True)
        (job / "job.json").write_text(json.dumps({"git_ref": name}), encoding="utf-8")
        (job / "markers.inventory.json").write_text(json.dumps({
            "markers": [{"id": marker_id, "warnClass": "NULL", "file": "src/check.go",
                         "line": 42, "msg": "Finding"}],
            "truncated": False, "total_count": 1, "returned_count": 1,
            "filters_applied": {"advanced_filter": queue.GOST_FILTER},
        }), encoding="utf-8")
        (job / "decisions.jsonl").write_text(json.dumps({
            "marker_id": marker_id, "warnClass": "NULL", "file": "src/check.go",
            "line": 42, "verdict": None,
        }) + "\n", encoding="utf-8")
        jobs.append(job)
    monkeypatch.setattr(qt_gui, "read_codex_rate_limits", lambda: {})
    window = TriageQtWindow(jobs[0], app_directory)
    try:
        window.show()
        app.processEvents()
        with (jobs[1] / "marker-history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "attempt_id": "second-result", "marker_id": "second-marker",
                "status": "failed", "warnClass": "NULL", "file": "src/check.go", "line": 42,
            }) + "\n")
        window.refresh()
        app.processEvents()
        assert len(window.notification_buttons) == 1
        toast = next(iter(window.notification_buttons.values()))
        assert toast.objectName() == "toastRed"
        window.busy = True
        toast.click()
        assert len(window.notification_buttons) == 1  # Busy switch must not dismiss it.
        window.busy = False
        toast.click()
        app.processEvents()
        assert window.job == jobs[1].resolve()
        assert window.current_marker_id == "second-marker"
        assert window.tabs.currentWidget() is window.markers_tab
        assert not window.notification_panel.isVisible()
        for index, job in enumerate(jobs):
            with (job / "marker-history.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "attempt_id": f"bulk-{index}", "marker_id": ("first-marker", "second-marker")[index],
                    "status": "completed", "verdict": "False Positive",
                    "warnClass": "NULL", "file": "src/check.go", "line": 42,
                }) + "\n")
        window.refresh()
        app.processEvents()
        assert len(window.notification_buttons) == 2
        assert window.notification_dismiss_all.text() == "Скрыть все"
        window.notification_dismiss_all.click()
        app.processEvents()
        assert window.tabs.currentWidget() is window.markers_tab
        assert not window.notification_panel.isVisible()
        assert all(qt_gui.sync_notifications(job) == [] for job in jobs)
    finally:
        window.close()


def test_four_tabs_queue_and_card_render_without_side_effects(tmp_path: Path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    root = tmp_path
    app_directory = root / "app"
    app_directory.mkdir()
    job = root / "RESULTS" / "example-job"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({
        "repository_url": "https://example.invalid/example.git",
        "git_ref": "v1", "parallel_workers": 1,
        "batch_size": 10, "run_mode": "single_batch", "codex_model": "gpt-5.6-sol",
    }), encoding="utf-8")
    markers = [
        {"id": f"marker-{index}", "review": "Undecided", "warnClass": "TEST",
         "file": f"source{index}.go", "line": index, "msg": f"Finding {index}"}
        for index in range(1, 11)
    ]
    (job / "markers.inventory.json").write_text(json.dumps({
        "markers": markers, "truncated": False,
        "total_count": len(markers), "returned_count": len(markers),
        "filters_applied": {"advanced_filter": queue.GOST_FILTER},
    }), encoding="utf-8")
    (job / "decisions.jsonl").write_text("".join(json.dumps({
        "marker_id": marker["id"], "warnClass": marker["warnClass"],
        "file": marker["file"], "line": marker["line"], "verdict": None,
    }) + "\n" for marker in markers), encoding="utf-8")

    monkeypatch.setattr(qt_gui, "read_codex_rate_limits", lambda: {
        "rateLimits": {"primary": {"usedPercent": 51, "windowDurationMins": 10080}},
    })
    monkeypatch.setattr(qt_gui, "read_codex_models", lambda: [
        {"model": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"},
        {"model": "gpt-6-astra", "display_name": "GPT-6-Astra"},
    ])
    window = TriageQtWindow(job, app_directory)
    try:
        assert window._codex_limit_future is not None
        window._codex_limit_future.result(timeout=3)
        window.drain_codex_limit()
        assert "Доступно Codex: 49%" in window.codex_limit.text()
        assert "недельный лимит" in window.codex_limit.text()
        assert window._jobs_future is not None
        window._jobs_future.result(timeout=3)
        window.drain_jobs()
        assert window.jobs_table.rowCount() == 1
        assert [window.tabs.tabText(index).split()[0] for index in range(4)] == [
            "Обзор", "Маркеры", "История", "Настройки",
        ]
        assert "История" not in [widget.text() for widget in window.markers_tab.findChildren(QPushButton)]
        assert window.open_svacer_button.property("tone") == "primary"
        assert window.edit_button.property("tone") == "violet"
        assert window.approve_button.property("tone") == "success"
        assert window.reset_button.property("tone") == "warning"
        assert window.stop_job_button.property("tone") == "danger"
        assert window.active_table.rowCount() == 0
        assert window.queue_table.rowCount() == 0
        assert window.marker_table.rowCount() == 10
        assert window.marker_table.item(0, 0).text() == "Ожидает"
        assert not window.analysis_button.isEnabled()
        assert window.analysis_button.text() == "Выберите маркеры"
        setting_labels = [widget.text() for widget in window.settings_tab.findChildren(qt_gui.QLabel)]
        assert not any("Режим обработки" in text or "Маркеров за один запуск" in text
                       for text in setting_labels)
        assert not hasattr(window, "batch_size")
        assert not window.send_button.isEnabled()
        assert not (job / "control.json").exists()
        release_connection = Event()
        monkeypatch.setattr(qt_gui, "check_mcp", lambda *_args: (
            release_connection.wait(timeout=1), "недоступен",
        )[1])
        window.check_connection()
        assert window._connection_future is not None
        assert not window.busy
        assert not window.analysis_button.isEnabled()
        release_connection.set()

        window.state.update({
            "codex_run": {"active": True, "phase": "analysis"},
            "run_mode": "single_batch", "batch_size": 10, "parallel_workers": 1,
            "workers": {1: {"marker_ids": ["marker-1"], "current_status": "assigned"}},
        })
        window.populate_work_queue()
        assert window.active_table.rowCount() == 1
        assert window.queue_table.rowCount() == 0
        window.state["priority_marker_ids"] = ["marker-3"]
        window.populate_work_queue()
        assert window.queue_table.rowCount() == 1
        window.queue_table.selectRow(0)
        window.queue_table.cellClicked.emit(0, 0)
        assert window.current_live_id == "marker-3"
        assert "В очереди" in window.live_meta.text()
        assert not window.active_table.selectionModel().hasSelection()
        window.state["priority_marker_ids"] = ["marker-3", "marker-4"]
        window.populate_work_queue()
        window.queue_table.selectionModel().select(
            window.queue_table.model().index(1, 0),
            qt_gui.QItemSelectionModel.SelectionFlag.Select | qt_gui.QItemSelectionModel.SelectionFlag.Rows,
        )
        window.populate_work_queue()
        assert window.selected_queue_ids() == ["marker-3", "marker-4"]
        # Clicking the previously selected active row must return its live output.
        window.active_table.cellClicked.emit(0, 0)
        assert window.current_live_id == "marker-1"
        assert "В работе" in window.live_meta.text()
        assert not window.queue_table.selectionModel().hasSelection()
        window.populate_work_queue()
        assert window.current_live_id == "marker-1"
        window.state["priority_marker_ids"] = []
        window.populate_work_queue()
        window.update_action_states()
        assert window.analysis_button.property("tone") == "danger"
        window.marker_table.selectRow(3)
        window.update_action_states()
        assert window.add_queue_button.isEnabled()
        window.state["parallel_workers"] = 2
        window.state["workers"][2] = {"marker_ids": ["marker-2"], "current_status": "assigned"}
        window.populate_work_queue()
        assert window.active_table.rowCount() == 2
        assert window.queue_table.rowCount() == 0

        window.open_live_monitor()
        assert window.live_dialog is not None
        assert window.live_dialog.isMaximized()
        window.live_dialog.close()
        window.open_live_marker_card()
        assert window.tabs.currentWidget() is window.markers_tab
        assert "Finding 1" in window.marker_detail.toPlainText()
        assert "Трасса" in window.marker_detail.toPlainText()
        window.refresh()
        assert window.analysis_button.property("tone") == "neutral"

        for index in range(4):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
        assert window._model_future is not None
        window._model_future.result(timeout=3)
        window.drain_model_catalog()
        assert window.model_combo.currentData() == "gpt-5.6-sol"
        window.model_combo.setCurrentIndex(window.model_combo.findData("gpt-6-astra"))
        assert window.analysis_scope_combo.currentData() == "product_and_tooling"
        window.analysis_scope_combo.setCurrentIndex(window.analysis_scope_combo.findData("shipped_product"))
        window.save_execution_settings()
        assert json.loads((job / "job.json").read_text(encoding="utf-8"))["codex_model"] == "gpt-6-astra"
        assert json.loads((job / "job.json").read_text(encoding="utf-8"))["analysis_scope"] == "shipped_product"
        window.load_settings_form()
        assert window.model_combo.currentData() == "gpt-6-astra"
        assert window.analysis_scope_combo.currentData() == "shipped_product"
        with monkeypatch.context() as running:
            running.setattr(qt_gui, "read_run_record", lambda _: {"active": True})
            window.analysis_scope_combo.setCurrentIndex(window.analysis_scope_combo.findData("product_and_tooling"))
            window.save_execution_settings()
            assert json.loads((job / "job.json").read_text(encoding="utf-8"))["analysis_scope"] == "shipped_product"
            assert "завершите текущий анализ" in window.status.text()
        window.load_settings_form()
        window.refresh()
        assert window.queue_table.rowCount() == 0
        assert window.history_table.rowCount() == 0
        assert not (job / "control.json").exists()

        decisions = [
            {"marker_id": marker["id"], "warnClass": marker["warnClass"],
             "file": marker["file"], "line": marker["line"],
             "verdict": "False Positive" if marker["id"] == "marker-1" else None}
            for marker in markers
        ]
        (job / "decisions.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in decisions), encoding="utf-8",
        )
        window.refresh()
        assert window.queue_table.rowCount() == 0
        assert "1 из 10" in window.progress_text.text()
        assert window.marker_table.item(0, 0).text() == "False Positive"
        assert window.marker_table.item(0, 0).foreground().color() == qt_gui.QColor(qt_gui.COLORS["green"])
        captured = {}
        def fake_edit(*args, **kwargs):
            captured["marker_id"] = args[2]
            captured["comment"] = args[3]
            captured["verdict"] = kwargs["verdict"]
            return {"verdict": kwargs["verdict"], "invalidated_import_files": []}
        monkeypatch.setattr(qt_gui, "edit_saved_decision", fake_edit)
        window.current_marker_id = "marker-1"
        def interact() -> None:
            dialog = app.activeModalWidget()
            assert isinstance(dialog, QDialog)
            dialog.findChildren(QComboBox)[0].setCurrentText("Won't fix")
            dialog.findChild(QPlainTextEdit).setPlainText("Доказанный путь, локальный комментарий")
            next(widget for widget in dialog.findChildren(QPushButton) if widget.text() == "Сохранить").click()
        QTimer.singleShot(0, interact)
        window.edit_current_decision()
        assert captured == {
            "marker_id": "marker-1", "comment": "Доказанный путь, локальный комментарий",
            "verdict": "Won't fix",
        }
        (job / "incomplete-analysis.json").write_text(
            json.dumps({"marker-2": {"reason": "requires more context"}}), encoding="utf-8",
        )
        window.refresh()
        assert window.queue_table.rowCount() == 0
        assert window.marker_table.item(1, 0).text() == "Не завершён"
        assert window.marker_table.item(1, 0).foreground().color() == qt_gui.QColor(qt_gui.COLORS["amber"])
        assert not (job / "control.json").exists()
        window.workers.setValue(2)
        window.refresh()
        assert window.workers.value() == 2
        assert json.loads((job / "job.json").read_text(encoding="utf-8"))["parallel_workers"] == 1
        (job / "svacer-import-preview.json").write_text(json.dumps({
            "confirmation": "CONFIRM", "marker_count": 10, "conflict_count": 0,
        }), encoding="utf-8")
        monkeypatch.setattr(qt_gui.QMessageBox, "question", lambda *_args: qt_gui.QMessageBox.StandardButton.No)
        monkeypatch.setattr(window, "run_background", lambda *_args: (_ for _ in ()).throw(
            AssertionError("import must not run after cancellation"),
        ))
        window.confirm_and_send_prepared({})
        assert "отменена" in window.status.text()
        selected = window.marker_table.selectionModel()
        window.marker_table.selectRow(2)
        selected.select(
            window.marker_table.model().index(3, 0),
            qt_gui.QItemSelectionModel.SelectionFlag.Select | qt_gui.QItemSelectionModel.SelectionFlag.Rows,
        )
        assert window.selected_marker_ids() == ["marker-3", "marker-4"]
        window._table_signatures.pop("markers", None)
        window.populate_markers()
        assert window.selected_marker_ids() == ["marker-3", "marker-4"]
        monkeypatch.setattr(qt_gui, "launch_runner", lambda *_args: (_ for _ in ()).throw(
            AssertionError("adding to the queue must not start analysis"),
        ))
        window.add_selected_to_queue()
        assert json.loads((job / "control.json").read_text(encoding="utf-8"))["priority_marker_ids"] == [
            "marker-3", "marker-4",
        ]
        assert window.queue_table.rowCount() == 2
        assert "Анализ не запущен" in window.status.text()
        monkeypatch.setattr(qt_gui.QMessageBox, "question", lambda *_args: qt_gui.QMessageBox.StandardButton.Yes)
        window.marker_table.selectRow(0)
        window.add_selected_to_queue()
        control = json.loads((job / "control.json").read_text(encoding="utf-8"))
        assert control["priority_marker_ids"] == ["marker-3", "marker-4", "marker-1"]
        assert control["recheck_marker_ids"] == ["marker-1"]
        assert window.queue_table.item(2, 1).text() == "Перепроверка"
        assert window.queue_table.item(2, 1).foreground().color() == qt_gui.QColor(qt_gui.COLORS["violet"])
        assert window.decision_by_id["marker-1"]["verdict"] == "False Positive"
        assert window.queue_table.selectionMode() == qt_gui.QTableWidget.SelectionMode.ExtendedSelection
        window.queue_table.selectRow(1)
        window.queue_table.selectionModel().select(
            window.queue_table.model().index(2, 0),
            qt_gui.QItemSelectionModel.SelectionFlag.Select | qt_gui.QItemSelectionModel.SelectionFlag.Rows,
        )
        window.populate_work_queue()
        assert window.selected_queue_ids() == ["marker-4", "marker-1"]
        assert window.remove_queue_button.text() == "Убрать из очереди (2)"
        monkeypatch.setattr(qt_gui.QMessageBox, "question", lambda *_args: qt_gui.QMessageBox.StandardButton.No)
        window.remove_selected_from_queue()
        assert window.queue_table.rowCount() == 3
        monkeypatch.setattr(qt_gui.QMessageBox, "question", lambda *_args: qt_gui.QMessageBox.StandardButton.Yes)
        window.remove_selected_from_queue()
        control = json.loads((job / "control.json").read_text(encoding="utf-8"))
        assert control["priority_marker_ids"] == ["marker-3"]
        assert control["recheck_marker_ids"] == []
        assert window.queue_table.rowCount() == 1
        assert window.decision_by_id["marker-1"]["verdict"] == "False Positive"
        window.state.update({"total": 10, "completed": 10, "priority_marker_ids": ["marker-1"]})
        window.update_action_states()
        assert window.analysis_button.isEnabled()
        assert window.analysis_button.text() == "Начать анализ"
    finally:
        window.close()
