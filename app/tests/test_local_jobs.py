"""Local lifecycle regressions; only temporary jobs and mocked server/trash."""
import copy
import json
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run
import local_jobs as jobs
import triage_gui_qt as ui
import triage_queue as q
from test_manual_queue_ui import manual_window, table_ids


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def updated_inventory(job):
    value = read(job / "markers.inventory.json")
    value["markers"][0]["review"] = {"status": "Confirmed"}
    value["markers"].append({"id": "new", "warnClass": "NULL", "file": "new.go", "line": 1})
    value["total_count"] = value["returned_count"] = len(value["markers"])
    return value


def test_refresh_preserves_local_results_queue_drafts_and_backup(manual_window):
    _, job, _, ids, _ = manual_window
    decisions = q.load_decisions(job / "decisions.jsonl")
    decisions[0].update(verdict="False Positive", comment="Proven local result")
    q.atomic_write_jsonl(job / "decisions.jsonl", decisions)
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": [ids[2]], "pause_requested": True})
    before = {name: (job / name).read_bytes() for name in
              ("markers.inventory.json", "decisions.jsonl", "control.json", "notes/batch-001-worker-1.json")}
    result = jobs.update_marker_inventory(job, job.parent.parent, updated_inventory(job))
    assert result["added"] == 1
    rows = q.load_decisions(job / "decisions.jsonl")
    assert rows[:18] == decisions and rows[-1]["verdict"] is None
    assert read(job / "markers.inventory.json")["markers"][0]["review"]["status"] == "Confirmed"
    for name in ("control.json", "notes/batch-001-worker-1.json"):
        assert (job / name).read_bytes() == before[name]
    backup = next((job / "inventory-backups").iterdir())
    for name in ("markers.inventory.json", "decisions.jsonl"):
        assert (backup / name).read_bytes() == before[name]


@pytest.mark.parametrize("failure", ["truncated", "removed", "relocated", "filter", "import", "active"])
def test_bad_refresh_changes_nothing(manual_window, failure):
    _, job, run, _, _ = manual_window
    payload = updated_inventory(job)
    if failure == "truncated": payload["truncated"] = True
    if failure == "removed":
        payload["markers"].pop(0)
        payload["total_count"] = payload["returned_count"] = len(payload["markers"])
    if failure == "relocated": payload["markers"][0]["line"] = 999
    if failure == "filter": payload["filters_applied"]["review"] = ["Undecided"]
    if failure == "import": q.atomic_write_json(job / "svacer-import-attempt.json", {})
    if failure == "active": run["active"] = True
    before = {name: (job / name).read_bytes() for name in ("markers.inventory.json", "decisions.jsonl")}
    with pytest.raises(ValueError):
        jobs.update_marker_inventory(job, job.parent.parent, payload)
    assert all((job / name).read_bytes() == data for name, data in before.items())


def test_refresh_rollback_retains_previous_pair_and_backup(manual_window, monkeypatch):
    _, job, _, _, _ = manual_window
    before = {name: (job / name).read_bytes() for name in ("markers.inventory.json", "decisions.jsonl")}
    monkeypatch.setattr(jobs, "atomic_write_json", lambda *_a: (_ for _ in ()).throw(OSError("Disk error")))
    with pytest.raises(OSError):
        jobs.update_marker_inventory(job, job.parent.parent, updated_inventory(job))
    assert all((job / name).read_bytes() == data for name, data in before.items())


def test_empty_snapshot_can_be_refreshed_later(tmp_path, monkeypatch):
    job = tmp_path / "RESULTS" / "empty"
    job.mkdir(parents=True)
    q.atomic_write_json(job / "job.json", {})
    monkeypatch.setattr(codex_run, "read_run_record", lambda _job: {"active": False})
    empty = {"markers": [], "truncated": False, "total_count": 0, "returned_count": 0,
             "filters_applied": {"advanced_filter": q.GOST_FILTER}}
    assert jobs.update_marker_inventory(job, tmp_path, empty)["inventory_total"] == 0
    value = copy.deepcopy(empty)
    value.update(markers=[{"id": "m", "file": "f.go", "line": 1, "warnClass": "NULL"}],
                 total_count=1, returned_count=1)
    assert jobs.update_marker_inventory(job, tmp_path, value)["added"] == 1


