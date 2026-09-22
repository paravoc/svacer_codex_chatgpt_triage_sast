"""Project GUI and source pinning regressions; no live services or model calls."""
from concurrent.futures import Future
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run
import project_setup as setup
import project_setup_qt as gui
import triage_gui_qt as ui
import triage_queue as q
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from test_manual_queue_ui import manual_window  # noqa: F401


SHA = "a" * 40
SECOND = "b" * 40
TAG = {"kind": "tag", "name": "v2.11.2", "ref": "refs/tags/v2.11.2", "commit": SHA}
BRANCH = {"kind": "branch", "name": "release/2.11", "ref": "refs/heads/release/2.11", "commit": SECOND}
REPO = "https://github.com/nats-io/nats-server.git"
SCOPE = "https://svacer.example.test/mode/review/project/40711feb-63bc-4e6e-9d27-375e72cd9602/branch/77fd3c03-4161-4193-a8bf-5ecc45ee396d/snapshot/7de432a4-5a88-438c-89a0-e730c248020d"
SETTINGS = {"svacer_url": "https://svacer.example.test", "advanced_filter": q.GOST_FILTER,
            "verification_enabled": True, "verification_verdicts": ["Confirmed"],
            "parallel_workers": 1, "verification_workers": 1}
PROJECT_SCOPE = SCOPE.split("/branch/")[0]
BRANCH_SCOPE = SCOPE.split("/snapshot/")[0]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_missing_desktop_settings_receive_safe_workflow_defaults():
    completed = q.complete_desktop_settings({"svacer_url": "https://svacer.example.test"})

    assert completed["advanced_filter"] == q.GOST_FILTER
    assert completed["filter_name"] == "ГОСТ 71207-2024"
    assert completed["verification_enabled"] is True
    assert completed["verification_verdicts"] == ["Confirmed"]
    assert completed["mcp_url"] == q.DEFAULT_MCP_URL


def test_explicit_desktop_settings_are_not_replaced():
    completed = q.complete_desktop_settings({"parallel_workers": 3, "verification_workers": 2})

    assert completed["parallel_workers"] == 3
    assert completed["verification_workers"] == 2


def test_refs_peel_annotated_tags_deduplicate_and_sort_versions():
    rows = setup.parse_remote_refs("\n".join([
        f"{SHA}\trefs/tags/v2.9.1", f"{SHA}\trefs/tags/v2.11.2",
        f"{SECOND}\trefs/tags/v2.11.2^{{}}", f"{SHA}\trefs/tags/v2.11.2",
        f"{SECOND}\trefs/heads/v2.11.2", f"{SHA}\tHEAD", "bad\trefs/tags/oops",
    ]))
    assert [(r["name"], r["kind"]) for r in rows] == [("v2.11.2", "tag"), ("v2.9.1", "tag"), ("v2.11.2", "branch")]
    assert rows[0]["commit"] == SECOND  # Commit object, not the annotated tag object.
    assert rows[0]["ref"] != rows[2]["ref"]


@pytest.mark.parametrize("url", ["-x", "file:///tmp/repo", "ext::bad", "https://user:fake@example.test/repo",
                                "https://example.test/repo?token=fake", "https://example.test/repo#secret",
                                "https://example.test/repo\ncommand", "ssh://other@example.test/repo"])
def test_reject_unsafe_repository_urls(url):
    with pytest.raises(ValueError):
        setup.repository_url(url)


def test_readonly_git_discovery_hides_terminal_and_limits_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(setup.shutil, "which", lambda _: "git.exe")
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=f"{SHA}\trefs/tags/v2.11.2\n", stderr="")
    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.list_remote_refs(REPO) == [TAG]
    command, kwargs = calls[0]
    assert command == ["git.exe", "ls-remote", "--tags", "--heads", "--", REPO]
    assert kwargs["timeout"] == 30
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["env"]["GCM_INTERACTIVE"] == "Never"
    assert "BatchMode=yes" in kwargs["env"]["GIT_SSH_COMMAND"]
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "fake-sensitive-detail"))
    with pytest.raises(ValueError) as err:
        setup.list_remote_refs(REPO)
    assert "fake-sensitive-detail" not in str(err.value)


def test_git_discovery_timeout(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda _: "git")
    def run(*a, **k):
        raise subprocess.TimeoutExpired("git", 30)
    monkeypatch.setattr(setup.subprocess, "run", run)
    with pytest.raises(ValueError, match="30 секунд"):
        setup.list_remote_refs(REPO)


