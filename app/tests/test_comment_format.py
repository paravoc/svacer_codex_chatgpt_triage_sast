"""Offline plain-text comment regressions; no analysis or Svacer calls."""

import copy
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comment_format import svacer_comment_text
from triage_gui import comment_without_heading
import export_decisions_csv
import triage_queue as q
from test_queue_execution import make_job, result_for


RAW = "Метод `GetName()` проверяет `x != nil` ([plugin.pb.go:462](/root/.cache/bazel/external/protobuf/plugin.pb.go:462))."
PLAIN = "Метод GetName() проверяет x != nil (plugin.pb.go:462)."


@pytest.mark.parametrize("raw, expected", [
    (RAW, PLAIN),
    ("[src/a.cc:10–12](/root/build/src/a.cc:10-12)", "src/a.cc:10–12"),
    (r"[a.go:3](C:\build\a.go:3)", "a.go:3"),
    ("[`a.go:3`](/src/a.go:3)", "a.go:3"),
    ("[a.go:3](/src/other.go:3)", "[a.go:3](/src/other.go:3)"),
    ("[a.go:3](/src/a.go:30)", "[a.go:3](/src/a.go:30)"),
    ("[a.go:3](/src/a.go:3-5)", "[a.go:3](/src/a.go:3-5)"),
    ("[a.go:3](/src/not-a.go:3)", "[a.go:3](/src/not-a.go:3)"),
    ("[документация](https://example.test/api)", "[документация](https://example.test/api)"),
    ("[a.go:3](https://example.test/a.go:3)", "[a.go:3](https://example.test/a.go:3)"),
    ("[другая проверка](/src/a.go:3)", "[другая проверка](/src/a.go:3)"),
    ("![a.go:3](/src/a.go:3)", "![a.go:3](/src/a.go:3)"),
    ("Если `p == nil`, чтения нет.\nНужно проверить вызовы.",
     "Если p == nil, чтения нет.\nНужно проверить вызовы."),
    ("Одиночный символ ` и парные `` внутри строки.", "Одиночный символ ` и парные `` внутри строки."),
    ("Литерал ``foo `bar` baz`` сохраняется.", "Литерал ``foo `bar` baz`` сохраняется."),
    ("Комментарий без разметки; поле уже проверено.", "Комментарий без разметки; поле уже проверено."),
])
def test_comment_format_is_conservative_and_idempotent(raw, expected):
    assert svacer_comment_text(raw) == expected
    assert svacer_comment_text(expected) == expected


def test_normalization_changes_only_comment_and_keeps_raw_evidence():
    row = {"verdict": "False Positive", "comment": RAW,
           "reachable_path": [], "counterevidence": ["nil check"], "proof_gaps": [],
           "source_evidence": [{"file_path": "/src/plugin.pb.go", "excerpt": "`literal`"}]}
    before = copy.deepcopy(row)
    result = q.normalize_worker_result(row)
    assert row == before
    assert result == {**before, "comment": PLAIN}
    assert q.normalize_worker_result({"comment": None}) == {"comment": None}


def test_portable_manifest_includes_comment_formatter():
    manifest = (Path(__file__).resolve().parents[1] / "make_portable_package.ps1").read_text(encoding="utf-8-sig")
    assert "'comment_format.py'" in manifest


def test_display_and_copy_remove_markdown_but_keep_conclusion():
    assert comment_without_heading("FALSE POSITIVE\n" + RAW) == PLAIN


def test_worker_apply_stores_same_plain_text_as_display(tmp_path):
    job = make_job(tmp_path, count=1)
    raw = {**result_for(job, "m00"), "comment": RAW}
    q.apply_worker_result_rows(q.load_decisions(job / "decisions.jsonl"), [raw], ["m00"],
                               job / "decisions.jsonl", {"m00"})
    saved = q.load_decisions(job / "decisions.jsonl")[0]
    assert saved["comment"] == comment_without_heading(RAW) == PLAIN
    assert raw["comment"] == RAW
    assert saved["verdict"] == raw["verdict"]
    assert saved["evidence"] == raw["evidence"]


def test_local_editor_formats_text_without_changing_evidence_or_verdict(tmp_path):
    job = make_job(tmp_path, count=1)
    row = result_for(job, "m00")
    q.atomic_write_jsonl(job / "decisions.jsonl", [row])
    q.edit_saved_decision(job / "markers.inventory.json", job / "decisions.jsonl", "m00", RAW)
    assert q.load_decisions(job / "decisions.jsonl") == [{**row, "comment": PLAIN}]


def test_csv_uses_the_same_text_without_changing_saved_decisions(tmp_path, monkeypatch):
    source, output = tmp_path / "decisions.jsonl", tmp_path / "decisions.csv"
    q.atomic_write_jsonl(source, [{"marker_id": "m", "comment": RAW}])
    before = source.read_bytes()
    monkeypatch.setattr(sys, "argv", ["export", "--decisions", str(source), "--out", str(output)])
    assert export_decisions_csv.main() == 0
    with output.open(encoding="utf-8-sig", newline="") as stream:
        assert next(csv.DictReader(stream))["comment"] == PLAIN
    assert source.read_bytes() == before