def test_initial_refresh_failure_can_be_retried(tmp_path, monkeypatch):
    job = tmp_path / "RESULTS" / "new"
    job.mkdir(parents=True)
    q.atomic_write_json(job / "job.json", {})
    monkeypatch.setattr(codex_run, "read_run_record", lambda _job: {"active": False})
    payload = {"markers": [], "truncated": False, "total_count": 0, "returned_count": 0,
               "filters_applied": {"advanced_filter": q.GOST_FILTER}}
    original_write = jobs.atomic_write_json
    monkeypatch.setattr(jobs, "atomic_write_json", lambda *_a: (_ for _ in ()).throw(OSError("Disk error")))
    with pytest.raises(OSError):
        jobs.update_marker_inventory(job, tmp_path, payload)
    assert not (job / "decisions.jsonl").exists()
    monkeypatch.setattr(jobs, "atomic_write_json", original_write)
    assert jobs.update_marker_inventory(job, tmp_path, payload)["inventory_total"] == 0


def test_refresh_button_really_requests_server_even_when_cached(manual_window, monkeypatch):
    window, job, _, _, _ = manual_window
    payload = updated_inventory(job)
    metadata = {**read(job / "job.json"), "advanced_filter": q.GOST_FILTER,
                "project_id": "project", "branch_id": "branch", "snapshot_id": "snapshot"}
    q.atomic_write_json(job / "job.json", metadata)
    window.settings["advanced_filter"] = q.GOST_FILTER
    called = []
    async def get_markers(_url, _token, tool, args):
        called.append((tool, args))
        return json.dumps(payload)
    monkeypatch.setattr(ui, "call_mcp_tool", get_markers)
    monkeypatch.setattr(ui, "check_mcp", lambda *_a: "подключён")
    monkeypatch.setattr(window, "run_background", lambda work, done, _message: done(work()))
    window.fetch_markers()
    assert len(called) == 1 and called[0][0] == "get_markers"
    assert called[0][1]["snapshot_id"] == "snapshot"
    assert "review" not in called[0][1]
    assert "new" in table_ids(window.marker_table)
    assert window.fetch_button.text() == "Обновить маркеры"


def fake_recycle(monkeypatch, root):
    def move(path):
        source = Path(path)
        target = root / ("recycled-" + source.name)
        source.rename(target)
        return True, str(target)
    monkeypatch.setattr(QFile, "moveToTrash", move)


def test_delete_only_selected_job_is_recoverable(manual_window, monkeypatch):
    _, job, _, _, _ = manual_window
    other = job.parent / "other"
    other.mkdir()
    q.atomic_write_json(other / "job.json", {})
    before = (job / "decisions.jsonl").read_bytes()
    fake_recycle(monkeypatch, job.parent.parent)
    jobs.trash_local_job(job, job.parent.parent)
    assert not job.exists() and other.is_dir()
    assert (job.parent.parent / ("recycled-" + job.name) / "decisions.jsonl").read_bytes() == before


@pytest.mark.parametrize("failure", ["root", "results", "outside", "active", "invalid_state", "import", "trash_failure"])
def test_delete_fail_closed(manual_window, monkeypatch, tmp_path, failure):
    _, job, run, _, _ = manual_window
    root = job.parent.parent
    target = job
    if failure == "root": target = root
    if failure == "results": target = job.parent
    if failure == "outside":
        target = tmp_path / "outside"
        target.mkdir()
        q.atomic_write_json(target / "job.json", {})
    if failure == "active": run["active"] = True
    if failure == "invalid_state": (job / "codex-run.json").write_text("broken", encoding="utf-8")
    if failure == "import": q.atomic_write_json(job / "svacer-import-attempt.json", {})
    monkeypatch.setattr(QFile, "moveToTrash", lambda *_a: (False, "") if failure == "trash_failure"
                        else pytest.fail("Must not reach the recycle bin"))
    with pytest.raises((ValueError, OSError)):
        jobs.trash_local_job(target, root)
    assert job.exists() and (job / "decisions.jsonl").exists()