def test_public_github_repository_search_is_bounded_and_sends_only_query(monkeypatch):
    payload = {"items": [
        {"full_name": "openresty/openresty", "clone_url": "https://github.com/openresty/openresty.git",
         "description": "A platform powered by LuaJIT", "stargazers_count": 15000,
         "language": "C", "private": False, "fork": False, "archived": False, "disabled": False},
        {"full_name": "LuaJIT/LuaJIT", "clone_url": "https://github.com/LuaJIT/LuaJIT.git",
         "description": "Mirror of the LuaJIT git repository", "stargazers_count": 5200,
         "language": "C", "private": False, "fork": False, "archived": False, "disabled": False},
        {"full_name": "private/repo", "clone_url": "https://github.com/private/repo.git",
         "private": True},
        {"full_name": "evil/repo", "clone_url": "https://evil.example/repo.git"},
    ]}
    seen = {}

    def open_url(request, timeout):
        seen.update(url=request.full_url, timeout=timeout, headers=dict(request.header_items()))
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(setup, "urlopen", open_url)
    rows = setup.search_public_github_repositories("  luajit  ", limit=12)
    assert rows[0] == {
        "name": "LuaJIT/LuaJIT", "url": "https://github.com/LuaJIT/LuaJIT.git",
        "description": "Mirror of the LuaJIT git repository", "stars": 5200, "language": "C",
    }
    assert [row["name"] for row in rows] == ["LuaJIT/LuaJIT", "openresty/openresty"]
    query = parse_qs(urlsplit(seen["url"]).query)
    assert query["q"] == ["luajit in:name,description fork:false archived:false"]
    assert query["sort"] == ["stars"] and query["per_page"] == ["12"]
    assert seen["timeout"] == 15 and "Authorization" not in seen["headers"]
    assert seen["headers"]["User-agent"] == "Svacer-Triage/1.0"


@pytest.mark.parametrize("query", ["", "x", "lua\njit", "x" * 101])
def test_public_github_repository_search_rejects_bad_queries(query):
    with pytest.raises(ValueError):
        setup.repository_search_query(query)


def test_public_github_repository_search_hides_error_body(monkeypatch):
    def fail(request, timeout):
        raise setup.HTTPError(request.full_url, 403, "fake-sensitive-detail", {},
                              io.BytesIO(b"fake-sensitive-body"))

    monkeypatch.setattr(setup, "urlopen", fail)
    with pytest.raises(ValueError, match="ограничил частоту") as error:
        setup.search_public_github_repositories("luajit")
    assert "fake-sensitive" not in str(error.value)


def test_create_project_pins_ref_and_keeps_empty_manual_queue(tmp_path):
    job = setup.create_project(tmp_path, SETTINGS, SCOPE + "/filter/anything/marker/anything", REPO, TAG)
    data = read(job / "job.json")
    assert data["snapshot_url"] == SCOPE
    assert data["git_ref"] == "v2.11.2" and data["git_ref_full"] == TAG["ref"]
    assert data["git_commit"] == SHA and data["manual_selection_only"] is True
    assert data["advanced_filter"] == q.GOST_FILTER
    assert data["verification_enabled"] and data["verification_verdicts"] == ["Confirmed"]
    assert not (job / "control.json").exists() and not (job / "codex-run.json").exists()
    assert (job / "raw").is_dir() and (job / "notes").is_dir()
    tool_dir, prompt = codex_run._job_paths(job)
    assert tool_dir == tmp_path.resolve() and prompt.read_text(encoding="utf-8").strip()


def test_create_project_repairs_first_login_minimal_settings(tmp_path):
    minimal = {"svacer_url": "https://svacer.example.test", "mcp_url": q.DEFAULT_MCP_URL}

    job = setup.create_project(tmp_path, minimal, SCOPE, REPO, TAG)
    data = read(job / "job.json")

    assert data["advanced_filter"] == q.GOST_FILTER
    assert data["verification_enabled"] is True
    assert data["verification_verdicts"] == ["Confirmed"]


@pytest.mark.parametrize("scope", [SCOPE.replace("svacer.example.test", "wrong.test"),
                                  SCOPE + "?token=fake", "https://svacer.example.test/not-a-snapshot"])
def test_invalid_scope_creates_no_job(tmp_path, scope):
    with pytest.raises(ValueError):
        setup.create_project(tmp_path, SETTINGS, scope, REPO, TAG)
    assert not list(tmp_path.iterdir())


def test_update_failed_source_preserves_queue_and_backups(tmp_path):
    job = setup.create_project(tmp_path, SETTINGS, SCOPE, REPO, TAG)
    original = read(job / "job.json")
    original.pop("git_commit")
    original["git_ref"] = "2.11.2"
    q.atomic_write_json(job / "job.json", original)
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ["m1", "m2"]})
    before = (job / "control.json").read_bytes()
    setup.update_project_source(job, tmp_path, REPO, TAG)
    assert read(job / "job.json")["git_ref"] == "v2.11.2"
    assert (job / "control.json").read_bytes() == before
    backups = list((job / "source-settings-backups").glob("*.json"))
    assert len(backups) == 1 and read(backups[0]) == original


@pytest.mark.parametrize("evidence", ["revision.txt", "notes/batch.json", "raw/trace.json", "svacer-import-attempt.json"])
def test_source_change_refuses_existing_evidence(tmp_path, evidence):
    job = setup.create_project(tmp_path, SETTINGS, SCOPE, REPO, TAG)
    (job / evidence).write_text("{}", encoding="utf-8")
    before = (job / "job.json").read_bytes()
    with pytest.raises(ValueError, match="новый проект"):
        setup.update_project_source(job, tmp_path, REPO, BRANCH)
    assert (job / "job.json").read_bytes() == before


