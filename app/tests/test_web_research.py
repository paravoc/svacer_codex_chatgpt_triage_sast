"""Offline checks for explicit web enablement, privacy rules and continuation."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_run as r
import web_research as w
from decision_quality import source_text
from triage_gui import codex_activity_entries
from test_queue_execution import make_job, claim
from test_parallel_processes import setup_job, execute


def web_event(query="site:github.com/nghttp2/nghttp2 v1.66.0 upgrade", kind="item.completed"):
    return {"type": kind, "timestamp": "2026-09-23T12:00:00+03:00", "item": {
        "type": "web_search", "query": query, "action": {"type": "search", "queries": [query]}}}


def test_live_web_search_is_explicit_and_domain_filtered(tmp_path):
    command = r.build_codex_command("codex", tmp_path, tmp_path / "last.txt")
    assert 'web_search="live"' in command
    config = next(arg for arg in command if arg.startswith("tools.web_search.allowed_domains="))
    domains = json.loads(config.split("=", 1)[1])
    assert domains == list(w.PUBLIC_SOURCE_DOMAINS)
    assert "github.com" in domains and "pkg.go.dev" in domains
    assert not any("*" in domain or ":" in domain for domain in domains)
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--ignore-user-config" in command


@pytest.mark.parametrize("verification", [False, True])
def test_primary_and_verifier_prompts_allow_public_search_without_private_context(tmp_path, verification):
    job = make_job(tmp_path, count=1)
    ctx = {**claim(job), "verification_only": verification}
    prompt = r.build_runtime_prompt(job, tmp_path, ctx)
    assert "Поиск публичных исходников в интернете РАЗРЕШЁН" in prompt
    assert "НИКОГДА не передавай в поиск" in prompt
    assert "URL страницы не подменяет file_path" in prompt
    assert "не становится официальным" in prompt
    assert "не выполняй git clone/fetch, MCP, веб-поиск" not in prompt.lower()
    assert "не ищи исходники в интернете" not in prompt.lower()
    if verification:
        assert "верни challenged" in prompt
        assert "сохрани needs_context" not in prompt
    else:
        assert "запиши обычный source_request" in prompt


def test_navigation_cache_is_scoped_and_reused_not_source_evidence(tmp_path):
    job = make_job(tmp_path, count=1)
    ctx = {**claim(job), "snapshot_id": "snap", "revision": "rev"}
    mid = ctx["batch"]["marker_ids"][0]
    w.save_web_event(job, ctx, mid, web_event(kind="item.started"))
    path = w.research_cache_path(job, ctx, mid)
    assert not path.exists()
    w.save_web_event(job, ctx, mid, web_event())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["source_evidence"] is False
    assert "nghttp2" in saved["query"]
    assert json.dumps(str(path.resolve()), ensure_ascii=False) in w.public_research_prompt(job, ctx)
    assert w.research_cache_path(job, {**ctx, "revision": "other"}, mid) != path
    assert w.research_cache_path(job, {**ctx, "snapshot_id": "other"}, mid) != path
    assert w.research_cache_path(job, ctx, "other-marker") != path
    with pytest.raises((ValueError, OSError)):
        source_text(job, {**ctx, "repository": str(job / "repository")}, str(path))


def test_oversized_web_events_do_not_expand_navigation_cache(tmp_path):
    job = make_job(tmp_path, count=1)
    ctx = claim(job)
    mid = ctx["batch"]["marker_ids"][0]
    w.save_web_event(job, ctx, mid, web_event("a" * 20000))
    assert not w.research_cache_path(job, ctx, mid).exists()


def test_gui_shows_actual_web_events_but_not_command_chatter(tmp_path):
    job = make_job(tmp_path, count=1)
    events = [web_event(kind="item.started"), web_event(),
              {"type": "item.completed", "item": {"type": "command_execution", "command": "read source"}}]
    (job / r.EVENT_LOG).write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
    entries = codex_activity_entries(job)
    assert len(entries) == 1 and "Публичный веб-поиск" in entries[0] and "nghttp2" in entries[0]
    assert "Локальная проверка" not in entries[0]


def test_worker_web_events_are_saved_for_continuation(tmp_path, monkeypatch):
    job, ctx = setup_job(tmp_path, monkeypatch, "web")
    execute(job, ctx)
    for mid in ctx["batch"]["marker_ids"]:
        cache = w.research_cache_path(job, ctx, mid)
        assert json.loads(cache.read_text(encoding="utf-8"))["action"]["type"] == "search"
    assert len(list((job / "web-research").glob("*.jsonl"))) == 3


def test_web_discovery_resumes_with_exact_snapshot_source_and_navigation(tmp_path, monkeypatch):
    from parallel_analysis import read_worker_runtime

    job, ctx = setup_job(tmp_path, monkeypatch, "web_sources")
    ctx["snapshot_id"] = "exact-snapshot"
    (job / "external-sources").mkdir()
    r.atomic_json(job / "external-sources/include.json", {
        "snapshot_id": "exact-snapshot", "file_path": "include.h",
        "preview": {"content": "if (!value) return;\n", "line": 0, "total_lines": 1},
    })
    monkeypatch.setattr(r, "fetch_snapshot_source", lambda *args: pytest.fail("use exact cached source"))
    assert execute(job, ctx) == (0, 0)
    worker = next(row for row in read_worker_runtime(job)["workers"].values() if row["worker"] == 2)
    directory = (job / worker["event_log"]).parent
    saved = r.read_json(directory / "context.json")
    assert saved["source_request_round"] == 1
    assert saved["source_request_resolutions"] == [{
        "file_path": "include.h", "status": "snapshot_cache", "local_path": str(Path("external-sources/include.json")),
    }]
    assert source_text(job, saved, "include.h") == "if (!value) return;\n"
    cache = w.research_cache_path(job, ctx, saved["batch"]["marker_ids"][0])
    assert len(cache.read_text(encoding="utf-8").splitlines()) == 1
    assert json.dumps(str(cache.resolve())) in (directory / "prompt.txt").read_text(encoding="utf-8")
    assert len(list(directory.glob("prompt-*.txt"))) == 2
