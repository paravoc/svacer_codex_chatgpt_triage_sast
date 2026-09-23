"""Sharing violations must not truncate queue state or orphan a process."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import triage_queue as q


def denied():
    error = PermissionError("test sharing violation")
    error.winerror = 32
    return error


@pytest.mark.parametrize("reader", [q.load_decisions, q.read_state_text])
def test_state_read_retries_sharing_violation_without_returning_empty(tmp_path, monkeypatch, reader):
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"marker_id":"m", "comment":"Проверено"}\n', encoding="utf-8")
    original = Path.read_text
    attempts = []
    def busy(self, *args, **kwargs):
        if self == path:
            attempts.append(1)
            if len(attempts) < 4:
                raise denied()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", busy)
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    result = reader(path)
    assert len(attempts) == 4
    assert "Проверено" in str(result)


def test_permanent_read_error_is_bounded_not_an_empty_queue(tmp_path, monkeypatch):
    attempts = []
    def busy(*args, **kwargs):
        attempts.append(1)
        raise denied()
    monkeypatch.setattr(Path, "read_text", busy)
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        q.load_decisions(tmp_path / "decisions.jsonl")
    assert len(attempts) == 20


def test_missing_or_corrupt_state_is_not_retried_or_hidden(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(q.time, "sleep", sleeps.append)
    path = tmp_path / "decisions.jsonl"
    with pytest.raises(FileNotFoundError):
        q.load_decisions(path)
    path.write_text("not JSON", encoding="utf-8")
    with pytest.raises(SystemExit, match="JSONL"):
        q.load_decisions(path)
    assert not sleeps


@pytest.mark.parametrize("jsonl", [False, True])
def test_atomic_save_retries_transient_reader_lock(tmp_path, monkeypatch, jsonl):
    path = tmp_path / "state.json"
    path.write_text('{"old": true}')
    replace = q.os.replace
    attempts = []
    def busy(source, target):
        attempts.append(1)
        assert path.read_text() == '{"old": true}'
        if len(attempts) < 4:
            raise denied()
        replace(source, target)
    monkeypatch.setattr(q.os, "replace", busy)
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    (q.atomic_write_jsonl(path, [{"new": True}]) if jsonl else q.atomic_write_json(path, {"new": True}))
    assert len(attempts) == 4 and json.loads(path.read_text()) == {"new": True}


def test_permanent_write_error_is_bounded_and_preserves_old_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("old")
    attempts = []
    def busy(*args):
        attempts.append(1)
        raise denied()
    monkeypatch.setattr(q.os, "replace", busy)
    monkeypatch.setattr(q.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        q.atomic_write_json(path, {"new": True})
    assert len(attempts) == 20 and path.read_text() == "old"
    assert json.loads(next(tmp_path.glob("*.tmp")).read_text()) == {"new": True}
