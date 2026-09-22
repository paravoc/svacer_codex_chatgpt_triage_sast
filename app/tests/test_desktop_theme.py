"""Offline widget regressions and an isolated visual preview; no real job or network."""
import json
import sys
import threading
import time
import tkinter as tk
from contextlib import ExitStack
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_theme as theme
import triage_gui as gui


@pytest.fixture(scope="module")
def tk_root():
    window = tk.Tk()
    window.withdraw()
    yield window
    window.destroy()


@pytest.fixture
def root(tk_root):
    # Reuse the interpreter like the real desktop app; repeated Tk init/destroy is
    # unstable on some Windows Tcl builds and is not an application workflow.
    yield tk_root
    tk_root.withdraw()
    tk_root.update_idletasks()
    for child in tk_root.winfo_children():
        child.destroy()


def build_preview(root):
    """Real UI construction, but all startup I/O explicitly disconnected."""
    stack = ExitStack()
    stack.enter_context(patch.object(gui, "read_json", return_value={
        "project_name": "Gateway", "parallel_workers": 1, "batch_size": 15,
        "run_mode": "single_batch"}))
    stack.enter_context(patch.object(gui.os, "getenv", return_value=""))
    for method in ("refresh", "check_connection", "refresh_job_selector", "refresh_history_projects", "refresh_history_table"):
        stack.enter_context(patch.object(gui.TriageGui, method, return_value=None))
    app = gui.TriageGui(root, Path("theme-preview"), Path("theme-preview/app"))
    root.title("Svacer — изолированный просмотр темы")
    app.subtitle_var.set("Gateway v1.7.2  ·  локальный просмотр оформления")
    app.job_selector_var.set("Gateway v1.7.2")
    app.connection_label.configure(text="Просмотр темы", fg=theme.MUTED)
    app.progress_var.set("4 из 77  ·  5,2%")
    app.scope_var.set("В снимке 77  ·  размечено 4  ·  очередь приостановлена")
    app.analysis_run_var.set("Анализ остановлен. Сохранённые решения доступны в карточках.")
    app.codex_limit_var.set("")
    app.progress.configure(value=5.2)
    app.jobs_table.insert("", "end", values=("gateway v1.7.2", "Пауза", "4/77", "0"))
    app.jobs_table.insert("", "end", values=("envoy v1.37.2", "Пауза", "15/111", "0"))
    for i, (file, line) in enumerate((("chart_downloader.go", 249), ("runner.go", 170), ("validate.go", 1020))):
        app.pending_marker_table.insert("", "end", values=(i + 1, "Ожидает", "DEREF_AFTER_NULL", file, line))
    app.live_marker_title_var.set("DEREF_AFTER_NULL — chart_downloader.go:249")
    app.live_marker_status_var.set("Выбранный маркер · ожидает анализа")
    app.live_display_entries = []
    app.live_activity_text.configure(state="normal")
    app.live_activity_text.insert("1.0", "Здесь появятся сообщения агента для выбранного маркера.")
    app.live_activity_text.configure(state="disabled")
    app.message_var.set("Изолированный просмотр: анализ и отправка данных не запускаются.")
    # All buttons are inert in the preview, while retaining their original command registrations
    # in the actual application. Navigation, resizing, selection and scrolling remain usable.
    def disconnect(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                child.configure(command=lambda: None)
            disconnect(child)
    disconnect(root)
    return app, stack


def drain_animations(root, app, timeout=1.0):
    deadline = time.monotonic() + timeout
    while (app.panel_animations or app.window_animations) and time.monotonic() < deadline:
        root.update()
        time.sleep(.01)
    root.update()
    assert not app.panel_animations
    assert not app.window_animations


def test_theme_reentrant_and_images_retained(root):
    first = theme.apply_theme(root)
    images = root._triage_theme_images
    theme.apply_theme(root)
    assert root._triage_theme_images is images
    assert first.theme_use() == "triage-desktop"
    assert images
    assert str(first.lookup("Treeview", "fieldbackground")) == theme.SURFACE


@pytest.mark.parametrize("name", ["TButton", "Neutral.TButton", "Accent.TButton", "Success.TButton", "Danger.TButton", "Warning.TButton"])
def test_rounded_buttons_keep_native_commands_and_disabled_state(root, name):
    style = theme.apply_theme(root)
    calls = []
    button = ttk.Button(root, text="Подтвердить черновик", style=name, command=lambda: calls.append(1))
    button.pack()
    root.update_idletasks()
    button.invoke()
    button.state(["disabled"])
    button.invoke()
    assert calls == [1]
    assert style.lookup(name, "foreground", ("disabled",)) == "#7d8994"
    button.state(["!disabled", "focus"])
    button.invoke()
    assert calls == [1, 1]
    assert button.winfo_reqwidth() > 80


def test_widgets_build_and_all_tabs_remain(root):
    app, stack = build_preview(root)
    try:
        root.update_idletasks()
        assert [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()] == ["Обзор", "Маркеры", "История", "Настройки"]
        for tab in app.notebook.tabs():
            app.notebook.select(tab)
            root.update_idletasks()
        for name in ("triage_one_button", "approve_draft_button", "edit_decision_button", "marker_history_button",
                     "analysis_button", "reset_queue_button", "send_button"):
            assert getattr(app, name).winfo_exists()
        assert app.workers_var.get() == "1"
        assert app.batch_size_var.get() == "15"
        assert app.active_marker_table.bind("<Double-1>")
    finally:
        stack.close()


def test_png_has_transparent_corners_and_solid_center(root):
    image = tk.PhotoImage(master=root, data=theme.rounded_png(theme.SURFACE, theme.BORDER), format="png")
    assert image.transparency_get(0, 0)
    assert not image.transparency_get(image.width() // 2, image.height() // 2)


def test_progress_layout_keeps_native_pbar_node(root):
    style = theme.apply_theme(root)
    layout = style.layout("Horizontal.TProgressbar")
    # ttk sizes this node by value/maximum; an arbitrary image name would stay a fixed width.
    assert layout[0][1]["children"][0][0].endswith(".pbar")


def test_draft_and_unfinished_stay_out_of_visible_queue():
    decisions = [{"marker_id": f"m{i}", "verdict": None} for i in range(4)]
    state = {"workers": {}, "verifiers": {}, "priority_marker_ids": ["m1", "m0"]}
    assert gui.pending_marker_ids(decisions, state, {"m0", "m2"}) == ["m1", "m3"]
    assert gui.marker_matches_filter("draft", "m0", None, True, set())


def test_current_launch_queue_only_shows_selected_window():
    decisions = [{"marker_id": f"m{i}", "verdict": None} for i in range(70)]
    inventory_ids = [f"m{i}" for i in range(70)]
    state = {
        "codex_run": {"active": True, "phase": "analysis"},
        "run_mode": "single_batch", "batch_size": 10, "parallel_workers": 1,
        "workers": {1: {"marker_ids": ["m0"], "current_status": "assigned"}},
    }
    assert set(gui.marker_assignments(state)) == {"m0"}
    assert gui.current_run_queue_ids(decisions, state, set(), inventory_ids) == []
    state["priority_marker_ids"] = inventory_ids[:10]
    assert gui.current_run_queue_ids(decisions, state, set(), inventory_ids) == inventory_ids[1:10]

    state["parallel_workers"] = 2
    state["workers"][2] = {"marker_ids": ["m1"], "current_status": "assigned"}
    assert set(gui.marker_assignments(state)) == {"m0", "m1"}
    assert gui.current_run_queue_ids(decisions, state, set(), inventory_ids) == inventory_ids[2:10]

    decisions[0]["verdict"] = "False Positive"
    state["workers"] = {1: {"marker_ids": ["m1"], "current_status": "assigned"}}
    state["parallel_workers"] = 1
    state["run_remaining"] = 9
    assert gui.current_run_queue_ids(decisions, state, set(), inventory_ids) == inventory_ids[2:10]

    state["codex_run"] = {"active": False}
    assert gui.current_run_queue_ids(decisions, state, set(), inventory_ids) == inventory_ids[1:10]

    state["codex_run"] = {"active": False, "status": "failed"}
    state["run_remaining"] = 4
    assert gui.current_run_queue_ids(decisions, state, set(), inventory_ids) == inventory_ids[1:10]


def test_saved_result_from_current_batch_is_not_reported_as_still_running():
    state = {
        "codex_run": {"active": True, "phase": "quality_repair"},
        "workers": {
            1: {
                "marker_ids": ["current"],
                "current_status": "assigned",
                "saved_marker_ids": ["old", "current"],
                "current_saved_marker_ids": ["current"],
            },
            2: {
                "marker_ids": ["retry"],
                "current_status": "assigned",
                "saved_marker_ids": ["retry"],
                "current_saved_marker_ids": [],
            },
        },
    }
    assert gui.marker_assignments(state) == {"retry": "Агент 2"}


def test_saved_result_from_current_batch_is_not_reported_as_still_queued():
    decisions = [
        {"marker_id": "saved", "verdict": None},
        {"marker_id": "waiting", "verdict": None},
    ]
    state = {
        "codex_run": {"active": True, "phase": "quality_repair"},
        "manual_queue_requested": True,
        "priority_marker_ids": ["saved", "waiting"],
        "recheck_marker_ids": ["saved", "waiting"],
        "workers": {
            1: {
                "marker_ids": ["saved"],
                "current_status": "assigned",
                "current_saved_marker_ids": ["saved"],
            },
        },
    }
    assert gui.current_saved_marker_ids(state) == {"saved"}
    assert gui.current_run_queue_ids(
        decisions, state, {"saved", "waiting"}, ["saved", "waiting"],
    ) == ["waiting"]


def test_priority_selection_uses_only_available_agents():
    decisions = [{"marker_id": f"m{i}", "verdict": None} for i in range(20)]
    state = {
        "codex_run": {"active": True, "phase": "analysis"},
        "priority_marker_ids": [f"m{i}" for i in range(10)],
        "parallel_workers": 2, "batch_size": 10,
        "workers": {1: {"marker_ids": ["m0"], "current_status": "assigned"}},
    }
    assert set(gui.marker_assignments(state)) == {"m0"}
    assert gui.current_run_queue_ids(
        decisions, state, set(), [f"m{i}" for i in range(20)],
    ) == [f"m{i}" for i in range(1, 10)]


def test_staged_rechecks_appear_in_visible_manual_queue():
    decisions = [
        {"marker_id": "m0", "verdict": "False Positive"},
        {"marker_id": "m1", "verdict": None},
        {"marker_id": "m2", "verdict": "Confirmed"},
    ]
    state = {
        "codex_run": {"active": False}, "workers": {}, "verifiers": {},
        "priority_marker_ids": ["m2", "m1", "m0"],
        "recheck_marker_ids": ["m2", "m0"],
        "batch_size": 1, "parallel_workers": 1,
    }
    assert gui.current_run_queue_ids(decisions, state, set(), ["m0", "m1", "m2"]) == [
        "m2", "m1", "m0",
    ]


def test_overview_moves_markers_from_launch_queue_to_agents(root):
    app, stack = build_preview(root)
    try:
        app.inventory_by_id = {
            f"m{i}": {"id": f"m{i}", "warnClass": "DETECTOR", "file": "sample.go", "line": i}
            for i in range(70)
        }
        app.decisions = [{"marker_id": f"m{i}", "verdict": None} for i in range(70)]
        app.decision_by_id = {row["marker_id"]: row for row in app.decisions}
        state = {
            "codex_run": {"active": True, "phase": "analysis"},
            "run_mode": "single_batch", "batch_size": 10, "parallel_workers": 1,
            "priority_marker_ids": [f"m{i}" for i in range(10)],
            "workers": {1: {"marker_ids": ["m0"], "current_status": "assigned"}},
        }
        with patch.object(app, "refresh_live_marker_monitor"):
            app.refresh_work_queue_tables(state)
            assert len(app.active_row_markers) == 1
            assert len(app.pending_row_markers) == 9
            assert "9" in app.pending_title_var.get()

            state["parallel_workers"] = 2
            state["workers"][2] = {"marker_ids": ["m1"], "current_status": "assigned"}
            app.refresh_work_queue_tables(state)
            assert len(app.active_row_markers) == 2
            assert len(app.pending_row_markers) == 8
            assert set(app.pending_row_markers.values()) == {f"m{i}" for i in range(2, 10)}
    finally:
        stack.close()


def test_unfinished_without_note_is_still_visible_as_draft(tmp_path):
    (tmp_path / "incomplete-analysis.json").write_text(
        '{"m0": {"reason": "Требуется дополнить доказательство"}}', encoding="utf-8",
    )
    drafts = gui.unapplied_draft_results(tmp_path, [{"marker_id": "m0", "verdict": None}])
    assert drafts["m0"]["analysis_status"] == "needs_context"
    assert gui.pending_marker_ids([{"marker_id": "m0", "verdict": None}], {}, set(drafts)) == []


def test_status_alias_draft_is_visible_and_not_queued(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "batch-001-worker-1.json").write_text(
        json.dumps([{"marker_id": "m0", "status": "False Positive", "comment": "Evidence"}]),
        encoding="utf-8",
    )
    decisions = [{"marker_id": "m0", "verdict": None}]
    drafts = gui.unapplied_draft_results(tmp_path, decisions)
    assert drafts["m0"]["verdict"] == "False Positive"
    assert gui.pending_marker_ids(decisions, {}, set(drafts)) == []


def test_completed_single_batch_is_not_displayed_as_stuck_pause():
    run = {"status": "paused", "active": False, "reason": "Партия завершена. Для следующей нажмите «Начать анализ»."}
    state = {"paused": True, "single_batch_completed": True}
    assert gui.friendly_run_state(run, state) == ("Партия готова", "completed")
    assert gui.friendly_run_state(run, {"one_shot_completed": True}) == ("Выбранное обработано", "completed")
    label, color = gui.format_codex_run_status("gateway", "job", run)
    assert "партия завершена" in label
    assert color == theme.GREEN


def test_terminal_run_status_shows_exact_event_time_and_safe_fallback(tmp_path):
    run = {
        "status": "failed",
        "active": False,
        "finished_at": "2026-09-21T13:35:41",
        "reason": "fatal: couldn't find remote ref 2.11.2",
    }
    assert gui.format_run_event_time(run) == "время ошибки: 21.09.2026 13:35:41"
    label, color = gui.format_codex_run_status("nats-server", "job", run)
    assert "ошибка  •  время ошибки: 21.09.2026 13:35:41" in label
    assert run["reason"] in label
    assert color == theme.RED

    # An active run must never show a stale terminal timestamp.
    assert gui.format_run_event_time({**run, "active": True}) == ""

    # Old records did not contain finished_at. The UI labels the file time as
    # an update time instead of claiming it is the exact moment of the error.
    (tmp_path / "codex-run.json").write_text("{}", encoding="utf-8")
    fallback = gui.format_run_event_time(
        {"status": "failed", "active": False, "finished_at": "not-a-date"}, tmp_path,
    )
    assert fallback.startswith("запись обновлена: ")


def test_analysis_control_is_overview_only_and_uses_start_finish(root, tmp_path):
    app, stack = build_preview(root)
    try:
        (tmp_path / "markers.inventory.json").write_text("{}", encoding="utf-8")
        (tmp_path / "decisions.jsonl").write_text("", encoding="utf-8")
        app.job = tmp_path
        parent = app.analysis_button.master
        ancestors = []
        while parent is not None:
            ancestors.append(parent)
            parent = parent.master
        assert app.overview_tab in ancestors
        assert app.analysis_button not in app.workflow_action_box.winfo_children()

        app.update_workflow_action({"codex_run": {"active": False}, "total": 2, "completed": 0})
        assert app.analysis_button.cget("text") == "Начать анализ"
        app.update_workflow_action({"codex_run": {"active": True}, "total": 2, "completed": 0})
        assert app.analysis_button.cget("text") == "Завершить анализ"
        app.update_workflow_action({"codex_run": {"active": True}, "paused": True, "total": 2, "completed": 0})
        assert app.analysis_button.cget("text") == "Завершается…"
        assert app.analysis_button.instate(["disabled"])
    finally:
        stack.close()


def test_finish_analysis_waits_for_current_batch(root):
    app, stack = build_preview(root)
    try:
        calls = []
        with patch.object(gui, "read_run_record", return_value={"active": True}), \
             patch.object(gui, "collect_state", return_value={"paused": False}), \
             patch.object(gui, "set_pause", side_effect=lambda job, paused: calls.append((job, paused))), \
             patch.object(app, "confirm_stop_job", side_effect=AssertionError("do not discard current work")):
            app.analysis_action()
        assert calls == [(app.job, True)]
    finally:
        stack.close()


def test_start_analysis_reopens_stopped_queue(root, tmp_path):
    app, stack = build_preview(root)
    try:
        app.job = tmp_path
        for name, content in (
            ("markers.inventory.json", "{}"),
            ("decisions.jsonl", ""),
            ("START_PROMPT.txt", "Analyze pending markers"),
        ):
            (tmp_path / name).write_text(content, encoding="utf-8")
        calls = []
        with patch.object(gui, "read_run_record", return_value={"active": False}), \
             patch.object(gui, "collect_state", return_value={"paused": True, "total": 2, "completed": 0}), \
             patch.object(gui, "set_pause", side_effect=lambda job, paused: calls.append((job, paused))), \
             patch.object(gui, "launch_runner", return_value={"runner_pid": 123}):
            app.analysis_action()
        assert calls == [(tmp_path, False)]
    finally:
        stack.close()


def test_history_table_shows_only_results_and_errors(root, tmp_path):
    job = tmp_path / "sample-job"
    job.mkdir()
    records = [
        {"attempt_id": "ready", "marker_id": "m0", "status": "completed", "verdict": "Confirmed"},
        {"attempt_id": "error", "marker_id": "m1", "status": "failed", "verdict": None},
        {"attempt_id": "empty", "marker_id": "m2", "status": "completed", "verdict": None},
    ]
    (job / "marker-history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8",
    )
    original_refresh = gui.TriageGui.refresh_history_table
    app, stack = build_preview(root)
    try:
        with patch.object(gui, "list_saved_jobs", return_value=[job]), \
             patch.object(gui, "job_identity", return_value=({}, "Gateway")):
            original_refresh(app, force=True)
        rows = app.history_table.get_children()
        assert len(rows) == 2
        assert {app.history_table.item(row, "values")[5] for row in rows} == {"Confirmed", "Ошибка"}
        assert app.history_summary_var.get().startswith("Записей с результатом или ошибкой: 2")
        with patch.object(gui, "list_saved_jobs", return_value=[job]), \
             patch.object(gui, "load_marker_history", side_effect=AssertionError("unnecessary reload")):
            original_refresh(app)
        assert app.history_table.get_children() == rows
    finally:
        stack.close()


def test_unchanged_jobs_do_not_reorder_or_reselect(root):
    app, stack = build_preview(root)
    try:
        app.jobs_table.delete(*app.jobs_table.get_children())
        path = app.job.resolve()
        rows = [("job-current", path, ("Gateway", "Работает", "1/4", 1), "active", True)]
        app.apply_jobs_rows(rows)
        with patch.object(app.jobs_table, "move", wraps=app.jobs_table.move) as move, \
             patch.object(app.jobs_table, "selection_set", wraps=app.jobs_table.selection_set) as select:
            app.apply_jobs_rows(rows)
        move.assert_not_called()
        select.assert_not_called()
        assert app.stop_monitored_job_button.instate(["!disabled"])
    finally:
        stack.close()


def test_jobs_scan_does_not_block_ui_thread(root):
    app, stack = build_preview(root)
    finished = threading.Event()
    scanned_by = []
    try:
        def state_for_job(_path):
            scanned_by.append(threading.get_ident())
            finished.set()
            return {"completed": 1, "total": 4}

        with patch.object(gui, "list_saved_jobs", return_value=[app.job]), \
             patch.object(gui, "job_identity", return_value=({}, "Gateway")), \
             patch.object(gui, "collect_state", side_effect=state_for_job), \
             patch.object(gui, "read_run_record", return_value={"active": True}), \
             patch.object(gui, "friendly_run_state", return_value=("Работает", "active")), \
             patch.object(gui, "active_marker_ids", return_value=[]):
            app.refresh_jobs_table()
            assert finished.wait(2)
            deadline = time.monotonic() + 2
            while app.jobs_refresh_results.empty() and time.monotonic() < deadline:
                time.sleep(.01)
            app.drain_jobs_refresh()
        assert len(scanned_by) == 1
        assert scanned_by[0] != threading.get_ident()
        assert not app.jobs_refresh_in_flight
        assert len(app.jobs_table.get_children()) == 1
    finally:
        stack.close()


def test_hidden_activity_updates_without_replaying_animation(root):
    app, stack = build_preview(root)
    try:
        app.render_activity_widget(app.live_activity_text, ["Агент: начал проверку"])
        with patch.object(app, "animate_activity_entry") as animate:
            app.render_activity_widget(
                app.live_activity_text,
                ["Агент: начал проверку", "Агент: изучает вызовы"],
            )
        animate.assert_not_called()
        assert "Агент: изучает вызовы" in app.live_activity_text.get("1.0", "end")
    finally:
        stack.close()


def test_dark_palette_retains_text_contrast():
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        return sum(weight * (part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4)
                   for part, weight in zip(channels, (.2126, .7152, .0722)))
    assert luminance(theme.BG) < luminance("#181818")
    assert (luminance(theme.TEXT) + .05) / (luminance(theme.SURFACE) + .05) > 7
    assert (luminance(theme.MUTED) + .05) / (luminance(theme.SURFACE) + .05) > 5


def test_panel_open_close_reverse_and_resize(root):
    app, stack = build_preview(root)
    try:
        root.geometry("1280x840+5000+5000")
        root.deiconify()
        root.update()
        for toggle, frame in ((app.toggle_jobs_panel, app.jobs_body),
                              (app.toggle_active_panel, app.active_frame),
                              (app.toggle_pending_panel, app.pending_frame)):
            toggle()  # collapse completes without leaving a fixed height
            drain_animations(root, app)
            assert not frame.winfo_manager()
            toggle()  # rapid reversals retain a single live timer
            toggle()
            toggle()
            drain_animations(root, app)
            assert frame.winfo_manager() == "pack"
            assert frame.pack_propagate()
            toggle()
            drain_animations(root, app)
            assert not frame.winfo_manager()
            toggle()
            # A slow Tk build can deliver the first root Configure event only
            # after the panel animation has been scheduled.
            app.root_size = None
            root.geometry("1100x760+5000+5000")
            root.update()
            assert not app.panel_animations
            assert frame.winfo_manager() == "pack"
            assert frame.pack_propagate()
            root.geometry("1280x840+5000+5000")
            root.update()
            assert frame.winfo_height() > 12
        assert app.pending_marker_table.get_children()
    finally:
        root.withdraw()
        stack.close()


def test_expanded_monitor_open_close_reopen_and_resize(root):
    app, stack = build_preview(root)
    try:
        root.geometry("1280x840+5000+5000")
        root.deiconify()
        root.update()
        app.live_marker_id = "m0"
        app.live_display_entries = ["Агент: проверяет достижимость"]
        app.open_live_monitor_window()
        window = app.live_monitor_window
        assert window is not None
        window.state("normal")
        window.geometry("1000x700+5000+5000")
        root.update()
        app.close_live_monitor_window()
        app.open_live_monitor_window()  # reopening cancels the pending close
        assert app.live_monitor_window is window
        window.state("normal")
        window.geometry("1100x740+5000+5000")
        drain_animations(root, app)
        assert float(window.attributes("-alpha")) == pytest.approx(1.0)
        assert app.monitor_activity_text.winfo_height() > 100
        app.close_live_monitor_window()
        drain_animations(root, app)
        assert app.live_monitor_window is None
        assert not [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
    finally:
        if app.live_monitor_window is not None:
            app.live_monitor_window.destroy()
        root.withdraw()
        stack.close()


def test_edit_dialog_can_resize_and_close_without_stale_grab(root):
    app, stack = build_preview(root)
    try:
        root.geometry("1280x840+5000+5000")
        root.deiconify()
        root.update()
        app.current_marker_id = "m0"
        app.decision_by_id["m0"] = {
            "verdict": "False Positive", "warnClass": "DEREF_AFTER_NULL",
            "file": "example.go", "line": 12, "comment": "Проверено локально.",
        }
        app.edit_current_decision()
        dialogs = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
        assert len(dialogs) == 1
        dialog = dialogs[0]
        dialog.geometry("850x650+5000+5000")
        drain_animations(root, app)
        assert dialog.winfo_height() >= 600
        dialog.event_generate("<Escape>")
        drain_animations(root, app)
        assert not dialog.winfo_exists()
        assert root.grab_current() is None
    finally:
        root.withdraw()
        stack.close()


def test_closing_monitor_while_activity_animates_is_safe(root):
    app, stack = build_preview(root)
    errors = []
    original_reporter = root.report_callback_exception
    root.report_callback_exception = lambda *args: errors.append(args)
    try:
        root.geometry("1280x840+5000+5000")
        root.deiconify()
        root.update()
        app.live_marker_id = "m0"
        app.set_live_activity(["Агент: начал проверку"])
        app.open_live_monitor_window()
        app.set_live_activity(["Агент: начал проверку", "Агент: изучает вызовы"])
        app.close_live_monitor_window()
        drain_animations(root, app)
        deadline = time.monotonic() + .35
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert app.live_monitor_window is None
        assert not errors
    finally:
        root.report_callback_exception = original_reporter
        if app.live_monitor_window is not None:
            app.live_monitor_window.destroy()
        root.withdraw()
        stack.close()


def test_main_close_cancels_window_and_panel_timers(root):
    app, stack = build_preview(root)
    try:
        root.geometry("1280x840+5000+5000")
        root.deiconify()
        root.update()
        app.toggle_pending_panel()
        app._schedule_connection_poll(3000)
        app.live_marker_id = "m0"
        app.open_live_monitor_window()
        assert app.panel_animations and app.window_animations
        with patch.object(root, "destroy") as destroy:
            app.close()
            destroy.assert_called_once_with()
        assert app.closed
        assert not app.panel_animations
        assert not app.window_animations
        assert app.connection_after_id is None
        assert app.refresh_after_id is None
    finally:
        if app.live_monitor_window is not None:
            app.live_monitor_window.destroy()
        root.withdraw()
        stack.close()


def test_theme_included_in_portable_allowlist():
    manifest = (Path(__file__).resolve().parents[1] / "make_portable_package.ps1").read_text(encoding="utf-8-sig")
    assert "'START.cmd', 'START.vbs', 'README.md', 'LICENSE', 'pyproject.toml', 'poetry.lock'" in manifest
    assert "'START.ps1', 'bootstrap.ps1', 'startup_splash.ps1', 'CODEX_TASK.md'" in manifest
    assert "'demo.mp4'" in manifest
    assert "'demo-preview.gif'" in manifest
    assert "'screenshots/overview.png'" in manifest
    assert "'svacer-mcp/LICENSE'" not in manifest
    assert 'app/svacer-mcp/svacer_mcp/' not in manifest
    assert "'triage_connector/server.py'" in manifest
    assert "'triage_connector/markup.py'" in manifest
    assert "'requirements-connector.txt'" not in manifest
    assert "'desktop_theme.py'" in manifest
    assert "'marker_history.py'" in manifest
    assert "'marker_notifications.py'" in manifest
    assert "'decision_quality.py'" in manifest
    assert "'assets/svacer-triage.svg'" in manifest
    assert "'assets/svacer-triage-v2.ico'" in manifest
    assert "'assets/svacer-hamster-loading.gif'" in manifest
    assert "'svacer-settings.example.json'" in manifest
    assert "'svacer-settings.json'" not in manifest


@pytest.mark.parametrize("size", ["1060x720", "1280x840"])
def test_queue_and_activity_stay_visible_in_window(root, size):
    app, stack = build_preview(root)
    try:
        root.geometry(size + "+5000+5000")
        root.deiconify()
        root.update()
        assert app.pending_marker_table.winfo_height() >= 50
        assert app.live_activity_text.winfo_height() >= 20, {
            name: (getattr(app, name).winfo_height(), getattr(app, name).winfo_reqheight())
            for name in ("overview_workspace", "active_marker_table", "live_panel", "live_activity_text")}
        app.notebook.select(app.markers_tab)
        app.approve_draft_button.pack(side="right", padx=(0, 6))
        root.update()
        for button in (app.approve_draft_button, app.triage_one_button, app.open_svacer_button,
                       app.edit_decision_button, app.marker_history_button):
            assert button.winfo_width() >= button.winfo_reqwidth()
    finally:
        root.withdraw()
        stack.close()


if __name__ == "__main__":
    window = tk.Tk()
    app, stack = build_preview(window)
    app.live_marker_id = "preview-marker"
    app.live_display_entries = ["Агент: проверяет достижимость пути в тестовом примере."]
    for button, command in (
        (app.jobs_toggle_button, app.toggle_jobs_panel),
        (app.active_toggle_button, app.toggle_active_panel),
        (app.pending_toggle_button, app.toggle_pending_panel),
        (app.live_open_button, app.open_live_monitor_window),
    ):
        button.configure(command=command, state="normal")
    try:
        window.mainloop()
    finally:
        stack.close()
