import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as r
import dependency_sources as d
import triage_queue as q
from analysis_scope import build_only_result, apply_scope_review, SHIPPED_SCOPE, FULL_SCOPE
from decision_quality import review_result
from marker_history import append_batch_history, normalize_history_measurements
from parallel_analysis import run_workers
from test_dependency_sources import fixture
from test_queue_execution import claim


@pytest.fixture
def scope_job(tmp_path, monkeypatch):
    job, ctx, calls = fixture(tmp_path, monkeypatch, 'bazel_gazelle')
    repo = Path(ctx['repository'])
    recipe = repo / 'bazel/repository_locations.bzl'
    recipe.write_text(recipe.read_text().replace("version='v1'", "use_category=['build'], version='v1'"))
    for args in (['init', '-q'], ['add', 'bazel'],
                 ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                  '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture']):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                       **r.hidden_subprocess_kwargs())
    ctx['revision'] = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True,
                                              **r.hidden_subprocess_kwargs()).strip()
    ctx['review_contract_version'] = 1
    metadata = r.read_json(job / 'job.json')
    metadata.update(analysis_scope=SHIPPED_SCOPE, git_commit=ctx['revision'], snapshot_id=ctx['snapshot_id'])
    r.atomic_json(job / 'job.json', metadata)
    inventory = r.read_json(job / 'markers.inventory.json')
    inventory['markers'][0]['file'] = ctx['external_sources'][0]['file_path']
    marker = inventory['markers'][0]
    r.atomic_json(job / 'markers.inventory.json', inventory)
    decisions = q.load_decisions(job / 'decisions.jsonl')
    decisions[0]['file'] = marker['file']
    q.atomic_write_jsonl(job / 'decisions.jsonl', decisions)
    d.prepare_dependencies(job, ctx)
    return job, ctx, marker


def test_explicit_build_only_scope_has_no_invented_defect(scope_job):
    job, ctx, marker = scope_job
    row = build_only_result(job, ctx, marker)
    assert row['verdict'] == "Won't fix" and row['defect_scope'] == 'out_of_scope'
    assert row['component_defect_proven'] is None and row['product_defect_reachable'] is None
    assert not row['reachable_path'] and not row['proof_gaps']
    assert review_result(job, ctx, row) == []
    assert q.validate_worker_result(row, q.load_decisions(job / 'decisions.jsonl')[0]) == []


def test_scope_comment_is_publication_text_not_internal_policy(scope_job):
    job, ctx, marker = scope_job
    row = build_only_result(job, ctx, marker)
    assert row['comment'].startswith('bazel_gazelle v1 отнесён к зависимостям только для сборки')
    assert 'вне области проверки поставляемого продукта' in row['comment']
    assert 'bazel/repository_locations.bzl:' in row['comment']
    assert 'не утверждается' not in row['comment']
    assert 'выбранной пользователем' not in row['comment']
    assert 'безопасность самого инструмента' in row['disposition_reason']
    path = Path(ctx['repository']) / 'bazel/repository_locations.bzl'
    path.write_text(path.read_text().replace("version='v1'", "project_name='Gazelle', version='v1'"))
    named = build_only_result(job, ctx, marker)
    assert named['comment'].startswith('Gazelle v1 ')
    assert review_result(job, ctx, named) == []


def test_legacy_scope_comment_does_not_invalidate_completed_or_inflight_results(scope_job):
    job, ctx, marker = scope_job
    row = build_only_result(job, ctx, marker)
    row['comment'] = row['disposition_reason']
    assert review_result(job, ctx, row) == []
    row['scope_exclusion']['policy'] = FULL_SCOPE
    assert review_result(job, ctx, row)


@pytest.mark.parametrize('comment', ['', None, 'a' * 1801,
    'Компонент безопасен; срабатывание не требует исправления.'])
def test_scope_comment_change_does_not_bypass_validation(scope_job, comment):
    job, ctx, marker = scope_job
    row = {**build_only_result(job, ctx, marker), 'comment': comment}
    assert review_result(job, ctx, row)


