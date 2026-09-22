"""Reviewed Svacer markers remain selectable, without trusting remote verdicts."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_manual_queue_ui import manual_window, table_ids, select_and_enqueue
import triage_gui_qt as ui
import triage_queue as q
from triage_dashboard import atomic_json, collect_state
from triage_gui import marker_matches_filter


@pytest.fixture
def reviewed_window(manual_window):
    window, job, run, ids, app = manual_window
    inventory_path = job / "markers.inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    reviews = ["Undecided", "False Positive", "Won't fix", "Confirmed", "Unclear"]
    for marker, review in zip(inventory["markers"], reviews):
        marker["review"] = {"status": review}
    inventory["markers"][2]["review"] = "Won't fix"
    atomic_json(inventory_path, inventory)
    # Simulate an old job: only unreviewed markers have local records.
    decisions = q.load_decisions(job / "decisions.jsonl", include_reviewed=False)
    q.atomic_write_jsonl(job / "decisions.jsonl", [d for d in decisions if d["marker_id"] not in ids[1:5]])
    atomic_json(job / "notes" / "batch-001-worker-1.json", [])
    atomic_json(job / "incomplete-analysis.json", {})
    window.marker_signature = None
    window.refresh()
    return window, job, run, ids, app


def test_all_reviews_visible_separate_from_local_results(reviewed_window):
    window, job, _, ids, _ = reviewed_window
    assert table_ids(window.marker_table) == ids
    assert window.marker_table.item(1, 0).text() == "False Positive · Svacer"
    assert window.marker_table.item(2, 0).text() == "Won't fix · Svacer"
    assert all(d["verdict"] is None for d in window.decisions)
    assert len(q.load_decisions(job / "decisions.jsonl", include_reviewed=False)) == 14
    assert window.state["completed"] == 0
    assert window.state["already_reviewed"] == 4
    assert window.state["total"] == 18
    assert window.jobs_table.horizontalHeaderItem(2).text() == "В Svacer"
    assert window.jobs_table.horizontalHeaderItem(3).text() == "Локально"
    assert "Локально проверено: 0 из 18" in window.progress_text.text()
    assert "в Svacer размечено 4 из 18" in window.scope.text()
    assert window.queue_table.rowCount() == 0
    for label, expected in (("Размечены в Svacer", ids[1:5]),
                            ("Не размечены в Svacer", [ids[0], *ids[5:]]),
                            ("False Positive", [ids[1]]), ("Won't fix", [ids[2]])):
        window.verdict_filter.setCurrentText(label)
        window.populate_markers()
        assert table_ids(window.marker_table) == expected
    window.current_marker_id = ids[2]
    window.render_marker()
    assert "Разметка Svacer при загрузке" in window.marker_detail.toPlainText()
    assert "не результат локальной перепроверки" in window.marker_detail.toPlainText()


def test_reviewed_selection_fifo_and_cancel_keep_remote_markup(reviewed_window, monkeypatch):
    window, job, _, ids, _ = reviewed_window
    original_inventory = (job / "markers.inventory.json").read_bytes()
    original_decisions = (job / "decisions.jsonl").read_bytes()
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.No)
    select_and_enqueue(window, [1, 2])
    assert not (job / "control.json").exists()
    assert (job / "decisions.jsonl").read_bytes() == original_decisions
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.Yes)
    select_and_enqueue(window, [1, 2])
    assert table_ids(window.queue_table) == ids[1:3]
    control = json.loads((job / "control.json").read_text(encoding="utf-8"))
    assert control["recheck_marker_ids"] == ids[1:3]
    assert control["pause_requested"] is True
    assert len(q.load_decisions(job / "decisions.jsonl", include_reviewed=False)) == 18
    control["pause_requested"] = False
    atomic_json(job / "control.json", control)
    claimed = q.claim_next_batch(job / "markers.inventory.json", job / "decisions.jsonl", 1, 1)
    assert claimed["batch"]["assignments"][0]["marker_ids"] == [ids[1]]
    assert (job / "markers.inventory.json").read_bytes() == original_inventory
    assert collect_state(job)["completed"] == 0


@pytest.mark.parametrize("workers", [1, 2])
def test_reviewed_fifo_finishes_only_selected_markers(reviewed_window, monkeypatch, workers):
    from test_queue_execution import claim, write_results
    import codex_run

    window, job, _, ids, _ = reviewed_window
    original = (job / "markers.inventory.json").read_bytes()
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.Yes)
    select_and_enqueue(window, [1, 2])
    control = json.loads((job / "control.json").read_text(encoding="utf-8"))
    control["pause_requested"] = False
    atomic_json(job / "control.json", control)
    seen = []
    for _ in range(2):
        ctx = claim(job, budget=workers, workers=workers)
        if not ctx.get("batch"):
            break
        seen.extend(mid for a in ctx["batch"]["assignments"] for mid in a["marker_ids"])
        write_results(job, ctx)
        codex_run.finalize_turn(job, ctx)
    assert seen == ids[1:3]
    assert claim(job, budget=workers, workers=workers).get("batch") is None
    assert (job / "markers.inventory.json").read_bytes() == original
    assert collect_state(job)["completed"] == 2


def test_new_template_includes_every_review_without_importing_verdict(reviewed_window, tmp_path):
    _, job, _, ids, _ = reviewed_window
    output = tmp_path / "new-decisions.jsonl"
    result = subprocess.run([sys.executable, str(Path(q.__file__).with_name("make_mcp_decisions_template.py")),
                             "--inventory", str(job / "markers.inventory.json"), "--out", str(output)],
                            capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    rows = q.load_decisions(output)
    assert [row["marker_id"] for row in rows] == ids
    assert all(row["verdict"] is None for row in rows)


def test_filter_uses_local_result_but_keeps_original_review_filter():
    assert marker_matches_filter("Confirmed", "m", "Confirmed", False, set(), "False Positive")
    assert not marker_matches_filter("False Positive", "m", "Confirmed", False, set(), "False Positive")
    assert marker_matches_filter("svacer_reviewed", "m", "Confirmed", False, set(), "False Positive")
    assert not marker_matches_filter("pending", "m", None, False, set(), "False Positive")


def test_old_job_migration_does_not_hide_missing_unreviewed_rows(reviewed_window):
    _, job, _, ids, _ = reviewed_window
    inventory = q.load_inventory(job / "markers.inventory.json")
    rows = q.load_decisions(job / "decisions.jsonl", include_reviewed=False)
    q.atomic_write_jsonl(job / "decisions.jsonl", [row for row in rows if row["marker_id"] != ids[0]])
    with pytest.raises(SystemExit, match="missing"):
        q.state(inventory, q.load_decisions(job / "decisions.jsonl"))


def test_connector_fetch_preserves_all_review_statuses():
    from test_connector import FakeAPI, make_service, run, P, B, S
    from triage_gui import MARKER_INVENTORY_FIELDS

    fake = FakeAPI()
    reviews = ["Undecided", "False Positive", "Won't fix", "Confirmed", "Unclear"]
    base = fake.markers[0]
    fake.markers = [{**base, "id": f"m{i}", "review": {"status": review}}
                    for i, review in enumerate(reviews)]
    result = run(make_service(fake).get_markers(P, B, S, advanced_filter=q.GOST_FILTER,
                  fields=MARKER_INVENTORY_FIELDS, limit=0))
    assert result["returned_count"] == 5 and result["truncated"] is False
    assert [q.marker_review_status(m) for m in result["markers"]] == reviews
    assert not result["filters_applied"].get("review")
    assert fake.sent == []


def test_remote_verdict_and_queue_label_are_independent(reviewed_window, monkeypatch):
    window, job, run, ids, _ = reviewed_window
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.Yes)
    select_and_enqueue(window, [1])
    assert window.marker_table.item(1, 0).text() == "False Positive · Svacer"
    assert window.marker_table.item(1, 1).text() == "В очереди"
    assert "перепроверка" in window.marker_table.item(1, 1).toolTip()
    atomic_json(job / "workers.status.json", {
        "state": "assigned", "batch": 1,
        "workers": [{"worker": 1, "status": "assigned", "marker_ids": [ids[1]], "assigned": 1}],
    })
    run.update(active=True, status="running", phase="analysis")
    window.refresh()
    assert window.marker_table.item(1, 0).text() == "False Positive · Svacer"
    assert window.marker_table.item(1, 1).text() == "В работе"
    assert not window.add_queue_button.isEnabled()
    assert window.add_queue_button.text() == "Уже в работе"
    window.verdict_filter.setCurrentText("False Positive")
    assert table_ids(window.marker_table) == [ids[1]]
    assert window.marker_table.item(0, 1).text() == "В работе"


def test_completed_local_recheck_stays_queued_until_assignment(reviewed_window, monkeypatch):
    window, job, _, ids, _ = reviewed_window
    decisions = q.load_decisions(job / "decisions.jsonl")
    decisions[1]["verdict"] = "Won't fix"
    q.atomic_write_jsonl(job / "decisions.jsonl", decisions)
    window.refresh()
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *_a: ui.QMessageBox.StandardButton.Yes)
    select_and_enqueue(window, [1])
    assert window.marker_table.item(1, 0).text() == "Won't fix"
    assert window.marker_table.item(1, 1).text() == "В очереди"
    window.marker_table.setCurrentCell(1, 0)
    assert window.current_marker_id == ids[1]
    assert window.triage_one_button.text() == "В очереди"
    assert not window.triage_one_button.isEnabled()
