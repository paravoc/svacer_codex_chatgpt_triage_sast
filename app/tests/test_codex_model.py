"""Offline checks for per-job Codex model selection."""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as runner
from marker_history import append_batch_history


def test_model_picker_uses_visible_catalog_and_valid_identifiers(monkeypatch):
    requests = []

    def fake_request(method, params, **kwargs):
        requests.append((method, params))
        return {"data": [
            {"model": "gpt-6-astra", "displayName": "GPT-6-Astra", "hidden": False},
            {"model": "gpt-6-astra", "displayName": "Duplicate"},
            {"model": "private-hidden", "hidden": True},
            {"model": "--unsafe", "displayName": "Invalid"},
        ]}

    monkeypatch.setattr(runner, "_read_codex_app_server", fake_request)
    assert runner.read_codex_models() == [{"model": "gpt-6-astra", "display_name": "GPT-6-Astra"}]
    assert requests == [("model/list", {"limit": 100, "includeHidden": False})]
    assert runner.normalize_codex_model("") is None
    assert runner.normalize_codex_model(" gpt-5.6-sol ") == "gpt-5.6-sol"
    for invalid in ("--danger", "gpt 6", "x\n--sandbox danger", 123):
        with pytest.raises(ValueError):
            runner.normalize_codex_model(invalid)


def test_explicit_model_is_passed_only_to_that_codex_execution(tmp_path):
    default = runner.build_codex_command("codex", tmp_path, tmp_path / "last.txt")
    chosen = runner.build_codex_command("codex", tmp_path, tmp_path / "last.txt", "gpt-6-astra")
    assert "--model" not in default
    assert chosen[1:4] == ["exec", "--model", "gpt-6-astra"]
    assert chosen[4:] == default[2:]


def test_codex_execution_isolated_from_user_config_and_secret_environment(tmp_path):
    command = runner.build_codex_command("codex", tmp_path, tmp_path / "last.txt")
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--approve-for-me" in command
    assert "--ephemeral" not in command
    assert "--sandbox" not in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "project_doc_max_bytes=0" in command
    assert 'shell_environment_policy.inherit="core"' in command
    assert "shell_environment_policy.ignore_default_excludes=false" in command
    policy = next(value for value in command if value.startswith("shell_environment_policy.exclude="))
    assert "SVACER_*" in policy and "OPENAI_*" in policy and "CODEX_*" in policy


def test_marker_history_keeps_requested_model_without_claiming_default(tmp_path):
    (tmp_path / "job.json").write_text("{}", encoding="utf-8")
    (tmp_path / "decisions.jsonl").write_text(
        '{"marker_id":"m1","verdict":"False Positive"}\n', encoding="utf-8",
    )
    context = {
        "batch_number": 1,
        "codex_model": "gpt-6-astra",
        "batch": {"marker_ids": ["m1"], "assignments": [{"worker": 1, "marker_ids": ["m1"]}]},
    }
    rows = append_batch_history(
        tmp_path, context, launch_id="launch", runner_batch=1,
        started_at="2026-09-19T10:00:00+03:00", finished_at="2026-09-19T10:00:01+03:00",
        elapsed_seconds=1.0, exit_code=0, usage={}, agent_messages=[],
    )
    assert rows[0]["requested_model"] == "gpt-6-astra"


def test_unavailable_selected_model_has_actionable_error(tmp_path):
    (tmp_path / runner.ERROR_LOG).write_text("Error: model is not available\n", encoding="utf-8")
    assert "Выберите другую" in runner.classify_stop_reason(1, tmp_path)


def test_codex_turn_timeout_stops_only_its_process_tree(monkeypatch):
    class Process:
        pid = 12345
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("codex", timeout)

    stopped = []
    monkeypatch.setattr(runner, "_terminate_process_tree", lambda process: stopped.append(process.pid))
    with pytest.raises(runner.IncompleteAnalysisError, match="очередь и локальные черновики сохранены"):
        runner._wait_for_codex(Process(), timeout=60)
    assert stopped == [12345]