def test_source_change_refuses_active_job(tmp_path, monkeypatch):
    job = setup.create_project(tmp_path, SETTINGS, SCOPE, REPO, TAG)
    monkeypatch.setattr(codex_run, "read_run_record", lambda _: {"active": True})
    with pytest.raises(ValueError, match="завершите анализ"):
        setup.update_project_source(job, tmp_path, REPO, BRANCH)


@pytest.fixture
def dialog(manual_window):
    parent, job, _, _, app = manual_window
    dlg = gui.ProjectSetupDialog(parent, job.parent.parent, SETTINGS)
    try:
        dlg.show()
        app.processEvents()
        yield dlg
    finally:
        dlg.reject()
        dlg.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def set_catalog(dlg):
    dlg.snapshot.setText(SCOPE)
    dlg.repository.setText(REPO)
    dlg.loaded_url, dlg.refs = REPO, [TAG, BRANCH]
    dlg.filter_refs()


def test_gui_explicit_tag_selection_creates_project_without_analysis(dialog):
    set_catalog(dialog)
    assert dialog.ref_list.currentRow() == -1  # Never silently choose latest version.
    assert not dialog.save_button.isEnabled()
    dialog.search.setText("2.11.2")
    assert dialog.ref_list.count() == 1
    dialog.ref_list.setCurrentRow(0)
    assert SHA in dialog.selected_hint.text()
    assert not dialog.save_button.isEnabled()
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled()
    dialog.save_project()
    assert dialog.result() == gui.QDialog.DialogCode.Accepted
    assert read(dialog.created_job / "job.json")["git_commit"] == SHA
    assert not (dialog.created_job / "codex-run.json").exists()


def test_gui_stale_network_response_is_ignored_and_can_retry(dialog, monkeypatch):
    dialog.repository.setText(REPO)
    future = Future()
    monkeypatch.setattr(dialog.executor, "submit", lambda *a: future)
    dialog.load_refs()
    assert dialog.progress.isVisible() and not dialog.load_button.isEnabled()
    assert dialog.isVisible()
    dialog.repository.setText("https://github.com/example/other.git")
    future.set_result([TAG])
    dialog.drain_refs()
    assert not dialog.refs and dialog.ref_list.count() == 0
    assert dialog.load_button.isEnabled() and not dialog.save_button.isEnabled()
    assert "URL изменён" in dialog.message.text()


def test_gui_keyword_search_selects_repository_in_compact_frame(dialog, monkeypatch):
    rows = [{
        "name": f"LuaJIT/project-{index}",
        "url": f"https://github.com/LuaJIT/project-{index}.git",
        "description": "LuaJIT implementation", "stars": 5000 - index, "language": "C",
    } for index in range(12)]
    future = Future()
    future.set_result(rows)
    monkeypatch.setattr(dialog.executor, "submit", lambda *_args: future)
    initial_height = dialog.repository_frame.height()
    dialog.repository_query.setText("luajit")
    dialog.search_repositories()
    assert not dialog.repository_search_button.isEnabled()
    dialog.drain_repository_search()
    QApplication.instance().processEvents()
    assert dialog.repository_search_button.isEnabled()
    assert dialog.repository_list.count() == 12
    assert "Найдено: 12" in dialog.repository_count.text()
    assert dialog.repository_frame.height() == initial_height == 150
    assert not dialog.repository_list.isWindow()
    assert dialog.repository_list.parentWidget() is dialog.repository_frame
    assert dialog.repository_list.verticalScrollBar().maximum() > 0
    dialog.repository_list.setCurrentRow(0)
    assert dialog.repository.text() == "https://github.com/LuaJIT/project-0.git"
    assert "Выбран LuaJIT/project-0" in dialog.message.text()
    assert not dialog.refs and not dialog.loaded_url
    assert dialog.created_job is None


def test_gui_stale_repository_search_is_ignored_even_if_query_becomes_empty(dialog, monkeypatch):
    future = Future()
    monkeypatch.setattr(dialog.executor, "submit", lambda *_args: future)
    dialog.repository_query.setText("luajit")
    dialog.search_repositories()
    dialog.repository_query.clear()
    future.set_result([{"name": "LuaJIT/LuaJIT", "url": "https://github.com/LuaJIT/LuaJIT.git",
                        "description": "", "stars": 1, "language": "C"}])
    dialog.drain_repository_search()
    assert dialog.repository_list.count() == 0
    assert "Фраза изменена" in dialog.repository_count.text()


def test_gui_network_failure_does_not_create_empty_job(dialog, monkeypatch):
    dialog.repository.setText(REPO)
    future = Future()
    future.set_exception(ValueError("Нет доступа"))
    monkeypatch.setattr(dialog.executor, "submit", lambda *a: future)
    dialog.load_refs()
    dialog.drain_refs()
    assert "Нет доступа" in dialog.message.text()
    assert dialog.created_job is None and not dialog.save_button.isEnabled()


def test_gui_cancel_pending_request_never_saves(dialog, monkeypatch):
    dialog.repository.setText(REPO)
    future = Future()
    monkeypatch.setattr(dialog.executor, "submit", lambda *a: future)
    dialog.load_refs()
    dialog.reject()
    future.set_result([TAG])
    assert not dialog.timer.isActive()
    assert dialog.created_job is None


