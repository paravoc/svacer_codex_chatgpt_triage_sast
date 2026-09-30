"""Offline issue drafting and opt-in PoC tests; no model or service is contacted."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from issue_workspace import IssueStore, digest
from test_developer_issues import fixture


def setup_store(tmp_path):
    campaign, _, job, row = fixture(tmp_path)
    root = campaign.parent.parent
    directory = root / "RESULTS" / "job-1"
    return IssueStore(root), directory, job, row


def write_rows(job, rows):
    (job / "decisions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_all_confirmed_including_unverified_and_errors_are_explicit(tmp_path):
    store, directory, _, row = setup_store(tmp_path)
    rows = [row, {**row, "marker_id": "pending", "verification": {"status": "pending"}},
            {**row, "marker_id": "fp", "verdict": "False Positive"}]
    write_rows(directory, rows)
    corrupt = store.tool_root / "jobs" / "broken"
    corrupt.mkdir(parents=True)
    (corrupt / "job.json").write_text("{}")
    (corrupt / "decisions.jsonl").write_text("broken")
    candidates, errors = store.candidates([directory, corrupt])
    assert [item["marker_id"] for item in candidates] == ["marker-1", "pending"]
    assert errors and "broken" in errors[0]
    assert not store.base.exists()


def test_queue_survives_restart_and_does_not_overwrite_edits_or_analysis(tmp_path):
    store, directory, _, _ = setup_store(tmp_path)
    originals = {name: (directory / name).read_bytes() for name in ("job.json", "decisions.jsonl")}
    candidates, _ = store.candidates([directory])
    assert store.enqueue(candidates, with_poc=False) == 1
    assert store.enqueue(candidates, with_poc=False) == 0
    item = IssueStore(store.tool_root).load_queue()[0]
    result = store.prepare(item)
    body = (Path(result["case"]) / "body.md").read_text(encoding="utf-8")
    assert "a" * 8 in body and "admin-only endpoint" in body
    assert "Source: input" in body and "## Impact" in body
    assert "use(p)" not in body and "```" not in body
    assert "Source evidence" not in body and "Proposed fix" not in body
    store.save_draft(item, "Reviewed title", "Reviewed markdown\n")
    store.save_template("## New template\n$description\n")
    store.prepare(item)
    assert (store.case_dir(item) / "body.md").read_text() == "Reviewed markdown\n"
    assert json.loads((store.case_dir(item) / "title.json").read_text())["title"] == "Reviewed title"
    assert all((directory / name).read_bytes() == content for name, content in originals.items())
    assert not (directory / "control.json").exists()


@pytest.mark.parametrize("change", ["decision", "revision", "verdict"])
def test_changed_evidence_requires_requeue_and_keeps_old_draft(tmp_path, change):
    store, directory, job, row = setup_store(tmp_path)
    items, _ = store.candidates([directory])
    old = items[0]
    store.prepare(old)
    if change == "revision":
        (directory / "job.json").write_text(json.dumps({**job, "git_commit": "b" * 40}))
    else:
        write_rows(directory, [{**row, **({"comment": "changed"} if change == "decision" else {"verdict": "False Positive"})}])
    with pytest.raises(ValueError, match="изменились"):
        store.prepare(old)
    assert (store.case_dir(old) / "body.md").is_file()
    current, _ = store.candidates([directory])
    assert not current or current[0]["id"] != old["id"]


def test_queue_claim_prevents_duplicate_workers_and_dead_work_can_be_retried(tmp_path, monkeypatch):
    store, directory, _, _ = setup_store(tmp_path)
    items, _ = store.candidates([directory])
    store.enqueue(items, with_poc=False)
    monkeypatch.setattr("codex_run.process_is_alive", lambda pid: pid == os.getpid())
    assert store.claim(items[0]["id"])["status"] == "running"
    assert IssueStore(store.tool_root).claim(items[0]["id"]) is None
    with pytest.raises(ValueError, match="обрабатывается"):
        store.retry({items[0]["id"]})
    store.update(items[0]["id"], pid=-1)
    store.retry({items[0]["id"]})
    assert store.load_queue()[0]["status"] == "queued"


def test_poc_failure_keeps_markdown_and_does_not_call_publish(tmp_path, monkeypatch):
    store, directory, _, _ = setup_store(tmp_path)
    items, _ = store.candidates([directory])
    monkeypatch.setattr("poc_generation.existing_generations", lambda *a, **k: [])
    def fail(*a, **k):
        raise ValueError("missing callers")
    monkeypatch.setattr("poc_generation.generate_for_marker", fail)
    result = store.prepare({**items[0], "with_poc": True})
    assert result["status"] == "poc_error" and "missing callers" in result["message"]
    assert (Path(result["case"]) / "body.md").is_file()


def test_poc_attachment_verifies_files_and_decision_before_copying(tmp_path, monkeypatch):
    store, directory, _, row = setup_store(tmp_path)
    items, _ = store.candidates([directory])
    item = items[0]
    generation = directory.parent / "generated-poc" / "case" / "run-1"
    (generation / "poc").mkdir(parents=True)
    content = "print('local test')\n"
    poc = generation / "poc" / "test.py"
    poc.write_bytes(content.encode())
    (generation / "generation.json").write_text(json.dumps({"status": "generated_unverified", "files": [
        {"path": "poc/test.py", "sha256": digest(content.encode())}]}))
    monkeypatch.setattr("poc_generation.existing_generations", lambda *a, **k: [generation])
    attachment = store.poc_attachment(item)
    assert "not executed" in attachment and content.strip() in attachment
    poc.write_text("changed")
    with pytest.raises(ValueError, match="изменился"):
        store.poc_attachment(item)
    write_rows(directory, [{**row, "comment": "new evidence"}])
    with pytest.raises(ValueError, match="Решение изменилось"):
        store.poc_attachment(item)


def test_issue_reuses_poc_from_card_without_a_model_request(tmp_path, monkeypatch):
    import poc_generation as poc
    from issue_workspace import canonical
    store, directory, job, row = setup_store(tmp_path)
    items, _ = store.candidates([directory])
    generated = poc.marker_output_root(directory, row["marker_id"]) / "saved-run"
    generated.mkdir(parents=True)
    (generated / "generation.json").write_text(json.dumps({
        "marker_id": row["marker_id"], "source_revision": job["git_commit"],
        "decision_sha256": digest(canonical(row)), "status": "generated_unverified",
    }))
    monkeypatch.setattr(poc, "generate_for_marker", lambda *a, **k: pytest.fail("Existing PoC generated again"))
    result = store.prepare({**items[0], "with_poc": False})
    assert result["status"] == "poc_unverified" and result["poc"] == str(generated)


def test_unsafe_job_and_case_paths_and_unknown_template_are_rejected(tmp_path):
    store, _, _, _ = setup_store(tmp_path)
    with pytest.raises(ValueError):
        store.job(store.tool_root / "RESULTS" / ".." / "outside")
    with pytest.raises(ValueError):
        store.case_dir({"id": "../outside"})
    with pytest.raises(ValueError, match="Неизвестное поле"):
        store.save_template("$wrong_placeholder")


def make_window(tmp_path, monkeypatch):
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication
    from developer_issues_qt import QMessageBox
    from issue_brief import local_brief
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr("issue_brief.generate_brief", lambda job, row, fields: local_brief(fields))
    import triage_gui_qt as gui
    store, directory, _, row = setup_store(tmp_path)
    app_dir = store.tool_root / "app"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "read_local_mcp_token", lambda: "")
    monkeypatch.setattr(gui, "read_codex_rate_limits", lambda: {})
    monkeypatch.setattr(gui, "read_codex_models", lambda: [])
    monkeypatch.setattr(gui, "check_mcp", lambda *a: "offline")
    monkeypatch.setattr(gui.TriageQtWindow, "check_connection", lambda self: None)
    app = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
    app.setStyle("Fusion")
    app.setStyleSheet(gui.STYLE)
    window = gui.TriageQtWindow(directory, app_dir)
    window._initial_connection_prompted = True
    window.issues_tab.refresh(force=True)
    return app, window, row


def test_desktop_batch_and_edits_clipboard_export_are_persistent(tmp_path, monkeypatch):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from developer_issues_qt import QFileDialog
    app, window, row = make_window(tmp_path, monkeypatch)
    tab = window.issues_tab
    try:
        window.show()
        window.tabs.setCurrentWidget(tab)
        assert window.tabs.count() == 5
        write_rows(window.job, [row, {**row, "marker_id": "marker-2", "line": 43}])
        tab.refresh(force=True)
        tab.search.setText("no match")
        assert tab.confirmed_table.rowCount() == 0
        tab.enqueue(True)
        assert len(tab.store.load_queue()) == 2
        tab.start_queue()
        for _ in range(100):
            time.sleep(0.01)
            QTest.qWait(50)
            window.drain_task()
            if not tab.running:
                break
        assert not tab.running
        assert [item["status"] for item in tab.store.load_queue()] == ["draft", "draft"]
        tab.queue_table.selectRow(0)
        assert tab.editor.isEnabled()
        tab.title.setText("Human reviewed title")
        tab.editor.setPlainText("Reviewed content\n")
        tab.copy_body()
        assert QApplication.clipboard().text() == "Reviewed content\n"
        tab.queue_table.selectRow(1)
        tab.queue_table.selectRow(0)
        assert tab.title.text() == "Human reviewed title"
        assert tab.editor.toPlainText() == "Reviewed content\n"
        target = tmp_path / "issue.md"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(target), ""))
        tab.export_md()
        assert target.read_text() == "# Human reviewed title\n\nReviewed content\n"
        tab.scope.setCurrentIndex(1)
        assert len(tab.candidates) == 2
        saved_body = tab.store.case_dir(tab.current) / "body.md"
        tab.queue_table.clearSelection()
        tab.queue_table.selectRow(0)
        tab.remove_selected()
        assert not tab.editor.isEnabled() and tab.current is None
        assert saved_body.read_text() == "Reviewed content\n"
    finally:
        window.close()
        app.processEvents()


def test_cancel_poc_consent_keeps_queue_and_does_not_start_model(tmp_path, monkeypatch):
    from developer_issues_qt import QMessageBox
    app, window, _ = make_window(tmp_path, monkeypatch)
    tab = window.issues_tab
    try:
        tab.with_poc.setChecked(True)
        tab.enqueue(True)
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
        monkeypatch.setattr(window, "run_artifact", lambda *a, **k: pytest.fail("Cancelled batch started a worker"))
        tab.start_queue()
        assert not tab.running
        assert tab.store.load_queue()[0]["status"] == "queued"
        assert not (tab.store.case_dir(tab.store.load_queue()[0]) / "body.md").exists()
    finally:
        window.close()
        app.processEvents()


def test_a_stale_item_does_not_block_the_next_issue(tmp_path, monkeypatch):
    from PySide6.QtTest import QTest
    app, window, row = make_window(tmp_path, monkeypatch)
    tab = window.issues_tab
    try:
        second = {**row, "marker_id": "marker-2"}
        write_rows(window.job, [row, second])
        tab.refresh(force=True)
        tab.enqueue(True)
        write_rows(window.job, [{**row, "comment": "new evidence"}, second])
        tab.start_queue()
        for _ in range(100):
            time.sleep(0.01)
            QTest.qWait(30)
            window.drain_task()
            if not tab.running:
                break
        assert [item["status"] for item in tab.store.load_queue()] == ["error", "draft"]
        assert not tab.running
    finally:
        window.close()
        app.processEvents()
