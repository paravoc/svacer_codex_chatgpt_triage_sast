"""English wording tests with a fake CLI; no account or model is used."""
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from issue_brief import brief_prompt, validate_brief
from issue_workspace import report_fields
from test_issue_workspace import setup_store, write_rows


BRIEF = {
    "title": "[TEAM] CWE-476: NULL pointer dereference in handler (widget aaaaaaaa)",
    "description": "The handler dereferences a NULL result without checking it.",
    "reachability": "A local administrator must enable the affected module and invoke the handler.",
    "source": "lookup() returns NULL for a missing entry.",
    "sink": "handler() dereferences the returned pointer at x.go:42.",
    "impact": "The process can terminate. Arbitrary code execution has not been demonstrated.",
}


def test_russian_analysis_is_rewritten_to_bounded_english_without_source_excerpts(tmp_path, monkeypatch):
    store, directory, job, row = setup_store(tmp_path)
    row = {**row, "comment": "Обработчик разыменовывает NULL без проверки."}
    write_rows(directory, [row])
    item = store.candidates([directory])[0][0]
    def fake_run(command, **kwargs):
        prompt = kwargs["input"]
        assert "Обработчик" in prompt
        assert "use(p)" not in prompt and "source_evidence" not in prompt
        assert "root cause" in prompt and "undefined behavior into a proven crash" in prompt
        assert kwargs["cwd"].resolve() != directory.resolve()
        assert "read-only" in command
        (kwargs["cwd"] / "answer.json").write_text(json.dumps(BRIEF), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("codex_run.find_codex_executable", lambda: "fake-codex")
    monkeypatch.setattr("issue_brief.subprocess.run", fake_run)
    with pytest.raises(ValueError, match="английский"):
        store.prepare(item)  # no consent -> no implicit translation/network call
    result = store.prepare(item, english_via_codex=True)
    body = (Path(result["case"]) / "body.md").read_text(encoding="utf-8")
    assert not re.search(r"[\u0400-\u04ff]", body)
    assert len(body.split()) < 180 and "```" not in body
    assert re.findall(r"^## (.+)$", body, re.M) == ["Description", "Impact"]
    assert BRIEF["impact"] in body


@pytest.mark.parametrize("bad", [
    {**BRIEF, "impact": "Процесс падает."},
    {**BRIEF, "description": "```c\nptr->value\n```"},
    {**BRIEF, "description": "word " * 170},
])
def test_rejects_verbose_non_english_or_code_output(bad):
    with pytest.raises(ValueError):
        validate_brief(bad)


def test_rewriting_backs_up_existing_edits_and_keeps_analysis_unchanged(tmp_path, monkeypatch):
    store, directory, _, _ = setup_store(tmp_path)
    item = store.candidates([directory])[0][0]
    store.prepare(item)
    store.save_draft(item, "Original title", "Ручной черновик\n")
    before = (directory / "decisions.jsonl").read_bytes()
    monkeypatch.setattr("issue_brief.generate_brief", lambda *a: BRIEF)
    store.rewrite_brief(item)
    backup = next((store.case_dir(item) / "previous").iterdir())
    assert (backup / "body.md").read_text(encoding="utf-8") == "Ручной черновик\n"
    assert json.loads((backup / "title.json").read_text())["title"] == "Original title"
    assert (directory / "decisions.jsonl").read_bytes() == before
    assert BRIEF["description"] in (store.case_dir(item) / "body.md").read_text()


@pytest.mark.parametrize("changed", ["analysis", "editor"])
def test_changed_analysis_or_draft_is_not_overwritten_after_translation(tmp_path, monkeypatch, changed):
    store, directory, _, row = setup_store(tmp_path)
    item = store.candidates([directory])[0][0]
    store.prepare(item)
    body = store.case_dir(item) / "body.md"
    before = body.read_text()
    def fake(*a):
        if changed == "analysis":
            write_rows(directory, [{**row, "comment": "changed"}])
        else:
            store.save_draft(item, "New title", "New edits\n")
        return BRIEF
    monkeypatch.setattr("issue_brief.generate_brief", fake)
    with pytest.raises(ValueError, match="измен"):
        store.rewrite_brief(item)
    assert body.read_text() == (before if changed == "analysis" else "New edits\n")


def test_context_rejects_credential_shaped_text(tmp_path):
    _, _, job, row = setup_store(tmp_path)
    row = {**row, "comment": "token=ghp_" + "a" * 36}
    with pytest.raises(ValueError, match="секрет"):
        brief_prompt(job, row, report_fields(job, row))


def test_existing_draft_rewrite_button_refreshes_editor_and_cancellation_keeps_text(tmp_path, monkeypatch):
    from PySide6.QtCore import QEvent
    from developer_issues_qt import QMessageBox
    from test_artifact_parallel import spin
    from test_issue_workspace import make_window
    app, window, _ = make_window(tmp_path, monkeypatch)
    tab = window.issues_tab
    try:
        tab.enqueue(True)
        item = tab.store.load_queue()[0]
        tab.store.update(item["id"], **tab.store.prepare(item))
        tab.render_queue()
        tab.queue_table.selectRow(0)
        tab.editor.setPlainText("Original Russian draft\n")
        tab.save_editor()
        assert tab.brief_button.isEnabled()
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
        tab.rewrite_brief()
        assert tab.editor.toPlainText() == "Original Russian draft\n"
        assert not window.artifact_tasks.active
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
        monkeypatch.setattr("issue_brief.generate_brief", lambda *a: BRIEF)
        tab.rewrite_brief()
        spin(window, lambda: tab._rewriting_id is None and not window.artifact_tasks.active)
        assert tab.title.text() == BRIEF["title"]
        assert BRIEF["description"] in tab.editor.toPlainText()
        assert tab.editor.isEnabled() and not window.busy
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