def test_gui_branch_manual_commit_and_url_change_revoke_confirmation(dialog):
    set_catalog(dialog)
    dialog.kind.setCurrentIndex(1)
    dialog.ref_list.setCurrentRow(0)
    dialog.confirm.setChecked(True)
    assert dialog.selection() == BRANCH and dialog.save_button.isEnabled()
    dialog.repository.setText("https://github.com/example/other.git")
    assert not dialog.confirm.isChecked() and not dialog.save_button.isEnabled()
    dialog.kind.setCurrentIndex(2)
    dialog.commit.setText("abc123")
    dialog.confirm.setChecked(True)
    assert not dialog.save_button.isEnabled()
    dialog.commit.setText(SHA)
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled()
    assert "до запуска модели" in dialog.selected_hint.text()


def test_tag_results_are_embedded_bounded_and_scrollable(dialog):
    set_catalog(dialog)
    before = dialog.ref_frame.height()
    dialog.refs = [
        {**TAG, "name": f"v2.{minor}.{patch}", "ref": f"refs/tags/v2.{minor}.{patch}"}
        for minor in (10, 11, 12) for patch in range(100)
    ]
    dialog.filter_refs()
    QApplication.instance().processEvents()
    assert dialog.ref_list.count() == 300
    assert dialog.ref_frame.height() == before == 166
    assert not dialog.ref_list.isWindow()
    assert dialog.ref_list.parentWidget() is dialog.ref_frame
    assert dialog.ref_list.verticalScrollBar().maximum() > 0
    dialog.search.setText("2.11")
    QApplication.instance().processEvents()
    assert dialog.ref_list.count() == 100
    assert "100 из 300" in dialog.ref_count.text()
    assert dialog.ref_frame.height() == before
    assert all("2.11" in dialog.ref_list.item(i).text() for i in range(100))
    item = dialog.ref_list.item(99)
    dialog.ref_list.setCurrentItem(item)
    dialog.ref_list.scrollToItem(item)
    assert dialog.selection()["name"] == "v2.11.99"
    assert dialog.ref_list.verticalScrollBar().value() > 0
    assert SHA in item.toolTip()
    QApplication.instance().processEvents()
    assert dialog.selected_hint.y() > dialog.ref_frame.geometry().bottom()


def test_version_search_matches_version_prefix_not_patch_suffix():
    for name in ("v2.11.2", "2.11.1-RC1", "release/v2.11.0", "nats-v2.11.2"):
        assert gui.ref_matches_query(name, "2.11")
    assert not gui.ref_matches_query("v2.12.11", "2.11")
    assert gui.ref_matches_query("v2.11.0-RC1", "rc1")
    assert gui.ref_matches_query("v2.11.2", " V2.11 ")


def test_project_dialog_uses_large_screen_bounded_default_size():
    assert gui.preferred_project_dialog_size(1920, 1080) == gui.QSize(1320, 1000)
    assert gui.preferred_project_dialog_size(1280, 720) == gui.QSize(1232, 672)
    assert gui.preferred_project_dialog_size(800, 600) == gui.QSize(752, 552)


def test_search_removes_hidden_selection_and_empty_state_is_clear(dialog):
    set_catalog(dialog)
    dialog.ref_list.setCurrentRow(0)
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled()
    dialog.search.setText("nonexistent")
    assert dialog.ref_list.count() == 0 and dialog.selection() is None
    assert "Совпадений нет" in dialog.ref_count.text()
    assert not dialog.confirm.isChecked() and not dialog.save_button.isEnabled()
    dialog.search.clear()
    assert dialog.ref_list.count() == 1 and dialog.ref_list.currentRow() == -1
    dialog.kind.setCurrentIndex(2)
    assert dialog.ref_frame.isHidden() and not dialog.commit.isHidden()


@pytest.fixture
def new_project_flow(manual_window, monkeypatch):
    window, old_job, _, _, _ = manual_window
    window.settings = dict(SETTINGS)
    payload = {
        "markers": [
            {"id": "reviewed", "warnClass": "NULL", "file": "source.go", "line": 1,
             "review": {"status": "False Positive"}},
            {"id": "unreviewed", "warnClass": "NULL", "file": "source.go", "line": 2},
        ],
        "total_count": 2, "returned_count": 2, "truncated": False,
        "filters_applied": {"advanced_filter": q.GOST_FILTER},
    }
    calls, submitted = [], []

    async def get_markers(_url, _token, tool, args):
        calls.append((tool, args))
        return json.dumps(payload)

    # Keep the worker pending until the test explicitly completes it: GUI event
    # handling must remain usable without any real network/model calls.
    def submit(work):
        future = Future()
        submitted.append((future, work))
        return future

    def finish():
        future, work = submitted[-1]
        try:
            result = work()
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        window.drain_task()

    def complete_dialog(dlg):
        set_catalog(dlg)
        dlg.ref_list.setCurrentRow(0)
        dlg.confirm.setChecked(True)
        dlg.save_project()
        return dlg.result()

    monkeypatch.setattr(gui.ProjectSetupDialog, "exec", complete_dialog)
    monkeypatch.setattr(ui, "call_mcp_tool", get_markers)
    monkeypatch.setattr(ui, "check_mcp", lambda *_a: "подключён")
    monkeypatch.setattr(window._task_executor, "submit", submit)
    return window, old_job, payload, calls, submitted, finish