def test_edit_scope_comment_preserves_evidence_queue_and_history(scope_job):
    job, ctx, marker = scope_job
    apply_scope_review(job, ctx, 'm00')
    decisions_path = job / 'decisions.jsonl'
    rows = q.load_decisions(decisions_path)
    saved = rows[0]
    comment = saved['comment']
    saved['comment'] = saved['disposition_reason']  # A result saved by an older worker.
    q.atomic_write_jsonl(decisions_path, rows)
    r.atomic_json(job / r.RUN_FILE, {'active': True, 'launch_id': 'other-markers'})
    r.atomic_json(job / 'workers.status.json', {'state': 'assigned', 'workers': [{'marker_ids': ['m01']}]})
    protected = {p: p.read_bytes() for p in (job / r.RUN_FILE, job / 'workers.status.json',
                                            job / 'control.json', job / 'marker-history.jsonl')}
    q.edit_saved_decision(job / 'markers.inventory.json', decisions_path, 'm00', comment)
    updated = q.load_decisions(decisions_path)
    assert updated[0] == {**saved, 'comment': comment}
    assert updated[1:] == rows[1:]
    assert review_result(job, ctx, updated[0]) == []
    assert all(p.read_bytes() == before for p, before in protected.items())


@pytest.mark.parametrize('scope', [None, FULL_SCOPE, 'unknown'])
def test_no_implicit_scope_change(scope_job, scope):
    job, ctx, marker = scope_job
    data = r.read_json(job / 'job.json')
    data['analysis_scope'] = scope
    if scope is None:
        del data['analysis_scope']
    r.atomic_json(job / 'job.json', data)
    assert build_only_result(job, ctx, marker) is None


@pytest.mark.parametrize('category', [['build', 'dataplane_core'], ['build', 'api'], ['test_only'], [], 'build'])
def test_mixed_or_unknown_categories_are_not_excluded(scope_job, category):
    job, ctx, marker = scope_job
    path = Path(ctx['repository']) / 'bazel/repository_locations.bzl'
    path.write_text(path.read_text().replace("['build']", repr(category)))
    assert build_only_result(job, ctx, marker) is None


@pytest.mark.parametrize('change', ['revision', 'snapshot', 'hash', 'source', 'conflict'])
def test_wrong_source_or_provenance_blocks_scope_closure(scope_job, change):
    job, ctx, marker = scope_job
    row = build_only_result(job, ctx, marker)
    dep = ctx['dependency_sources'][0]
    if change == 'revision':
        ctx['revision'] = 'other'
    elif change == 'snapshot':
        ctx['snapshot_id'] = 'other'
    elif change == 'hash':
        dep['archive_sha256'] = '0' * 64
    elif change == 'source':
        (job / dep['root'] / 'src/sink.c').write_text('changed();\n')
    else:
        dep['snapshot_conflicts'] = ['src/sink.c']
    assert build_only_result(job, ctx, marker) is None
    assert review_result(job, ctx, row)


@pytest.mark.parametrize('mutation', [
    {'component_defect_proven': True}, {'product_defect_reachable': False},
    {'verdict': 'False Positive'}, {'disposition_kind': 'inferred'}, {'decision_policy_version': 2},
])
def test_scope_policy_cannot_masquerade_as_technical_verdict(scope_job, mutation):
    job, ctx, marker = scope_job
    row = {**build_only_result(job, ctx, marker), **mutation}
    assert review_result(job, ctx, row)
    assert q.validate_worker_result(row, q.load_decisions(job / 'decisions.jsonl')[0])


