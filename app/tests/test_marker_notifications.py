"""Persistent in-window notifications; no analysis or network calls."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marker_notifications import dismiss_notification, dismiss_notifications, sync_notifications


def append_history(job: Path, **values) -> None:
    with (job / "marker-history.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(values) + "\n")


def test_success_is_persistent_until_clicked_and_old_history_is_not_flooded(tmp_path):
    append_history(tmp_path, attempt_id="old", marker_id="old-id", status="completed",
                   verdict="False Positive")
    assert sync_notifications(tmp_path) == []
    append_history(tmp_path, attempt_id="new", marker_id="marker-1", status="completed",
                   verdict="Confirmed", warnClass="NULL", file="src/check.go", line=42)
    pending = sync_notifications(tmp_path)
    assert len(pending) == 1
    assert pending[0]["tone"] == "green"
    assert pending[0]["title"] == "Маркер просканирован"
    assert "check.go:42" in pending[0]["subject"]
    assert sync_notifications(tmp_path) == pending  # neither polling nor reopening clears it
    dismiss_notification(tmp_path, pending[0]["id"])
    assert sync_notifications(tmp_path) == []


def test_incomplete_and_failed_attempts_have_distinct_tones(tmp_path):
    assert sync_notifications(tmp_path) == []
    append_history(tmp_path, attempt_id="draft", marker_id="m1", status="completed")
    append_history(tmp_path, attempt_id="needs", marker_id="m2", status="incomplete")
    append_history(tmp_path, attempt_id="error", marker_id="m3", status="failed")
    pending = sync_notifications(tmp_path)
    assert [(note["marker_id"], note["tone"]) for note in pending] == [
        ("m2", "yellow"), ("m3", "red"),
    ]


def test_dismiss_all_visible_keeps_later_notifications(tmp_path):
    assert sync_notifications(tmp_path) == []
    for index in range(2):
        append_history(tmp_path, attempt_id=f"old-{index}", marker_id=f"m{index}",
                       status="completed", verdict="False Positive")
    visible = sync_notifications(tmp_path)
    assert len(visible) == 2
    append_history(tmp_path, attempt_id="new", marker_id="m2",
                   status="completed", verdict="Confirmed")
    dismiss_notifications(tmp_path, [note["id"] for note in visible])
    assert [note["marker_id"] for note in sync_notifications(tmp_path)] == ["m2"]
    assert [note["marker_id"] for note in sync_notifications(tmp_path)] == ["m2"]


def test_preparation_failure_is_attributed_only_to_same_launch(tmp_path):
    assert sync_notifications(tmp_path) == []
    (tmp_path / "batch-context.json").write_text(json.dumps({
        "launch_id": "new-launch", "batch": {"marker_ids": ["m7"]},
    }), encoding="utf-8")
    (tmp_path / "codex-run.json").write_text(json.dumps({
        "launch_id": "new-launch", "status": "failed", "finished_at": "2026-09-19T12:00:00Z",
    }), encoding="utf-8")
    note = sync_notifications(tmp_path)[0]
    assert note["tone"] == "red"
    assert note["marker_id"] == "m7"
    dismiss_notification(tmp_path, note["id"])
    (tmp_path / "codex-run.json").write_text(json.dumps({
        "launch_id": "later-launch", "status": "failed",
    }), encoding="utf-8")
    later = sync_notifications(tmp_path)[0]
    assert later["marker_id"] == ""
    assert later["title"] == "Ошибка анализа"


def test_failed_run_does_not_duplicate_per_marker_history(tmp_path):
    assert sync_notifications(tmp_path) == []
    append_history(tmp_path, attempt_id="run-1:1:m1", launch_id="run-1", marker_id="m1",
                   status="failed", warnClass="NULL")
    append_history(tmp_path, attempt_id="run-1:1:m2", launch_id="run-1", marker_id="m2",
                   status="failed", warnClass="NULL")
    (tmp_path / "codex-run.json").write_text(json.dumps({
        "launch_id": "run-1", "status": "failed",
    }), encoding="utf-8")
    pending = sync_notifications(tmp_path)
    assert len(pending) == 2
    assert {note["marker_id"] for note in pending} == {"m1", "m2"}


def test_corrupt_notification_state_is_not_overwritten(tmp_path):
    path = tmp_path / "ui-notifications.json"
    path.write_text('{"schema_version": 9, "pending": []}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="повреждён"):
        sync_notifications(tmp_path)
    assert path.read_bytes() == before


def test_failed_notification_keeps_specific_reason_until_dismissed(tmp_path):
    assert sync_notifications(tmp_path) == []
    reason = "Сервис Codex отклонил подключение (HTTP 403). Очередь сохранена."
    append_history(tmp_path, attempt_id="run:1:m1", marker_id="m1", status="failed",
                   failure_reason=reason)
    pending = sync_notifications(tmp_path)
    assert pending[0]["detail"] == reason
    assert sync_notifications(tmp_path) == pending
    dismiss_notification(tmp_path, pending[0]["id"])
    assert sync_notifications(tmp_path) == []