def test_new_project_automatically_loads_all_markers_without_analysis(new_project_flow):
    window, old_job, _, calls, submitted, finish = new_project_flow
    before = {path: path.read_bytes() for path in old_job.rglob("*") if path.is_file()}
    window.open_new_job_wizard()
    assert window.job != old_job
    assert read(window.job / "job.json")["git_commit"] == SHA
    assert "v2.11.2" in window.job_combo.currentText()
    assert window.busy and not window.fetch_button.isEnabled()
    assert len(submitted) == 1 and not calls
    assert "Автоматически" in window.status.text()
    # Repeated clicks during loading must not create a second task or request.
    window.open_new_job_wizard()
    window.fetch_markers()
    assert len(submitted) == 1
    QApplication.instance().processEvents()
    assert window.isVisible() and window.busy
    finish()
    assert not window.busy and window._task_future is None and window._task_failed is None
    assert window.fetch_button.isEnabled()
    assert window.fetch_button.text() == "Обновить маркеры"
    assert window.tabs.currentWidget() is window.markers_tab
    assert len(calls) == 1 and calls[0][0] == "get_markers"
    args = calls[0][1]
    assert args["project_id"] == "40711feb-63bc-4e6e-9d27-375e72cd9602"
    assert args["branch_id"] == "77fd3c03-4161-4193-a8bf-5ecc45ee396d"
    assert args["snapshot_id"] == "7de432a4-5a88-438c-89a0-e730c248020d"
    assert args["advanced_filter"] == q.GOST_FILTER and args["limit"] == 0
    assert "review" not in args
    assert not args["review_history"] and not args["comment_history"]
    assert not args["traces"]
    assert {m["id"] for m in read(window.job / "markers.inventory.json")["markers"]} == {"reviewed", "unreviewed"}
    assert window.marker_table.rowCount() == 2 and window.queue_table.rowCount() == 0
    assert all(row["verdict"] is None for row in q.load_decisions(window.job / "decisions.jsonl"))
    assert not (window.job / "control.json").exists()
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not (window.job / "codex-run.json").exists()


@pytest.mark.parametrize("failure", ["offline", "truncated", "server_error"])
def test_automatic_marker_load_failure_keeps_project_and_allows_retry(new_project_flow, monkeypatch, failure):
    window, old_job, payload, calls, submitted, finish = new_project_flow
    before = (old_job / "job.json").read_bytes()
    with monkeypatch.context() as errors:
        if failure == "offline":
            errors.setattr(ui, "check_mcp", lambda *_a: "не подключён")
        elif failure == "truncated":
            payload["truncated"] = True
        else:
            async def fail(*_a):
                raise RuntimeError("Сервис временно недоступен")
            errors.setattr(ui, "call_mcp_tool", fail)
        window.open_new_job_wizard()
        finish()
    created = window.job
    assert created != old_job and (created / "job.json").is_file()
    assert "Проект сохранён" in window.status.text()
    assert "Получить маркеры" in window.status.text()
    assert not window.busy and window.fetch_button.isEnabled()
    assert not (created / "markers.inventory.json").exists()
    assert not (created / "decisions.jsonl").exists()
    assert not (created / "codex-run.json").exists()
    payload["truncated"] = False
    window.fetch_button.click()
    assert len(submitted) == 2 and window.busy
    finish()
    assert window.job == created and window.marker_table.rowCount() == 2
    assert window.queue_table.rowCount() == 0 and "Маркеры загружены: 2" in window.status.text()
    assert (old_job / "job.json").read_bytes() == before


def test_cancelled_new_project_does_not_load_markers(new_project_flow, monkeypatch):
    window, old_job, _, calls, submitted, _ = new_project_flow
    monkeypatch.setattr(gui.ProjectSetupDialog, "exec", lambda _dlg: gui.QDialog.DialogCode.Rejected)
    window.open_new_job_wizard()
    assert window.job == old_job
    assert not calls and not submitted and not window.busy


def test_automatic_empty_inventory_is_success_not_error(new_project_flow):
    window, _, payload, calls, _, finish = new_project_flow
    payload.update(markers=[], total_count=0, returned_count=0)
    window.open_new_job_wizard()
    finish()
    assert len(calls) == 1 and (window.job / "markers.inventory.json").is_file()
    assert "в этом снимке маркеров нет" in window.status.text()
    assert window.fetch_button.text() == "Обновить маркеры"
    assert window.marker_table.rowCount() == window.queue_table.rowCount() == 0