def test_delete_last_job_keeps_empty_window_and_new_project_button(manual_window, monkeypatch):
    window, job, _, _, app = manual_window
    fake_recycle(monkeypatch, job.parent.parent)
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(window, "run_background", lambda work, done, _message: done(work()))
    window.delete_current_job()
    window.refresh()
    app.processEvents()
    assert window.marker_table.rowCount() == 0
    assert window.jobs_table.rowCount() == 0
    assert window.new_job_button.isEnabled()
    assert not window.fetch_button.isVisible()
    assert not window.delete_job_button.isEnabled()
    assert window.progress_text.text() == "0 из 0"
    assert not job.exists()  # Timers must not recreate the deleted directory.


def test_delete_switches_to_other_job_and_refuses_relaunch(manual_window, monkeypatch):
    window, job, _, _, _ = manual_window
    other = job.parent / "other"
    other.mkdir()
    q.atomic_write_json(other / "job.json", {})
    fake_recycle(monkeypatch, job.parent.parent)
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(window, "run_background", lambda work, done, _message: done(work()))
    window.delete_current_job()
    assert window.job == other.resolve()
    assert window.fetch_button.text() == "Получить маркеры"
    monkeypatch.setattr(codex_run, "_launch_runner_locked", lambda *_a: pytest.fail("Deleted job cannot run"))
    with pytest.raises(ValueError, match="удалена"):
        codex_run.launch_runner(job, job.parent.parent / "app")
    assert not job.exists()


def test_delete_cancel_and_stale_job_list(manual_window, monkeypatch):
    window, job, _, _, _ = manual_window
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.No)
    monkeypatch.setattr(ui, "trash_local_job", lambda *_a: pytest.fail("Cancelled"))
    window.delete_current_job()
    assert job.exists()
    window.job_paths = []
    window.apply_job_rows([("old", "Работает", "1/18", "1")], [str(job.resolve())])
    assert window.jobs_table.rowCount() == 0


def test_startup_without_jobs_can_create_new_project(manual_window):
    old, job, _, _, app = manual_window
    root = job.parent.parent / "empty-install"
    app_dir = root / "app"
    app_dir.mkdir(parents=True)
    window = ui.TriageQtWindow(root / "RESULTS" / ".no-job", app_dir)
    try:
        window.show()
        app.processEvents()
        assert window.new_job_button.isEnabled()
        assert not window.delete_job_button.isEnabled()
        assert not (root / "RESULTS").exists()
        assert "Локальных задач нет" in window.subtitle.text()
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, ui.QEvent.Type.DeferredDelete)
        app.processEvents()


def test_external_delete_recreate_switches_to_same_project(manual_window):
    window, job, _, _, _ = manual_window
    q.atomic_write_json(job / "job.json", {"repository_url": "https://example.test/nats", "git_ref": "v2"})
    window.refresh()
    replacement = job.parent / "new-nats"
    replacement.mkdir()
    q.atomic_write_json(replacement / "job.json", read(job / "job.json"))
    other = job.parent / "other"
    other.mkdir()
    q.atomic_write_json(other / "job.json", {"repository_url": "https://example.test/other"})
    job.rename(job.parent.parent / "old-job-backup")
    window.refresh()  # Simulate the periodic refresh of an already open window.
    assert window.job == replacement.resolve()
    assert "nats v2" in window.subtitle.text()
    assert window.job_combo.currentText().endswith("new-nats")
    assert window.fetch_button.isVisible() and window.fetch_button.isEnabled()
    assert not job.exists()


def test_external_delete_last_clears_stale_selector(manual_window):
    window, job, _, _, _ = manual_window
    job.rename(job.parent.parent / "old-job-backup")
    window.refresh()
    assert window.job_combo.count() == 0
    assert window.job_paths == []
    assert "Локальных задач нет" in window.subtitle.text()


def test_startup_with_deleted_job_recovers_after_widgets_are_ready(manual_window):
    existing, job, _, _, app = manual_window
    window = ui.TriageQtWindow(job.parent / "deleted-job", existing.app_directory)
    try:
        window.show()
        app.processEvents()
        assert window.job == job.resolve()
        assert window.marker_table.rowCount() == 18
        assert window.job_combo.currentIndex() >= 0
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, ui.QEvent.Type.DeferredDelete)
        app.processEvents()
