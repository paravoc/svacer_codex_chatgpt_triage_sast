import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_quality as quality
import codex_run as runner
import triage_queue as q
from test_queue_execution import make_job, claim, result_for


@pytest.fixture
def proof(tmp_path):
    job = make_job(tmp_path)
    repo = job / "repository"
    repo.mkdir()
    (repo / "same.go").write_text("if p == nil { return }\nuse(p.value)\n", encoding="utf-8")
    context = {**claim(job), "review_contract_version": 1, "snapshot_id": "snapshot-A", "repository": str(repo)}
    for args in (["init", "-q"], ["add", "same.go"], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                                    "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=10,
                       **runner.hidden_subprocess_kwargs())
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                                       **runner.hidden_subprocess_kwargs()).strip()
    context["revision"] = revision
    row = result_for(job, "m00")
    row.update(review_contract_version=1, source_revision=revision,
               comment="На same.go:1 nil обрабатывается возвратом до чтения поля; опасное состояние исключено.",
               source_evidence=[{"file_path": "same.go", "line_start": 1, "line_end": 2,
                                 "excerpt": "if p == nil { return }\nuse(p.value)",
                                 "roles": sorted(quality.ROLES), "supports": "guard precedes read"}])
    return job, context, row


def test_proof_matches_source_and_comment(proof):
    assert quality.review_result(*proof) == []


def test_entrypoint_is_allowed_only_as_an_optional_evidence_role(proof):
    job, context, row = proof
    row["source_evidence"][0]["roles"].append("entrypoint")
    assert quality.review_result(job, context, row) == []

    row["source_evidence"][0]["roles"] = ["entrypoint"]
    errors = quality.review_result(job, context, row)
    assert any("evidence does not cover" in error for error in errors)


def test_unknown_evidence_role_has_actionable_feedback(proof):
    job, context, row = proof
    row["source_evidence"][0]["roles"].append("caller")
    errors = quality.review_result(job, context, row)
    assert any("unknown roles: caller" in error for error in errors)


def test_component_only_wontfix_passes_full_local_validation(tmp_path):
    job = make_job(tmp_path, count=1)
    repo = job / "repository"
    repo.mkdir()
    source = "package sample\ntype Config struct { Value int }\nfunc read(c *Config) int { return c.Value }\nfunc product() int { return read(&Config{Value: 1}) }\n"
    (repo / "same.go").write_text(source, encoding="utf-8")
    for args in (["init", "-q"], ["add", "same.go"],
                 ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=10,
                       **runner.hidden_subprocess_kwargs())
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                                       **runner.hidden_subprocess_kwargs()).strip()
    context = {**claim(job), "review_contract_version": 1, "repository": str(repo),
               "revision": revision}
    row = result_for(job, "m00", "Won't fix")
    row.update(decision_policy_version=2, component_defect_proven=True,
               product_defect_reachable=False, counterevidence=[],
               reachable_path=["read(nil) -> c.Value"],
               disposition_reason="Продукт передаёт ненулевой Config, а дефект ограничен прямым компонентным вызовом.",
               source_revision=revision, review_contract_version=1,
               comment="В same.go:3 read(nil) разыменует nil. Продуктовый вызов в same.go:4 передаёт ненулевой Config; дефект компонента не достигается продуктом.",
               source_evidence=[{"file_path": "same.go", "line_start": 1, "line_end": 4,
                                 "excerpt": source.strip(), "roles": sorted(quality.ROLES),
                                 "supports": "Путь компонента с nil и единственный продуктовый вызов"}])
    assert q.validate_worker_result(row, q.load_decisions(job / "decisions.jsonl")[0]) == []
    assert quality.review_result(job, context, row) == []
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [row])
    runner.finalize_turn(job, context)
    saved = q.load_decisions(job / "decisions.jsonl")[0]
    assert saved["verdict"] == "Won't fix"
    assert saved["component_defect_proven"] is True
    assert saved["product_defect_reachable"] is False


def test_comment_may_cite_any_verified_line_in_evidence_range(proof):
    job, ctx, row = proof
    row["comment"] = "На same.go:2 чтение происходит только после проверки nil; опасное состояние исключено."
    assert quality.review_result(job, ctx, row) == []
    row["comment"] = "На same.go:3 чтение происходит только после проверки nil; опасное состояние исключено."
    assert any("comment must cite" in error for error in quality.review_result(job, ctx, row))


@pytest.mark.parametrize("bad_comment", [
    "На same.go:1 не удалось найти исходники зависимости, поэтому дефект не доказан.",
    "На same.go:1 вероятно безопасно, но нужна проверка реального JSON provider.",
    "На same.go:1 данные не доказывают существование всех путей выполнения.",
    "На same.go:1 прямых вызовов не найдено; решение False Positive.",
    "At same.go:1 unable to verify the exact implementation of the dependency.",
    "short", "X" * 1801,
])
def test_incomplete_comment_cannot_become_final(proof, bad_comment):
    job, ctx, row = proof
    row["comment"] = bad_comment
    assert quality.review_result(job, ctx, row)


def test_negative_fact_about_code_is_not_censored(proof):
    job, ctx, row = proof
    row["comment"] = "На same.go:1 проверка отсутствует в вызывающей функции; источник передаёт nil до чтения поля."
    # This validator checks traceability, not truth; semantic review must reject an incorrect claim.
    assert not quality.UNRESOLVED.search(row["comment"])