@pytest.mark.parametrize("kind", ["branch", "commit"])
def test_project_only_url_explains_disabled_button_even_with_valid_git_revision(dialog, kind):
    set_catalog(dialog)
    dialog.kind.setCurrentIndex(1 if kind == "branch" else 2)
    if kind == "branch":
        dialog.ref_list.setCurrentRow(0)
        assert "последний commit" in dialog.selected_hint.text()
    else:
        dialog.commit.setText(SHA)
    dialog.snapshot.setText(PROJECT_SCOPE)
    dialog.confirm.setChecked(True)
    assert not dialog.save_button.isEnabled()
    assert "только на проект" in dialog.validation.text()
    assert "Выбрать снимок" in dialog.save_button.toolTip()
    dialog.snapshot.setText(SCOPE)
    assert not dialog.confirm.isChecked()  # A new snapshot needs new confirmation.
    assert "галочкой" in dialog.validation.text()
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled() and dialog.validation.isHidden()
    dialog.save_project()
    assert read(dialog.created_job / "job.json")["git_ref_kind"] == kind


@pytest.mark.parametrize("sha", [SHA, SHA.upper(), "  " + SHA + "  ", "f" * 64])
def test_manual_commit_creates_without_loading_git_refs(dialog, sha):
    dialog.snapshot.setText(SCOPE)
    dialog.repository.setText(REPO)
    dialog.kind.setCurrentIndex(2)
    dialog.commit.setText(sha)
    dialog.confirm.setChecked(True)
    assert not dialog.refs and dialog.save_button.isEnabled()
    dialog.save_project()
    assert read(dialog.created_job / "job.json")["git_commit"] == sha.strip().lower()


@pytest.mark.parametrize("sha", ["", "c6ffc141", "g" * 40, "a" * 39, "a" * 41, "a" * 63])
def test_manual_invalid_commit_gives_specific_reason(dialog, sha):
    dialog.snapshot.setText(SCOPE)
    dialog.repository.setText(REPO)
    dialog.kind.setCurrentIndex(2)
    dialog.commit.setText(sha)
    dialog.confirm.setChecked(True)
    assert not dialog.save_button.isEnabled()
    assert "40 или 64" in dialog.validation.text()
    assert dialog.created_job is None


@pytest.mark.parametrize("refs,initial_kind,expected_kind", [([BRANCH], 0, 1), ([TAG], 1, 0), ([], 0, 0)])
def test_no_tags_no_branches_and_empty_repository(dialog, monkeypatch, refs, initial_kind, expected_kind):
    dialog.repository.setText(REPO)
    dialog.kind.setCurrentIndex(initial_kind)
    dialog.search.setText("old-version")
    future = Future()
    future.set_result(refs)
    monkeypatch.setattr(dialog.executor, "submit", lambda *_a: future)
    dialog.load_refs()
    dialog.drain_refs()
    assert dialog.kind.currentIndex() == expected_kind
    assert dialog.selection() is None and not dialog.save_button.isEnabled()
    if refs:
        assert dialog.ref_list.count() == 1 and not dialog.search.text()
    else:
        assert "пустым" in dialog.message.text()
    # A manually known SHA remains usable even when the refs list is empty.
    dialog.snapshot.setText(SCOPE)
    dialog.kind.setCurrentIndex(2)
    dialog.commit.setText(SHA)
    dialog.confirm.setChecked(True)
    assert dialog.save_button.isEnabled()


@pytest.fixture
def scope_requests(dialog, monkeypatch):
    scope = setup.scope_fields(SCOPE, SETTINGS["svacer_url"])
    responses = {
        "branch": [{"project_id": scope["project_id"], "branches": [
            {"branch_id": scope["branch_id"], "branch_name": "v2.1"}]}],
        "snapshot": [{"snapshot_id": scope["snapshot_id"], "name": "Svace build", "commit_hash": SHA}],
    }
    calls, pending = [], []
    def load(kind, scope):
        calls.append((kind, dict(scope)))
        return responses[kind]
    def submit(work):
        future = Future()
        pending.append((future, work))
        return future
    def finish():
        future, work = pending[-1]
        try:
            future.set_result(work())
        except Exception as exc:
            future.set_exception(exc)
        dialog.drain_scope()
    dialog.scope_loader = load
    monkeypatch.setattr(dialog.executor, "submit", submit)
    set_catalog(dialog)
    dialog.ref_list.setCurrentRow(0)
    dialog.snapshot.setText(PROJECT_SCOPE)
    return dialog, responses, calls, pending, finish


def test_project_link_selects_explicit_svacer_branch_and_snapshot(scope_requests):
    dlg, _, calls, pending, finish = scope_requests
    dlg.load_scope()
    assert len(pending) == 1 and not dlg.save_button.isEnabled()
    finish()
    assert calls[0][0] == "branch"
    assert dlg.svacer_branch.currentIndex() == 0 and dlg.svacer_snapshot.count() == 0
    dlg.svacer_branch.setCurrentIndex(1)
    assert dlg.snapshot.text() == BRANCH_SCOPE
    assert len(pending) == 2
    finish()
    assert calls[1][0] == "snapshot"
    assert dlg.svacer_snapshot.currentIndex() == 0  # Never silently choose latest.
    assert not dlg.save_button.isEnabled()
    dlg.svacer_snapshot.setCurrentIndex(1)
    assert dlg.snapshot.text() == SCOPE and SHA in dlg.scope_status.text()
    assert not dlg.confirm.isChecked()
    dlg.confirm.setChecked(True)
    assert dlg.save_button.isEnabled()
    dlg.save_project()
    assert read(dlg.created_job / "job.json")["snapshot_id"] == SCOPE.rsplit("/", 1)[1]


