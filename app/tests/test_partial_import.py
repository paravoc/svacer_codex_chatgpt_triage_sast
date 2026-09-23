"""Partial publication uses only fake HTTP; no live Svacer writes."""
import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import triage_queue as q
from import_selection import BATCHES, select_ready
from triage_dashboard import collect_state
from triage_connector.markup import MarkupImport
from triage_connector.client import ConnectorError
from test_connector import FakeAPI, make_job, make_service, run


class PartialAPI(FakeAPI):
    def handle(self, request):
        old = copy.deepcopy(self.exported)
        reply = super().handle(request)
        if request.url.path == "/api/public/markup/import" and not self.ignore_import:
            updated = {row["invariant"]: row for row in self.exported}
            self.exported = [updated.pop(row["invariant"], row) for row in old] + list(updated.values())
        return reply


def fixture(tmp_path, count=4):
    fake = PartialAPI()
    marker, exported = fake.markers[0], fake.exported[0]
    fake.markers = [{**copy.deepcopy(marker), "id": f"m{i}", "invariant": f"inv{i}", "line": i}
                    for i in range(1, count + 1)]
    fake.exported = [{**copy.deepcopy(exported), "invariant": f"inv{i}",
                      "locations": [{**exported["locations"][0], "line": i}]}
                     for i in range(1, count + 1)]
    job = make_job(tmp_path, fake)
    complete = q.load_decisions(job / "decisions.jsonl")
    rows = copy.deepcopy(complete)
    for row in rows[1:]:
        row["verdict"] = None
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    return fake, job, complete, MarkupImport(make_service(fake), tmp_path, "tester")


def test_partial_batches_are_repeatable_without_duplicates_or_queue_changes(tmp_path):
    fake, job, complete, imports = fixture(tmp_path)
    q.atomic_write_json(job / "control.json", {"priority_marker_ids": ["m2", "m3", "m4"]})
    before_queue = (job / "control.json").read_bytes()
    async def scenario():
        first = await imports.prepare(str(job))
        assert first["marker_ids"] == ["m1"] and first["selection"]["pending"] == 3
        assert collect_state(job)["import_ready"] == 1
        assert (await imports.apply(str(job), first["confirmation"]))["verification"]["verified"]
        assert not (job / "svacer-import-attempt.json").exists()
        assert collect_state(job)["import_ready"] == 0
        assert collect_state(job)["import_selection"]["already_sent"] == 1
        with pytest.raises(ConnectorError):
            await imports.apply(str(job), first["confirmation"])
        with pytest.raises(ConnectorError):
            await imports.prepare(str(job))
        rows = q.load_decisions(job / "decisions.jsonl")
        rows[1] = complete[1]
        q.atomic_write_jsonl(job / "decisions.jsonl", rows)
        second = await imports.prepare(str(job))
        assert second["marker_ids"] == ["m2"] and second["selection"]["already_sent"] == 1
        assert (await imports.apply(str(job), second["confirmation"]))["verification"]["verified"]
    run(scenario())
    assert [[row["invariant"] for row in payload] for payload in fake.sent] == [["inv1"], ["inv2"]]
    assert (job / "control.json").read_bytes() == before_queue
    assert q.load_decisions(job / "decisions.jsonl")[2]["verdict"] is None
    assert len(list((job / BATCHES).glob("*/receipt.json"))) == 2


def test_ready_subset_excludes_invalid_and_unverified_results(tmp_path):
    fake, job, complete, imports = fixture(tmp_path)
    rows = q.load_decisions(job / "decisions.jsonl")
    rows[2] = {**complete[2], "verdict": "Confirmed", "component_defect_proven": True,
               "product_defect_reachable": True, "defect_scope": "product", "severity": "Major",
               "action": "Fix required", "verification": {"status": "pending"}}
    rows[3] = {**complete[3], "decision_policy_version": None}
    q.atomic_write_jsonl(job / "decisions.jsonl", rows)
    preview = run(imports.prepare(str(job)))
    assert preview["marker_ids"] == ["m1"]
    assert preview["selection"]["needs_attention"] == 2
    assert "независимой" in preview["selection"]["skipped"]["m3"]
    assert not fake.sent


def test_background_completion_does_not_expand_or_invalidate_confirmed_selection(tmp_path):
    fake, job, complete, imports = fixture(tmp_path)
    q.atomic_write_json(job / "codex-run.json", {"active": True})
    async def scenario():
        preview = await imports.prepare(str(job))
        rows = q.load_decisions(job / "decisions.jsonl")
        rows[1] = complete[1]
        q.atomic_write_jsonl(job / "decisions.jsonl", rows)
        fake.exported[1]["comments"] = [{"text": "independent remote review"}]
        assert (await imports.apply(str(job), preview["confirmation"]))["verification"]["verified"]
        assert (await imports.prepare(str(job)))["marker_ids"] == ["m2"]
    run(scenario())
    assert [row["invariant"] for row in fake.sent[0]] == ["inv1"]
    assert json.loads((job / "codex-run.json").read_text())["active"]


