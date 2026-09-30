"""PoC generation contract with a fake Codex process, never an actual model call."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poc_generation as poc
from test_developer_issues import fixture


def generated_response(job, row):
    readme = "\n\n".join((
        f"## Finding\n{row['warnClass']} x.go:{row['line']}",
        f"## Source revision\n{job['git_commit']}",
        "## Preconditions\nLocal test only", "## Build and run\nRun the isolated test",
        "## Expected observation\nCheck the handler", "## Actual observation\nNot run",
        "## Control\nAn existing object", "## Limitations\nGenerated, not validated",
    ))
    return {"status": "generated", "summary": "A local test candidate", "missing_evidence": [],
            "limitations": ["not run"], "verification_plan": ["Build and run baseline/control"],
            "files": [{"path": "README.md", "content": readme},
                      {"path": "poc/test.go", "content": "package example\n// Synthetic test fixture\n"}]}


def test_generation_saves_unverified_files_and_exact_source_without_executing_them(tmp_path, monkeypatch):
    campaign, _, job, row = fixture(tmp_path)
    directory = campaign.parent / "job-1"
    response = generated_response(job, row)
    calls = []
    monkeypatch.setattr("codex_run.find_codex_executable", lambda: "fake-codex")
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        (Path(kwargs["cwd"]) / "answer.json").write_text(json.dumps(response), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(poc.subprocess, "run", fake_run)
    result = poc.generate_for_marker(directory, row["marker_id"])
    assert len(calls) == 1
    command, request = calls[0]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" not in command
    assert "use(p)" in request["input"]
    assert "snapshot-1" not in request["input"] and "marker-1" not in request["input"]
    assert result["status"] == "generated_unverified" and result["reproduction_status"] == "not_run"
    output = Path(result["directory"])
    assert (output / "poc" / "test.go").read_text() == response["files"][1]["content"]
    assert poc.existing_generations(directory, row["marker_id"]) == [output]
    changed = {**row, "comment": "updated decision"}
    (directory / "decisions.jsonl").write_text(json.dumps(changed) + "\n")
    assert poc.existing_generations(directory, row["marker_id"]) == []


def test_missing_evidence_is_saved_as_a_gap_and_contains_no_executable(tmp_path, monkeypatch):
    campaign, _, _, row = fixture(tmp_path)
    monkeypatch.setattr("codex_run.find_codex_executable", lambda: "fake-codex")
    response = {"status": "needs_evidence", "summary": "Missing caller", "missing_evidence": ["Inspect handler caller"],
                "limitations": [], "verification_plan": [], "files": []}
    def fake_run(*args, **kwargs):
        (Path(kwargs["cwd"]) / "answer.json").write_text(json.dumps(response))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(poc.subprocess, "run", fake_run)
    result = poc.generate_for_marker(campaign.parent / "job-1", row["marker_id"])
    assert result["status"] == "needs_evidence" and not result["files"]
    assert not (Path(result["directory"]) / "poc").exists()


@pytest.mark.parametrize("path", ["../outside.go", "poc/../../outside.go", "C:/outside.go", "poc/CON.go", "poc/run.ps1"])
def test_untrusted_model_cannot_choose_unsafe_file_names(tmp_path, path):
    _, _, job, row = fixture(tmp_path)
    response = generated_response(job, row)
    response["files"][1]["path"] = path
    with pytest.raises(ValueError):
        poc._validate_response(response, job=job, row=row)


def test_unverified_or_fabricated_source_never_reaches_codex(tmp_path, monkeypatch):
    campaign, _, _, row = fixture(tmp_path)
    directory = campaign.parent / "job-1"
    monkeypatch.setattr(poc.subprocess, "run", lambda *a, **k: pytest.fail("Invalid source reached Codex"))
    pending = {**row, "verification": {"status": "pending"}}
    (directory / "decisions.jsonl").write_text(json.dumps(pending) + "\n")
    with pytest.raises(ValueError, match="independently verified"):
        poc.generate_for_marker(directory, row["marker_id"])
    fabricated = copy.deepcopy(row)
    fabricated["source_evidence"][0]["excerpt"] = "invented source"
    (directory / "decisions.jsonl").write_text(json.dumps(fabricated) + "\n")
    with pytest.raises(ValueError, match="blocked"):
        poc.generate_for_marker(directory, row["marker_id"])


@pytest.mark.parametrize("changed_marker", ["selected", "other"])
def test_analysis_changes_are_checked_per_marker_before_saving_poc(tmp_path, monkeypatch, changed_marker):
    campaign, _, job, row = fixture(tmp_path)
    directory = campaign.parent / "job-1"
    monkeypatch.setattr("codex_run.find_codex_executable", lambda: "fake-codex")
    response = generated_response(job, row)
    def fake_run(*args, **kwargs):
        changed = {**row, "comment": "Updated by analysis"}
        if changed_marker == "other":
            changed["marker_id"] = "different-marker"
            rows = [row, changed]
        else:
            rows = [changed]
        (directory / "decisions.jsonl").write_text("".join(json.dumps(item) + "\n" for item in rows))
        (Path(kwargs["cwd"]) / "answer.json").write_text(json.dumps(response))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(poc.subprocess, "run", fake_run)
    if changed_marker == "selected":
        with pytest.raises(ValueError, match="изменились"):
            poc.generate_for_marker(directory, row["marker_id"])
        assert poc.existing_generations(directory, row["marker_id"]) == []
    else:
        result = poc.generate_for_marker(directory, row["marker_id"])
        assert result["status"] == "generated_unverified"