def test_branch_url_reuses_only_explicit_branch_not_latest_snapshot(scope_requests):
    dlg, _, _, pending, finish = scope_requests
    dlg.snapshot.setText(BRANCH_SCOPE)
    dlg.load_scope()
    finish()
    assert dlg.svacer_branch.currentIndex() == 1 and len(pending) == 2
    finish()
    assert dlg.svacer_snapshot.currentIndex() == 0 and dlg.snapshot.text() == BRANCH_SCOPE


def test_scope_response_for_changed_url_is_discarded(scope_requests):
    dlg, _, _, _, finish = scope_requests
    dlg.load_scope()
    dlg.snapshot.setText(PROJECT_SCOPE.replace("40711feb", "11111111"))
    finish()
    assert dlg.svacer_branch.count() == 0 and dlg.scope_future is None
    assert dlg.scope_button.isEnabled() and not dlg.save_button.isEnabled()


def test_scope_lookup_failure_and_empty_snapshots_allow_retry(scope_requests):
    dlg, responses, _, _, finish = scope_requests
    original = responses["branch"]
    responses["branch"] = []
    dlg.load_scope()
    finish()
    assert "не найден" in dlg.scope_status.text() and dlg.scope_button.isEnabled()
    responses["branch"] = original
    dlg.load_scope()
    finish()
    responses["snapshot"] = []
    dlg.svacer_branch.setCurrentIndex(1)
    finish()
    assert "нет снимков" in dlg.scope_status.text()
    assert not dlg.save_button.isEnabled() and dlg.created_job is None


def test_cancelled_scope_lookup_cannot_change_form(scope_requests):
    dlg, _, _, _, finish = scope_requests
    dlg.load_scope()
    dlg.reject()
    finish()
    assert not dlg.scope_timer.isActive() and dlg.created_job is None
    assert dlg.svacer_branch.count() == 0


def test_small_window_keeps_actions_and_reason_visible(dialog):
    set_catalog(dialog)
    dialog.snapshot.setText(PROJECT_SCOPE)
    dialog.resize(620, 520)
    QApplication.instance().processEvents()
    assert dialog.save_button.geometry().bottom() < dialog.height()
    assert dialog.validation.geometry().bottom() < dialog.save_button.geometry().top()
    assert dialog.form_scroll.verticalScrollBar().maximum() > 0
    assert not dialog.validation.isHidden()


@pytest.mark.parametrize("url", [PROJECT_SCOPE + "/branch/bad", BRANCH_SCOPE + "/snapshot/bad",
                               PROJECT_SCOPE + "/snapshot/" + SCOPE.rsplit("/", 1)[1]])
def test_malformed_partial_scope_is_not_accepted_as_project(url):
    with pytest.raises(ValueError, match="некорректный UUID"):
        setup.scope_fields(url, SETTINGS["svacer_url"])


def test_scope_lookup_uses_only_readonly_tools(manual_window, monkeypatch):
    window, *_ = manual_window
    calls = []
    async def call(_url, _token, tool, args):
        calls.append((tool, args))
        return "[]"
    monkeypatch.setattr(ui, "check_mcp", lambda *_a: "подключён")
    monkeypatch.setattr(ui, "call_mcp_tool", call)
    scope = setup.scope_fields(SCOPE, SETTINGS["svacer_url"])
    assert window.load_project_scope("branch", scope) == []
    assert window.load_project_scope("snapshot", scope) == []
    assert calls == [("get_projects", {}), ("get_snapshots", {
        "project_id": scope["project_id"], "branch_id": scope["branch_id"]})]
    with pytest.raises(ValueError):
        window.load_project_scope("apply_markup_import", scope)
    monkeypatch.setattr(ui, "check_mcp", lambda *_a: "не подключён")
    with pytest.raises(ValueError, match="не подключён"):
        window.load_project_scope("branch", scope)
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [{}, [None], [{"snapshot_id": "bad"}],
                                    [{"snapshot_id": "7de432a4-5a88-438c-89a0-e730c248020d"}] * 2])
def test_invalid_scope_catalog_is_rejected(payload):
    with pytest.raises(ValueError):
        setup.scope_options(payload, "snapshot", "40711feb-63bc-4e6e-9d27-375e72cd9602")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def local_repository(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    (origin / "code.txt").write_text("test source", encoding="utf-8")
    git(origin, "add", "code.txt")
    git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    commit = git(origin, "rev-parse", "HEAD")
    git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "tag.gpgsign=false", "tag", "-a", "v2.11.2", "-m", "fixture")
    job = tmp_path / "job"
    job.mkdir()
    return origin, job, commit


def test_prepare_repository_pinned_tag_and_reuse_exact_commit(local_repository):
    origin, job, commit = local_repository
    data = {"repository_url": str(origin), "git_ref": "v2.11.2", "git_ref_full": "refs/tags/v2.11.2", "git_commit": commit}
    repository, actual = codex_run.prepare_repository(job, data, "test")
    assert actual == commit and (job / "revision.txt").read_text(encoding="utf-8").strip() == commit
    assert git(repository, "config", "--get", "core.longpaths") == "true"
    # Even after a remote tag disappears, a pinned checkout remains reproducible.
    git(origin, "tag", "-d", "v2.11.2")
    assert codex_run.prepare_repository(job, data, "test")[1] == commit
    assert git(repository, "rev-parse", "HEAD") == commit


