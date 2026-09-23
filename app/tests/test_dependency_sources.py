"""Offline provenance, cache, patch and local-first source regressions."""
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dependency_sources as d
import codex_run as r
from decision_quality import source_text
from test_queue_execution import make_job, claim

NAME = "com_github_luajit_luajit"


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return output.getvalue()


def fixture(tmp_path, monkeypatch, name=NAME):
    job = make_job(tmp_path)
    repo = job / "repository"
    (repo / "bazel").mkdir(parents=True)
    content = archive({"dep/src/sink.c": b"sink();\n", "dep/src/provider.h": b"guard();\n"})
    (repo / "bazel/repository_locations.bzl").write_text(
        f"REPOSITORY_LOCATIONS_SPEC = dict({name}=dict(version='v1', sha256='{d.digest(content)}', "
        "urls=['https://github.com/owner/dep/archive/{version}.tar.gz'], strip_prefix='dep'))")
    (repo / "bazel/repositories.bzl").write_text(f"external_http_archive('{name}')")
    original = f"/build/external/{name}/src/sink.c"
    preview = job / "external-sources/sink.json"
    preview.parent.mkdir()
    d.atomic_write_json(preview, {"snapshot_id": "snapshot", "file_path": original,
                                "preview": {"content": "sink();\n", "line": 0, "total_lines": 1}})
    context = {"revision": "product-sha", "snapshot_id": "snapshot", "repository": str(repo),
               "external_sources": [{"file_path": original, "local_path": "external-sources/sink.json"}]}
    calls = []
    def download(url):
        calls.append(url)
        return content
    monkeypatch.setattr(d, "download", download)
    return job, context, calls


def test_prefetch_and_reuse_full_dependency_without_network(tmp_path, monkeypatch):
    job, context, calls = fixture(tmp_path, monkeypatch)
    d.prepare_dependencies(job, context)
    record = context["dependency_sources"][0]
    assert record["files"] == 2 and record["snapshot_matches"] and not record["snapshot_conflicts"]
    path = f"/build/external/{NAME}/src/provider.h"
    assert source_text(job, context, path) == "guard();\n"
    assert source_text(job, context, str(job / record["root"] / "src/provider.h")) == "guard();\n"
    d.prepare_dependencies(job, context)
    assert len(calls) == 1


@pytest.mark.parametrize("name", ["com_github_nghttp2_nghttp2", "other_pinned_library"])
def test_dependency_names_are_discovered_from_bazel_paths(tmp_path, monkeypatch, name):
    job, context, calls = fixture(tmp_path, monkeypatch, name)
    d.prepare_dependencies(job, context)
    assert not context["dependency_source_errors"]
    assert len(calls) == 1
    assert source_text(job, context, f"external/{name}/src/provider.h") == "guard();\n"
    root = job / context["dependency_sources"][0]["root"]
    assert source_text(job, context, str(root / "src/provider.h")) == "guard();\n"


def test_unknown_external_dependency_is_not_silently_ignored(tmp_path, monkeypatch):
    job, context, calls = fixture(tmp_path, monkeypatch)
    context["external_sources"][0]["file_path"] = "/build/external/not_pinned/code.c"
    d.prepare_dependencies(job, context)
    assert not calls
    assert context["dependency_source_errors"][0]["name"] == "not_pinned"
    assert "No pinned" in context["dependency_source_errors"][0]["error"]


def test_build_recipes_are_discovered_not_limited_to_known_libraries(tmp_path, monkeypatch):
    name = "com_github_nghttp2_nghttp2"
    job, context, _ = fixture(tmp_path, monkeypatch, name)
    repo = Path(context["repository"])
    (repo / "bazel/foreign_cc").mkdir()
    (repo / "bazel/foreign_cc/BUILD").write_text(f"# source @{name}//:all")
    (repo / "bazel/dep.BUILD").write_text("# product dependency rule")
    (repo / "bazel/repositories.bzl").write_text(
        f"external_http_archive('{name}', build_file='@envoy//bazel:dep.BUILD')")
    d.prepare_dependencies(job, context)
    paths = context["dependency_sources"][0]["product_recipe_paths"]
    assert str(repo / "bazel/foreign_cc/BUILD") in paths
    assert str(repo / "bazel/dep.BUILD") in paths


def test_archive_overrides_are_not_mistaken_for_the_pinned_archive(tmp_path, monkeypatch):
    job, context, calls = fixture(tmp_path, monkeypatch)
    (Path(context["repository"]) / "bazel/repositories.bzl").write_text(
        f"external_http_archive('{NAME}', strip_prefix='different')")
    d.prepare_dependencies(job, context)
    assert not calls
    assert "archive overrides" in context["dependency_source_errors"][0]["error"]


