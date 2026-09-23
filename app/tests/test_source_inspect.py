"""Source navigation without rg, shell cwd assumptions, credentials or network."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import source_inspect as s
from triage_queue import atomic_write_json


def product(tmp_path):
    job = tmp_path / "job with spaces"
    repo = job / "repository"
    repo.mkdir(parents=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).decode().strip()
    git("init", "--quiet")
    (repo / "producer.c").write_text("stream = create();\nif (!stream) return -1;\nreturn 0;\n")
    (repo / "caller.cc").write_text("if (producer() != 0) return -1;\nuse(stream);\n")
    git("add", "producer.c", "caller.cc")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--no-gpg-sign", "-m", "fixture")
    (repo / "untracked.c").write_text("not evidence")
    (repo / ".env").write_text("fixture-only")
    context = {"repository": str(repo), "revision": git("rev-parse", "HEAD"), "snapshot_id": "snap"}
    atomic_write_json(job / "context.json", context)
    return job, context


def test_search_and_read_do_not_depend_on_working_directory(tmp_path, monkeypatch):
    job, context = product(tmp_path)
    monkeypatch.chdir(tmp_path)
    index = s.SourceIndex(job, context)
    found = index.search("producer", "product", "*.cc")
    assert found["complete"] and found["matches"][0]["file_path"] == "caller.cc"
    assert found["matches"][0]["line"] == 1
    assert index.read("caller.cc", 1, 1)["lines"] == ["1: if (producer() != 0) return -1;"]
    evidence = index.read("producer.c", 1, 2, evidence=True)
    assert evidence == {"file_path": "producer.c", "line_start": 1, "line_end": 2,
                        "excerpt": "stream = create();\nif (!stream) return -1;"}


@pytest.mark.parametrize("path", [".env", "untracked.c", "../context.json", "auth.json"])
def test_helper_cannot_read_secrets_untracked_or_outside_files(tmp_path, path):
    job, context = product(tmp_path)
    index = s.SourceIndex(job, context)
    assert all(row["file_path"] not in {".env", "untracked.c"} for row in index.files()["files"])
    with pytest.raises(ValueError):
        index.read(path, 1, 1)


def test_snapshot_is_read_as_source_not_one_line_json(tmp_path):
    job, context = product(tmp_path)
    preview = job / "external-sources/preview.json"
    preview.parent.mkdir()
    path = "/build/external/lib/src/stream.c"
    atomic_write_json(preview, {"snapshot_id": "snap", "file_path": path,
        "preview": {"content": "create();\nguard();\nsink();\n", "line": 0, "total_lines": 3}})
    context["external_sources"] = [{"file_path": path, "local_path": "external-sources/preview.json"}]
    index = s.SourceIndex(job, context)
    assert index.search("guard", "snapshot")["matches"][0]["line"] == 2
    found = index.search("guard", "dependency", "stream.c")
    assert found['complete'] and found['matches'][0]['line'] == 2
    assert found['matches'][0]['scope'] == 'snapshot'
    assert index.files('dependency', 'stream.c')['total'] == 1
    assert index.files('product', 'stream.c')['total'] == 0
    assert index.read(path, 2, 2, evidence=True)["excerpt"] == "guard();"
    context["snapshot_id"] = "other"
    with pytest.raises(ValueError, match="another snapshot"):
        s.SourceIndex(job, context).read(path, 2, 2)
    invalid = s.SourceIndex(job, context).search('guard', 'dependency', 'stream.c')
    assert not invalid['complete'] and invalid['errors'] and not invalid['matches']


def test_dependency_search_finds_requested_header_without_archive(tmp_path):
    job, context = product(tmp_path)
    path = '/build/external/boringssl/include/openssl/x509.h'
    (job / 'external-sources').mkdir()
    atomic_write_json(job / 'external-sources/header.json', {
        'snapshot_id': 'snap', 'file_path': path,
        'preview': {'content': '// type must be GEN_*\nvoid GENERAL_NAME_set0_value();\n',
                    'line': 0, 'total_lines': 2}})
    context.update(source_catalog=[{'file_path': path, 'local_path': 'external-sources/header.json'}],
                   dependency_sources=[], dependency_source_errors=[{'name': 'boringssl', 'error': 'cache failed'}])
    result = s.SourceIndex(job, context).search('GENERAL_NAME_set0_value', 'dependency', 'x509.h')
    assert result['complete'] and result['files_scanned'] == 1
    assert result['matches'][0]['file_path'] == path
    assert result['matches'][0]['line'] == 2


def test_truncated_search_and_invalid_read_are_explicit(tmp_path, monkeypatch):
    job, context = product(tmp_path)
    index = s.SourceIndex(job, context)
    found = index.search("stream", limit=1)
    assert found["truncated"] and not found["complete"]
    monkeypatch.setattr(s, "MAX_SCAN_BYTES", 1)
    assert not index.search("missing")["complete"]
    with pytest.raises(ValueError):
        index.read("producer.c", 1, 100, evidence=True)
    with pytest.raises(ValueError, match="revision"):
        s.SourceIndex(job, {**context, "revision": "wrong"})


def test_cli_uses_absolute_paths_without_rg(tmp_path):
    job, _ = product(tmp_path)
    completed = subprocess.run([sys.executable, str(Path(s.__file__).resolve()), "--job", str(job),
        "--context", str(job / "context.json"), "search", "--scope", "product", "--term", "producer"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["matches"][0]["file_path"] == "caller.cc"


def test_short_file_read_clamps_to_eof_but_evidence_stays_exact(tmp_path):
    job, context = product(tmp_path)
    index = s.SourceIndex(job, context)
    result = index.read("producer.c", 1, 40)
    assert result["clamped_to_eof"] and result["line_end"] == 3
    assert len(result["lines"]) == 3
    with pytest.raises(ValueError, match="EOF"):
        index.read("producer.c", 1, 40, evidence=True)
    assert index.read("producer.c", 1, 3, evidence=True)["line_end"] == 3


def test_long_read_returns_bounded_page_and_next_line(tmp_path, monkeypatch):
    job, context = product(tmp_path)
    index = s.SourceIndex(job, context)
    monkeypatch.setattr(index, 'content', lambda path: '\n'.join(map(str, range(150))))
    first = index.read('producer.c', 1, 150)
    assert len(first['lines']) == 99 and first['next_line'] == 100
    assert first['truncated'] and not first['clamped_to_eof']
    rest = index.read('producer.c', first['next_line'], 150)
    assert len(rest['lines']) == 51 and not rest['truncated']


def test_basename_glob_matches_nested_files_and_empty_scope_is_not_complete(tmp_path):
    job, context = product(tmp_path)
    path = '/build/external/library/lib/session.c'
    preview = job/'external-sources/session.json'
    preview.parent.mkdir()
    atomic_write_json(preview, {'snapshot_id':'snap','file_path':path,
        'preview':{'content':'open_stream();\n','line':0,'total_lines':1}})
    context['external_sources']=[{'file_path':path,'local_path':'external-sources/session.json'}]
    index=s.SourceIndex(job,context)
    found=index.search('open_stream','snapshot','session.c')
    assert found['complete'] and found['matches'][0]['file_path']==path
    empty=index.search('open_stream','snapshot','missing.c')
    assert empty['files_matched']==0 and not empty['complete']