def test_edit_after_preview_blocks_without_any_remote_write(tmp_path):
    fake, job, complete, imports = fixture(tmp_path)
    async def scenario():
        preview = await imports.prepare(str(job))
        rows = q.load_decisions(job / "decisions.jsonl")
        rows[0]["comment"] = "A different checked explanation."
        q.atomic_write_jsonl(job / "decisions.jsonl", rows)
        with pytest.raises(ConnectorError):
            await imports.apply(str(job), preview["confirmation"])
    run(scenario())
    assert not fake.sent


def test_same_invariant_pending_alias_is_not_silently_marked(tmp_path):
    fake, job, _, imports = fixture(tmp_path, count=2)
    inventory = json.loads((job / "markers.inventory.json").read_text())
    inventory["markers"][1]["invariant"] = "inv1"
    q.atomic_write_json(job / "markers.inventory.json", inventory)
    with pytest.raises(ConnectorError):
        run(imports.prepare(str(job)))
    assert not fake.requests


def test_revised_comment_can_be_sent_again_then_reverted_explicitly(tmp_path):
    fake, job, complete, imports = fixture(tmp_path, count=1)
    async def scenario():
        for comment in [complete[0]["comment"], "Rechecked: a different complete explanation.", complete[0]["comment"]]:
            row = {**complete[0], "comment": comment}
            q.atomic_write_jsonl(job / "decisions.jsonl", [row])
            preview = await imports.prepare(str(job))
            assert (await imports.apply(str(job), preview["confirmation"]))["verification"]["verified"]
    run(scenario())
    assert len(fake.sent) == 3


@pytest.mark.parametrize("mode", ["timeout", "ignored"])
def test_uncertain_partial_batch_blocks_future_sends_even_after_restart(tmp_path, mode):
    fake, job, complete, imports = fixture(tmp_path)
    fake.fail_import = mode == "timeout"
    fake.ignore_import = mode == "ignored"
    async def scenario():
        preview = await imports.prepare(str(job))
        assert not (await imports.apply(str(job), preview["confirmation"]))["verification"]["verified"]
        q.atomic_write_jsonl(job / "decisions.jsonl", complete)
        new_process = MarkupImport(make_service(fake), tmp_path, "tester")
        with pytest.raises(ConnectorError):
            await new_process.prepare(str(job))
    run(scenario())
    assert len(fake.sent) == 1 and collect_state(job)["import_blocked"]
    assert not list((job / BATCHES).glob("*/receipt.json"))


def test_crash_after_receipt_recovers_lock_without_resending(tmp_path):
    fake, job, complete, imports = fixture(tmp_path)
    async def scenario():
        preview = await imports.prepare(str(job))
        await imports.apply(str(job), preview["confirmation"])
        archived = json.loads((job / BATCHES / preview["nonce"] / "attempt.json").read_text())
        q.atomic_write_json(job / "svacer-import-attempt.json", archived)
        q.atomic_write_jsonl(job / "decisions.jsonl", complete)
        assert not collect_state(job)["import_blocked"]
        next_preview = await MarkupImport(make_service(fake), tmp_path, "tester").prepare(str(job))
        assert next_preview["marker_ids"] == ["m2", "m3", "m4"]
        assert not (job / "svacer-import-attempt.json").exists()
    run(scenario())
    assert len(fake.sent) == 1


def test_parallel_clients_can_only_publish_the_same_batch_once(tmp_path):
    fake, job, _, imports = fixture(tmp_path)
    async def scenario():
        preview = await imports.prepare(str(job))
        other = MarkupImport(make_service(fake), tmp_path, "tester")
        results = await asyncio.gather(imports.apply(str(job), preview["confirmation"]),
                                       other.apply(str(job), preview["confirmation"]), return_exceptions=True)
        assert sum(isinstance(row, dict) and row["verification"]["verified"] for row in results) == 1
    run(scenario())
    assert len(fake.sent) == 1


def test_malformed_receipt_blocks_instead_of_losing_duplicate_protection(tmp_path):
    fake, job, _, imports = fixture(tmp_path)
    path = job / BATCHES / "bad" / "receipt.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConnectorError):
        run(imports.prepare(str(job)))
    assert not fake.requests