@pytest.mark.parametrize("change", ["wrong_quote", "wrong_revision", "wrong_lines", "missing_roles", "bad_role_type", "outside", "missing_reference"])
def test_unverifiable_evidence_is_rejected(proof, change):
    job, ctx, row = proof
    ref = row["source_evidence"][0]
    if change == "wrong_quote": ref["excerpt"] = "invented guard"
    if change == "wrong_revision": row["source_revision"] = "other-commit"
    if change == "wrong_lines": ref["line_end"] = 3
    if change == "missing_roles": ref["roles"] = ["sink"]
    if change == "bad_role_type": ref["roles"] = [{}]
    if change == "outside": ref["file_path"] = "../outside.go"
    if change == "missing_reference": row["comment"] = "Nil обрабатывается возвратом до чтения поля; опасное состояние исключено."
    assert quality.review_result(job, ctx, row)


@pytest.mark.parametrize("path", [".env", ".env.local", ".ssh/id_rsa", "auth.json", "x/private.key", "../source.go", ".git/config"])
def test_sensitive_and_traversal_paths_are_not_read(path):
    assert not quality.safe_source_path(path)


def test_snapshot_mismatch_is_rejected_even_if_local_file_exists(proof):
    job, ctx, row = proof
    sources = job / "external-sources"
    sources.mkdir()
    q.atomic_write_json(sources / "source.json", {"snapshot_id": "wrong", "file_path": "same.go",
        "preview": {"content": "if p == nil { return }\nuse(p.value)", "line": 0, "total_lines": 2}})
    ctx["external_sources"] = [{"file_path": "same.go", "local_path": "external-sources/source.json"}]
    assert quality.review_result(job, ctx, row)


def test_quality_error_keeps_decisions_untouched(proof):
    job, ctx, row = proof
    row["source_evidence"][0]["excerpt"] = "invented text"
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [row])
    before = (job / "decisions.jsonl").read_bytes()
    with pytest.raises(runner.DecisionQualityError): runner.finalize_turn(job, ctx)
    assert (job / "decisions.jsonl").read_bytes() == before


def test_confirmed_action_schema_error_is_repairable(proof):
    job, ctx, row = proof
    row.update(verdict="Confirmed", defect_scope="product", severity="Major",
               action="Проверить указатель перед чтением")
    path = job / "notes" / "batch-001-worker-1.json"
    q.atomic_write_json(path, row)
    before = (job / "decisions.jsonl").read_bytes()
    with pytest.raises(runner.DecisionQualityError, match="Confirmed requires action"):
        runner.finalize_turn(job, ctx)
    assert (job / "decisions.jsonl").read_bytes() == before


def test_exactly_one_evidence_repair_is_allowed(proof):
    job, ctx, row = proof
    path = job / "notes" / "batch-001-worker-1.json"
    bad = copy.deepcopy(row)
    bad["source_evidence"][0]["excerpt"] = "wrong source"
    q.atomic_write_json(path, [bad])
    calls = []
    def repair(context):
        assert context["quality_feedback"]
        calls.append(1)
        q.atomic_write_json(path, [row])
    runner.finalize_with_quality_repair(job, job, ctx, repair)
    assert calls == [1]
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] == "False Positive"


def test_repeated_failed_repair_stops_without_loop(proof):
    job, ctx, row = proof
    row["source_evidence"][0]["excerpt"] = "wrong source"
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [row])
    calls = []
    with pytest.raises(runner.DecisionQualityError):
        runner.finalize_with_quality_repair(job, job, ctx, lambda _: calls.append(1))
    assert calls == [1]
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] is None


def test_real_source_text_normalizes_indentation_only(proof):
    job, ctx, row = proof
    row["source_evidence"][0]["excerpt"] = "  if p == nil { return }\r\n    use(p.value)"
    assert not quality.review_result(job, ctx, row)


def test_modified_checkout_cannot_supply_fake_proof(proof):
    job, ctx, row = proof
    (job / "repository" / "same.go").write_text("modified source", encoding="utf-8")
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [row])
    with pytest.raises(runner.IncompleteAnalysisError, match="Исходники изменились"):
        runner.finalize_turn(job, ctx)
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] is None


def test_source_catalog_reuses_only_matching_snapshot(proof):
    job, ctx, row = proof
    cache = job / "external-sources"
    cache.mkdir()
    for snapshot in ("snapshot-A", "snapshot-B"):
        q.atomic_write_json(cache / f"{snapshot}.json", {"snapshot_id": snapshot, "file_path": "dep.go",
            "preview": {"content": "package dep", "line": 0, "total_lines": 1}})
    assert runner.source_catalog(job, "snapshot-A") == [
        {"file_path": "dep.go", "local_path": str(Path("external-sources") / "snapshot-A.json")}]


def test_reopen_clears_previous_proof_contract(proof):
    job, ctx, row = proof
    rows = q.load_decisions(job / "decisions.jsonl")
    rows[0].update(row)
    q.reopen(rows, ["m00"], job / "decisions.jsonl", {"m00"})
    saved = q.load_decisions(job / "decisions.jsonl")[0]
    assert saved["verdict"] is None
    for field in ("review_contract_version", "source_revision", "source_evidence"):
        assert field not in saved


def test_untracked_source_is_not_revision_evidence(proof):
    job, ctx, row = proof
    (job / "repository" / "untracked.go").write_text("if p == nil { return }\nuse(p.value)\n", encoding="utf-8")
    row["source_evidence"][0]["file_path"] = "untracked.go"
    row["comment"] = row["comment"].replace("same.go", "untracked.go")
    q.atomic_write_json(job / "notes" / "batch-001-worker-1.json", [row])
    with pytest.raises(runner.DecisionQualityError, match="вне зафиксированной ревизии"):
        runner.finalize_turn(job, ctx)
    assert q.load_decisions(job / "decisions.jsonl")[0]["verdict"] is None
