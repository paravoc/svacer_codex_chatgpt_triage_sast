"""History shows actual decisions and failures, never outcome-less attempts."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import marker_history as history


def test_history_hides_no_result_attempts_without_erasing_log(tmp_path):
    cases = (
        ("ready", "completed", "False Positive"),
        ("confirmed", "completed", "Confirmed"),
        ("unclear", "completed", "Unclear"),
        ("failed", "failed", None),
        ("incomplete", "incomplete", None),
        ("empty", "completed", None),
        ("interrupted", "interrupted", None),
        ("unrecognized", "completed", "Draft"),
    )
    path = tmp_path / history.HISTORY_FILE
    path.write_text("".join(json.dumps({
        "attempt_id": marker_id, "launch_id": "run", "runner_batch": index,
        "marker_id": marker_id, "status": status, "verdict": verdict,
        "batch_marker_count": 1, "batch_usage": {},
    }) + "\n" for index, (marker_id, status, verdict) in enumerate(cases)), encoding="utf-8")
    before = path.read_bytes()

    visible = history.load_marker_history(tmp_path)

    assert [row["marker_id"] for row in visible] == [
        "ready", "confirmed", "unclear", "failed", "incomplete",
    ]
    assert path.read_bytes() == before


def test_legacy_current_decision_is_not_attributed_to_old_attempt(tmp_path):
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps({"marker_id": "m0", "verdict": "Confirmed"}) + "\n", encoding="utf-8",
    )
    events = [
        {"type": "triage.run.started", "launch_id": "old"},
        {"type": "triage.batch.started", "batch": 1, "marker_ids": ["m0"]},
        {"type": "turn.completed", "usage": {"input_tokens": 50}},
    ]
    (tmp_path / history.EVENT_FILE).write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8",
    )
    assert history.load_marker_history(tmp_path) == []


def test_malformed_verdict_does_not_break_history_filter():
    assert not history.has_history_outcome({"status": "completed", "verdict": ["Confirmed"]})
    assert history.has_history_outcome({"status": "failed", "verdict": ["invalid"]})


def test_revalidation_uses_original_batch_measurements_and_shows_unchanged_decision():
    snapshot = {
        "verdict": "False Positive", "confidence": "high",
        "comment": "Guard excludes the dangerous state.",
        "source_evidence": [{"file_path": "same.go", "line_start": 1}],
    }
    previous = {
        "job_id": "job", "marker_id": "m0", "started_at": "2026-01-01T10:00:00+00:00",
        "status": "incomplete", "verdict": None, "decision_snapshot": snapshot,
        "batch_marker_count": 3, "worker_elapsed_seconds": 125.0,
        "batch_total_tokens": 900, "tokens_exact": False,
    }
    current = {
        "job_id": "job", "marker_id": "m0", "started_at": "2026-01-01T11:00:00+00:00",
        "status": "completed", "verdict": "False Positive", "decision_snapshot": snapshot,
        "launch_id": "run:revalidated", "batch_marker_count": 3,
        "worker_elapsed_seconds": 0.0, "batch_total_tokens": 0,
    }
    records = [current, previous]

    assert history.previous_history_attempt(records, 0) is previous
    comparison = history.compare_history_attempts(previous, current)
    assert comparison["decision_unchanged"] is True
    assert comparison["status_changed"] is True
    measurement = history.history_measurements(current, previous)
    assert measurement["duration_seconds"] == 125.0
    assert measurement["tokens"] == 900
    assert measurement["inherited_from_previous"] is True


def test_history_comparison_names_changed_decision_fields():
    previous = {"decision_snapshot": {"verdict": "Unclear", "comment": "Need caller."}}
    current = {"decision_snapshot": {"verdict": "Confirmed", "comment": "Caller reaches sink."}}
    comparison = history.compare_history_attempts(previous, current)
    assert comparison["decision_unchanged"] is False
    assert [change["label"] for change in comparison["changes"]] == ["Вердикт", "Комментарий Svacer"]