def test_scope_worker_uses_zero_model_calls_and_preserves_queue(scope_job, monkeypatch):
    job, source_ctx, marker = scope_job
    q.atomic_write_json(job / 'control.json', {'priority_marker_ids': ['m00', 'm01'], 'manual_queue_requested': True})
    ctx = {**claim(job, budget=1), **source_ctx, 'launch_id': 'scope-test'}
    r.atomic_json(job / r.RUN_FILE, {'launch_id': 'scope-test', 'active': True})
    monkeypatch.setattr(r, 'find_codex_executable', lambda: pytest.fail('scope decision called Codex'))
    assert run_workers(r, job, job, 'scope-test', ctx, r.now_iso(), 1) == (0, 0)
    r.finalize_turn(job, ctx)
    decision = q.load_decisions(job / 'decisions.jsonl')[0]
    assert decision['verdict'] == "Won't fix" and decision['scope_exclusion']['policy'] == SHIPPED_SCOPE
    assert q.priority_marker_ids(job / 'decisions.jsonl') == ['m01']
    records = append_batch_history(job, ctx, launch_id='scope-test', runner_batch=1,
                                   started_at=r.now_iso(), finished_at=r.now_iso(), elapsed_seconds=1,
                                   exit_code=0, usage={}, agent_messages=[])
    assert records[0]['status'] == 'completed'
    assert records[0]['attributed_tokens'] == 0 and records[0]['tokens_exact']
    assert records[0]['execution_kind'] == 'scope_policy'
    assert normalize_history_measurements(records[0])['duration_seconds'] is not None
    assert records[0]['decision_snapshot']['scope_exclusion'] == decision['scope_exclusion']
    q.reopen(q.load_decisions(job / 'decisions.jsonl'), ['m00'], job / 'decisions.jsonl', {'m00'})
    assert 'scope_exclusion' not in q.load_decisions(job / 'decisions.jsonl')[0]


def test_revoking_scope_before_finalize_rejects_saved_exclusion(scope_job):
    job, ctx, marker = scope_job
    row = build_only_result(job, ctx, marker)
    data = r.read_json(job / 'job.json')
    data['analysis_scope'] = FULL_SCOPE
    r.atomic_json(job / 'job.json', data)
    assert review_result(job, ctx, row)


def test_local_scope_review_preserves_pause_other_markers_and_history(scope_job):
    job, ctx, marker = scope_job
    q.atomic_write_json(job / 'control.json', {'priority_marker_ids': ['m00', 'm01'],
        'deferred_marker_ids': ['m00', 'm01'], 'pause_requested': True})
    q.atomic_write_json(job / 'incomplete-analysis.json', {'m00': {'reason': 'old gap'}, 'm01': {'reason': 'keep'}})
    q.atomic_write_json(job / 'workers.status.json', {'state': 'assigned', 'workers': [{'marker_ids': ['m01']}]})
    previous_status = (job / 'workers.status.json').read_bytes()
    result = apply_scope_review(job, ctx, 'm00')
    assert result['verdict'] == "Won't fix" and result['model_calls'] == 0
    control = r.read_json(job / 'control.json')
    assert control['pause_requested'] is True
    assert control['priority_marker_ids'] == control['deferred_marker_ids'] == ['m01']
    assert r.read_json(job / 'incomplete-analysis.json') == {'m01': {'reason': 'keep'}}
    assert previous_status == (job / 'workers.status.json').read_bytes()
    assert (Path(result['audit']) / 'completed.json').is_file()
    from marker_history import load_marker_history, history_measurements
    record = load_marker_history(job)[0]
    assert record['verdict'] == "Won't fix" and record['status'] == 'completed'
    assert history_measurements(record)['tokens'] == 0
    assert record['decision_snapshot']['component_defect_proven'] is None
    with pytest.raises(ValueError, match='без сохранённого'):
        apply_scope_review(job, ctx, 'm00')


@pytest.mark.parametrize('blocked', ['reservation', 'import', 'changed_revision'])
def test_scope_review_cannot_disturb_reserved_imported_or_changed_job(scope_job, blocked):
    job, ctx, marker = scope_job
    before = (job / 'decisions.jsonl').read_bytes()
    if blocked == 'reservation':
        r.atomic_json(job / 'workers.status.json', {'state': 'assigned', 'workers': [{'marker_ids': ['m00']}]})
    elif blocked == 'import':
        r.atomic_json(job / 'svacer-import-attempt.json', {})
    else:
        (Path(ctx['repository']) / 'bazel/repositories.bzl').write_text('# changed')
    with pytest.raises(ValueError):
        apply_scope_review(job, ctx, 'm00')
    assert (job / 'decisions.jsonl').read_bytes() == before
    assert not (job / 'marker-history.jsonl').exists()