def test_wrong_archive_checksum_never_publishes_cache(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(d, "download", lambda _: b"corrupt")
    d.prepare_dependencies(job, context)
    assert not context["dependency_sources"]
    assert "SHA-256 mismatch" in context["dependency_source_errors"][0]["error"]
    assert not list((job / "dependency-sources").rglob("source-manifest.json"))


def test_snapshot_mismatch_disables_dependency_evidence(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    preview = job / "external-sources/sink.json"
    saved = json.loads(preview.read_text())
    saved["preview"]["content"] = "different();\n"
    d.atomic_write_json(preview, saved)
    d.prepare_dependencies(job, context)
    assert context["dependency_sources"][0]["snapshot_conflicts"]
    with pytest.raises(ValueError, match="not present"):
        source_text(job, context, f"external/{NAME}/src/provider.h")
    assert source_text(job, context, saved["file_path"]) == "different();\n"


@pytest.mark.parametrize("change", ["file", "manifest", "revision", "snapshot"])
def test_cache_tampering_or_wrong_provenance_cannot_supply_evidence(tmp_path, monkeypatch, change):
    job, context, _ = fixture(tmp_path, monkeypatch)
    d.prepare_dependencies(job, context)
    record = context["dependency_sources"][0]
    if change == "file":
        (job / record["root"] / "src/provider.h").write_text("changed")
    elif change == "manifest":
        (job / record["manifest_path"]).write_text("{}")
    else:
        context["revision" if change == "revision" else "snapshot_id"] = "other"
    with pytest.raises(ValueError):
        source_text(job, context, f"external/{NAME}/src/provider.h")


@pytest.mark.parametrize("path", ["../bad.c", "/absolute.c", "dep/../../bad.c", "C:/bad.c", "dep\\bad.c"])
def test_archive_paths_cannot_escape(path):
    with pytest.raises(ValueError):
        d.unpack(archive({path: b"x"}), "dep")


def test_links_and_secret_fixtures_are_not_read_or_extracted():
    assert d.unpack(archive({"dep/link": None, "dep/.env": b"test", "dep/cert.key": b"test",
                             "dep/code.c": b"code"}), "dep") == {"code.c": b"code"}


def test_archive_expansion_and_case_collisions_are_bounded(monkeypatch):
    with pytest.raises(ValueError):
        d.unpack(archive({"dep/A.c": b"a", "dep/a.c": b"b"}), "dep")
    monkeypatch.setattr(d, "MAX_CONTENT", 3)
    with pytest.raises(ValueError):
        d.unpack(archive({"dep/a.c": b"abcd"}), "dep")


def test_patches_require_exact_context_and_do_not_execute_code():
    files = {"code.c": b"one\ntwo\n"}
    patch = "--- a/code.c\n+++ b/code.c\n@@ -1,2 +1,2 @@\n one\n-two\n+changed\n"
    d.apply_patch_data(files, patch)
    assert files["code.c"] == b"one\nchanged\n"
    with pytest.raises(ValueError, match="context mismatch"):
        d.apply_patch_data(files, patch)
    d.apply_patch_data(files, "--- /dev/null\n+++ b/build.sh\n@@ -0,0 +1 @@\n+not-executed\n")
    assert files["build.sh"] == b"not-executed\n"


def test_gnu_patch_timestamps_empty_context_and_unique_exact_relocation():
    files = {"code.c": b"prefix\none\n\ntwo\n"}
    patch = ("--- a/code.c\t2020-11-23 08:59:08\n+++ b/code.c\t2021-01-15 17:15:43\n"
             "@@ -10,3 +10,3 @@\n one\n\n-two\n+changed\n")
    d.apply_patch_data(files, patch)
    assert files["code.c"] == b"prefix\none\n\nchanged\n"
    with pytest.raises(ValueError, match="context mismatch"):
        d.apply_patch_data({"code.c": b"one\n\ndifferent\n"}, patch)
    with pytest.raises(ValueError, match="ambiguous"):
        d.apply_patch_data({"code.c": b"one\n\ntwo\none\n\ntwo\n"}, patch)


@pytest.mark.parametrize("url", ["http://github.com/a", "https://evil.invalid/a", "https://github.com:999/a",
                                "https://user:secret@github.com/a"])
def test_download_origin_is_restricted(url):
    with pytest.raises(ValueError):
        d.checked_url(url)


def test_repeated_missing_path_does_not_block_new_local_build_file(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    context = claim(job)
    repo = job / "repository"
    (repo / "bazel/foreign_cc").mkdir(parents=True)
    (repo / "bazel/foreign_cc/BUILD").write_text("build recipe")
    context.update(repository=str(repo), requested_source_paths=["missing/CMakeLists.txt"])
    monkeypatch.delenv("SVACER_LOCAL_MCP_TOKEN", raising=False)
    monkeypatch.setattr(r, "fetch_snapshot_source", lambda *a: pytest.fail("No network for local source"))
    request = job / "notes/source-requests-001.json"
    d.atomic_write_json(request, [
        {"file_path": "missing/CMakeLists.txt", "reason": "old request"},
        {"file_path": "bazel/foreign_cc/BUILD", "reason": "actual product build"}])
    assert r.resolve_source_requests(job, tmp_path, context)
    assert context["source_request_resolutions"][0]["status"] == "pinned_repository"
    assert source_text(job, context, "bazel/foreign_cc/BUILD") == "build recipe"


def test_old_failed_request_can_now_use_new_dependency_cache(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    d.atomic_write_json(job / "job.json", {"snapshot_id": "snapshot"})
    d.prepare_dependencies(job, context)
    path = f"external/{NAME}/src/provider.h"
    context.update(batch_number=1, requested_source_paths=[path])
    monkeypatch.delenv("SVACER_LOCAL_MCP_TOKEN", raising=False)
    d.atomic_write_json(job / "notes/source-requests-001.json", [{"file_path": path, "reason": "guard"}])
    assert r.resolve_source_requests(job, tmp_path, context)
    assert context["source_request_resolutions"][0]["status"] == "pinned_dependency"


def test_repeated_available_source_gets_one_navigation_correction_not_a_fetch(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    path = context['external_sources'][0]['file_path']
    context.update(batch_number=1, batch={'marker_ids': ['m00'], 'assignments': []})
    d.atomic_write_json(job / 'job.json', {'snapshot_id': 'snapshot'})
    monkeypatch.delenv('SVACER_LOCAL_MCP_TOKEN', raising=False)
    monkeypatch.setattr(r, 'fetch_snapshot_source', lambda *args: pytest.fail('cached file fetched'))
    request = job / 'notes/source-requests-001.json'
    def ask():
        d.atomic_write_json(request, [{'file_path': path, 'reason': 'inspect the API contract'}])
        return r.resolve_source_requests(job, tmp_path, context)
    assert ask()
    assert ask()  # First repeat guides the worker to the readable local file.
    assert context['source_request_reuse_counts'] == {path: 1}
    assert context['available_source_requests'][0]['file_path'] == path
    prompt = r.build_runtime_prompt(job, tmp_path, context)
    assert 'УЖЕ доступны локально' in prompt and path in prompt
    assert 'snapshot_cache — точный исходник снимка' in prompt
    assert source_text(job, context, path) == 'sink();\n'
    with pytest.raises(r.IncompleteAnalysisError, match='Повторный запрос'):
        ask()  # No unlimited retries or fabricated conclusion.


def test_new_snapshot_dependency_is_prepared_during_source_round(tmp_path, monkeypatch):
    job, context, calls = fixture(tmp_path, monkeypatch, "com_github_nghttp2_nghttp2")
    path = context["external_sources"][0]["file_path"]
    context.update(batch_number=1, external_sources=[])
    d.atomic_write_json(job / "job.json", {"snapshot_id": "snapshot"})
    monkeypatch.setattr(r, "source_catalog", lambda *args: [{"file_path": path, "local_path": "external-sources/sink.json"}])
    d.atomic_write_json(job / "notes/source-requests-001.json", [{"file_path": path, "reason": "producer"}])
    monkeypatch.delenv("SVACER_LOCAL_MCP_TOKEN", raising=False)
    assert r.resolve_source_requests(job, tmp_path, context)
    assert len(calls) == 1
    assert not context["dependency_source_errors"]
    assert source_text(job, context, "external/com_github_nghttp2_nghttp2/src/provider.h") == "guard();\n"


def test_parallel_cache_instances_preserve_absolute_evidence_paths(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    d.prepare_dependencies(job, context)
    first = context["dependency_sources"][0]
    import shutil
    second = {**first, "root": first["root"] + "-other"}
    shutil.copytree(job / first["root"], job / second["root"])
    second["manifest_path"] = second["root"] + "/source-manifest.json"
    context["dependency_sources"].append(second)
    assert source_text(job, context, str(job / second["root"] / "src/provider.h")) == "guard();\n"


def test_generated_build_files_are_not_fabricated(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    d.prepare_dependencies(job, context)
    assert d.dependency_text(job, context, f"external/{NAME}/generated-config.h") is None


def test_long_upstream_paths_prepare_and_read_without_global_windows_settings(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    relative = '/'.join(['long-upstream-directory-name'] * 9) + '/provider.h'
    content = archive({'dep/src/sink.c': b'sink();\n', 'dep/' + relative: b'guard();\n'})
    recipe = Path(context['repository']) / 'bazel/repository_locations.bzl'
    previous = d.pinned_spec(Path(context['repository']), NAME)['sha256']
    recipe.write_text(recipe.read_text().replace(previous, d.digest(content)))
    monkeypatch.setattr(d, 'download', lambda _: content)
    d.prepare_dependencies(job, context)
    assert context['dependency_source_errors'] == []
    record = context['dependency_sources'][0]
    target = job / record['root'] / relative
    assert len(str(target)) > 260
    assert source_text(job, context, f'external/{NAME}/{relative}') == 'guard();\n'
    monkeypatch.setattr(d, 'download', lambda _: pytest.fail('cache downloaded again'))
    d.prepare_dependencies(job, context, allow_download=False)
    assert not context['dependency_source_errors']


def test_relative_cache_evidence_is_job_rooted_not_process_cwd(tmp_path, monkeypatch):
    job, context, _ = fixture(tmp_path, monkeypatch)
    d.prepare_dependencies(job, context)
    record = context['dependency_sources'][0]
    relative = record['root'] + '/src/provider.h'
    monkeypatch.chdir(tmp_path)
    assert source_text(job, context, relative) == 'guard();\n'
    (job / relative).write_text('tampered();\n')
    with pytest.raises(ValueError, match='changed'):
        source_text(job, context, relative)


def test_product_patch_is_applied_and_included_in_cache_identity(tmp_path, monkeypatch):
    job, context, calls = fixture(tmp_path, monkeypatch)
    repo = Path(context["repository"])
    (repo / "bazel/product.patch").write_text(
        "--- a/src/provider.h\n+++ b/src/provider.h\n@@ -1 +1 @@\n-guard();\n+product_guard();\n")
    (repo / "bazel/repositories.bzl").write_text(
        f"external_http_archive('{NAME}', patches=['@envoy//bazel:product.patch'], patch_args=['-p1'])")
    d.prepare_dependencies(job, context)
    assert not context["dependency_source_errors"]
    assert source_text(job, context, f"external/{NAME}/src/provider.h") == "product_guard();\n"
    first = context["dependency_sources"][0]["root"]
    (repo / "bazel/product.patch").write_text(
        "--- a/src/provider.h\n+++ b/src/provider.h\n@@ -1 +1 @@\n-guard();\n+new_guard();\n")
    d.prepare_dependencies(job, context)
    assert context["dependency_sources"][0]["root"] != first
    assert source_text(job, context, f"external/{NAME}/src/provider.h") == "new_guard();\n"
    assert len(calls) == 2


def test_custom_patch_commands_are_not_executed(tmp_path, monkeypatch):
    job, context, calls = fixture(tmp_path, monkeypatch)
    (Path(context["repository"]) / "bazel/repositories.bzl").write_text(
        f"external_http_archive('{NAME}', patch_cmds=['do not execute'])")
    d.prepare_dependencies(job, context)
    assert not calls and not context["dependency_sources"]
    assert "manual preparation" in context["dependency_source_errors"][0]["error"]


def test_cache_only_mode_refuses_missing_cache(tmp_path, monkeypatch):
    job, context, calls = fixture(tmp_path, monkeypatch)
    d.prepare_dependencies(job, context, allow_download=False)
    assert not calls and not context["dependency_sources"]
    assert "offline preparation" in context["dependency_source_errors"][0]["error"]


def test_local_and_missing_paths_do_not_require_auth_for_whole_request(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    context = claim(job)
    repo = job / "repository"
    repo.mkdir()
    (repo / "BUILD").write_text("local rule")
    context["repository"] = str(repo)
    monkeypatch.delenv("SVACER_LOCAL_MCP_TOKEN", raising=False)
    d.atomic_write_json(job / "notes/source-requests-001.json", [
        {"file_path": "missing-generated.h", "reason": "actual build defines"},
        {"file_path": "BUILD", "reason": "local rule"}])
    assert r.resolve_source_requests(job, tmp_path, context)
    assert context["source_request_resolutions"][0]["status"] == "pinned_repository"
    assert context["source_request_errors"] == [{"file_path": "missing-generated.h", "status": "connection_required"}]