def test_prepare_repository_legacy_job_reuses_saved_revision_without_refetch(local_repository, monkeypatch):
    origin, job, commit = local_repository
    data = {"repository_url": str(origin), "git_ref": "v2.11.2"}
    repository, actual = codex_run.prepare_repository(job, data, "test")
    assert actual == commit and (job / "revision.txt").read_text(encoding="utf-8").strip() == commit

    # Legacy shallow checkouts can contain the commit but no local tag.  The
    # saved revision must keep subsequent batches network-free even if the
    # original remote tag is no longer available.
    subprocess.run(["git", "-C", str(repository), "tag", "-d", "v2.11.2"],
                   check=False, capture_output=True, text=True, encoding="utf-8")
    git(origin, "tag", "-d", "v2.11.2")

    def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("a prepared legacy job must not fetch the tag again")

    monkeypatch.setattr(codex_run, "_fetch_repository_ref", unexpected_fetch)
    assert codex_run.prepare_repository(job, data, "test")[1] == commit
    assert git(repository, "rev-parse", "HEAD") == commit


def test_prepare_repository_wrong_pin_rejects_before_use(local_repository):
    origin, job, _ = local_repository
    with pytest.raises(RuntimeError, match="другой commit"):
        codex_run.prepare_repository(job, {"repository_url": str(origin), "git_ref": "refs/tags/v2.11.2", "git_commit": SHA}, "test")
    assert not (job / "revision.txt").exists() and not (job / "repository").exists()
    assert not list(job.glob("repository.clone-*"))


def test_prepare_repository_missing_version_points_to_gui(local_repository):
    origin, job, _ = local_repository
    with pytest.raises(RuntimeError, match="Выбрать Git-тег"):
        codex_run.prepare_repository(job, {"repository_url": str(origin), "git_ref": "2.11.2"}, "test")
    assert not (job / "revision.txt").exists()


def test_branch_advance_keeps_commit_chosen_by_user(local_repository):
    origin, job, selected = local_repository
    branch = git(origin, "symbolic-ref", "HEAD")
    (origin / "code.txt").write_text("newer source, not the selected snapshot", encoding="utf-8")
    git(origin, "add", "code.txt")
    git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-m", "branch advanced")
    assert git(origin, "rev-parse", "HEAD") != selected
    data = {"repository_url": str(origin), "git_ref_full": branch,
            "git_ref_kind": "branch", "git_commit": selected}
    repository, actual = codex_run.prepare_repository(job, data, "test")
    assert actual == selected
    assert (repository / "code.txt").read_text(encoding="utf-8") == "test source"
    assert codex_run.prepare_repository(job, data, "test")[1] == selected


def test_manual_commit_does_not_require_tag(local_repository):
    origin, job, selected = local_repository
    git(origin, "tag", "-d", "v2.11.2")
    data = {"repository_url": str(origin), "git_ref": selected,
            "git_ref_kind": "commit", "git_commit": selected}
    assert codex_run.prepare_repository(job, data, "test")[1] == selected


@pytest.mark.parametrize("advanced", [False, True])
def test_branch_fetch_fallback_never_accepts_a_different_commit(local_repository, monkeypatch, advanced):
    origin, job, selected = local_repository
    branch = git(origin, "symbolic-ref", "HEAD")
    if advanced:
        (origin / "code.txt").write_text("changed after selection", encoding="utf-8")
        git(origin, "add", "code.txt")
        git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "-m", "branch advanced")
    original = codex_run._fetch_repository_ref
    calls = []
    def fetch(executable, repository, ref):
        calls.append(ref)
        if ref == selected:
            raise RuntimeError("Server refuses direct object-ID fetch")
        original(executable, repository, ref)
    monkeypatch.setattr(codex_run, "_fetch_repository_ref", fetch)
    data = {"repository_url": str(origin), "git_ref_full": branch,
            "git_ref_kind": "branch", "git_commit": selected}
    if advanced:
        with pytest.raises(RuntimeError, match="другой commit"):
            codex_run.prepare_repository(job, data, "test")
        assert not (job / "revision.txt").exists() and not (job / "repository").exists()
    else:
        assert codex_run.prepare_repository(job, data, "test")[1] == selected
    assert calls == [selected, branch]


def test_sha256_repository_can_be_prepared(tmp_path):
    origin, job = tmp_path / "sha256-origin", tmp_path / "sha256-job"
    origin.mkdir()
    job.mkdir()
    git(origin, "init", "--object-format=sha256")
    (origin / "code.txt").write_text("SHA-256 repository", encoding="utf-8")
    git(origin, "add", "code.txt")
    git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-m", "fixture")
    selected = git(origin, "rev-parse", "HEAD")
    assert len(selected) == 64
    data = {"repository_url": str(origin), "git_ref": selected,
            "git_ref_kind": "commit", "git_commit": selected}
    assert codex_run.prepare_repository(job, data, "test")[1] == selected
