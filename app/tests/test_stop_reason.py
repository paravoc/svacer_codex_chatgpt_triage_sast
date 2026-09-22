"""Offline regressions for execution failures, not SAST verdicts."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as runner


def events(job, *values):
    (job / runner.EVENT_LOG).write_text(
        "".join(json.dumps(value) + "\n" for value in values), encoding="utf-8",
    )


def test_terminal_403_wins_over_optional_mcp_and_html(tmp_path):
    (tmp_path / runner.ERROR_LOG).write_text(
        'ERROR rmcp::transport::worker failed: HTTP 403 optional MCP\n'
        'ERROR MCP server_name="chatcut" failed: invalid API key\n', encoding="utf-8",
    )
    events(tmp_path,
           {"type": "turn.started"},
           {"type": "turn.failed", "error": {"message":
               "unexpected status 403 Forbidden: <html>quota 429 mcp initialize failed</html>"}})
    reason = runner.classify_stop_reason(1, tmp_path)
    assert "HTTP 403" in reason
    assert "Очередь сохранена" in reason
    assert "Svacer" not in reason
    assert "лимита аккаунта" not in reason


def test_old_launch_and_agent_text_are_not_failure_evidence(tmp_path):
    (tmp_path / runner.ERROR_LOG).write_text(
        "[old] Запуск Codex, партия 1\nERROR usage limit\n"
        "[new] Запуск Codex, партия 1\n", encoding="utf-8",
    )
    events(tmp_path,
           {"type": "turn.failed", "error": {"message": "usage limit"}},
           {"type": "thread.started"}, {"type": "turn.started"},
           {"type": "item.completed", "item": {"type": "agent_message", "text":
               "Look for MCP error, model not found, quota, 429, network timeout"}},
           {"type": "turn.failed", "error": {"message": "unexpected termination"}})
    assert "завершился с кодом 1" in runner.classify_stop_reason(1, tmp_path)


@pytest.mark.parametrize("diagnostic, expected", [
    ('ERROR rmcp::transport failed server_name="svacer" initialize connection refused', "Svacer MCP"),
    ('ERROR rmcp::transport failed server_name="another" initialize connection refused', "кодом 1"),
    ('ERROR rmcp::transport failed HTTP 403 optional MCP', "кодом 1"),
    ('ERROR codex_api::endpoint::responses_websocket HTTP error: 403 Forbidden', "HTTP 403"),
    ('ERROR codex_models_manager::manager unexpected status 403 Forbidden', "HTTP 403"),
    ('ERROR unexpected status 429 Too Many Requests', "лимита аккаунта"),
    ('ERROR not logged in, run codex login', "авторизации"),
    ('ERROR connection reset', "ошибки сети"),
])
def test_failure_domains_are_distinct(tmp_path, diagnostic, expected):
    (tmp_path / runner.ERROR_LOG).write_text(diagnostic, encoding="utf-8")
    assert expected in runner.classify_stop_reason(1, tmp_path)


def test_stop_and_empty_logs_are_safe(tmp_path):
    assert "остановлен извне" in runner.classify_stop_reason(-1, tmp_path)
    assert "кодом 1" in runner.classify_stop_reason(1, tmp_path)


def test_tail_reads_only_bounded_suffix(tmp_path, monkeypatch):
    path = tmp_path / "large.log"
    path.write_bytes(b"x" * 100_000 + b"suffix")
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("Do not load the full log"))
    assert runner._tail(path, 6) == "suffix"
